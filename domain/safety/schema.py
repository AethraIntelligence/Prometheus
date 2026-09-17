"""Whether this version of the platform may open the store it was pointed at.

The decision is made from two facts and nothing else: the revision the store
says it is at, and the ordered history of revisions this code knows. It never
opens the store for writing to find out.

* **No revision** and no tables means a fresh installation: create it.
* **A known revision behind the head** means an upgrade, which is only started
  after a preflight and a backup (`infrastructure.runtime.migration`).
* **A revision this code has never heard of** means the store was written by a
  newer version. It is refused, and nothing is written - an older program
  "repairing" a newer schema is how a downgrade destroys data it cannot see.
* **A revision older than the oldest supported one** is refused with the version
  that can still upgrade it, rather than attempted through a chain nobody tests.
* **Tables with no revision at all** is a store this platform did not create, or
  one whose history was lost. Refused: guessing a starting point is guessing
  which migrations to run twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SchemaVerdict(StrEnum):
    FRESH = "FRESH"
    CURRENT = "CURRENT"
    UPGRADE = "UPGRADE"
    NEWER = "NEWER"
    UNSUPPORTED = "UNSUPPORTED"
    UNVERSIONED = "UNVERSIONED"

    @property
    def may_open(self) -> bool:
        return self in {SchemaVerdict.FRESH, SchemaVerdict.CURRENT, SchemaVerdict.UPGRADE}


@dataclass(frozen=True, slots=True)
class SchemaDecision:
    verdict: SchemaVerdict
    current: str | None
    head: str
    pending: tuple[str, ...] = ()
    message: str = ""


def judge(
    current: str | None,
    history: tuple[str, ...],
    *,
    has_tables: bool,
    oldest_supported: str | None = None,
) -> SchemaDecision:
    """`history` is ordered from the first revision to the head."""
    if not history:
        raise ValueError("a platform with no migrations cannot judge a schema")
    head = history[-1]
    if current is None:
        if has_tables:
            return SchemaDecision(
                SchemaVerdict.UNVERSIONED,
                None,
                head,
                message=(
                    "The database has tables but no schema version, so it was not created "
                    "by Prometheus or its history was lost. It was not changed. Restore it "
                    "from a backup, or point Prometheus at an empty data directory."
                ),
            )
        return SchemaDecision(SchemaVerdict.FRESH, None, head, pending=history)
    if current not in history:
        return SchemaDecision(
            SchemaVerdict.NEWER,
            current,
            head,
            message=(
                f"The database is at schema {current}, which this version of Prometheus "
                f"does not know (it knows up to {head}). It was written by a newer version "
                "and was not changed. Install that version again, or restore a backup "
                "made with this one."
            ),
        )
    position = history.index(current)
    too_old = (
        oldest_supported is not None
        and oldest_supported in history
        and position < history.index(oldest_supported)
    )
    if too_old:
        return SchemaDecision(
            SchemaVerdict.UNSUPPORTED,
            current,
            head,
            message=(
                f"The database is at schema {current}, older than the oldest this "
                f"version upgrades from ({oldest_supported}). It was not changed. "
                "Upgrade through an intermediate release first."
            ),
        )
    pending = history[position + 1 :]
    if not pending:
        return SchemaDecision(SchemaVerdict.CURRENT, current, head)
    return SchemaDecision(SchemaVerdict.UPGRADE, current, head, pending=pending)
