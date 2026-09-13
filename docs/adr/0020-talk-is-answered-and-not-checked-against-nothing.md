# ADR 0020: Talk is answered on the spot, and not checked against nothing

## Status

Accepted - 2026-09-13

## Context

Since Phase 11 a direct answer has faced the same verifier as delegated work,
and since Phase 18 that verifier is told the answer's record is that nothing
was done. Both rules came out of real failures: "read the notes and leave me a
summary file" closed with one sentence and no file, and a request answered
three times out of a memory of the previous run.

On a local model the rule had the opposite failure, and it was the first thing
a person saw. In the desktop window:

- "Hello" was read as talk, but the model copied the blank `answer` from the
  empty form, and a reading with no answer was treated as work - a plan, three
  employees, and a "Still missing" card.
- "What can you do?" and "What is the capital of France?" got correct answers,
  and the verifier rejected every one: judged against "nothing was done", it
  wrote "no task was executed" as the missing criterion. Rewriting its prompt
  did not change that on the model in question.
- The reverse was also true. Asked for "no work" as one field of the long
  reading, the same model marked today's weather, an exchange rate and the
  number of files in a folder as talk, and answered all three from nothing.

## Decision

1. **A direct answer with no acceptance criteria is not verified.** Talk has no
   standard to check against, and a check without one has only ever disagreed
   with it. `Intent.is_conversation` is that case; a reading that writes any
   criterion - which a request for something to exist does - is verified
   exactly as before.
2. **"No work" is believed only when a narrow question agrees.**
   `prompts/prometheus_triage` asks one thing, "look it up or do something, or
   reply from what you know", and takes one letter back. Where it and the
   reading disagree, the request is work: a slow answer beats an invented one.
   On the model that failed above it sorted every request needing a lookup or
   the user's files correctly; its mistakes send some talk to the workforce.
3. **A reading of talk with an empty reply asks for the reply**
   (`prompts/prometheus_reply`) instead of turning into a plan.

## Consequences

- Chat-like requests are answered in one to three calls, without a plan.
- A work request misread as talk by *both* the reading and the triage is
  answered with a sentence and not checked. That is the Phase 11 failure, now
  requiring two independent misreadings rather than one.
- The triage is a second model call on every reading of "no work". It is
  optional on `IntentReader` so that scripted tests keep their scripts; the
  composition root always supplies it.
