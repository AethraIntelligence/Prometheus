"""Which settings a person may change from a window, and how each is shown.

A list, not every field of `Settings`. Left out on purpose: provider keys and
endpoints (Settings -> Providers owns them, and a key belongs in the credential
store rather than a plain file), paths and the database URL (moving the store is
`prometheus storage-migrate`, which copies and verifies; a text field would
simply point the platform at an empty database), and the interface's host and
port (a window that can rebind the server it talks to can lock itself out, or
publish an unauthenticated surface to a network - ADR 0006).

The text is written for the person reading the screen, not for the source: the
docstrings in `settings.py` say why a default is what it is.
"""

from __future__ import annotations

from dataclasses import dataclass

from domain.configuration.models import SettingKind

#: The file a window's choices are kept in, beside the database. A JSON object
#: shaped like `Settings`: `{"approval_mode": "deny", "flags": {"memory": false}}`.
FILE_NAME = "settings.json"


@dataclass(frozen=True, slots=True)
class Editable:
    key: str
    group: str
    label: str
    help: str
    kind: SettingKind
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    optional: bool = False

    @property
    def path(self) -> tuple[str, ...]:
        return tuple(self.key.split("."))

    @property
    def environment_variable(self) -> str:
        return "PROMETHEUS_" + "__".join(part.upper() for part in self.path)


BOOL, CHOICE, INT, NUM, TEXT, LIST = (
    SettingKind.BOOLEAN,
    SettingKind.CHOICE,
    SettingKind.INTEGER,
    SettingKind.NUMBER,
    SettingKind.TEXT,
    SettingKind.LIST,
)

