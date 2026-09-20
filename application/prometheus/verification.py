"""Prometheus checks the work before the user sees it.

The employee runtime already verifies each task against its own goal. This is a
second, different check, and §7.8 is right to ask for it: a task can be
perfectly done and the objective still unmet. "Find twenty" decomposed into
"find some" and executed flawlessly produces eleven, and every verdict along the
way says pass.

So this judges the objective's acceptance criteria - written down before the
work started, precisely so the standard cannot be invented after seeing the
result - against everything that came back.

One consequence is worth stating: when the reading of the request produced no
criteria, the request itself is the standard. Passing by default there would
switch this check off exactly when comprehension had been weakest, which is the
opposite of what it is for.

Three rules hold it up. **A criterion with no evidence is unmet**, however
confident the reports read; the employee that did the work is the wrong witness
to whether it worked, and so is its summary. **An unreadable verdict is a
rejection**, because a checker that cannot be understood must not be allowed to
wave work through by failing quietly. And **a verdict that passes while naming
something missing has not passed** - that contradiction turned up in the first
real run of this phase, and reading it either way is a choice, so it is read the
safe way and logged.

**And it is told what was actually done**, which is Phase 16's fix for the task
verifier applied one level up, for the same reason and after a worse failure. A
direct answer arrives here as prose and nothing else, and the check that is
supposed to stop "needs no work" from closing a request that asked for a file
was being handed only the sentence claiming it was done. Three consecutive
Phase 18 runs of the same request were answered from a memory of the last one -
the document had been written yesterday, the answer said what it contained, and
the file had been deleted before the run started. Every criterion read as met.
What separates that from a real result is not in the text, so the text is no
longer all this sees.
"""

from __future__ import annotations

import structlog

from application.present import dated
from application.prompts import render
from domain.capabilities.models import CapabilityRequirement
from domain.employees.verification import MIN_JUDGEMENT_QUALITY, Verdict
from domain.llm.json_output import extract_object
from domain.llm.models import LLMRequest, Message, RoutingHints, TaskKind
from domain.llm.protocols import LLM
from domain.workforce.protocols import Objective

log = structlog.get_logger(__name__)


class ObjectiveVerifier:
    """Judges an objective, not a task."""

    def __init__(self, llm: LLM) -> None:
        self._llm = llm

    async def verify(
        self, objective: Objective, results: str, *, actions: str = ""
    ) -> Verdict:
        """`actions` is what the platform records as having happened.

        Empty is allowed and means "not stated", not "nothing happened" - the
        two are different and only the caller can tell them apart, so nothing
        here guesses.
        """
        if not results.strip():
            # Nothing came back. No model call is needed to know that is not it.
            return Verdict.rejected(
                "No task produced a result", *objective.acceptance_criteria
            )
        if not objective.acceptance_criteria:
            # Nothing was written down to check against, which happens when the
            # reading of the request produced no criteria. Passing on that basis
            # would make this stage a no-op precisely when comprehension was
            # weakest - so the request itself becomes the standard, exactly as
            # the employee verifier falls back to the task when there is no plan.
            log.info("prometheus.verifying_against_the_request", objective_id=str(objective.id))

        prompt = render(
            "prometheus_verifier",
            objective=objective.text,
            criteria=_criteria(objective),
            results=results,
            actions=actions.strip() or "Not recorded.",
        )
        response = await self._llm.generate(
            LLMRequest(
                messages=(*dated(), Message.user(prompt)),
                temperature=0.0,
                response_format={"type": "json_object"},
            )
        )

        parsed = extract_object(response.content)
        if parsed is None:
            log.warning("prometheus.verdict_unreadable", objective_id=str(objective.id))
            return Verdict.rejected(
                "The check on the objective did not return a readable verdict",
                "a second opinion on whether this is done",
            )

        missing = _missing(parsed.get("missing"))
        passed = bool(parsed.get("passed", False))
        if passed and missing:
            # It said yes and then listed what is absent. The list is the more
            # specific claim, and the one a second attempt could act on.
            log.info(
                "prometheus.verdict_contradicted",
                objective_id=str(objective.id),
                missing=list(missing),
            )
            passed = False

        verdict = Verdict(
            passed=passed,
            reason=str(parsed.get("reason", "")).strip(),
            missing=missing,
        )
        log.info(
            "prometheus.objective_verified",
            objective_id=str(objective.id),
            passed=verdict.passed,
            missing=len(verdict.missing),
        )
        return verdict

    @staticmethod
    def routing() -> tuple[TaskKind, CapabilityRequirement, RoutingHints]:
        """A floor, not a hint: this is the last thing standing between a bad
        run and a user being told it succeeded, and hints do not filter."""
        return (
            TaskKind.VERIFICATION,
            CapabilityRequirement(min_quality=MIN_JUDGEMENT_QUALITY),
            RoutingHints(quality=0.7, cost_sensitivity=0.5),
        )


def _criteria(objective) -> str:
    if not objective.acceptance_criteria:
        return (
            "No criteria were written down for this objective. Judge the result "
            "against the request itself, exactly as it was stated."
        )
    return "\n".join(f"- {item}" for item in objective.acceptance_criteria)


#: What a model writes in `missing` when it means "nothing". Filtered rather
#: than trusted as a contradiction: a verdict of "passed, missing: none" is
#: agreeing with itself, not disagreeing.
_NOTHING = frozenset({"none", "nothing", "n/a", "na", "-", "no", "nil", "empty"})


def _missing(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list | tuple):
        return ()
    return tuple(
        text
        for item in raw
        if (text := str(item).strip()) and text.lower().rstrip(".") not in _NOTHING
    )
