"""Asking a runner on this machine what it has, and reading its disk when it is not up.

No runner is started: the server's answer is scripted through httpx's mock
transport and its store is a temporary directory, which is exactly the two
things a person's machine varies in.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from infrastructure.llm.discovery import ConnectionModelDiscovery, Ollama, OpenAICompatible

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
    discover = ConnectionModelDiscovery(transport=answering({"/api/tags": tags}))

    found = await discover("local", ADDRESS)

    assert found.names == ("gpt-oss:20b", "lfm2:24b")
    assert found.reachable and found.supported
    assert found.runner == "Ollama"


async def test_a_runner_nobody_wrote_a_class_for_still_shows_a_list() -> None:
    """LM Studio, a llama.cpp server, vLLM: the OpenAI-compatible listing is the fallback."""
    discover = ConnectionModelDiscovery(
        transport=answering({"/v1/models": {"data": [{"id": "qwen3-8b"}]}})
    )

    found = await discover("local", "http://127.0.0.1:1234/v1")

    assert found.names == ("qwen3-8b",)
    assert found.reachable


async def test_a_stopped_ollama_still_offers_what_is_on_the_disk(tmp_path: Path) -> None:
    """The defect: a stopped runner turned the list into a blank field with no reason."""
    store = pulled(tmp_path, "gemma4:31b-cloud", "nomic-embed-text:latest", "someone/tuned:q4")
    discover = ConnectionModelDiscovery(
        {"local": (Ollama(store=store), OpenAICompatible())}, transport=refusing()
    )

    found = await discover("local", ADDRESS)

    assert found.names == ("gemma4:31b-cloud", "nomic-embed-text:latest", "someone/tuned:q4")
    assert not found.reachable, "said so, because nothing on that list runs until it is started"
    assert found.runner == "Ollama"


async def test_another_runners_address_is_never_answered_with_ollamas_disk(tmp_path: Path) -> None:
    store = pulled(tmp_path, "gemma4:31b-cloud")
    discover = ConnectionModelDiscovery(
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

    found = await ConnectionModelDiscovery(transport=httpx.MockTransport(handle))("openrouter", "")

    assert not found.supported
    assert asked == []


async def test_an_empty_address_means_the_kinds_own_default() -> None:
    discover = ConnectionModelDiscovery(transport=answering({"/api/tags": {"models": []}}))

    found = await discover("local", "")

    assert found.address == ADDRESS
    assert found.reachable and found.names == ()


async def test_the_context_a_model_takes_is_read_from_ollama() -> None:
    """A number nobody typed: 8192 was defaulted for a model that takes 32768."""
    shown = {"model_info": {"general.architecture": "lfm2moe", "lfm2moe.context_length": 32768}}
    discover = ConnectionModelDiscovery(transport=answering({"/api/show": shown}))

    assert await discover.context_tokens("local", ADDRESS, "lfm2:24b") == 32768


async def test_a_context_that_cannot_be_asked_is_not_invented() -> None:
    assert await ConnectionModelDiscovery(transport=refusing()).context_tokens(
        "local", ADDRESS, "lfm2:24b"
    ) is None
    assert await ConnectionModelDiscovery().context_tokens("openrouter", "", "x") is None


# --- A hosted kind with a short list -------------------------------------------


class OneSecret:
    """A resolver holding one credential, like the store does."""

    def __init__(self, name: str, value: str) -> None:
        self._name, self._value = name, value
        self.asked: list[str] = []

    def get(self, name: str):
        from domain.secrets.models import Secret

        self.asked.append(name)
        if name != self._name:
            raise KeyError(name)
        return Secret(name, self._value)


async def test_a_decision_service_is_asked_for_its_own_short_list() -> None:
    """Its names cannot be guessed: it refused "Jev", which is the vendor."""
    seen: dict = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": "jev-latest", "description": "stable", "release_date": "2026-09-01"},
                    {"name": "jev-preview", "description": "newest", "release_date": "2026-09-01"},
                ]
            },
        )

    secrets = OneSecret("jev_api_key", "ts-key")
    discover = ConnectionModelDiscovery(
        transport=httpx.MockTransport(handle), secrets=secrets
    )

    found = await discover("typesafe", "https://api.typesafe.ai", "jev_api_key")

    assert found.names == ("jev-latest", "jev-preview")
    assert found.reachable and found.supported and found.runner == "TypeSafe"
    assert seen["url"] == "https://api.typesafe.ai/v1/models"
    assert seen["auth"] == "Bearer ts-key"
    assert secrets.asked == ["jev_api_key"]


async def test_the_version_segment_is_not_doubled() -> None:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"models": [{"name": "jev-latest"}]})

    discover = ConnectionModelDiscovery(transport=httpx.MockTransport(handle))
    await discover("typesafe", "https://api.typesafe.ai/v1")

    assert seen == ["https://api.typesafe.ai/v1/models"]


async def test_a_listing_that_refuses_the_key_reads_as_not_answering() -> None:
    discover = ConnectionModelDiscovery(
        transport=httpx.MockTransport(lambda _: httpx.Response(401, json={"detail": "no"}))
    )

    found = await discover("typesafe", "https://api.typesafe.ai")

    assert found.supported and not found.reachable and found.names == ()
    assert found.runner == "TypeSafe"
