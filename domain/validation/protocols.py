"""How a scenario reaches the platform - and the reason there are exactly three.

A scenario is a request a user could have made, so it runs the way a user's
request runs: through the manager, through a named employee, or through a
declared workflow. Those are the platform's three doors and this file names all
of them as contracts, so the harness holds no way to run work that a person does
not also have. A fourth contract here would be a fourth way to do the work, and
what the suite then measured would be itself.

`StepExecution` is deliberately reused rather than restated: the workflow engine
already asks for exactly this, the task runner already satisfies it, and a
second protocol with the same signature would be two names for one idea.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol

from domain.validation.scenario import Scenario
from domain.workflows.run import WorkflowRun
from domain.workforce.protocols import Objective, ObjectiveResult
from domain.workspace.models import WorkspaceId


class ScenarioRegistry(Protocol):
    """The only way the harness learns which scenarios exist."""

    def list_all(self) -> list[Scenario]: ...

    def get(self, name: str) -> Scenario: ...


class ObjectiveExecution(Protocol):
    """Prometheus, seen from outside: state a goal, get an answer.

    Narrower than `WorkforceManager` on purpose. The harness needs the two calls
    the CLI makes and nothing else, and asking for the whole manager contract
    would let a future harness plan or delegate on its own.
    """

    async def receive(self, request: str, workspace_id: WorkspaceId | None = None) -> Objective: ...

    async def handle_objective(self, objective: Objective) -> ObjectiveResult: ...


class WorkflowExecution(Protocol):
    """Running a predefined process by name."""

    async def run(self, name: str, *, inputs: dict[str, object] | None = None) -> WorkflowRun: ...


class Approver(Protocol):
    """The person a run would have had at the keyboard, for the length of one run.

    The harness cannot approve anything and this does not change that: it hands
    over the answer the scenario's author wrote down before the run and takes it
    back afterwards. What decides is still the gate, and what it decides about
    is still every action above the threshold.

    A context manager rather than a value, because the answer has to stop
    applying when the run ends. A stance left installed would silently approve
    the next scenario, which is the one mistake a validation suite must not make.
    """

    def answering(
        self, allowed: frozenset[str], stop_on: frozenset[str] = frozenset()
    ) -> AbstractContextManager[None]: ...
