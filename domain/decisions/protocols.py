from __future__ import annotations

from typing import Protocol

from domain.decisions.models import ChoiceAnswer, ChoiceQuestion


class Decider(Protocol):
    """Answers typed questions.

    Two kinds of backend sit behind it and a caller cannot tell which: a text
    model asked to answer with one letter, and a model built to decide rather
    than to write. Both are chosen by routing the `DECISION` kind of work.
    """

    async def choose(self, question: ChoiceQuestion) -> ChoiceAnswer: ...
