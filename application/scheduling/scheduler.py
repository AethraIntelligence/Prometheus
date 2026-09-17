"""The loop that starts work nobody asked for, right now (§12.9).

It is deliberately the least clever thing in the platform. It wakes up, asks
what is due, and hands each one to the manager exactly as `ask-prometheus` would.
Everything that makes an objective safe - the policies, the approval gate, the
STOP file, the budget - is below this line and is not re-implemented above it.
A scheduler that knew how to run a task would be a second way to run one.

Four decisions, and each rejects a plausible alternative.

**It polls.** Not a timer per schedule, not a heap of wake-up calls: one pass on
a fixed tick that asks the store what is due. A process that is killed loses
nothing, because the answer lives in the row rather than in a timer somebody has
to re-arm; and "due" is then a question a person can also ask, which is what
`prometheus schedules` prints.

**A schedule that is still running does not start again.** The commonest
scheduling failure is a job whose period is shorter than its duration, and the
symptom is not an error - it is a machine getting slower all week. So a firing
is remembered while it is in flight and the next pass skips it, having said so.

**The clock advances before the work, not after.** A schedule is marked fired
the moment it is picked up. Crashing mid-objective then costs that one
objective, and not an endless retry of whatever was expensive enough to crash.

**A failure is an event, not a stop.** One schedule whose objective raised must
not take the loop down with it - the other schedules on this machine have
nothing to do with it. It is logged, recorded, and the pass continues.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import structlog

from application.workflows.engine import WorkflowEngine
from application.workspaces import folders
from domain.conversations.repository import ConversationRepository
from domain.scheduling.models import Event, Schedule, Trigger
from domain.scheduling.protocols import EventLog, ScheduleRepository
from domain.workflows.definition import WorkflowDefinition, WorkflowTrigger
from domain.workflows.run import WorkflowRun
from domain.workforce import directions as carried
from domain.workforce.directions import Directions
from domain.workforce.protocols import ObjectiveResult, WorkforceManager
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId

log = structlog.get_logger(__name__)

#: How often the loop looks. Well under the shortest interval a schedule may
#: declare, and far enough above zero that an idle machine is idle.
DEFAULT_TICK_SECONDS = 30.0
DEFAULT_LEASE_SECONDS = 120

#: The event kind the scheduler writes when a proactive objective ends. It is
#: an event like any other, so a schedule can be declared to run *on* one - the
#: cheapest possible way to have one piece of work follow another without
#: inventing a second kind of dependency.
OBJECTIVE_FINISHED = "objective.finished"
WORKFLOW_FINISHED = "workflow.finished"


class Scheduler:
    """Turns due schedules and pending events into objectives."""

    def __init__(
        self,
        *,
        manager: WorkforceManager,
        schedules: ScheduleRepository,
        events: EventLog,
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        # Injected so a test can decide what time it is instead of waiting.
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        # Every workspace on the machine, asked each pass. Without it only
        # `workspace_id` is looked at - which is what a machine with one
        # workspace has, and what left a schedule made in any other one never
        # firing at all.
        workspaces: Callable[[], Awaitable[Iterable[WorkspaceId]]] | None = None,
        # Where a firing's thread is marked as spoken in, so it rises to the top
        # of the list the way a thread somebody typed into does.
        conversations: ConversationRepository | None = None,
        # Where a thread with no folder of its own gets one - the workspace's
        # root. Without it a firing works where the machine's tools point.
        folder_root: Callable[[WorkspaceId], Path] | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        owner: str | None = None,
        workflows: WorkflowEngine | None = None,
    ) -> None:
        self._manager = manager
        self._schedules = schedules
        self._events = events
        self._workspace_id = workspace_id
        self._tick = tick_seconds
        self._clock = clock
        self._workspaces = workspaces
        self._conversations = conversations
        self._folder_root = folder_root
        self._lease_seconds = max(30, lease_seconds)
        self._owner = owner or str(uuid4())
        self._workflows = workflows
        #: Schedules with an objective in flight. In memory on purpose: it is a
        #: fact about this process, and a process that died is not still running
        #: anything.
        self._running: set[UUID] = set()

    # --- One pass -------------------------------------------------------------

    async def tick(self) -> tuple[ObjectiveResult | WorkflowRun, ...]:
        """Fire everything that is due or triggered, once. Never raises."""
        now = self._clock()
        results: list[ObjectiveResult | WorkflowRun] = []
        for workspace_id in await self._workspaces_to_look_in():
            results.extend(await self._tick_in(workspace_id, now))
        return tuple(results)

    async def _workspaces_to_look_in(self) -> list[WorkspaceId]:
        if self._workspaces is None:
            return [self._workspace_id]
        try:
            found = list(await self._workspaces())
        except Exception as error:
            log.warning("scheduler.workspaces_unreadable", error=str(error))
            return [self._workspace_id]
        # The first workspace is always looked in: everything made before there
        # were others belongs to it, and a listing that missed it would stop them.
        return list(dict.fromkeys([self._workspace_id, *found]))

    async def _tick_in(
        self, workspace_id: WorkspaceId, now: datetime
    ) -> list[ObjectiveResult | WorkflowRun]:
        results: list[ObjectiveResult | WorkflowRun] = []
        for schedule in await self._schedules.due(now, workspace_id):
            fired = await self._fire(schedule, now, Trigger.SCHEDULED)
            if fired is not None:
                results.append(fired)

        for schedule in await self._schedules.list(workspace_id):
            if not schedule.enabled or not schedule.on_event:
                continue
            for event in await self._events.pending(schedule.on_event, workspace_id):
                fired = await self._fire(schedule, now, Trigger.EVENT, event=event)
                if fired is not None:
                    results.append(fired)
        return results

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        """Tick until asked to stop. The entry point `prometheus serve` starts."""
        signal = stop or asyncio.Event()
        log.info("scheduler.started", tick_seconds=self._tick)
        while not signal.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(signal.wait(), timeout=self._tick)
            except TimeoutError:
                continue
        log.info("scheduler.stopped")

    # --- One firing -----------------------------------------------------------

    async def _fire(
        self,
        schedule: Schedule,
        now: datetime,
        trigger: Trigger,
        *,
        event: Event | None = None,
    ) -> ObjectiveResult | WorkflowRun | None:
        if schedule.id in self._running:
            # The period is shorter than the work. Said out loud, because the
            # alternative symptom is a machine that is quietly always busy.
            log.warning(
                "scheduler.still_running",
                schedule=schedule.name or str(schedule.id),
                since=schedule.last_run_at.isoformat() if schedule.last_run_at else "",
            )
            return None

        try:
            claimed = await self._schedules.claim(
                schedule.id, self._owner, now, self._lease_seconds
            )
        except Exception as error:
            log.warning("scheduler.lease_failed", schedule=str(schedule.id), error=str(error))
            return None
        if not claimed:
            log.info("scheduler.leased_elsewhere", schedule=str(schedule.id))
            return None

        # An event is acknowledged only after this schedule owns the firing.
        # The old order consumed it first, so a competing process could make
        # the event disappear and then discover it was not allowed to work.
        if event is not None and not await self._events.consume(event.id, now):
            await self._schedules.release(schedule.id, self._owner)
            return None

        self._running.add(schedule.id)
        lease_stop = asyncio.Event()
        heartbeat = asyncio.create_task(self._keep_lease(schedule.id, lease_stop))
        try:
            # Carried around the whole objective, as a window request's are, so
            # every task it starts prefers the schedule's model.
            with carried.given(await self._directions_for(schedule)):
                return await self._run(schedule, now, trigger, event)
        except Exception as error:
            # One schedule's failure is not the loop's. The others on this
            # machine have nothing to do with it.
            log.warning(
                "scheduler.firing_failed",
                schedule=schedule.name or str(schedule.id),
                error=f"{type(error).__name__}: {error}",
            )
            await self._record(
                schedule, None, "FAILED", trigger=trigger, source_event=event
            )
            return None
        finally:
            lease_stop.set()
            await heartbeat
            try:
                await self._schedules.release(schedule.id, self._owner)
            except Exception as error:
                log.warning(
                    "scheduler.lease_not_released", schedule=str(schedule.id), error=str(error)
                )
            self._running.discard(schedule.id)

    async def _keep_lease(self, schedule_id: UUID, stop: asyncio.Event) -> None:
        every = max(10.0, self._lease_seconds / 3)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=every)
                return
            except TimeoutError:
                pass
            try:
                if not await self._schedules.renew(
                    schedule_id, self._owner, self._clock(), self._lease_seconds
                ):
                    log.warning("scheduler.lease_lost", schedule=str(schedule_id))
                    return
            except Exception as error:
                log.warning(
                    "scheduler.lease_renewal_failed",
                    schedule=str(schedule_id),
                    error=str(error),
                )
                return

    async def _run(
        self, schedule: Schedule, now: datetime, trigger: Trigger, event: Event | None
    ) -> ObjectiveResult | WorkflowRun:
        # Count and advance the firing before crossing into objective creation.
        # If creation itself fails, the next polling tick must not retry the
        # same due moment forever. Event runs were already consumed above.
        fired = schedule.fired(now)
        await self._schedules.save(fired)
        if schedule.is_workflow:
            if self._workflows is None:
                raise RuntimeError("This scheduler was built without workflow execution.")
            definition = WorkflowDefinition.from_snapshot(schedule.workflow_snapshot)
            run = await self._workflows.run_definition(
                definition,
                inputs=schedule.workflow_inputs,
                trigger=(
                    WorkflowTrigger.SCHEDULED
                    if trigger is Trigger.SCHEDULED
                    else WorkflowTrigger.EVENT
                ),
                workspace_id=schedule.workspace_id,
            )
            log.info(
                "scheduler.workflow_fired",
                schedule=schedule.name or str(schedule.id),
                workflow=schedule.workflow_name,
                workflow_version=schedule.workflow_version,
                run_id=str(run.id),
            )
            await self._record(
                schedule,
                None,
                run.status.value,
                trigger=trigger,
                source_event=event,
                workflow_run=run,
            )
            return run
        objective = await self._manager.receive(
            _request_for(schedule, event),
            workspace_id=schedule.workspace_id,
            conversation_id=schedule.conversation_id,
        )
        await self._touch_thread(schedule, now)
        await self._schedules.save(fired.with_last_objective(objective.id))
        log.info(
            "scheduler.fired",
            schedule=schedule.name or str(schedule.id),
            trigger=trigger.value,
            objective_id=str(objective.id),
            model=schedule.model or None,
            approvals=schedule.approvals.value,
        )
        result = await self._manager.handle_objective(objective)
        await self._record(
            schedule,
            objective.id,
            result.status.value,
            trigger=trigger,
            source_event=event,
        )
        return result

    async def _touch_thread(self, schedule: Schedule, now: datetime) -> None:
        """Guarded like `_record`: a thread that cannot be marked is not a failed run."""
        if self._conversations is None or schedule.conversation_id is None:
            return
        try:
            thread = await self._conversations.get(schedule.conversation_id)
            if thread is not None:
                await self._conversations.save(thread.touched(now))
        except Exception as error:
            log.warning("scheduler.thread_not_touched", error=str(error))

    async def _directions_for(self, schedule: Schedule) -> Directions:
        """The schedule's directions, in the folder of the thread it writes into.

        The same folder a person typing into that thread would get, so the
        morning run and the question asked about it afterwards see one set of
        files. Guarded: a thread that cannot be read still fires, where the
        machine's tools point.
        """
        if self._conversations is None or schedule.conversation_id is None:
            return schedule.directions
        try:
            thread = await self._conversations.get(schedule.conversation_id)
            if thread is None:
                return schedule.directions
            if not thread.folder and self._folder_root is not None:
                root = self._folder_root(thread.workspace_id)
                thread = thread.in_folder(str(folders.session_folder(root, thread)))
                await self._conversations.save(thread)
            return replace(schedule.directions, folder=thread.folder)
        except Exception as error:
            log.warning("scheduler.thread_folder_unreadable", error=str(error))
            return schedule.directions

    async def _record(
        self,
        schedule: Schedule,
        objective_id: UUID | None,
        status: str,
        *,
        trigger: Trigger,
        source_event: Event | None = None,
        workflow_run: WorkflowRun | None = None,
    ) -> None:
        """Say what became of a firing, in the log a person reads afterwards.

        Guarded like every other write that is not the work itself: a scheduler
        that could be brought down by its own bookkeeping would be worse than
        one that occasionally forgets to write a line.
        """
        try:
            await self._events.record(
                Event.create(
                    WORKFLOW_FINISHED if workflow_run else OBJECTIVE_FINISHED,
                    workspace_id=schedule.workspace_id,
                    source=schedule.name or str(schedule.id),
                    payload={
                        "schedule_id": str(schedule.id),
                        "objective_id": str(objective_id) if objective_id else "",
                        "status": status,
                        "schedule_version": schedule.version,
                        "trigger": trigger.value,
                        "source_event_id": str(source_event.id) if source_event else "",
                        "source_event_kind": source_event.kind if source_event else "",
                        "workflow_run_id": str(workflow_run.id) if workflow_run else "",
                        "workflow_name": schedule.workflow_name,
                        "workflow_version": schedule.workflow_version or 0,
                        "cost_usd": workflow_run.cost_usd if workflow_run else 0.0,
                        "quality": workflow_run.quality if workflow_run else 0.0,
                    },
                )
            )
        except Exception as error:
            log.warning("scheduler.event_not_recorded", error=str(error))


def _request_for(schedule: Schedule, event: Event | None) -> str:
    """What the manager is actually asked.

    The schedule's own words, plus what the event carried when there was one.
    The event is stated as context rather than merged into the sentence: a
    request rewritten from a payload is a request nobody wrote, and the trace
    would show a sentence the user never typed.
    """
    if event is None:
        return schedule.request
    details = ", ".join(f"{key}: {value}" for key, value in sorted(event.payload.items()))
    context = f"\n\n(Triggered by {event.kind}" + (f" - {details}" if details else "") + ")"
    return schedule.request + context
