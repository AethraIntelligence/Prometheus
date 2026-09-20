"""Provenance, revision, uses and thread briefs on a real database.

The unit tests hold the rules; this holds the columns. Everything the phase
added to what is stored has to survive a round trip through SQL - on SQLite by
default and on PostgreSQL when `PROMETHEUS_TEST_POSTGRES_URL` is set - and the
retrieval evals have to pass against the real text index, not only against the
in-memory store's word overlap.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from application.memory.evals import render_report, run_retrieval_evals
from domain.conversations import session as rules
from domain.memory.models import (
    MemoryBasis,
    MemoryItem,
    MemoryKind,
    MemoryQuery,
    MemoryScope,
    MemoryStatus,
    Provenance,
    SourceKind,
)
from domain.memory.ranking import SUPERSEDED_RETENTION
from domain.memory.usage import MemoryUse
from domain.tasks.task import Task, TaskCreatedBy
from domain.workforce.protocols import Objective, Plan
from infrastructure.memory.sql import SqlMemory
from infrastructure.persistence.memory_use_repository import SqlMemoryUseLog
from infrastructure.persistence.objective_repository import SqlObjectiveRepository
from infrastructure.persistence.plan_repository import SqlPlanRepository
from infrastructure.persistence.session_repository import SqlSessionStateRepository
from infrastructure.persistence.task_repository import SqlTaskRepository

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


async def test_the_retrieval_evals_pass_on_the_real_index(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    report = await run_retrieval_evals(lambda: SqlMemory(session_factory), backend="sql")

    assert report.passed, render_report(report)


async def test_provenance_and_revision_survive_a_round_trip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    memory = SqlMemory(session_factory)
    replacement = uuid4()
    contested = uuid4()
    item = MemoryItem.create(
        "The report lives in reports/q2.md",
        scope=MemoryScope.WORKSPACE,
        kind=MemoryKind.SEMANTIC,
        basis=MemoryBasis.REPORTED,
        confidence=0.75,
        status=MemoryStatus.SUPERSEDED,
        superseded_by=replacement,
        revised_at=NOW,
        contradicts=(contested,),
        provenance=Provenance(
            kind=SourceKind.CONSOLIDATION, ref="r-1", label="eight outcomes",
            derived_from=("task-1", "task-2"),
        ),
    )  # fmt: skip
    await memory.remember(item)

    hidden = await memory.recall(MemoryQuery(text="report"))
    (stored,) = await memory.recall(
        MemoryQuery(text="report", include_superseded=True, ids=frozenset({item.id}))
    )

    assert hidden == []
    assert stored.basis is MemoryBasis.REPORTED
    assert stored.confidence == 0.75
    assert stored.status is MemoryStatus.SUPERSEDED
    assert stored.superseded_by == replacement
    assert stored.revised_at == NOW
    assert stored.contradicts == (contested,)
    assert stored.provenance == item.provenance


async def test_prune_drops_a_superseded_memory_only_after_its_retention(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    memory = SqlMemory(session_factory)

    def superseded(content: str, revised_at: datetime) -> MemoryItem:
        return replace(
            MemoryItem.create(content, scope=MemoryScope.WORKSPACE, kind=MemoryKind.SEMANTIC),
            status=MemoryStatus.SUPERSEDED,
            revised_at=revised_at,
        )

    recent = superseded("recently corrected", NOW - timedelta(days=1))
    old = superseded("corrected long ago", NOW - SUPERSEDED_RETENTION - timedelta(days=1))
    for item in (recent, old):
        await memory.remember(item)

    assert await memory.prune(now=NOW) == 1
    left = await memory.recall(MemoryQuery(include_superseded=True, as_of=NOW))
    assert [item.id for item in left] == [recent.id]


async def test_uses_are_recorded_and_read_back_by_objective_task_and_memory(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    log = SqlMemoryUseLog(session_factory)
    memory_id, objective_id, task_id = uuid4(), uuid4(), uuid4()
    await log.record(
        [
            MemoryUse(memory_id=memory_id, reason="It mentions report", objective_id=objective_id,
                      reader="manager", weight=0.4),
            MemoryUse(memory_id=memory_id, reason="It mentions q3", task_id=task_id,
                      reader="task", weight=0.2),
        ]
    )  # fmt: skip

    assert [use.reader for use in await log.for_objective(objective_id)] == ["manager"]
    assert [use.reason for use in await log.for_task(task_id)] == ["It mentions q3"]
    assert len(await log.for_memory(memory_id)) == 2


async def test_a_turn_counts_as_having_recalled_what_its_tasks_recalled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A task's use names the task; the thread asks about objectives.

    The window offers to explain a recollection only where there was one, and
    most recollection happens inside the tasks - so the two are related here,
    through the plan, rather than by the manager's own uses standing in for it.
    """
    log = SqlMemoryUseLog(session_factory)
    objective = Objective.create("Where are the invoices?")
    quiet = Objective.create("Hello")
    plan_id = uuid4()
    task = replace(
        Task.create("Find them", created_by=TaskCreatedBy.PROMETHEUS), plan_id=plan_id
    )
    await SqlObjectiveRepository(session_factory).save(objective)
    await SqlTaskRepository(session_factory).save(task)
    await SqlPlanRepository(session_factory).save(
        Plan(id=plan_id, objective_id=objective.id, tasks=(task,))
    )
    await log.record(
        [MemoryUse(memory_id=uuid4(), reason="It mentions invoices", task_id=task.id,
                   reader="task")]
    )  # fmt: skip

    assert await log.used_by([objective.id, quiet.id]) == {objective.id}
    assert await log.used_by([]) == set()


async def test_a_thread_brief_survives_a_restart(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = uuid4()
    turns = [
        replace(Objective.create(f"request {i}", conversation_id=conversation_id),
                created_at=NOW + timedelta(minutes=i))
        for i in range(3)
    ]  # fmt: skip
    state = rules.with_stage(
        None, conversation_id, turns, summary="three requests", goal_brief="the goal", now=NOW
    )
    state = rules.resolving(state, conversation_id, "the totals")

    await SqlSessionStateRepository(session_factory).save(state)
    # A second repository over the same database is what a restarted process has.
    read = await SqlSessionStateRepository(session_factory).get(conversation_id)

    assert read is not None
    assert read.goal_brief == "the goal"
    assert read.resolved_questions == ("the totals",)
    assert read.stages == state.stages
