"""Domain values as JSON, in one place.

Kept apart from the routes so the shape the browser sees is defined once and can
be read without reading the server. It is a projection, not a serialization
format: the interface shows what a person needs to judge a run - what it did,
what it cost, what went wrong - and nothing that only the runtime cares about.

It sits in `application/` rather than in one interface's package because the
shape is a property of the platform, not of HTTP: a desktop shell, a page and a
chat bot showing three different summaries of the same run would be three
answers to a question with one answer.

Two omissions are deliberate. The transcript is not exposed: it is the model's
working memory, it is large, and a page that renders it becomes a log viewer,
which is exactly the thing Phase 6 exists to stop the developer reading. And
nothing here reaches into `execution.state`, so the interface does not become a
second consumer of the resume cursor's shape.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from domain.approvals.models import Approval, ApprovalRequest, CapabilityLease
from domain.configuration.models import Setting
from domain.conversations.models import Conversation
from domain.conversations.session import SessionBrief, SessionNote
from domain.employees.definition import EmployeeDefinition
from domain.integrations.catalog import FieldKind, Plugin
from domain.integrations.models import Integration
from domain.integrations.specs import spec_for
from domain.knowledge.models import Document, Passage
from domain.llm.catalog import ModelEntry
from domain.llm.telemetry import LLMCallRecord
from domain.memory.models import MemoryItem
from domain.memory.usage import MemoryUse
from domain.policies.risk import at_least
from domain.policies.rules import APPROVAL_THRESHOLD
from domain.providers.guide import ProviderGuide
from domain.providers.models import Connection, InstalledModels
from domain.scheduling.models import Schedule
from domain.tasks.task import Task, TaskEvent
from domain.tools.models import ToolSpec
from domain.tools.telemetry import ToolCallRecord
from domain.workflows.definition import WorkflowDefinition
from domain.workflows.run import WorkflowRun
from domain.workforce.protocols import Objective, Plan
from domain.workspace.models import Workspace


def task_summary(task: Task, *, running: bool = False) -> dict[str, Any]:
    """One line of history: enough to list, not enough to inspect."""
    return {
        "id": str(task.id),
        "goal": task.goal,
        "status": task.status.value,
        "running": running,
        "step": task.execution.step,
        "cost_usd": round(task.cost_usd, 6),
        "attempts": task.attempts,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
        "employee_id": str(task.assigned_employee_id) if task.assigned_employee_id else None,
    }


def task_detail(
    task: Task,
    *,
    running: bool = False,
    calls: list[ToolCallRecord] | None = None,
    events: list[TaskEvent] | None = None,
    employee: str | None = None,
) -> dict[str, Any]:
    """A past or present run, opened.

    The tool calls come from the stored log rather than the live stream, which
    is what makes an execution from last week open the same way as the one
    running now.
    """
    return {
        **task_summary(task, running=running),
        "employee": employee,
        "plan": task.plan.to_dict() if task.plan else None,
        "result": (
            {
                "summary": task.result.summary,
                "artifacts": list(task.result.artifacts),
            }
            if task.result
            else None
        ),
        "error": (
            {"kind": task.error.kind, "message": task.error.message} if task.error else None
        ),
        "calls": [tool_call(call) for call in calls or ()],
        "events": [
            {
                "from": event.from_status.value if event.from_status else None,
                "to": event.to_status.value,
                "at": event.created_at.isoformat(),
            }
            for event in events or ()
        ],
    }


def tool_call(call: ToolCallRecord) -> dict[str, Any]:
    """One action, as it was recorded - arguments already redacted on the way in."""
    return {
        "tool": call.tool,
        "success": call.success,
        "interface": call.interface.value,
        "latency_ms": call.latency_ms,
        "arguments": call.input_data,
        "output": call.output,
        "error": call.error,
        "at": call.created_at.isoformat(),
    }


def approval(
    request: ApprovalRequest,
    *,
    live: bool = True,
    conversation_id: UUID | None = None,
    subject_name: str = "",
) -> dict[str, Any]:
    """A question waiting on a person.

    `live` says whether this process is the one parked on the answer. A PENDING
    row left behind by a killed run can still be recorded as decided, but no
    tool call is going to resume from it, and saying so beats implying it.

    `conversation_id` is the thread whose work asked, so an interface can put
    the question where that work is read. A scheduled run asking while the
    person was starting a new task put it in the new task, which had not asked
    anything. None where the work belongs to no thread - a terminal, a script.
    """
    safe = request.redacted()
    return {
        "id": str(safe.id),
        "task_id": str(safe.task_id),
        "action": safe.action,
        "risk": safe.risk_level.value,
        "reason": safe.reason,
        "payload": safe.payload,
        "scope": (
            {
                "subject": safe.scope.subject,
                "subject_name": subject_name or safe.scope.subject,
                "action": safe.scope.action,
                "resource": safe.scope.resource,
                "limits": safe.scope.limits,
            }
            if safe.scope
            else None
        ),
        "preview": safe.preview,
        "policy_source": safe.policy_source,
        "requires_explicit_confirmation": safe.requires_explicit_confirmation,
        "context_sources": list(safe.context_sources),
        "requested_at": safe.requested_at.isoformat(),
        "live": live,
        "conversation_id": str(conversation_id) if conversation_id else None,
    }


def stored_approval(
    record: Approval,
    *,
    live: bool = False,
    conversation_id: UUID | None = None,
    subject_name: str = "",
) -> dict[str, Any]:
    return {
        **approval(
            record.request,
            live=live,
            conversation_id=conversation_id,
            subject_name=subject_name,
        ),
        "state": record.state.value,
        "grant": record.grant.value,
        "lease_id": str(record.lease_id) if record.lease_id else None,
    }


def inbox_approval(
    record: Approval,
    *,
    live: bool,
    task: Task | None = None,
    objective_id: UUID | None = None,
    conversation_id: UUID | None = None,
    subject_name: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """One decision as a person supervising all work needs to see it."""
    moment = now or datetime.now(UTC)
    age_seconds = max(0, int((moment - record.request.requested_at).total_seconds()))
    if age_seconds < 60:
        wait_group = "NEW"
    elif age_seconds < 300:
        wait_group = "WAITING"
    else:
        wait_group = "LONG_WAIT"
    resource = (
        record.request.scope.resource
        if record.request.scope is not None
        else record.request.tool or "General action"
    )
    actionable = record.is_pending and live
    if actionable:
        status_explanation = "The employee is waiting for your decision."
    elif record.is_pending:
        status_explanation = "No active action is waiting on this request."
    elif record.state.value == "APPROVED":
        status_explanation = "Approved. The exact permitted action was released."
    elif record.state.value == "REJECTED":
        status_explanation = "Rejected. The action was not performed."
    else:
        status_explanation = "Expired. Nothing can execute from this request."
    return {
        **stored_approval(
            record,
            live=live,
            conversation_id=conversation_id,
            subject_name=subject_name,
        ),
        "objective_id": str(objective_id) if objective_id else None,
        "task_goal": task.goal if task else "",
        "task_status": task.status.value if task else None,
        "resource_group": resource,
        "wait_seconds": age_seconds,
        "wait_group": wait_group,
        "actionable": actionable,
        "approve_effect": (
            "The employee will perform this exact action within the displayed scope."
            if actionable
            else "This request can no longer release an action."
        ),
        "reject_effect": (
            "The action will not run; the employee will continue or finish without it."
            if actionable
            else "This request is already final."
        ),
        "status_explanation": status_explanation,
        "expires_at": (
            record.request.expires_at.isoformat() if record.request.expires_at else None
        ),
        "resolved_at": record.resolved_at.isoformat() if record.resolved_at else None,
        "resolved_by": record.resolved_by,
        "comment": record.comment,
    }


def capability_lease(lease: CapabilityLease, *, subject_name: str = "") -> dict[str, Any]:
    return {
        "id": str(lease.id),
        "workspace_id": str(lease.workspace_id),
        "subject": lease.scope.subject,
        "subject_name": subject_name or lease.scope.subject,
        "action": lease.scope.action,
        "resource": lease.scope.resource,
        "limits": lease.scope.limits,
        "grant": lease.grant.value,
        "reason": lease.reason,
        "task_id": str(lease.task_id) if lease.task_id else None,
        "approval_id": str(lease.approval_id),
        "created_at": lease.created_at.isoformat(),
        "expires_at": lease.expires_at.isoformat() if lease.expires_at else None,
    }


def employee(definition: EmployeeDefinition) -> dict[str, Any]:
    return {
        "id": str(definition.id),
        "name": definition.name,
        "title": definition.role.title,
        "description": definition.role.description,
        "tools": sorted(definition.allowed_tools),
        "integrations": sorted(definition.integrations),
        "capabilities": sorted(str(c) for c in definition.capabilities),
        "limits": {
            "max_steps": definition.limits.max_steps,
            "max_cost_usd": definition.limits.max_cost_usd,
            "max_wall_time_seconds": definition.limits.max_wall_time_seconds,
        },
    }


# --- Integrations -------------------------------------------------------------


def integration_capability(item: Integration, tool) -> dict[str, Any]:
    """One thing an integration offers, with the platform's verdict attached.

    `requires_approval` is computed here and sent, rather than left for an
    interface to work out from the effect. An interface that decided which
    actions are dangerous would be a second policy engine - one written in
    TypeScript, disagreeing silently with the real one, and applied only on the
    surfaces that remembered to implement it. The rule is the same rule the
    gate applies, read from the same threshold.
    """
    spec = spec_for(item, tool)
    return {
        "name": tool.name,
        "qualified_name": spec.name,
        "description": tool.description,
        "effect": spec.effect.value,
        "risk": spec.risk_level.value,
        "requires_approval": at_least(spec.risk_level, APPROVAL_THRESHOLD),
        "classified": tool.name in item.effects,
    }


def integration(item: Integration) -> dict[str, Any]:
    """One connected service, as a person needs to see it.

    No credential and no part of one. `secrets` is the *names* of what it needs,
    because a person setting one up has to know which are missing, and a name is
    not a secret.
    """
    return {
        "id": str(item.id),
        "name": item.name,
        "kind": item.kind.value,
        "status": item.status.value,
        "enabled": item.enabled,
        "usable": item.is_usable,
        "capabilities": sorted(str(c) for c in item.granted_capabilities),
        "secrets": list(item.secret_names),
        "tool_count": len(item.discovered),
        "tools": [integration_capability(item, tool) for tool in item.discovered],
    }


def installed_integration(
    item: Integration, *, plugin: str = "", holders: tuple[str, ...] = ()
) -> dict[str, Any]:
    """A connected service as the Plugins screen shows it.

    `plugin` is the catalog entry it came from, or empty for a server somebody
    added by hand. `holders` is everyone who may use it, however they were
    granted - the file or the window - and `granted_to` is the window's half
    alone, which is the half a person can change there.
    """
    return {
        **integration(item),
        "plugin": plugin,
        "holders": list(holders),
        "granted_to": sorted(item.granted_to),
    }


# --- Plugins ------------------------------------------------------------------


def plugin(
    item: Plugin,
    *,
    installed: Integration | None,
    runtime_ready: bool,
    suggested: list[str],
    stored: set[str],
    provided: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """One installable service, and whether this machine has it.

    `settings` carries `stored` for each secret - whether a value is already
    kept under that name - so reinstalling after a removal does not ask for a
    token the machine already holds. Never the value.
    """
    return {
        "id": item.id,
        "name": item.name,
        "description": item.description,
        "about": item.about,
        "category": item.category,
        "publisher": item.publisher,
        "homepage": item.homepage,
        "popular": item.popular,
        "runtime": item.runtime.value,
        "runtime_ready": runtime_ready,
        "icon": (
            {
                "view_box": item.icon.view_box,
                "paths": list(item.icon.paths),
                "color": item.icon.color,
                "background": item.icon.background,
            }
            if item.icon
            else None
        ),
        "capabilities": sorted(str(c) for c in item.capabilities),
        "settings": [
            {
                "key": field.key,
                "label": field.label,
                "kind": field.kind.value,
                "required": field.required,
                "placeholder": field.placeholder,
                "help": field.help,
                "help_url": field.help_url,
                "stored": field.kind is FieldKind.SECRET and field.key in stored,
                # The installation supplies it, so a person may leave it empty;
                # typing their own replaces it for this machine.
                "provided": field.kind is FieldKind.SECRET and field.key in provided,
            }
            for field in item.fields
        ],
        "setup": [{"text": step.text, "url": step.url} for step in item.setup],
        "sign_in": (
            {"label": item.sign_in.label, "help": item.sign_in.help} if item.sign_in else None
        ),
        "suggested_employees": suggested,
        "installed": str(installed.id) if installed else "",
        "status": installed.status.value if installed else "",
    }


# --- Tools --------------------------------------------------------------------


def tool(spec: ToolSpec, *, used_by: tuple[str, ...] = ()) -> dict[str, Any]:
    """One thing this machine can do, and who is allowed to ask for it.

    `used_by` is the employees that listed the tool in their own declaration,
    and it is the whole answer to whether the tool is in use: a tool nobody
    lists is a tool nobody can call. There is deliberately no enabled flag -
    a grant is a line in an employee's file, and a switch on a screen would be
    a second place to change it, which is the one that goes stale (§71c).

    `requires_approval` is computed here for the same reason it is computed for
    an integration's capability: an interface that worked it out from the
    effect would be a second policy engine, disagreeing quietly with the gate.
    """
    return {
        "name": spec.name,
        # The first line only. The rest of a tool's description is written for
        # the model that has to call it correctly, not for a person reading a
        # list of what their machine can do.
        "description": spec.description.splitlines()[0] if spec.description else "",
        "effect": spec.effect.value,
        "risk": spec.risk_level.value,
        "requires_approval": at_least(spec.risk_level, APPROVAL_THRESHOLD),
        "interface": spec.interface_level.value,
        "reversible": spec.reversible,
        "capabilities": sorted(str(c) for c in spec.capabilities),
        "used_by": list(used_by),
    }


# --- The manager --------------------------------------------------------------


def objective_summary(item: Objective, *, thinking: bool = False) -> dict[str, Any]:
    """One line of what has been asked for."""
    return {
        "id": str(item.id),
        "text": item.text,
        "status": item.status.value,
        "thinking": thinking,
        "created_at": item.created_at.isoformat(),
        "finished_at": item.finished_at.isoformat() if item.finished_at else None,
        "cost_usd": round(item.result.cost_usd, 6) if item.result else 0.0,
    }


def objective_detail(
    item: Objective, *, thinking: bool = False, plans: list[Plan] | None = None
) -> dict[str, Any]:
    """One request, opened: what Prometheus made of it, and what it did about it.

    Every revision is shown, not only the last. A superseded plan is the only
    evidence of why a second attempt was needed, and hiding it would leave the
    user reading an answer with no account of how it was arrived at.
    """
    return {
        **objective_summary(item, thinking=thinking),
        "constraints": item.constraints,
        "acceptance_criteria": list(item.acceptance_criteria),
        "result": (
            {
                "summary": item.result.summary,
                "missing": list(item.result.missing),
                "output": item.result.output,
            }
            if item.result
            else None
        ),
        "plans": [plan_view(plan) for plan in plans or ()],
    }


def plan_view(plan: Plan) -> dict[str, Any]:
    return {
        "id": str(plan.id),
        "revision": plan.revision,
        "status": plan.status.value,
        "rationale": plan.rationale,
        "tasks": [
            {
                "id": str(task.id),
                "goal": task.goal,
                "status": task.status.value,
                "cost_usd": round(task.cost_usd, 6),
                "depends_on": [str(other) for other in sorted(plan.depends_on(task.id))],
            }
            for task in plan.tasks
        ],
    }


# --- Work Center -------------------------------------------------------------


def work_task(
    task: Task,
    *,
    employee: EmployeeDefinition | None,
    calls: list[ToolCallRecord],
    model_calls: list[LLMCallRecord],
    depends_on: tuple[UUID, ...] = (),
    objective_terminal: bool = False,
) -> dict[str, Any]:
    """One delegated unit, with enough evidence to operate rather than debug it."""
    limits = employee.limits if employee else None
    elapsed = max(
        0.0,
        ((datetime.now(UTC) if not task.is_terminal else task.updated_at) - task.created_at)
        .total_seconds(),
    )
    # One line per model *and reason*: the same model chosen as the planning
    # default and again as an escalation are two different decisions, and a
    # trace that merged them could not show the second.
    models: dict[tuple[str, str, str, str, int], dict[str, Any]] = {}
    for call in model_calls:
        key = (call.provider, call.model, call.task_kind, call.reason, call.escalation_level)
        entry = models.setdefault(
            key,
            {
                "provider": call.provider,
                "model": call.model,
                "entry": call.entry,
                "task_kind": call.task_kind,
                "reason": call.reason,
                "escalation_level": call.escalation_level,
                "calls": 0,
                "failed": 0,
                "cost_usd": 0.0,
            },
        )
        entry["calls"] += 1
        entry["failed"] += 0 if call.success else 1
        entry["cost_usd"] = round(float(entry["cost_usd"]) + call.usage.cost_usd, 6)
    escalated = any(call.escalation_level > 0 for call in model_calls)
    return {
        **task_summary(task),
        "parent_id": str(task.parent_id) if task.parent_id else None,
        "employee": employee.name if employee else "Unassigned",
        "employee_title": employee.role.title if employee else "",
        "assignment_reason": task.assignment_reason or "No assignment explanation was recorded.",
        "depends_on": [str(item) for item in depends_on],
        "current_step": task.execution.step,
        "budgets": {
            "steps": {"used": task.execution.step, "limit": limits.max_steps if limits else None},
            "cost_usd": {
                "used": round(task.cost_usd, 6),
                "limit": limits.max_cost_usd if limits else None,
            },
            "wall_time_seconds": {
                "used": round(elapsed, 1),
                "limit": limits.max_wall_time_seconds if limits else None,
            },
        },
        "models": list(models.values()),
        "escalated": escalated,
        "model_reason": (
            "Ran on a stronger model after an earlier attempt failed in a way a better "
            "model can fix."
            if escalated
            else "Each model below was chosen for the kind of work shown, for the reason shown."
            if model_calls
            else "No model call has been recorded for this task yet."
        ),
        "tools": [
            {
                **tool_call(call),
                "reason": (
                    f"Used {call.tool} through {call.interface.value.lower()} because the "
                    "assigned employee declared this capability."
                ),
            }
            for call in calls
        ],
        "result": (
            {
                "summary": task.result.summary,
                "artifacts": list(task.result.artifacts),
                "evidence": task.result.output,
            }
            if task.result
            else None
        ),
        "error": (
            {
                "kind": task.error.kind,
                "message": task.error.message,
                "details": task.error.details,
            }
            if task.error
            else None
        ),
        "controls": {
            "pause": task.status.value in {"RUNNING", "WAITING_FOR_TOOL"},
            "resume": task.status.value == "PAUSED",
            "cancel": not task.is_terminal,
            "retry": task.status.value in {"FAILED", "CANCELLED"}
            and (task.plan_id is None or objective_terminal),
            "handoff": (
                task.status.value in {"FAILED", "CANCELLED"}
                and (task.plan_id is None or objective_terminal)
            )
            or (task.status.value == "PAUSED" and task.plan_id is None),
        },
    }


def work_item(
    objective: Objective,
    *,
    plans: list[Plan],
    tasks: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    thinking: bool,
) -> dict[str, Any]:
    statuses = {str(task["status"]) for task in tasks}
    if "WAITING_FOR_APPROVAL" in statuses:
        bucket = "WAITING"
        next_action = "Review the requested approval to let work continue."
    elif objective.status.value == "PAUSED" or "PAUSED" in statuses:
        bucket = "BLOCKED"
        next_action = "Resume the paused work, hand it off, or cancel it."
    elif objective.status.value == "ESCALATED" or "WAITING_FOR_TOOL" in statuses:
        bucket = "BLOCKED"
        next_action = "Review the blocker and decide whether to retry or hand off."
    elif objective.status.value == "FAILED":
        bucket = "FAILED"
        next_action = "Inspect the failure evidence, then retry if the cause is resolved."
    elif objective.status.value in {"DONE", "CANCELLED"}:
        bucket = "COMPLETED"
        next_action = "Review the result and its evidence."
    else:
        bucket = "ACTIVE"
        next_action = "No action is needed while the employees continue working."

    current = next(
        (
            task
            for task in reversed(tasks)
            if task["status"] not in {"COMPLETED", "FAILED", "CANCELLED"}
        ),
        tasks[-1] if tasks else None,
    )
    current_plan = next(
        (plan for plan in reversed(plans) if plan.status.value != "SUPERSEDED"),
        plans[-1] if plans else None,
    )
    return {
        **objective_detail(objective, thinking=thinking, plans=plans),
        "conversation_id": str(objective.conversation_id) if objective.conversation_id else None,
        "bucket": bucket,
        "next_action": next_action,
        "current_task_id": current["id"] if current else None,
        "current_step": current["current_step"] if current else 0,
        "plan": plan_view(current_plan) if current_plan else None,
        "tasks": tasks,
        "artifacts": artifacts,
        "controls": {
            "pause": bucket == "ACTIVE" and bool(tasks),
            "resume": objective.status.value == "PAUSED" or "PAUSED" in statuses,
            "cancel": objective.status.value not in {"DONE", "FAILED", "CANCELLED"},
            "retry": objective.status.value in {"FAILED", "ESCALATED", "CANCELLED"},
        },
    }


# --- Conversations ------------------------------------------------------------


def conversation(
    item: Conversation, *, messages: int = 0, status: str | None = None
) -> dict[str, Any]:
    """A thread, as a line in a list of them.

    `status` is where the latest request in it stands, so a list can mark a
    thread that is still working without opening it. It is read off the
    objectives by the caller rather than stored on the conversation: a status
    kept in two places is the one that goes stale.
    """
    return {
        "id": str(item.id),
        "title": item.title,
        "kind": item.kind.value,
        "messages": messages,
        "status": status,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def message(
    item: Objective, *, thinking: bool = False, artifacts: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """One turn of a conversation: what was asked, and what came back.

    A turn is an objective, so this is the objective summary plus the two things
    a chat view needs and a history list does not - the answer's text, and
    whether there is an answer yet. There is no separate message record; see
    `domain/conversations/models.py` for why one would be a second history of
    the same work.

    `artifacts` are the files the work left behind (`artifacts.py`), so a turn
    can show the file and not only a sentence saying it was written.
    """
    answer = item.result
    return {
        **objective_summary(item, thinking=thinking),
        "answer": answer.summary if answer else "",
        "missing": list(answer.missing) if answer else [],
        "answered": answer is not None,
        "directions": directions(item.directions),
        "artifacts": artifacts or [],
    }


# --- Workspaces ---------------------------------------------------------------


def workspace(
    item: Workspace, *, active: bool = False, file_root: str = ""
) -> dict[str, Any]:
    """One separation of contexts, as an interface sees it.

    `file_root` is passed in rather than read off the record because the record
    holds only an override: where a workspace's files actually are is a question
    about this machine, and the process's `WorkspaceContext` is what answers it.
    """
    return {
        "id": str(item.id),
        "name": item.name,
        "description": item.description,
        "file_root": file_root or item.file_root or "",
        "folders": [str(folder) for folder in item.settings.get("folders", [])],
        "is_default": item.is_default,
        "active": active,
        "created_at": item.created_at.isoformat(),
    }


# --- Knowledge ----------------------------------------------------------------


def document(item: Document) -> dict[str, Any]:
    """One thing the user brought, as an interface sees it.

    `searchable` rather than a rule about which statuses count: whether a
    document answers questions is the domain's answer, and an interface that
    derived it from the status string would be the second place that rule lives.
    """
    return {
        "id": str(item.id),
        "title": item.title,
        "source": item.source,
        "media_type": item.media_type,
        "status": item.status.value,
        "searchable": item.is_searchable,
        "needs_indexing": item.needs_indexing,
        "chunks": item.chunk_count,
        "size_bytes": item.size_bytes,
        "error": item.error,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def passage(item: Passage) -> dict[str, Any]:
    """A retrieved quotation, with what it came from and how it was found.

    Both halves of the score are shown. A machine with no embedding model
    retrieves lexically and answers slightly worse, and that should be visible
    in the interface rather than inferred from disappointing answers.
    """
    return {
        "document_id": str(item.chunk.document_id),
        "title": item.title,
        "source": item.source,
        "content": item.chunk.content,
        "score": round(item.score, 4),
        "lexical": round(item.lexical, 4),
        "semantic": round(item.semantic, 4),
    }


# --- Memory -------------------------------------------------------------------


#: `metadata["source"]` of a note a person added themselves, rather than one the
#: platform wrote about its own work.
STATED_BY_PERSON = "person"


def memory_item(item: MemoryItem) -> dict[str, Any]:
    """One thing the platform remembers, as an interface shows it.

    The scope is shown because it answers a question a person actually asks:
    whether something is true of this workspace or of them. Since Phase 9 so is
    what it rests on - basis, confidence, source and status - because the next
    question is whether to believe it. Nothing here is derived: the ranking
    that decided this item came back at all happened in the domain, and
    re-scoring it for display would be a second opinion.
    """
    return {
        "id": str(item.id),
        "kind": item.kind.value,
        "scope": item.scope.value,
        "content": item.content,
        "importance": round(item.importance, 3),
        "created_at": item.created_at.isoformat(),
        "expires_at": item.expires_at.isoformat() if item.expires_at else "",
        "stated": item.metadata.get("source") == STATED_BY_PERSON,
        "basis": item.basis.value,
        "factual": item.is_factual,
        "confidence": round(item.confidence, 3),
        "status": item.status.value,
        "superseded_by": str(item.superseded_by) if item.superseded_by else "",
        "revised_at": item.revised_at.isoformat() if item.revised_at else "",
        "contradicts": [str(other) for other in item.contradicts],
        "source": {
            "kind": item.provenance.kind.value,
            "ref": item.provenance.ref,
            "label": item.provenance.label,
            "derived_from": list(item.provenance.derived_from),
        },
    }


def memory_use(use: MemoryUse, item: MemoryItem | None) -> dict[str, Any]:
    """One recorded use of a memory. `memory` is None where it is not shown here:
    forgotten since, or an employee's private note, whose words stay private."""
    return {
        "memory_id": str(use.memory_id),
        "reader": use.reader,
        "reason": use.reason,
        "weight": round(use.weight, 4),
        "objective_id": str(use.objective_id) if use.objective_id else "",
        "task_id": str(use.task_id) if use.task_id else "",
        "used_at": use.used_at.isoformat(),
        "memory": memory_item(item) if item is not None else None,
    }


