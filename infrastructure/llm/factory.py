"""Turns a routing decision into a client, with metering attached.

This is the one place that maps a provider name to a class. A caller asks the
router for a model and this for a client, and never learns which vendor answered.

**Where the key comes from is a property of the connection, not of the vendor.**
Until Phase 17 there was one `api_key` and one `base_url` for everything, so
"which provider" and "which account" were the same question - and a person with
two keys to one vendor had no way to say so. Now a catalog entry names a
connection, the connection names a credential and an address, and the value is
resolved here, at the moment the client is built, from `SecretResolver`.

An entry naming no connection still works and means what it always meant: the
key and address this machine was configured with. Every entry in the shipped
catalog is one of those, and a machine with a `.env` and no settings page must
not stop working because a table was added.
"""

from __future__ import annotations

from domain.errors import ConfigurationError
from domain.llm.models import ModelChoice
from domain.llm.protocols import LLM
from domain.llm.telemetry import LLMCallLog
from domain.secrets.protocols import SecretResolver
from infrastructure.llm.anthropic import AnthropicProvider
from infrastructure.llm.catalog import ModelCatalog
from infrastructure.llm.connections import ConnectionDirectory
from infrastructure.llm.embeddings import OpenAICompatibleEmbeddings
from infrastructure.llm.gemini import GeminiProvider
from infrastructure.llm.local import BASE_URL as LOCAL_BASE_URL
from infrastructure.llm.local import LocalProvider
from infrastructure.llm.local_server import OnDemandServer
from infrastructure.llm.openai import OpenAIProvider
from infrastructure.llm.openrouter import OpenRouterProvider
from infrastructure.llm.retry import RetryPolicy
from infrastructure.llm.telemetry import MeteredLLM


