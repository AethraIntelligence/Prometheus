"""A typed question answered by a text model: one letter, and how likely it was.

The options are lettered and the model is asked for the letter alone, so the
answer is one token and the parse is a lookup rather than a reading. Where the
provider reports token probabilities, the ones for the option letters at the
answering position *are* the distribution over the options - measured, one
call, no extra tokens - and the chosen letter's share of them is the
confidence. Where it does not, the answer comes back without one.

**One call per question, always.** Asking again and counting agreement would
also produce a number, at N times the cost and on a local model N times the
wait, for a decision that is one of the cheapest things the manager does.
Rejected on purpose (`domain/decisions/models.py`).

**A provider that refuses the probabilities is asked again without them, once,
and then never again.** Some servers reject the field outright rather than
ignore it; losing the measurement is better than losing the decision, and
asking with it every time would pay for the refusal on every call.

**A model that thinks before it answers is given room to, once it is seen to.**
Five tokens is an answer for a model that answers and nothing at all for one
that opens with `<think>`: every such model in the local catalog came back
empty, and an empty triage is "work", so every greeting was planned. A reply
cut off with nothing visible is asked again with `THINKING_TOKENS`, and the
decider remembers, so only the first question pays twice. Slower, and logged
as such: a model that does not think is the better route for decisions.
"""

from __future__ import annotations

import re
from math import exp
from string import ascii_uppercase

import structlog

from application.prompts import render
from domain.capabilities.models import CapabilityRequirement
from domain.decisions.models import ChoiceAnswer, ChoiceQuestion
from domain.errors import ProviderError
from domain.llm.models import (
    FinishReason,
    LLMRequest,
    LLMResponse,
    Message,
    RoutingHints,
    TaskKind,
    TokenLogprob,
)
from domain.llm.protocols import LLM

log = structlog.get_logger(__name__)

SOURCE = "text model"

#: Enough alternatives at one position to see every letter of a short list.
TOP_LOGPROBS = 5

#: Room for one letter, and a little for a model that pads it.
ANSWER_TOKENS = 5

#: Room for a model that reasons before the letter.
THINKING_TOKENS = 2048

_THOUGHT = re.compile(r"<think>.*?</think>", re.DOTALL)

#: `\boxed{B}` - how a model trained on maths marks a final answer. Read as a
#: letter from the first character, it is "b" from "boxed", whatever is inside.
_STANDALONE = re.compile(r"(?<![A-Za-z0-9])([A-Z])(?![A-Za-z0-9])")

_BOXED = re.compile(r"\\boxed\{\s*(?:\\text\{)?\s*([A-Za-z])\b")


class TextDecider:
    """Implements `domain.decisions.protocols.Decider` over any `LLM`."""

    def __init__(self, llm: LLM) -> None:
        self._llm = llm
        self._measure = True
        self._room = ANSWER_TOKENS

    async def choose(self, question: ChoiceQuestion) -> ChoiceAnswer:
        if len(question.options) > len(ascii_uppercase):
            raise ValueError("a choice can offer at most 26 options")
        letters = {ascii_uppercase[i]: option.key for i, option in enumerate(question.options)}
        prompt = render(
            "decision_choice",
            state=question.state,
            question=question.question,
            options="\n".join(
                f"{letter} - {option.text}"
                for letter, option in zip(letters, question.options, strict=True)
            ),
        )
        response = await self._ask(prompt)
        if (
            self._room == ANSWER_TOKENS
            and response.finish_reason is FinishReason.LENGTH
            and not _visible(response.content).strip()
        ):
            log.info("decision.model_thinks", purpose=question.purpose, room=THINKING_TOKENS)
            self._room = THINKING_TOKENS
            response = await self._ask(prompt)

        found = _letter(response.content, letters)
        if found is None:
            log.warning(
                "decision.unreadable",
                purpose=question.purpose,
                reply=_visible(response.content)[:40],
            )
            return ChoiceAnswer.unreadable(SOURCE)

        letter, offset = found
        probabilities = _distribution(response, letters, letter, offset)
        key = letters[letter]
        return ChoiceAnswer(
            key=key,
            probabilities=probabilities,
            confidence=probabilities.get(key) if probabilities else None,
            source=SOURCE,
        )

    @staticmethod
    def routing() -> tuple[TaskKind, CapabilityRequirement, RoutingHints]:
        """One letter out, so speed and price rank first where nothing is configured."""
        return (
            TaskKind.DECISION,
            CapabilityRequirement(),
            RoutingHints(quality=0.6, cost_sensitivity=0.6, latency_sensitivity=0.8),
        )

    async def _ask(self, prompt: str) -> LLMResponse:
        request = LLMRequest(
            messages=(Message.user(prompt),),
            temperature=0.0,
            max_tokens=self._room,
            top_logprobs=TOP_LOGPROBS if self._measure else None,
        )
        try:
            return await self._llm.generate(request)
        except ProviderError as error:
            if error.transient or not self._measure:
                raise
            log.info("decision.logprobs_refused", error=str(error)[:200])
            self._measure = False
            return await self._llm.generate(
                LLMRequest(
                    messages=request.messages,
                    temperature=request.temperature,
                    max_tokens=request.max_tokens,
                )
            )


