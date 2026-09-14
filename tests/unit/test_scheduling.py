"""Phase 12.9: work that starts without anybody asking for it.

The scheduler is deliberately dull, and these are the four things it is not
allowed to get wrong: a missed run is not made up, a schedule already running
does not start again, an event is acted on once, and one schedule's failure is
not the loop's.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time, timedelta

import pytest

from application.scheduling.scheduler import OBJECTIVE_FINISHED, Scheduler
from domain.scheduling.models import Event, Recurrence, Schedule, Trigger
from domain.workforce.protocols import Objective, ObjectiveResult, ObjectiveStatus
from infrastructure.persistence.schedule_repository import (
    InMemoryEventLog,
    InMemoryScheduleRepository,
)

NOON = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

#: Every schedule these tests fire is stamped with `created_at=NOON`, because an
#: interval schedule's first due time *is* its `created_at` and the scheduler
#: here runs on a frozen clock. Left to `datetime.now()`, a schedule created
#: after noon on the day in the constant is due later than the clock that asks,
#: and nothing fires - so the suite passed until 12:00 UTC on 2026-09-08 and
#: could not pass afterwards. A test whose result depends on the wall clock is a
#: test that reports on the calendar rather than on the code.


class RecordingManager:
    """A manager that records what it was asked and answers instantly."""

    def __init__(self, fail: bool = False) -> None:
        self.requests: list[str] = []
        self.conversations: list = []
        self.models: list[str] = []
        self.approvals: list = []
        self.workspaces: list = []
        self._fail = fail

    async def receive(self, request: str, workspace_id=None, conversation_id=None) -> Objective:
        from domain.workforce import directions

        self.models.append(directions.current().model)
        self.approvals.append(directions.current().approvals)
        self.requests.append(request)
        self.conversations.append(conversation_id)
        self.workspaces.append(workspace_id)
        return Objective.create(request)

    async def handle_objective(self, objective: Objective) -> ObjectiveResult:
        if self._fail:
            raise RuntimeError("the objective blew up")
        return ObjectiveResult(
            objective_id=objective.id, summary="done", status=ObjectiveStatus.DONE
        )


def scheduler(manager, schedules, events, now=NOON) -> Scheduler:
    return Scheduler(manager=manager, schedules=schedules, events=events, clock=lambda: now)


# --- The recurrence itself ----------------------------------------------------


def test_an_interval_is_due_immediately_and_a_time_of_day_is_not() -> None:
    hourly = Schedule.create("check", recurrence=Recurrence(every_seconds=3600))
    morning = Schedule.create("check", recurrence=Recurrence(daily_at=time(7, 30)))

    assert hourly.next_due_at == hourly.created_at
    assert morning.next_due_at is not None
    assert morning.next_due_at > morning.created_at


def test_a_missed_run_is_not_made_up() -> None:
    """A laptop shut for the weekend does not owe an hourly schedule the weekend."""
    hourly = Schedule.create(
        "check",
        recurrence=Recurrence(every_seconds=3600),
        next_due_at=NOON - timedelta(days=2),
    )

    moved = hourly.fired(NOON)

    assert moved.next_due_at == NOON + timedelta(hours=1)
    assert moved.runs == 1


def test_an_interval_shorter_than_a_minute_is_refused() -> None:
    with pytest.raises(ValueError, match="shortest interval"):
        Recurrence(every_seconds=5)


def test_a_schedule_fires_on_a_time_or_an_event_and_never_both() -> None:
    with pytest.raises(ValueError, match="not both"):
        Schedule.create(
            "check", recurrence=Recurrence(every_seconds=3600), on_event="inbox.arrived"
        )
    with pytest.raises(ValueError, match="not both"):
        Schedule.create("check")


def test_an_event_driven_schedule_is_never_due_on_the_clock() -> None:
    waiting = Schedule.create("check", on_event="inbox.arrived")

    assert waiting.trigger is Trigger.EVENT
    assert not waiting.is_due(NOON + timedelta(days=365))


# --- One pass -----------------------------------------------------------------


async def test_a_due_schedule_becomes_an_objective() -> None:
    schedules, events = InMemoryScheduleRepository(), InMemoryEventLog()
    await schedules.save(
        Schedule.create(
            "Summarise the notes",
            name="daily",
            recurrence=Recurrence(every_seconds=3600),
            created_at=NOON,
        )
    )
    manager = RecordingManager()

    results = await scheduler(manager, schedules, events).tick()

    assert manager.requests == ["Summarise the notes"]
    assert [r.status for r in results] == [ObjectiveStatus.DONE]
    stored = (await schedules.list())[0]
    assert stored.runs == 1
    assert stored.next_due_at == NOON + timedelta(hours=1)
    assert stored.last_objective_id is not None


async def test_a_paused_schedule_does_not_fire() -> None:
    schedules, events = InMemoryScheduleRepository(), InMemoryEventLog()
    created = Schedule.create("check", recurrence=Recurrence(every_seconds=3600))
    await schedules.save(created.set_enabled(False))
    manager = RecordingManager()

    await scheduler(manager, schedules, events).tick()

    assert manager.requests == []


async def test_a_schedule_still_running_does_not_start_again() -> None:
    """The commonest scheduling failure, and its symptom is slowness, not an error."""
    schedules, events = InMemoryScheduleRepository(), InMemoryEventLog()
    await schedules.save(
        Schedule.create("check", recurrence=Recurrence(every_seconds=60), created_at=NOON)
    )

    started = asyncio.Event()
    release = asyncio.Event()

    class SlowManager(RecordingManager):
        async def handle_objective(self, objective):
            started.set()
            await release.wait()
            return await super().handle_objective(objective)

    manager = SlowManager()
    loop = scheduler(manager, schedules, events)

    first = asyncio.create_task(loop.tick())
    # Bounded on purpose. If the first pass never reaches the work, this test's
    # premise is already false, and an unbounded wait would hang the whole suite
    # instead of failing this one line - which is exactly what it did.
    async with asyncio.timeout(5):
        await started.wait()
    await loop.tick()  # a second pass while the first is still in flight
    release.set()
    await first

    assert manager.requests == ["check"], "it was asked once"


async def test_an_event_fires_its_schedule_once() -> None:
    schedules, events = InMemoryScheduleRepository(), InMemoryEventLog()
    await schedules.save(Schedule.create("Triage the inbox", on_event="inbox.arrived"))
    await events.record(Event.create("inbox.arrived", payload={"from": "someone"}))
    manager = RecordingManager()
    loop = scheduler(manager, schedules, events)

    await loop.tick()
    await loop.tick()

    assert len(manager.requests) == 1
    assert "Triage the inbox" in manager.requests[0]
    assert "inbox.arrived" in manager.requests[0], "what triggered it is stated, not merged in"


async def test_an_event_of_another_kind_is_left_alone() -> None:
    schedules, events = InMemoryScheduleRepository(), InMemoryEventLog()
    await schedules.save(Schedule.create("Triage the inbox", on_event="inbox.arrived"))
    await events.record(Event.create("something.else"))
    manager = RecordingManager()

    await scheduler(manager, schedules, events).tick()

    assert manager.requests == []
    assert not (await events.recent())[0].consumed


async def test_a_failing_objective_does_not_stop_the_loop() -> None:
    schedules, events = InMemoryScheduleRepository(), InMemoryEventLog()
    await schedules.save(
        Schedule.create("check", recurrence=Recurrence(every_seconds=3600), created_at=NOON)
    )
    manager = RecordingManager(fail=True)

    results = await scheduler(manager, schedules, events).tick()

    assert results == ()
    recorded = await events.recent()
    assert recorded[0].kind == OBJECTIVE_FINISHED
    assert recorded[0].payload["status"] == "FAILED"


async def test_what_a_firing_produced_is_itself_an_event() -> None:
    """So one piece of work can follow another without a second kind of edge."""
    schedules, events = InMemoryScheduleRepository(), InMemoryEventLog()
    await schedules.save(
        Schedule.create("check", recurrence=Recurrence(every_seconds=3600), created_at=NOON)
    )

    await scheduler(RecordingManager(), schedules, events).tick()

    recorded = await events.recent()
    assert [e.kind for e in recorded] == [OBJECTIVE_FINISHED]
    assert recorded[0].payload["status"] == "DONE"


# --- A schedule made in the window --------------------------------------------


async def test_every_workspace_is_looked_in_not_only_the_first() -> None:
    """A schedule made in a second workspace used to wait forever."""
    schedules, events = InMemoryScheduleRepository(), InMemoryEventLog()
    await schedules.save(
        Schedule.create(
            "Check the sales folder",
            recurrence=Recurrence(every_seconds=3600),
            workspace_id="sales",
            created_at=NOON,
        )
    )
    manager = RecordingManager()

    async def every_workspace():
        return ["default", "sales"]

    found = Scheduler(
        manager=manager,
        schedules=schedules,
        events=events,
        clock=lambda: NOON,
        workspaces=every_workspace,
    )
    await found.tick()

    assert manager.requests == ["Check the sales folder"]
    assert manager.workspaces == ["sales"]


async def test_a_firing_is_written_into_the_schedules_thread() -> None:
    from uuid import uuid4

    thread = uuid4()
    schedules, events = InMemoryScheduleRepository(), InMemoryEventLog()
    await schedules.save(
        Schedule.create(
            "Morning digest",
            recurrence=Recurrence(every_seconds=3600),
            conversation_id=thread,
            created_at=NOON,
        )
    )
    manager = RecordingManager()

    await scheduler(manager, schedules, events).tick()

    assert manager.conversations == [thread]


def test_resuming_a_paused_schedule_does_not_owe_what_the_pause_skipped() -> None:
    hourly = Schedule.create(
        "check", recurrence=Recurrence(every_seconds=3600), next_due_at=NOON - timedelta(days=3)
    ).set_enabled(False)

    resumed = hourly.set_enabled(True, now=NOON)

    assert resumed.enabled
    assert resumed.next_due_at == NOON + timedelta(hours=1)


async def test_a_firing_prefers_the_schedules_model_and_nothing_leaks_past_it() -> None:
    from domain.workforce import directions

    schedules, events = InMemoryScheduleRepository(), InMemoryEventLog()
    await schedules.save(
        Schedule.create(
            "Morning digest",
            recurrence=Recurrence(every_seconds=3600),
            model="balanced",
            created_at=NOON,
        )
    )
    manager = RecordingManager()

    await scheduler(manager, schedules, events).tick()

    assert manager.models == ["balanced"]
    assert directions.current().model == "", "the choice belongs to that run only"


def test_editing_the_timing_counts_the_next_run_from_now_and_keeps_the_history() -> None:
    hourly = Schedule.create(
        "check", recurrence=Recurrence(every_seconds=3600), created_at=NOON
    ).fired(NOON)

    edited = hourly.edited(
        request="check twice as rarely",
        name="rare",
        recurrence=Recurrence(every_seconds=7200),
        on_event="",
        model="fast",
        now=NOON + timedelta(minutes=10),
    )

    assert edited.id == hourly.id and edited.runs == 1
    assert edited.next_due_at == NOON + timedelta(minutes=10, hours=2)
    assert (edited.request, edited.name, edited.model) == ("check twice as rarely", "rare", "fast")


def test_editing_only_the_words_keeps_the_next_run() -> None:
    hourly = Schedule.create("check", recurrence=Recurrence(every_seconds=3600), created_at=NOON)

    edited = hourly.edited(
        request="check again",
        name="",
        recurrence=Recurrence(every_seconds=3600),
        on_event="",
        model="",
        now=NOON + timedelta(minutes=10),
    )

    assert edited.next_due_at == hourly.next_due_at


async def test_a_firing_is_carried_under_the_schedules_approvals() -> None:
    from domain.workforce.directions import ApprovalChoice

    schedules, events = InMemoryScheduleRepository(), InMemoryEventLog()
    await schedules.save(
        Schedule.create(
            "Tidy the downloads",
            recurrence=Recurrence(every_seconds=3600),
            approvals=ApprovalChoice.DENY,
            created_at=NOON,
        )
    )
    manager = RecordingManager()

    await scheduler(manager, schedules, events).tick()

    assert manager.approvals == [ApprovalChoice.DENY]
