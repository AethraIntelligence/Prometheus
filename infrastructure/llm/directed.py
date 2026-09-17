"""A client that routes again when the run in front of it changed what is wanted.

Clients are built when a component is assembled - the manager's six once per
process - which is the right time to choose a model when the catalog is the only
thing deciding. Two things arrive later, with one run: a model the person picked
under the field, and an escalation after a failure (Phase 10). A client bound
at startup would hear of neither.

So this holds the client routing chose at assembly and, only while a run is
carrying a chosen model or an escalation, asks the router again with the same
requirement and hands the call to whatever that answers. Outside such a run it
is the bound client and nothing else.

Either way the decision the call is made under is put where the meter reads it,
so every recorded call says which entry ran and why.
"""

from __future__ import annotations

from collections.abc import Callable

from domain.llm import escalation, routing
from domain.llm.models import LLMRequest, LLMResponse, ModelChoice, TaskKind
from domain.llm.protocols import LLM
from domain.workforce import directions


class DirectedLLM:
    """Implements `domain.llm.protocols.LLM`, wrapping the client routing chose."""

    def __init__(
        self,
        bound: tuple[LLM, ModelChoice],
        reroute: Callable[[], tuple[LLM, ModelChoice]],
        *,
        task_kind: TaskKind | None = None,
    ) -> None:
        self._bound = bound
        self._reroute = reroute
        self._task_kind = task_kind

    @property
    def choice(self) -> ModelChoice:
        """What this client runs on outside any run-specific direction."""
        return self._bound[1]

    async def generate(self, request: LLMRequest) -> LLMResponse:
        rerouted = directions.current().model or escalation.current().active
        client, choice = self._reroute() if rerouted else self._bound
        decision = routing.RoutingDecision(
            task_kind=self._task_kind.value if self._task_kind else "",
            entry=choice.entry,
            reason=choice.reason,
            escalation_level=choice.escalation_level,
            privacy=choice.privacy,
        )
        with routing.deciding(decision):
            return await client.generate(request)
