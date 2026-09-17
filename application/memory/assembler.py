"""What a run is told about what happened before it.

A run's context is four things put together, in this order of trust:

1. **the goal** - what was asked, unmodified;
2. **what the delegator passed down** - facts and constraints the manager chose
   deliberately, which are the most recent and the most specific;
3. **what was recalled** - the platform's own notes about earlier work;
4. **what was retrieved** - passages of the user's own documents.

The last two are different in kind, which is why they are separate fields rather
than one list. A recollection is hedged ("you noted last time") because memory
can be stale; a passage is a *quotation*, and the useful thing about it is which
document it came from. They meet here and nowhere else, which is what keeps one
answer to "what does this run know" (ADR 0016).

A retrieved passage is also external content: it was written by somebody who is
not the user, and an instruction inside it is not an instruction to the
platform. It is framed with the same helper an integration's results are framed
with, rather than a second one written for documents (§25).

Recall is scoped, always. The query names the workspace, the plan and the
employee, and the store answers within those bounds - an employee never reads
another employee's private memory, and that is enforced where the rows are
rather than trusted here (`domain/memory/access.py`).

Since Phase 9 each recollection also says what it rests on - stated, recorded,
reported or inferred, where from, when, how confident and whose - so a guess is
read as a guess (`domain/memory/citation.py`). And which memories a run was given
is recorded with the reason each one was chosen (`domain/memory/usage.py`), so
"why did it do that" has an answer a person can read.

The recalled part is labelled as recollection rather than merged into the facts.
A model told "the report is in reports/q3.md" as a fact will act on it; told
"you noted last time that the report was in reports/q3.md" it will check. The
difference matters because memory can be stale in a way an assignment cannot.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from domain.employees.definition import EmployeeDefinition
from domain.integrations.untrusted import frame
from domain.knowledge.models import KnowledgeQuery, Passage
from domain.knowledge.protocols import Retriever
from domain.memory.citation import explain, recollection
from domain.memory.models import MemoryItem, MemoryQuery, MemoryScope
from domain.memory.protocols import Memory
from domain.memory.usage import MemoryUse, MemoryUseLog
from domain.tasks.task import Task
from domain.workforce.assignment import SharedContext

log = structlog.get_logger(__name__)

#: How many recollections a run is given. A budget rather than everything that
#: matched: context is the scarcest thing in a run, and the tenth-best memory
#: costs the same tokens as the best one.
DEFAULT_LIMIT = 6

#: How many passages of the user's own documents. Smaller than the recall
#: budget because a passage is a page of text and a recollection is a sentence.
DEFAULT_KNOWLEDGE_LIMIT = 4


@dataclass(frozen=True, slots=True)
class AssembledContext:
    """The context for one run, and where each part came from."""

    goal: str
    facts: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    recalled: tuple[MemoryItem, ...] = ()
    retrieved: tuple[Passage, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.facts or self.constraints or self.recalled or self.retrieved)

    def recollections(self) -> tuple[str, ...]:
        """The remembered part, as the lines a prompt shows."""
        return tuple(recollection(item) for item in self.recalled if item.content.strip())

    def quotations(self) -> tuple[str, ...]:
        """The retrieved part, framed as what it is: someone else's words.

        Each passage carries its citation, because a quotation whose origin was
        dropped between the store and the prompt is a recollection - and this is
        deliberately not one.
        """
        return tuple(
            frame(passage.chunk.content.strip(), origin=passage.citation, kind="document")
            for passage in self.retrieved
            if passage.chunk.content.strip()
        )


class ContextAssembler:
    """Goal, assignment and memory, assembled into one context (§9.6)."""

    def __init__(
        self,
        memory: Memory | None,
        *,
        limit: int = DEFAULT_LIMIT,
        retriever: Retriever | None = None,
        knowledge_limit: int = DEFAULT_KNOWLEDGE_LIMIT,
        uses: MemoryUseLog | None = None,
    ) -> None:
        self._memory = memory
        self._uses = uses
        self._limit = limit
        # None where knowledge is switched off or nothing was ever added. The
        # run is then a Phase 14 run, which is the same arrangement memory has:
        # optional at every level, never a degraded special case.
        self._retriever = retriever
        self._knowledge_limit = knowledge_limit

    async def assemble(
        self,
        task: Task,
        definition: EmployeeDefinition,
        context: SharedContext | None = None,
    ) -> AssembledContext:
        passed = context or SharedContext()
        recalled = await self._recall(task, definition, passed)
        retrieved = await self._retrieve(task, passed)
        return AssembledContext(
            goal=task.goal,
            facts=passed.facts,
            constraints=passed.constraints,
            recalled=recalled,
            retrieved=retrieved,
        )

    async def _retrieve(
        self, task: Task, context: SharedContext
    ) -> tuple[Passage, ...]:
        """Passages of the user's own documents that bear on this task.

        Guarded exactly as recall is: knowledge improves a run and is never a
        precondition for one, so a store that cannot be read costs the run its
        citations rather than its existence.
        """
        if self._retriever is None or self._knowledge_limit <= 0:
            return ()
        try:
            passages = await self._retriever.retrieve(
                KnowledgeQuery(
                    text=" ".join((task.goal, *context.facts)),
                    workspace_id=task.workspace_id,
                    limit=self._knowledge_limit,
                )
            )
        except Exception as error:
            log.warning("knowledge.retrieval_failed", task_id=str(task.id), error=str(error))
            return ()
        if passages:
            log.info(
                "knowledge.retrieved",
                task_id=str(task.id),
                passages=len(passages),
                documents=len({passage.chunk.document_id for passage in passages}),
            )
        return tuple(passages)

    async def _recall(
        self, task: Task, definition: EmployeeDefinition, context: SharedContext
    ) -> tuple[MemoryItem, ...]:
        # None where memory is switched off and knowledge is not. The two are
        # separate capabilities, and a machine that wants its own documents
        # searched should not have to keep notes about its own runs to get them.
        if self._memory is None:
            return ()
        query = MemoryQuery(
            # The goal plus what came with it: an assignment's facts name the
            # subject in words the goal often leaves out.
            text=" ".join((task.goal, *context.facts)),
            workspace_id=task.workspace_id,
            # Three scopes inside this workspace and one above it: how the
            # person wants work done is true wherever they are working, and a
            # run that could not see it would ask them again (§15.4).
            scopes=frozenset(
                {
                    MemoryScope.WORKSPACE,
                    MemoryScope.PLAN,
                    MemoryScope.EMPLOYEE_PRIVATE,
                    MemoryScope.USER,
                }
            ),
            employee_id=definition.id,
            plan_id=task.plan_id,
            task_id=task.id,
            limit=self._limit,
        )
        try:
            items = await self._memory.recall(query)
        except Exception as error:
            # Memory is an improvement to a run, never a precondition for one.
            log.warning("memory.recall_failed", task_id=str(task.id), error=str(error))
            return ()
        if items:
            log.info(
                "memory.recalled",
                task_id=str(task.id),
                employee=definition.name,
                items=len(items),
            )
            await record_uses(
                self._uses, items, query, reader="task", task_id=task.id
            )
        return tuple(items)


async def record_uses(
    log_: MemoryUseLog | None,
    items: list[MemoryItem],
    query: MemoryQuery,
    *,
    reader: str,
    task_id=None,
    objective_id=None,
) -> None:
    """Write down why each of these was recalled. Never raises at the caller."""
    if log_ is None or not items:
        return
    try:
        await log_.record(
            [
                MemoryUse(
                    memory_id=item.id,
                    reason=(explanation := explain(item, query)).reason,
                    weight=explanation.weight,
                    objective_id=objective_id,
                    task_id=task_id,
                    workspace_id=query.workspace_id,
                    reader=reader,
                )
                for item in items
            ]
        )
    except Exception as error:  # an explanation must not cost the run its memory
        log.warning("memory.use_not_recorded", reader=reader, error=str(error))
