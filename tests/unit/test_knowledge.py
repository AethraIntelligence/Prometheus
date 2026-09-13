"""Documents: cut, stored, retrieved - and kept apart from memory."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from application.knowledge.service import KnowledgeService
from domain.errors import PrometheusError
from domain.knowledge.chunking import chunk
from domain.knowledge.models import DocumentStatus, KnowledgeQuery
from domain.knowledge.ranking import blend, cosine, coverage, normalise
from domain.workspace.models import WorkspaceId
from infrastructure.knowledge.extraction import Extractors, UnsupportedDocumentError
from infrastructure.knowledge.retriever import HybridRetriever
from infrastructure.knowledge.store import (
    InMemoryKnowledgeStore,
    SqlKnowledgeStore,
    pack,
    unpack,
)
from infrastructure.llm.embeddings import RoutedEmbeddings


class FakeEmbeddings:
    """Implements `domain.knowledge.protocols.EmbeddingProvider`, arithmetically.

    A bag of words as three numbers. Crude on purpose: what the tests are about
    is what the platform does with vectors - writes down which model made them,
    refuses to compare across models, blends the two searches - and a real model
    would make every one of those assertions depend on a download.
    """

    def __init__(self, model: str = "fake-embed", *, dimension: int = 3) -> None:
        self._model = model
        self._dimension = dimension

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimension(self) -> int:
        return self._dimension

    async def embed(self, texts):
        vectors = []
        for text in texts:
            words = text.casefold().split()
            counts = [
                float(sum(1 for word in words if word.startswith(letter)))
                for letter in ("d", "r", "p")
            ]
            vectors.append(tuple(counts[: self._dimension] or [1.0]))
        return vectors


class BrokenEmbeddings(FakeEmbeddings):
    async def embed(self, texts):
        raise RuntimeError("the embedding server is not answering")


#: The default for the helper below, as a value rather than a call in a
#: default - ruff will not have one, and typer's rule applies here too.
_EMBEDDINGS = FakeEmbeddings()


def service(store=None, embeddings=_EMBEDDINGS) -> KnowledgeService:
    return KnowledgeService(
        store=store or InMemoryKnowledgeStore(),
        extractors=Extractors(),
        embeddings=embeddings,
    )


# --- Cutting ------------------------------------------------------------------


def test_a_passage_is_built_from_paragraphs_not_from_offsets() -> None:
    text = "\n\n".join(f"Paragraph {index}. " + "word " * 40 for index in range(6))

    passages = chunk(text, size=400, overlap=0)

    assert len(passages) > 1
    assert all(len(passage) <= 400 for passage in passages)
    assert passages[0].startswith("Paragraph 0.")


def test_passages_overlap_so_a_fact_that_straddles_a_boundary_survives() -> None:
    text = "\n\n".join(f"Sentence {index} about deliveries." for index in range(40))

    passages = chunk(text, size=200, overlap=60)

    tails = [passage[-60:] for passage in passages[:-1]]
    assert any(tail.split()[0] in passages[index + 1] for index, tail in enumerate(tails))


def test_a_single_paragraph_longer_than_the_budget_is_cut_on_sentences() -> None:
    text = " ".join(f"This is sentence {index}." for index in range(200))

    passages = chunk(text, size=300)

    assert len(passages) > 1
    assert all(passage.strip().endswith(".") for passage in passages)


# --- Extraction ---------------------------------------------------------------


def test_a_format_nothing_reads_is_refused_by_name(tmp_path: Path) -> None:
    """Silent is the failure that matters: mojibake chunks and retrieves fine."""
    unreadable = tmp_path / "photo.tiff"
    unreadable.write_bytes(b"\x00\x01")

    with pytest.raises(UnsupportedDocumentError) as refused:
        Extractors().extract(unreadable)

    assert ".tiff" in str(refused.value)


def test_html_is_read_as_words_and_not_as_tags(tmp_path: Path) -> None:
    page = tmp_path / "page.html"
    page.write_text(
        "<html><head><style>p{color:red}</style></head>"
        "<body><p>Delivery takes three days.</p><script>x=1</script></body></html>",
        encoding="utf-8",
    )

    text = Extractors().extract(page)

    assert "Delivery takes three days." in text
    assert "color:red" not in text and "x=1" not in text


def test_a_word_document_is_read_as_its_paragraphs(tmp_path: Path) -> None:
    """Found in the window: a .docx was refused as a format nothing reads."""
    from zipfile import ZipFile

    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = (
        f'<w:document xmlns:w="{ns}"><w:body>'
        "<w:p><w:r><w:t>Warranty</w:t></w:r>"
        '<w:r><w:t xml:space="preserve"> policy.</w:t></w:r></w:p>'
        "<w:p><w:r><w:t>Two years from delivery.</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    document = tmp_path / "warranty.docx"
    with ZipFile(document, "w") as archive:
        archive.writestr("word/document.xml", body)

    assert Extractors().extract(document) == "Warranty policy.\n\nTwo years from delivery."


def test_an_old_word_file_is_refused_with_what_to_do(tmp_path: Path) -> None:
    legacy = tmp_path / "old.docx"
    legacy.write_bytes(b"not a zip")

    with pytest.raises(UnsupportedDocumentError, match=r"saved as \.docx"):
        Extractors().extract(legacy)


# --- Adding -------------------------------------------------------------------


async def test_a_document_is_stored_cut_and_embedded(tmp_path: Path) -> None:
    source = tmp_path / "policy.md"
    source.write_text(
        "Delivery takes three days.\n\nReturns run for thirty days.", encoding="utf-8"
    )
    knowledge = service()

    document = await knowledge.add_file(source)

    assert document.status is DocumentStatus.INDEXED
    passages = await knowledge.passages(document.id)
    assert passages and all(passage.embedding_model == "fake-embed" for passage in passages)
    assert all(passage.embedding_dimension == 3 for passage in passages)


async def test_the_same_text_added_twice_is_one_document(tmp_path: Path) -> None:
    source = tmp_path / "policy.md"
    source.write_text("Delivery takes three days.", encoding="utf-8")
    knowledge = service()

    first = await knowledge.add_file(source)
    second = await knowledge.add_file(source)

    assert first.id == second.id
    assert len(await knowledge.list()) == 1


async def test_a_machine_with_no_embedding_model_still_gets_a_searchable_document(
    tmp_path: Path,
) -> None:
    """EXTRACTED is a real state: the text is there, the meaning is not yet."""
    source = tmp_path / "policy.md"
    source.write_text("Delivery takes three days.", encoding="utf-8")

    document = await service(embeddings=None).add_file(source)

    assert document.status is DocumentStatus.EXTRACTED
    assert document.is_searchable


async def test_an_embedding_server_that_is_down_does_not_lose_the_upload(
    tmp_path: Path,
) -> None:
    source = tmp_path / "policy.md"
    source.write_text("Delivery takes three days.", encoding="utf-8")
    store = InMemoryKnowledgeStore()

    document = await service(store, embeddings=BrokenEmbeddings()).add_file(source)

    assert document.status is DocumentStatus.EXTRACTED
    assert await store.chunks_for(document.id), "the text is kept, only the vectors are missing"


async def test_a_file_with_nothing_in_it_is_refused(tmp_path: Path) -> None:
    empty = tmp_path / "empty.md"
    empty.write_text("   ", encoding="utf-8")

    with pytest.raises(PrometheusError):
        await service().add_file(empty)


async def test_reindexing_embeds_with_whatever_model_is_configured_now(
    tmp_path: Path,
) -> None:
    """The case the two columns on a chunk exist for (ADR 0016)."""
    source = tmp_path / "policy.md"
    source.write_text("Delivery takes three days.", encoding="utf-8")
    store = InMemoryKnowledgeStore()
    document = await service(store, embeddings=None).add_file(source)

    updated = await service(store, embeddings=FakeEmbeddings("second-model")).reindex(
        document.id
    )

    assert updated.status is DocumentStatus.INDEXED
    assert {passage.embedding_model for passage in await store.chunks_for(document.id)} == {
        "second-model"
    }


# --- Retrieval ----------------------------------------------------------------


async def build_corpus(store: InMemoryKnowledgeStore, embeddings) -> None:
    knowledge = KnowledgeService(store=store, extractors=Extractors(), embeddings=embeddings)
    await knowledge.add_text(
        "Delivery takes three days. Dispatch happens daily.",
        title="Delivery policy",
        workspace_id=WorkspaceId("work"),
    )
    await knowledge.add_text(
        "Refunds are paid within ten days of receiving the return.",
        title="Refund policy",
        workspace_id=WorkspaceId("work"),
    )


async def test_a_passage_comes_back_with_the_document_it_came_from() -> None:
    store = InMemoryKnowledgeStore()
    await build_corpus(store, FakeEmbeddings())

    found = await HybridRetriever(store, embeddings=FakeEmbeddings()).retrieve(
        KnowledgeQuery(text="dispatch delivery", workspace_id=WorkspaceId("work"))
    )

    assert found
    assert found[0].title == "Delivery policy"
    assert found[0].citation.startswith("Delivery policy")


async def test_retrieval_stays_inside_the_workspace_that_asked() -> None:
    store = InMemoryKnowledgeStore()
    await build_corpus(store, FakeEmbeddings())

    found = await HybridRetriever(store, embeddings=FakeEmbeddings()).retrieve(
        KnowledgeQuery(text="delivery", workspace_id=WorkspaceId("personal"))
    )

    assert found == [], "another workspace's documents are not this workspace's knowledge"


async def test_a_machine_with_no_embedding_model_retrieves_lexically_and_says_so() -> None:
    store = InMemoryKnowledgeStore()
    await build_corpus(store, None)

    found = await HybridRetriever(store, embeddings=None).retrieve(
        KnowledgeQuery(text="refunds", workspace_id=WorkspaceId("work"))
    )

    assert found and found[0].title == "Refund policy"
    assert found[0].semantic == 0.0 and found[0].lexical > 0.0


async def test_vectors_from_another_model_are_not_compared() -> None:
    """A name that matches while the length does not is the silent case."""
    store = InMemoryKnowledgeStore()
    await build_corpus(store, FakeEmbeddings("first-model"))

    found = await HybridRetriever(store, embeddings=FakeEmbeddings("second-model")).retrieve(
        KnowledgeQuery(text="delivery", workspace_id=WorkspaceId("work"))
    )

    assert all(passage.semantic == 0.0 for passage in found), "stale vectors are skipped"
    assert found, "and the text index still answers"


# --- Ranking ------------------------------------------------------------------


def test_a_passage_found_by_both_searches_beats_one_found_by_either() -> None:
    assert blend(1.0, 1.0) > blend(1.0, 0.0) > 0
    assert blend(0.0, 1.0) > blend(1.0, 0.0), "meaning outweighs words, and both count"


def test_scores_are_normalised_to_their_own_best_hit() -> None:
    assert normalise({"a": 2.0, "b": 1.0}) == {"a": 1.0, "b": 0.5}
    assert normalise({"a": 0.0}) == {}, "nothing matched is not everything matched"


def test_a_vector_with_no_direction_is_not_similar_to_anything() -> None:
    assert cosine((0.0, 0.0), (1.0, 1.0)) == 0.0
    assert cosine((1.0, 0.0), (1.0, 0.0)) == 1.0


# --- Storage ------------------------------------------------------------------


def test_a_vector_survives_the_round_trip_through_bytes() -> None:
    vector = (0.25, -0.5, 0.75)

    assert unpack(pack(vector)) == vector
    assert unpack(None) is None


async def test_documents_and_passages_survive_a_restart(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlKnowledgeStore(session_factory)
    knowledge = KnowledgeService(
        store=store, extractors=Extractors(), embeddings=FakeEmbeddings()
    )
    document = await knowledge.add_text("Delivery takes three days.", title="Delivery")

    reopened = SqlKnowledgeStore(session_factory)

    assert (await reopened.get(document.id)).title == "Delivery"
    passages = await reopened.chunks_for(document.id)
    assert passages[0].embedding is not None
    assert passages[0].embedding_model == "fake-embed"


async def test_deleting_a_document_takes_its_passages_with_it(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlKnowledgeStore(session_factory)
    knowledge = KnowledgeService(
        store=store, extractors=Extractors(), embeddings=FakeEmbeddings()
    )
    document = await knowledge.add_text("Delivery takes three days.", title="Delivery")

    assert await knowledge.delete(document.id)
    assert await store.chunks_for(document.id) == []


async def test_the_text_index_finds_a_passage_by_a_word_in_it(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The FTS half, against the real index rather than the in-memory stand-in."""
    store = SqlKnowledgeStore(session_factory)
    knowledge = KnowledgeService(store=store, extractors=Extractors(), embeddings=None)
    await knowledge.add_text(
        "Refunds are paid within ten days.", title="Refunds", workspace_id=WorkspaceId("work")
    )

    found = await HybridRetriever(store).retrieve(
        KnowledgeQuery(text="how long do refunds take?", workspace_id=WorkspaceId("work"))
    )

    assert found and "Refunds" in found[0].title


