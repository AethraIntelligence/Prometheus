# Validation

Real work a human actually wanted done, used to close a phase.

A phase is not finished because its tests are green. It is finished when the
capability it added has done a useful piece of work, recorded here.

Phase 1 is the one exception: it builds the foundation and has nothing a user
could ask for yet.

## Two directories, and why they are two

**`tasks/`** is what happened and why - one Markdown report per phase, written
for a person. It is the most useful prose in the repository: three of the rules
in `CLAUDE.md` were defects one of these runs found before they were rules.
What it cannot do is be re-run.

**`scenarios/`** is the request itself, declared so it can be made again. One
YAML file per scenario: what is asked, which of the platform's three doors it
goes through, what the machine must have for the request to mean anything, and
what would count as done - written before the run, so the standard cannot be
adjusted after seeing the output.

Merging the two would give either a report nobody can re-run or a declaration
nobody can read.

**`REPORT.md`** is generated, not written. `prometheus validation-report` computes
it from the recorded runs; editing it by hand would turn a measurement back into
an opinion.

## Running them

```bash
prometheus scenarios                 # what is declared, and how each has gone here
prometheus validate                  # all of them, recording every attempt
prometheus validate sort-a-folder    # one
prometheus validate --regression     # only what has passed here before (§11.5)
prometheus validate sort-a-folder --repeat 5
prometheus validation-report --write validation/REPORT.md
```

This is not the test suite. `uv run pytest` is free, offline and fast, and says
the platform still does what it was built to do. This costs money and minutes,
and says whether that is worth anything on a request somebody actually made.

Against locally served models it costs nothing:

```bash
export PROMETHEUS_MODEL_CATALOG_PATH=infrastructure/llm/models.local.toml
```

A run with nobody watching refuses everything that needs approval, and the
report says NEEDED_APPROVAL rather than pretending the capability is absent.
That is the honest reading of an unattended machine; to measure what a person at
the keyboard would get, answer the prompts.

## The release gate

`release-gate.yaml` is the machine-readable product contract. It names five
primary user outcomes and one separate safety invariant, and sets minimum pass
rates plus ceilings for time, cost and human intervention. The latest complete
window is also compared with the preceding window, so a still-acceptable number
cannot hide a sharp regression.

```bash
prometheus validation-gate --check-config  # free, deterministic, no database or provider
prometheus validate --release              # record the full stochastic window
prometheus validation-gate                 # human-readable decision and non-zero on failure
prometheus validation-gate --json          # the same decision for automation
```

Skipped runs never count as attempts. A release machine that lacks a required
capability therefore reports insufficient evidence rather than a false pass.
The safety target allows no failed attempt. Thresholds are initial product
targets, not constants of nature; raising one is a reviewed product decision,
and lowering one requires an explanation in the release notes.

## Adding one

Write a file in `scenarios/`. Nothing else - no Python, no registration.
`tests/e2e/test_validation_harness.py` declares one in a temporary directory and
runs it, which is that claim as a test.
