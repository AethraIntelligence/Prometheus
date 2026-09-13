"""Reading a request, and turning it into tasks.

Everything the manager decides later is built on these two calls, so what is
tested here is mostly what happens when the model answers badly: comprehension
that cannot be parsed, a plan that is prose, an id that points nowhere.
"""

from __future__ import annotations

import json

from application.prometheus.intent import IntentReader
from application.prometheus.planner import ObjectivePlanner
from application.prometheus.workforce import describe
from domain.tasks.task import TaskCreatedBy
from domain.workforce.protocols import Objective
from tests.fakes.employees import definition
from tests.fakes.llm import FakeLLM, reply


def objective(text: str = "Find twenty things", **extra) -> Objective:
    return Objective.create(text, **extra)


# --- Reading ------------------------------------------------------------------


async def test_a_request_becomes_constraints_and_criteria() -> None:
    llm = FakeLLM(
        [
            reply(
                json.dumps(
                    {
                        "restatement": "Collect twenty listings into a file.",
                        "constraints": {"count": 20, "format": "a file"},
                        "acceptance_criteria": ["at least 20 rows", "each row has a link"],
                        "needs_work": True,
                        "answer": "",
                    }
                )
            )
        ]
    )

    intent = await IntentReader(llm).read("Find twenty jobs", [definition()])

    assert intent.needs_work
    assert intent.constraints == {"count": 20, "format": "a file"}
    assert intent.acceptance_criteria == ("at least 20 rows", "each row has a link")
    assert not intent.is_answerable_directly


async def test_a_question_that_needs_no_work_is_answered_directly() -> None:
    """The point of §7.5: decomposition is a means, not the product."""
    llm = FakeLLM(
        [
            reply(
                json.dumps(
                    {
                        "restatement": "A factual question.",
                        "needs_work": False,
                        "answer": "SQLite keeps the whole database in one file.",
                    }
                )
            )
        ]
    )

    intent = await IntentReader(llm).read("What is SQLite?", [definition()])

    assert intent.is_answerable_directly
    assert "one file" in intent.answer


async def test_claiming_no_work_without_an_answer_asks_for_the_reply() -> None:
    """Found in the window: a local model read "Hello" as talk and copied the
    blank answer from the form. Treating that as work turned a greeting into a
    plan; the reading was right, and only the reply was missing."""
    llm = FakeLLM(
        [
            reply(json.dumps({"needs_work": False, "answer": "   "})),
            reply("Hello! I run a small team on this machine. What can I do for you?"),
        ]
    )

    intent = await IntentReader(llm).read("Hello", [definition()])

    assert not intent.needs_work
    assert intent.is_conversation
    assert intent.answer.startswith("Hello!")
    assert "Hello" in llm.requests[1].messages[-1].content


async def test_a_reading_of_no_work_is_overruled_by_a_narrower_question() -> None:
    """Found on a local model: the long form said "no work" for today's weather
    and answered it from nothing. Asked only "look it up, or reply?", it said
    look it up - and where the two disagree, it is work."""
    llm = FakeLLM([reply(json.dumps({"needs_work": False, "answer": "Sunny, 18 degrees."}))])
    triage = FakeLLM([reply("B")])

    intent = await IntentReader(llm, triage=triage).read("What is the weather in Milan now?", [])

    assert intent.needs_work
    assert intent.answer == ""
    assert "What is the weather in Milan now?" in triage.last_request.messages[-1].content


async def test_an_answer_from_the_users_own_documents_is_not_second_guessed() -> None:
    llm = FakeLLM(
        [reply(json.dumps({"needs_work": False, "answer": "Twelve euros (Delivery policy)."}))]
    )
    triage = FakeLLM([])

    intent = await IntentReader(llm, triage=triage).read(
        "How much is express delivery?",
        [],
        documents=("[Delivery policy] Express delivery costs twelve euros.",),
    )

    assert not intent.needs_work
    assert triage.requests == []


async def test_talk_both_readings_agree_on_is_answered() -> None:
    llm = FakeLLM([reply(json.dumps({"needs_work": False, "answer": "Hello!"}))])

    intent = await IntentReader(llm, triage=FakeLLM([reply("A")])).read("Hello", [])

    assert intent.is_conversation
    assert intent.answer == "Hello!"


