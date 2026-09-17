"""Loads scenario declarations from `validation/scenarios/*.yaml`.

The third registry of exactly this shape, after employees and workflows, and the
sameness is deliberate: everything in this platform that a person writes by hand
is a file discovered from a directory, checked strictly at load, and named in the
error when it is wrong. A scenario that silently loads with half its
expectations would report a pass that means nothing, which is worse than an
employee doing so - a bad employee produces bad work somebody notices.

Scenarios live beside the prose reports rather than replacing them.
`validation/tasks/` is what happened and why, written for a reader;
`validation/scenarios/` is the request itself, written to be made again. Merging
them would mean either a report nobody can re-run or a declaration nobody can
read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from domain.errors import ConfigurationError, NotFoundError
from domain.validation.scenario import Entry, Expectations, Requirement, Scenario
from infrastructure.observability.logging import get_logger

log = get_logger(__name__)

DEFAULT_SCENARIOS_DIR = Path(__file__).resolve().parents[2] / "validation" / "scenarios"

KNOWN_FIELDS = frozenset(
    {
        "name",
        "description",
        "phase",
        "tags",
        "requires",
        "request",
        "employee",
        "workflow",
        "inputs",
        "setup",
        "reset",
        "approve",
        "thread",
        "workspace",
        "knowledge",
        "expect",
        "regression",
    }
)
KNOWN_EXPECT_FIELDS = frozenset(
    {
        "output_contains",
        "output_excludes",
        "files_exist",
        "file_contains",
        "tools_denied",
        "tools_forbidden",
        "min_employees",
        "max_cost_usd",
        "max_steps",
        "must_succeed",
    }
)


class YamlScenarioRegistry:
    """Implements `domain.validation.protocols.ScenarioRegistry`."""

    def __init__(self, directory: Path | None = None) -> None:
        self._directory = directory or DEFAULT_SCENARIOS_DIR
        self._loaded: dict[str, Scenario] | None = None

    @property
    def directory(self) -> Path:
        return self._directory

    def list_all(self) -> list[Scenario]:
        return sorted(self._all().values(), key=lambda scenario: (scenario.phase, scenario.name))

    def get(self, name: str) -> Scenario:
        scenario = self._all().get(name)
        if scenario is None:
            known = ", ".join(sorted(self._all())) or "none"
            raise NotFoundError(f"Unknown scenario: {name}. Declared here: {known}.")
        return scenario

    def reload(self) -> None:
        self._loaded = None

    def _all(self) -> dict[str, Scenario]:
        if self._loaded is None:
            self._loaded = self._discover()
        return self._loaded

    def _discover(self) -> dict[str, Scenario]:
        if not self._directory.is_dir():
            return {}
        found: dict[str, Scenario] = {}
        for path in sorted(self._directory.glob("*.yaml")):
            scenario = _load(path)
            found[scenario.name] = scenario
        log.info("scenarios.loaded", count=len(found), directory=str(self._directory))
        return found


def _load(path: Path) -> Scenario:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ConfigurationError(f"{path}: not valid YAML - {error}") from error
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{path}: expected a mapping at the top level.")

    unknown = sorted(set(raw) - KNOWN_FIELDS)
    if unknown:
        raise ConfigurationError(
            f"{path}: unknown field(s) {', '.join(unknown)}. "
            f"Known fields: {', '.join(sorted(KNOWN_FIELDS))}."
        )

    name = str(raw.get("name", "") or "").strip()
    # The file is the identity, as it is for a workflow. A scenario's name ends
    # up in a report that is compared across months; two files answering to one
    # name would make that comparison quietly wrong.
    if name != path.stem:
        raise ConfigurationError(
            f"{path}: name is '{name}' but the file is '{path.stem}.yaml'. They must match."
        )

    entry, target = _entry(path, raw)
    request = str(raw.get("request", "") or "").strip()
    if entry is not Entry.WORKFLOW and not request:
        raise ConfigurationError(f"{path}: 'request' is required and must have text.")

    thread = _strings(path, "thread", raw.get("thread"))
    if thread and entry is not Entry.OBJECTIVE:
        raise ConfigurationError(
            f"{path}: 'thread' is a conversation with Prometheus, so it needs a request "
            "to Prometheus rather than to an employee or a workflow."
        )

    phase = raw.get("phase", 0)
    if not isinstance(phase, int) or isinstance(phase, bool) or phase < 0:
        raise ConfigurationError(f"{path}: phase must be a whole number.")

    return Scenario(
        name=name,
        request=request,
        entry=entry,
        target=target,
        description=str(raw.get("description", "") or "").strip(),
        phase=phase,
        tags=_strings(path, "tags", raw.get("tags")),
        requires=_requirements(path, raw.get("requires")),
        setup=_files(path, "setup", raw.get("setup")),
        reset=_paths(path, raw.get("reset")),
        approve=_strings(path, "approve", raw.get("approve")),
        thread=thread,
        workspace=str(raw.get("workspace", "") or "").strip(),
        knowledge=_knowledge(path, raw.get("knowledge")),
        inputs=dict(raw.get("inputs") or {}),
        expect=_expectations(path, raw.get("expect")),
        regression=bool(raw.get("regression", False)),
    )


def _knowledge(path: Path, raw: Any) -> dict[str, dict[str, str]]:
    """Documents to add before the request, as workspace -> title -> text.

    Checked as strictly as `setup` is, and for the same reason: a scenario file
    is a declaration, and a mistyped shape has to be an error at load rather
    than an empty workspace that quietly makes the run pass.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{path}: knowledge must be workspace -> title -> text.")
    found: dict[str, dict[str, str]] = {}
    for workspace, documents in raw.items():
        if not isinstance(documents, dict) or not documents:
            raise ConfigurationError(
                f"{path}: knowledge['{workspace}'] must be title -> text."
            )
        found[str(workspace)] = {
            str(title): str(text) for title, text in documents.items() if str(text).strip()
        }
    return found


