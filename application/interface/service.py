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

from collections.abc import AsyncIterator
from dataclasses import dataclass
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
from domain.conversations.models import Conversation
from domain.conversations.repository import ConversationRepository
from domain.employees.protocols import EmployeeRegistry
from domain.errors import ConfigurationError, IntegrationNotFoundError, PrometheusError
from domain.integrations.models import IntegrationKind
from domain.knowledge.models import KnowledgeQuery
from domain.knowledge.protocols import Retriever
from domain.llm.catalog import ModelEntry
from domain.llm.models import TaskKind
from domain.llm.telemetry import LLMCallLog
from domain.memory.models import MemoryQuery, MemoryScope
from domain.memory.protocols import Memory
from domain.policies.models import ActorKind, SimpleActor
from domain.policies.risk import Effect
from domain.secrets.protocols import CredentialStore
from domain.tasks.repository import TaskRepository
from domain.tools.protocols import ToolRegistry
from domain.tools.telemetry import ToolCallLog
from domain.workforce.repository import ObjectiveRepository, PlanRepository
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId

log = structlog.get_logger(__name__)

#: How much history a listing returns when the caller does not say. An
#: interface showing a person their own machine wants a page of it, not all of
#: it; anything wanting all of it is a report, and reports read the store.
DEFAULT_LIMIT = 50


class ApprovalsDisabledError(PrometheusError):
    """Asked to decide something on a configuration with no approvals at all."""


class IntegrationsDisabledError(PrometheusError):
    """Asked about connected services on a machine where they are switched off."""


class WorkspacesDisabledError(PrometheusError):
    """Asked to switch context on an interface built without workspaces."""


class KnowledgeDisabledError(PrometheusError):
    """Asked about documents on a machine where knowledge is switched off."""


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
    #: Read-only. The facade shows what is remembered and cannot forget it:
    #: `MemoryMaintenance` is a separate contract for exactly that reason, and
    #: an interface that held both would make "show me" one click from "delete".
    memory: Memory | None = None
    #: What this machine can do at all. Read-only here: the registry is the
    #: authority on which tools exist, and who may call one is the employee's
    #: own declaration - neither is an interface's to change.
    tools: ToolRegistry | None = None
    history_limit: int = DEFAULT_LIMIT


class PrometheusService:
    """The application-level operations an interface is allowed to perform."""

    def __init__(self, dependencies: ServiceDependencies) -> None:
        self._d = dependencies

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
        return {
            **views.conversation(
                conversation,
                messages=len(thread),
                status=thread[-1].status.value if thread else None,
            ),
            "messages": [
                views.message(item, thinking=self._d.runs.is_thinking(item.id))
                for item in thread
            ],
        }

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

    async def list_memory(self, *, search: str = "", limit: int = 20) -> list[dict[str, Any]]:
        """What this workspace remembers, through the one contract memory has.

        The same scopes a run reads - this workspace's, and the person's own -
        and deliberately not an employee's private notes: the facade has no more
        access to the store than a running task does (ADR 0009).
        """
        if self._d.memory is None:
            return []
        items = await self._d.memory.recall(
            MemoryQuery(
                text=search,
                workspace_id=await self._here(),
                scopes=frozenset({MemoryScope.WORKSPACE, MemoryScope.USER}),
                limit=limit,
            )
        )
        return [views.memory_item(item) for item in items]

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
        context_tokens: int = 8_192,
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
        return views.model_entry(await self._providers().add_model(entry, await self._here()))

    async def remove_model(self, name: str) -> None:
        await self._providers().remove_model(name, await self._here())

    async def list_task_defaults(self) -> dict[str, str]:
        """Which model each kind of work goes to."""
        defaults = await self._providers().defaults(await self._here())
        return {kind.value: name for kind, name in defaults.items()}

    async def send_work_to(self, task_kind: str, entry_name: str) -> dict[str, str]:
        await self._providers().send_work_to(
            _task_kind(task_kind), entry_name, await self._here()
        )
        return await self.list_task_defaults()

    async def clear_task_default(self, task_kind: str) -> dict[str, str]:
        await self._providers().clear_default(_task_kind(task_kind))
        return await self.list_task_defaults()

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
