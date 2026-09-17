# Prometheus Desktop

The desktop interface: a window, and the local runtime behind it.

It is an **adapter**, not an application. Nothing here plans, chooses an
employee, calls a model, drives a browser or decides whether an action is
allowed - all of that is the Python core, reached over the same local HTTP the
browser page uses. See `docs/adr/0014-an-interface-is-an-adapter-over-one-application-boundary.md`.

```
Tauri window
  └── React (this directory)
        └── HTTP + server-sent events on 127.0.0.1
              └── prometheus serve  ─ the same process that runs the work
```

## Running it

You need Node 20+ and a Rust toolchain (`rustup`), plus the Python side working
(`uv sync`, `uv run alembic upgrade head`).

```bash
cd desktop
npm install
npm run tauri dev        # builds the window; starts the runtime if none is up
```

The shell starts `uv run prometheus serve` itself unless something is already
answering on `127.0.0.1:8765`. A runtime you started in a terminal is used as
it is - and is **not** killed when the window closes, because it was not this
process's to stop.

| Variable | Default | What it changes |
|---|---|---|
| `PROMETHEUS_BASE_URL` | `http://127.0.0.1:8765` | Where the window looks for the runtime |
| `PROMETHEUS_RUNTIME_CMD` | `uv run prometheus serve` | How the shell starts one when none is up |

The window asks the shell for that address before it draws, and the shell waits
for the engine to answer before replying - a page that fired its first request
into a port still being bound would show an error for the whole session.

A window that cannot draw, or whose request fails, prints why on the terminal
that started it (`prometheus: the window reported a problem: ...`). A blank window
is otherwise indistinguishable from a working one with nothing to show.

Without the shell, the browser page at `http://127.0.0.1:8765` is the same
engine and the same work. `npm run dev` serves this UI at `localhost:1420`
against a runtime you start yourself.

```bash
npm test                 # frontend tests, no runtime needed
npm run build            # type-check and bundle
npm run tauri build      # a packaged application
```

## Structure - Feature-Sliced Design

Layers import **downwards only**, and two slices on the same layer never import
each other. `src/architecture.test.ts` reads the source and fails the build on a
violation, the same way `import-linter` does for the Python layers.

```
src/
├── app/          the application: one provider, one screen
├── pages/        task, Workforce, settings - screens and their state
├── widgets/      conversation and reusable page composition, no fetching
├── features/     send-request, decide-approval, stop-run - one action each
├── entities/     conversation, activity, approval, employee - what things are
└── shared/       api (the ONLY file that knows the transport), lib
```

Three rules the tests enforce, each with a reason:

- **The transport lives in `shared/api/` and nowhere else.** Which `fetch` this
  is depends on where the page runs: a browser uses its own, and a packaged
  window uses the shell's, because WebKit will not let a page served from
  `tauri://localhost` reach `http://127.0.0.1` at all. The event stream is read
  off the response body for the same reason - `EventSource` has no way through.
  One file answers "how is the runtime reached", and moving to IPC or to a
  remote engine changes that file only.
- **No model, provider or tool implementation is named anywhere.** The UI must
  not know which model answered or how the browser is driven; a branch on either
  would be a core decision copied into TypeScript, free to disagree with it.
- **No business rule.** A status is rendered, never derived; an approval is
  carried, never decided. If a field is missing, it is added to the projection
  in `application/interface/views.py`, where every interface gets it at once.
