"""The effect gate for this process: a counter, a list and an event."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

from domain.safety.effects import EffectInFlight
from infrastructure.observability.logging import get_logger

log = get_logger(__name__)


class InMemoryEffectGate:
    """Implements `domain.safety.effects.EffectGate`."""

    def __init__(self) -> None:
        self._in_flight: dict[int, EffectInFlight] = {}
        self._ids = itertools.count()
        self._open = asyncio.Event()
        self._open.set()
        self._idle = asyncio.Event()
        self._idle.set()

    @asynccontextmanager
    async def entering(self, task_id: UUID, tool: str, effect: str) -> AsyncIterator[None]:
        if not self._open.is_set():
            log.info("effects.waiting_for_release", tool=tool, task_id=str(task_id))
        await self._open.wait()
        token = next(self._ids)
        self._in_flight[token] = EffectInFlight(task_id, tool, effect, datetime.now(UTC))
        self._idle.clear()
        try:
            yield
        finally:
            self._in_flight.pop(token, None)
            if not self._in_flight:
                self._idle.set()

    def in_flight(self) -> tuple[EffectInFlight, ...]:
        return tuple(self._in_flight.values())

    def hold(self) -> None:
        self._open.clear()
        log.info("effects.held", in_flight=len(self._in_flight))

    def release(self) -> None:
        self._open.set()
        log.info("effects.released")

    @property
    def held(self) -> bool:
        return not self._open.is_set()

    async def wait_idle(self, timeout_seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=max(0.0, timeout_seconds))
        except TimeoutError:
            return not self._in_flight
        return True
