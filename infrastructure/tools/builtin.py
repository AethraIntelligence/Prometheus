"""The one place a tool is registered.

Adding a capability is a new file under `infrastructure/tools/` and a line in
this list. Nothing in `application/` learns the tool's name, no employee class
is edited, and whether an employee may call it is settled by its declaration -
so a tool nobody lists is a tool nobody can use, which is the intended default.

Building is lazy per group: a browser is not launched, and Playwright is not
imported, for a workforce whose employees only read files.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from domain.browser.protocols import Browser
from domain.computer.protocols import Computer, ScreenReader
from domain.documents.protocols import PdfRenderer
from domain.search.protocols import SearchEngine
from domain.tools.protocols import Tool
from infrastructure.documents.pdf import ChromiumPdfRenderer
from infrastructure.tools.code import CodeExecutionTool
from infrastructure.tools.computer import computer_tools
from infrastructure.tools.filesystem import (
    FileListTool,
    FileMoveTool,
    FileReadTool,
    FileRoot,
    FileWriteTool,
)
from infrastructure.tools.pdf import PdfWriteTool
from infrastructure.tools.registry import InMemoryToolRegistry
from infrastructure.tools.web import BrowserExtractTool, BrowserOpenTool, WebSearchTool


def filesystem_tools(
    root: Path | Callable[[], Path], *, pdf_renderer: PdfRenderer | None = None
) -> list[Tool]:
    file_root = FileRoot(root)
    file_root.ensure()
    return [
        FileListTool(file_root),
        FileReadTool(file_root),
        FileWriteTool(file_root),
        FileMoveTool(file_root),
        # The engine is started only when a PDF is written, so offering the
        # tool costs a machine that never writes one nothing.
        PdfWriteTool(file_root, pdf_renderer or ChromiumPdfRenderer()),
    ]


def search_tools(engine: SearchEngine) -> list[Tool]:
    return [WebSearchTool(engine)]


def browser_tools(browser: Browser) -> list[Tool]:
    return [BrowserOpenTool(browser), BrowserExtractTool(browser)]


def code_tools(*, timeout_seconds: float = 30.0) -> list[Tool]:
    return [CodeExecutionTool(timeout_seconds=timeout_seconds)]


def screen_tools(computer: Computer, reader: Callable[[], ScreenReader]) -> list[Tool]:
    return list(computer_tools(computer, reader))


def build_registry(
    *,
    file_root: Path | Callable[[], Path],
    search_engine: Callable[[], SearchEngine] | None = None,
    browser: Callable[[], Browser] | None = None,
    code_execution: bool = True,
    code_timeout_seconds: float = 30.0,
    computers: Callable[[], list[tuple[Computer, Callable[[], ScreenReader]]]] | None = None,
) -> InMemoryToolRegistry:
    """Everything this machine can do, before any employee's rights are applied."""
    tools: list[Tool] = list(filesystem_tools(file_root))
    if search_engine is not None:
        tools += search_tools(search_engine())
    if browser is not None:
        tools += browser_tools(browser())
    if code_execution:
        tools += code_tools(timeout_seconds=code_timeout_seconds)
    if computers is not None:
        # A surface at a time, so the browser one exists whether or not the
        # desktop is switched on. The names differ per surface, which is what
        # lets an employee be given the page and not the machine.
        for computer, reader in computers():
            tools += screen_tools(computer, reader)
    return InMemoryToolRegistry(tools)
