"""A PDF from Markdown, printed by Chromium.

Chromium rather than a PDF library, for one reason that decides it: text. A
pure-Python writer needs a font file for every alphabet it will meet, and an
answer in the language the person configured - Cyrillic, Greek, CJK - came out
as empty boxes. Chromium lays text out with the system's fonts, the way the
person's own browser would, and Playwright is already how the platform drives
one. It is started per document and closed after: a report is written a few
times a day, and a browser kept open for it is memory held for nothing.

The Markdown is rendered with raw HTML switched off. What is being written is
often made of pages an employee read, and a `<script>` quoted from one of them
is text in a report, not something to run while printing it.
"""

from __future__ import annotations

import html
from pathlib import Path

from markdown_it import MarkdownIt

from domain.errors import ConfigurationError

_STYLE = """
@page { size: A4; margin: 22mm 20mm; }
body { font-family: -apple-system, "Segoe UI", "Helvetica Neue", Arial, "Noto Sans", sans-serif;
       font-size: 11pt; line-height: 1.55; color: #1d1d1f; }
h1 { font-size: 21pt; margin: 0 0 14pt; letter-spacing: -0.01em; }
h2 { font-size: 15pt; margin: 20pt 0 8pt; }
h3 { font-size: 12.5pt; margin: 16pt 0 6pt; }
p, ul, ol, table, pre, blockquote { margin: 0 0 9pt; }
li { margin: 0 0 4pt; }
a { color: #0b57d0; text-decoration: none; }
code { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 9.5pt;
       background: #f3f3f5; padding: 0 3px; border-radius: 3px; }
pre { background: #f6f6f8; padding: 9pt; border-radius: 6px; white-space: pre-wrap; }
pre code { background: none; padding: 0; }
blockquote { border-left: 3px solid #d6d6da; padding-left: 10pt; color: #55555a; }
table { border-collapse: collapse; width: 100%; font-size: 10pt; }
th, td { border: 1px solid #d6d6da; padding: 4pt 6pt; text-align: left; vertical-align: top; }
th { background: #f3f3f5; }
hr { border: 0; border-top: 1px solid #e0e0e4; margin: 14pt 0; }
"""


def page(markdown: str, *, title: str = "") -> str:
    """A whole HTML document for `markdown`, titled when a title was given."""
    renderer = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")
    body = renderer.render(markdown)
    heading = f"<h1>{html.escape(title)}</h1>" if title.strip() else ""
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body>{heading}{body}</body></html>"
    )


class ChromiumPdfRenderer:
    """Implements `domain.documents.protocols.PdfRenderer`."""

    def __init__(self, *, timeout_ms: int = 30_000) -> None:
        self._timeout_ms = timeout_ms

    async def render(self, html_text: str, target: Path) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as error:
            raise ConfigurationError(
                "Writing a PDF needs Playwright, which `uv sync` installs."
            ) from error
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(headless=True)
            except Exception as error:
                raise ConfigurationError(
                    "Playwright is installed but has no browser engine. Run: "
                    "uv run playwright install chromium"
                ) from error
            try:
                document = await browser.new_page()
                # Nothing is fetched: the page is the document and its style.
                await document.route("**/*", lambda route: route.abort())
                await document.set_content(html_text, timeout=self._timeout_ms)
                await document.pdf(path=str(target), format="A4", print_background=True)
            finally:
                await browser.close()
