"""An employee is a declaration, not code.

Adding `Prometheus Legal` or `Prometheus Recruiter` means adding a definition. It must not
require a change in `application/`, `infrastructure/`, or in Prometheus itself.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID, uuid4

from domain.capabilities.models import Capability, CapabilityRequirement
from domain.employees.contract import UNDECLARED, WorkContract
from domain.employees.limits import ExecutionLimits
from domain.llm.models import ModelProfile
from domain.memory.models import MemoryScope
from domain.policies.models import ActorKind
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId

EmployeeId = UUID


@dataclass(frozen=True, slots=True)
class Role:
    """What the employee is, in the words a human would use."""

    title: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class Goal:
    """A standing objective the employee works towards."""

    text: str
    priority: int = 5


@dataclass(frozen=True, slots=True)
class EmployeeDefinition:
    """Everything that makes one employee different from another.

    The runtime is shared; only this differs. A second runtime would be a design
    error, not a feature.
    """

    id: EmployeeId
    name: str
    role: Role
    goals: tuple[Goal, ...] = ()
    allowed_tools: frozenset[str] = field(default_factory=frozenset)
    #: What kinds of work this employee can take on - the vocabulary Prometheus
    #: searches by. Distinct from `model_profile.capabilities`, which is what it
    #: needs *from a model*: an employee that reads files needs no more of a
    #: model than one that does not, and what makes it able to is the tools it
    #: was granted. Declared here rather than derived from those tools so the
    #: registry stays a reader of declarations, and checked against them by
    #: `domain.employees.validation` so it cannot quietly claim more than it has.
    capabilities: frozenset[Capability] = field(default_factory=frozenset)
    #: Integrations this employee is granted, by name. A grant is per
    #: integration rather than per tool because an integration's tool names are
    #: not known when this file is written and change when its server is
    #: updated - a permission that has to be re-typed after every update is a
    #: permission that stops matching what the employee needs (ADR 0015). It is
    #: expanded into `allowed_tools` where the two are known, so least privilege
    #: is still enforced against a list of names and this is still a
    #: declaration.
    integrations: frozenset[str] = field(default_factory=frozenset)
    policies: frozenset[str] = field(default_factory=frozenset)
    model_profile: ModelProfile = field(default_factory=ModelProfile)
    memory_scope: MemoryScope = MemoryScope.EMPLOYEE_PRIVATE
    #: What one run of this employee is allowed to spend. Part of the
    #: declaration, because how much a job is worth is a property of the job.
    limits: ExecutionLimits = field(default_factory=ExecutionLimits)
    #: The employee's own voice, shipped alongside its YAML.
    system_prompt: str = ""
    #: What it needs handed to it and what it hands back (Phase 11). A
    #: declaration without one gets `UNDECLARED`, whose defaults route exactly
    #: as before and say that they are defaults.
    contract: WorkContract = UNDECLARED
    workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    enabled: bool = True

    @classmethod
    def create(cls, name: str, role: Role, **extra: object) -> EmployeeDefinition:
        return cls(id=uuid4(), name=name, role=role, **extra)  # type: ignore[arg-type]

    # --- Actor protocol -------------------------------------------------------
    # An employee definition is itself an Actor: policies apply to it directly.

    @property
    def actor_id(self) -> str:
        return str(self.id)

    @property
    def actor_kind(self) -> ActorKind:
        return ActorKind.EMPLOYEE

    def offers(self, requirement: CapabilityRequirement) -> bool:
        """Whether this employee could take on work with these requirements.

        An employee that declared nothing is not thereby able to do everything:
        it falls back to what its model can do, which is reasoning and no more.
        """
        return requirement.is_satisfied_by(
            self.capabilities or self.model_profile.capabilities
        )

    @property
    def definition_hash(self) -> str:
        """Version of the declaration, used to detect edited employee files."""
        payload = json.dumps(
            {
                "name": self.name,
                "role": [self.role.title, self.role.description],
                "goals": [[g.text, g.priority] for g in self.goals],
                "allowed_tools": sorted(self.allowed_tools),
                "integrations": sorted(self.integrations),
                "capabilities": sorted(str(c) for c in self.capabilities),
                "policies": sorted(self.policies),
                "memory_scope": str(self.memory_scope),
                "system_prompt": self.system_prompt,
                "limits": [
                    self.limits.max_steps,
                    self.limits.max_cost_usd,
                    self.limits.max_wall_time_seconds,
                ],
                "contract": {
                    "accepts": sorted(self.contract.accepts),
                    "produces": sorted(self.contract.produces),
                    "evidence": sorted(self.contract.evidence),
                    "failure_kinds": sorted(self.contract.failure_kinds),
                    "declared": self.contract.declared,
                },
                "model_profile": {
                    "capabilities": sorted(str(c) for c in self.model_profile.capabilities),
                    "min_context_tokens": self.model_profile.min_context_tokens,
                    "max_cost_per_1k_usd": self.model_profile.max_cost_per_1k_usd,
                    "temperature": self.model_profile.temperature,
                },
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()


class EmployeeStatus(StrEnum):
    IDLE = "IDLE"
    BUSY = "BUSY"
    DISABLED = "DISABLED"
