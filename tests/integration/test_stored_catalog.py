"""The catalog as something a person edits, and the three rules that keeps.

Written against a real SQLite file: the whole claim is that what somebody
changed in the settings window is still there after a restart, and an in-memory
database would not exercise that.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.capabilities.models import Capability
from domain.llm.catalog import Privacy
from domain.llm.models import TaskKind
from infrastructure.llm.catalog import ModelCatalog, ModelEntry
from infrastructure.llm.store import StoredCatalogSource
from infrastructure.persistence.catalog_repository import SqlCatalogRepository


@pytest.fixture
def catalog(session_factory: async_sessionmaker[AsyncSession]) -> SqlCatalogRepository:
    return SqlCatalogRepository(session_factory)


def source(catalog: SqlCatalogRepository, path: Path | None = None) -> StoredCatalogSource:
    return StoredCatalogSource(catalog, configured_path=path)


async def test_a_fresh_installation_is_seeded_from_the_file_that_ships(
    catalog: SqlCatalogRepository,
) -> None:
    loaded = await source(catalog).load()

    assert loaded.entries, "a machine that has never been configured still has models"
    assert loaded.defaults, "and knows where to send each kind of work"
    assert not await catalog.is_empty()


async def test_what_a_person_changed_is_not_undone_by_the_next_start(
    catalog: SqlCatalogRepository,
) -> None:
    """Re-seeding every time would put the developers' choices back every morning."""
    await source(catalog).load()
    await catalog.set_default(TaskKind.VERIFICATION, "deep")

    again = await source(catalog).load()
    assert again.defaults[TaskKind.VERIFICATION] == "deep"


async def test_a_default_pointing_at_an_entry_that_is_gone_is_dropped(
    catalog: SqlCatalogRepository,
) -> None:
    """The router treats a default as the winner, so a dangling one routes
    every task of that kind into an error."""
    first = await source(catalog).load()
    kind, name = next(iter(first.defaults.items()))
    assert await catalog.delete_entry(name)

    again = await source(catalog).load()
    assert kind not in again.defaults
    assert name not in {entry.name for entry in again.entries}


async def test_a_named_file_is_the_catalog_and_the_store_is_not_consulted(
    catalog: SqlCatalogRepository, tmp_path: Path
) -> None:
    """How validation and CI say which models to use, in one variable."""
    path = tmp_path / "only.toml"
    path.write_text(
        '[models.only]\nprovider = "local"\nmodel = "m"\n'
        'capabilities = ["TEXT_REASONING"]\n[defaults]\nplanning = "only"\n',
        encoding="utf-8",
    )
    await catalog.save_entry(ModelEntry(name="stored", provider="local", model="other"))

    loaded = await source(catalog, path).load()
    assert [entry.name for entry in loaded.entries] == ["only"]
    assert source(catalog, path).is_overridden


async def test_an_entry_keeps_what_it_was_given(catalog: SqlCatalogRepository) -> None:
    await catalog.save_entry(
        ModelEntry(
            name="work",
            provider="openai",
            model="a-model",
            connection="openai-work",
            capabilities=frozenset({Capability.TOOL_CALLING}),
            context_tokens=64_000,
            input_cost_per_1k_usd=0.5,
            quality=0.9,
            privacy=Privacy.LOCAL,
            latency_ms=1200,
        )
    )

    stored = (await catalog.entries())[0]
    assert (stored.privacy, stored.latency_ms) == (Privacy.LOCAL, 1200), "the contract too"
    assert stored.connection == "openai-work"
    assert stored.capabilities == frozenset({Capability.TOOL_CALLING})
    assert stored.context_tokens == 64_000
    assert stored.choice.connection == "openai-work", "and it reaches the factory"


async def test_which_entries_a_connection_is_holding_up_can_be_asked(
    catalog: SqlCatalogRepository,
) -> None:
    """Read before removing a connection: entries pointing at it stop working."""
    await catalog.save_entry(ModelEntry(name="a", provider="openai", model="m", connection="work"))
    await catalog.save_entry(ModelEntry(name="b", provider="openai", model="m", connection="home"))

    assert [entry.name for entry in await catalog.entries_using("work")] == ["a"]


async def test_saving_an_entry_twice_replaces_it_rather_than_duplicating(
    catalog: SqlCatalogRepository,
) -> None:
    await catalog.save_entry(ModelEntry(name="a", provider="openai", model="m", quality=0.4))
    await catalog.save_entry(ModelEntry(name="a", provider="openai", model="m", quality=0.9))

    entries = await catalog.entries()
    assert len(entries) == 1 and entries[0].quality == 0.9


def test_the_two_sources_produce_the_same_kind_of_value() -> None:
    """Nothing above can tell which source a catalog came from, which is the point."""
    assert isinstance(ModelCatalog.load(), ModelCatalog)


async def test_a_model_nothing_can_reach_is_not_offered_to_the_router(
    catalog: SqlCatalogRepository,
) -> None:
    """Found by the Phase 17 validation run.

    A machine with a local provider connected and every kind of work routed to
    it still sent a call to a shipped hosted entry: the quality floor on
    verification filtered out the routed model, and the hints then ranked one
    with nothing behind it. The row stays - it is the user's, and Settings still
    lists it - but it is not something to route work to.
    """
    await catalog.save_entry(ModelEntry(name="reachable", provider="local", model="m"))
    await catalog.save_entry(ModelEntry(name="no-key", provider="openai", model="m"))

    loaded = await StoredCatalogSource(
        catalog, reachable=lambda entry: entry.provider == "local"
    ).load()

    assert [entry.name for entry in loaded.entries] == ["reachable"]
    assert {row.name for row in await catalog.entries()} == {"reachable", "no-key"}


async def test_a_default_naming_an_unreachable_model_is_dropped_with_it(
    catalog: SqlCatalogRepository,
) -> None:
    await catalog.save_entry(ModelEntry(name="no-key", provider="openai", model="m"))
    await catalog.set_default(TaskKind.PLANNING, "no-key")

    loaded = await StoredCatalogSource(catalog, reachable=lambda entry: False).load()

    assert TaskKind.PLANNING not in loaded.defaults
