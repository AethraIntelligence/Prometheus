from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from domain.capabilities.models import Capability
from domain.computer.interfaces import InterfaceLevel
from domain.integrations.untrusted import TrustLevel
from domain.policies.models import RiskLevel
from domain.policies.risk import Effect, highest, risk_of
from domain.tools.schema import Param, ParameterSet, parameters_from_json_schema


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """What a model is told about a tool, plus what the platform needs to gate it."""

    name: str
    description: str
    json_schema: dict[str, Any] = field(default_factory=dict)
    #: What this call does to the world. The risk level follows from it
    #: (`domain.policies.risk`), so a tool author declares what their tool does
    #: and not what the platform should think about it.
    effect: Effect = Effect.READ
    risk_level: RiskLevel = RiskLevel.LOW
    capabilities: frozenset[Capability] = field(default_factory=frozenset)
    reversible: bool = True
    #: How this tool reaches the world. The default is API because a tool that
    #: does not say otherwise is a direct call - the filesystem, a search
    #: endpoint - and only the tools that really do drive a screen should have
    #: to declare that they do. See `domain.computer.interfaces`.
    interface_level: InterfaceLevel = InterfaceLevel.API
    #: The declared parameters, kept alongside the rendered schema so the same
    #: declaration both describes the tool and validates the call.
    parameters: ParameterSet = field(default_factory=ParameterSet)
    #: Authority carried by successful output when it is shown to a model.
    #: External pages, documents, mail, integrations and screen text are data,
    #: never instructions, and must opt into UNTRUSTED explicitly.
    result_trust: TrustLevel = TrustLevel.INTERNAL
    result_kind: str = "tool"
    #: Required output fields and their JSON types. Empty means the tool has no
    #: more specific contract than a bounded JSON object.
    result_schema: dict[str, str] = field(default_factory=dict)

    @classmethod
    def of(
        cls,
        name: str,
        description: str,
        *parameters: Param,
        effect: Effect = Effect.READ,
        risk_level: RiskLevel = RiskLevel.LOW,
        capabilities: frozenset[Capability] = frozenset(),
        reversible: bool = True,
        interface_level: InterfaceLevel = InterfaceLevel.API,
        result_trust: TrustLevel = TrustLevel.INTERNAL,
        result_kind: str = "tool",
        result_schema: dict[str, str] | None = None,
    ) -> ToolSpec:
        """Declare a tool once; the JSON Schema is derived, never hand-written.

        `risk_level` may raise what the effect implies and never lower it: a
        read of something sensitive is a real case, a "harmless" delete is not.
        """
        parameter_set = ParameterSet(parameters)
        return cls(
            name=name,
            description=description,
            json_schema=parameter_set.to_json_schema(),
            effect=effect,
            risk_level=highest(risk_level, risk_of(effect)),
            capabilities=capabilities,
            reversible=reversible,
            interface_level=interface_level,
            parameters=parameter_set,
            result_trust=result_trust,
            result_kind=result_kind,
            result_schema=dict(result_schema or {}),
        )


    @classmethod
    def from_json_schema(
        cls,
        name: str,
        description: str,
        schema: dict[str, Any] | None,
        *,
        effect: Effect = Effect.EXECUTE,
        risk_level: RiskLevel = RiskLevel.LOW,
        capabilities: frozenset[Capability] = frozenset(),
        reversible: bool = True,
        interface_level: InterfaceLevel = InterfaceLevel.INTEGRATION,
        result_trust: TrustLevel = TrustLevel.UNTRUSTED,
        result_kind: str = "integration",
        result_schema: dict[str, str] | None = None,
    ) -> ToolSpec:
        """Declare a tool whose shape was written elsewhere.

        Used for a capability discovered at runtime - an integration's tool -
        where the schema arrives from the other side instead of from a `Param`
        declaration here. The parameters are parsed out of it so validation,
        coercion and unknown-argument reporting apply exactly as they do to a
        tool written in this repository, and the schema the model is shown is
        then rendered back from them: what is described and what is enforced
        must be the same thing, and the way to guarantee that is to have one
        source for both.

        `effect` defaults to EXECUTE, which is HIGH, which asks. A discovered
        tool nobody has classified could be anything, and the cost of being
        wrong the other way is an action the user never approved (ADR 0015).
        `interface_level` defaults to INTEGRATION for the same reason it is not
        API: this reaches the world through something the platform speaks to on
        the user's behalf, and a trace should say so.
        """
        return cls.of(
            name,
            description,
            *parameters_from_json_schema(schema).params,
            effect=effect,
            risk_level=risk_level,
            capabilities=capabilities,
            reversible=reversible,
            interface_level=interface_level,
            result_trust=result_trust,
            result_kind=result_kind,
            result_schema=result_schema,
        )

@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    input_data: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolResult:
    success: bool
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    latency_ms: int = 0

    @classmethod
    def ok(cls, **output: Any) -> ToolResult:
        return cls(success=True, output=output)

    @classmethod
    def failure(cls, error: str) -> ToolResult:
        return cls(success=False, error=error)
