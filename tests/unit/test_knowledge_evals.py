"""The document retrieval evals, and a broken store for each thing they claim.

The same rule as `test_retrieval_evals.py`: a metric nobody has watched fail is
a metric that measures nothing. Each one here is given a store or a retriever
that breaks exactly that property, and must catch it - otherwise a green eval
is a green eval either way, which is worse than having none.
"""

from __future__ import annotations

import pytest

from application.knowledge.evals import (
    AWAY_NOTES,
    CV,
    DELIVERY,
    GROUNDEDNESS,
    ISOLATION,
    PRECISION,
    RELEVANCE,
    KnowledgeReport,
    render_report,
    run_knowledge_evals,
)
from domain.knowledge.models import KnowledgeQuery, Passage
from infrastructure.knowledge.retriever import HybridRetriever
from infrastructure.knowledge.store import InMemoryKnowledgeStore

#: Words that put a passage in one topic or another, so a fake can tell
#: "money back" from "delivery" without a downloaded model. Crude on purpose:
#: what is under test is the eval, not an embedding model.
TOPICS = {
    "delivery": (
        "delivery",
        "shipped",
        "parcels",
        "dispatch",
        "courier",
        "warehouse",
        "aisle",
        "picked",
        "collected",
    ),
    "refund": ("refund", "return", "reimbursed", "money", "back", "card", "unopened"),
    "person": ("denys", "zhodik", "engineer", "systems", "platforms"),
}


class TopicEmbeddings:
    """Implements `domain.knowledge.protocols.EmbeddingProvider` by topic."""

    def __init__(self, model: str = "topic-fake") -> None:
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimension(self) -> int:
        return len(TOPICS)

    async def embed(self, texts):
        vectors = []
        for text in texts:
            low = text.casefold()
            vectors.append(
                tuple(
                    float(sum(1 for word in words if word in low))
                    for words in TOPICS.values()
                )
            )
        return vectors

    async def embed_query(self, text):
        return (await self.embed([text]))[0]


def retriever(store, **extra):
    return HybridRetriever(
        store, embeddings=TopicEmbeddings(), min_similarity=0.3, **extra
    )


async def run(make_retriever=retriever, **extra) -> KnowledgeReport:
    return await run_knowledge_evals(
        InMemoryKnowledgeStore,
        make_retriever,
        embeddings=TopicEmbeddings(),
        backend="test",
        **extra,
    )


async def test_a_working_retrieval_passes_every_metric() -> None:
    report = await run()

    assert report.passed, render_report(report)
    assert report.skipped == (), "nothing is skipped where a model is present"
    assert report.model == "topic-fake"


async def test_without_a_model_the_cases_that_need_meaning_are_skipped_not_failed() -> None:
    """A machine with no embedding model is scored on what it can be asked."""
    report = await run_knowledge_evals(
        InMemoryKnowledgeStore,
        lambda store: HybridRetriever(store),
        embeddings=None,
        backend="test",
    )

    assert report.passed, render_report(report)
    assert report.skipped, "the cases that need meaning have to be reported as such"
    assert "words only" in render_report(report)


class Blind:
    """A retriever that answers every question with the first passage it has."""

    def __init__(self, store) -> None:
        self._store = store

    async def retrieve(self, query: KnowledgeQuery):
        found = await self._store.candidates(workspace_id=query.workspace_id)
        return [
            Passage(chunk=chunk, title=title, source=source, score=1.0, lexical=1.0)
            for chunk, title, source in found[: query.limit]
        ]


async def test_relevance_catches_a_retrieval_that_answers_anything_with_anything() -> None:
    report = await run(Blind)

    assert not report.passed
    assert report.scores[RELEVANCE] < 1.0
    assert report.scores[PRECISION] < 1.0


class Ungrounded:
    """A retriever that drops the citation on the way back."""

    def __init__(self, store) -> None:
        self._inner = retriever(store)

    async def retrieve(self, query: KnowledgeQuery):
        return [
            Passage(chunk=passage.chunk, title="", source="", score=passage.score)
            for passage in await self._inner.retrieve(query)
        ]


async def test_groundedness_catches_a_passage_with_no_source() -> None:
    """A quotation whose origin was dropped is a memory, and this is not one."""
    report = await run(Ungrounded)

    assert not report.passed
    assert report.scores[GROUNDEDNESS] < 1.0


class Leaky:
    """A retriever that forgets which workspace it was asked about."""

    def __init__(self, store) -> None:
        self._store = store
        self._inner = retriever(store)

    async def retrieve(self, query: KnowledgeQuery):
        from dataclasses import replace

        from application.knowledge.evals import AWAY

        here = await self._inner.retrieve(query)
        there = await self._inner.retrieve(replace(query, workspace_id=AWAY))
        return (here + there)[: query.limit]


async def test_isolation_catches_a_retrieval_that_crosses_a_workspace() -> None:
    report = await run(Leaky)

    assert not report.passed
    assert report.scores[ISOLATION] < 1.0
    failed = [one.case.name for one in report.results if not one.passed]
    assert any("another workspace" in name for name in failed)


@pytest.mark.parametrize("known", [DELIVERY, CV, AWAY_NOTES])
def test_every_document_the_cases_name_is_in_the_corpus(known) -> None:
    """A case naming a document nobody seeded would pass by never being found."""
    from application.knowledge.evals import corpus

    assert known in {document.id for document in corpus()}
