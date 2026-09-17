"""Local-first configuration.

Provider keys and paths, and nothing else. Anything that would only make sense
with a server behind it does not belong here.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from app.config import editable
from app.config.feature_flags import FeatureFlags
from domain.workspace.models import DEFAULT_WORKSPACE_ID


def _default_data_dir() -> Path:
    return Path.home() / ".prometheus"


class WindowSettingsSource(PydanticBaseSettingsSource):
    """What a person saved from Settings -> General, read on every start.

    It ranks below the process environment and above `.env`. Below the
    environment, because a variable set in a shell is the most specific thing
    anybody said about this one process, and the window shows such a setting as
    locked rather than overriding it. Above `.env`, because the window is where
    a person changes things *now*: a switch they turned off that came back on
    because a file they never opened says otherwise is a switch that lies.

    The file sits beside the database, so where it is follows `data_dir` - which
    is itself resolved from the sources above this one, never from the file.
    """

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        *,
        ahead: tuple[PydanticBaseSettingsSource, ...],
    ) -> None:
        super().__init__(settings_cls)
        self._ahead = ahead

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def path(self) -> Path:
        for source in self._ahead:
            named = source().get("data_dir")
            if named:
                return Path(named).expanduser() / editable.FILE_NAME
        return _default_data_dir() / editable.FILE_NAME

    def __call__(self) -> dict[str, Any]:
        try:
            stored = json.loads(self.path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            # A damaged file must not stop the platform from starting: the
            # window is where it would be repaired, and that needs a runtime.
            return {}
        return editable.only_editable(stored)


def normalise_database_url(url: str) -> str:
    """The URL a person pasted, with the driver this platform speaks.

    Everything here is async, so a bare `postgresql://` - what every hosting
    panel hands out - has to become `postgresql+asyncpg://` before an engine
    can be built from it. Done once, here, rather than in each of the three
    places a URL is read.
    """
    for prefix in ("postgresql+asyncpg://", "sqlite+aiosqlite://"):
        if url.startswith(prefix):
            return url
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    if url.startswith("sqlite://"):
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="PROMETHEUS_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # --- Paths ---------------------------------------------------------------
    data_dir: Path = Field(default_factory=_default_data_dir)
    #: Where everything is kept. Unset means the SQLite file in `data_dir`,
    #: which is what `clone && run` gets and what the packaged window needs.
    #: A PostgreSQL URL - including a Supabase one, which is the same thing -
    #: puts the whole store on a server instead. `prometheus storage migrate --to`
    #: is how the data follows.
    database_url: str | None = None

    # --- Provider access -----------------------------------------------------
    llm_api_key: str | None = None
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_default_model: str = "anthropic/claude-sonnet-5"
    #: Override the bundled model catalog. Changing models is a config change.
    model_catalog_path: Path | None = None
    #: Where employee declarations are discovered. Adding an employee is adding
    #: a directory here, and nothing else.
    employees_dir: Path | None = None
    #: How long to wait on a model. Unset means each provider's own default,
    #: which differs for a reason: a hosted model that has not answered in two
    #: minutes is not going to, and a model running on this laptop is often only
    #: halfway through. Set it when you know better than both.
    llm_timeout_seconds: float | None = None
    llm_retry_attempts: int = 3
    #: Where a locally served model listens. Used only by the 'local' provider.
    local_llm_base_url: str = "http://127.0.0.1:11434/v1"
    #: Whether a call to a local model starts Ollama, hidden, when nothing
    #: answers at the address above. Nothing is started until a local model is
    #: actually used. Off for a machine that runs its model server some other
    #: way and wants to see it missing.
    local_llm_autostart: bool = True

    # --- Tools ---------------------------------------------------------------
    #: The one directory the filesystem tools can see. Point it at the folder
    #: the work is actually in; nothing outside it is reachable. It belongs to
    #: the *first* workspace: another workspace gets its own root, so that
    #: switching moves what an employee can read (§15.3).
    #:
    #: `PROMETHEUS_WORKSPACE_DIR` still sets it. The name was one of three things
    #: the word "workspace" meant, and this is the one that lost the argument -
    #: but an installation that put it in an `.env` file a year ago should not
    #: silently start writing somewhere else.
    file_root: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("PROMETHEUS_FILE_ROOT", "PROMETHEUS_WORKSPACE_DIR"),
    )
    #: Which workspace this machine works in when nothing says otherwise. The
    #: switch itself is a file beside the database, written by
    #: `prometheus workspace use`; this is what an installation that has never
    #: switched gets, and what a fresh one starts in.
    active_workspace: str = str(DEFAULT_WORKSPACE_ID)
    #: Where workflow declarations are read from. None means the ones that
    #: ship with the platform, the same way employees are found.
    workflows_dir: Path | None = None
    #: Where plugin declarations are read from - the services Settings ->
    #: Plugins offers to install. None means the ones that ship.
    plugins_dir: Path | None = None
    #: Credentials this installation supplies to plugins that ask for them by
    #: name, so a person does not have to - the platform's own Google OAuth
    #: client, for one. Set as `PROMETHEUS_PLUGIN_CREDENTIALS__<NAME>=value` in
    #: `.env`, never in the repository: a client secret in public source is
    #: found and flagged within hours. A credential a person stored under the
    #: same name wins over this one.
    plugin_credentials: dict[str, str] = Field(default_factory=dict)
    #: Where validation scenarios are read from. Same rule again: the real work
    #: the platform is measured on is a directory of files, not a fixture.
    scenarios_dir: Path | None = None
    #: prompt | deny | allow. What happens when an irreversible action comes up:
    #: ask the person at the terminal, refuse, or - only if explicitly set -
    #: proceed. A run with nobody watching refuses whatever this says.
    approval_mode: Literal["prompt", "deny", "allow"] = "prompt"
    #: How long an unanswered approval stays worth answering. A question nobody
    #: came back to is closed as EXPIRED rather than left pending forever,
    #: because a pending row is a task that `prometheus resume` will keep picking up.
    #: Zero disables the deadline, which is what a terminal prompt wants: the
    #: person is standing there.
    approval_ttl_seconds: float = 900.0
    browser_headless: bool = True
    browser_timeout_seconds: float = 30.0
    #: How long an integration's server may take to answer one call. A local
    #: subprocess answers in milliseconds; this is where slow has become gone.
    integration_timeout_seconds: float = 30.0
    code_timeout_seconds: float = 30.0
    #: Generated code is fail-closed and uses the same Docker boundary on every
    #: platform. A machine without a running engine and the sandbox image never
    #: receives a weaker plain-process fallback.
    code_memory_mb: int = Field(default=512, ge=32, le=16_384)
    code_disk_mb: int = Field(default=64, ge=1, le=4_096)
    code_max_output_chars: int = Field(default=20_000, ge=1_000, le=1_000_000)

    # --- Computer use --------------------------------------------------------
    #: Applications the desktop surface may act in. Empty means none: acting on
    #: the machine is opt-in per application, the way the filesystem tools are
    #: opt-in per directory. Ignored by the browser surface, which has no
    #: applications to choose between.
    computer_allowed_applications: tuple[str, ...] = ()
    #: The part of the screen that may be touched, as "WIDTHxHEIGHT+X+Y".
    #: Unset means the whole of it.
    computer_allowed_region: str | None = None
    #: A budget for actions on a screen, separate from the run's step limit:
    #: one step of the loop can ask for several clicks.
    computer_max_actions: int = 200

    # --- Memory --------------------------------------------------------------
    #: How many recollections a run is given. Context is the scarcest thing in a
    #: run, and the seventh-best memory costs the same tokens as the best one.
    memory_recall_limit: int = 6
    #: How many episodes accumulate before the oldest are folded into one. Below
    #: this, recall's own ranking filters better than a summary would.
    memory_consolidation_threshold: int = 12

    # --- Knowledge -----------------------------------------------------------
    #: How many passages of the user's own documents a run is given. Smaller
    #: than it could be: a passage is a page of text, and four of them beside
    #: the goal, the assignment and what was recalled is already most of what a
    #: run can carry.
    knowledge_recall_limit: int = 4
    #: The cosine similarity below which a passage is not the answer to a
    #: question. It depends on the embedding model: measured on bge-m3 and
    #: nomic-embed-text here, unrelated questions reached 0.51 and relevant
    #: ones started at 0.56. Changing the model is a reason to measure again.
    knowledge_min_similarity: float = 0.53

    # --- Local interface -----------------------------------------------------
    #: The loopback address, and not configurable to anything else by accident.
    #: This interface starts tasks and approves irreversible actions; it has no
    #: authentication because it is not reachable, and binding it elsewhere
    #: would quietly turn a local tool into an unauthenticated remote one.
    ui_host: str = "127.0.0.1"
    ui_port: int = 8765
    #: How long an irreversible action waits for someone to answer in the
    #: interface before it is refused.
    ui_approval_timeout_seconds: float = 600.0
    #: How many past tasks the history list loads.
    ui_history_limit: int = 50

    # --- Work that starts on its own -----------------------------------------
    #: How often `prometheus serve` looks for a schedule that is due. Well under
    #: the shortest interval a schedule may declare, and far enough above zero
    #: that an idle machine is idle.
    scheduler_tick_seconds: float = 30.0

    # --- Runtime -------------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    #: Language the agents answer the user in. Response language is configuration,
    #: never text hard-coded into the sources.
    response_language: str = "en"

    flags: FeatureFlags = Field(default_factory=FeatureFlags)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        window = WindowSettingsSource(
            settings_cls, ahead=(init_settings, env_settings, dotenv_settings)
        )
        return init_settings, env_settings, window, dotenv_settings, file_secret_settings

    @property
    def window_settings_path(self) -> Path:
        """Where Settings -> General keeps what a person chose."""
        return self.data_dir / editable.FILE_NAME

    @field_validator("data_dir", "file_root", mode="after")
    @classmethod
    def _expand_home(cls, value: Path | None) -> Path | None:
        """`~/.prometheus` in the environment means the home directory, not a directory called `~`.

        A shell expands the tilde before the process sees it, but an `.env` file
        is read by this process, so `PROMETHEUS_DATA_DIR=~/.prometheus` arrives
        literally - and without this the platform quietly writes its database
        into a directory named `~` beside whatever the working directory was.
        """
        return value if value is None else Path(value).expanduser()

    @property
    def resolved_file_root(self) -> Path:
        """Where the first workspace's files live, separate from the platform's own.

        `~/Documents/Prometheus` on an installation that keeps its data where
        the platform does by default: what the work produces is the person's,
        and a hidden directory beside the database is not where anybody looks
        for a report. A data directory pointed elsewhere - a test, a second
        installation - keeps its files beside it, so it never writes into the
        documents of whoever is running it.
        """
        if self.file_root:
            return self.file_root
        if self.data_dir == _default_data_dir():
            return Path.home() / "Documents" / "Prometheus"
        return self.data_dir / "workspace"

    @property
    def workspace_roots_dir(self) -> Path:
        """Where a workspace that was created later keeps its files.

        Beside the database rather than under the first workspace's root: a
        workspace nested inside another is one the other's employees can read,
        and an isolation the file tools do not enforce is not one.
        """
        return self.data_dir / "workspaces"

    @property
    def active_workspace_path(self) -> Path:
        """Which workspace this machine is in. A file, like the stop signal."""
        return self.data_dir / "ACTIVE_WORKSPACE"

    @property
    def browser_tools_enabled(self) -> bool:
        return self.flags.browser_tools

    @property
    def code_execution_enabled(self) -> bool:
        return self.flags.code_execution

    @property
    def approvals_enabled(self) -> bool:
        return self.flags.approvals

    @property
    def workflows_enabled(self) -> bool:
        return self.flags.workflows

    @property
    def scheduler_enabled(self) -> bool:
        """Off unless asked for, and that is the correct default (§12.9).

        Everything else here starts because somebody typed something. This
        starts on its own, with nobody at the keyboard to answer the approval
        gate - so a machine that has not opted in never runs work nobody asked
        for on the day it was installed.
        """
        return self.flags.scheduler

    @property
    def computer_use_enabled(self) -> bool:
        """Phase 5's Definition of Done rests on this being a switch.

        Off, the computer tools are not registered at all - and every scenario
        with an API or a browser path keeps working, because those are different
        tools at a different level of the hierarchy.
        """
        return self.flags.computer_use

    @property
    def integrations_enabled(self) -> bool:
        return self.flags.integrations

    @property
    def memory_enabled(self) -> bool:
        """Off, employees start every task knowing only what they were told.

        Which is Phase 8's behaviour exactly - nothing else changes, because
        nothing above the container knows whether there is a memory behind the
        contract.
        """
        return self.flags.memory

    @property
    def knowledge_enabled(self) -> bool:
        """Off, a run knows only what it was told and what it remembers."""
        return self.flags.knowledge

    @property
    def stop_file_path(self) -> Path:
        """The brake. `prometheus stop` writes it; every screen action reads it."""
        return self.data_dir / "STOP"

    def ensure_file_root(self) -> Path:
        directory = self.resolved_file_root
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @property
    def db_path(self) -> Path:
        return self.data_dir / "prometheus.db"

    @property
    def resolved_database_url(self) -> str:
        """Where everything is kept. SQLite by default (§3, ADR 0017).

        One database for all of it - tasks, objectives, plans, memory,
        knowledge, audit - because two stores can disagree about what happened
        and would need two migrations instead of one.

        A `postgresql://` or `postgres://` URL is normalised to the async driver
        rather than refused: that is what a person copies out of Supabase or a
        hosting panel, and failing on it would be the platform being right about
        a driver name at the user's expense.
        """
        if not self.database_url:
            return f"sqlite+aiosqlite:///{self.db_path}"
        return normalise_database_url(self.database_url)

    @property
    def storage_backend(self) -> str:
        """Which backend this installation is on, as a word for a person.

        Supabase is not a third answer: it is PostgreSQL with a different
        connection string, and treating it as its own backend would mean
        maintaining two implementations of one dialect (ADR 0017).
        """
        return "postgres" if "postgres" in self.resolved_database_url else "sqlite"

    def ensure_data_dir(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
