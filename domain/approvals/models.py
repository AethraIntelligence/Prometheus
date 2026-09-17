from __future__ import annotations

import hashlib
import json
import posixpath
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from domain.policies.models import RiskLevel
from domain.secrets.models import redact
from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId


class ApprovalState(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class ApprovalGrant(StrEnum):
    """How far one explicit approval reaches."""

    ONCE = "ONCE"
    TASK = "TASK"
    PERSISTENT = "PERSISTENT"


@dataclass(frozen=True, slots=True)
class ApprovalScope:
    """The exact subject, action and resource a decision is allowed to cover."""

    subject: str
    action: str
    resource: str
    limits: dict[str, int | float | str] = field(default_factory=dict)

    def allows(self, requested: ApprovalScope) -> bool:
        if (
            self.subject != requested.subject
            or self.action != requested.action
            or self.resource != requested.resource
        ):
            return False
        for name, requested_value in requested.limits.items():
            allowed = self.limits.get(name)
            if allowed is None:
                return False
            if isinstance(requested_value, (int, float)) and isinstance(allowed, (int, float)):
                if requested_value > allowed:
                    return False
            elif requested_value != allowed:
                return False
        return True


def scope_for(subject: str, action: str, payload: dict[str, Any]) -> ApprovalScope:
    """Build a stable least-privilege scope from one validated tool call."""
    parts: list[str] = []
    path_keys = ("path", "source", "destination", "directory", "folder")
    url_keys = ("url", "uri", "domain", "host")
    identity_keys = (
        "to",
        "recipient",
        "recipients",
        "channel",
        "repository",
        "project",
        "record_id",
        "id",
    )
    for key in path_keys:
        if key in payload:
            raw = str(payload[key]).replace("\\", "/").strip()
            parts.append(f"{key}:{posixpath.normpath(raw)}")
    for key in url_keys:
        if key not in payload:
            continue
        raw = str(payload[key]).strip()
        parsed = urlsplit(raw if "://" in raw else f"//{raw}")
        try:
            host = (parsed.hostname or raw).lower()
            parsed_port = parsed.port
        except ValueError:
            host = raw.lower()
            parsed_port = None
        port = f":{parsed_port}" if parsed_port else ""
        parts.append(f"domain:{host}{port}")
    for key in identity_keys:
        if key in payload:
            value = payload[key]
            rendered = ",".join(map(str, value)) if isinstance(value, list) else str(value)
            parts.append(f"{key}:{rendered.strip()}")

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    if not parts:
        digest = hashlib.sha256(canonical.encode()).hexdigest()[:16]
        parts.append(f"call:{digest}")

    limits: dict[str, int | float | str] = {}
    for key in ("amount", "value", "count", "limit"):
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            limits[f"max_{key}"] = value
    for key in ("content", "body", "text", "code"):
        value = payload.get(key)
        if isinstance(value, str):
            limits["max_payload_bytes"] = max(
                int(limits.get("max_payload_bytes", 0)), len(value.encode("utf-8"))
            )
    return ApprovalScope(
        subject=subject,
        action=action,
        resource=" | ".join(sorted(set(parts))),
        limits=limits,
    )


@dataclass(frozen=True, slots=True)
class CapabilityLease:
    """A revocable, expiring grant created from a human approval."""

    id: UUID
    workspace_id: WorkspaceId
    scope: ApprovalScope
    grant: ApprovalGrant
    reason: str
    approval_id: UUID
    task_id: UUID | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_by: str | None = None

    @classmethod
    def create(
        cls,
        *,
        workspace_id: WorkspaceId,
        scope: ApprovalScope,
        grant: ApprovalGrant,
        reason: str,
        approval_id: UUID,
        task_id: UUID | None = None,
        duration_seconds: float | None = None,
    ) -> CapabilityLease:
        now = datetime.now(UTC)
        expires_at = (
            now + timedelta(seconds=duration_seconds)
            if duration_seconds is not None and duration_seconds > 0
            else None
        )
        return cls(
            id=uuid4(),
            workspace_id=workspace_id,
            scope=scope,
            grant=grant,
            reason=reason,
            approval_id=approval_id,
            task_id=task_id,
            created_at=now,
            expires_at=expires_at,
        )

    def is_active(self, now: datetime | None = None) -> bool:
        moment = now or datetime.now(UTC)
        return self.revoked_at is None and (
            self.expires_at is None or self.expires_at > moment
        )

    def matches(
        self,
        requested: ApprovalScope,
        *,
        workspace_id: WorkspaceId,
        task_id: UUID,
        now: datetime | None = None,
    ) -> bool:
        if not self.is_active(now) or self.workspace_id != workspace_id:
            return False
        if self.grant is ApprovalGrant.TASK and self.task_id != task_id:
            return False
        return self.scope.allows(requested)

    def revoke(self, *, by: str = "user", now: datetime | None = None) -> CapabilityLease:
        return replace(self, revoked_at=now or datetime.now(UTC), revoked_by=by)


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """A human decision an irreversible action is waiting on.

    Only a human resolves these. Prometheus can ask; it can never approve its own work.
    """

    id: UUID
    task_id: UUID
    action: str
    #: The tool this question is about. `action` is that tool rendered with its
    #: arguments, for a person to read; this is what the question is *about*,
    #: for anything that has to decide by tool rather than by prose. Kept apart
    #: because reading the name back out of the rendered line is parsing our own
    #: formatting, which is the habit the rest of the platform refuses.
    tool: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    risk_level: RiskLevel = RiskLevel.HIGH
    workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    requested_by_employee_id: UUID | None = None
    requested_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: Why this action needed asking, in the words shown to the person deciding.
    reason: str = ""
    #: The exact grant this question may create. Kept beside the human-readable
    #: action so a later decision never has to reconstruct authority from prose.
    scope: ApprovalScope | None = None
    #: A bounded, redacted account of the expected effect, supplied by the
    #: tool or derived from its declaration before anything runs.
    preview: dict[str, Any] = field(default_factory=dict)
    policy_source: str = "risk_threshold"
    #: True when external data preceded this action. Such a request must be
    #: answered by a person for this exact call; AUTO and reusable grants are
    #: intentionally insufficient authority.
    requires_explicit_confirmation: bool = False
    context_sources: tuple[dict[str, str], ...] = ()
    #: When this question stops being worth answering. None means it waits
    #: forever, which is the right default for a terminal prompt and the wrong
    #: one for a page nobody has open.
    expires_at: datetime | None = None

    @classmethod
    def create(cls, task_id: UUID, action: str, **extra: Any) -> ApprovalRequest:
        return cls(id=uuid4(), task_id=task_id, action=action, **extra)

    def redacted(self) -> ApprovalRequest:
        """The form that is safe to show and to store."""
        scope = (
            replace(
                self.scope,
                subject=redact(self.scope.subject),
                action=redact(self.scope.action),
                resource=redact(self.scope.resource),
                limits=redact(self.scope.limits),
            )
            if self.scope
            else None
        )
        return replace(
            self,
            action=redact(self.action),
            payload=redact(self.payload),
            reason=redact(self.reason),
            scope=scope,
            preview=redact(self.preview),
            policy_source=redact(self.policy_source),
            context_sources=tuple(redact(self.context_sources)),
        )

    def expiring_in(self, seconds: float | None) -> ApprovalRequest:
        """The same question with a deadline on it. None leaves it open."""
        if seconds is None or seconds <= 0:
            return self
        return replace(self, expires_at=self.requested_at + timedelta(seconds=seconds))


@dataclass(frozen=True, slots=True)
class Approval:
    """A request plus what was decided about it.

    Kept as one persisted value rather than two tables: the question and the
    answer are read together every time, and a pending request is just one whose
    answer has not arrived.
    """

    request: ApprovalRequest
    state: ApprovalState = ApprovalState.PENDING
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    comment: str = ""
    grant: ApprovalGrant = ApprovalGrant.ONCE
    lease_id: UUID | None = None

    @property
    def id(self) -> UUID:
        return self.request.id

    @property
    def is_pending(self) -> bool:
        return self.state is ApprovalState.PENDING

    def is_overdue(self, now: datetime | None = None) -> bool:
        """Still unanswered, and past the point where answering it means much.

        An expired question is not an approved one. The whole design says an
        action nobody confirmed does not happen, and a deadline passing is one
        more way of nobody confirming it.
        """
        if not self.is_pending or self.request.expires_at is None:
            return False
        return (now or datetime.now(UTC)) >= self.request.expires_at

    def expire(self, now: datetime | None = None) -> Approval:
        return replace(
            self,
            state=ApprovalState.EXPIRED,
            resolved_at=now or datetime.now(UTC),
            resolved_by="timeout",
        )

    def resolve(
        self, decision: ApprovalState, *, resolved_by: str = "user", comment: str = ""
    ) -> Approval:
        return replace(
            self,
            state=decision,
            resolved_at=datetime.now(UTC),
            resolved_by=resolved_by,
            comment=comment,
        )
