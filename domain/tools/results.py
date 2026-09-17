"""The contract between a tool process and a model context.

Tool implementations may return arbitrary Python values, especially tools
discovered from another process. A model context gets a smaller contract:
JSON-compatible values, bounded nesting, bounded collections and a hard byte
budget. The durable call ledger may retain the original result; this module is
the boundary crossed before any value becomes prompt text.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from domain.integrations.untrusted import NOTE, Provenance, TrustLevel, frame
from domain.tools.models import ToolResult, ToolSpec

MAX_RESULT_CHARS = 24_000
MAX_STRING_CHARS = 12_000
MAX_COLLECTION_ITEMS = 100
MAX_RESULT_DEPTH = 8


@dataclass(frozen=True, slots=True)
class ContextResult:
    """A validated result and the authority its contents carry."""

    result: ToolResult
    provenance: Provenance
    truncated: bool = False

    def render(self, tool_name: str) -> str:
        if not self.result.success:
            return f"{tool_name} failed: {self.result.error}"
        if not self.result.output:
            return f"{tool_name} returned nothing."
        rendered = json.dumps(
            self.result.output, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if self.provenance.trust is TrustLevel.UNTRUSTED:
            quoted = frame(
                rendered,
                origin=self.provenance.source,
                kind=self.provenance.kind,
            )
            return f"{tool_name} returned untrusted data:\n{NOTE}\n{quoted}"
        return f"{tool_name} returned: {self.result.output}"


def for_context(result: ToolResult, spec: ToolSpec) -> ContextResult:
    """Validate and bound a result before it can become prompt text."""
    cleaned, truncated = _clean(result.output, depth=0)
    if not isinstance(cleaned, dict):  # ToolResult promises a dict; enforce it here.
        cleaned = {"value": cleaned}
        truncated = True

    schema_error = _schema_error(cleaned, spec.result_schema) if result.success else None
    if schema_error:
        result = ToolResult.failure(
            f"Tool result violated its declared schema: {schema_error}"
        )
        cleaned = {}

    encoded = json.dumps(cleaned, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded) > MAX_RESULT_CHARS:
        cleaned = {
            "preview": encoded[:MAX_RESULT_CHARS],
            "truncated": True,
            "reason": "tool result exceeded the model-context limit",
        }
        truncated = True

    return ContextResult(
        result=ToolResult(
            success=result.success,
            output=cleaned,
            error=_bounded_text(result.error) if result.error else None,
            latency_ms=result.latency_ms,
        ),
        provenance=Provenance(
            source=spec.name,
            kind=spec.result_kind,
            trust=spec.result_trust,
        ),
        truncated=truncated,
    )


def _schema_error(output: dict[str, Any], schema: dict[str, str]) -> str | None:
    kinds = {
        "null": lambda value: value is None,
        "boolean": lambda value: isinstance(value, bool),
        "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
        "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
        "string": lambda value: isinstance(value, str),
        "array": lambda value: isinstance(value, list),
        "object": lambda value: isinstance(value, dict),
    }
    for name, kind in schema.items():
        if name not in output:
            return f"missing '{name}'"
        accepts = kinds.get(kind)
        if accepts is None:
            return f"unsupported schema type '{kind}' for '{name}'"
        if not accepts(output[name]):
            return f"'{name}' should be {kind}"
    return None


def _clean(value: Any, *, depth: int) -> tuple[Any, bool]:
    if depth >= MAX_RESULT_DEPTH:
        return "[nested value omitted]", True
    if value is None or isinstance(value, (bool, int)):
        return value, False
    if isinstance(value, float):
        return (value, False) if math.isfinite(value) else (str(value), True)
    if isinstance(value, str):
        bounded = _bounded_text(value)
        return bounded, bounded != value
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        truncated = len(value) > MAX_COLLECTION_ITEMS
        for key, item in list(value.items())[:MAX_COLLECTION_ITEMS]:
            cleaned, changed = _clean(item, depth=depth + 1)
            result[str(key)] = cleaned
            truncated = truncated or changed or not isinstance(key, str)
        return result, truncated
    if isinstance(value, (list, tuple)):
        result = []
        truncated = len(value) > MAX_COLLECTION_ITEMS
        for item in value[:MAX_COLLECTION_ITEMS]:
            cleaned, changed = _clean(item, depth=depth + 1)
            result.append(cleaned)
            truncated = truncated or changed
        return result, truncated
    return f"[unsupported {type(value).__name__} omitted]", True


def _bounded_text(value: str) -> str:
    if len(value) <= MAX_STRING_CHARS:
        return value
    return value[:MAX_STRING_CHARS] + "\n[tool text truncated]"
