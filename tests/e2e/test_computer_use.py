"""Phase 5, end to end on the real tools and a scripted model.

Two claims are checked here, and they are the two the phase stands on.

**An employee can operate a screen it cannot address any other way** - look,
click, type, and establish from the screen itself that it worked.

**Computer Use switches off, and everything below it keeps working.** That is
the Definition of Done, and it is the reason the hierarchy is a property of the
tools rather than a paragraph in a prompt: turning the screen off leaves the
browser tools registered, listed and callable, because they are a different rung.
"""

from __future__ import annotations

from pathlib import Path

from application.employee_runtime.approvals import ApprovalGate
from application.employee_runtime.executor import Executor
from application.employee_runtime.transcript import Transcript
from domain.computer.interfaces import InterfaceLevel
from domain.computer.models import ScreenTarget, Surface
from domain.llm.models import ToolCallRequest
from domain.policies.models import ActorKind, SimpleActor
from domain.tasks.task import Task
from infrastructure.persistence.tool_call_repository import InMemoryToolCallLog
from infrastructure.tools.builtin import build_registry
from tests.fakes.approvals import ScriptedApprovalService
from tests.fakes.browser import FakeBrowser, FakeSearchEngine
from tests.fakes.computer import FakeComputer, FakeScreenReader
from tests.fakes.employees import definition
from tests.fakes.llm import FakeLLM, reply, tool_reply

OPERATOR_TOOLS = frozenset(
    {
        "browser.open",
        "browser.extract",
        "computer.screen",
        "computer.click",
        "computer.type",
        "computer.verify",
    }
)

EVERYTHING = SimpleActor("test", ActorKind.USER, frozenset({"*"}))


def registry(root: Path, *, screens: bool = True, desktop: bool = False):
    computer = FakeComputer(surface=Surface.BROWSER)
    reader = FakeScreenReader(
        answer="A viewer with a Search field in the middle.",
        targets=(ScreenTarget("Search field", 640, 400, 0.9),),
    )
    surfaces = []
    if screens:
        surfaces.append((computer, reader))
    if desktop:
        surfaces.append((FakeComputer(surface=Surface.DESKTOP, focused="Preview"), reader))
    return computer, build_registry(
        file_root=root / "workspace",
        search_engine=FakeSearchEngine,
        browser=FakeBrowser,
        code_execution=False,
        computers=(lambda: surfaces) if surfaces else None,
    )


def opening(task: Task, employee, tools) -> Transcript:
    return Transcript(
        messages=Executor.opening_messages(
            task,
            employee,
            None,
            "You operate screens.",
            interfaces=tuple(spec.interface_level for spec in tools.list_specs(employee)),
        )
    )


async def test_an_employee_operates_a_screen_and_confirms_what_it_did(
    tmp_path: Path,
) -> None:
    computer, tools = registry(tmp_path)
    task = Task.create("Search for 'wal mode' in the embedded viewer")
    employee = definition("operator", tools=OPERATOR_TOOLS)
    log = InMemoryToolCallLog()

    llm = FakeLLM(
        [
            # The page first: it is the rung above, and the one to try first.
            tool_reply(
                ToolCallRequest(
                    id="1", name="browser.open",
                    arguments={"url": "https://sqlite.org/wal.html"},
                )
            ),
            # The viewer is a canvas, so: look, click, type, and check.
            tool_reply(
                ToolCallRequest(
                    id="2", name="computer.screen",
                    arguments={"question": "where is the search field?"},
                )
            ),
            tool_reply(
                ToolCallRequest(
                    id="3", name="computer.click",
                    arguments={"x": 640, "y": 400, "expect": "the search field has focus"},
                )
            ),
            tool_reply(
                ToolCallRequest(
                    id="4", name="computer.type",
                    arguments={"text": "wal mode", "expect": "the query is in the field"},
                )
            ),
            reply("Typed 'wal mode' into the viewer's search field, confirmed on screen."),
        ]
    )

    approvals = ScriptedApprovalService.approving()
    outcome = await Executor(
        llm,
        tools,
        call_log=log,
        approvals=ApprovalGate(approvals),
    ).run(
        task, employee, opening(task, employee, tools)
    )

    assert outcome.finished
    assert computer.names() == ["screenshot", "click", "screenshot", "type", "screenshot"]
    assert all(request.requires_explicit_confirmation for request in approvals.requests)
    # Every action was established by looking, not assumed.
    assert all(
        call.output.get("verified") is True
        for call in log.calls
        if call.tool in {"computer.click", "computer.type"}
    )
    # And the trace says how each step reached the world.
    assert [(c.tool, c.interface.value) for c in log.calls] == [
        ("browser.open", "BROWSER"),
        ("computer.screen", "COMPUTER_USE"),
        ("computer.click", "COMPUTER_USE"),
        ("computer.type", "COMPUTER_USE"),
    ]


