"""One answer, from however many reports it took.

The user asked Prometheus, not the team, and has not seen the plan. Handing back a list
of task summaries would make them do the manager's job - reading four reports to
find the one sentence they wanted.

The rule that matters is what synthesis must *not* lose: names, numbers, links
and paths. A summary that drops the specifics is worse than the reports it
replaced, because the reports at least had them. So the prompt says so, and the
fallback below - used when a model call is unavailable or produces nothing -
keeps the reports intact rather than inventing a smoother answer than the
evidence supports.

A shortfall is stated, never smoothed. An objective that came up short and reads
as though it did not is the one failure mode of this component that a user
cannot detect for themselves.
"""

from __future__ import annotations

import structlog

from application.present import dated
from application.prometheus.language import DEFAULT_LANGUAGE, instruction
from application.prometheus.supervisor import TaskOutcome
from application.prompts import render
from domain.capabilities.models import CapabilityRequirement
from domain.llm.models import LLMRequest, Message, RoutingHints, TaskKind
from domain.llm.protocols import LLM
from domain.workforce.protocols import Objective

log = structlog.get_logger(__name__)


class Synthesizer:
    """Turns what the team produced into what the user reads."""

    def __init__(self, llm: LLM, *, language: str = DEFAULT_LANGUAGE) -> None:
        self._llm = llm
        self._language = language

    async def synthesize(
        self,
        objective: Objective,
        outcomes: tuple[TaskOutcome, ...],
        *,
        missing: tuple[str, ...] = (),
        resolution: str = "",
    ) -> str:
        results = describe(outcomes)
        if not results.strip():
            return "Nothing was produced for this objective."

        response = await self._llm.generate(
            LLMRequest(
                messages=(
                    *dated(),
                    *instruction(self._language),
                    Message.user(
                        render(
                            "prometheus_synthesis",
                            objective=objective.text,
                            criteria=_criteria(objective),
                            results=results,
                            shortfall=_shortfall(missing),
                            resolution=_resolution(resolution),
                        )
                    ),
                ),
                temperature=0.2,
            )
        )
        answer = response.content.strip()
        if not answer:
            # Better the raw reports than a blank page: the work happened, and
            # the user is entitled to it even when the last call said nothing.
            log.warning("prometheus.synthesis_empty", objective_id=str(objective.id))
            return results
        return answer

    @staticmethod
    def routing() -> tuple[TaskKind, CapabilityRequirement, RoutingHints]:
        """The only text the user actually reads. Worth a good model."""
        return (
            TaskKind.SYNTHESIS,
            CapabilityRequirement(),
            RoutingHints(quality=0.8, cost_sensitivity=0.4),
        )


def describe(outcomes: tuple[TaskOutcome, ...]) -> str:
    """What came back, task by task - the input to synthesis and to verification.

    Failures are included, not filtered. A checker shown only the successes is
    being asked whether the parts that worked worked.
    """
    blocks: list[str] = []
    for outcome in outcomes:
        summary = (
            outcome.task.result.summary.strip()
            if outcome.task.result and outcome.task.result.summary.strip()
            else outcome.reason or "produced nothing"
        )
        header = f"## {outcome.task.goal}"
        status = "" if outcome.succeeded else f" [{outcome.task.status.value}]"
        blocks.append(f"{header}{status}\nDone by: {outcome.employee or 'nobody'}\n{summary}")
        artifacts = outcome.task.result.artifacts if outcome.task.result else ()
        if artifacts:
            blocks.append("Files: " + ", ".join(artifacts))
    return "\n\n".join(blocks)


def actions(outcomes: tuple[TaskOutcome, ...]) -> str:
    """What the platform records as having happened, one line per action.

    The name of each tool and whether it worked, and deliberately nothing else:
    the verifier's question here is whether the thing was done, and showing it
    what a call returned turns it into a second reader of the material (the
    employee verifier learned that in Phase 16 and the note is on `_actions`
    there).

    Its most important output is the empty case. An objective that ran no tasks
    at all - because the manager decided the request needed none - says so in
    one sentence, and that sentence is the difference between a verdict on a
    result and a verdict on a description of one.
    """
    lines: list[str] = []
    for outcome in outcomes:
        observations = (outcome.task.result.output.get("observations") or ()) if (
            outcome.task.result
        ) else ()
        for entry in observations:
            if not isinstance(entry, dict):
                continue
            tool = str((entry.get("details") or {}).get("tool", "")).strip()
            if not tool:
                continue
            lines.append(f"- {tool} ({'ok' if entry.get('succeeded', True) else 'failed'})")
    if not lines:
        return NOTHING_WAS_DONE
    return "\n".join(lines)


#: Said in full rather than left as an empty section, because a heading with
#: nothing under it reads as missing information rather than as information.
NOTHING_WAS_DONE = (
    "Nothing. No task was run, no tool was called, and no file was created or "
    "changed. Whatever the result says was produced from the request itself and "
    "from what was already remembered here, not from doing the work."
)


def _criteria(objective: Objective) -> str:
    if not objective.acceptance_criteria:
        return "not stated"
    return "\n".join(f"- {item}" for item in objective.acceptance_criteria)


def _shortfall(missing: tuple[str, ...]) -> str:
    if not missing:
        return ""
    return "# What is still missing\n\n" + "\n".join(f"- {item}" for item in missing)


def _resolution(resolution: str) -> str:
    """A disagreement the manager already settled, so synthesis does not re-open it.

    Stated as a decision rather than as more evidence: given both sides again, a
    model writes a paragraph holding both, which is exactly the blended answer
    §87 exists to prevent.
    """
    if not resolution.strip():
        return ""
    return (
        "# A disagreement you have already settled\n\n"
        "Write the answer this leaves standing. Do not re-argue it and do not "
        "present the discarded side as a finding:\n\n" + resolution.strip()
    )
