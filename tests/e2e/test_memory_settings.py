"""Managing memory through the surface a person actually uses.

Search, a note of their own, and forgetting one line - through HTTP, against
the real application and a SQLite file. The rule with the most teeth is the
negative one: what can be forgotten is exactly what the screen shows, so an id
belonging to an employee's private notes is not found rather than deleted.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.config.container import build_container
from app.config.settings import Settings
from app.ui.server import create_app
from domain.memory.models import MemoryItem, MemoryKind, MemoryQuery, MemoryScope
from infrastructure.persistence.models import Base
from infrastructure.persistence.session import create_engine
from tests.fakes.llm import FakeLLM, reply

REPO_ROOT = Path(__file__).resolve().parents[2]


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'prometheus.db'}",
        file_root=tmp_path / "workspace",
        employees_dir=REPO_ROOT / "employees",
        log_format="console",
    )


def create_schema(settings: Settings) -> None:
    async def _create() -> None:
        engine = create_engine(settings.resolved_database_url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_create())


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    resolved = settings_for(tmp_path)
    create_schema(resolved)
    return resolved


@pytest.fixture
def client(settings: Settings) -> TestClient:
    """The real application; the model it never calls here is answered from a script."""

    def build(resolved: Settings):
        container = build_container(resolved)
        container.llm_for = lambda *args, **kwargs: FakeLLM([reply("")])  # type: ignore[method-assign]
        return container

    with TestClient(create_app(settings, build=build)) as client:
        yield client


def test_a_note_is_kept_found_and_forgotten(client: TestClient) -> None:
    listing = client.get("/api/memory").json()
    assert listing == {"available": True, "can_forget": True, "items": []}

    added = client.post(
        "/api/memory",
        json={"content": "Always answer  in Russian", "about_the_person": True},
    )
    assert added.status_code == 201
    note = added.json()
    assert (note["scope"], note["kind"], note["stated"]) == ("USER", "SEMANTIC", True)
    assert note["content"] == "Always answer in Russian"
    assert note["expires_at"] == ""

    client.post("/api/memory", json={"content": "Invoices live in finance/2026"})
    found = client.get("/api/memory", params={"q": "invoices"}).json()["items"]
    assert [item["content"] for item in found] == ["Invoices live in finance/2026"]
    assert found[0]["scope"] == "WORKSPACE"

    assert client.delete(f"/api/memory/{note['id']}").json() == {"forgotten": True}
    remaining = client.get("/api/memory").json()["items"]
    assert [item["content"] for item in remaining] == ["Invoices live in finance/2026"]
    assert client.delete(f"/api/memory/{note['id']}").status_code == 404


def test_an_empty_note_is_refused(client: TestClient) -> None:
    assert client.post("/api/memory", json={"content": "   "}).status_code == 400
    assert client.post("/api/memory", json={"content": ""}).status_code == 422


def test_what_the_screen_does_not_show_cannot_be_forgotten_from_it(
    settings: Settings, client: TestClient
) -> None:
    private = MemoryItem.create(
        "An employee's own note",
        scope=MemoryScope.EMPLOYEE_PRIVATE,
        kind=MemoryKind.SEMANTIC,
        employee_id=uuid4(),
    )

    async def seed_and_count() -> int:
        container = build_container(settings)
        try:
            await container.memory.remember(private)
            client.delete(f"/api/memory/{private.id}")
            return len(
                await container.memory.recall(
                    MemoryQuery(
                        scopes=frozenset({MemoryScope.EMPLOYEE_PRIVATE}),
                        employee_id=private.employee_id,
                    )
                )
            )
        finally:
            await container.aclose()

    assert asyncio.run(seed_and_count()) == 1
    assert client.delete(f"/api/memory/{uuid4()}").status_code == 404
