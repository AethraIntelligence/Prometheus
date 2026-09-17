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
from pathlib import Path
from typing import Any

from domain.tools.artifacts import produced_files

__all__ = ["artifact", "media_type", "produced_files", "within"]


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
