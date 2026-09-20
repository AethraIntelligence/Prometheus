from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from domain.approvals.gate import RiskAssessment
from domain.policies.models import Actor
from domain.tools.models import ToolResult, ToolSpec


class Tool(Protocol):
    @property
    def spec(self) -> ToolSpec: ...

    async def execute(self, input_data: dict[str, Any]) -> ToolResult: ...


@runtime_checkable
class RiskAssessor(Protocol):
    """An optional second half of `Tool`, for tools whose risk is per call.

    Writing a file that does not exist and overwriting one that does are the
    same tool and very different actions. A tool that can tell them apart says
    so here; one that cannot simply does not implement this, and its declared
    `ToolSpec.risk_level` stands.
    """

    def assess(self, input_data: dict[str, Any]) -> RiskAssessment | None: ...


@runtime_checkable
class EffectPreviewer(Protocol):
    """Optional safe preview produced before a tool changes the world."""

    def preview(self, input_data: dict[str, Any]) -> dict[str, Any]: ...


@runtime_checkable
class ArgumentSettler(Protocol):
    """An optional last word on a call's arguments, before anybody reads them.

    A tool is already forgiving about how a model calls it - types coerced,
    unknown arguments dropped. This is the same forgiveness one step earlier,
    for the cases where the corrected call has to be the one the person is
    shown and the one a permission is remembered for: the risk assessment, the
    approval's scope, the audit line and the effect must all name the file that
    is actually written, not the one first asked for.

    Settling is pure and happens before any permission is decided, so it can
    only ever change what is asked about - never whether it is asked.
    """

    def settle(self, input_data: dict[str, Any]) -> dict[str, Any]: ...


class ToolRegistry(Protocol):
    """Permissions are enforced here, not in the caller.

    An actor only ever sees the specs it is allowed to use, so a model cannot ask
    for a tool it has no right to.
    """

    def register(self, tool: Tool) -> None: ...

    def unregister(self, name: str) -> bool: ...

    def get(self, name: str, actor: Actor) -> Tool: ...

    def list_specs(self, actor: Actor) -> list[ToolSpec]: ...
