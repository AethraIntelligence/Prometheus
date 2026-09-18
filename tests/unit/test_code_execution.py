"""Running generated code: what it can do, and what it is stopped from doing."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from domain.policies.models import RiskLevel
from infrastructure.tools.code import (
    CodeExecutionTool,
    DockerSandbox,
    UnavailableSandbox,
    sandbox_backend,
)

_REAL_SANDBOX_AVAILABLE = sandbox_backend().available
_SKIP_REAL_SANDBOX = not _REAL_SANDBOX_AVAILABLE and not os.environ.get("CI")


class TestProcessSandbox:
    """Identity wrapper for deterministic resource-limit tests only."""

    name = "test-process"
    available = True
    reason = ""

    def wrap(self, command: Sequence[str], directory: Path) -> list[str]:
        del directory
        return list(command)


def tool(**kwargs) -> CodeExecutionTool:
    return CodeExecutionTool(sandbox=TestProcessSandbox(), **kwargs)


async def test_a_program_runs_and_its_output_comes_back() -> None:
    result = await tool().execute({"code": "print(6 * 7)"})

    assert result.success
    assert result.output["stdout"].strip() == "42"
    assert result.output["exit_code"] == 0


async def test_a_program_that_fails_reports_the_traceback_instead_of_raising() -> None:
    result = await tool().execute({"code": "raise ValueError('nope')"})

    assert not result.success
    assert "ValueError" in result.output["stderr"]


async def test_a_runaway_program_is_stopped() -> None:
    result = await tool(timeout_seconds=2).execute({"code": "while True: pass"})

    assert not result.success
    assert "stopped after" in result.error


async def test_the_environment_is_not_inherited(monkeypatch) -> None:
    """A key on this machine must not be readable by code the model wrote."""
    monkeypatch.setenv("PROMETHEUS_LLM_API_KEY", "sk-do-not-leak")

    result = await tool().execute(
        {"code": "import os; print(list(os.environ.keys()))"}
    )

    assert "PROMETHEUS_LLM_API_KEY" not in result.output["stdout"]


async def test_output_is_truncated_rather_than_flooding_the_context() -> None:
    result = await tool().execute({"code": "print('x' * 100000)"})

    assert result.success
    assert "truncated" in result.output["stdout"]
    assert len(result.output["stdout"]) < 25_000


async def test_each_run_gets_an_empty_directory_of_its_own() -> None:
    runner = tool()

    first = await runner.execute({"code": "open('left-behind.txt', 'w').write('x')"})
    second = await runner.execute({"code": "import os; print(os.listdir('.'))"})

    assert first.success
    assert "left-behind.txt" not in second.output["stdout"]


def test_running_code_always_needs_a_person() -> None:
    """A sandbox limits reach; it does not make an arbitrary program reversible."""
    spec = tool().spec

    assert spec.reversible is False
    assert spec.risk_level is RiskLevel.HIGH


async def test_a_call_with_no_code_is_reported_to_the_model() -> None:
    result = await tool().execute({})

    assert not result.success
    assert "code" in result.error


async def test_an_unavailable_sandbox_never_falls_back_to_a_process() -> None:
    runner = CodeExecutionTool(
        sandbox=UnavailableSandbox(reason="Install the configured sandbox first.")
    )

    result = await runner.execute({"code": "print('must not run')"})

    assert not result.success
    assert result.error == "Install the configured sandbox first."
    assert "Unavailable" in runner.spec.description


def test_docker_receives_the_same_locked_down_contract_on_every_host(tmp_path: Path) -> None:
    sandbox = DockerSandbox(
        memory_mb=256,
        disk_mb=32,
        executable="docker",
        check_readiness=False,
    )
    command = sandbox.wrap(["ignored-python"], tmp_path)

    rendered = " ".join(command)
    assert "--network none" in rendered
    assert "--read-only" in command
    assert "--cap-drop ALL" in rendered
    assert "--security-opt no-new-privileges" in rendered
    assert "--memory 256m" in rendered
    assert "--memory-swap 256m" in rendered
    assert "--cpus 1" in rendered
    assert "--pids-limit 32" in rendered
    assert "--user 65534:65534" in rendered
    assert "--pull never" in rendered
    assert command.count("--mount") == 1
    # Fields only, and no bare `rw`: this assertion used to carry the same
    # invalid argument the command did, so the shape was tested against itself
    # and `docker run` refused every container with exit 125. What the grammar
    # is remains Docker's to say - the two tests below are the ones that ask it.
    assert f"type=bind,src={tmp_path.resolve()},dst=/workspace" in command
    assert not any("," in part and part.endswith(",rw") for part in command)
    assert "--env" not in command
    assert "--tmpfs" in command
    assert rendered.endswith("python -I -B /workspace/main.py")


@pytest.mark.skipif(shutil.which("docker") is None, reason="No docker command on this machine")
def test_docker_accepts_every_flag_of_that_contract(tmp_path: Path) -> None:
    """Ask Docker whether the command is a command, which no assertion can.

    The contract above is a list of strings this repository writes and this
    repository checks, so a flag that Docker does not accept passes it happily -
    `--mount ...,rw` did, and every container refused to start with exit 125 on
    the only machines that ran one. The CLI parses its arguments before it talks
    to the daemon, so this needs the binary and not an engine: 125 is "that is
    not a command", anything else means the grammar was accepted and the rest is
    this machine's business.
    """
    sandbox = DockerSandbox(memory_mb=64, disk_mb=8, executable="docker", check_readiness=False)

    answer = subprocess.run(
        sandbox.wrap(["ignored"], tmp_path), capture_output=True, text=True, timeout=60
    )

    assert answer.returncode != 125, answer.stderr


async def test_file_effects_are_reported_before_the_scratch_directory_is_removed() -> None:
    result = await tool().execute(
        {"code": "open('answer.txt', 'w').write('forty-two')"}
    )

    assert result.success
    assert result.output["effects"] == [{"path": "answer.txt", "bytes": 9}]
    assert result.output["network"] == "denied"


async def test_disk_usage_is_bounded() -> None:
    result = await tool(disk_mb=1).execute(
        {"code": "open('too-large.bin', 'wb').write(b'x' * 2_000_000)"}
    )

    assert not result.success


async def test_memory_usage_is_bounded() -> None:
    result = await tool(memory_mb=64).execute(
        {
            "code": (
                "import time\n"
                "data = bytearray(512 * 1024 * 1024)\n"
                "print(len(data), flush=True)\n"
                "time.sleep(1)\n"
            )
        }
    )

    assert not result.success
    assert "memory limit" in result.error


async def test_a_program_stopped_inside_its_allocation_still_says_which_limit() -> None:
    """The half of the memory cap that no watcher can see.

    Where the address-space rlimit is enforced the child never grows past the
    cap: the allocation raises and it exits, faster than anything polling its
    resident size. The refusal is the same refusal, and used to read as "exited
    with 1" on exactly the machines that enforce the limit properly.
    """
    result = await tool(memory_mb=64).execute({"code": "raise MemoryError"})

    assert not result.success
    assert "memory limit" in result.error
    assert result.output["exit_code"] != 0


@pytest.mark.skipif(_SKIP_REAL_SANDBOX, reason="Docker sandbox unavailable locally")
async def test_the_os_sandbox_cannot_read_a_file_outside_its_scratch_directory(
    tmp_path: Path,
) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("do-not-leak", encoding="utf-8")
    source = (
        "try:\n"
        f"    print(open({str(secret)!r}).read())\n"
        "except OSError:\n"
        "    print('blocked')\n"
    )

    result = await CodeExecutionTool().execute({"code": source})

    assert result.success
    assert result.output["stdout"].strip() == "blocked"
    assert "do-not-leak" not in result.output["stdout"]


@pytest.mark.skipif(_SKIP_REAL_SANDBOX, reason="Docker sandbox unavailable locally")
async def test_the_os_sandbox_cannot_reach_even_a_local_network_listener() -> None:
    server = await asyncio.start_server(lambda _reader, writer: writer.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    source = (
        "import socket\n"
        "try:\n"
        f"    socket.create_connection(('127.0.0.1', {port}), timeout=1)\n"
        "    print('connected')\n"
        "except OSError:\n"
        "    print('blocked')\n"
    )
    try:
        result = await CodeExecutionTool().execute({"code": source})
    finally:
        server.close()
        await server.wait_closed()

    assert result.success
    assert result.output["stdout"].strip() == "blocked"
