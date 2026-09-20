"""The user is answered in the language they were promised.

`PROMETHEUS_RESPONSE_LANGUAGE` existed from Phase 2 and reached exactly one CLI
command. Everything a person reads through the manager - the reply to a request
that needs no work, and the answer at the end of one that did - ignored it, so a
request typed in Russian came back in English. A window makes that a bug you
cannot look away from, which is how Phase 13 found it.

What is checked here is the instruction, not the model's obedience: whether the
sentence was sent is ours, whether it was followed is the model's.
"""

from __future__ import annotations

import json

from application.prometheus.intent import IntentReader
from application.prometheus.language import instruction
from application.prometheus.supervisor import TaskOutcome
from application.prometheus.synthesis import Synthesizer
from domain.llm.models import Role
from domain.tasks.task import Task, TaskResult, TaskStatus
from domain.workforce.protocols import Objective
from tests.fakes.llm import FakeLLM, reply


def outcome() -> TaskOutcome:
    task, event = Task.create("Read the notes").transition_to(
        TaskStatus.RUNNING,
    )
    done, _ = task.transition_to(
        TaskStatus.COMPLETED, result=TaskResult(summary="The notes say the answer is 41.")
    )
    del event
    return TaskOutcome(task=done, employee="researcher")


def system_messages(llm: FakeLLM) -> list[str]:
    """The system messages about language, which are the ones under test.

    Every stage also states what day it is (`application/present.py`), and that
    is not a language instruction: it is sent whatever the setting says, and a
    test that counted it would fail whenever anything else was added beside it.
    """
    return [
        message.content
        for request in llm.requests
        for message in request.messages
        if message.role is Role.SYSTEM and not message.content.startswith("Today is")
    ]


async def test_english_adds_nothing_at_all() -> None:
    """The default must cost nothing: no message, no tokens, no instruction."""
    llm = FakeLLM([reply("The notes say the answer is 41.")])
    await Synthesizer(llm).synthesize(Objective.create("What do my notes say?"), (outcome(),))

    assert system_messages(llm) == []
    assert instruction("en") == ()
    assert instruction("English") == ()
    assert instruction("") == ()


async def test_the_answer_the_user_reads_is_asked_for_in_their_language() -> None:
    # French, because the codebase is English-only and a Latin-script example
    # says the same thing. What is under test is the instruction, not a script.
    llm = FakeLLM([reply("Les notes disent que la reponse est 41.")])
    await Synthesizer(llm, language="fr").synthesize(
        Objective.create("Que disent mes notes?"), (outcome(),)
    )

    [said] = system_messages(llm)
    assert "in fr" in said
    assert "names, numbers, paths" in said, "the specifics survive translation"


async def test_only_the_readable_field_of_an_intent_is_asked_for_in_that_language() -> None:
    """A restatement in another language would reach the planner, not the person.

    The intent reading is machinery with one sentence in it. Telling the model
    to answer wholesale in Russian would hand a Russian restatement to
    decomposition and a Russian key to `extract_object`.
    """
    llm = FakeLLM(
        [reply(json.dumps({"restatement": "greeting", "needs_work": False, "answer": "Bonjour"}))]
    )
    intent = await IntentReader(llm, language="fr").read("bonjour", [])

    [said] = system_messages(llm)
    assert 'the "answer" field' in said
    assert intent.answer == "Bonjour"
    assert intent.needs_work is False


async def test_a_language_nobody_configured_leaves_every_prompt_untouched() -> None:
    llm = FakeLLM([reply(json.dumps({"restatement": "read the notes", "needs_work": True}))])
    await IntentReader(llm).read("Read my notes", [])

    assert system_messages(llm) == []
