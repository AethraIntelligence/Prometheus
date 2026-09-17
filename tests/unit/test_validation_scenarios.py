"""The declaration, the expectations and the verdicts - all three without a model.

What is being defended here is that the harness cannot flatter the platform. A
scenario whose expectations were mis-parsed passes on nothing; a run classified
under the wrong failure sends the roadmap somewhere else; a capability called
reliable on one success is a claim the phase exists to stop being made.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from domain.errors import ConfigurationError, NotFoundError, PlanningError, ProviderError
from domain.validation.evidence import CheckResult, CheckStatus, Evidence, Metrics, check
from domain.validation.failures import FailureKind, classify
from domain.validation.reliability import Verdict, build_report, reliability_of
from domain.validation.run import RunStatus, ValidationRun, outcome_of
from domain.validation.scenario import Entry, Expectations, Requirement, Scenario
from infrastructure.validation.yaml_registry import YamlScenarioRegistry


def write(directory: Path, name: str, text: str) -> Path:
    path = directory / f"{name}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


# --- The declaration ----------------------------------------------------------


def test_a_scenario_is_a_file_and_nothing_else(tmp_path: Path) -> None:
    write(
        tmp_path,
        "count-the-invoices",
        """
        name: count-the-invoices
        description: How many invoices are in the folder.
        phase: 4
        employee: organizer
        tags: [files]
        requires: [MEMORY]
        request: Count the invoices in inbox/ and tell me the total.
        setup:
          inbox/one.txt: an invoice
        expect:
          output_contains: [total]
          files_exist: [report.md]
          max_steps: 5
        """,
    )
    scenario = YamlScenarioRegistry(tmp_path).get("count-the-invoices")

    assert scenario.entry is Entry.TASK
    assert scenario.target == "organizer"
    assert scenario.requires == (Requirement.MEMORY,)
    assert scenario.setup == {"inbox/one.txt": "an invoice"}
    assert scenario.expect.max_steps == 5


def test_the_shipped_scenarios_load() -> None:
    """The declarations in `validation/scenarios/` are part of the product."""
    scenarios = YamlScenarioRegistry().list_all()
    assert scenarios, "the platform ships with no scenarios to be measured on"
    assert all(scenario.request or scenario.target for scenario in scenarios)


@pytest.mark.parametrize(
    ("body", "complaint"),
    [
        ("name: other\nrequest: do it", "file"),
        ("name: s\nemployee: a\nworkflow: b\nrequest: x", "both"),
        ("name: s", "request"),
        ("name: s\nrequest: x\nexpect:\n  output_contins: [a]", "unknown expectation"),
        ("name: s\nrequest: x\nsetup:\n  ../escape.txt: hi", "leaves the workspace"),
        ("name: s\nrequest: x\nreset: ['/etc']", "leaves the workspace"),
        ("name: s\nrequest: x\nreset: ['.']", "the workspace itself"),
        ("name: s\nrequest: x\nrequires: [TELEPATHY]", "requires"),
        ("name: s\nrequest: x\nexpect:\n  max_steps: 0", "max_steps"),
    ],
)
def test_a_declaration_that_would_measure_nothing_is_refused(
    tmp_path: Path, body: str, complaint: str
) -> None:
    write(tmp_path, "s", body)
    with pytest.raises(ConfigurationError) as caught:
        YamlScenarioRegistry(tmp_path).get("s")
    assert complaint in str(caught.value)


def test_an_unknown_scenario_says_what_is_declared(tmp_path: Path) -> None:
    write(tmp_path, "here", "name: here\nrequest: do it")
    with pytest.raises(NotFoundError) as caught:
        YamlScenarioRegistry(tmp_path).get("elsewhere")
    assert "here" in str(caught.value)


# --- The expectations ---------------------------------------------------------


def evidence(**changes: object) -> Evidence:
    base = {"succeeded": True, "summary": "The total is 42.", "metrics": Metrics(steps=3)}
    return Evidence(**{**base, **changes})  # type: ignore[arg-type]


def test_every_expectation_is_evaluated_even_after_one_fails() -> None:
    """Three things missing and one thing missing are different situations."""
    results = check(
        Expectations(output_contains=("nowhere",), files_exist=("a.md", "b.md")),
        evidence(files_present=()),
    )
    assert len(results) == 4  # finished, the phrase, and both files
    assert sum(1 for result in results if not result.passed) == 3


def test_a_forbidden_tool_fails_a_run_that_otherwise_answered_correctly() -> None:
    """Phase 10's lesson: a good answer arrived at wrongly is not a pass."""
    results = check(
        Expectations(output_contains=("42",), tools_forbidden=("email.send",)),
        evidence(tools_used=("fs.read", "email.send")),
    )
    assert [result.passed for result in results] == [True, True, False]


def test_a_run_that_was_supposed_to_be_refused_passes_when_it_was() -> None:
    results = check(
        Expectations(tools_denied=("code.run",), must_succeed=False),
        evidence(succeeded=False, tools_denied=("code.run",)),
    )
    assert all(result.passed for result in results)


