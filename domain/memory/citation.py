"""How a memory is shown to a model, and why it was chosen at all.

Two questions a recollection has to answer before it is worth its tokens: *what
does this rest on* and *why is it here*. The first is rendered into the line a
prompt shows - basis, source, date, confidence and scope - so a model reads a
guess as a guess. The second is kept as a record for the person
(`MemoryUse`), because "the platform used something it remembered" is not an
explanation anyone can check.

Both are derived here, deterministically, from the item and the query. A model
is not asked why it was given something: it was not the one who chose it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from domain.memory.models import (
    MemoryBasis,
    MemoryItem,
    MemoryQuery,
    MemoryScope,
    MemoryStatus,
    SourceKind,
)
from domain.memory.ranking import decay, trust

_WORD = re.compile(r"\w+", re.UNICODE)

#: Words too common to count as the reason something was recalled.
_COMMON = frozenset(
    {
        "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
        "is", "are", "was", "were", "be", "it", "this", "that", "what", "where",
        "when", "how", "do", "did", "does", "i", "you", "me", "my", "we", "our",
        "at", "by", "from", "as",
    }
)  # fmt: skip

BASIS_LABEL: dict[MemoryBasis, str] = {
    MemoryBasis.STATED: "stated by the user",
    MemoryBasis.OBSERVED: "recorded by the platform",
    MemoryBasis.REPORTED: "reported by an employee, not independently checked",
    MemoryBasis.INFERRED: "an assumption inferred by a model",
}

SCOPE_LABEL: dict[MemoryScope, str] = {
    MemoryScope.WORKSPACE: "this workspace",
    MemoryScope.PLAN: "this plan",
    MemoryScope.EMPLOYEE_PRIVATE: "this employee's own notes",
    MemoryScope.USER: "the user, in every workspace",
    MemoryScope.SYSTEM: "this installation",
}

SOURCE_LABEL: dict[SourceKind, str] = {
    SourceKind.PERSON: "a note the user added",
    SourceKind.OBJECTIVE: "a request",
    SourceKind.TASK: "a task",
    SourceKind.CONSOLIDATION: "a summary of earlier memories",
    SourceKind.UNKNOWN: "an unrecorded source",
}


def cite(item: MemoryItem) -> str:
    """The provenance of one memory, in a line a model can read."""
    source = SOURCE_LABEL[item.provenance.kind]
    if item.provenance.label:
        source = f'{source} "{_short(item.provenance.label, 80)}"'
    parts = [
        BASIS_LABEL[item.basis],
        f"from {source}",
        _aware(item.created_at).date().isoformat(),
        f"confidence {item.confidence:.2f}",
        SCOPE_LABEL[item.scope],
    ]
    if item.status is MemoryStatus.CONTESTED:
        parts.append("disputed by another memory")
    return "[" + "; ".join(parts) + "]"


def recollection(item: MemoryItem) -> str:
    """The memory as a prompt shows it: the claim, then what it rests on."""
    return f"{item.content.strip()} {cite(item)}"


@dataclass(frozen=True, slots=True)
class Explanation:
    """Why one memory was put in front of a run."""

    matched: tuple[str, ...]
    scope: str
    basis: str
    age_days: float
    weight: float

    @property
    def reason(self) -> str:
        if self.matched:
            found = "mentions " + ", ".join(f'"{word}"' for word in self.matched[:6])
        else:
            found = "is standing knowledge recalled without a search match"
        return (
            f"It {found}; it belongs to {self.scope}; it is {self.basis}; "
            f"written {self.age_days:.0f} day(s) earlier."
        )


def explain(item: MemoryItem, query: MemoryQuery, now: datetime | None = None) -> Explanation:
    """The reasons this item answered this query, stated from the record."""
    moment = now or query.as_of or datetime.now(UTC)
    asked = {word.lower() for word in _WORD.findall(query.text)} - _COMMON
    held = {word.lower() for word in _WORD.findall(item.content)}
    age = max((moment - _aware(item.created_at)).total_seconds(), 0.0) / 86400.0
    return Explanation(
        matched=tuple(sorted(word for word in asked & held if len(word) > 1)),
        scope=SCOPE_LABEL[item.scope],
        basis=BASIS_LABEL[item.basis],
        age_days=age,
        weight=round(item.importance * decay(item.kind, age) * trust(item), 4),
    )


def _short(text: str, limit: int) -> str:
    clean = " ".join(text.split())
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