class ProviderFactory:
    """Implements `domain.llm.protocols.LLMFactory`."""

    def __init__(
        self,
        *,
        catalog: ModelCatalog,
        api_key: str | None,
        base_url: str,
        local_base_url: str = LOCAL_BASE_URL,
        call_log: LLMCallLog | None = None,
        retry_policy: RetryPolicy | None = None,
        timeout_seconds: float | None = None,
        connections: ConnectionDirectory | None = None,
        secrets: SecretResolver | None = None,
        local_autostart: bool = False,
    ) -> None:
        self._catalog = catalog
        # Off unless the composition root says otherwise: a factory built by a
        # test must not start a model server on the machine running the suite.
        self._local_autostart = local_autostart
        self._local_servers: dict[str, OnDemandServer] = {}
        self._api_key = api_key
        self._base_url = base_url
        self._local_base_url = local_base_url
        self._call_log = call_log
        self._retry_policy = retry_policy
        # None means "each provider's own default", which is the right answer
        # far more often than one number is: a hosted model that has not
        # answered in two minutes is not going to, and a 20B model on a laptop
        # is often only halfway through. Configuring it overrides both, because
        # somebody who sets it has a machine in mind.
        self._timeout = timeout_seconds
        self._connections = connections
        self._secrets = secrets
        self._clients: dict[tuple[str, str, str], LLM] = {}

    def for_choice(self, choice: ModelChoice) -> LLM:
        # The connection is part of the identity: two accounts on one vendor
        # are two clients, and caching them under one key would send the second
        # account's work through the first account's key.
        key = (choice.provider, choice.model, choice.connection)
        if key not in self._clients:
            self._clients[key] = MeteredLLM(
                self._build(choice),
                provider=choice.provider,
                catalog=self._catalog,
                call_log=self._call_log,
            )
        return self._clients[key]

    def _build(self, choice: ModelChoice) -> LLM:
        if choice.provider == "openrouter":
            return OpenRouterProvider(
                self._require_key(choice),
                base_url=self._address(choice, self._base_url),
                default_model=choice.model,
                retry_policy=self._retry_policy,
                **self._timeout_kwargs(),
            )
        if choice.provider == "openai":
            return OpenAIProvider(self._require_key(choice))
        if choice.provider == "anthropic":
            return AnthropicProvider(
                self._require_key(choice),
                default_model=choice.model,
                retry_policy=self._retry_policy,
                **self._timeout_kwargs(),
            )
        if choice.provider == "gemini":
            return GeminiProvider(self._require_key(choice))
        if choice.provider == "local":
            address = self._address(choice, self._local_base_url)
            return LocalProvider(
                base_url=address,
                default_model=choice.model,
                retry_policy=self._retry_policy,
                server=self._local_server(address),
                **self._timeout_kwargs(),
            )
        raise ConfigurationError(
            f"Unknown provider '{choice.provider}'. Providers are registered in "
            "infrastructure/llm/factory.py and their models in models.toml."
        )

    def for_embeddings(self, choice: ModelChoice) -> OpenAICompatibleEmbeddings:
        """A client for the embedding model the router chose.

        Not metered: `MeteredLLM` prices prompt and output tokens against a
        catalog entry, and an embedding call has neither. What it costs is
        visible where it is decided - the catalog entry, which is local and
        therefore zero unless somebody points it elsewhere.
        """
        entry = self._catalog.find(choice.provider, choice.model)
        local = choice.provider == "local"
        if choice.provider not in ("local", "openai"):
            raise ConfigurationError(
                f"'{choice.provider}' does not serve embeddings. Point the "
                "`embedding` default in the model catalog at a provider that "
                "does - a local model runner needs no key and no network."
            )
        address = self._address(choice, self._local_base_url if local else self._base_url)
        return OpenAICompatibleEmbeddings(
            base_url=address,
            model=choice.model,
            api_key=None if local else self._require_key(choice),
            dimensions=entry.dimensions if entry else 0,
            server=self._local_server(address) if local else None,
        )

    def _local_server(self, address: str) -> OnDemandServer | None:
        """One starter per address, so chat and embeddings start it once between them."""
        if not self._local_autostart:
            return None
        if address not in self._local_servers:
            self._local_servers[address] = OnDemandServer(address)
        return self._local_servers[address]

    def _timeout_kwargs(self) -> dict[str, float]:
        """Passed only when configured, so each provider keeps its own default."""
        return {} if self._timeout is None else {"timeout_seconds": self._timeout}

    def _connection(self, choice: ModelChoice):
        """The record this choice is reached through, if it names one."""
        if not choice.connection or self._connections is None:
            return None
        return self._connections.get(choice.connection)

    def _address(self, choice: ModelChoice, fallback: str) -> str:
        """The connection's address if it has one, and the machine's otherwise."""
        connection = self._connection(choice)
        return connection.base_url if connection and connection.base_url else fallback

    def _require_key(self, choice: ModelChoice) -> str:
        """The credential for this call, resolved now rather than held anywhere.

        A named connection is answered from the credential store; an entry that
        names none falls back to the single configured key, which is what every
        installation before Phase 17 has. The error says which of the two is
        missing, because "no API key" pointing at the wrong one of those is a
        person editing the file that was already correct.
        """
        connection = self._connection(choice)
        if connection is not None:
            if not connection.secret_name:
                raise ConfigurationError(
                    f"The connection '{connection.name}' has no credential. Add one "
                    "in Settings, or point this model at a connection that has."
                )
            if self._secrets is None:
                raise ConfigurationError(
                    f"The connection '{connection.name}' needs a credential and this "
                    "process has no way to resolve one."
                )
            return self._secrets.get(connection.secret_name).reveal()

        if choice.connection:
            raise ConfigurationError(
                f"'{choice.model}' is configured to use the connection "
                f"'{choice.connection}', which does not exist here."
            )
        if not self._api_key:
            raise ConfigurationError(
                f"No API key configured for '{choice.provider}'. Add a connection in "
                "Settings, or set PROMETHEUS_LLM_API_KEY in .env."
            )
        return self._api_key

    async def aclose(self) -> None:
        for client in self._clients.values():
            inner = getattr(client, "_inner", client)
            closer = getattr(inner, "aclose", None)
            if closer is not None:
                await closer()
        self._clients.clear()
