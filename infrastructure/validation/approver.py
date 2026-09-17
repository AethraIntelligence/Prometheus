"""The person a scenario says would have been there.

Every approval recorded by the first full validation pass was rejected by
`no-approver`: the suite runs with nothing attached to stdin, so every action
above the threshold was refused and three scenarios whose work begins with
writing a file could not pass on any model. The report then blamed approvals for
runs whose real problem was never reached.

Configuring the machine to allow was the obvious answer and the wrong one. It is
machine-wide, so it would also allow the scenario whose entire point is that the
platform refuses, and it would record a suite in which the brake was switched
off - which measures a different product.

So the answer is declared per scenario, per tool, and applies for exactly one
run. Anything not named is refused, because a suite where silence means yes is a
suite that stops noticing when the gate breaks.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from domain.approvals.models import ApprovalRequest
from domain.safety.emergency import EmergencyBrake
from infrastructure.observability.logging import get_logger

log = get_logger(__name__)


class DeclaredApprover:
    """Implements `domain.validation.protocols.Approver`."""

    def __init__(self, brake: EmergencyBrake | None = None) -> None:
        self._allowed: frozenset[str] = frozenset()
        self._stop_on: frozenset[str] = frozenset()
        self._brake = brake
        self._engaged_here = False

    def confirm(self, request: ApprovalRequest) -> bool:
        """Answer by tool name, never by reading the rendered action line."""
        if request.tool in self._stop_on and self._brake is not None:
            # A second person pulls the brake as the first one says yes.
            self._brake.engage(
                f"validation: stopped while {request.tool} was being approved", by="validation"
            )
            self._engaged_here = True
        answer = request.tool in self._allowed
        log.info(
            "validation.approval_answered",
            tool=request.tool,
            action=request.action[:80],
            approved=answer,
        )
        return answer

    @contextmanager
    def answering(
        self, allowed: frozenset[str], stop_on: frozenset[str] = frozenset()
    ) -> Iterator[None]:
        previous, previous_stop = self._allowed, self._stop_on
        self._allowed, self._stop_on = allowed, stop_on
        try:
            yield
        finally:
            self._allowed, self._stop_on = previous, previous_stop
            # Released only if this run pulled it: a stop a person set is theirs.
            if self._engaged_here and self._brake is not None:
                self._brake.release()
            self._engaged_here = False
