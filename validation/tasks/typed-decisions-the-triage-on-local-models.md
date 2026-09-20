# Typed decisions - the triage, asked of the local models that answer it

**What was under test:** ADR 0031's first caller. The triage - "reply from what
you know, or is this work?" - moved from a prompt whose first character was read
back to a `ChoiceQuestion` answered through `Decider`, with the option letters'
token probabilities as its confidence. The claim: the same decision, parsed
instead of guessed at, and measured where it can be.

**The machine:** Ollama 0.32.14, the local catalog
(`PROMETHEUS_MODEL_CATALOG_PATH=infrastructure/llm/models.local.toml`), a
scratch data directory so nothing of the owner's store took part. Decisions
route where extraction does: `local-fast`, which is `lfm2.5:8b`. No key, no
spend. Twelve requests - six that are talk, six that need work, two of each in
Russian - were put to the triage directly, and two scenarios were run end to
end through `prometheus validate`.

**Result:** the triage had never worked on this catalog. It does now: 12 of 12
on `lfm2.5:8b`. And the measured confidence turned out to be worth nothing on
any local text model, which is the finding that shapes what comes next.

## What the run found

### Every local model in the catalog answered the triage with nothing

`lfm2.5:8b` and `gpt-oss:20b` both reason before they answer, and neither can
be told not to through the chat-completions API (`think: false` and
`reasoning_effort: "none"` were tried; `lfm2.5` opened with `<think>` either
way). The triage asked for five tokens. Five tokens of `<think>\nThe user` is
an empty answer, an empty answer's first letter is not "A", and not "A" is work.

So on this catalog every greeting was overruled into work, and "Hello" was
planned. The unit tests could not see it: a fake model answers "A" in one token.

`TextDecider` now reads a reply that was cut off with nothing visible as a model
that thinks, asks again with room for it (2048 tokens), and remembers - only
the first decision pays twice. Reasoning is stripped before the letter is read.

### A model trained on maths boxes its answer

With room to think, `lfm2.5:8b` answered two of twelve as `\boxed{B}`. Read by
first character that is "b" from "boxed" - right by accident for B, wrong for
every `\boxed{A}`. The letter is now read from the box, and otherwise from the
first capital standing on its own, so "Answer: B" is B and "Both" is nothing.

### The order of the prompt mattered more than its words

The first cut put the state first and the options after a generic heading; the
small model scored 9 of 12, twice calling a lookup talk - the dangerous
direction. Rewording the options fixed the direction and not the score. Putting
the question back in the order the old prompt had - who is speaking, then the
request, then the options - scored 12 of 12. `decision_choice/v1` is that order.

| Model | Thinks | Score | Per decision | Confidence |
|---|---|---|---|---|
| `lfm2.5:8b` | yes | 12/12 | 3-9 s | 1.00 on every answer |
| `lfm2:24b` | no | 6/12 - "work" for everything | 0.5 s | 0.94-1.00, wrong ones included |

### Token probabilities are not a confidence on these models

Neither number means anything. After reasoning, the letter is a foregone
conclusion and its probability is 1.00 whether it is right or wrong; without
reasoning, `lfm2:24b` was 0.99 sure, under the first cut of the prompt, that
the weather needed no looking up. With the final prompt the gate
(`TALK_CONFIDENCE`) changed no verdict on either model. It stays, because it is
free and a hosted model's probabilities may be better - but on this machine a
calibrated confidence is something only a model trained to give one will
supply. That is TypeSafe's claim, and the reason the delegation path asks a
decision model only when one is routed and only takes its answer above
`DELEGATION_CONFIDENCE`.

### End to end

- `answer-a-question`: passed, 0 steps, 28 s.
- `state-a-goal`: passed, 5 steps, 140 s. The long reading said this request -
  read the notes, *leave me a file* - needed no work; the triage said work and
  overruled it, which is the Phase 11 failure the triage exists for.
- `ask-prometheus` with a greeting, in Russian and in English: answered
  directly, no plan. Before, both were planned.

### The first calls to TypeSafe

Connected from the window (kind `typesafe`, its key in the credential store,
`decision` routed to the entry) and asked the same triage question through the
shipped adapter. The first attempt came back `400 api_usage_error: Unknown
model: Jev` - the entry named the vendor rather than a model id; with
`jev-latest` it answered five of five correctly in 0.31-0.77 s, at a cost too
small to round to a cent. Its `confidence` saturates: every answer came back
1.0 with the distribution entirely on one option, so on questions this easy it
reports certainty rather than a spread. Whether it is calibrated where it
matters is a question for harder ones.

### A greeting was planned, and the triage was never asked

Live, with a hosted free model reading requests: "hello, how are you" was read
as **work with no acceptance criteria at all**, and the triage only ever
doubted the opposite reading. The platform wrote `reply.txt`, had it rejected
by its own verifier, planned a second attempt, listed the directory, and
stopped to ask the user's permission to write `greeting.txt` - a step-up the
approval layer was right to demand, since untrusted directory contents had
just been read. Every part behaved as designed, on a reading that should never
have happened.

The triage now doubts that reading too, and only that one: work with nothing
to satisfy and nothing to respect. A reading that states a criterion stands.

## What was not tested

Whether Jev's confidence separates hard cases: the questions put to it were
answered 1.0 either way. Nothing here measures it where a decision is close,
which is the only place a confidence earns its keep.