def memory_trace(
    item: MemoryItem,
    *,
    superseded_by: MemoryItem | None,
    contradicts: list[MemoryItem],
    derived_from: list[MemoryItem],
    uses: list[MemoryUse],
) -> dict[str, Any]:
    return {
        "item": memory_item(item),
        "superseded_by": memory_item(superseded_by) if superseded_by else None,
        "contradicts": [memory_item(other) for other in contradicts],
        "derived_from": [memory_item(other) for other in derived_from],
        "uses": [memory_use(use, None) for use in uses],
    }


def session_brief(brief: SessionBrief) -> dict[str, Any]:
    """A thread's brief: what it established, and how much of it is compacted."""
    return {
        "conversation_id": str(brief.conversation_id),
        "goal": brief.goal,
        "decisions": [_session_note(note) for note in brief.decisions],
        "open_questions": [_session_note(note) for note in brief.open_questions],
        "artifacts": [
            {
                "path": item.path,
                "objective_id": str(item.objective_id),
                "recorded_at": item.recorded_at.isoformat(),
            }
            for item in brief.artifacts
        ],
        "stages": [
            {
                "index": stage.index,
                "summary": stage.summary,
                "objective_ids": [str(i) for i in stage.objective_ids],
                "started_at": stage.started_at.isoformat(),
                "ended_at": stage.ended_at.isoformat(),
                "compacted_at": stage.compacted_at.isoformat(),
                "summarised": stage.summarised,
            }
            for stage in brief.stages
        ],
        "total_turns": brief.total_turns,
        "compacted_turns": brief.compacted_turns,
        "recent_turns": len(brief.recent),
    }


