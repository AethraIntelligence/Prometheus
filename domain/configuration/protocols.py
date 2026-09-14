"""What an interface may do to this installation's configuration.

Read what is editable and change some of it. Nothing else: which settings are on
the list, how a value is validated and where it is kept belong to the adapter
beside `Settings`, because the application layer is not allowed to know a
settings object exists (ADR 0001).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Protocol

from domain.configuration.models import Setting, SettingValue


class SettingsEditor(Protocol):
    def current(self) -> tuple[Setting, ...]:
        """Every editable setting, in the order a person reads them."""
        ...

    def change(self, values: Mapping[str, SettingValue]) -> tuple[Setting, ...]:
        """Save these values for the next start, all or none.

        Raises `SettingValueError` naming the first key that is unknown, locked
        by the environment, or not a value its setting accepts - and then saves
        nothing, so a form with one bad field does not half-apply.
        """
        ...

    def reset(self, keys: Iterable[str] | None = None) -> tuple[Setting, ...]:
        """Forget what a window saved for these keys - all of them when None.

        The setting then comes from wherever it would have without the window:
        `.env`, or the platform's default. A key the environment holds is left
        alone rather than refused, because there was nothing saved to forget.
        """
        ...
