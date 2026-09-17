"""Optional OpenTelemetry-compatible export with a bounded loss boundary."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from domain.observability.models import TraceEvent
from domain.observability.protocols import TraceHealth, TraceSink
from domain.secrets.models import redact
from infrastructure.observability.logging import get_logger

log = get_logger(__name__)

Export = Callable[[dict[str, Any]], Awaitable[None]]


def otlp_span(event: TraceEvent) -> dict[str, Any]:
    """Map the internal contract without discarding Prometheus-specific facts."""
    safe = event.sanitized()
    return {
        "traceId": safe.trace_id.hex,
        "spanId": safe.span_id.hex[:16],
        "parentSpanId": safe.parent_id.hex[:16] if safe.parent_id else "",
        "name": safe.name,
        "kind": safe.kind.value,
        "startTimeUnixNano": int(safe.started_at.timestamp() * 1_000_000_000),
        "endTimeUnixNano": (
            int(safe.ended_at.timestamp() * 1_000_000_000) if safe.ended_at else 0
        ),
        "status": {"code": safe.status.value},
        "attributes": {
            "gen_ai.operation.name": safe.kind.value.lower(),
            "prometheus.trace.schema_version": safe.schema_version,
            "prometheus.workspace.id": str(safe.workspace_id),
            "prometheus.entity.type": safe.entity_type,
            "prometheus.entity.id": safe.entity_id,
            "prometheus.reason_code": safe.reason_code,
            **{f"prometheus.{key}": value for key, value in safe.attributes.items()},
        },
    }


class BufferedTraceExporter(TraceSink):
    """Drop the oldest export on overflow; never wait in the execution path."""

    def __init__(self, export: Export, *, capacity: int = 256, name: str = "otlp-json") -> None:
        self._export = export
        self._queue: asyncio.Queue[TraceEvent] = asyncio.Queue(maxsize=max(1, capacity))
        self._name = name
        self._worker: asyncio.Task[None] | None = None
        self._dropped = 0
        self._last_error = ""
        self._closed = False

    async def emit(self, event: TraceEvent) -> None:
        if self._closed:
            self._dropped += 1
            return
        if self._worker is None:
            self._worker = asyncio.create_task(self._work())
        if self._queue.full():
            with suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
                self._queue.task_done()
            self._dropped += 1
        self._queue.put_nowait(event.sanitized())

    async def _work(self) -> None:
        while not self._closed or not self._queue.empty():
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=0.1)
            except TimeoutError:
                continue
            try:
                await self._export(otlp_span(event))
                self._last_error = ""
            except Exception as error:
                self._last_error = str(redact(f"{type(error).__name__}: {error}"))
                log.warning("trace.export_failed", exporter=self._name, error=self._last_error)
            finally:
                self._queue.task_done()

    def health(self) -> TraceHealth:
        return TraceHealth(
            available=not self._last_error,
            queued=self._queue.qsize(),
            dropped=self._dropped,
            last_error=self._last_error,
            exporter=self._name,
        )

    async def aclose(self) -> None:
        self._closed = True
        if self._worker is not None:
            await self._worker


class FanoutTraceSink(TraceSink):
    """Persist locally and offer export independently; neither may fail work."""

    def __init__(self, *sinks: TraceSink) -> None:
        self._sinks = sinks

    async def emit(self, event: TraceEvent) -> None:
        for sink in self._sinks:
            try:
                await sink.emit(event)
            except Exception as error:
                log.warning("trace.sink_failed", error=str(redact(error)))
