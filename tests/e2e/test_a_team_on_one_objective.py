"""Phase 12's Definition of Done: three people, one objective, parallel branches.

*A goal requiring three employees is completed, with parallel branches; the
memory isolation test is green.*

Written the way Phase 8's is - against the employees that actually ship, read
off `employees/`, with nothing declared inside the test. What is faked is the
models and the runtime, because the claim under test is about the manager's
decomposition, its execution and what each employee is allowed to know - not
about whether a browser opened.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

from application.prometheus.delegation import CapabilityDelegator
from application.prometheus.intent import IntentReader
from application.prometheus.manager import PrometheusManager
from application.prometheus.planner import ObjectivePlanner
from application.prometheus.reconciliation import Reconciler
from application.prometheus.supervisor import Supervisor
from application.prometheus.synthesis import Synthesizer
from application.prometheus.verification import ObjectiveVerifier
from domain.tasks.task import Task, TaskResult, TaskStatus
from domain.workforce.assignment import TaskAssignment
from domain.workforce.protocols import ObjectiveStatus
from infrastructure.employees.yaml_registry import YamlEmployeeRegistry
from infrastructure.persistence.objective_repository import InMemoryObjectiveRepository
from infrastructure.persistence.plan_repository import InMemoryPlanRepository
from tests.fakes.llm import FakeLLM, reply
from tests.fakes.workforce import RecordingExecution

EMPLOYEES = Path(__file__).resolve().parents[2] / "employees"

#: Two things gathered independently, then written up. The shape §85 uses as its
#: own example of delegation, and the smallest honest case for a team: the two
#: gathering tasks have nothing to say to each other, and the third needs both.
PLAN = json.dumps(
    {
        "rationale": "Two independent sources, then one document.",
        "tasks": [
            {"id": "t1", "goal": "Read the sales figures", "needs": ["CODE"]},
            {"id": "t2", "goal": "Find what competitors charge", "needs": ["WEB_BROWSING"]},
            {
                "id": "t3",
                "goal": "Write report.md from both",
                "needs": ["FILE_ACCESS"],
                "depends_on": ["t1", "t2"],
            },
        ],
    }
)

INTENT = json.dumps(
    {
        "restatement": "compare our prices with theirs and write it up",
        "needs_work": True,
        "acceptance_criteria": ["report.md exists and cites both sources"],
    }
)


class TimedExecution(RecordingExecution):
    """Records what each task was told, and how many ran at once."""

    def __init__(self) -> None:
        super().__init__()
        self.peak = 0
        self.told: dict[str, tuple[str, ...]] = {}
        self._running = 0

    async def start(self, task: Task, assignment: TaskAssignment) -> Task:
        self.started.append((task, assignment))
        self.told[task.goal] = assignment.context.facts
        self._running += 1
        self.peak = max(self.peak, self._running)
        await asyncio.sleep(0)
        self._running -= 1
        # What a real run leaves behind: a tool call that reached the world, and
        # the file it wrote. The shipped roles' contracts are judged on exactly
        # this, so a fake that reported success with nothing behind it would be
        # (correctly) not accepted.
        written = f"notes/{len(self.started)}.md"
        return replace(
            task,
            status=TaskStatus.COMPLETED,
            result=TaskResult(
                summary=f"Result of: {task.goal}",
                output={
                    "observations": [
                        {
                            "step": 1,
                            "summary": f"fs.write wrote {written}",
                            "succeeded": True,
                            "details": {"tool": "fs.write", "wrote": written},
                        }
                    ]
                },
            ),
            cost_usd=0.01,
        )


def manager_for(
    llm: FakeLLM, execution: RecordingExecution, chooser: FakeLLM | None = None
) -> PrometheusManager:
    registry = YamlEmployeeRegistry(EMPLOYEES)
    return PrometheusManager(
        intent=IntentReader(llm),
        planner=ObjectivePlanner(llm),
        supervisor=Supervisor(
            execution=execution,
            delegator=CapabilityDelegator(chooser or llm, registry),
            max_attempts=1,
        ),
        verifier=ObjectiveVerifier(llm),
        synthesizer=Synthesizer(llm),
        reconciler=Reconciler(llm),
        registry=registry,
        objectives=InMemoryObjectiveRepository(),
        plans=InMemoryPlanRepository(),
    )


def script() -> FakeLLM:
    """The manager's own story: read it, plan it, reconcile, check, answer."""
    return FakeLLM(
        [
            reply(INTENT),
            reply(PLAN),
            reply(json.dumps({"consistent": True, "conflicts": []})),
            reply(json.dumps({"passed": True, "reason": "report.md is there"})),
            reply("report.md compares our prices with theirs."),
        ]
    )


def chooses() -> FakeLLM:
    """Delegation gets its own client, because two of the three need no call.

    `Read the sales figures` needs CODE and only one employee declares it, so
    the field narrows to one and choosing costs nothing. The other two are
    asked for, in wave order: the gatherer first, the writer in the next wave.
    """
    return FakeLLM(
        [
            reply(json.dumps({"employee": "researcher", "why": "it looks things up"})),
            reply(json.dumps({"employee": "writer", "why": "it writes documents"})),
        ]
    )


async def test_three_employees_one_objective_with_a_parallel_branch() -> None:
    execution = TimedExecution()
    manager = manager_for(script(), execution, chooses())

    objective = await manager.receive(
        "Compare our prices with what competitors charge and write it up"
    )
    result = await manager.handle_objective(objective)

    assert result.status is ObjectiveStatus.DONE

    employees = [entry["employee"] for entry in result.output["tasks"]]
    assert sorted(employees) == ["analyst", "researcher", "writer"], "three people, by capability"
    assert execution.peak == 2, "the two independent tasks ran at the same time"
    assert len(execution.started) == 3


async def test_each_employee_is_told_only_what_the_plan_said_to_tell_it() -> None:
    """§86 end to end: the two gatherers never learn of each other."""
    execution = TimedExecution()
    manager = manager_for(script(), execution, chooses())

    await manager.handle_objective(
        await manager.receive("Compare our prices with what competitors charge")
    )

    assert execution.told["Read the sales figures"] == ()
    assert execution.told["Find what competitors charge"] == ()

    written = " ".join(execution.told["Write report.md from both"])
    assert "Read the sales figures" in written
    assert "Find what competitors charge" in written


async def test_the_writer_never_sees_the_others_transcripts_only_their_answers() -> None:
    execution = TimedExecution()
    manager = manager_for(script(), execution, chooses())

    await manager.handle_objective(await manager.receive("Compare and write it up"))

    for fact in execution.told["Write report.md from both"]:
        assert fact.startswith(("Read the sales figures ->", "Find what competitors charge ->"))