async def test_a_coordinate_is_never_guessed_because_looking_comes_first(
    tmp_path: Path,
) -> None:
    """The employee is given the coordinate by the screen, not by the model."""
    _, tools = registry(tmp_path)
    employee = definition("operator", tools=OPERATOR_TOOLS)

    result = await tools.get("computer.screen", employee).execute(
        {"question": "where is the search field?"}
    )

    assert result.output["targets"][0]["x"] == 640


async def test_turning_computer_use_off_leaves_the_browser_path_working(
    tmp_path: Path,
) -> None:
    """Phase 5's Definition of Done, as one assertion pair."""
    _, with_screens = registry(tmp_path, screens=True)
    _, without = registry(tmp_path, screens=False)

    names = {spec.name for spec in with_screens.list_specs(EVERYTHING)}
    reduced = {spec.name for spec in without.list_specs(EVERYTHING)}

    assert "computer.click" in names
    assert "computer.click" not in reduced
    assert {"browser.open", "browser.extract", "web.search"} <= reduced


async def test_a_scenario_with_a_browser_path_still_finishes_with_the_screen_off(
    tmp_path: Path,
) -> None:
    _, tools = registry(tmp_path, screens=False)
    task = Task.create("Find out what WAL mode does")
    employee = definition("operator", tools=OPERATOR_TOOLS)

    llm = FakeLLM(
        [
            tool_reply(
                ToolCallRequest(
                    id="1", name="browser.open",
                    arguments={"url": "https://sqlite.org/wal.html"},
                )
            ),
            tool_reply(ToolCallRequest(id="2", name="browser.extract", arguments={})),
            reply("Readers do not block writers."),
        ]
    )

    outcome = await Executor(llm, tools).run(
        task, employee, opening(task, employee, tools)
    )

    assert outcome.finished
    assert "Readers do not block" in outcome.answer


async def test_the_desktop_is_a_separate_grant_from_the_page(tmp_path: Path) -> None:
    """An employee that may click in a page it opened must not thereby own the machine."""
    _, tools = registry(tmp_path, screens=True, desktop=True)
    employee = definition("operator", tools=OPERATOR_TOOLS)

    allowed = {spec.name for spec in tools.list_specs(employee)}
    everything = {spec.name for spec in tools.list_specs(EVERYTHING)}

    assert "desktop.click" in everything
    assert "desktop.click" not in allowed


async def test_nothing_reaches_the_desktop_without_a_person_saying_yes(
    tmp_path: Path,
) -> None:
    surface = FakeComputer(surface=Surface.DESKTOP, focused="Preview")
    tools = build_registry(
        file_root=tmp_path / "workspace",
        code_execution=False,
        computers=lambda: [(surface, FakeScreenReader())],
    )
    task = Task.create("Click the button in Preview")
    employee = definition("operator", tools=frozenset({"desktop.click"}))
    service = ScriptedApprovalService.rejecting()

    llm = FakeLLM(
        [
            tool_reply(
                ToolCallRequest(id="1", name="desktop.click", arguments={"x": 5, "y": 5})
            ),
            reply("The user did not approve clicking on their desktop."),
        ]
    )

    await Executor(llm, tools, approvals=ApprovalGate(service)).run(
        task, employee, opening(task, employee, tools)
    )

    assert surface.actions == [], "a refused desktop click must never reach the surface"
    assert service.requests[0].risk_level.value == "HIGH"


def test_the_desktop_is_the_bottom_rung_and_says_so(tmp_path: Path) -> None:
    _, tools = registry(tmp_path, screens=True, desktop=True)

    levels = {
        spec.name: spec.interface_level for spec in tools.list_specs(EVERYTHING)
    }

    assert levels["fs.read"] is InterfaceLevel.API
    assert levels["browser.open"] is InterfaceLevel.BROWSER
    assert levels["computer.click"] is InterfaceLevel.COMPUTER_USE
    assert levels["desktop.click"] is InterfaceLevel.DESKTOP