def _session_note(note: SessionNote) -> dict[str, Any]:
    return {
        "text": note.text,
        "objective_id": str(note.objective_id),
        "recorded_at": note.recorded_at.isoformat(),
    }


def connection(item: Connection, *, has_key: bool) -> dict[str, Any]:
    """One way in to a provider, as a settings page sees it.

    `has_key` and never the key. The value is not returned by any method on
    this boundary and not held in any view: a page that can display a
    credential is a page that puts one in a screenshot, a bug report and a log,
    and there is nothing a person does with a key they already typed except
    replace it.
    """
    return {
        "id": str(item.id),
        "name": item.name,
        "kind": item.kind,
        "base_url": item.base_url,
        "description": item.description,
        "needs_credential": item.needs_credential,
        "has_key": has_key,
        "usable": item.is_usable and (has_key or not item.needs_credential),
    }


def provider_kind(kind: Any) -> dict[str, Any]:
    """One kind of provider this machine can talk to."""
    return {
        "name": kind.name,
        "label": kind.label,
        "needs_credential": kind.needs_credential,
        "default_base_url": kind.default_base_url,
    }


def installed_models(found: InstalledModels) -> dict[str, Any]:
    """What a connection has, and whether the runner said so or its disk did.

    `from_disk` is spelled out rather than left for the page to work out from
    `reachable` and the list: a stopped runner with models on disk is the case
    the window has to explain, and explaining it is not a rule the window should
    be deriving.
    """
    return {
        "models": list(found.names),
        "supported": found.supported,
        "reachable": found.reachable,
        "from_disk": bool(found.names) and not found.reachable,
        "runner": found.runner,
        "address": found.address,
    }


