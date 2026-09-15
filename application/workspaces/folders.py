"""Which folder a thread works in.

**Every thread gets one of its own.** A workspace used to be one directory, so
ten threads left their files side by side and the second request in a thread
could not tell its own draft from another thread's. The first request in a
thread gives it a folder under the workspace's root, named after the thread, and
the name is kept on the thread: a later rename moves the label, not the files.

**A person can point a thread elsewhere**, and a folder chosen is still one
folder. The file tools stay confined to exactly one directory per run; picking
the folder is what changed, not how far a tool can reach. Two folders are
refused: the top of the disk and the home directory itself. Both are a choice
of every file the person has, which a dialog's default location makes easy to
make by accident.

**The folder is made when it is first written to, not when it is named.** A
thread that only talks never leaves an empty directory behind.
"""

from __future__ import annotations

from pathlib import Path

from domain.conversations.models import Conversation
from domain.errors import FolderError

#: Long enough to recognise a thread by, short enough for a file manager's column.
NAME_LIMIT = 48
_UNSAFE = set('/\\:*?"<>|')


def folder_name(conversation: Conversation) -> str:
    """A directory name a person would recognise the thread by."""
    title = conversation.title.rstrip("…").strip()
    kept = "".join(" " if c in _UNSAFE or not c.isprintable() else c for c in title)
    name = " ".join(kept.split())[:NAME_LIMIT].strip(" .")
    return name or f"Task {str(conversation.id)[:8]}"


def session_folder(root: Path, conversation: Conversation) -> Path:
    """A folder under `root` for this thread, not already somebody else's."""
    base = root.expanduser() / folder_name(conversation)
    candidate, number = base, 2
    while candidate.exists():
        candidate = base.with_name(f"{base.name} {number}")
        number += 1
    return candidate


def chosen_folder(raw: str) -> Path:
    """A folder a person picked, checked before any work is confined to it."""
    text = raw.strip()
    if not text:
        raise FolderError("No folder was given.")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise FolderError(f"'{text}' is not a full path to a folder.")
    resolved = path.resolve()
    if resolved == Path(resolved.anchor) or resolved == Path.home().resolve():
        raise FolderError(
            f"'{resolved}' would hand every file under it to the work. Choose a folder inside it."
        )
    if resolved.exists() and not resolved.is_dir():
        raise FolderError(f"'{resolved}' is a file, not a folder.")
    return resolved
