"""The manager's half of knowledge: what this workspace has been given to read.

`WorkspaceMemory` is the same shape one layer over, and the parallel is
deliberate. Prometheus reads at a different grain from an employee: an employee
retrieves against the task it was handed, and the manager retrieves against the
request as the person wrote it - before there is a plan, and before anybody has
been chosen. Without this, a question whose answer is in an uploaded document
gets read as needing no work, answered "I do not have that", and the document is
never reached, because retrieval only happened inside a task nobody started.

Two things follow from where this sits.

**What it returns is quoted, never asserted.** The passages go into
`SharedContext.facts` the way recollections do, but framed as external content
with their source attached (§25, ADR 0016): the manager is passing on somebody
else's words, and an instruction inside a document is not an instruction to the
platform.

**Failure is silence.** Retrieval that cannot answer costs the run its
citations, never the run - the same guarantee memory has, for the same reason:
the request is still worth attempting without it.
"""

from __future__ import annotations

import structlog

from domain.integrations.untrusted import frame
from domain.knowledge.models import KnowledgeQuery
from domain.knowledge.protocols import Retriever
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId

log = structlog.get_logger(__name__)

#: How much the manager carries. Smaller than a task's budget: this is prepended
#: to the reading of the request and to every task in the plan, so its cost is
#: paid per task rather than once.
DEFAULT_LIMIT = 3


class WorkspaceKnowledge:
    """What the user's own documents say about a request, for the manager."""

    def __init__(self, retriever: Retriever, *, limit: int = DEFAULT_LIMIT) -> None:
        self._retriever = retriever
        self._limit = limit

    async def context_for(
        self, request: str, *, workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    ) -> tuple[str, ...]:
        """Passages worth having in front of whoever reads this request."""
        if not request.strip():
            return ()
        try:
            passages = await self._retriever.retrieve(
                KnowledgeQuery(text=request, workspace_id=workspace_id, limit=self._limit)
            )
        except Exception as error:
            log.warning("knowledge.recall_failed", error=str(error))
            return ()
        if passages:
            log.info("knowledge.recalled", passages=len(passages))
        return tuple(
            frame(passage.chunk.content.strip(), origin=passage.citation, kind="document")
            for passage in passages
            if passage.chunk.content.strip()
        )
