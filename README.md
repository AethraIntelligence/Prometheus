# Prometheus

**Tell it what you want done. An AI manager plans the work, hands it to a team of
digital employees, checks the result, and does it all on your own machine.**

```mermaid
flowchart LR
    You -->|a goal, in your words| P[Prometheus<br/>the manager]
    P -->|plans and delegates| W[Workforce]
    W --> R[researcher] & O[organizer] & A[analyst] & OP[operator] & WR[writer]
    R & O & A & OP & WR -->|files · web · browser · screen · code · your services| X[The world]
    P -->|checks the result against criteria written first| You
```

Prometheus is a local-first platform: no server to rent, no account to create, no
data leaving your computer unless you point it at a hosted model. It chats like an
assistant when you ask a question, and works like a team when you ask for work.

---

## In short

| | |
|---|---|
| **It does real work, not just answers** | Reads and sorts your files, searches the web and reads pages, analyses data by running code, writes documents, answers from documents you gave it, and operates interfaces that have no API. |
| **You can trust what it reports** | Success criteria are written *before* the work starts, and results are checked against what actually happened on your machine, not against what the model says it did. |
| **You stay in control** | Anything irreversible waits for your approval. Employees get only the tools they are declared for. One command stops everything. Every action, including refused ones, is audited. |
| **It is yours to run and extend** | Runs fully offline on local models through Ollama. A new employee, workflow or connected service is a file, not code. |

---

## Quick start

