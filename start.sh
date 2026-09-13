#!/usr/bin/env bash
#
# Start the desktop window for development.
#
# `cd desktop && npm run tauri dev` is the whole of it once a machine is set
# up; this script is the setup nobody remembers on a fresh clone - the Python
# dependencies, the Node ones, and the migration. Each step is skipped when it
# is already done, so running this twice costs a few seconds and changes
# nothing.
#
# It does not start `prometheus serve`. The Tauri shell does that itself when
# nothing is answering on the port, and never kills a runtime it did not start -
# so a terminal already running `prometheus serve` keeps its engine and the window
# attaches to it. Starting a second one here would take that decision away from
# whoever is at the keyboard.

set -euo pipefail

cd "$(dirname "$0")"

say() { printf '\033[36m==>\033[0m %s\n' "$1"; }
fail() { printf '\033[31m==>\033[0m %s\n' "$1" >&2; exit 1; }

command -v uv >/dev/null || fail "uv is not installed: https://docs.astral.sh/uv/"
command -v npm >/dev/null || fail "npm is not installed. Node 20+ is needed for the window."
command -v cargo >/dev/null || fail "the Rust toolchain is not installed: https://rustup.rs"

say "Python dependencies"
uv sync --quiet

say "Database schema"
# Alembic is quiet when there is nothing to do, so this is cheap on every run
# and is the one step whose absence looks like a broken application rather than
# a missing step: without it every request answers "no schema yet".
uv run alembic upgrade head

if [ ! -d desktop/node_modules ]; then
  say "Window dependencies (first run only)"
  (cd desktop && npm install)
fi

say "Starting the window. It will start the runtime unless one is answering."
cd desktop
exec npm run tauri dev
