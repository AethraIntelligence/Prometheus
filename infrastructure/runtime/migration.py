"""Bringing a store up to this version's schema without being able to lose it.

A packaged application cannot ask a person to run `alembic upgrade head`, so the
runtime migrates on start. That is only acceptable if a failed or interrupted
migration leaves a working installation behind, so every upgrade is:

1. **Judged** (`domain.safety.schema`) from a read-only look at the store. A
   store written by a newer version, an unversioned one, or one older than the
   oldest supported revision is refused before anything is written.
2. **Preflighted.** The data directory must be writable, there must be room for
   a backup and for the migration's own copy of each rebuilt table, and a SQLite
   file must pass `PRAGMA integrity_check`. Each failure names what to do.
3. **Backed up** with SQLite's online backup API - a consistent snapshot even of
   a file with a write-ahead log - into `backups/`, and the snapshot is checked
   before the migration is allowed to start.
4. **Marked.** A small file records that a migration is in progress and where
   its backup is. It is removed only after the schema reaches the head.
5. **Migrated.** On an error the backup is put back atomically.

**A migration killed half-way is the case the marker exists for.** The process
that was migrating cannot restore anything; the next start finds the marker,
restores the backup it names, and migrates again. No step here depends on a
migration being transactional, because not all of them are.

PostgreSQL is judged and refused the same way, but not backed up: a server's
backup is `pg_dump` or the provider's snapshot, taken by whoever runs it. An
upgrade there proceeds only when that is acknowledged in settings.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url

from domain.errors import StorageError
from domain.safety.schema import SchemaDecision, SchemaVerdict, judge
from infrastructure.observability.logging import get_logger

log = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "persistence" / "migrations"
#: The oldest schema this version upgrades from. Every revision back to the
#: first is exercised by `tests/integration/test_schema_upgrades.py`.
OLDEST_SUPPORTED_REVISION = "001"
MARKER_NAME = "MIGRATION_IN_PROGRESS"
BACKUPS_DIR_NAME = "backups"
#: Room a migration needs beyond the backup: SQLite's batch mode rebuilds a
#: table by copying it, and the largest table can be most of the file.
HEADROOM_FACTOR = 2
MIN_FREE_BYTES = 64 * 1024 * 1024


class StorageIncompatibleError(StorageError):
    """The store cannot be opened by this version. Nothing was changed."""

    def __init__(self, decision: SchemaDecision) -> None:
        super().__init__(decision.message)
        self.decision = decision


class MigrationPreflightError(StorageError):
    """A migration was not started because it could not be made safe."""


@dataclass(frozen=True, slots=True)
class MigrationOutcome:
    decision: SchemaDecision
    migrated: bool = False
    backup: Path | None = None
    recovered_from: Path | None = None


def alembic_config(database_url: str) -> Config:
    """The migrations, located from the installed package rather than a working directory."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def history() -> tuple[str, ...]:
    script = ScriptDirectory.from_config(alembic_config("sqlite://"))
    return tuple(revision.revision for revision in reversed(list(script.walk_revisions())))


def sqlite_path(database_url: str) -> Path | None:
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite") or not url.database:
        return None
    return Path(url.database)


# --- Looking without touching ---------------------------------------------------------


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def inspect_sqlite(path: Path) -> tuple[str | None, bool]:
    """The revision and whether there are any tables, read without a write."""
    if not path.exists() or path.stat().st_size == 0:
        return None, False
    try:
        with _read_only(path) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "alembic_version" not in tables:
                return None, bool(tables)
            row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.DatabaseError as error:
        raise StorageError(
            f"The database at {path} cannot be read ({error}). It was not changed. "
            "Restore it from a backup with `prometheus restore`."
        ) from error
    return (str(row[0]) if row else None), True


def integrity_problem(path: Path) -> str | None:
    try:
        with _read_only(path) as connection:
            rows = connection.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.DatabaseError as error:
        return str(error)
    answers = [str(row[0]) for row in rows]
    return None if answers == ["ok"] else "; ".join(answers[:5])


