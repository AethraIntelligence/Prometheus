"""Who may read what. A scope is an access boundary, not a hint (§86).

This is written once, in the domain, and every backend applies it to the rows it
is about to return - including the backends that already expressed it in their
own query language. That looks like a double check and is a deliberate one: the
SQL is an optimisation, this is the rule, and an index that drifts from it
returns fewer rows rather than somebody else's.

The rule itself is short. An employee sees the workspace's memory, the memory of
the plan it is working inside, and its own private memory. It never sees another
employee's private memory, and a caller that names no employee sees no private
memory at all. Within that, one kind is narrower still: working memory belongs
to the task that wrote it and is not read from another.
"""

from __future__ import annotations

from domain.memory.models import (
    WORKSPACE_BOUND,
    MemoryItem,
    MemoryKind,
    MemoryQuery,
    MemoryScope,
    MemoryStatus,
)
from domain.memory.ranking import is_live


def visible(item: MemoryItem, query: MemoryQuery) -> bool:
    """Whether this item may be returned for this query."""
    # Two scopes are above the workspace: what the *person* prefers and what is
    # true of this *installation*. Both were stated in some workspace and
    # neither is about it, so the row keeps that as provenance and the boundary
    # does not apply. Everything else is inside one.
    if item.scope in WORKSPACE_BOUND and item.workspace_id != query.workspace_id:
        return False
    if item.scope not in query.scopes:
        return False
    if item.scope is MemoryScope.EMPLOYEE_PRIVATE and (
        query.employee_id is None or item.employee_id != query.employee_id
    ):
        return False
    # A plan-scoped item belongs to the plan it was written under. Reading
    # another plan's working context is how one objective's mistakes end up in
    # the next one's prompt.
    #
    # A query that names no plan matches no plan, rather than all of them. The
    # weaker reading - skip the check when there is nothing to compare against -
    # is the one that was here, and it made a standalone `run-task`, whose task
    # has no plan, the one caller that could read every plan's memory at once.
    # A scope is an access boundary (§86), and a boundary with a hole for the
    # callers who happen not to mention it is not one.
    if item.scope is MemoryScope.PLAN and item.plan_id != query.plan_id:
        return False
    if query.kinds and item.kind not in query.kinds:
        return False
    if query.ids and item.id not in query.ids:
        return False
    # A superseded memory is the platform's former belief. It is kept so a
    # correction can be traced, and a run that read it would act on what the
    # person already corrected.
    if item.status is MemoryStatus.SUPERSEDED and not query.include_superseded:
        return False
    # A task id is provenance on every kind but one: what a task *learned* is
    # worth reading in the next task, which is the entire point of remembering
    # it. Working memory is the exception - it is the running notes of one task,
    # and reading another task's is reading someone's half-finished sentence.
    if (
        item.kind is MemoryKind.WORKING
        and item.task_id is not None
        and item.task_id != query.task_id
    ):
        return False
    # History may include a superseded belief, but retention remains a harder
    # boundary. Asking for revision history must not resurrect an item whose
    # explicit expiry has passed while the maintenance pass has not run yet.
    return is_live(item, query.as_of)
