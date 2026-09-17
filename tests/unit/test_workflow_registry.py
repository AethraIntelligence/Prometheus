"""A workflow is a declaration, and a declaration is checked (Phase 10, §10.7).

The employee registry's rule applied to the other kind of file. Every case here
is one that would otherwise show up as a run that quietly did less than the
author meant: a typo'd field silently dropped, a dependency on a step that was
renamed, `max_attempts: 0` meaning no attempts at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from domain.errors import ConfigurationError, NotFoundError
from domain.workflows.definition import OnFailure, WorkflowTrigger
from infrastructure.workflows.yaml_registry import YamlWorkflowRegistry

GOOD = """
name: nightly
description: Tidy up.
steps:
  - name: tidy
    employee: organizer
    instruction: Put it in order.
  - name: report
    employee: researcher
    depends_on: [tidy]
    instruction: Write it up.
    max_attempts: 2
    on_failure: CONTINUE
"""


def declare(root: Path, name: str, body: str) -> YamlWorkflowRegistry:
    (root / f"{name}.yaml").write_text(body, encoding="utf-8")
    return YamlWorkflowRegistry(root)


def test_a_whole_declaration_is_read(tmp_path: Path) -> None:
    definition = declare(tmp_path, "nightly", GOOD).get("nightly")

    assert definition.trigger is WorkflowTrigger.MANUAL
    assert [step.name for step in definition.steps] == ["tidy", "report"]
    assert definition.steps[1].depends_on == ("tidy",)
    assert definition.steps[1].max_attempts == 2
    assert definition.steps[1].on_failure is OnFailure.CONTINUE
    assert definition.employees == {"organizer", "researcher"}


def test_an_empty_directory_declares_nothing(tmp_path: Path) -> None:
    assert YamlWorkflowRegistry(tmp_path).list_all() == []


def test_a_directory_that_is_not_there_is_not_an_error(tmp_path: Path) -> None:
    """A machine with no workflows is a normal machine."""
    assert YamlWorkflowRegistry(tmp_path / "absent").list_all() == []


def test_asking_for_one_that_does_not_exist_says_what_does(tmp_path: Path) -> None:
    registry = declare(tmp_path, "nightly", GOOD)
    with pytest.raises(NotFoundError, match="nightly"):
        registry.get("nightlee")


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("name: x\nstep:\n  - name: a\n", "unknown field"),
        ("name: x\nsteps: []\n", "at least one step"),
        ("name: x\n", "at least one step"),
        ("name: x\ntrigger: SOMETIMES\nsteps: [{name: a, employee: b, instruction: c}]\n",
         "trigger"),
        ("name: x\nsteps: [{name: a, employee: b, instruction: c, oops: 1}]\n", "unknown field"),
        ("name: x\nsteps: [{employee: b, instruction: c}]\n", "'name' is required"),
        ("name: x\nsteps: [{name: a, employee: '', instruction: c}]\n", "'employee' is required"),
        ("name: x\nsteps: [{name: a, employee: b, instruction: c, max_attempts: 0}]\n",
         "max_attempts"),
        ("name: x\nsteps: [{name: a, employee: b, instruction: c, on_failure: MAYBE}]\n",
         "on_failure"),
        ("name: x\nsteps: [{name: a, employee: b, instruction: c, depends_on: 7}]\n",
         "depends_on"),
    ],
)
def test_a_declaration_that_says_something_untrue_is_refused(
    tmp_path: Path, body: str, expected: str
) -> None:
    registry = declare(tmp_path, "x", body)
    with pytest.raises(ConfigurationError, match=expected):
        registry.list_all()


def test_the_file_name_is_the_identity(tmp_path: Path) -> None:
    """Same rule as an employee's directory: two workflows with one name are
    impossible rather than resolved by load order."""
    registry = declare(tmp_path, "nightly", "name: something-else\nsteps: [{name: a, "
                       "employee: b, instruction: c}]\n")
    with pytest.raises(ConfigurationError, match="must match"):
        registry.list_all()


def test_a_single_dependency_may_be_written_without_a_list(tmp_path: Path) -> None:
    """A convenience that is worth the line: `depends_on: tidy` is what people
    write, and refusing it would be pedantry rather than strictness."""
    body = (
        "name: x\nsteps:\n"
        "  - {name: a, employee: b, instruction: c}\n"
        "  - {name: d, employee: b, instruction: c, depends_on: a}\n"
    )
    definition = declare(tmp_path, "x", body).get("x")
    assert definition.steps[1].depends_on == ("a",)


def test_reloading_picks_up_a_file_added_since(tmp_path: Path) -> None:
    registry = declare(tmp_path, "nightly", GOOD)
    assert len(registry.list_all()) == 1

    declare(tmp_path, "weekly", GOOD.replace("name: nightly", "name: weekly"))
    registry.reload()

    assert [d.name for d in registry.list_all()] == ["nightly", "weekly"]


def test_versions_are_addressable_and_latest_is_explicit(tmp_path: Path) -> None:
    declare(tmp_path, "nightly", GOOD)
    (tmp_path / "nightly.v2.yaml").write_text(
        GOOD.replace("name: nightly", "name: nightly\nversion: 2"),
        encoding="utf-8",
    )
    registry = YamlWorkflowRegistry(tmp_path)

    assert registry.get("nightly").version == 2
    assert registry.get("nightly", 1).version == 1
    assert registry.get("nightly", 2).version == 2


def test_typed_inputs_profiles_and_budgets_are_checked(tmp_path: Path) -> None:
    body = """
name: typed
version: 1
inputs:
  count: {type: INTEGER, required: true, description: Number of reports.}
profile: {approvals: DENY, model: careful}
budget: {max_steps: 2, max_cost_usd: 1.5, max_wall_time_seconds: 60}
steps:
  - {name: run, employee: organizer, instruction: "Make {count} reports"}
"""
    definition = declare(tmp_path, "typed", body).get("typed", 1)

    assert definition.values({"count": 2}) == {"count": 2}
    assert definition.profile.approvals.value == "DENY"
    assert definition.profile.model == "careful"
    assert definition.budget.max_cost_usd == 1.5
    with pytest.raises(ValueError, match="whole number"):
        definition.values({"count": "two"})
    with pytest.raises(ValueError, match="required"):
        definition.values()