def test_a_ceiling_is_checked_against_what_was_actually_spent() -> None:
    results = check(
        Expectations(max_cost_usd=0.10, max_steps=2),
        evidence(metrics=Metrics(steps=9, cost_usd=0.4)),
    )
    assert [result.passed for result in results] == [True, False, False]


# --- The classification -------------------------------------------------------


def test_an_exception_is_classified_by_type_never_by_its_message() -> None:
    assert classify(evidence(), error=PlanningError("no plan")) is FailureKind.PLANNING
    assert classify(evidence(), error=ProviderError("upstream")) is FailureKind.MODEL


def test_a_refusal_explains_the_run_it_happened_in() -> None:
    kind = classify(
        evidence(succeeded=False, tools_denied=("code.run",)),
        (CheckResult("finished", CheckStatus.FAILED),),
    )
    assert kind is FailureKind.NEEDED_APPROVAL


def test_being_asked_is_not_the_same_as_being_refused() -> None:
    """A question answered yes changed nothing about the run. Found by the first
    full pass, which blamed approvals for a verifier rejecting a correct file."""
    kind = classify(
        evidence(
            succeeded=False,
            metrics=Metrics(steps=5, tool_calls=5, approvals_requested=1),
        ),
        (CheckResult("finished", CheckStatus.FAILED),),
    )
    assert kind is FailureKind.MODEL


def test_nothing_attempted_at_all_is_a_planning_failure() -> None:
    kind = classify(
        evidence(succeeded=False, metrics=Metrics()),
        (CheckResult("finished", CheckStatus.FAILED),),
    )
    assert kind is FailureKind.PLANNING


def test_a_run_that_did_nothing_at_all_blames_planning_even_if_it_claims_success() -> None:
    """Found by the first real pass: a request to read a folder and leave a
    summary was answered directly, in one sentence, with no task and no tool.
    Deciding a request needs no work is a planning decision."""
    kind = classify(
        evidence(succeeded=True, metrics=Metrics()),
        (CheckResult("file 'summary.md'", CheckStatus.FAILED),),
    )
    assert kind is FailureKind.PLANNING


def test_a_run_that_finished_and_produced_the_wrong_thing_blames_nothing_else() -> None:
    kind = classify(
        evidence(metrics=Metrics(steps=4, tool_calls=2)),
        (CheckResult("file 'a.md'", CheckStatus.FAILED),),
    )
    assert kind is FailureKind.EXPECTATION


def test_a_memory_scenario_with_nothing_to_recall_is_a_memory_failure() -> None:
    kind = classify(
        evidence(succeeded=False, metrics=Metrics(steps=2, tool_calls=1)),
        (CheckResult("finished", CheckStatus.FAILED),),
        memory_expected=True,
    )
    assert kind is FailureKind.MEMORY


def test_a_passing_run_is_at_fault_for_nothing() -> None:
    assert classify(evidence(), (CheckResult("finished", CheckStatus.PASSED),)) is FailureKind.NONE


# --- The verdicts -------------------------------------------------------------


def run(scenario: str, status: RunStatus, **extra: object) -> ValidationRun:
    return ValidationRun.create(scenario, status, **extra)  # type: ignore[arg-type]


def test_one_success_is_not_reliability() -> None:
    """The whole of what the word 'reliably' is doing in the phase's DoD."""
    once = reliability_of("sort", [run("sort", RunStatus.PASSED)])
    assert once.verdict is Verdict.SOMETIMES

    twice = reliability_of("sort", [run("sort", RunStatus.PASSED)] * 2)
    assert twice.verdict is Verdict.RELIABLE


def test_a_skip_is_neither_a_pass_nor_a_failure() -> None:
    entry = reliability_of("screen", [run("screen", RunStatus.SKIPPED)])
    assert entry.verdict is Verdict.UNAVAILABLE
    assert entry.attempts == 0

    report = build_report(["screen"], [run("screen", RunStatus.SKIPPED)])
    assert report.completion_rate == 0.0
    assert report.total_runs == 0  # a machine's configuration is not a result


def test_the_verdict_carries_the_failure_that_keeps_happening() -> None:
    history = [
        run("web", RunStatus.FAILED, failure=FailureKind.TOOL),
        run("web", RunStatus.FAILED, failure=FailureKind.TOOL),
        run("web", RunStatus.FAILED, failure=FailureKind.MODEL),
    ]
    entry = reliability_of("web", history)
    assert entry.verdict is Verdict.NEVER
    assert entry.common_failure is FailureKind.TOOL


def test_a_scenario_that_has_passed_here_is_in_the_regression_set() -> None:
    report = build_report(
        ["sort", "screen"],
        [run("sort", RunStatus.PASSED), run("sort", RunStatus.FAILED)],
    )
    assert report.regression_set == ("sort",)