def _entry(path: Path, raw: dict[str, Any]) -> tuple[Entry, str]:
    employee = str(raw.get("employee", "") or "").strip()
    workflow = str(raw.get("workflow", "") or "").strip()
    if employee and workflow:
        raise ConfigurationError(
            f"{path}: name an employee or a workflow, not both - they are different doors."
        )
    if workflow:
        return Entry.WORKFLOW, workflow
    if employee:
        return Entry.TASK, employee
    return Entry.OBJECTIVE, ""


def _requirements(path: Path, raw: Any) -> tuple[Requirement, ...]:
    known = {member.value for member in Requirement}
    values = []
    for entry in _strings(path, "requires", raw):
        upper = entry.upper()
        if upper not in known:
            raise ConfigurationError(
                f"{path}: requires '{entry}' is not one of {', '.join(sorted(known))}."
            )
        values.append(Requirement(upper))
    return tuple(values)


def _paths(path: Path, raw: Any) -> tuple[str, ...]:
    """Workspace-relative paths to clear. Checked as strictly as `setup` is.

    An absolute path or a `..` here would delete something outside the folder
    the platform is confined to, from a file nobody reviews as code.
    """
    values = _strings(path, "reset", raw)
    for relative in values:
        if not relative or relative in (".", "/"):
            raise ConfigurationError(f"{path}: reset cannot name the workspace itself.")
        if relative.startswith("/") or ".." in Path(relative).parts:
            raise ConfigurationError(f"{path}: reset path '{relative}' leaves the workspace.")
    return values


def _strings(path: Path, field: str, raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw.strip(),)
    if not isinstance(raw, list) or not all(isinstance(entry, str) for entry in raw):
        raise ConfigurationError(f"{path}: {field} must be a list of strings.")
    return tuple(entry.strip() for entry in raw if entry.strip())


def _files(path: Path, field: str, raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{path}: {field} must be a mapping of path to text.")
    files: dict[str, str] = {}
    for key, value in raw.items():
        relative = str(key).strip()
        if relative.startswith("/") or ".." in Path(relative).parts:
            # The workspace is the boundary everywhere else; a scenario file is
            # not the place it stops being one.
            raise ConfigurationError(
                f"{path}: {field} path '{relative}' leaves the workspace."
            )
        files[relative] = str(value)
    return files


def _expectations(path: Path, raw: Any) -> Expectations:
    if raw is None:
        return Expectations()
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{path}: expect must be a mapping.")

    unknown = sorted(set(raw) - KNOWN_EXPECT_FIELDS)
    if unknown:
        raise ConfigurationError(
            f"{path}: unknown expectation(s) {', '.join(unknown)}. "
            f"Known: {', '.join(sorted(KNOWN_EXPECT_FIELDS))}."
        )

    cost = raw.get("max_cost_usd")
    if cost is not None and (not isinstance(cost, int | float) or isinstance(cost, bool)):
        raise ConfigurationError(f"{path}: max_cost_usd must be a number.")
    steps = raw.get("max_steps")
    if steps is not None and (not isinstance(steps, int) or isinstance(steps, bool) or steps < 1):
        raise ConfigurationError(f"{path}: max_steps must be a whole number of 1 or more.")
    people = raw.get("min_employees")
    if people is not None and (
        not isinstance(people, int) or isinstance(people, bool) or people < 1
    ):
        raise ConfigurationError(f"{path}: min_employees must be a whole number of 1 or more.")

    return Expectations(
        output_contains=_strings(path, "output_contains", raw.get("output_contains")),
        output_excludes=_strings(path, "output_excludes", raw.get("output_excludes")),
        files_exist=_strings(path, "files_exist", raw.get("files_exist")),
        file_contains=_files(path, "file_contains", raw.get("file_contains")),
        tools_denied=_strings(path, "tools_denied", raw.get("tools_denied")),
        tools_forbidden=_strings(path, "tools_forbidden", raw.get("tools_forbidden")),
        min_employees=people,
        max_cost_usd=float(cost) if cost is not None else None,
        max_steps=steps,
        must_succeed=bool(raw.get("must_succeed", True)),
    )
