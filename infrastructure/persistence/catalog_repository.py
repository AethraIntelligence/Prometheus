"""Catalog storage: the entries a person added, and where work is sent.

SQL does not leave this package. What comes back is `ModelEntry` and a map of
defaults - the same values the TOML loader produces, so nothing above can tell
which source it got them from, which is the point.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.capabilities.models import Capability
from domain.errors import StorageError, StorageNotInitializedError
from domain.llm.models import TaskKind
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId
from infrastructure.llm.catalog import ModelEntry, Privacy, default_privacy
from infrastructure.persistence.dialect import upsert
from infrastructure.persistence.models import ModelEntryRow, TaskDefaultRow
from infrastructure.persistence.session import session_scope


def _to_row(entry: ModelEntry, workspace_id: WorkspaceId) -> dict[str, Any]:
    return {
        "workspace_id": str(workspace_id),
        "name": entry.name,
        "provider": entry.provider,
        "model": entry.model,
        "connection": entry.connection,
        "capabilities": sorted(item.value for item in entry.capabilities),
        "context_tokens": entry.context_tokens,
        "input_cost_per_1k_usd": entry.input_cost_per_1k_usd,
        "output_cost_per_1k_usd": entry.output_cost_per_1k_usd,
        "quality": entry.quality,
        "dimensions": entry.dimensions,
        "privacy": entry.privacy.value,
        "latency_ms": entry.latency_ms,
        "updated_at": datetime.now(UTC),
    }


def _to_entry(row: ModelEntryRow) -> ModelEntry:
    return ModelEntry(
        name=row.name,
        provider=row.provider,
        model=row.model,
        connection=row.connection or "",
        # An unknown capability name is dropped rather than raised on: it means
        # a row written by a newer version of the platform, and a catalog that
        # refuses to load is worse than one entry that cannot be selected.
        capabilities=frozenset(
            Capability(name) for name in (row.capabilities or []) if name in Capability.__members__
        ),
        context_tokens=row.context_tokens,
        input_cost_per_1k_usd=row.input_cost_per_1k_usd,
        output_cost_per_1k_usd=row.output_cost_per_1k_usd,
        quality=row.quality,
        dimensions=row.dimensions,
        privacy=(
            Privacy(row.privacy)
            if row.privacy in Privacy.__members__
            else default_privacy(row.provider)
        ),
        latency_ms=row.latency_ms or 0,
    )


class SqlCatalogRepository:
    """The stored half of the model catalog."""

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

    # --- Entries --------------------------------------------------------------

    async def save_entry(
        self, entry: ModelEntry, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> None:
        values = _to_row(entry, workspace_id)
        now = datetime.now(UTC)
        async with self._session() as session:
            statement = upsert(session, ModelEntryRow).values(
                id=str(uuid4()), created_at=now, **values
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[ModelEntryRow.workspace_id, ModelEntryRow.name],
                    set_={
                        key: value
                        for key, value in values.items()
                        if key not in ("workspace_id", "name")
                    },
                )
            )

    async def entries(self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID) -> list[ModelEntry]:
        async with self._session() as session:
            found = await session.execute(
                select(ModelEntryRow)
                .where(ModelEntryRow.workspace_id == str(workspace_id))
                .order_by(ModelEntryRow.name)
            )
            return [_to_entry(row) for row in found.scalars()]

    async def delete_entry(
        self, name: str, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> bool:
        async with self._session() as session:
            result = await session.execute(
                delete(ModelEntryRow).where(
                    ModelEntryRow.workspace_id == str(workspace_id),
                    ModelEntryRow.name == name,
                )
            )
            return bool(result.rowcount)

    async def entries_using(
        self, connection: str, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> list[ModelEntry]:
        """Which entries a connection is holding up. Read before removing one."""
        stored = await self.entries(workspace_id)
        return [entry for entry in stored if entry.connection == connection]

    # --- Defaults -------------------------------------------------------------

    async def set_default(
        self, task_kind: TaskKind, entry_name: str, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> None:
        values = {
            "workspace_id": str(workspace_id),
            "entry_name": entry_name,
            "updated_at": datetime.now(UTC),
        }
        async with self._session() as session:
            statement = upsert(session, TaskDefaultRow).values(
                task_kind=task_kind.value, **values
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[TaskDefaultRow.task_kind], set_=values
                )
            )

    async def defaults(
        self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> dict[TaskKind, str]:
        async with self._session() as session:
            found = await session.execute(
                select(TaskDefaultRow).where(TaskDefaultRow.workspace_id == str(workspace_id))
            )
            return {
                TaskKind(row.task_kind): row.entry_name
                for row in found.scalars()
                if row.task_kind in TaskKind.__members__
            }

    async def clear_default(self, task_kind: TaskKind) -> bool:
        async with self._session() as session:
            result = await session.execute(
                delete(TaskDefaultRow).where(TaskDefaultRow.task_kind == task_kind.value)
            )
            return bool(result.rowcount)

    async def is_empty(self, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID) -> bool:
        """Whether this installation has ever been seeded."""
        async with self._session() as session:
            found = await session.execute(
                select(ModelEntryRow.id)
                .where(ModelEntryRow.workspace_id == str(workspace_id))
                .limit(1)
            )
            return found.scalar_one_or_none() is None
