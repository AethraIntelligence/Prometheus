# Workflows

A predefined process: named steps, each delegated to an employee, with declared
dependencies between them. Adding one is adding a file here - no Python, and no
change to any employee.

```bash
uv run prometheus workflows                      # what is declared, and whether it can run
uv run prometheus run-workflow <name> --input key=value
```

A step reads what earlier steps produced through `{steps.<name>}`, and the run's
inputs through `{key}`. `max_attempts` says how many times a step is worth
repeating - which depends on what it does, not on the engine - and
`on_failure: CONTINUE` says a step's failure is survivable. The default is to
stop, because the step after a failed one usually reads what it produced.

Everything below the decomposition is the same as for work Prometheus planned
itself: the same employees, the same limits, the same approval gate. A workflow
cannot do anything an employee could not have been asked to do directly.

Every declaration has an integer `version`. Published versions are immutable:
a changed process is a new `<name>.v<version>.yaml` file. Schedules pin the full
selected version, so a later file edit cannot silently alter work already
activated. Inputs may declare `type`, `required`, `default`, and `description`;
`profile` fixes approvals and preferred model, while `budget` bounds attempts,
cost, and wall time. The API exposes readiness and dry-run checks before a
version is run or scheduled.

**Nothing ships here.** A fresh installation has no declarations at all, and
that is the point: a person putting a task on a clock is not choosing a process,
so a catalog entry they did not write is a question with no answer behind it.
The window offers no way to pick one either.

A declaration arrives one of two ways. Somebody writes the file - this is that
path, and it is for whoever is reading this. Or a schedule's own successful runs
settle into the same order often enough that the platform offers to keep it, on
that schedule's card, in that schedule's words; confirming writes the file here
and points the schedule at it. An improvement later is the next version beside
it, never over it, so going back is always possible.

The same version can run manually or from a time/event schedule. Schedules use
declared per-step retry, coalesce missed times, and skip overlapping firings.
