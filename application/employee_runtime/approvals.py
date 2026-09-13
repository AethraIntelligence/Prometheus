"""The brake that comes with the hands.

Phase 4 put one rule on the single path every tool call takes: an action the
user cannot undo does not happen until the user says so. Phase 10 keeps the
position and replaces the rule with a policy engine, which changes three things.

**A denial is not a question.** Risk alone can only ever produce "ask"; a
declared policy can produce "no". A tool this employee is forbidden is refused
without a prompt, because offering the user a chance to allow one call of
something an employee was declared not to do turns the declaration into a
suggestion.

**Waiting is a state of the task, not of the coroutine.** A task parked on a
person is moved to WAITING_FOR_APPROVAL and back, so `prometheus tasks` says what a
run is actually doing rather than showing it as RUNNING with nothing happening.

**The refusals are recorded here, and only the refusals.** A denied action and
a declined approval are the two things that leave no trace anywhere else - the
tool never ran, so there is no tool call to account for. What did run is audited
by the executor, and what the user answered is in the approvals table; writing a
second line here for a call that is about to be audited anyway would mean every
approved action appearing twice in the one place people read to count them.

The gate stays between the executor and the tool rather than inside either. In
the executor it would be checked once and forgotten by the next tool; inside
each tool it would be re-implemented per tool, and the one that forgot would be
the one that mattered.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog

from domain.approvals.gate import describe, resolve_risk
from domain.approvals.models import ApprovalRequest, ApprovalState
from domain.approvals.protocols import ApprovalService
from domain.audit.protocols import AuditLog, AuditRecord
from domain.employees.definition import EmployeeDefinition
from domain.policies.engine import PolicyEngine, PolicyRequest
from domain.policies.models import Decision
from domain.policies.rules import RuleBasedPolicyEngine
from domain.secrets.models import redact
from domain.tasks.task import Task, TaskStatus
from domain.tools.protocols import RiskAssessor, Tool
from domain.workforce import directions
from domain.workforce.directions import ApprovalChoice

log = structlog.get_logger(__name__)

#: Told to the executor when an action was refused, and through it to the model.
#: Phrased as a fact plus a way forward: a model told only "denied" retries.
_DENIED = (
    "{action} was refused: {reason}. Do not retry it; find another way or say why you cannot."
)
_NOT_APPROVED = (
    "The user did not approve this action ({state}): {reason}. "
    "Do not retry it; find another way or say why you cannot."
)

#: Called when the task starts and stops waiting on a person.
StatusSink = Callable[[TaskStatus], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class GateOutcome:
    allowed: bool
    reason: str = ""


ALLOWED = GateOutcome(allowed=True)


class ApprovalGate:
    """Decides, and where needed asks, before a tool runs."""

    def __init__(
        self,
        service: ApprovalService | None = None,
        *,
        engine: PolicyEngine | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        self._service = service
        self._engine = engine or RuleBasedPolicyEngine()
        self._audit = audit

    async def check(
        self,
        tool: Tool,
        input_data: dict[str, Any],
        task: Task,
        definition: EmployeeDefinition,
        *,
        status: StatusSink | None = None,
    ) -> GateOutcome:
        assessment = tool.assess(input_data) if isinstance(tool, RiskAssessor) else None
        action = describe(tool.spec, input_data)
        decision = self._engine.evaluate(
            PolicyRequest(
                actor=definition,
                action=action,
                tool=tool.spec.name,
                payload=redact(input_data),
                effect=tool.spec.effect,
                risk_level=resolve_risk(tool.spec, assessment),
                reversible=tool.spec.reversible,
                policies=definition.policies,
                risk_reason=assessment.reason if assessment else "",
            )
        )

        if decision.decision is Decision.ALLOW:
            return ALLOWED

        if decision.decision is Decision.DENY:
            log.info("policy.denied", tool=tool.spec.name, reason=decision.reason)
            await self._record(task, definition, tool, action, "DENIED", decision.reason)
            return GateOutcome(
                allowed=False, reason=_DENIED.format(action=action, reason=decision.reason)
            )

        if directions.current().approvals is ApprovalChoice.DENY:
            # Checked before the approver, because refusing needs nobody: it is
            # the one answer that is always the person's to give in advance.
            reason = "this request was set to refuse anything that needs approval"
            await self._record(task, definition, tool, action, "DENIED", reason)
            return GateOutcome(allowed=False, reason=_DENIED.format(action=action, reason=reason))

        if self._service is None:
            # No configured way to ask means no way to say yes. Refusing is the
            # only answer that cannot do damage.
            reason = (
                f"{tool.spec.name} needs the user's approval and no approver "
                "is configured on this machine."
            )
            await self._record(task, definition, tool, action, "DENIED", reason)
            return GateOutcome(allowed=False, reason=reason)

        request = ApprovalRequest.create(
            task_id=task.id,
            action=action,
            tool=tool.spec.name,
            payload=redact(input_data),
            risk_level=decision.risk_level,
            workspace_id=task.workspace_id,
            requested_by_employee_id=definition.id,
            reason=decision.reason,
        )
        log.info(
            "approval.requested",
            task_id=str(task.id),
            tool=tool.spec.name,
            risk=decision.risk_level.value,
        )
        state = await self._ask(request, status)
        if state is ApprovalState.APPROVED:
            return ALLOWED
        await self._record(task, definition, tool, action, "DENIED", decision.reason)
        return GateOutcome(
            allowed=False,
            reason=_NOT_APPROVED.format(state=state.value.lower(), reason=decision.reason),
        )

    async def _ask(
        self, request: ApprovalRequest, status: StatusSink | None
    ) -> ApprovalState:
        """Ask, with the task marked as waiting for the whole time it takes.

        The status is restored whatever the answer was, including an exception:
        a task left in WAITING_FOR_APPROVAL by a failed question would be
        resumable forever, waiting on a person nobody asked.
        """
        assert self._service is not None
        await self._set_status(status, TaskStatus.WAITING_FOR_APPROVAL)
        try:
            return await self._service.request(request)
        finally:
            await self._set_status(status, TaskStatus.RUNNING)

    @staticmethod
    async def _set_status(status: StatusSink | None, value: TaskStatus) -> None:
        if status is None:
            return
        try:
            await status(value)
        except Exception as error:  # bookkeeping must not fail the decision
            log.warning("approval.status_not_recorded", status=value.value, error=str(error))

    async def _record(
        self,
        task: Task,
        definition: EmployeeDefinition,
        tool: Tool,
        action: str,
        result: str,
        reason: str,
    ) -> None:
        """Write the decision down. Never at the cost of the decision itself."""
        if self._audit is None:
            return
        try:
            await self._audit.record(
                AuditRecord(
                    action=action,
                    actor_kind=definition.actor_kind,
                    actor_id=definition.actor_id,
                    result=result,
                    workspace_id=task.workspace_id,
                    task_id=task.id,
                    tool=tool.spec.name,
                    details={"reason": reason, "effect": tool.spec.effect.value},
                )
            )
        except Exception as error:
            log.warning("audit.not_recorded", action=action, error=str(error))
