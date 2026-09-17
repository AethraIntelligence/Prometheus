"""What infrastructure needs from configuration, and nothing more.

`Settings` itself lives in `app/config/`, which is the composition root's
business. Infrastructure states its requirement as a Protocol instead of
importing upwards, so the layering rule holds: `infrastructure -> domain` only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class RuntimeSettings(Protocol):
    data_dir: Path
    log_level: str
    log_format: str

    # --- Model access ---------------------------------------------------------
    llm_api_key: str | None
    llm_base_url: str
    model_catalog_path: Path | None
    employees_dir: Path | None
    workflows_dir: Path | None
    plugins_dir: Path | None
    plugin_credentials: dict[str, str]
    scenarios_dir: Path | None
    llm_timeout_seconds: float | None
    llm_retry_attempts: int
    local_llm_base_url: str
    local_llm_autostart: bool
    local_models_only: bool
    knowledge_min_similarity: float

    # --- Tools ----------------------------------------------------------------
    approval_mode: str
    approval_ttl_seconds: float
    browser_headless: bool
    browser_timeout_seconds: float
    #: How long an integration's server may take to answer one call.
    integration_timeout_seconds: float
    code_timeout_seconds: float
    computer_allowed_applications: tuple[str, ...]
    computer_allowed_region: str | None
    computer_max_actions: int

    # --- Workspaces -----------------------------------------------------------
    #: Which workspace this machine works in when the switch file says nothing.
    active_workspace: str

    # --- Memory ---------------------------------------------------------------
    memory_recall_limit: int
    memory_consolidation_threshold: int

    # --- Knowledge ------------------------------------------------------------
    knowledge_recall_limit: int

    @property
    def resolved_database_url(self) -> str: ...

    @property
    def storage_backend(self) -> str: ...

    @property
    def resolved_file_root(self) -> Path: ...

    @property
    def workspace_roots_dir(self) -> Path: ...

    @property
    def active_workspace_path(self) -> Path: ...

    @property
    def browser_tools_enabled(self) -> bool: ...

    @property
    def code_execution_enabled(self) -> bool: ...

    @property
    def approvals_enabled(self) -> bool: ...

    @property
    def workflows_enabled(self) -> bool: ...

    @property
    def computer_use_enabled(self) -> bool: ...

    @property
    def integrations_enabled(self) -> bool: ...

    @property
    def memory_enabled(self) -> bool: ...

    @property
    def knowledge_enabled(self) -> bool: ...

    @property
    def stop_file_path(self) -> Path: ...

    @property
    def secret_backend(self) -> str: ...

    def ensure_data_dir(self) -> Path: ...

    def ensure_file_root(self) -> Path: ...
