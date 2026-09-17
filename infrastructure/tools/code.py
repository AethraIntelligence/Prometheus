"""Generated Python runs in one Docker sandbox on every supported host."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from domain.capabilities.models import Capability
from domain.policies.risk import Effect
from domain.tools.models import ToolResult, ToolSpec
from domain.tools.schema import Param
from infrastructure.observability.logging import get_logger

log = get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_TIMEOUT_SECONDS = 300.0
DEFAULT_MEMORY_MB = 512
DEFAULT_DISK_MB = 64
DEFAULT_MAX_OUTPUT_CHARS = 20_000


class SandboxBackend(Protocol):
    """Turns a Python command into one confined by the container runtime."""

    name: str
    available: bool
    reason: str
    uses_host_limits: bool

    def wrap(self, command: Sequence[str], directory: Path) -> list[str]: ...

    def environment(self, directory: Path) -> dict[str, str]: ...

    def cleanup(self, directory: Path) -> None: ...


@dataclass(frozen=True, slots=True)
class UnavailableSandbox:
    name: str = "unavailable"
    available: bool = False
    uses_host_limits: bool = False
    reason: str = (
        "The Docker code sandbox is unavailable. Start Docker and install the "
        "configured Python sandbox image."
    )

    def wrap(self, command: Sequence[str], directory: Path) -> list[str]:
        del command, directory
        raise RuntimeError(self.reason)

    def environment(self, directory: Path) -> dict[str, str]:
        del directory
        return {}

    def cleanup(self, directory: Path) -> None:
        del directory


@lru_cache(maxsize=4)
def _docker_readiness(executable: str, image: str) -> tuple[bool, str]:
    try:
        daemon = subprocess.run(
            [executable, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"Docker could not be checked: {error}"
    if daemon.returncode:
        return False, "Docker is installed but its engine is not running."
    image_check = subprocess.run(
        [executable, "image", "inspect", image],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if image_check.returncode:
        return False, f"The sandbox image is missing. Run: docker pull {image}"
    return True, ""


class DockerSandbox:
    """One cross-platform container contract with no network or host access."""

    name = "docker"
    uses_host_limits = False

    def __init__(
        self,
        *,
        memory_mb: int,
        disk_mb: int,
        executable: str | None = None,
        image: str = "python:3.12-alpine",
        check_readiness: bool = True,
    ) -> None:
        self._executable = executable or shutil.which("docker") or ""
        self._image = image
        self._memory_mb = memory_mb
        self._disk_mb = disk_mb
        if not self._executable:
            self.available = False
            self.reason = "Docker is not installed."
        elif check_readiness:
            self.available, self.reason = _docker_readiness(self._executable, image)
        else:
            self.available, self.reason = True, ""

    @staticmethod
    def _name(directory: Path) -> str:
        return f"prometheus-code-{directory.name.removeprefix('prometheus-code-')}"

    def wrap(self, command: Sequence[str], directory: Path) -> list[str]:
        del command
        if not self.available:
            raise RuntimeError(self.reason)
        scratch = str(directory.resolve())
        return [
            self._executable,
            "run",
            "--rm",
            "--pull",
            "never",
            "--name",
            self._name(directory),
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "32",
            "--memory",
            f"{self._memory_mb}m",
            "--memory-swap",
            f"{self._memory_mb}m",
            "--cpus",
            "1",
            "--ulimit",
            "nofile=64:64",
            "--ulimit",
            f"fsize={self._disk_mb * 1024 * 1024}:{self._disk_mb * 1024 * 1024}",
            "--user",
            "65534:65534",
            "--workdir",
            "/workspace",
            "--mount",
            f"type=bind,src={scratch},dst=/workspace,rw",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=16m",
            self._image,
            "python",
            "-I",
            "-B",
            "/workspace/main.py",
        ]

    def environment(self, directory: Path) -> dict[str, str]:
        del directory
        keys = (
            "HOME",
            "USERPROFILE",
            "APPDATA",
            "PATH",
            "DOCKER_CONFIG",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
        )
        return {key: os.environ[key] for key in keys if key in os.environ}

    def cleanup(self, directory: Path) -> None:
        subprocess.run(
            [self._executable, "rm", "-f", self._name(directory)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )


class SandboxHalter:
    """Implements `domain.safety.emergency.Halter` for the code sandbox.

    A program running in a container has no step boundary for a cancellation to
    reach, so the stop kills the containers this platform started - found by the
    name prefix every one of them is given, and never any other container on the
    machine. Nothing needs resuming: the next call starts a new container.
    """

    name = "sandbox"

    def __init__(self, executable: str | None = None) -> None:
        self._executable = executable if executable is not None else shutil.which("docker") or ""

    async def halt(self) -> int:
        if not self._executable:
            return 0
        listed = await asyncio.to_thread(
            subprocess.run,
            [self._executable, "ps", "-q", "--filter", "name=^prometheus-code-"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        ids = [line for line in listed.stdout.split() if line]
        if ids:
            await asyncio.to_thread(
                subprocess.run,
                [self._executable, "kill", *ids],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        return len(ids)

    async def resume(self) -> None:
        return None


def sandbox_backend(
    *, memory_mb: int = DEFAULT_MEMORY_MB, disk_mb: int = DEFAULT_DISK_MB
) -> SandboxBackend:
    """The single Docker boundary, or an explicit fail-closed implementation."""
    candidate = DockerSandbox(memory_mb=memory_mb, disk_mb=disk_mb)
    return candidate if candidate.available else UnavailableSandbox(reason=candidate.reason)


def _limits(memory_mb: int, cpu_seconds: int, disk_mb: int):
    """Resource caps applied in the sandboxed child before Python starts."""

    def apply() -> None:  # pragma: no cover - runs in the child process
        import contextlib
        import resource

        memory_bytes = memory_mb * 1024 * 1024
        file_bytes = disk_mb * 1024 * 1024
        caps = (
            (resource.RLIMIT_AS, memory_bytes),
            (resource.RLIMIT_DATA, memory_bytes),
            (resource.RLIMIT_CPU, cpu_seconds),
            (resource.RLIMIT_FSIZE, file_bytes),
            (resource.RLIMIT_NOFILE, 64),
        )
        if hasattr(resource, "RLIMIT_NPROC"):
            caps = (*caps, (resource.RLIMIT_NPROC, 32))
        for kind, value in caps:
            with contextlib.suppress(ValueError, OSError):
                resource.setrlimit(kind, (value, value))
        # Desktop processes already run as the signed-in, unprivileged user.
        # A service accidentally started as root must not pass that identity to
        # generated code; 65534 is the conventional nobody uid/gid on both
        # supported Unix families.
        if os.geteuid() == 0:
            os.setgroups([])
            os.setgid(65534)
            os.setuid(65534)

    return apply


class CodeExecutionTool:
    """Runs short Python programs only when a supported sandbox is ready."""

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        memory_mb: int = DEFAULT_MEMORY_MB,
        disk_mb: int = DEFAULT_DISK_MB,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
        interpreter: str | None = None,
        sandbox: SandboxBackend | None = None,
    ) -> None:
        self._timeout = min(timeout_seconds, MAX_TIMEOUT_SECONDS)
        self._memory_mb = max(32, memory_mb)
        self._disk_mb = max(1, disk_mb)
        self._max_output_chars = max(1_000, max_output_chars)
        self._interpreter = interpreter or sys.executable
        self._sandbox = sandbox or sandbox_backend(memory_mb=memory_mb, disk_mb=disk_mb)
        readiness = (
            f"OS sandbox: {self._sandbox.name}."
            if self._sandbox.available
            else f"Unavailable: {self._sandbox.reason}"
        )
        self._spec = ToolSpec.of(
            "code.run",
            "Run a short Python program in an isolated, network-disabled scratch "
            f"directory. {readiness} Needs the user's confirmation.",
            Param("code", description="The Python source to run. Print what you need to see."),
            Param(
                "timeout_seconds",
                type="integer",
                required=False,
                default=int(DEFAULT_TIMEOUT_SECONDS),
                description="How long it may run before being stopped.",
            ),
            effect=Effect.EXECUTE,
            capabilities=frozenset({Capability.CODE}),
            reversible=False,
        )

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    @property
    def sandbox(self) -> SandboxBackend:
        return self._sandbox

    async def execute(self, input_data: dict[str, object]) -> ToolResult:
        from domain.errors import DomainError

        try:
            arguments = self._spec.parameters.validate(input_data)
        except DomainError as error:
            return ToolResult.failure(str(error))
        if not self._sandbox.available:
            return ToolResult.failure(self._sandbox.reason)

        source = str(arguments["code"])
        timeout = min(float(arguments.get("timeout_seconds", self._timeout)), self._timeout)
        directory = Path(tempfile.mkdtemp(prefix="prometheus-code-"))
        try:
            return await self._run(source, directory, timeout)
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    async def _run(self, source: str, directory: Path, timeout: float) -> ToolResult:
        script = directory / "main.py"
        script.write_text(source, encoding="utf-8")
        environment = {
            "PATH": os.defpath,
            "HOME": str(directory),
            "TMPDIR": str(directory),
            "LANG": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
        }
        if hasattr(self._sandbox, "environment"):
            environment = self._sandbox.environment(directory)
        command = self._sandbox.wrap(
            [self._interpreter, "-I", "-B", str(script)], directory
        )
        if self._sandbox.name == "docker":
            directory.chmod(0o777)
            script.chmod(0o644)
        elif os.name == "posix" and os.geteuid() == 0:
            for path in (directory, *directory.iterdir()):
                os.chown(path, 65534, 65534)
        kwargs = {}
        if os.name == "posix" and getattr(self._sandbox, "uses_host_limits", True):
            kwargs["preexec_fn"] = _limits(
                self._memory_mb, max(1, int(timeout) + 1), self._disk_mb
            )

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=directory,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name == "posix",
                **kwargs,
            )
        except OSError as error:
            return ToolResult.failure(
                f"The {self._sandbox.name} sandbox could not start: {error}"
            )

        stdout_task = asyncio.create_task(
            _read_limited(process.stdout, self._max_output_chars)
        )
        stderr_task = asyncio.create_task(
            _read_limited(process.stderr, self._max_output_chars)
        )
        resource_task = asyncio.create_task(
            _enforce_runtime_limits(
                process,
                directory,
                disk_limit=self._disk_mb * 1024 * 1024,
                memory_limit=self._memory_mb * 1024 * 1024,
            )
        )
        timed_out = False
        exceeded: str | None = None
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except TimeoutError:
            timed_out = True
            _kill_process_group(process)
            await process.wait()
        finally:
            if resource_task.done() and not resource_task.cancelled():
                exceeded = resource_task.result()
            else:
                resource_task.cancel()
            await asyncio.gather(resource_task, return_exceptions=True)
            if hasattr(self._sandbox, "cleanup"):
                await asyncio.to_thread(self._sandbox.cleanup, directory)

        stdout, stdout_clipped = await stdout_task
        stderr, stderr_clipped = await stderr_task
        effects = _file_effects(directory)
        if timed_out:
            log.warning("code.timeout", timeout_seconds=timeout, sandbox=self._sandbox.name)
            return ToolResult.failure(
                f"The program was stopped after {timeout:.0f} seconds without finishing."
            )
        output = {
            "stdout": _decode(stdout, stdout_clipped),
            "stderr": _decode(stderr, stderr_clipped),
            "exit_code": process.returncode,
            "sandbox": self._sandbox.name,
            "network": "denied",
            "subject": "unprivileged",
            "effects": effects,
        }
        if os.name == "posix":
            import signal

            if process.returncode == -signal.SIGXFSZ:
                exceeded = "disk"
        if exceeded:
            return ToolResult(
                success=False,
                output=output,
                error=(
                    f"The program exceeded the {self._disk_mb} MB disk limit."
                    if exceeded == "disk"
                    else f"The program exceeded the {self._memory_mb} MB memory limit."
                ),
            )
        error = None
        if process.returncode != 0:
            error = f"exited with {process.returncode}"
            sandbox_error = output["stderr"].strip()
            if sandbox_error.startswith(("docker:", "Error response from daemon:")):
                error = f"The {self._sandbox.name} sandbox rejected startup: {sandbox_error}"
        return ToolResult(
            success=process.returncode == 0,
            output=output,
            error=error,
        )


async def _read_limited(
    stream: asyncio.StreamReader | None, limit: int
) -> tuple[bytes, bool]:
    if stream is None:
        return b"", False
    kept = bytearray()
    clipped = False
    while chunk := await stream.read(8192):
        remaining = limit - len(kept)
        if remaining > 0:
            kept.extend(chunk[:remaining])
        if len(chunk) > max(remaining, 0):
            clipped = True
    return bytes(kept), clipped


async def _enforce_runtime_limits(
    process: asyncio.subprocess.Process,
    directory: Path,
    *,
    disk_limit: int,
    memory_limit: int,
) -> str | None:
    while process.returncode is None:
        await asyncio.sleep(0.02)
        size = _directory_size(directory)
        if size > disk_limit:
            _kill_process_group(process)
            return "disk"
        resident = await _resident_bytes(process.pid)
        if resident is not None and resident > memory_limit:
            _kill_process_group(process)
            return "memory"
    return None


def _directory_size(directory: Path) -> int:
    """Measure scratch usage while generated code may concurrently mutate it."""
    size = 0
    try:
        paths = tuple(directory.rglob("*"))
    except OSError:
        return size
    for path in paths:
        try:
            if path.is_file():
                size += path.stat().st_size
        except OSError:
            continue
    return size


async def _resident_bytes(pid: int) -> int | None:
    """Current RSS without an optional process-inspection dependency."""
    status = Path(f"/proc/{pid}/status")
    if status.exists():
        try:
            line = next(
                item for item in status.read_text(encoding="utf-8").splitlines()
                if item.startswith("VmRSS:")
            )
            return int(line.split()[1]) * 1024
        except (OSError, StopIteration, ValueError):
            return None
    ps = shutil.which("ps")
    if not ps:
        return None
    try:
        probe = await asyncio.create_subprocess_exec(
            ps,
            "-o",
            "rss=",
            "-p",
            str(pid),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await probe.communicate()
        return int(stdout.strip() or b"0") * 1024
    except (OSError, ValueError):
        return None


def _file_effects(directory: Path) -> list[dict[str, object]]:
    ignored = {"main.py"}
    return [
        {"path": str(path.relative_to(directory)), "bytes": path.stat().st_size}
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.name not in ignored
    ]


def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    if os.name == "posix":
        import signal

        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
    process.kill()


def _decode(raw: bytes, clipped: bool) -> str:
    text = raw.decode("utf-8", errors="replace")
    if clipped:
        return text + "\n... [truncated by the output limit]"
    return text
