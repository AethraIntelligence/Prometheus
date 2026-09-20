"""A decider that answers from a script, for the callers of `Decider`."""

from __future__ import annotations

from collections.abc import Sequence

from domain.decisions.models import ChoiceAnswer, ChoiceQuestion


class FakeDecider:
    """Implements `domain.decisions.protocols.Decider` by replaying answers.

    An exhausted script answers "unreadable", which is what a caller has to
    survive anyway, rather than raising in the middle of the code under test.
    """

    def __init__(self, answers: Sequence[ChoiceAnswer | Exception] = ()) -> None:
        self._answers: list[ChoiceAnswer | Exception] = list(answers)
        self.questions: list[ChoiceQuestion] = []

    async def choose(self, question: ChoiceQuestion) -> ChoiceAnswer:
        self.questions.append(question)
        if not self._answers:
            return ChoiceAnswer.unreadable("fake")
        answer = self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
