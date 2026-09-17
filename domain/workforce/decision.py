"""Why this employee, and why not the others - as a record, not a log line.

`tasks.assignment_reason` was a sentence, and the candidates the delegator
passed over existed only in `prometheus.delegated` at info level. A person
asking "why did the analyst get the write-up" could read the answer and never
the alternatives, and a statistic could count neither. So the decision is a
value with codes, stored on the assignment it produced.

Codes are closed vocabularies for the reason every other routing term is: a
reason nobody else understands cannot be counted, filtered or tested.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

#: The stored shape's version. A reader meeting a higher one treats the decision
#: as unrecorded rather than guessing at fields it does not know.
DECISION_VERSION = 1


class SelectionCode(StrEnum):
    """How the chosen employee came to be chosen."""

    #: Nobody else was declared at all.
    ONLY_EMPLOYEE = "ONLY_EMPLOYEE"
    #: Narrowing by what the task needs left one.
    ONLY_QUALIFIED = "ONLY_QUALIFIED"
    #: A model chose between several qualified candidates.
    MODEL_CHOSE = "MODEL_CHOSE"
    #: A model chose between candidates nothing distinguishes.
    MODEL_CHOSE_AMONG_EQUALS = "MODEL_CHOSE_AMONG_EQUALS"
    #: The model named nobody on the list; the deterministic fallback chose.
    FALLBACK = "FALLBACK"
    #: Nobody could start from what the earlier work delivered; nobody was chosen.
    NO_COMPATIBLE_EMPLOYEE = "NO_COMPATIBLE_EMPLOYEE"
    #: A person named the employee.
    CHOSEN_BY_PERSON = "CHOSEN_BY_PERSON"
    #: A workflow step named the employee.
    DECLARED_BY_WORKFLOW = "DECLARED_BY_WORKFLOW"
    #: Written before decisions were recorded.
    UNRECORDED = "UNRECORDED"


class RejectionCode(StrEnum):
    """Why a candidate did not get the task."""

    UNAVAILABLE = "UNAVAILABLE"
    LACKS_CAPABILITY = "LACKS_CAPABILITY"
    LACKS_SERVICE = "LACKS_SERVICE"
    CAPABILITY_LOST_HERE = "CAPABILITY_LOST_HERE"
    HANDOFF_INCOMPATIBLE = "HANDOFF_INCOMPATIBLE"
    AVOIDED_AFTER_FAILURE = "AVOIDED_AFTER_FAILURE"
    NOT_CHOSEN = "NOT_CHOSEN"


@dataclass(frozen=True, slots=True)
class Alternative:
    employee: str
    code: RejectionCode
    reason: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"employee": self.employee, "code": self.code.value, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class Decision:
    """One delegation, recorded."""

    code: SelectionCode
    reason: str = ""
    alternatives: tuple[Alternative, ...] = ()
    #: Qualified candidates nothing in their declarations tells apart. Two
    #: people claiming the same work with no difference is a finding about the
    #: workforce, surfaced where the choice between them was made.
    indistinguishable: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": DECISION_VERSION,
            "code": self.code.value,
            "reason": self.reason,
            "alternatives": [item.to_dict() for item in self.alternatives],
            "indistinguishable": list(self.indistinguishable),
        }

    @classmethod
    def from_dict(cls, raw: object) -> Decision | None:
        """A stored decision, or None where there is none this build can read."""
        if not isinstance(raw, dict) or not raw:
            return None
        version = raw.get("version")
        if not isinstance(version, int) or version > DECISION_VERSION:
            return None
        try:
            code = SelectionCode(str(raw.get("code", "")))
        except ValueError:
            return None
        alternatives = []
        for item in raw.get("alternatives") or ():
            if not isinstance(item, dict):
                continue
            try:
                rejection = RejectionCode(str(item.get("code", "")))
            except ValueError:
                continue
            alternatives.append(
                Alternative(str(item.get("employee", "")), rejection, str(item.get("reason", "")))
            )
        return cls(
            code=code,
            reason=str(raw.get("reason", "")),
            alternatives=tuple(alternatives),
            indistinguishable=tuple(str(name) for name in raw.get("indistinguishable") or ()),
        )
