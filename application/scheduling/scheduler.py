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
from datetime import UTC, datetime
from uuid import UUID

import structlog

from domain.conversations.repository import ConversationRepository
from domain.scheduling.models import Event, Schedule, Trigger
from domain.scheduling.protocols import EventLog, ScheduleRepository
from domain.workforce import directions as carried
from domain.workforce.protocols import ObjectiveResult, WorkforceManager
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId

log = structlog.get_logger(__name__)

#: How often the loop looks. Well under the shortest interval a schedule may
#: declare, and far enough above zero that an idle machine is idle.
DEFAULT_TICK_SECONDS = 30.0

#: The event kind the scheduler writes when a proactive objective ends. It is
#: an event like any other, so a schedule can be declared to run *on* one - the
#: cheapest possible way to have one piece of work follow another without
#: inventing a second kind of dependency.
OBJECTIVE_FINISHED = "objective.finished"


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
    ) -> None:
        self._manager = manager
        self._schedules = schedules
        self._events = events
        self._workspace_id = workspace_id
        self._tick = tick_seconds
        self._clock = clock
        self._workspaces = workspaces
        self._conversations = conversations
        #: Schedules with an objective in flight. In memory on purpose: it is a
        #: fact about this process, and a process that died is not still running
        #: anything.
        self._running: set[UUID] = set()

    # --- One pass -------------------------------------------------------------

    async def tick(self) -> tuple[ObjectiveResult, ...]:
        """Fire everything that is due or triggered, once. Never raises."""
        now = self._clock()
        results: list[ObjectiveResult] = []
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

    async def _tick_in(self, workspace_id: WorkspaceId, now: datetime) -> list[ObjectiveResult]:
        results: list[ObjectiveResult] = []
        for schedule in await self._schedules.due(now, workspace_id):
            fired = await self._fire(schedule, now, Trigger.SCHEDULED)
            if fired is not None:
                results.append(fired)

        for schedule in await self._schedules.list(workspace_id):
            if not schedule.enabled or not schedule.on_event:
                continue
            for event in await self._events.pending(schedule.on_event, workspace_id):
                # Claimed before the work, so two passes - or two schedules
                # racing on one kind - cannot both act on one event.
                if not await self._events.consume(event.id, now):
                    continue
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
    ) -> ObjectiveResult | None:
        if schedule.id in self._running:
            # The period is shorter than the work. Said out loud, because the
            # alternative symptom is a machine that is quietly always busy.
            log.warning(
                "scheduler.still_running",
                schedule=schedule.name or str(schedule.id),
                since=schedule.last_run_at.isoformat() if schedule.last_run_at else "",
            )
            return None

        self._running.add(schedule.id)
        try:
            # Carried around the whole objective, as a window request's are, so
            # every task it starts prefers the schedule's model.
            with carried.given(schedule.directions):
                return await self._run(schedule, now, trigger, event)
        except Exception as error:
            # One schedule's failure is not the loop's. The others on this
            # machine have nothing to do with it.
            log.warning(
                "scheduler.firing_failed",
                schedule=schedule.name or str(schedule.id),
                error=f"{type(error).__name__}: {error}",
            )
            await self._record(schedule, None, "FAILED")
            return None
        finally:
            self._running.discard(schedule.id)

    async def _run(
        self, schedule: Schedule, now: datetime, trigger: Trigger, event: Event | None
    ) -> ObjectiveResult:
        objective = await self._manager.receive(
            _request_for(schedule, event),
            workspace_id=schedule.workspace_id,
            conversation_id=schedule.conversation_id,
        )
        await self._touch_thread(schedule, now)
        # Marked before the work, not after: a crash mid-objective costs
        # this run and not a loop that keeps re-attempting whatever was
        # heavy enough to crash.
        await self._schedules.save(schedule.fired(now, objective.id))
        log.info(
            "scheduler.fired",
            schedule=schedule.name or str(schedule.id),
            trigger=trigger.value,
            objective_id=str(objective.id),
            model=schedule.model or None,
            approvals=schedule.approvals.value,
        )
        result = await self._manager.handle_objective(objective)
        await self._record(schedule, objective.id, result.status.value)
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

    async def _record(self, schedule: Schedule, objective_id: UUID | None, status: str) -> None:
        """Say what became of a firing, in the log a person reads afterwards.

        Guarded like every other write that is not the work itself: a scheduler
        that could be brought down by its own bookkeeping would be worse than
        one that occasionally forgets to write a line.
        """
        try:
            await self._events.record(
                Event.create(
                    OBJECTIVE_FINISHED,
                    workspace_id=schedule.workspace_id,
                    source=schedule.name or str(schedule.id),
                    payload={
                        "schedule_id": str(schedule.id),
                        "objective_id": str(objective_id) if objective_id else "",
                        "status": status,
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
