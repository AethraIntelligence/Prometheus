"""Turning a file into text, and refusing to guess when it cannot.

The extractors are small and separate because the failure they exist to prevent
is silent: a PDF read as bytes and decoded as UTF-8 produces a page of mojibake
that chunks, embeds and retrieves perfectly well, and answers questions with
nonsense. So a format either has an extractor that understands it or is refused
with a sentence saying which format it is - and refusing is the behaviour that
lets somebody install the missing extra and try again.

**PDF and Word come with the platform.** `pypdf` is a dependency, and a `.docx`
is read with the standard library: it is a zip of XML, and what a person means by
"the document" is its paragraphs. An environment missing `pypdf` still refuses a
PDF by name rather than reading it badly.

**Nothing here summarises, truncates or reformats.** What comes out is what the
document said, because everything downstream - the chunker, the index, the
citation in a prompt - is built on the text being the text.
"""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path

from domain.errors import PrometheusError

#: What the plain reader will accept. Everything here is text a person could
#: open in an editor; anything else needs an extractor that understands it.
TEXT_SUFFIXES = frozenset(
    {".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".log", ".json", ".yaml", ".yml"}
)
HTML_SUFFIXES = frozenset({".html", ".htm"})
PDF_SUFFIXES = frozenset({".pdf"})
WORD_SUFFIXES = frozenset({".docx"})
READABLE_SUFFIXES = TEXT_SUFFIXES | HTML_SUFFIXES | PDF_SUFFIXES | WORD_SUFFIXES


class UnsupportedDocumentError(PrometheusError):
    """A file this machine cannot read as text, named rather than guessed at."""


class PlainTextExtractor:
    """Implements `domain.knowledge.protocols.TextExtractor` for text files."""

    def supports(self, path: Path, media_type: str = "") -> bool:
        return path.suffix.lower() in TEXT_SUFFIXES or media_type.startswith("text/")

    def extract(self, path: Path, media_type: str = "") -> str:
        raw = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix.lower() == ".json":
            # Pretty-printed, because one line of minified JSON is one chunk
            # with no structure for the chunker to cut on.
            try:
                return json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
            except ValueError:
                return raw
        return raw


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skipping = False

    def handle_starttag(self, tag: str, attrs) -> None:
        del attrs
        if tag in ("script", "style"):
            self._skipping = True
        elif tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skipping = False

    def handle_data(self, data: str) -> None:
        if not self._skipping and data.strip():
            self.parts.append(data.strip())


class HtmlExtractor:
    """Implements `domain.knowledge.protocols.TextExtractor` for saved pages.

    The standard library's parser rather than a dependency: what is wanted is
    the words, and the block-level tags are enough structure for the chunker to
    cut on. A page that needs a real renderer to read is a page the browser
    tools should be opening, not this.
    """

    def supports(self, path: Path, media_type: str = "") -> bool:
        return path.suffix.lower() in HTML_SUFFIXES or media_type == "text/html"

    def extract(self, path: Path, media_type: str = "") -> str:
        parser = _Text()
        parser.feed(path.read_text(encoding="utf-8", errors="replace"))
        return " ".join(parser.parts).replace(" \n\n ", "\n\n").strip()


class PdfExtractor:
    """Implements `domain.knowledge.protocols.TextExtractor`, if `pypdf` is here.

    Imported inside the call rather than at module scope, so that a machine
    without the extra can still read every other format - the same rule the
    browser tools follow.
    """

    def supports(self, path: Path, media_type: str = "") -> bool:
        return path.suffix.lower() in PDF_SUFFIXES or media_type == "application/pdf"

    def extract(self, path: Path, media_type: str = "") -> str:
        try:
            from pypdf import PdfReader
        except ImportError as error:
            raise UnsupportedDocumentError(
                "Reading a PDF needs pypdf, which `uv sync` installs. Run it again: uv sync"
            ) from error
        reader = PdfReader(str(path))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
        text = "\n\n".join(page for page in pages if page)
        if not text.strip():
            # A PDF of scanned images has no text layer, and an empty document
            # that says INDEXED is worse than one that says why it is not.
            raise UnsupportedDocumentError(
                f"{path.name} has no text in it - a scan needs OCR, which this "
                "platform does not do."
            )
        return text


class WordExtractor:
    """Implements `domain.knowledge.protocols.TextExtractor` for `.docx`.

    A Word document is a zip whose `word/document.xml` holds the text in runs
    (`w:t`) inside paragraphs (`w:p`). Read with the standard library: a
    dependency that parses styles, tables and images to hand back the words is
    a dependency for the part nothing here uses. A paragraph break is kept as a
    blank line, which is what the chunker cuts on.
    """

    NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

    def supports(self, path: Path, media_type: str = "") -> bool:
        return path.suffix.lower() in WORD_SUFFIXES or media_type == (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )

    def extract(self, path: Path, media_type: str = "") -> str:
        from xml.etree import ElementTree
        from zipfile import BadZipFile, ZipFile

        try:
            with ZipFile(path) as archive:
                body = archive.read("word/document.xml")
        except (BadZipFile, KeyError) as error:
            raise UnsupportedDocumentError(
                f"{path.name} is not a Word document this can read - an old .doc "
                "has to be saved as .docx first."
            ) from error
        root = ElementTree.fromstring(body)
        paragraphs = []
        for paragraph in root.iter(f"{self.NAMESPACE}p"):
            words = "".join(
                "\t" if node.tag == f"{self.NAMESPACE}tab" else node.text or ""
                for node in paragraph.iter()
                if node.tag in (f"{self.NAMESPACE}t", f"{self.NAMESPACE}tab")
            ).strip()
            if words:
                paragraphs.append(words)
        text = "\n\n".join(paragraphs)
        if not text.strip():
            raise UnsupportedDocumentError(f"{path.name} has no text in it.")
        return text


class Extractors:
    """The extractors this machine has, asked in order.

    A collection rather than a function so that adding a format is adding a
    class, and so that the refusal names what was actually tried.
    """

    def __init__(self, *extractors) -> None:
        self._extractors = extractors or (
            PlainTextExtractor(),
            HtmlExtractor(),
            PdfExtractor(),
            WordExtractor(),
        )

    def supports(self, path: Path, media_type: str = "") -> bool:
        return any(one.supports(path, media_type) for one in self._extractors)

    def extract(self, path: Path, media_type: str = "") -> str:
        for extractor in self._extractors:
            if extractor.supports(path, media_type):
                return extractor.extract(path, media_type)
        raise UnsupportedDocumentError(
            f"Nothing here reads {path.suffix or 'that kind of file'}. "
            f"Readable: {', '.join(sorted(READABLE_SUFFIXES))}"
        )