def model_entry(entry: ModelEntry, *, used_for: tuple[str, ...] = ()) -> dict[str, Any]:
    """One catalog entry, with the work that currently goes to it.

    `used_for` is here rather than left to the page because it is the thing a
    person is about to act on: removing an entry that planning depends on is a
    different decision from removing one nothing points at.
    """
    return {
        "name": entry.name,
        "provider": entry.provider,
        "model": entry.model,
        "connection": entry.connection,
        "capabilities": sorted(item.value for item in entry.capabilities),
        "context_tokens": entry.context_tokens,
        "input_cost_per_1k_usd": entry.input_cost_per_1k_usd,
        "output_cost_per_1k_usd": entry.output_cost_per_1k_usd,
        "quality": entry.quality,
        "dimensions": entry.dimensions,
        # The contract routing reads: which quality band escalation moves it
        # in, whether its prompts leave the machine, how long it usually takes.
        "tier": entry.contract.tier,
        "privacy": entry.privacy.value,
        "latency_ms": entry.latency_ms,
        # What it can be given, so a page offers an embedding model for
        # embedding and a chat model for everything else, and never the reverse.
        "embeds": entry.embeds,
        "generates_text": entry.generates_text,
        "used_for": list(used_for),
    }


def setting(item: Setting) -> dict[str, Any]:
    """One switch: what is saved, what is running, and whether it can be changed here."""
    return {
        "key": item.key,
        "group": item.group,
        "label": item.label,
        "help": item.help,
        "kind": item.kind.value,
        "value": _jsonable(item.value),
        "running": _jsonable(item.running),
        "default": _jsonable(item.default),
        "saved": item.saved,
        "choices": list(item.choices),
        "minimum": item.minimum,
        "optional": item.optional,
        "locked_by": item.locked_by,
        "restart_needed": item.restart_needed,
    }


