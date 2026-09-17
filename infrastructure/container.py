"""Dependency wiring.

Dependencies are built lazily: a CLI call that only prints the version must not
open a database file, and a test that only needs a repository must not configure
a provider.

The container does not read configuration - it is handed the settings it needs.
That keeps `infrastructure` from importing `app`, which owns configuration.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cached_property

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domain.approvals.protocols import (
    ApprovalRepository,
    ApprovalService,
    CapabilityLeaseRepository,
)
from domain.audit.protocols import AuditLog
from domain.browser.protocols import Browser
from domain.capabilities.models import Capability, CapabilityRequirement
from domain.computer.constraints import ComputerConstraints
from domain.computer.models import Region
from domain.computer.protocols import Computer, ScreenReader, StopSignal
from domain.conversations.repository import ConversationRepository, SessionStateRepository
from domain.employees.protocols import EmployeeRegistry
from domain.employees.validation import Issue, check_all
from domain.errors import PrometheusError
from domain.integrations.repository import IntegrationRepository
from domain.knowledge.protocols import EmbeddingProvider, KnowledgeStore, Retriever
from domain.llm.models import RoutingHints, TaskKind
from domain.llm.protocols import LLM, ModelRouter
from domain.llm.telemetry import LLMCallLog
from domain.memory.protocols import Memory, MemoryMaintenance
from domain.memory.usage import MemoryUseLog
from domain.scheduling.protocols import EventLog, ScheduleRepository
from domain.search.protocols import SearchEngine
from domain.secrets.protocols import CredentialStore, SecretResolver
from domain.tasks.cancellation import Cancellations
from domain.tasks.repository import TaskRepository
from domain.tools.protocols import ToolRegistry
from domain.tools.telemetry import ToolCallLog
from domain.validation.protocols import ScenarioRegistry
from domain.validation.run import ValidationRunRepository
from domain.validation.scenario import Requirement
from domain.workflows.protocols import WorkflowRegistry
from domain.workflows.run import WorkflowRunRepository
from domain.workforce.repository import (
    AssignmentRepository,
    ObjectiveRepository,
    PlanRepository,
)
from domain.workspace.models import WorkspaceId
from domain.workspace.protocols import WorkspaceContext
from domain.workspace.repository import WorkspaceRepository
from infrastructure.employees.yaml_registry import YamlEmployeeRegistry
from infrastructure.llm.catalog import ModelCatalog
from infrastructure.llm.factory import ProviderFactory
from infrastructure.llm.retry import RetryPolicy
from infrastructure.llm.router import CapabilityAwareModelRouter
from infrastructure.observability.logging import configure_logging, get_logger
from infrastructure.persistence.in_memory_task_repository import InMemoryTaskRepository
from infrastructure.persistence.session import create_engine, create_session_factory
from infrastructure.progress.broadcaster import InMemoryProgressBroadcaster
from infrastructure.secrets.env import EnvSecretResolver
from infrastructure.settings import RuntimeSettings
from infrastructure.tools.builtin import build_registry
from infrastructure.workspace.context import LocalWorkspaceContext


class Container:
    def __init__(
        self,
        settings: RuntimeSettings,
        *,
        in_memory: bool = False,
        screen_reader: Callable[[], ScreenReader] | None = None,
    ) -> None:
        self.settings = settings
        self._in_memory = in_memory
        self._configured_logging = False
        # Handed in rather than built here: reading a screen is an application
        # component that needs a model, and this container may not import
        # `application`. The composition root owns the one wire that crosses
        # both, which is what it is for (ADR 0001).
        self._screen_reader = screen_reader
        #: Set by an interface that answers approvals itself, before anything
        #: builds the approval service. None means the terminal answers them.
        self._confirmer: Callable[..., object] | None = None
        #: The catalog once storage has been read. None means nobody has read
        #: it yet and the shipped file answers - see `model_catalog`.
        self._catalog: ModelCatalog | None = None
        #: Set by the composition root, which is the only place allowed to
        #: build an application component (ADR 0001).
        self._integrations: Callable[[], object] | None = None

    # --- Cross-cutting --------------------------------------------------------

    def configure(self) -> None:
        """Idempotent start-up: logging, and the data directory the DB lives in."""
        if not self._configured_logging:
            configure_logging(self.settings.log_level, self.settings.log_format)
            self._configured_logging = True
        if not self._in_memory:
            self.settings.ensure_data_dir()

    @cached_property
    def logger(self):
        # structlog's bound logger type is not stable enough to annotate.
        self.configure()
        return get_logger("prometheus")

    @cached_property
    def progress(self) -> InMemoryProgressBroadcaster:
        """Where a running task says what it is doing, for whoever is watching.

        Built lazily like everything else, and handed to the runtime as a
        `ProgressSink`: the CLI never subscribes, so it never pays for a buffer.
        """
        return InMemoryProgressBroadcaster()

    @cached_property
    def cancellations(self) -> Cancellations:
        """Which tasks a person has asked to stop, for this process's lifetime."""
        from infrastructure.tasks.cancellation import InMemoryCancellations

        return InMemoryCancellations()

    # --- Persistence ----------------------------------------------------------

    @cached_property
    def engine(self) -> AsyncEngine:
        self.configure()
        return create_engine(self.settings.resolved_database_url)

    @cached_property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return create_session_factory(self.engine)

    @cached_property
    def task_repository(self) -> TaskRepository:
        if self._in_memory:
            return InMemoryTaskRepository()
        # Imported here so an in-memory container never pulls in the SQL adapter.
        from infrastructure.persistence.task_repository import SqlTaskRepository

        return SqlTaskRepository(self.session_factory)

    @cached_property
    def llm_call_log(self) -> LLMCallLog:
        if self._in_memory:
            from infrastructure.persistence.llm_call_repository import InMemoryLLMCallLog

            return InMemoryLLMCallLog()
        from infrastructure.persistence.llm_call_repository import SqlLLMCallLog

        return SqlLLMCallLog(self.session_factory)

    # --- Models ---------------------------------------------------------------

    @cached_property
    def model_catalog(self) -> ModelCatalog:
        """What the router and the factory read. The file until storage says otherwise.

        Synchronous because everything that reads a catalog reads it while
        choosing a model, and a coroutine there would put an await on the one
        path that must stay cheap. `load_catalog` replaces it during start-up
        with what the store holds; a process that never got that far runs on the
        shipped file, which is a working machine rather than a broken one.
        """
        if self._catalog is None:
            self._catalog = ModelCatalog.load(self.settings.model_catalog_path)
        return self._catalog

    def use_catalog(self, catalog: ModelCatalog) -> None:
        """Told what the catalog is, once storage has been read.

        Anything built from it is dropped rather than left holding the old one:
        a router that keeps the file's entries after a person added a model in
        the window is a settings page that appears not to work.
        """
        self._catalog = catalog
        for built in ("model_router", "llm_factory", "_embedding_client"):
            self.__dict__.pop(built, None)

    @cached_property
    def catalog_repository(self):
        from infrastructure.persistence.catalog_repository import SqlCatalogRepository

        return SqlCatalogRepository(self.session_factory)

    @cached_property
    def catalog_source(self):
        from infrastructure.llm.store import StoredCatalogSource

        return StoredCatalogSource(
            self.catalog_repository,
            configured_path=self.settings.model_catalog_path,
            reachable=self._can_reach,
        )

    def _can_reach(self, entry) -> bool:
        """Whether a client could be built for this entry on this machine.

        Three ways an entry is reachable: it names a connection that has what it
        needs; it needs no credential at all; or it names nothing and the machine
        has the single configured key, which is what every installation before
        Phase 17 relies on.
        """
        if entry.connection:
            connection = self.connection_directory.get(entry.connection)
            return connection is not None and (
                not connection.needs_credential or bool(connection.secret_name)
            )
        from infrastructure.llm.providers import kind_named

        try:
            return not kind_named(entry.provider).needs_credential or bool(
                self.settings.llm_api_key
            )
        except PrometheusError:
            # A provider this build has no adapter for. Nothing can call it, so
            # nothing should route to it - and the settings page still shows it.
            return False

    @cached_property
    def connections(self):
        from infrastructure.persistence.connection_repository import SqlConnectionRepository

        return SqlConnectionRepository(self.session_factory)

    @cached_property
    def connection_directory(self):
        from infrastructure.llm.connections import ConnectionDirectory

        return ConnectionDirectory(self.connections)

    @cached_property
    def model_router(self) -> ModelRouter:
        return CapabilityAwareModelRouter(self.model_catalog)

    @cached_property
    def llm_factory(self) -> ProviderFactory:
        return ProviderFactory(
            catalog=self.model_catalog,
            connections=self.connection_directory,
            secrets=self.secret_resolver,
            api_key=self.settings.llm_api_key,
            base_url=self.settings.llm_base_url,
            local_base_url=self.settings.local_llm_base_url,
            local_autostart=self.settings.local_llm_autostart,
            call_log=self.llm_call_log,
            retry_policy=RetryPolicy(attempts=self.settings.llm_retry_attempts),
            timeout_seconds=self.settings.llm_timeout_seconds,
        )

    def llm_for(
        self,
        task_kind: TaskKind,
        requirement: CapabilityRequirement | None = None,
        hints: RoutingHints | None = None,
    ) -> LLM:
        """Pick a model for a piece of work and hand back a client for it."""
        from infrastructure.llm.directed import DirectedLLM

        def route() -> LLM:
            choice = self.model_router.select(
                task_kind, requirement or CapabilityRequirement(), hints
            )
            # Debug, not info: clients are built when the runtime is assembled,
            # so at info level this reads as though the work happened.
            self.logger.debug(
                "llm.routed", task_kind=str(task_kind), model=choice.model, reason=choice.reason
            )
            return self.llm_factory.for_choice(choice)

        return DirectedLLM(route(), route)

    # --- Workforce ------------------------------------------------------------

    @cached_property
    def employee_registry(self) -> EmployeeRegistry:
        """The declarations, with any integration grants already applied.

        Wrapped rather than changed: reading a YAML file is a property of the
        repository, and whether `gmail` is connected on this machine right now
        is a property of the machine. The wrapper holds a snapshot that
        `IntegrationService` refreshes when a person changes something, so
        `list()` stays synchronous and never reaches a database.
        """
        registry = YamlEmployeeRegistry(self.settings.employees_dir)
        if not self.settings.integrations_enabled:
            return registry
        from infrastructure.employees.granting import GrantingEmployeeRegistry

        return GrantingEmployeeRegistry(registry)

    def tool_capabilities(self) -> dict[str, frozenset[Capability]]:
        """What each tool on this machine lets an employee do.

        Read off the specs rather than kept as a second list, so a tool that
        gains a capability does not need remembering anywhere else.
        """
        from domain.policies.models import ActorKind, SimpleActor

        everything = SimpleActor("container", ActorKind.SYSTEM, frozenset({"*"}))
        return {spec.name: spec.capabilities for spec in self.tool_registry.list_specs(everything)}

    def check_employees(self) -> tuple[Issue, ...]:
        """What is wrong with the declarations, given the tools that exist here.

        A declaration is written by hand and read by nobody until something goes
        wrong with it, and the ways it goes wrong are quiet: a tool that is
        never offered, a capability that is never searched. So it is checked
        where both halves are known, which is here.
        """
        return check_all(
            self.employee_registry.list(),
            self.tool_capabilities(),
            self.connected_integrations(),
        )

    def connected_integrations(self) -> frozenset[str]:
        """The names an employee declaration may be granted, as known right now.

        Read off the same snapshot the grants come from rather than from the
        database, because this is called where a declaration is checked - at
        start-up and from `prometheus employees` - and both already have it.
        """
        registry = self.employee_registry
        integrations = getattr(registry, "integrations", ())
        return frozenset(item.name for item in integrations)

    # --- Tools ----------------------------------------------------------------

    @cached_property
    def credential_store(self) -> CredentialStore:
        """Where a credential the user typed into the interface is kept.

        A separate contract from reading, so that holding the resolver - which
        every tool does - is not the same as being able to write one.
        """

        return self._credentials

    @cached_property
    def _credentials(self):
        """The encrypted store, with the environment still winning over it.

        In memory it holds ciphertext and nothing else; the master key opens one
        value at a time, when a tool asks. `restore` fills it, and until then it
        answers with whatever the environment has - which is the correct answer
        for a process that has not started properly rather than a degraded one.
        """
        from infrastructure.persistence.secret_repository import SqlSecretRepository
        from infrastructure.secrets.encrypted import EncryptedCredentialStore
        from infrastructure.secrets.encryption import Envelope, resolve_master_key

        return EncryptedCredentialStore(
            SqlSecretRepository(self.session_factory),
            Envelope(resolve_master_key(self.settings.data_dir)),
            fallback=EnvSecretResolver(),
        )

    @cached_property
    def legacy_credentials(self):
        """The 0600 JSON file installations before Phase 17 have.

        Kept only to be read once at start-up. It is never written to again and
        never deleted: erasing the only copy of a credential the moment
        something else believes it has written another one is how a recoverable
        problem becomes a lost key.
        """
        from infrastructure.secrets.local import LocalCredentialStore

        return LocalCredentialStore(self.settings.data_dir / "credentials.json")

    @cached_property
    def secret_resolver(self) -> SecretResolver:
        """The environment first, then anything the interface stored.

        One object serves both halves, like the memory adapter: separate
        contracts so that reading does not imply writing, one implementation
        because they are the same file.
        """
        if not self.settings.integrations_enabled:
            return EnvSecretResolver()
        return self._credentials

    @cached_property
    def search_engine(self) -> SearchEngine:
        from infrastructure.search.duckduckgo import DuckDuckGoSearch

        return DuckDuckGoSearch(timeout_seconds=self.settings.browser_timeout_seconds)

    @cached_property
    def browser(self) -> Browser:
        from infrastructure.browser.playwright_browser import PlaywrightBrowser

        return PlaywrightBrowser(
            self.search_engine,
            headless=self.settings.browser_headless,
            timeout_seconds=self.settings.browser_timeout_seconds,
        )

    # --- Computer use ---------------------------------------------------------

    @cached_property
    def stop_signal(self) -> StopSignal:
        from infrastructure.computer.stop import FileStopSignal

        return FileStopSignal(self.settings.stop_file_path)

    def use_screen_reader(self, factory: Callable[[], ScreenReader]) -> None:
        """Supply the component that can look at a screen.

        Called by the composition root after construction, because the factory
        needs the container it is being given to - it routes a model through it.
        """
        self._screen_reader = factory

    @cached_property
    def screen_reader(self) -> ScreenReader:
        if self._screen_reader is None:
            from domain.errors import DependencyNotConfiguredError

            raise DependencyNotConfiguredError(
                "Computer use needs a screen reader; the composition root did not "
                "supply one."
            )
        return self._screen_reader()

    def _constraints(self, *, desktop: bool) -> ComputerConstraints:
        region = (
            Region.parse(self.settings.computer_allowed_region)
            if self.settings.computer_allowed_region
            else None
        )
        return ComputerConstraints(
            allowed_applications=frozenset(self.settings.computer_allowed_applications),
            allowed_region=region,
            max_actions=self.settings.computer_max_actions,
            # A page has no application to be in front of, and asking the
            # question there would refuse everything for no gain in safety.
            applies_to_applications=desktop,
        )

    def _guarded(self, computer: Computer, *, desktop: bool) -> Computer:
        from infrastructure.computer.guarded import GuardedComputer

        return GuardedComputer(
            computer, self._constraints(desktop=desktop), stop_signal=self.stop_signal
        )

    @cached_property
    def browser_computer(self) -> Computer:
        """Pixels inside the tab the employee already opened."""
        from infrastructure.computer.playwright_computer import PlaywrightComputer

        return self._guarded(PlaywrightComputer(self.browser), desktop=False)

    @cached_property
    def desktop_computer(self) -> Computer:
        from infrastructure.computer.desktop import DesktopComputer

        return self._guarded(
            DesktopComputer(enabled=self.settings.computer_use_enabled), desktop=True
        )

    def _computers(self) -> list[tuple[Computer, Callable[[], ScreenReader]]]:
        """Which screens exist on this machine, in hierarchy order.

        The browser surface comes with the browser: if an employee may drive a
        page, it may look at one. The desktop is separate and behind its own
        flag, because the browser tab is the platform's and the desktop is the
        user's.
        """
        # The reader is passed as a way to get one, not as one: listing the
        # registry must not route a model, and `prometheus tools` does nothing else.
        def reader() -> ScreenReader:
            return self.screen_reader

        surfaces: list[tuple[Computer, Callable[[], ScreenReader]]] = []
        if self.settings.browser_tools_enabled:
            surfaces.append((self.browser_computer, reader))
        if self.settings.computer_use_enabled:
            surfaces.append((self.desktop_computer, reader))
        return surfaces

    @cached_property
    def tool_registry(self) -> ToolRegistry:
        """Everything this machine can do. Who may do what is settled per employee.

        The callables are passed rather than the objects: a workforce that never
        opens a page never launches a browser, and never imports Playwright.
        """
        self.configure()
        return build_registry(
            file_root=self.workspaces.current_root,
            search_engine=(
                (lambda: self.search_engine) if self.settings.browser_tools_enabled else None
            ),
            browser=(lambda: self.browser) if self.settings.browser_tools_enabled else None,
            code_execution=self.settings.code_execution_enabled,
            code_timeout_seconds=self.settings.code_timeout_seconds,
            code_memory_mb=self.settings.code_memory_mb,
            code_disk_mb=self.settings.code_disk_mb,
            code_max_output_chars=self.settings.code_max_output_chars,
            computers=(
                self._computers
                if self._screen_reader is not None
                and (self.settings.browser_tools_enabled or self.settings.computer_use_enabled)
                else None
            ),
        )

    @cached_property
    def tool_call_log(self) -> ToolCallLog:
        if self._in_memory:
            from infrastructure.persistence.tool_call_repository import InMemoryToolCallLog

            return InMemoryToolCallLog()
        from infrastructure.persistence.tool_call_repository import SqlToolCallLog

        return SqlToolCallLog(self.session_factory)

    # --- Workspaces ---------------------------------------------------------

    @cached_property
    def workspace_repository(self) -> WorkspaceRepository:
        if self._in_memory:
            from infrastructure.persistence.workspace_repository import (
                InMemoryWorkspaceRepository,
            )

            return InMemoryWorkspaceRepository()
        from infrastructure.persistence.workspace_repository import (
            SqlWorkspaceRepository,
        )

        return SqlWorkspaceRepository(self.session_factory)

    @cached_property
    def workspaces(self) -> WorkspaceContext:
        """Which workspace this process is in, and where its files are.

        Built without touching the database: the filesystem tools ask it for a
        root on every call, and a call cannot wait on a query. What exists is
        declared into it by whoever has just read the store.
        """
        self.configure()
        return LocalWorkspaceContext(
            path=self.settings.active_workspace_path,
            default_root=self.settings.resolved_file_root,
            roots_base=self.settings.workspace_roots_dir,
            configured=WorkspaceId(self.settings.active_workspace),
        )

    # --- Integrations -------------------------------------------------------

    @cached_property
    def integration_repository(self) -> IntegrationRepository:
        if self._in_memory:
            from infrastructure.persistence.integration_repository import (
                InMemoryIntegrationRepository,
            )

            return InMemoryIntegrationRepository()
        from infrastructure.persistence.integration_repository import (
            SqlIntegrationRepository,
        )

        return SqlIntegrationRepository(self.session_factory)

    def use_integrations(self, factory: Callable[[], object]) -> None:
        """Supply the lifecycle of connected services.

        Handed in for the same reason the screen reader is: `IntegrationService`
        is an application component and this container may not import one
        (ADR 0001). The composition root owns the wire, and this container owns
        the repository, the registry and the connector it is built from.
        """
        self._integrations = factory

    @cached_property
    def plugin_catalog(self):
        """The services this machine offers to install, declared under `plugins/`."""
        from infrastructure.integrations.yaml_catalog import YamlPluginCatalog

        return YamlPluginCatalog(self.settings.plugins_dir)

    @property
    def plugin_credentials(self) -> dict[str, str]:
        """What this installation supplies to plugins, keyed as the plugins name it.

        Upper-cased here because the settings reader lower-cases what it reads
        from the environment, and a plugin's setting keys are variable names.
        """
        return {
            key.upper(): value
            for key, value in (self.settings.plugin_credentials or {}).items()
            if value
        }

    def plugin_runtimes(self) -> dict[str, dict[str, object]]:
        """Whether each kind of server can start here, and what to install if not.

        A method rather than a cached value: installing Node.js while the window
        is open should change the answer the next time the screen asks.
        """
        from infrastructure.integrations.runtimes import HINTS, available

        return {
            runtime.value: {"ready": ready, "hint": HINTS[runtime][0], "url": HINTS[runtime][1]}
            for runtime, ready in available().items()
        }

    @cached_property
    def integrations(self):
        """The lifecycle of connected services, or None if there is none.

        None means either the feature is switched off or the composition root
        did not supply one - a CLI command that only prints the version, say.
        Callers treat both the same way, because both mean the same thing:
        nothing here can connect a service.
        """
        if not self.settings.integrations_enabled or self._integrations is None:
            return None
        return self._integrations()

    def refresh_grants(self, integrations) -> None:
        """Have the employee registry see the current set of integrations.

        The snapshot follows the store rather than polling it: connecting or
        removing a service is a person-sized event, and a grant has to be right
        on the very next run rather than after a cache expires.
        """
        registry = self.employee_registry
        if hasattr(registry, "refresh"):
            registry.refresh(integrations)

    # --- Knowledge ----------------------------------------------------------

    @cached_property
    def knowledge_store(self) -> KnowledgeStore | None:
        """Where the user's own documents are kept, or None if that is off.

        A different store from memory, on purpose: memory decays and is pruned
        by age, and a specification somebody uploaded does not stop being true
        because nobody opened it for a fortnight (ADR 0016).
        """
        if not self.settings.knowledge_enabled:
            return None
        if self._in_memory:
            from infrastructure.knowledge.store import InMemoryKnowledgeStore

            return InMemoryKnowledgeStore()
        from infrastructure.knowledge.store import SqlKnowledgeStore

        return SqlKnowledgeStore(self.session_factory)

    @cached_property
    def embeddings(self) -> EmbeddingProvider | None:
        """A model that turns text into vectors, if the catalog offers one.

        Asked for by capability like everything else - nothing here names a
        model - and None where the catalog has no entry that can do it. None is
        not a failure: retrieval falls back to the text index and says so, which
        is what keeps `clone && run` working on a machine with no model server
        at all.
        """
        if not self.settings.knowledge_enabled:
            return None
        from infrastructure.llm.embeddings import RoutedEmbeddings

        return RoutedEmbeddings(lambda: self._embedding_client)

    @cached_property
    def _embedding_client(self) -> EmbeddingProvider | None:
        """The client for the model chosen now. Dropped with the catalog."""
        try:
            choice = self.model_router.select(
                TaskKind.EMBEDDING,
                CapabilityRequirement(required=frozenset({Capability.EMBEDDING})),
                RoutingHints(),
            )
            return self.llm_factory.for_embeddings(choice)
        except PrometheusError as error:
            self.logger.info("knowledge.no_embedding_model", reason=str(error))
            return None

    @cached_property
    def retriever(self) -> Retriever | None:
        """The half of knowledge that goes into a run: search, never delete."""
        store = self.knowledge_store
        if store is None:
            return None
        from infrastructure.knowledge.retriever import HybridRetriever

        return HybridRetriever(
            store,
            embeddings=self.embeddings,
            min_similarity=self.settings.knowledge_min_similarity,
        )

    # --- Memory -----------------------------------------------------------------

    @cached_property
    def memory(self) -> Memory | None:
        """Where what was learned is kept. None means nothing is remembered.

        One object serves both memory contracts. They are separate so that a
        caller of `recall` is not thereby handed the ability to delete; they are
        the same adapter because reading and forgetting happen over the same
        rows, and a second connection to the same file buys nothing.
        """
        if not self.settings.memory_enabled:
            return None
        if self._in_memory:
            from infrastructure.memory.in_memory import InMemoryMemory

            return InMemoryMemory()
        from infrastructure.memory.sql import SqlMemory

        return SqlMemory(self.session_factory)

    @property
    def memory_maintenance(self) -> MemoryMaintenance | None:
        """The same store, seen through the contract that may forget."""
        return self.memory  # type: ignore[return-value]

    @cached_property
    def memory_uses(self) -> MemoryUseLog:
        """Which memories work was given, and why. Built with no flag: with
        memory off nothing is recalled, so nothing is written to it."""
        if self._in_memory:
            from infrastructure.persistence.memory_use_repository import InMemoryMemoryUseLog

            return InMemoryMemoryUseLog()
        from infrastructure.persistence.memory_use_repository import SqlMemoryUseLog

        return SqlMemoryUseLog(self.session_factory)

    @cached_property
    def session_states(self) -> SessionStateRepository:
        """The compacted half of each thread's brief."""
        if self._in_memory:
            from infrastructure.persistence.session_repository import (
                InMemorySessionStateRepository,
            )

            return InMemorySessionStateRepository()
        from infrastructure.persistence.session_repository import SqlSessionStateRepository

        return SqlSessionStateRepository(self.session_factory)

    # --- Approvals --------------------------------------------------------------

    @cached_property
    def approval_repository(self) -> ApprovalRepository:
        if self._in_memory:
            from infrastructure.persistence.approval_repository import (
                InMemoryApprovalRepository,
            )

            return InMemoryApprovalRepository()
        from infrastructure.persistence.approval_repository import SqlApprovalRepository

        return SqlApprovalRepository(self.session_factory)

    @cached_property
    def capability_leases(self) -> CapabilityLeaseRepository:
        if self._in_memory:
            from infrastructure.persistence.approval_repository import (
                InMemoryCapabilityLeaseRepository,
            )

            return InMemoryCapabilityLeaseRepository()
        from infrastructure.persistence.approval_repository import (
            SqlCapabilityLeaseRepository,
        )

        return SqlCapabilityLeaseRepository(self.session_factory)

    def use_approval_confirmer(self, confirmer: Callable[..., object]) -> None:
        """Have approvals asked somewhere other than the terminal.

        The local interface calls this before anything runs. Asking on stdin
        when the person is looking at a browser would park every irreversible
        action on a prompt nobody can see.
        """
        self._confirmer = confirmer

    @cached_property
    def approval_service(self) -> ApprovalService | None:
        """None means nobody can be asked - and so nothing irreversible happens."""
        if not self.settings.approvals_enabled:
            return None
        from infrastructure.approvals.telegram import TelegramApprovalService

        if self._confirmer is None and TelegramApprovalService.configured(self.secret_resolver):
            # A machine told how to reach somebody uses that in preference to
            # stdin, because the case this covers is precisely the one where
            # nobody is looking at stdin. An interface that supplied its own
            # confirmer has already answered the question and wins over both.
            return TelegramApprovalService(
                self.approval_repository,
                self.secret_resolver,
                ttl_seconds=self.settings.approval_ttl_seconds or 900.0,
            )

        from infrastructure.approvals.service import LocalApprovalService

        if self._confirmer is None:
            return LocalApprovalService(
                self.approval_repository, mode=self.settings.approval_mode
            )
        return LocalApprovalService(
            self.approval_repository,
            mode=self.settings.approval_mode,
            confirmer=self._confirmer,  # type: ignore[arg-type]
            ttl_seconds=self.settings.approval_ttl_seconds,
            # An interface that supplies its own approver is the approver: the
            # terminal's stdin says nothing about whether anyone is watching.
            is_interactive=lambda: True,
        )

    # --- Workflows ----------------------------------------------------------------

    @cached_property
    def workflow_registry(self) -> WorkflowRegistry:
        from infrastructure.workflows.yaml_registry import YamlWorkflowRegistry

        return YamlWorkflowRegistry(self.settings.workflows_dir)

    @cached_property
    def workflow_runs(self) -> WorkflowRunRepository:
        if self._in_memory:
            from infrastructure.persistence.workflow_repository import (
                InMemoryWorkflowRunRepository,
            )

            return InMemoryWorkflowRunRepository()
        from infrastructure.persistence.workflow_repository import (
            SqlWorkflowRunRepository,
        )

        return SqlWorkflowRunRepository(self.session_factory)

    # --- Validation ---------------------------------------------------------------

    @cached_property
    def scenario_registry(self) -> ScenarioRegistry:
        from infrastructure.validation.yaml_registry import YamlScenarioRegistry

        return YamlScenarioRegistry(self.settings.scenarios_dir)

    @cached_property
    def validation_runs(self) -> ValidationRunRepository:
        if self._in_memory:
            from infrastructure.persistence.validation_run_repository import (
                InMemoryValidationRunRepository,
            )

            return InMemoryValidationRunRepository()
        from infrastructure.persistence.validation_run_repository import (
            SqlValidationRunRepository,
        )

        return SqlValidationRunRepository(self.session_factory)

    def available_requirements(self) -> frozenset[Requirement]:
        """What this machine can actually do, as a scenario would name it.

        Read off the same switches everything else reads, so a scenario is
        skipped for exactly the reason a tool would have been missing. Asking
        the settings twice - once here and once where the capability is built -
        is what would let the suite claim a browser that is not there.
        """
        settings = self.settings
        available = set()
        if settings.browser_tools_enabled:
            available.add(Requirement.BROWSER)
        from infrastructure.tools.code import sandbox_backend

        if (
            settings.code_execution_enabled
            and sandbox_backend().available
        ):
            available.add(Requirement.CODE_EXECUTION)
        if settings.computer_use_enabled:
            available.add(Requirement.COMPUTER_USE)
        if settings.memory_enabled:
            available.add(Requirement.MEMORY)
        if settings.workflows_enabled:
            available.add(Requirement.WORKFLOWS)
        if settings.approvals_enabled:
            available.add(Requirement.APPROVALS)
        if settings.integrations_enabled:
            available.add(Requirement.INTEGRATIONS)
        return frozenset(available)

    # --- Audit ------------------------------------------------------------------

    @cached_property
    def audit(self) -> AuditLog:
        """Always built, unlike memory.

        Memory is a feature a machine can do without; a record of what was done
        to that machine is not. There is no flag here for the same reason there
        is no flag on the approval gate.
        """
        if self._in_memory:
            from infrastructure.persistence.audit_repository import InMemoryAuditLog

            return InMemoryAuditLog()
        from infrastructure.persistence.audit_repository import SqlAuditLog

        return SqlAuditLog(self.session_factory)

    @cached_property
    def employee_repository(self):
        if self._in_memory:
            from infrastructure.persistence.employee_repository import (
                InMemoryEmployeeRepository,
            )

            return InMemoryEmployeeRepository()
        from infrastructure.persistence.employee_repository import SqlEmployeeRepository

        return SqlEmployeeRepository(self.session_factory)

    async def sync_employees(self) -> int:
        """Persist the declared employees so tasks can reference them.

        Called before running anything: the declarations are the source of
        truth, and the table has to know about an employee before a task can be
        assigned to it. It is also the last moment at which a declaration can be
        checked against the machine before work starts, so it is checked here -
        logged, not raised. A workforce of four with one bad declaration should
        run the other three, and the run that then goes wrong has a line saying
        why in front of it.
        """
        for issue in self.check_employees():
            self.logger.warning(
                "employee.declaration_issue",
                employee=issue.employee,
                severity=issue.severity.value,
                detail=issue.message,
            )
        return await self.employee_repository.sync(self.employee_registry.list())

    @cached_property
    def assignment_repository(self) -> AssignmentRepository:
        if self._in_memory:
            from infrastructure.persistence.assignment_repository import (
                InMemoryAssignmentRepository,
            )

            return InMemoryAssignmentRepository()
        from infrastructure.persistence.assignment_repository import (
            SqlAssignmentRepository,
        )

        return SqlAssignmentRepository(self.session_factory)

    # --- The manager's own record ---------------------------------------------

    @cached_property
    def conversation_repository(self) -> ConversationRepository:
        """The threads an interface groups requests into.

        Built like every other repository and with no flag: a conversation is
        four columns, and an interface that cannot show what was asked five
        minutes ago is not a conversational surface.
        """
        if self._in_memory:
            from infrastructure.persistence.conversation_repository import (
                InMemoryConversationRepository,
            )

            return InMemoryConversationRepository()
        from infrastructure.persistence.conversation_repository import (
            SqlConversationRepository,
        )

        return SqlConversationRepository(self.session_factory)

    @cached_property
    def objective_repository(self) -> ObjectiveRepository:
        if self._in_memory:
            from infrastructure.persistence.objective_repository import (
                InMemoryObjectiveRepository,
            )

            return InMemoryObjectiveRepository()
        from infrastructure.persistence.objective_repository import SqlObjectiveRepository

        return SqlObjectiveRepository(self.session_factory)

    @cached_property
    def plan_repository(self) -> PlanRepository:
        if self._in_memory:
            from infrastructure.persistence.plan_repository import InMemoryPlanRepository

            # Paired with the task repository so an in-memory run reads a
            # plan's task states from the same place a SQLite one does.
            return InMemoryPlanRepository(self.task_repository)
        from infrastructure.persistence.plan_repository import SqlPlanRepository

        return SqlPlanRepository(self.session_factory)

    # --- Work that starts on its own ------------------------------------------

    @cached_property
    def schedule_repository(self) -> ScheduleRepository:
        if self._in_memory:
            from infrastructure.persistence.schedule_repository import (
                InMemoryScheduleRepository,
            )

            return InMemoryScheduleRepository()
        from infrastructure.persistence.schedule_repository import SqlScheduleRepository

        return SqlScheduleRepository(self.session_factory)

    @cached_property
    def event_log(self) -> EventLog:
        # Built with no flag, for the same reason the audit log is: an event
        # nobody recorded is the one nobody can explain a run from afterwards.
        if self._in_memory:
            from infrastructure.persistence.schedule_repository import InMemoryEventLog

            return InMemoryEventLog()
        from infrastructure.persistence.schedule_repository import SqlEventLog

        return SqlEventLog(self.session_factory)

    async def aclose(self) -> None:
        if "llm_factory" in self.__dict__:
            await self.llm_factory.aclose()
        # Only what was actually built: a run that never searched has no client
        # to close, and asking for one here would create it in order to do so.
        for name in ("browser", "search_engine", "integrations"):
            resource = self.__dict__.get(name)
            if resource is not None:
                # Integrations join the list because a connected server is a
                # subprocess this runtime started, and a process that exits
                # without stopping them leaves them running against nothing.
                await resource.aclose()
        if "engine" in self.__dict__:
            await self.engine.dispose()
