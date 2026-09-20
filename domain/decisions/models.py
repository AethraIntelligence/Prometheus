"""A typed question and its answer: pick one of these, and say how sure.

Several things the manager does are not writing at all. "Talk or work?" and
"who takes this task?" are a choice from a closed list, and asking a model for
them as prose - then reading a letter or a JSON field back out of whatever it
wrote - puts a parser between the platform and the decision it is making. A
question stated as options has an answer that is one of them or is none, and
"none" is a value the caller can act on rather than a guess about what a
paragraph meant.

**Confidence is measured or absent, never asked for.** A model that writes
"confidence: 0.9" has produced two more tokens, not a probability. Where the
backend reports how likely each option was - a text model's token
probabilities, or a model built to decide - the answer carries it; where it
does not, `confidence` is None and a caller treats that as "unknown", which is
not the same as "sure". Repeating the question and counting agreement would
manufacture a number too, at several times the cost of the decision, and was
rejected for that reason.

No vendor and no model is named here. What answers is chosen by routing, like
every other piece of work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite


@dataclass(frozen=True, slots=True)
class Option:
    """One answer on offer. `key` is what the caller branches on; `text` is what is read."""

    key: str
    text: str

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("an option needs a key")
        if not self.text.strip():
            raise ValueError(f"option '{self.key}' needs text")


@dataclass(frozen=True, slots=True)
class ChoiceQuestion:
    """Choose one of `options` given `state`.

    `state` is what is being decided about - a request, a task, a report - and
    `question` is what is asked of it, kept apart because a backend built for
    decisions takes them apart, and a text model reads them better that way too.
    """

    state: str
    question: str
    options: tuple[Option, ...]
    #: What the decision is for, in a word, for the log and the trace.
    purpose: str = ""

    def __post_init__(self) -> None:
        if len(self.options) < 2:
            raise ValueError("a choice needs at least two options")
        keys = [option.key for option in self.options]
        if len(set(keys)) != len(keys):
            raise ValueError("option keys must be distinct")

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(option.key for option in self.options)


@dataclass(frozen=True, slots=True)
class ChoiceAnswer:
    """Which option, how likely each one was, and what answered.

    `key` is None when the backend answered with something that is not an
    option: an unreadable answer is not an answer, and a caller must not be
    handed a default dressed up as one.
    """

    key: str | None
    #: Every option's probability where the backend measured them, summing to
    #: one over the options; empty where it did not.
    probabilities: dict[str, float] = field(default_factory=dict)
    #: How sure the backend is, from 0 to 1, or None when nothing measured it.
    #: A text model's is the chosen option's share of the letters' token
    #: probabilities; a decision model's is its own calibrated statistic over
    #: the distribution. Thresholds are therefore worth checking per backend.
    confidence: float | None = None
    #: Which kind of backend answered - "text model" or "decision model" - for
    #: the trace. Never branched on.
    source: str = ""

    def __post_init__(self) -> None:
        if self.confidence is not None and (
            not isfinite(self.confidence) or not 0 <= self.confidence <= 1
        ):
            raise ValueError("confidence must be between 0 and 1")

    @property
    def answered(self) -> bool:
        return self.key is not None

    def is_sure(self, key: str, *, at_least: float) -> bool:
        """This option, and measured at `at_least` or better - or not measured at all.

        Unmeasured passes on purpose: a backend that reports no probabilities
        still answers, and a platform that only trusted measured answers would
        stop working on every provider that cannot give them. A caller that
        needs a measurement reads `confidence` itself.
        """
        if self.key != key:
            return False
        return self.confidence is None or self.confidence >= at_least

    @classmethod
    def unreadable(cls, source: str = "") -> ChoiceAnswer:
        return cls(key=None, source=source)
