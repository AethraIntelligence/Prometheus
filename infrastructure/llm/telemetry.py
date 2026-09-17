"""A decorator that prices every call and writes it down.

Wrapping rather than threading accounting through each adapter means a new
provider gets cost tracking for free, and cannot forget to report it.
"""

from __future__ import annotations

import time
from dataclasses import replace
from uuid import UUID

from domain.errors import ProviderError
from domain.llm import routing
from domain.llm.models import LLMRequest, LLMResponse, Usage
from domain.llm.protocols import LLM
from domain.llm.telemetry import LLMCallLog, LLMCallRecord
from infrastructure.llm.catalog import ModelCatalog
from infrastructure.observability.logging import get_logger

log = get_logger(__name__)


class MeteredLLM:
    """Implements `domain.llm.protocols.LLM`, wrapping another implementation."""

    def __init__(
        self,
        inner: LLM,
        *,
        provider: str,
        catalog: ModelCatalog,
        call_log: LLMCallLog | None = None,
        task_id: UUID | None = None,
    ) -> None:
        self._inner = inner
        self._provider = provider
        self._catalog = catalog
        self._call_log = call_log
        self._task_id = task_id

    def for_task(self, task_id: UUID) -> MeteredLLM:
        """A view of the same client that bills its calls to one task."""
        return MeteredLLM(
            self._inner,
            provider=self._provider,
            catalog=self._catalog,
            call_log=self._call_log,
            task_id=task_id,
        )

    async def generate(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()
        try:
            response = await self._inner.generate(request)
        except ProviderError as error:
            await self._write(
                LLMCallRecord(
                    provider=self._provider,
                    model=request.model or "unknown",
                    usage=self._elapsed_usage(started),
                    success=False,
                    task_id=self._task_id or routing.billed_task(),
                    error=f"{type(error).__name__}: {error}",
                    **_why(),
                )
            )
            raise

        priced = replace(response, usage=self._price(response))
        await self._write(
            LLMCallRecord(
                provider=self._provider,
                model=priced.model,
                usage=priced.usage,
                success=True,
                task_id=self._task_id or routing.billed_task(),
                **_why(),
            )
        )
        log.info(
            "llm.call",
            provider=self._provider,
            model=priced.model,
            prompt_tokens=priced.usage.prompt_tokens,
            output_tokens=priced.usage.output_tokens,
            cost_usd=priced.usage.cost_usd,
            latency_ms=priced.usage.latency_ms,
        )
        return priced

    def _price(self, response: LLMResponse) -> Usage:
        usage = response.usage
        entry = self._catalog.find(self._provider, response.model)
        if entry is None:
            # An unpriced model is reported at zero rather than guessed at. The
            # call still shows up in the log, so the gap is visible.
            log.warning("llm.unpriced_model", provider=self._provider, model=response.model)
            return usage
        return replace(
            usage, cost_usd=entry.cost_of(usage.prompt_tokens, usage.output_tokens)
        )

    def _elapsed_usage(self, started: float) -> Usage:
        return Usage(latency_ms=int((time.perf_counter() - started) * 1000))

    async def _write(self, record: LLMCallRecord) -> None:
        if self._call_log is None:
            return
        await self._call_log.record(record)


def _why() -> dict[str, object]:
    """The routing decision this call was made under, as call-record fields."""
    decision = routing.decision()
    if decision is None:
        return {}
    return {
        "task_kind": decision.task_kind,
        "entry": decision.entry,
        "reason": decision.reason,
        "escalation_level": decision.escalation_level,
    }
