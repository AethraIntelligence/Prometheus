"""The work loop: think, act, observe, repeat - inside a budget.

Two things here are deliberate and easy to get wrong.

**Observe is a real step.** A loop that goes straight from a tool result to the
next tool call has nowhere to notice that the result was empty, contradicted the
last one, or already answered the question. So every action is followed by an
explicit interpretation, recorded on the task, and the next decision is made
from that rather than from raw output.

**Limits are checked before each step, not after the run.** An agent that loops
does not fail loudly; it keeps working and keeps spending. Stopping at a budget
is a normal outcome with a normal result, not a crash.

**Every tool call passes the same three checks, in this order:** may this
employee use this tool at all, does this particular call need a human, and what
did it cost. Permission first, because refusing an unknown tool is cheaper than
asking about it; approval second, because a question the user answers is only
worth asking for a call that would otherwise happen.

**Progress is announced, cancellation is asked for, and both are optional.**
The loop tells a sink what it is doing and asks a signal whether it should stop,
between steps and before each tool call. Both default to a component that does
nothing, so a run nobody is watching costs nothing to be watchable, and a run
nobody is cancelling never pays for the question.

**A tool that keeps being refused is withdrawn.** Being told no is information,
and once is enough for a model to act on; fifteen times is a step budget spent
on a conversation with the gate. So the refusals in this task's own transcript
decide which tools the next request offers, and a tool refused twice is not one
of them (`domain.tools.refusals`).

**The emergency stop is read twice, and the second time is the one that matters.**
Between steps it ends the run the way a cancellation does. Then, for every call,
it is read again after the approval gate has answered and immediately before the
tool is called: an approval granted in the same instant somebody pulled the brake
must not carry the action past it (`domain.safety.emergency`).

**The interface hierarchy is decided from what the employee has, and recorded.**
The tools an employee is allowed to use are what say whether it can reach the
world through an API, a browser or a screen, so the choice is made here, once,
from `list_specs` - logged before the first step, stated to the model, and
written against every call. A trace can then answer why a run clicked on a
picture of a button instead of calling something.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

import structlog

from application.employee_runtime.approvals import ApprovalGate, StatusSink
from application.employee_runtime.transcript import Transcript
from domain.audit.protocols import AuditLog, AuditRecord
from domain.capabilities.models import CapabilityRequirement
from domain.computer.interfaces import InterfaceLevel, describe, select
from domain.employees.definition import EmployeeDefinition
from domain.employees.limits import ExecutionLimits, LimitKind
from domain.errors import PermissionDeniedError, ToolNotFoundError
from domain.integrations.untrusted import PLATFORM_POLICY, Provenance
from domain.llm.models import (
    LLMRequest,
    Message,
    Role,
    RoutingHints,
    TaskKind,
    ToolCallRequest,
)
from domain.llm.protocols import LLM
from domain.policies.risk import Effect
from domain.safety.effects import EffectGate
from domain.safety.emergency import EmergencyStop
from domain.secrets.models import redact
from domain.tasks.cancellation import CancellationSignal, NeverCancelled
from domain.tasks.plan import Observation, TaskPlan
from domain.tasks.progress import NullProgress, ProgressEvent, ProgressKind, ProgressSink
from domain.tasks.task import Task, TaskStatus
from domain.tools.artifacts import written_path
from domain.tools.models import ToolResult
from domain.tools.protocols import Tool, ToolRegistry
from domain.tools.refusals import REFUSAL_LIMIT, REFUSED, refusal_counts, withheld
from domain.tools.results import ContextResult, for_context
from domain.tools.telemetry import ToolCallLog, ToolCallRecord

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """One turn of the loop, and whether the work should continue."""

    transcript: Transcript
    finished: bool = False
    answer: str = ""
    stopped_by: LimitKind | None = None
    #: A person asked for this task to stop. Not a failure and not a limit: the
    #: work that was done stands, and nothing more is attempted.
    cancelled: bool = False


class Executor:
    """Implements `domain.employees.protocols.Executor`."""

    def __init__(
        self,
        llm: LLM,
        tools: ToolRegistry,
        *,
        limits: ExecutionLimits | None = None,
        approvals: ApprovalGate | None = None,
        call_log: ToolCallLog | None = None,
        audit: AuditLog | None = None,
        progress: ProgressSink | None = None,
        cancellation: CancellationSignal | None = None,
        stop: EmergencyStop | None = None,
        effects: EffectGate | None = None,
        # Injected so tests can control time instead of waiting for it.
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._limits = limits or ExecutionLimits()
        self._approvals = approvals or ApprovalGate()
        self._call_log = call_log
        self._audit = audit
        self._progress = progress or NullProgress()
        self._cancellation = cancellation or NeverCancelled()
        self._stop = stop
        self._effects = effects
        self._clock = clock

    async def run(
        self,
        task: Task,
        definition: EmployeeDefinition,
        transcript: Transcript,
        *,
        on_step: Callable[[Transcript], Awaitable[None]] | None = None,
        on_status: StatusSink | None = None,
    ) -> StepOutcome:
        """Advance the task until it finishes or runs out of budget.

        `on_step` is awaited after each step with the current transcript. That
        callback is what makes a killed process resumable, so it runs before the
        next model call, not at the end.

        `on_status` is how the loop says the task has stopped being RUNNING
        without owning the task row: parked on a person, and then back. The
        executor does not persist tasks - the runtime does - and a loop that
        reached for a repository to say what it is doing would be the wrong half
        of the system holding it.
        """
        started = self._clock()
        granted = self._tools.list_specs(definition)
        choice = select(spec.interface_level for spec in granted)
        if choice is not None:
            log.info(
                "interface.selected",
                task_id=str(task.id),
                employee=definition.name,
                level=choice.level.value,
                reason=choice.reason,
                available=[level.value for level in choice.available],
            )

        while True:
            await self._wait_if_paused(task, on_status)
            if self._halted(task):
                log.info("task.cancelled", task_id=str(task.id), steps=transcript.steps)
                return StepOutcome(
                    transcript=transcript,
                    finished=True,
                    answer=self._best_answer(transcript),
                    cancelled=True,
                )

            exceeded = self._limits.exceeded_by(
                steps=transcript.steps,
                cost_usd=transcript.cost_usd,
                elapsed_seconds=self._clock() - started,
            )
            if exceeded is not None:
                log.warning(
                    "task.limit_reached",
                    task_id=str(task.id),
                    limit=exceeded.value,
                    steps=transcript.steps,
                    cost_usd=round(transcript.cost_usd, 6),
                )
                return StepOutcome(
                    transcript=transcript,
                    finished=True,
                    answer=self._best_answer(transcript),
                    stopped_by=exceeded,
                )

            pending = self._pending_calls(transcript)
            if pending:
                # The process stopped after the model requested an action but
                # before every result reached the transcript. Resume that exact
                # exchange instead of asking the model to invent new call ids.
                calls = pending
            else:
                # Recomputed from the transcript each turn rather than tracked
                # in a local, so a resumed run offers the same tools.
                barred = withheld(transcript.observations)
                offered = tuple(spec for spec in granted if spec.name not in barred)
                response = await self._llm.generate(
                    LLMRequest(
                        messages=transcript.messages_for_model(),
                        tools=offered,
                        temperature=definition.model_profile.temperature,
                    )
                )
                transcript = transcript.with_spend(response.usage.cost_usd).advanced()

                if not response.tool_calls:
                    transcript = transcript.with_message(Message.assistant(response.content))
                    if on_step is not None:
                        await on_step(transcript)
                    return StepOutcome(
                        transcript=transcript, finished=True, answer=response.content
                    )

                transcript = transcript.with_message(
                    Message.assistant(response.content, tool_calls=response.tool_calls)
                )
                calls = response.tool_calls
                # The model's intent is durable before the first external
                # action. On restart `_pending_calls` can continue the same ids.
                if on_step is not None:
                    await on_step(transcript)

            for call in calls:
                await self._wait_if_paused(task, on_status)
                if self._halted(task):
                    # Asked to stop between two calls of the same step: the
                    # remaining calls are not made, and the ones already made
                    # stay in the transcript where the next reader can see them.
                    return StepOutcome(
                        transcript=transcript,
                        finished=True,
                        answer=self._best_answer(transcript),
                        cancelled=True,
                    )
                await self._announce(
                    task,
                    ProgressKind.TOOL_CALL,
                    f"{call.name}({self._arguments_of(call)})",
                    step=transcript.steps,
                    payload={"tool": call.name, "arguments": redact(call.arguments)},
                )
                result, refused = await self._invoke(
                    call,
                    definition,
                    task,
                    on_status,
                    untrusted_context=transcript.untrusted_sources,
                )
                observation = self._observe(
                    transcript.steps,
                    call,
                    result,
                    definition,
                    refused=refused,
                    refusals_so_far=refusal_counts(transcript.observations)[call.name],
                )
                transcript = transcript.with_observation(observation).with_message(
                    Message.tool(observation.summary, call.id)
                )
                # One call at a time, so a crash during the next call never
                # loses the completed result of this one.
                if on_step is not None:
                    await on_step(transcript)
                await self._announce(
                    task,
                    ProgressKind.OBSERVATION,
                    observation.summary,
                    step=observation.step,
                    payload={"succeeded": observation.succeeded, **observation.details},
                )

    def _halted(self, task: Task) -> bool:
        """Asked to stop: this task by a person, or all work by the emergency stop."""
        if self._cancellation.is_cancelled(task.id):
            return True
        return self._stop is not None and self._stop.engaged()

    async def _wait_if_paused(
        self,
        task: Task,
        status: StatusSink | None,
    ) -> None:
        """Park only at a safe boundary, never in the middle of a tool call."""
        is_paused = getattr(self._cancellation, "is_paused", None)
        if is_paused is None or not is_paused(task.id):
            return
        if status is not None:
            await status(TaskStatus.PAUSED)
        await self._announce(
            task,
            ProgressKind.STAGE,
            "Paused safely. Resume continues from this step.",
        )
        wait_until_resumed = getattr(self._cancellation, "wait_until_resumed", None)
        if wait_until_resumed is not None:
            await wait_until_resumed(task.id)
        if not self._cancellation.is_cancelled(task.id) and status is not None:
            await status(TaskStatus.RUNNING)


    @staticmethod
    def _pending_calls(transcript: Transcript) -> tuple[ToolCallRequest, ...]:
        """Tool requests in the latest exchange that have no durable reply yet."""
        for index in range(len(transcript.messages) - 1, -1, -1):
            message = transcript.messages[index]
            if message.role is not Role.ASSISTANT or not message.tool_calls:
                continue
            answered = {
                item.tool_call_id
                for item in transcript.messages[index + 1 :]
                if item.role is Role.TOOL and item.tool_call_id
            }
            return tuple(call for call in message.tool_calls if call.id not in answered)
        return ()

    # --- Announcing -----------------------------------------------------------

    async def _announce(
        self,
        task: Task,
        kind: ProgressKind,
        message: str,
        *,
        step: int = 0,
        payload: dict[str, object] | None = None,
    ) -> None:
        """Tell whoever is watching. Never at the cost of the work itself."""
        try:
            await self._progress.emit(
                ProgressEvent(
                    task_id=task.id,
                    kind=kind,
                    message=message,
                    step=step,
                    payload=dict(payload or {}),
                    workspace_id=task.workspace_id,
                )
            )
        except Exception as error:  # a watcher must not be able to fail a run
            log.warning("progress.emit_failed", kind=kind.value, error=str(error))

    @staticmethod
    def _arguments_of(call: ToolCallRequest) -> str:
        """The call as a person would read it, with credentials already gone."""
        return ", ".join(f"{key}={value!r}" for key, value in redact(call.arguments).items())

    # --- Acting ---------------------------------------------------------------

    async def _invoke(
        self,
        call: ToolCallRequest,
        definition: EmployeeDefinition,
        task: Task,
        on_status: StatusSink | None = None,
        *,
        untrusted_context: tuple[Provenance, ...] = (),
    ) -> tuple[ToolResult, bool]:
        """Run the call, and say whether it was refused rather than merely failed.

        The second half of that answer is what `domain.tools.refusals` counts,
        and the distinction is the whole of it: a tool that ran and returned an
        error may work next time, a tool that was never reached will not.
        """
        replayed = await self._previous_result(task, call)
        if replayed is not None:
            return replayed, False

        if self._stop is not None and self._stop.engaged():
            reason = self._stopped(call)
            await self._record(task, call, ToolResult.failure(reason))
            return ToolResult.failure(reason), True

        try:
            tool = self._tools.get(call.name, definition)
        except (ToolNotFoundError, PermissionDeniedError) as error:
            # Not a crash: the model asked for something it cannot have, and
            # being told so is information it can act on.
            log.info("tool.refused", tool=call.name, employee=definition.name, reason=str(error))
            return ToolResult.failure(str(error)), True

        gate = await self._approvals.check(
            tool,
            call.arguments,
            task,
            definition,
            status=on_status,
            untrusted_context=tuple(untrusted_context),
        )
        if not gate.allowed:
            log.info("tool.not_approved", tool=call.name, task_id=str(task.id))
            await self._record(task, call, ToolResult.failure(gate.reason))
            return ToolResult.failure(gate.reason), True

        reserved = await self._reserve(task, call, tool.spec.interface_level)
        if reserved is False:
            replayed = await self._previous_result(task, call)
            if replayed is not None:
                return replayed, False
            return ToolResult.failure("The tool call could not be reserved safely."), False

        if self._stop is not None and self._stop.engaged():
            # The approval/effect boundary. The gate may have answered yes a
            # moment ago; the stop is newer than that answer and wins. The
            # reservation is closed as not performed, so a later reader of the
            # ledger does not mistake it for an action whose outcome is unknown.
            reason = self._stopped(call)
            stopped = ToolResult.failure(reason)
            log.warning("tool.stopped_before_effect", tool=call.name, task_id=str(task.id))
            if reserved is None:
                await self._record(task, call, stopped, tool.spec.interface_level)
            else:
                await self._complete(task, call, stopped, tool.spec.interface_level)
            await self._audit_stopped(task, definition, tool, call, reason)
            return stopped, True

        started = self._clock()
        try:
            if self._effects is not None and tool.spec.effect is not Effect.READ:
                # Through the gate an update holds: an effect is never cut in
                # half by a restart, and one that waits here resumes cleanly.
                async with self._effects.entering(task.id, call.name, tool.spec.effect.value):
                    if self._stop is not None and self._stop.engaged():
                        result = ToolResult.failure(self._stopped(call))
                    else:
                        result = await tool.execute(call.arguments)
            else:
                result = await tool.execute(call.arguments)
        except Exception as error:  # a tool must not take the task down with it
            log.warning("tool.failed", tool=call.name, error=str(error))
            result = ToolResult.failure(f"{type(error).__name__}: {error}")

        if not result.latency_ms:
            result = replace(result, latency_ms=int((self._clock() - started) * 1000))
        if reserved is None:
            await self._record(task, call, result, tool.spec.interface_level)
        else:
            await self._complete(task, call, result, tool.spec.interface_level)
        await self._audit_call(task, definition, tool, call, result)
        return result, False

    async def _previous_result(
        self, task: Task, call: ToolCallRequest
    ) -> ToolResult | None:
        if self._call_log is None or not call.id or not hasattr(self._call_log, "get_call"):
            return None
        try:
            previous = await self._call_log.get_call(task.id, call.id)
        except Exception as error:
            log.warning("tool.idempotency_read_failed", tool=call.name, error=str(error))
            return ToolResult.failure("The tool-call ledger could not be read safely.")
        if previous is None:
            return None
        if not previous.completed:
            return ToolResult.failure(
                "A previous attempt may already have performed this action; "
                "its outcome is unknown, so it was not repeated."
            )
        return ToolResult(
            success=previous.success,
            output=previous.output,
            error=previous.error,
            latency_ms=previous.latency_ms,
        )

    async def _reserve(
        self, task: Task, call: ToolCallRequest, interface: InterfaceLevel
    ) -> bool | None:
        """True for a durable reservation, None for a legacy telemetry sink."""
        if self._call_log is None or not call.id or not hasattr(self._call_log, "reserve"):
            return None
        try:
            return await self._call_log.reserve(
                ToolCallRecord(
                    tool=call.name,
                    success=False,
                    task_id=task.id,
                    call_id=call.id,
                    completed=False,
                    input_data=call.arguments,
                    interface=interface,
                )
            )
        except Exception as error:
            log.warning("tool.idempotency_reserve_failed", tool=call.name, error=str(error))
            return False

    async def _complete(
        self,
        task: Task,
        call: ToolCallRequest,
        result: ToolResult,
        interface: InterfaceLevel,
    ) -> None:
        assert self._call_log is not None
        try:
            await self._call_log.complete(
                ToolCallRecord(
                    tool=call.name,
                    success=result.success,
                    latency_ms=result.latency_ms,
                    task_id=task.id,
                    call_id=call.id,
                    completed=True,
                    input_data=call.arguments,
                    output=result.output,
                    error=result.error,
                    interface=interface,
                )
            )
        except Exception as error:
            # The action already happened. Never fail into an automatic retry;
            # the STARTED reservation remains the conservative recovery truth.
            log.warning("tool.idempotency_complete_failed", tool=call.name, error=str(error))

    async def _audit_call(
        self,
        task: Task,
        definition: EmployeeDefinition,
        tool: Tool,
        call: ToolCallRequest,
        result: ToolResult,
    ) -> None:
        """Who did what, next to what it cost. Never at the cost of the work.

        Distinct from `_record`, which is accounting: that one answers what this
        run spent, this one answers who acted on the machine. They agree on the
        successful calls and diverge on the refused ones, which is the pair of
        questions worth being able to ask separately.
        """
        if self._audit is None:
            return
        try:
            await self._audit.record(
                AuditRecord(
                    action=f"{call.name}({self._arguments_of(call)})",
                    actor_kind=definition.actor_kind,
                    actor_id=definition.actor_id,
                    result="SUCCESS" if result.success else "FAILURE",
                    workspace_id=task.workspace_id,
                    task_id=task.id,
                    tool=call.name,
                    latency_ms=result.latency_ms,
                    details={
                        "effect": tool.spec.effect.value,
                        "interface": tool.spec.interface_level.value,
                        **({"error": result.error} if result.error else {}),
                    },
                )
            )
        except Exception as error:
            log.warning("audit.not_recorded", tool=call.name, error=str(error))

    def _stopped(self, call: ToolCallRequest) -> str:
        why = self._stop.reason if self._stop is not None else ""
        return (
            f"{call.name} was not performed: all work is stopped ({why}). "
            "Do not retry it; nothing more can be done until a person resumes work."
        )

    async def _audit_stopped(
        self,
        task: Task,
        definition: EmployeeDefinition,
        tool: Tool,
        call: ToolCallRequest,
        reason: str,
    ) -> None:
        """A refusal the gate never saw: it said yes, and the stop came after."""
        if self._audit is None:
            return
        try:
            await self._audit.record(
                AuditRecord(
                    action=f"{call.name}({self._arguments_of(call)})",
                    actor_kind=definition.actor_kind,
                    actor_id=definition.actor_id,
                    result="DENIED",
                    workspace_id=task.workspace_id,
                    task_id=task.id,
                    tool=call.name,
                    details={
                        "effect": tool.spec.effect.value,
                        "policy_source": "emergency_stop",
                        "reason": reason,
                    },
                )
            )
        except Exception as error:
            log.warning("audit.not_recorded", tool=call.name, error=str(error))

    async def _record(
        self,
        task: Task,
        call: ToolCallRequest,
        result: ToolResult,
        interface: InterfaceLevel = InterfaceLevel.API,
    ) -> None:
        """Account for the call. Never at the cost of the task itself."""
        if self._call_log is None:
            return
        try:
            await self._call_log.record(
                ToolCallRecord(
                    tool=call.name,
                    success=result.success,
                    latency_ms=result.latency_ms,
                    task_id=task.id,
                    input_data=call.arguments,
                    output=result.output,
                    error=result.error,
                    call_id=call.id or None,
                    interface=interface,
                )
            )
        except Exception as error:  # telemetry is not worth failing a run over
            log.warning("tool.telemetry_failed", tool=call.name, error=str(error))

    # --- Observing ------------------------------------------------------------

    def _observe(
        self,
        step: int,
        call: ToolCallRequest,
        result: ToolResult,
        definition: EmployeeDefinition,
        *,
        refused: bool = False,
        refusals_so_far: int = 0,
    ) -> Observation:
        """Interpret what just happened, explicitly, before deciding anything."""
        # Redacted here, not at the log: this summary goes back into the
        # transcript, which is persisted and sent to the model on the next step.
        contextual = self._context_result(call.name, result, definition)
        summary = contextual.render(call.name)

        if refused and refusals_so_far + 1 >= REFUSAL_LIMIT:
            # Said here rather than in a message of its own, because this line
            # is already on its way into the transcript and a separate note
            # would be added again by every process that resumed the run.
            summary += (
                f" {call.name} has now been refused {refusals_so_far + 1} times "
                "in this task and is no longer available. Reach the goal another "
                "way, or report what stopped you."
            )
            log.info(
                "tool.withheld",
                tool=call.name,
                employee=definition.name,
                refusals=refusals_so_far + 1,
            )

        interface = self._interface_of(call.name, definition)
        observation = Observation(
            step=step,
            summary=summary,
            succeeded=contextual.result.success,
            details={
                "tool": call.name,
                "arguments": redact(call.arguments),
                "interface": interface.value,
                "provenance": contextual.provenance.to_dict(),
                "result_truncated": contextual.truncated,
                **({REFUSED: True} if refused else {}),
                # What a contract requiring an artifact is judged by. The path
                # only, relative to the workspace, never the content.
                **(
                    {"wrote": written}
                    if result.success and (written := written_path(result.output))
                    else {}
                ),
            },
        )
        log.info(
            "task.observed",
            step=step,
            tool=call.name,
            interface=interface.value,
            succeeded=contextual.result.success,
            refused=refused,
        )
        return observation

    def _context_result(
        self,
        name: str,
        result: ToolResult,
        definition: EmployeeDefinition,
    ) -> ContextResult:
        """Apply the result contract at the one path into model context."""
        try:
            spec = self._tools.get(name, definition).spec
        except (ToolNotFoundError, PermissionDeniedError):
            # Refused and unknown tools carry an error, not external content.
            from domain.tools.models import ToolSpec

            spec = ToolSpec.of(name, "Unavailable tool")
        return for_context(replace(result, output=redact(result.output)), spec)

    def _interface_of(self, name: str, definition: EmployeeDefinition) -> InterfaceLevel:
        """How this call reached the world. A refused call reached it not at all."""
        try:
            return self._tools.get(name, definition).spec.interface_level
        except (ToolNotFoundError, PermissionDeniedError):
            return InterfaceLevel.API

    # --- Fallbacks ------------------------------------------------------------

    @staticmethod
    def _best_answer(transcript: Transcript) -> str:
        """What to report when a run is cut short.

        Whatever the employee last said is more useful than an empty result, and
        the caller is told separately that a limit stopped it.
        """
        for message in reversed(transcript.messages):
            if message.content and message.role.value == "assistant":
                return message.content
        return ""

    @staticmethod
    def opening_messages(
        task: Task,
        definition: EmployeeDefinition,
        plan: TaskPlan | None,
        system_prompt: str,
        feedback: tuple[str, ...] = (),
        interfaces: tuple[InterfaceLevel, ...] = (),
        recalled: tuple[str, ...] = (),
        knowledge: tuple[str, ...] = (),
    ) -> tuple[Message, ...]:
        """The transcript a fresh run starts from."""
        system = system_prompt or f"You are a {definition.role.title}."
        system += f"\n\n# Data boundary\n\n{PLATFORM_POLICY}"
        if definition.goals:
            system += "\n\nYour standing goals:\n" + "\n".join(
                f"- {goal.text}" for goal in definition.goals
            )
        # Told to the model in the same terms the trace records it in, and only
        # when it has a choice to make: an employee with one level has no
        # hierarchy to respect, and a paragraph about one is noise.
        ladder = describe(interfaces)
        if ladder:
            system += f"\n\n{ladder}"

        instruction = f"# Task\n\n{task.goal}"
        if plan and not plan.is_empty:
            instruction += "\n\n# Your plan\n\n" + "\n".join(
                f"{step.index + 1}. {step.description}"
                + (f" (expected: {step.expected_outcome})" if step.expected_outcome else "")
                for step in plan.steps
            )
        if recalled:
            # Stated as recollection, not as fact. A model told "the report is
            # in reports/q3.md" acts on it; told that it noted this last time,
            # it checks - and memory, unlike an assignment, can be stale.
            instruction += (
                "\n\n# What you remember from earlier work\n\n"
                "These may be out of date. Use them as leads, and confirm "
                "anything you rely on:\n"
                + "\n".join(f"- {line}" for line in recalled)
            )
        if knowledge:
            # A quotation, not a recollection: it comes with its source, it is
            # not hedged, and it is framed as external content because the user
            # brought it and did not write it (ADR 0016, §25).
            instruction += (
                "\n\n# From the user's own documents\n\n"
                "Quote and cite these where they answer the task:\n\n"
                + "\n\n".join(knowledge)
            )
        if feedback:
            instruction += (
                "\n\n# A previous attempt was rejected\n\nFix these before answering:\n"
                + "\n".join(f"- {item}" for item in feedback)
            )
        instruction += (
            "\n\nWork through this and then give your final answer as plain text. "
            "Call a tool only when you need one."
        )
        return (Message.system(system), Message.user(instruction))

    @staticmethod
    def routing(
        definition: EmployeeDefinition,
    ) -> tuple[TaskKind, CapabilityRequirement, RoutingHints]:
        return (
            TaskKind.EXECUTION,
            definition.model_profile.as_requirement(),
            RoutingHints(
                quality=0.7,
                cost_sensitivity=0.5,
                needs_tools=bool(definition.allowed_tools),
            ),
        )
