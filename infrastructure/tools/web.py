"""Search and browsing, as three tools over two contracts.

`web.search` and the browser are kept apart on purpose. A question usually
needs a list of candidate sources, and answering it by loading pages costs a
browser launch and a page render per result. Searching is cheap; opening is
not, and the employee should be able to choose.

Opening and extracting are also separate, because a page load and reading what
loaded are separately observable: an employee that opened a page which returned
nothing should see that as its own step rather than as an empty answer.

The two of them declare themselves BROWSER on the interface hierarchy and search
declares itself API - because that is what it is. A search endpoint returns
structured results over HTTP; nothing about it involves rendering a page.
"""

from __future__ import annotations

from domain.browser.protocols import Browser
from domain.capabilities.models import Capability
from domain.computer.interfaces import InterfaceLevel
from domain.integrations.untrusted import TrustLevel
from domain.search.models import SearchQuery
from domain.search.protocols import SearchEngine
from domain.tools.models import ToolResult, ToolSpec
from domain.tools.schema import Param
from infrastructure.tools.base import BaseTool

DEFAULT_RESULT_LIMIT = 5
MAX_RESULT_LIMIT = 10


class WebSearchTool(BaseTool):
    def __init__(self, engine: SearchEngine) -> None:
        super().__init__(
            ToolSpec.of(
                "web.search",
                "Search the web and get back a list of titles, URLs and snippets. "
                "Use this to find sources; use browser.open to read one.",
                Param("query", description="What to search for."),
                Param("limit", type="integer", required=False, default=DEFAULT_RESULT_LIMIT,
                      description=f"How many results to return, at most {MAX_RESULT_LIMIT}."),
                capabilities=frozenset({Capability.WEB_BROWSING}),
                interface_level=InterfaceLevel.API,
                result_trust=TrustLevel.UNTRUSTED,
                result_kind="web_search",
                result_schema={"query": "string", "count": "integer", "results": "array"},
            )
        )
        self._engine = engine

    async def run(self, query: str, limit: int = DEFAULT_RESULT_LIMIT) -> ToolResult:
        results = await self._engine.search(
            SearchQuery(text=query, limit=min(max(limit, 1), MAX_RESULT_LIMIT))
        )
        if not results:
            # An empty result is a successful call with nothing in it. Saying so
            # plainly stops the model reading a failure into silence.
            return ToolResult.ok(query=query, results=[], count=0)
        return ToolResult.ok(
            query=query,
            count=len(results),
            results=[
                {"title": r.title, "url": r.url, "snippet": r.snippet} for r in results
            ],
        )


class BrowserOpenTool(BaseTool):
    def __init__(self, browser: Browser) -> None:
        super().__init__(
            ToolSpec.of(
                "browser.open",
                "Open a URL in the browser. Follow it with browser.extract to read "
                "what the page says.",
                Param("url", description="The full URL, including https://."),
                capabilities=frozenset({Capability.WEB_BROWSING}),
                interface_level=InterfaceLevel.BROWSER,
            )
        )
        self._browser = browser

    async def run(self, url: str) -> ToolResult:
        await self._browser.open(url)
        return ToolResult.ok(url=url, opened=True)


class BrowserExtractTool(BaseTool):
    def __init__(self, browser: Browser) -> None:
        super().__init__(
            ToolSpec.of(
                "browser.extract",
                "Read the text of the page that is currently open, with its title "
                "and final URL.",
                capabilities=frozenset({Capability.WEB_BROWSING}),
                interface_level=InterfaceLevel.BROWSER,
                result_trust=TrustLevel.UNTRUSTED,
                result_kind="web_page",
                result_schema={"url": "string", "title": "string", "text": "string"},
            )
        )
        self._browser = browser

    async def run(self) -> ToolResult:
        extracted = await self._browser.extract()
        return ToolResult.ok(
            url=extracted.url,
            title=extracted.title,
            text=extracted.text,
            **extracted.metadata,
        )
