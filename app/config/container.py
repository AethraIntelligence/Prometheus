"""The composition root: settings, adapters and the runtime meet here.

`infrastructure/container.py` builds adapters and knows nothing above itself.
Assembling the employee runtime needs both `application` and `infrastructure`,
and this is the one place allowed to see both - which is what a composition root
is for. See docs/adr/0001.
"""

from __future__ import annotations

from app.config.settings import Settings, get_settings
from application.computer.screen_reader import LLMScreenReader
from application.employee_runtime.approvals import ApprovalGate
from application.employee_runtime.executor import Executor
from application.employee_runtime.planner import Planner
from application.employee_runtime.runtime import EmployeeRuntime, RuntimeDependencies
from application.employee_runtime.verifier import Verifier
from application.integrations.service import IntegrationService
from application.interface.activity import Activity
from application.interface.runs import Runs
from application.interface.service import (
    DEFAULT_LIMIT,
    PrometheusService,
    ServiceDependencies,
)
from application.knowledge.service import KnowledgeService
from application.knowledge.workspace import WorkspaceKnowledge
from application.memory.assembler import ContextAssembler
from application.memory.consolidation import Consolidator
from application.memory.distiller import OutcomeDistiller
from application.memory.recorder import MemoryRecorder
from application.memory.workspace import WorkspaceMemory
from application.prometheus.delegation import CapabilityDelegator
from application.prometheus.intent import IntentReader
from application.prometheus.language import DEFAULT_LANGUAGE
from application.prometheus.manager import PrometheusManager
from application.prometheus.planner import ObjectivePlanner
from application.prometheus.reconciliation import Reconciler
from application.prometheus.supervisor import Supervisor
from application.prometheus.synthesis import Synthesizer
from application.prometheus.verification import ObjectiveVerifier
from application.providers.service import ProviderService
from application.task_runner import TaskRunner
from application.validation.harness import ValidationHarness
from application.workflows.engine import WorkflowEngine
from application.workspaces.service import WorkspaceService
from domain.approvals.protocols import ApprovalWaiter
from domain.employees.definition import EmployeeDefinition
from domain.errors import PrometheusError
from infrastructure.container import Container
from infrastructure.knowledge.extraction import Extractors
from infrastructure.llm.discovery import LocalModelDiscovery
from infrastructure.llm.providers import KINDS
from infrastructure.mcp.connector import cached_connector, mcp_connector
from infrastructure.validation.approver import DeclaredApprover


def build_container(settings: Settings | None = None, *, in_memory: bool = False) -> Container:
    """Build the container, and hand it the one dependency it cannot build.

    Reading a screen needs an application component (`LLMScreenReader`) and a
    model that only the container can route to. Neither layer may reach the
    other, so the wire is made here and passed as a callable: nothing is built,
    and no model is routed, for a run that never looks at a screen.
    """
    container = Container(settings or get_settings(), in_memory=in_memory)
    container.use_screen_reader(
        lambda: LLMScreenReader(container.llm_for(*LLMScreenReader.routing()))
    )
    container.use_integrations(lambda: build_integrations(container))
    return container


def build_integrations(container: Container) -> IntegrationService:
    """Assemble the lifecycle of connected services.

    Here rather than in the infrastructure container for the same reason the
    screen reader is: the service is an application component and the connector
    is an adapter, and this is the one place allowed to see both. The service
    itself never learns what MCP is - it is handed a way to connect.
    """
    return IntegrationService(
        container.integration_repository,
        container.tool_registry,
        mcp_connector(timeout_seconds=container.settings.integration_timeout_seconds),
        # Start-up reads the record; it does not launch every configured server
        # in order to re-learn what is already written down.
        restorer=cached_connector(
            timeout_seconds=container.settings.integration_timeout_seconds
        ),
        secrets=container.secret_resolver,
        on_change=container.refresh_grants,
    )


async def load_grants(container: Container) -> None:
    """Let the employee registry see what is connected, and nothing more.

    What a *listing* needs. `prepare` gets the same thing as a side effect of
    restoring, but printing the declarations must not start a server or write
    to the store - and a reader that could do either would be a command with a
    side effect nobody expects from the word "list".
    """
    try:
        container.refresh_grants(await container.integration_repository.list())
    except PrometheusError as error:
        container.logger.warning("integrations.not_read", error=str(error))


