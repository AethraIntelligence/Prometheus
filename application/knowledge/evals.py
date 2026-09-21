"""Retrieval evals for documents: is what comes back the answer, and only it?

The same argument as `application/memory/evals.py`, one store over. Unit tests
say the blend is arithmetically right; these say the *retrieval* answers a
question a person would ask, on a realistic mix - several documents, several
workspaces, a document embedded by a model since replaced - and they run against
whichever store and whichever embedding model are handed in. Until this existed,
knowledge was the one part of the platform that could only be reported as "does
not fail", never as "works": memory had thresholds and documents had nothing.

Four properties, each with a threshold stated before anything is run:

* **relevance** - the passage that answers the question comes back, and comes
  back first. A retrieval that has it in fourth place has it where a budget of
  three will cut it off.
* **precision** - a question this workspace has nothing to say about is
  answered with nothing. This is the property that regressed in Phase 15 - one
  document in a workspace came back for "Hello", because a fraction of the best
  hit makes the best hit 1.0 however unrelated - and it is the one that puts
  text in front of a model as though it were evidence.
* **groundedness** - every passage carries the title and source it is to be
  quoted with. A quotation whose origin was dropped is a memory, and this is
  deliberately not one (ADR 0016).
* **isolation** - nothing crosses a workspace, and a query naming documents is
  answered from those documents. The threshold is 1.0 and is not a knob: one
  leak is the failure, not a lower score.

**A case that needs meaning says so.** "How do I get my money back" finds a
passage about refunds only if something embedded it, and on a machine with no
embedding model those cases are not failures - they are questions that could not
be asked. They are reported as skipped, and the run is scored on the rest. The
alternative - a fake embedder inside the eval - would measure the fake, which is
exactly what these exist to stop being done.

**Two of the precision cases need meaning, and that is a finding rather than a
convenience.** Asked on a machine with an embedding model, "what is the capital
of France?" retrieves nothing: the model looks at each passage, rejects it, and
`MIN_WORD_COVERAGE` throws out what is left on a shared "the". With no model
there is no first half to that sentence - nothing has rejected anything - so the
coverage floor is not applied and the question comes back with passages, on both
backends. That is the known cost of the lexical-only path and not something a
change could regress, so the cases say `needs_meaning` and the limit is written
here. It would be dishonest either way round: failing a machine for a mode the
platform offers deliberately, or quietly scoring it as though it were precise.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import UUID

from domain.knowledge.models import Chunk, Document, DocumentStatus, KnowledgeQuery
from domain.knowledge.protocols import EmbeddingProvider, KnowledgeStore, Retriever
from domain.workspace.models import WorkspaceId

RELEVANCE = "relevance"
PRECISION = "precision"
GROUNDEDNESS = "groundedness"
ISOLATION = "isolation"

THRESHOLDS: dict[str, float] = {
    RELEVANCE: 1.0,
    PRECISION: 1.0,
    GROUNDEDNESS: 1.0,
    ISOLATION: 1.0,
}

HOME = WorkspaceId("eval-home")
AWAY = WorkspaceId("eval-away")

#: Fixed ids, so a failure names the same document on every run and two runs of
#: the eval can be compared line by line.
DELIVERY = UUID("00000000-0000-4000-8000-0000000c0001")
REFUNDS = UUID("00000000-0000-4000-8000-0000000c0002")
CV = UUID("00000000-0000-4000-8000-0000000c0003")
AWAY_NOTES = UUID("00000000-0000-4000-8000-0000000c0004")
STALE = UUID("00000000-0000-4000-8000-0000000c0005")

#: What the document embedded by a model since replaced says it was embedded by.
#: Anything but the model being asked with; the point is that the two are not
#: comparable, not which one it was.
RETIRED_MODEL = "retired-embedding-model"


@dataclass(frozen=True, slots=True)
class EvalDocument:
    """One document of the corpus: what it is, and the passages it is cut into.

    The passages are written rather than produced by the chunker. What is being
    measured is retrieval, and a corpus that changed shape because the chunk
    size changed would report a retrieval regression that never happened.
    """

    id: UUID
    title: str
    source: str
    passages: tuple[str, ...]
    workspace_id: WorkspaceId = HOME
    #: Which model these passages claim to have been embedded by. Empty means
    #: whichever one the run is using, so they are comparable.
    embedded_by: str = ""


@dataclass(frozen=True, slots=True)
class EvalCase:
    name: str
    metric: str
    question: str
    workspace_id: WorkspaceId = HOME
    document_ids: frozenset[UUID] = frozenset()
    limit: int = 4
    #: Documents at least one passage of which must come back.
    expected: frozenset[UUID] = frozenset()
    #: Documents no passage of which may come back.
    forbidden: frozenset[UUID] = frozenset()
    #: The document the best-scoring passage must belong to, if any.
    first: UUID | None = None
    #: Nothing at all may come back.
    empty: bool = False
    #: Asked only where something can embed. Skipped, never failed, otherwise.
    needs_meaning: bool = False


@dataclass(frozen=True, slots=True)
class CaseResult:
    case: EvalCase
    returned: tuple[str, ...]
    problems: tuple[str, ...] = ()
    skipped: bool = False

    @property
    def passed(self) -> bool:
        return not self.problems


@dataclass(frozen=True, slots=True)
class KnowledgeReport:
    backend: str
    #: Which model embedded this run, or empty where it retrieved on words
    #: alone. Printed, because the same corpus scores differently under two
    #: models and a score with no model beside it is not a measurement.
    model: str = ""
    results: tuple[CaseResult, ...] = ()
    thresholds: dict[str, float] = field(default_factory=lambda: dict(THRESHOLDS))

    @property
    def scores(self) -> dict[str, float]:
        scores: dict[str, float] = {}
        for metric in self.thresholds:
            chosen = [
                result
                for result in self.results
                if result.case.metric == metric and not result.skipped
            ]
            scores[metric] = (
                sum(result.passed for result in chosen) / len(chosen) if chosen else 1.0
            )
        return scores

    @property
    def skipped(self) -> tuple[CaseResult, ...]:
        return tuple(result for result in self.results if result.skipped)

    @property
    def passed(self) -> bool:
        scores = self.scores
        return all(scores[metric] >= floor for metric, floor in self.thresholds.items())


def corpus() -> tuple[EvalDocument, ...]:
    """The fixed documents every eval run starts from.

    Ordinary prose about ordinary things, and no two documents answering one
    question. A corpus of near-duplicates would measure tie-breaking, which is
    not what anybody is retrieving for.
    """
    return (
        EvalDocument(
            id=DELIVERY,
            title="Delivery policy",
            source="/eval/delivery-policy.md",
            passages=(
                "Orders placed before noon are shipped the same working day. "
                "Domestic delivery takes five working days from dispatch.",
                "International parcels are tracked and arrive within fourteen "
                "working days. Customs delays are outside our control.",
            ),
        ),
        EvalDocument(
            id=REFUNDS,
            title="Refund rules",
            source="/eval/refunds.md",
            passages=(
                "A customer may return an unopened item within thirty days and "
                "have the price reimbursed to the card that paid for it.",
            ),
        ),
        EvalDocument(
            id=CV,
            title="Denys Zhodik CV",
            source="/eval/cv.pdf",
            passages=(
                "Denys Zhodik is an engineer who has worked on distributed "
                "systems, developer platforms and local-first software.",
            ),
        ),
        EvalDocument(
            id=AWAY_NOTES,
            title="Another client's delivery notes",
            source="/eval/away-delivery.md",
            passages=(
                "Delivery for this client is by courier and takes two working "
                "days. Orders are shipped from the northern warehouse.",
            ),
            workspace_id=AWAY,
        ),
        EvalDocument(
            id=STALE,
            title="Warehouse handbook",
            source="/eval/warehouse.md",
            passages=(
                "Goods are picked from the northern aisle and consolidated "
                "before they leave the building each evening.",
            ),
            embedded_by=RETIRED_MODEL,
        ),
    )


def cases() -> tuple[EvalCase, ...]:
    return (
        EvalCase(
            "the delivery policy answers a question about delivery",
            RELEVANCE,
            "how long does delivery take?",
            expected=frozenset({DELIVERY}),
            first=DELIVERY,
        ),
        EvalCase(
            "a question about refunds finds the refund rules, which never say "
            "the word",
            RELEVANCE,
            "how do I get my money back?",
            expected=frozenset({REFUNDS}),
            first=REFUNDS,
            needs_meaning=True,
        ),
        EvalCase(
            "a name finds the document that carries it",
            RELEVANCE,
            "Denys Zhodik",
            expected=frozenset({CV}),
            first=CV,
        ),
        EvalCase(
            "a question this workspace has nothing to say about is answered "
            "with nothing",
            PRECISION,
            "what is the capital of France?",
            empty=True,
            needs_meaning=True,
        ),
        EvalCase(
            "a greeting retrieves nothing",
            PRECISION,
            "Hello",
            empty=True,
            needs_meaning=True,
        ),
        EvalCase(
            "a question about delivery does not drag in the CV",
            PRECISION,
            "how long does delivery take?",
            # The CV, and not every other document. Asked of the model this
            # machine runs, the refund rules score 0.64 against this question
            # and the CV 0.41: one is a policy about the same transaction and
            # the other is about a person. Forbidding the refund rules would be
            # demanding the retrieval discriminate more finely than the model
            # it is built on, which is a threshold nobody could meet and an
            # eval that would be turned off rather than fixed.
            forbidden=frozenset({CV}),
        ),
        EvalCase(
            "every passage can be quoted with where it came from",
            GROUNDEDNESS,
            "delivery and refunds",
        ),
        EvalCase(
            "another workspace's delivery notes are never retrieved",
            ISOLATION,
            "how long does delivery take?",
            forbidden=frozenset({AWAY_NOTES}),
        ),
        EvalCase(
            "a query naming a document is answered from that document",
            ISOLATION,
            "how long does delivery take?",
            document_ids=frozenset({REFUNDS}),
            forbidden=frozenset({DELIVERY, CV, AWAY_NOTES, STALE}),
        ),
        EvalCase(
            "a passage embedded by a retired model is not matched by meaning",
            ISOLATION,
            "where are items collected before dispatch?",
            forbidden=frozenset({STALE}),
            needs_meaning=True,
        ),
    )


async def seed(
    store: KnowledgeStore, *, embeddings: EmbeddingProvider | None = None
) -> dict[UUID, str]:
    """Write the corpus, embedding it with whatever this run is using.

    Written through the store rather than through `KnowledgeService`, because
    the passages are fixed: putting them through the chunker would make a
    change of chunk size look like a change in retrieval.
    """
    model = embeddings.model if embeddings is not None else ""
    titles: dict[UUID, str] = {}
    for document in corpus():
        record = Document(
            id=document.id,
            workspace_id=document.workspace_id,
            title=document.title,
            source=document.source,
            media_type="text/plain",
            status=DocumentStatus.INDEXED if model else DocumentStatus.EXTRACTED,
            chunk_count=len(document.passages),
        )
        await store.save(record)
        titles[document.id] = document.title
        chunks = [
            Chunk.create(record, ordinal, passage)
            for ordinal, passage in enumerate(document.passages)
        ]
        if embeddings is not None and model:
            vectors = await embeddings.embed([one.content for one in chunks])
            chunks = [
                one.embedded_by(document.embedded_by or model, vector)
                for one, vector in zip(chunks, vectors, strict=True)
            ]
        await store.replace_chunks(record.id, chunks)
    return titles


async def run_knowledge_evals(
    make_store: Callable[[], KnowledgeStore],
    make_retriever: Callable[[KnowledgeStore], Retriever],
    *,
    embeddings: EmbeddingProvider | None = None,
    backend: str = "",
) -> KnowledgeReport:
    """Seed a fresh store and ask every case of it."""
    store = make_store()
    titles = await seed(store, embeddings=embeddings)
    retriever = make_retriever(store)
    model = embeddings.model if embeddings is not None else ""

    results: list[CaseResult] = []
    for case in cases():
        if case.needs_meaning and not model:
            results.append(CaseResult(case=case, returned=(), skipped=True))
            continue
        found = await retriever.retrieve(
            KnowledgeQuery(
                text=case.question,
                workspace_id=case.workspace_id,
                limit=case.limit,
                document_ids=case.document_ids,
            )
        )
        seen = [passage.chunk.document_id for passage in found]
        returned = tuple(titles.get(one, str(one)) for one in seen)
        problems: list[str] = []
        if case.empty and found:
            problems.append(f"{len(found)} passage(s) came back for a question with no answer")
        for document_id in sorted(case.expected - set(seen), key=str):
            problems.append(f"{titles.get(document_id, document_id)} was not retrieved")
        for document_id in sorted(case.forbidden & set(seen), key=str):
            problems.append(
                f"{titles.get(document_id, document_id)} was retrieved and must not be"
            )
        if case.first is not None and (not seen or seen[0] != case.first):
            problems.append(
                f"{titles.get(case.first, case.first)} did not rank first; "
                f"got {returned[0] if returned else 'nothing'}"
            )
        if case.metric == GROUNDEDNESS:
            for passage in found:
                if not passage.title or not passage.source:
                    problems.append(f"a passage came back with no citation: {passage.citation}")
        results.append(CaseResult(case=case, returned=returned, problems=tuple(problems)))
    return KnowledgeReport(backend=backend, model=model, results=tuple(results))


def render_report(report: KnowledgeReport) -> str:
    lines = [
        f"Document retrieval evals ({report.backend or 'in-memory'}"
        f", {report.model or 'no embedding model - words only'})"
    ]
    scores = report.scores
    for metric, floor in report.thresholds.items():
        mark = "ok" if scores[metric] >= floor else "BELOW"
        lines.append(f"  {metric:<13} {scores[metric]:.2f} (threshold {floor:.2f}) {mark}")
    for result in report.results:
        if not result.passed:
            lines.append(f"  FAILED [{result.case.metric}] {result.case.name}")
            lines.extend(f"    - {problem}" for problem in result.problems)
    for result in report.skipped:
        lines.append(f"  skipped (needs an embedding model) {result.case.name}")
    lines.append("PASSED" if report.passed else "FAILED")
    return "\n".join(lines)