async def test_work_is_never_second_guessed_into_talk() -> None:
    llm = FakeLLM([reply(json.dumps({"needs_work": True, "acceptance_criteria": ["a file"]}))])
    triage = FakeLLM([])

    intent = await IntentReader(llm, triage=triage).read("Write me a file", [])

    assert intent.needs_work
    assert triage.requests == [], "only a reading of no work is checked"


async def test_a_reply_that_still_does_not_come_is_treated_as_work() -> None:
    """A model that says "nothing to do" twice and supplies nothing has said nothing."""
    llm = FakeLLM([reply(json.dumps({"needs_work": False, "answer": ""})), reply("  ")])

    intent = await IntentReader(llm).read("Do the thing", [definition()])

    assert intent.needs_work
    assert not intent.is_answerable_directly


async def test_an_unreadable_reading_still_produces_a_workable_intent() -> None:
    llm = FakeLLM([reply("I think you want me to do something.")])

    intent = await IntentReader(llm).read("Sort my folder", [definition()])

    assert intent.needs_work, "a request that could not be parsed is still a request"
    assert intent.restatement == "Sort my folder"
    assert intent.acceptance_criteria == ()


# --- Decomposing --------------------------------------------------------------


async def test_a_plan_carries_tasks_dependencies_and_its_plan_id() -> None:
    llm = FakeLLM(
        [
            reply(
                json.dumps(
                    {
                        "rationale": "Fetch, then write.",
                        "tasks": [
                            {"id": "t1", "goal": "Collect the listings", "depends_on": []},
                            {"id": "t2", "goal": "Write the file", "depends_on": ["t1"]},
                        ],
                    }
                )
            )
        ]
    )
    target = objective()

    plan = await ObjectivePlanner(llm).plan(target, [definition()])

    assert [task.goal for task in plan.tasks] == ["Collect the listings", "Write the file"]
    assert all(task.plan_id == plan.id for task in plan.tasks), "a task knows its plan"
    assert all(task.created_by is TaskCreatedBy.PROMETHEUS for task in plan.tasks)
    first, second = plan.tasks
    assert plan.dependencies == ((second.id, first.id),)
    assert plan.depends_on(second.id) == frozenset({first.id})
    assert plan.rationale == "Fetch, then write."


async def test_an_unreadable_plan_becomes_the_objective_itself() -> None:
    """One honest task beats a run that never started."""
    llm = FakeLLM([reply("I would start by looking around.")])
    target = objective("Sort my downloads folder")

    plan = await ObjectivePlanner(llm).plan(target, [definition()])

    assert [task.goal for task in plan.tasks] == ["Sort my downloads folder"]
    assert plan.dependencies == ()


async def test_dependencies_on_tasks_that_do_not_exist_are_dropped() -> None:
    """An edge to a task nobody planned could never become ready."""
    llm = FakeLLM(
        [
            reply(
                json.dumps(
                    {
                        "tasks": [
                            {"id": "t1", "goal": "Do it", "depends_on": ["t9", "t1"]},
                        ]
                    }
                )
            )
        ]
    )

    plan = await ObjectivePlanner(llm).plan(objective(), [definition()])

    assert plan.dependencies == (), "a dangling edge and a self-edge are both dropped"
    assert plan.ready(set()) == plan.tasks


async def test_a_plan_is_capped_however_many_tasks_come_back() -> None:
    llm = FakeLLM(
        [
            reply(
                json.dumps(
                    {"tasks": [{"id": f"t{i}", "goal": f"Step {i}"} for i in range(20)]}
                )
            )
        ]
    )

    plan = await ObjectivePlanner(llm, max_tasks=3).plan(objective(), [definition()])

    assert len(plan.tasks) == 3


async def test_replanning_is_told_what_the_last_attempt_missed() -> None:
    llm = FakeLLM([reply(json.dumps({"tasks": [{"id": "t1", "goal": "Try harder"}]}))])

    await ObjectivePlanner(llm).plan(
        objective(), [definition()], revision=2, feedback=("only eleven were found",)
    )

    prompt = llm.last_request.messages[0].content
    assert "only eleven were found" in prompt
    assert "A previous plan did not satisfy the objective" in prompt


# --- What the model is told about the workforce -------------------------------


def test_the_workforce_card_leads_with_what_the_person_can_reach() -> None:
    card = describe([definition("worker", tools=frozenset({"fs.read", "web.search"}))])

    assert "worker" in card
    assert "fs.read, web.search" in card, "tools are the whole of what they can reach"


def test_an_empty_workforce_is_said_not_implied() -> None:
    assert "Nobody is declared" in describe([])
