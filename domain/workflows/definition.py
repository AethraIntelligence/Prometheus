"""A predefined process, declared rather than coded.

A plan is what Prometheus decides an objective takes; a workflow is what somebody
already knows it takes. They run through the same machinery for a reason - the
same employees, the same tools, the same approval gate - and differ only in
where the decomposition came from. That is why a workflow is a declaration and
not a subclass of anything: adding one must not require changing an employee,
and running one must not require a second runtime.

Retry lives on the step (§10.8) rather than on the engine. Whether re-running is
worth it is a property of the work: fetching a page again is free and reasonable,
sending a message again is neither. An engine-wide retry count would have to be
set for the least forgiving step and would then be wrong for every other one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from domain.workforce.directions import ApprovalChoice


class WorkflowTrigger(StrEnum):
    MANUAL = "MANUAL"
    SCHEDULED = "SCHEDULED"
    EVENT = "EVENT"
    CONDITIONAL = "CONDITIONAL"


class OnFailure(StrEnum):
    """What the run does when a step is finally out of attempts."""

    #: Nothing after this step runs. The default: a later step usually reads
    #: what an earlier one produced, and running it anyway produces a confident
    #: answer built on a gap.
    STOP = "STOP"
    #: The step is recorded as failed and the run carries on. For a step whose
    #: output is a nice-to-have - a notification, an extra source.
    CONTINUE = "CONTINUE"


class InputKind(StrEnum):
    STRING = "STRING"
    INTEGER = "INTEGER"
    NUMBER = "NUMBER"
    BOOLEAN = "BOOLEAN"


@dataclass(frozen=True, slots=True)
class WorkflowInput:
    kind: InputKind = InputKind.STRING
    required: bool = False
    default: Any = None
    description: str = ""

    def coerce(self, value: object) -> object:
        """Validate one externally supplied value without guessing broadly."""
        if self.kind is InputKind.STRING:
            if not isinstance(value, str):
                raise ValueError("must be text")
            return value
        if self.kind is InputKind.BOOLEAN:
            if not isinstance(value, bool):
                raise ValueError("must be true or false")
            return value
        if self.kind is InputKind.INTEGER:
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError("must be a whole number")
            return value
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError("must be a number")
        return value


@dataclass(frozen=True, slots=True)
class WorkflowProfile:
    approvals: ApprovalChoice = ApprovalChoice.ASK
    model: str = ""


@dataclass(frozen=True, slots=True)
class WorkflowBudget:
    max_steps: int | None = None
    max_cost_usd: float | None = None
    max_wall_time_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class WorkflowStep:
    name: str
    employee: str
    instruction: str
    depends_on: tuple[str, ...] = ()
    #: How many times to run this step before giving up on it. One means no
    #: retry, which is the safe default for anything that touches the world.
    max_attempts: int = 1
    on_failure: OnFailure = OnFailure.STOP


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    """A predefined process. Adding one must not require changing any employee."""

    name: str
    version: int = 1
    description: str = ""
    trigger: WorkflowTrigger = WorkflowTrigger.MANUAL
    steps: tuple[WorkflowStep, ...] = ()
    inputs: dict[str, Any] = field(default_factory=dict)
    input_schema: dict[str, WorkflowInput] = field(default_factory=dict)
    profile: WorkflowProfile = field(default_factory=WorkflowProfile)
    budget: WorkflowBudget = field(default_factory=WorkflowBudget)

    @property
    def employees(self) -> frozenset[str]:
        """Who this workflow needs. What `prometheus workflows` checks against."""
        return frozenset(step.employee for step in self.steps)

    def values(self, supplied: dict[str, object] | None = None) -> dict[str, object]:
        supplied = supplied or {}
        unknown = sorted(set(supplied) - set(self.inputs) - set(self.input_schema))
        if unknown:
            raise ValueError(f"Unknown workflow input(s): {', '.join(unknown)}")
        values: dict[str, object] = dict(self.inputs)
        for name, spec in self.input_schema.items():
            if name not in values and spec.default is not None:
                values[name] = spec.default
            if spec.required and name not in supplied and name not in values:
                raise ValueError(f"Workflow input '{name}' is required.")
        for name, value in supplied.items():
            spec = self.input_schema.get(name)
            try:
                values[name] = spec.coerce(value) if spec else value
            except ValueError as error:
                raise ValueError(f"Workflow input '{name}' {error}.") from error
        return values

    def to_snapshot(self) -> dict[str, Any]:
        """A complete immutable version suitable for pinning to a schedule."""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "trigger": self.trigger.value,
            "inputs": dict(self.inputs),
            "input_schema": {
                name: {
                    "kind": spec.kind.value,
                    "required": spec.required,
                    "default": spec.default,
                    "description": spec.description,
                }
                for name, spec in self.input_schema.items()
            },
            "profile": {
                "approvals": self.profile.approvals.value,
                "model": self.profile.model,
            },
            "budget": {
                "max_steps": self.budget.max_steps,
                "max_cost_usd": self.budget.max_cost_usd,
                "max_wall_time_seconds": self.budget.max_wall_time_seconds,
            },
            "steps": [
                {
                    "name": step.name,
                    "employee": step.employee,
                    "instruction": step.instruction,
                    "depends_on": list(step.depends_on),
                    "max_attempts": step.max_attempts,
                    "on_failure": step.on_failure.value,
                }
                for step in self.steps
            ],
        }

    @classmethod
    def from_snapshot(cls, raw: dict[str, Any]) -> WorkflowDefinition:
        schema = {
            name: WorkflowInput(
                kind=InputKind(str(spec.get("kind", "STRING"))),
                required=bool(spec.get("required", False)),
                default=spec.get("default"),
                description=str(spec.get("description", "")),
            )
            for name, spec in dict(raw.get("input_schema") or {}).items()
        }
        profile = dict(raw.get("profile") or {})
        budget = dict(raw.get("budget") or {})
        return cls(
            name=str(raw["name"]),
            version=int(raw.get("version", 1)),
            description=str(raw.get("description", "")),
            trigger=WorkflowTrigger(str(raw.get("trigger", "MANUAL"))),
            inputs=dict(raw.get("inputs") or {}),
            input_schema=schema,
            profile=WorkflowProfile(
                approvals=ApprovalChoice(str(profile.get("approvals", "ASK"))),
                model=str(profile.get("model", "")),
            ),
            budget=WorkflowBudget(
                max_steps=budget.get("max_steps"),
                max_cost_usd=budget.get("max_cost_usd"),
                max_wall_time_seconds=budget.get("max_wall_time_seconds"),
            ),
            steps=tuple(
                WorkflowStep(
                    name=str(step["name"]),
                    employee=str(step["employee"]),
                    instruction=str(step["instruction"]),
                    depends_on=tuple(step.get("depends_on") or ()),
                    max_attempts=int(step.get("max_attempts", 1)),
                    on_failure=OnFailure(str(step.get("on_failure", "STOP"))),
                )
                for step in raw.get("steps") or ()
            ),
        )
