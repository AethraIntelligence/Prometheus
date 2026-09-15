"""The files a piece of work left behind, read off what it did.

An answer that says "saved to raw_news.txt" is a sentence; the file is a fact
the store can vouch for. So the list is derived from the tool calls that
succeeded, never from the answer's text - the same rule `validation` applies,
and for the same reason: a model that says it wrote a file is the failure this
would otherwise repeat on screen.

Read by the shape of what a tool reported rather than by its name. A tool that
wrote a file says `path` and `bytes_written`; one that moved a file says
`source` and `destination`. A name check would have to learn every tool an
integration ever adds, and would be wrong the first time one did.
"""

from __future__ import annotations

import mimetypes
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from domain.tools.telemetry import ToolCallRecord


def produced_files(calls: Iterable[ToolCallRecord]) -> list[str]:
    """Relative paths, in the order they were first written, as they stand now.

    A file moved after it was written is listed where it went: the old path is
    somewhere nobody can open any more.
    """
    found: list[str] = []
    for call in sorted(calls, key=lambda item: item.created_at):
        if not call.success:
            continue
        output = call.output or {}
        written = output.get("path")
        if isinstance(written, str) and "bytes_written" in output:
            if written not in found:
                found.append(written)
            continue
        source, destination = output.get("source"), output.get("destination")
        if isinstance(source, str) and isinstance(destination, str) and source in found:
            found[found.index(source)] = destination
    return found


def within(root: Path, relative: str) -> Path | None:
    """The file under `root`, or None where the path leads anywhere else."""
    base = root.expanduser().resolve()
    target = (base / relative).resolve()
    if target != base and base not in target.parents:
        return None
    return target


def media_type(path: str) -> str:
    guessed, _ = mimetypes.guess_type(path)
    if guessed:
        return guessed
    # Markdown is what employees write most, and not every platform's table
    # knows it; left unguessed, the one file worth rendering would be offered
    # only as a download.
    return "text/markdown" if path.lower().endswith((".md", ".markdown")) else ""


def artifact(relative: str, root: Path | None) -> dict[str, Any]:
    """One file, as an interface sees it: what to call it and whether it is still there."""
    target = within(root, relative) if root is not None else None
    present = target is not None and target.is_file()
    return {
        "path": relative,
        "name": Path(relative).name,
        "location": str(target) if target is not None else "",
        "media_type": media_type(relative),
        "size": target.stat().st_size if present and target is not None else None,
        "exists": present,
    }
