"""Operating a screen, as five tools over three contracts.

The shape of the loop these tools are built for is: **look, act, check.** A
model that clicks and assumes is the failure mode Computer Use is famous for,
and it is not fixed by telling the model to be careful. So it is built into the
tools:

- `computer.screen` is the only way to find out where anything is, and it comes
  back with coordinates rather than prose;
- every acting tool takes an optional `expect`, and when it is given, the tool
  takes a second screenshot and asks whether the expected thing actually
  happened. The answer, not the assumption, is what goes into the observation;
- `computer.verify` asks the same question on its own, for a state that no
  single action produced.

Risk is read from the surface, not from the tool. The same click is a different
act depending on where it lands: in a page the platform opened, it is undone by
going back; on the user's own desktop it can be anything at all. So the browser
surface runs at MEDIUM and the desktop asks a person every time. See ADR 0005.
"""

from __future__ import annotations

from collections.abc import Callable

from domain.approvals.gate import RiskAssessment
from domain.capabilities.models import Capability
from domain.computer.interfaces import InterfaceLevel
from domain.computer.models import ComputerAction, Screenshot, ScreenView, Surface
from domain.computer.protocols import Computer, ScreenReader
from domain.errors import ConfigurationError
from domain.integrations.untrusted import TrustLevel
from domain.policies.models import RiskLevel
from domain.policies.risk import Effect
from domain.tools.models import ToolResult, ToolSpec
from domain.tools.schema import Param
from infrastructure.tools.base import BaseTool

#: Clicking twice is a double click; more than that is a model in a loop.
MAX_CLICKS = 3

#: A screen reader, or a way to get one later. See `ComputerTool.eyes`.
Eyes = ScreenReader | Callable[[], ScreenReader]

_LEVELS = {
    Surface.BROWSER: InterfaceLevel.COMPUTER_USE,
    Surface.DESKTOP: InterfaceLevel.DESKTOP,
}

#: The two surfaces get different tool names on purpose. They are different
#: rungs of the hierarchy with different blast radii, and an employee that may
#: click inside a page it opened must not thereby be able to click on the user's
#: desktop. Least privilege only works if the two can be listed separately.
_PREFIX = {Surface.BROWSER: "computer", Surface.DESKTOP: "desktop"}

_WHERE = {
    Surface.BROWSER: "Acts inside the page the browser has open.",
    Surface.DESKTOP: "Acts on the user's own machine. Every action here waits "
    "for the user to approve it.",
}


class ComputerTool(BaseTool):
    """What the five of them share: a surface, eyes, and the check afterwards."""

    def __init__(
        self,
        computer: Computer,
        reader: Eyes,
        name: str,
        description: str,
        *parameters: Param,
        #: Does this tool change the state of what is in front of it? Looking
        #: and scrolling do not; clicking, typing and pressing a key do. It is
        #: the question the gate cares about, so it is the one asked here -
        #: "does it touch the surface at all" would put a scroll and a click in
        #: the same bucket, and they are not the same act.
        changes_state: bool = True,
    ) -> None:
        surface = computer.surface
        super().__init__(
            ToolSpec.of(
                f"{_PREFIX[surface]}.{name}",
                # `{prefix}` so a tool that points at its siblings names the ones
                # this employee actually has, rather than the other surface's.
                f"{description.format(prefix=_PREFIX[surface])} {_WHERE[surface]}",
                *parameters,
                # An action on the user's own desktop is not undoable by anything
                # this platform can offer, and says so rather than being trusted
                # to be careful. Looking and scrolling are always reversible.
                reversible=not (changes_state and surface is Surface.DESKTOP),
                effect=Effect.WRITE if changes_state else Effect.READ,
                risk_level=_risk_of(surface, changes_state),
                capabilities=frozenset({Capability.COMPUTER_USE}),
                interface_level=_LEVELS[surface],
                result_trust=TrustLevel.UNTRUSTED,
                result_kind="screen",
            )
        )
        self._computer = computer
        self._reader = reader
        self._resolved: ScreenReader | None = None

    @property
    def eyes(self) -> ScreenReader:
        """Resolved on first use, never at construction.

        The registry is built to be *listed* far more often than it is called -
        `prometheus tools` does exactly that - and building the reader routes a model,
        which on a machine with no key configured is a failure. Declaring a tool
        must not need a provider.
        """
        if self._resolved is None:
            self._resolved = self._reader() if callable(self._reader) else self._reader
        return self._resolved

    async def _look(self) -> Screenshot:
        return await self._computer.screenshot()

    async def _check(self, expect: str) -> dict[str, object]:
        """Confirm by looking, and say plainly when the answer is no.

        Returned as part of the acting tool's own result rather than left for a
        following step: an action whose effect was never established is the thing
        this is here to prevent, and a separate step is a step a model can skip.
        """
        if not expect.strip():
            return {"verified": False, "note": "No expectation was given, so nothing was checked."}
        view = await self.eyes.confirm(await self._look(), expect)
        return {
            "verified": bool(view.confirmed),
            "expected": expect,
            "screen_shows": view.answer,
        }


