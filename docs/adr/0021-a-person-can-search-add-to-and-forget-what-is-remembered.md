# ADR 0021: A person can search, add to and forget what is remembered

## Status

Accepted - 2026-09-14. Amends ADR 0009.

## Context

Since Phase 13 the window has shown memory and nothing else. The reason was
ADR 0009's: reading and forgetting are two contracts, and an interface that held
both would put "delete" one click from "show me". Forgetting was
`prometheus memory --prune`, which only drops what has expired.

In use that left a person with no answer to the three things they actually
wanted. A distilled memory that is wrong - "the system always relocates
gym-membership.txt into sorted/finance/ (FAILED)" - is recalled into every
later run, and the only way to remove it was SQL. A fact the platform should
know ("invoices for 2026 are in finance/2026") could reach memory only by being
said inside a request and happening to be read as a preference. And a listing
of a few dozen paragraphs had no search, although `recall` has always taken
words.

## Decision

**Search is `recall` with words in it.** The listing already went through the
one contract; the window now sends what is typed, a moment after typing stops.
Nothing ranks or filters in the interface, so a search shows what a run asking
the same words would read.

**A note is a SEMANTIC item a person stated.** One question is asked - true of
you in every workspace (USER), or of this one (WORKSPACE) - because every other
field has one right answer: no expiry, the importance of a preference, and
`metadata.source = "person"` so the listing can say who wrote it. A note is at
most 2000 characters; anything longer is a document (ADR 0016).

**Forgetting stays a second contract, and is bounded by what is shown.**
`ServiceDependencies` takes `memory_maintenance` separately from `memory`, so a
surface built with only the first can show and add and says it cannot forget.
`MemoryMaintenance.forget` gained `within: MemoryQuery`: the backend deletes
only the given ids that query could read, decided by `domain/memory/access.py`
on the rows rather than restated as SQL. The facade passes the query its listing
reads with, so an id from another workspace or an employee's private notes is
not found (404) rather than deleted. Consolidation passes nothing and is
unchanged.

**Forgetting takes two clicks**, as removing a document does: nothing brings a
forgotten line back.

## Consequences

- What ADR 0009 protected is still protected: holding `recall` does not imply
  deleting, and no caller can delete what it cannot read. What changed is that
  the interface is now given both contracts, on purpose.
- Private employee memory, plan memory and working notes remain invisible to a
  person in the window, and therefore unforgettable from it. Clearing those is
  still housekeeping - expiry and consolidation - not a screen.
- Editing a line is not offered. Forgetting it and adding the corrected note is
  two steps, and keeps "what the platform concluded" and "what a person said"
  distinguishable on the record.

## Alternatives considered

**Filtering the listing in the window.** Instant, and a second ranking that
disagrees with the one every run reads - a line found on screen would not be
the line recalled.

**`forget` checked in the facade by listing first.** A listing is limited, so an
item past the first page could not be forgotten, and the check would be a copy
of the access rule outside the domain.

**Editing in place.** `remember` upserts on the id, so it is one call away; it
was left out because an edited distillation reads as the platform's own
conclusion while being the person's words.
