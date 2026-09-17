"""Files, confined to one root directory.

The root is not a suggestion the model is asked to respect - it is resolved
before every operation, after following symlinks, and a path that lands outside
it is refused. That is why "write outside the working directory" is not on the
approval list: it cannot happen, and a confirmation prompt for an impossible
action teaches the user to click through prompts.

What does need a person is destroying something that already exists. Creating
`report.md` and overwriting `report.md` are the same tool call away from each
other, so the tools tell the gate which one this is.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable
from pathlib import Path

from domain.approvals.gate import RiskAssessment
from domain.capabilities.models import Capability
from domain.errors import PermissionDeniedError, ToolInputError
from domain.policies.models import RiskLevel
from domain.policies.risk import Effect
from domain.tools.models import ToolResult, ToolSpec
from domain.tools.schema import Param
from infrastructure.tools.base import BaseTool

#: Enough for a long document, small enough that one read cannot fill the model's
#: context by itself. A bigger file is read in parts, which is what `offset` is for.
DEFAULT_MAX_BYTES = 100_000


class FileRoot:
    """The one directory the filesystem tools can see, asked for per call.

    A callable rather than a fixed path, because since Phase 15 the root follows
    the workspace: switching workspace moves what an employee can read, and a
    root captured when the registry was built would keep the second workspace
    reading the first one's files. The tools are built once and the answer is
    resolved at the moment of the call - which is also how the run that is
    already going keeps the root it started with (`WorkspaceContext.enter`).
    """

    def __init__(self, root: Path | Callable[[], Path]) -> None:
        self._root = root if callable(root) else (lambda path=root: path)

    @property
    def root(self) -> Path:
        return self._root().expanduser().resolve()

    def ensure(self) -> Path:
        root = self.root
        root.mkdir(parents=True, exist_ok=True)
        return root

    def resolve(self, relative: str) -> Path:
        """Turn a model-supplied path into a real one inside the root, or refuse.

        `resolve()` before the check, not after: `notes/../../.ssh/id_rsa` and a
        symlink pointing out of the root both only reveal themselves once
        the path is made absolute.
        """
        if not relative or not relative.strip():
            raise ToolInputError("A path is required")
        root = self.ensure()
        candidate = (root / relative.strip()).expanduser()
        resolved = candidate.resolve()
        if resolved != root and root not in resolved.parents:
            raise PermissionDeniedError(
                f"'{relative}' is outside the working directory ({root})"
            )
        return resolved

    def display(self, path: Path) -> str:
        """Paths are reported relative to the root: the model never sees the host."""
        try:
            return str(path.relative_to(self.root)) or "."
        except ValueError:  # pragma: no cover - resolve() already guarantees this
            return str(path)


class FileRootTool(BaseTool):
    def __init__(self, spec: ToolSpec, root: FileRoot) -> None:
        super().__init__(spec)
        self._root = root


class FileListTool(FileRootTool):
    def __init__(self, root: FileRoot) -> None:
        super().__init__(
            ToolSpec.of(
                "fs.list",
                "List the files and directories under a path in the working directory.",
                Param("path", description="Directory to list, relative to the working "
                      "directory. Use '.' for the top level.", required=False, default="."),
                Param("pattern", description="Optional glob, for example '*.pdf'.",
                      required=False, default="*"),
                Param("recursive", type="boolean", description="Descend into subdirectories.",
                      required=False, default=False),
                capabilities=frozenset({Capability.FILE_ACCESS}),
            ),
            root,
        )

    async def run(
        self, path: str = ".", pattern: str = "*", recursive: bool = False
    ) -> ToolResult:
        target = self._root.resolve(path)
        if not target.is_dir():
            return ToolResult.failure(f"'{path}' is not a directory")
        matches = target.rglob(pattern) if recursive else target.glob(pattern)
        entries = [
            {
                "path": self._root.display(entry),
                "kind": "directory" if entry.is_dir() else "file",
                "bytes": entry.stat().st_size if entry.is_file() else None,
            }
            for entry in sorted(matches)
        ]
        return ToolResult.ok(path=self._root.display(target), entries=entries,
                             count=len(entries))


class FileReadTool(FileRootTool):
    def __init__(self, root: FileRoot, *, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        super().__init__(
            ToolSpec.of(
                "fs.read",
                "Read a text file from the working directory.",
                Param("path", description="File to read, relative to the working directory."),
                Param("offset", type="integer", description="Byte to start reading from, for "
                      "a file too large to read at once.", required=False, default=0),
                capabilities=frozenset({Capability.FILE_ACCESS}),
            ),
            root,
        )
        self._max_bytes = max_bytes

    async def run(self, path: str, offset: int = 0) -> ToolResult:
        target = self._root.resolve(path)
        if not target.is_file():
            return ToolResult.failure(f"'{path}' is not a file")
        size = target.stat().st_size
        with target.open("rb") as handle:
            handle.seek(max(offset, 0))
            chunk = handle.read(self._max_bytes)
        # Replacing undecodable bytes rather than failing: a stray byte in a log
        # file should not stop an employee from reading the rest of it.
        return ToolResult.ok(
            path=self._root.display(target),
            content=chunk.decode("utf-8", errors="replace"),
            bytes_read=len(chunk),
            total_bytes=size,
            truncated=offset + len(chunk) < size,
        )


class FileWriteTool(FileRootTool):
    """Implements `domain.tools.protocols.Tool` and `RiskAssessor`."""

    def __init__(self, root: FileRoot) -> None:
        super().__init__(
            ToolSpec.of(
                "fs.write",
                "Write a text file in the working directory. Overwriting an existing "
                "file needs the user's confirmation.",
                Param("path", description="File to write, relative to the working directory."),
                Param("content", description="The full text to write."),
                effect=Effect.WRITE,
                capabilities=frozenset({Capability.FILE_ACCESS}),
            ),
            root,
        )

    def assess(self, input_data: dict[str, object]) -> RiskAssessment | None:
        try:
            target = self._root.resolve(str(input_data.get("path", "")))
        except (ToolInputError, PermissionDeniedError):
            # Refused paths never reach execution; the gate has nothing to ask.
            return RiskAssessment(RiskLevel.LOW, "")
        if target.exists():
            return RiskAssessment(
                RiskLevel.HIGH, f"Overwrite the existing file {self._root.display(target)}"
            )
        return RiskAssessment(RiskLevel.LOW, "")

    def preview(self, input_data: dict[str, object]) -> dict[str, object]:
        target = self._root.resolve(str(input_data.get("path", "")))
        after = str(input_data.get("content", ""))
        before = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
        diff = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"before/{self._root.display(target)}",
                tofile=f"after/{self._root.display(target)}",
            )
        )
        return {
            "kind": "file_diff",
            "path": self._root.display(target),
            "overwrites": target.exists(),
            "diff": diff[:4_000] + ("\n... [preview truncated]" if len(diff) > 4_000 else ""),
        }

    async def run(self, path: str, content: str) -> ToolResult:
        target = self._root.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        target.write_text(content, encoding="utf-8")
        return ToolResult.ok(
            path=self._root.display(target),
            bytes_written=len(content.encode("utf-8")),
            overwritten=existed,
        )


class FileMoveTool(FileRootTool):
    """Implements `domain.tools.protocols.Tool` and `RiskAssessor`."""

    def __init__(self, root: FileRoot) -> None:
        super().__init__(
            ToolSpec.of(
                "fs.move",
                "Move or rename a file inside the working directory. Moving onto an "
                "existing file needs the user's confirmation.",
                Param("source", description="File to move, relative to the working directory."),
                Param("destination", description="Where to move it to. A path ending in '/' "
                      "or naming an existing directory moves the file into it."),
                effect=Effect.WRITE,
                capabilities=frozenset({Capability.FILE_ACCESS}),
            ),
            root,
        )

    def assess(self, input_data: dict[str, object]) -> RiskAssessment | None:
        try:
            source = self._root.resolve(str(input_data.get("source", "")))
            destination = self._destination(source, str(input_data.get("destination", "")))
        except (ToolInputError, PermissionDeniedError):
            return RiskAssessment(RiskLevel.LOW, "")
        if destination.exists():
            return RiskAssessment(
                RiskLevel.HIGH,
                f"Replace the existing file {self._root.display(destination)}",
            )
        # A move inside the root is undone by another move, so sorting a
        # folder full of documents does not ask a question per document.
        return RiskAssessment(RiskLevel.LOW, "")

    def preview(self, input_data: dict[str, object]) -> dict[str, object]:
        source = self._root.resolve(str(input_data.get("source", "")))
        target = self._destination(source, str(input_data.get("destination", "")))
        return {
            "kind": "file_move",
            "source": self._root.display(source),
            "destination": self._root.display(target),
            "overwrites": target.exists(),
        }

    def _destination(self, source: Path, raw: str) -> Path:
        target = self._root.resolve(raw)
        if raw.strip().endswith("/") or target.is_dir():
            return target / source.name
        return target

    async def run(self, source: str, destination: str) -> ToolResult:
        origin = self._root.resolve(source)
        if not origin.exists():
            return ToolResult.failure(f"'{source}' does not exist")
        target = self._destination(origin, destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        origin.rename(target)
        return ToolResult.ok(
            source=self._root.display(origin),
            destination=self._root.display(target),
        )
