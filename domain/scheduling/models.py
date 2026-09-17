"""Work that starts without anybody asking for it (§12.9).

Everything the platform has done so far began with a sentence somebody typed.
This is the first thing that does not, and that changes what has to be written
down rather than what has to be built: a run nobody is watching needs a record
of *why it started* that a person can read afterwards, because there is no
conversation above it to explain itself.

Two triggers, one shape. A `Schedule` says what to ask for and when: either
every so often, or when something of a named kind happens. Both produce the
same thing - an objective, handed to the same manager, through the same doors -
because a second way to start work would be the same design error as a second
employee runtime.

**Time here is UTC and explicit.** A local-time schedule is a promise about a
machine that travels, changes clocks twice a year, and is asleep for some of
it. UTC and a stated offset is a promise about a moment.

**A missed run is not made up.** A machine that was off for a day does not owe
an hourly schedule twenty-four objectives; it owes one, now. Catching up is a
decision, and this is the decision: the point of "every hour" is that the work
is current, and twenty-four stale runs are worse than one fresh one - louder,
more expensive, and each of them wrong about a world that has moved.

**Nothing here bypasses anything.** A scheduled objective meets the same
policies, the same approval gate and the same STOP file as one a person asked
for. That is deliberate and is not a limitation to be worked around later: an
action nobody is present to approve is exactly the action that most needs
somebody present.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from domain.workforce.directions import ApprovalChoice, Directions
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId


class Trigger(StrEnum):
    """What started a piece of work. `MANUAL` is a person; the rest are not."""

    MANUAL = "MANUAL"
    SCHEDULED = "SCHEDULED"
    EVENT = "EVENT"


#: The shortest interval a schedule may declare. Not a technical limit - the
#: poll loop could go faster - but a statement that this is a platform for work
#: an employee does, and an employee run takes minutes. A schedule that fires
#: faster than its own objectives finish is a queue with a friendly name.
MIN_INTERVAL_SECONDS = 60


@dataclass(frozen=True, slots=True)
class Recurrence:
    """When a schedule is due. Exactly one of these is set.

    Deliberately not a cron expression. Cron is a small language, and a small
    language wants a parser, a dialect argument and a test suite of its own -
    for a local platform whose real schedules are "every morning" and "every
    few hours". These two cover those, they cannot be written wrongly in a way
    that silently means something else, and the day the third case turns up it
    is a field here rather than a dependency.
    """

    every_seconds: int | None = None
    #: A time of day, in UTC. The one recurring shape that an interval cannot
    #: express: "every morning" drifts under `every_seconds=86400` as soon as a
    #: run starts late, and drifts further every day after that.
    daily_at: time | None = None
    #: IANA zone whose wall clock `daily_at` belongs to. Empty keeps the
    #: backwards-compatible UTC meaning for schedules created before zones.
    timezone: str = ""

    def __post_init__(self) -> None:
        if (self.every_seconds is None) == (self.daily_at is None):
            raise ValueError("A recurrence is either an interval or a time of day, not both")
        if self.every_seconds is not None and self.every_seconds < MIN_INTERVAL_SECONDS:
            raise ValueError(
                f"The shortest interval is {MIN_INTERVAL_SECONDS} seconds; "
                f"{self.every_seconds} was asked for"
            )
        if self.every_seconds is not None and self.timezone:
            raise ValueError("An interval does not have a time zone")
        if self.timezone:
            try:
                ZoneInfo(self.timezone)
            except ZoneInfoNotFoundError as error:
                raise ValueError(f"Unknown time zone: {self.timezone}") from error

    def next_after(self, moment: datetime) -> datetime:
        """The first firing strictly after `moment`."""
        if self.every_seconds is not None:
            return moment + timedelta(seconds=self.every_seconds)
        assert self.daily_at is not None
        zone = ZoneInfo(self.timezone) if self.timezone else UTC
        local = moment.astimezone(zone)
        candidate = datetime.combine(local.date(), self.daily_at, tzinfo=zone)
        if candidate <= local:
            candidate = datetime.combine(
                local.date() + timedelta(days=1), self.daily_at, tzinfo=zone
            )
        return candidate.astimezone(UTC)

    def describe(self) -> str:
        if self.every_seconds is not None:
            return f"every {self.every_seconds}s"
        zone = self.timezone or "UTC"
        return f"daily at {self.daily_at.isoformat(timespec='minutes')} {zone}"


@dataclass(frozen=True, slots=True)
class Schedule:
    """A standing instruction to ask Prometheus for something.

    It holds a request in the user's own words, not a plan and not an employee.
    That is the same rule the whole platform runs on and it matters more here:
    a schedule outlives the workforce it was written against, and one that
    named an employee would keep pointing at a declaration somebody deleted.
    """

    id: UUID
    request: str
    workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    name: str = ""
    recurrence: Recurrence | None = None
    #: The kind of event this waits for, when it waits for one rather than for
    #: a time. Exactly one of `recurrence` and `on_event` is set.
    on_event: str = ""
    enabled: bool = True
    next_due_at: datetime | None = None
    last_run_at: datetime | None = None
    #: The objective the last firing produced, so a schedule can be read back to
    #: what it actually did. Not a foreign key, for the reason `audit_log` has
    #: none: the record outlives what it points at.
    last_objective_id: UUID | None = None
    runs: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: The thread every firing is written into, when the schedule was made
    #: somewhere that shows threads. A label like `objectives.conversation_id`:
    #: no foreign key, and None - a schedule made in a terminal - is not a
    #: degraded case. Without it a scheduled run from the window happened
    #: somewhere nobody who uses the window could see.
    conversation_id: UUID | None = None
    #: A catalog entry by name that its runs prefer, as the model chosen under
    #: the field does for one request. Empty means the router decides. It is a
    #: preference and not a requirement, for the reason `Directions.model` is:
    #: a model that cannot do a piece of the work is passed over for that piece.
    model: str = ""
    #: What its runs do about an action that needs approval. ASK puts the
    #: question in the window, where it waits for somebody and is refused when
    #: nobody answers in time; AUTO goes ahead where this machine would have
    #: asked; DENY refuses without asking. The same three the composer offers.
    approvals: ApprovalChoice = ApprovalChoice.ASK
    #: Incremented whenever the standing instruction is edited. A run records
    #: this value so its exact configuration remains explainable afterwards.
    version: int = 1

    @classmethod
    def create(cls, request: str, **extra: Any) -> Schedule:
        schedule = cls(id=uuid4(), request=request.strip(), **extra)
        if not schedule.request:
            raise ValueError("A schedule with no request would ask for nothing")
        if (schedule.recurrence is None) == (not schedule.on_event):
            raise ValueError("A schedule fires either on a recurrence or on an event, not both")
        if schedule.recurrence is not None and schedule.next_due_at is None:
            # An interval is due from the moment it is created rather than one
            # interval later: "every hour" typed now means the first one is now,
            # and waiting an hour to find out whether the schedule works at all
            # is why people test schedules by setting them to a minute and then
            # forgetting. A time of day is the opposite - "every morning at
            # seven" set at four in the afternoon means tomorrow morning, and
            # firing on the spot would be answering a different instruction.
            first = (
                schedule.created_at
                if schedule.recurrence.every_seconds is not None
                else schedule.recurrence.next_after(schedule.created_at)
            )
            return replace(schedule, next_due_at=first)
        return schedule

    @property
    def trigger(self) -> Trigger:
        return Trigger.SCHEDULED if self.recurrence is not None else Trigger.EVENT

    def is_due(self, now: datetime) -> bool:
        """Time-based only. An event-driven schedule is due when an event says so."""
        if not self.enabled or self.recurrence is None or self.next_due_at is None:
            return False
        return self.next_due_at <= now

    def fired(self, now: datetime, objective_id: UUID | None = None) -> Schedule:
        """Move on from a firing, without owing the runs that were missed.

        `next_after(now)` and not `next_after(next_due_at)`: the second is the
        cron behaviour, and on a laptop that was shut for the weekend it means
        a burst of runs about a world that has moved on.
        """
        return replace(
            self,
            last_run_at=now,
            last_objective_id=objective_id,
            runs=self.runs + 1,
            next_due_at=self.recurrence.next_after(now) if self.recurrence else None,
        )

    def manually_fired(self, now: datetime, objective_id: UUID) -> Schedule:
        """Record a person-triggered run without moving the automatic clock."""
        return replace(
            self,
            last_run_at=now,
            last_objective_id=objective_id,
            runs=self.runs + 1,
        )

    def with_last_objective(self, objective_id: UUID) -> Schedule:
        """Attach the objective created for the already-counted firing."""
        return replace(self, last_objective_id=objective_id)

    def set_enabled(self, enabled: bool, now: datetime | None = None) -> Schedule:
        """Pause or resume. Resuming never owes the runs a pause skipped.

        A schedule paused for a week comes back due at its next moment from
        now - the same rule as a machine that was switched off.
        """
        if not enabled or self.enabled or self.recurrence is None:
            return replace(self, enabled=enabled)
        moment = now or datetime.now(UTC)
        stale = self.next_due_at is None or self.next_due_at < moment
        return replace(
            self,
            enabled=True,
            next_due_at=self.recurrence.next_after(moment) if stale else self.next_due_at,
        )

    def edited(
        self,
        *,
        request: str,
        name: str,
        recurrence: Recurrence | None,
        on_event: str,
        model: str,
        approvals: ApprovalChoice = ApprovalChoice.ASK,
        now: datetime | None = None,
    ) -> Schedule:
        """The same standing instruction, said differently. Its history stays.

        When the timing changed, the next run is counted from now - for an
        interval too, unlike `create`: somebody changing "every hour" to "every
        two hours" is correcting a schedule, not asking for a run the moment
        they press save. Unchanged timing keeps the next run it had.
        """
        text = request.strip()
        if not text:
            raise ValueError("A schedule with no request would ask for nothing")
        if (recurrence is None) == (not on_event.strip()):
            raise ValueError("A schedule fires either on a recurrence or on an event, not both")
        moment = now or datetime.now(UTC)
        timing_changed = recurrence != self.recurrence or on_event.strip() != self.on_event
        next_due = self.next_due_at
        if timing_changed:
            next_due = recurrence.next_after(moment) if recurrence is not None else None
        return replace(
            self,
            request=text,
            name=name.strip(),
            recurrence=recurrence,
            on_event=on_event.strip(),
            model=model.strip(),
            approvals=approvals,
            version=self.version + 1,
            next_due_at=next_due,
        )

    @property
    def directions(self) -> Directions:
        """How each run of it is carried out."""
        return Directions(approvals=self.approvals, model=self.model)

    def describe(self) -> str:
        return self.recurrence.describe() if self.recurrence else f"on event '{self.on_event}'"


@dataclass(frozen=True, slots=True)
class Event:
    """Something happened that work might be owed to.

    Recorded rather than dispatched. A schedule waiting on a kind of event finds
    it here on the next pass, so an event that arrives while nothing is watching
    is not lost - which is the failure a callback would have and a row does not.
    """

    id: UUID
    kind: str
    workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: When a schedule acted on it. An event is consumed once: two schedules
    #: waiting on one kind is a legitimate configuration, and each firing twice
    #: for one event is not, so the claim is recorded on the event itself.
    consumed_at: datetime | None = None

    @classmethod
    def create(cls, kind: str, **extra: Any) -> Event:
        name = kind.strip()
        if not name:
            raise ValueError("An event with no kind cannot trigger anything")
        return cls(id=uuid4(), kind=name, **extra)

    @property
    def consumed(self) -> bool:
        return self.consumed_at is not None

    def consume(self, now: datetime | None = None) -> Event:
        return replace(self, consumed_at=now or datetime.now(UTC))
