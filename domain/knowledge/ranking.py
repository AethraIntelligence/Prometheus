"""How a lexical hit and a semantic hit are compared with each other.

The problem is that they are not the same number. A text index answers with a
rank whose scale is its own; a vector store answers with a similarity. Neither
means anything to the other, and taking the larger of the two would mean
whichever index happened to be more generous always won.

So both are normalised to a fraction of the best hit *of their own kind* before
this sees them - the rule Phase 9 already applies to FTS5 rank - and this blends
what is left. A passage found by both is worth more than a passage found by one,
which is the whole reason for running two searches, and the weights say which
half is trusted more when they disagree.

Deliberately not a model call. Ranking happens on every retrieval, and a call
per retrieval would make reading a document more expensive than working with it
- the same reasoning that keeps `domain/memory/ranking.py` an arithmetic
expression.
"""

from __future__ import annotations

from re import findall

#: Semantic weighs more because it is what this phase exists for: a lexical
#: index finds "shipping" by the word "shipping" and not by the word "delivery".
#: Lexical is kept, and kept substantial, because it is what finds an invoice
#: number, a surname or an error code - the queries where meaning is not the
#: question.
SEMANTIC_WEIGHT = 0.65
LEXICAL_WEIGHT = 0.35

#: How much of the best result a passage has to be worth to come back at all.
#: A retrieval that pads its answer to the limit with whatever scored lowest
#: puts irrelevant text in front of a model as though it were evidence.
CUTOFF_RATIO = 0.35


#: How much of a question a passage has to contain, word for word, to be
#: returned on its words alone when the embedding model did not recognise it.
#: The text index matches any one word, and `normalise` makes the best match
#: worth 1.0 however weak it was - so "what is the capital of France?" came back
#: with a delivery policy because both contain "the". Half keeps what the
#: lexical half is for (an invoice number, a surname: short queries, all there)
#: and drops a passage that shares one word with a sentence.
MIN_WORD_COVERAGE = 0.5


def coverage(question: str, passage: str) -> float:
    """The share of the question's words that the passage contains."""
    asked = {word.casefold() for word in findall(r"\w+", question) if len(word) > 2}
    if not asked:
        return 0.0
    present = {word.casefold() for word in findall(r"\w+", passage)}
    return len(asked & present) / len(asked)


def blend(lexical: float, semantic: float) -> float:
    """One score out of two searches that measured different things."""
    return LEXICAL_WEIGHT * max(lexical, 0.0) + SEMANTIC_WEIGHT * max(semantic, 0.0)


def normalise(scores: dict[str, float]) -> dict[str, float]:
    """Each score as a fraction of the best one, so two searches are comparable.

    An empty search and a search where everything scored zero both come back
    empty rather than dividing by it.
    """
    best = max(scores.values(), default=0.0)
    if best <= 0.0:
        return {}
    return {key: value / best for key, value in scores.items()}


def cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    """Similarity of two vectors, or 0.0 where the question is meaningless.

    Different lengths never reach here - a chunk states what embedded it and a
    mismatch is filtered before comparison (ADR 0016) - but a zero vector does,
    from a passage of punctuation, and it has no direction to compare.
    """
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    return max(min(dot / (left_norm * right_norm), 1.0), -1.0)
