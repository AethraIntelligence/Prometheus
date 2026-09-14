"""Turning "this employee may use Gmail" into names a registry can enforce.

A grant is declared per integration and enforced per tool, and this is the one
place the two meet. `ToolRegistry` already refuses a tool an actor was not
granted, by name, before a call is built - so rather than teaching it a second
kind of permission, the integration's current tool names are added to the
employee's `allowed_tools` and least privilege stays exactly where it was.

Three properties are the reason it is written as a function over values rather
than as a lookup somewhere:

**It can only add.** An employee's own `allowed_tools` is never reduced here,
and a grant contributes only names belonging to an integration that employee
named. Nothing in this module can widen anything the user did not write down.

**A disabled or unusable integration contributes nothing.** Not an error - the
declaration is still right, the service is merely switched off - and the
employee simply cannot see those tools until it is switched back on.

**A grant nobody can satisfy is silence, not a failure.** An employee naming an
integration this machine has never had loads and runs; it is reported by
`domain.employees.validation` as a warning, for the same reason a missing tool
is: the declaration may be right and the machine merely configured differently.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from domain.employees.definition import EmployeeDefinition
from domain.integrations.models import Integration


def granted(
    definition: EmployeeDefinition, integrations: Iterable[Integration]
) -> EmployeeDefinition:
    """The employee as it should actually be run, with its grants expanded."""
    usable = [
        integration
        for integration in integrations
        if integration.is_usable and holds(definition, integration)
    ]
    if not usable:
        return definition

    tools = set(definition.allowed_tools)
    capabilities = set(definition.capabilities)
    names = set(definition.integrations)
    for integration in usable:
        tools |= integration.tool_names
        capabilities |= integration.granted_capabilities
        # The name too, so routing by service (ADR 0018) finds a holder granted
        # from the window exactly as it finds one granted in the file.
        names.add(integration.name)
    return replace(
        definition,
        allowed_tools=frozenset(tools),
        capabilities=frozenset(capabilities),
        integrations=frozenset(names),
    )


def holds(definition: EmployeeDefinition, integration: Integration) -> bool:
    """Granted either way: in the employee's own file, or on this machine's record."""
    return (
        integration.name in definition.integrations
        or definition.name in integration.granted_to
    )


def missing(
    definition: EmployeeDefinition, integrations: Iterable[Integration]
) -> frozenset[str]:
    """Integrations this employee names that this machine does not offer."""
    available = {integration.name for integration in integrations}
    return frozenset(definition.integrations - available)
