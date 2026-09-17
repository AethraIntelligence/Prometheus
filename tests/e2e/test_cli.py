"""The Phase 1 Definition of Done: the CLI runs on a clean machine.

No Docker, no database server, no account.
"""

from __future__ import annotations

from typer.testing import CliRunner

from app.cli.main import app

runner = CliRunner()


def test_version_runs_without_any_infrastructure() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.startswith("prometheus ")


def test_config_masks_the_provider_key(monkeypatch) -> None:
    from app.config.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("PROMETHEUS_LLM_API_KEY", "super-secret")
    try:
        result = runner.invoke(app, ["config"])
        assert result.exit_code == 0
        assert "llm_api_key:   set" in result.stdout
        assert "super-secret" not in result.stdout
    finally:
        get_settings.cache_clear()


def test_tasks_reports_an_empty_local_database(monkeypatch, tmp_path) -> None:
    from app.config.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("PROMETHEUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(
        "PROMETHEUS_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'prometheus.db'}"
    )
    try:
        # The schema has to exist before the repository can read it.
        import asyncio

        from infrastructure.persistence.models import Base
        from infrastructure.persistence.session import create_engine

        async def _create() -> None:
            engine = create_engine(get_settings().resolved_database_url)
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            await engine.dispose()

        asyncio.run(_create())

        result = runner.invoke(app, ["tasks"])
        assert result.exit_code == 0
        assert "No resumable tasks." in result.stdout
    finally:
        get_settings.cache_clear()


def test_tasks_explains_a_missing_schema_instead_of_a_stack_trace(monkeypatch, tmp_path) -> None:
    from app.config.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("PROMETHEUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PROMETHEUS_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}")
    try:
        result = runner.invoke(app, ["tasks"])
        assert result.exit_code == 1
        assert "no schema yet" in result.output
        assert "alembic upgrade head" in result.output
    finally:
        get_settings.cache_clear()


def test_models_lists_the_catalog_and_its_defaults() -> None:
    result = runner.invoke(app, ["models"])
    assert result.exit_code == 0
    assert "default for" in result.output


def test_ask_without_a_key_explains_itself_instead_of_calling_out(monkeypatch) -> None:
    from app.config.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.delenv("PROMETHEUS_LLM_API_KEY", raising=False)
    try:
        result = runner.invoke(app, ["ask", "Which city?"])
        assert result.exit_code == 1
        assert "PROMETHEUS_LLM_API_KEY" in result.output
    finally:
        get_settings.cache_clear()


def test_spend_reports_zero_on_a_fresh_database(monkeypatch, tmp_path) -> None:
    import asyncio

    from app.config.settings import get_settings
    from infrastructure.persistence.models import Base
    from infrastructure.persistence.session import create_engine

    get_settings.cache_clear()
    monkeypatch.setenv("PROMETHEUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(
        "PROMETHEUS_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'prometheus.db'}"
    )
    try:

        async def _create() -> None:
            engine = create_engine(get_settings().resolved_database_url)
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            await engine.dispose()

        asyncio.run(_create())

        result = runner.invoke(app, ["spend"])
        assert result.exit_code == 0
        assert "calls:         0" in result.output
        assert "$0.000000" in result.output
    finally:
        get_settings.cache_clear()


def test_tools_shows_who_may_call_each_tool(monkeypatch, tmp_path) -> None:
    """Least privilege is printable: the user can see the grants, not trust them."""
    from app.config.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("PROMETHEUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PROMETHEUS_WORKSPACE_DIR", str(tmp_path / "workspace"))
    try:
        result = runner.invoke(app, ["tools"])
        assert result.exit_code == 0
        assert "fs.move" in result.output
        assert "organizer" in result.output
        # Since Phase 8 exactly one employee may run code, and the grant says
        # which. Before it, this line read "nobody" - the change is one
        # declaration, and it is visible here without reading any of them.
        code_line = next(line for line in result.output.splitlines() if "code.run" in line)
        assert "analyst" in code_line
    finally:
        get_settings.cache_clear()


