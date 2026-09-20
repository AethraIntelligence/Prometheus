"""A run is told what day it is, everywhere a model decides what "latest" means.

A real request - "find the latest AI news" - came back about February 2025,
confidently and with sources, because that is where the model's training data
ends and nothing in the prompt said otherwise. A model cannot notice this on its
own: its memory is not marked as old.

What is checked here is that the sentence is sent, in both halves that need it -
the manager, whose reading of a request decides what counts as recent, and the
employee, who does the searching. Whether the model then searches for the right
year is the model's.
"""

from __future__ import annotations

from datetime import datetime

from application.employee_runtime.executor import Executor
from application.present import dated, today
from application.prometheus.intent import IntentReader
from application.prometheus.planner import ObjectivePlanner
from domain.llm.models import Role
from domain.tasks.task import Task
from domain.workforce.protocols import Objective
from tests.fakes.employees import definition
from tests.fakes.llm import FakeLLM, reply

WHEN = datetime(2026, 9, 20, 10, 51)


def system_text(llm: FakeLLM) -> str:
    return "\n".join(
        message.content
        for request in llm.requests
        for message in request.messages
        if message.role is Role.SYSTEM
    )


def test_the_date_is_stated_the_way_a_person_writes_it() -> None:
    assert today(WHEN) == "Sunday, 20 September 2026 (2026-09-20)"
    [message] = dated(WHEN)
    assert "Sunday, 20 September 2026 (2026-09-20)" in message.content
    # The date alone changes nothing: a model reads it, agrees, and goes on
    # answering out of memory. The instruction is the operative half.
    assert "training data ends before this date" in message.content


def test_the_employee_doing_the_searching_is_told() -> None:
    messages = Executor.opening_messages(
        Task.create("Find the latest AI news"), definition(), None, "Be useful."
    )

    assert any(today() in message.content for message in messages)


async def test_the_manager_reads_a_request_knowing_what_recent_means() -> None:
    llm = FakeLLM([reply('{"restatement": "", "needs_work": true}')])

    await IntentReader(llm).read("find the latest AI news", [])

    assert today() in system_text(llm)


async def test_a_plan_is_written_knowing_it_too() -> None:
    llm = FakeLLM([reply('{"tasks": []}')])

    await ObjectivePlanner(llm).plan(Objective.create("Find the latest AI news"), [])

    assert today() in system_text(llm)
