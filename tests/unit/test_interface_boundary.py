"""The boundary every interface talks to, tested without one.

No FastAPI, no browser, no window. What is being checked is the part a second
interface would otherwise have to write for itself: that a request is carried in
whatever it says about its origin, that a thread is a view of objectives rather
than a second record of them, that unknown means "no such thing" rather than a
crash, and that answering an approval is carrying a decision rather than making
one.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest

from application.interface.activity import Activity, ActivityEvent
from application.interface.contracts import (
    Attachment,
    InputType,
    RequestSource,
    UserRequest,
)
from application.interface.runs import Runs
from application.interface.service import (
    ApprovalsDisabledError,
    PrometheusService,
    ServiceDependencies,
)
from domain.approvals.models import ApprovalRequest
from domain.conversations.models import TITLE_LIMIT, Conversation
from domain.errors import PrometheusError
from domain.llm.telemetry import SpendSummary
from domain.tasks.progress import ProgressEvent, ProgressKind
from domain.tasks.task import Task, TaskResult, TaskStatus
from domain.workforce.protocols import Objective, ObjectiveResult, ObjectiveStatus
from domain.workspace.models import WorkspaceId
from infrastructure.persistence.conversation_repository import InMemoryConversationRepository
from infrastructure.persistence.in_memory_task_repository import InMemoryTaskRepository
from infrastructure.persistence.objective_repository import InMemoryObjectiveRepository
from tests.fakes.workforce import FakeRegistry


class RecordingManager:
    """Enough of `PrometheusManager` to see what the boundary hands it.

    Deliberately not a real manager: what is under test is the interface layer,
    and a test that needed a planner to check that a request was carried would
    be testing the planner.
    """

    def __init__(self, objectives: InMemoryObjectiveRepository) -> None:
        self._objectives = objectives
        self.received: list[tuple[str, UUID | None]] = []
        self.workspaces: list[str | None] = []
        self.directions: list = []

    async def receive(self, request: str, workspace_id=None, conversation_id=None) -> Objective:
        self.received.append((request, conversation_id))
        self.workspaces.append(str(workspace_id) if workspace_id else None)
        objective = Objective.create(
            request,
            **({"workspace_id": workspace_id} if workspace_id else {}),
            **({"conversation_id": conversation_id} if conversation_id else {}),
        )
        await self._objectives.save(objective)
        return objective

    async def handle_objective(self, objective: Objective) -> ObjectiveResult:
        from domain.workforce import directions

        self.directions.append(directions.current())
        finished = objective.to(
            ObjectiveStatus.DONE,
            ObjectiveResult(
                objective_id=objective.id, summary="Done.", status=ObjectiveStatus.DONE
            ),
        )
        await self._objectives.save(finished)
        return finished.result


class NoWaiter:
    """Implements `domain.approvals.protocols.ApprovalWaiter`, holding nothing."""

    def __init__(self, *pending: ApprovalRequest) -> None:
        self._pending = list(pending)
        self.decisions: list[tuple[UUID, bool]] = []

    def pending(self, task_id=None) -> list[ApprovalRequest]:
        return list(self._pending)

    def decide(self, approval_id: UUID, approved: bool) -> bool:
        self.decisions.append((approval_id, approved))
        return any(item.id == approval_id for item in self._pending)

    def release(self, task_id: UUID) -> int:
        return 0


class EmptyLog:
    async def list_for_task(self, task_id):
        return []

    async def record(self, *args, **kwargs):
        return None

    async def total(self, *args, **kwargs):
        return SpendSummary(calls=0, prompt_tokens=0, output_tokens=0, cost_usd=0.0)


class NoPlans:
    async def save(self, plan):
        return None

    async def get(self, plan_id):
        return None

    async def for_objective(self, objective_id):
        return []


class Stream:
    """Implements `domain.tasks.progress.ProgressStream` over a fixed script."""

    def __init__(self, *events: ProgressEvent, history: dict | None = None) -> None:
        self._events = list(events)
        self._history = history or {}

    def recent(self, task_id: UUID) -> list[ProgressEvent]:
        return list(self._history.get(task_id, ()))

    @asynccontextmanager
    async def subscribe(self):
        queue: asyncio.Queue[ProgressEvent] = asyncio.Queue()
        for event in self._events:
            queue.put_nowait(event)
        yield queue


def build(
    *, waiter: NoWaiter | None = None, approval_service=None
) -> tuple[PrometheusService, dict]:
    objectives = InMemoryObjectiveRepository()
    conversations = InMemoryConversationRepository()
    tasks = InMemoryTaskRepository()
    manager = RecordingManager(objectives)
    parts = {
        "objectives": objectives,
        "conversations": conversations,
        "tasks": tasks,
        "manager": manager,
        "waiter": waiter or NoWaiter(),
    }
    service = PrometheusService(
        ServiceDependencies(
            runs=Runs(
                runner=None,  # type: ignore[arg-type]
                manager=manager,  # type: ignore[arg-type]
                tasks=tasks,
                objectives=objectives,
                cancellations=_NoCancellations(),
                approvals=parts["waiter"],
            ),
            activity=Activity(
                Stream(), tasks=tasks, objectives=objectives, plans=NoPlans()
            ),
            conversations=conversations,
            objectives=objectives,
            plans=NoPlans(),
            tasks=tasks,
            employees=FakeRegistry(),
            approvals=_NoApprovals(),
            waiter=parts["waiter"],
            tool_calls=EmptyLog(),
            llm_calls=EmptyLog(),
            approval_service=approval_service,
        )
    )
    return service, parts


class _NoCancellations:
    def cancel(self, task_id, reason=""):
        return None

    def clear(self, task_id):
        return None

    def is_cancelled(self, task_id):
        return False

    def reason(self, task_id):
        return ""


class _NoApprovals:
    async def save(self, approval):
        return None

    async def get(self, approval_id):
        return None

    async def list_pending(self, workspace_id=None):
        return []

    async def for_task(self, task_id):
        return []

    async def expire_overdue(self, now=None):
        return 0


# --- What crosses the boundary -------------------------------------------------


def test_a_request_carries_where_it_came_from_and_nothing_acts_on_it() -> None:
    """`source` is recorded, never read.

    This is the property the whole interface layer exists to keep, so it is
    asserted on the type rather than left to a reviewer: the request is a value,
    and the platform's own vocabulary has no branch on it.
    """
    request = UserRequest(
        content="  Sort my files  ",
        source=RequestSource.DESKTOP,
        input_type=InputType.VOICE,
        attachments=(Attachment(path="/tmp/note.txt", media_type="text/plain"),),
    )

    assert request.text == "Sort my files", "trimmed, never rewritten"
    assert request.source is RequestSource.DESKTOP
    assert request.input_type is InputType.VOICE
    assert request.attachments[0].path == "/tmp/note.txt"


async def test_the_same_sentence_from_two_interfaces_becomes_the_same_objective() -> None:
    service, parts = build()
    thread = await service.create_conversation()

    for source in (RequestSource.DESKTOP, RequestSource.TELEGRAM, RequestSource.CLI):
        await service.submit(
            UserRequest(
                content="What do my notes say?",
                source=source,
                conversation_id=UUID(thread["id"]),
            )
        )

    asked = [text for text, _ in parts["manager"].received]
    assert asked == ["What do my notes say?"] * 3


async def test_the_workspace_a_request_names_reaches_the_objective() -> None:
    """The defect Phase 15 was written knowing about (§15.2).

    `UserRequest.workspace_id` was declared at the boundary in Phase 13 and
    dropped in `Runs.ask`. With one workspace nothing showed it; with two,
    switching would have looked like it worked and would not have been.
    """
    service, parts = build()

    await service.submit(
        UserRequest(content="Sort the invoices", workspace_id=WorkspaceId("work"))
    )

    assert parts["manager"].workspaces == ["work"]
    stored = await parts["objectives"].list_recent(WorkspaceId("work"))
    assert [item.workspace_id for item in stored] == ["work"]
    assert await parts["objectives"].list_recent() == [], "and not in the other one"


async def test_an_existing_thread_cannot_be_moved_to_another_workspace_by_a_request() -> None:
    service, parts = build()
    thread = await service.create_conversation(workspace_id=WorkspaceId("work"))

    await service.submit(
        UserRequest(
            content="Continue",
            workspace_id=WorkspaceId("personal"),
            conversation_id=UUID(thread["id"]),
        )
    )

    assert parts["manager"].workspaces == ["work"]


async def test_an_empty_request_is_refused_before_anything_is_recorded() -> None:
    service, parts = build()

    with pytest.raises(PrometheusError):
        await service.submit(UserRequest(content="   "))

    assert parts["manager"].received == []


# --- Conversations -------------------------------------------------------------


async def test_a_thread_is_a_view_of_objectives_not_a_second_record() -> None:
    """One message is one objective. Nothing is stored twice.

    The check that matters: the answer a window shows comes from the objective
    itself, so it cannot disagree with the record of the work.
    """
    service, parts = build()
    thread = await service.create_conversation()
    await service.submit(
        UserRequest(content="Summarise my notes", conversation_id=UUID(thread["id"]))
    )

    [objective] = await parts["objectives"].list_recent()
    await parts["objectives"].save(
        objective.to(
            ObjectiveStatus.DONE,
            ObjectiveResult(
                objective_id=objective.id,
                summary="Four folders.",
                status=ObjectiveStatus.DONE,
                missing=("nothing",),
            ),
        )
    )

    read = await service.get_conversation(UUID(thread["id"]))
    assert [message["text"] for message in read["messages"]] == ["Summarise my notes"]
    assert read["messages"][0]["answer"] == "Four folders."
    assert read["messages"][0]["answered"] is True
    assert read["messages"][0]["missing"] == ["nothing"]


async def test_a_thread_is_named_after_the_first_thing_asked_in_it() -> None:
    service, _ = build()
    thread = await service.create_conversation()

    await service.submit(
        UserRequest(content="Find three papers", conversation_id=UUID(thread["id"]))
    )
    await service.submit(
        UserRequest(content="Now summarise them", conversation_id=UUID(thread["id"]))
    )

    read = await service.get_conversation(UUID(thread["id"]))
    assert read["title"] == "Find three papers", "the second request does not rename the thread"


def test_a_long_first_request_becomes_a_label_not_a_paragraph() -> None:
    long = "Please go and " + "read every file in the archive " * 10
    named = Conversation.create().titled_from(long)

    assert len(named.title) <= TITLE_LIMIT
    assert named.title.endswith("…")


def test_a_thread_the_user_named_keeps_its_name() -> None:
    named = Conversation.create("Weekly research").titled_from("something else entirely")
    assert named.title == "Weekly research"


async def test_a_request_naming_a_thread_that_is_gone_is_still_carried() -> None:
    """The work is what the person asked for; a stale window id must not lose it."""
    service, parts = build()

    answer = await service.submit(UserRequest(content="Are you there?", conversation_id=uuid4()))

    assert answer["text"] == "Are you there?"
    assert parts["manager"].received[0][1] is None, "recorded as a standalone objective"


async def test_asking_for_something_that_does_not_exist_is_an_answer_not_a_crash() -> None:
    service, _ = build()

    assert await service.get_conversation(uuid4()) is None
    assert await service.get_objective(uuid4()) is None
    assert await service.get_task(uuid4()) is None
    assert await service.cancel_task(uuid4()) is None


# --- Approvals -----------------------------------------------------------------


async def test_answering_a_question_nothing_is_parked_on_needs_somewhere_to_record_it() -> None:
    """A configuration with no approval service cannot close an open question.

    It is a 409, not a silent success: a window that showed "rejected" for a
    decision nobody stored would be the interface deciding something.
    """
    service, _ = build(waiter=NoWaiter(), approval_service=None)

    with pytest.raises(ApprovalsDisabledError):
        await service.decide_approval(uuid4(), approved=False)


async def test_a_decision_reaches_the_run_that_is_waiting_for_it() -> None:
    question = ApprovalRequest.create(uuid4(), "fs.write(path='report.md')")
    waiter = NoWaiter(question)
    service, _ = build(waiter=waiter)

    answered = await service.decide_approval(question.id, approved=True)

    assert waiter.decisions == [(question.id, True)]
    assert answered["state"] == "APPROVED"
    assert answered["live"] is True, "a tool call in this process was parked on it"


async def test_what_is_waiting_says_which_questions_a_run_is_still_parked_on() -> None:
    question = ApprovalRequest.create(uuid4(), "send an email")
    service, _ = build(waiter=NoWaiter(question))

    [waiting] = await service.list_approvals()

    assert waiting["action"] == "send an email"
    assert waiting["live"] is True


async def test_a_question_names_the_thread_whose_work_asked_it() -> None:
    """So a window puts it in that thread, not in whichever one is on screen.

    A scheduled run asked while the person was starting a new task, and the
    question appeared in the new task, which had asked nothing.
    """
    service, parts = build()
    thread = await service.create_conversation()
    objective = await parts["manager"].receive(
        "Check the news", conversation_id=UUID(thread["id"])
    )
    parts["waiter"]._pending = [ApprovalRequest.create(objective.id, "fs.write")]
    parts["waiter"]._pending.append(ApprovalRequest.create(uuid4(), "fs.delete"))

    asked, orphan = await service.list_approvals()

    assert asked["conversation_id"] == thread["id"]
    assert orphan["conversation_id"] is None


# --- Activity ------------------------------------------------------------------


async def test_activity_follows_a_manager_into_the_tasks_it_delegates() -> None:
    """What makes a trace read as one piece of work.

    The manager stamps its events with the objective; an employee does not -
    it is running a task and knows nothing about a manager. The task ids are
    discovered from what the manager announces.
    """
    objective_id = uuid4()
    task_id = uuid4()
    unrelated = uuid4()

    stream = Stream(
        ProgressEvent(
            task_id=objective_id,
            objective_id=objective_id,
            kind=ProgressKind.STAGE,
            message="Delegating",
            payload={"task_id": str(task_id)},
        ),
        ProgressEvent(task_id=task_id, kind=ProgressKind.TOOL_CALL, message="fs.read"),
        ProgressEvent(task_id=unrelated, kind=ProgressKind.TOOL_CALL, message="someone else"),
        ProgressEvent(
            task_id=objective_id,
            objective_id=objective_id,
            kind=ProgressKind.RESULT,
            message="Done",
        ),
    )
    activity = Activity(
        stream,
        tasks=InMemoryTaskRepository(),
        objectives=InMemoryObjectiveRepository(),
        plans=NoPlans(),
    )

    seen = [event async for event in activity.for_objective(objective_id) if event]

    assert [event.message for event in seen] == ["Delegating", "fs.read", "Done"]
    assert seen[-1].is_final


async def test_a_stream_watching_a_finished_task_ends_instead_of_hanging() -> None:
    """A run that is already over has nothing further to say.

    Ending the stream is what tells a window the work is done without the
    window having to be told separately to stop listening - and without it
    guessing from a timeout, which is the version that shows "working…" forever
    when a process died.
    """
    tasks = InMemoryTaskRepository()
    done, event = Task.create("Tidy up").transition_to(
        TaskStatus.CANCELLED, result=TaskResult(summary="Stopped.")
    )
    await tasks.save(done, event)

    activity = Activity(
        Stream(
            ProgressEvent(task_id=done.id, kind=ProgressKind.STAGE, message="too late"),
        ),
        tasks=tasks,
        objectives=InMemoryObjectiveRepository(),
        plans=NoPlans(),
    )

    seen = [event async for event in activity.for_task(done.id) if event is not None]

    assert seen == [], "the stream ended on a task that had already finished"


def test_an_activity_event_says_what_it_is_without_exposing_the_runtime() -> None:
    event = ActivityEvent.of(
        ProgressEvent(task_id=uuid4(), kind=ProgressKind.TOOL_CALL, message="fs.write", step=3)
    )
    shown = event.to_dict()

    assert shown["kind"] == "TOOL_CALL"
    assert shown["message"] == "fs.write"
    assert shown["step"] == 3
    assert set(shown) == {"task_id", "objective_id", "kind", "message", "step", "payload", "at"}


# --- How a request is carried out, and stopping it -----------------------------


async def test_what_a_request_says_about_how_reaches_the_run_that_does_it() -> None:
    from domain.workforce import directions
    from domain.workforce.directions import ApprovalChoice, Directions

    service, parts = build()
    chosen = Directions(approvals=ApprovalChoice.AUTO, model="balanced")

    await service.submit(UserRequest(content="Sort my files", directions=chosen))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert parts["manager"].directions == [chosen]
    assert directions.current() == directions.NONE, "carried by the run, not left behind"


async def test_stopping_an_objective_nobody_is_carrying_closes_it() -> None:
    """The window said "Reading your request..." through every restart.

    A process that died took the coroutine with it, and stopping cancelled a
    coroutine that was not there and wrote nothing - so the row stayed RECEIVED
    and every interface went on reading it as work in progress.
    """
    service, parts = build()
    left_behind = Objective.create("Sort my files")
    await parts["objectives"].save(left_behind)

    stopped = await service.cancel_objective(left_behind.id)

    assert stopped is not None and stopped["stopped"] is False
    record = await parts["objectives"].get(left_behind.id)
    assert record.status is ObjectiveStatus.CANCELLED
    assert record.result is not None, "an answered turn is one no interface shows as busy"


async def test_a_thread_can_be_renamed_and_keeps_the_name() -> None:
    service, _ = build()
    thread = await service.create_conversation()

    renamed = await service.rename_conversation(UUID(thread["id"]), "  Supplier   renewals ")
    await service.submit(
        UserRequest(content="What is due next month?", conversation_id=UUID(thread["id"]))
    )

    assert renamed["title"] == "Supplier renewals"
    listed = await service.get_conversation(UUID(thread["id"]))
    assert listed["title"] == "Supplier renewals"


async def test_deleting_a_thread_stops_its_work_and_keeps_the_history() -> None:
    service, parts = build()
    thread = await service.create_conversation()
    running = Objective.create("Sort my files", conversation_id=UUID(thread["id"]))
    await parts["objectives"].save(running)

    assert await service.delete_conversation(UUID(thread["id"])) is True

    assert await service.get_conversation(UUID(thread["id"])) is None
    kept = await parts["objectives"].get(running.id)
    assert kept is not None and kept.status is ObjectiveStatus.CANCELLED
    assert await service.delete_conversation(UUID(thread["id"])) is False
