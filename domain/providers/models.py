"""A connection to a provider: which kind, reached how, paying with what.

**A provider is a kind; a connection is an account.** The kind is something the
platform knows how to talk to; what a user adds is a way in - a name they chose,
the kind it is, optionally an address, and the name of the credential it pays
with. That distinction is the whole reason this type exists: one person can hold
two keys to one vendor - work and personal, or a client's and their own - and a
record keyed by vendor cannot say that.

**`kind` is a plain string here, and that is not laziness.** No vendor is named
anywhere under `domain/` (ADR 0003), and `tests/unit/test_architecture_boundaries.py`
fails the build over it - so which kinds exist is decided where the adapters
are, in `infrastructure/llm/`, exactly as `ModelChoice.provider` has always been
a string. A closed enum here would have put five product names in the one layer
that must not know any.

**The credential is named here and never held here.** Same asymmetry as an
integration record (ADR 0015) and for a stronger reason: this row is read to
build a client on a hot path, and a value that appears in a row appears in every
log line, error and debugger frame that ever prints one. `secret_name` points at
the credential store; the value is resolved at the moment of the call.

`base_url` empty means the adapter's own default, which is what almost every
connection wants. It is here for the two cases that are real: a model runner on
another port, and a gateway that speaks a kind's protocol at its own address.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from domain.workspace.models import DEFAULT_WORKSPACE_ID, WorkspaceId


@dataclass(frozen=True, slots=True)
class Connection:
    """One way in to a provider. The name is the identity."""

    id: UUID
    name: str
    #: Which kind of provider this reaches, in whatever vocabulary the adapters
    #: use. Checked against what this machine implements before it is stored.
    kind: str
    #: Empty means the adapter's own default address.
    base_url: str = ""
    #: Which credential this pays with. Empty for a kind that needs none.
    secret_name: str = ""
    #: Whether this kind can work without a credential at all. Answered where
    #: the adapters are and carried here, so nothing above has to ask twice.
    needs_credential: bool = True
    description: str = ""
    workspace_id: WorkspaceId = DEFAULT_WORKSPACE_ID
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def create(cls, name: str, kind: str, **extra: object) -> Connection:
        return cls(id=uuid4(), name=name, kind=kind, **extra)  # type: ignore[arg-type]

    @property
    def is_usable(self) -> bool:
        """Whether a client could be built from this at all.

        Not whether the key works - that is a network call, and this is a value.
        """
        return not self.needs_credential or bool(self.secret_name)

    def with_secret(self, secret_name: str) -> Connection:
        return replace(self, secret_name=secret_name, updated_at=datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class InstalledModels:
    """What a connection can be asked it has, and how sure that answer is.

    Three answers a settings page has to tell apart, and a bare list of names
    could not: the runner answered (`reachable`), the runner is not running but
    the models it would serve are on this disk (`reachable` false, names
    present), and this kind of provider cannot be asked at all (`supported`
    false). The second is the one that used to look like the third - a stopped
    runner turned the list into an empty text field with no word about why.
    """

    names: tuple[str, ...] = ()
    #: Whether this kind of provider can be asked what it has at all.
    supported: bool = False
    #: Whether the runner answered just now. False with names means read off disk.
    reachable: bool = False
    #: Which runner answered, or whose models were found - a label for a person.
    runner: str = ""
    #: The address that was asked.
    address: str = ""
