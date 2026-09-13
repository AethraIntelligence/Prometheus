"""What a sentence from the user turns out to be asking for.

Between "the user typed something" and "there is a plan" sits a question the
plan (§7.3, §7.5) treats as two and which is really one: *what is being asked,
and does it need the workforce at all?*

Both are read at once, because they are answered from the same reading. A
sentence that wants a fact back needs no employee, no plan and no tools, and
decomposing it anyway produces a task whose whole content is the question that
was already asked. Decomposition is a means, not the product.

The user's own words are never replaced by this. A restatement is kept beside
them so a misreading is visible next to the sentence it came from, rather than
silently becoming the thing that gets worked on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Intent:
    """Prometheus's reading of one request."""

    #: What Prometheus understood, in its own words. Shown to the user, never worked
    #: from in place of the original.
    restatement: str = ""
    #: Limits the user stated for *this* request: a count, a format, a place, a
    #: deadline. They describe the work in hand and expire with it.
    constraints: dict[str, Any] = field(default_factory=dict)
    #: Standing preferences: how the user wants work done here from now on, in
    #: their own words. Kept apart from `constraints` because the two look alike
    #: and behave nothing alike - "in the sales folder" is where this job is,
    #: "always in Markdown" is how every job should end. Remembering the first
    #: as though it were the second points the next request at the wrong folder,
    #: which is exactly what it did the first time they were one field.
    preferences: tuple[str, ...] = ()
    #: What would have to be true for the user to call this done.
    acceptance_criteria: tuple[str, ...] = ()
    #: Whether this needs work doing, as opposed to an answer giving.
    needs_work: bool = True
    #: The answer, when it needs none. Empty otherwise.
    answer: str = ""

    @property
    def is_answerable_directly(self) -> bool:
        return not self.needs_work and bool(self.answer.strip())

    @property
    def is_conversation(self) -> bool:
        """Answered on the spot and left unchecked: no work, and no standard.

        A request that asks for anything to exist afterwards gets a criterion
        saying so, and is then checked like any other direct answer. The one
        left is talk - and a check with nothing to check against has only ever
        disagreed with it.
        """
        return self.is_answerable_directly and not self.acceptance_criteria