def _jsonable(value: object) -> object:
    return list(value) if isinstance(value, tuple) else value


def provider_guide(
    guide: ProviderGuide,
    *,
    applied: dict[str, bool],
    connections: dict[str, str],
) -> dict[str, Any]:
    """Which provider and models to start with, and how far this machine has got.

    `applied` and `connection` arrive decided per setup, like `used_for` on a
    model: working out in the window whether a setup's models are all present
    would be the catalog's question answered a second time.
    """
    return {
        "intro": guide.intro,
        "checked": guide.checked,
        "setups": [
            {
                "id": setup.id,
                "title": setup.title,
                "badge": setup.badge,
                "summary": setup.summary,
                "kind": setup.kind,
                "connection_name": setup.connection_name,
                "good_for": setup.good_for,
                "connection": connections.get(setup.kind, ""),
                "applied": applied.get(setup.id, False),
                "steps": [
                    {
                        "text": step.text,
                        "action": step.action,
                        "url": step.url,
                        "command": step.command,
                    }
                    for step in setup.steps
                ],
                "models": [
                    {
                        "name": model.name,
                        "role": model.role,
                        "model": model.model,
                        "why": model.why,
                        "capabilities": list(model.capabilities),
                        "context_tokens": model.context_tokens,
                        "free": model.input_cost_per_1k_usd == 0
                        and model.output_cost_per_1k_usd == 0,
                        "input_cost_per_1m_usd": round(model.input_cost_per_1k_usd * 1000, 4),
                        "output_cost_per_1m_usd": round(model.output_cost_per_1k_usd * 1000, 4),
                        "route": list(model.route),
                    }
                    for model in setup.models
                ],
                "cautions": list(setup.cautions),
            }
            for setup in guide.setups
        ],
        "providers": [
            {"kind": item.kind, "label": item.label, "verdict": item.verdict, "text": item.text}
            for item in guide.providers
        ],
        "requirements": [{"title": item.title, "text": item.text} for item in guide.requirements],
    }


