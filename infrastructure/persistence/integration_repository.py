"""Integration storage. SQL does not leave this package.

The only thing here worth a comment is the round trip of the three columns the
platform trusts. `effects` and `capabilities` come back as domain enums, and a
value that is no longer one - a tool classified with an effect a later version
renamed, a capability removed from the vocabulary - is *dropped* rather than
raising. The alternative is an integration that cannot be loaded at all because
one of its twenty tools has a stale label, and an unusable record is worse than
a conservative one: a dropped effect falls back to EXECUTE, which asks.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.capabilities.models import Capability
from domain.errors import StorageError, StorageNotInitializedError
from domain.integrations.models import (
    DiscoveredTool,
    Integration,
    IntegrationKind,
    IntegrationStatus,
)
from domain.policies.risk import Effect
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId
from infrastructure.persistence.dialect import upsert
from infrastructure.persistence.models import IntegrationRow
from infrastructure.persistence.session import session_scope


def _to_row(integration: Integration) -> dict[str, Any]:
    return {
        "id": str(integration.id),
        "workspace_id": str(integration.workspace_id),
        "name": integration.name,
        "kind": integration.kind.value,
        "status": integration.status.value,
        "configuration": dict(integration.configuration),
        "effects": {name: effect.value for name, effect in integration.effects.items()},
        "capabilities": sorted(str(c) for c in integration.granted_capabilities),
        "secret_names": list(integration.secret_names),
        "discovered": [
            {
                "name": tool.name,
                "description": tool.description,
                "json_schema": tool.json_schema,
            }
            for tool in integration.discovered
        ],
        "granted_to": sorted(integration.granted_to),
        "enabled": integration.enabled,
        "updated_at": datetime.now(UTC),
    }


def _effects(raw: Any) -> dict[str, Effect]:
    if not isinstance(raw, dict):
        return {}
    known = {}
    for name, value in raw.items():
        try:
            known[str(name)] = Effect(value)
        except ValueError:
            continue  # unknown label: falls back to EXECUTE, which asks
    return known


def _capabilities(raw: Any) -> frozenset[Capability]:
    if not isinstance(raw, list):
        return frozenset()
    found = set()
    for value in raw:
        try:
            found.add(Capability(value))
        except ValueError:
            continue
    return frozenset(found)


def _discovered(raw: Any) -> tuple[DiscoveredTool, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(
        DiscoveredTool(
            name=str(entry.get("name", "")),
            description=str(entry.get("description", "")),
            json_schema=entry.get("json_schema") or {},
        )
        for entry in raw
        if isinstance(entry, dict) and entry.get("name")
    )


def _to_integration(row: IntegrationRow) -> Integration:
    return Integration(
        id=UUID(row.id),
        name=row.name,
        kind=IntegrationKind(row.kind),
        status=IntegrationStatus(row.status),
        configuration=dict(row.configuration or {}),
        effects=_effects(row.effects),
        granted_capabilities=_capabilities(row.capabilities),
        secret_names=tuple(str(name) for name in (row.secret_names or [])),
        discovered=_discovered(row.discovered),
        enabled=bool(row.enabled),
        workspace_id=WorkspaceId(row.workspace_id),
        granted_to=frozenset(str(name) for name in (row.granted_to or [])),
    )


class SqlIntegrationRepository:
    """Implements `domain.integrations.repository.IntegrationRepository`."""

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

    async def save(self, integration: Integration) -> None:
        values = _to_row(integration)
        async with self._session() as session:
            statement = upsert(session, IntegrationRow).values(
                **values, created_at=datetime.now(UTC)
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[IntegrationRow.id],
                    set_={
                        key: value
                        for key, value in values.items()
                        if key not in ("id", "created_at")
                    },
                )
            )

    async def get(self, integration_id: UUID) -> Integration | None:
        async with self._session() as session:
            row = await session.get(IntegrationRow, str(integration_id))
            return _to_integration(row) if row else None

    async def by_name(
        self, name: str, *, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> Integration | None:
        async with self._session() as session:
            found = await session.execute(
                select(IntegrationRow).where(
                    IntegrationRow.workspace_id == str(workspace_id),
                    IntegrationRow.name == name,
                )
            )
            row = found.scalar_one_or_none()
            return _to_integration(row) if row else None

    async def list(
        self, *, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> list[Integration]:
        async with self._session() as session:
            found = await session.execute(
                select(IntegrationRow)
                .where(IntegrationRow.workspace_id == str(workspace_id))
                .order_by(IntegrationRow.name)
            )
            return [_to_integration(row) for row in found.scalars()]

    async def delete(self, integration_id: UUID) -> bool:
        async with self._session() as session:
            result = await session.execute(
                delete(IntegrationRow).where(IntegrationRow.id == str(integration_id))
            )
            return bool(result.rowcount)


class InMemoryIntegrationRepository:
    """The same contract without a file, for tests and an in-memory container."""

    def __init__(self) -> None:
        self._items: dict[UUID, Integration] = {}

    async def save(self, integration: Integration) -> None:
        self._items[integration.id] = integration

    async def get(self, integration_id: UUID) -> Integration | None:
        return self._items.get(integration_id)

    async def by_name(
        self, name: str, *, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> Integration | None:
        for item in self._items.values():
            if item.name == name and item.workspace_id == workspace_id:
                return item
        return None

    async def list(
        self, *, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> list[Integration]:
        return sorted(
            (item for item in self._items.values() if item.workspace_id == workspace_id),
            key=lambda item: item.name,
        )

    async def delete(self, integration_id: UUID) -> bool:
        return self._items.pop(integration_id, None) is not None
