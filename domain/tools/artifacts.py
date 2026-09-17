"""Which files a piece of work wrote, read off the tool calls that succeeded.

In the domain since Phase 9, because two readers need it: an interface listing a
turn's files, and a thread's brief indexing every file the thread produced. The
rule is the one `application/interface/artifacts.py` states - by the shape of
what a tool reported, never by the answer's text.
"""

from __future__ import annotations

from collections.abc import Iterable

from domain.tools.telemetry import ToolCallRecord


def written_path(output: dict | None) -> str | None:
    """The file one successful call reports having written, by the same shape rule.

    Also kept on the observation (Phase 11), because a contract that requires
    an artifact is judged off the task record, and the record keeps summaries
    of what tools returned rather than the outputs themselves.
    """
    written = (output or {}).get("path")
    if isinstance(written, str) and "bytes_written" in (output or {}):
        return written
    return None


def produced_files(calls: Iterable[ToolCallRecord]) -> list[str]:
    """Relative paths, in the order they were first written, as they stand now.

    A file moved after it was written is listed where it went: the old path is
    somewhere nobody can open any more.
    """
    found: list[str] = []
    for call in sorted(calls, key=lambda item: item.created_at):
        if not call.success:
            continue
        output = call.output or {}
        written = output.get("path")
        if isinstance(written, str) and "bytes_written" in output:
            if written not in found:
                found.append(written)
            continue
        source, destination = output.get("source"), output.get("destination")
        if isinstance(source, str) and isinstance(destination, str) and source in found:
            found[found.index(source)] = destination
    return found