def _risk_of(surface: Surface, changes_state: bool) -> RiskLevel:
    if not changes_state:
        return RiskLevel.LOW
    return RiskLevel.HIGH if surface is Surface.DESKTOP else RiskLevel.MEDIUM


def _view_to_output(view: ScreenView) -> dict[str, object]:
    return {
        "screen_shows": view.answer,
        "targets": [
            {"label": t.label, "x": t.x, "y": t.y, "confidence": round(t.confidence, 2)}
            for t in view.targets
        ],
    }


class ScreenTool(ComputerTool):
    """Look at the screen and ask a question about it."""

    def __init__(self, computer: Computer, reader: Eyes) -> None:
        super().__init__(
            computer,
            reader,
            "screen",
            "Look at the screen and answer a question about it. Returns what is "
            "there and the coordinates of anything you asked to find, which is "
            "what {prefix}.click needs. Do this before every click.",
            Param(
                "question",
                description="What you need to know, for example 'where is the "
                "Search field?' or 'what does this dialog say?'",
            ),
            changes_state=False,
        )

    async def run(self, question: str) -> ToolResult:
        screenshot = await self._look()
        view = await self.eyes.read(screenshot, question)
        return ToolResult.ok(
            question=question,
            screen_width=screenshot.width,
            screen_height=screenshot.height,
            **_view_to_output(view),
        )


class VerifyTool(ComputerTool):
    """Check the screen against an expectation, without touching anything."""

    def __init__(self, computer: Computer, reader: Eyes) -> None:
        super().__init__(
            computer,
            reader,
            "verify",
            "Check whether the screen actually shows something. Use this to "
            "establish that what you did worked, instead of assuming it did.",
            Param(
                "expectation",
                description="What should be on the screen if the last step worked, "
                "for example 'the settings dialog is open'.",
            ),
            changes_state=False,
        )

    async def run(self, expectation: str) -> ToolResult:
        view = await self.eyes.confirm(await self._look(), expectation)
        return ToolResult.ok(
            expectation=expectation,
            verified=bool(view.confirmed),
            **_view_to_output(view),
        )


class ClickTool(ComputerTool):
    """Implements `domain.tools.protocols.Tool` and `RiskAssessor`."""

    def __init__(self, computer: Computer, reader: Eyes) -> None:
        super().__init__(
            computer,
            reader,
            "click",
            "Click at a point on the screen. Get the coordinates from "
            "{prefix}.screen first - do not guess them.",
            Param("x", type="integer", description="Pixels from the left edge."),
            Param("y", type="integer", description="Pixels from the top edge."),
            Param("clicks", type="integer", required=False, default=1,
                  description="2 for a double click."),
            Param("expect", required=False, default="",
                  description="What should be on the screen afterwards. Given, the "
                  "click is checked by looking, and the result says whether it worked."),
        )

    def assess(self, input_data: dict[str, object]) -> RiskAssessment | None:
        return _assess(self._computer.surface, ComputerAction.CLICK)

    async def run(
        self, x: int, y: int, clicks: int = 1, expect: str = ""
    ) -> ToolResult:
        await self._computer.click(x, y, clicks=min(max(clicks, 1), MAX_CLICKS))
        return ToolResult.ok(
            clicked={"x": x, "y": y, "clicks": clicks}, **await self._check(expect)
        )


