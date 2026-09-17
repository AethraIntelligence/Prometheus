"""Phase 13: two schedulers starting at once, and a clock that moves, against a real store.

The single-owner lock keeps a second runtime off a data directory, so this is
the layer beneath it: even if two schedulers did reach one store at the same
instant, the claim in the database lets exactly one fire. And a clock that jumps
back after a firing does not fire it again.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from application.scheduling.scheduler import Scheduler
from domain.scheduling.models import Recurrence, Schedule
from infrastructure.persistence.schedule_repository import SqlEventLog, SqlScheduleRepository
from tests.unit.test_scheduling import RecordingManager

NOON = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


async def test_two_schedulers_starting_together_fire_a_due_schedule_once(session_factory) -> None:
    schedules = SqlScheduleRepository(session_factory)
    events = SqlEventLog(session_factory)
    await schedules.save(
        Schedule.create(
            "morning digest", recurrence=Recurrence(every_seconds=3600), created_at=NOON
        )
    )
    manager = RecordingManager()
    racing = [
        Scheduler(
            manager=manager, schedules=schedules, events=events, clock=lambda: NOON, owner=name
        )
        for name in ("first-process", "second-process", "third-process")
    ]

    await asyncio.gather(*(scheduler.tick() for scheduler in racing))

    assert manager.requests == ["morning digest"]


async def test_a_clock_moved_back_after_a_firing_does_not_fire_it_again(session_factory) -> None:
    schedules = SqlScheduleRepository(session_factory)
    events = SqlEventLog(session_factory)
    await schedules.save(
        Schedule.create("hourly check", recurrence=Recurrence(every_seconds=3600), created_at=NOON)
    )
    manager = RecordingManager()
    now = {"value": NOON}
    scheduler = Scheduler(
        manager=manager, schedules=schedules, events=events, clock=lambda: now["value"]
    )

    await scheduler.tick()
    now["value"] = NOON - timedelta(hours=3)  # the machine's clock was corrected backwards
    await scheduler.tick()
    now["value"] = NOON + timedelta(minutes=30)
    await scheduler.tick()

    assert manager.requests == ["hourly check"]

    now["value"] = NOON + timedelta(hours=5)  # forwards: one run owed, not five
    await scheduler.tick()
    assert manager.requests == ["hourly check", "hourly check"]
