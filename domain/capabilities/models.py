"""Capabilities are the vocabulary of routing and permissions.

An employee declares what it needs; a model, a tool or another employee declares
what it offers. Nothing in the domain names a vendor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Capability(StrEnum):
    """A discrete ability a model, tool or employee can offer."""

    TEXT_REASONING = "TEXT_REASONING"
    LONG_CONTEXT = "LONG_CONTEXT"
    TOOL_CALLING = "TOOL_CALLING"
    STRUCTURED_OUTPUT = "STRUCTURED_OUTPUT"
    VISION = "VISION"
    CODE = "CODE"
    WEB_BROWSING = "WEB_BROWSING"
    COMPUTER_USE = "COMPUTER_USE"
    FILE_ACCESS = "FILE_ACCESS"
    #: Turns text into a vector. Declared like any other capability, so
    #: retrieval asks the router for a model rather than naming one (ADR 0003).
    EMBEDDING = "EMBEDDING"
    EMAIL = "EMAIL"


@dataclass(frozen=True, slots=True)
class CapabilityRequirement:
    """What a piece of work needs in order to be routed somewhere."""

    required: frozenset[Capability] = field(default_factory=frozenset)
    preferred: frozenset[Capability] = field(default_factory=frozenset)
    min_context_tokens: int | None = None
    #: The floor under how good the model has to be, on the catalog's own
    #: ranking. A capability is a yes or a no, and some work needs neither a
    #: new capability nor a hint but a better model: judging whether an
    #: objective is done is the case this came from, where the cheapest entry
    #: in the catalog answered by copying the example out of its own prompt.
    #:
    #: It is a requirement rather than a hint on purpose. Hints only rank what
    #: survives the filter, and the configured default beats them - so a
    #: default pointing at the cheapest entry would keep winning the work it
    #: had just been shown to be unfit for. Stated here, it filters first, and
    #: the router says in its reason that the default could not do the work.
    min_quality: float = 0.0
    #: Only models whose prompts never leave this machine. A requirement, like
    #: the quality floor, so a configured default that is remote is filtered out
    #: and the trace says so rather than the prompt being sent anyway.
    local_only: bool = False

    def is_satisfied_by(self, offered: frozenset[Capability]) -> bool:
        return self.required <= offered

    def score(self, offered: frozenset[Capability]) -> int:
        """How well an offer matches, used to rank candidates that all qualify."""
        if not self.is_satisfied_by(offered):
            return -1
        return len(self.preferred & offered)
