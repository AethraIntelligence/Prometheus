"""A local model runner - Ollama and anything else with the same endpoint.

The wire format is the chat-completions one, so the work here is not the
protocol. It is everything around it:

* **No key.** There is nothing to authenticate to.
* **No cost.** Calls are priced at zero in the catalog, which is true rather
  than a placeholder. `prometheus spend` stays meaningful either way.
* **Slow.** A 20B model on a laptop answers in tens of seconds, not hundreds of
  milliseconds, so the default timeout is much larger than a hosted one.
* **A server that may not be running.** That is a configuration problem with a
  clear fix, and the error says so instead of reporting a mystery outage.
"""

from __future__ import annotations

import httpx

from domain.errors import ProviderUnavailableError
from domain.llm.models import LLMRequest, LLMResponse
from infrastructure.llm.chat_completions import ChatCompletionsProvider
from infrastructure.llm.local_server import OnDemandServer

PROVIDER_NAME = "local"
BASE_URL = "http://127.0.0.1:11434/v1"

#: Local generation is measured in tens of seconds; a hosted timeout would cut
#: off answers that were going to arrive.
DEFAULT_TIMEOUT_SECONDS = 600.0


class LocalProvider(ChatCompletionsProvider):
    provider_name = PROVIDER_NAME

    def __init__(
        self,
        *,
        base_url: str = BASE_URL,
        default_model: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        server: OnDemandServer | None = None,
        **kwargs: object,
    ) -> None:
        """`server`, when given, is started before a call finds it silent;
        without one a stopped server is the caller's to start."""
        super().__init__(
            None,
            base_url=base_url,
            default_model=default_model,
            timeout_seconds=timeout_seconds,
            **kwargs,  # type: ignore[arg-type]
        )
        self._server = server

    async def generate(self, request: LLMRequest) -> LLMResponse:
        if self._server is not None:
            await self._server.ensure()
        try:
            return await super().generate(request)
        except ProviderUnavailableError as error:
            raise ProviderUnavailableError(
                f"{self._base_url} is not answering. Is the local model server running? "
                f"({error})"
            ) from error


async def is_available(base_url: str = BASE_URL, *, timeout_seconds: float = 2.0) -> bool:
    """Cheap liveness check, used to skip tests that need a running server."""
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(f"{base_url.rstrip('/')}/models")
            return response.status_code < 400
    except httpx.HTTPError:
        return False
