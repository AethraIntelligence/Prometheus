# Observability and diagnostics

Open **Observability** in the desktop sidebar, or choose **Open trace** from a
Work Center item. A trace connects the request, plan revisions, tasks and
assignments to model routing, tool effects, approvals, evidence and recovery.
The first causal failure is marked separately from retries and later recovery.
Parallel task spans share a parent and are ordered by their recorded timestamps;
the viewer does not imply a dependency between siblings.

The viewer stores and displays metadata, not content. Prompts, model responses,
file and screenshot contents and raw tool payloads are labelled as not captured
or redacted. Known credential fields, bearer values and common key forms are
sanitized before logs, trace persistence and export.

The health cards report the configured database, model profiles, sandbox,
integrations, scheduler, exporter and audit-chain verification. An unavailable
collector or trace store is shown as degraded and never changes the result of a
task. Operational metrics state their window, count, denominator and minimum
sample size; an empty window is “no data”, not a perfect or zero success rate.
Success is successful terminal runs divided by non-cancelled terminal runs;
accepted-result rate uses only judged assignments. Intervention rate counts
runs with a human-resolved approval, recovery rate counts recovered runs among
runs with a recovery span, and cost per accepted result divides their model
cost by accepted results. Median latency needs one sample and p95 needs twenty;
rates need five. Unsafe irreversible actions without a recorded approval are an
exact count whose required value is zero.

## Diagnostic bundles

Use **Preview bundle** before saving. Export is an explicit action and creates a
new file; an existing path is never replaced. The bundle includes application
and schema versions, feature flags, non-secret profile fingerprints, health and
only the selected sanitized traces. It excludes environment variables, the
master key, raw databases, credentials, prompts and responses, and user file or
screenshot content.

The HTTP boundary exposes the same data at `/api/traces`,
`/api/diagnostics/health`, `/api/diagnostics/metrics` and
`/api/diagnostics/bundle`. `POST /api/diagnostics/export` requires a destination
path supplied by the user. Trace retention is workspace-scoped through
`POST /api/traces/prune` and does not delete execution or audit records.

## Audit verification

Run:

```bash
uv run prometheus audit --verify
```

The command verifies every canonical record hash and the durable chain-head
checkpoint. A changed row, a removed middle row or a removed newest row fails
verification. Calendar rotation does not interrupt the chain. This is tamper
detection for a local record, not a promise that the owner of the machine cannot
delete or replace local data and software.
