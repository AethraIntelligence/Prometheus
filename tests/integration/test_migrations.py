"""Migration 001 must produce exactly the schema the models declare."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from infrastructure.persistence.models import Base

REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(database_url: str) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option(
        "script_location", str(REPO_ROOT / "infrastructure/persistence/migrations")
    )
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_the_history_is_a_single_line() -> None:
    """Branching migrations on a one-file local database buy nothing but pain."""
    script = ScriptDirectory.from_config(_alembic_config("sqlite://"))
    assert len(script.get_heads()) == 1


def test_migration_creates_the_declared_tables(tmp_path: Path) -> None:
    from alembic import command

    database = tmp_path / "migrated.db"
    command.upgrade(_alembic_config(f"sqlite+aiosqlite:///{database}"), "head")

    inspector = inspect(create_engine(f"sqlite:///{database}"))
    # The two text indexes - one over memory, one over document passages - and
    # the shadow tables SQLite creates for them are declared as DDL rather than
    # as metadata, because a virtual table has no columns to compare. They are
    # excluded here and checked by exercising a search instead
    # (tests/integration/test_memory_repository.py, tests/unit/test_knowledge.py).
    indexes = ("memory_items_fts", "chunks_fts")
    tables = {
        name
        for name in inspector.get_table_names()
        if name != "alembic_version" and not name.startswith(indexes)
    }
    assert tables == set(Base.metadata.tables)

    for name, table in Base.metadata.tables.items():
        migrated = {column["name"] for column in inspector.get_columns(name)}
        assert migrated == set(table.columns.keys()), f"column drift in {name}"


def test_workflow_schedule_migration_keeps_legacy_schedule_and_history(
    tmp_path: Path,
) -> None:
    from alembic import command

    database = tmp_path / "legacy-schedule.db"
    config = _alembic_config(f"sqlite+aiosqlite:///{database}")
    command.upgrade(config, "037")
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO schedules "
                "(id, workspace_id, name, request, on_event, enabled, runs, created_at, "
                "timezone, model, approvals, version) VALUES "
                "(:id, 'default', 'Digest', 'Summarise notes', 'inbox.arrived', 1, 7, "
                ":created, '', '', 'ASK', 3)"
            ),
            {"id": "00000000-0000-0000-0000-000000000001", "created": "2026-09-17"},
        )
        connection.execute(
            text(
                "INSERT INTO events "
                "(id, workspace_id, kind, payload, source, created_at) VALUES "
                "(:id, 'default', 'objective.finished', :payload, 'Digest', :created)"
            ),
            {
                "id": "00000000-0000-0000-0000-000000000002",
                "payload": '{"schedule_version": 3}',
                "created": "2026-09-17",
            },
        )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        schedule = connection.execute(
            text(
                "SELECT request, runs, version, workflow_name, workflow_version "
                "FROM schedules"
            )
        ).one()
        events = connection.execute(text("SELECT count(*) FROM events")).scalar_one()
    assert tuple(schedule) == ("Summarise notes", 7, 3, "", None)
    assert events == 1


def test_memory_provenance_migration_traces_existing_memories_and_keeps_the_index(
    tmp_path: Path,
) -> None:
    """Migration 039 on a store with memories in it.

    Existing rows get the provenance they already imply, not a blanket
    "unknown", and the text index still finds them afterwards - the columns are
    added one by one precisely so its triggers survive.
    """
    from alembic import command

    database = tmp_path / "legacy-memory.db"
    config = _alembic_config(f"sqlite+aiosqlite:///{database}")
    command.upgrade(config, "038")
    engine = create_engine(f"sqlite:///{database}")
    rows = [
        ("00000000-0000-0000-0000-00000000000a", "SEMANTIC", "Invoices live in finance",
         None, '{"source": "person"}'),
        ("00000000-0000-0000-0000-00000000000b", "SEMANTIC", "The user prefers: Markdown",
         None, '{"source": "Always use Markdown"}'),
        ("00000000-0000-0000-0000-00000000000c", "EPISODIC", "Sorted the inbox",
         "00000000-0000-0000-0000-0000000000ff", '{"status": "COMPLETED"}'),
    ]  # fmt: skip
    with engine.begin() as connection:
        for item_id, kind, content, task_id, meta in rows:
            connection.execute(
                text(
                    "INSERT INTO memory_items (id, workspace_id, scope, kind, content, "
                    "task_id, metadata, importance, created_at) VALUES "
                    "(:id, 'default', 'WORKSPACE', :kind, :content, :task, :meta, 0.5, "
                    "'2026-09-01 00:00:00')"
                ),
                {"id": item_id, "kind": kind, "content": content, "task": task_id, "meta": meta},
            )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        traced = connection.execute(
            text(
                "SELECT content, basis, confidence, source_kind, source_ref, status "
                "FROM memory_items ORDER BY id"
            )
        ).all()
        found = connection.execute(
            text("SELECT item_id FROM memory_items_fts WHERE memory_items_fts MATCH 'inbox'")
        ).all()
        inserted = connection.execute(
            text(
                "INSERT INTO memory_items (id, workspace_id, scope, kind, content, metadata, "
                "importance, created_at) VALUES ('00000000-0000-0000-0000-00000000000d', "
                "'default', 'WORKSPACE', 'SEMANTIC', 'Refunds live in returns', '{}', 0.5, "
                "'2026-09-18 00:00:00')"
            )
        )
        assert inserted.rowcount == 1
        refound = connection.execute(
            text("SELECT item_id FROM memory_items_fts WHERE memory_items_fts MATCH 'refunds'")
        ).all()

    assert [tuple(row) for row in traced] == [
        ("Invoices live in finance", "STATED", 1.0, "PERSON", "", "ACTIVE"),
        ("The user prefers: Markdown", "STATED", 0.8, "OBJECTIVE", "", "ACTIVE"),
        (
            "Sorted the inbox",
            "REPORTED",
            0.75,
            "TASK",
            "00000000-0000-0000-0000-0000000000ff",
            "ACTIVE",
        ),
    ]
    assert len(found) == 1
    assert len(refound) == 1, "the triggers that keep the index in step survived"


def test_model_contract_migration_marks_served_entries_local(tmp_path: Path) -> None:
    from alembic import command

    database = tmp_path / "legacy-catalog.db"
    config = _alembic_config(f"sqlite+aiosqlite:///{database}")
    command.upgrade(config, "039")
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        for name, provider in (("here", "local"), ("there", "openrouter")):
            connection.execute(
                text(
                    "INSERT INTO model_entries (id, workspace_id, name, provider, model, "
                    "connection, capabilities, context_tokens, input_cost_per_1k_usd, "
                    "output_cost_per_1k_usd, quality, dimensions, created_at, updated_at) "
                    "VALUES (:id, 'default', :name, :provider, 'm', '', '[]', 8192, 0, 0, "
                    "0.5, 0, '2026-09-01', '2026-09-01')"
                ),
                {"id": f"00000000-0000-0000-0000-0000000000{len(name):02d}", "name": name,
                 "provider": provider},
            )  # fmt: skip

    command.upgrade(config, "head")

    with engine.connect() as connection:
        rows = dict(
            connection.execute(text("SELECT name, privacy FROM model_entries")).all()
        )
    assert rows == {"here": "LOCAL", "there": "REMOTE"}
