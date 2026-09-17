"""Error taxonomy shared by every layer.

Domain code raises these; infrastructure adapters are responsible for mapping
vendor-specific failures onto them, so nothing above `infrastructure/` ever has
to know which provider failed.
"""

from __future__ import annotations


class PrometheusError(Exception):
    """Base class for every error the platform raises on purpose."""


# --- Configuration and wiring -------------------------------------------------


class ConfigurationError(PrometheusError):
    """The platform is misconfigured and cannot start or serve a request."""


class DependencyNotConfiguredError(ConfigurationError):
    """A dependency was requested from the container before it was available."""


class SettingValueError(PrometheusError):
    """A person asked for a setting value this installation will not take.

    Not a `ConfigurationError`: nothing is misconfigured yet, and the refusal is
    what keeps it that way. A transport answers it as a 400 with this sentence.
    """


# --- Domain rules -------------------------------------------------------------


class DomainError(PrometheusError):
    """A domain rule was violated."""


class WorkControlError(DomainError):
    """A requested run control would make the execution state ambiguous."""


class InvalidStateTransitionError(DomainError):
    """A task was asked to move to a status it cannot reach from the current one."""

    def __init__(self, from_status: str, to_status: str) -> None:
        super().__init__(f"Invalid task transition: {from_status} -> {to_status}")
        self.from_status = from_status
        self.to_status = to_status


class NotFoundError(DomainError):
    """A referenced entity does not exist."""


class TaskNotFoundError(NotFoundError):
    def __init__(self, task_id: object) -> None:
        super().__init__(f"Unknown task: {task_id}")


class EmployeeNotFoundError(NotFoundError):
    def __init__(self, name: str) -> None:
        super().__init__(f"Unknown employee: {name}")


class IntegrationNotFoundError(NotFoundError):
    """Asked about an integration that is not connected on this machine.

    Here rather than beside the lifecycle that raises it, next to the other
    three not-founds: a transport has to be able to turn it into a 404 without
    reaching past the interface boundary for the class.
    """


class WorkspaceNotFoundError(NotFoundError):
    """Named a workspace this machine does not have.

    Beside the other not-founds for the same reason `IntegrationNotFoundError`
    is: a transport turns it into a 404 without reaching past the interface
    boundary for the class.
    """


class ToolNotFoundError(NotFoundError):
    def __init__(self, name: str) -> None:
        super().__init__(f"Unknown tool: {name}")


class ToolInputError(DomainError):
    """A tool was called with arguments it cannot work with.

    Reported back to the model rather than raised at the user: being told the
    argument was wrong is something a model can act on in the next step.
    """


class SecretNotFoundError(ConfigurationError):
    """A tool asked for a credential that is not configured on this machine."""


# --- Permissions --------------------------------------------------------------


class DuplicateIntegrationError(DomainError):
    """Two integrations of one name would make their tools ambiguous.

    `gmail.send_message` has to name exactly one action, and an employee
    declaration granting `gmail` has to mean exactly one service.
    """


class PluginConfigurationError(DomainError, ValueError):
    """What a person supplied cannot install this plugin, and which part.

    A `ValueError` too, because it is one - a value that does not fit - and a
    caller that only knows the standard hierarchy should still recognise it.
    """


class DuplicateWorkspaceError(DomainError):
    """Two workspaces of one name would resolve to one id.

    The id is a slug of the name and is written on every row the workspace
    owns, so two names that slug the same way are two contexts sharing one
    boundary - which is the one thing a boundary may not do.
    """


class FolderError(DomainError):
    """A folder a person chose that no work can be confined to."""


class ProtectedWorkspaceError(DomainError):
    """Asked to remove the workspace everything else falls back to.

    The first workspace holds every row written before there were others, and
    is where a machine lands when the active one is gone. Removing it would
    orphan the first and leave nowhere to do the second.
    """


class PermissionDeniedError(DomainError):
    """An actor tried to use a capability it is not allowed to use.

    Delegation never escalates privileges: effective rights are the intersection
    of the delegating actor's rights and the executing actor's rights.
    """


class ApprovalRequiredError(DomainError):
    """An irreversible action was attempted without a human decision."""


class ApprovalDeniedError(DomainError):
    """A human was asked about an irreversible action and said no."""


# --- Execution ----------------------------------------------------------------


class ExecutionError(PrometheusError):
    """A task could not be carried out."""


class LimitExceededError(ExecutionError):
    """A run hit one of its budgets: steps, cost or wall time."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"Execution limit reached ({kind}){f': {detail}' if detail else ''}")
        self.kind = kind


class StopRequestedError(ExecutionError):
    """The user pulled the brake while the platform was acting on the machine."""


class ComputerUseError(ExecutionError):
    """An action on a screen could not be carried out."""


class PlanningError(ExecutionError):
    """The model did not produce a usable plan."""


class DelegationError(ExecutionError):
    """There is no one to give this task to.

    A configuration problem wearing an execution problem's clothes - a machine
    with no declared employee cannot do work - but it surfaces mid-objective and
    is answered by adding a declaration, so it is reported to the user with the
    objective it stopped rather than at start-up.
    """


class VerificationFailedError(ExecutionError):
    """The result did not hold up, and no attempts are left."""

    def __init__(self, reason: str, missing: tuple[str, ...] = ()) -> None:
        super().__init__(reason)
        self.missing = missing


# --- Storage ------------------------------------------------------------------


class StorageError(PrometheusError):
    """The local store could not be read or written."""


class StorageNotInitializedError(StorageError):
    """The database exists but has no schema yet; migrations have not been run."""


# --- External providers -------------------------------------------------------


class ProviderError(PrometheusError):
    """An external provider failed. Base class for adapter-level failures."""

    transient: bool = False


class RateLimitError(ProviderError):
    transient = True


class TimeoutError(ProviderError):
    transient = True


class InvalidRequestError(ProviderError):
    """The provider rejected the request; retrying the same payload will not help."""


class ProviderUnavailableError(ProviderError):
    transient = True


# --- Integrations -------------------------------------------------------------


class IntegrationError(PrometheusError):
    """An external integration failed.

    Mirrors `ProviderError` rather than inventing a second vocabulary: what the
    orchestrator needs to know is whether trying again is worth anything, and
    that is a property of the failure's *type*, never of its message
    (`application.orchestrator`). A separate retry engine for integrations
    would be a second answer to a question the platform already answers.
    """

    transient: bool = False


class IntegrationUnavailableError(IntegrationError):
    """The server is not answering. It may be on the next call."""

    transient = True


class IntegrationTimeoutError(IntegrationError):
    transient = True


class IntegrationProtocolError(IntegrationError):
    """The server answered with something this platform cannot read.

    Not transient: a malformed frame or an unreadable tool schema is what that
    server does, and the same call will produce the same thing.
    """


class IntegrationAuthenticationError(IntegrationError):
    """The credential was rejected or is gone.

    Not transient, and deliberately not a `ConfigurationError`: what has to
    change is a stored secret rather than a setting, and the difference decides
    whether the user is sent to their settings file or to the integration.
    """
