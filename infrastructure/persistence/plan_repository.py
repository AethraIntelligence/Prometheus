"""Plan storage: the decomposition and its edges, saved together.

A plan row on its own is a revision number and a rationale. What makes it a plan
is which tasks it contains and which of them wait for which - so `save` writes
the edges in the same transaction, and `get` refuses to hand back a plan whose
tasks it could not read.

The immutable task definitions also live on the plan row. That is not a second
copy of live task state: it is the recovery manifest needed when a process dies
after planning but before delegation creates the first task row. On reads,
persisted task rows replace their definitions, so status, execution cursor and
result still have one authority. Unstarted definitions fill only the gaps.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.capabilities.models import Capability, CapabilityRequirement
from domain.errors import StorageError, StorageNotInitializedError
from domain.tasks.repository import TaskRepository
from domain.tasks.task import Task, TaskCreatedBy
from domain.workforce.protocols import Plan, PlanStatus
from domain.workforce.routing import Requirement
from domain.workspace.models import WorkspaceId
from infrastructure.persistence.dialect import upsert
from infrastructure.persistence.mappers import row_to_task
from infrastructure.persistence.models import PlanRow, PlanTaskDependencyRow, TaskRow
from infrastructure.persistence.session import session_scope


def _definition(plan: Plan) -> dict[str, object]:
    """The immutable part of a plan needed before task rows exist."""
    return {
        "tasks": [
            {
                "id": str(task.id),
                "workspace_id": str(task.workspace_id),
                "goal": task.goal,
                "created_by": task.created_by.value,
                "priority": task.priority,
                "parent_id": str(task.parent_id) if task.parent_id else None,
                "plan_id": str(task.plan_id) if task.plan_id else None,
                "created_at": task.created_at.isoformat(),
            }
            for task in plan.tasks
        ],
        "requirements": {
            str(task_id): {
                "required": sorted(item.value for item in requirement.capabilities.required),
                "preferred": sorted(item.value for item in requirement.capabilities.preferred),
                "min_context_tokens": requirement.capabilities.min_context_tokens,
                "min_quality": requirement.capabilities.min_quality,
                "services": sorted(requirement.services),
            }
            for task_id, requirement in plan.requirements.items()
        },
    }


def _from_definition(raw: dict) -> tuple[tuple[Task, ...], dict[UUID, Requirement]]:
    tasks: list[Task] = []
    for item in raw.get("tasks", ()):
        if not isinstance(item, dict):
            continue
        try:
            tasks.append(
                Task(
                    id=UUID(str(item["id"])),
                    workspace_id=WorkspaceId(str(item["workspace_id"])),
                    goal=str(item["goal"]),
                    created_by=TaskCreatedBy(str(item.get("created_by", "prometheus"))),
                    priority=int(item.get("priority", 5)),
                    parent_id=UUID(str(item["parent_id"])) if item.get("parent_id") else None,
                    plan_id=UUID(str(item["plan_id"])) if item.get("plan_id") else None,
                    created_at=datetime.fromisoformat(str(item["created_at"])),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue

    requirements: dict[UUID, Requirement] = {}
    known = {item.value: item for item in Capability}
    for task_id, item in (raw.get("requirements", {}) or {}).items():
        if not isinstance(item, dict):
            continue
        try:
            requirements[UUID(str(task_id))] = Requirement(
                capabilities=CapabilityRequirement(
                    required=frozenset(
                        known[value] for value in item.get("required", ()) if value in known
                    ),
                    preferred=frozenset(
                        known[value] for value in item.get("preferred", ()) if value in known
                    ),
                    min_context_tokens=(
                        int(item["min_context_tokens"])
                        if item.get("min_context_tokens") is not None
                        else None
                    ),
                    min_quality=float(item.get("min_quality", 0.0)),
                ),
                services=frozenset(str(value) for value in item.get("services", ())),
            )
        except (TypeError, ValueError):
            continue
    return tuple(tasks), requirements


class SqlPlanRepository:
    """Implements `domain.workforce.repository.PlanRepository`."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[AsyncSession]:
        try:
            async with session_scope(self._session_factory) as session:
                yield session
        except OperationalError as error:
            message = str(error.orig)
            if "no such table" in message or "unable to open database file" in message:
                raise StorageNotInitializedError(
                    "The local database has no schema yet."
                ) from error
            raise StorageError(message) from error

    async def save(self, plan: Plan) -> None:
        values = {
            "id": str(plan.id),
            "workspace_id": str(plan.workspace_id),
            "objective_id": str(plan.objective_id),
            "revision": plan.revision,
            "status": plan.status.value,
            "rationale": plan.rationale,
            "definition": _definition(plan),
        }
        async with self._session() as session:
            statement = upsert(session, PlanRow).values(**values)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[PlanRow.id],
                    set_={k: v for k, v in values.items() if k != "id"},
                )
            )
            # Edges are replaced wholesale rather than merged: a plan saved
            # twice has one set of dependencies, and a partial update would
            # leave an edge from a revision that no longer exists.
            await session.execute(
                delete(PlanTaskDependencyRow).where(
                    PlanTaskDependencyRow.plan_id == str(plan.id)
                )
            )
            known = {task.id for task in plan.tasks}
            for task_id, depends_on in plan.dependencies:
                # An edge to a task outside this plan cannot be satisfied and
                # would fail the foreign key anyway; dropping it here says so.
                if task_id in known and depends_on in known:
                    session.add(
                        PlanTaskDependencyRow(
                            plan_id=str(plan.id),
                            task_id=str(task_id),
                            depends_on=str(depends_on),
                        )
                    )

    async def get(self, plan_id: UUID) -> Plan | None:
        async with self._session() as session:
            row = await session.get(PlanRow, str(plan_id))
            if row is None:
                return None
            return await self._hydrate(session, row)

    async def for_objective(self, objective_id: UUID) -> list[Plan]:
        async with self._session() as session:
            rows = await session.scalars(
                select(PlanRow)
                .where(PlanRow.objective_id == str(objective_id))
                .order_by(PlanRow.revision.desc())
            )
            return [await self._hydrate(session, row) for row in rows]

    async def _hydrate(self, session: AsyncSession, row: PlanRow) -> Plan:
        rows = await session.scalars(
            select(TaskRow)
            .where(TaskRow.plan_id == row.id)
            # Plan order, not creation order: the planner sets a descending
            # priority so the first task it wrote reads first here too.
            .order_by(TaskRow.priority.desc(), TaskRow.created_at)
        )
        edges = await session.scalars(
            select(PlanTaskDependencyRow).where(PlanTaskDependencyRow.plan_id == row.id)
        )
        started = {task.id: task for task in (row_to_task(item) for item in rows)}
        planned, requirements = _from_definition(row.definition or {})
        tasks = tuple(started.get(task.id, task) for task in planned)
        if not tasks:
            # Plans written before definitions were durable still retain every
            # task that reached the task store.
            tasks = tuple(started.values())
        return Plan(
            id=UUID(row.id),
            objective_id=UUID(row.objective_id),
            tasks=tasks,
            dependencies=tuple(
                (UUID(edge.task_id), UUID(edge.depends_on)) for edge in edges
            ),
            revision=row.revision,
            status=PlanStatus(row.status),
            rationale=row.rationale,
            workspace_id=WorkspaceId(row.workspace_id),
            requirements=requirements,
        )