class TypeTool(ComputerTool):
    """Implements `domain.tools.protocols.Tool` and `RiskAssessor`."""

    def __init__(self, computer: Computer, reader: Eyes) -> None:
        super().__init__(
            computer,
            reader,
            "type",
            "Type text wherever the cursor is. Click the field first.",
            Param("text", description="The text to type."),
            Param("expect", required=False, default="",
                  description="What should be on the screen afterwards."),
        )

    def assess(self, input_data: dict[str, object]) -> RiskAssessment | None:
        return _assess(self._computer.surface, ComputerAction.TYPE)

    async def run(self, text: str, expect: str = "") -> ToolResult:
        await self._computer.type(text)
        # The text is not echoed back: it goes into the transcript and the tool
        # log, and what gets typed into a screen is often a credential.
        return ToolResult.ok(typed_characters=len(text), **await self._check(expect))


class PressTool(ComputerTool):
    """Implements `domain.tools.protocols.Tool` and `RiskAssessor`."""

    def __init__(self, computer: Computer, reader: Eyes) -> None:
        super().__init__(
            computer,
            reader,
            "press",
            "Press a key or a combination, for example 'Enter', 'Escape' or "
            "'ctrl+a'.",
            Param("key", description="The key, or parts joined by '+'."),
            Param("expect", required=False, default="",
                  description="What should be on the screen afterwards."),
        )

    def assess(self, input_data: dict[str, object]) -> RiskAssessment | None:
        return _assess(self._computer.surface, ComputerAction.PRESS)

    async def run(self, key: str, expect: str = "") -> ToolResult:
        await self._computer.press(key)
        return ToolResult.ok(pressed=key, **await self._check(expect))


class ScrollTool(ComputerTool):
    """Implements `domain.tools.protocols.Tool` and `RiskAssessor`."""

    def __init__(self, computer: Computer, reader: Eyes) -> None:
        super().__init__(
            computer,
            reader,
            "scroll",
            "Scroll the screen. A positive amount moves the content up, which is "
            "what you want to read further down a page.",
            Param("amount", type="integer", description="How far, in pixels."),
            Param("expect", required=False, default="",
                  description="What should be on the screen afterwards."),
            # Scrolling changes what is visible, not what exists. Asking a person
            # to approve it - even on the desktop - would be a prompt about
            # nothing, and prompts about nothing are how people learn to click
            # through the prompts that matter.
            changes_state=False,
        )

    async def run(self, amount: int, expect: str = "") -> ToolResult:
        await self._computer.scroll(amount)
        return ToolResult.ok(scrolled=amount, **await self._check(expect))


def _assess(surface: Surface, action: ComputerAction) -> RiskAssessment | None:
    """Where the action lands is what decides, so the surface is what is asked."""
    if surface is Surface.DESKTOP:
        return RiskAssessment(
            RiskLevel.HIGH,
            f"{action.value.lower()} on the user's own desktop, outside anything "
            "this platform controls",
        )
    return RiskAssessment(RiskLevel.MEDIUM, "")


def computer_tools(computer: Computer, reader: Eyes) -> list[ComputerTool]:
    """The six, in the order they are meant to be used."""
    if reader is None:  # pragma: no cover - the container never builds one without
        raise ConfigurationError("Computer use needs a screen reader with vision")
    return [
        ScreenTool(computer, reader),
        ClickTool(computer, reader),
        TypeTool(computer, reader),
        PressTool(computer, reader),
        ScrollTool(computer, reader),
        VerifyTool(computer, reader),
    ]
