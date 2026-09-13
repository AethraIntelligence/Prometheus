"""The native surface: the only capability here that can reach anything.

Nothing in this file touches a mouse. What it checks is the four things that
stand in front of one - the flag, the missing driver, the question about what is
in front, and the fact that a blocking driver call does not hold the event loop
that would answer a stop signal.
"""

from __future__ import annotations

import platform
import sys

import pytest

from domain.computer.models import Surface
from domain.errors import ComputerUseError, ConfigurationError
from infrastructure.computer.desktop import DesktopComputer, focused_application


class FakeDriver:
    """Stands in for the native driver, which is an optional extra."""

    FAILSAFE = False
    PAUSE = 0.0

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.threads: set[int] = set()

    def _record(self, name: str, *args: object, **kwargs: object) -> None:
        import threading

        self.threads.add(threading.get_ident())
        self.calls.append((name, args, kwargs))

    def click(self, **kwargs: object) -> None:
        self._record("click", **kwargs)

    def write(self, text: str) -> None:
        self._record("write", text)

    def press(self, key: str) -> None:
        self._record("press", key)

    def hotkey(self, *parts: str) -> None:
        self._record("hotkey", *parts)

    def scroll(self, amount: int) -> None:
        self._record("scroll", amount)

    def size(self) -> tuple[int, int]:
        return (1920, 1080)


def loaded(driver: FakeDriver) -> DesktopComputer:
    computer = DesktopComputer(enabled=True)
    computer._driver = driver
    return computer


def test_the_surface_says_what_it_is() -> None:
    assert DesktopComputer().surface is Surface.DESKTOP


async def test_with_the_flag_off_nothing_is_even_loaded() -> None:
    """The container does not build one either; this is the second line."""
    with pytest.raises(ConfigurationError, match="switched off"):
        await DesktopComputer(enabled=False).click(1, 1)


async def test_a_missing_driver_says_how_to_install_it(monkeypatch: pytest.MonkeyPatch) -> None:
    computer = DesktopComputer(enabled=True)
    # The driver is a dependency now; a broken environment is simulated, and a
    # test must never be the thing that moves a real mouse.
    monkeypatch.setitem(sys.modules, "pyautogui", None)

    with pytest.raises(ConfigurationError, match="uv sync"):
        await computer.click(1, 1)


async def test_a_key_combination_is_pressed_as_one() -> None:
    driver = FakeDriver()

    await loaded(driver).press("ctrl+a")

    assert driver.calls == [("hotkey", ("ctrl", "a"), {})]


async def test_a_single_key_is_not_treated_as_a_combination() -> None:
    driver = FakeDriver()

    await loaded(driver).press("Enter")

    assert driver.calls == [("press", ("Enter",), {})]


async def test_scrolling_matches_the_direction_every_other_surface_uses() -> None:
    """Positive means the content moves up, whatever the driver counts in."""
    driver = FakeDriver()

    await loaded(driver).scroll(300)

    assert driver.calls == [("scroll", (-300,), {})]


async def test_typed_text_is_handed_over_but_never_returned() -> None:
    driver = FakeDriver()

    await loaded(driver).type("hunter2")

    assert driver.calls == [("write", ("hunter2",), {})]


async def test_a_blocking_driver_call_does_not_run_on_the_event_loop() -> None:
    """An event loop stalled behind a mouse move cannot answer a stop signal."""
    import threading

    driver = FakeDriver()

    await loaded(driver).click(5, 5)

    assert driver.threads and threading.get_ident() not in driver.threads


async def test_a_driver_that_fails_raises_the_platform_s_own_error() -> None:
    class Broken(FakeDriver):
        def click(self, **kwargs: object) -> None:
            raise RuntimeError("no display")

    with pytest.raises(ComputerUseError, match="click failed on the desktop"):
        await loaded(Broken()).click(1, 1)


def test_asking_what_is_in_front_answers_or_admits_it_cannot() -> None:
    """`None` is a refusal further up, so it has to be the answer when unknown."""
    answer = focused_application()

    if platform.system() == "Darwin":
        assert answer is None or isinstance(answer, str)
    else:
        assert answer is None


async def test_the_screen_size_comes_from_the_driver() -> None:
    assert await loaded(FakeDriver()).screen_size() == (1920, 1080)
