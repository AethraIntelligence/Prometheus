"""Turning text into vectors, over the embeddings wire format.

The same shape as `chat_completions`: several services - a local model runner, a
hosted provider - expose one `POST /embeddings`, so the module is named after
the format rather than after a vendor, and which model answers is the router's
decision like every other model choice (ADR 0003).

Three things it has to get right.

**A batch is one call.** Indexing a document is hundreds of passages, and a call
per passage over a local server is minutes instead of seconds. The order that
comes back is the order that went out, checked rather than assumed: an answer
whose vectors are shuffled would attach every passage to its neighbour's
meaning, which nothing downstream could ever notice.

**The dimension is what came back**, not what the catalog claimed. The catalog
entry says what to expect so that a store can be planned; this reports what was
actually produced, because that is what gets written onto the chunk and what a
later query is compared against.

**A question and a passage are not embedded the same way.** Several models - the
e5 family, nomic-embed-text - are trained asymmetrically: the question is
prefixed one way and the text answering it another, and asking without the
prefixes costs several points of similarity on every comparison, which reads as
a slightly worse answer nobody can attribute to anything. The prefixes are the
model's, so they are declared on its catalog entry and applied here.

**A prefix makes it a different producer, and `model` says so.** Adding one
changes every vector the same model returns, and the stored vectors would go on
being compared against new queries by name alone - the exact silent mismatch
ADR 0016 exists to prevent. So the identity written onto a chunk carries the
scheme, and turning prefixes on re-indexes the workspace the way changing the
model does, because as far as anything comparing vectors is concerned it is one.

**A server that is not there is a configuration problem with a clear fix**, and
the error says so - the same rule the local chat provider follows. Retrieval
above catches it and falls back to the text index rather than failing a run.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import httpx

from domain.errors import ConfigurationError, ProviderError
from domain.knowledge.models import Vector
from domain.knowledge.protocols import EmbeddingProvider
from infrastructure.llm.errors import translate_status, translate_transport_error
from infrastructure.llm.local_server import OnDemandServer
from infrastructure.observability.logging import get_logger

log = get_logger(__name__)

#: Embedding a batch is fast even locally; a minute means something is wrong.
DEFAULT_TIMEOUT_SECONDS = 120.0


class OpenAICompatibleEmbeddings:
    """Implements `domain.knowledge.protocols.EmbeddingProvider`."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        dimensions: int = 0,
        query_prefix: str = "",
        passage_prefix: str = "",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
        server: OnDemandServer | None = None,
    ) -> None:
        self._server = server
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._query_prefix = query_prefix
        self._passage_prefix = passage_prefix
        self._api_key = api_key
        self._declared = dimensions
        self._seen = 0
        self._timeout = timeout_seconds
        self._client = client

    @property
    def model(self) -> str:
        """The model, and how it was asked, because the two make the vector.

        Plain where there are no prefixes, so a machine that already has an
        indexed workspace is not told to re-index it for nothing.
        """
        if not self._query_prefix and not self._passage_prefix:
            return self._model
        return f"{self._model} [{self._query_prefix}|{self._passage_prefix}]"

    @property
    def dimension(self) -> int:
        """What this model actually produces, once it has produced anything."""
        return self._seen or self._declared

    async def embed_query(self, text: str) -> Vector:
        vectors = await self._post([f"{self._query_prefix}{text}"])
        return vectors[0] if vectors else ()

    async def embed(self, texts) -> list[Vector]:
        return await self._post([f"{self._passage_prefix}{text}" for text in texts])

    async def _post(self, texts: Sequence[str]) -> list[Vector]:
        wanted = [text for text in texts]
        if not wanted:
            return []
        if self._server is not None:
            await self._server.ensure()
        payload: dict[str, object] = {"model": self._model, "input": wanted}
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            response = await client.post(
                f"{self._base_url}/embeddings", json=payload, headers=headers
            )
        except httpx.HTTPError as error:
            raise translate_transport_error(error, provider="embeddings") from error
        finally:
            if self._client is None:
                await client.aclose()
        if response.status_code >= 400:
            raise translate_status(
                response.status_code, response.text, provider="embeddings"
            )

        data = response.json().get("data")
        if not isinstance(data, list) or len(data) != len(wanted):
            raise ProviderError(
                f"The embedding server answered with {len(data or ())} vectors for "
                f"{len(wanted)} passages."
            )
        # Ordered by the index the server reports rather than by arrival: the
        # format allows either, and pairing a passage with its neighbour's
        # meaning is a wrong answer nothing downstream could detect.
        ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
        vectors = [tuple(float(number) for number in item["embedding"]) for item in ordered]
        if vectors:
            self._seen = len(vectors[0])
            if self._declared and self._seen != self._declared:
                # Not fatal - what is written on the chunk is what came back -
                # but the catalog is now wrong about a number a person may be
                # sizing a store on.
                log.warning(
                    "embeddings.dimension_differs_from_catalog",
                    model=self._model,
                    declared=self._declared,
                    actual=self._seen,
                )
        return vectors


class RoutedEmbeddings:
    """Implements `EmbeddingProvider` by asking which model is chosen now.

    Everything that embeds - indexing a document, a query at retrieval - is
    built once when the process starts, and used to be handed the client for
    whichever model was chosen then. Choosing another in the window changed
    the catalog and nothing that embedded: documents went on being indexed with
    the old model until a restart, which is a setting that appears not to work.
    Resolving on every use is one dictionary lookup, and makes the choice take
    effect on the next call.

    `model` is empty when nothing can embed, which callers read as "no model"
    exactly as they read None.
    """

    def __init__(self, resolve: Callable[[], EmbeddingProvider | None]) -> None:
        self._resolve = resolve

    @property
    def model(self) -> str:
        current = self._resolve()
        return current.model if current is not None else ""

    @property
    def dimension(self) -> int:
        current = self._resolve()
        return current.dimension if current is not None else 0

    async def embed(self, texts: Sequence[str]) -> list[Vector]:
        return await self._current().embed(texts)

    async def embed_query(self, text: str) -> Vector:
        return await self._current().embed_query(text)

    def _current(self) -> EmbeddingProvider:
        current = self._resolve()
        if current is None:
            raise ConfigurationError("No model in the catalog can turn text into vectors.")
        return current