async def _restore_credentials(container: Container) -> None:
    """Load the sealed credentials, and take over from the file store once.

    The import runs on every start and does nothing after the first: a name
    already in the store is skipped, so this costs one query on a machine that
    was never on the old version. The file is left where it is - see
    `Container.legacy_credentials` for why.
    """
    credentials = container.credential_store
    restore = getattr(credentials, "restore", None)
    if restore is None:
        return
    try:
        await restore()
        legacy = container.legacy_credentials
        names = await legacy.names()
        if names:
            await credentials.import_from(legacy, names)  # type: ignore[attr-defined]
    except PrometheusError as error:
        container.logger.warning("credentials.not_restored", error=str(error))


async def _load_catalog(container: Container) -> None:
    """Replace the shipped catalog with what this installation holds.

    Guarded, like everything else here: a machine whose catalog rows cannot be
    read runs on the file it shipped with, which is the configuration a fresh
    clone has and a perfectly good one. Failing to start because a settings
    table is unreadable would turn an editable preference into a hard dependency.
    """
    await container.connection_directory.restore()
    if container.catalog_source.is_overridden:
        return
    try:
        container.use_catalog(await container.catalog_source.load())
    except PrometheusError as error:
        container.logger.warning("catalog.not_loaded", error=str(error))


async def prepare(container: Container) -> None:
    """Everything that has to be true before this process does any work.

    Three steps, and the order is the point.

    The workspaces are declared first, and cost one query: the filesystem tools
    ask the context for a root on every call and cannot wait on a read, so what
    exists has to be in it before anything runs. A machine that has never
    created a second one gets the row migration 014 wrote.

    The integrations are restored next, because syncing the employees checks
    their declarations against what this machine offers - and a grant to a
    service that has not been restored yet reads, correctly but uselessly, as a
    grant to a service nobody connected.

    The credentials are restored before both, because an integration reached
    during the restore resolves a secret, and a resolver that has not read its
    rows yet answers "no such credential" - which is indistinguishable from the
    user never having entered one.

    Restoring is guarded the way remembering is: a machine whose integration
    store cannot be read should run the work it was asked for without them,
    with a line in the log saying so. Losing a capability is worse than not
    having it, and losing the whole run over it is worse again.
    """
    await build_workspaces(container).list()
    await _restore_credentials(container)
    await _load_catalog(container)
    integrations = getattr(container, "integrations", None)
    if integrations is not None:
        try:
            restored = await integrations.restore()
            if restored:
                container.logger.info("integrations.restored", count=restored)
        except PrometheusError as error:
            container.logger.warning("integrations.not_restored", error=str(error))
    await container.sync_employees()


def build_memory(
    container: Container,
) -> tuple[ContextAssembler | None, MemoryRecorder | None]:
    """The two halves of memory, or neither - and the run's context either way.

    Neither when memory is switched off, and that is the whole of the difference
    it makes: the runtime takes both as optional, so a machine with memory off
    runs the Phase 8 loop rather than a degraded Phase 9 one.

    The assembler outlives that switch, because since Phase 15 it also carries
    retrieved documents (ADR 0016), and the two capabilities are separate: a
    machine that wants its own documents searched should not have to keep notes
    about its own runs in order to get them.
    """
    settings = container.settings
    memory = container.memory
    maintenance = container.memory_maintenance
    if memory is None or maintenance is None:
        assembler = (
            ContextAssembler(
                None,
                retriever=container.retriever,
                knowledge_limit=settings.knowledge_recall_limit,
            )
            if container.retriever is not None
            else None
        )
        return assembler, None
    recorder = MemoryRecorder(
        memory,
        distiller=OutcomeDistiller(container.llm_for(*OutcomeDistiller.routing())),
        consolidator=Consolidator(
            container.llm_for(*Consolidator.routing()),
            memory,
            maintenance,
            threshold=settings.memory_consolidation_threshold,
        ),
    )
    return (
        ContextAssembler(
            memory,
            limit=settings.memory_recall_limit,
            retriever=container.retriever,
            knowledge_limit=settings.knowledge_recall_limit,
        ),
        recorder,
    )


