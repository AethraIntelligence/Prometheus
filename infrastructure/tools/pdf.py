"""Writing a document as a PDF, confined to the same folder as every other file tool.

A tool of its own rather than `fs.write` with a flag: what it takes is prose in
Markdown and what it leaves is a binary file, and an employee that was told
"write the file" should not have to know which of the two a person meant by
"a PDF". It reports what `fs.write` reports - `path` and `bytes_written` - so
the file shows up beside the answer exactly as a text file does.

Overwriting asks, for the reason it does in `fs.write`.
"""

from __future__ import annotations

from domain.approvals.gate import RiskAssessment
from domain.capabilities.models import Capability
from domain.documents.protocols import PdfRenderer
from domain.errors import ConfigurationError, PermissionDeniedError, ToolInputError
from domain.policies.models import RiskLevel
from domain.policies.risk import Effect
from domain.tools.models import ToolResult, ToolSpec
from domain.tools.schema import Param
from infrastructure.documents.pdf import page
from infrastructure.tools.filesystem import FileRoot, FileRootTool


def _as_pdf(path: str) -> str:
    text = path.strip()
    return text if text.lower().endswith(".pdf") else f"{text}.pdf"


class PdfWriteTool(FileRootTool):
    """Implements `domain.tools.protocols.Tool` and `RiskAssessor`."""

    def __init__(self, root: FileRoot, renderer: PdfRenderer) -> None:
        super().__init__(
            ToolSpec.of(
                "fs.write_pdf",
                "Write a document as a PDF file in the working directory, from Markdown: "
                "headings, lists, tables, links. Use it when a PDF was asked for. "
                "Overwriting an existing file needs the user's confirmation.",
                Param("path", description="File to write, relative to the working directory. "
                      "'.pdf' is added when missing."),
                Param("content", description="The document, in Markdown."),
                Param("title", description="A title printed at the top. Leave it out when "
                      "the content starts with its own heading.", required=False, default=""),
                effect=Effect.WRITE,
                capabilities=frozenset({Capability.FILE_ACCESS}),
            ),
            root,
        )
        self._renderer = renderer

    def assess(self, input_data: dict[str, object]) -> RiskAssessment | None:
        try:
            target = self._root.resolve(_as_pdf(str(input_data.get("path", ""))))
        except (ToolInputError, PermissionDeniedError):
            return RiskAssessment(RiskLevel.LOW, "")
        if target.exists():
            return RiskAssessment(
                RiskLevel.HIGH, f"Overwrite the existing file {self._root.display(target)}"
            )
        return RiskAssessment(RiskLevel.LOW, "")

    async def run(self, path: str, content: str, title: str = "") -> ToolResult:
        if not path.strip():
            raise ToolInputError("A path is required")
        target = self._root.resolve(_as_pdf(path))
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        try:
            await self._renderer.render(page(content, title=title), target)
        except ConfigurationError as error:
            return ToolResult.failure(str(error))
        return ToolResult.ok(
            path=self._root.display(target),
            bytes_written=target.stat().st_size,
            overwritten=existed,
        )
