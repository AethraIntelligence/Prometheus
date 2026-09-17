from __future__ import annotations

import pytest

from domain.errors import InvalidStateTransitionError
from domain.tasks.task import Task, TaskError, TaskResult, TaskStatus, can_transition


def make_task() -> Task:
    return Task.create("Summarize the weekly changelog")


def test_new_task_starts_in_created() -> None:
    task = make_task()
    assert task.status is TaskStatus.CREATED
    assert task.execution.step == 0
    assert not task.is_terminal


def test_transition_returns_new_task_and_event() -> None:
    task = make_task()
    running, event = task.transition_to(TaskStatus.RUNNING)

    assert running.status is TaskStatus.RUNNING
    assert task.status is TaskStatus.CREATED, "tasks are immutable values"
    assert event.from_status is TaskStatus.CREATED
    assert event.to_status is TaskStatus.RUNNING
    assert event.task_id == task.id


def test_illegal_transition_is_rejected() -> None:
    task = make_task()
    with pytest.raises(InvalidStateTransitionError):
        task.transition_to(TaskStatus.COMPLETED)


def test_terminal_statuses_have_no_way_out() -> None:
    task, _ = make_task().transition_to(TaskStatus.RUNNING)
    completed, _ = task.complete(TaskResult(summary="done"))

    assert completed.is_terminal
    for status in TaskStatus:
        assert not can_transition(TaskStatus.COMPLETED, status)


def test_failure_records_the_error_and_counts_the_attempt() -> None:
    running, _ = make_task().transition_to(TaskStatus.RUNNING)
    failed, event = running.fail(TaskError(kind="ProviderError", message="upstream is down"))

    assert failed.status is TaskStatus.FAILED
    assert failed.attempts == 1
    assert failed.error is not None
    assert event.to_status is TaskStatus.FAILED


def test_only_work_in_progress_is_resumable() -> None:
    running, _ = make_task().transition_to(TaskStatus.RUNNING)
    assert running.is_resumable

    waiting, _ = running.transition_to(TaskStatus.WAITING_FOR_APPROVAL)
    assert not waiting.is_resumable, "a task waiting on a human is not ours to resume"

    completed, _ = running.complete(TaskResult(summary="done"))
    assert not completed.is_resumable


def test_a_safe_pause_can_resume_without_becoming_terminal() -> None:
    running, _ = make_task().transition_to(TaskStatus.RUNNING)
    paused, event = running.transition_to(TaskStatus.PAUSED)

    assert paused.is_resumable
    assert not paused.is_terminal
    assert event.to_status is TaskStatus.PAUSED
    resumed, _ = paused.transition_to(TaskStatus.RUNNING)
    assert resumed.status is TaskStatus.RUNNING


def test_execution_cursor_advances_without_losing_state() -> None:
    task = make_task()
    advanced = task.with_execution(task.execution.advance(cursor="step-1"))
    again = advanced.with_execution(advanced.execution.advance(page=2))

    assert again.execution.step == 2
    assert again.execution.state == {"cursor": "step-1", "page": 2}