class InMemoryPlanRepository:
    """Implements `domain.workforce.repository.PlanRepository`.

    Give it the task repository and it behaves like the SQLite one: current
    task rows replace their immutable definitions while tasks not started yet
    remain available for recovery. Without it, the tasks are whatever was
    saved. The argument exists so the fake can be held to the same contract,
    which is the only thing that makes a paired test worth writing.
    """

    def __init__(self, tasks: TaskRepository | None = None) -> None:
        self._plans: dict[UUID, Plan] = {}
        self._tasks = tasks

    async def save(self, plan: Plan) -> None:
        self._plans[plan.id] = deepcopy(plan)

    async def get(self, plan_id: UUID) -> Plan | None:
        found = self._plans.get(plan_id)
        return await self._fresh(found) if found else None

    async def _fresh(self, plan: Plan) -> Plan:
        if self._tasks is None:
            return deepcopy(plan)
        tasks = []
        for task in plan.tasks:
            stored = await self._tasks.get(task.id)
            tasks.append(stored or task)
        return replace(deepcopy(plan), tasks=tuple(tasks))

    async def for_objective(self, objective_id: UUID) -> list[Plan]:
        return [
            await self._fresh(plan)
            for plan in sorted(
                self._plans.values(), key=lambda p: p.revision, reverse=True
            )
            if plan.objective_id == objective_id
        ]