def _visible(content: str) -> str:
    """The reply without its reasoning. An unfinished `<think>` leaves nothing."""
    text = _THOUGHT.sub("", content)
    return "" if "<think>" in text else text


def _letter(content: str, letters: dict[str, str]) -> tuple[str, int] | None:
    """The option letter the visible reply gives, and where it stands in `content`.

    Reasoning is skipped, and so is packaging - "**A**", "(b)" and "\\boxed{A}"
    all answered. Otherwise it is the first capital that stands on its own and
    names an option; anything after it is ignored.
    """
    visible = _visible(content)
    skipped = len(content) - len(visible) if visible and content.endswith(visible) else 0
    boxed = _BOXED.search(visible)
    if boxed:
        upper = boxed.group(1).upper()
        return (upper, skipped + boxed.start(1)) if upper in letters else None
    bare = re.sub(r"[^A-Za-z0-9]", "", visible)
    if len(bare) == 1:
        # The whole reply is one character in some packaging: "(b)", "**A**".
        upper = bare.upper()
        return (upper, skipped + visible.index(bare)) if upper in letters else None
    for match in _STANDALONE.finditer(visible):
        # A capital standing on its own, so "Answer: B" is B while "Both",
        # "boxed" and the article in "a lookup" are not letters at all.
        if match.group(1) in letters:
            return match.group(1), skipped + match.start(1)
    return None


def _distribution(
    response: LLMResponse, letters: dict[str, str], letter: str, offset: int
) -> dict[str, float]:
    """Each option's probability, at the position the answering letter was written.

    Only the letters' share is kept and renormalised, so a model that also
    considered writing "The" does not make every option look unlikely. Empty
    when nothing was measured or the position cannot be found.
    """
    position = _position(response.logprobs, response.content, letter, offset)
    if position is None or not position.alternatives:
        # The sampled token alone, renormalised over the letters, is always
        # 1.0 - a certainty nobody measured.
        return {}
    mass: dict[str, float] = {}
    for token, logprob in position.alternatives:
        candidate = token.strip().upper()
        if candidate in letters:
            mass[candidate] = mass.get(candidate, 0.0) + exp(logprob)
    total = sum(mass.values())
    if total <= 0:
        return {}
    return {letters[item]: mass.get(item, 0.0) / total for item in letters}


def _position(
    tokens: tuple[TokenLogprob, ...], content: str, letter: str, offset: int
) -> TokenLogprob | None:
    """The token the answer's letter was written in.

    Where the tokens spell out the reply, the one covering the letter's offset.
    Where they do not - a server that reports the reasoning channel's tokens
    and returns only the final text - the last token that is the letter itself,
    since the answer comes after the reasoning.
    """
    if "".join(token.token for token in tokens) == content:
        start = 0
        for token in tokens:
            end = start + len(token.token)
            if start <= offset < end:
                return token if token.token.strip().upper() == letter else None
            start = end
        return None
    return next((token for token in reversed(tokens) if token.token.strip() == letter), None)
