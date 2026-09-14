"""Tool names as a provider's API will accept them, and back.

Every tool this platform declares is namespaced with a dot - `fs.read`,
`browser.extract`, `gmail.send_message` - and the function-calling formats allow
only `a-z A-Z 0-9 _ -`. Some servers are lenient about it and some are not: a
run on OpenRouter worked for months until the request was served by a provider
that validates, and then every call carrying tools was a 400 before the model saw
it. So the translation is done for every adapter, not only for the one whose
documentation happened to say so.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

#: Anything outside what the function-calling formats accept.
NOT_ALLOWED = re.compile(r"[^a-zA-Z0-9_-]")


def wire_names(names: Iterable[str], *, max_length: int = 64) -> dict[str, str]:
    """Map each tool name to one the API will accept, reversibly.

    `fs.read` becomes `fs_read`. Two different tools can sanitise to the same
    thing - `fs.read` and `fs_read` would - and silently merging them would
    route a call to the wrong tool, so a clash gets a suffix instead. The order
    the registry lists tools in is stable, so the mapping is too.
    """
    mapping: dict[str, str] = {}
    taken: set[str] = set()
    for name in names:
        wire = NOT_ALLOWED.sub("_", name)[:max_length] or "tool"
        if wire in taken:
            base, index = wire, 2
            while wire in taken:
                suffix = f"_{index}"
                wire = f"{base[: max_length - len(suffix)]}{suffix}"
                index += 1
        taken.add(wire)
        mapping[name] = wire
    return mapping


def wire_name(name: str, mapping: dict[str, str], *, max_length: int = 64) -> str:
    """A name in a replayed transcript, sent the way the request's tools are.

    A tool a past turn called may no longer be offered - it is withheld after
    two refusals - and is then translated on its own, so the model is never
    shown a history it could not have made.
    """
    return mapping.get(name) or NOT_ALLOWED.sub("_", name)[:max_length] or "tool"


def real_names(mapping: dict[str, str]) -> dict[str, str]:
    """The reverse: what the model called, as the executor knows it."""
    return {wire: real for real, wire in mapping.items()}
