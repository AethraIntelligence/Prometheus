"""Loads plugin declarations from `plugins/<id>/plugin.yaml`.

The fourth registry of this shape, after employees, workflows and scenarios, and
held to the same rule: adding a plugin is adding a directory - `plugin.yaml` and,
if it has one, `icon.svg` beside it. If that ever requires touching Python, this
is the file that would have to change.

**A plugin the lock does not describe is not offered** (Phase 13).
`catalog.lock.json` beside the declarations holds a digest of each plugin's
files and the pinned artifact its command runs; a declaration that is not in the
lock, whose files changed since it was locked, or whose arguments do not run
exactly the locked artifact is skipped - and cannot be installed, because
installing starts from this catalog (`domain/integrations/provenance.py`).

It differs from the others in one decision. A malformed employee stops the
runtime, because a workforce that silently lost a member plans wrongly; a
malformed plugin is skipped and logged, because a catalog is a shop window and
one broken tile must not empty it. The file is named in the log line, and
`tests/unit/test_plugin_catalog.py` loads every shipped plugin strictly, so a
broken one fails the build rather than a person's afternoon.

The icon is read as path data only. `viewBox` and each `<path d>` are kept and
everything else in the file - scripts, styles, links, other elements - is
dropped, so an icon file is a drawing and cannot be anything else.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import yaml

from domain.capabilities.models import Capability
from domain.errors import ConfigurationError
from domain.integrations.catalog import (
    FieldKind,
    Plugin,
    PluginField,
    PluginIcon,
    PluginRuntime,
    PluginSignIn,
    SetupStep,
)
from domain.integrations.provenance import PluginArtifact, declaration_digest, pinned_in
from domain.policies.risk import Effect
from infrastructure.observability.logging import get_logger

log = get_logger(__name__)

DEFAULT_PLUGINS_DIR = Path(__file__).resolve().parents[2] / "plugins"
LOCK_NAME = "catalog.lock.json"

KNOWN_FIELDS = frozenset(
    {
        "id", "name", "description", "about", "category", "publisher", "homepage",
        "popular", "runtime", "command", "args", "settings", "capabilities",
        "effects", "icon", "env", "setup", "sign_in",
    }
)
KNOWN_SIGN_IN_FIELDS = frozenset({"label", "tool", "arguments", "help"})
KNOWN_SETTING_FIELDS = frozenset(
    {"key", "label", "kind", "required", "placeholder", "help", "help_url"}
)
KNOWN_ICON_FIELDS = frozenset({"color", "background"})

#: An id is a tool-name prefix and a routing term: short, lower-case, no dots.
ID = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
#: Path data is numbers, commands, separators - nothing else is drawing.
PATH_DATA = re.compile(r"^[MmZzLlHhVvCcSsQqTtAaEe0-9.,+\-\s]+$")


class YamlPluginCatalog:
    """Implements `domain.integrations.catalog.PluginCatalog`."""

    def __init__(self, directory: Path | None = None, *, strict: bool = False) -> None:
        self._directory = directory or DEFAULT_PLUGINS_DIR
        self._strict = strict
        self._loaded: dict[str, Plugin] | None = None

    @property
    def directory(self) -> Path:
        return self._directory

    def list(self) -> list[Plugin]:
        return sorted(self._all().values(), key=lambda plugin: plugin.name.lower())

    def get(self, plugin_id: str) -> Plugin | None:
        return self._all().get(plugin_id)

    def reload(self) -> None:
        self._loaded = None

    def _all(self) -> dict[str, Plugin]:
        if self._loaded is None:
            self._loaded = self._discover()
        return self._loaded

    def _discover(self) -> dict[str, Plugin]:
        if not self._directory.is_dir():
            return {}
        lock = self._lock()
        found: dict[str, Plugin] = {}
        for path in sorted(self._directory.glob("*/plugin.yaml")):
            try:
                plugin = verified(load(path), path.parent, lock)
            except ConfigurationError as error:
                if self._strict:
                    raise
                log.warning("plugins.not_loaded", path=str(path), error=str(error))
                continue
            found[plugin.id] = plugin
        return found

    def _lock(self) -> dict[str, dict[str, Any]]:
        path = self._directory / LOCK_NAME
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if self._strict:
                raise ConfigurationError(f"{path}: the plugin catalog has no lock.") from None
            log.warning("plugins.no_lock", path=str(path))
            return {}
        except (OSError, ValueError) as error:
            if self._strict:
                raise ConfigurationError(f"{path}: cannot be read: {error}") from error
            log.warning("plugins.lock_unreadable", path=str(path), error=str(error))
            return {}
        if not isinstance(raw, dict) or raw.get("format_version") != 1:
            if self._strict:
                raise ConfigurationError(f"{path}: not a lock this version can read.")
            return {}
        plugins = raw.get("plugins")
        return plugins if isinstance(plugins, dict) else {}


def plugin_files(directory: Path) -> list[tuple[str, bytes]]:
    return [
        (item.name, item.read_bytes()) for item in sorted(directory.iterdir()) if item.is_file()
    ]


def verified(plugin: Plugin, directory: Path, lock: dict[str, dict[str, Any]]) -> Plugin:
    """The plugin with its locked artifact, or a refusal naming what does not match."""
    entry = lock.get(plugin.id)
    if not isinstance(entry, dict):
        raise ConfigurationError(f"{directory}: '{plugin.id}' is not in the catalog lock.")
    if entry.get("declaration_sha256") != declaration_digest(plugin_files(directory)):
        raise ConfigurationError(
            f"{directory}: '{plugin.id}' changed since it was locked; it is not offered."
        )
    raw_artifact = entry.get("artifact")
    if raw_artifact is None:
        return plugin
    try:
        artifact = PluginArtifact.from_dict(raw_artifact)
    except (KeyError, ValueError, TypeError) as error:
        raise ConfigurationError(f"{directory}: the lock's artifact is unreadable.") from error
    if not pinned_in(plugin.args, artifact):
        raise ConfigurationError(
            f"{directory}: '{plugin.id}' does not run the locked {artifact.reference}."
        )
    return replace(plugin, artifact=artifact)


def load(path: Path) -> Plugin:
    """One plugin, strictly. Every refusal names the file."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as error:
        raise ConfigurationError(f"{path}: cannot be read: {error}") from error
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{path}: expected a mapping at the top level.")
    _no_unknown(path, raw, KNOWN_FIELDS, "plugin")

    plugin_id = str(raw.get("id", "")).strip()
    if not ID.match(plugin_id):
        raise ConfigurationError(
            f"{path}: 'id' must be lower-case letters, digits and hyphens, got '{plugin_id}'."
        )
    if path.parent.name != plugin_id:
        raise ConfigurationError(
            f"{path}: 'id' is '{plugin_id}' but the directory is '{path.parent.name}'."
        )
    for required in ("name", "description", "category", "runtime", "command"):
        if not str(raw.get(required, "")).strip():
            raise ConfigurationError(f"{path}: '{required}' is required.")

    return Plugin(
        id=plugin_id,
        name=str(raw["name"]).strip(),
        description=str(raw["description"]).strip(),
        about=str(raw.get("about", "")).strip(),
        category=str(raw["category"]).strip(),
        publisher=str(raw.get("publisher", "")).strip(),
        homepage=str(raw.get("homepage", "")).strip(),
        popular=bool(raw.get("popular", False)),
        runtime=_enum(path, PluginRuntime, raw["runtime"], "runtime"),
        command=str(raw["command"]).strip(),
        args=tuple(str(arg) for arg in _list(path, raw.get("args"), "args")),
        fields=tuple(_setting(path, item) for item in _list(path, raw.get("settings"), "settings")),
        capabilities=frozenset(
            _enum(path, Capability, value, "capabilities")
            for value in _list(path, raw.get("capabilities"), "capabilities")
        ),
        effects=_effects(path, raw.get("effects")),
        icon=_icon(path, raw.get("icon")),
        env=_env(path, raw.get("env")),
        setup=tuple(_step(path, item) for item in _list(path, raw.get("setup"), "setup")),
        sign_in=_sign_in(path, raw.get("sign_in")),
    )


