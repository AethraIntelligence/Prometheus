"""The emergency stop as an operation: set the brake, then reach everything it has to stop.

`domain.safety.emergency` says what the state is. This says what pulling it
does, and the order is the contract.

1. **The record is written first.** From that moment the task runner refuses
   new work, the scheduler fires nothing, and the executor refuses every effect
   it has not already started - including one whose approval arrives a moment
   later. Everything after this step is cleanup of work that can no longer act.
2. **The sweep.** Parked approvals are answered no, running tasks are asked to
   stop at their next boundary, the manager's objectives are cancelled and
   closed, work left by a crashed process is closed in the store, standing
   capability leases are revoked, connections to services are closed and
   sandboxed programs are killed.
3. **The audit line**, in every workspace, naming who stopped and what was
   reached. A sweep that half-failed says which half.

Each part of the sweep is guarded on its own. A stop whose lease revocation
raised must still disconnect the services and kill the sandbox; the brake is
already on, so a failed cleanup step is reported and never undoes it.

**A stop set from outside is enforced too.** `prometheus stop` in a second
terminal writes the same record and cannot reach this process's memory, so
`watch` notices the record changing and runs the same sweep - and `enforce`
runs it at start-up when the record was already there, so a machine that
crashed after a stop comes back stopped with nothing resumed.

**Releasing starts nothing.** Cancelled work stays cancelled, and revoked
leases stay revoked; what comes back is the ability to start work, the
scheduler's next pass and the connections to services.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import structlog

from application.integrations.service import IntegrationService
from application.interface.runs import HaltedWork, Runs
from domain.approvals.protocols import CapabilityLeaseRepository
from domain.audit.protocols import AuditLog, AuditRecord
from domain.policies.models import ActorKind
from domain.safety.emergency import EmergencyBrake, Halter, StopState
from domain.tasks.cancellation import LiveTasks
from domain.tasks.repository import TaskRepository
from domain.tasks.task import Task
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId

log = structlog.get_logger(__name__)

#: How often a running process looks for a stop somebody set from outside. The
#: executor reads the record itself before every effect, so this bounds how long
#: cleanup takes to begin, not how long an action can slip through.
WATCH_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class StopReport:
    """What a stop or a release reached. Never a claim that an effect was undone."""

    state: StopState
    halted: HaltedWork = field(default_factory=HaltedWork)
    leases_revoked: int = 0
    halted_capabilities: dict[str, int] = field(default_factory=dict)
    #: Parts of the sweep that raised, by name. The brake stays on regardless.
    failures: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.to_dict(),
            "objectives_cancelled": self.halted.objectives_cancelled,
            "tasks_signalled": self.halted.tasks_signalled,
            "tasks_closed": self.halted.tasks_closed,
            "approvals_released": self.halted.approvals_released,
            "leases_revoked": self.leases_revoked,
            "halted": dict(self.halted_capabilities),
            "failures": list(self.failures),
            # Said on every report, because it is the first question a person
            # who pulled a brake asks and the answer never changes.
            "completed_effects_undone": False,
        }


class EmergencyStopControl:
    """Engage, release and enforce the machine-wide stop."""

    def __init__(
        self,
        *,
        brake: EmergencyBrake,
        runs: Runs,
        tasks: TaskRepository,
        workspaces: Callable[[], Awaitable[Iterable[WorkspaceId]]],
        audit: AuditLog | None = None,
        leases: CapabilityLeaseRepository | None = None,
        integrations: IntegrationService | None = None,
        halters: tuple[Halter, ...] = (),
        live: LiveTasks | None = None,
    ) -> None:
        self._brake = brake
        self._runs = runs
        self._tasks = tasks
        self._workspaces = workspaces
        self._audit = audit
        self._leases = leases
        self._integrations = integrations
        self._halters = halters
        self._live = live
        #: What this process last acted on, so `watch` sweeps once per change
        #: rather than once per look, and never re-sweeps its own engage.
        self._seen_engaged = brake.engaged()
        self._lock = asyncio.Lock()

    def state(self) -> StopState:
        return self._brake.state()

    # --- Engaging -------------------------------------------------------------

    async def engage(self, reason: str = "", *, by: str = "user") -> StopReport:
        """Stop all work now. Idempotent: a second stop sweeps again and changes nothing else."""
        async with self._lock:
            state = self._brake.engage(reason, by=by)
            self._seen_engaged = True
            report = await self._sweep(state)
            await self._record("emergency_stop.engaged", report, actor=by)
            return report

    async def enforce(self) -> StopReport | None:
        """At start-up: if the machine is stopped, make sure nothing it held is still live."""
        async with self._lock:
            state = self._brake.state()
            self._seen_engaged = state.engaged
            if not state.engaged:
                return None
            report = await self._sweep(state)
            await self._record("emergency_stop.enforced", report, actor="system")
            return report

    # --- Releasing ------------------------------------------------------------

    async def release(self, *, by: str = "user") -> StopReport:
        """Allow work again. Starts nothing, resumes nothing, restores no lease."""
        async with self._lock:
            before = self._brake.state()
            self._brake.release()
            self._seen_engaged = False
            failures = await self._restore_capabilities()
            report = StopReport(state=self._brake.state(), failures=failures)
            if before.engaged:
                await self._record("emergency_stop.released", report, actor=by)
            return report

    # --- Watching -------------------------------------------------------------

    async def watch(self, done: asyncio.Event, *, interval: float = WATCH_SECONDS) -> None:
        """Enforce a stop set, or honour a release made, by another process."""
        while not done.is_set():
            try:
                await self.check_outside_change()
            except Exception as error:  # the watcher must outlive a bad pass
                log.warning("safety.watch_failed", error=str(error))
            try:
                await asyncio.wait_for(done.wait(), timeout=interval)
            except TimeoutError:
                continue

    async def check_outside_change(self) -> StopReport | None:
        state = self._brake.state()
        if state.engaged == self._seen_engaged:
            return None
        async with self._lock:
            state = self._brake.state()
            if state.engaged == self._seen_engaged:
                return None
            self._seen_engaged = state.engaged
            if state.engaged:
                # Whoever wrote the record audited the stop itself; this line
                # is what this process did about it.
                report = await self._sweep(state)
                await self._record("emergency_stop.enforced", report, actor="system")
                return report
            return StopReport(state=state, failures=await self._restore_capabilities())

    async def audit_outside(self, action: str, state: StopState, *, actor: str) -> None:
        """For a process that only writes the record - the CLI - to say it did."""
        await self._record(action, StopReport(state=state), actor=actor)

    # --- The sweep ------------------------------------------------------------

    async def _sweep(self, state: StopState) -> StopReport:
        failures: list[str] = []
        workspaces = await self._workspace_ids()

        unfinished: list[Task] = []
        for workspace_id in workspaces:
            try:
                unfinished.extend(await self._tasks.list_resumable(workspace_id))
            except Exception as error:
                failures.append("tasks")
                log.warning("safety.unfinished_unreadable", error=str(error))

        halted = HaltedWork()
        try:
            halted = await self._runs.halt_all(state.reason, unfinished, live=self._live)
        except Exception as error:
            failures.append("work")
            log.warning("safety.halt_failed", error=str(error))

        revoked = 0
        if self._leases is not None:
            try:
                for workspace_id in workspaces:
                    for lease in await self._leases.list_active(workspace_id):
                        if await self._leases.revoke(lease.id, by="emergency_stop"):
                            revoked += 1
            except Exception as error:
                failures.append("leases")
                log.warning("safety.leases_not_revoked", error=str(error))

        halted_capabilities: dict[str, int] = {}
        if self._integrations is not None:
            try:
                halted_capabilities["integrations"] = await self._integrations.disconnect_all()
            except Exception as error:
                failures.append("integrations")
                log.warning("safety.integrations_not_closed", error=str(error))
        for halter in self._halters:
            try:
                halted_capabilities[halter.name] = await halter.halt()
            except Exception as error:
                failures.append(halter.name)
                log.warning("safety.halter_failed", halter=halter.name, error=str(error))

        report = StopReport(
            state=state,
            halted=halted,
            leases_revoked=revoked,
            halted_capabilities=halted_capabilities,
            failures=tuple(failures),
        )
        log.warning("safety.swept", **{k: v for k, v in report.to_dict().items() if k != "state"})
        return report

    async def _restore_capabilities(self) -> tuple[str, ...]:
        failures: list[str] = []
        if self._integrations is not None:
            try:
                await self._integrations.restore()
            except Exception as error:
                failures.append("integrations")
                log.warning("safety.integrations_not_restored", error=str(error))
        for halter in self._halters:
            try:
                await halter.resume()
            except Exception as error:
                failures.append(halter.name)
                log.warning("safety.halter_not_resumed", halter=halter.name, error=str(error))
        return tuple(failures)

    async def _workspace_ids(self) -> list[WorkspaceId]:
        try:
            found = list(await self._workspaces())
        except Exception as error:
            log.warning("safety.workspaces_unreadable", error=str(error))
            found = []
        return list(dict.fromkeys([DEFAULT_WORKSPACE_ID, *found]))

    async def _record(self, action: str, report: StopReport, *, actor: str) -> None:
        """One line per workspace: the stop reached all of them, and each chain says so."""
        if self._audit is None:
            return
        details = report.to_dict()
        for workspace_id in await self._workspace_ids():
            try:
                await self._audit.record(
                    AuditRecord(
                        action=action,
                        actor_kind=ActorKind.SYSTEM if actor in {"system", "outside"}
                        else ActorKind.USER,
                        actor_id=actor,
                        result="FAILURE" if report.failures else "SUCCESS",
                        workspace_id=workspace_id,
                        details=details,
                    )
                )
            except Exception as error:
                log.warning("safety.audit_not_recorded", action=action, error=str(error))
