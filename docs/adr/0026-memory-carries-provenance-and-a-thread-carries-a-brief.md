# ADR 0026: Memory carries provenance, and a thread carries a brief

## Status

Accepted - 2026-09-17.

## Context

Two things went wrong in long use, and both were invisible in a short one.

A recollection reached a prompt as a bare sentence. "The user said: always
answer in Markdown" and a model's summary of eight task outcomes were rendered
alike, ranked alike and acted on alike, so an assumption carried the weight of
an instruction. Nothing recorded where a memory came from, a changed preference
was only ever added next to the old one, and a person could not see why a run
had been given a memory at all.

A thread reached the manager as its last eight turns. That is bounded and exact,
and it forgets: a decision made in turn three of forty and a file written in
turn five were simply gone. Sending every turn instead grows every model call
with the thread.

## Decision

**Every memory states what it rests on.** `MemoryItem` gains a basis (STATED,
OBSERVED, REPORTED, INFERRED), a confidence set by the writer, a provenance
(source kind, record id, label, what it was derived from) and a status (ACTIVE,
SUPERSEDED, CONTESTED). These are columns (migration 039), backfilled from what
existing rows already imply. A recollection is rendered with its basis, source,
date, confidence and scope (`domain/memory/citation.py`), and ranking multiplies
in a trust weight so an assumption sorts below a fact without disappearing.

**Disagreement is decided by a rule, detected by a model.** When a standing
memory is written, a cheap model is shown the few memories the store found by
their words and asked which the new one replaces or contradicts. What follows is
`domain/memory/revision.py`: a replacement, or a contradiction by evidence at
least as strong, supersedes; weaker evidence contests, and both are kept and
marked. A superseded memory is never recalled by a run and is pruned after 30
days. An unreadable or unreachable judge changes nothing. A person's correction
supersedes without asking a model.

**Every recall is recorded with its reason.** `memory_uses` names the memory, the
objective or task, the reader and a reason derived from the record - matched
words, scope, basis, age - and keeps no copy of the content, so forgetting a
memory still forgets it.

**Retrieval is evaluated, not assumed.** `application/memory/evals.py` seeds a
fixed corpus into any `Memory` and checks groundedness, freshness and isolation
against thresholds stated in code. It runs against the in-memory store in unit
tests, against SQL in integration tests (and PostgreSQL when configured), and
from `prometheus memory-eval`.

**A thread has a brief with two halves.** Decisions (a request's constraints,
the later one winning), open questions (what an unfinished answer said was
missing) and the artifact index (files the thread's tool calls wrote) are
*projected* from the objectives on every read and never stored. The goal brief
and stage summaries are the only stored part (`conversation_sessions`): once a
whole stage of turns sits outside the six recent ones, a model folds it, with a
deterministic fallback. Each stage names the objectives it covers, so a turn is
folded once and a restart continues where it stopped. The manager reads the
brief plus the recent turns inside a fixed character budget.

## Consequences

- A long thread costs the same context at turn 300 as at turn 15, and an early
  decision stays in front of the manager.
- Objectives, tasks and the audit trail are never changed by compaction.
- Two model calls are added, both rare: one per standing memory written and one
  per compacted stage. No call is added per task.
- Decisions are only as good as the constraints the intent reader extracts; a
  decision stated in prose and not extracted reaches later turns only through a
  stage summary.
- Contradiction detection covers standing (SEMANTIC) memories. Episodic
  outcomes are left to decay and expiry, which already answer "which is newer".
