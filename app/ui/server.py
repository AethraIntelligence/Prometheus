"""The HTTP adapter: transport, and deliberately nothing else.

Phase 6's Definition of Done is that a developer uses Prometheus without reading logs
in a terminal. Phase 7 changed what the page asks for - an outcome, not an
employee. Phase 13 changed where the answer comes from: every route here now
calls `PrometheusService`, the application-level boundary, and this file owns URLs,
status codes, request bodies and the framing of an event stream. That is the
whole of its job. A rule that lives here is a rule the desktop shell and a chat
bot would each have to reimplement, and the three would disagree.

**It binds to 127.0.0.1 and has no authentication.** Those two facts are one
decision, not two. The interface starts tasks, approves irreversible actions and
can stop the machine's screen; it is safe without a password precisely because
nothing off this machine can reach it. Binding it anywhere else would turn a
local tool into an unauthenticated remote one, which is why the host is a
setting that documents itself rather than a command-line flag inviting `0.0.0.0`.

**The trace is pushed, not polled.** Server-sent events, because the traffic is
one-way - the server describes, the client draws - and SSE reconnects on its
own, needs no library, and survives a window being left open while nothing runs.
A websocket would buy a direction nobody uses.

**Approvals are answered here, and the run really is parked.** The tool call
waits on a future (`WaitingConfirmer`); this hands it the answer. Nothing is
approved by default, by timeout, or by the page being closed.

**The desktop shell is a client of this, not a second server.** It talks the
same HTTP and the same SSE a browser does, which is what keeps it an interface
rather than a fork of the platform - and what makes the browser page and the
Tauri window two views of one running engine rather than two engines.

The container, the runner and the live runs are per-application, created at
startup and closed at shutdown, so a client talking to a dead engine is not a
state this can be in.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config.container import (
    build_container,
    build_manager,
    build_service,
    build_workflow_engine,
    prepare,
)
from app.config.settings import Settings, get_settings
from app.ui.restart import RESTART, RestartSignal
from application.interface.activity import ActivityEvent
from application.interface.contracts import (
    ApprovalChoice,
    Directions,
    InputType,
    RequestSource,
    UserRequest,
)
from application.interface.service import ApprovalsDisabledError, PrometheusService
from application.scheduling.scheduler import Scheduler
from domain.errors import (
    DuplicateIntegrationError,
    DuplicateWorkspaceError,
    FolderError,
    IntegrationNotFoundError,
    NotFoundError,
    PluginConfigurationError,
    PrometheusError,
    ProtectedWorkspaceError,
    StorageNotInitializedError,
    WorkspaceNotFoundError,
)
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId
from infrastructure.approvals.waiting import WaitingConfirmer
from infrastructure.container import Container

log = structlog.get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

#: How long the event stream waits before sending a comment line. Without it a
#: proxy or a sleeping laptop can drop an idle connection with nothing to show
#: for it, and the client would sit silently on a stream that is already dead.
HEARTBEAT = ": keep-alive\n\n"

#: The origins a desktop shell talks from. Two kinds, and the second one was
#: learned the hard way: in development the window loads from a local dev
#: server, but a *packaged* window serves its page from Tauri's own protocol
#: and sends `tauri://localhost` as its origin - so a list of loopback URLs
#: silently blocks every built application while every development run works.
#:
#: They are still all local: a custom protocol on this machine and two loopback
#: ports. Nothing here widens what can reach this server from a network.
DESKTOP_ORIGINS = (
    "tauri://localhost",
    "http://tauri.localhost",
    "http://localhost:1420",
    "http://127.0.0.1:1420",
)


class NewTask(BaseModel):
    goal: str = Field(min_length=1)
    employee: str = Field(min_length=1)


class NewObjective(BaseModel):
    request: str = Field(min_length=1)
    conversation_id: UUID | None = None
    #: Which interface this arrived from, and what the person actually gave.
    #: Recorded on the way in and never branched on; see
    #: `application/interface/contracts.py`.
    source: RequestSource = RequestSource.WEB
    input_type: InputType = InputType.TEXT
    #: Which context the request belongs to. Absent means the workspace this
    #: machine is working in, which is what a surface without a selector wants
    #: and what every request meant before there was more than one.
    workspace_id: str | None = None
    #: How to go about it, as chosen under the field. Absent means ask before
    #: anything that needs approval and let the router choose the model - what
    #: every request meant before a window could say otherwise.
    approvals: ApprovalChoice = ApprovalChoice.ASK
    model: str = Field(default="", max_length=120)
    #: A folder the person chose for this thread's work. Empty keeps the one
    #: the thread has, or gives it its own.
    folder: str = Field(default="", max_length=1024)


class SettingsChange(BaseModel):
    """Some of Settings -> General, by key. What each value may be is the editor's to say."""

    values: dict[str, bool | int | float | str | list[str] | None] = Field(min_length=1)


class ScheduleForm(BaseModel):
    """A schedule as the window's form holds it. Exactly one "when" is checked by the core."""

    request: str = ""
    name: str = Field(default="", max_length=120)
    every_minutes: int | None = None
    #: HH:MM in the person's own clock, with the offset their clock had when
    #: they typed it. Converted to UTC by the core, which is where the rule lives.
    daily_at: str = ""
    utc_offset_minutes: int = 0
    #: IANA zone keeps a wall-clock promise across daylight-saving changes.
    timezone: str = Field(default="", max_length=64)
    on_event: str = Field(default="", max_length=64)
    #: A catalog entry its runs prefer. Empty: the router decides.
    model: str = Field(default="", max_length=120)
    #: ASK, AUTO or DENY, checked by the core.
    approvals: str = Field(default="ASK", max_length=8)
    workflow_name: str = Field(default="", max_length=64)
    workflow_version: int | None = Field(default=None, ge=1)
    workflow_inputs: dict[str, Any] = Field(default_factory=dict)