async def build_runtime(container: Container, definition: EmployeeDefinition) -> EmployeeRuntime:
    """Assemble the one runtime for a given employee declaration.

    Every employee gets the same three stages and the same loop. What differs is
    the declaration passed in: role, goals, tools, model profile, limits.
    """
    context, recorder = build_memory(container)
    return EmployeeRuntime(
        definition,
        RuntimeDependencies(
            planner=Planner(container.llm_for(*Planner.routing())),
            executor=Executor(
                container.llm_for(*Executor.routing(definition)),
                container.tool_registry,
                limits=definition.limits,
                approvals=ApprovalGate(container.approval_service, audit=container.audit),
                call_log=container.tool_call_log,
                audit=container.audit,
                progress=container.progress,
                cancellation=container.cancellations,
            ),
            verifier=Verifier(container.llm_for(*Verifier.routing())),
            tasks=container.task_repository,
            tools=container.tool_registry,
            limits=definition.limits,
            system_prompt=definition.system_prompt,
            progress=container.progress,
            context=context,
            recorder=recorder,
        ),
    )


def build_manager(container: Container) -> PrometheusManager:
    """Assemble Prometheus.

    Six model-facing components, each routed for what it is: comprehension and
    decomposition get a good model, choosing from a short list gets a cheap one,
    judging - the verdict and deciding which of two people is right - is held
    above the bottom of the catalog, and the answer the user reads gets a good
    model again. None of them names a model, and none of them names an employee -
    the workforce arrives from the registry and the work is done through
    `TaskExecution`, which is the task runner.
    """
    registry = container.employee_registry
    _, recorder = build_memory(container)
    # The one setting the manager needs. Read here rather than inside
    # `application/`, which is not allowed to know a settings object exists.
    language = getattr(container.settings, "response_language", DEFAULT_LANGUAGE)
    return PrometheusManager(
        intent=IntentReader(
            container.llm_for(*IntentReader.routing()), language=language
        ),
        planner=ObjectivePlanner(container.llm_for(*ObjectivePlanner.routing())),
        supervisor=Supervisor(
            execution=build_task_runner(container),
            delegator=CapabilityDelegator(
                container.llm_for(*CapabilityDelegator.routing()), registry
            ),
            progress=container.progress,
        ),
        verifier=ObjectiveVerifier(container.llm_for(*ObjectiveVerifier.routing())),
        synthesizer=Synthesizer(
            container.llm_for(*Synthesizer.routing()), language=language
        ),
        reconciler=Reconciler(container.llm_for(*Reconciler.routing())),
        registry=registry,
        objectives=container.objective_repository,
        plans=container.plan_repository,
        progress=container.progress,
        memory=(
            WorkspaceMemory(container.memory, recorder)
            if container.memory is not None and recorder is not None
            else None
        ),
        knowledge=(
            WorkspaceKnowledge(container.retriever)
            if container.retriever is not None
            else None
        ),
    )


def build_task_runner(container: Container) -> TaskRunner:
    async def _runtime(definition: EmployeeDefinition) -> EmployeeRuntime:
        return await build_runtime(container, definition)

    return TaskRunner(
        tasks=container.task_repository,
        assignments=container.assignment_repository,
        registry=container.employee_registry,
        build_runtime=_runtime,
        progress=container.progress,
        workspaces=container.workspaces,
    )


def build_workspaces(container: Container) -> WorkspaceService:
    """The records and the switch, which are two halves of one answer.

    The repository says what exists; the context says which one this process is
    in and where its files are. Neither is useful without the other, so nothing
    above is handed one of them.
    """
    return WorkspaceService(
        repository=container.workspace_repository, context=container.workspaces
    )


def build_knowledge(container: Container) -> KnowledgeService | None:
    """The lifecycle of the user's own documents, or None where that is off.

    The extractors are built here rather than in the infrastructure container
    for no deeper reason than that they are a set: which formats this machine
    can read is a composition decision, and adding one is adding a class to the
    tuple.
    """
    store = container.knowledge_store
    if store is None:
        return None
    return KnowledgeService(
        store=store, extractors=Extractors(), embeddings=container.embeddings
    )


