"""What happens to a memory when a newer one disagrees with it.

A pure function, like the policy engine: the same pair and the same relation
decide the same way, so a test can state the rule and an audit can trust it.
Deciding *whether* two memories disagree is a judgement and belongs to a model
(`application/memory/revision.py`); deciding what follows from it is not.

The rule is short. A newer memory supersedes an older one it replaces, or one it
contradicts when it rests on at least as much - a person restating a preference
is a change of mind, and a record of what ran outranks a model's summary of
what ran. Where the newer one rests on *less*, nothing is settled: a summary
does not get to overrule what the person said, and a person's statement is not
erased by a guess. Both are then CONTESTED, both are still recalled, and both
say so. The older memory is never deleted here - superseding keeps it for the
retention period (`ranking.SUPERSEDED_RETENTION`), so a correction can be traced.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from enum import StrEnum

from domain.memory.models import MemoryBasis, MemoryItem, MemoryStatus

#: How much each basis is worth when two memories disagree.
BASIS_RANK: dict[MemoryBasis, int] = {
    MemoryBasis.STATED: 3,
    MemoryBasis.OBSERVED: 2,
    MemoryBasis.REPORTED: 1,
    MemoryBasis.INFERRED: 0,
}


class Relation(StrEnum):
    """How a new memory stands to an existing one."""

    UNRELATED = "UNRELATED"
    #: Says the same thing, updated - a restated preference, a moved file.
    REPLACES = "REPLACES"
    #: Cannot be true at the same time, and neither says it is the update.
    CONTRADICTS = "CONTRADICTS"


def revise(
    new: MemoryItem,
    old: MemoryItem,
    relation: Relation,
    *,
    now: datetime | None = None,
) -> tuple[MemoryItem, MemoryItem]:
    """Both memories as they should be stored after the relation is known."""
    if relation is Relation.UNRELATED or new.id == old.id:
        return new, old
    moment = now or datetime.now(UTC)
    outranked = BASIS_RANK[new.basis] < BASIS_RANK[old.basis]
    if not outranked:
        return new, replace(
            old,
            status=MemoryStatus.SUPERSEDED,
            superseded_by=new.id,
            revised_at=moment,
        )
    # Weaker evidence against stronger: shown side by side, decided by nobody
    # here. A person resolves it by correcting or forgetting one of them.
    return (
        replace(
            new,
            status=MemoryStatus.CONTESTED,
            revised_at=moment,
            contradicts=_with(new.contradicts, old),
        ),
        replace(
            old,
            status=MemoryStatus.CONTESTED,
            revised_at=moment,
            contradicts=_with(old.contradicts, new),
        ),
    )


def _with(existing: tuple, other: MemoryItem) -> tuple:
    return existing if other.id in existing else (*existing, other.id)
