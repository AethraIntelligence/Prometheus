"""Credentials, and the rule that they never reach a model.

A key is resolved inside the tool that needs it, at the moment of the call. It
is never put in a prompt, an argument, a log line or a stored observation - so
the value type here refuses to print itself, and `redact` is applied to
everything that leaves a tool call for a log, the database or the transcript.

The wrapper is not security against the process itself: anything running here
can read the environment. It is protection against the realistic failure, which
is a secret being copied into a transcript, persisted, and then sent to a
provider on the next step.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

MASK = "***"

#: Argument names whose value never appears in a log, a record or a transcript.
SENSITIVE_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "access_token",
        "authorization",
        "credential",
        "credentials",
        "password",
        "passwd",
        "private_key",
        "secret",
        "token",
    }
)

# Values in free text need a second line of defence. Field-name redaction cannot
# protect an exception such as ``401 for Bearer ...`` or a copied ``API_KEY=``
# line. The patterns intentionally recognise credentials rather than arbitrary
# long words: over-redacting ids makes a diagnostic trace useless.
_TEXT_SECRETS = (
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|"
        r"password|passwd|private[_-]?key|secret)\s*[:=]\s*([^\s,;]+)"
    ),
    re.compile(r"\b(sk-[A-Za-z0-9_-]{8,})\b"),
)


@dataclass(frozen=True, slots=True)
class Secret:
    """A string that does not render itself.

    `str(secret)` is the mask, and so is its repr - the value comes out only
    through `reveal()`, which is easy to grep for in a review.
    """

    name: str
    _value: str

    def reveal(self) -> str:
        return self._value

    def __str__(self) -> str:
        return MASK

    def __repr__(self) -> str:
        return f"Secret({self.name!r}, {MASK})"


def is_sensitive(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in SENSITIVE_NAMES)


def redact_text(value: str, *, known_values: tuple[str, ...] = ()) -> str:
    """Mask credentials embedded in prose, headers and exception messages."""
    safe = value
    for secret in known_values:
        if secret:
            safe = safe.replace(secret, MASK)
    safe = _TEXT_SECRETS[0].sub(lambda match: f"{match.group(1)}{MASK}", safe)
    safe = _TEXT_SECRETS[1].sub(lambda match: f"{match.group(1)}={MASK}", safe)
    return _TEXT_SECRETS[2].sub(MASK, safe)


def redact(value: Any, *, known_values: tuple[str, ...] = ()) -> Any:
    """Mask secret-looking names and free-text values in a nested structure."""
    if isinstance(value, Secret):
        return MASK
    if isinstance(value, BaseException):
        return redact_text(f"{type(value).__name__}: {value}", known_values=known_values)
    if isinstance(value, str):
        return redact_text(value, known_values=known_values)
    if isinstance(value, dict):
        return {
            key: MASK if is_sensitive(str(key)) else redact(item, known_values=known_values)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, known_values=known_values) for item in value]
    return value
