"""Talking to an MCP server over its standard input and output.

Written here rather than taken from an SDK, and the reason is the dependency
rather than the protocol: what the platform needs of MCP is three calls -
initialize, list the tools, call one - over newline-delimited JSON-RPC, and that
is less code than the adapter that would wrap a library providing it. When a
second transport is needed the shape below is what it implements.

Two decisions worth knowing before editing this.

**The subprocess is a resource with an owner.** It is started on demand and
closed by whoever started it; nothing here reaps a process it did not spawn, and
`aclose` is safe to call twice, because the common path is a runtime shutting
down after a run that may or may not have reached this server.

**Everything that goes wrong is an `IntegrationError`.** A server that is not
answering, one that answered with something unreadable, one that never started:
each becomes a typed failure whose `transient` flag the orchestrator already
knows how to read. Parsing a message for a reason to retry is what this avoids.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

import structlog

from domain.errors import (
    IntegrationProtocolError,
    IntegrationTimeoutError,
    IntegrationUnavailableError,
)
from infrastructure.integrations import programs

log = structlog.get_logger(__name__)

#: How long one request may take. A local server answers in milliseconds; this
#: is the point at which "slow" has become "not coming".
DEFAULT_TIMEOUT_SECONDS = 30.0

#: A frame larger than this is refused rather than buffered. An external process
#: must not be able to exhaust this machine's memory by answering at length.
MAX_LINE_BYTES = 8 * 1024 * 1024

#: `${NAME}` in an argument, replaced from the child's environment at start.
#: A server that takes its credential as an argument rather than a variable -
#: a database URL, say - still never has the value written on the record: the
#: record holds the placeholder, and the secret arrives with the environment.
PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True, slots=True)
class ServerCommand:
    """How to start one server. The whole of an stdio integration's config."""

    command: str
    args: tuple[str, ...] = ()
    #: Added to the child's environment. Values come from `SecretResolver` at
    #: the moment of connecting and are never stored on the integration record.
    env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_configuration(cls, configuration: dict[str, Any]) -> ServerCommand:
        command = str(configuration.get("command", "")).strip()
        if not command:
            raise IntegrationProtocolError(
                "an MCP server over stdio needs a 'command' to start"
            )
        raw_args = configuration.get("args", ())
        raw_env = configuration.get("env", {})
        return cls(
            command=command,
            args=tuple(str(arg) for arg in raw_args) if isinstance(raw_args, list) else (),
            # Plain values only - a plugin's fixed settings. A secret is never
            # on the record; it arrives with the environment at connect.
            env=(
                {str(k): str(v) for k, v in raw_env.items()} if isinstance(raw_env, dict) else {}
            ),
        )


