#!/usr/bin/env bash
#
# Start Prometheus from a clone, with nothing built.
#
#   ./start.sh            the runtime and the interface, opened in your browser
#   ./start.sh --no-open  the same, without opening a browser
#
# The desktop window is ./start-desktop.sh.
#
# Nothing is compiled or bundled in the default mode: the runtime runs from
# source through `uv`, and the interface is served by Vite's development server,
# which the runtime already accepts requests from. Every setup step is skipped
# when it is already done, so a second run costs a few seconds.
#
# The database is not migrated here. `prometheus serve` does it on start, after
# checking the schema and taking a backup - the same path an installed
# application takes - so there is one way the store is upgraded, not two.
#
# A runtime already answering on the port is used, never replaced: a terminal
# running `prometheus serve` keeps its engine, and this attaches to it. What this
# script started it also stops, on Ctrl+C.

set -euo pipefail

cd "$(dirname "$0")"

OPEN=1
for argument in "$@"; do
  case "$argument" in
    --no-open) OPEN=0 ;;
    -h|--help) sed -n '3,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) printf 'Unknown option: %s (see ./start.sh --help)\n' "$argument" >&2; exit 2 ;;
  esac
done

say() { printf '\033[36m==>\033[0m %s\n' "$1"; }
fail() { printf '\033[31m==>\033[0m %s\n' "$1" >&2; exit 1; }

command -v uv >/dev/null || fail "uv is not installed: https://docs.astral.sh/uv/"
command -v npm >/dev/null || fail "npm is not installed. Node 20+ is needed for the interface."

say "Python dependencies"
uv sync --quiet

if [ ! -d desktop/node_modules ]; then
  say "Interface dependencies (first run only)"
  (cd desktop && npm install)
fi

RUNTIME_URL="http://127.0.0.1:8765"
INTERFACE_URL="http://localhost:1420"
STARTED=()

# Each started process gets a process group of its own (job control), and the
# whole group is stopped: `npx` and `uv run` start the real server as a child,
# and stopping only the parent left Vite answering on the port.
set -m
stop_started() {
  trap - EXIT INT TERM
  for pid in "${STARTED[@]:-}"; do
    [ -n "$pid" ] && kill -TERM -- "-$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap stop_started EXIT INT TERM

answering() { curl -sf -m 2 "$1" >/dev/null 2>&1; }

wait_for() {
  local url="$1" what="$2" seconds="$3"
  for _ in $(seq 1 "$seconds"); do
    answering "$url" && return 0
    sleep 1
  done
  fail "$what did not start within ${seconds}s"
}

if answering "$RUNTIME_URL/api/health"; then
  say "A runtime is already answering on $RUNTIME_URL - using it"
else
  say "Starting the runtime (migrates the database on first start)"
  uv run prometheus serve &
  STARTED+=("$!")
  wait_for "$RUNTIME_URL/api/health" "The runtime" 120
fi

if answering "$INTERFACE_URL"; then
  say "The interface is already served on $INTERFACE_URL"
else
  say "Starting the interface"
  (cd desktop && exec npx vite --port 1420 --strictPort) &
  STARTED+=("$!")
  wait_for "$INTERFACE_URL" "The interface" 60
fi

say "Prometheus is ready: $INTERFACE_URL"
if [ "$OPEN" = 1 ]; then
  if command -v open >/dev/null; then open "$INTERFACE_URL"
  elif command -v xdg-open >/dev/null; then xdg-open "$INTERFACE_URL" >/dev/null 2>&1 &
  fi
fi
say "Press Ctrl+C to stop."
wait