**You need:** Python 3.12+, [uv](https://docs.astral.sh/uv/), Node 20+, the Rust
toolchain, and either [Ollama](https://ollama.com) (free, local) or a provider key.

```bash
git clone <this repository> && cd prometheus
./start.sh
```

`start.sh` installs everything with one `uv sync` (no extras to remember), creates
the database, and opens the desktop window. The window starts the runtime, the
runtime starts Ollama if your catalog uses local models, and the browser engine is
downloaded on first start.

Then type a goal:

> *What is the capital of France?* - answered on the spot.
> *What is the weather in Milan right now?* - looked up on the web.
> *How much does express delivery cost?* - quoted from the policy you added.
> *Read my meeting notes and write decisions.md, one line per person.* - planned, delegated, checked.

<details>
<summary>Prefer the terminal?</summary>

```bash
uv sync
uv run alembic upgrade head                 # creates ~/.prometheus/prometheus.db
cp .env.example .env                        # add a provider key, or use Ollama below
uv run prometheus ask-prometheus "Summarise every file in notes/ into summary.md"
uv run prometheus serve                     # the same runtime as a local web page
```

Free and offline:

```bash
ollama pull gpt-oss:20b && ollama pull bge-m3
export PROMETHEUS_MODEL_CATALOG_PATH=infrastructure/llm/models.local.toml
```

</details>

---

## What it can do

### Answer, or work - it decides

A greeting, a question about itself or a fact that does not change gets a direct
answer, like a chat assistant. Anything that has to be looked up, touches your
files or must exist afterwards becomes work: a plan, the right employees, and a
check before you see the result. A second, narrow question guards the boundary, so
"today's weather" is looked up rather than invented.

### A team of five, each declared in one file

| Employee | Does | Reaches the world through |
|---|---|---|
| `researcher` | Finds out what is true and says where it came from | web search, browser, files |
| `organizer` | Puts a folder in order and says what it moved | files |
| `analyst` | Computes answers from data on this machine | files, code it runs under limits |
| `operator` | Works interfaces with no API - a canvas, an embedded viewer | browser, then the screen |
| `writer` | Turns findings into the document that was asked for | files |

Work is routed by what each employee declares it can do. Nothing in the manager
names an employee, so adding a sixth is adding a directory.

### Knows your documents

Add PDF, Word (`.docx`), Markdown, HTML, CSV, JSON or plain text from the system
file picker. Answers quote the document they came from. Search is hybrid - by
meaning and by words - with a multilingual embedding model (`bge-m3`), so a question
in Russian finds a policy written in English. A newer version of a file replaces
the old one in place; changing the embedding model re-indexes by itself.

### Remembers, within limits you can see

What a task learned, what a plan produced and how you like things done are
recalled into the next run, so *"do the same for the returns folder"* means
something. Working notes expire in hours, outcomes fade over months, stated
preferences stay until you change them. Documents are kept apart from memory:
they do not decay, and they are cited.

### Separates contexts

Workspaces keep work and personal apart: each has its own files, documents and
history, and switching moves the one directory the file tools can see.

### Uses the most direct way in

```
API  ->  integration  ->  browser  ->  screen inside the page  ->  your desktop
```

Every tool declares which rung it is on, and the choice is logged. Screen control
follows *look, act, check*: a vision model finds the target, every click can be
confirmed by looking again, and a target outside the frame is refused, not guessed.

### Connects to your services

Any [MCP](https://modelcontextprotocol.io) server added in **Settings -> Plugins**
becomes ordinary tools. What each capability does to the world is classified on
your machine, never by the server; anything unclassified is treated as dangerous
and asks first; nobody can use a service until you grant it.

### Runs known processes, and starts work on its own

A process you already know the shape of is a YAML file under `workflows/`. A
schedule ("every morning", "every 3 hours") or an event can start an objective with
nobody at the keyboard - through the same checks and the same brake. Work nobody
asked for is opt-in: `PROMETHEUS_FLAGS__SCHEDULER=true`.

---

## Why you can trust the result

1. **The standard comes first.** Reading your request produces acceptance criteria,
   stored before any work starts, so success cannot be redefined after the fact.
2. **The record outranks the report.** The verifier sees what the platform recorded
   - which tools ran, which files were written - next to what the employees claim.
   An answer describing a file nobody wrote does not pass.
3. **A shortfall is said plainly.** Rejected work is replanned once with what was
   missing; after that you get what did succeed plus a clear list of what did not.
4. **Contradictions are surfaced, not blended.** When two employees disagree, the
   manager settles what the evidence settles and shows you the rest.
5. **Behaviour is measured, not asserted.** `validation/scenarios/` holds real
   requests; `prometheus validate` runs them and `prometheus validation-report`
   computes what works reliably, sometimes, or not at all.

## Why it is safe to let it act

- **Risk follows the effect.** Reading is low, writing medium; sending, spending,
  deleting, publishing and running generated code are high and wait for a person.
  A tool can declare itself riskier than its effect, never safer.
- **Least privilege is a list you can read.** An employee gets exactly the tools it
  lists (`prometheus tools`), and its declared policies (`read_only`, `no_sending`...)
  can only narrow what it may do.
- **The filesystem is fenced.** File tools see one directory per workspace, symlinks
  resolved; anything outside is refused, not approved.
- **The desktop is opt-in per application.** With no allowed applications, nothing
  on your desktop can be touched, and every desktop action asks each time.
- **There is a brake.** `prometheus stop` writes a file every screen action reads
  first - it works from a second terminal while a run holds the screen.
- **Silence is a no.** An unanswered approval expires and is refused. Approvals can
  also reach you on Telegram.
- **Everything is audited**, including actions that were refused and never ran.
- **Content is data, not instructions.** A web page, an email or a document is
  quoted to the model inside markers; nothing in it can change a policy or a grant.

---

## Models: local, hosted, or both

Nothing outside `infrastructure/llm/` names a model or a vendor. A caller states what
the work needs - reasoning, tool calling, vision, a long context, embeddings - and
the router picks from the catalog.

- **In the window:** Settings -> Providers and models. Add a connection (Ollama,
  OpenAI, Anthropic, Gemini, OpenRouter), pick installed models from a list - their
  context length is read from the runner - and choose which model does each kind of
  work, including embeddings.
- **In files:** `models.toml` (OpenRouter), `models.anthropic.toml`, and
  `models.local.toml` (Ollama, zero cost).
- **Ollama settings stay in Ollama.** Prometheus starts the Ollama app when it is
  needed and never overrides your context length or other runner options.

Costs and tokens of every call are logged: `prometheus spend`.

---

## Where things are kept

SQLite by default - one file in `~/.prometheus`. PostgreSQL (or Supabase) is a
setting, and moving is *copy -> verify row by row -> erase only when you confirm*:

```bash
uv run prometheus storage-migrate --to postgresql://localhost/prometheus
```

---

## Extending it

| To add | You write | Example |
|---|---|---|
| An employee | `employees/<name>/employee.yaml` | tools it may use, work it can take, limits |
| A workflow | `workflows/<name>.yaml` | named steps, who does each, dependencies |
| A connected service | nothing - Settings -> Plugins | any MCP server by name and command |
| A tool | one file in `infrastructure/tools/` + one line in `builtin.py` | declare parameters and effect; schema and risk follow |
| A model | a catalog entry, or Settings | no code change anywhere |

```yaml
# employees/translator/employee.yaml
name: translator
role: Translator
goals:
  - text: Say what the original says, not what it would have said.
allowed_tools: [fs.read, fs.write]   # may it?  least privilege
capabilities: [FILE_ACCESS]          # can it?  what the manager routes by
model_profile:
  capabilities: [TEXT_REASONING, LONG_CONTEXT]
limits: { max_steps: 8, max_cost_usd: 0.50 }
```

---

## Architecture

```
app/             transport and composition root: CLI, local HTTP API
application/     the manager, the one employee runtime, memory, knowledge, the interface boundary
domain/          protocols and values - depends on nothing
infrastructure/  adapters: models, storage, tools, browser, MCP
desktop/         Tauri + React window - an adapter over the HTTP API, no business logic
employees/  workflows/  prompts/  validation/scenarios/    declarations and content
```

- **One boundary for every interface.** The CLI, the web page and the desktop window
  all talk to `application/interface/`; a new surface is an adapter, not a fork.
- **One runtime for every employee.** Employees differ only by declaration.
- **Layering is enforced**, by `import-linter` and by tests that read the source; the
  frontend follows Feature-Sliced Design with its own architecture test.
- **Decisions are written down.** Twenty ADRs in [`docs/adr/`](docs/adr) record
  what was chosen, what was rejected, and the failure that forced the choice.

<details>
<summary>The command line</summary>

```text
ask-prometheus "<goal>"   objectives   serve   stop [--clear]
employees   tools   policies   approvals   approve <id>   reject <id>   audit
run-task --employee <name> "<goal>"   resume   workflows   run-workflow <name>
workspaces   workspace-new   workspace-use   documents   document-add <path>
memory [--search] [--prune]   models   spend   integrations   prompts
schedule "<request>" --every N | --daily-at HH:MM   schedules   events
storage   storage-migrate --to <url> [--erase]   validate   validation-report
```

</details>

---

## Development

```bash
uv run pytest                                  # whole suite: no network, no key
uv run ruff check . && uv run lint-imports
uv run python scripts/check_english_only.py
cd desktop && npm test                         # the window, against a scripted runtime
PROMETHEUS_TEST_POSTGRES_URL=postgresql://localhost/prometheus_test uv run pytest
```

The codebase is English-only - identifiers, comments, logs, schema and docs. What
the agents answer in is configuration: `PROMETHEUS_RESPONSE_LANGUAGE`.

## Status

Nineteen phases complete, from a single model call to a verified, multi-employee
workforce with memory, documents, integrations, a desktop window and a second
storage backend - each closed only after doing real work, recorded in
[`validation/tasks/`](validation/tasks). The roadmap is in
`development/implementation-plan.md`.