async def _postgres_revision(database_url: str) -> tuple[str | None, bool]:
    from sqlalchemy import inspect, text

    from infrastructure.persistence.session import create_engine

    engine = create_engine(database_url)
    try:
        async with engine.connect() as connection:
            names = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
            if "alembic_version" not in names:
                return None, bool(names)
            found = await connection.execute(text("SELECT version_num FROM alembic_version"))
            row = found.first()
            return (str(row[0]) if row else None), True
    finally:
        await engine.dispose()


def decide(database_url: str) -> SchemaDecision:
    path = sqlite_path(database_url)
    if path is not None:
        current, has_tables = inspect_sqlite(path)
    else:
        import asyncio

        current, has_tables = asyncio.run(_postgres_revision(database_url))
    return judge(
        current,
        history(),
        has_tables=has_tables,
        oldest_supported=OLDEST_SUPPORTED_REVISION,
    )


# --- Preflight and backup ---------------------------------------------------------------


def preflight(data_dir: Path, database: Path | None, *, disk_usage=None) -> None:
    """Raise with a concrete action if a migration here could not be made safe."""
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / f".write-probe-{os.getpid()}"
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as error:
        raise MigrationPreflightError(
            f"The data directory {data_dir} is not writable ({error.strerror or error}). "
            "Nothing was changed. Make it writable by this user, or set PROMETHEUS_DATA_DIR "
            "to a directory that is."
        ) from error

    size = database.stat().st_size if database is not None and database.exists() else 0
    needed = max(MIN_FREE_BYTES, size * (1 + HEADROOM_FACTOR))
    free = (disk_usage or shutil.disk_usage)(data_dir).free
    if free < needed:
        raise MigrationPreflightError(
            f"Upgrading the database needs about {needed // (1024 * 1024)} MB free in "
            f"{data_dir}, and {free // (1024 * 1024)} MB is. Nothing was changed. Free some "
            "space and start Prometheus again."
        )

    if database is not None and database.exists() and size > 0:
        problem = integrity_problem(database)
        if problem is not None:
            raise MigrationPreflightError(
                f"The database at {database} is damaged ({problem}). It was not upgraded. "
                "Restore a backup with `prometheus restore`."
            )


