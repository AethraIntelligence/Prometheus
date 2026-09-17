# Employees

An employee is a **declaration**, not code. Adding `Prometheus Legal` or
`Prometheus Recruiter` means adding a directory here - and nothing in `application/`,
`infrastructure/` or Prometheus itself may need to change for it.

All employees share one runtime (`application/employee_runtime/`). They differ
only in role, goals, allowed tools, declared capabilities, policies, model
profile, memory scope and hand-off contract.

```
employees/<name>/
    employee.yaml          the whole employee
    prompts/system.md      optional: its own voice
```

The directory name is the employee's identity, and `employee.yaml` must declare
the same `name`. `infrastructure/employees/yaml_registry.py` discovers it, and
refuses a declaration it cannot make sense of - an unknown field, a temperature
outside 0-2, a budget of zero - naming the file that caused it. A typo fails at
load rather than becoming an employee with no tools.

## The two lists, and what each is for

`allowed_tools` is **least privilege**: an employee gets what it lists and
nothing else, and `prometheus tools` shows the resulting grants per tool. A tool nobody
lists is a tool nobody can call.

`capabilities` is **discovery**: what kind of work this employee can be given.
Prometheus searches by these, so a capability left out is work that never arrives, and
one claimed with no tool behind it is work that arrives and cannot be started.
`prometheus employees` prints both, and says which declarations disagree with the tools
this machine actually has.

The distinction matters because the two answer different questions - *may it?*
and *can it?* - and a single list would silently answer one of them wrong.

## Hand-off contract and readiness

An optional `contract` says what earlier work this employee can start from,
what it promises to deliver, which evidence must exist before that result can
be accepted, and which failures are expected for the role:

```yaml
contract:
  accepts: [FINDINGS]
  produces: [ANSWER, FILE]
  evidence: [ARTIFACT]
  failure_kinds: [REFUSED, NOT_ACCEPTED, BUDGET, TRANSIENT, EXECUTION, CANCELLED]
```

The closed vocabularies are:

- products: `ANSWER`, `FINDINGS`, `FILE`, `CHANGES`;
- evidence: `TOOL_RESULT`, `ARTIFACT`;
- failures: `REFUSED`, `NOT_ACCEPTED`, `BUDGET`, `TRANSIENT`, `EXECUTION`,
  `CANCELLED`.

Declarations without `contract` remain valid for compatibility. They accept any
upstream product, produce `ANSWER`, require no evidence and allow every failure
kind; the Workforce screen labels this contract as undeclared.

Readiness is not stored in this file. Prometheus computes `READY`, `DEGRADED` or
`UNAVAILABLE` from the declaration and the current machine: tools, integrations,
policies, sandbox and model availability. An unavailable employee is excluded
from delegation, and its profile states both the reason and the recovery action.

## The five that ship

| | does | reaches the world through |
|---|---|---|
| `researcher` | finds out what is true and says where it came from | web, browser, files |
| `organizer` | puts a folder of documents in order | files |
| `operator` | works interfaces that have no API and no usable DOM | browser, then the screen |
| `analyst` | computes answers from data on this machine | files, and code it runs |
| `writer` | turns findings into the requested document | files and PDF |

Five, not thirty. Each is one file plus a prompt, and none of them has a line of
Python behind it - `tests/e2e/test_a_new_employee.py` proves that by declaring a
new one in a temporary directory and having Prometheus use it.
