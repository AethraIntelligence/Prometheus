"""What the container builds for computer use, and - mostly - what it does not.

The flag is the Definition of Done of this phase, so it is checked at the level
where it is actually enforced: whether the tool exists at all. A tool that is
not registered cannot be listed, cannot be granted to an employee, and cannot be
called - which is a stronger guarantee than a check inside the tool.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from domain.errors import DependencyNotConfiguredError
from domain.policies.models import ActorKind, SimpleActor
from infrastructure.container import Container
from tests.fakes.browser import FakeSearchEngine
from tests.fakes.computer import FakeScreenReader

EVERYTHING = SimpleActor("test", ActorKind.USER, frozenset({"*"}))


class Stub:
    """Anything satisfying `RuntimeSettings`; no environment involved."""

    def __init__(self, root: Path, *, browser: bool = True, computer: bool = False) -> None:
        self.data_dir = root
        self.log_level = "INFO"
        self.log_format = "json"
        self._browser = browser
        self._computer = computer
        self.approval_mode = "deny"
        self.approval_ttl_seconds = 0.0
        self.browser_headless = True
        self.browser_timeout_seconds = 5.0
        self.code_timeout_seconds = 5.0
        self.code_memory_mb = 512
        self.code_disk_mb = 64
        self.code_max_output_chars = 20_000
        self.computer_allowed_applications: tuple[str, ...] = ()
        self.computer_allowed_region: str | None = None
        self.computer_max_actions = 50
        self.active_workspace = "default"

    @property
    def resolved_database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.data_dir / 'prometheus.db'}"

    @property
    def resolved_file_root(self) -> Path:
        return self.data_dir / "workspace"

    @property
    def browser_tools_enabled(self) -> bool:
        return self._browser

    @property
    def code_execution_enabled(self) -> bool:
        return False

    @property
    def approvals_enabled(self) -> bool:
        return False

    @property
    def computer_use_enabled(self) -> bool:
        return self._computer

    @property
    def workspace_roots_dir(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def active_workspace_path(self) -> Path:
        return self.data_dir / "ACTIVE_WORKSPACE"

    @property
    def stop_file_path(self) -> Path:
        return self.data_dir / "STOP"

    def ensure_data_dir(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir

    def ensure_file_root(self) -> Path:
        self.resolved_file_root.mkdir(parents=True, exist_ok=True)
        return self.resolved_file_root


def container(settings: Stub) -> Container:
    built = Container(settings, in_memory=True)
    built.use_screen_reader(FakeScreenReader)
    # Kept off the network: the real one would try to reach a search engine.
    built.__dict__["search_engine"] = FakeSearchEngine()
    return built


def names(built: Container) -> set[str]:
    return {spec.name for spec in built.tool_registry.list_specs(EVERYTHING)}


def test_the_desktop_is_absent_until_the_flag_turns_it_on(tmp_path: Path) -> None:
    offered = names(container(Stub(tmp_path, computer=False)))

    assert "computer.click" in offered, "the page surface comes with the browser"
    assert "desktop.click" not in offered


def test_the_flag_registers_the_desktop_alongside_the_page(tmp_path: Path) -> None:
    offered = names(container(Stub(tmp_path, computer=True)))

    assert {"computer.click", "desktop.click"} <= offered


def test_with_the_browser_off_there_is_no_page_to_drive(tmp_path: Path) -> None:
    offered = names(container(Stub(tmp_path, browser=False, computer=False)))

    assert not {name for name in offered if name.startswith(("computer.", "desktop."))}
    assert {"fs.read", "fs.write"} <= offered, "the API rung is untouched"


def test_nothing_about_a_screen_is_built_by_listing_the_registry(tmp_path: Path) -> None:
    """`prometheus tools` must not route a model, or it fails without a provider key."""
    built = Container(Stub(tmp_path, computer=True), in_memory=True)
    built.use_screen_reader(FakeScreenReader)
    built.__dict__["search_engine"] = FakeSearchEngine()

    names(built)

    assert "screen_reader" not in built.__dict__


def test_without_a_screen_reader_the_screens_are_simply_not_offered(
    tmp_path: Path,
) -> None:
    """The composition root supplies it; a container built raw has no eyes."""
    built = Container(Stub(tmp_path, computer=True), in_memory=True)
    built.__dict__["search_engine"] = FakeSearchEngine()

    offered = {spec.name for spec in built.tool_registry.list_specs(EVERYTHING)}

    assert not {name for name in offered if name.startswith(("computer.", "desktop."))}
    with pytest.raises(DependencyNotConfiguredError):
        _ = built.screen_reader


def test_the_desktop_is_confined_to_the_applications_that_were_named(
    tmp_path: Path,
) -> None:
    settings = Stub(tmp_path, computer=True)
    settings.computer_allowed_applications = ("Preview",)
    settings.computer_allowed_region = "800x600+0+0"

    guard = container(settings).desktop_computer

    assert guard.constraints.allowed_applications == frozenset({"Preview"})
    assert str(guard.constraints.allowed_region) == "800x600+0+0"
    assert guard.constraints.applies_to_applications


def test_the_page_surface_is_not_asked_to_name_an_application(tmp_path: Path) -> None:
    guard = container(Stub(tmp_path)).browser_computer

    assert not guard.constraints.applies_to_applications
