"""Phase 12.6: when two employees disagree (§87).

Prometheus resolves what the reports themselves settle and escalates the rest. What
it must never do is average two contradictory findings into one confident
paragraph, which is the failure these tests exist to make impossible.
"""

from __future__ import annotations

import json

from application.prometheus.reconciliation import Reconciler
from domain.workforce.protocols import Objective, ObjectiveStatus
from tests.fakes.llm import FakeLLM, reply
from tests.unit.test_prometheus_manager import build, intent, plan, verdict

OBJECTIVE = Objective.create("How many were there?")


def answer(**overrides) -> str:
    return json.dumps(
        {"consistent": True, "conflicts": [], "resolution": "", "resolvable": False, **overrides}
    )


async def test_one_report_is_never_asked_about() -> None:
    """A single result cannot contradict itself, and the call costs money."""
    llm = FakeLLM([])

    result = await Reconciler(llm).reconcile(OBJECTIVE, ("eleven",))

    assert result.consistent
    assert llm.requests == []


async def test_agreeing_reports_pass_through() -> None:
    result = await Reconciler(FakeLLM([reply(answer())])).reconcile(
        OBJECTIVE, ("eleven", "eleven, from the same file")
    )

    assert result.consistent
    assert not result.escalate


async def test_a_conflict_the_evidence_settles_is_resolved() -> None:
    llm = FakeLLM(
        [
            reply(
                answer(
                    consistent=False,
                    conflicts=["one says eleven, the other twenty"],
                    resolvable=True,
                    resolution="Eleven stands: it counted rows, the other estimated.",
                )
            )
        ]
    )

    result = await Reconciler(llm).reconcile(OBJECTIVE, ("eleven", "twenty"))

    assert not result.consistent
    assert result.resolved
    assert not result.escalate


async def test_a_conflict_nothing_settles_is_escalated() -> None:
    llm = FakeLLM(
        [reply(answer(consistent=False, conflicts=["eleven against twenty"], resolvable=False))]
    )

    result = await Reconciler(llm).reconcile(OBJECTIVE, ("eleven", "twenty"))

    assert result.escalate
    assert result.conflicts == ("eleven against twenty",)


async def test_consistent_while_listing_a_contradiction_is_not_consistent() -> None:
    """The list is the specific claim, read the same way a verdict's is."""
    llm = FakeLLM([reply(answer(consistent=True, conflicts=["they disagree on the count"]))])

    result = await Reconciler(llm).reconcile(OBJECTIVE, ("eleven", "twenty"))

    assert not result.consistent


async def test_an_unreadable_answer_means_consistent() -> None:
    """The opposite of the verifier, on purpose: a stutter is not evidence."""
    result = await Reconciler(FakeLLM([reply("I could not decide.")])).reconcile(
        OBJECTIVE, ("eleven", "twenty")
    )

    assert result.consistent


# --- Through the manager ------------------------------------------------------


async def test_an_unresolved_conflict_escalates_instead_of_being_written_up() -> None:
    """The whole point: a blended answer is never produced."""
    llm = FakeLLM(
        [
            reply(intent()),
            reply(plan("count them", "count them again")),
            # Two tasks run, then the reports are compared.
            reply(
                answer(consistent=False, conflicts=["eleven against twenty"], resolvable=False)
            ),
            reply("Between eleven and twenty were found."),
        ]
    )
    manager, execution, _, _ = build(
        script=[], llm=llm, reconciler=Reconciler(llm), workforce=None
    )

    result = await manager.handle_objective(await manager.receive("How many were there?"))

    assert result.status is ObjectiveStatus.ESCALATED
    assert result.missing == ("eleven against twenty",)
    assert len(execution.started) == 2


async def test_a_resolved_conflict_reaches_the_answer_as_a_decision() -> None:
    llm = FakeLLM(
        [
            reply(intent()),
            reply(plan("count them", "count them again")),
            reply(
                answer(
                    consistent=False,
                    conflicts=["eleven against twenty"],
                    resolvable=True,
                    resolution="Eleven stands: it counted, the other estimated.",
                )
            ),
            reply(verdict(True)),
            reply("There were eleven."),
        ]
    )
    manager, _, _, _ = build(script=[], llm=llm, reconciler=Reconciler(llm), workforce=None)

    result = await manager.handle_objective(await manager.receive("How many were there?"))

    assert result.status is ObjectiveStatus.DONE
    synthesis = "\n".join(message.content for message in llm.requests[-1].messages)
    assert "already settled" in synthesis
    assert "Eleven stands" in synthesis
