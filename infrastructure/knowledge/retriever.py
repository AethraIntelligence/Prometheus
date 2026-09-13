"""Finding the passages that answer a question.

Two searches and one blend. The text index says which passages mention the
words; the embedding of the question is compared with the embedding of every
passage. Both halves come back normalised to their own best hit, and
`domain/knowledge/ranking.py` decides what the pair is worth - which is where
the policy lives, so that replacing either half is replacing an adapter.

**A machine with no embedding model still retrieves.** The semantic half is
simply absent, the lexical half answers, and the passage says which half found
it. That matters twice: `clone && run` keeps working with no model configured,
and a retrieval that has quietly become lexical-only is visible rather than
inferred from disappointing answers.

**A vector written by another model is not compared.** The chunk states what
embedded it, and a mismatch drops it out of the semantic half instead of
producing a number from two things that mean different things (ADR 0016). The
count is logged, because that number is exactly "how much of this workspace
needs re-indexing".
"""

from __future__ import annotations

import structlog

from domain.knowledge.models import KnowledgeQuery, Passage
from domain.knowledge.protocols import EmbeddingProvider, PassageIndex
from domain.knowledge.ranking import (
    CUTOFF_RATIO,
    MIN_WORD_COVERAGE,
    blend,
    cosine,
    coverage,
    normalise,
)

log = structlog.get_logger(__name__)

#: How many passages the two searches may look at before ranking cuts them
#: down. Wider than the limit because a passage that is fourth lexically and
#: first semantically has to survive to be blended.
CANDIDATE_FACTOR = 8


class HybridRetriever:
    """Implements `domain.knowledge.protocols.Retriever`."""

    def __init__(
        self,
        index: PassageIndex,
        *,
        embeddings: EmbeddingProvider | None = None,
        cutoff: float = CUTOFF_RATIO,
        min_similarity: float = 0.0,
    ) -> None:
        """`min_similarity` is the raw cosine a passage needs before the
        semantic half counts it at all. It belongs to the embedding model - on
        this machine's bge-m3 and nomic-embed-text, unrelated questions scored
        up to 0.51 and relevant ones from 0.56 - so it is configuration, not a
        constant here. Zero is no floor."""
        self._index = index
        self._embeddings = embeddings
        self._cutoff = cutoff
        self._min_similarity = min_similarity

    async def retrieve(self, query: KnowledgeQuery) -> list[Passage]:
        if not query.text.strip() or query.limit <= 0:
            return []
        candidates = await self._index.candidates(
            workspace_id=query.workspace_id, document_ids=query.document_ids
        )
        if not candidates:
            return []

        lexical = await self._index.matching(
            query.text, max(query.limit * CANDIDATE_FACTOR, 20)
        )
        semantic, compared = await self._semantic(query, candidates)

        passages = []
        for chunk, title, source in candidates:
            key = str(chunk.id)
            lexical_score = lexical.get(key, 0.0)
            semantic_score = semantic.get(key, 0.0)
            if not lexical_score and not semantic_score:
                continue
            if (
                compared
                and not semantic_score
                and coverage(query.text, chunk.content) < MIN_WORD_COVERAGE
            ):
                # The model looked at this passage and did not find the
                # question in it, and all the index has is a shared word.
                continue
            passages.append(
                Passage(
                    chunk=chunk,
                    title=title,
                    source=source,
                    score=blend(lexical_score, semantic_score),
                    lexical=lexical_score,
                    semantic=semantic_score,
                )
            )
        return self._best(passages, query.limit)

    async def _semantic(
        self, query: KnowledgeQuery, candidates
    ) -> tuple[dict[str, float], bool]:
        """Similarity per passage, and whether a model actually compared any.

        Before normalising, anything under the floor is dropped: a fraction of
        the best hit makes the best hit 1.0 however unrelated it is, and a
        workspace with one document returned it for "Hello".
        """
        if self._embeddings is None or not self._embeddings.model:
            return {}, False
        try:
            vectors = await self._embeddings.embed([query.text])
        except Exception as error:
            # A retrieval that fell back to lexical is worth far more than a run
            # that failed because an embedding server was down, and the log line
            # is what tells somebody the answers got worse for a reason.
            log.warning("knowledge.embedding_failed", error=str(error))
            return {}, False
        if not vectors:
            return {}, False
        asked = vectors[0]
        model, dimension = self._embeddings.model, len(asked)
        scores: dict[str, float] = {}
        stale = compared = 0
        for chunk, _, _ in candidates:
            if not chunk.comparable_with(model, dimension):
                stale += 1
                continue
            compared += 1
            similarity = cosine(chunk.embedding or (), asked)
            if similarity > 0 and similarity >= self._min_similarity:
                scores[str(chunk.id)] = similarity
        if stale:
            log.info("knowledge.chunks_need_reindexing", count=stale, model=model)
        return normalise(scores), compared > 0

    def _best(self, passages: list[Passage], limit: int) -> list[Passage]:
        ranked = sorted(passages, key=lambda passage: -passage.score)
        if not ranked:
            return []
        floor = ranked[0].score * self._cutoff
        return [passage for passage in ranked[:limit] if passage.score >= floor]
