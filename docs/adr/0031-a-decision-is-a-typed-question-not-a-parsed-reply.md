# ADR 0031: A decision is a typed question, not a parsed reply

## Status

Accepted - 2026-09-19. Callers: the manager's triage, and delegation where a
decision model is routed. Per-criterion verification, reconciliation and memory
revision are candidates, each to be moved on the evidence of a validation run
rather than all at once.

## Context

Several things the manager does are choices from a closed list: "talk or
work?", "who takes this task?", "is this criterion met?", "does this memory
replace that one?". Every one of them is asked of a text model as prose and
read back by a parser - the first letter of the reply, or a field of a JSON
object - and the parse is where the failures have been: an empty form copied
back as an answer, a role title instead of a name, a flag filled in and its
reason left blank.

None of those answers says how sure it is. A triage that is 51% sure a request
is talk looks exactly like one that is 99% sure, and the platform believes both.

Models built to decide rather than to write have appeared (TypeSafe's Jev is
the one that prompted this): they take a state, a question and options, and
return a choice with a calibrated confidence and a probability per option.
Their contract is not `LLM.generate`, and routing one as a text model would
hand it a plan prompt it cannot read.

## Decision

**`domain/decisions/` states the contract.** `ChoiceQuestion` (state,
question, options) in, `ChoiceAnswer` (key or none, per-option probabilities,
confidence or none) out, through `Decider.choose`. An answer that names no
option is `unreadable`, never a default dressed up as a choice.

**Confidence is measured or absent, never asked for.** `TextDecider` letters
the options, asks for one token, and reads the letters' share of that
position's token probabilities where the provider reports them
(`LLMRequest.top_logprobs`, `LLMResponse.logprobs`). That is one call and no
extra output. Where nothing is reported, `confidence` is None - "unknown", not
"sure". A provider that refuses the field is asked without it from then on.

**Self-consistency is rejected.** Asking N times and counting agreement also
yields a number, at N times the cost - and on a local model, where calls queue,
N times the wait - for what is meant to be the cheapest thing the manager
does. The owner ruled it out explicitly.

**`DECISION` is a kind of work, and `DECISION` a capability.** The kind is
routed in Settings like the others; the bundled catalogs send it where
extraction goes, so nothing changes until a person moves it. An entry whose
only capability is `DECISION` (or `EMBEDDING`) does not generate text, and the
catalog never offers it for text work.

**A model that decides is reached through `RoutedDecider`,** which reads the
route on every call. Routed to a text model, the question is lettered and
asked. Routed to a decision-only entry, it goes to that provider's `Decider`
(`ProviderFactory.for_decisions`); if that cannot answer - a missing key, a
rate limit, an outage - the text model is asked instead and the fallback is
logged. `PROMETHEUS_LOCAL_MODELS_ONLY` keeps decisions off a remote decision
model exactly as it does for text. The routing profile records where
decisions actually go, so a validation run under a decision model is not
counted with runs under a text model.

**TypeSafe is an adapter like any provider's.** The `typesafe` kind takes a
connection and its key from the window; `infrastructure/decisions/typesafe.py`
speaks `POST /v1/systemone` as its API reference documents it - state,
instructions, a criteria map of option to description - passes its own
calibrated confidence through, retries 429 and 529, and meters every call at
the routed entry's price (input tokens only). Score and a true/false estimate
are further question shapes, added when a caller needs them.

**A model that thinks is given room to.** Asking for five tokens returned
nothing from every reasoning model in the local catalog; `TextDecider` asks
again with room once it sees that, and strips the reasoning before reading the
letter (`validation/tasks/typed-decisions-the-triage-on-local-models.md`).

**Delegation asks a decision model first, and only a decision model.** Its
text path writes a reason and hands down facts, and a one-letter text answer
measured nothing on the local models - so the delegator's decider has no text
model behind it. Where `decision` is routed to a model that decides and at or
above `MIN_DELEGATION_QUALITY`, that model is asked; its choice stands only at
`DELEGATION_CONFIDENCE` or above, measured. Doubt, silence or an error is the
question asked the long way, as before.

**The triage is the first caller, and it doubts both readings - but not
equally.** A reading of "no work" is believed only when the triage answers
"reply" and, where confidence was measured, at 0.7 or better; below that the
request is work, because a slow answer beats an invented one. A reading of
"work" is doubted only when it states *no acceptance criterion and no
constraint*: such a reading could not say what the work was for, and a hosted
model produced one for "hello, how are you" - which the platform answered by
writing a file, having it rejected, planning again and stopping to ask
permission. A reading that states a criterion is believed, because turning
real work into a sentence is the worse of the two failures. A machine with no
triage never overrules in that direction.

The quoted-document exemption belongs to the first direction only. Retrieval
answers a greeting with a passage in any workspace that holds one - that is
what the similarity floor is for - and treating that as evidence that work is
needed left the greeting planned a second time, with three documents in the
workspace and the triage never asked.

## Consequences

- Where token probabilities exist, every triage records how sure it was, and
  a doubtful "talk" becomes work instead of an answer from nothing.
- Adding `DECISION` to the routed kinds changes every routing profile's
  fingerprint once: validation runs before this change count against the old
  fingerprint.
- Anthropic's API reports no token probabilities, so on that catalog the
  triage answers without a confidence, exactly as before. On the local models
  the probabilities exist and mean nothing (1.00 right or wrong): a calibrated
  confidence comes from a model trained to give one, or not at all.
- Connecting a decision model is a catalog entry plus a route; no caller
  changes.
- Its models are listed rather than typed. `GET /v1/models` is one documented
  endpoint returning a handful of names, so the `typesafe` kind joins the
  local runners in `RUNNERS` - the exception a big provider's catalogue of
  hundreds does not earn. The credential is resolved inside the adapter from
  the name the connection carries, so no secret crosses the application
  layer, and the window ticks `DECISION` for such a connection rather than
  the text capabilities. Typing the name is what produced `Unknown model:
  Jev` on the first live attempt.