def schedule(
    item: Schedule,
    *,
    last_status: str | None = None,
    recent_runs: list[dict[str, Any]] | None = None,
    consecutive_failures: int = 0,
    last_success_at: str | None = None,
) -> dict[str, Any]:
    """A standing request: what, when, and what became of it last time.

    Times go out as UTC ISO strings and the recurrence as its parts. How "09:00
    UTC" reads on the person's own clock is the window's to render - it knows
    the clock - while which moment is meant is the core's.
    """
    recurrence = item.recurrence
    return {
        "id": str(item.id),
        "name": item.name,
        "request": item.request,
        "enabled": item.enabled,
        "every_seconds": recurrence.every_seconds if recurrence else None,
        "daily_at_utc": (
            recurrence.daily_at.isoformat(timespec="minutes")
            if recurrence and recurrence.daily_at and not recurrence.timezone
            else None
        ),
        "daily_at": (
            recurrence.daily_at.isoformat(timespec="minutes")
            if recurrence and recurrence.daily_at
            else None
        ),
        "timezone": recurrence.timezone if recurrence else "",
        "on_event": item.on_event,
        "describe": item.describe(),
        "next_due_at": item.next_due_at.isoformat() if item.next_due_at else None,
        "last_run_at": item.last_run_at.isoformat() if item.last_run_at else None,
        "last_status": last_status,
        "runs": item.runs,
        "conversation_id": str(item.conversation_id) if item.conversation_id else None,
        "model": item.model,
        "approvals": item.approvals.value,
        "created_at": item.created_at.isoformat(),
        "version": item.version,
        "workflow_name": item.workflow_name,
        "workflow_version": item.workflow_version,
        "workflow_inputs": dict(item.workflow_inputs),
        "recent_runs": recent_runs or [],
        "consecutive_failures": consecutive_failures,
        "last_success_at": last_success_at,
        # Explicit product policies. Every surface can explain delay and
        # overlap without reverse-engineering scheduler timing.
        "retry_policy": item.retry_policy.value,
        "overlap_policy": item.concurrency_policy.value,
        "misfire_policy": item.misfire_policy.value,
    }


