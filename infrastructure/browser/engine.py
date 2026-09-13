"""Fetching the browser engine the browser tools drive, once, on its own.

`uv sync` installs Playwright, which is a driver and not a browser: the Chromium
it launches is a separate download that no package manager step performs. A
fresh clone therefore had every web task fail on its first page with "run
playwright install", a command nobody who had just run the app knew about.

So `prometheus serve` asks for it at start, in a thread, and does not wait:
`playwright install chromium` returns in about a second when the engine is
already there, and a first download should not hold the window's runtime from
answering. A failure is a log line - the browser tools then say what is missing
when they are used, which is what they did before.
"""

from __future__ import annotations

import subprocess
import sys
import threading

import structlog

log = structlog.get_logger(__name__)

#: A first download is a couple of hundred megabytes on an ordinary connection.
TIMEOUT_SECONDS = 900


def fetch_in_background() -> threading.Thread:
    thread = threading.Thread(target=_fetch, name="prometheus-browser-engine", daemon=True)
    thread.start()
    return thread


def _fetch() -> None:
    try:
        finished = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        log.warning("browser.engine_not_fetched", error=str(error))
        return
    if finished.returncode != 0:
        log.warning("browser.engine_not_fetched", error=finished.stderr.strip()[-500:])
    else:
        log.info("browser.engine_ready")
