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
"""

from __future__ import annotations

from uuid import UUID

import structlog

from application.prometheus.workforce import describe
from application.prompts import render
from domain.capabilities.models import CapabilityRequirement
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
from domain.workforce.routing import MIN_DELEGATION_QUALITY, Requirement, holders

log = structlog.get_logger(__name__)


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


class CapabilityDelegator:
    """Implements `domain.workforce.protocols.Delegator`."""

    def __init__(
        self,
        llm: LLM,
        registry: EmployeeRegistry,
        *,
        requirement: Requirement | None = None,
    ) -> None:
        self._llm = llm
        self._registry = registry
        self._requirement = requirement

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
        candidates = self._candidates(task, requirement)
        if not candidates:
            raise DelegationError(
                "No declared employee can take this task. Add one under employees/."
            )
        remaining = [d for d in candidates if d.name not in (avoid or set())]
        if remaining:
            candidates = remaining

        if len(candidates) == 1:
            chosen = candidates[0]
            reason = _why_only(requirement)
            extra = SharedContext()
        else:
            chosen, reason, extra = await self._ask(task, candidates)

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
        log.info(
            "prometheus.delegated",
            task_id=str(task.id),
            employee=chosen.name,
            reason=reason,
            candidates=[d.name for d in candidates],
            granted_tools=sorted(granted),
        )
        return chosen, passed, reason

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
        chosen, context, _ = await self.choose(task)
        return TaskAssignment.create(
            task_id=task.id,
            employee_id=chosen.id,
            assigned_by=ActorKind.PROMETHEUS,
            assigned_by_id="prometheus",
            context=context,
            workspace_id=task.workspace_id,
        )

    def employee_name(self, employee_id: UUID | None, workspace_id) -> str:
        """Resolve a persisted assignment without making a new decision."""
        for definition in self._registry.list(workspace_id):
            if definition.id == employee_id:
                return definition.name
        return str(employee_id) if employee_id is not None else "unassigned"

    # --- Internals ------------------------------------------------------------

    def _candidates(
        self, task: Task, requirement: Requirement | None
    ) -> list[EmployeeDefinition]:
        """Who could take this, narrowed by what it needs where that is known.

        Both axes narrow, and neither refuses. Nobody declaring what the task
        asks for is worth saying - it is usually a missing declaration rather
        than a missing employee - and is answered by widening back to the
        previous field rather than by failing the task.
        """
        wanted = requirement or self._requirement
        everyone = self._registry.list(task.workspace_id)
        if wanted is None or not wanted.narrows:
            return everyone

        found = everyone
        if wanted.capabilities.required:
            found = self._registry.find_by_capability(wanted.capabilities) or self._said(
                task, sorted(c.value for c in wanted.capabilities.required), everyone
            )
        if wanted.services:
            found = holders(found, wanted.services) or self._said(
                task, sorted(wanted.services), found
            )
        return found

    @staticmethod
    def _said(
        task: Task, needed: list[str], fallback: list[EmployeeDefinition]
    ) -> list[EmployeeDefinition]:
        log.info("prometheus.no_one_declares", task_id=str(task.id), needed=needed)
        return fallback

    async def _ask(
        self, task: Task, candidates: list[EmployeeDefinition]
    ) -> tuple[EmployeeDefinition, str, SharedContext]:
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
            return chosen, "the model did not name a declared employee; chosen by tools", (
                SharedContext()
            )

        return (
            chosen,
            str(parsed.get("reason", "")).strip() or "chosen by the manager",
            SharedContext(
                facts=_lines(parsed.get("facts")),
                constraints=_lines(parsed.get("constraints")),
            ),
        )


def _why_only(requirement: Requirement | None) -> str:
    """Why a field of one is a field of one - narrowed, or simply small."""
    if requirement is not None and requirement.narrows:
        return "the only employee that declares " + requirement.describe()
    return "the only employee available for this task"


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
