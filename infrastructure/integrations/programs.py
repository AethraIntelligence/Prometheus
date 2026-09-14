"""Finding the program that starts a server, from wherever this process was started.

A window opened from the Dock inherits launchd's `PATH` - `/usr/bin:/bin` and
little else - and not the shell's. `npx` installed through nvm, `uvx` in
`~/.local/bin` and `docker` from Docker Desktop are all somewhere that `PATH`
does not reach, so a plugin that installed and ran from a terminal said "could
not start 'npx'" the first time somebody opened the window the ordinary way.

So a bare program name is looked up on `PATH` first and then in the places the
usual installers put things. A command that is already a path is used as it is:
somebody who typed one meant that file.

The directory a program was found in is also what the child needs on its own
`PATH`: `npx` is a script whose first line is `#!/usr/bin/env node`, and a node
that is next to it and nowhere on the inherited `PATH` is a second "not found",
one level down and much harder to read.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

#: Where installers put programs a server is started with, in the order to try.
#: Globs because version managers keep one directory per version.
SEARCH_PATTERNS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "~/.local/bin",
    "~/.cargo/bin",
    "~/.volta/bin",
    "~/.bun/bin",
    "~/.nvm/versions/node/*/bin",
    "~/.fnm/node-versions/*/installation/bin",
    "/Applications/Docker.app/Contents/Resources/bin",
)


def search_directories() -> list[Path]:
    """Every directory that exists among the patterns, newest version first."""
    found: list[Path] = []
    for pattern in SEARCH_PATTERNS:
        expanded = Path(pattern).expanduser()
        if "*" in pattern:
            # A sort on the path is a sort on the version for how these are
            # named (v20.11.0 < v24.1.0 needs more than that, but the newest
            # major is what a person installed last, and reads first here).
            matches = sorted(
                Path(expanded.anchor).glob(str(expanded.relative_to(expanded.anchor))),
                key=_version_key,
                reverse=True,
            )
            found.extend(path for path in matches if path.is_dir())
        elif expanded.is_dir():
            found.append(expanded)
    return found


def _version_key(path: Path) -> tuple[int, ...]:
    for part in path.parts:
        digits = part.lstrip("v").split(".")
        if digits and all(piece.isdigit() for piece in digits):
            return tuple(int(piece) for piece in digits)
    return ()


def find(program: str) -> Path | None:
    """Where `program` is, or None. A path is returned as it is if it exists."""
    if not program:
        return None
    if os.sep in program:
        candidate = Path(program).expanduser()
        return candidate if candidate.exists() else None
    on_path = shutil.which(program)
    if on_path:
        return Path(on_path)
    for directory in search_directories():
        candidate = directory / program
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def child_path(program: Path, inherited: str | None) -> str:
    """The `PATH` a child started from `program` should see."""
    parts = [str(program.parent)]
    parts += [part for part in (inherited or "").split(os.pathsep) if part]
    seen: set[str] = set()
    return os.pathsep.join(part for part in parts if not (part in seen or seen.add(part)))
