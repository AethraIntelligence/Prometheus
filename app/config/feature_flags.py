"""Flags for capabilities that arrive in later phases.

Each one is off until the phase that implements it lands, so a half-built
capability can sit in the tree without being reachable. Once the phase lands,
the flag stays as the switch that turns the capability off on a machine where
it is not wanted - Phase 5's DoD depends on exactly that.
"""

from __future__ import annotations

from pydantic import BaseModel


class FeatureFlags(BaseModel):
    browser_tools: bool = True  # Phase 4
    code_execution: bool = True  # Phase 4
    approvals: bool = True  # Phase 4
    #: On by default since 2026-09-13, at the user's request. What still stands
    #: between an employee and the machine is not this flag: an empty
    #: PROMETHEUS_COMPUTER_ALLOWED_APPLICATIONS refuses every desktop action,
    #: and every one that is allowed waits for a person.
    computer_use: bool = True  # Phase 5
    prometheus_manager: bool = False  # Phase 7
    memory: bool = True  # Phase 9
    workflows: bool = True  # Phase 10
    scheduler: bool = False  # Phase 12
    # Phase 14. On by default and harmless with nothing connected: an
    # integration is something a person adds, so the switch that matters is
    # the empty list, not this. It exists to turn off a capability that is
    # not wanted on a given machine, which is what every flag here is for.
    integrations: bool = True
    # Phase 15. On by default and, like integrations, harmless with nothing
    # added: knowledge is documents a person brought, so the empty list is the
    # real switch. Off, `Container.knowledge` is None and the runtime assembles
    # the Phase 14 context - the same arrangement memory has.
    knowledge: bool = True