def snapshot(database: Path, destination: Path) -> Path:
    """A consistent copy through SQLite's own backup API, checked before it is trusted."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".partial")
    partial.unlink(missing_ok=True)
    source = _read_only(database)
    try:
        target = sqlite3.connect(partial)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    with partial.open("rb") as handle:
        os.fsync(handle.fileno())
    problem = integrity_problem(partial)
    if problem is not None:
        partial.unlink(missing_ok=True)
        raise MigrationPreflightError(f"The backup could not be verified ({problem}).")
    os.replace(partial, destination)
    return destination


def put_back(backup: Path, database: Path) -> None:
    """Replace the database with a backup in one rename, and drop its journal files."""
    staged = database.with_name(database.name + ".restoring")
    shutil.copyfile(backup, staged)
    with staged.open("rb") as handle:
        os.fsync(handle.fileno())
    for suffix in ("-wal", "-shm", "-journal"):
        database.with_name(database.name + suffix).unlink(missing_ok=True)
    os.replace(staged, database)


# --- Migrating --------------------------------------------------------------------------


def prepare_storage(
    data_dir: Path,
    database_url: str,
    *,
    postgres_backup_acknowledged: bool = False,
    upgrade: Callable[[Config], None] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> MigrationOutcome:
    """Bring the store to the head, or refuse without changing it."""
    run_upgrade = upgrade or (lambda config: command.upgrade(config, "head"))
    database = sqlite_path(database_url)
    marker = data_dir / MARKER_NAME

    recovered_from: Path | None = None
    if database is not None and marker.exists():
        recovered_from = _recover_interrupted(marker, database)

    decision = decide(database_url)
    if not decision.verdict.may_open:
        raise StorageIncompatibleError(decision)
    if decision.verdict is SchemaVerdict.CURRENT:
        return MigrationOutcome(decision, recovered_from=recovered_from)

    if database is None:
        if decision.verdict is SchemaVerdict.UPGRADE and not postgres_backup_acknowledged:
            raise MigrationPreflightError(
                f"The PostgreSQL schema is at {decision.current} and this version needs "
                f"{decision.head}. Take a backup (pg_dump, or your provider's snapshot), then "
                "set PROMETHEUS_POSTGRES_BACKUP_ACKNOWLEDGED=true and start again."
            )
        run_upgrade(alembic_config(database_url))
        return MigrationOutcome(decide(database_url), migrated=True)

    preflight(data_dir, database)
    backup: Path | None = None
    if decision.verdict is SchemaVerdict.UPGRADE:
        stamp = clock().strftime("%Y%m%dT%H%M%SZ")
        backup = snapshot(
            database,
            data_dir
            / BACKUPS_DIR_NAME
            / f"pre-migration-{decision.current}-to-{decision.head}-{stamp}.db",
        )
    # Written for a fresh store too: a first migration killed half-way leaves
    # tables with no version, which would otherwise be refused on every start.
    _write_marker(marker, backup, decision)

    try:
        run_upgrade(alembic_config(database_url))
        after = decide(database_url)
        if after.verdict is not SchemaVerdict.CURRENT:
            raise StorageError(f"the schema stopped at {after.current}, not {after.head}")
    except BaseException as error:
        if backup is None:
            _discard(database)
            marker.unlink(missing_ok=True)
            raise StorageError(
                f"Creating the database failed ({error}). The half-created file was removed; "
                "nothing else was touched. Report this error."
            ) from error
        put_back(backup, database)
        marker.unlink(missing_ok=True)
        log.warning("storage.migration_rolled_back", backup=str(backup), error=str(error))
        raise StorageError(
            f"Upgrading the database failed ({error}). The database was put back as it "
            f"was before the upgrade, from {backup}. Nothing was lost; report this error."
        ) from error
    marker.unlink(missing_ok=True)
    log.info(
        "storage.migrated",
        from_revision=decision.current,
        to_revision=decision.head,
        backup=str(backup) if backup else None,
    )
    return MigrationOutcome(after, migrated=True, backup=backup, recovered_from=recovered_from)


def _discard(database: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        database.with_name(database.name + suffix).unlink(missing_ok=True)


def _write_marker(marker: Path, backup: Path | None, decision: SchemaDecision) -> None:
    temporary = marker.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "backup": str(backup) if backup is not None else None,
                "from": decision.current,
                "to": decision.head,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, marker)


def _recover_interrupted(marker: Path, database: Path) -> Path | None:
    try:
        recorded = json.loads(marker.read_text(encoding="utf-8"))
        if recorded["backup"] is None:
            # The interrupted migration was creating the store; there was
            # nothing in it to keep.
            _discard(database)
            marker.unlink(missing_ok=True)
            log.warning("storage.interrupted_creation_discarded", database=str(database))
            return None
        backup = Path(recorded["backup"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise StorageError(
            f"An earlier upgrade was interrupted and its record at {marker} cannot be read. "
            "The database was not changed. Restore a backup from the backups folder with "
            "`prometheus restore`, then remove that file."
        ) from error
    if not backup.exists() or integrity_problem(backup) is not None:
        raise StorageError(
            f"An earlier upgrade was interrupted and its backup {backup} is missing or "
            "damaged. The database was not changed. Restore a backup with "
            "`prometheus restore`."
        )
    put_back(backup, database)
    marker.unlink(missing_ok=True)
    log.warning("storage.interrupted_migration_recovered", backup=str(backup))
    return backup
