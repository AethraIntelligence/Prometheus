#!/usr/bin/env bash
#
# Start Prometheus in the desktop window, from a clone, without a release build.
#
#   ./start-desktop.sh
#
# The window runs in development mode (`tauri dev`): the interface is served by
# Vite and the Rust shell is compiled in debug mode - once, on the first run,
# which takes a few minutes; later runs reuse it. Nothing is packaged, signed or
# installed. The browser version, with no Rust at all, is ./start.sh.
#
# It does not start `prometheus serve`. The shell does that itself when nothing
# answers on the port, and the runtime migrates the database on start, after a
# backup. A runtime already running in a terminal is used, never replaced.
#
# When the window closes - or Ctrl+C stops `tauri dev`, which ends the shell
# before it can stop its child - a runtime that was not already running before
# this script is stopped here, found by the owner record beside the data
# directory. One that was running before is left alone.

set -euo pipefail

cd "$(dirname "$0")"

say() { printf '\033[36m==>\033[0m %s\n' "$1"; }
fail() { printf '\033[31m==>\033[0m %s\n' "$1" >&2; exit 1; }

command -v uv >/dev/null || fail "uv is not installed: https://docs.astral.sh/uv/"
command -v npm >/dev/null || fail "npm is not installed. Node 20+ is needed for the window."
command -v cargo >/dev/null || fail "the Rust toolchain is not installed: https://rustup.rs"

say "Python dependencies"
uv sync --quiet

if [ ! -d desktop/node_modules ]; then
  say "Window dependencies (first run only)"
  (cd desktop && npm install)
fi

if [ ! -d desktop/src-tauri/target/debug ]; then
  say "First run: compiling the window's shell (a few minutes, once)"
fi

DATA_DIR="${PROMETHEUS_DATA_DIR:-$HOME/.prometheus}"
DATA_DIR="${DATA_DIR%/}"
OWNER="$(dirname "$DATA_DIR")/$(basename "$DATA_DIR").runtime.owner.json"

already_running=0
curl -sf -m 2 http://127.0.0.1:8765/api/health >/dev/null 2>&1 && already_running=1

stop_runtime_we_caused() {
  trap - EXIT INT TERM
  [ "$already_running" = 1 ] && return 0
  [ -f "$OWNER" ] || return 0
  pid="$(sed -n 's/.*"pid": *\([0-9][0-9]*\).*/\1/p' "$OWNER")"
  [ -n "$pid" ] || return 0
  say "Stopping the runtime the window started (pid $pid)"
  kill -TERM "$pid" 2>/dev/null || true
  for _ in $(seq 1 30); do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 1
  done
  kill -KILL "$pid" 2>/dev/null || true
}
trap stop_runtime_we_caused EXIT INT TERM

say "Starting the window. It starts the runtime unless one is answering."
(cd desktop && npm run tauri dev) || true
