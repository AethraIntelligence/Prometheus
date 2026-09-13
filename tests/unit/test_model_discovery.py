"""Asking a runner on this machine what it has, and reading its disk when it is not up.

No runner is started: the server's answer is scripted through httpx's mock
transport and its store is a temporary directory, which is exactly the two
things a person's machine varies in.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from infrastructure.llm.discovery import LocalModelDiscovery, Ollama, OpenAICompatible

ADDRESS = "http://127.0.0.1:11434/v1"


def answering(routes: dict[str, dict]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        body = routes.get(request.url.path)
        return httpx.Response(200, json=body) if body is not None else httpx.Response(404)

    return httpx.MockTransport(handle)


def refusing() -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("All connection attempts failed", request=request)

    return httpx.MockTransport(handle)


def pulled(store: Path, *names: str) -> Path:
    for name in names:
        path, tag = name.rsplit(":", 1)
        parts = path.split("/")
        if len(parts) == 1:
            parts = ["registry.ollama.ai", "library", *parts]
        elif len(parts) == 2:
            parts = ["registry.ollama.ai", *parts]
        manifest = store / "manifests" / Path(*parts) / tag
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("{}")
    return store


async def test_a_running_ollama_is_asked_in_its_own_words() -> None:
    tags = {"models": [{"name": "lfm2:24b"}, {"name": "gpt-oss:20b"}]}
    discover = LocalModelDiscovery(transport=answering({"/api/tags": tags}))

    found = await discover("local", ADDRESS)

    assert found.names == ("gpt-oss:20b", "lfm2:24b")
    assert found.reachable and found.supported
    assert found.runner == "Ollama"


async def test_a_runner_nobody_wrote_a_class_for_still_shows_a_list() -> None:
    """LM Studio, a llama.cpp server, vLLM: the OpenAI-compatible listing is the fallback."""
    discover = LocalModelDiscovery(
        transport=answering({"/v1/models": {"data": [{"id": "qwen3-8b"}]}})
    )

    found = await discover("local", "http://127.0.0.1:1234/v1")

    assert found.names == ("qwen3-8b",)
    assert found.reachable


async def test_a_stopped_ollama_still_offers_what_is_on_the_disk(tmp_path: Path) -> None:
    """The defect: a stopped runner turned the list into a blank field with no reason."""
    store = pulled(tmp_path, "gemma4:31b-cloud", "nomic-embed-text:latest", "someone/tuned:q4")
    discover = LocalModelDiscovery(
        {"local": (Ollama(store=store), OpenAICompatible())}, transport=refusing()
    )

    found = await discover("local", ADDRESS)

    assert found.names == ("gemma4:31b-cloud", "nomic-embed-text:latest", "someone/tuned:q4")
    assert not found.reachable, "said so, because nothing on that list runs until it is started"
    assert found.runner == "Ollama"


async def test_another_runners_address_is_never_answered_with_ollamas_disk(tmp_path: Path) -> None:
    store = pulled(tmp_path, "gemma4:31b-cloud")
    discover = LocalModelDiscovery(
        {"local": (Ollama(store=store), OpenAICompatible())}, transport=refusing()
    )

    found = await discover("local", "http://127.0.0.1:1234/v1")

    assert found.names == ()
    assert found.supported and not found.reachable


async def test_a_hosted_provider_is_not_asked_at_all() -> None:
    asked = []

    def handle(request: httpx.Request) -> httpx.Response:
        asked.append(request.url)
        return httpx.Response(200, json={})

    found = await LocalModelDiscovery(transport=httpx.MockTransport(handle))("openrouter", "")

    assert not found.supported
    assert asked == []


async def test_an_empty_address_means_the_kinds_own_default() -> None:
    discover = LocalModelDiscovery(transport=answering({"/api/tags": {"models": []}}))

    found = await discover("local", "")

    assert found.address == ADDRESS
    assert found.reachable and found.names == ()
