"""A thread works in a folder: its own by default, or one a person chose."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from application.interface.contracts import UserRequest
from application.workspaces.folders import chosen_folder, folder_name, session_folder
from application.workspaces.service import WorkspaceService
from domain.conversations.models import Conversation
from domain.errors import FolderError
from domain.workforce import directions
from domain.workforce.directions import Directions
from domain.workspace.models import DEFAULT_WORKSPACE_ID
from infrastructure.persistence.workspace_repository import InMemoryWorkspaceRepository
from infrastructure.tools.filesystem import FileRoot, FileWriteTool
from infrastructure.tools.pdf import PdfWriteTool
from infrastructure.workspace.context import LocalWorkspaceContext
from tests.fakes.documents import FakePdfRenderer
from tests.unit.test_interface_boundary import build


def context(tmp_path: Path) -> LocalWorkspaceContext:
    return LocalWorkspaceContext(
        path=tmp_path / "ACTIVE_WORKSPACE",
        default_root=tmp_path / "Prometheus",
        roots_base=tmp_path / "workspaces",
    )


# --- Naming and choosing ------------------------------------------------------


def test_a_thread_is_named_on_disk_the_way_it_is_named_in_the_list() -> None:
    thread = Conversation.create("Check new 10 news in IT/AI: today?")

    assert folder_name(thread) == "Check new 10 news in IT AI today"
    assert folder_name(Conversation.create()).startswith("Task ")


def test_a_second_thread_of_one_name_does_not_move_into_the_first_ones_folder(
    tmp_path: Path,
) -> None:
    thread = Conversation.create("News")
    (tmp_path / "News").mkdir()

    assert session_folder(tmp_path, thread) == tmp_path / "News 2"


def test_a_folder_that_hands_over_every_file_is_refused(tmp_path: Path) -> None:
    for reckless in ("/", str(Path.home()), "relative/path", ""):
        with pytest.raises(FolderError):
            chosen_folder(reckless)
    (tmp_path / "a.txt").write_text("x")
    with pytest.raises(FolderError):
        chosen_folder(str(tmp_path / "a.txt"))
    assert chosen_folder(str(tmp_path)) == tmp_path.resolve()


# --- The tools follow the thread ----------------------------------------------


async def test_a_run_writes_into_its_threads_folder_and_a_run_without_one_into_the_root(
    tmp_path: Path,
) -> None:
    workspaces = context(tmp_path)
    tool = FileWriteTool(FileRoot(workspaces.current_root))
    folder = tmp_path / "Prometheus" / "News"

    async def in_thread() -> None:
        with directions.given(Directions(folder=str(folder))):
            await tool.execute({"path": "raw.txt", "content": "thread"})

    await asyncio.gather(in_thread(), tool.execute({"path": "raw.txt", "content": "root"}))

    assert (folder / "raw.txt").read_text() == "thread"
    assert (tmp_path / "Prometheus" / "raw.txt").read_text() == "root"


#: "News" in Russian, spelled as escapes: the point is a title in another
#: alphabet, and the source stays English-only.
TITLE = "\u041d\u043e\u0432\u043e\u0441\u0442\u0438"


async def test_a_pdf_is_written_where_text_would_be_and_reported_the_same_way(
    tmp_path: Path,
) -> None:
    renderer = FakePdfRenderer()
    tool = PdfWriteTool(FileRoot(tmp_path), renderer)

    result = await tool.execute(
        {"path": "report", "title": TITLE, "content": "1. **One**\n\n<script>x</script>"}
    )

    assert result.success, result.error
    assert result.output["path"] == "report.pdf"
    assert result.output["bytes_written"] == (tmp_path / "report.pdf").stat().st_size
    html, _ = renderer.rendered[0]
    assert "<strong>One</strong>" in html and f"<h1>{TITLE}</h1>" in html
    assert "<script>" not in html, "a page quoted in a report is text, not markup"
    assert tool.assess({"path": "report.pdf"}).risk_level.name == "HIGH"


# --- The thread keeps its folder ----------------------------------------------


async def settle() -> None:
    """The objectives run in the background; let them reach the manager."""
    for _ in range(5):
        await asyncio.sleep(0)


def with_workspaces(tmp_path: Path):
    service, parts = build()
    workspaces = WorkspaceService(
        repository=InMemoryWorkspaceRepository(), context=context(tmp_path)
    )
    service._d = replace(service._d, workspaces=workspaces)
    return service, parts


async def test_the_first_request_gives_a_thread_its_own_folder_and_later_ones_keep_it(
    tmp_path: Path,
) -> None:
    service, parts = with_workspaces(tmp_path)
    thread = await service.create_conversation()
    conversation_id = UUID(thread["id"])

    await service.submit(UserRequest(content="Check the news", conversation_id=conversation_id))
    await service.create_conversation()  # another thread, same title later
    await service.submit(UserRequest(content="Now in a PDF", conversation_id=conversation_id))
    await settle()

    expected = str(tmp_path / "Prometheus" / "Check the news")
    assert [item.folder for item in parts["manager"].directions] == [expected, expected]
    opened = await service.get_conversation(conversation_id)
    assert opened["folder"] == expected


async def test_a_folder_a_person_chose_is_where_the_next_request_works(tmp_path: Path) -> None:
    service, parts = with_workspaces(tmp_path)
    chosen = tmp_path / "Clients"
    chosen.mkdir()
    thread = await service.create_conversation()
    conversation_id = UUID(thread["id"])

    changed = await service.set_conversation_folder(conversation_id, str(chosen))
    await service.submit(UserRequest(content="Sort these", conversation_id=conversation_id))
    await settle()
    reset = await service.set_conversation_folder(conversation_id, "")

    assert changed["folder"] == str(chosen.resolve())
    assert parts["manager"].directions[-1].folder == str(chosen.resolve())
    assert reset["folder"].startswith(str(tmp_path / "Prometheus"))
    with pytest.raises(FolderError):
        await service.set_conversation_folder(conversation_id, "/")


async def test_a_workspace_keeps_the_folders_a_person_saved(tmp_path: Path) -> None:
    service, _ = with_workspaces(tmp_path)
    one, two = tmp_path / "one", tmp_path / "two"
    await service.list_workspaces()

    saved = await service.update_workspace(
        DEFAULT_WORKSPACE_ID, folders=[str(one), str(two), str(one), " "]
    )
    renamed = await service.update_workspace(DEFAULT_WORKSPACE_ID, name="Home")

    assert saved["folders"] == [str(one.resolve()), str(two.resolve())]
    assert renamed["folders"] == saved["folders"], "a rename does not forget them"
