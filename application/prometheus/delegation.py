"""Choosing who does a task, and handing it down without handing down more.

§7.6 is the load-bearing sentence of this phase: *zero mentions of a concrete
employee in Prometheus's code.* Candidates come from `EmployeeRegistry` and nowhere
else, so the workforce is a directory of declarations rather than a list in this
file. Delete a declaration and nothing here changes; add one and it is offered
on the next run. `tests/unit/test_prometheus_governance.py` enforces that by reading
`employees/` and failing if any of those names appears in this package - prose
included, because a name in a comment is a name that will be in a branch later.

**The field is narrowed by what the work needs, before anybody is asked.** The
plan says what each task requires - a capability, or the name of a connected
service - and `EmployeeRegistry.find_by_capability` and
`domain.workforce.routing.holders` answer who qualifies. The second axis is
Phase 18's: every term in the capability vocabulary is already claimed by
somebody the platform ships with, so an integration could only ever contribute
one that four employees already declare. Narrowing is what makes a declared
capability worth declaring, and what keeps a workforce of thirty a search rather
than thirty cards in a prompt. A narrowing that leaves nobody is discarded
rather than obeyed - a task routed to no one is worse than a task routed
imperfectly, and the requirement was a hint about the work, not a rule about the
workforce.

**One candidate needs no model call.** A workforce of one has nothing to choose
between, and asking a model to pick from a list of one spends money to be told
what was already true. Narrowing often produces exactly that, which is the point:
the only employee that can run code gets the task that needs code, for free.

**A choice the model gets wrong is corrected, not obeyed.** Names are checked
against the registry; an invented one falls back to the ranking below. The model
ranks, it does not authorise.

**Delegation never escalates privileges.** The employee's own declaration is the
only source of what it may use, so Prometheus cannot widen it by asking. It can
deliberately *narrow* - `SharedContext.granted_tools` records what the manager
meant to allow - and the runtime intersects that with the declaration, so the
narrowing is real and the widening is impossible. `effective_tools` in
`domain/policies` is exactly that intersection, and this is its caller.

**Nobody unavailable is offered, and the decision is kept** (Phase 11). Before
anybody is narrowed by what the task needs, the field loses whoever cannot work
here right now - an integration not connected, no model able to run them, a
capability whose tool this machine lacks - and, for a task that depends on
earlier work, whoever cannot start from what that work actually delivered. Each
one passed over is recorded with a code, on the assignment, next to the reason
the chosen one was chosen. A hand-off nobody can take is not forced onto the
least bad candidate: the task is not started, and the plan hears why.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import structlog

from application.prometheus.workforce import describe, profile
from application.prompts import render
from domain.capabilities.models import CapabilityRequirement
from domain.decisions.models import ChoiceQuestion, Option
from domain.decisions.protocols import Decider
from domain.employees.definition import EmployeeDefinition
from domain.employees.protocols import EmployeeRegistry
from domain.errors import DelegationError
from domain.llm.json_output import extract_object
from domain.llm.models import LLMRequest, Message, RoutingHints, TaskKind
from domain.llm.protocols import LLM
from domain.policies.models import Actor, ActorKind, SimpleActor, effective_tools
from domain.secrets.models import is_sensitive
from domain.tasks.task import Task
from domain.workforce.assignment import SharedContext, TaskAssignment
from domain.workforce.decision import Alternative, Decision, RejectionCode, SelectionCode
from domain.workforce.handoff import Delivery, compatible, explain
from domain.workforce.readiness import WorkforceReadiness
from domain.workforce.routing import MIN_DELEGATION_QUALITY, Requirement, holders

log = structlog.get_logger(__name__)

#: How sure a decision model must be, measured, before its choice is taken
#: without the text model's. Below it - or unmeasured - the question goes the
#: long way, because who does the work commits a whole employee run.
DELEGATION_CONFIDENCE = 0.6

#: A typed choice lists every candidate as an option; past this many, the
#: question is asked the long way. Well within what either backend takes.
MAX_OPTIONS = 26


def manager_actor(workforce: list[EmployeeDefinition]) -> Actor:
    """Prometheus as an actor, whose reach is the union of what its people may do.

    Not a wildcard. A manager that could grant anything would make
    `effective_tools` an identity function and the intersection meaningless;
    this way Prometheus can hand down exactly what somebody was already trusted with,
    and nothing that nobody was.
    """
    return SimpleActor(
        actor_id="prometheus",
        actor_kind=ActorKind.PROMETHEUS,
        allowed_tools=frozenset().union(*(d.allowed_tools for d in workforce)) if workforce
        else frozenset(),
    )


@dataclass(frozen=True, slots=True)
class Delegation:
    """Who takes the task - or nobody - what they are told, and the recorded decision."""

    chosen: EmployeeDefinition | None
    context: SharedContext
    decision: Decision

    @property
    def reason(self) -> str:
        return self.decision.reason


class CapabilityDelegator:
    """Implements `domain.workforce.protocols.Delegator`."""

    def __init__(
        self,
        llm: LLM,
        registry: EmployeeRegistry,
        *,
        requirement: Requirement | None = None,
        readiness: WorkforceReadiness | None = None,
        decider: Decider | None = None,
    ) -> None:
        """`decider` is asked first where one is given - the composition root
        gives one that answers only through a model built to decide - and its
        choice stands only when it is measured as sure (`DELEGATION_CONFIDENCE`).
        Anything else is asked of `llm` as before."""
        self._llm = llm
        self._registry = registry
        self._decider = decider
        self._requirement = requirement
        # None where nothing can tell - a test's workforce, a surface built
        # without tools. Everybody is then taken as able, which is how
        # delegation behaved before readiness existed.
        self._readiness = readiness

    def definition(self, name: str, workspace_id) -> EmployeeDefinition | None:
        """A declared employee by name, without deciding anything."""
        return next((d for d in self._registry.list(workspace_id) if d.name == name), None)

    async def choose(
        self,
        task: Task,
        *,
        context: SharedContext | None = None,
        avoid: set[str] | None = None,
        requirement: Requirement | None = None,
    ) -> tuple[EmployeeDefinition, SharedContext, str]:
        """Who should do this, what they are told, and why they were picked.

        `avoid` names employees a previous attempt already proved cannot reach
        what this task needs. It is a preference, not a prohibition: if avoiding
        them leaves nobody, the task goes back to the best of a bad field rather
        than failing for want of a second option.
        """
        delegation = await self.decide(
            task, context=context, avoid=avoid, requirement=requirement
        )
        if delegation.chosen is None:
            raise DelegationError(delegation.reason)
        return delegation.chosen, delegation.context, delegation.reason

    async def decide(
        self,
        task: Task,
        *,
        context: SharedContext | None = None,
        avoid: set[str] | None = None,
        requirement: Requirement | None = None,
        upstream: tuple[Delivery, ...] = (),
    ) -> Delegation:
        """The whole decision, including who was passed over and why.

        Raises `DelegationError` only when nobody is declared, or nobody
        declared can work here at all - facts about the workforce. A hand-off
        nobody can take is a fact about this plan, and comes back as a
        delegation with nobody chosen.
        """
        everyone = self._registry.list(task.workspace_id)
        if not everyone:
            raise DelegationError(
                "No declared employee can take this task. Add one under employees/."
            )
        wanted = requirement or self._requirement
        rejected: dict[str, Alternative] = {}

        able = self._able(everyone, wanted, rejected)
        if not able:
            raise DelegationError(
                "No declared employee can work here right now: "
                + "; ".join(f"{item.employee} - {item.reason}" for item in rejected.values())
            )

        narrowed = self._candidates(task, wanted, able, rejected)
        candidates = [d for d in narrowed if compatible(d.contract, upstream)]
        if upstream and not candidates:
            # The narrowing was a hint about the work; the hand-off is a fact
            # about what exists to work from. Widen before giving up.
            candidates = [d for d in able if compatible(d.contract, upstream)]
        for definition in able:
            if upstream and not compatible(definition.contract, upstream):
                rejected[definition.name] = Alternative(
                    definition.name,
                    RejectionCode.HANDOFF_INCOMPATIBLE,
                    explain(definition.contract, upstream),
                )
            elif definition not in candidates:
                rejected.setdefault(
                    definition.name,
                    Alternative(definition.name, RejectionCode.LACKS_CAPABILITY, ""),
                )
            else:
                rejected.pop(definition.name, None)
        if not candidates:
            decision = Decision(
                code=SelectionCode.NO_COMPATIBLE_EMPLOYEE,
                reason=(
                    "Nobody can start from what the earlier work delivered: "
                    + "; ".join(item.reason for item in rejected.values() if item.reason)
                ),
                alternatives=tuple(rejected.values()),
            )
            log.info("prometheus.handoff_refused", task_id=str(task.id), reason=decision.reason)
            return Delegation(chosen=None, context=context or SharedContext(), decision=decision)

        remaining = [d for d in candidates if d.name not in (avoid or set())]
        if remaining:
            for definition in candidates:
                if definition not in remaining:
                    rejected[definition.name] = Alternative(
                        definition.name,
                        RejectionCode.AVOIDED_AFTER_FAILURE,
                        "an earlier attempt showed it could not reach what this task needs",
                    )
            candidates = remaining

        indistinguishable: tuple[str, ...] = ()
        if len(candidates) == 1:
            chosen = candidates[0]
            reason = _why_only(wanted)
            code = (
                SelectionCode.ONLY_QUALIFIED
                if rejected or (wanted is not None and wanted.narrows)
                else SelectionCode.ONLY_EMPLOYEE
            )
            extra = SharedContext()
        else:
            chosen, reason, extra, fell_back = await self._ask(task, candidates)
            twins = tuple(sorted(d.name for d in candidates if _signature(d) == _signature(chosen)))
            code = SelectionCode.FALLBACK if fell_back else SelectionCode.MODEL_CHOSE
            if len(twins) > 1:
                indistinguishable = twins
                if not fell_back:
                    code = SelectionCode.MODEL_CHOSE_AMONG_EQUALS
            for definition in candidates:
                if definition is not chosen:
                    rejected[definition.name] = Alternative(
                        definition.name,
                        RejectionCode.NOT_CHOSEN,
                        "declared the same work with nothing to tell them apart"
                        if definition.name in indistinguishable
                        else "qualified, and ranked below the chosen employee",
                    )

        passed = _merge(context, extra)
        granted = effective_tools(manager_actor(candidates), chosen)
        passed = SharedContext(
            facts=passed.facts,
            constraints=passed.constraints,
            artifacts=passed.artifacts,
            # Recorded on the assignment, applied by the runtime. What Prometheus meant
            # to allow is then answerable from the row, not from a log line.
            data={**passed.data, "granted_tools": sorted(granted)},
        )
        rejected.pop(chosen.name, None)
        decision = Decision(
            code=code,
            reason=reason,
            alternatives=tuple(sorted(rejected.values(), key=lambda item: item.employee)),
            indistinguishable=indistinguishable,
        )
        log.info(
            "prometheus.delegated",
            task_id=str(task.id),
            employee=chosen.name,
            reason=reason,
            code=code.value,
            candidates=[d.name for d in candidates],
            passed_over={item.employee: item.code.value for item in decision.alternatives},
            granted_tools=sorted(granted),
        )
        return Delegation(chosen=chosen, context=passed, decision=decision)

    @staticmethod
    def routing() -> tuple[TaskKind, CapabilityRequirement, RoutingHints]:
        """Short work, and not cheap work: see `MIN_DELEGATION_QUALITY`.

        Still `EXTRACTION` - a card is read and a name comes back, which is what
        that kind describes - but with a floor under it, so a catalog whose
        cheapest entry is the default for extraction cannot be the thing that
        decides who does the work.
        """
        return (
            TaskKind.EXTRACTION,
            CapabilityRequirement(min_quality=MIN_DELEGATION_QUALITY),
            RoutingHints(quality=0.6, cost_sensitivity=0.6),
        )

    async def delegate(self, task: Task) -> TaskAssignment:
        """The `Delegator` contract: an assignment, not yet persisted."""
        delegation = await self.decide(task)
        if delegation.chosen is None:
            raise DelegationError(delegation.reason)
        return TaskAssignment.create(
            task_id=task.id,
            employee_id=delegation.chosen.id,
            assigned_by=ActorKind.PROMETHEUS,
            assigned_by_id="prometheus",
            context=delegation.context,
            workspace_id=task.workspace_id,
            decision=delegation.decision,
        )

    def employee_name(self, employee_id: UUID | None, workspace_id) -> str:
        """Resolve a persisted assignment without making a new decision."""
        for definition in self._registry.list(workspace_id):
            if definition.id == employee_id:
                return definition.name
        return str(employee_id) if employee_id is not None else "unassigned"

    # --- Internals ------------------------------------------------------------

    def _able(
        self,
        everyone: list[EmployeeDefinition],
        wanted: Requirement | None,
        rejected: dict[str, Alternative],
    ) -> list[EmployeeDefinition]:
        """Everybody who can work here now - and, for this task, still can do what it needs."""
        if self._readiness is None:
            return list(everyone)
        able: list[EmployeeDefinition] = []
        needed = wanted.capabilities.required if wanted is not None else frozenset()
        for definition in everyone:
            try:
                readiness = self._readiness.readiness(definition)
            except Exception as error:  # an unanswerable check must not stop the work
                log.warning(
                    "prometheus.readiness_unknown", employee=definition.name, error=str(error)
                )
                able.append(definition)
                continue
            if not readiness.assignable:
                rejected[definition.name] = Alternative(
                    definition.name, RejectionCode.UNAVAILABLE, readiness.explain()
                )
            elif needed & readiness.lost_capabilities:
                lost = ", ".join(sorted(c.value for c in needed & readiness.lost_capabilities))
                rejected[definition.name] = Alternative(
                    definition.name,
                    RejectionCode.CAPABILITY_LOST_HERE,
                    f"declares {lost}, which nothing it may use provides on this machine",
                )
            else:
                able.append(definition)
        return able

    def _candidates(
        self,
        task: Task,
        wanted: Requirement | None,
        everyone: list[EmployeeDefinition],
        rejected: dict[str, Alternative],
    ) -> list[EmployeeDefinition]:
        """Who could take this, narrowed by what it needs where that is known.

        Both axes narrow, and neither refuses. Nobody declaring what the task
        asks for is worth saying - it is usually a missing declaration rather
        than a missing employee - and is answered by widening back to the
        previous field rather than by failing the task.
        """
        if wanted is None or not wanted.narrows:
            return everyone

        found = everyone
        if wanted.capabilities.required:
            names = {d.name for d in self._registry.find_by_capability(wanted.capabilities)}
            declaring = [d for d in everyone if d.name in names]
            needed = ", ".join(sorted(c.value for c in wanted.capabilities.required))
            if declaring:
                for definition in everyone:
                    if definition not in declaring:
                        rejected[definition.name] = Alternative(
                            definition.name,
                            RejectionCode.LACKS_CAPABILITY,
                            f"does not declare {needed}",
                        )
                found = declaring
            else:
                found = self._said(
                    task, sorted(c.value for c in wanted.capabilities.required), everyone
                )
        if wanted.services:
            holding = holders(found, wanted.services)
            if holding:
                services = ", ".join(sorted(wanted.services))
                for definition in found:
                    if definition not in holding:
                        rejected[definition.name] = Alternative(
                            definition.name,
                            RejectionCode.LACKS_SERVICE,
                            f"does not hold {services}",
                        )
                found = holding
            else:
                found = self._said(task, sorted(wanted.services), found)
        return found

    @staticmethod
    def _said(
        task: Task, needed: list[str], fallback: list[EmployeeDefinition]
    ) -> list[EmployeeDefinition]:
        log.info("prometheus.no_one_declares", task_id=str(task.id), needed=needed)
        return fallback

    async def _decide(
        self, task: Task, candidates: list[EmployeeDefinition]
    ) -> tuple[EmployeeDefinition, str] | None:
        """The decision model's choice, when there is one and it is measured as sure."""
        if self._decider is None or len(candidates) > MAX_OPTIONS:
            return None
        try:
            answer = await self._decider.choose(
                ChoiceQuestion(
                    state=task.goal,
                    question=render("prometheus_delegation_choice").strip(),
                    options=tuple(Option(d.name, profile(d)) for d in candidates),
                    purpose="delegation",
                )
            )
        except Exception as error:  # a second opinion that fails is not a failed hand-off
            log.warning("prometheus.decider_failed", task_id=str(task.id), error=str(error))
            return None
        by_name = {d.name: d for d in candidates}
        chosen = by_name.get(answer.key or "")
        sure = answer.confidence is not None and answer.confidence >= DELEGATION_CONFIDENCE
        log.info(
            "prometheus.delegation_decided",
            task_id=str(task.id),
            employee=answer.key,
            confidence=answer.confidence,
            source=answer.source,
            taken=bool(chosen and sure),
        )
        if chosen is None or not sure or answer.confidence is None:
            return None
        return chosen, _measured(answer.confidence)

    async def _ask(
        self, task: Task, candidates: list[EmployeeDefinition]
    ) -> tuple[EmployeeDefinition, str, SharedContext, bool]:
        decided = await self._decide(task, candidates)
        if decided is not None:
            # Nothing is handed down besides the name, and on purpose: the
            # prose path's facts and constraints are written from the task text
            # and the cards alone, so they restate what the employee is given.
            picked, why = decided
            return picked, why, SharedContext(), False
        prompt = render("prometheus_delegation", goal=task.goal, candidates=describe(candidates))
        response = await self._llm.generate(
            LLMRequest(
                messages=(Message.user(prompt),),
                temperature=0.0,
                response_format={"type": "json_object"},
            )
        )
        parsed = extract_object(response.content) or {}

        by_name = {d.name: d for d in candidates}
        chosen = by_name.get(str(parsed.get("employee", "")).strip())
        if chosen is None:
            # Not an error: a name that is not on the list is the model failing
            # to choose, and the work still has to go somewhere sensible.
            chosen = _best_by_tools(task, candidates)
            log.info(
                "prometheus.delegation_fallback",
                task_id=str(task.id),
                offered=str(parsed.get("employee", ""))[:64],
                employee=chosen.name,
            )
            return (
                chosen,
                "the model did not name a declared employee; chosen by tools",
                SharedContext(),
                True,
            )

        return (
            chosen,
            _reason(parsed.get("reason")),
            SharedContext(
                facts=_lines(parsed.get("facts")),
                constraints=_lines(parsed.get("constraints")),
            ),
            False,
        )


