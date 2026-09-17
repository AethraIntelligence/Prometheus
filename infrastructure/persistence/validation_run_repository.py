"""Validation run storage. SQL does not leave this package."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.errors import StorageError, StorageNotInitializedError
from domain.validation.evidence import Metrics
from domain.validation.failures import FailureKind
from domain.validation.profile import RoutingProfile
from domain.validation.run import RunStatus, ValidationRun
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId
from infrastructure.persistence.dialect import upsert
from infrastructure.persistence.models import ValidationRunRow
from infrastructure.persistence.session import session_scope


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _to_values(run: ValidationRun) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "workspace_id": str(run.workspace_id),
        "scenario": run.scenario,
        "status": run.status.value,
        "failure": run.failure.value,
        "result": run.to_result(),
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


def _to_run(row: ValidationRunRow) -> ValidationRun:
    result = row.result or {}
    return ValidationRun(
        id=UUID(row.id),
        scenario=row.scenario,
        status=RunStatus(row.status),
        # Read leniently: a run recorded under a category this build has since
        # renamed is still evidence, and losing it to a ValueError would be the
        # storage layer editing history.
        failure=_failure(row.failure),
        summary=str(result.get("summary", "")),
        note=str(result.get("note", "")),
        checks=ValidationRun.checks_from(result.get("checks", [])),
        metrics=Metrics.from_dict(result.get("metrics", {})),
        profile=RoutingProfile.from_dict(result.get("profile")),
        employee_ids=tuple(str(item) for item in result.get("employee_ids") or ()),
        workspace_id=WorkspaceId(row.workspace_id),
        started_at=_aware(row.started_at),  # type: ignore[arg-type]
        finished_at=_aware(row.finished_at),
    )


def _failure(value: str) -> FailureKind:
    try:
        return FailureKind(value)
    except ValueError:
        return FailureKind.UNKNOWN


class SqlValidationRunRepository:
    """Implements `domain.validation.run.ValidationRunRepository`."""

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
                raise StorageNotInitializedError("The local database has no schema yet.") from error
            raise StorageError(message) from error

    async def save(self, run: ValidationRun) -> None:
        values = _to_values(run)
        async with self._session() as session:
            statement = upsert(session, ValidationRunRow).values(**values)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[ValidationRunRow.id],
                    set_={k: v for k, v in values.items() if k not in ("id", "started_at")},
                )
            )

    async def recent(
        self,
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
        *,
        scenario: str | None = None,
        limit: int = 100,
    ) -> list[ValidationRun]:
        async with self._session() as session:
            query = select(ValidationRunRow).where(
                ValidationRunRow.workspace_id == str(workspace_id)
            )
            if scenario is not None:
                query = query.where(ValidationRunRow.scenario == scenario)
            rows = await session.scalars(
                query.order_by(ValidationRunRow.started_at.desc()).limit(limit)
            )
            return [_to_run(row) for row in rows]


class InMemoryValidationRunRepository:
    """Implements the same contract, for tests and for a run with no store."""

    def __init__(self) -> None:
        self._runs: dict[UUID, ValidationRun] = {}

    async def save(self, run: ValidationRun) -> None:
        self._runs[run.id] = run

    async def recent(
        self,
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
        *,
        scenario: str | None = None,
        limit: int = 100,
    ) -> list[ValidationRun]:
        matching = [
            run
            for run in self._runs.values()
            if run.workspace_id == workspace_id
            and (scenario is None or run.scenario == scenario)
        ]
        return sorted(matching, key=lambda run: run.started_at, reverse=True)[:limit]
