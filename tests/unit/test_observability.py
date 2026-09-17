"""The trace is useful evidence only while it is safe and mathematically honest."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from domain.observability.metrics import MIN_P95_SAMPLES, RunSample, calculate
from domain.observability.models import RunKind, SpanKind, SpanStatus, TraceEvent
from domain.secrets.models import MASK, redact, redact_text
from domain.workspace.models import DEFAULT_WORKSPACE_ID
from infrastructure.observability.exporter import BufferedTraceExporter


def test_free_text_and_nested_payloads_are_redacted_before_any_sink() -> None:
    canaries = (
        "sk-canary-123456789",
        "Bearer eyJhbGciOiJIUzI1NiJ9.canary.signature",
        "api_key=plain-canary-value",
        "Authorization: Basic-canary-token",
    )
    safe = redact(
        {
            "prompt": " ".join(canaries),
            "headers": {"authorization": canaries[1]},
            "exception": RuntimeError(canaries[2]),
            "tool": {"input": canaries[0], "secret_setting": "another-canary"},
        }
    )
    rendered = str(safe)
    assert "canary" not in rendered
    assert safe["headers"]["authorization"] == MASK
    assert safe["tool"]["secret_setting"] == MASK
    assert MASK in redact_text(canaries[0])


def test_a_trace_event_keeps_metadata_and_never_keeps_content() -> None:
    event = TraceEvent(
        trace_id=uuid4(),
        span_id=uuid4(),
        kind=SpanKind.TOOL,
        status=SpanStatus.ERROR,
        name="Bearer abcdefghijklmnop",
        attributes={"authorization": "token", "error": "api_key=top-secret-value"},
    ).sanitized()
    assert event.name == f"Bearer {MASK}"
    assert event.attributes == {"authorization": MASK, "error": f"api_key={MASK}"}


def test_metrics_keep_cancellation_out_of_success_and_withhold_small_p95() -> None:
    now = datetime.now(UTC)
    samples = [
        RunSample(
            kind=RunKind.TASK,
            workspace_id=DEFAULT_WORKSPACE_ID,
            started_at=now - timedelta(minutes=index),
            terminal=True,
            succeeded=index < 3,
            accepted=index < 2,
            cancelled=index == 5,
            latency_ms=(index + 1) * 100,
            interventions=int(index == 3),
            recoveries=int(index == 1),
            cost_usd=1.0,
        )
        for index in range(6)
    ]
    result = calculate(
        samples,
        workspace_id=DEFAULT_WORKSPACE_ID,
        window_start=now - timedelta(days=1),
        window_end=now,
        run_kind=RunKind.TASK,
    )
    assert result.success_rate.denominator == 5
    assert result.success_rate.value == 3 / 5
    assert result.accepted_result_rate.denominator == 5
    assert result.accepted_result_rate.value == 2 / 5
    assert result.latency_p95_ms.value is None
    assert result.latency_p95_ms.minimum_samples == MIN_P95_SAMPLES
    assert result.unsafe_action_count.value == 0


async def test_a_slow_or_broken_exporter_uses_bounded_drop_oldest_queue() -> None:
    exported: list[dict] = []

    async def broken(payload: dict) -> None:
        exported.append(payload)
        raise RuntimeError("Authorization: Bearer exporter-canary-token")

    exporter = BufferedTraceExporter(broken, capacity=1)
    trace_id = uuid4()
    for index in range(3):
        await exporter.emit(
            TraceEvent(
                trace_id=trace_id,
                span_id=uuid4(),
                kind=SpanKind.MODEL,
                status=SpanStatus.OK,
                name=f"call {index}",
            )
        )
    await exporter.aclose()

    health = exporter.health()
    assert health.dropped == 2
    assert health.available is False
    assert "canary" not in health.last_error
    assert len(exported) == 1
