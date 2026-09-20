"""Files: what the tools do, and what they refuse to do."""

from __future__ import annotations

from pathlib import Path

import pytest

from domain.policies.models import RiskLevel
from infrastructure.tools.filesystem import (
    FileListTool,
    FileMoveTool,
    FileReadTool,
    FileRoot,
    FileWriteTool,
)


@pytest.fixture
def root(tmp_path: Path) -> FileRoot:
    root = tmp_path / "root"
    root.mkdir()
    (root / "notes.txt").write_text("hello", encoding="utf-8")
    (root / "invoices").mkdir()
    return FileRoot(root)


async def test_a_file_inside_the_root_is_read(root: FileRoot) -> None:
    result = await FileReadTool(root).execute({"path": "notes.txt"})

    assert result.success
    assert result.output["content"] == "hello"
    assert result.output["truncated"] is False


async def test_a_path_escaping_the_root_is_refused(root: FileRoot) -> None:
    result = await FileReadTool(root).execute({"path": "../../.ssh/id_rsa"})

    assert not result.success
    assert "outside the working directory" in result.error


async def test_a_symlink_out_of_the_root_is_refused(root: FileRoot, tmp_path) -> None:
    """Resolved before the check: the escape is only visible once symlinks are followed."""
    secret = tmp_path / "secret.txt"
    secret.write_text("token", encoding="utf-8")
    (root.root / "innocent.txt").symlink_to(secret)

    result = await FileReadTool(root).execute({"path": "innocent.txt"})

    assert not result.success
    assert "outside the working directory" in result.error


async def test_a_large_file_is_read_in_parts(root: FileRoot) -> None:
    (root.root / "big.txt").write_text("x" * 50, encoding="utf-8")
    tool = FileReadTool(root, max_bytes=10)

    first = await tool.execute({"path": "big.txt"})
    assert first.output["truncated"] is True
    assert first.output["bytes_read"] == 10

    rest = await tool.execute({"path": "big.txt", "offset": 45})
    assert rest.output["truncated"] is False


async def test_listing_reports_paths_relative_to_the_root(root: FileRoot) -> None:
    result = await FileListTool(root).execute({"path": "."})

    paths = {entry["path"] for entry in result.output["entries"]}
    assert paths == {"notes.txt", "invoices"}
    assert all(not path.startswith("/") for path in paths)


async def test_listing_can_filter_and_descend(root: FileRoot) -> None:
    (root.root / "invoices" / "march.pdf").write_bytes(b"%PDF")

    result = await FileListTool(root).execute({"pattern": "*.pdf", "recursive": True})

    assert [entry["path"] for entry in result.output["entries"]] == ["invoices/march.pdf"]


async def test_writing_a_new_file_creates_missing_directories(root: FileRoot) -> None:
    result = await FileWriteTool(root).execute(
        {"path": "reports/2026/summary.md", "content": "done"}
    )

    assert result.success
    assert (root.root / "reports/2026/summary.md").read_text() == "done"
    assert result.output["overwritten"] is False


MARKDOWN = "# AI news\n\n- **GPT-4.5** shipped\n- Grok 3 shipped\n"


async def test_markdown_asked_for_as_txt_is_written_as_markdown(root: FileRoot) -> None:
    """The model names the file, and it names documents `.txt` out of habit.

    The window then draws a document of headings and bullets as raw asterisks,
    and the system opens it in a text editor. The name follows the content, and
    the result says which file was actually written.
    """
    result = await FileWriteTool(root).execute({"path": "news/ai.txt", "content": MARKDOWN})

    assert result.success
    assert result.output["path"] == "news/ai.md"
    assert (root.root / "news/ai.md").read_text() == MARKDOWN
    assert not (root.root / "news/ai.txt").exists()


async def test_a_name_the_model_meant_is_left_alone(root: FileRoot) -> None:
    """Only an undecided name is settled, and only for content that is Markdown."""
    tool = FileWriteTool(root)

    await tool.execute({"path": "rows.csv", "content": MARKDOWN})
    await tool.execute({"path": "plain.txt", "content": "just a sentence, with a dash - here"})

    assert (root.root / "rows.csv").exists()
    assert (root.root / "plain.txt").exists()


def test_what_is_asked_about_is_the_file_that_will_be_written(root: FileRoot) -> None:
    """Settling happens before the gate, so the approval names the real file."""
    tool = FileWriteTool(root)

    settled = tool.settle({"path": "notes.txt", "content": MARKDOWN})

    assert settled["path"] == "notes.md"
    assert tool.preview(settled)["path"] == "notes.md"
    # `notes.txt` exists in the fixture; `notes.md` does not, and the risk
    # follows the file that is actually at stake.
    assert tool.assess(settled).risk_level is RiskLevel.LOW


async def test_writing_a_new_file_is_low_risk_and_overwriting_is_not(root: FileRoot) -> None:
    """The same tool, two different actions. Only one of them needs a person."""
    tool = FileWriteTool(root)

    assert tool.assess({"path": "fresh.md"}).risk_level is RiskLevel.LOW
    assert tool.assess({"path": "notes.txt"}).risk_level is RiskLevel.HIGH


def test_an_overwrite_preview_contains_a_bounded_diff(root: FileRoot) -> None:
    preview = FileWriteTool(root).preview(
        {"path": "notes.txt", "content": "hello, safely updated\n"}
    )

    assert preview["kind"] == "file_diff"
    assert preview["overwrites"] is True
    assert "-hello" in preview["diff"]
    assert "+hello, safely updated" in preview["diff"]


async def test_moving_into_a_directory_keeps_the_file_name(root: FileRoot) -> None:
    result = await FileMoveTool(root).execute(
        {"source": "notes.txt", "destination": "invoices"}
    )

    assert result.output["destination"] == "invoices/notes.txt"
    assert (root.root / "invoices" / "notes.txt").exists()


async def test_moving_onto_an_existing_file_needs_a_person(root: FileRoot) -> None:
    (root.root / "invoices" / "notes.txt").write_text("older", encoding="utf-8")
    tool = FileMoveTool(root)

    assessment = tool.assess({"source": "notes.txt", "destination": "invoices/notes.txt"})

    assert assessment.risk_level is RiskLevel.HIGH


async def test_sorting_a_folder_does_not_ask_a_question_per_file(root: FileRoot) -> None:
    # A move to a free name is undone by another move, so it is not gated.
    assessment = FileMoveTool(root).assess(
        {"source": "notes.txt", "destination": "invoices/renamed.txt"}
    )

    assert assessment.risk_level is RiskLevel.LOW


async def test_moving_something_that_is_not_there_is_reported_not_raised(
    root: FileRoot,
) -> None:
    result = await FileMoveTool(root).execute({"source": "ghost.txt", "destination": "x.txt"})

    assert not result.success
    assert "does not exist" in result.error
