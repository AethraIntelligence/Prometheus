"""Causal context copied by asyncio into parallel task waves."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID

from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId


@dataclass(frozen=True, slots=True)
class TraceContext:
    trace_id: UUID
    parent_id: UUID | None = None
    causation_id: UUID | None = None
    workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID


_current: ContextVar[TraceContext | None] = ContextVar("observability_trace", default=None)


def current() -> TraceContext | None:
    return _current.get()


@contextmanager
def tracing(context: TraceContext) -> Iterator[TraceContext]:
    token = _current.set(context)
    try:
        yield context
    finally:
        _current.reset(token)
