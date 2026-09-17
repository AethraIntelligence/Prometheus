"""Phase 13: an upgrade that cannot lose the installation it upgrades.

Every test runs the real migrations against a real SQLite file. What each one
proves is written as its name; the common thread is that a store is either
brought to the head or left exactly as it was, and a person is told what to do.
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from collections import namedtuple
from pathlib import Path

import pytest
from alembic import command

from domain.errors import StorageError
from domain.safety.schema import SchemaVerdict
from infrastructure.runtime import migration
from infrastructure.runtime.migration import (
    MigrationPreflightError,
    StorageIncompatibleError,
    alembic_config,
    history,
    prepare_storage,
)


def _url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _at(database: Path, revision: str) -> None:
    command.upgrade(alembic_config(_url(database)), revision)


def _add_task(database: Path, goal: str = "Kept across the upgrade") -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO tasks (id, goal, created_at, updated_at) VALUES "
            "('00000000-0000-0000-0000-000000000001', ?, '2026-09-01', '2026-09-01')",
            (goal,),
        )


def _goals(database: Path) -> list[str]:
    with sqlite3.connect(database) as connection:
        return [row[0] for row in connection.execute("SELECT goal FROM tasks")]


def _revision(database: Path) -> str:
    with sqlite3.connect(database) as connection:
        return connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]


def test_a_fresh_installation_is_created_at_the_head(tmp_path: Path) -> None:
    database = tmp_path / "prometheus.db"

    outcome = prepare_storage(tmp_path, _url(database))

    assert outcome.migrated and outcome.backup is None
    assert _revision(database) == history()[-1]
    assert not (tmp_path / migration.MARKER_NAME).exists()


def test_the_oldest_supported_schema_upgrades_with_its_data_and_a_verified_backup(
    tmp_path: Path,
) -> None:
    database = tmp_path / "prometheus.db"
    _at(database, migration.OLDEST_SUPPORTED_REVISION)
    _add_task(database)

    outcome = prepare_storage(tmp_path, _url(database))

    assert outcome.decision.verdict is SchemaVerdict.CURRENT
    assert _revision(database) == history()[-1]
    assert _goals(database) == ["Kept across the upgrade"]
    assert outcome.backup is not None and outcome.backup.parent.name == "backups"
    assert migration.integrity_problem(outcome.backup) is None
    assert _goals(outcome.backup) == ["Kept across the upgrade"]


def test_a_current_store_is_left_alone(tmp_path: Path) -> None:
    database = tmp_path / "prometheus.db"
    prepare_storage(tmp_path, _url(database))
    before = _digest(database)

    outcome = prepare_storage(tmp_path, _url(database))

    assert not outcome.migrated
    assert _digest(database) == before
    assert not (tmp_path / "backups").exists()


def test_a_store_written_by_a_newer_version_is_refused_without_a_single_write(
    tmp_path: Path,
) -> None:
    database = tmp_path / "prometheus.db"
    prepare_storage(tmp_path, _url(database))
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE alembic_version SET version_num = '999'")
    before = _digest(database)

    with pytest.raises(StorageIncompatibleError, match="newer version") as refused:
        prepare_storage(tmp_path, _url(database))

    assert refused.value.decision.verdict is SchemaVerdict.NEWER
    assert _digest(database) == before
    assert not (tmp_path / "backups").exists()


def test_tables_without_a_version_are_refused(tmp_path: Path) -> None:
    database = tmp_path / "prometheus.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE something (id INTEGER)")
    before = _digest(database)

    with pytest.raises(StorageIncompatibleError, match="no schema version"):
        prepare_storage(tmp_path, _url(database))
    assert _digest(database) == before


def test_a_failed_upgrade_puts_the_store_back_as_it_was(tmp_path: Path) -> None:
    database = tmp_path / "prometheus.db"
    _at(database, "040")
    _add_task(database)
    before = _goals(database)

    def breaks_half_way(config) -> None:
        command.upgrade(config, "041")
        raise RuntimeError("the disk went away")

    with pytest.raises(StorageError, match="put back"):
        prepare_storage(tmp_path, _url(database), upgrade=breaks_half_way)

    assert _revision(database) == "040"
    assert _goals(database) == before
    assert not (tmp_path / migration.MARKER_NAME).exists()


_KILLED_MID_MIGRATION = """
import os, signal, sys
from alembic import command
from pathlib import Path
from infrastructure.runtime.migration import prepare_storage

