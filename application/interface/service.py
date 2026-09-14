"""One object every interface talks to.

`PrometheusService` is a facade and is written to stay one. It decides nothing: it
does not plan, does not choose an employee, does not call a tool and does not
judge whether an action is allowed. Every method here is a short arrangement of
things that already exist - the manager, the task runner, the repositories, the
approval service - and the moment one of them starts containing a rule, that
rule has been moved out of the core and into the interface layer, which is the
regression this package exists to prevent.

Two consequences worth stating.

**There is one way to start work, and this is not a second one.** A request
becomes an objective through `PrometheusManager`, exactly as `ask-prometheus`, a workflow
step and a schedule firing do. A method here that ran a task itself would be a
second execution engine wearing a convenience name.

**Unknown means None, not an exception.** Asking for a conversation that does
not exist is a normal thing for an interface to do - a stale link, a window
reopened after a database was cleared - and the answer is "there is no such
thing", which every transport can render. Exceptions are kept for what actually
went wrong.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog

from application.integrations.service import IntegrationService
from application.interface import views
from application.interface.activity import Activity, ActivityEvent
from application.interface.contracts import RequestSource, UserRequest
from application.interface.runs import Runs
from application.knowledge.service import KnowledgeService
from application.providers.service import ProviderService
from application.workspaces.service import WorkspaceService
from domain.approvals.models import ApprovalState
from domain.approvals.protocols import (
    ApprovalRepository,
    ApprovalService,
    ApprovalWaiter,
)
from domain.capabilities.models import Capability
from domain.configuration.models import SettingValue
from domain.configuration.protocols import SettingsEditor
from domain.conversations.models import Conversation
from domain.conversations.repository import ConversationRepository
from domain.employees.protocols import EmployeeRegistry
from domain.errors import (
    ConfigurationError,
    IntegrationNotFoundError,
    NotFoundError,
    PrometheusError,
)
from domain.integrations.catalog import (
    PLUGIN_KEY,
    PluginCatalog,
    plan_install,
    suggested_holders,
)
from domain.integrations.models import Integration, IntegrationKind
from domain.knowledge.models import KnowledgeQuery
from domain.knowledge.protocols import Retriever
from domain.llm.catalog import DEFAULT_CONTEXT_TOKENS, ModelEntry
from domain.llm.models import TaskKind
from domain.llm.telemetry import LLMCallLog
from domain.memory.models import MemoryItem, MemoryKind, MemoryQuery, MemoryScope
from domain.memory.protocols import Memory, MemoryMaintenance
from domain.policies.models import ActorKind, SimpleActor
from domain.policies.risk import Effect
from domain.scheduling.models import MIN_INTERVAL_SECONDS, Recurrence, Schedule
from domain.scheduling.protocols import ScheduleRepository
from domain.secrets.protocols import CredentialStore
from domain.tasks.repository import TaskRepository
from domain.tools.protocols import ToolRegistry
from domain.tools.telemetry import ToolCallLog
from domain.workforce.directions import ApprovalChoice
from domain.workforce.repository import ObjectiveRepository, PlanRepository
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId

log = structlog.get_logger(__name__)

#: How much history a listing returns when the caller does not say. An
#: interface showing a person their own machine wants a page of it, not all of
#: it; anything wanting all of it is a report, and reports read the store.
DEFAULT_LIMIT = 50

#: The memory a person is shown, and therefore the only memory they can forget.
MEMORY_SHOWN = frozenset({MemoryScope.WORKSPACE, MemoryScope.USER})

#: A note is a sentence or a paragraph. Anything longer is a document, which is
#: quoted with its source rather than recalled (ADR 0016).
MAX_NOTE_LENGTH = 2000

#: What a person said outright outranks what the platform inferred from a run,
#: and matches a preference read out of a request.
STATED_IMPORTANCE = 0.8


class ApprovalsDisabledError(PrometheusError):
    """Asked to decide something on a configuration with no approvals at all."""


class IntegrationsDisabledError(PrometheusError):
    """Asked about connected services on a machine where they are switched off."""


class WorkspacesDisabledError(PrometheusError):
    """Asked to switch context on an interface built without workspaces."""


class KnowledgeDisabledError(PrometheusError):
    """Asked about documents on a machine where knowledge is switched off."""


class MemoryDisabledError(PrometheusError):
    """Asked to change memory on a machine where memory, or forgetting, is off."""


@dataclass(frozen=True, slots=True)
class ServiceDependencies:
    """Everything the facade arranges, handed in rather than reached for.

    A frozen bag rather than a dozen constructor arguments, and deliberately
    not the container: the container knows how to build an adapter, and nothing
    in the application layer is allowed to.
    """

    runs: Runs
    activity: Activity
    conversations: ConversationRepository
    objectives: ObjectiveRepository
    plans: PlanRepository
    tasks: TaskRepository
    employees: EmployeeRegistry
    approvals: ApprovalRepository
    waiter: ApprovalWaiter
    tool_calls: ToolCallLog
    llm_calls: LLMCallLog
    approval_service: ApprovalService | None = None
    #: None where integrations are switched off. Every method below then
    #: says so rather than pretending there are none: an interface showing
    #: an empty list would invite the user to add one and then fail.
    integrations: IntegrationService | None = None
    #: The services this machine offers to install, and whether each kind of
    #: server can start here. Both None where a surface was built without them;
    #: the listing is then empty rather than an error.
    plugins: PluginCatalog | None = None
    plugin_runtimes: Callable[[], dict[str, dict[str, object]]] | None = None
    #: Names of credentials the installation supplies to plugins. Names only:
    #: the values reach a server at connect and never this object.
    provided_credentials: frozenset[str] = frozenset()
    #: Where a credential typed into an interface is kept. Separate from
    #: the resolver every tool holds, so that reading one does not imply
    #: being able to write one.
    credentials: CredentialStore | None = None
    #: How contexts are separated on this machine. None only where an
    #: interface was built without one - the workspace of a request is then
    #: whatever it says, and nothing can be switched.
    workspaces: WorkspaceService | None = None
    #: The user's own documents, and the search over them. None where
    #: knowledge is switched off - the methods below then say so rather than
    #: answering with an empty list, which would invite somebody to add one.
    knowledge: KnowledgeService | None = None
    retriever: Retriever | None = None
    #: Providers, keys and where each kind of work goes. None where a surface
    #: was built without settings - every method below then says so rather than
    #: showing an empty list somebody would try to add to.
    providers: ProviderService | None = None
    #: What is remembered, and a person's own notes added to it.
    memory: Memory | None = None
    #: Forgetting, handed in separately. Holding `recall` still does not imply
    #: being able to delete (ADR 0009): a surface built with `memory` alone can
    #: show and add, and says so, rather than finding the power in the same
    #: object. Where both are given, what may be forgotten is exactly what
    #: `list_memory` shows - see `forget_memory`.
    memory_maintenance: MemoryMaintenance | None = None
    #: What this machine can do at all. Read-only here: the registry is the
    #: authority on which tools exist, and who may call one is the employee's
    #: own declaration - neither is an interface's to change.
    tools: ToolRegistry | None = None
    #: The switches this installation starts with, as Settings -> General
    #: shows them. None where a surface was built without one; the listing is
    #: then empty and a change is refused, rather than saved nowhere.
    settings: SettingsEditor | None = None
    #: The standing requests. None where a surface was built without them.
    schedules: ScheduleRepository | None = None
    #: Whether this process is firing schedules. Said rather than inferred: a
    #: schedule shown as "next at 09:00" on a machine that will not fire it is
    #: the one lie this screen must not tell.
    scheduler_running: bool = False
    history_limit: int = DEFAULT_LIMIT


class PrometheusService:
    """The application-level operations an interface is allowed to perform."""

    def __init__(self, dependencies: ServiceDependencies) -> None:
        self._d = dependencies
        #: Work started by a setting and owned by nobody else - held so it is
        #: not collected mid-way, and awaited when the interface closes.
        self._background: set[asyncio.Task[None]] = set()

    # --- Conversations --------------------------------------------------------

    async def _here(self) -> WorkspaceId:
        """The workspace a listing is about: the one this machine is working in.

        A listing that ignored it would show a person their other context's
        history the moment they switched, which is the whole thing a workspace
        is for. Where nothing separates contexts - an interface built without
        workspaces - it is the first one, which is what every listing meant
        before Phase 15.
        """
        if self._d.workspaces is None:
            return DEFAULT_WORKSPACE_ID
        return (await self._d.workspaces.active()).id

    async def create_conversation(self, title: str = "", *, workspace_id=None) -> dict[str, Any]:
        """Open a thread, in the workspace this machine is in unless told which."""
        conversation = Conversation.create(
            title, workspace_id=workspace_id or await self._here()
        )
        await self._d.conversations.save(conversation)
        log.info("interface.conversation_created", conversation_id=str(conversation.id))
        return views.conversation(conversation)

    async def list_conversations(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        found = await self._d.conversations.list_recent(
            await self._here(), limit=limit or self._d.history_limit
        )
        listed = []
        for item in found:
            # One read per thread, bounded by the history limit: a list that
            # cannot say which thread is still working sends a person opening
            # each one to find out.
            thread = await self._d.objectives.for_conversation(item.id)
            listed.append(
                views.conversation(
                    item,
                    messages=len(thread),
                    status=thread[-1].status.value if thread else None,
                )
            )
        return listed

    async def get_conversation(self, conversation_id: UUID) -> dict[str, Any] | None:
        """A thread and everything said in it, oldest first.

        The messages are objectives. Nothing is stored twice, so a thread cannot
        show an answer that the record of the work disagrees with.
        """
        conversation = await self._d.conversations.get(conversation_id)
        if conversation is None:
            return None
        thread = await self._d.objectives.for_conversation(conversation_id)
        schedule = await self._schedule_writing_into(conversation)
        # What the composer opens on. A schedule's thread shows the schedule's
        # own settings, because those are what its next run will use and what
        # the person set; any other thread shows how its last request was asked.
        if schedule is not None:
            chosen = schedule.directions
        elif thread:
            chosen = thread[-1].directions
        else:
            chosen = None
        return {
            **views.conversation(
                conversation,
                messages=len(thread),
                status=thread[-1].status.value if thread else None,
            ),
            "directions": views.directions(chosen) if chosen is not None else None,
            "schedule_id": str(schedule.id) if schedule is not None else None,
            "messages": [
                views.message(item, thinking=self._d.runs.is_thinking(item.id))
                for item in thread
            ],
        }

    async def _schedule_writing_into(self, conversation: Conversation) -> Schedule | None:
        if self._d.schedules is None:
            return None
        try:
            found = await self._d.schedules.list(conversation.workspace_id)
        except Exception as error:  # a thread must open even if schedules cannot be read
            log.warning("interface.schedules_unreadable", error=str(error))
            return None
        return next((item for item in found if item.conversation_id == conversation.id), None)

    async def rename_conversation(self, conversation_id: UUID, title: str) -> dict[str, Any] | None:
        conversation = await self._d.conversations.get(conversation_id)
        if conversation is None:
            return None
        renamed = conversation.renamed(title)
        await self._d.conversations.save(renamed)
        log.info("interface.conversation_renamed", conversation_id=str(conversation_id))
        return views.conversation(renamed)

    async def delete_conversation(self, conversation_id: UUID) -> bool:
        """Take a thread out of the list, stopping whatever is still running in it.

        The objectives are not deleted with it, for the reason a workspace's are
        not: what was asked, what ran and what it did to the machine is history,
        and the audit and memory that point at it must not start pointing at
        nothing. Stopped first, because a thread that disappears while its work
        carries on is work nobody can see or stop any more.
        """
        conversation = await self._d.conversations.get(conversation_id)
        if conversation is None:
            return False
        for item in await self._d.objectives.for_conversation(conversation_id):
            if not item.is_terminal:
                await self._d.runs.cancel_objective(item.id)
        deleted = await self._d.conversations.delete(conversation_id)
        log.info("interface.conversation_deleted", conversation_id=str(conversation_id))
        return deleted

    # --- Workspaces -----------------------------------------------------------

    async def list_workspaces(self) -> list[dict[str, Any]]:
        """What contexts exist here, and which one this machine is working in."""
        if self._d.workspaces is None:
            return []
        active = await self._d.workspaces.active()
        return [
            views.workspace(
                item,
                active=item.id == active.id,
                file_root=str(self._d.workspaces.root_for(item.id)),
            )
            for item in await self._d.workspaces.list()
        ]

    async def active_workspace(self) -> dict[str, Any] | None:
        if self._d.workspaces is None:
            return None
        item = await self._d.workspaces.active()
        return views.workspace(
            item, active=True, file_root=str(self._d.workspaces.root_for(item.id))
        )

    async def create_workspace(
        self, name: str, *, description: str = "", file_root: str | None = None
    ) -> dict[str, Any]:
        if self._d.workspaces is None:
            raise WorkspacesDisabledError("This interface has no workspaces behind it.")
        item = await self._d.workspaces.create(
            name, description=description, file_root=file_root
        )
        return views.workspace(item, file_root=str(self._d.workspaces.root_for(item.id)))

    async def update_workspace(
        self,
        workspace_id: WorkspaceId,
        *,
        name: str | None = None,
        description: str | None = None,
        file_root: str | None = None,
    ) -> dict[str, Any]:
        if self._d.workspaces is None:
            raise WorkspacesDisabledError("This interface has no workspaces behind it.")
        item = await self._d.workspaces.update(
            workspace_id, name=name, description=description, file_root=file_root
        )
        return views.workspace(item, file_root=str(self._d.workspaces.root_for(item.id)))

    async def use_workspace(self, workspace_id: WorkspaceId) -> dict[str, Any]:
        """Switch this machine. What it moves is the default for the next request.

        A run already going keeps the workspace it started in - that is
        `WorkspaceContext.enter`, applied by the task runner - so switching
        never reaches inside work that is already happening.
        """
        if self._d.workspaces is None:
            raise WorkspacesDisabledError("This interface has no workspaces behind it.")
        item = await self._d.workspaces.use(workspace_id)
        return views.workspace(
            item, active=True, file_root=str(self._d.workspaces.root_for(item.id))
        )

    async def delete_workspace(self, workspace_id: WorkspaceId) -> bool:
        """Remove the record. History and files stay, deliberately (§15.1)."""
        if self._d.workspaces is None:
            raise WorkspacesDisabledError("This interface has no workspaces behind it.")
        return await self._d.workspaces.delete(workspace_id)

    # --- Memory ---------------------------------------------------------------

    @property
    def memory_available(self) -> bool:
        return self._d.memory is not None

    @property
    def memory_can_forget(self) -> bool:
        return self._d.memory is not None and self._d.memory_maintenance is not None

    async def _readable(self, text: str = "", limit: int = 20) -> MemoryQuery:
        """What a person may see of memory here, stated once for reading and forgetting.

        The same scopes a run reads - this workspace's, and the person's own -
        and deliberately not an employee's private notes: the facade has no more
        access to the store than a running task does (ADR 0009).
        """
        return MemoryQuery(
            text=text,
            workspace_id=await self._here(),
            scopes=MEMORY_SHOWN,
            limit=limit,
        )

    async def list_memory(self, *, search: str = "", limit: int = 20) -> list[dict[str, Any]]:
        """What this workspace remembers, through the one contract memory has.

        A search is `recall` with words in it, ranked by the domain like every
        other recall - not a second query written for a screen.
        """
        if self._d.memory is None:
            return []
        items = await self._d.memory.recall(await self._readable(search, limit))
        return [views.memory_item(item) for item in items]

    async def remember(self, content: str, *, about_the_person: bool) -> dict[str, Any]:
        """Keep something a person told the platform directly.

        SEMANTIC and without an expiry, like a preference read out of a request:
        it is a statement of how things are, and what supersedes it is the
        person removing it, not time passing. `about_the_person` is the one
        question worth asking - is this true of you everywhere, or of this
        workspace - because it is the difference between USER and WORKSPACE,
        and every other field has one right answer.
        """
        if self._d.memory is None:
            raise MemoryDisabledError("Memory is switched off on this machine.")
        stated = " ".join(content.split())
        if not stated:
            raise ValueError("There is nothing to remember.")
        if len(stated) > MAX_NOTE_LENGTH:
            raise ValueError(
                f"A note is at most {MAX_NOTE_LENGTH} characters; "
                "a longer text is a document."
            )
        here = await self._here()
        item = MemoryItem.create(
            stated,
            scope=MemoryScope.USER if about_the_person else MemoryScope.WORKSPACE,
            kind=MemoryKind.SEMANTIC,
            workspace_id=here,
            importance=STATED_IMPORTANCE,
            metadata={"source": views.STATED_BY_PERSON, "stated_in": str(here)},
        )
        await self._d.memory.remember(item)
        return views.memory_item(item)

    async def forget_memory(self, item_id: UUID) -> bool:
        """Forget one thing a person can see. False where there is no such thing *here*.

        Bounded by the same query `list_memory` reads with, so an id copied from
        another workspace, or belonging to an employee's private notes, is not
        found rather than deleted.
        """
        if self._d.memory is None or self._d.memory_maintenance is None:
            raise MemoryDisabledError("Forgetting is not available on this machine.")
        forgotten = await self._d.memory_maintenance.forget(
            [item_id], within=await self._readable()
        )
        return forgotten > 0

    # --- Knowledge ------------------------------------------------------------

    @property
    def knowledge_available(self) -> bool:
        return self._d.knowledge is not None

    async def list_documents(self) -> list[dict[str, Any]]:
        """What the active workspace knows because somebody put it there."""
        if self._d.knowledge is None:
            return []
        found = await self._d.knowledge.list(workspace_id=await self._here())
        return [views.document(item) for item in found]

    async def add_document(
        self, path: str, *, title: str = "", media_type: str = ""
    ) -> dict[str, Any]:
        """Read a file on this machine into the active workspace.

        By path, not by bytes: the file the person dropped on the window is
        already on this machine, and carrying it through the request would make
        the interface a second file store - the same reasoning as `Attachment`.
        """
        if self._d.knowledge is None:
            raise KnowledgeDisabledError("Documents are switched off on this machine.")
        document = await self._d.knowledge.add_file(
            Path(path), workspace_id=await self._here(), title=title, media_type=media_type
        )
        return views.document(document)

    async def replace_document(self, document_id: UUID, path: str) -> dict[str, Any]:
        """A newer version of a document's file, read in place of the old one."""
        if self._d.knowledge is None:
            raise KnowledgeDisabledError("Documents are switched off on this machine.")
        return views.document(await self._d.knowledge.replace_file(document_id, Path(path)))

    async def reindex_document(self, document_id: UUID) -> dict[str, Any]:
        if self._d.knowledge is None:
            raise KnowledgeDisabledError("Documents are switched off on this machine.")
        return views.document(await self._d.knowledge.reindex(document_id))

    async def delete_document(self, document_id: UUID) -> bool:
        """Remove the document and its passages. Memory is left alone (ADR 0016)."""
        if self._d.knowledge is None:
            raise KnowledgeDisabledError("Documents are switched off on this machine.")
        return await self._d.knowledge.delete(document_id)

    async def search_documents(self, question: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """What the documents say about a question, as passages with their source.

        The same retrieval a run gets, asked directly. It is here so a person
        can see what an employee would have been given, which is the difference
        between "the answer was wrong" and "the answer was not in there".
        """
        if self._d.retriever is None:
            return []
        found = await self._d.retriever.retrieve(
            KnowledgeQuery(text=question, workspace_id=await self._here(), limit=limit)
        )
        return [views.passage(item) for item in found]

    # --- Asking for work ------------------------------------------------------

    async def submit(self, request: UserRequest) -> dict[str, Any]:
        """The one way in. Everything else on this object reads or stops work.

        The request's `source` is logged and never branched on: a sentence typed
        into a desktop window and the same sentence sent from a terminal produce
        the same objective and the same plan. That is the property the whole
        interface layer exists to keep, and it is kept here by not writing the
        `if`.
        """
        text = request.text
        if not text:
            raise PrometheusError("An empty request has nothing to work on.")

        conversation = await self._thread_for(request)
        objective = await self._d.runs.ask(
            text,
            conversation_id=conversation.id if conversation else None,
            workspace_id=request.workspace_id,
            directions=request.directions,
        )
        log.info(
            "interface.request_submitted",
            objective_id=str(objective.id),
            source=request.source.value,
            workspace_id=str(request.workspace_id),
            input_type=request.input_type.value,
            approvals=request.directions.approvals.value,
            model=request.directions.model or None,
            attachments=len(request.attachments),
            conversation_id=str(conversation.id) if conversation else None,
        )
        return views.message(objective, thinking=True)

    async def _thread_for(self, request: UserRequest) -> Conversation | None:
        """Name the thread after its first request, and mark it spoken in.

        A request naming a thread that no longer exists is not an error: the
        work is what the person asked for, and losing it to a stale window id
        would be the interface deciding something. It becomes a standalone
        objective, exactly like one from the CLI.
        """
        if request.conversation_id is None:
            return None
        conversation = await self._d.conversations.get(request.conversation_id)
        if conversation is None:
            log.info(
                "interface.unknown_conversation", conversation_id=str(request.conversation_id)
            )
            return None
        updated = conversation.titled_from(request.text).touched(request.received_at)
        await self._d.conversations.save(updated)
        return updated

    async def start_task(self, goal: str, employee: str) -> dict[str, Any]:
        """Hand one task to one named employee, bypassing the manager.

        Kept because a script and a machine with no interface still need it, and
        deliberately not what a conversational surface calls: choosing who does
        the work is the manager's job, not the user's.
        """
        task = await self._d.runs.start(goal.strip(), employee)
        return views.task_summary(task, running=True)

    # --- Watching -------------------------------------------------------------

    async def list_objectives(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        found = await self._d.objectives.list_recent(
            await self._here(), limit=limit or self._d.history_limit
        )
        return [
            views.objective_summary(item, thinking=self._d.runs.is_thinking(item.id))
            for item in found
        ]

    async def get_objective(self, objective_id: UUID) -> dict[str, Any] | None:
        item = await self._d.objectives.get(objective_id)
        if item is None:
            return None
        return views.objective_detail(
            item,
            thinking=self._d.runs.is_thinking(objective_id),
            plans=await self._d.plans.for_objective(objective_id),
        )

    async def list_tasks(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        found = await self._d.tasks.list_recent(
            await self._here(), limit=limit or self._d.history_limit
        )
        return [
            views.task_summary(task, running=self._d.runs.is_running(task.id))
            for task in found
        ]

    async def get_task(self, task_id: UUID) -> dict[str, Any] | None:
        task = await self._d.tasks.get(task_id)
        if task is None:
            return None
        names = {d.id: d.name for d in self._d.employees.list()}
        return views.task_detail(
            task,
            running=self._d.runs.is_running(task_id),
            calls=await self._d.tool_calls.list_for_task(task_id),
            events=await self._d.tasks.events(task_id),
            employee=names.get(task.assigned_employee_id) if task.assigned_employee_id else None,
        )

    def task_activity(self, task_id: UUID | None = None) -> AsyncIterator[ActivityEvent | None]:
        return self._d.activity.for_task(task_id)

    def objective_activity(self, objective_id: UUID) -> AsyncIterator[ActivityEvent | None]:
        return self._d.activity.for_objective(objective_id)

    # --- Stopping -------------------------------------------------------------

    async def cancel_task(self, task_id: UUID, reason: str = "") -> dict[str, Any] | None:
        task = await self._d.runs.cancel(task_id, reason)
        if task is None:
            return None
        return views.task_summary(task, running=self._d.runs.is_running(task_id))

    async def cancel_objective(self, objective_id: UUID) -> dict[str, Any] | None:
        stopped = await self._d.runs.cancel_objective(objective_id)
        item = await self._d.objectives.get(objective_id)
        if item is None:
            return None
        return {**views.objective_summary(item), "stopped": stopped}

    # --- Approvals ------------------------------------------------------------

    async def list_approvals(self) -> list[dict[str, Any]]:
        """What is waiting, live first.

        The stored rows are read too, because a question left behind by a killed
        run is still an open decision - it is simply one no tool call is parked
        on, and saying which is which beats implying they are the same.
        """
        live = {item.id: item for item in self._d.waiter.pending()}
        stored = await self._d.approvals.list_pending()
        return [views.approval(item, live=True) for item in live.values()] + [
            views.stored_approval(record) for record in stored if record.id not in live
        ]

    async def decide_approval(
        self, approval_id: UUID, *, approved: bool, comment: str = ""
    ) -> dict[str, Any]:
        """Answer a question. The interface carries the answer and nothing else.

        Whether the action needed asking was decided by the policy engine before
        this was ever shown to anybody; whether it now happens is decided by the
        person. Nothing in the interface layer gets a vote, which is why there
        is no path here that resolves an approval on its own.
        """
        state = ApprovalState.APPROVED if approved else ApprovalState.REJECTED
        answered = self._d.waiter.decide(approval_id, approved)
        if not answered:
            service = self._d.approval_service
            if service is None:
                raise ApprovalsDisabledError(
                    "Approvals are switched off in this configuration."
                )
            await service.resolve(approval_id, state, comment=comment)
        return {"id": str(approval_id), "state": state.value, "live": answered}


    # --- Integrations ---------------------------------------------------------

    def _integrations(self) -> IntegrationService:
        if self._d.integrations is None:
            raise IntegrationsDisabledError(
                "Integrations are switched off in this configuration."
            )
        return self._d.integrations

    @property
    def integrations_available(self) -> bool:
        """Whether this machine can connect anything at all.

        Asked by an interface deciding whether to show the section, and it is
        the only integration question that has an answer when the feature is
        off - everything else raises, because "there are none" and "you cannot
        have any" are different things to tell a person.
        """
        return self._d.integrations is not None

    # --- Providers, keys and where work goes ----------------------------------
    #
    # A key goes in through here and never comes back out: no method returns a
    # credential, and the views carry `has_key` instead. Everything else about
    # a provider is ordinary configuration and is shown in full.

    def _providers(self) -> ProviderService:
        if self._d.providers is None:
            raise ConfigurationError(
                "Provider settings are not available in this process."
            )
        return self._d.providers

    async def list_provider_kinds(self) -> list[dict[str, Any]]:
        """What this machine can talk to at all. The list a person picks from."""
        return [views.provider_kind(kind) for kind in self._providers().kinds()]

    async def provider_guide(self) -> dict[str, Any] | None:
        """Recommended setups, with whether each is connected and applied here."""
        providers = self._providers()
        guide = providers.guide
        if guide is None:
            return None
        workspace = await self._here()
        entries = await providers.list_models(workspace)
        connections: dict[str, str] = {}
        for connection in await providers.list_connections(workspace):
            connections.setdefault(connection.kind, connection.name)
        return views.provider_guide(
            guide,
            applied={
                setup.id: setup.kind in connections and providers.setup_applied(setup, entries)
                for setup in guide.setups
            },
            connections=connections,
        )

    async def apply_provider_setup(self, setup_id: str, connection: str = "") -> dict[str, Any]:
        providers = self._providers()
        added = await providers.apply_setup(setup_id, connection.strip(), await self._here())
        await self._follow_embedding_model()
        return {"added": [entry.name for entry in added]}

    async def list_connections(self) -> list[dict[str, Any]]:
        providers = self._providers()
        stored = await self._stored_credential_names()
        return [
            views.connection(item, has_key=item.secret_name in stored)
            for item in await providers.list_connections(await self._here())
        ]

    async def add_connection(
        self,
        name: str,
        kind: str,
        *,
        api_key: str = "",
        base_url: str = "",
        description: str = "",
    ) -> dict[str, Any]:
        connection = await self._providers().add_connection(
            name,
            kind,
            api_key=api_key,
            base_url=base_url,
            description=description,
            workspace_id=await self._here(),
        )
        return views.connection(connection, has_key=bool(connection.secret_name))

    async def replace_connection_key(self, name: str, api_key: str) -> dict[str, Any]:
        connection = await self._providers().replace_key(name, api_key, await self._here())
        return views.connection(connection, has_key=True)

    async def remove_connection(self, name: str) -> None:
        await self._providers().remove_connection(name, await self._here())

    async def list_installed_models(self, connection: str) -> dict[str, Any]:
        """What a runner already has, and whether it answered or its disk did."""
        found = await self._providers().available_models(connection, await self._here())
        return views.installed_models(found)

    async def list_models(self) -> list[dict[str, Any]]:
        providers = self._providers()
        workspace = await self._here()
        defaults = await providers.defaults(workspace)
        used_for: dict[str, list[str]] = {}
        for kind, entry_name in defaults.items():
            used_for.setdefault(entry_name, []).append(kind.value)
        return [
            views.model_entry(entry, used_for=tuple(sorted(used_for.get(entry.name, ()))))
            for entry in await providers.list_models(workspace)
        ]

    async def add_model(
        self,
        name: str,
        provider: str,
        model: str,
        *,
        connection: str = "",
        capabilities: tuple[str, ...] = (),
        context_tokens: int | None = None,
        input_cost_per_1k_usd: float = 0.0,
        output_cost_per_1k_usd: float = 0.0,
        quality: float = 0.5,
        dimensions: int = 0,
    ) -> dict[str, Any]:
        """Strings in, domain values on - as everywhere else on this boundary.

        A capability this platform does not have is an error the caller can
        read, never a word quietly dropped: an entry whose capabilities were
        half-ignored is a model the router will not choose, for a reason nobody
        can see in the window.
        """
        providers = self._providers()
        workspace = await self._here()
        if context_tokens is None:
            # Asked of the runner rather than defaulted: a number nobody typed
            # is the one most likely to be wrong, and a wrong one here makes
            # the router refuse the model for reasons the window never shows.
            context_tokens = await providers.context_of(
                connection.strip(), model.strip(), workspace
            ) or DEFAULT_CONTEXT_TOKENS
        entry = ModelEntry(
            name=name.strip(),
            provider=provider.strip(),
            model=model.strip(),
            connection=connection.strip(),
            capabilities=frozenset(_capability(item) for item in capabilities),
            context_tokens=context_tokens,
            input_cost_per_1k_usd=input_cost_per_1k_usd,
            output_cost_per_1k_usd=output_cost_per_1k_usd,
            quality=quality,
            dimensions=dimensions,
        )
        added = views.model_entry(await providers.add_model(entry, workspace))
        await self._follow_embedding_model()
        return added

    async def remove_model(self, name: str) -> None:
        await self._providers().remove_model(name, await self._here())
        await self._follow_embedding_model()

    async def list_task_defaults(self) -> dict[str, str]:
        """Which model each kind of work goes to."""
        defaults = await self._providers().defaults(await self._here())
        return {kind.value: name for kind, name in defaults.items()}

    async def send_work_to(self, task_kind: str, entry_name: str) -> dict[str, str]:
        await self._providers().send_work_to(
            _task_kind(task_kind), entry_name, await self._here()
        )
        await self._follow_embedding_model()
        return await self.list_task_defaults()

    async def clear_task_default(self, task_kind: str) -> dict[str, str]:
        await self._providers().clear_default(_task_kind(task_kind))
        await self._follow_embedding_model()
        return await self.list_task_defaults()

    async def _follow_embedding_model(self) -> None:
        """Re-index, in the background, what the model now chosen did not embed.

        Any change to models can change which one embeds - routing it, clearing
        it, removing the entry it pointed at - so this follows every one of them
        and lets the store say whether there is anything to do. In the
        background because a setting should answer when it is saved, and a
        workspace of documents takes longer than that; the documents screen
        shows them re-indexed as each one finishes.
        """
        if self._d.knowledge is None:
            return
        knowledge, workspace = self._d.knowledge, await self._here()

        async def reindex() -> None:
            try:
                await knowledge.reindex_stale(workspace_id=workspace)
            except Exception as error:  # a setting was saved; this is extra
                log.warning("knowledge.reindex_failed", error=str(error))

        work = asyncio.create_task(reindex())
        self._background.add(work)
        work.add_done_callback(self._background.discard)

    async def _stored_credential_names(self) -> frozenset[str]:
        if self._d.credentials is None:
            return frozenset()
        return frozenset(await self._d.credentials.names())

    async def list_integrations(self) -> list[dict[str, Any]]:
        return [views.integration(item) for item in await self._integrations().list()]

    async def get_integration(self, integration_id: UUID) -> dict[str, Any] | None:
        try:
            return views.integration(await self._integrations().get(integration_id))
        except IntegrationNotFoundError:
            # Unknown is None, as everywhere else on this object: a stale link
            # in a window somebody left open is a normal thing to hand in.
            return None

    async def add_integration(
        self,
        name: str,
        configuration: dict[str, Any],
        *,
        kind: str = IntegrationKind.MCP.value,
        capabilities: tuple[str, ...] = (),
        secret_names: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Write down a service. Starting it is `connect_integration`.

        Strings come in and domain values go on: an interface names a capability
        the way a person typed it, and one this platform does not have is an
        error the caller can read rather than a silently ignored word.
        """
        item = await self._integrations().add(
            name,
            configuration,
            kind=IntegrationKind(kind),
            capabilities=frozenset(_capability(value) for value in capabilities),
            secret_names=tuple(secret_names),
        )
        return views.integration(item)

    async def connect_integration(self, integration_id: UUID) -> dict[str, Any]:
        """Start it and ask what it offers.

        A failure comes back as a status on the integration rather than as an
        exception: "that command did not start" is information the person needs
        next to the thing that did not start, not a stack trace.
        """
        return views.integration(await self._integrations().connect(integration_id))

    async def enable_integration(self, integration_id: UUID) -> dict[str, Any]:
        return views.integration(await self._integrations().enable(integration_id))

    async def disable_integration(self, integration_id: UUID) -> dict[str, Any]:
        return views.integration(await self._integrations().disable(integration_id))

    async def remove_integration(self, integration_id: UUID) -> bool:
        return await self._integrations().remove(integration_id)

    async def classify_capability(
        self, integration_id: UUID, effects: dict[str, str]
    ) -> dict[str, Any]:
        """Say what an integration's tools do to the world.

        This is where a person's judgement enters the trust boundary, and it is
        the only way in: the server's own claims never reach the stored map
        (ADR 0015). What follows from the classification - the risk, whether it
        asks - is not decided here and cannot be set from here.
        """
        classified = await self._integrations().classify(
            integration_id, {name: _effect(value) for name, value in effects.items()}
        )
        return views.integration(classified)

    # --- Plugins -----------------------------------------------------------------

    async def list_plugins(self) -> dict[str, Any]:
        """Everything that can be installed, and what is installed already.

        One answer for the whole screen, because every part of it depends on
        the same three reads: the catalog, the integrations, the employees. An
        integration that no plugin describes - a server somebody added by hand -
        is listed among the installed ones too, since to the person it is
        simply another thing this machine is connected to.
        """
        if self._d.integrations is None:
            return {"available": False, "runtimes": {}, "plugins": [], "installed": []}
        installed = await self._integrations().list()
        by_plugin = {_plugin_of(item): item for item in installed if _plugin_of(item)}
        runtimes = self._d.plugin_runtimes() if self._d.plugin_runtimes else {}
        definitions = self._d.employees.list()
        stored = set(await self._d.credentials.names()) if self._d.credentials else set()
        plugins = self._d.plugins.list() if self._d.plugins else []
        known = {plugin.id for plugin in plugins}
        return {
            "available": True,
            "runtimes": runtimes,
            "plugins": [
                views.plugin(
                    plugin,
                    installed=by_plugin.get(plugin.id),
                    runtime_ready=bool(runtimes.get(plugin.runtime.value, {}).get("ready", True)),
                    suggested=suggested_holders(plugin, definitions),
                    stored=stored,
                    provided=self._d.provided_credentials,
                )
                for plugin in plugins
            ],
            "installed": [
                views.installed_integration(
                    item,
                    plugin=_plugin_of(item) if _plugin_of(item) in known else "",
                    holders=_holders(item, definitions),
                )
                for item in installed
            ],
        }

    async def install_plugin(
        self,
        plugin_id: str,
        values: dict[str, str],
        *,
        employees: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        """Install a plugin: keep its secrets, record it, grant it, start it.

        Four operations the core already has, in the one order that works - a
        secret before the record that names it, the grant before the connection
        announces its tools - so a person asking for one thing gets one call.
        A server that does not start still leaves the record, with the status
        saying why, exactly as adding one by hand does.

        `employees` None means the suggestion; an empty tuple means nobody, which
        a person may choose and which is then theirs to change.
        """
        integrations = self._integrations()
        if self._d.plugins is None:
            raise NotFoundError("No plugins are declared on this machine.")
        plugin = self._d.plugins.get(plugin_id)
        if plugin is None:
            raise NotFoundError(f"Unknown plugin: {plugin_id}")
        stored = (
            frozenset(await self._d.credentials.names()) if self._d.credentials else frozenset()
        )
        plan = plan_install(plugin, values, stored=stored | self._d.provided_credentials)
        if plan.secrets:
            if self._d.credentials is None:
                raise IntegrationsDisabledError(
                    "There is nowhere to keep a credential in this configuration."
                )
            for name, value in plan.secrets.items():
                await self._d.credentials.store(name, value)
        definitions = self._d.employees.list()
        known = {definition.name for definition in definitions}
        chosen = (
            suggested_holders(plugin, definitions)
            if employees is None
            else [name for name in employees if name in known]
        )
        item = await integrations.add(
            plugin.id,
            plan.configuration,
            capabilities=plugin.capabilities,
            secret_names=plan.secret_names,
            effects=dict(plugin.effects),
            granted_to=frozenset(chosen),
        )
        connected = await integrations.connect(item.id)
        return views.installed_integration(
            connected, plugin=plugin.id, holders=_holders(connected, self._d.employees.list())
        )

    async def sign_in_integration(self, integration_id: UUID) -> dict[str, Any]:
        """Ask a plugin whose server signs in through the browser to do so.

        The call is the plugin's declared probe - a read - with `${KEY}` filled
        from the plain settings it was installed with. `signed_in` false means
        the server was not yet authorised and has opened its own sign-in page on
        this machine; asking again once that is done answers true.
        """
        integrations = self._integrations()
        item = await integrations.get(integration_id)
        plugin = self._d.plugins.get(_plugin_of(item)) if self._d.plugins else None
        if plugin is None or plugin.sign_in is None:
            raise NotFoundError(f"{item.name} has no sign-in to start.")
        env = item.configuration.get("env") or {}
        arguments = {
            key: _filled(value, env if isinstance(env, dict) else {})
            for key, value in plugin.sign_in.arguments.items()
        }
        signed_in = await integrations.try_tool(item.id, plugin.sign_in.tool, arguments)
        return {"signed_in": signed_in}

    async def grant_integration(
        self, integration_id: UUID, employees: tuple[str, ...]
    ) -> dict[str, Any]:
        """Which employees may use a connected service, as chosen in the window.

        A name that is not an employee here is dropped rather than stored: a grant
        to nobody-in-particular would read as a restriction that is in force.
        """
        known = {definition.name for definition in self._d.employees.list()}
        item = await self._integrations().grant(
            integration_id, frozenset(name for name in employees if name in known)
        )
        # Read again after the grant: the registry's snapshot of who holds what
        # is refreshed by the change itself, and the list from before it would
        # still carry the grant just taken away.
        return views.installed_integration(
            item, plugin=_plugin_of(item), holders=_holders(item, self._d.employees.list())
        )

    async def store_credential(self, name: str, value: str) -> dict[str, Any]:
        """Keep a credential this machine will need at the moment of a call.

        The value goes in and never comes back out of this boundary: what is
        returned is the name, so an interface can show that it is set without
        ever holding it.
        """
        store = self._d.credentials
        if store is None:
            raise IntegrationsDisabledError(
                "There is nowhere to keep a credential in this configuration."
            )
        await store.store(name, value)
        return {"name": name, "stored": True}

    # --- Work that starts on its own -------------------------------------------

    def _schedules(self) -> ScheduleRepository:
        if self._d.schedules is None:
            raise ConfigurationError("This interface was built without schedules.")
        return self._d.schedules

    async def list_schedules(self) -> dict[str, Any]:
        """The standing requests here, and whether anything is firing them."""
        if self._d.schedules is None:
            return {"available": False, "running": False, "schedules": []}
        found = await self._d.schedules.list(await self._here())
        listed = []
        for item in found:
            status = None
            if item.last_objective_id is not None:
                last = await self._d.objectives.get(item.last_objective_id)
                status = last.status.value if last else None
            listed.append(views.schedule(item, last_status=status))
        return {"available": True, "running": self._d.scheduler_running, "schedules": listed}

    async def create_schedule(
        self,
        request: str,
        *,
        name: str = "",
        every_minutes: int | None = None,
        daily_at: str = "",
        utc_offset_minutes: int = 0,
        on_event: str = "",
        conversation_id: UUID | None = None,
        model: str = "",
        approvals: str = "ASK",
    ) -> dict[str, Any]:
        """A standing request, with a thread its runs are written into.

        Exactly one "when". A time of day arrives on the person's clock with
        that clock's offset and is stored in UTC, as every schedule is: a
        machine that travels or changes its clocks keeps firing at the moment
        that was meant when it was set. The thread is the one it was made from,
        or a new one named after it - created now, so the first run has
        somewhere to go and the window has something to open.
        """
        store = self._schedules()
        text = request.strip()
        if not text:
            raise PrometheusError("A schedule with no request would ask for nothing.")
        recurrence = _recurrence(every_minutes, daily_at, utc_offset_minutes, on_event)
        workspace = await self._here()
        model = await self._model_for_schedule(model, workspace)

        thread = None
        if conversation_id is not None:
            thread = await self._d.conversations.get(conversation_id)
        if thread is None:
            thread = Conversation.create(name.strip() or text, workspace_id=workspace)
            await self._d.conversations.save(thread)

        try:
            created = Schedule.create(
                text,
                name=name.strip(),
                recurrence=recurrence,
                on_event=on_event.strip(),
                workspace_id=workspace,
                conversation_id=thread.id,
                model=model,
                approvals=_approval_choice(approvals),
            )
        except ValueError as error:
            raise PrometheusError(str(error)) from error
        await store.save(created)
        log.info("schedule.created", schedule_id=str(created.id), when=created.describe())
        return views.schedule(created)

    async def update_schedule(
        self,
        schedule_id: UUID,
        request: str,
        *,
        name: str = "",
        every_minutes: int | None = None,
        daily_at: str = "",
        utc_offset_minutes: int = 0,
        on_event: str = "",
        model: str = "",
        approvals: str = "ASK",
    ) -> dict[str, Any]:
        """Say a schedule differently: what, when, with which model and approvals.

        The whole schedule is sent, as the form holds it, and checked by the same
        rules as a new one. Its thread, its count and its last run stay - it is
        the same standing instruction, and its history is still its own.
        """
        store = self._schedules()
        found = await store.get(schedule_id)
        if found is None:
            raise NotFoundError("No schedule with that id.")
        recurrence = _recurrence(every_minutes, daily_at, utc_offset_minutes, on_event)
        chosen = await self._model_for_schedule(model, found.workspace_id)
        try:
            updated = found.edited(
                request=request,
                name=name,
                recurrence=recurrence,
                on_event=on_event,
                model=chosen,
                approvals=_approval_choice(approvals),
            )
        except ValueError as error:
            raise PrometheusError(str(error)) from error
        await store.save(updated)
        log.info("schedule.edited", schedule_id=str(updated.id), when=updated.describe())
        return views.schedule(updated)

    async def _model_for_schedule(self, model: str, workspace: WorkspaceId) -> str:
        """A catalog entry that can write text, or "" for the router's choice.

        Checked when it is chosen rather than when it runs: a schedule that
        names a model nobody has is found out at four in the morning otherwise.
        An entry removed later is not an error either way - the router passes a
        preference it cannot find and decides as it would have.
        """
        name = model.strip()
        if not name or self._d.providers is None:
            return name
        entries = {entry.name: entry for entry in await self._d.providers.list_models(workspace)}
        entry = entries.get(name)
        if entry is None:
            raise PrometheusError(f"There is no model called '{name}' in the catalog.")
        if not entry.generates_text:
            raise PrometheusError(f"'{name}' cannot write text, so it cannot run a request.")
        return name

    async def set_schedule_enabled(self, schedule_id: UUID, enabled: bool) -> dict[str, Any]:
        store = self._schedules()
        found = await store.get(schedule_id)
        if found is None:
            raise NotFoundError("No schedule with that id.")
        updated = found.set_enabled(enabled)
        await store.save(updated)
        return views.schedule(updated)

    async def delete_schedule(self, schedule_id: UUID) -> bool:
        """The instruction goes; the thread and what its runs did stay, as history does."""
        store = self._schedules()
        if await store.get(schedule_id) is None:
            raise NotFoundError("No schedule with that id.")
        return await store.delete(schedule_id)

    async def run_schedule_now(self, schedule_id: UUID) -> dict[str, Any]:
        """The request, asked now, in the schedule's thread - through the one way in.

        Not a firing: the schedule's clock and its count stay as they were, and
        the run is a person asking, with a person there to answer approvals.
        """
        found = await self._schedules().get(schedule_id)
        if found is None:
            raise NotFoundError("No schedule with that id.")
        answer = await self.submit(
            UserRequest(
                content=found.request,
                source=RequestSource.API,
                conversation_id=found.conversation_id,
                workspace_id=found.workspace_id,
                directions=found.directions,
            )
        )
        return {
            **answer,
            "conversation_id": str(found.conversation_id) if found.conversation_id else None,
        }

    # --- General settings -----------------------------------------------------

    async def list_settings(self) -> dict[str, Any]:
        """Every switch, and whether anything saved is still waiting for a restart.

        `restart_needed` is read off the settings rather than kept here: the
        editor already knows both what is running and what was saved, and a
        second flag in the facade would be one more thing to fall out of step.
        """
        if self._d.settings is None:
            return {"available": False, "restart_needed": False, "any_saved": False, "settings": []}
        return _settings_view(self._d.settings.current())

    async def change_settings(self, values: dict[str, SettingValue]) -> dict[str, Any]:
        if self._d.settings is None:
            raise ConfigurationError("This interface was built without settings.")
        changed = self._d.settings.change(values)
        log.info("settings.changed", keys=sorted(values))
        return _settings_view(changed)

    async def reset_settings(self, keys: list[str] | None = None) -> dict[str, Any]:
        """Back to `.env` or the platform's default - the named keys, or every one."""
        if self._d.settings is None:
            raise ConfigurationError("This interface was built without settings.")
        reset = self._d.settings.reset(keys)
        log.info("settings.reset", keys=sorted(keys) if keys is not None else "all")
        return _settings_view(reset)

    # --- The workforce --------------------------------------------------------

    def list_employees(self) -> list[dict[str, Any]]:
        """Who is available, and what each of them may do.

        Read from the registry every time rather than cached: an employee is a
        directory, and one added while the interface is open should appear in it.
        """
        return [views.employee(d) for d in self._d.employees.list()]

    def list_tools(self) -> list[dict[str, Any]]:
        """Everything this machine can do, and which employees may ask for it.

        Asked with an actor that is allowed everything, because the question is
        what exists here rather than what one employee may call - the registry
        would otherwise answer with the caller's own privileges, and a person
        looking at their machine's capabilities would see a filtered list with
        nothing saying it was filtered.

        Least privilege is still what is *reported*: each tool carries the
        employees that listed it, so a tool nobody lists reads as reaching
        nobody, which is exactly what it does.
        """
        if self._d.tools is None:
            return []
        declared = self._d.employees.list()
        everything = SimpleActor("interface", ActorKind.USER, frozenset({"*"}))
        return [
            views.tool(
                spec,
                used_by=tuple(
                    sorted(d.name for d in declared if spec.name in d.allowed_tools)
                ),
            )
            for spec in self._d.tools.list_specs(everything)
        ]

    async def spend(self) -> dict[str, Any]:
        summary = await self._d.llm_calls.total()
        return {
            "calls": summary.calls,
            "prompt_tokens": summary.prompt_tokens,
            "output_tokens": summary.output_tokens,
            "cost_usd": round(summary.cost_usd, 6),
        }

    async def aclose(self) -> None:
        """Stop carrying work, without leaving a run half-written.

        The interface shutting down is not the work being abandoned: every live
        run is asked to stop the cooperative way first, so it writes its own
        terminal state and `resume` can pick it up.
        """
        await self._d.runs.aclose()
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)

    def carrying(self) -> int:
        """Runs in flight here: what a restart would stop, said before it does."""
        return self._d.runs.carrying

    def health(self) -> dict[str, Any]:
        """Enough for a shell to know the runtime it started is up.

        Deliberately touches no storage: a desktop application polling this
        while the engine boots must not open a database connection per poll,
        and "is the process answering" is the question being asked.
        """
        return {
            "status": "ok",
            "workspace": str(DEFAULT_WORKSPACE_ID),
            "sources": [source.value for source in RequestSource],
        }


def _approval_choice(value: str) -> ApprovalChoice:
    try:
        return ApprovalChoice((value or "ASK").strip().upper())
    except ValueError as error:
        raise PrometheusError(
            f"'{value}' is not an approvals choice: ASK, AUTO or DENY."
        ) from error


def _recurrence(
    every_minutes: int | None, daily_at: str, utc_offset_minutes: int, on_event: str
) -> Recurrence | None:
    chosen = [every_minutes is not None, bool(daily_at.strip()), bool(on_event.strip())]
    if sum(chosen) != 1:
        raise PrometheusError(
            "Choose exactly one: every so many minutes, a time of day, or an event."
        )
    if on_event.strip():
        return None
    try:
        if every_minutes is not None:
            if every_minutes * 60 < MIN_INTERVAL_SECONDS:
                raise PrometheusError("The shortest interval is one minute.")
            return Recurrence(every_seconds=every_minutes * 60)
        local = time.fromisoformat(daily_at.strip())
    except ValueError as error:
        raise PrometheusError(f"'{daily_at}' is not a time of day (HH:MM).") from error
    minutes = (local.hour * 60 + local.minute - utc_offset_minutes) % (24 * 60)
    return Recurrence(daily_at=time(minutes // 60, minutes % 60))


def _settings_view(settings) -> dict[str, Any]:
    return {
        "available": True,
        "restart_needed": any(item.restart_needed for item in settings),
        "any_saved": any(item.saved for item in settings),
        "settings": [views.setting(item) for item in settings],
    }


def _plugin_of(item: Integration) -> str:
    """The plugin an integration was installed from, if it was installed from one."""
    return str(item.configuration.get(PLUGIN_KEY, "") or "")


def _filled(value: object, env: dict) -> object:
    """A probe argument with `${KEY}` taken from the installation's plain settings."""
    if not isinstance(value, str):
        return value
    return re.sub(
        r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}",
        lambda match: str(env.get(match.group(1), "")),
        value,
    )


def _holders(item: Integration, definitions) -> tuple[str, ...]:
    """Everyone who may use it, whichever way they were granted it."""
    from domain.integrations.grants import holds

    return tuple(sorted(d.name for d in definitions if holds(d, item)))


def _capability(value: str) -> Capability:
    """A capability name from an interface, or a readable refusal.

    The vocabulary is closed on purpose (ADR 0015): a routing term any
    integration could invent is a term no employee declaration can be checked
    against. So a name outside it is an error with the list in it, rather than
    a word that is quietly dropped and a search that never matches.
    """
    try:
        return Capability(value)
    except ValueError as error:
        known = ", ".join(sorted(str(c) for c in Capability))
        raise PrometheusError(
            f"'{value}' is not a capability this platform knows. Known: {known}."
        ) from error


def _effect(value: str) -> Effect:
    """An effect name from an interface, or a readable refusal.

    Deliberately the only vocabulary this route accepts. A risk level or an
    "approval required" flag sent from a window would be an interface setting
    its own policy; the effect is the one thing a person actually knows - what
    the tool does to the world - and the risk follows from it (ADR 0010).
    """
    try:
        return Effect(value)
    except ValueError as error:
        known = ", ".join(effect.value for effect in Effect)
        raise PrometheusError(
            f"'{value}' is not an effect. A capability does one of: {known}. "
            "Risk is not set here; it follows from the effect."
        ) from error


def _task_kind(value: str) -> TaskKind:
    """A kind of work named by an interface, or a readable refusal.

    Same rule as `_capability` and the same reason: sending work to a kind the
    router has never heard of would store a row nothing reads, and a settings
    page that appears to have saved something is worse than one that says no.
    """
    try:
        return TaskKind(value.strip().upper())
    except ValueError as error:
        known = ", ".join(sorted(kind.value for kind in TaskKind))
        raise PrometheusError(f"Unknown kind of work '{value}'. Known: {known}.") from error