def build_service(
    container: Container, waiter: ApprovalWaiter, *, history_limit: int = DEFAULT_LIMIT
) -> PrometheusService:
    """Assemble the boundary every interface talks to.

    The waiter is passed in rather than read off the container because it is a
    property of the *interface*, not of the machine: a terminal answers an
    approval on the call stack that asked, a page answers it from a request
    arriving later, and whoever built the surface is the only one who knows
    which. Nothing else here differs between one interface and the next.
    """
    return PrometheusService(
        ServiceDependencies(
            runs=Runs(
                runner=build_task_runner(container),
                manager=build_manager(container),
                tasks=container.task_repository,
                objectives=container.objective_repository,
                cancellations=container.cancellations,
                approvals=waiter,
            ),
            activity=Activity(
                container.progress,
                tasks=container.task_repository,
                objectives=container.objective_repository,
                plans=container.plan_repository,
            ),
            conversations=container.conversation_repository,
            objectives=container.objective_repository,
            plans=container.plan_repository,
            tasks=container.task_repository,
            employees=container.employee_registry,
            tools=container.tool_registry,
            approvals=container.approval_repository,
            waiter=waiter,
            tool_calls=container.tool_call_log,
            llm_calls=container.llm_call_log,
            approval_service=container.approval_service,
            integrations=container.integrations,
            workspaces=build_workspaces(container),
            knowledge=build_knowledge(container),
            retriever=container.retriever,
            memory=container.memory,
            credentials=container.credential_store,
            providers=build_providers(container),
            history_limit=history_limit,
        )
    )


def build_workflow_engine(container: Container) -> WorkflowEngine:
    """A workflow runs through the same runner every other task does.

    That is the whole point of the engine being three lines: a predefined
    process and a plan Prometheus invented differ in where the decomposition came
    from and in nothing below it - same employees, same limits, same gate.
    """
    return WorkflowEngine(
        container.workflow_registry,
        build_task_runner(container),
        container.workflow_runs,
    )


def build_providers(container: Container) -> ProviderService:
    """Settings for providers, models and where work goes.

    The kinds and the discovery function are handed in from here: the list of
    what this machine can talk to belongs beside the adapters, and the
    application layer may not import them (ADR 0001).

    `on_change` is what makes a settings page take effect without a restart -
    the catalog is re-read and anything built from it is dropped.
    """

    async def reload() -> None:
        await container.connection_directory.restore()
        if not container.catalog_source.is_overridden:
            container.use_catalog(await container.catalog_source.load())

    return ProviderService(
        container.connections,
        container.catalog_repository,
        credentials=container.credential_store,
        kinds=KINDS,
        discover=LocalModelDiscovery(),
        on_change=reload,
    )


def build_harness(container: Container) -> ValidationHarness:
    """Assemble the validation harness with all three of the platform's doors.

    It gets the manager, the task runner and the workflow engine - the same
    three objects the CLI builds for `ask-prometheus`, `run-task` and
    `run-workflow` - and nothing else that can do work. Everything else handed
    in here is a way of reading what happened afterwards.
    """
    # Installed here rather than inside the harness: what answers an approval
    # is an adapter, and the composition root is the one place allowed to hand
    # one to the runtime. The harness is given the same object as a contract it
    # can only open and close around a run.
    approver = DeclaredApprover()
    container.use_approval_confirmer(approver.confirm)
    return ValidationHarness(
        scenarios=container.scenario_registry,
        runs=container.validation_runs,
        tasks=container.task_repository,
        workspace=container.settings.ensure_file_root(),
        available=container.available_requirements(),
        objectives=build_manager(container),
        employees=build_task_runner(container),
        workflows=build_workflow_engine(container),
        tool_calls=container.tool_call_log,
        audit=container.audit,  # type: ignore[arg-type]
        approvals=container.approval_repository,
        memory=container.memory,
        knowledge=build_knowledge(container),
        workspaces=build_workspaces(container),
        approver=approver,
    )