class FixedEmbeddings:
    """Every text that mentions a word gets one direction, everything else another."""

    def __init__(self, word: str) -> None:
        self._word = word

    @property
    def model(self) -> str:
        return "fixed"

    @property
    def dimension(self) -> int:
        return 2

    async def embed(self, texts):
        return [
            (1.0, 0.0) if self._word in text.casefold() else (0.3, 0.95) for text in texts
        ]


async def test_one_unrelated_document_is_not_returned_for_everything() -> None:
    """Found on a workspace with one document: "Hello" came back with the
    delivery policy, because normalising made its weak similarity the best."""
    store = InMemoryKnowledgeStore()
    embeddings = FixedEmbeddings("delivery")
    knowledge = KnowledgeService(store=store, extractors=Extractors(), embeddings=embeddings)
    await knowledge.add_text(
        "Express delivery arrives the next working day and costs twelve euros.",
        title="Delivery policy",
        workspace_id=WorkspaceId("work"),
    )
    retriever = HybridRetriever(store, embeddings=embeddings, min_similarity=0.53)

    unrelated = await retriever.retrieve(
        KnowledgeQuery(text="what is the capital of France?", workspace_id=WorkspaceId("work"))
    )
    related = await retriever.retrieve(
        KnowledgeQuery(text="when does delivery arrive?", workspace_id=WorkspaceId("work"))
    )

    assert unrelated == [], "a shared 'the' is not an answer"
    assert related and related[0].title == "Delivery policy"


