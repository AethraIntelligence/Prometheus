"""A PDF renderer that needs no browser engine."""

from __future__ import annotations

from pathlib import Path


class FakePdfRenderer:
    """Implements `domain.documents.protocols.PdfRenderer`, keeping what it was given."""

    def __init__(self) -> None:
        self.rendered: list[tuple[str, Path]] = []

    async def render(self, html: str, target: Path) -> None:
        self.rendered.append((html, target))
        target.write_bytes(b"%PDF-1.7\n% fake\n" + html.encode("utf-8"))
