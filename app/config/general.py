"""Settings -> General: the switches in `Settings`, read and saved from a window.

Beside `Settings` rather than in `infrastructure/`, because it is `Settings`
it describes: which type a field has, what it defaults to and which environment
variable names it are facts about that class, and infrastructure is not allowed
to import it (ADR 0001). The application layer sees only `SettingsEditor`.

**A change is saved for the next start, and says so.** Every one of these values
is read when the container is built - the flags decide which tools exist at all
- so applying one to a running process would mean rebuilding the platform under
a run that is using it. Each setting therefore carries both what is running and
what was saved, and the window shows the difference instead of hiding it.

**Resetting forgets, it does not write a default down.** The key leaves the
file, so the value comes from `.env` or the field's default again - and a later
change to `.env` is obeyed, which a copied default would silently outrank.

**Validation is the field's own.** A value goes through the pydantic type the
field is declared with, so `approval_mode` accepts exactly its three words and a
flag exactly a boolean; this module repeats no rule `Settings` already states.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError
from pydantic_settings import DotEnvSettingsSource

from app.config import editable
from app.config.feature_flags import FeatureFlags
from app.config.settings import Settings
from domain.configuration.models import Setting, SettingKind, SettingValue
from domain.errors import SettingValueError


class FileSettingsEditor:
    """`SettingsEditor` over `settings.json` beside the database."""

    def __init__(self, running: Settings, *, environ: Mapping[str, str] | None = None) -> None:
        self._running = running
        self._environ = {
            name.upper(): value for name, value in (environ or os.environ).items()
        }
        # What the file held when this process started, which is what the
        # running values were read from. A key saved then and reset since owes
        # a restart even though nothing is saved for it any more.
        self._saved_at_start = self._saved()

    @property
    def path(self) -> Path:
        return self._running.window_settings_path

    def current(self) -> tuple[Setting, ...]:
        saved, dotenv = self._saved(), self._dotenv()
        return tuple(self._describe(entry, saved, dotenv) for entry in editable.EDITABLE)

    def change(self, values: Mapping[str, SettingValue]) -> tuple[Setting, ...]:
        accepted = {key: self._accept(key, value) for key, value in values.items()}
        stored = self._raw()
        for key, value in accepted.items():
            node = stored
            *parents, leaf = editable.BY_KEY[key].path
            for part in parents:
                if not isinstance(node.get(part), dict):
                    node[part] = {}
                node = node[part]
            node[leaf] = list(value) if isinstance(value, tuple) else value
        self._write(stored)
        return self.current()

    def reset(self, keys: Iterable[str] | None = None) -> tuple[Setting, ...]:
        chosen = [entry.key for entry in editable.EDITABLE] if keys is None else list(keys)
        unknown = [key for key in chosen if key not in editable.BY_KEY]
        if unknown:
            raise SettingValueError(f"There is no setting called {unknown[0]!r}.")
        stored = self._raw()
        for key in chosen:
            *parents, leaf = editable.BY_KEY[key].path
            node: Any = stored
            for part in parents:
                node = node.get(part) if isinstance(node, dict) else None
            if isinstance(node, dict):
                node.pop(leaf, None)
        if isinstance(stored.get("flags"), dict) and not stored["flags"]:
            del stored["flags"]
        self._write(stored)
        return self.current()

    # --- Reading ---------------------------------------------------------------

    def _describe(
        self, entry: editable.Editable, saved: dict[str, Any], dotenv: dict[str, Any]
    ) -> Setting:
        running = _plain(_lookup(self._running, entry.path))
        locked_by = entry.environment_variable if self._locked(entry) else ""
        found, value = _find(saved, entry.path)
        default = _default(entry, dotenv)
        if found and not locked_by:
            next_start = _plain(value)
        elif locked_by or not _find(self._saved_at_start, entry.path)[0]:
            next_start = running
        else:
            next_start = default
        return Setting(
            key=entry.key,
            group=entry.group,
            label=entry.label,
            help=entry.help,
            kind=entry.kind,
            value=next_start,
            running=running,
            default=default,
            saved=found and not locked_by,
            choices=entry.choices,
            minimum=entry.minimum,
            optional=entry.optional,
            locked_by=locked_by,
        )

    def _dotenv(self) -> dict[str, Any]:
        try:
            return DotEnvSettingsSource(Settings)()
        except (OSError, ValueError):
            return {}

    def _locked(self, entry: editable.Editable) -> bool:
        return entry.environment_variable in self._environ

    def _raw(self) -> dict[str, Any]:
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return stored if isinstance(stored, dict) else {}

    def _saved(self) -> dict[str, Any]:
        return editable.only_editable(self._raw())

    # --- Writing ---------------------------------------------------------------

    def _accept(self, key: str, value: SettingValue) -> SettingValue:
        entry = editable.BY_KEY.get(key)
        if entry is None:
            raise SettingValueError(f"There is no setting called {key!r}.")
        if self._locked(entry):
            raise SettingValueError(
                f"{entry.label} is set by {entry.environment_variable} in the environment "
                "the runtime was started with; change it there."
            )
        if isinstance(value, str) and entry.kind in (SettingKind.TEXT, SettingKind.CHOICE):
            value = value.strip()
        if entry.kind is SettingKind.LIST and isinstance(value, list | tuple):
            value = tuple(str(item).strip() for item in value if str(item).strip())
        if value in (None, "") and entry.optional:
            return None
        if value in (None, "") and entry.kind is not SettingKind.LIST:
            raise SettingValueError(f"{entry.label} needs a value.")
        # Stricter than pydantic's coercion on purpose: "yes" becoming True and
        # True becoming 1 are conveniences for an `.env` file, and a window
        # sending either has a bug that should be seen rather than saved.
        if entry.kind is SettingKind.BOOLEAN and not isinstance(value, bool):
            raise SettingValueError(f"{entry.label} is either on or off.")
        if entry.kind in (SettingKind.INTEGER, SettingKind.NUMBER) and isinstance(value, bool):
            raise SettingValueError(f"{entry.label} must be a number.")
        try:
            accepted = TypeAdapter(_annotation(entry)).validate_python(value)
        except ValidationError as error:
            reason = error.errors()[0].get("msg", "not a valid value")
            raise SettingValueError(f"{entry.label}: {reason}.") from error
        if entry.minimum is not None and accepted is not None and accepted < entry.minimum:
            raise SettingValueError(f"{entry.label} cannot be less than {entry.minimum:g}.")
        return _plain(accepted)

    def _write(self, stored: dict[str, Any]) -> None:
        """Replace the file whole, so a crash mid-write leaves the old one."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        partial = self.path.with_suffix(".json.partial")
        partial.write_text(json.dumps(stored, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        partial.replace(self.path)


def _default(entry: editable.Editable, dotenv: dict[str, Any]) -> SettingValue:
    """What the next start gets for this key if the window says nothing."""
    found, raw = _find(dotenv, entry.path)
    if found:
        try:
            return _plain(TypeAdapter(_annotation(entry)).validate_python(raw))
        except ValidationError:
            pass
    *parents, leaf = entry.path
    model = FeatureFlags if parents == ["flags"] else Settings
    return _plain(model.model_fields[leaf].get_default(call_default_factory=True))


def _annotation(entry: editable.Editable) -> Any:
    *parents, leaf = entry.path
    model = FeatureFlags if parents == ["flags"] else Settings
    return model.model_fields[leaf].annotation


def _lookup(settings: Settings, path: tuple[str, ...]) -> Any:
    node: Any = settings
    for part in path:
        node = getattr(node, part)
    return node


def _find(stored: dict[str, Any], path: tuple[str, ...]) -> tuple[bool, Any]:
    node: Any = stored
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _plain(value: Any) -> SettingValue:
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value)
    if isinstance(value, Path):
        return str(value)
    return value
