"""Which backend answers a decision: the one `decision` is routed to, if it decides.

A person routes the `decision` kind of work in Settings like any other. Routed
to a text model, the question is lettered and asked (`TextDecider`); routed to
an entry that only decides, it goes to that entry's adapter instead. The choice
is read on every call rather than at assembly, so changing the route in the
window reaches the next decision without a restart.

**A decision model that cannot answer hands the question to the text model.**
It is a faster and cheaper way to reach the same decision, not the only way:
a missing key, a rate limit or an outage would otherwise stop
the manager at its first question, and the text model can always be asked.
The fallback is logged with the reason, so a route that never takes effect is
visible rather than silent.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import structlog

from domain.decisions.models import ChoiceAnswer, ChoiceQuestion
from domain.decisions.protocols import Decider
from domain.errors import ConfigurationError, ProviderError
from domain.llm.catalog import ModelEntry, Privacy
from domain.llm.models import ModelChoice, TaskKind
from infrastructure.llm.catalog import ModelCatalog

log = structlog.get_logger(__name__)


class RoutedDecider:
    """Implements `domain.decisions.protocols.Decider`."""

    def __init__(
        self,
        text: Decider | None,
        *,
        catalog: Callable[[], ModelCatalog],
        typed: Callable[[ModelChoice], Decider],
        local_only: bool = False,
        min_quality: float = 0.0,
    ) -> None:
        """`text` None answers only through a decision model, and "unreadable"
        otherwise - for a caller that already has its own way of asking a text
        model and wants a decision model's opinion only where there is one.

        `min_quality` is the caller's floor, applied to the decision model the
        way the router applies it to a text model: an entry below it is not
        asked, whatever the route says."""
        self._text = text
        self._catalog = catalog
        self._typed = typed
        self._local_only = local_only
        self._min_quality = min_quality

    async def choose(self, question: ChoiceQuestion) -> ChoiceAnswer:
        entry = self._decision_model()
        if entry is not None:
            try:
                choice = replace(
                    entry.choice,
                    entry=entry.name,
                    reason="decision routed to a decision model",
                    privacy=entry.privacy.value,
                )
                return await self._typed(choice).choose(question)
            except (ConfigurationError, ProviderError) as error:
                log.warning(
                    "decision.fallback_to_text",
                    entry=entry.name,
                    purpose=question.purpose,
                    error=str(error)[:200],
                )
        if self._text is None:
            return ChoiceAnswer.unreadable("no decision model")
        return await self._text.choose(question)

    def _decision_model(self) -> ModelEntry | None:
        """The entry `decision` is routed to, when it is one that decides and may be used."""
        catalog = self._catalog()
        name = catalog.defaults.get(TaskKind.DECISION)
        entry = next((item for item in catalog.entries if item.name == name), None)
        if entry is None or not entry.decides or entry.generates_text:
            return None
        if entry.quality < self._min_quality:
            return None
        if self._local_only and entry.privacy is not Privacy.LOCAL:
            # The machine's rule is above the route, exactly as it is in the
            # router: nothing is sent to a remote model while it is on.
            return None
        return entry