def schedule_run(
    event: Any,
    objective: Objective | None,
    *,
    workflow_run: WorkflowRun | None = None,
) -> dict[str, Any]:
    """One completed firing, distinct from the instruction that produced it."""
    result = objective.result if objective is not None else None
    return {
        "id": str(event.id),
        "objective_id": str(objective.id) if objective is not None else event.payload.get(
            "objective_id", ""
        ),
        "status": (
            objective.status.value
            if objective is not None
            else workflow_run.status.value
            if workflow_run is not None
            else event.payload.get("status", "FAILED")
        ),
        "started_at": (
            objective.created_at.isoformat()
            if objective is not None
            else workflow_run.started_at.isoformat()
            if workflow_run is not None
            else None
        ),
        "finished_at": (
            objective.finished_at.isoformat()
            if objective is not None and objective.finished_at is not None
            else workflow_run.finished_at.isoformat()
            if workflow_run is not None and workflow_run.finished_at is not None
            else event.created_at.isoformat()
        ),
        "cost_usd": (
            result.cost_usd
            if result is not None
            else workflow_run.cost_usd
            if workflow_run is not None
            else float(event.payload.get("cost_usd") or 0.0)
        ),
        "quality": (
            workflow_run.quality
            if workflow_run is not None
            else float(event.payload.get("quality") or 0.0)
        ),
        "summary": (
            result.summary
            if result is not None
            else workflow_run.summary
            if workflow_run is not None
            else ""
        ),
        "schedule_version": int(event.payload.get("schedule_version") or 1),
        "workflow_name": str(event.payload.get("workflow_name") or ""),
        "workflow_version": int(event.payload.get("workflow_version") or 0) or None,
    }


