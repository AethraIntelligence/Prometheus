"""A real task, written down so the same request can be made twice.

The validation tasks under `validation/` were prose until now: what was asked,
what came back, what broke. Prose is the right form for the reasoning and the
wrong form for the request - a sentence in a report cannot be re-run, and a
capability that worked in July is only known to still work if the July request
can be made again in September (§11.5).

So a scenario is a declaration, discovered the way an employee or a workflow is,
and it states three things and no more:

* **what is asked**, in one of the three ways this platform can be asked - a
  goal for Prometheus, a goal for a named employee, or a workflow by name. There is
  deliberately no fourth: a scenario that needed its own way of running work
  would be measuring something the user cannot do.
* **what the machine must have** for the request to mean anything. A scenario
  that drives a screen says so, and is skipped rather than failed where the
  desktop is switched off. A suite that fails on a correctly configured machine
  teaches people to ignore it.
* **what would count as done** - the same discipline Prometheus applies to an
  objective, applied to the scenario: written before the run, so the standard
  cannot be adjusted after seeing the output.

What a scenario does *not* carry is a way to fix the world it needs. `reset`
removes paths inside the workspace and `setup` writes files into it, and that is
the whole of it. Anything more - a fixture process, a stubbed provider - would
make the run a test again, and the point of the phase is that it is not one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Entry(StrEnum):
    """Which of the platform's three doors this request goes through."""

    #: `prometheus ask-prometheus` - the manager reads it and decides everything else.
    #: The default, because it is how the platform is normally used.
    OBJECTIVE = "OBJECTIVE"
    #: `prometheus run-task --employee <name>` - the employee is named, so the
    #: manager's routing is not part of what is being measured.
    TASK = "TASK"
    #: `prometheus run-workflow <name>` - the decomposition was written by hand.
    WORKFLOW = "WORKFLOW"


class Requirement(StrEnum):
    """A capability of the machine, not of the platform.

    Each one is a switch the user owns. A scenario naming one it does not have
    is skipped with the reason, which is the difference between a suite that
    reports the truth about this machine and one that reports the truth about
    somebody else's.
    """

    BROWSER = "BROWSER"
    CODE_EXECUTION = "CODE_EXECUTION"
    COMPUTER_USE = "COMPUTER_USE"
    MEMORY = "MEMORY"
    WORKFLOWS = "WORKFLOWS"
    APPROVALS = "APPROVALS"
    #: External services can be connected here. Like BROWSER, it says the
    #: capability exists rather than that any particular service is set up:
    #: which ones are connected is the user's business, and a scenario that
    #: needs a specific one says so in its own text.
    INTEGRATIONS = "INTEGRATIONS"


@dataclass(frozen=True, slots=True)
class Expectations:
    """What would count as done, stated before the run.

    Every field is optional and an empty `Expectations` passes anything that
    finished - which is a real and useful scenario: some requests are being
    watched for whether they complete at all, and inventing criteria for them
    would only make the record less honest.

    Two of these are about what must *not* have happened. That asymmetry is the
    Phase 10 lesson kept: a run that produced a good answer by sending an email
    nobody approved has failed, and no check on the output would notice.
    """

    #: Text the final answer must contain, matched case-insensitively. A phrase,
    #: not a sentence: an answer is prose, and asking prose to match exactly is
    #: asking the model to write the same words twice.
    output_contains: tuple[str, ...] = ()
    #: Text the final answer must *not* contain. The mirror of the field above
    #: and not a stylistic preference: the cheapest proof that a boundary held
    #: is that a fact which exists only on the other side of it did not appear
    #: in the answer. Phase 15's own Definition of Done is one of these.
    output_excludes: tuple[str, ...] = ()
    #: Workspace-relative paths that must exist when the run is over.
    files_exist: tuple[str, ...] = ()
    #: Path -> text that file must contain.
    file_contains: dict[str, str] = field(default_factory=dict)
    #: Tools the run must have been refused. The check that the brake held.
    tools_denied: tuple[str, ...] = ()
    #: Tools that must not have run successfully, whoever allowed them.
    tools_forbidden: tuple[str, ...] = ()
    #: How many different people the work had to reach. A floor rather than a
    #: ceiling, and the only expectation here about *how* a result was arrived
    #: at - which is exactly why Phase 18 needed it. A scenario called "a team
    #: on one objective" passed twice with one employee doing both ends of its
    #: own plan: every declared check was about the file, so the scenario could
    #: not fail for the reason it was written. Whether several people were
    #: involved is not visible in an output, and a claim nothing can falsify is
    #: not a measurement.
    min_employees: int | None = None
    #: Employees, by name, whose work the run must include (Phase 11). The one
    #: check on *which* people were chosen rather than how many: a document
    #: request that never reached the role declared for writing documents was
    #: routed wrongly however good the file is.
    employees_involved: tuple[str, ...] = ()
    #: Every task's result was accepted by the manager on the evidence. A run
    #: whose answer passed while a step was taken on its report alone passed on
    #: the verifier's generosity, not on the hand-offs.
    results_accepted: bool = False
    #: Ceilings, not targets. A run that answers correctly and costs ten times
    #: what it should is a failure of a kind no output check can see.
    max_cost_usd: float | None = None
    max_steps: int | None = None
    #: The run has to finish in the affirmative - the objective DONE, the task
    #: COMPLETED, the workflow COMPLETED. Off for a scenario whose whole point
    #: is that the platform refuses.
    must_succeed: bool = True
    #: Files that must still be exactly as the scenario's setup left them
    #: (Phase 13). The counterpart of `files_exist`: a refused overwrite leaves
    #: nothing to find in the output, and the evidence is the file not moving.
    files_unchanged: tuple[str, ...] = ()

    @property
    def named_files(self) -> tuple[str, ...]:
        """Every path this cares about, so a runner knows what to read."""
        return tuple(
            dict.fromkeys((*self.files_exist, *self.file_contains, *self.files_unchanged))
        )

    @property
    def is_empty(self) -> bool:
        return not (
            self.output_contains
            or self.output_excludes
            or self.files_exist
            or self.file_contains
            or self.files_unchanged
            or self.tools_denied
            or self.tools_forbidden
            or self.min_employees is not None
            or bool(self.employees_involved)
            or self.results_accepted
            or self.max_cost_usd is not None
            or self.max_steps is not None
        )


