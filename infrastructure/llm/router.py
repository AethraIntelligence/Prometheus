"""Capability-aware routing.

A caller says what the work needs - reasoning, tools, a long context, a budget -
and gets back a model. It never says which one, which is what makes swapping
models a change to `models.toml` rather than to any employee or to Prometheus.

The precedence is deliberate and worth stating plainly:

1. **Requirements filter.** A model that cannot do the work is never a candidate
   - including one below a quality floor, and a REMOTE one when the work or the
   machine says prompts stay local.
2. **The person's choice for this request** wins among what survived.
3. **The configured default wins** whenever it survives that filter. Editing
   `models.toml` must be enough to change which model runs, so a soft hint is
   not allowed to quietly route somewhere else - and somewhere more expensive.
4. **Hints rank the rest** - quality, cost and latency - when the task kind has
   no default, or the default cannot do the work.
5. **Escalation steps up from there** (Phase 10). After a failure a stronger
   model plausibly fixes, the run carries an escalation level, and the choice
   made by 2-4 is replaced by the cheapest candidate of a higher quality tier.
   Where there is none, the choice stands and the reason says so.

Every choice carries its reason, the entry and the escalation level, so the
call log - and through it the trace - can say why a model ran.
"""

from __future__ import annotations

from domain.capabilities.models import Capability, CapabilityRequirement
from domain.errors import ConfigurationError
from domain.llm import escalation as escalations
from domain.llm.catalog import ModelContract, tier_of
from domain.llm.models import ModelChoice, RoutingHints, TaskKind
from domain.workforce import directions
from infrastructure.llm.catalog import ModelCatalog, ModelEntry

#: Weakest first. Escalation only ever moves right.
TIER_ORDER = ("FAST", "BALANCED", "STRONG")


