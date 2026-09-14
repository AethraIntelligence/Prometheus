"""Settings -> General through HTTP, against the real application.

The same request a window sends. What matters is that a refusal is a 400 with a
sentence a person can act on, and that the listing says a restart is still owed
rather than implying the running engine already changed.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config.container import build_container
from app.config.settings import Settings
from app.ui.server import create_app
from tests.e2e.test_memory_settings import create_schema, settings_for
from tests.fakes.llm import FakeLLM, reply


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = settings_for(tmp_path)
    create_schema(settings)

    def build(resolved: Settings):
        container = build_container(resolved)
        container.llm_for = lambda *args, **kwargs: FakeLLM([reply("")])  # type: ignore[method-assign]
        return container

    with TestClient(create_app(settings, build=build)) as client:
        yield client


def test_settings_are_listed_changed_and_owe_a_restart(client: TestClient, tmp_path: Path) -> None:
    listing = client.get("/api/settings").json()
    assert listing["available"] is True
    assert listing["restart_needed"] is False
    scheduler = next(s for s in listing["settings"] if s["key"] == "flags.scheduler")
    assert (scheduler["kind"], scheduler["value"], scheduler["locked_by"]) == (
        "BOOLEAN",
        False,
        "",
    )

    changed = client.put("/api/settings", json={"values": {"flags.scheduler": True}})
    assert changed.status_code == 200
    body = changed.json()
    assert body["restart_needed"] is True
    scheduler = next(s for s in body["settings"] if s["key"] == "flags.scheduler")
    assert (scheduler["value"], scheduler["running"]) == (True, False)
    assert (tmp_path / "settings.json").exists()


def test_a_refused_value_is_a_sentence_not_a_stack_trace(client: TestClient) -> None:
    refused = client.put("/api/settings", json={"values": {"approval_mode": "sometimes"}})

    assert refused.status_code == 400
    assert "When nobody can be asked" in refused.json()["detail"]


def test_a_setting_the_environment_holds_cannot_be_changed_here(
    client: TestClient,
) -> None:
    # The suite itself sets this one, which makes it the honest example.
    listing = client.get("/api/settings").json()["settings"]
    autostart = next(s for s in listing if s["key"] == "local_llm_autostart")
    assert autostart["locked_by"] == "PROMETHEUS_LOCAL_LLM_AUTOSTART"

    refused = client.put("/api/settings", json={"values": {"local_llm_autostart": True}})
    assert refused.status_code == 400


def test_reset_goes_back_to_the_default(client: TestClient) -> None:
    client.put("/api/settings", json={"values": {"approval_mode": "deny"}})
    assert client.get("/api/settings").json()["any_saved"] is True

    body = client.post("/api/settings/reset", json={"keys": None}).json()

    mode = next(s for s in body["settings"] if s["key"] == "approval_mode")
    assert (mode["value"], mode["default"], mode["saved"]) == ("prompt", "prompt", False)
    assert body["any_saved"] is False