def test_a_tool_no_one_declares_is_shown_as_reachable_by_nobody(monkeypatch, tmp_path) -> None:
    """The default that least privilege rests on, checked where it is visible."""
    from app.config.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("PROMETHEUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PROMETHEUS_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("PROMETHEUS_EMPLOYEES_DIR", str(tmp_path / "employees"))
    reader = tmp_path / "employees" / "reader"
    reader.mkdir(parents=True)
    (reader / "employee.yaml").write_text(
        "name: reader\nrole: Reader\nallowed_tools: [fs.read]\ncapabilities: [FILE_ACCESS]\n",
        encoding="utf-8",
    )
    try:
        result = runner.invoke(app, ["tools"])
        assert result.exit_code == 0
        code_line = next(line for line in result.output.splitlines() if "code.run" in line)
        assert "nobody" in code_line
    finally:
        get_settings.cache_clear()


def test_approvals_reports_an_empty_queue(monkeypatch, tmp_path) -> None:
    import asyncio

    from app.config.settings import get_settings
    from infrastructure.persistence.models import Base
    from infrastructure.persistence.session import create_engine

    get_settings.cache_clear()
    monkeypatch.setenv("PROMETHEUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(
        "PROMETHEUS_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'prometheus.db'}"
    )
    try:

        async def _create() -> None:
            engine = create_engine(get_settings().resolved_database_url)
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            await engine.dispose()

        asyncio.run(_create())

        result = runner.invoke(app, ["approvals"])
        assert result.exit_code == 0
        assert "Nothing is waiting for approval." in result.output
    finally:
        get_settings.cache_clear()


def test_an_id_that_is_not_an_id_is_reported_plainly(monkeypatch, tmp_path) -> None:
    from app.config.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("PROMETHEUS_DATA_DIR", str(tmp_path))
    try:
        result = runner.invoke(app, ["approve", "not-a-uuid"])
        assert result.exit_code == 1
    finally:
        get_settings.cache_clear()


def test_tools_shows_the_level_each_one_reaches_the_world_at(monkeypatch, tmp_path) -> None:
    """Phase 5: the hierarchy is not a claim in a prompt, it is printed per tool."""
    from app.config.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("PROMETHEUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PROMETHEUS_WORKSPACE_DIR", str(tmp_path / "workspace"))
    try:
        result = runner.invoke(app, ["tools"])
        assert result.exit_code == 0
        assert "COMPUTER_USE" in result.output
        assert "BROWSER" in result.output
        assert "operator" in result.output
    finally:
        get_settings.cache_clear()


def test_stop_can_be_pulled_and_released_from_a_second_terminal(
    monkeypatch, tmp_path
) -> None:
    """It is a file precisely so this works while the first terminal is busy."""
    from app.config.settings import get_settings
    from infrastructure.computer.stop import FileStopSignal

    get_settings.cache_clear()
    monkeypatch.setenv("PROMETHEUS_DATA_DIR", str(tmp_path))
    try:
        stopped = runner.invoke(app, ["stop", "--reason", "wrong window"])
        assert stopped.exit_code == 0
        signal = FileStopSignal(get_settings().stop_file_path)
        assert signal.engaged()
        assert signal.reason == "wrong window"

        released = runner.invoke(app, ["stop", "--clear"])
        assert released.exit_code == 0
        assert not signal.engaged()

        again = runner.invoke(app, ["stop", "--clear"])
        assert "was not stopped" in again.output
    finally:
        get_settings.cache_clear()


def test_employees_lists_every_declaration(monkeypatch, tmp_path) -> None:
    """It crashed on an attribute employees never had; type checking found it (Phase 13)."""
    from app.config.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("PROMETHEUS_DATA_DIR", str(tmp_path))
    try:
        result = runner.invoke(app, ["employees"])
        assert result.exit_code == 0, result.output
        assert "organizer" in result.stdout
    finally:
        get_settings.cache_clear()