class CapabilityAwareModelRouter:
    """Implements `domain.llm.protocols.ModelRouter`."""

    def __init__(self, catalog: ModelCatalog, *, local_only: bool = False) -> None:
        self._catalog = catalog
        # The machine's own rule, above any caller: with it on, nothing any
        # piece of work asks for is sent to a remote model.
        self._local_only = local_only

    def select(
        self,
        task_kind: TaskKind,
        required: CapabilityRequirement,
        hints: RoutingHints | None = None,
    ) -> ModelChoice:
        hints = hints or RoutingHints()
        requirement = self._tighten(required, hints)
        candidates = self._catalog.candidates(requirement)

        if not candidates:
            # Capability values, not their reprs: this sentence reaches the
            # window's trace, where `<Capability.CODE: 'CODE'>` read as a crash.
            needed = ", ".join(sorted(item.value for item in requirement.required))
            raise ConfigurationError(
                "No model in the catalog can do this work: it needs "
                + (needed or "any model")
                + (
                    f" with a context of at least {requirement.min_context_tokens} tokens"
                    if requirement.min_context_tokens
                    else ""
                )
                + (
                    f", at quality {requirement.min_quality} or better"
                    if requirement.min_quality
                    else ""
                )
                + (", served on this machine" if requirement.local_only else "")
            )

        base, reason = self._base(task_kind, candidates, requirement, hints)
        current = escalations.current()
        if current.active and reason != "chosen for this request":
            stronger = self._stronger(base, candidates, current.level)
            cause = current.cause.value.lower().replace("_", " ") if current.cause else "a failure"
            if stronger is not None:
                return self._choice(
                    stronger,
                    f"escalated from '{base.name}' ({tier_of(base.quality).lower()}) "
                    f"to {tier_of(stronger.quality).lower()} after {cause}",
                    current.level,
                )
            reason = f"{reason}; no stronger model to escalate to after {cause}"
        return self._choice(base, reason, 0)

    def stronger_available(
        self, task_kind: TaskKind, required: CapabilityRequirement | None = None
    ) -> bool:
        """Whether escalating this kind of work would change the model at all.

        Asked before a failed task is retried as an escalation: a retry that
        would run on the same model is a repeat, and repeats are the retry
        policy's business, not this one's.
        """
        requirement = self._tighten(required or CapabilityRequirement(), RoutingHints())
        candidates = self._catalog.candidates(requirement)
        if not candidates:
            return False
        base, reason = self._base(task_kind, candidates, requirement, RoutingHints())
        if reason == "chosen for this request":
            return False
        return self._stronger(base, candidates, 1) is not None

    def _base(
        self,
        task_kind: TaskKind,
        candidates: list[ModelEntry],
        requirement: CapabilityRequirement,
        hints: RoutingHints,
    ) -> tuple[ModelEntry, str]:
        # The person's choice for this request, above the configured default and
        # still below the requirements: it is looked for among the candidates,
        # so a model that cannot do this piece of work is passed over here and
        # the choice holds for every piece that it can.
        chosen = directions.current().model
        for entry in candidates:
            if chosen and entry.name == chosen:
                return entry, "chosen for this request"

        preferred = self._catalog.defaults.get(task_kind)
        for entry in candidates:
            if entry.name == preferred:
                return entry, f"configured default for {task_kind.value.lower()}"

        best = max(candidates, key=lambda entry: self._score(entry, requirement, hints))
        if preferred is None:
            return best, f"no default for {task_kind.value.lower()}; ranked by quality and cost"
        return best, f"default '{preferred}' cannot do this work"

    @staticmethod
    def _stronger(
        base: ModelEntry, candidates: list[ModelEntry], level: int
    ) -> ModelEntry | None:
        """The cheapest candidate `level` quality tiers above `base`, or None.

        Tiers rather than raw quality: two entries a hundredth apart on a
        hand-maintained ranking are not a step up worth paying for. Where fewer
        tiers exist above than were asked for, the highest one there is.
        """
        def rank(entry: ModelEntry) -> int:
            return TIER_ORDER.index(tier_of(entry.quality))

        here = rank(base)
        above = sorted({rank(entry) for entry in candidates if rank(entry) > here})
        if not above:
            return None
        target = above[min(max(level, 1), len(above)) - 1]
        in_tier = [entry for entry in candidates if rank(entry) == target]
        # Cheapest in the tier, then the better of equally cheap.
        return min(
            in_tier,
            key=lambda entry: (ModelContract.of(entry).estimated_cost_per_1k_usd, -entry.quality),
        )

    @staticmethod
    def _choice(entry: ModelEntry, reason: str, level: int) -> ModelChoice:
        # The connection travels with every choice, whichever branch made it:
        # without it the factory falls back to the one configured key, and a
        # model added through a connection in the window is billed to - or
        # refused for want of - an account nobody pointed it at.
        return ModelChoice(
            provider=entry.provider,
            model=entry.model,
            reason=reason,
            connection=entry.connection,
            entry=entry.name,
            escalation_level=level,
            privacy=entry.privacy.value,
        )

    def _tighten(
        self, required: CapabilityRequirement, hints: RoutingHints
    ) -> CapabilityRequirement:
        """Fold the hints that are really requirements into the requirement.

        Asking for tool calling as a 'hint' and then getting a model that cannot
        call tools is a failure at run time, so it is treated as mandatory here.
        """
        capabilities = set(required.required)
        if hints.needs_tools:
            capabilities.add(Capability.TOOL_CALLING)

        context = required.min_context_tokens
        if hints.context_tokens is not None:
            context = max(context or 0, hints.context_tokens)

        return CapabilityRequirement(
            required=frozenset(capabilities),
            preferred=required.preferred,
            min_context_tokens=context,
            min_quality=required.min_quality,
            local_only=required.local_only or self._local_only,
        )

    def _score(
        self,
        entry: ModelEntry,
        requirement: CapabilityRequirement,
        hints: RoutingHints,
    ) -> tuple[float, float]:
        contract = ModelContract.of(entry)
        estimated_cost = contract.estimated_cost_per_1k_usd

        score = entry.quality * hints.quality
        score -= estimated_cost * hints.cost_sensitivity * 10
        # Latency counts only where it is known: an entry nobody measured is
        # neither rewarded nor punished for it. Ten seconds costs a full point
        # of quality at the highest sensitivity.
        if entry.latency_ms:
            score -= min(entry.latency_ms / 10_000, 1.0) * hints.latency_sensitivity * 0.5
        score += requirement.score(entry.capabilities) * 0.05
        # Cheaper breaks a tie: two models that score the same are not the same
        # bill at the end of the month.
        return (round(score, 6), -estimated_cost)