def _measured(confidence: float) -> str:
    return f"chosen by a decision model, {confidence:.0%} sure"


def _why_only(requirement: Requirement | None) -> str:
    """Why a field of one is a field of one - narrowed, or simply small."""
    if requirement is not None and requirement.narrows:
        return "the only employee that declares " + requirement.describe()
    return "the only employee available for this task"


def _signature(definition: EmployeeDefinition) -> tuple:
    """What routing can tell two employees apart by. Equal means indistinguishable."""
    return (
        frozenset(definition.capabilities),
        frozenset(definition.integrations),
        frozenset(definition.allowed_tools),
        definition.contract.produces,
    )


def _best_by_tools(task: Task, candidates: list[EmployeeDefinition]) -> EmployeeDefinition:
    """A deterministic fallback: whose tools the task's words point at.

    Crude on purpose. It exists so a 20B model that answers with a role title
    instead of a name does not stop the work, and it is stable, so the same
    task falls back to the same person twice.
    """
    words = set(task.goal.lower().replace(".", " ").replace(",", " ").split())

    def score(definition: EmployeeDefinition) -> tuple[int, int, str]:
        hits = sum(
            1
            for tool in definition.allowed_tools
            for part in tool.replace(".", " ").split()
            if part in words
        )
        # More tools breaks a tie towards the employee who can reach more of the
        # world, and the name breaks the last one so the choice is repeatable.
        return (hits, len(definition.allowed_tools), definition.name)

    return max(candidates, key=score)


