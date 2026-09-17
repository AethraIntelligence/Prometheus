"""One application boundary for traces, health, metrics and explicit export."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from uuid import UUID

from domain.audit.integrity import AuditVerification
from domain.audit.protocols import AuditTrail
from domain.observability.metrics import RunSample, calculate
from domain.observability.models import RunKind, SpanKind, SpanStatus, TraceEvent, TraceView
from domain.observability.protocols import TraceRepository
from domain.secrets.models import redact
from domain.workspace.models import WorkspaceId

DEFAULT_WINDOW_DAYS = 30
DEFAULT_TRACE_RETENTION_DAYS = 30


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def event_view(event: TraceEvent) -> dict[str, Any]:
    return {
        "schema_version": event.schema_version,
        "event_id": str(event.event_id),
        "trace_id": str(event.trace_id),
        "span_id": str(event.span_id),
        "parent_id": str(event.parent_id) if event.parent_id else None,
        "correlation_id": event.correlation_id,
        "causation_id": str(event.causation_id) if event.causation_id else None,
        "workspace_id": str(event.workspace_id),
        "entity_type": event.entity_type,
        "entity_id": event.entity_id,
        "actor": event.actor,
        "kind": event.kind.value,
        "status": event.status.value,
        "name": event.name,
        "reason_code": event.reason_code,
        "started_at": _iso(event.started_at),
        "ended_at": _iso(event.ended_at),
        "duration_ms": event.duration_ms,
        "attributes": redact(event.attributes),
        "sequence": event.sequence,
    }


def trace_view(trace: TraceView) -> dict[str, Any]:
    failure = trace.first_failure
    return {
        "schema_version": trace.schema_version,
        "trace_id": str(trace.trace_id),
        "run_kind": trace.run_kind.value,
        "workspace_id": str(trace.workspace_id),
        "root": {"type": trace.root_entity_type, "id": trace.root_entity_id},
        "degraded": trace.degraded,
        "degradation_reason": redact(trace.degradation_reason),
        "first_failure_span_id": str(failure.span_id) if failure else None,
        "events": [event_view(event) for event in trace.events],
    }


class ObservabilityService:
    def __init__(
        self,
        traces: TraceRepository,
        audit: AuditTrail,
        *,
        health: Callable[[], dict[str, Any]],
        schema_revision: str = "unknown",
        feature_flags: Callable[[], dict[str, bool]] = dict,
        profile_fingerprints: Callable[[], list[str]] = list,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._traces = traces
        self._audit = audit
        self._health = health
        self._schema_revision = schema_revision
        self._feature_flags = feature_flags
        self._profile_fingerprints = profile_fingerprints
        self._now = now

    async def get(self, identifier: UUID) -> dict[str, Any] | None:
        found = await self._traces.get(identifier)
        return trace_view(found) if found else None

    async def recent(
        self,
        workspace_id: WorkspaceId,
        *,
        limit: int = 50,
        entity_type: str = "",
        entity_id: str = "",
    ) -> list[dict[str, Any]]:
        found = await self._traces.recent(
            workspace_id, limit=limit, entity_type=entity_type, entity_id=entity_id
        )
        return [trace_view(trace) for trace in found]

    async def prune(
        self, workspace_id: WorkspaceId, *, retention_days: int = DEFAULT_TRACE_RETENTION_DAYS
    ) -> int:
        if retention_days < 1:
            raise ValueError("Trace retention must be at least one day.")
        return await self._traces.prune(
            workspace_id, self._now() - timedelta(days=retention_days)
        )

    async def verify_audit(self, workspace_id: WorkspaceId) -> AuditVerification:
        return await self._audit.verify(workspace_id)

    async def health(self, workspace_id: WorkspaceId) -> dict[str, Any]:
        audit = await self.verify_audit(workspace_id)
        trace_health = self._traces.health()
        return redact(
            {
                **self._health(),
                "observability": asdict(trace_health),
                "audit": asdict(audit),
            }
        )

    async def metrics(
        self,
        workspace_id: WorkspaceId,
        *,
        days: int = DEFAULT_WINDOW_DAYS,
        run_kind: RunKind | None = None,
        model_profile: str = "",
    ) -> dict[str, Any]:
        end = self._now()
        start = end - timedelta(days=max(1, days))
        traces = await self._traces.recent(workspace_id, limit=5_000)
        samples = [self._sample(trace) for trace in traces if trace.events]
        snapshot = calculate(
            samples,
            workspace_id=workspace_id,
            window_start=start,
            window_end=end,
            run_kind=run_kind,
            model_profile=model_profile,
        )
        return asdict(snapshot)

    def _sample(self, trace: TraceView) -> RunSample:
        terminal_statuses = {
            SpanStatus.OK,
            SpanStatus.ERROR,
            SpanStatus.CANCELLED,
            SpanStatus.DENIED,
        }
        roots = [event for event in trace.events if event.parent_id is None]
        root = next(
            (event for event in reversed(roots) if event.status in terminal_statuses),
            roots[-1] if roots else trace.events[0],
        )
        terminal = root.status in terminal_statuses
        assignments = [
            event
            for event in trace.events
            if event.kind in {SpanKind.ASSIGNMENT, SpanKind.REASSIGNMENT}
        ]
        accepted_values = [event.attributes.get("accepted") for event in assignments]
        accepted = None if not accepted_values else all(value is True for value in accepted_values)
        models = [event for event in trace.events if event.kind is SpanKind.MODEL]
        approvals = [
            event
            for event in trace.events
            if event.kind is SpanKind.APPROVAL and event.entity_type == "approval"
        ]
        recoveries = [
            event
            for event in trace.events
            if event.kind
            in {SpanKind.RETRY, SpanKind.REPLAN, SpanKind.REASSIGNMENT, SpanKind.RESUME}
        ]
        return RunSample(
            kind=trace.run_kind,
            workspace_id=trace.workspace_id,
            started_at=root.started_at,
            terminal=terminal,
            succeeded=root.status is SpanStatus.OK,
            accepted=accepted,
            cancelled=root.status is SpanStatus.CANCELLED,
            latency_ms=root.duration_ms or 0,
            approval_wait_ms=sum(event.duration_ms or 0 for event in approvals),
            interventions=sum(
                event.ended_at is not None
                and event.actor not in {"", "timeout", "no-approver", "policy"}
                for event in approvals
            ),
            recoveries=len(recoveries),
            cost_usd=sum(float(event.attributes.get("cost_usd", 0.0)) for event in models),
            unsafe_actions=sum(
                int(bool(event.attributes.get("unsafe_without_approval")))
                for event in trace.events
            ),
            model_profile=next(
                (str(event.actor) for event in models if event.actor), ""
            ),
        )

    async def diagnostic_bundle(
        self, workspace_id: WorkspaceId, trace_ids: tuple[UUID, ...] = ()
    ) -> dict[str, Any]:
        selected = []
        for trace_id in trace_ids[:20]:
            trace = await self._traces.get(trace_id)
            if trace and trace.workspace_id == workspace_id:
                selected.append(trace_view(trace))
        try:
            release = version("prometheus-workforce")
        except PackageNotFoundError:
            release = "development"
        return redact(
            {
                "format": "prometheus-diagnostic-bundle",
                "version": 1,
                "created_at": _iso(self._now()),
                "application_version": release,
                "schema_revision": self._schema_revision,
                "workspace_id": str(workspace_id),
                "feature_flags": self._feature_flags(),
                "profile_fingerprints": self._profile_fingerprints(),
                "health": await self.health(workspace_id),
                "traces": selected,
                "excluded": [
                    "environment",
                    "master key",
                    "raw database",
                    "credentials",
                    "prompt and response content",
                    "file and screenshot content",
                ],
            }
        )

    async def export_bundle(
        self,
        path: Path,
        workspace_id: WorkspaceId,
        trace_ids: tuple[UUID, ...] = (),
    ) -> Path:
        """Write only after an explicit call, and never replace an existing file."""
        bundle = await self.diagnostic_bundle(workspace_id, trace_ids)
        path = path.expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as target:
            json.dump(bundle, target, indent=2, sort_keys=True)
            target.write("\n")
        return path