def _env(path: Path, raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{path}: 'env' must map variable names to values.")
    env = {}
    for key, value in raw.items():
        if not KEY.match(str(key)):
            raise ConfigurationError(f"{path}: env key '{key}' is not a variable name.")
        env[str(key)] = str(value)
    return env


def _step(path: Path, raw: Any) -> SetupStep:
    if isinstance(raw, str):
        return SetupStep(text=raw.strip())
    if not isinstance(raw, dict) or not str(raw.get("text", "")).strip():
        raise ConfigurationError(f"{path}: each setup step needs 'text'.")
    url = str(raw.get("url", "")).strip()
    if url and not url.startswith("https://"):
        raise ConfigurationError(f"{path}: setup step url must be https.")
    return SetupStep(text=str(raw["text"]).strip(), url=url)


def _sign_in(path: Path, raw: Any) -> PluginSignIn | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{path}: 'sign_in' must be a mapping.")
    _no_unknown(path, raw, KNOWN_SIGN_IN_FIELDS, "sign_in")
    label, tool = str(raw.get("label", "")).strip(), str(raw.get("tool", "")).strip()
    if not label or not tool:
        raise ConfigurationError(f"{path}: 'sign_in' needs a label and a tool.")
    arguments = raw.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise ConfigurationError(f"{path}: 'sign_in.arguments' must be a mapping.")
    return PluginSignIn(
        label=label, tool=tool, arguments=dict(arguments), help=str(raw.get("help", "")).strip()
    )


def _setting(path: Path, raw: Any) -> PluginField:
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{path}: each setting must be a mapping.")
    _no_unknown(path, raw, KNOWN_SETTING_FIELDS, "setting")
    key = str(raw.get("key", "")).strip()
    if not KEY.match(key):
        raise ConfigurationError(f"{path}: setting key '{key}' is not a variable name.")
    label = str(raw.get("label", "")).strip()
    if not label:
        raise ConfigurationError(f"{path}: setting '{key}' needs a label.")
    help_url = str(raw.get("help_url", "")).strip()
    if help_url and not help_url.startswith("https://"):
        raise ConfigurationError(f"{path}: setting '{key}' help_url must be https.")
    return PluginField(
        key=key,
        label=label,
        kind=_enum(path, FieldKind, raw.get("kind", "SECRET"), "kind"),
        required=bool(raw.get("required", True)),
        placeholder=str(raw.get("placeholder", "")),
        help=str(raw.get("help", "")).strip(),
        help_url=help_url,
    )


def _effects(path: Path, raw: Any) -> dict[str, Effect]:
    """Effect name to the tools that have it, turned round into tool to effect.

    Written grouped because that is how a person reviews it - "these read,
    these write" - and a tool listed twice is refused rather than resolved by
    whichever group came last.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{path}: 'effects' must map an effect to tool names.")
    effects: dict[str, Effect] = {}
    for label, tools in raw.items():
        effect = _enum(path, Effect, label, "effects")
        for tool in _list(path, tools, f"effects.{label}"):
            name = str(tool).strip()
            if name in effects:
                raise ConfigurationError(f"{path}: tool '{name}' is given two effects.")
            effects[name] = effect
    return effects


def _icon(path: Path, raw: Any) -> PluginIcon | None:
    svg = path.parent / "icon.svg"
    if not svg.is_file():
        return None
    style = raw or {}
    if not isinstance(style, dict):
        raise ConfigurationError(f"{path}: 'icon' must be a mapping.")
    _no_unknown(path, style, KNOWN_ICON_FIELDS, "icon")
    color = str(style.get("color", "#111111"))
    background = str(style.get("background", "#ffffff"))
    for value in (color, background):
        if not COLOR.match(value):
            raise ConfigurationError(f"{path}: icon colour '{value}' is not #rrggbb.")
    try:
        root = ElementTree.fromstring(svg.read_text(encoding="utf-8"))
    except (OSError, ElementTree.ParseError) as error:
        raise ConfigurationError(f"{svg}: not a readable SVG: {error}") from error
    view_box = root.get("viewBox", "0 0 24 24").strip()
    paths = tuple(
        element.get("d", "").strip()
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1] == "path" and element.get("d")
    )
    if not paths or not all(PATH_DATA.match(d) for d in paths):
        raise ConfigurationError(f"{svg}: an icon is path data and nothing else.")
    if not re.match(r"^[0-9.\-\s]+$", view_box):
        raise ConfigurationError(f"{svg}: viewBox '{view_box}' is not four numbers.")
    return PluginIcon(view_box=view_box, paths=paths, color=color, background=background)


def _list(path: Path, raw: Any, name: str) -> list[Any]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigurationError(f"{path}: '{name}' must be a list.")
    return raw


def _enum(path: Path, kind, value: Any, name: str):
    try:
        return kind(str(value).strip().upper())
    except ValueError as error:
        known = ", ".join(member.value for member in kind)
        raise ConfigurationError(
            f"{path}: '{value}' is not a valid {name}. Known: {known}."
        ) from error


def _no_unknown(path: Path, raw: dict, known: frozenset[str], what: str) -> None:
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ConfigurationError(f"{path}: unknown {what} field(s): {', '.join(unknown)}.")
