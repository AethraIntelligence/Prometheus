"""The employee registry, with integration grants already applied.

A decorator rather than a change to `YamlEmployeeRegistry`, because the two
answer different questions and only one of them is about files. The YAML
registry reads declarations, which is a property of the repository; whether
`gmail` is connected on this machine right now is a property of the machine, and
mixing them would make reading an employee file depend on a database.

So this wraps any `EmployeeRegistry` and hands out the same employees with their
grants expanded (`domain.integrations.grants`). Everything above - Prometheus's
capability search, the delegator, the runtime, the tool registry's own least
privilege check - keeps working on an `EmployeeDefinition` and learns nothing.

**The snapshot is refreshed, not queried.** `EmployeeRegistry.list` is
synchronous and called while a plan is being made; reaching a database from it
would put I/O on that path, and an async version would spread through every
caller for no gain. So the integrations are loaded once at start-up and again
whenever the set actually changes - connecting, enabling, removing - which is a
person-sized event rather than a per-call one.
"""

from __future__ import annotations

from collections.abc import Iterable

from domain.capabilities.models import CapabilityRequirement
from domain.employees.definition import EmployeeDefinition
from domain.employees.protocols import EmployeeRegistry
from domain.integrations.grants import granted
from domain.integrations.models import Integration
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId


class GrantingEmployeeRegistry:
    """Implements `domain.employees.protocols.EmployeeRegistry`."""

    def __init__(
        self, inner: EmployeeRegistry, integrations: Iterable[Integration] = ()
    ) -> None:
        self._inner = inner
        self._integrations: tuple[Integration, ...] = tuple(integrations)

    def refresh(self, integrations: Iterable[Integration]) -> None:
        """Take the current set. Called when a person changed something."""
        self._integrations = tuple(integrations)

    @property
    def integrations(self) -> tuple[Integration, ...]:
        return self._integrations

    def declared_integrations(self, name: str) -> frozenset[str] | None:
        """What the employee's own file names, before any grant from the window.

        Readiness needs the difference: a service the file requires makes the
        employee unavailable without it, a service granted later only adds.
        """
        try:
            return self._inner.get(name).integrations
        except Exception:
            return None

    def _granted(self, definition: EmployeeDefinition) -> EmployeeDefinition:
        return granted(definition, self._integrations)

    def list(
        self, workspace: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> list[EmployeeDefinition]:
        return [self._granted(d) for d in self._inner.list(workspace)]

    def get(self, name: str) -> EmployeeDefinition:
        return self._granted(self._inner.get(name))

    def find_by_capability(
        self, requirement: CapabilityRequirement
    ) -> list[EmployeeDefinition]:
        """Searched *after* expansion, so a granted capability is discoverable.

        Delegating to the inner registry's search would ask it about
        capabilities the declarations do not carry - the whole point of a grant
        is that holding an integration is what makes the employee able - so the
        search happens here, over the expanded definitions, using the same
        `offers` the inner registry uses.
        """
        candidates = [d for d in self.list() if d.enabled and d.offers(requirement)]
        return sorted(
            candidates,
            key=lambda d: requirement.score(d.capabilities or d.model_profile.capabilities),
            reverse=True,
        )
