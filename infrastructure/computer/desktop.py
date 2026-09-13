"""The user's own machine, and the only capability here that can reach anything.

Everything else in the platform is confined by construction: the filesystem
tools cannot leave one directory, the browser cannot leave its tab. This one
can click anywhere, so it is the one place where the confinement has to be
argued rather than assumed. Four things stand between it and the machine:

1. **A flag.** `PROMETHEUS_FLAGS__COMPUTER_USE` is off by default, and with it off this
   class is never built - the container does not import it.
2. **An optional dependency.** The native driver is an extra, imported at call
   time like Playwright, so installing the platform does not install a robot.
3. **`ComputerConstraints`,** applied by `GuardedComputer` in front of every
   action: which applications may be in front, which part of the screen may be
   touched, how many actions in total.
4. **Approval.** The desktop tools declare themselves irreversible, so every
   desktop action waits for a person. That is deliberately heavier than the
   browser surface - see docs/adr/0005.

The driver is synchronous and blocks for the duration of a click, so every call
goes through a thread. An event loop stalled behind a mouse move is an event
loop that cannot answer a stop signal.
"""

from __future__ import annotations

import asyncio
import io
import platform
import shutil
import subprocess
from typing import Any

from domain.computer.models import Screenshot, Surface
from domain.errors import ComputerUseError, ConfigurationError
from infrastructure.observability.logging import get_logger

log = get_logger(__name__)

INSTALL_HINT = (
    "Desktop computer use needs its driver, which `uv sync` installs. Run it again: uv sync"
)

#: How long the platform waits to be told which application is in front. The
#: answer gates every desktop action, so a hung query has to fail rather than
#: hold the machine.
FOCUS_QUERY_TIMEOUT_SECONDS = 3.0

_MACOS_FRONTMOST = (
    'tell application "System Events" to get name of first process whose frontmost is true'
)


def focused_application() -> str | None:
    """Which application is in front, or `None` when the platform cannot say.

    `None` is a refusal, not a shrug: `ComputerConstraints.check_application`
    turns it into a denial. Implemented for macOS, which is where this was
    developed and validated; on anything else the honest answer is that we do
    not know, and the constraint refuses accordingly.
    """
    if platform.system() != "Darwin" or shutil.which("osascript") is None:
        return None
    try:
        completed = subprocess.run(
            ["osascript", "-e", _MACOS_FRONTMOST],
            capture_output=True,
            text=True,
            timeout=FOCUS_QUERY_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        log.warning("computer.focus_query_failed", error=str(error))
        return None
    name = completed.stdout.strip()
    return name or None


class DesktopComputer:
    """Implements `domain.computer.protocols.Computer` on the native desktop."""

    def __init__(self, *, enabled: bool = False, action_pause_seconds: float = 0.1) -> None:
        self._enabled = enabled
        self._pause = action_pause_seconds
        self._driver: Any = None

    @property
    def surface(self) -> Surface:
        return Surface.DESKTOP

    def focused_application(self) -> str | None:
        return focused_application()

    # --- Driver ---------------------------------------------------------------

    def _load(self) -> Any:
        """Import the native driver, late and once."""
        if not self._enabled:
            raise ConfigurationError(
                "Computer use on the desktop is switched off. Set "
                "PROMETHEUS_FLAGS__COMPUTER_USE=true to turn it on."
            )
        if self._driver is not None:
            return self._driver
        try:
            import pyautogui
        except ImportError as error:
            raise ConfigurationError(INSTALL_HINT) from error
        except Exception as error:
            # On a headless machine the driver fails at import while looking for
            # a display. That is a configuration problem, not a bug.
            raise ConfigurationError(
                f"The desktop driver could not reach a display: {error}"
            ) from error

        # Its own emergency stop: a mouse thrown into a screen corner aborts the
        # next action. Kept on - it is the fastest brake in the system, and the
        # only one the user can reach without a keyboard.
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = self._pause
        self._driver = pyautogui
        return self._driver

    @staticmethod
    async def _do(action: str, function: Any, *args: Any, **kwargs: Any) -> Any:
        """Run one blocking driver call off the loop, and name it if it fails."""
        try:
            return await asyncio.to_thread(function, *args, **kwargs)
        except Exception as error:
            raise ComputerUseError(f"{action} failed on the desktop: {error}") from error

    # --- Computer -------------------------------------------------------------

    async def screenshot(self) -> Screenshot:
        driver = self._load()

        def _capture() -> tuple[bytes, int, int]:
            image = driver.screenshot()
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue(), image.width, image.height

        try:
            data, width, height = await asyncio.to_thread(_capture)
        except Exception as error:
            raise ComputerUseError(f"screenshot failed on the desktop: {error}") from error
        return Screenshot(image=data, width=width, height=height)

    async def click(self, x: int, y: int, *, clicks: int = 1) -> None:
        driver = self._load()
        await self._do("click", driver.click, x=x, y=y, clicks=max(clicks, 1))
        log.info("computer.clicked", surface="DESKTOP", x=x, y=y, clicks=clicks)

    async def type(self, text: str) -> None:
        driver = self._load()
        await self._do("type", driver.write, text)
        # The text itself is never logged: it is the user's, and a password
        # typed into a login form would otherwise land in the log file.
        log.info("computer.typed", surface="DESKTOP", characters=len(text))

    async def press(self, key: str) -> None:
        driver = self._load()
        parts = [part.strip() for part in key.split("+") if part.strip()]
        if len(parts) > 1:
            await self._do("press", driver.hotkey, *parts)
        else:
            await self._do("press", driver.press, key)
        log.info("computer.pressed", surface="DESKTOP", key=key)

    async def scroll(self, amount: int) -> None:
        driver = self._load()
        # Inverted: every other surface here counts a positive scroll as moving
        # the content up, and the driver counts wheel clicks the other way.
        await self._do("scroll", driver.scroll, -amount)
        log.info("computer.scrolled", surface="DESKTOP", amount=amount)

    async def screen_size(self) -> tuple[int, int]:
        driver = self._load()
        size = await asyncio.to_thread(driver.size)
        return int(size[0]), int(size[1])
