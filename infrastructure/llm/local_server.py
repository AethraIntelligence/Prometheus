"""Starting the local model server when the catalog needs it and nobody has.

A request typed into the window with Ollama stopped used to fail on its first
model call, a second after it was sent. The error is right - the local provider
says plainly that nothing is answering - but the fix it asks for is one command
the person should not have had to know, on a machine where the platform already
knows it is the one that will be calling.

So `prometheus serve` starts it, and only in the one case where that is
unambiguous:

* **The catalog has a local entry.** A machine working through a hosted
  provider gets no model server it never asked for.
* **The address is this machine's.** A runner on another host is somebody
  else's to start, and spawning one here would answer a different address.
* **Nothing is already answering.** A server started from a terminal, or by the
  Ollama application, is used and never replaced - the same rule the window's
  shell follows for the runtime itself.

**The application is preferred to the binary.** Where Ollama is installed as
an application, its settings - the context length above all - live in the
application and are handed to the server only when the application starts it.
A bare `ollama serve` ignores them: the first version of this started one, and
a model the person had set to a 64K context loaded with 4096 tokens and
silently cut every employee's prompt short. How long a context should be is
the person's to decide, in the program they use for it, and not this
platform's.

**It is never stopped.** The server is started in its own session and outlives
this process, because it is shared: the Ollama application, a terminal and the
test suite all talk to the same one, and closing a window is not a reason to
unload a model somebody else is using. That is also how Ollama itself runs.

Failing to start it is not fatal. The runtime starts anyway, and the provider's
own error still says what is missing.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import time
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

import structlog

log = structlog.get_logger(__name__)

LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})

#: Where Ollama puts its binary when `PATH` does not say. A window opened from
#: the Dock inherits launchd's short `PATH`, not the shell's, and would find
#: nothing on it.
KNOWN_LOCATIONS = (
    Path("/opt/homebrew/bin/ollama"),
    Path("/usr/local/bin/ollama"),
    Path("/Applications/Ollama.app/Contents/Resources/ollama"),
)

#: Where the macOS application is installed, for everyone or for one person.
APPLICATIONS = (
    Path("/Applications/Ollama.app"),
    Path.home() / "Applications" / "Ollama.app",
)

#: Ollama binds its port in well under a second; loading a model is a separate,
#: later cost paid by the first call, which has its own timeout.
DEFAULT_WAIT_SECONDS = 15.0


class Outcome(StrEnum):
    ANSWERING = "answering"
    STARTED = "started"
    NOT_LOCAL = "not_local"
    NOT_INSTALLED = "not_installed"
    DID_NOT_ANSWER = "did_not_answer"


def ensure_running(
    base_url: str,
    *,
    wait_seconds: float = DEFAULT_WAIT_SECONDS,
    executable: Path | None = None,
    application: Path | None = None,
) -> Outcome:
    """Start Ollama if `base_url` is on this machine and silent.

    `executable` and `application` name what to start instead of looking for
    it; given one, the other is not looked for.
    """
    parts = urlsplit(base_url)
    host = parts.hostname or ""
    port = parts.port or 11434
    if host not in LOOPBACK:
        return Outcome.NOT_LOCAL
    if _answering(host, port):
        return Outcome.ANSWERING

    if executable is None:
        application = application or _find_application()
    command = _command(application, executable or (None if application else _find()))
    if command is None:
        log.warning("local_models.not_installed", base_url=base_url)
        return Outcome.NOT_INSTALLED

    try:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        log.warning("local_models.not_started", command=command, error=str(error))
        return Outcome.DID_NOT_ANSWER
    started_by = " ".join(command)

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if _answering(host, port):
            log.info("local_models.started", by=started_by, base_url=base_url)
            return Outcome.STARTED
        time.sleep(0.2)
    log.warning("local_models.did_not_answer", by=started_by, base_url=base_url)
    return Outcome.DID_NOT_ANSWER


def _command(application: Path | None, binary: Path | None) -> list[str] | None:
    if application is not None:
        # `-g` keeps it in the background: the person opened a window of ours,
        # not Ollama's.
        return ["open", "-g", "-a", str(application)]
    if binary is not None:
        return [str(binary), "serve"]
    return None


def _find_application() -> Path | None:
    if sys.platform != "darwin":
        return None
    return next((path for path in APPLICATIONS if path.is_dir()), None)


def _find() -> Path | None:
    found = shutil.which("ollama")
    if found:
        return Path(found)
    return next((path for path in KNOWN_LOCATIONS if path.is_file()), None)


def _answering(host: str, port: int) -> bool:
    # A connection, not a request: whether something is listening is the whole
    # question, and it is the one that works for any runner on that port.
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False
