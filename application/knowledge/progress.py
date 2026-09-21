"""What is being indexed right now, for whoever asks between two polls.

State, not a stream. A document is indexed inside the request that asked for it,
so the interface waiting on that request cannot also be reading an event stream
it opened first - and a person who opened the screen halfway through would have
missed every event anyway. So the monitor keeps the latest word about each
operation and answers "what is happening" at any moment, which is the question
the screen actually asks.

Two rules.

**A finished operation is kept for a moment, then dropped.** The poll that
learns a document failed is usually the one after the last one that saw it
working; dropping it the instant it ended would make a failure invisible on the
one screen built to show it. `RETENTION` is how long the answer stays.

**Watching never fails the work.** `report` swallows nothing because it does
nothing that can raise - it replaces a value in a dict - and the caller treats
it as it treats progress: the work is what matters, the watcher is not.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from domain.knowledge.models import IndexingProgress

#: How long after it ended an operation is still reported. Long enough for a
#: screen polling at about a second to see the last word, short enough that a
#: person is not told about something they finished a minute ago.
RETENTION = timedelta(seconds=20)

#: A ceiling on what is remembered, so a machine adding files all day cannot
#: grow this without bound if something ends without ever reporting a finish.
MAX_TRACKED = 200


class IndexingMonitor:
    """The latest word about every document currently being read or embedded."""

    def __init__(self, *, retention: timedelta = RETENTION) -> None:
        self._retention = retention
        self._latest: dict[str, IndexingProgress] = {}

    def report(self, progress: IndexingProgress) -> None:
        self._latest[progress.key] = progress
        self._forget_old()

    def active(self, *, now: datetime | None = None) -> list[IndexingProgress]:
        """What to show: everything running, plus what has just stopped."""
        self._forget_old(now=now)
        return sorted(self._latest.values(), key=lambda one: one.at)

    def _forget_old(self, *, now: datetime | None = None) -> None:
        moment = now or datetime.now(UTC)
        self._latest = {
            key: one
            for key, one in self._latest.items()
            if not one.finished or moment - one.at < self._retention
        }
        if len(self._latest) > MAX_TRACKED:
            keep = sorted(self._latest.values(), key=lambda one: one.at)[-MAX_TRACKED:]
            self._latest = {one.key: one for one in keep}
