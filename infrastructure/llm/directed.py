"""A client that routes again when a request chose its own model.

Clients are built when a component is assembled - the manager's six once per
process - which is the right time to choose a model when the catalog is the only
thing deciding. A model the person picked under the field arrives later, with
one request, and a client bound at startup would never hear of it.

So this holds the client routing chose at assembly and, only while a run is
carrying a chosen model, asks the router again with the same requirement and
hands the call to whatever that answers. Outside such a run it is the bound
client and nothing else, so a machine where nobody picks a model pays one
`ContextVar` read per call.
"""

from __future__ import annotations

from collections.abc import Callable

from domain.llm.models import LLMRequest, LLMResponse
from domain.llm.protocols import LLM
from domain.workforce import directions


class DirectedLLM:
    """Implements `domain.llm.protocols.LLM`, wrapping the client routing chose."""

    def __init__(self, bound: LLM, reroute: Callable[[], LLM]) -> None:
        self._bound = bound
        self._reroute = reroute

    async def generate(self, request: LLMRequest) -> LLMResponse:
        client = self._reroute() if directions.current().model else self._bound
        return await client.generate(request)
