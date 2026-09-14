"""One setting as an interface shows it: what it is, what it holds, who owns it.

Two values rather than one, because a setting read at startup has two honest
answers to "what is it": what this process is running with, and what the next
start will get. Showing only the second would tell a person a switch is off
while the engine behind the window still has it on; showing only the first
would make a change look as if it had been ignored.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SettingKind(StrEnum):
    BOOLEAN = "BOOLEAN"
    CHOICE = "CHOICE"
    INTEGER = "INTEGER"
    NUMBER = "NUMBER"
    TEXT = "TEXT"
    #: A list of short strings, such as application names.
    LIST = "LIST"


#: What a value may be, in any kind. Lists are tuples so a setting stays hashable.
SettingValue = bool | int | float | str | tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class Setting:
    #: Dotted path, e.g. `flags.memory` or `approval_mode`.
    key: str
    group: str
    label: str
    help: str
    kind: SettingKind
    #: What the next start gets.
    value: SettingValue
    #: What this process is running with.
    running: SettingValue
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    #: Whether None is a meaningful answer ("use each provider's own default").
    optional: bool = False
    #: What this setting falls back to when nothing is saved from a window:
    #: the `.env` file's value, or the platform's own default.
    default: SettingValue = None
    #: Whether a window saved this value, and so whether resetting would change it.
    saved: bool = False
    #: The environment variable that decides this value, when one does. A
    #: variable set in the shell outranks anything saved from a window, so the
    #: window shows it and does not pretend it can change it.
    locked_by: str = ""

    @property
    def restart_needed(self) -> bool:
        return self.value != self.running
