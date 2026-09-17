"""A scripted model.

The whole suite runs on this: deterministic answers, no network, no keys. If a
test needs a real provider to pass, the abstraction it is testing has leaked.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace

from domain.errors import ProviderError
from domain.llm.models import FinishReason, LLMRequest, LLMResponse, ToolCallRequest, Usage


class FakeLLM:
    """Implements `domain.llm.protocols.LLM` by replaying a script.

    A script entry is either a response to return or an exception to raise, so a
    test can rehearse a rate limit followed by a success without touching HTTP.
    """

    def __init__(
        self,
        script: Sequence[LLMResponse | Exception] | None = None,
        *,
        model: str = "fake/model",
        on_request: Callable[[LLMRequest], None] | None = None,
    ) -> None:
        self._script: list[LLMResponse | Exception] = list(script or [])
        self._model = model
        self._on_request = on_request
        self.requests: list[LLMRequest] = []

    # --- Scripting ------------------------------------------------------------

    @classmethod
    def answering(cls, *answers: str, model: str = "fake/model") -> FakeLLM:
        return cls([reply(text, model=model) for text in answers], model=model)

    def queue(self, item: LLMResponse | Exception) -> FakeLLM:
        self._script.append(item)
        return self

    @property
    def call_count(self) -> int:
        return len(self.requests)

    @property
    def last_request(self) -> LLMRequest:
        if not self.requests:
            raise AssertionError("FakeLLM was never called")
        return self.requests[-1]

    # --- LLM ------------------------------------------------------------------

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if self._on_request is not None:
            self._on_request(request)

        if not self._script:
            raise AssertionError(
                f"FakeLLM ran out of script on call {len(self.requests)}; "
                "queue another response or exception"
            )

        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return replace(item, model=item.model or self._model)


def reply(
    content: str,
    *,
    model: str = "fake/model",
    prompt_tokens: int = 10,
    output_tokens: int = 5,
    latency_ms: int = 1,
) -> LLMResponse:
    return LLMResponse(
        content=content,
        model=model,
        usage=Usage(
            prompt_tokens=prompt_tokens, output_tokens=output_tokens, latency_ms=latency_ms
        ),
    )


def tool_reply(*calls: ToolCallRequest, model: str = "fake/model") -> LLMResponse:
    return LLMResponse(
        content="",
        model=model,
        tool_calls=calls,
        usage=Usage(prompt_tokens=10, output_tokens=5, latency_ms=1),
        finish_reason=FinishReason.TOOL_CALLS,
    )


def transient(message: str = "rate limited") -> ProviderError:
    from domain.errors import RateLimitError

    return RateLimitError(message)


class AnyModelRouter:
    """Implements `domain.llm.protocols.ModelRouter`: every piece of work has a model.

    For a test that scripts the model itself (`container.llm_for = ...`) and
    so has no catalog behind it. Readiness asks the router whether a run could
    be placed; without this it would answer, truthfully, that nothing can.
    """

    def select(self, task_kind, required, hints=None):
        from domain.llm.models import ModelChoice

        return ModelChoice(provider="fake", model="fake/model", reason="scripted by the test")

    def stronger_available(self, task_kind, required=None) -> bool:
        return False


def scripted_models(container, llm) -> None:
    """Every model call answered by `llm`, and every piece of work routable.

    The router is re-applied after the container swaps its catalog at start-up,
    which drops whatever router it held.
    """
    container.llm_for = lambda *args, **kwargs: llm
    container.model_router = AnyModelRouter()
    replace = container.use_catalog

    def use_catalog(catalog) -> None:
        replace(catalog)
        container.model_router = AnyModelRouter()

    container.use_catalog = use_catalog