def workflow_run(run: WorkflowRun) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "workflow": run.workflow,
        "workflow_version": run.workflow_version,
        "trigger": run.trigger.value,
        "inputs": dict(run.inputs),
        "status": run.status.value,
        "summary": run.summary,
        "cost_usd": run.cost_usd,
        "quality": run.quality,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "steps": [step.to_dict() for step in run.steps],
    }


def workflow_definition(
    definition: WorkflowDefinition,
    *,
    readiness: dict[str, Any],
    runs: list[WorkflowRun],
) -> dict[str, Any]:
    succeeded = sum(run.succeeded for run in runs)
    return {
        "name": definition.name,
        "version": definition.version,
        "description": definition.description,
        "trigger": definition.trigger.value,
        "inputs": [
            {
                "name": name,
                "kind": spec.kind.value,
                "required": spec.required,
                "default": definition.inputs.get(name, spec.default),
                "description": spec.description,
            }
            for name, spec in definition.input_schema.items()
        ],
        "profile": {
            "approvals": definition.profile.approvals.value,
            "model": definition.profile.model,
        },
        "budget": {
            "max_steps": definition.budget.max_steps,
            "max_cost_usd": definition.budget.max_cost_usd,
            "max_wall_time_seconds": definition.budget.max_wall_time_seconds,
        },
        "steps": [
            {
                "name": step.name,
                "employee": step.employee,
                "depends_on": list(step.depends_on),
                "max_attempts": step.max_attempts,
                "on_failure": step.on_failure.value,
            }
            for step in definition.steps
        ],
        "readiness": readiness,
        "metrics": {
            "runs": len(runs),
            "success_rate": round(succeeded / len(runs), 4) if runs else None,
            "average_cost_usd": (
                round(sum(run.cost_usd for run in runs) / len(runs), 6)
                if runs
                else None
            ),
        },
        "recent_runs": [workflow_run(run) for run in runs[:5]],
    }


def directions(item: Any) -> dict[str, str]:
    """Approvals, a preferred model and the folder, as the composer shows them."""
    return {
        "approvals": item.approvals.value,
        "model": item.model,
        "folder": getattr(item, "folder", ""),
    }