data, url, stop_at = Path(sys.argv[1]), sys.argv[2], sys.argv[3]

def killed(config):
    command.upgrade(config, stop_at)
    os.kill(os.getpid(), signal.SIGKILL)

prepare_storage(data, url, upgrade=killed)
"""


def _kill_during_migration(tmp_path: Path, database: Path, stop_at: str) -> None:
    import subprocess

    finished = subprocess.run(
        [sys.executable, "-c", _KILLED_MID_MIGRATION, str(tmp_path), _url(database), stop_at],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert finished.returncode == -9, finished.stderr.decode()


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL is a POSIX signal")
def test_a_migration_killed_half_way_is_recovered_on_the_next_start(tmp_path: Path) -> None:
    """The process doing it cannot restore anything; the next one must."""
    database = tmp_path / "prometheus.db"
    _at(database, "040")
    _add_task(database)

    _kill_during_migration(tmp_path, database, "041")

    assert (tmp_path / migration.MARKER_NAME).exists(), "a killed process removes no marker"
    assert _revision(database) == "041", "the half-applied state is what a crash leaves"

    outcome = prepare_storage(tmp_path, _url(database))

    assert outcome.recovered_from is not None
    assert _revision(database) == history()[-1]
    assert _goals(database) == ["Kept across the upgrade"]
    assert not (tmp_path / migration.MARKER_NAME).exists()


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL is a POSIX signal")
def test_a_killed_first_creation_is_discarded_rather_than_refused(tmp_path: Path) -> None:
    database = tmp_path / "prometheus.db"

    _kill_during_migration(tmp_path, database, "003")
    assert (tmp_path / migration.MARKER_NAME).exists()

    outcome = prepare_storage(tmp_path, _url(database))

    assert _revision(database) == history()[-1]
    assert outcome.migrated


def test_no_room_for_a_backup_refuses_before_anything_is_written(tmp_path: Path) -> None:
    database = tmp_path / "prometheus.db"
    _at(database, "040")
    before = _digest(database)
    Usage = namedtuple("Usage", "total used free")

    with pytest.raises(MigrationPreflightError, match="Free some space"):
        migration.preflight(tmp_path, database, disk_usage=lambda _: Usage(1, 1, 1024))

    assert _digest(database) == before


@pytest.mark.skipif(
    __import__("os").geteuid() == 0, reason="root ignores directory permissions"
)
def test_a_read_only_data_directory_is_named_with_what_to_do(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    data.chmod(0o500)
    try:
        with pytest.raises(MigrationPreflightError, match="not writable"):
            migration.preflight(data, None)
    finally:
        data.chmod(0o700)


def test_a_damaged_database_is_not_upgraded(tmp_path: Path) -> None:
    database = tmp_path / "prometheus.db"
    _at(database, "040")
    raw = bytearray(database.read_bytes())
    # Page 2 onward is table content; overwrite it without touching the header.
    raw[4096:8192] = b"\xff" * 4096
    database.write_bytes(bytes(raw))

    with pytest.raises(StorageError):
        prepare_storage(tmp_path, _url(database))
    assert not (tmp_path / migration.MARKER_NAME).exists()


def test_garbage_where_the_database_should_be_says_restore(tmp_path: Path) -> None:
    database = tmp_path / "prometheus.db"
    database.write_bytes(b"this is not a database" * 200)

    with pytest.raises(StorageError, match="prometheus restore"):
        prepare_storage(tmp_path, _url(database))