EDITABLE: tuple[Editable, ...] = (
    # --- What the platform may do at all ----------------------------------------
    Editable("flags.browser_tools", "Capabilities", "Web browser",
             "Employees may open and read pages in a real browser.", BOOL),
    Editable("flags.code_execution", "Capabilities", "Running code",
             "Employees may write and run scripts on this machine.", BOOL),
    Editable("flags.computer_use", "Capabilities", "Desktop control",
             "Employees may look at and click on the desktop. Only the applications "
             "listed under Desktop control are reachable, and every action waits for you.", BOOL),
    Editable("flags.approvals", "Capabilities", "Approvals",
             "Risky actions stop and ask a person before they run.", BOOL),
    Editable("flags.memory", "Capabilities", "Memory",
             "Runs remember what earlier ones learned and how you want work done.", BOOL),
    Editable("flags.knowledge", "Capabilities", "Documents",
             "Documents you add are searched and quoted in answers.", BOOL),
    Editable("flags.integrations", "Capabilities", "Plugins and integrations",
             "Connected services (MCP servers, plugins) become tools employees can be granted.",
             BOOL),
    Editable("flags.workflows", "Capabilities", "Workflows",
             "Predefined processes under workflows/ can be run.", BOOL),
    Editable("flags.scheduler", "Capabilities", "Scheduled work",
             "Schedules and events start work on their own, with nobody at the keyboard. "
             "Anything risky is refused while nobody is there to approve it.", BOOL),
    # --- Approvals ------------------------------------------------------------
    Editable("approval_mode", "Approvals", "When nobody can be asked",
             "What an irreversible action does in the terminal: ask (prompt), refuse (deny) "
             "or go ahead (allow). A run with nobody watching refuses whatever this says.",
             CHOICE, choices=("prompt", "deny", "allow")),
    Editable("ui_approval_timeout_seconds", "Approvals", "Wait for an answer in the window",
             "Seconds an action waits for you to answer before it is refused.", NUM, minimum=1),
    Editable("approval_ttl_seconds", "Approvals", "Close unanswered approvals after",
             "Seconds before an unanswered question is closed as expired. 0 waits forever.",
             NUM, minimum=0),
    # --- Answers and models ----------------------------------------------------
    Editable("response_language", "Answers", "Answer in",
             "The language Prometheus answers you in, as a code such as en, uk or de.", TEXT),
    Editable("local_llm_autostart", "Models", "Start the local model server",
             "Start Ollama, hidden, the first time a local model is used and nothing answers.",
             BOOL),
    Editable("llm_retry_attempts", "Models", "Retries after a provider error",
             "How many times a transient provider error is retried.", INT, minimum=0),
    Editable("llm_timeout_seconds", "Models", "Model timeout",
             "Seconds to wait on a model. Empty uses each provider's own default: "
             "two minutes hosted, ten on this machine.", NUM, minimum=1, optional=True),
    # --- Tools ------------------------------------------------------------------
    Editable("browser_headless", "Tools", "Hide the browser window",
             "Run the browser without showing its window.", BOOL),
    Editable("browser_timeout_seconds", "Tools", "Browser timeout",
             "Seconds a page may take to load or respond.", NUM, minimum=1),
    Editable("code_timeout_seconds", "Tools", "Code timeout",
             "Seconds a script may run before it is stopped.", NUM, minimum=1),
    Editable("integration_timeout_seconds", "Tools", "Integration timeout",
             "Seconds a connected service may take to answer one call.", NUM, minimum=1),
    # --- Desktop control ----------------------------------------------------------
    Editable("computer_allowed_applications", "Desktop control", "Allowed applications",
             "Applications the desktop tools may act in, one per line. Empty means none.", LIST),
    Editable("computer_allowed_region", "Desktop control", "Allowed screen region",
             "The part of the screen that may be touched, as WIDTHxHEIGHT+X+Y. "
             "Empty means the whole screen.", TEXT, optional=True),
    Editable("computer_max_actions", "Desktop control", "Actions per run",
             "The most clicks and keystrokes one run may make on a screen.", INT, minimum=1),
    # --- Memory and documents -------------------------------------------------------
    Editable("memory_recall_limit", "Memory and documents", "Memories per run",
             "How many recollections a run starts with.", INT, minimum=0),
    Editable("memory_consolidation_threshold", "Memory and documents",
             "Summarise past outcomes after",
             "How many past outcomes accumulate before the oldest are folded into one.",
             INT, minimum=2),
    Editable("knowledge_recall_limit", "Memory and documents", "Passages per run",
             "How many passages of your documents a run is given.", INT, minimum=0),
    Editable("knowledge_min_similarity", "Memory and documents", "Minimum similarity",
             "How close in meaning a passage must be to count (0 to 1). "
             "Depends on the embedding model; measure again after changing it.",
             NUM, minimum=0),
    # --- Runtime ----------------------------------------------------------------------
    Editable("ui_history_limit", "Runtime", "History length",
             "How many past tasks the history loads.", INT, minimum=1),
    Editable("scheduler_tick_seconds", "Runtime", "Look for due schedules every",
             "Seconds between checks for a schedule that is due.", NUM, minimum=1),
    Editable("log_level", "Runtime", "Log level", "How much the runtime writes to its log.",
             CHOICE, choices=("DEBUG", "INFO", "WARNING", "ERROR")),
    Editable("log_format", "Runtime", "Log format",
             "json for tools that read logs, console for people.", CHOICE,
             choices=("json", "console")),
)

BY_KEY = {entry.key: entry for entry in EDITABLE}


def only_editable(stored: object) -> dict[str, object]:
    """The part of a saved file that is on the list, and nothing else.

    A hand-edited file naming `database_url` must not move the store from under
    a person who never saw that screen, so what is not listed is not read.
    """
    if not isinstance(stored, dict):
        return {}
    kept: dict[str, object] = {}
    for entry in EDITABLE:
        node: object = stored
        for part in entry.path:
            if not isinstance(node, dict) or part not in node:
                break
            node = node[part]
        else:
            target = kept
            for part in entry.path[:-1]:
                target = target.setdefault(part, {})  # type: ignore[assignment]
            target[entry.path[-1]] = node
    return kept