def test_a_declared_scenario_nobody_ran_is_reported_rather_than_omitted() -> None:
    report = build_report(["untried"], [])
    assert report.of(Verdict.UNTRIED)[0].scenario == "untried"


def test_a_run_is_a_pass_only_when_nothing_is_at_fault_and_nothing_is_missing() -> None:
    passed = outcome_of("s", evidence(), (CheckResult("finished", CheckStatus.PASSED),),
                        FailureKind.NONE)
    assert passed.status is RunStatus.PASSED

    failed = outcome_of("s", evidence(), (CheckResult("f", CheckStatus.FAILED),), FailureKind.NONE)
    assert failed.status is RunStatus.FAILED


def test_a_scenario_knows_what_this_machine_cannot_give_it() -> None:
    scenario = Scenario(name="s", requires=(Requirement.COMPUTER_USE, Requirement.MEMORY))
    assert scenario.missing_requirements(frozenset({Requirement.MEMORY})) == (
        Requirement.COMPUTER_USE,
    )


# --- Phase 16: the refusal a scenario asked for, and the person it declared ----
#
# Every approval recorded by the first full pass was rejected by `no-approver`,
# and the two rules below are what that hid: a run doing exactly what its author
# wrote down could not be recorded as a pass, and the refusal it was written to
# observe was reported as the reason it failed.


def test_a_scenario_that_declared_a_refusal_is_not_blamed_for_it() -> None:
    """`tools_denied` in an expectation is the author asking for that refusal."""
    kind = classify(
        evidence(succeeded=False, tools_denied=("code.run",)),
        (CheckResult("code.run refused", CheckStatus.PASSED),),
        expected_denials=frozenset({"code.run"}),
    )
    assert kind is FailureKind.NONE


def test_a_refusal_nobody_declared_still_explains_the_run() -> None:
    kind = classify(
        evidence(succeeded=False, tools_denied=("fs.write", "code.run")),
        (CheckResult("finished", CheckStatus.FAILED),),
        expected_denials=frozenset({"code.run"}),
    )
    assert kind is FailureKind.NEEDED_APPROVAL


def test_a_run_that_must_not_succeed_can_still_pass() -> None:
    """The declared expectations are the whole standard, `must_succeed` included.

    The workflow scenario that stops where the sending would have been could
    never pass: it correctly did not succeed, every check about that passed, and
    the verdict came back NEEDED_APPROVAL anyway.
    """
    expect = Expectations(tools_denied=("code.run",), must_succeed=False)
    facts = evidence(succeeded=False, tools_denied=("code.run",))
    results = check(expect, facts)
    run = outcome_of(
        "triage",
        facts,
        results,
        classify(facts, results, expected_denials=frozenset(expect.tools_denied)),
    )
    assert run.status is RunStatus.PASSED


def test_a_scenario_says_what_the_person_would_have_answered(tmp_path: Path) -> None:
    write(
        tmp_path,
        "attended",
        "name: attended\nrequest: do it\napprove: [fs.write]\n",
    )
    scenario = YamlScenarioRegistry(tmp_path).get("attended")
    assert scenario.approve == ("fs.write",)


def test_a_scenario_that_says_nothing_is_a_run_nobody_was_there_for(tmp_path: Path) -> None:
    """The default is unattended, which is what a machine nobody asked should be."""
    write(tmp_path, "alone", "name: alone\nrequest: do it\n")
    assert YamlScenarioRegistry(tmp_path).get("alone").approve == ()


def test_work_routed_to_the_wrong_people_fails_however_good_the_file_is() -> None:
    """Phase 11: the choice of employee is checked, not just the count of them."""
    results = check(
        Expectations(files_exist=("r.md",), employees_involved=("analyst", "writer")),
        evidence(files_present=("r.md",), employee_names=("analyst", "researcher")),
    )

    assert [result.passed for result in results] == [True, True, True, False]
    assert "researcher" in results[-1].detail


def test_every_result_accepted_needs_verdicts_and_no_refusal() -> None:
    wanted = Expectations(results_accepted=True)
    two_results = Metrics(steps=3, tasks=2)

    assert all(result.passed for result in check(wanted, evidence(judged=2, metrics=two_results)))
    unjudged = check(wanted, evidence(judged=0, metrics=two_results))[-1]
    assert not unjudged.passed and unjudged.detail == "only 0 of 2 result(s) were judged"
    partial = check(wanted, evidence(judged=1, metrics=two_results))[-1]
    assert not partial.passed and partial.detail == "only 1 of 2 result(s) were judged"
    refused = check(
        wanted,
        evidence(
            judged=2,
            metrics=two_results,
            unaccepted=("Write it (EVIDENCE_MISSING)",),
        ),
    )[-1]
    assert not refused.passed and "EVIDENCE_MISSING" in refused.detail


def test_the_phase_11_scenario_declares_who_must_do_the_work() -> None:
    scenario = YamlScenarioRegistry().get("delegation-handoff-evidence")

    assert scenario.expect.employees_involved == ("analyst", "writer")
    assert scenario.expect.results_accepted is True
