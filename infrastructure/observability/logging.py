"""Structured JSON logging with a correlation id on every record.

One objective can fan out into several tasks and assignments. Without an id
carried through the whole chain, the log of a multi-step run is unreadable, so
the ids are bound into structlog's context rather than passed by hand.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import structlog

from domain.secrets.models import redact

CORRELATION_KEYS = ("correlation_id", "objective_id", "plan_id", "task_id", "assignment_id")


#: Libraries that log every request at INFO. Their traffic is ours, already
#: recorded with cost and latency, so their version of it is noise on a terminal.
NOISY_LOGGERS = ("httpx", "httpcore", "aiosqlite", "asyncio")


def _redact_event(
    _logger: object, _method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Sanitize the complete record before any renderer or sink sees it."""
    return redact(event_dict)


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Install the structlog pipeline. Safe to call more than once."""
    logging.basicConfig(format="%(message)s", level=getattr(logging, level, logging.INFO))
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=True)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _redact_event,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level, logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


def new_correlation_id() -> str:
    return uuid4().hex


@contextmanager
def correlation_context(**ids: str | None) -> Iterator[str]:
    """Bind correlation ids for the duration of a block.

    Unknown keys are rejected on purpose: a typo that silently drops the id is
    worse than a loud failure, because it only shows up when reading a trace.
    """
    unknown = set(ids) - set(CORRELATION_KEYS)
    if unknown:
        raise ValueError(f"Unknown correlation keys: {sorted(unknown)}")

    bound = {k: v for k, v in ids.items() if v is not None}
    bound.setdefault("correlation_id", new_correlation_id())
    tokens = structlog.contextvars.bind_contextvars(**bound)
    try:
        yield bound["correlation_id"]
    finally:
        structlog.contextvars.reset_contextvars(**tokens)
