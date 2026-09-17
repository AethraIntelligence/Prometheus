"""Phase 13: a backup that can be trusted, and a restore that cannot make things worse.

Real migrations, a real SQLite file and real archives on `tmp_path`. The common
thread: a restore either produces a working installation or changes nothing,
and a secret leaves the machine only sealed, only when asked.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import zipfile
from collections import namedtuple
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command

from domain.safety.backup import MANIFEST_NAME, BackupError, EntryKind
from infrastructure.runtime import backup as backups
from infrastructure.runtime.backup import Installation, create_backup, restore_backup
from infrastructure.runtime.migration import alembic_config, history, prepare_storage

PASSPHRASE = "correct horse battery staple"


def _installation(root: Path) -> Installation:
    data = root / "data"
    return Installation(
        data_dir=data,
        database=data / "prometheus.db",
        file_roots={"files": root / "Documents" / "Prometheus"},
        app_version="test",
    )


def _populate(installation: Installation, revision: str | None = None) -> None:
    data = installation.data_dir
    data.mkdir(parents=True, exist_ok=True)
    url = f"sqlite+aiosqlite:///{installation.database}"
    if revision is None:
        prepare_storage(data, url)
    else:
        command.upgrade(alembic_config(url), revision)
    with sqlite3.connect(installation.database) as connection:
        connection.execute(
            "INSERT INTO tasks (id, goal, created_at, updated_at) VALUES "
            "('00000000-0000-0000-0000-000000000001', 'Write the report', "
            "'2026-09-01', '2026-09-01')"
        )
    (data / "settings.json").write_text('{"memory": false}', encoding="utf-8")
    (data / "ACTIVE_WORKSPACE").write_text("default", encoding="utf-8")
    (data / "workspaces" / "second").mkdir(parents=True)
    (data / "workspaces" / "second" / "notes.md").write_text("second", encoding="utf-8")
    # Never data: secrets, the brake, a lock, a marker, earlier backups.
    (data / "master.key").write_text(base64.urlsafe_b64encode(bytes(32)).decode(), "utf-8")
    (data / "credentials.json").write_text('{"token": "plaintext-canary"}', encoding="utf-8")
    (data / "STOP").write_text("stopped", encoding="utf-8")
    (data / "backups").mkdir()
    (data / "backups" / "old.db").write_bytes(b"old")
    files = installation.file_roots["files"]
    (files / "Report thread").mkdir(parents=True)
    (files / "Report thread" / "report.md").write_text("# Report\n42", encoding="utf-8")
    # Settle the file: a connection the migrations left for the collector would
    # otherwise checkpoint its log in the middle of a test comparing digests.
    import gc

    gc.collect()
    with sqlite3.connect(installation.database) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _goals(database: Path) -> list[str]:
    with sqlite3.connect(database) as connection:
        return [row[0] for row in connection.execute("SELECT goal FROM tasks")]


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.name.endswith(("-wal", "-shm")):
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _clock() -> datetime:
    return datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


def test_a_backup_holds_the_data_and_nothing_that_is_not_data(tmp_path: Path) -> None:
    installation = _installation(tmp_path)
    _populate(installation)

    manifest = create_backup(installation, tmp_path / "prometheus.zip", clock=_clock)

    names = set(zipfile.ZipFile(tmp_path / "prometheus.zip").namelist())
    assert "data/prometheus.db" in names
    assert "data/settings.json" in names
    assert "data/workspaces/second/notes.md" in names
    assert "files/files/Report thread/report.md" in names
    for forbidden in ("master.key", "credentials.json", "STOP", "backups", "runtime.lock"):
        assert not any(forbidden in name for name in names), forbidden
    assert not manifest.includes_secrets
    assert manifest.schema_revision == history()[-1]
    assert b"plaintext-canary" not in (tmp_path / "prometheus.zip").read_bytes()


def test_deleting_everything_and_restoring_gives_the_same_result_back(tmp_path: Path) -> None:
    installation = _installation(tmp_path)
    _populate(installation)
    archive = tmp_path / "prometheus.zip"
    create_backup(installation, archive, clock=_clock)

    import shutil

    shutil.rmtree(installation.data_dir)
    shutil.rmtree(installation.file_roots["files"])

    report = restore_backup(archive, installation, clock=_clock)

    assert report.previous is None
    assert _goals(installation.database) == ["Write the report"]
    assert (installation.data_dir / "settings.json").read_text() == '{"memory": false}'
    assert (installation.data_dir / "workspaces" / "second" / "notes.md").read_text() == "second"
    report_file = installation.file_roots["files"] / "Report thread" / "report.md"
    assert report_file.read_text() == "# Report\n42"
    assert report.restored_files == 1
    assert not (installation.data_dir / backups.STAGED_COMPLETE).exists()
    assert prepare_storage(
        installation.data_dir, f"sqlite+aiosqlite:///{installation.database}"
    ).decision.verdict.may_open


def test_restoring_over_a_working_installation_keeps_the_old_one_and_its_machine_files(
    tmp_path: Path,
) -> None:
    installation = _installation(tmp_path)
    _populate(installation)
    archive = tmp_path / "prometheus.zip"
    create_backup(installation, archive, clock=_clock)
    with sqlite3.connect(installation.database) as connection:
        connection.execute("DELETE FROM tasks")
    report_file = installation.file_roots["files"] / "Report thread" / "report.md"
    report_file.write_text("changed since the backup", encoding="utf-8")

    report = restore_backup(archive, installation, clock=_clock)

    assert _goals(installation.database) == ["Write the report"]
    assert report.previous is not None and report.previous.exists()
    assert _goals(report.previous / "prometheus.db") == []
    assert (installation.data_dir / "backups" / "old.db").exists(), "backups stay with the machine"
    assert (installation.data_dir / "master.key").exists(), "the machine's key stays too"
    assert report_file.read_text() == "changed since the backup", "never overwritten"
    assert report.conflicts == (str(report_file),)


def test_secrets_are_added_only_sealed_and_open_only_with_the_passphrase(tmp_path: Path) -> None:
    installation = _installation(tmp_path)
    _populate(installation)
    key = bytes(range(32))
    archive = tmp_path / "moving.zip"

    manifest = create_backup(
        installation, archive, passphrase=PASSPHRASE, master_key=lambda: key, clock=_clock
    )

    assert manifest.includes_secrets
    raw = archive.read_bytes()
    assert key not in raw and base64.b64encode(key) not in raw
    assert base64.urlsafe_b64encode(key) not in raw

    target = _installation(tmp_path / "new-machine")
    before = tmp_path / "new-machine"
    before.mkdir()
    with pytest.raises(BackupError, match="passphrase does not open"):
        restore_backup(archive, target, passphrase="the wrong passphrase", clock=_clock)
    assert not target.data_dir.exists(), "a wrong passphrase writes nothing"

    with pytest.raises(BackupError, match="Give its passphrase"):
        restore_backup(archive, target, clock=_clock)
    assert not target.data_dir.exists()

    stored: list[bytes] = []
    report = restore_backup(
        archive, target, passphrase=PASSPHRASE, store_master_key=stored.append, clock=_clock
    )

    assert stored == [key]
    assert report.secrets_restored
    assert _goals(target.database) == ["Write the report"]


def test_a_short_passphrase_is_refused(tmp_path: Path) -> None:
    installation = _installation(tmp_path)
    _populate(installation)

    with pytest.raises(BackupError, match="at least"):
        create_backup(installation, tmp_path / "x.zip", passphrase="short", master_key=bytes)
    assert not (tmp_path / "x.zip").exists()


def test_an_existing_destination_is_never_replaced(tmp_path: Path) -> None:
    installation = _installation(tmp_path)
    _populate(installation)
    (tmp_path / "prometheus.zip").write_bytes(b"somebody's file")

    with pytest.raises(BackupError, match="already exists"):
        create_backup(installation, tmp_path / "prometheus.zip")
    assert (tmp_path / "prometheus.zip").read_bytes() == b"somebody's file"


# --- Archives that must not be trusted ------------------------------------------------------


def _rewrite(archive: Path, change) -> Path:
    """A copy of the archive with its members passed through `change(name, data)`."""
    tampered = archive.with_name("tampered.zip")
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(tampered, "w") as target:
        for info in source.infolist():
            result = change(info.filename, source.read(info))
            if result is not None:
                name, data = result
                target.writestr(name, data)
    return tampered


@pytest.fixture
def made(tmp_path: Path) -> tuple[Installation, Path]:
    installation = _installation(tmp_path)
    _populate(installation)
    archive = tmp_path / "prometheus.zip"
    create_backup(installation, archive, clock=_clock)
    return installation, archive


def _unchanged_by(installation: Installation, action) -> None:
    before = _tree_digest(installation.data_dir)
    with pytest.raises(BackupError):
        action()
    assert _tree_digest(installation.data_dir) == before


def test_a_changed_byte_is_caught_before_anything_is_written(made) -> None:
    installation, archive = made

    def flip(name: str, data: bytes):
        if name == "data/settings.json":
            data = data.replace(b"false", b"true ")
        return name, data

    tampered = _rewrite(archive, flip)
    _unchanged_by(installation, lambda: restore_backup(tampered, installation, clock=_clock))


def test_a_partial_archive_is_refused(made) -> None:
    installation, archive = made
    partial = archive.with_name("partial.zip")
    partial.write_bytes(archive.read_bytes()[: archive.stat().st_size // 2])

    _unchanged_by(installation, lambda: restore_backup(partial, installation, clock=_clock))


def test_a_file_the_manifest_does_not_list_is_refused(made) -> None:
    installation, archive = made
    with zipfile.ZipFile(archive, "a") as appended:
        appended.writestr("data/extra.py", "import os")

    _unchanged_by(installation, lambda: restore_backup(archive, installation, clock=_clock))


def test_a_manifest_path_that_climbs_out_is_refused(made, tmp_path: Path) -> None:
    installation, archive = made

    def climb(name: str, data: bytes):
        if name != MANIFEST_NAME:
            return name, data
        manifest = json.loads(data)
        manifest["entries"][1]["path"] = "../../escaped.txt"
        return name, json.dumps(manifest).encode()

    tampered = _rewrite(archive, climb)
    _unchanged_by(installation, lambda: restore_backup(tampered, installation, clock=_clock))
    assert not (tmp_path.parent / "escaped.txt").exists()


def test_a_backup_from_a_newer_version_is_refused(made) -> None:
    installation, archive = made

    def newer(name: str, data: bytes):
        if name != MANIFEST_NAME:
            return name, data
        manifest = json.loads(data)
        manifest["schema_revision"] = "999"
        return name, json.dumps(manifest).encode()

    tampered = _rewrite(archive, newer)
    _unchanged_by(installation, lambda: restore_backup(tampered, installation, clock=_clock))


def test_a_backup_in_an_unknown_format_is_refused(made) -> None:
    installation, archive = made

    def future(name: str, data: bytes):
        if name != MANIFEST_NAME:
            return name, data
        manifest = json.loads(data)
        manifest["format_version"] = 2
        return name, json.dumps(manifest).encode()

    tampered = _rewrite(archive, future)
    _unchanged_by(installation, lambda: restore_backup(tampered, installation, clock=_clock))


def test_an_older_schema_is_restored_and_upgraded_on_the_next_start(tmp_path: Path) -> None:
    installation = _installation(tmp_path)
    _populate(installation, revision="040")
    archive = tmp_path / "old.zip"
    create_backup(installation, archive, clock=_clock)
    target = _installation(tmp_path / "elsewhere")

    restore_backup(archive, target, clock=_clock)
    outcome = prepare_storage(target.data_dir, f"sqlite+aiosqlite:///{target.database}")

    assert outcome.migrated and outcome.decision.current == history()[-1]
    assert _goals(target.database) == ["Write the report"]


def test_no_room_to_restore_changes_nothing(made) -> None:
    installation, archive = made
    Usage = namedtuple("Usage", "total used free")

    _unchanged_by(
        installation,
        lambda: restore_backup(
            archive, installation, disk_usage=lambda _: Usage(1, 1, 10), clock=_clock
        ),
    )


# --- A restore a crash interrupted ---------------------------------------------------------


def test_a_crash_between_the_renames_is_completed_when_the_staging_was_complete(
    made, tmp_path: Path
) -> None:
    installation, _ = made
    data = installation.data_dir
    staged = data.parent / f".{data.name}.restoring-1"
    staged.mkdir()
    backups.migration.snapshot(installation.database, staged / "prometheus.db")
    (staged / backups.STAGED_COMPLETE).write_text("1")
    data.rename(data.parent / f".{data.name}.pre-restore-1")

    assert backups.finish_interrupted_restore(data) == "completed"
    assert _goals(installation.database) == ["Write the report"]


def test_a_crash_before_staging_finished_puts_the_old_installation_back(made) -> None:
    installation, _ = made
    data = installation.data_dir
    (data.parent / f".{data.name}.restoring-1").mkdir()
    data.rename(data.parent / f".{data.name}.pre-restore-1")

    assert backups.finish_interrupted_restore(data) == "undone"
    assert _goals(installation.database) == ["Write the report"]


def test_the_manifest_lists_what_the_archive_holds(made) -> None:
    _, archive = made
    verified = backups.verify_backup(archive)

    kinds = {entry.kind for entry in verified.manifest.entries}
    assert kinds == {EntryKind.DATABASE, EntryKind.DATA, EntryKind.ARTIFACT}


def test_a_full_disk_while_writing_leaves_no_archive_and_no_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installation = _installation(tmp_path)
    _populate(installation)
    destination = tmp_path / "out" / "prometheus.zip"
    real_add = backups._add
    calls = {"count": 0}

    def fills_up(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 3:
            raise OSError(28, "No space left on device")
        return real_add(*args, **kwargs)

    monkeypatch.setattr(backups, "_add", fills_up)

    with pytest.raises(OSError, match="No space"):
        create_backup(installation, destination, clock=_clock)

    assert not destination.exists()
    assert list(destination.parent.iterdir()) == [], "no half-written archive is left behind"
