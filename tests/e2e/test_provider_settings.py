"""Adding a provider through the surface a person actually uses.

The whole of Phase 17 as one test file: a key typed into the settings page ends
up encrypted in the store, a model added afterwards can name the connection it
is reached through, and a kind of work can be sent to that model - through HTTP,
against the real application, with nothing mocked below the transport.

The rule with the most teeth here is the negative one: **no response ever
carries a key**. It is asserted on every body that comes back rather than on
one, because a settings page leaks a credential exactly once.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config.container import build_container
from app.config.settings import Settings
from app.ui.server import create_app
from infrastructure.persistence.models import Base
from infrastructure.persistence.session import create_engine
from tests.fakes.llm import FakeLLM, reply

REPO_ROOT = Path(__file__).resolve().parents[2]
KEY = "sk-live-not-a-real-key"


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'prometheus.db'}",
        file_root=tmp_path / "workspace",
        employees_dir=REPO_ROOT / "employees",
        log_format="console",
    )


def create_schema(settings: Settings) -> None:
    import asyncio

    async def _create() -> None:
        engine = create_engine(settings.resolved_database_url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_create())


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """The real application, with the one thing a settings page never needs
    - a provider to talk to - answered from a script.

    A test machine has no API key, and building a client without one is
    correctly an error. That error belongs to running work, not to configuring
    it, and this fixture keeps the two apart.
    """
    settings = settings_for(tmp_path)
    create_schema(settings)

    def build(resolved: Settings):
        container = build_container(resolved)
        container.llm_for = lambda *args, **kwargs: FakeLLM([reply("")])  # type: ignore[method-assign]
        return container

    return TestClient(create_app(settings, build=build))


def test_a_person_adds_a_provider_a_model_and_says_where_work_goes(
    client: TestClient, tmp_path: Path
) -> None:
    with client:
        added = client.post(
            "/api/providers/connections",
            json={"name": "openai-work", "kind": "openai", "api_key": KEY},
        )
        assert added.status_code == 201, added.text
        assert added.json()["has_key"] is True
        assert KEY not in added.text, "a key goes in and never comes back"

        made = client.post(
            "/api/providers/models",
            json={
                "name": "work-model",
                "provider": "openai",
                "model": "some-model",
                "connection": "openai-work",
                "capabilities": ["TEXT_REASONING", "TOOL_CALLING"],
            },
        )
        assert made.status_code == 201, made.text

        routed = client.put(
            "/api/providers/defaults",
            json={"task_kind": "PLANNING", "entry_name": "work-model"},
        )
        assert routed.status_code == 200, routed.text
        assert routed.json()["defaults"]["PLANNING"] == "work-model"

        page = client.get("/api/providers").json()
        assert [c["name"] for c in page["connections"]] == ["openai-work"]
        assert "work-model" in {m["name"] for m in page["models"]}
        entry = next(m for m in page["models"] if m["name"] == "work-model")
        assert entry["connection"] == "openai-work"
        assert "PLANNING" in entry["used_for"], "the page says what depends on it"
        assert KEY not in str(page)

    # And what landed on disk is unreadable without the master key.
    database = (tmp_path / "prometheus.db").read_bytes()
    assert KEY.encode() not in database
    assert (tmp_path / "master.key").exists()


def test_a_second_account_on_the_same_provider_is_an_ordinary_second_row(
    client: TestClient,
) -> None:
    """The request this phase came from: different keys, same vendor."""
    with client:
        for name, key in (("openai-work", "sk-work"), ("openai-personal", "sk-home")):
            assert (
                client.post(
                    "/api/providers/connections",
                    json={"name": name, "kind": "openai", "api_key": key},
                ).status_code
                == 201
            )

        page = client.get("/api/providers").json()
        assert [c["name"] for c in page["connections"]] == ["openai-personal", "openai-work"]
        assert all(c["kind"] == "openai" for c in page["connections"])


def test_a_key_can_be_replaced_without_touching_what_points_at_it(
    client: TestClient,
) -> None:
    with client:
        client.post(
            "/api/providers/connections",
            json={"name": "openai-work", "kind": "openai", "api_key": "sk-old"},
        )
        client.post(
            "/api/providers/models",
            json={
                "name": "work-model",
                "provider": "openai",
                "model": "m",
                "connection": "openai-work",
            },
        )

        rotated = client.put(
            "/api/providers/connections/openai-work/key", json={"api_key": "sk-new"}
        )
        assert rotated.status_code == 200
        assert "sk-new" not in rotated.text

        models = client.get("/api/providers").json()["models"]
        assert any(m["connection"] == "openai-work" for m in models)


def test_removing_a_connection_a_model_needs_is_refused_and_says_which(
    client: TestClient,
) -> None:
    with client:
        client.post(
            "/api/providers/connections",
            json={"name": "openai-work", "kind": "openai", "api_key": KEY},
        )
        client.post(
            "/api/providers/models",
            json={
                "name": "work-model",
                "provider": "openai",
                "model": "m",
                "connection": "openai-work",
            },
        )

        refused = client.delete("/api/providers/connections/openai-work")
        assert refused.status_code >= 400
        assert "work-model" in refused.text

        client.delete("/api/providers/models/work-model")
        assert client.delete("/api/providers/connections/openai-work").status_code == 200


def test_a_hosted_provider_offers_no_installed_list_and_that_is_not_an_error(
    client: TestClient,
) -> None:
    """The page falls back to a text field; nothing about settings breaks."""
    with client:
        client.post(
            "/api/providers/connections",
            json={"name": "openai-work", "kind": "openai", "api_key": KEY},
        )

        answer = client.get("/api/providers/connections/openai-work/installed")
        assert answer.status_code == 200
        body = answer.json()
        assert body["models"] == []
        assert body["supported"] is False, "said plainly, not left for the page to infer"


def test_work_cannot_be_sent_to_a_model_that_does_not_exist(client: TestClient) -> None:
    with client:
        answer = client.put(
            "/api/providers/defaults",
            json={"task_kind": "PLANNING", "entry_name": "imaginary"},
        )
        assert answer.status_code >= 400


def test_a_kind_this_machine_cannot_talk_to_is_refused(client: TestClient) -> None:
    with client:
        answer = client.post(
            "/api/providers/connections",
            json={"name": "x", "kind": "some-startup", "api_key": "k"},
        )
        assert answer.status_code >= 400
