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
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog

from application.conversations.session import SessionMemory
from application.integrations.service import IntegrationService
from application.interface import artifacts, views
from application.interface.activity import Activity, ActivityEvent
from application.interface.contracts import RequestSource, UserRequest
from application.interface.runs import Runs
from application.knowledge.service import KnowledgeService
from application.memory.revision import MemoryReviser
from application.observability.service import ObservabilityService
from application.providers.service import ProviderService
from application.workflows.engine import WorkflowEngine
from application.workflows.suggestions import WorkflowSuggestions
from application.workforce.profiles import WorkforceProfiles
from application.workspaces import folders
from application.workspaces.service import WorkspaceService
from domain.approvals.models import (
    Approval,
    ApprovalGrant,
    ApprovalState,
    CapabilityLease,
)
from domain.approvals.protocols import (
    ApprovalRepository,
    ApprovalService,
    ApprovalWaiter,
    CapabilityLeaseRepository,
)
from domain.capabilities.models import Capability
from domain.configuration.models import SettingValue
from domain.configuration.protocols import SettingsEditor
from domain.conversations.models import Conversation, ConversationKind
from domain.conversations.repository import ConversationRepository
from domain.employees.protocols import EmployeeRegistry
from domain.errors import (
    ConfigurationError,
    IntegrationNotFoundError,
    NotFoundError,
    PrometheusError,
    WorkControlError,
)
from domain.integrations.catalog import (
    PLUGIN_KEY,
    PluginCatalog,
    plan_install,
    suggested_holders,
)
from domain.integrations.models import Integration, IntegrationKind
from domain.integrations.provenance import (
    ArtifactVerifier,
    PluginVerificationError,
    same_published,
)
from domain.knowledge.models import KnowledgeQuery
from domain.knowledge.protocols import Retriever
from domain.llm.catalog import (
    DEFAULT_CONTEXT_TOKENS,
    ModelEntry,
    Privacy,
    default_privacy,
)
from domain.llm.models import TaskKind
from domain.llm.telemetry import LLMCallLog
from domain.memory.models import (
    MemoryBasis,
    MemoryItem,
    MemoryKind,
    MemoryQuery,
    MemoryScope,
    Provenance,
    SourceKind,
)
from domain.memory.protocols import Memory, MemoryMaintenance
from domain.memory.usage import MemoryUseLog
from domain.observability.models import RunKind
from domain.policies.models import ActorKind, SimpleActor
from domain.policies.risk import Effect
from domain.safety.backup import Backups
from domain.safety.effects import EffectGate
from domain.scheduling.models import MIN_INTERVAL_SECONDS, Recurrence, Schedule
from domain.scheduling.protocols import EventLog, ScheduleRepository
from domain.secrets.protocols import CredentialStore
from domain.tasks.repository import TaskRepository
from domain.tasks.task import Task
from domain.tools.protocols import ToolRegistry
from domain.tools.telemetry import ToolCallLog
from domain.workflows.definition import WorkflowDefinition, WorkflowTrigger
from domain.workflows.protocols import WorkflowRegistry
from domain.workflows.run import WorkflowRunRepository
from domain.workforce.directions import ApprovalChoice, Directions
from domain.workforce.protocols import Objective, ObjectiveStatus
from domain.workforce.repository import (
    AssignmentRepository,
    ObjectiveRepository,
    PlanRepository,
)
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId

if TYPE_CHECKING:
    from application.safety.emergency import EmergencyStopControl

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
    leases: CapabilityLeaseRepository | None = None
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
    #: Which memories work was given, and why. None where a surface was built
    #: without it; "why was this used" then has no record to answer from.
    memory_uses: MemoryUseLog | None = None
    #: Checks a note against what it may replace. None writes notes as they
    #: are, which is what happened before Phase 9.
    memory_reviser: MemoryReviser | None = None
    #: Each thread's brief: decisions, open questions, files, compacted stages.
    sessions: SessionMemory | None = None
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
    #: Durable records of individual schedule firings. Kept separate from the
    #: standing instruction so one failed morning is not reduced to a counter.
    events: EventLog | None = None
    workflows: WorkflowEngine | None = None
    workflow_registry: WorkflowRegistry | None = None
    workflow_runs: WorkflowRunRepository | None = None
    #: Whether this process is firing schedules. Said rather than inferred: a
    #: schedule shown as "next at 09:00" on a machine that will not fire it is
    #: the one lie this screen must not tell.
    scheduler_running: bool = False
    history_limit: int = DEFAULT_LIMIT
    #: Who was assigned what, and why. None where a surface was built without
    #: it; a task then shows its one-line reason and no alternatives.
    assignments: AssignmentRepository | None = None
    #: Profiles, readiness and records of the workforce (Phase 11). None where
    #: a surface was built without them - the screen then says so.
    workforce: WorkforceProfiles | None = None
    #: Recurring processes offered as workflow drafts. None where workflows
    #: are off; the screen then says suggestions are unavailable.
    workflow_suggestions: WorkflowSuggestions | None = None
    #: One durable, sanitized explanation of a run. None only for narrowly
    #: constructed tests and older embedding applications.
    observability: ObservabilityService | None = None
    #: The machine-wide brake. None only where a surface was built without one;
    #: every stop operation then refuses rather than pretending to have stopped.
    emergency: EmergencyStopControl | None = None
    #: Making and checking backups of this installation. None where a surface
    #: was built without them, or the store is not one a backup covers.
    backups: Backups | None = None
    #: Effects executing in this process, and the hold an update places on them.
    effects: EffectGate | None = None
    #: Asks a registry what it publishes for a pinned plugin artifact. None
    #: refuses to install any plugin that has one, rather than skipping the check.
    artifact_verifier: ArtifactVerifier | None = None


