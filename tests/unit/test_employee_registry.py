"""An employee is a declaration. This is where that is enforced."""

from __future__ import annotations

from pathlib import Path

import pytest

from domain.capabilities.models import Capability, CapabilityRequirement
from domain.errors import ConfigurationError, EmployeeNotFoundError
from domain.memory.models import MemoryScope
from infrastructure.employees.yaml_registry import YamlEmployeeRegistry, employee_id_for

REPO_ROOT = Path(__file__).resolve().parents[2]

MINIMAL = """
name: analyst
role: Data Analyst
role_description: Turns numbers into an answer.
goals:
  - text: Say what the data supports.
    priority: 1
allowed_tools: [fs.read]
policies: [no_irreversible_actions]
model_profile:
  capabilities: [TEXT_REASONING, CODE]
  min_context_tokens: 32000
  temperature: 0.1
memory_scope: EMPLOYEE_PRIVATE
limits:
  max_steps: 5
  max_cost_usd: 0.25
  max_wall_time_seconds: 120
"""


def write_employee(root: Path, name: str, body: str) -> None:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "employee.yaml").write_text(body, encoding="utf-8")


def test_the_shipped_researcher_loads_from_its_declaration_alone() -> None:
    registry = YamlEmployeeRegistry(REPO_ROOT / "employees")
    researcher = registry.get("researcher")

    assert researcher.role.title == "Research Specialist"
    assert researcher.goals
    assert researcher.memory_scope is MemoryScope.EMPLOYEE_PRIVATE
    assert researcher.limits.max_steps == 20
    assert researcher.system_prompt.startswith("You are a Research Specialist")
    # Phase 4: it searches and reads pages, and has no tool it does not need.
    assert "web.search" in researcher.allowed_tools
    assert "code.run" not in researcher.allowed_tools


def test_adding_a_second_employee_is_one_yaml_file(tmp_path: Path) -> None:
    """The Phase 3 DoD, stated as a test.

    No Python file is touched, no class is written, nothing is registered.
    """
    write_employee(tmp_path, "analyst", MINIMAL)
    registry = YamlEmployeeRegistry(tmp_path)

    analyst = registry.get("analyst")
    assert analyst.role.title == "Data Analyst"
    assert analyst.allowed_tools == frozenset({"fs.read"})
    assert analyst.model_profile.capabilities == frozenset(
        {Capability.TEXT_REASONING, Capability.CODE}
    )
    assert analyst.model_profile.min_context_tokens == 32000
    assert analyst.limits.max_cost_usd == 0.25
    assert [d.name for d in registry.list()] == ["analyst"]


def test_an_employee_keeps_its_identity_across_restarts(tmp_path: Path) -> None:
    # Derived from the name, not generated: assignment history has to survive a
    # reinstall, and a new random id every boot would orphan it.
    write_employee(tmp_path, "analyst", MINIMAL)

    first = YamlEmployeeRegistry(tmp_path).get("analyst").id
    second = YamlEmployeeRegistry(tmp_path).get("analyst").id

    assert first == second == employee_id_for("analyst")


def test_an_edited_declaration_changes_its_hash_but_not_its_id(tmp_path: Path) -> None:
    write_employee(tmp_path, "analyst", MINIMAL)
    before = YamlEmployeeRegistry(tmp_path).get("analyst")

    (tmp_path / "analyst" / "employee.yaml").write_text(
        MINIMAL.replace("allowed_tools: [fs.read]", "allowed_tools: [fs.read, fs.write]"),
        encoding="utf-8",
    )
    after = YamlEmployeeRegistry(tmp_path).get("analyst")

    assert after.id == before.id
    assert after.definition_hash != before.definition_hash


def test_a_declaration_without_prompts_still_loads(tmp_path: Path) -> None:
    write_employee(tmp_path, "analyst", MINIMAL)
    assert YamlEmployeeRegistry(tmp_path).get("analyst").system_prompt == ""


def test_defaults_apply_when_the_declaration_is_terse(tmp_path: Path) -> None:
    write_employee(tmp_path, "minimal", "name: minimal\nrole: Generalist\n")
    definition = YamlEmployeeRegistry(tmp_path).get("minimal")

    assert definition.allowed_tools == frozenset()
    assert definition.limits.max_steps == 12
    assert definition.model_profile.capabilities == frozenset({Capability.TEXT_REASONING})


def test_a_disabled_employee_is_not_listed(tmp_path: Path) -> None:
    write_employee(tmp_path, "retired", "name: retired\nrole: Former\nenabled: false\n")
    registry = YamlEmployeeRegistry(tmp_path)

    assert registry.list() == []
    assert registry.get("retired").name == "retired", "still addressable by name"


def test_an_unknown_employee_names_the_ones_that_exist(tmp_path: Path) -> None:
    write_employee(tmp_path, "analyst", MINIMAL)
    with pytest.raises(EmployeeNotFoundError, match="analyst"):
        YamlEmployeeRegistry(tmp_path).get("nobody")


def test_a_missing_required_field_is_a_configuration_error(tmp_path: Path) -> None:
    write_employee(tmp_path, "broken", "role: No Name\n")
    with pytest.raises(ConfigurationError, match="missing required field"):
        YamlEmployeeRegistry(tmp_path).list()


def test_an_unknown_capability_is_rejected_rather_than_ignored(tmp_path: Path) -> None:
    write_employee(
        tmp_path, "broken", "name: b\nrole: R\nmodel_profile:\n  capabilities: [TELEPATHY]\n"
    )
    with pytest.raises(ConfigurationError):
        YamlEmployeeRegistry(tmp_path).list()


def test_invalid_yaml_says_which_file(tmp_path: Path) -> None:
    write_employee(tmp_path, "broken", "name: [unclosed\n")
    with pytest.raises(ConfigurationError, match="not valid YAML"):
        YamlEmployeeRegistry(tmp_path).list()


def test_the_directory_is_the_employees_identity(tmp_path: Path) -> None:
    """Which is also what makes two employees with one name impossible.

    `employees/<name>/` is how a person finds the declaration and how a diff
    reads, so a name that disagrees with its directory makes both misleading -
    and a duplicate name would need two directories called the same thing.
    """
    write_employee(tmp_path, "somewhere-else", MINIMAL)
    with pytest.raises(ConfigurationError, match="but sits in 'somewhere-else/'"):
        YamlEmployeeRegistry(tmp_path).list()


def test_employees_are_found_by_what_they_can_do(tmp_path: Path) -> None:
    # This is how Prometheus will discover a new employee in Phase 7 without anyone
    # editing Prometheus.
    write_employee(tmp_path, "analyst", MINIMAL)
    write_employee(tmp_path, "writer", "name: writer\nrole: Writer\n")
    registry = YamlEmployeeRegistry(tmp_path)

    coders = registry.find_by_capability(
        CapabilityRequirement(required=frozenset({Capability.CODE}))
    )
    assert [d.name for d in coders] == ["analyst"]

    everyone = registry.find_by_capability(CapabilityRequirement())
    assert {d.name for d in everyone} == {"analyst", "writer"}