class NewSchedule(ScheduleForm):
    #: The thread this schedule repeats, when it was made from one.
    conversation_id: UUID | None = None


class ScheduleEdit(BaseModel):
    enabled: bool


class WorkflowInvocation(BaseModel):
    version: int | None = Field(default=None, ge=1)
    inputs: dict[str, Any] = Field(default_factory=dict)


class SetupChoice(BaseModel):
    """Which connection a recommended setup goes through. Empty: the first of its kind."""

    connection: str = Field(default="", max_length=120)


class SettingsReset(BaseModel):
    """Which settings to reset. None means every one a window saved."""

    keys: list[str] | None = None


class ConversationEdit(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class ConversationFolder(BaseModel):
    #: An absolute path the person chose. Empty gives the thread its own again.
    folder: str = Field(default="", max_length=1024)


class ConversationApprovals(BaseModel):
    approvals: ApprovalChoice


class ConversationModel(BaseModel):
    model: str = Field(default="", max_length=120)


class NewConversation(BaseModel):
    title: str = ""
    kind: str = Field(default="TASK", pattern="^(ASK|TASK)$")


class NewConnection(BaseModel):
    """What a person typed into "Add provider".

    `api_key` comes in and is never sent back by anything: it goes straight to
    the credential store, and every view of a connection carries `has_key`
    instead. A response body that echoed it would put a live key in a browser's
    network log.
    """

    name: str = Field(min_length=1, max_length=120)
    kind: str = Field(min_length=1, max_length=32)
    api_key: str = ""
    base_url: str = ""
    description: str = ""


class NewKey(BaseModel):
    api_key: str = Field(min_length=1)


class NewModel(BaseModel):
    """One catalog entry, as the settings page describes it."""

    name: str = Field(min_length=1, max_length=120)
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=200)
    connection: str = ""
    capabilities: tuple[str, ...] = ()
    #: Left out, it is asked of the runner that serves the model.
    context_tokens: int | None = None
    input_cost_per_1k_usd: float = 0.0
    output_cost_per_1k_usd: float = 0.0
    quality: float = 0.5
    dimensions: int = 0


class WorkRouting(BaseModel):
    """Give this kind of work to that model."""

    task_kind: str = Field(min_length=1, max_length=32)
    entry_name: str = Field(min_length=1, max_length=120)


class NewIntegration(BaseModel):
    """What a person typed into "Add MCP Server".

    `configuration` is free-form because its shape belongs to the transport -
    a command and its arguments for stdio, something else for whatever arrives
    next - and a schema here would have to be widened for every one of them.
    """

    name: str = Field(min_length=1, max_length=120)
    configuration: dict[str, Any] = Field(default_factory=dict)
    kind: str = "MCP"
    capabilities: tuple[str, ...] = ()
    #: Names only. A value is sent to `/api/credentials` and never lands here.
    secret_names: tuple[str, ...] = ()


class PluginInstall(BaseModel):
    """A plugin's settings as a person filled them in, and who may use it.

    Values only for the settings the plugin declares - anything else is
    refused in the domain, so this form cannot put a variable of its choosing
    into a server's environment. `employees` left out means the suggestion.
    """

    values: dict[str, str] = Field(default_factory=dict)
    employees: tuple[str, ...] | None = None


class Grants(BaseModel):
    employees: tuple[str, ...] = ()


class DocumentFile(BaseModel):
    """A newer version of a document's file, by path for the same reason as below."""

    path: str = Field(min_length=1, max_length=4096)


class NewDocument(BaseModel):
    """A file on this machine, by path.

    Not an upload. The platform is local-first and the file is already here;
    a multipart body would make the interface a second copy of it, and the
    desktop window has the path the moment somebody drops one on it.
    """

    path: str = Field(min_length=1)
    title: str = ""
    media_type: str = ""


class NewMemory(BaseModel):
    """Something a person tells the platform to keep, in their own words."""

    content: str = Field(min_length=1, max_length=2000)
    #: True of the person in every workspace, rather than of this one.
    about_the_person: bool = False


