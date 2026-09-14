# ADR 0023: The switches the runtime starts with are changed from the window

## Status

Accepted - 2026-09-14. Departs from the settings screen's original rule.

## Context

The settings screen was written "deliberately not an administration console":
everything configurable was a file the runtime already read, and a screen that
mirrored `.env` would be a second place to change it - the one that goes stale.

In use that left a person of the packaged window with no way to change anything
the runtime starts with. Turning on scheduled work, switching memory off, giving
the desktop tools an application they may act in, or making a model wait longer
all meant finding `.env` - which a window opened from the Dock does not even read
from a place the person knows - editing a line and restarting. The flags are
the platform's most important switches, and they were the ones a person could
not reach.

## Decision

**Settings -> General lists a chosen set of `Settings` fields**
(`app/config/editable.py`): the feature flags, approvals, answer language,
model retries and timeout, tool timeouts, desktop-control limits, memory and
knowledge limits, and logging. Keys, provider endpoints, paths, the database URL
and the interface's host and port are not on it: a key belongs in the
credential store, moving the store is `storage-migrate`, and a window that can
rebind its own server can lock itself out or expose an unauthenticated surface.

**What is saved goes to `$PROMETHEUS_DATA_DIR/settings.json`**, and `Settings`
reads it as a source of its own: below the process environment, above `.env`.
The window is where a person changes something *now*, so it outranks a file they
never opened; a variable exported in a shell is the most specific thing said
about one process, so it outranks the window - and the window shows that
setting with the variable's name and does not offer the control. A file naming
anything not on the list is ignored, so a hand edit cannot move the store.

**A change is for the next start, and says so.** Every one of these is read when
the container is built - the flags decide which tools exist at all - so each
setting carries what is running and what was saved, and the screen marks the
difference. Applying a flag to a running engine would mean rebuilding the
platform under a run that is using it.

**Validation is the field's own type.** `app/config/general.py` puts a value
through the pydantic annotation the field is declared with, plus a minimum, and
refuses the whole change with a sentence. The application layer sees only
`domain.configuration.protocols.SettingsEditor`; the window renders what the
runtime returns and decides nothing.

**Resetting forgets rather than writes a default.** One setting, or all of them
in two clicks, leaves the file; the value comes from `.env` or the field's
default again, and each setting carries that fallback so the screen can say what
a reset goes back to. Copying the default into the file would outrank a later
edit to `.env` without anyone seeing why.

## Consequences

- `.env` stays where keys and paths go, and still works for everything; a value
  set in both places is the window's until it is changed back there or the file
  is deleted.
- The CLI reads the same file, because it reads `Settings`: a switch turned off
  in the window is off for `ask-prometheus` too.
- The test suite points the default data directory at a temporary one, so a
  developer's saved switches never reach a test that did not name a directory.
- There is no restart button yet. The shell only owns a runtime it started, and
  a runtime started in a terminal is not the window's to restart.