def test_word_coverage_counts_the_question_not_the_passage() -> None:
    assert coverage("INV-2291", "Invoice INV-2291 was paid") == 1.0
    assert coverage("what is the capital of France?", "the delivery policy") == 0.25
    assert coverage("?!", "anything") == 0.0


async def test_changing_the_embedding_model_reindexes_what_the_old_one_embedded() -> None:
    store = InMemoryKnowledgeStore()
    await build_corpus(store, FakeEmbeddings("first-model"))
    later = KnowledgeService(
        store=store, extractors=Extractors(), embeddings=FakeEmbeddings("second-model")
    )

    assert await later.reindex_stale(workspace_id=WorkspaceId("work")) == 2
    assert await later.reindex_stale(workspace_id=WorkspaceId("work")) == 0, "once is enough"
    for document in await store.list(workspace_id=WorkspaceId("work")):
        assert {c.embedding_model for c in await store.chunks_for(document.id)} == {"second-model"}


async def test_the_embedding_model_is_whichever_is_chosen_at_the_moment_of_the_call() -> None:
    """A choice made in the window reaches indexing without a restart."""
    current: list = [FakeEmbeddings("first-model")]
    routed = RoutedEmbeddings(lambda: current[0])

    assert routed.model == "first-model"
    current[0] = FakeEmbeddings("second-model")
    assert routed.model == "second-model"
    current[0] = None
    assert routed.model == ""
    with pytest.raises(PrometheusError):
        await routed.embed(["text"])


async def test_a_newer_version_of_the_file_replaces_the_document_in_place(tmp_path: Path) -> None:
    """Not a second document: two that disagree would both be quoted as current."""
    first = tmp_path / "policy.md"
    first.write_text("Delivery takes three days.", encoding="utf-8")
    store = InMemoryKnowledgeStore()
    knowledge = service(store)
    document = await knowledge.add_file(first)

    second = tmp_path / "policy-v2.md"
    second.write_text("Delivery takes two days.", encoding="utf-8")
    updated = await knowledge.replace_file(document.id, second)

    assert updated.id == document.id
    assert updated.source == str(second)
    assert [c.content for c in await store.chunks_for(document.id)] == ["Delivery takes two days."]
    assert len(await store.list()) == 1