class NewWorkspace(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    #: Where its files live. Absent means beside the database, under the
    #: workspace's own id - never inside another workspace's root.
    file_root: str | None = None


class WorkspaceEdit(BaseModel):
    name: str | None = None
    description: str | None = None
    file_root: str | None = None
    #: The whole list of saved folders. Absent leaves it as it is.
    folders: list[str] | None = None


class Classification(BaseModel):
    """What a person says an integration's tools do to the world.

    Tool name to effect. Nothing about risk or approval is accepted here: those
    follow from the effect, and letting an interface send them would be letting
    it set its own policy.
    """

    effects: dict[str, str] = Field(default_factory=dict)


class NewCredential(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1)

class Decision(BaseModel):
    approved: bool
    comment: str = ""
    grant: Literal["ONCE", "TASK", "PERSISTENT"] = "ONCE"
    duration_seconds: float | None = Field(default=None, gt=0, le=31_536_000)


class Cancellation(BaseModel):
    reason: str = ""


class Handoff(BaseModel):
    employee: str = Field(min_length=1, max_length=120)


def create_app(
    settings: Settings | None = None,
    *,
    build: Callable[[Settings], Container] = build_container,
    restart: RestartSignal = RESTART,
) -> FastAPI:
    """Build the interface around its own container.

    Settings and the way the container is built are both arguments, for the same
    reason the container is handed its settings rather than reading them: it is
    what lets the whole interface be exercised against a temporary database and
    a scripted model, with no server running and no provider key configured.
    """
    resolved = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = build(resolved)
        # Set before anything can ask: an approval that reached stdin while the
        # person is looking at a window is an approval nobody can answer.
        confirmer = WaitingConfirmer(
            timeout_seconds=resolved.ui_approval_timeout_seconds,
            progress=container.progress,
        )
        container.use_approval_confirmer(confirmer)
        await prepare(container)

        app.state.settings = resolved
        app.state.restart = restart
        app.state.container = container
        app.state.confirmer = confirmer
        app.state.service = build_service(
            container,
            confirmer,
            history_limit=resolved.ui_history_limit,
            scheduler_running=resolved.scheduler_enabled,
        )
        await app.state.service.recover()
        # Started on the same loop that serves the requests, for the same
        # reason a task is: one process, one database, and a proactive
        # objective that is watched in the trace exactly like one somebody
        # asked for. Off unless the flag says otherwise - work nobody asked
        # for is opt-in.
        stop_scheduler = asyncio.Event()
        scheduler_task: asyncio.Task[None] | None = None
        if resolved.scheduler_enabled:
            async def every_workspace() -> list[WorkspaceId]:
                return [item.id for item in await container.workspace_repository.list()]

            scheduler = Scheduler(
                manager=build_manager(container),
                schedules=container.schedule_repository,
                events=container.event_log,
                tick_seconds=resolved.scheduler_tick_seconds,
                workspaces=every_workspace,
                conversations=container.conversation_repository,
                folder_root=container.workspaces.root_for,
                workflows=(
                    build_workflow_engine(container) if resolved.workflows_enabled else None
                ),
            )
            scheduler_task = asyncio.create_task(scheduler.run_forever(stop_scheduler))

        log.info(
            "ui.started",
            host=resolved.ui_host,
            port=resolved.ui_port,
            scheduler=resolved.scheduler_enabled,
        )
        try:
            yield
        finally:
            stop_scheduler.set()
            if scheduler_task is not None:
                # Awaited rather than cancelled: a firing that is halfway
                # through an objective should finish the tick it is in, and the
                # loop already checks the signal between them.
                await scheduler_task
            await app.state.service.aclose()
            await container.aclose()

    app = FastAPI(title="Prometheus", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(DESKTOP_ORIGINS),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    _routes(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


# --- Routes -------------------------------------------------------------------


def _routes(app: FastAPI) -> None:
    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        """Is the engine answering. What a shell polls while it starts one up.

        `started_at` is this process's, so a window waiting out a restart can
        tell the new process from the old one still answering its last requests.
        """
        signal: RestartSignal = request.app.state.restart
        return {
            **_service(request).health(),
            "started_at": signal.started_at,
            "can_restart": signal.available,
            "carrying": _service(request).carrying(),
        }

    @app.post("/api/runtime/restart", status_code=202)
    async def restart_runtime(request: Request) -> dict[str, Any]:
        """Stop gracefully and start again, so code, `.env` and settings are reread.

        Answered first and acted on a moment later: the stop closes the server,
        and a request that closed it before replying would look to the window
        like a failure of the very thing that is working.
        """
        signal: RestartSignal = request.app.state.restart
        if not signal.available:
            raise HTTPException(
                status_code=409,
                detail=(
                    "This runtime cannot restart itself - it was not started with "
                    "`prometheus serve`, or runs with --reload. Restart it where it runs."
                ),
            )
        stopping = _service(request).carrying()
        asyncio.get_running_loop().call_later(0.3, signal.request)
        return {"restarting": True, "stopping": stopping, "started_at": signal.started_at}

    @app.get("/api/employees")
    async def employees(request: Request) -> dict[str, Any]:
        return {"employees": _service(request).list_employees()}

    @app.get("/api/tools")
    async def tools(request: Request) -> dict[str, Any]:
        """What this machine can do, and who is allowed to ask for it.

        Read-only, and there is deliberately no route that switches one on or
        off: a grant is a line in an employee's declaration, and an endpoint
        that changed it here would be a second way to say the same thing.
        """
        return {"tools": _service(request).list_tools()}

    @app.get("/api/tasks")
    async def history(request: Request) -> dict[str, Any]:
        return {"tasks": await _guarded(_service(request).list_tasks())}

    @app.post("/api/tasks", status_code=201)
    async def start(request: Request, body: NewTask) -> dict[str, Any]:
        try:
            return await _service(request).start_task(body.goal, body.employee)
        except PrometheusError as error:
            # An unknown employee is the user asking for something that does not
            # exist, not a server fault: 400, with the reason said plainly.
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/tasks/{task_id}")
    async def detail(request: Request, task_id: UUID) -> dict[str, Any]:
        found = await _guarded(_service(request).get_task(task_id))
        return _found(found, f"Unknown task: {task_id}")

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel(request: Request, task_id: UUID, body: Cancellation) -> dict[str, Any]:
        found = await _guarded(_service(request).cancel_task(task_id, body.reason))
        return _found(found, f"Unknown task: {task_id}")

    @app.post("/api/tasks/{task_id}/retry", status_code=201)
    async def retry_task(request: Request, task_id: UUID) -> dict[str, Any]:
        try:
            found = await _guarded(_service(request).retry_task(task_id))
        except PrometheusError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return _found(found, f"Unknown task: {task_id}")

    @app.post("/api/tasks/{task_id}/handoff", status_code=201)
    async def handoff_task(
        request: Request, task_id: UUID, body: Handoff
    ) -> dict[str, Any]:
        try:
            found = await _guarded(_service(request).handoff_task(task_id, body.employee))
        except PrometheusError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return _found(found, f"Unknown task: {task_id}")

    # --- Conversations --------------------------------------------------------

    @app.post("/api/conversations", status_code=201)
    async def open_thread(request: Request, body: NewConversation) -> dict[str, Any]:
        return await _guarded(
            _service(request).create_conversation(body.title, kind=body.kind)
        )

    @app.get("/api/conversations")
    async def threads(request: Request) -> dict[str, Any]:
        return {"conversations": await _guarded(_service(request).list_conversations())}

    @app.get("/api/conversations/{conversation_id}")
    async def thread(request: Request, conversation_id: UUID) -> dict[str, Any]:
        found = await _guarded(_service(request).get_conversation(conversation_id))
        return _found(found, f"Unknown conversation: {conversation_id}")

    @app.patch("/api/conversations/{conversation_id}")
    async def rename_thread(
        request: Request, conversation_id: UUID, body: ConversationEdit
    ) -> dict[str, Any]:
        found = await _guarded(_service(request).rename_conversation(conversation_id, body.title))
        return _found(found, f"Unknown conversation: {conversation_id}")

    @app.put("/api/conversations/{conversation_id}/folder")
    async def thread_folder(
        request: Request, conversation_id: UUID, body: ConversationFolder
    ) -> dict[str, Any]:
        try:
            found = await _guarded(
                _service(request).set_conversation_folder(conversation_id, body.folder)
            )
        except FolderError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return _found(found, f"Unknown conversation: {conversation_id}")

    @app.put("/api/conversations/{conversation_id}/approvals")
    async def thread_approvals(
        request: Request, conversation_id: UUID, body: ConversationApprovals
    ) -> dict[str, Any]:
        found = await _guarded(
            _service(request).set_conversation_approvals(conversation_id, body.approvals)
        )
        return _found(found, f"Unknown conversation: {conversation_id}")

    @app.put("/api/conversations/{conversation_id}/model")
    async def thread_model(
        request: Request, conversation_id: UUID, body: ConversationModel
    ) -> dict[str, Any]:
        found = await _guarded(
            _service(request).set_conversation_model(conversation_id, body.model)
        )
        return _found(found, f"Unknown conversation: {conversation_id}")

    @app.delete("/api/conversations/{conversation_id}")
    async def delete_thread(request: Request, conversation_id: UUID) -> dict[str, Any]:
        if not await _guarded(_service(request).delete_conversation(conversation_id)):
            raise HTTPException(status_code=404, detail=f"Unknown conversation: {conversation_id}")
        return {"deleted": True}

    @app.post("/api/conversations/{conversation_id}/messages", status_code=201)
    async def say(
        request: Request, conversation_id: UUID, body: NewObjective
    ) -> dict[str, Any]:
        """Say something in a thread. One message is one objective."""
        return await _ask(request, body, conversation_id=conversation_id)

    # --- The manager ----------------------------------------------------------

    @app.post("/api/objectives", status_code=201)
    async def ask(request: Request, body: NewObjective) -> dict[str, Any]:
        """State a goal. Prometheus decides what it means and who does it."""
        return await _ask(request, body, conversation_id=body.conversation_id)

    @app.get("/api/objectives")
    async def objectives(request: Request) -> dict[str, Any]:
        return {"objectives": await _guarded(_service(request).list_objectives())}

    @app.get("/api/objectives/{objective_id}")
    async def objective(request: Request, objective_id: UUID) -> dict[str, Any]:
        found = await _guarded(_service(request).get_objective(objective_id))
        return _found(found, f"Unknown objective: {objective_id}")

    @app.get("/api/objectives/{objective_id}/file")
    async def objective_file(request: Request, objective_id: UUID, path: str) -> FileResponse:
        found = await _guarded(_service(request).artifact_file(objective_id, path))
        if found is None:
            raise HTTPException(status_code=404, detail=f"No file '{path}' from this request")
        target, media_type = found
        return FileResponse(target, media_type=media_type, filename=target.name,
                            content_disposition_type="inline")

    @app.post("/api/objectives/{objective_id}/cancel")
    async def stop_objective(request: Request, objective_id: UUID) -> dict[str, Any]:
        found = await _guarded(_service(request).cancel_objective(objective_id))
        return _found(found, f"Unknown objective: {objective_id}")

    # --- Work Center ---------------------------------------------------------

    @app.get("/api/work")
    async def work(request: Request) -> dict[str, Any]:
        return {"items": await _guarded(_service(request).list_work_items())}

    @app.get("/api/work/{objective_id}")
    async def work_item(request: Request, objective_id: UUID) -> dict[str, Any]:
        found = await _guarded(_service(request).get_work_item(objective_id))
        return _found(found, f"Unknown objective: {objective_id}")

    @app.post("/api/work/{objective_id}/pause")
    async def pause_work(request: Request, objective_id: UUID) -> dict[str, Any]:
        found = await _guarded(_service(request).pause_objective(objective_id))
        return _found(found, f"Unknown objective: {objective_id}")

    @app.post("/api/work/{objective_id}/resume")
    async def resume_work(request: Request, objective_id: UUID) -> dict[str, Any]:
        found = await _guarded(_service(request).resume_objective(objective_id))
        return _found(found, f"Unknown objective: {objective_id}")

    @app.post("/api/work/{objective_id}/retry", status_code=201)
    async def retry_work(request: Request, objective_id: UUID) -> dict[str, Any]:
        try:
            found = await _guarded(_service(request).retry_objective(objective_id))
        except PrometheusError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return _found(found, f"Unknown objective: {objective_id}")

    @app.get("/api/approvals")
    async def approvals(request: Request) -> dict[str, Any]:
        return {"approvals": await _guarded(_service(request).list_approvals())}

    @app.get("/api/approval-inbox")
    async def approval_inbox(request: Request) -> dict[str, Any]:
        return await _guarded(_service(request).list_approval_inbox())

    @app.post("/api/approvals/{approval_id}")
    async def decide(request: Request, approval_id: UUID, body: Decision) -> dict[str, Any]:
        try:
            return await _service(request).decide_approval(
                approval_id,
                approved=body.approved,
                comment=body.comment,
                grant=body.grant,
                duration_seconds=body.duration_seconds,
            )
        except ApprovalsDisabledError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except PrometheusError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.get("/api/capability-leases")
    async def capability_leases(request: Request) -> dict[str, Any]:
        return {"leases": await _guarded(_service(request).list_capability_leases())}

    @app.delete("/api/capability-leases/{lease_id}")
    async def revoke_capability_lease(request: Request, lease_id: UUID) -> dict[str, Any]:
        revoked = await _guarded(_service(request).revoke_capability_lease(lease_id))
        if not revoked:
            raise HTTPException(status_code=404, detail=f"Unknown active permission: {lease_id}")
        return {"id": str(lease_id), "revoked": True}


    @app.get("/api/memory")
    async def memory(request: Request, q: str = "", limit: int = 20) -> dict[str, Any]:
        """What this workspace remembers, or what of it matches `q`."""
        service = _service(request)
        return {
            "available": service.memory_available,
            "can_forget": service.memory_can_forget,
            "items": await _guarded(service.list_memory(search=q, limit=limit)),
        }

    @app.post("/api/memory", status_code=201)
    async def remember(request: Request, body: NewMemory) -> dict[str, Any]:
        return await _memory(
            _service(request).remember(body.content, about_the_person=body.about_the_person)
        )

    @app.delete("/api/memory/{item_id}")
    async def forget(request: Request, item_id: UUID) -> dict[str, Any]:
        """Forget one line a person can see. 404 for anything they cannot."""
        forgotten = await _memory(_service(request).forget_memory(item_id))
        if not forgotten:
            raise HTTPException(status_code=404, detail=f"Nothing remembered here as {item_id}.")
        return {"forgotten": True}

    # --- Documents ------------------------------------------------------------

    @app.get("/api/documents")
    async def documents(request: Request) -> dict[str, Any]:
        service = _service(request)
        return {
            "available": service.knowledge_available,
            "documents": await _guarded(service.list_documents()),
        }

    @app.post("/api/documents", status_code=201)
    async def add_document(request: Request, body: NewDocument) -> dict[str, Any]:
        return await _knowledge(
            _service(request).add_document(
                body.path, title=body.title, media_type=body.media_type
            )
        )

    @app.post("/api/documents/{document_id}/file")
    async def replace_document(
        request: Request, document_id: UUID, body: DocumentFile
    ) -> dict[str, Any]:
        return await _knowledge(_service(request).replace_document(document_id, body.path))

    @app.post("/api/documents/{document_id}/reindex")
    async def reindex_document(request: Request, document_id: UUID) -> dict[str, Any]:
        return await _knowledge(_service(request).reindex_document(document_id))

    @app.delete("/api/documents/{document_id}")
    async def remove_document(request: Request, document_id: UUID) -> dict[str, Any]:
        removed = await _knowledge(_service(request).delete_document(document_id))
        return {"removed": removed}

    @app.get("/api/documents/search")
    async def search_documents(request: Request, q: str, limit: int = 5) -> dict[str, Any]:
        """What an employee would be given for this question, shown to a person."""
        return {"passages": await _guarded(_service(request).search_documents(q, limit=limit))}

    # --- Workspaces -----------------------------------------------------------

    @app.get("/api/workspaces")
    async def workspaces(request: Request) -> dict[str, Any]:
        return {"workspaces": await _guarded(_service(request).list_workspaces())}

    @app.post("/api/workspaces", status_code=201)
    async def add_workspace(request: Request, body: NewWorkspace) -> dict[str, Any]:
        return await _workspace(
            _service(request).create_workspace(
                body.name, description=body.description, file_root=body.file_root
            )
        )

    @app.patch("/api/workspaces/{workspace_id}")
    async def edit_workspace(
        request: Request, workspace_id: str, body: WorkspaceEdit
    ) -> dict[str, Any]:
        return await _workspace(
            _service(request).update_workspace(
                WorkspaceId(workspace_id),
                name=body.name,
                description=body.description,
                file_root=body.file_root,
                folders=body.folders,
            )
        )

    @app.post("/api/workspaces/{workspace_id}/use")
    async def use_workspace(request: Request, workspace_id: str) -> dict[str, Any]:
        """Switch the machine. Work already running keeps the workspace it began in."""
        return await _workspace(_service(request).use_workspace(WorkspaceId(workspace_id)))

    @app.delete("/api/workspaces/{workspace_id}")
    async def remove_workspace(request: Request, workspace_id: str) -> dict[str, Any]:
        removed = await _workspace(
            _service(request).delete_workspace(WorkspaceId(workspace_id))
        )
        return {"removed": removed}

    # --- Providers, models and where work goes --------------------------------

    @app.get("/api/providers")
    async def providers(request: Request) -> dict[str, Any]:
        """Everything the settings page shows, in one request.

        One call rather than four, because these are read together and shown
        together: a page that renders connections before it knows which models
        depend on them has to re-render, and a person watching that sees the
        settings change under their hands.
        """
        service = _service(request)
        return {
            "kinds": await _settings_change(service.list_provider_kinds()),
            "connections": await _settings_change(service.list_connections()),
            "models": await _settings_change(service.list_models()),
            "defaults": await _settings_change(service.list_task_defaults()),
            "guide": await _settings_change(service.provider_guide()),
        }

    @app.post("/api/providers/setups/{setup_id}/apply")
    async def apply_setup(request: Request, setup_id: str, body: SetupChoice) -> dict[str, Any]:
        """One click from a recommendation to a working catalog and routing."""
        return await _settings_change(
            _service(request).apply_provider_setup(setup_id, body.connection)
        )

    @app.post("/api/providers/connections", status_code=201)
    async def add_connection(request: Request, body: NewConnection) -> dict[str, Any]:
        return await _settings_change(
            _service(request).add_connection(
                body.name,
                body.kind,
                api_key=body.api_key,
                base_url=body.base_url,
                description=body.description,
            )
        )

    @app.put("/api/providers/connections/{name}/key")
    async def replace_connection_key(
        request: Request, name: str, body: NewKey
    ) -> dict[str, Any]:
        return await _settings_change(_service(request).replace_connection_key(name, body.api_key))

    @app.delete("/api/providers/connections/{name}")
    async def remove_connection(request: Request, name: str) -> dict[str, Any]:
        await _settings_change(_service(request).remove_connection(name))
        return {"removed": True}

    @app.get("/api/providers/connections/{name}/installed")
    async def installed_models(request: Request, name: str) -> dict[str, Any]:
        """What the runner behind this connection already has.

        Never an error for a runner that is not running or a provider that
        cannot be asked: the body says which of those it is, and the page offers
        a text field beside the reason rather than instead of one.
        """
        return await _settings_change(_service(request).list_installed_models(name))

    @app.post("/api/providers/models", status_code=201)
    async def add_model(request: Request, body: NewModel) -> dict[str, Any]:
        return await _settings_change(
            _service(request).add_model(
                body.name,
                body.provider,
                body.model,
                connection=body.connection,
                capabilities=body.capabilities,
                context_tokens=body.context_tokens,
                input_cost_per_1k_usd=body.input_cost_per_1k_usd,
                output_cost_per_1k_usd=body.output_cost_per_1k_usd,
                quality=body.quality,
                dimensions=body.dimensions,
            )
        )

    @app.delete("/api/providers/models/{name}")
    async def remove_model(request: Request, name: str) -> dict[str, Any]:
        await _settings_change(_service(request).remove_model(name))
        return {"removed": True}

    @app.put("/api/providers/defaults")
    async def send_work_to(request: Request, body: WorkRouting) -> dict[str, Any]:
        return {
            "defaults": await _settings_change(
                _service(request).send_work_to(body.task_kind, body.entry_name)
            )
        }

    @app.delete("/api/providers/defaults/{task_kind}")
    async def clear_task_default(request: Request, task_kind: str) -> dict[str, Any]:
        return {"defaults": await _settings_change(_service(request).clear_task_default(task_kind))}

    # --- Work that starts on its own --------------------------------------------

    @app.get("/api/workflows")
    async def workflows(request: Request) -> dict[str, Any]:
        return await _settings_change(_service(request).list_workflows())

    @app.post("/api/workflows/{name}/dry-run")
    async def dry_run_workflow(
        request: Request, name: str, body: WorkflowInvocation
    ) -> dict[str, Any]:
        return await _settings_change(
            _service(request).dry_run_workflow(
                name, version=body.version, inputs=body.inputs
            )
        )

    @app.post("/api/workflows/{name}/run", status_code=201)
    async def run_workflow(
        request: Request, name: str, body: WorkflowInvocation
    ) -> dict[str, Any]:
        return await _settings_change(
            _service(request).run_workflow_now(
                name, version=body.version, inputs=body.inputs
            )
        )

    @app.get("/api/schedules")
    async def schedules(request: Request) -> dict[str, Any]:
        return await _settings_change(_service(request).list_schedules())

    @app.post("/api/schedules", status_code=201)
    async def add_schedule(request: Request, body: NewSchedule) -> dict[str, Any]:
        return await _settings_change(
            _service(request).create_schedule(
                body.request,
                name=body.name,
                every_minutes=body.every_minutes,
                daily_at=body.daily_at,
                utc_offset_minutes=body.utc_offset_minutes,
                timezone=body.timezone,
                on_event=body.on_event,
                conversation_id=body.conversation_id,
                model=body.model,
                approvals=body.approvals,
                workflow_name=body.workflow_name,
                workflow_version=body.workflow_version,
                workflow_inputs=body.workflow_inputs,
            )
        )

    @app.put("/api/schedules/{schedule_id}")
    async def change_schedule(
        request: Request, schedule_id: UUID, body: ScheduleForm
    ) -> dict[str, Any]:
        return await _settings_change(
            _service(request).update_schedule(
                schedule_id,
                body.request,
                name=body.name,
                every_minutes=body.every_minutes,
                daily_at=body.daily_at,
                utc_offset_minutes=body.utc_offset_minutes,
                timezone=body.timezone,
                on_event=body.on_event,
                model=body.model,
                approvals=body.approvals,
                workflow_name=body.workflow_name,
                workflow_version=body.workflow_version,
                workflow_inputs=body.workflow_inputs,
            )
        )

    @app.patch("/api/schedules/{schedule_id}")
    async def edit_schedule(
        request: Request, schedule_id: UUID, body: ScheduleEdit
    ) -> dict[str, Any]:
        return await _settings_change(
            _service(request).set_schedule_enabled(schedule_id, body.enabled)
        )

    @app.delete("/api/schedules/{schedule_id}")
    async def remove_schedule(request: Request, schedule_id: UUID) -> dict[str, Any]:
        return {"removed": await _settings_change(_service(request).delete_schedule(schedule_id))}

    @app.post("/api/schedules/{schedule_id}/run", status_code=201)
    async def run_schedule(request: Request, schedule_id: UUID) -> dict[str, Any]:
        """Ask for it now, into its own thread - how a person checks it does what they meant."""
        return await _settings_change(_service(request).run_schedule_now(schedule_id))

    # --- General settings -----------------------------------------------------

    @app.get("/api/settings")
    async def general_settings(request: Request) -> dict[str, Any]:
        return await _settings_change(_service(request).list_settings())

    @app.put("/api/settings")
    async def change_general_settings(request: Request, body: SettingsChange) -> dict[str, Any]:
        return await _settings_change(_service(request).change_settings(body.values))

    @app.post("/api/settings/reset")
    async def reset_general_settings(request: Request, body: SettingsReset) -> dict[str, Any]:
        return await _settings_change(_service(request).reset_settings(body.keys))

    # --- Integrations ---------------------------------------------------------

    @app.get("/api/integrations")
    async def integrations(request: Request) -> dict[str, Any]:
        service = _service(request)
        if not service.integrations_available:
            # Not an error: a machine with the feature off is a legitimate
            # configuration, and an interface needs to know the difference
            # between "none connected" and "cannot connect any".
            return {"available": False, "integrations": []}
        return {
            "available": True,
            "integrations": await _guarded(service.list_integrations()),
        }

    @app.post("/api/integrations", status_code=201)
    async def add_integration(request: Request, body: NewIntegration) -> dict[str, Any]:
        try:
            return await _guarded(
                _service(request).add_integration(
                    body.name,
                    body.configuration,
                    kind=body.kind,
                    capabilities=body.capabilities,
                    secret_names=body.secret_names,
                )
            )
        except PrometheusError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/integrations/{integration_id}")
    async def integration_detail(request: Request, integration_id: UUID) -> dict[str, Any]:
        found = await _guarded(_service(request).get_integration(integration_id))
        return _found(found, f"Unknown integration: {integration_id}")

    @app.post("/api/integrations/{integration_id}/connect")
    async def connect_integration(request: Request, integration_id: UUID) -> dict[str, Any]:
        return await _integration(_service(request).connect_integration(integration_id))

    @app.post("/api/integrations/{integration_id}/enable")
    async def enable_integration(request: Request, integration_id: UUID) -> dict[str, Any]:
        return await _integration(_service(request).enable_integration(integration_id))

    @app.post("/api/integrations/{integration_id}/disable")
    async def disable_integration(request: Request, integration_id: UUID) -> dict[str, Any]:
        return await _integration(_service(request).disable_integration(integration_id))

    @app.post("/api/integrations/{integration_id}/capabilities")
    async def classify(
        request: Request, integration_id: UUID, body: Classification
    ) -> dict[str, Any]:
        try:
            return await _integration(
                _service(request).classify_capability(integration_id, body.effects)
            )
        except PrometheusError as error:
            # An effect this platform does not have is the caller being wrong,
            # and the message says what the vocabulary actually is.
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.delete("/api/integrations/{integration_id}")
    async def remove_integration(request: Request, integration_id: UUID) -> dict[str, Any]:
        removed = await _integration(_service(request).remove_integration(integration_id))
        return {"removed": removed}

    @app.post("/api/integrations/{integration_id}/sign-in")
    async def sign_in_integration(request: Request, integration_id: UUID) -> dict[str, Any]:
        """Start - or confirm - a plugin's browser sign-in. See the facade."""
        try:
            return await _integration(_service(request).sign_in_integration(integration_id))
        except NotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.put("/api/integrations/{integration_id}/grants")
    async def grant_integration(
        request: Request, integration_id: UUID, body: Grants
    ) -> dict[str, Any]:
        return await _integration(
            _service(request).grant_integration(integration_id, body.employees)
        )

    # --- Plugins --------------------------------------------------------------

    @app.get("/api/plugins")
    async def plugins(request: Request) -> dict[str, Any]:
        """Everything installable, everything installed, and what this machine lacks."""
        return await _guarded(_service(request).list_plugins())

    @app.post("/api/plugins/{plugin_id}/install", status_code=201)
    async def install_plugin(
        request: Request, plugin_id: str, body: PluginInstall
    ) -> dict[str, Any]:
        try:
            return await _integration(
                _service(request).install_plugin(
                    plugin_id, body.values, employees=body.employees
                )
            )
        except PluginConfigurationError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except DuplicateIntegrationError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except NotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post("/api/credentials", status_code=201)
    async def store_credential(request: Request, body: NewCredential) -> dict[str, Any]:
        """Keep a credential. The value goes in and never comes back out.

        There is deliberately no route that reads one: a credential is resolved
        inside the tool that needs it, at the moment of the call, and an
        endpoint that returned one would be a way to lift every secret on the
        machine out through a window (§74).
        """
        try:
            return await _guarded(_service(request).store_credential(body.name, body.value))
        except PrometheusError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/spend")
    async def spend(request: Request) -> dict[str, Any]:
        return await _guarded(_service(request).spend())

    @app.get("/api/events")
    async def events(
        request: Request, task: UUID | None = None, objective: UUID | None = None
    ) -> StreamingResponse:
        service = _service(request)
        activity = (
            service.objective_activity(objective)
            if objective is not None
            else service.task_activity(task)
        )
        return StreamingResponse(
            _sse(request, activity),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )


# --- The stream ---------------------------------------------------------------


async def _sse(
    request: Request, activity: AsyncIterator[ActivityEvent | None]
) -> AsyncIterator[str]:
    """Frame what the application layer is already saying.

    Everything about *what* is streamed - the replay, following an objective
    across its tasks, ending when the work does - belongs to `Activity`. What is
    left here is the wire format and the one thing only a transport knows: that
    a quiet connection needs a keep-alive, and that a client which has gone away
    should stop the iteration rather than be written to.
    """
    async for event in activity:
        if await request.is_disconnected():
            return
        yield HEARTBEAT if event is None else f"data: {json.dumps(event.to_dict())}\n\n"


# --- Helpers ------------------------------------------------------------------


def _service(request: Request) -> PrometheusService:
    return request.app.state.service


def _found(value: dict[str, Any] | None, missing: str) -> dict[str, Any]:
    if value is None:
        raise HTTPException(status_code=404, detail=missing)
    return value


async def _ask(
    request: Request, body: NewObjective, *, conversation_id: UUID | None
) -> dict[str, Any]:
    """Submit a request, in the workspace it names or the one this machine is in.

    Resolving the default here rather than inside the core keeps `UserRequest`
    what it is: everything the platform needs, stated by the adapter. A surface
    with no workspace selector - the CLI, a schedule, a bot - says nothing and
    gets the active one, exactly as a person at the terminal would expect.
    """
    service = _service(request)
    named = body.workspace_id
    if named is None:
        active = await _guarded(service.active_workspace())
        named = active["id"] if active else str(DEFAULT_WORKSPACE_ID)
    try:
        return await _guarded(
            service.submit(
                UserRequest(
                    content=body.request,
                    source=body.source,
                    input_type=body.input_type,
                    conversation_id=conversation_id,
                    workspace_id=WorkspaceId(named),
                    directions=Directions(
                        approvals=body.approvals,
                        model=body.model.strip(),
                        folder=body.folder.strip(),
                    ),
                )
            )
        )
    except PrometheusError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


async def _knowledge(awaitable):
    """One document operation, with its refusals turned into answers.

    A file that is not there, or a format nothing here reads, is a 400: the
    request was understood and cannot be carried out, and the message says
    which of the two it was. Knowledge switched off is a 409, like every other
    capability a machine is configured without.
    """
    from application.interface.service import KnowledgeDisabledError

    try:
        return await _guarded(awaitable)
    except KnowledgeDisabledError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except NotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PrometheusError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


async def _workspace(awaitable):
    """One workspace operation, with its refusals turned into answers.

    An unknown workspace is a 404 - a window left open across a removal is an
    ordinary thing - and the two ways of asking for something impossible (a
    name already taken, the first workspace) are 409s: the request was well
    formed and the platform will not do it.
    """
    from application.interface.service import WorkspacesDisabledError

    try:
        return await _guarded(awaitable)
    except WorkspaceNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (DuplicateWorkspaceError, ProtectedWorkspaceError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except FolderError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except WorkspacesDisabledError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


async def _integration(awaitable):
    """One integration operation, with its two refusals turned into answers.

    An unknown id is a 404 because a window left open across a removal is an
    ordinary thing; a machine with integrations switched off is a 409, because
    the request was well formed and the platform is configured not to do it.
    """
    from application.interface.service import IntegrationsDisabledError

    try:
        return await _guarded(awaitable)
    except IntegrationNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except IntegrationsDisabledError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


async def _settings_change(awaitable):
    """A settings request answered with a status a page can act on.

    Configuration mistakes here are the ordinary case rather than the exception:
    a name already taken, a kind this machine cannot talk to, a connection three
    models depend on. Each of those is something the person can fix in the form
    they are looking at, so it comes back as a 400 with the sentence the
    application layer wrote - not as a 500, which tells them to read a log.
    """
    try:
        return await _guarded(awaitable)
    except NotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PrometheusError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


async def _memory(awaitable):
    """One memory change, with its refusals turned into answers.

    Memory or forgetting switched off is a 409, like every other capability a
    machine is configured without; a note with nothing in it is a 400.
    """
    from application.interface.service import MemoryDisabledError

    try:
        return await _guarded(awaitable)
    except MemoryDisabledError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


async def _guarded(awaitable):
    """Turn "there is no schema yet" into an answer instead of a stack trace."""
    try:
        return await awaitable
    except StorageNotInitializedError as error:
        raise HTTPException(
            status_code=503,
            detail=f"{error} Run: uv run alembic upgrade head",
        ) from error
