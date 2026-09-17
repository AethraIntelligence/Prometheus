"""Canonical audit hashes and verifier results.

The chain detects local alteration; it does not claim to prevent the machine's
owner from replacing both the store and the application that verifies it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC
from typing import Any

from domain.audit.protocols import AuditRecord
from domain.secrets.models import redact

GENESIS_HASH = "0" * 64
AUDIT_CHAIN_VERSION = 1


def canonical_record(record: AuditRecord, *, sequence: int, previous_hash: str) -> bytes:
    value: dict[str, Any] = {
        "version": AUDIT_CHAIN_VERSION,
        "sequence": sequence,
        "previous_hash": previous_hash,
        "timestamp": record.timestamp.astimezone(UTC).isoformat(),
        "workspace_id": str(record.workspace_id),
        "actor_kind": record.actor_kind.value,
        "actor_id": record.actor_id,
        "task_id": str(record.task_id) if record.task_id else None,
        "assignment_id": str(record.assignment_id) if record.assignment_id else None,
        "action": record.action,
        "tool": record.tool,
        "model": record.model,
        "result": record.result,
        "cost_usd": record.cost_usd,
        "latency_ms": record.latency_ms,
        "details": redact(record.details),
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def record_hash(record: AuditRecord, *, sequence: int, previous_hash: str) -> str:
    return hashlib.sha256(
        canonical_record(record, sequence=sequence, previous_hash=previous_hash)
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class AuditVerification:
    valid: bool
    checked: int
    first_invalid_sequence: int | None = None
    reason: str = ""
