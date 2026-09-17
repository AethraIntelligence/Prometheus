"""Asking the person at the keyboard, and remembering what they said.

Three modes, because the honest answer differs by situation and guessing is
worse than configuring:

* **prompt** - a human is at a terminal, so ask them.
* **deny** - nobody is there (a scheduled run, a test, a piped process). The
  action is refused and recorded as refused. Silence is not consent.
* **allow** - the user has explicitly said they do not want to be asked on this
  machine. Still recorded, so `prometheus approvals` shows what was done under it.

Who gets asked is a separate question from whether to ask, and that is why the
confirmer is injected. On a terminal it reads stdin; under the local interface
it is a coroutine that parks until someone clicks in the browser. The rule -
anything but an explicit yes is a no - is the same either way, and lives in the
confirmer rather than being re-decided here.

Every request is written to the database *before* it is answered. A process
killed while waiting for a decision leaves a PENDING row, which is what makes
the question survivable rather than lost.

A question can also have a deadline. That is not a convenience: a PENDING row is
a task `prometheus resume` keeps picking up, so a question nobody ever answers is a
run that never finishes. Expiring is a refusal like any other - the rule that an
unconfirmed action does not happen covers "nobody was there" as much as "they
said no".
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from collections.abc import Awaitable, Callable
from enum import StrEnum
from uuid import UUID

from domain.approvals.models import Approval, ApprovalRequest, ApprovalState
from domain.approvals.protocols import ApprovalRepository
from domain.errors import NotFoundError
from domain.workforce import directions
from domain.workforce.directions import ApprovalChoice
from infrastructure.observability.logging import get_logger

log = get_logger(__name__)

#: Sync or async: a terminal answers immediately, a browser does not.
Confirmer = Callable[[ApprovalRequest], "bool | Awaitable[bool]"]


class ApprovalMode(StrEnum):
    PROMPT = "prompt"
    DENY = "deny"
    ALLOW = "allow"


def console_confirmer(request: ApprovalRequest) -> bool:
    """Ask on the terminal. Anything but an explicit yes is a no."""
    safe = request.redacted()
    print(f"\nApproval needed: {safe.action}", file=sys.stderr)
    if safe.reason:
        print(f"  why:  {safe.reason}", file=sys.stderr)
    print(f"  risk: {safe.risk_level.value}", file=sys.stderr)
    if safe.scope:
        print(
            f"  scope: {safe.scope.subject} may {safe.scope.action} on "
            f"{safe.scope.resource}",
            file=sys.stderr,
        )
    if safe.preview:
        rendered = str(safe.preview)
        print(f"  preview: {rendered[:2_000]}", file=sys.stderr)
    try:
        answer = input("Allow this action? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return False
    return answer.strip().lower() in {"y", "yes"}


class LocalApprovalService:
    """Implements `domain.approvals.protocols.ApprovalService`."""

    def __init__(
        self,
        repository: ApprovalRepository,
        *,
        mode: ApprovalMode | str = ApprovalMode.PROMPT,
        confirmer: Confirmer | None = None,
        ttl_seconds: float = 0.0,
        is_interactive: Callable[[], bool] = lambda: sys.stdin is not None and sys.stdin.isatty(),
    ) -> None:
        self._repository = repository
        self._mode = ApprovalMode(mode)
        self._confirmer = confirmer or console_confirmer
        self._ttl_seconds = ttl_seconds
        self._is_interactive = is_interactive

    async def request(self, action: ApprovalRequest) -> ApprovalState:
        action = action.expiring_in(self._ttl_seconds)
        approval = Approval(request=action.redacted())
        await self._repository.save(approval)

        decision, resolved_by = await self._decide(action)
        # The interface may have attached an exact grant while the confirmer
        # was waiting. Resolve the current row so that concurrent decision
        # metadata is not overwritten by the original PENDING value.
        approval = await self._repository.get(action.id) or approval
        await self._repository.save(
            approval.resolve(decision, resolved_by=resolved_by, comment=action.reason)
        )
        log.info(
            "approval.resolved",
            approval_id=str(action.id),
            action=action.action,
            risk=action.risk_level.value,
            state=decision.value,
            resolved_by=resolved_by,
        )
        return decision

    async def _decide(self, action: ApprovalRequest) -> tuple[ApprovalState, str]:
        if self._mode is ApprovalMode.ALLOW:
            return ApprovalState.APPROVED, "configuration"
        if self._mode is ApprovalMode.DENY or not self._is_interactive():
            return ApprovalState.REJECTED, "no-approver"
        # Only here, where this machine would have asked somebody: a request
        # cannot talk a machine configured to refuse into doing the thing.
        if directions.current().approvals is ApprovalChoice.AUTO:
            return ApprovalState.APPROVED, "request"
        answer = self._confirmer(action.redacted())
        if not inspect.isawaitable(answer):
            # A synchronous confirmer is the terminal, and a person at a
            # terminal is not something to time out on: they are standing there,
            # and stealing the prompt from under them would be worse than
            # waiting. Only an awaited answer - a browser, a chat - can expire.
            return (ApprovalState.APPROVED if answer else ApprovalState.REJECTED), "user"
        try:
            approved = await self._within_deadline(answer)
        except TimeoutError:
            log.info("approval.expired", approval_id=str(action.id), action=action.action)
            return ApprovalState.EXPIRED, "timeout"
        return (ApprovalState.APPROVED if approved else ApprovalState.REJECTED), "user"

    async def _within_deadline(self, answer: Awaitable[bool]) -> bool:
        if self._ttl_seconds <= 0:
            return await answer
        return await asyncio.wait_for(answer, timeout=self._ttl_seconds)

    async def resolve(
        self,
        approval_id: UUID,
        decision: ApprovalState,
        *,
        resolved_by: str = "user",
        comment: str = "",
    ) -> None:
        approval = await self._repository.get(approval_id)
        if approval is None:
            raise NotFoundError(f"Unknown approval: {approval_id}")
        await self._repository.save(
            approval.resolve(decision, resolved_by=resolved_by, comment=comment)
        )
