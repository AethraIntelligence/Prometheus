"""How Prometheus describes the people it can call on.

One function, used by three prompts, for a reason worth stating: the *only*
thing Prometheus knows about its workforce is what the registry hands it. There is no
list of employees in this package, no name in a prompt, and no branch on one.
Add a declaration under `employees/` and it appears here on the next run; delete
one and it stops being offered, with no edit anywhere in `application/prometheus/`.

What goes into the card is what a manager would need to choose: the role, what
the person is for, and - decisively - the tools they are allowed to use, because
that is the whole of what they can actually reach. Someone with no web tools
cannot look anything up, however well their role reads against the task.

Since Phase 18 the card also names the *services* this person was granted. The
tools those brought were already in the list, namespaced by the service, and
that turned out not to be enough: a name buried among a dozen `fs.*` entries is
not something a plan can be narrowed by. Named on its own line it is a term the
planner can write into a task's `needs`, and `domain.workforce.routing` turns it
into a field of the people who actually hold it - which is the whole of the fix
for work that went to whoever the model liked rather than to whoever could
reach the service.
"""

from __future__ import annotations

from domain.employees.definition import EmployeeDefinition


def describe(definitions: list[EmployeeDefinition]) -> str:
    """The workforce as a model should see it. Empty is said, not implied."""
    if not definitions:
        return "Nobody is declared. You have no one to give work to."
    return "\n\n".join(_card(definition) for definition in definitions)


def profile(definition: EmployeeDefinition) -> str:
    """One card without its heading, for a question whose option already names them."""
    return _card(definition).split("\n", 1)[1]


def _card(definition: EmployeeDefinition) -> str:
    lines = [f"## {definition.name}", f"Role: {definition.role.title}"]
    if definition.role.description:
        lines.append(definition.role.description.strip())
    lines.append(f"Tools they may use: {', '.join(sorted(definition.allowed_tools)) or 'none'}")
    if definition.integrations:
        lines.append(
            "Connected services they hold: " + ", ".join(sorted(definition.integrations))
        )
    if definition.goals:
        lines.append(
            "They always try to: "
            + "; ".join(goal.text for goal in sorted(definition.goals, key=lambda g: g.priority))
        )
    lines.append(
        f"Limits per task: {definition.limits.max_steps} steps, "
        f"${definition.limits.max_cost_usd:g}"
    )
    return "\n".join(lines)
