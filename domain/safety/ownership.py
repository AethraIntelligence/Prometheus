"""One runtime owns one data directory.

SQLite tolerates two processes; the platform does not. Two engines on one store
would each resume the other's objectives, each fire the same schedule's
follow-up work, each park approvals the other cannot release - and each would
be right by its own reading of the rows. So a process that *does work* against
a data directory first becomes its owner, and a second one is told who already
is rather than quietly becoming another.

**Absence is proved by the operating system, not guessed from a file.** The
record says who owns the directory; the lock under it is held by the kernel for
as long as that process lives and released when it dies, however it dies. A
record whose lock can be taken is stale by construction - no pid check, no
timeout, no clock that a sleeping laptop can skew.

Reading needs no ownership. Listing tasks, reading the audit, or opening the
window's history while an engine runs is safe; running work is not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from domain.errors import WorkControlError


@dataclass(frozen=True, slots=True)
class RuntimeOwner:
    """Who holds a data directory: enough to find it and to say so to a person."""

    pid: int
    purpose: str
    host: str = ""
    url: str = ""
    version: str = ""
    started_at: datetime | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "pid": self.pid,
                "purpose": self.purpose,
                "host": self.host,
                "url": self.url,
                "version": self.version,
                "started_at": (self.started_at or datetime.now(UTC)).isoformat(),
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str) -> RuntimeOwner | None:
        try:
            raw: Any = json.loads(text)
            started = raw.get("started_at")
            return cls(
                pid=int(raw["pid"]),
                purpose=str(raw.get("purpose", "")),
                host=str(raw.get("host", "")),
                url=str(raw.get("url", "")),
                version=str(raw.get("version", "")),
                started_at=datetime.fromisoformat(started) if started else None,
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            return None

    def describe(self) -> str:
        where = f" at {self.url}" if self.url else ""
        return f"{self.purpose} (pid {self.pid}{where})"


class DataDirectoryOwnedError(WorkControlError):
    """Another live process already owns this data directory."""

    def __init__(self, owner: RuntimeOwner | None) -> None:
        who = owner.describe() if owner else "another Prometheus process"
        super().__init__(
            f"This data directory is already in use by {who}. Use that one, or stop "
            "it first; a second engine on the same data would run the same work twice."
        )
        self.owner = owner


class RuntimeLock(Protocol):
    """Implemented once per operating system family, in infrastructure."""

    def acquire(self, owner: RuntimeOwner) -> None:
        """Become the owner, or raise `DataDirectoryOwnedError` naming who is."""
        ...

    def release(self) -> None: ...

    def current(self) -> RuntimeOwner | None:
        """Who owns the directory now, if anybody live does."""
        ...
