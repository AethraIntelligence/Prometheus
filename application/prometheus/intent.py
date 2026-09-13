"""Reading what was actually asked for, and whether it needs the workforce.

Two questions, answered from one reading, because they come apart badly. §7.3
asks for free text to become an `Objective` with constraints and acceptance
criteria; §7.5 asks Prometheus to do simple things directly rather than decompose them
on principle. Both are the same act of comprehension, and splitting them into
two model calls means the second one re-reads what the first already understood.

The user's sentence is never replaced. What Prometheus understood is recorded *beside*
it, so a misreading stays visible next to the thing it misread instead of
quietly becoming the objective.

An unreadable answer is not a failure here. A request that could not be parsed
into constraints is still a request, and treating it as work to be planned is a
better outcome than refusing to start - so the fallback is the plainest possible
reading: do what they said, criteria unstated.
"""

from __future__ import annotations

import structlog

from application.prometheus.language import DEFAULT_LANGUAGE, instruction
from application.prometheus.workforce import describe
from application.prompts import render
from domain.capabilities.models import CapabilityRequirement
from domain.employees.definition import EmployeeDefinition
from domain.llm.json_output import extract_object
from domain.llm.models import LLMRequest, Message, RoutingHints, TaskKind
from domain.llm.protocols import LLM
from domain.workforce.intent import Intent

log = structlog.get_logger(__name__)


class IntentReader:
    """Free text in, `Intent` out."""

    def __init__(
        self, llm: LLM, *, language: str = DEFAULT_LANGUAGE, triage: LLM | None = None
    ) -> None:
        """`triage` is the model asked for a second opinion before a reading of
        "no work" is believed. Without one the reading stands on its own, which
        is what a scripted test wants and what a strong model can carry."""
        self._llm = llm
        self._language = language
        self._triage = triage

    async def read(
        self,
        request: str,
        workforce: list[EmployeeDefinition],
        *,
        remembered: tuple[str, ...] = (),
        documents: tuple[str, ...] = (),
    ) -> Intent:
        prompt = render(
            "prometheus_intent",
            request=request,
            workforce=describe(workforce),
            # A sentence like "do the same again" is not readable on its own.
            # What this workspace already knows is what makes it a request
            # rather than a fragment.
            remembered=_remembered(remembered),
            # Separate from what is remembered, and not by accident. A
            # recollection is a lead that may be stale; a passage of the user's
            # own document is evidence with a source on it, and a question whose
            # answer is in one can be answered here rather than decomposed into
            # a plan to go and find what is already in front of us.
            documents=_documents(documents),
        )
        response = await self._llm.generate(
            LLMRequest(
                messages=(
                    # Only the `answer` field: the rest of this reading is
                    # machinery, and a restatement in another language would
                    # reach the planner rather than the person.
                    *instruction(self._language, about='the "answer" field'),
                    Message.user(prompt),
                ),
                temperature=0.0,
                response_format={"type": "json_object"},
            )
        )

        parsed = extract_object(response.content)
        if parsed is None:
            log.warning("prometheus.intent_unreadable", reply=response.content[:200])
            return Intent(restatement=request, needs_work=True)

        needs_work = bool(parsed.get("needs_work", True))
        answer = str(parsed.get("answer", "")).strip()
        if not needs_work and not await self._is_talk(request):
            # A second opinion, asked as one narrow question, before a reply is
            # believed. The reading's own flag is one field of a long form, and
            # a local model filled it "no work" for today's weather, an
            # exchange rate and the number of files in a folder - then answered
            # all three from nothing. Asked only "look it up, or reply?", the
            # same model sorted every one of them correctly. Where the two
            # disagree it is work: a slow answer beats an invented one.
            log.info("prometheus.intent_overruled", restatement=parsed.get("restatement", ""))
            needs_work, answer = True, ""
        if not needs_work and not answer:
            # It said this is talk and left the reply empty - which is what a
            # small model does with an empty form: fills the flag and copies the
            # blank. Reading that as work turned "Hello" into a plan. The
            # reading is kept and the reply asked for on its own; only a reply
            # that still does not come makes it work.
            log.info("prometheus.intent_answerless", restatement=parsed.get("restatement", ""))
            answer = await self._reply(request, workforce)
            needs_work = not answer

        intent = Intent(
            restatement=str(parsed.get("restatement", "")).strip() or request,
            constraints=_as_mapping(parsed.get("constraints")),
            preferences=_as_criteria(parsed.get("preferences")),
            acceptance_criteria=_as_criteria(parsed.get("acceptance_criteria")),
            needs_work=needs_work,
            answer="" if needs_work else answer,
        )
        log.info(
            "prometheus.intent_read",
            needs_work=intent.needs_work,
            criteria=len(intent.acceptance_criteria),
            constraints=sorted(intent.constraints),
            preferences=len(intent.preferences),
        )
        return intent

    async def _is_talk(self, request: str) -> bool:
        """True only for a clear "reply from what you know"; anything else is work."""
        if self._triage is None:
            return True
        response = await self._triage.generate(
            LLMRequest(
                messages=(Message.user(render("prometheus_triage", request=request)),),
                temperature=0.0,
                max_tokens=5,
            )
        )
        return response.content.strip()[:1].upper() == "A"

    async def _reply(self, request: str, workforce: list[EmployeeDefinition]) -> str:
        response = await self._llm.generate(
            LLMRequest(
                messages=(
                    *instruction(self._language, about="your reply"),
                    Message.user(
                        render("prometheus_reply", request=request, workforce=describe(workforce))
                    ),
                ),
                temperature=0.3,
            )
        )
        return response.content.strip()

    @staticmethod
    def routing() -> tuple[TaskKind, CapabilityRequirement, RoutingHints]:
        """Comprehension is the one thing here that must not be cheap.

        Everything downstream is built on this reading; a constraint dropped
        here is a constraint nothing later can recover.
        """
        return (
            TaskKind.EXTRACTION,
            CapabilityRequirement(),
            RoutingHints(quality=0.8, cost_sensitivity=0.3),
        )


def _remembered(lines: tuple[str, ...]) -> str:
    if not lines:
        return ""
    return (
        "# What this workspace already knows\n\n"
        "From earlier work here, and possibly out of date. Use it to understand "
        "what they mean, not to decide what they want.\n"
        + "\n".join(f"- {line}" for line in lines)
    )


def _documents(passages: tuple[str, ...]) -> str:
    if not passages:
        return ""
    return (
        "# From the user's own documents\n\n"
        "Quoted, with the document each came from. You may answer from these "
        "directly, saying which document you used.\n\n" + "\n\n".join(passages)
    )


def _as_mapping(raw: object) -> dict[str, object]:
    return {str(key): value for key, value in raw.items()} if isinstance(raw, dict) else {}


def _as_criteria(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list | tuple):
        return ()
    return tuple(str(item).strip() for item in raw if str(item).strip())
