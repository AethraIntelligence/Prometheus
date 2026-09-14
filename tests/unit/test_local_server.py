"""Starting the local model server: only here, only when silent, never twice."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from infrastructure.llm import local_server
from infrastructure.llm.local_server import OnDemandServer, Outcome, ensure_running


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        local_server.subprocess, "Popen", lambda args, **_: calls.append(list(args))
    )
    return calls


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_a_remote_runner_is_somebody_elses_to_start(spawned: list[list[str]]) -> None:
    assert ensure_running("http://10.0.0.5:11434/v1") is Outcome.NOT_LOCAL
    assert spawned == []


def test_a_server_already_answering_is_used_not_replaced(spawned: list[list[str]]) -> None:
    with socket.socket() as listening:
        listening.bind(("127.0.0.1", 0))
        listening.listen()
        port = listening.getsockname()[1]
        assert ensure_running(f"http://127.0.0.1:{port}/v1") is Outcome.ANSWERING
    assert spawned == []


def test_nothing_is_started_where_ollama_is_not_installed(
    spawned: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_server, "_find", lambda: None)
    monkeypatch.setattr(local_server, "_find_application", lambda: None)
    outcome = ensure_running(f"http://127.0.0.1:{_closed_port()}/v1")
    assert outcome is Outcome.NOT_INSTALLED
    assert spawned == []


def test_a_silent_local_address_starts_the_server(spawned: list[list[str]]) -> None:
    outcome = ensure_running(
        f"http://127.0.0.1:{_closed_port()}/v1",
        executable=Path("/opt/ollama"),
        wait_seconds=0.3,
    )
    # The fake never binds the port, so it is started and then reported silent.
    assert spawned == [["/opt/ollama", "serve"]]
    assert outcome is Outcome.DID_NOT_ANSWER


def test_the_application_is_started_rather_than_the_binary(
    spawned: list[list[str]],
) -> None:
    """Its settings - the context length above all - reach only a server it started."""
    ensure_running(
        f"http://127.0.0.1:{_closed_port()}/v1",
        application=Path("/Applications/Ollama.app"),
        wait_seconds=0.1,
    )
    assert spawned == [["open", "-g", "-j", "-a", "/Applications/Ollama.app"]]


async def test_nothing_is_started_until_a_local_model_is_called() -> None:
    """A machine working through an API key never opens a model server it never uses."""
    asked: list[str] = []
    server = OnDemandServer(
        "http://127.0.0.1:11434/v1", start=lambda url: asked.append(url) or Outcome.STARTED
    )
    assert asked == []
    await server.ensure()
    assert asked == ["http://127.0.0.1:11434/v1"]


async def test_a_machine_with_nothing_to_start_is_not_asked_again() -> None:
    asked: list[str] = []

    def start(url: str) -> Outcome:
        asked.append(url)
        return Outcome.NOT_INSTALLED

    server = OnDemandServer("http://127.0.0.1:11434/v1", start=start)
    await server.ensure()
    await server.ensure()
    assert len(asked) == 1


async def test_a_server_that_stopped_is_started_again() -> None:
    """Answering once is not answering forever: somebody may quit Ollama mid-session."""
    outcomes = [Outcome.ANSWERING, Outcome.STARTED]
    server = OnDemandServer("http://127.0.0.1:11434/v1", start=lambda _: outcomes.pop(0))
    await server.ensure()
    await server.ensure()
    assert outcomes == []
