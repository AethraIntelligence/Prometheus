"""Loads employee declarations from `employees/*/employee.yaml`.

This is where "an employee is a declaration, not code" is actually enforced. If
adding an employee ever requires touching a Python file, it will be this one -
and that would be the bug.

It is strict about shape and says where the problem is. A declaration is written
by hand and read by nobody until something goes wrong with it, so `allowed_tool`
instead of `allowed_tools`, a temperature of 20, or a goal that is a dict with
no `text` would otherwise all fail silently - as an employee with no tools, a
model told to be maximally random, or a goal that reads as `{}`. Every one of
those is refused at load with the file that caused it.

What it deliberately does *not* check is anything about the machine: whether a
tool exists, whether a capability is backed. That needs the tool registry, it
differs between machines, and `domain.employees.validation` does it where both
are known.

It lives in `infrastructure/` rather than `application/` for the same reason the
task repository does: it reads from disk. The plan sketches it under
`application/workforce/`, but the layering rule it also states - application
depends on the domain only - puts file I/O here.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid5

import yaml

from domain.capabilities.models import Capability, CapabilityRequirement
from domain.employees.contract import (
    UNDECLARED,
    EvidenceKind,
    FailureKind,
    WorkContract,
    WorkProduct,
)
from domain.employees.definition import EmployeeDefinition, Goal, Role
from domain.employees.limits import ExecutionLimits
from domain.errors import ConfigurationError, EmployeeNotFoundError
from domain.llm.models import ModelProfile
from domain.memory.models import MemoryScope
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId
from infrastructure.observability.logging import get_logger

log = get_logger(__name__)

DEFAULT_EMPLOYEES_DIR = Path(__file__).resolve().parents[2] / "employees"

#: Employee ids are derived from the name rather than generated, so the same
#: declaration keeps the same identity across restarts and across machines -
#: which is what makes assignment history survive a reinstall.
EMPLOYEE_NAMESPACE = UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


#: Every field a declaration may carry. Anything else is a typo, and refusing
#: it is the whole reason the set is written down.
KNOWN_FIELDS = frozenset(
    {
        "name",
        "role",
        "role_description",
        "goals",
        "allowed_tools",
        "integrations",
        "capabilities",
        "policies",
        "model_profile",
        "memory_scope",
        "limits",
        "enabled",
        "contract",
    }
)

#: Every field a `contract:` may carry. Same reason as above.
KNOWN_CONTRACT_FIELDS = frozenset({"accepts", "produces", "evidence", "failure_kinds"})


def employee_id_for(name: str, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID) -> UUID:
    return uuid5(EMPLOYEE_NAMESPACE, f"{workspace_id}/{name}")


def _goals(path: Path, raw: object) -> tuple[Goal, ...]:
    if not isinstance(raw, list | tuple):
        raise ConfigurationError(f"{path}: goals must be a list")
    goals: list[Goal] = []
    for index, item in enumerate(raw):
        if isinstance(item, dict):
            text = str(item.get("text", "")).strip()
            if not text:
                raise ConfigurationError(f"{path}: goal {index + 1} has no text")
            goals.append(Goal(text=text, priority=int(item.get("priority", 5))))
        elif str(item).strip():
            goals.append(Goal(text=str(item).strip()))
        else:
            raise ConfigurationError(f"{path}: goal {index + 1} is empty")
    return tuple(goals)


def _names(path: Path, field: str, raw: object) -> frozenset[str]:
    if not isinstance(raw, list | tuple):
        raise ConfigurationError(f"{path}: {field} must be a list")
    names = [str(item).strip() for item in raw]
    if any(not name for name in names):
        raise ConfigurationError(f"{path}: {field} contains an empty entry")
    return frozenset(names)


def _limits(path: Path, raw: object) -> ExecutionLimits:
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{path}: limits must be a mapping")
    try:
        limits = ExecutionLimits(
            max_steps=int(raw.get("max_steps", 12)),
            max_cost_usd=float(raw.get("max_cost_usd", 1.0)),
            max_wall_time_seconds=float(raw.get("max_wall_time_seconds", 600.0)),
        )
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"{path}: limits must be numbers ({error})") from error
    for name, value in (
        ("max_steps", limits.max_steps),
        ("max_cost_usd", limits.max_cost_usd),
        ("max_wall_time_seconds", limits.max_wall_time_seconds),
    ):
        # A budget of zero is not a careful employee; it is one that stops
        # before its first step, having spent a model call to get there.
        if value <= 0:
            raise ConfigurationError(f"{path}: {name} must be greater than 0, not {value}")
    return limits


def _contract(path: Path, raw: object) -> WorkContract:
    """The hand-off terms, or the defaults a declaration without them has always had.

    Missing is not an error: every declaration written before Phase 11 has no
    contract and must route exactly as it did. A contract that is present is
    read as strictly as the rest of the file - `produce` instead of `produces`
    would otherwise be a role that silently delivers only an answer.
    """
    if raw is None:
        return UNDECLARED
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{path}: contract must be a mapping")
    unknown = sorted(set(raw) - KNOWN_CONTRACT_FIELDS)
    if unknown:
        raise ConfigurationError(
            f"{path}: contract has unknown field(s): {', '.join(unknown)}. "
            f"Known fields: {', '.join(sorted(KNOWN_CONTRACT_FIELDS))}"
        )

    def terms[T](field: str, vocabulary: type[T], default: frozenset[T]) -> frozenset[T]:
        if field not in raw:
            return default
        values = raw[field]
        if not isinstance(values, list | tuple):
            raise ConfigurationError(f"{path}: contract.{field} must be a list")
        try:
            return frozenset(vocabulary(str(value).strip().upper()) for value in values)  # type: ignore[call-arg]
        except ValueError as error:
            known = ", ".join(item.value for item in vocabulary)  # type: ignore[attr-defined]
            raise ConfigurationError(
                f"{path}: contract.{field}: {error}. Known: {known}"
            ) from error

    return WorkContract(
        accepts=terms("accepts", WorkProduct, UNDECLARED.accepts),
        produces=terms("produces", WorkProduct, UNDECLARED.produces),
        evidence=terms("evidence", EvidenceKind, UNDECLARED.evidence),
        failure_kinds=terms("failure_kinds", FailureKind, UNDECLARED.failure_kinds),
        declared=True,
    )


def _system_prompt(path: Path) -> str:
    prompt = path.parent / "prompts" / "system.md"
    return prompt.read_text(encoding="utf-8").strip() if prompt.exists() else ""


class YamlEmployeeRegistry:
    """Implements `domain.employees.protocols.EmployeeRegistry`."""

    def __init__(
        self,
        directory: Path | None = None,
        workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID,
    ) -> None:
        self._directory = directory or DEFAULT_EMPLOYEES_DIR
        self._workspace_id = workspace_id
        self._definitions: dict[str, EmployeeDefinition] | None = None

    # --- Discovery ------------------------------------------------------------

    def _load(self) -> dict[str, EmployeeDefinition]:
        if self._definitions is not None:
            return self._definitions

        definitions: dict[str, EmployeeDefinition] = {}
        for path in sorted(self._directory.glob("*/employee.yaml")):
            definition = self._parse(path)
            if definition.name in definitions:
                raise ConfigurationError(
                    f"Two employees are called '{definition.name}'; names must be unique"
                )
            definitions[definition.name] = definition

        self._definitions = definitions
        log.info("employees.loaded", count=len(definitions), names=sorted(definitions))
        return definitions

    def _parse(self, path: Path) -> EmployeeDefinition:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as error:
            raise ConfigurationError(f"{path} is not valid YAML: {error}") from error
        if not isinstance(raw, dict):
            raise ConfigurationError(f"{path} must contain a mapping, not {type(raw).__name__}")

        unknown = sorted(set(raw) - KNOWN_FIELDS)
        if unknown:
            # A misspelt field is the failure this catches: `allowed_tool` is
            # not a stricter employee, it is an employee with no tools at all.
            raise ConfigurationError(
                f"{path} has unknown field(s): {', '.join(unknown)}. "
                f"Known fields: {', '.join(sorted(KNOWN_FIELDS))}"
            )

        for required in ("name", "role"):
            if not str(raw.get(required, "")).strip():
                raise ConfigurationError(f"{path} is missing required field '{required}'")

        name = str(raw["name"]).strip()
        if name != path.parent.name:
            # The directory is how a person finds the file and how `git diff`
            # reads; a name that disagrees with it makes both misleading.
            raise ConfigurationError(
                f"{path} declares name '{name}' but sits in '{path.parent.name}/'. "
                "They must match."
            )

        role = Role(
            title=str(raw["role"]).strip(),
            description=str(raw.get("role_description", "")).strip(),
        )

        profile = raw.get("model_profile") or {}
        if not isinstance(profile, dict):
            raise ConfigurationError(f"{path}: model_profile must be a mapping")
        try:
            temperature = float(profile.get("temperature", 0.2))
            model_profile = ModelProfile(
                capabilities=frozenset(
                    Capability(c) for c in profile.get("capabilities", ["TEXT_REASONING"])
                ),
                min_context_tokens=profile.get("min_context_tokens"),
                max_cost_per_1k_usd=profile.get("max_cost_per_1k_usd"),
                temperature=temperature,
            )
            capabilities = frozenset(Capability(c) for c in raw.get("capabilities") or ())
            memory_scope = MemoryScope(raw.get("memory_scope", MemoryScope.EMPLOYEE_PRIVATE))
        except (TypeError, ValueError) as error:
            known = ", ".join(c.value for c in Capability)
            raise ConfigurationError(f"{path}: {error}. Known capabilities: {known}") from error
        if not 0.0 <= temperature <= 2.0:
            raise ConfigurationError(
                f"{path}: temperature must be between 0 and 2, not {temperature}"
            )

        definition = EmployeeDefinition(
            id=employee_id_for(name, self._workspace_id),
            name=name,
            role=role,
            goals=_goals(path, raw.get("goals") or ()),
            allowed_tools=_names(path, "allowed_tools", raw.get("allowed_tools") or ()),
            integrations=_names(path, "integrations", raw.get("integrations") or ()),
            capabilities=capabilities,
            policies=_names(path, "policies", raw.get("policies") or ()),
            model_profile=model_profile,
            memory_scope=memory_scope,
            limits=_limits(path, raw.get("limits") or {}),
            system_prompt=_system_prompt(path),
            contract=_contract(path, raw.get("contract")),
            workspace_id=self._workspace_id,
            enabled=bool(raw.get("enabled", True)),
        )
        return definition

    # --- EmployeeRegistry -----------------------------------------------------

    def list(self, workspace: WorkspaceId = DEFAULT_WORKSPACE_ID) -> list[EmployeeDefinition]:
        return [
            definition
            for definition in self._load().values()
            if definition.workspace_id == workspace and definition.enabled
        ]

    def get(self, name: str) -> EmployeeDefinition:
        definition = self._load().get(name)
        if definition is None:
            known = ", ".join(sorted(self._load())) or "none"
            raise EmployeeNotFoundError(f"{name}. Declared employees: {known}")
        return definition

    def find_by_capability(
        self, requirement: CapabilityRequirement
    ) -> list[EmployeeDefinition]:
        """Which employees could take on work with these requirements.

        Matched against what the employee *declares it can do*, not against what
        its model can do: an employee that sorts files needs no more of a model
        than one that does not, and what makes it able to sort them is the tools
        it was granted. Ranked so the closest fit comes first - among candidates
        that all qualify, the one that also offers what was preferred.

        This is how Prometheus finds a newly declared employee without anyone editing
        Prometheus, which is the whole of Phase 8's Definition of Done.
        """
        matching = [
            definition for definition in self.list() if definition.offers(requirement)
        ]
        return sorted(
            matching,
            key=lambda d: (
                -requirement.score(d.capabilities or d.model_profile.capabilities),
                d.name,
            ),
        )
