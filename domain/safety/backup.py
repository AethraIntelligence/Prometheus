"""What a backup of one installation is, and what a restore may trust in one.

A backup is a local archive a person asked for. It is not synchronisation: it is
made once, on purpose, and nothing reads it until somebody restores it.

**The manifest is the contract.** Every file in the archive is listed with its
size and SHA-256, the schema revision of the database inside it, the version
that wrote it and a format version. A restore reads nothing the manifest does
not list, trusts nothing whose digest does not match, and refuses a format
version it does not know - the same rule as the stop record and the schema: the
two ways of being wrong about a backup are not symmetrical.

**Paths are data from a file somebody may have edited.** An entry path is
relative, forward-slashed, has no `..`, no drive, no empty or dotted segment -
checked here, once, before anything is extracted, so an archive cannot write
outside the place it is being restored into.

**Secrets are never in a backup by default.** The database inside holds
credentials as ciphertext and the key that opens them lives in the operating
system's vault, which a backup does not read. Moving to another computer needs
the key too, so it can be added - sealed with a passphrase the person chooses,
and marked on the manifest so a restore knows to ask for it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from domain.errors import PrometheusError

FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
SECRETS_NAME = "secrets.sealed"
#: A passphrase shorter than this is refused rather than warned about: the file
#: it protects is meant to be carried to another machine.
MIN_PASSPHRASE_LENGTH = 12

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_DRIVE = re.compile(r"^[A-Za-z]:")


class BackupError(PrometheusError):
    """A backup could not be made, or cannot be trusted. Nothing was overwritten."""


class EntryKind(StrEnum):
    #: A consistent snapshot of the database.
    DATABASE = "DATABASE"
    #: A file from the data directory, restored with it as one unit.
    DATA = "DATA"
    #: A file the work produced, restored beside what is there without overwriting.
    ARTIFACT = "ARTIFACT"
    #: A declaration a person added outside the shipped ones.
    DECLARATION = "DECLARATION"
    #: The master key, sealed with the passphrase. Present only when asked for.
    SECRETS = "SECRETS"


@dataclass(frozen=True, slots=True)
class BackupEntry:
    path: str
    kind: EntryKind
    size: int
    sha256: str
    #: For ARTIFACT and DECLARATION entries: which configured root the file
    #: belongs to, so it is put back under the root the *restoring* machine has.
    root: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind.value,
            "size": self.size,
            "sha256": self.sha256,
            "root": self.root,
        }


@dataclass(frozen=True, slots=True)
class BackupManifest:
    created_at: datetime
    app_version: str
    schema_revision: str
    entries: tuple[BackupEntry, ...]
    #: Where each root was on the machine that made it. Shown, never used as a
    #: destination: a restore writes under its own configured roots.
    roots: dict[str, str] = field(default_factory=dict)
    format_version: int = FORMAT_VERSION

    @property
    def includes_secrets(self) -> bool:
        return any(entry.kind is EntryKind.SECRETS for entry in self.entries)

    @property
    def total_bytes(self) -> int:
        return sum(entry.size for entry in self.entries)

    def entry(self, path: str) -> BackupEntry | None:
        return next((item for item in self.entries if item.path == path), None)

    def to_json(self) -> str:
        return json.dumps(
            {
                "format_version": self.format_version,
                "created_at": self.created_at.isoformat(),
                "app_version": self.app_version,
                "schema_revision": self.schema_revision,
                "roots": dict(sorted(self.roots.items())),
                "entries": [entry.to_dict() for entry in self.entries],
            },
            indent=2,
            sort_keys=True,
        )


def safe_entry_path(path: object) -> str:
    """The path if an archive may use it, or `BackupError` saying why not."""
    if not isinstance(path, str) or not path:
        raise BackupError("A backup entry has no path.")
    if "\\" in path or "\x00" in path or path.startswith("/") or _DRIVE.match(path):
        raise BackupError(f"A backup entry has an unsafe path: {path!r}.")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise BackupError(f"A backup entry has an unsafe path: {path!r}.")
    if path in {MANIFEST_NAME}:
        raise BackupError("A backup entry may not be named like the manifest.")
    return path


def parse_manifest(text: str) -> BackupManifest:
    """Read a manifest strictly. Anything unexpected is a refusal, not a default."""
    try:
        raw = json.loads(text)
    except ValueError as error:
        raise BackupError("The backup's manifest is not readable.") from error
    if not isinstance(raw, dict):
        raise BackupError("The backup's manifest is not readable.")
    version = raw.get("format_version")
    if version != FORMAT_VERSION:
        raise BackupError(
            f"The backup is in format {version!r}, which this version of Prometheus cannot "
            f"read (it reads format {FORMAT_VERSION}). Nothing was changed."
        )
    try:
        created = datetime.fromisoformat(str(raw["created_at"]))
        revision = str(raw["schema_revision"])
        app_version = str(raw.get("app_version", ""))
        roots_raw = raw.get("roots", {})
        entries_raw = raw["entries"]
    except (KeyError, ValueError, TypeError) as error:
        raise BackupError("The backup's manifest is incomplete.") from error
    if not isinstance(entries_raw, list) or not isinstance(roots_raw, dict):
        raise BackupError("The backup's manifest is incomplete.")

    entries: list[BackupEntry] = []
    seen: set[str] = set()
    for item in entries_raw:
        if not isinstance(item, dict):
            raise BackupError("A backup entry is not readable.")
        path = safe_entry_path(item.get("path"))
        if path in seen:
            raise BackupError(f"The backup lists {path!r} twice.")
        seen.add(path)
        try:
            kind = EntryKind(str(item.get("kind")))
        except ValueError as error:
            raise BackupError(f"The backup entry {path!r} is of an unknown kind.") from error
        size, digest, root = item.get("size"), item.get("sha256"), item.get("root", "")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise BackupError(f"The backup entry {path!r} has an invalid size.")
        if not isinstance(digest, str) or not _DIGEST.match(digest):
            raise BackupError(f"The backup entry {path!r} has an invalid checksum.")
        if not isinstance(root, str):
            raise BackupError(f"The backup entry {path!r} has an invalid root.")
        entries.append(BackupEntry(path, kind, size, digest, root))

    databases = [entry for entry in entries if entry.kind is EntryKind.DATABASE]
    if len(databases) != 1:
        raise BackupError("A backup must contain exactly one database.")
    if sum(entry.kind is EntryKind.SECRETS for entry in entries) > 1:
        raise BackupError("A backup may contain at most one sealed secrets file.")
    return BackupManifest(
        created_at=created,
        app_version=app_version,
        schema_revision=revision,
        entries=tuple(entries),
        roots={str(key): str(value) for key, value in roots_raw.items()},
    )


class Backups(Protocol):
    """Making and checking backups, as an interface asks for them.

    Restoring is not here on purpose: it replaces the store the running process
    has open, so it happens between one process and the next, never inside one.
    """

    def create(
        self, destination: str | None = None, *, passphrase: str | None = None
    ) -> dict[str, Any]: ...

    def verify(self, path: str, *, passphrase: str | None = None) -> dict[str, Any]: ...


def summary(manifest: BackupManifest) -> dict[str, Any]:
    """What a person is shown about a backup before trusting it."""
    counts: dict[str, int] = {}
    for entry in manifest.entries:
        counts[entry.kind.value] = counts.get(entry.kind.value, 0) + 1
    return {
        "created_at": manifest.created_at.isoformat(),
        "app_version": manifest.app_version,
        "schema_revision": manifest.schema_revision,
        "format_version": manifest.format_version,
        "includes_secrets": manifest.includes_secrets,
        "bytes": manifest.total_bytes,
        "entries": counts,
    }


def check_passphrase(passphrase: str) -> None:
    if len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise BackupError(
            f"A passphrase for exported secrets must be at least {MIN_PASSPHRASE_LENGTH} "
            "characters. Anyone holding the file and the passphrase can read every stored "
            "credential."
        )
