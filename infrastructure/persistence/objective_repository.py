"""Objective storage. SQL does not leave this package."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.errors import StorageError, StorageNotInitializedError
from domain.workforce.directions import ApprovalChoice, Directions
from domain.workforce.protocols import Objective, ObjectiveResult, ObjectiveStatus
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId
from infrastructure.persistence.dialect import upsert
from infrastructure.persistence.models import ObjectiveRow
from infrastructure.persistence.session import session_scope


def _to_row(objective: Objective) -> dict[str, object]:
    return {
        "id": str(objective.id),
        "workspace_id": str(objective.workspace_id),
        "conversation_id": (
            str(objective.conversation_id) if objective.conversation_id else None
        ),
        "text": objective.text,
        "constraints": objective.constraints,
        "acceptance_criteria": list(objective.acceptance_criteria),
        "directions": {
            "approvals": objective.directions.approvals.value,
            "model": objective.directions.model,
            "folder": objective.directions.folder,
        },
        "status": objective.status.value,
        "result": (
            {
                "summary": objective.result.summary,
                "output": objective.result.output,
                "missing": list(objective.result.missing),
                "cost_usd": objective.result.cost_usd,
            }
            if objective.result
            else None
        ),
        "created_at": objective.created_at,
        "finished_at": objective.finished_at,
    }


def _to_objective(row: ObjectiveRow) -> Objective:
    status = ObjectiveStatus(row.status)
    stored = row.result or None
    return Objective(
        id=UUID(row.id),
        text=row.text,
        workspace_id=WorkspaceId(row.workspace_id),
        conversation_id=UUID(row.conversation_id) if row.conversation_id else None,
        constraints=row.constraints or {},
        acceptance_criteria=tuple(row.acceptance_criteria or ()),
        directions=_directions(row.directions or {}),
        status=status,
        result=(
            ObjectiveResult(
                objective_id=UUID(row.id),
                summary=stored.get("summary", ""),
                status=status,
                output=stored.get("output", {}),
                missing=tuple(stored.get("missing", ())),
                cost_usd=float(stored.get("cost_usd", 0.0)),
            )
            if stored
            else None
        ),
        created_at=_aware(row.created_at),
        finished_at=_aware(row.finished_at) if row.finished_at else None,
    )


def _directions(stored: dict) -> Directions:
    try:
        approvals = ApprovalChoice(str(stored.get("approvals") or "ASK"))
    except ValueError:
        approvals = ApprovalChoice.ASK
    return Directions(
        approvals=approvals,
        model=str(stored.get("model") or ""),
        folder=str(stored.get("folder") or ""),
    )


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class SqlObjectiveRepository:
    """Implements `domain.workforce.repository.ObjectiveRepository`."""

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

    async def save(self, objective: Objective) -> None:
        values = _to_row(objective)
        async with self._session() as session:
            # Upsert: an objective is written when it arrives and again at every
            # stage, under the id it already has.
            statement = upsert(session, ObjectiveRow).values(**values)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[ObjectiveRow.id],
                    set_={k: v for k, v in values.items() if k not in ("id", "created_at")},
                )
            )

    async def get(self, objective_id: UUID) -> Objective | None:
        async with self._session() as session:
            row = await session.get(ObjectiveRow, str(objective_id))
            return _to_objective(row) if row else None

    async def list_recent(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID, *, limit: int = 50
    ) -> list[Objective]:
        async with self._session() as session:
            rows = await session.scalars(
                select(ObjectiveRow)
                .where(ObjectiveRow.workspace_id == str(workspace_id))
                .order_by(ObjectiveRow.created_at.desc(), ObjectiveRow.id.desc())
                .limit(limit)
            )
            return [_to_objective(row) for row in rows]

    async def for_conversation(self, conversation_id: UUID) -> list[Objective]:
        async with self._session() as session:
            rows = await session.scalars(
                select(ObjectiveRow)
                .where(ObjectiveRow.conversation_id == str(conversation_id))
                .order_by(ObjectiveRow.created_at, ObjectiveRow.id)
            )
            return [_to_objective(row) for row in rows]


class InMemoryObjectiveRepository:
    """Implements `domain.workforce.repository.ObjectiveRepository`."""

    def __init__(self) -> None:
        self._objectives: dict[UUID, Objective] = {}

    async def save(self, objective: Objective) -> None:
        self._objectives[objective.id] = deepcopy(objective)

    async def get(self, objective_id: UUID) -> Objective | None:
        found = self._objectives.get(objective_id)
        return deepcopy(found) if found else None

    async def list_recent(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID, *, limit: int = 50
    ) -> list[Objective]:
        newest = sorted(self._objectives.values(), key=lambda o: o.created_at, reverse=True)
        return [deepcopy(o) for o in newest if o.workspace_id == workspace_id][:limit]

    async def for_conversation(self, conversation_id: UUID) -> list[Objective]:
        thread = [o for o in self._objectives.values() if o.conversation_id == conversation_id]
        return [deepcopy(o) for o in sorted(thread, key=lambda o: o.created_at)]
