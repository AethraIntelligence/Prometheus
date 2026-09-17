"""The retrieval evals measure the backend, so they have to catch a bad one.

A suite that passes on every store is not measuring anything. So besides passing
on the store the platform ships, it is run against two stores that are wrong in
the two ways that matter - one that ignores the workspace, one that forgets to
hide superseded memories - and each must be caught by the metric it breaks.
"""

from __future__ import annotations

from dataclasses import replace

from application.memory.evals import (
    FRESHNESS,
    ISOLATION,
    THRESHOLDS,
    render_report,
    run_retrieval_evals,
)
from domain.memory.models import MemoryQuery
from infrastructure.memory.in_memory import InMemoryMemory


class IgnoresTheWorkspace(InMemoryMemory):
    async def recall(self, query: MemoryQuery):
        found = []
        for workspace in {item.workspace_id for item in self._items.values()}:
            found.extend(await super().recall(replace(query, workspace_id=workspace)))
        return found


class ShowsSuperseded(InMemoryMemory):
    async def recall(self, query: MemoryQuery):
        return await super().recall(replace(query, include_superseded=True))


async def test_the_shipped_store_meets_every_threshold() -> None:
    report = await run_retrieval_evals(InMemoryMemory, backend="in-memory")

    assert report.passed, render_report(report)
    assert set(report.scores) == set(THRESHOLDS)


async def test_a_store_that_leaks_across_workspaces_fails_isolation() -> None:
    report = await run_retrieval_evals(IgnoresTheWorkspace)

    assert not report.passed
    assert report.scores[ISOLATION] < 1.0


async def test_a_store_that_recalls_what_was_corrected_fails_freshness() -> None:
    report = await run_retrieval_evals(ShowsSuperseded)

    assert not report.passed
    assert report.scores[FRESHNESS] < 1.0
    assert "report_superseded was recalled" in render_report(report)
