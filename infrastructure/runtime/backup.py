"""Making, checking and restoring a backup of one installation.

`domain.safety.backup` says what may be trusted in an archive; this is the
filesystem half. Four rules shape it.

**What is copied is an allow-list.** A consistent snapshot of the database
(SQLite's online backup API), the settings a person saved, which workspace is
active, the later workspaces' files, the first workspace's files and any
declarations a person keeps outside the shipped ones. Never the master key, the
plaintext credential file, the stop record, a lock, a migration marker or an
earlier backup - a cache or a transient file is not data, and a secret is only
ever added sealed, on request.

**A backup is written beside its destination and moved into place.** An archive
that exists is complete; a destination that already exists is refused rather
than overwritten.

**A restore checks everything before it changes anything.** The manifest, every
listed file's size and digest, that nothing unlisted is in the archive, that the
database inside passes an integrity check and is at a schema this version can
open, that there is room, and - when secrets are included - that the passphrase
opens them. Only then is anything written, and it is written to a staging
directory beside the data directory.

**The switch is two renames, and the old installation is kept.** The data
directory is moved aside and the staged one moved into its place; the previous
one stays beside it, named in the report, until a person deletes it. Files the
work produced are put back only where nothing is: a file somebody changed since
the backup is theirs, and a restore that overwrites it is a second data loss.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from domain.safety.backup import (
    MANIFEST_NAME,
    SECRETS_NAME,
    BackupEntry,
    BackupError,
    BackupManifest,
    EntryKind,
    check_passphrase,
    parse_manifest,
)
from domain.safety.schema import judge
from infrastructure.observability.logging import get_logger
from infrastructure.runtime import migration

log = get_logger(__name__)

CHUNK = 1024 * 1024
DATA_PREFIX = "data/"
DATABASE_NAME = "prometheus.db"
#: Files from the data directory that are data. Everything else in it is not.
DATA_FILES = ("settings.json", "ACTIVE_WORKSPACE")
DATA_TREES = ("workspaces",)
#: Carried from the installation being replaced into the restored one, because
#: they belong to this machine rather than to the backup.
MACHINE_FILES = ("backups",)
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1
SECRETS_AAD = b"prometheus-backup-secrets-v1"
STAGED_COMPLETE = "RESTORE_STAGED"


@dataclass(frozen=True, slots=True)
class Installation:
    """Where one installation keeps what a backup covers."""

    data_dir: Path
    database: Path
    #: Roots outside the data directory whose files the work produced, by name.
    file_roots: dict[str, Path] = field(default_factory=dict)
    #: Declaration directories a person configured, by name.
    declaration_roots: dict[str, Path] = field(default_factory=dict)
    app_version: str = ""


@dataclass(frozen=True, slots=True)
class RestoreReport:
    manifest: BackupManifest
    previous: Path | None
    restored_files: int = 0
    unchanged_files: int = 0
    #: Files that exist here with different contents; left as they are.
    conflicts: tuple[str, ...] = ()
    secrets_restored: bool = False
    #: Written to the log and shown: what a person should do next, if anything.
    notes: tuple[str, ...] = ()


# --- Making one -------------------------------------------------------------------------


def create_backup(
    installation: Installation,
    destination: Path,
    *,
    passphrase: str | None = None,
    master_key: Callable[[], bytes] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> BackupManifest:
    if destination.exists():
        raise BackupError(f"{destination} already exists. Choose a new name; nothing was replaced.")
    if passphrase is not None:
        check_passphrase(passphrase)
        if master_key is None:
            raise BackupError("Secrets were asked for, but this installation has no key to add.")
    if not installation.database.exists():
        raise BackupError(f"There is no database at {installation.database} to back up.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f".{destination.name}.partial")
    partial.unlink(missing_ok=True)
    entries: list[BackupEntry] = []
    try:
        with tempfile.TemporaryDirectory(prefix="prometheus-backup-") as scratch:
            snapshot = Path(scratch) / DATABASE_NAME
            migration.snapshot(installation.database, snapshot)
            revision, _ = migration.inspect_sqlite(snapshot)
            if revision is None:
                raise BackupError("The database has no schema version, so it was not backed up.")

            with zipfile.ZipFile(partial, "x", compression=zipfile.ZIP_DEFLATED) as archive:
                entries.append(
                    _add(archive, snapshot, DATA_PREFIX + DATABASE_NAME, EntryKind.DATABASE)
                )
                for name in DATA_FILES:
                    source = installation.data_dir / name
                    if source.is_file() and not source.is_symlink():
                        entries.append(_add(archive, source, DATA_PREFIX + name, EntryKind.DATA))
                for tree in DATA_TREES:
                    for source, relative in _files_under(installation.data_dir / tree):
                        entries.append(
                            _add(archive, source, f"{DATA_PREFIX}{tree}/{relative}", EntryKind.DATA)
                        )
                for kind, prefix, roots in (
                    (EntryKind.ARTIFACT, "files", installation.file_roots),
                    (EntryKind.DECLARATION, "declarations", installation.declaration_roots),
                ):
                    for key, root in sorted(roots.items()):
                        for source, relative in _files_under(root):
                            entries.append(
                                _add(archive, source, f"{prefix}/{key}/{relative}", kind, root=key)
                            )
                if passphrase is not None and master_key is not None:
                    sealed = seal_secrets(master_key(), passphrase)
                    entries.append(_add_bytes(archive, sealed, SECRETS_NAME, EntryKind.SECRETS))

                manifest = BackupManifest(
                    created_at=clock(),
                    app_version=installation.app_version,
                    schema_revision=revision,
                    entries=tuple(entries),
                    roots={
                        key: str(path)
                        for key, path in {
                            **installation.file_roots,
                            **installation.declaration_roots,
                        }.items()
                    },
                )
                archive.writestr(MANIFEST_NAME, manifest.to_json())
        with partial.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(partial, destination)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    log.info(
        "backup.created",
        path=str(destination),
        entries=len(entries),
        bytes=manifest.total_bytes,
        secrets=manifest.includes_secrets,
    )
    return manifest


def _files_under(root: Path) -> Iterator[tuple[Path, str]]:
    """Regular files below a root, relative and forward-slashed. Links are not followed."""
    if not root.is_dir():
        return
    for directory, subdirectories, files in os.walk(root, followlinks=False):
        subdirectories.sort()
        for name in sorted(files):
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            yield path, path.relative_to(root).as_posix()


def _add(
    archive: zipfile.ZipFile, source: Path, name: str, kind: EntryKind, *, root: str = ""
) -> BackupEntry:
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as reader, archive.open(name, "w", force_zip64=True) as writer:
        while chunk := reader.read(CHUNK):
            digest.update(chunk)
            size += len(chunk)
            writer.write(chunk)
    return BackupEntry(name, kind, size, digest.hexdigest(), root)


def _add_bytes(archive: zipfile.ZipFile, data: bytes, name: str, kind: EntryKind) -> BackupEntry:
    archive.writestr(name, data)
    return BackupEntry(name, kind, len(data), hashlib.sha256(data).hexdigest())


# --- Secrets, sealed --------------------------------------------------------------------


def _derive(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P).derive(
        passphrase.encode("utf-8")
    )


def seal_secrets(master_key: bytes, passphrase: str) -> bytes:
    salt, nonce = os.urandom(16), os.urandom(12)
    plaintext = json.dumps({"master_key": base64.b64encode(master_key).decode("ascii")})
    ciphertext = AESGCM(_derive(passphrase, salt)).encrypt(
        nonce, plaintext.encode("utf-8"), SECRETS_AAD
    )
    return json.dumps(
        {
            "kdf": "scrypt",
            "n": SCRYPT_N,
            "r": SCRYPT_R,
            "p": SCRYPT_P,
            "salt": base64.b64encode(salt).decode("ascii"),
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        },
        sort_keys=True,
    ).encode("utf-8")


def open_secrets(sealed: bytes, passphrase: str) -> bytes:
    try:
        raw = json.loads(sealed)
        if raw.get("kdf") != "scrypt" or (raw["n"], raw["r"], raw["p"]) != (
            SCRYPT_N,
            SCRYPT_R,
            SCRYPT_P,
        ):
            raise BackupError("The sealed secrets use parameters this version does not accept.")
        salt = base64.b64decode(raw["salt"])
        nonce = base64.b64decode(raw["nonce"])
        ciphertext = base64.b64decode(raw["ciphertext"])
    except (ValueError, KeyError, TypeError) as error:
        raise BackupError("The sealed secrets in this backup are damaged.") from error
    try:
        plaintext = AESGCM(_derive(passphrase, salt)).decrypt(nonce, ciphertext, SECRETS_AAD)
    except InvalidTag as error:
        raise BackupError(
            "The passphrase does not open the secrets in this backup. Nothing was changed."
        ) from error
    key = base64.b64decode(json.loads(plaintext)["master_key"])
    if len(key) != 32:
        raise BackupError("The sealed master key in this backup is the wrong length.")
    return key


# --- Checking one -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VerifiedBackup:
    manifest: BackupManifest
    master_key: bytes | None = None


def verify_backup(
    archive_path: Path,
    *,
    passphrase: str | None = None,
    history: tuple[str, ...] | None = None,
) -> VerifiedBackup:
    """Everything a restore needs to trust, checked without writing outside a temp dir."""
    try:
        archive = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as error:
        raise BackupError(f"{archive_path} is not a readable backup ({error}).") from error
    with archive:
        try:
            manifest = parse_manifest(archive.read(MANIFEST_NAME).decode("utf-8"))
        except KeyError as error:
            raise BackupError(f"{archive_path} has no manifest; it is not a backup.") from error
        except UnicodeDecodeError as error:
            raise BackupError("The backup's manifest is not readable.") from error

        listed = {entry.path for entry in manifest.entries}
        names = [info.filename for info in archive.infolist() if not info.is_dir()]
        unlisted = sorted(set(names) - listed - {MANIFEST_NAME})
        if unlisted:
            raise BackupError(
                f"The backup contains files its manifest does not list: {unlisted[:3]}."
            )
        if len(names) != len(set(names)):
            raise BackupError("The backup contains the same file twice.")
        for entry in manifest.entries:
            _check_entry(archive, entry)

        known = history if history is not None else migration.history()
        decision = judge(manifest.schema_revision, known, has_tables=True)
        if not decision.verdict.may_open:
            raise BackupError(decision.message.replace("database", "backup's database", 1))

        with tempfile.TemporaryDirectory(prefix="prometheus-verify-") as scratch:
            database = Path(scratch) / DATABASE_NAME
            _extract(archive, _database_entry(manifest), database)
            problem = migration.integrity_problem(database)
            if problem is not None:
                raise BackupError(f"The database in the backup is damaged ({problem}).")
            revision, _ = migration.inspect_sqlite(database)
            if revision != manifest.schema_revision:
                raise BackupError("The backup's database is not at the schema its manifest says.")

        master_key = None
        if manifest.includes_secrets and passphrase is not None:
            sealed_entry = next(e for e in manifest.entries if e.kind is EntryKind.SECRETS)
            master_key = open_secrets(archive.read(sealed_entry.path), passphrase)
    return VerifiedBackup(manifest, master_key)


def _database_entry(manifest: BackupManifest) -> BackupEntry:
    return next(entry for entry in manifest.entries if entry.kind is EntryKind.DATABASE)


def _check_entry(archive: zipfile.ZipFile, entry: BackupEntry) -> None:
    try:
        info = archive.getinfo(entry.path)
    except KeyError as error:
        raise BackupError(f"The backup is missing {entry.path}; it is incomplete.") from error
    if info.file_size != entry.size:
        raise BackupError(f"{entry.path} in the backup is not the size its manifest says.")
    digest = hashlib.sha256()
    read = 0
    try:
        with archive.open(info) as reader:
            while chunk := reader.read(CHUNK):
                read += len(chunk)
                if read > entry.size:
                    raise BackupError(f"{entry.path} in the backup is larger than listed.")
                digest.update(chunk)
    except (zipfile.BadZipFile, OSError, EOFError) as error:
        raise BackupError(f"{entry.path} in the backup cannot be read ({error}).") from error
    if read != entry.size or digest.hexdigest() != entry.sha256:
        raise BackupError(f"{entry.path} in the backup does not match its checksum.")


def _extract(archive: zipfile.ZipFile, entry: BackupEntry, destination: Path) -> None:
    """Write one entry, checking its digest again on the way: the archive may have changed."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with archive.open(entry.path) as reader, destination.open("wb") as writer:
        while chunk := reader.read(CHUNK):
            digest.update(chunk)
            writer.write(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    if digest.hexdigest() != entry.sha256:
        destination.unlink(missing_ok=True)
        raise BackupError(f"{entry.path} changed while it was being restored.")


# --- Restoring one ----------------------------------------------------------------------


def restore_backup(
    archive_path: Path,
    target: Installation,
    *,
    passphrase: str | None = None,
    skip_secrets: bool = False,
    store_master_key: Callable[[bytes], None] | None = None,
    history: tuple[str, ...] | None = None,
    disk_usage: Callable[[Path], object] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RestoreReport:
    verified = verify_backup(archive_path, passphrase=passphrase, history=history)
    manifest = verified.manifest
    if manifest.includes_secrets and verified.master_key is None and not skip_secrets:
        raise BackupError(
            "This backup includes sealed secrets. Give its passphrase, or restore without "
            "them and reconnect services afterwards. Nothing was changed."
        )

    data_dir = target.data_dir.expanduser().resolve() if target.data_dir.exists() else (
        target.data_dir.expanduser().absolute()
    )
    parent = data_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    free = getattr((disk_usage or shutil.disk_usage)(parent), "free", 0)
    if free < manifest.total_bytes * 2:
        raise BackupError(
            f"Restoring needs about {manifest.total_bytes * 2 // (1024 * 1024) + 1} MB free "
            f"beside {data_dir}, and {free // (1024 * 1024)} MB is. Nothing was changed."
        )

    stamp = clock().strftime("%Y%m%dT%H%M%SZ")
    finish_interrupted_restore(data_dir)
    staged = parent / f".{data_dir.name}.restoring-{stamp}"
    shutil.rmtree(staged, ignore_errors=True)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for entry in manifest.entries:
                if entry.kind in (EntryKind.DATABASE, EntryKind.DATA):
                    relative = entry.path.removeprefix(DATA_PREFIX)
                    _extract(archive, entry, staged / relative)
        (staged / STAGED_COMPLETE).write_text(stamp, encoding="utf-8")
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        raise

    previous = _switch(data_dir, staged, parent / f".{data_dir.name}.pre-restore-{stamp}")
    notes: list[str] = []
    if previous is not None:
        for name in MACHINE_FILES:
            carried = previous / name
            if carried.exists() and not (data_dir / name).exists():
                shutil.move(str(carried), str(data_dir / name))
        legacy_key = previous / "master.key"
        if legacy_key.exists() and verified.master_key is None:
            shutil.copy2(legacy_key, data_dir / "master.key")
        notes.append(
            f"The installation that was replaced is kept at {previous}. Delete it once the "
            "restored one works."
        )

    restored = unchanged = 0
    conflicts: list[str] = []
    with zipfile.ZipFile(archive_path) as archive:
        for entry in manifest.entries:
            if entry.kind not in (EntryKind.ARTIFACT, EntryKind.DECLARATION):
                continue
            roots = (
                target.file_roots
                if entry.kind is EntryKind.ARTIFACT
                else target.declaration_roots
            )
            root = roots.get(entry.root)
            if root is None:
                conflicts.append(f"{entry.path} (no '{entry.root}' folder is configured here)")
                continue
            relative = entry.path.split("/", 2)[2]
            destination = root / relative
            if destination.exists():
                if _digest(destination) == entry.sha256:
                    unchanged += 1
                else:
                    conflicts.append(str(destination))
                continue
            _extract(archive, entry, destination)
            restored += 1

    secrets_restored = False
    if verified.master_key is not None and store_master_key is not None:
        try:
            store_master_key(verified.master_key)
            secrets_restored = True
        except Exception as error:
            notes.append(
                f"The data was restored, but the master key could not be stored ({error}). "
                "Stored credentials cannot be opened until it is; reconnect services or "
                "restore again once the keychain is unlocked."
            )
    elif manifest.includes_secrets:
        notes.append("Secrets were in the backup and were not restored; reconnect services.")
    if conflicts:
        notes.append(
            f"{len(conflicts)} file(s) already exist here with different contents and were "
            "left as they are."
        )
    report = RestoreReport(
        manifest=manifest,
        previous=previous,
        restored_files=restored,
        unchanged_files=unchanged,
        conflicts=tuple(conflicts),
        secrets_restored=secrets_restored,
        notes=tuple(notes),
    )
    log.info(
        "backup.restored",
        path=str(archive_path),
        previous=str(previous) if previous else None,
        restored_files=restored,
        conflicts=len(conflicts),
        secrets=secrets_restored,
    )
    return report


def _switch(data_dir: Path, staged: Path, aside: Path) -> Path | None:
    if not data_dir.exists():
        os.replace(staged, data_dir)
        (data_dir / STAGED_COMPLETE).unlink(missing_ok=True)
        return None
    os.replace(data_dir, aside)
    try:
        os.replace(staged, data_dir)
    except BaseException:
        os.replace(aside, data_dir)
        raise
    (data_dir / STAGED_COMPLETE).unlink(missing_ok=True)
    return aside


def finish_interrupted_restore(data_dir: Path) -> str | None:
    """Complete or undo a restore a crash stopped between its two renames.

    Called before every restore and on every start. With the data directory
    present there is nothing to do but remove a staging directory that never
    finished. With it missing, a fully staged restore is moved into place; a
    partial one is discarded and the installation it was replacing is put back.
    """
    parent = data_dir.parent
    if not parent.exists():
        return None
    staged = sorted(parent.glob(f".{data_dir.name}.restoring-*"))
    aside = sorted(parent.glob(f".{data_dir.name}.pre-restore-*"))
    if data_dir.exists():
        for leftover in staged:
            shutil.rmtree(leftover, ignore_errors=True)
        return "cleaned" if staged else None
    complete = [path for path in staged if (path / STAGED_COMPLETE).exists()]
    if complete:
        os.replace(complete[-1], data_dir)
        (data_dir / STAGED_COMPLETE).unlink(missing_ok=True)
        log.warning("backup.interrupted_restore_completed", data_dir=str(data_dir))
        return "completed"
    if aside:
        os.replace(aside[-1], data_dir)
        log.warning("backup.interrupted_restore_undone", data_dir=str(data_dir))
        return "undone"
    return None


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as reader:
        while chunk := reader.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def database_revision(path: Path) -> str | None:
    try:
        with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as connection:
            row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.DatabaseError:
        return None
    return str(row[0]) if row else None


class LocalBackups:
    """Implements `domain.safety.backup.Backups` for this machine's installation."""

    def __init__(
        self,
        installation: Callable[[], Installation],
        *,
        default_dir: Path,
        master_key: Callable[[], bytes],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._installation = installation
        self._default_dir = default_dir
        self._master_key = master_key
        self._clock = clock

    def default_destination(self) -> Path:
        stamp = self._clock().strftime("%Y-%m-%d %H%M%S")
        return self._default_dir / f"Prometheus backup {stamp}.zip"

    def create(
        self, destination: str | None = None, *, passphrase: str | None = None
    ) -> dict[str, object]:
        from domain.safety.backup import summary

        target = Path(destination).expanduser() if destination else self.default_destination()
        manifest = create_backup(
            self._installation(),
            target,
            passphrase=passphrase,
            master_key=self._master_key if passphrase is not None else None,
            clock=self._clock,
        )
        return {"path": str(target), **summary(manifest)}

    def verify(self, path: str, *, passphrase: str | None = None) -> dict[str, object]:
        from domain.safety.backup import summary

        verified = verify_backup(Path(path).expanduser(), passphrase=passphrase)
        return {
            "path": str(Path(path).expanduser()),
            **summary(verified.manifest),
            "secrets_unlocked": verified.master_key is not None,
        }
