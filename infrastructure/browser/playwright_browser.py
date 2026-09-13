"""A real browser, started only if someone actually opens a page.

Two decisions worth stating.

**The import is lazy.** Playwright is an optional extra: `uv sync` on a fresh
checkout should not download a browser engine for a user whose employees only
read files. Importing it at call time means the dependency is missing only for
the run that needs it, and the message says how to install it.

**Extraction returns text, not HTML.** A model given raw markup spends its
context on nav bars and script tags. `extract` returns the readable text of the
page, capped, with the title and the final URL after redirects - which is what
a claim needs to be attributable.
"""

from __future__ import annotations

from typing import Any

from domain.browser.protocols import ExtractResult
from domain.errors import ConfigurationError, ProviderError
from domain.search.models import SearchQuery, SearchResult
from domain.search.protocols import SearchEngine
from infrastructure.observability.logging import get_logger

log = get_logger(__name__)

#: Enough of a page to answer a question from, small enough not to fill the
#: context of the model that has to read it.
DEFAULT_MAX_CHARS = 20_000


class PlaywrightBrowser:
    """Implements `domain.browser.protocols.Browser`."""

    def __init__(
        self,
        search_engine: SearchEngine | None = None,
        *,
        headless: bool = True,
        timeout_seconds: float = 30.0,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> None:
        self._search_engine = search_engine
        self._headless = headless
        self._timeout_ms = int(timeout_seconds * 1000)
        self._max_chars = max_chars
        self._playwright: Any = None
        self._browser: Any = None
        self._page: Any = None

    # --- Lifecycle ------------------------------------------------------------

    async def page(self) -> Any:
        """The live page, for a component that drives it rather than reads it.

        `PlaywrightComputer` clicks and types in the page this browser already
        opened - the same tab the employee has been reading, which is the point:
        two engines side by side would show the model one page and act on
        another.
        """
        return await self._ensure_page()

    async def _ensure_page(self) -> Any:
        if self._page is not None:
            return self._page
        try:
            from playwright.async_api import async_playwright
        except ImportError as error:
            raise ConfigurationError(
                "The browser tools need Playwright, which `uv sync` installs. "
                "Run it again: uv sync"
            ) from error

        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.launch(headless=self._headless)
        except Exception as error:
            await self.aclose()
            raise ConfigurationError(
                "Playwright is installed but has no browser engine. Run: "
                "uv run playwright install chromium"
            ) from error
        self._page = await self._browser.new_page()
        self._page.set_default_timeout(self._timeout_ms)
        return self._page

    async def aclose(self) -> None:
        for resource in (self._browser, self._playwright):
            if resource is None:
                continue
            closer = getattr(resource, "close", None) or getattr(resource, "stop", None)
            if closer is not None:
                await closer()
        self._page = self._browser = self._playwright = None

    # --- Browser --------------------------------------------------------------

    async def search(self, query: str) -> list[SearchResult]:
        """Delegated: scraping a results page is a worse search engine."""
        if self._search_engine is None:
            raise ConfigurationError("This browser was built without a search engine")
        return await self._search_engine.search(SearchQuery(text=query))

    async def open(self, url: str) -> None:
        page = await self._ensure_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
        except Exception as error:
            raise ProviderError(f"Could not open {url}: {error}") from error
        log.info("browser.opened", url=url)

    async def extract(self) -> ExtractResult:
        page = await self._ensure_page()
        if not page.url or page.url == "about:blank":
            raise ProviderError("No page is open")
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        cleaned = "\n".join(line.strip() for line in (text or "").splitlines() if line.strip())
        return ExtractResult(
            url=page.url,
            title=await page.title(),
            text=cleaned[: self._max_chars],
            metadata={"truncated": len(cleaned) > self._max_chars, "characters": len(cleaned)},
        )