class StdioTransport:
    """One MCP server, spoken to over its own stdin and stdout."""

    def __init__(
        self,
        command: ServerCommand,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._command = command
        self._timeout = timeout_seconds
        self._process: asyncio.subprocess.Process | None = None
        self._next_id = 0
        # One request at a time. The protocol allows more, and allowing more
        # here would mean matching replies to requests by id across concurrent
        # readers of one pipe - complexity bought for a local subprocess that
        # answers in milliseconds.
        self._lock = asyncio.Lock()

    async def start(self, env: dict[str, str] | None = None) -> None:
        if self._process is not None and self._process.returncode is None:
            return
        found = programs.find(self._command.command)
        if found is None:
            raise IntegrationUnavailableError(
                f"could not start '{self._command.command}': it is not installed on "
                "this machine, or not anywhere this program can find it"
            )
        environment = self._environment(env)
        environment["PATH"] = programs.child_path(found, environment.get("PATH"))
        try:
            self._process = await asyncio.create_subprocess_exec(
                str(found),
                *self._arguments(environment),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
                # A tool list is one line of JSON, and a server with sixty
                # tools sends more than asyncio's 64 KiB default: the reader
                # refused it before the size check below could say so.
                limit=MAX_LINE_BYTES + 1,
            )
        except (OSError, ValueError) as error:
            raise IntegrationUnavailableError(
                f"could not start '{self._command.command}': {error}"
            ) from error
        log.info("mcp.server_started", command=self._command.command)

    def _environment(self, extra: dict[str, str] | None) -> dict[str, str]:
        """The child's environment: this process's, plus what it was handed.

        Secrets reach the server this way and no other: they are put in the
        child's environment at the moment of connecting, and never written to
        the integration record, a log line or a prompt.
        """
        return {**os.environ, **self._command.env, **(extra or {})}

    def _arguments(self, environment: dict[str, str]) -> list[str]:
        """The arguments with `${NAME}` filled in from the environment.

        A placeholder with nothing behind it is left as written: the server
        then says what is wrong with its argument, which is more use than an
        empty string it would read as a valid, empty value.
        """
        return [
            PLACEHOLDER.sub(
                lambda match: environment.get(match.group(1), match.group(0)), arg
            )
            for arg in self._command.args
        ]

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """One JSON-RPC call, and the result it produced."""
        async with self._lock:
            process = self._require_running()
            self._next_id += 1
            payload = {
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": method,
                "params": params or {},
            }
            await self._write(process, payload)
            message = await self._reply(process, self._next_id)

        if "error" in message:
            error = message["error"] or {}
            raise IntegrationProtocolError(
                f"{method} was refused by the server: "
                f"{error.get('message', 'no reason given')}"
            )
        return message.get("result")

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """A message with no reply, which is how the handshake is completed."""
        async with self._lock:
            process = self._require_running()
            await self._write(
                process, {"jsonrpc": "2.0", "method": method, "params": params or {}}
            )

    def _require_running(self) -> asyncio.subprocess.Process:
        process = self._process
        if process is None or process.returncode is not None:
            raise IntegrationUnavailableError(
                f"'{self._command.command}' is not running"
            )
        return process

    async def _write(
        self, process: asyncio.subprocess.Process, payload: dict[str, Any]
    ) -> None:
        assert process.stdin is not None
        try:
            process.stdin.write(json.dumps(payload).encode() + b"\n")
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as error:
            raise IntegrationUnavailableError(
                f"'{self._command.command}' closed its input: {error}"
            ) from error

    async def _reply(
        self, process: asyncio.subprocess.Process, request_id: int
    ) -> dict[str, Any]:
        """The answer to this request, past anything the server said meanwhile.

        A server may log or report progress as notifications between a request
        and its reply; taking the first message as the answer made such a
        server's handshake fail on a "result" that was a log line. Still bound
        by the timeout per message, so a chatty server that never answers ends.
        """
        while True:
            message = await self._read(process)
            if message.get("id") == request_id and "method" not in message:
                return message
            log.debug(
                "mcp.message_skipped",
                command=self._command.command,
                method=str(message.get("method", "")),
            )

    async def _read(self, process: asyncio.subprocess.Process) -> dict[str, Any]:
        assert process.stdout is not None
        try:
            line = await asyncio.wait_for(
                process.stdout.readline(), timeout=self._timeout
            )
        except TimeoutError as error:
            raise IntegrationTimeoutError(
                f"'{self._command.command}' did not answer in {self._timeout:g}s"
            ) from error
        except ValueError as error:  # readline's own limit, hit before ours
            raise IntegrationProtocolError(
                f"'{self._command.command}' sent a line too long to read"
            ) from error
        if not line:
            raise IntegrationUnavailableError(
                f"'{self._command.command}' closed its output"
            )
        if len(line) > MAX_LINE_BYTES:
            raise IntegrationProtocolError(
                f"'{self._command.command}' sent {len(line)} bytes in one message"
            )
        try:
            message = json.loads(line)
        except json.JSONDecodeError as error:
            raise IntegrationProtocolError(
                f"'{self._command.command}' sent something that is not JSON: {error}"
            ) from error
        if not isinstance(message, dict):
            raise IntegrationProtocolError(
                f"'{self._command.command}' sent {type(message).__name__}, not an object"
            )
        return message

    async def aclose(self) -> None:
        """Stop the server this transport started, and only that one."""
        process = self._process
        self._process = None
        if process is None or process.returncode is not None:
            return
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except TimeoutError:
            # It was asked politely and did not go. A local subprocess that
            # ignores SIGTERM is not owed a second chance to hold the runtime open.
            process.kill()
            await process.wait()
        except ProcessLookupError:
            pass
        log.info("mcp.server_stopped", command=self._command.command)
