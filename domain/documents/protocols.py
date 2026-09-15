"""Turning a document into a file a person can print or send.

A contract of its own rather than a method on the browser: the browser is what
an employee drives to read the web, and a page it has open belongs to that
run. Rendering a report is a different job that happens to be done well by the
same engine, and a renderer that reached into the employee's tab would print
whatever that tab was showing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class PdfRenderer(Protocol):
    async def render(self, html: str, target: Path) -> None:
        """Write `html` to `target` as a PDF. Raises when no engine is available."""
        ...
