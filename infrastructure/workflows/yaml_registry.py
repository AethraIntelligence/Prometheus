"""Loads workflow declarations from `workflows/*.yaml`.

The employee registry's rule, applied to the other kind of declaration: adding a
workflow is adding a file, and if it ever requires touching a Python file, this
is the one that would have to change - which would be the bug.

Strict about shape and specific about the file, for the same reason: a workflow
is written by hand and read by nobody until a run goes wrong. `step` instead of
`steps`, a dependency on a step that was renamed, or `max_attempts: 0` would all
otherwise show up as a run that quietly did less than the author intended.

What it does not check is the machine: whether the employees a workflow names
are declared here is a question for the employee registry, and `prometheus workflows`
asks it there.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from domain.errors import ConfigurationError, NotFoundError
from domain.workflows.definition import (
    InputKind,
    OnFailure,
    WorkflowBudget,
    WorkflowDefinition,
    WorkflowInput,
    WorkflowProfile,
    WorkflowStep,
    WorkflowTrigger,
)
from domain.workforce.directions import ApprovalChoice
from infrastructure.observability.logging import get_logger

log = get_logger(__name__)

DEFAULT_WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / "workflows"

KNOWN_FIELDS = frozenset(
    {"name", "version", "description", "trigger", "steps", "inputs", "profile", "budget"}
)
KNOWN_STEP_FIELDS = frozenset(
    {"name", "employee", "instruction", "depends_on", "max_attempts", "on_failure"}
)


class YamlWorkflowRegistry:
    """Implements `domain.workflows.protocols.WorkflowRegistry`."""

    def __init__(self, directory: Path | None = None) -> None:
        self._directory = directory or DEFAULT_WORKFLOWS_DIR
        self._loaded: dict[tuple[str, int], WorkflowDefinition] | None = None

    @property
    def directory(self) -> Path:
        return self._directory

    def list_all(self) -> list[WorkflowDefinition]:
        return sorted(
            self._all().values(), key=lambda definition: (definition.name, definition.version)
        )

    def get(self, name: str, version: int | None = None) -> WorkflowDefinition:
        versions = [
            definition for (workflow, _), definition in self._all().items() if workflow == name
        ]
        definition = (
            self._all().get((name, version))
            if version is not None
            else max(versions, key=lambda item: item.version, default=None)
        )
        if definition is None:
            known = ", ".join(
                f"{workflow}@{revision}" for workflow, revision in sorted(self._all())
            ) or "none"
            wanted = f"{name}@{version}" if version is not None else name
            raise NotFoundError(f"Unknown workflow: {wanted}. Declared here: {known}.")
        return definition

    def reload(self) -> None:
        self._loaded = None

    def _all(self) -> dict[tuple[str, int], WorkflowDefinition]:
        if self._loaded is None:
            self._loaded = self._discover()
        return self._loaded

    def _discover(self) -> dict[tuple[str, int], WorkflowDefinition]:
        if not self._directory.is_dir():
            return {}
        found: dict[tuple[str, int], WorkflowDefinition] = {}
        for path in sorted(self._directory.glob("*.yaml")):
            definition = _load(path)
            key = (definition.name, definition.version)
            if key in found:
                raise ConfigurationError(
                    f"{path}: workflow '{definition.name}' version {definition.version} "
                    "is already declared."
                )
            found[key] = definition
        log.info("workflows.loaded", count=len(found), directory=str(self._directory))
        return found


def _load(path: Path) -> WorkflowDefinition:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ConfigurationError(f"{path}: not valid YAML - {error}") from error
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{path}: expected a mapping at the top level.")

    unknown = sorted(set(raw) - KNOWN_FIELDS)
    if unknown:
        raise ConfigurationError(
            f"{path}: unknown field(s) {', '.join(unknown)}. "
            f"Known fields: {', '.join(sorted(KNOWN_FIELDS))}."
        )

    name = str(raw.get("name", "") or "").strip()
    version = raw.get("version", 1)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ConfigurationError(f"{path}: version must be a whole number of 1 or more.")
    # The file name is the identity, exactly as the directory name is for an
    # employee - so two workflows with one name are impossible rather than
    # resolved by load order.
    expected_stems = {name, f"{name}.v{version}"}
    if path.stem not in expected_stems:
        raise ConfigurationError(
            f"{path}: version {version} of '{name}' must be in "
            f"'{name}.yaml' or '{name}.v{version}.yaml'; name and file must match."
        )

    trigger = str(raw.get("trigger", WorkflowTrigger.MANUAL.value)).upper()
    if trigger not in {member.value for member in WorkflowTrigger}:
        raise ConfigurationError(
            f"{path}: trigger '{trigger}' is not one of "
            f"{', '.join(member.value for member in WorkflowTrigger)}."
        )

    steps = raw.get("steps") or []
    if not isinstance(steps, list) or not steps:
        raise ConfigurationError(f"{path}: a workflow needs at least one step.")

    defaults, schema = _inputs(path, raw.get("inputs") or {})
    profile = _profile(path, raw.get("profile") or {})
    budget = _budget(path, raw.get("budget") or {})
    return WorkflowDefinition(
        name=name,
        version=version,
        description=str(raw.get("description", "") or "").strip(),
        trigger=WorkflowTrigger(trigger),
        steps=tuple(_step(path, index, entry) for index, entry in enumerate(steps)),
        inputs=defaults,
        input_schema=schema,
        profile=profile,
        budget=budget,
    )


def _inputs(path: Path, raw: Any) -> tuple[dict[str, Any], dict[str, WorkflowInput]]:
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{path}: inputs must be a mapping.")
    defaults: dict[str, Any] = {}
    schema: dict[str, WorkflowInput] = {}
    for name, value in raw.items():
        spec_fields = {"type", "required", "default", "description"}
        if not isinstance(value, dict) or not set(value) & spec_fields:
            defaults[str(name)] = value
            kind = _kind_of(value)
            schema[str(name)] = WorkflowInput(kind=kind, default=value)
            continue
        unknown = sorted(set(value) - spec_fields)
        if unknown:
            raise ConfigurationError(
                f"{path}: input '{name}' has unknown field(s) {', '.join(unknown)}."
            )
        kind_name = str(value.get("type", "STRING")).upper()
        try:
            kind = InputKind(kind_name)
        except ValueError as error:
            raise ConfigurationError(
                f"{path}: input '{name}' has unknown type '{kind_name}'."
            ) from error
        spec = WorkflowInput(
            kind=kind,
            required=bool(value.get("required", False)),
            default=value.get("default"),
            description=str(value.get("description", "")),
        )
        if "default" in value:
            try:
                defaults[str(name)] = spec.coerce(value["default"])
            except ValueError as error:
                raise ConfigurationError(f"{path}: input '{name}' default {error}.") from error
        schema[str(name)] = spec
    return defaults, schema


def _kind_of(value: Any) -> InputKind:
    if isinstance(value, bool):
        return InputKind.BOOLEAN
    if isinstance(value, int):
        return InputKind.INTEGER
    if isinstance(value, float):
        return InputKind.NUMBER
    return InputKind.STRING


def _profile(path: Path, raw: Any) -> WorkflowProfile:
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{path}: profile must be a mapping.")
    unknown = sorted(set(raw) - {"approvals", "model"})
    if unknown:
        raise ConfigurationError(f"{path}: profile has unknown field(s) {', '.join(unknown)}.")
    try:
        approvals = ApprovalChoice(str(raw.get("approvals", "ASK")).upper())
    except ValueError as error:
        raise ConfigurationError(f"{path}: profile approvals must be ASK, AUTO or DENY.") from error
    return WorkflowProfile(approvals=approvals, model=str(raw.get("model", "")))


def _budget(path: Path, raw: Any) -> WorkflowBudget:
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{path}: budget must be a mapping.")
    fields = {"max_steps", "max_cost_usd", "max_wall_time_seconds"}
    unknown = sorted(set(raw) - fields)
    if unknown:
        raise ConfigurationError(f"{path}: budget has unknown field(s) {', '.join(unknown)}.")
    for name, value in raw.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise ConfigurationError(f"{path}: budget {name} must be greater than zero.")
    return WorkflowBudget(
        max_steps=int(raw["max_steps"]) if "max_steps" in raw else None,
        max_cost_usd=float(raw["max_cost_usd"]) if "max_cost_usd" in raw else None,
        max_wall_time_seconds=(
            float(raw["max_wall_time_seconds"])
            if "max_wall_time_seconds" in raw
            else None
        ),
    )


def _step(path: Path, index: int, raw: Any) -> WorkflowStep:
    where = f"{path}: step {index + 1}"
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{where}: expected a mapping.")

    unknown = sorted(set(raw) - KNOWN_STEP_FIELDS)
    if unknown:
        raise ConfigurationError(
            f"{where}: unknown field(s) {', '.join(unknown)}. "
            f"Known fields: {', '.join(sorted(KNOWN_STEP_FIELDS))}."
        )

    for required in ("name", "employee", "instruction"):
        if not str(raw.get(required, "") or "").strip():
            raise ConfigurationError(f"{where}: '{required}' is required and must have text.")

    attempts = raw.get("max_attempts", 1)
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
        raise ConfigurationError(f"{where}: max_attempts must be a whole number of 1 or more.")

    on_failure = str(raw.get("on_failure", OnFailure.STOP.value)).upper()
    if on_failure not in {member.value for member in OnFailure}:
        raise ConfigurationError(
            f"{where}: on_failure '{on_failure}' is not one of "
            f"{', '.join(member.value for member in OnFailure)}."
        )

    depends_on = raw.get("depends_on") or []
    if isinstance(depends_on, str):
        depends_on = [depends_on]
    if not isinstance(depends_on, list):
        raise ConfigurationError(f"{where}: depends_on must be a list of step names.")

    return WorkflowStep(
        name=str(raw["name"]).strip(),
        employee=str(raw["employee"]).strip(),
        instruction=str(raw["instruction"]).strip(),
        depends_on=tuple(str(entry).strip() for entry in depends_on),
        max_attempts=attempts,
        on_failure=OnFailure(on_failure),
    )
