"""Spend accounting against the same contract, in memory and on SQLite."""

from __future__ import annotations

from uuid import uuid4

import pytest

from domain.llm.models import Usage
from domain.llm.telemetry import LLMCallRecord
from infrastructure.persistence.llm_call_repository import InMemoryLLMCallLog, SqlLLMCallLog


@pytest.fixture(params=["in_memory", "sqlite"])
def call_log(request: pytest.FixtureRequest):
    if request.param == "in_memory":
        return InMemoryLLMCallLog()
    return SqlLLMCallLog(request.getfixturevalue("session_factory"))


def call(cost: float, *, task_id=None, success: bool = True) -> LLMCallRecord:
    return LLMCallRecord(
        provider="openrouter",
        model="vendor/medium",
        usage=Usage(prompt_tokens=100, output_tokens=20, cost_usd=cost, latency_ms=250),
        success=success,
        task_id=task_id,
    )


async def test_an_empty_log_totals_to_nothing(call_log) -> None:
    summary = await call_log.total()
    assert summary.calls == 0
    assert summary.cost_usd == 0.0


async def test_calls_accumulate(call_log) -> None:
    await call_log.record(call(0.01))
    await call_log.record(call(0.02))

    summary = await call_log.total()
    assert summary.calls == 2
    assert summary.prompt_tokens == 200
    assert summary.output_tokens == 40
    assert summary.cost_usd == pytest.approx(0.03)


async def test_spend_is_attributable_to_a_task(call_log) -> None:
    task_id = uuid4()
    await call_log.record(call(0.01, task_id=task_id))
    await call_log.record(call(0.05))

    assert (await call_log.total(task_id)).cost_usd == pytest.approx(0.01)
    assert (await call_log.total()).cost_usd == pytest.approx(0.06)


async def test_failed_calls_are_kept(call_log) -> None:
    await call_log.record(call(0.0, success=False))
    assert (await call_log.total()).calls == 1


async def test_why_a_model_ran_is_stored_with_the_call(session_factory) -> None:
    log = SqlLLMCallLog(session_factory)
    task_id = uuid4()
    await log.record(
        LLMCallRecord(
            provider="local",
            model="large",
            usage=Usage(prompt_tokens=1, output_tokens=1),
            success=True,
            task_id=task_id,
            task_kind="EXECUTION",
            entry="strong",
            reason="escalated from 'fast' (fast) to strong after step budget",
            escalation_level=1,
        )
    )

    (stored,) = await log.list_for_task(task_id)
    assert (stored.task_kind, stored.entry, stored.escalation_level) == ("EXECUTION", "strong", 1)
    assert stored.reason.startswith("escalated from 'fast'")
