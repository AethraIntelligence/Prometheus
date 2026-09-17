"""What is worth recalling, and what has stopped being worth it.

Two policies live here, and both are the domain's rather than a backend's:
recency and importance decide the order results come back in, and a kind of
memory decides how long an item is kept at all. A backend that ranked by its
own index alone would return whatever matched best, which for memory is the
wrong question - the best match to "the report" is every report ever written.

The formula is deliberately plain: score = relevance * importance * decay(age),
with a half-life per kind. Working notes are worth little by tomorrow; something
learned about how the user wants things done is worth as much next month. A
model is not asked to rank, because ranking is called on every recall and a call
per recall would make remembering more expensive than working.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from domain.memory.models import MemoryItem, MemoryKind, MemoryStatus

#: How long it takes an item of each kind to be worth half of what it was.
#: WORKING is measured in hours because it describes a task that is still
#: running; SEMANTIC barely decays because a preference does not expire on its
#: own - the user changing their mind is what supersedes it.
HALF_LIFE_DAYS: dict[MemoryKind, float] = {
    MemoryKind.WORKING: 0.25,
    MemoryKind.EPISODIC: 14.0,
    MemoryKind.PROCEDURAL: 90.0,
    MemoryKind.SEMANTIC: 365.0,
}

#: When an item of each kind stops being readable at all. `None` means it is
#: kept until something replaces it. A time to live is not the same as decay:
#: decay pushes an item down the list, a TTL takes it out of the store, and
#: working notes about a task nobody is running any more are pure noise.
TIME_TO_LIVE: dict[MemoryKind, timedelta | None] = {
    MemoryKind.WORKING: timedelta(hours=12),
    MemoryKind.EPISODIC: timedelta(days=180),
    MemoryKind.PROCEDURAL: None,
    MemoryKind.SEMANTIC: None,
}


def expires_at(kind: MemoryKind, created_at: datetime | None = None) -> datetime | None:
    """When an item of this kind should stop being recalled."""
    ttl = TIME_TO_LIVE.get(kind)
    if ttl is None:
        return None
    return (created_at or datetime.now(UTC)) + ttl


def decay(kind: MemoryKind, age_days: float) -> float:
    """How much of its weight an item of this kind keeps after `age_days`."""
    half_life = HALF_LIFE_DAYS.get(kind, 14.0)
    if half_life <= 0 or age_days <= 0:
        return 1.0
    return float(0.5 ** (age_days / half_life))


def score(item: MemoryItem, *, relevance: float = 1.0, now: datetime | None = None) -> float:
    """How strongly this item deserves a place in the context being assembled.

    `relevance` is what the backend's own search said - a rank from a text index
    here, a distance from a vector store later. It is a multiplier rather than
    the answer, so swapping the search does not swap the policy.
    """
    moment = now or datetime.now(UTC)
    created = item.created_at if item.created_at.tzinfo else item.created_at.replace(tzinfo=UTC)
    age_days = max((moment - created).total_seconds(), 0.0) / 86400.0
    return (
        relevance
        * max(item.importance, 0.0)
        * decay(item.kind, age_days)
        * trust(item)
    )


def trust(item: MemoryItem) -> float:
    """How much of its weight an item keeps for what it rests on.

    Half the weight is unconditional, so an assumption is ranked below a fact
    rather than out of sight: a model's summary is still the only record of
    what it summarised. A contested item is halved again - it is shown, marked,
    and not preferred over the one it disagrees with.
    """
    confidence = min(max(item.confidence, 0.0), 1.0)
    weight = 0.5 + 0.5 * confidence
    if item.status is MemoryStatus.CONTESTED:
        weight *= 0.5
    return weight


#: How long a superseded memory is kept after it was replaced. Long enough to
#: answer "why did last week's run do that"; not so long that every correction
#: doubles the store for good.
SUPERSEDED_RETENTION = timedelta(days=30)


def retention_over(item: MemoryItem, now: datetime | None = None) -> bool:
    """Whether a superseded item has been kept as long as it is worth keeping."""
    if item.status is not MemoryStatus.SUPERSEDED:
        return False
    moment = now or datetime.now(UTC)
    since = item.revised_at or item.created_at
    since = since if since.tzinfo else since.replace(tzinfo=UTC)
    return since + SUPERSEDED_RETENTION <= moment


#: How far below the best result an item may score and still be worth reading.
#: Text search is generous by design - a query is matched word by word, so a
#: memory sharing one common word with the goal is a hit - and without a floor
#: the weak hits fill the context budget the good ones needed.
CUTOFF_RATIO = 0.35


def best_of(
    scored: list[tuple[float, MemoryItem]], limit: int, *, cutoff: float = 0.0
) -> list[MemoryItem]:
    """The results worth returning, strongest first.

    Applied by every backend, so "what comes back" is one decision made in one
    place rather than a property of whichever index answered.

    The cutoff is for searches only. Listing a scope is a different question -
    "what is in here" - and answering it by dropping everything that scores
    poorly against nothing in particular would hide the old and the unimportant
    from the one command whose job is to show them.
    """
    ranked = sorted(scored, key=lambda pair: (-pair[0], -pair[1].created_at.timestamp()))
    if not ranked:
        return []
    floor = ranked[0][0] * cutoff
    return [item for weight, item in ranked[: max(limit, 0)] if weight >= floor]


def is_live(item: MemoryItem, now: datetime | None = None) -> bool:
    """Whether the item is still within its time to live."""
    if item.expires_at is None:
        return True
    moment = now or datetime.now(UTC)
    expiry = (
        item.expires_at if item.expires_at.tzinfo else item.expires_at.replace(tzinfo=UTC)
    )
    return expiry > moment
