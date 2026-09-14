"""Restarting from the window: the request is answered, then the server is told to stop.

The process replacement itself is `os.execv` and would replace the test runner,
so what is asserted is the part that decides: who may restart, what the window
is told, and that the server is asked to stop only after the answer went out.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config.container import build_container
from app.config.settings import Settings
from app.ui.restart import RestartSignal
from app.ui.server import create_app
from tests.e2e.test_memory_settings import create_schema, settings_for
from tests.fakes.llm import FakeLLM, reply


class FakeServer:
    should_exit = False


def app_with(tmp_path: Path, signal: RestartSignal) -> TestClient:
    settings = settings_for(tmp_path)
    create_schema(settings)

    def build(resolved: Settings):
        container = build_container(resolved)
        container.llm_for = lambda *args, **kwargs: FakeLLM([reply("")])  # type: ignore[method-assign]
        return container

    return TestClient(create_app(settings, build=build, restart=signal))


@pytest.fixture
def signal() -> RestartSignal:
    return RestartSignal()


def test_a_runtime_without_a_server_says_it_cannot_restart(
    tmp_path: Path, signal: RestartSignal
) -> None:
    with app_with(tmp_path, signal) as client:
        health = client.get("/api/health").json()
        assert health["can_restart"] is False
        assert health["started_at"] == signal.started_at

        refused = client.post("/api/runtime/restart")
        assert refused.status_code == 409
        assert "Restart it where it runs" in refused.json()["detail"]


def test_a_restart_is_answered_then_the_server_is_stopped(
    tmp_path: Path, signal: RestartSignal
) -> None:
    server = FakeServer()
    signal.attach(server)

    with app_with(tmp_path, signal) as client:
        assert client.get("/api/health").json()["can_restart"] is True

        answer = client.post("/api/runtime/restart")

        assert answer.status_code == 202
        assert answer.json()["restarting"] is True
        assert answer.json()["stopping"] == 0
        assert server.should_exit is False, "not before the answer went out"
        deadline = time.monotonic() + 3
        while not server.should_exit and time.monotonic() < deadline:
            client.get("/api/health")
            time.sleep(0.05)

    assert server.should_exit is True
    assert signal.requested is True
