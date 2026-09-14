from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import normalise_database_url
from infrastructure.persistence.models import Base
from infrastructure.persistence.session import create_engine, create_session_factory
from infrastructure.persistence.task_repository import SqlTaskRepository

POSTGRES_URL = os.environ.get("PROMETHEUS_TEST_POSTGRES_URL", "")


#: Rebuilt once per pytest session, on first use, and inside whichever event
#: loop that test runs in - a session-scoped async fixture would hold its
#: connections on a loop the tests do not use.
_postgres_schema_built = False


async def _postgres_factory() -> async_sessionmaker[AsyncSession]:
    """The configured server, with a schema built the way the platform builds it.

    Emptied between tests rather than dropped and recreated: twenty-six tables
    of DDL per test is a minute of the suite spent on nothing these tests are
    about, which is what the repositories do with rows.
    """
    global _postgres_schema_built
    engine = create_engine(normalise_database_url(POSTGRES_URL))
    async with engine.begin() as connection:
        if not _postgres_schema_built:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
            _postgres_schema_built = True
        else:
            tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
            await connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    return create_session_factory(engine)


@pytest_asyncio.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A real database file, not an in-memory one.

    The point of these tests is that work survives a restart, and a shared
    in-memory database would not exercise that.

    With `PROMETHEUS_TEST_POSTGRES_URL` set the same tests run against the second
    dialect instead - which is what Phase 19 needed and what three hand-written
    PostgreSQL tests could not give: thirteen repositories, one fixture, either
    backend. Unset, nothing changes and the suite still needs no server.
    """
    if POSTGRES_URL:
        factory = await _postgres_factory()
        yield factory
        await factory.kw["bind"].dispose()
        return

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
def sqlite_repository(session_factory: async_sessionmaker[AsyncSession]) -> SqlTaskRepository:
    return SqlTaskRepository(session_factory)


@pytest.fixture(autouse=True)
def _ignore_the_developers_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """No test reads `.env`, whoever is running it.

    The rule this defends is one the project states plainly: the suite needs no
    network and no provider key. A developer with a real `.env` broke that
    silently - a test asserting that a missing key is explained instead of
    called out would find a key, call out, and pass or fail on the weather.

    Settings still read the environment, so a test that wants a value sets one.
    What it cannot do is inherit one from a file it never mentioned.
    """
    from app.config.settings import Settings, get_settings

    monkeypatch.setitem(Settings.model_config, "env_file", None)
    # Nor what a person saved from Settings -> General on this machine: that
    # file sits beside the developer's own database, and a flag switched off
    # there would switch it off in every test that never named a data dir.
    home = tmp_path_factory.mktemp("prometheus-home")
    monkeypatch.setattr("app.config.settings._default_data_dir", lambda: home)
    # Nor does one start a model server on the machine running it.
    monkeypatch.setenv("PROMETHEUS_LOCAL_LLM_AUTOSTART", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