def _merge(base: SharedContext | None, extra: SharedContext) -> SharedContext:
    if base is None:
        return extra
    return SharedContext(
        facts=(*base.facts, *extra.facts),
        constraints=(*base.constraints, *extra.constraints),
        artifacts=base.artifacts,
        data=base.data,
    )


def _reason(raw: object) -> str:
    """The model's reason for its choice, as it will be stored and shown.

    Since Phase 11 this sentence is kept on the assignment and rendered in the
    Work Center, so it gets the same treatment as a line of passed-down context:
    one that mentions a credential is not kept at all, and a runaway one is cut.
    """
    text = " ".join(str(raw or "").split())[:500]
    if not text:
        return "chosen by the manager"
    if is_sensitive(text):
        log.warning("prometheus.reason_withheld", reason="looks like a credential")
        return "chosen by the manager (its stated reason was withheld)"
    return text


def _lines(raw: object) -> tuple[str, ...]:
    """What Prometheus passes down, with anything credential-shaped dropped on the way.

    `redact` masks by argument *name*, which is right for a tool call and no use
    at all here: these are sentences. So a line that mentions a credential is
    dropped whole rather than masked in part - a manager has no legitimate
    reason to write one, because a secret is resolved inside the tool that needs
    it and never travels. Dropping the line is the conservative reading of an
    ambiguous one, and the cost of being wrong is a fact the employee has to ask
    about rather than a key in the database, the transcript and the next prompt.

    A heuristic, and named as one. It is the last of several defences, not the
    only one.
    """
    if not isinstance(raw, list | tuple):
        return ()
    kept: list[str] = []
    for item in raw:
        text = str(item).strip()
        if not text:
            continue
        if is_sensitive(text):
            log.warning("prometheus.context_line_withheld", reason="looks like a credential")
            continue
        kept.append(text)
    return tuple(kept)
