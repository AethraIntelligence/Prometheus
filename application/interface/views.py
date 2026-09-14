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

from typing import Any

from domain.approvals.models import Approval, ApprovalRequest
from domain.configuration.models import Setting
from domain.conversations.models import Conversation
from domain.employees.definition import EmployeeDefinition
from domain.integrations.catalog import FieldKind, Plugin
from domain.integrations.models import Integration
from domain.integrations.specs import spec_for
from domain.knowledge.models import Document, Passage
from domain.llm.catalog import ModelEntry
from domain.memory.models import MemoryItem
from domain.policies.risk import at_least
from domain.policies.rules import APPROVAL_THRESHOLD
from domain.providers.guide import ProviderGuide
from domain.providers.models import Connection, InstalledModels
from domain.tasks.task import Task, TaskEvent
from domain.tools.models import ToolSpec
from domain.tools.telemetry import ToolCallRecord
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


def approval(request: ApprovalRequest, *, live: bool = True) -> dict[str, Any]:
    """A question waiting on a person.

    `live` says whether this process is the one parked on the answer. A PENDING
    row left behind by a killed run can still be recorded as decided, but no
    tool call is going to resume from it, and saying so beats implying it.
    """
    safe = request.redacted()
    return {
        "id": str(safe.id),
        "task_id": str(safe.task_id),
        "action": safe.action,
        "risk": safe.risk_level.value,
        "reason": safe.reason,
        "payload": safe.payload,
        "requested_at": safe.requested_at.isoformat(),
        "live": live,
    }


def stored_approval(record: Approval, *, live: bool = False) -> dict[str, Any]:
    return {**approval(record.request, live=live), "state": record.state.value}


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
        "messages": messages,
        "status": status,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def message(item: Objective, *, thinking: bool = False) -> dict[str, Any]:
    """One turn of a conversation: what was asked, and what came back.

    A turn is an objective, so this is the objective summary plus the two things
    a chat view needs and a history list does not - the answer's text, and
    whether there is an answer yet. There is no separate message record; see
    `domain/conversations/models.py` for why one would be a second history of
    the same work.
    """
    answer = item.result
    return {
        **objective_summary(item, thinking=thinking),
        "answer": answer.summary if answer else "",
        "missing": list(answer.missing) if answer else [],
        "answered": answer is not None,
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
    whether something is true of this workspace or of them. Nothing here is
    derived - the ranking that decided this item came back at all happened in
    the domain, and re-scoring it for display would be a second opinion.
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