class PrometheusService:
    """The application-level operations an interface is allowed to perform."""

    def __init__(self, dependencies: ServiceDependencies) -> None:
        self._d = dependencies
        #: Work started by a setting and owned by nobody else - held so it is
        #: not collected mid-way, and awaited when the interface closes.
        self._background: set[asyncio.Task[None]] = set()

    async def recover(self) -> dict[str, int]:
        """Reconcile state owned by the previous process and resume its work.

        A machine that was stopped comes back stopped: the stop is enforced
        before anything is resumed, and `Runs.recover` resumes nothing while it
        holds.
        """
        if self._d.emergency is not None:
            await self._d.emergency.enforce()
        expired = await self._d.approvals.expire_abandoned()
        reconciled = await self._d.runs.reconcile_abandoned_approvals(
            await self._every_workspace()
        )
        objectives = await self._d.runs.recover()
        if expired or objectives:
            log.info(
                "interface.recovered",
                expired_approvals=expired,
                objectives=objectives,
            )
        return {
            "expired_approvals": expired,
            "reconciled_tasks": reconciled,
            "objectives": objectives,
        }

    async def _every_workspace(self) -> list[WorkspaceId]:
        if self._d.workspaces is None:
            return [DEFAULT_WORKSPACE_ID]
        return [item.id for item in await self._d.workspaces.list()] or [DEFAULT_WORKSPACE_ID]

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

    async def create_conversation(
        self,
        title: str = "",
        *,
        workspace_id=None,
        kind: str = ConversationKind.TASK.value,
    ) -> dict[str, Any]:
        """Open a thread, in the workspace this machine is in unless told which."""
        try:
            purpose = ConversationKind(kind)
        except ValueError as error:
            raise PrometheusError(f"Unknown conversation kind: {kind}") from error
        conversation = Conversation.create(
            title, workspace_id=workspace_id or await self._here(), kind=purpose
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
        # Approval mode belongs to a normal interactive thread. Older rows have
        # no value and continue to open on the latest request, as they did
        # before the setting was persisted on conversations.
        if schedule is None and conversation.approvals is not None:
            chosen = (
                replace(chosen, approvals=conversation.approvals)
                if chosen is not None
                else Directions(approvals=conversation.approvals)
            )
        if schedule is None and conversation.model is not None:
            chosen = (
                replace(chosen, model=conversation.model)
                if chosen is not None
                else Directions(model=conversation.model)
            )
        # The folder is the thread's, whatever the last request carried: a
        # person may have pointed it elsewhere since.
        if chosen is not None:
            chosen = replace(chosen, folder=conversation.folder)
        recalled = await self._recalled(thread)
        return {
            **views.conversation(
                conversation,
                messages=len(thread),
                status=thread[-1].status.value if thread else None,
            ),
            "directions": views.directions(chosen) if chosen is not None else None,
            "schedule_id": str(schedule.id) if schedule is not None else None,
            "folder": conversation.folder,
            "messages": [
                views.message(
                    item,
                    thinking=self._d.runs.is_thinking(item.id),
                    artifacts=await self._artifacts(item),
                    memory_used=item.id in recalled,
                )
                for item in thread
            ],
        }

    async def _recalled(self, thread: list[Objective]) -> set[UUID]:
        """Which turns of a thread were given a memory, asked in one question.

        A failed read says none: a thread must open even where the record of
        what was recalled cannot be read, and the worst of that is an
        explanation nobody is offered.
        """
        if self._d.memory_uses is None or not thread:
            return set()
        try:
            return await self._d.memory_uses.used_by([item.id for item in thread])
        except Exception:
            return set()

    async def _artifacts(self, objective: Objective) -> list[dict[str, Any]]:
        """The files an objective's tasks wrote, where they are on this machine now.

        Only a finished turn is read: a running one is still writing, and its
        list is re-read when the answer arrives. A failed read is an empty list -
        a thread must open even if the accounting cannot be read.
        """
        if objective.result is None:
            return []
        return [
            artifacts.artifact(path, self._ran_in(objective))
            for path in await self._produced(objective)
        ]

    def _ran_in(self, objective: Objective) -> Path | None:
        """Where an objective's relative paths lead: the folder it ran in, as recorded."""
        if objective.directions.folder:
            return Path(objective.directions.folder)
        if self._d.workspaces is None:
            return None
        return self._d.workspaces.root_for(objective.workspace_id)

    async def _produced(self, objective: Objective) -> list[str]:
        try:
            calls = []
            for plan in await self._d.plans.for_objective(objective.id):
                for task in plan.tasks:
                    calls.extend(await self._d.tool_calls.list_for_task(task.id))
            return artifacts.produced_files(calls)
        except Exception as error:
            log.warning("interface.artifacts_unreadable", error=str(error))
            return []

    async def artifact_file(self, objective_id: UUID, path: str) -> tuple[Path, str] | None:
        """A file an objective produced, for an interface to show.

        Only a path the work is recorded as having written is served, not any
        path under the root: the window asks to preview what it was shown, and a
        route that read whatever it was handed would be a file browser nobody
        decided to build.
        """
        objective = await self._d.objectives.get(objective_id)
        root = self._ran_in(objective) if objective is not None else None
        if objective is None or root is None:
            return None
        if path not in await self._produced(objective):
            return None
        target = artifacts.within(root, path)
        if target is None or not target.is_file():
            return None
        return target, artifacts.media_type(path) or "application/octet-stream"

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
                await self.cancel_objective(item.id)
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
        folders: list[str] | None = None,
    ) -> dict[str, Any]:
        if self._d.workspaces is None:
            raise WorkspacesDisabledError("This interface has no workspaces behind it.")
        item = await self._d.workspaces.update(
            workspace_id,
            name=name,
            description=description,
            file_root=file_root,
            folders=folders,
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

    async def _readable(
        self, text: str = "", limit: int = 20, *, include_superseded: bool = False
    ) -> MemoryQuery:
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
            include_superseded=include_superseded,
        )

    async def list_memory(
        self, *, search: str = "", limit: int = 20, include_superseded: bool = False
    ) -> list[dict[str, Any]]:
        """What this workspace remembers, through the one contract memory has.

        A search is `recall` with words in it, ranked by the domain like every
        other recall - not a second query written for a screen. Superseded
        memories are shown only when asked for: they are history, kept so a
        correction can be traced, not what the platform currently believes.
        """
        if self._d.memory is None:
            return []
        items = await self._d.memory.recall(
            await self._readable(search, limit, include_superseded=include_superseded)
        )
        return [views.memory_item(item) for item in items]

    async def _shown(self, ids: set[UUID]) -> dict[UUID, MemoryItem]:
        """These memories, as far as a person may see them here - superseded included."""
        if self._d.memory is None or not ids:
            return {}
        query = replace(
            await self._readable(limit=len(ids), include_superseded=True),
            ids=frozenset(ids),
        )
        return {item.id: item for item in await self._d.memory.recall(query)}

    async def memory_detail(self, item_id: UUID) -> dict[str, Any]:
        """One memory traced: its source, what it replaced or disputes, and where it was used."""
        if self._d.memory is None:
            raise MemoryDisabledError("Memory is switched off on this machine.")
        found = await self._shown({item_id})
        item = found.get(item_id)
        if item is None:
            raise NotFoundError(f"Nothing remembered here as {item_id}.")
        related = {
            *(i for i in (item.superseded_by,) if i is not None),
            *item.contradicts,
            *(UUID(ref) for ref in item.provenance.derived_from if _is_uuid(ref)),
        }
        others = await self._shown(related)
        uses = (
            await self._d.memory_uses.for_memory(item_id)
            if self._d.memory_uses is not None
            else []
        )
        return views.memory_trace(
            item,
            superseded_by=others.get(item.superseded_by) if item.superseded_by else None,
            contradicts=[others[i] for i in item.contradicts if i in others],
            derived_from=[
                others[UUID(ref)]
                for ref in item.provenance.derived_from
                if _is_uuid(ref) and UUID(ref) in others
            ],
            uses=uses,
        )

    async def remember(self, content: str, *, about_the_person: bool) -> dict[str, Any]:
        """Keep something a person told the platform directly.

        SEMANTIC and without an expiry, like a preference read out of a request:
        it is a statement of how things are, and what supersedes it is the
        person removing or correcting it, or a later statement that replaces
        it. `about_the_person` is the one question worth asking - is this true
        of you everywhere, or of this workspace - because it is the difference
        between USER and WORKSPACE, and every other field has one right answer.
        """
        if self._d.memory is None:
            raise MemoryDisabledError("Memory is switched off on this machine.")
        stated = _note(content)
        here = await self._here()
        item = MemoryItem.create(
            stated,
            scope=MemoryScope.USER if about_the_person else MemoryScope.WORKSPACE,
            kind=MemoryKind.SEMANTIC,
            workspace_id=here,
            importance=STATED_IMPORTANCE,
            metadata={"source": views.STATED_BY_PERSON, "stated_in": str(here)},
            basis=MemoryBasis.STATED,
            confidence=1.0,
            provenance=Provenance(kind=SourceKind.PERSON, label="added by the user"),
        )
        if self._d.memory_reviser is not None:
            item = await self._d.memory_reviser.remember(item)
        else:
            await self._d.memory.remember(item)
        return views.memory_item(item)

    async def correct_memory(self, item_id: UUID, content: str) -> dict[str, Any]:
        """Replace what a memory says. The old one is superseded, not erased."""
        if self._d.memory is None:
            raise MemoryDisabledError("Memory is switched off on this machine.")
        stated = _note(content)
        old = (await self._shown({item_id})).get(item_id)
        if old is None or not old.is_active:
            raise NotFoundError(f"Nothing current is remembered here as {item_id}.")
        new, old = MemoryReviser.correction(old, stated)
        await self._d.memory.remember(new)
        await self._d.memory.remember(old)
        return views.memory_item(new)

    async def set_memory_retention(self, item_id: UUID, days: int | None) -> dict[str, Any]:
        """How long a memory is kept: `None` for until it is replaced, or a number of days."""
        if self._d.memory is None:
            raise MemoryDisabledError("Memory is switched off on this machine.")
        if days is not None and not 1 <= days <= 3650:
            raise ValueError("Keep a memory for between 1 and 3650 days, or until replaced.")
        item = (await self._shown({item_id})).get(item_id)
        if item is None:
            raise NotFoundError(f"Nothing remembered here as {item_id}.")
        updated = replace(
            item,
            expires_at=datetime.now(UTC) + timedelta(days=days) if days is not None else None,
        )
        await self._d.memory.remember(updated)
        return views.memory_item(updated)

    async def forget_memory(self, item_id: UUID) -> bool:
        """Forget one thing a person can see. False where there is no such thing *here*.

        Bounded by the same scopes `list_memory` reads with, so an id copied
        from another workspace, or belonging to an employee's private notes, is
        not found rather than deleted. Superseded memories can be forgotten too:
        keeping a correction's history is a default, not an obligation.
        """
        if self._d.memory is None or self._d.memory_maintenance is None:
            raise MemoryDisabledError("Forgetting is not available on this machine.")
        forgotten = await self._d.memory_maintenance.forget(
            [item_id], within=await self._readable(include_superseded=True)
        )
        return forgotten > 0

    async def memory_used_by(self, objective_id: UUID) -> dict[str, Any]:
        """Which memories one answer was given, by whom, and why each was chosen."""
        objective = await self._d.objectives.get(objective_id)
        if objective is None:
            raise NotFoundError(f"Unknown objective: {objective_id}")
        uses = []
        plans = await self._d.plans.for_objective(objective_id)
        if self._d.memory_uses is not None:
            uses.extend(await self._d.memory_uses.for_objective(objective_id))
            for plan in plans:
                for task in plan.tasks:
                    uses.extend(await self._d.memory_uses.for_task(task.id))
        ids = {use.memory_id for use in uses}
        shown = await self._shown(ids)
        if self._d.memory is not None and ids - set(shown):
            # Plan memory belongs to this objective's own work, so it is shown
            # here - read plan by plan, because a query names one plan or none.
            for plan in plans:
                query = MemoryQuery(
                    workspace_id=objective.workspace_id,
                    scopes=frozenset({MemoryScope.PLAN}),
                    plan_id=plan.id,
                    ids=frozenset(ids - set(shown)),
                    include_superseded=True,
                    limit=len(ids),
                )
                shown.update({item.id: item for item in await self._d.memory.recall(query)})
        return {
            "objective_id": str(objective_id),
            "recorded": self._d.memory_uses is not None,
            "uses": [views.memory_use(use, shown.get(use.memory_id)) for use in uses],
        }

    # --- Sessions -------------------------------------------------------------

    async def session_brief(self, conversation_id: UUID) -> dict[str, Any]:
        """What a thread has established: goal, decisions, open questions, files, stages."""
        if self._d.sessions is None:
            raise NotFoundError("This interface keeps no thread briefs.")
        if await self._d.conversations.get(conversation_id) is None:
            raise NotFoundError(f"Unknown conversation: {conversation_id}")
        return views.session_brief(await self._d.sessions.brief(conversation_id))

    async def resolve_session_question(
        self, conversation_id: UUID, question: str
    ) -> dict[str, Any]:
        if self._d.sessions is None:
            raise NotFoundError("This interface keeps no thread briefs.")
        if await self._d.conversations.get(conversation_id) is None:
            raise NotFoundError(f"Unknown conversation: {conversation_id}")
        if not question.strip():
            raise ValueError("Name the question that is settled.")
        return views.session_brief(
            await self._d.sessions.resolve(conversation_id, question.strip())
        )

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

    def indexing_documents(self) -> list[dict[str, Any]]:
        """What is being read or embedded at this moment.

        Not awaited and not stored: indexing happens inside the request that
        asked for it, so the surface waiting on that request has to learn this
        from a second one. State rather than a stream, because a person who
        opened the screen halfway through would have missed every event.
        """
        if self._d.knowledge is None:
            return []
        return [views.indexing(one) for one in self._d.knowledge.progress()]

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
        directions = request.directions
        workspace_id = request.workspace_id
        if conversation is not None:
            # A thread never crosses workspaces. The selector disappears after
            # creation, and the boundary enforces the same rule for stale or
            # hand-written clients instead of trusting presentation alone.
            workspace_id = conversation.workspace_id
            conversation = await self._folder_for(conversation, directions.folder)
            directions = replace(directions, folder=conversation.folder)
        elif directions.folder:
            directions = replace(directions, folder=str(folders.chosen_folder(directions.folder)))
        objective = await self._d.runs.ask(
            text,
            conversation_id=conversation.id if conversation else None,
            workspace_id=workspace_id,
            directions=directions,
        )
        log.info(
            "interface.request_submitted",
            objective_id=str(objective.id),
            source=request.source.value,
            workspace_id=str(workspace_id),
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
        updated = (
            conversation.titled_from(request.text)
            .touched(request.received_at)
            .with_approvals(request.directions.approvals)
            .with_model(request.directions.model)
        )
        await self._d.conversations.save(updated)
        return updated

    async def _folder_for(self, conversation: Conversation, chosen: str = "") -> Conversation:
        """The folder this thread works in, given one if it has none yet.

        A folder chosen with the request replaces the thread's. Otherwise the
        thread keeps the one it has, and a thread with none gets its own under
        the workspace's root. Without workspaces there is no root to put one
        under, and the thread works where the machine's tools already point.
        """
        if chosen.strip():
            folder = str(folders.chosen_folder(chosen))
        elif conversation.folder:
            return conversation
        elif self._d.workspaces is not None:
            root = self._d.workspaces.root_for(conversation.workspace_id)
            folder = str(folders.session_folder(root, conversation))
        else:
            return conversation
        if folder == conversation.folder:
            return conversation
        updated = conversation.in_folder(folder)
        await self._d.conversations.save(updated)
        log.info("interface.thread_folder", conversation_id=str(conversation.id), folder=folder)
        return updated

    async def set_conversation_folder(
        self, conversation_id: UUID, folder: str
    ) -> dict[str, Any] | None:
        """Point a thread at another folder, for the requests that follow.

        What was already written stays where it was; each earlier answer
        records the folder it ran in, so its files are still found. An empty
        folder gives the thread its own again.
        """
        conversation = await self._d.conversations.get(conversation_id)
        if conversation is None:
            return None
        if not folder.strip():
            conversation = conversation.in_folder("")
            await self._d.conversations.save(conversation)
        await self._folder_for(conversation, folder)
        return await self.get_conversation(conversation_id)

    async def set_conversation_approvals(
        self, conversation_id: UUID, approvals: ApprovalChoice
    ) -> dict[str, Any] | None:
        """Set how future requests in this thread handle approval gates."""
        conversation = await self._d.conversations.get(conversation_id)
        if conversation is None:
            return None
        await self._d.conversations.save(conversation.with_approvals(approvals))
        return await self.get_conversation(conversation_id)

    async def set_conversation_model(
        self, conversation_id: UUID, model: str
    ) -> dict[str, Any] | None:
        """Set the model future requests in this thread prefer."""
        conversation = await self._d.conversations.get(conversation_id)
        if conversation is None:
            return None
        await self._d.conversations.save(conversation.with_model(model))
        return await self.get_conversation(conversation_id)

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

    async def list_work_items(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Objective-centric work state for people supervising digital employees."""
        objectives = await self._d.objectives.list_recent(
            await self._here(), limit=limit or self._d.history_limit
        )
        recent = await self._d.tasks.list_recent(await self._here(), limit=200)
        return [await self._work_item(item, recent) for item in objectives]

    async def get_work_item(self, objective_id: UUID) -> dict[str, Any] | None:
        objective = await self._d.objectives.get(objective_id)
        if objective is None:
            return None
        recent = await self._d.tasks.list_recent(objective.workspace_id, limit=200)
        return await self._work_item(objective, recent)

    async def _work_item(
        self, objective: Objective, recent: list[Task]
    ) -> dict[str, Any]:
        plans = await self._d.plans.for_objective(objective.id)
        plan_ids = {plan.id for plan in plans}
        planned_ids = {task.id for plan in plans for task in plan.tasks}
        candidates = {
            task.id: task
            for task in recent
            if task.id in planned_ids or (task.plan_id is not None and task.plan_id in plan_ids)
        }
        for task_id in planned_ids - candidates.keys():
            stored = await self._d.tasks.get(task_id)
            if stored is not None:
                candidates[task_id] = stored

        employees = {
            definition.id: definition
            for definition in self._d.employees.list(objective.workspace_id)
        }
        dependencies = {
            task.id: tuple(plan.depends_on(task.id))
            for plan in plans
            for task in plan.tasks
        }
        task_views = []
        for task in sorted(candidates.values(), key=lambda item: (item.created_at, str(item.id))):
            model_calls = (
                await self._d.llm_calls.list_for_task(task.id)
                if hasattr(self._d.llm_calls, "list_for_task")
                else []
            )
            made = (
                await self._d.assignments.for_task(task.id)
                if self._d.assignments is not None
                else []
            )
            task_views.append(
                views.work_task(
                    task,
                    assignment=made[0] if made else None,
                    employee=employees.get(task.assigned_employee_id),
                    calls=await self._d.tool_calls.list_for_task(task.id),
                    model_calls=model_calls,
                    depends_on=dependencies.get(task.id, ()),
                    objective_terminal=objective.is_terminal,
                )
            )
        return views.work_item(
            objective,
            plans=plans,
            tasks=task_views,
            artifacts=await self._artifacts(objective),
            thinking=self._d.runs.is_thinking(objective.id),
        )

    async def pause_objective(self, objective_id: UUID) -> dict[str, Any] | None:
        objective = await self._d.objectives.get(objective_id)
        if objective is None:
            return None
        if objective.is_terminal:
            return await self.get_work_item(objective_id)
        for task in await self._objective_tasks(objective_id):
            await self._d.runs.pause(task.id)
        await self._d.objectives.save(objective.to(ObjectiveStatus.PAUSED))
        return await self.get_work_item(objective_id)

    async def resume_objective(self, objective_id: UUID) -> dict[str, Any] | None:
        objective = await self._d.objectives.get(objective_id)
        if objective is None:
            return None
        if objective.status is not ObjectiveStatus.PAUSED:
            return await self.get_work_item(objective_id)
        for task in await self._objective_tasks(objective_id):
            await self._d.runs.resume(task.id)
        resumed = objective.to(ObjectiveStatus.RUNNING)
        await self._d.objectives.save(resumed)
        self._d.runs.resume_objective(resumed)
        return await self.get_work_item(objective_id)

    async def retry_objective(self, objective_id: UUID) -> dict[str, Any] | None:
        objective = await self._d.objectives.get(objective_id)
        if objective is None:
            return None
        if not objective.is_terminal:
            raise PrometheusError("Only finished, failed, escalated or cancelled work can retry.")
        retried = await self._d.runs.ask(
            objective.text,
            conversation_id=objective.conversation_id,
            workspace_id=objective.workspace_id,
            directions=objective.directions,
        )
        return await self.get_work_item(retried.id)

    async def handoff_task(self, task_id: UUID, employee: str) -> dict[str, Any] | None:
        task = await self._d.tasks.get(task_id)
        if task is None:
            return None
        await self._ensure_task_control_is_safe(task)
        handed = await self._d.runs.handoff(task, employee)
        return views.task_summary(handed, running=True)

    async def retry_task(self, task_id: UUID) -> dict[str, Any] | None:
        task = await self._d.tasks.get(task_id)
        if task is None:
            return None
        await self._ensure_task_control_is_safe(task)
        if task.assigned_employee_id is None:
            raise PrometheusError("This task has no employee to retry it with.")
        employee = next(
            (
                item.name
                for item in self._d.employees.list(task.workspace_id)
                if item.id == task.assigned_employee_id
            ),
            None,
        )
        if employee is None:
            raise PrometheusError("The employee assigned to this task no longer exists.")
        handed = await self._d.runs.handoff(task, employee)
        return views.task_summary(handed, running=True)

    async def _ensure_task_control_is_safe(self, task: Task) -> None:
        if task.plan_id is None:
            return
        plan = await self._d.plans.get(task.plan_id)
        objective = await self._d.objectives.get(plan.objective_id) if plan else None
        if objective is not None and not objective.is_terminal:
            raise WorkControlError(
                "Prometheus is still supervising this plan. Cancel the objective before "
                "retrying or handing off one of its steps."
            )

    async def _objective_tasks(self, objective_id: UUID) -> list[Task]:
        found: dict[UUID, Task] = {}
        objective = await self._d.objectives.get(objective_id)
        workspace_id = objective.workspace_id if objective is not None else await self._here()
        plans = await self._d.plans.for_objective(objective_id)
        plan_ids = {plan.id for plan in plans}
        for plan in plans:
            for planned in plan.tasks:
                found[planned.id] = await self._d.tasks.get(planned.id) or planned
        for task in await self._d.tasks.list_recent(workspace_id, limit=200):
            if task.plan_id in plan_ids:
                found[task.id] = task
        return list(found.values())

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
        for task in await self._objective_tasks(objective_id):
            await self._d.runs.signal_cancel(task.id, "The objective was stopped.")
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
        workspace_id = await self._active_workspace_id()
        stored = await self._d.approvals.list_pending(workspace_id)
        # The row is saved immediately before the waiter is installed. Read in
        # that order too, then snapshot live state, so the narrow transition
        # cannot make an actually parked question look orphaned.
        live = {item.id: item for item in self._d.waiter.pending()}
        names = {str(item.id): item.name for item in self._d.employees.list()}
        threads: dict[UUID, UUID | None] = {}

        async def thread_of(task_id: UUID) -> UUID | None:
            if task_id not in threads:
                threads[task_id] = await self._conversation_of(task_id)
            return threads[task_id]

        return [
            views.approval(
                item,
                live=True,
                conversation_id=await thread_of(item.task_id),
                subject_name=names.get(item.scope.subject, "") if item.scope else "",
            )
            for item in live.values()
        ] + [
            views.stored_approval(
                record,
                conversation_id=await thread_of(record.request.task_id),
                subject_name=(
                    names.get(record.request.scope.subject, "")
                    if record.request.scope
                    else ""
                ),
            )
            for record in stored
            if record.id not in live
        ]

    async def list_approval_inbox(self) -> dict[str, Any]:
        """Every current decision plus recent final outcomes for this workspace."""
        workspace_id = await self._active_workspace_id()
        records = await self._d.approvals.list_recent(workspace_id, limit=100)
        live = {
            item.id: item
            for item in self._d.waiter.pending()
            if item.workspace_id == workspace_id
        }
        recorded = {record.id for record in records}
        # The waiter and durable repository are updated one after the other.
        # Include the narrow in-between state so a live decision never vanishes
        # from the global inbox merely because its insert is still completing.
        records = [
            *(Approval(request=request) for key, request in live.items() if key not in recorded),
            *records,
        ]
        names = {
            str(item.id): item.name for item in self._d.employees.list(workspace_id)
        }
        projected: list[dict[str, Any]] = []
        for record in records:
            task, objective = await self._approval_context(record.request.task_id)
            is_live = record.id in live
            ended = (task is not None and task.is_terminal) or (
                objective is not None and objective.is_terminal
            )
            if record.is_pending and not is_live:
                comment = (
                    "The related work ended before this decision was answered."
                    if ended
                    else "No active action was waiting for this decision."
                )
                record = record.resolve(
                    ApprovalState.EXPIRED,
                    resolved_by="work-ended" if ended else "stale",
                    comment=comment,
                )
                await self._d.approvals.save(record)
            projected.append(
                views.inbox_approval(
                    record,
                    live=is_live,
                    task=task,
                    objective_id=objective.id if objective else None,
                    conversation_id=objective.conversation_id if objective else None,
                    subject_name=(
                        names.get(record.request.scope.subject, "")
                        if record.request.scope
                        else ""
                    ),
                )
            )

        risk_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
        pending = [item for item in projected if item["state"] == "PENDING"]
        pending.sort(
            key=lambda item: (
                risk_order.get(str(item["risk"]), 4),
                str(item["requested_at"]),
            )
        )
        recent = [item for item in projected if item["state"] != "PENDING"][:30]
        return {
            "pending": pending,
            "recent": recent,
            "counts": {
                "total": len(pending),
                "actionable": sum(bool(item["actionable"]) for item in pending),
                "critical": sum(item["risk"] == "CRITICAL" for item in pending),
                "long_wait": sum(item["wait_group"] == "LONG_WAIT" for item in pending),
            },
        }

    async def _approval_context(
        self, task_id: UUID
    ) -> tuple[Task | None, Objective | None]:
        objective = await self._d.objectives.get(task_id)
        if objective is not None:
            return None, objective
        task = await self._d.tasks.get(task_id)
        if task is None or task.plan_id is None:
            return task, None
        plan = await self._d.plans.get(task.plan_id)
        return task, await self._d.objectives.get(plan.objective_id) if plan else None

    async def _conversation_of(self, task_id: UUID) -> UUID | None:
        """The thread a task's work was asked in: task -> plan -> objective.

        The id may also be the objective's own, when the manager asked rather
        than an employee. Anything that does not lead to a thread is None, and a
        failed read is None too - a question must still reach the person.
        """
        try:
            objective = await self._d.objectives.get(task_id)
            if objective is None:
                task = await self._d.tasks.get(task_id)
                if task is None or task.plan_id is None:
                    return None
                plan = await self._d.plans.get(task.plan_id)
                if plan is None:
                    return None
                objective = await self._d.objectives.get(plan.objective_id)
            return objective.conversation_id if objective else None
        except Exception:
            return None

    async def decide_approval(
        self,
        approval_id: UUID,
        *,
        approved: bool,
        comment: str = "",
        grant: ApprovalGrant | str = ApprovalGrant.ONCE,
        duration_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Answer a question. The interface carries the answer and nothing else.

        Whether the action needed asking was decided by the policy engine before
        this was ever shown to anybody; whether it now happens is decided by the
        person. Nothing in the interface layer gets a vote, which is why there
        is no path here that resolves an approval on its own.
        """
        grant = ApprovalGrant(grant)
        record = await self._d.approvals.get(approval_id)
        if record is None:
            live_request = next(
                (item for item in self._d.waiter.pending() if item.id == approval_id),
                None,
            )
            if live_request is None:
                raise ApprovalsDisabledError(f"Unknown approval: {approval_id}")
            record = Approval(request=live_request)
            await self._d.approvals.save(record)
        if not record.is_pending:
            return {
                "id": str(approval_id),
                "state": record.state.value,
                "live": False,
                "grant": record.grant.value,
                "lease_id": str(record.lease_id) if record.lease_id else None,
            }

        live_request = next(
            (item for item in self._d.waiter.pending() if item.id == approval_id),
            None,
        )
        if live_request is None:
            expired = record.resolve(
                ApprovalState.EXPIRED,
                resolved_by="stale",
                comment="No active action was waiting for this decision.",
            )
            await self._d.approvals.save(expired)
            return {
                "id": str(approval_id),
                "state": ApprovalState.EXPIRED.value,
                "live": False,
                "grant": ApprovalGrant.ONCE.value,
                "lease_id": None,
            }

        lease: CapabilityLease | None = None
        if approved and grant is not ApprovalGrant.ONCE:
            if self._d.leases is None or record.request.scope is None:
                raise ApprovalsDisabledError(
                    "This approval cannot create a scoped permission."
                )
            lease = CapabilityLease.create(
                workspace_id=record.request.workspace_id,
                scope=record.request.scope,
                grant=grant,
                reason=comment or record.request.reason,
                approval_id=approval_id,
                task_id=(record.request.task_id if grant is ApprovalGrant.TASK else None),
                duration_seconds=duration_seconds,
            )
            await self._d.leases.save(lease)
            record = replace(record, grant=grant, lease_id=lease.id)
            await self._d.approvals.save(record)

        state = ApprovalState.APPROVED if approved else ApprovalState.REJECTED
        answered = self._d.waiter.decide(approval_id, approved)
        if not answered:
            # The waiter disappeared between the live check and this click.
            # It is unsafe to record APPROVED when no action can receive it.
            if lease is not None and self._d.leases is not None:
                await self._d.leases.revoke(lease.id, by="stale")
                record = replace(
                    record, grant=ApprovalGrant.ONCE, lease_id=None
                )
            expired = record.resolve(
                ApprovalState.EXPIRED,
                resolved_by="stale",
                comment="The waiting action ended before the decision arrived.",
            )
            await self._d.approvals.save(expired)
            return {
                "id": str(approval_id),
                "state": ApprovalState.EXPIRED.value,
                "live": False,
                "grant": ApprovalGrant.ONCE.value,
                "lease_id": None,
            }
        return {
            "id": str(approval_id),
            "state": state.value,
            "live": answered,
            "grant": grant.value if approved else ApprovalGrant.ONCE.value,
            "lease_id": str(lease.id) if lease else None,
        }

    async def list_capability_leases(self) -> list[dict[str, Any]]:
        if self._d.leases is None:
            return []
        workspace_id = await self._active_workspace_id()
        names = {str(item.id): item.name for item in self._d.employees.list()}
        return [
            views.capability_lease(
                item, subject_name=names.get(item.scope.subject, "")
            )
            for item in await self._d.leases.list_active(workspace_id)
        ]

    async def revoke_capability_lease(self, lease_id: UUID) -> bool:
        if self._d.leases is None:
            return False
        return await self._d.leases.revoke(lease_id)

    async def _active_workspace_id(self) -> WorkspaceId:
        if self._d.workspaces is None:
            return DEFAULT_WORKSPACE_ID
        return (await self._d.workspaces.active()).id


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
        privacy: str = "",
        latency_ms: int = 0,
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
        clean_provider = provider.strip()
        try:
            entry = ModelEntry(
                name=name.strip(),
                provider=clean_provider,
                model=model.strip(),
                connection=connection.strip(),
                capabilities=frozenset(_capability(item) for item in capabilities),
                context_tokens=context_tokens,
                input_cost_per_1k_usd=input_cost_per_1k_usd,
                output_cost_per_1k_usd=output_cost_per_1k_usd,
                quality=quality,
                dimensions=dimensions,
                privacy=(
                    Privacy(privacy.strip().upper())
                    if privacy.strip()
                    else default_privacy(clean_provider)
                ),
                latency_ms=latency_ms,
            )
        except ValueError as error:
            raise PrometheusError(str(error)) from error
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
        await self._verify_artifact(plugin)
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

    async def _verify_artifact(self, plugin) -> None:
        """Before anything is stored or started: the registry still publishes what was locked."""
        if plugin.artifact is None:
            return
        if self._d.artifact_verifier is None:
            raise PluginVerificationError(
                f"{plugin.name} runs {plugin.artifact.reference}, and nothing here can verify "
                "it. It was not installed."
            )
        published = await self._d.artifact_verifier.published(plugin.artifact)
        if not same_published(plugin.artifact, published):
            log.warning("plugins.artifact_mismatch", plugin=plugin.id)
            raise PluginVerificationError(
                f"The registry no longer publishes the reviewed {plugin.artifact.reference}: "
                "its contents differ from the catalog lock. It was not installed."
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

    async def list_workflows(self) -> dict[str, Any]:
        """Versioned processes, their readiness and comparable run history."""
        if self._d.workflow_registry is None:
            return {"available": False, "workflows": []}
        workspace = await self._here()
        runs = (
            await self._d.workflow_runs.recent(workspace, limit=200)
            if self._d.workflow_runs is not None
            else []
        )
        listed = []
        for definition in self._d.workflow_registry.list_all():
            matching = [
                run
                for run in runs
                if run.workflow == definition.name
                and run.workflow_version == definition.version
            ]
            readiness = await self._workflow_readiness(definition, workspace)
            listed.append(
                views.workflow_definition(
                    definition,
                    readiness=readiness,
                    runs=matching,
                )
            )
        return {"available": True, "workflows": listed}

    async def dry_run_workflow(
        self, name: str, *, version: int | None = None, inputs: dict[str, object] | None = None
    ) -> dict[str, Any]:
        engine, registry = self._workflow_components()
        definition = registry.get(name, version)
        readiness = await self._workflow_readiness(definition, await self._here())
        preview = engine.dry_run(name, version=definition.version, inputs=inputs)
        return {**preview, "readiness": readiness, "executable": readiness["ready"]}

    async def run_workflow_now(
        self, name: str, *, version: int | None = None, inputs: dict[str, object] | None = None
    ) -> dict[str, Any]:
        engine, registry = self._workflow_components()
        definition = registry.get(name, version)
        workspace = await self._here()
        readiness = await self._workflow_readiness(definition, workspace)
        if not readiness["ready"]:
            raise PrometheusError("Workflow is not ready: " + "; ".join(readiness["issues"]))
        run = await engine.run(
            name,
            version=definition.version,
            inputs=inputs,
            trigger=WorkflowTrigger.MANUAL,
            workspace_id=workspace,
        )
        return views.workflow_run(run)

    def _workflow_components(self) -> tuple[WorkflowEngine, WorkflowRegistry]:
        if self._d.workflows is None or self._d.workflow_registry is None:
            raise ConfigurationError("This interface was built without workflows.")
        return self._d.workflows, self._d.workflow_registry

    async def _workflow_readiness(
        self, definition: WorkflowDefinition, workspace: WorkspaceId
    ) -> dict[str, Any]:
        employees = {item.name for item in self._d.employees.list(workspace)}
        missing = sorted(definition.employees - employees)
        issues = [f"Employee '{name}' is not declared." for name in missing]
        model = definition.profile.model
        if model and self._d.providers is not None:
            models = {
                item.name: item for item in await self._d.providers.list_models(workspace)
            }
            if model not in models:
                issues.append(f"Model '{model}' is not in the catalog.")
            elif not models[model].generates_text:
                issues.append(f"Model '{model}' cannot generate text.")
        attempts = sum(max(1, step.max_attempts) for step in definition.steps)
        maximum = definition.budget.max_steps
        if maximum is not None and attempts > maximum:
            issues.append(
                f"Declared retries may use {attempts} steps, above the budget of {maximum}."
            )
        return {"ready": not issues, "issues": issues}

    def _schedules(self) -> ScheduleRepository:
        if self._d.schedules is None:
            raise ConfigurationError("This interface was built without schedules.")
        return self._d.schedules

    async def list_schedules(self) -> dict[str, Any]:
        """The standing requests here, and whether anything is firing them."""
        if self._d.schedules is None:
            return {"available": False, "running": False, "schedules": []}
        workspace = await self._here()
        found = await self._d.schedules.list(workspace)
        events = await self._d.events.recent(workspace, limit=200) if self._d.events else []
        listed = []
        for item in found:
            status = None
            if item.last_objective_id is not None:
                last = await self._d.objectives.get(item.last_objective_id)
                status = last.status.value if last else None
            runs = []
            for event in events:
                if event.kind not in {"objective.finished", "workflow.finished"}:
                    continue
                if event.payload.get("schedule_id") != str(item.id):
                    continue
                raw_objective = str(event.payload.get("objective_id") or "")
                objective = None
                workflow_run = None
                if raw_objective:
                    with suppress(ValueError):
                        objective = await self._d.objectives.get(UUID(raw_objective))
                raw_workflow_run = str(event.payload.get("workflow_run_id") or "")
                if raw_workflow_run and self._d.workflow_runs is not None:
                    with suppress(ValueError):
                        workflow_run = await self._d.workflow_runs.get(UUID(raw_workflow_run))
                runs.append(views.schedule_run(event, objective, workflow_run=workflow_run))
                if len(runs) == 5:
                    break
            consecutive_failures = 0
            for run in runs:
                if run["status"] in {"DONE", "COMPLETED"}:
                    break
                consecutive_failures += 1
            last_success_at = next(
                (
                    run["finished_at"]
                    for run in runs
                    if run["status"] in {"DONE", "COMPLETED"}
                ),
                None,
            )
            listed.append(
                views.schedule(
                    item,
                    last_status=status,
                    recent_runs=runs,
                    consecutive_failures=consecutive_failures,
                    last_success_at=last_success_at,
                )
            )
        return {"available": True, "running": self._d.scheduler_running, "schedules": listed}

    async def create_schedule(
        self,
        request: str,
        *,
        name: str = "",
        every_minutes: int | None = None,
        daily_at: str = "",
        utc_offset_minutes: int = 0,
        timezone: str = "",
        on_event: str = "",
        conversation_id: UUID | None = None,
        model: str = "",
        approvals: str = "ASK",
        workflow_name: str = "",
        workflow_version: int | None = None,
        workflow_inputs: dict[str, object] | None = None,
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
        definition = None
        values: dict[str, object] = {}
        if workflow_name.strip():
            _, registry = self._workflow_components()
            definition = registry.get(workflow_name.strip(), workflow_version)
            readiness = await self._workflow_readiness(definition, await self._here())
            if not readiness["ready"]:
                raise PrometheusError(
                    "Workflow is not ready: " + "; ".join(readiness["issues"])
                )
            try:
                values = definition.values(workflow_inputs)
            except ValueError as error:
                raise PrometheusError(str(error)) from error
            text = text or f"Run {definition.name} workflow version {definition.version}"
        if not text:
            raise PrometheusError("A schedule with no request would ask for nothing.")
        recurrence = _recurrence(
            every_minutes, daily_at, utc_offset_minutes, on_event, timezone
        )
        workspace = await self._here()
        model = await self._model_for_schedule(
            definition.profile.model if definition else model, workspace
        )

        thread = None
        if conversation_id is not None:
            thread = await self._d.conversations.get(conversation_id)
        if thread is None and definition is None:
            thread = Conversation.create(name.strip() or text, workspace_id=workspace)
            await self._d.conversations.save(thread)

        try:
            created = Schedule.create(
                text,
                name=name.strip(),
                recurrence=recurrence,
                on_event=on_event.strip(),
                workspace_id=workspace,
                conversation_id=thread.id if thread else None,
                model=model,
                approvals=(
                    definition.profile.approvals
                    if definition
                    else _approval_choice(approvals)
                ),
                workflow_name=definition.name if definition else "",
                workflow_version=definition.version if definition else None,
                workflow_inputs=values,
                workflow_snapshot=definition.to_snapshot() if definition else {},
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
        timezone: str = "",
        on_event: str = "",
        model: str = "",
        approvals: str = "ASK",
        workflow_name: str = "",
        workflow_version: int | None = None,
        workflow_inputs: dict[str, object] | None = None,
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
        recurrence = _recurrence(
            every_minutes, daily_at, utc_offset_minutes, on_event, timezone
        )
        definition = None
        values = dict(found.workflow_inputs)
        if workflow_name.strip():
            _, registry = self._workflow_components()
            definition = registry.get(workflow_name.strip(), workflow_version)
            readiness = await self._workflow_readiness(definition, found.workspace_id)
            if not readiness["ready"]:
                raise PrometheusError(
                    "Workflow is not ready: " + "; ".join(readiness["issues"])
                )
            try:
                values = definition.values(workflow_inputs)
            except ValueError as error:
                raise PrometheusError(str(error)) from error
        elif found.is_workflow:
            definition = WorkflowDefinition.from_snapshot(found.workflow_snapshot)
            try:
                values = definition.values(workflow_inputs or found.workflow_inputs)
            except ValueError as error:
                raise PrometheusError(str(error)) from error
        chosen = await self._model_for_schedule(
            definition.profile.model if definition else model, found.workspace_id
        )
        try:
            updated = found.edited(
                request=request,
                name=name,
                recurrence=recurrence,
                on_event=on_event,
                model=chosen,
                approvals=(
                    definition.profile.approvals
                    if definition
                    else _approval_choice(approvals)
                ),
            )
        except ValueError as error:
            raise PrometheusError(str(error)) from error
        if definition is not None:
            updated = replace(
                updated,
                workflow_name=definition.name,
                workflow_version=definition.version,
                workflow_inputs=values,
                workflow_snapshot=definition.to_snapshot(),
            )
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
        if enabled and found.is_workflow:
            definition = WorkflowDefinition.from_snapshot(found.workflow_snapshot)
            readiness = await self._workflow_readiness(definition, found.workspace_id)
            if not readiness["ready"]:
                raise PrometheusError(
                    "Workflow is not ready: " + "; ".join(readiness["issues"])
                )
        updated = found.set_enabled(enabled)
        await store.save(updated)
        return views.schedule(updated)

    async def delete_schedule(self, schedule_id: UUID) -> bool:
        """The instruction goes; the thread and what its runs did stay, as history does."""
        store = self._schedules()
        if await store.get(schedule_id) is None:
            raise NotFoundError("No schedule with that id.")
        return await store.delete(schedule_id)

    # --- A schedule's own settled order ---------------------------------------
    #
    # A person setting work on a clock is not choosing a process, and until
    # Phase 20 the schedule form asked them to. The platform already noticed
    # when it kept replanning the same thing; what was missing was addressing
    # the offer to the standing request it was found in, where the words mean
    # something to whoever wrote them.
    #
    # Which schedule a pattern belongs to is read off the record and stored
    # nowhere: a schedule's firings are written into its thread, so a suggestion
    # is that schedule's exactly when every run it was found in is one of that
    # thread's objectives. No owner column, and no rule forbidding a process to
    # be used twice - there is simply no longer anywhere to choose one from.

    async def _schedule_suggestions(self, schedule: Schedule) -> list[Any]:
        """What this schedule's own successful runs keep repeating."""
        if self._d.workflow_suggestions is None or schedule.conversation_id is None:
            return []
        mine = {
            objective.id
            for objective in await self._d.objectives.for_conversation(schedule.conversation_id)
        }
        if not mine:
            return []
        shown = await self._d.workflow_suggestions.refresh(schedule.workspace_id)
        return [
            item
            for item in shown
            if item.suggestion.sources and set(item.suggestion.sources) <= mine
        ]

    async def list_schedule_suggestions(self, schedule_id: UUID) -> dict[str, Any]:
        found = await self._schedules().get(schedule_id)
        if found is None:
            raise NotFoundError("No schedule with that id.")
        if self._d.workflow_suggestions is None:
            return {"available": False, "suggestions": []}
        return {
            "available": True,
            "suggestions": [
                views.workflow_suggestion(item, effects={}, ready={})
                for item in await self._schedule_suggestions(found)
            ],
        }

    async def pin_schedule_order(self, schedule_id: UUID, suggestion_id: UUID) -> dict[str, Any]:
        """Confirm the order this schedule keeps arriving at, and run it that way.

        Two acts, in this order: the process is written as a declaration, and
        only then does the schedule point at it. A failed write leaves the
        schedule exactly as it was, still asking its request the long way.

        An improvement is a later version of the same declaration, never an
        edit of it: what ran before stays on disk and stays loadable, so
        unpinning or reading back what changed is possible afterwards.
        """
        store = self._schedules()
        found = await store.get(schedule_id)
        if found is None:
            raise NotFoundError("No schedule with that id.")
        _, registry = self._workflow_components()
        shown = await self._schedule_suggestions(found)
        chosen = next((item for item in shown if item.suggestion.id == suggestion_id), None)
        if chosen is None:
            raise NotFoundError("That suggestion is not open for this schedule.")
        name, version = self._next_declaration(found, registry)
        await self._suggestions().save(
            suggestion_id,
            found.workspace_id,
            name=name,
            description=f"The order {found.name or found.request} settled into.",
            version=version,
        )
        # The registry is reloaded by the save itself, through the callable the
        # composition root handed the suggestions service.
        definition = registry.get(name, version)
        updated = replace(
            found,
            workflow_name=name,
            workflow_version=version,
            # The schedule keeps its own words; only the route is fixed.
            workflow_inputs={"request": found.request},
            workflow_snapshot=definition.to_snapshot(),
        )
        await store.save(updated)
        log.info(
            "schedule.order_pinned",
            schedule_id=str(updated.id),
            workflow=name,
            version=version,
        )
        return views.schedule(updated)

    async def unpin_schedule_order(self, schedule_id: UUID) -> dict[str, Any]:
        """Back to asking the request the long way. The declaration stays on disk."""
        store = self._schedules()
        found = await store.get(schedule_id)
        if found is None:
            raise NotFoundError("No schedule with that id.")
        updated = replace(
            found, workflow_name="", workflow_version=None, workflow_inputs={}, workflow_snapshot={}
        )
        await store.save(updated)
        log.info("schedule.order_unpinned", schedule_id=str(updated.id))
        return views.schedule(updated)

    def _next_declaration(
        self, schedule: Schedule, registry: WorkflowRegistry
    ) -> tuple[str, int]:
        """The name and version this schedule's confirmed order is written as.

        Improving what this schedule already runs is the next version of that
        same name. Anything else starts at version 1 under a name nothing here
        holds - a first confirmation must never read as a revision of a process
        somebody else wrote.
        """
        declared: dict[str, int] = {}
        for definition in registry.list_all():
            declared[definition.name] = max(declared.get(definition.name, 0), definition.version)
        if schedule.workflow_name and schedule.workflow_name in declared:
            return schedule.workflow_name, declared[schedule.workflow_name] + 1
        base = _slug(schedule.name or schedule.request)
        if base not in declared:
            return base, 1
        suffix = 2
        while f"{base}-{suffix}" in declared:
            suffix += 1
        return f"{base}-{suffix}", 1

    async def run_schedule_now(self, schedule_id: UUID) -> dict[str, Any]:
        """The request, asked now, in the schedule's thread - through the one way in.

        It is a run like an automatic one for history and health, but it does
        not move the automatic clock. The request returns immediately so the
        thread can show progress while a recorder waits in the background.
        """
        found = await self._schedules().get(schedule_id)
        if found is None:
            raise NotFoundError("No schedule with that id.")
        if found.is_workflow:
            if self._d.workflows is None:
                raise ConfigurationError("This interface was built without workflows.")
            definition = WorkflowDefinition.from_snapshot(found.workflow_snapshot)
            readiness = await self._workflow_readiness(definition, found.workspace_id)
            if not readiness["ready"]:
                raise PrometheusError(
                    "Workflow is not ready: " + "; ".join(readiness["issues"])
                )
            run = await self._d.workflows.run_definition(
                definition,
                inputs=found.workflow_inputs,
                trigger=WorkflowTrigger.MANUAL,
                workspace_id=found.workspace_id,
            )
            current = await self._schedules().get(found.id)
            if current is not None:
                await self._schedules().save(current.manually_fired(datetime.now(UTC)))
            if self._d.events is not None:
                from domain.scheduling.models import Event

                await self._d.events.record(
                    Event.create(
                        "workflow.finished",
                        workspace_id=found.workspace_id,
                        source=found.name or str(found.id),
                        payload={
                            "schedule_id": str(found.id),
                            "schedule_version": found.version,
                            "objective_id": "",
                            "status": run.status.value,
                            "trigger": "MANUAL",
                            "workflow_run_id": str(run.id),
                            "workflow_name": run.workflow,
                            "workflow_version": run.workflow_version,
                            "cost_usd": run.cost_usd,
                            "quality": run.quality,
                        },
                    )
                )
            return {
                **views.workflow_run(run),
                "conversation_id": None,
            }
        answer = await self.submit(
            UserRequest(
                content=found.request,
                source=RequestSource.API,
                conversation_id=found.conversation_id,
                workspace_id=found.workspace_id,
                directions=found.directions,
            )
        )
        objective_id = UUID(answer["id"])
        recorder = asyncio.create_task(
            self._record_manual_schedule_run(found, objective_id),
            name=f"prometheus-manual-schedule-{found.id}",
        )
        self._background.add(recorder)
        recorder.add_done_callback(self._background.discard)
        return {
            **answer,
            "conversation_id": str(found.conversation_id) if found.conversation_id else None,
        }

    async def _record_manual_schedule_run(
        self, schedule: Schedule, objective_id: UUID
    ) -> None:
        result = await self._d.runs.wait_objective(objective_id)
        current = await self._schedules().get(schedule.id)
        if current is not None:
            await self._schedules().save(
                current.manually_fired(datetime.now(UTC), objective_id)
            )
        if self._d.events is not None:
            from domain.scheduling.models import Event

            status = result.status.value if result is not None else "FAILED"
            await self._d.events.record(
                Event.create(
                    "objective.finished",
                    workspace_id=schedule.workspace_id,
                    source=schedule.name or str(schedule.id),
                    payload={
                        "schedule_id": str(schedule.id),
                        "schedule_version": schedule.version,
                        "objective_id": str(objective_id),
                        "status": status,
                        "trigger": "MANUAL",
                    },
                )
            )

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

    async def workforce(self) -> dict[str, Any]:
        """Every role here, with the core's verdict on whether it can work now.

        Unavailable is an answer, not a missing one: a surface built without
        profiles says `available: false` rather than listing nobody, which would
        read as a workforce of zero.
        """
        if self._d.workforce is None:
            return {"available": False, "employees": [], "overlaps": []}
        workspace = await self._here()
        return {
            "available": True,
            "employees": [
                views.employee_card(profile) for profile in self._d.workforce.profiles(workspace)
            ],
            "overlaps": [list(group) for group in self._d.workforce.overlaps(workspace)],
        }

    async def employee_profile(
        self, name: str, *, window_days: int = 30
    ) -> dict[str, Any] | None:
        if self._d.workforce is None:
            raise ConfigurationError("This interface was built without workforce profiles.")
        if not 1 <= window_days <= 365:
            raise PrometheusError("The window is between 1 and 365 days.")
        record = await self._d.workforce.record(
            name, await self._here(), window=timedelta(days=window_days)
        )
        return views.employee_profile(record) if record is not None else None

    async def list_workflow_suggestions(self) -> dict[str, Any]:
        if self._d.workflow_suggestions is None:
            return {"available": False, "suggestions": []}
        workspace = await self._here()
        shown = await self._d.workflow_suggestions.refresh(workspace)
        effects: dict[str, list[str]] = {}
        ready: dict[str, str] = {}
        if self._d.workforce is not None:
            for profile in self._d.workforce.profiles(workspace):
                effects[profile.name] = sorted(
                    {tool.effect.value for tool in profile.tools if tool.effect is not None}
                )
                ready[profile.name] = profile.readiness.state.value
        return {
            "available": True,
            "suggestions": [
                views.workflow_suggestion(item, effects=effects, ready=ready) for item in shown
            ],
        }

    def _suggestions(self) -> WorkflowSuggestions:
        if self._d.workflow_suggestions is None:
            raise ConfigurationError("This interface was built without workflow suggestions.")
        return self._d.workflow_suggestions

    async def dismiss_workflow_suggestion(self, suggestion_id: UUID) -> dict[str, Any]:
        item = await self._suggestions().dismiss(suggestion_id, await self._here())
        return {"id": str(item.id), "status": item.status.value}

    async def snooze_workflow_suggestion(self, suggestion_id: UUID, days: int) -> dict[str, Any]:
        item = await self._suggestions().snooze(suggestion_id, await self._here(), days=days)
        return {
            "id": str(item.id),
            "status": item.status.value,
            "snoozed_until": item.snoozed_until.isoformat() if item.snoozed_until else None,
        }

    async def save_workflow_suggestion(
        self, suggestion_id: UUID, *, name: str, description: str = ""
    ) -> dict[str, Any]:
        item, where = await self._suggestions().save(
            suggestion_id, await self._here(), name=name, description=description
        )
        return {
            "id": str(item.id),
            "status": item.status.value,
            "workflow": item.workflow_name,
            # The file name only: the directory is this machine's, and nothing
            # an interface does with it needs the rest of the path.
            "file": Path(where).name,
        }

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

    # --- Observability -------------------------------------------------------

    def _observability(self) -> ObservabilityService:
        if self._d.observability is None:
            raise PrometheusError("Observability is unavailable on this runtime.")
        return self._d.observability

    async def trace(self, identifier: UUID) -> dict[str, Any] | None:
        return await self._observability().get(identifier)

    async def traces(
        self, *, limit: int = DEFAULT_LIMIT, entity_type: str = "", entity_id: str = ""
    ) -> list[dict[str, Any]]:
        return await self._observability().recent(
            await self._here(),
            limit=limit,
            entity_type=entity_type,
            entity_id=entity_id,
        )

    async def observability_health(self) -> dict[str, Any]:
        return await self._observability().health(await self._here())

    async def observability_metrics(
        self, *, days: int = 30, run_kind: str = "", model_profile: str = ""
    ) -> dict[str, Any]:
        kind = RunKind(run_kind.upper()) if run_kind else None
        return await self._observability().metrics(
            await self._here(), days=days, run_kind=kind, model_profile=model_profile
        )

    async def diagnostic_bundle(self, trace_ids: tuple[UUID, ...] = ()) -> dict[str, Any]:
        return await self._observability().diagnostic_bundle(await self._here(), trace_ids)

    async def export_diagnostic_bundle(
        self, path: Path, trace_ids: tuple[UUID, ...] = ()
    ) -> dict[str, str]:
        written = await self._observability().export_bundle(
            path, await self._here(), trace_ids
        )
        return {"path": str(written)}

    async def prune_traces(self, *, retention_days: int = 30) -> dict[str, int]:
        removed = await self._observability().prune(
            await self._here(), retention_days=retention_days
        )
        return {"removed": removed, "retention_days": retention_days}

    async def verify_audit(self) -> dict[str, Any]:
        return asdict(await self._observability().verify_audit(await self._here()))

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

    # --- The emergency stop ---------------------------------------------------

    def _emergency(self) -> EmergencyStopControl:
        if self._d.emergency is None:
            raise PrometheusError("This interface was built without an emergency stop.")
        return self._d.emergency

    def stop_state(self) -> dict[str, Any]:
        """Whether all work is stopped. Read from the record, never from memory."""
        if self._d.emergency is None:
            return {"engaged": False, "available": False}
        return {**self._d.emergency.state().to_dict(), "available": True}

    async def emergency_stop(self, reason: str = "", *, by: str = "user") -> dict[str, Any]:
        """Stop all work on this machine, now, until a person resumes it."""
        return (await self._emergency().engage(reason, by=by)).to_dict()

    # --- Updates --------------------------------------------------------------

    def update_readiness(self) -> dict[str, Any]:
        """Whether the program could be replaced this instant without cutting an effect.

        Decided here, not in a window: a shell that computed it from a task list
        would be the second place the rule lives, and the one that goes stale.
        """
        in_flight = self._d.effects.in_flight() if self._d.effects is not None else ()
        return {
            "safe": not in_flight,
            "held": bool(self._d.effects is not None and self._d.effects.held),
            "in_flight": [item.to_dict() for item in in_flight],
            "carrying": self._d.runs.carrying,
            # Running work is not a reason to wait: it is durable and resumes
            # after the restart. An effect in flight is the only one.
            "resumes_after_restart": self._d.runs.carrying > 0,
            "stop": self.stop_state(),
        }

    async def prepare_update(self, timeout_seconds: float = 30.0) -> dict[str, Any]:
        """Hold new effects and wait for the running ones to finish.

        Ready means the hold is in place and nothing is in flight; it stays until
        this process exits. Not ready releases the hold again, so work does not
        sit parked behind an update nobody is installing - the window can wait
        and ask again, or offer the emergency stop.
        """
        if self._d.effects is None:
            return {**self.update_readiness(), "ready": True}
        self._d.effects.hold()
        ready = await self._d.effects.wait_idle(timeout_seconds)
        if not ready:
            self._d.effects.release()
        log.info("interface.update_prepared", ready=ready)
        return {**self.update_readiness(), "ready": ready}

    def cancel_update(self) -> dict[str, Any]:
        if self._d.effects is not None:
            self._d.effects.release()
        return self.update_readiness()

    # --- Backups --------------------------------------------------------------

    def _backups(self) -> Backups:
        if self._d.backups is None:
            raise PrometheusError("Backups are not available on this interface.")
        return self._d.backups

    async def create_backup(
        self, destination: str | None = None, *, passphrase: str | None = None
    ) -> dict[str, Any]:
        """Write a backup while work continues. Off the loop: it reads every file."""
        return await asyncio.to_thread(
            self._backups().create, destination, passphrase=passphrase
        )

    async def verify_backup(self, path: str, *, passphrase: str | None = None) -> dict[str, Any]:
        """Check a backup completely without changing anything."""
        return await asyncio.to_thread(self._backups().verify, path, passphrase=passphrase)

    async def watch_stop(self, done: asyncio.Event) -> None:
        """Enforce a stop set from outside this process, until `done` is set."""
        if self._d.emergency is not None:
            await self._d.emergency.watch(done)

    async def resume_work(self, *, by: str = "user") -> dict[str, Any]:
        """Lift the stop. Starts nothing that was stopped."""
        return (await self._emergency().release(by=by)).to_dict()

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
            # A file read, not a query: it is what every window shows at all
            # times, and a stop that only appears after navigating is not a
            # stop anybody sees.
            "stop": self.stop_state(),
        }


def _slug(text: str) -> str:
    """A declaration name out of a person's words.

    The same shape the writer enforces - lowercase letters, digits and hyphens,
    two to sixty-three of them. Nobody types this and nobody is shown it; it
    exists because a file needs a name, so a request that slugs to nothing at
    all still gets one rather than a refusal a person cannot act on.
    """
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    trimmed = cleaned[:63].rstrip("-")
    return trimmed if len(trimmed) >= 2 else "settled-order"


def _approval_choice(value: str) -> ApprovalChoice:
    try:
        return ApprovalChoice((value or "ASK").strip().upper())
    except ValueError as error:
        raise PrometheusError(
            f"'{value}' is not an approvals choice: ASK, AUTO or DENY."
        ) from error


def _recurrence(
    every_minutes: int | None,
    daily_at: str,
    utc_offset_minutes: int,
    on_event: str,
    timezone: str = "",
) -> Recurrence | None:
    chosen = [every_minutes is not None, bool(daily_at.strip()), bool(on_event.strip())]
    if sum(chosen) != 1:
        raise PrometheusError(
            "Choose exactly one: every so many minutes, a time of day, or an event."
        )
    if on_event.strip():
        return None
    if every_minutes is not None:
        if every_minutes * 60 < MIN_INTERVAL_SECONDS:
            raise PrometheusError("The shortest interval is one minute.")
        return Recurrence(every_seconds=every_minutes * 60)
    try:
        local = time.fromisoformat(daily_at.strip())
    except ValueError as error:
        raise PrometheusError(f"'{daily_at}' is not a time of day (HH:MM).") from error
    if timezone.strip():
        try:
            return Recurrence(daily_at=local, timezone=timezone.strip())
        except ValueError as error:
            raise PrometheusError(str(error)) from error
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


def _note(content: str) -> str:
    stated = " ".join(content.split())
    if not stated:
        raise ValueError("There is nothing to remember.")
    if len(stated) > MAX_NOTE_LENGTH:
        raise ValueError(
            f"A note is at most {MAX_NOTE_LENGTH} characters; a longer text is a document."
        )
    return stated


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True
