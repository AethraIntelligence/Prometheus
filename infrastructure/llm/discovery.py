"""What a local model runner already has, so a person can pick from a list.

Adding a model by typing its name is how you get a settings page that accepts
`gemma3:27` and fails four hours later inside a task. The runner knows what it
has pulled; asking it is one request and turns a text field into a list.

Only local kinds are discovered, and that is not an oversight. A hosted
provider's catalogue is a moving list of hundreds behind an authenticated
endpoint whose shape differs per vendor, and what the platform needs to know
about a hosted model - what it can do, what it costs - is not in it. A local
runner's list is short, local, needs no key, and is exactly the set of things
that will actually run on this machine.

**A runner is one class and one line in `RUNNERS`.** The `local` kind is "an
OpenAI-compatible server on this machine", and more than one program is that.
Each runner knows how to ask its own server and, where it keeps its models in a
known place, how to read them off the disk. They are tried in order: the most
specific question first, because it answers with more (Ollama's own listing
includes embedding models its OpenAI-compatible one describes less well), and
the generic `/v1/models` last, which is what makes a runner nobody wrote a class
for still show a list.

**A stopped runner is not an empty list.** Its models are still on the disk,
and a person adding one wants to see them - with a word that the runner is not
answering, since nothing will run until it is. That is the answer that used to
be missing: the page turned into a blank text field and said nothing about why.

Failure is answered with a value and a log line, never an exception: a runner
that is not running is the ordinary state of a machine that uses hosted models,
and a settings page that fails to open because of it punishes the common case.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

import httpx
import structlog

from domain.providers.models import InstalledModels
from infrastructure.llm.providers import kind_named

log = structlog.get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 5.0


def _root_of(base_url: str) -> str:
    """The runner's own address, from the chat endpoint the platform talks to.

    The catalog holds `http://host:11434/v1` because that is what the chat
    client needs; a runner's own listing is usually not under `/v1`. Trimming
    the suffix rather than storing a second address keeps one thing for a person
    to configure.
    """
    trimmed = base_url.rstrip("/")
    return trimmed[: -len("/v1")] if trimmed.endswith("/v1") else trimmed


def _unique(names: list[str]) -> tuple[str, ...]:
    return tuple(sorted(dict.fromkeys(name.strip() for name in names if name.strip())))


class Runner(Protocol):
    """One program that serves models on this machine."""

    label: str

    async def ask(self, client: httpx.AsyncClient, base_url: str) -> tuple[str, ...] | None:
        """What the server says it has. None when this is not it, or it is not up."""
        ...

    def on_disk(self, base_url: str) -> tuple[str, ...]:
        """What it would serve, read without it. Empty when that cannot be told."""
        ...


class Ollama:
    label = "Ollama"
    #: The port it listens on unless told otherwise. What decides whether a
    #: stopped runner at this address is Ollama, whose disk may be read: another
    #: runner's address answering with Ollama's models would be a list of things
    #: that will not run there.
    DEFAULT_PORT = 11434
    DEFAULT_REGISTRY = "registry.ollama.ai"
    DEFAULT_NAMESPACE = "library"

    def __init__(self, store: Path | None = None) -> None:
        self._store = store

    async def ask(self, client: httpx.AsyncClient, base_url: str) -> tuple[str, ...] | None:
        url = f"{_root_of(base_url)}/api/tags"
        try:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            log.info("models.not_discovered", runner=self.label, url=url, error=str(error))
            return None
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            return None
        return _unique(
            [str(entry.get("name", "")) for entry in models if isinstance(entry, dict)]
        )

    def on_disk(self, base_url: str) -> tuple[str, ...]:
        address = urlparse(base_url)
        if address.hostname not in {"127.0.0.1", "localhost", "::1"}:
            return ()
        if (address.port or self.DEFAULT_PORT) != self.DEFAULT_PORT:
            return ()
        manifests = self.store() / "manifests"
        if not manifests.is_dir():
            return ()
        names = []
        # manifests/<registry>/<namespace>/<model>/<tag>, and `ollama list`
        # leaves out whichever of the first two are its defaults.
        for manifest in manifests.glob("*/*/*/*"):
            if not manifest.is_file():
                continue
            tag, model = manifest.name, manifest.parent.name
            namespace, registry = manifest.parent.parent.name, manifest.parent.parent.parent.name
            prefix = []
            if registry != self.DEFAULT_REGISTRY:
                prefix = [registry, namespace]
            elif namespace != self.DEFAULT_NAMESPACE:
                prefix = [namespace]
            names.append("/".join([*prefix, f"{model}:{tag}"]))
        return _unique(names)

    def store(self) -> Path:
        if self._store is not None:
            return self._store
        configured = os.environ.get("OLLAMA_MODELS", "").strip()
        return Path(configured).expanduser() if configured else Path.home() / ".ollama" / "models"


class OpenAICompatible:
    """Any server that lists its models the way the OpenAI API does.

    The fallback that makes a runner with no class of its own - LM Studio, a
    llama.cpp server, vLLM - show a list rather than a text field. It has no
    disk to read: nothing is known about where such a server keeps anything.
    """

    label = "Local model runner"

    async def ask(self, client: httpx.AsyncClient, base_url: str) -> tuple[str, ...] | None:
        url = f"{base_url.rstrip('/')}/models"
        try:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            log.info("models.not_discovered", runner=self.label, url=url, error=str(error))
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return None
        return _unique([str(entry.get("id", "")) for entry in data if isinstance(entry, dict)])

    def on_disk(self, base_url: str) -> tuple[str, ...]:
        return ()


#: Which runners a kind of connection may be, most specific first. A kind that
#: is not here cannot be asked what it has, and says so.
RUNNERS: dict[str, tuple[Runner, ...]] = {
    "local": (Ollama(), OpenAICompatible()),
}


class LocalModelDiscovery:
    """Implements `domain.providers.protocols.ModelDiscovery`."""

    def __init__(
        self,
        runners: dict[str, tuple[Runner, ...]] | None = None,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._runners = RUNNERS if runners is None else runners
        self._timeout = timeout_seconds
        self._transport = transport

    async def __call__(self, kind: str, base_url: str) -> InstalledModels:
        runners = self._runners.get(kind.strip().lower(), ())
        if not runners:
            return InstalledModels()
        address = base_url.strip() or _default_address(kind)
        if not address:
            return InstalledModels(supported=True)

        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            for runner in runners:
                names = await runner.ask(client, address)
                if names is not None:
                    log.info("models.discovered", runner=runner.label, count=len(names))
                    return InstalledModels(
                        names, supported=True, reachable=True, runner=runner.label, address=address
                    )

        for runner in runners:
            names = runner.on_disk(address)
            if names:
                log.info("models.found_on_disk", runner=runner.label, count=len(names))
                return InstalledModels(names, supported=True, runner=runner.label, address=address)
        # Named by the most general runner: nothing answered, so which program
        # was meant to be at this address is exactly what is not known.
        return InstalledModels(supported=True, runner=runners[-1].label, address=address)


def _default_address(kind: str) -> str:
    try:
        return kind_named(kind).default_base_url
    except Exception:
        return ""