@dataclass(frozen=True, slots=True)
class Scenario:
    """One real request, and what it would take to call it done."""

    name: str
    request: str = ""
    entry: Entry = Entry.OBJECTIVE
    #: Who the request goes to, for `Entry.TASK`; which process, for
    #: `Entry.WORKFLOW`. Empty for an objective, where choosing is the point.
    target: str = ""
    description: str = ""
    #: The phase whose capability this exercises. Kept because the interesting
    #: question after a bad run is which capability regressed, not which file.
    phase: int = 0
    tags: tuple[str, ...] = ()
    requires: tuple[Requirement, ...] = ()
    #: Files to put in the workspace first, as path -> contents.
    setup: dict[str, str] = field(default_factory=dict)
    #: Which workspace the request is made in. Empty means the first one, which
    #: is what every scenario before Phase 15 meant. A scenario that names one
    #: is measuring the boundary itself: the request is asked *here*, and what
    #: it must not reach is over there.
    workspace: str = ""
    #: Documents to add before the request, as workspace -> title -> text. A
    #: dictionary of workspaces rather than a list, because the interesting
    #: scenario is the one where the document is somewhere the request is not.
    knowledge: dict[str, dict[str, str]] = field(default_factory=dict)
    #: Workspace paths removed before `setup`, files or directories. Found by
    #: the first full pass: the second run of the sorting scenario met its own
    #: output from the first, so every move became an overwrite - which is HIGH
    #: and waits for a person - and the scenario failed for a reason that had
    #: nothing to do with sorting. A scenario has to be able to say what must
    #: *not* be there, or the second attempt measures the first one's leftovers.
    reset: tuple[str, ...] = ()
    #: What the person at the keyboard would have said yes to, by tool name.
    #: Nothing else is allowed, and a scenario that declares none is a run
    #: nobody was there for - which is what every run before this field was.
    #:
    #: This is part of the world the scenario sets up, like `setup` files, not
    #: a power the harness has: a request the platform decides to make of a
    #: person is answered the way the author said beforehand it would be. The
    #: alternative - running the suite with approvals configured to allow -
    #: cannot express the scenario whose whole point is a refusal, and the
    #: alternative of leaving it unattended made "write the file you were asked
    #: for" impossible to pass and filled the report with NEEDED_APPROVAL.
    approve: tuple[str, ...] = ()
    #: Tools whose approval arrives in the same instant somebody pulls the
    #: emergency stop (Phase 13): the declared person answers yes, and the stop
    #: is engaged as they do. What the scenario then measures is that the effect
    #: did not happen anyway - the approval/effect boundary, on a real model.
    stop_on_approval: tuple[str, ...] = ()
    #: Requests asked first, in order, in the same thread as `request`. Each is
    #: carried to an answer and none is measured: they are the history the
    #: measured request depends on. A thread is what a long session is, and a
    #: scenario about remembering one could not be written without it (Phase 9).
    thread: tuple[str, ...] = ()
    inputs: dict[str, Any] = field(default_factory=dict)
    expect: Expectations = field(default_factory=Expectations)
    #: Force this into the regression set on a machine with no history of it.
    #: Normally membership is earned - a scenario that has passed here once is
    #: expected to keep passing - and this is for a fresh clone, where nothing
    #: has passed yet and the set would otherwise be empty.
    regression: bool = False

    def missing_requirements(self, available: frozenset[Requirement]) -> tuple[Requirement, ...]:
        return tuple(item for item in self.requires if item not in available)
