"""TypeSafe - a model built to decide rather than to write.

One endpoint, `POST /v1/systemone`: a *state*, and a map of typed questions
answered against it. A `ChoiceQuestion` is already that shape - `state` is the
state, `question` the instructions, each option's key and text one entry of
the criteria map - and what comes back (`choice`, `probabilities`,
`confidence`) is `ChoiceAnswer` field by field. The question's own id is never
shown to the model, so it is a constant here.

**Its confidence is its own.** TypeSafe derives it from the distribution and
calibrates it in training; it is passed through as given, not recomputed from
`probabilities`, which is what the provider documents it for.

**Metered like a text model.** Every call is written to the same call log
`MeteredLLM` writes to, priced from the catalog entry - TypeSafe charges input
tokens only, which the entry states as an output price of zero - so
`prometheus spend` and the trace see decisions next to everything else.

The key arrives from the connection's credential, resolved by the factory at
the moment the client is built, exactly as every other provider's does.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from domain.decisions.models import ChoiceAnswer, ChoiceQuestion
from domain.errors import ProviderError
from domain.llm.models import Usage
from domain.llm.telemetry import LLMCallLog, LLMCallRecord
from infrastructure.llm.catalog import ModelCatalog
from infrastructure.llm.errors import translate_status, translate_transport_error
from infrastructure.llm.retry import RetryPolicy, with_retry
from infrastructure.observability.logging import get_logger

log = get_logger(__name__)

PROVIDER_NAME = "typesafe"
BASE_URL = "https://api.typesafe.ai"
PATH = "/v1/systemone"

SOURCE = "decision model"

#: The id the one question is sent under. Only this code reads it.
QUESTION_ID = "decision"

#: A decision model answers in well under a second; one that has not answered
#: in this long is not going to, and the text model is waiting behind it.
TIMEOUT_SECONDS = 15.0


class TypeSafeDecider:
    """Implements `domain.decisions.protocols.Decider` over TypeSafe's HTTP API."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        base_url: str = "",
        entry: str = "",
        catalog: ModelCatalog | None = None,
        call_log: LLMCallLog | None = None,
        client: httpx.AsyncClient | None = None,
        retry_policy: RetryPolicy | None = None,
        timeout_seconds: float = TIMEOUT_SECONDS,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._url = _endpoint(base_url or BASE_URL)
        self._entry = entry
        self._catalog = catalog
        self._call_log = call_log
        self._client = client
        self._owns_client = client is None
        self._retry_policy = retry_policy or RetryPolicy()
        self._timeout = timeout_seconds

    async def choose(self, question: ChoiceQuestion) -> ChoiceAnswer:
        payload = {
            "state": question.state,
            "model": self._model,
            "questions": {
                QUESTION_ID: {
                    "type": "choice",
                    "instructions": question.question,
                    "criteria": {option.key: option.text for option in question.options},
                }
            },
        }
        started = time.perf_counter()
        try:
            body = await with_retry(lambda: self._post(payload), self._retry_policy)
        except ProviderError as error:
            await self._record(self._model, Usage(latency_ms=_since(started)), error=error)
            raise

        usage = _usage(body, latency_ms=_since(started))
        answered_by = str(body.get("model") or self._model)
        usage = self._priced(answered_by, usage)
        await self._record(answered_by, usage)
        answer = _answer(body, question)
        log.info(
            "decision.answered",
            provider=PROVIDER_NAME,
            model=answered_by,
            purpose=question.purpose,
            choice=answer.key,
            confidence=answer.confidence,
            cost_usd=usage.cost_usd,
            latency_ms=usage.latency_ms,
        )
        return answer

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        try:
            response = await self._client.post(
                self._url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self._timeout,
            )
        except Exception as error:  # httpx transport failures
            raise translate_transport_error(error, provider=PROVIDER_NAME) from error
        if response.status_code >= 400:
            # 429 and 529 (overloaded) come back transient and are retried;
            # 401 and 422 are the request's fault and are not.
            raise translate_status(response.status_code, response.text, provider=PROVIDER_NAME)
        try:
            body = response.json()
        except ValueError as error:
            raise ProviderError(
                f"{PROVIDER_NAME} returned a body that is not JSON: {response.text[:200]}"
            ) from error
        if not isinstance(body, dict):
            raise ProviderError(f"{PROVIDER_NAME} returned an unusable response: {body!r}"[:300])
        return body

    def _priced(self, model: str, usage: Usage) -> Usage:
        entry = self._catalog.find(PROVIDER_NAME, model) if self._catalog else None
        if entry is None and self._catalog is not None and self._entry:
            # An alias such as `jev-latest` answers under a versioned id, which
            # the catalog does not list; the entry that was routed to still
            # states the price.
            entry = next((item for item in self._catalog.entries if item.name == self._entry), None)
        if entry is None:
            log.warning("llm.unpriced_model", provider=PROVIDER_NAME, model=model)
            return usage
        return Usage(
            prompt_tokens=usage.prompt_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=entry.cost_of(usage.prompt_tokens, usage.output_tokens),
            latency_ms=usage.latency_ms,
        )

    async def _record(
        self, model: str, usage: Usage, *, error: ProviderError | None = None
    ) -> None:
        if self._call_log is None:
            return
        await self._call_log.record(
            LLMCallRecord(
                provider=PROVIDER_NAME,
                model=model,
                usage=usage,
                success=error is None,
                error=f"{type(error).__name__}: {error}" if error else None,
                task_kind="DECISION",
                entry=self._entry,
                reason="decision routed to a decision model",
            )
        )


def _endpoint(base_url: str) -> str:
    """The evaluation URL from whatever address a person typed.

    Accepted with or without the version segment, because the provider's own
    SDK takes the bare host and a person copying from the API page brings `/v1`.
    """
    base = base_url.strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return f"{base}{PATH}"


def _since(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _usage(body: dict[str, Any], *, latency_ms: int) -> Usage:
    found = body.get("usage")
    raw: dict[str, Any] = found if isinstance(found, dict) else {}
    try:
        prompt = int(raw.get("input_tokens", 0))
        output = int(raw.get("output_tokens", 0))
    except (TypeError, ValueError):
        prompt, output = 0, 0
    return Usage(prompt_tokens=prompt, output_tokens=output, latency_ms=latency_ms)


def _answer(body: dict[str, Any], question: ChoiceQuestion) -> ChoiceAnswer:
    """The answer, or unreadable - never an option the question did not offer."""
    answers = body.get("answers")
    raw = answers.get(QUESTION_ID) if isinstance(answers, dict) else None
    if not isinstance(raw, dict):
        log.warning("decision.unreadable", provider=PROVIDER_NAME, purpose=question.purpose)
        return ChoiceAnswer.unreadable(SOURCE)

    choice = raw.get("choice")
    if choice not in question.keys:
        log.warning(
            "decision.unreadable",
            provider=PROVIDER_NAME,
            purpose=question.purpose,
            reply=str(choice)[:40],
        )
        return ChoiceAnswer.unreadable(SOURCE)

    probabilities: dict[str, float] = {}
    raw_probabilities = raw.get("probabilities")
    if isinstance(raw_probabilities, dict):
        try:
            probabilities = {
                key: float(raw_probabilities[key])
                for key in question.keys
                if key in raw_probabilities
            }
        except (TypeError, ValueError):
            probabilities = {}

    confidence: float | None
    try:
        confidence = float(raw["confidence"])
    except (KeyError, TypeError, ValueError):
        confidence = None
    if confidence is not None and not 0 <= confidence <= 1:
        confidence = None
    return ChoiceAnswer(
        key=str(choice), probabilities=probabilities, confidence=confidence, source=SOURCE
    )
