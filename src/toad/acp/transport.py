"""Transports that carry newline-delimited JSON-RPC frames for ACP.

Two transports are supported:

- :class:`StdioTransport` launches a subprocess and speaks JSON-RPC over
  its stdin/stdout pipes (the default ACP convention).
- :class:`WebSocketTransport` connects to a remote agent over
  ``ws://`` / ``wss://`` and exchanges the same newline-delimited JSON-RPC
  frames over a WebSocket.

Both expose the same small interface so the ACP agent loop does not need
to know which transport is in use.
"""

from __future__ import annotations

import asyncio
import os
import re
from contextlib import suppress
from pathlib import Path
from typing import Any


_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class EnvVarError(RuntimeError):
    """Raised when a ${VAR} reference cannot be resolved from os.environ."""


def expand_env(value: str) -> str:
    """Expand ``${VAR}`` references in ``value`` using :data:`os.environ`.

    Raises:
        EnvVarError: If a referenced variable is not set.
    """

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        try:
            return os.environ[name]
        except KeyError as error:
            raise EnvVarError(
                f"Environment variable ${{{name}}} is not set"
            ) from error

    return _ENV_VAR_RE.sub(repl, value)


class Transport:
    """Abstract newline-delimited JSON-RPC transport."""

    async def read_line(self) -> bytes:
        """Read one line (including trailing newline). Empty bytes mean EOF."""
        raise NotImplementedError

    def write(self, data: bytes) -> None:
        """Write raw bytes; caller is responsible for the trailing newline."""
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    async def reconnect(self) -> None:
        """Re-establish the underlying connection in place.

        Subclasses that support reconnection should reset internal state so
        that subsequent :meth:`read_line` / :meth:`write` calls use the fresh
        connection. Raises on failure; the caller may retry.
        """
        raise NotImplementedError("transport does not support reconnect")

    async def failure_details(self) -> str:
        """Human-readable info for error reporting."""
        return ""

    @property
    def exit_code(self) -> int | None:
        """Non-None, non-zero indicates the transport ended abnormally."""
        return None


class StdioTransport(Transport):
    """Transport backed by a child process's stdin/stdout."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process

    @classmethod
    async def connect(
        cls,
        command: str,
        cwd: Path,
        env: dict[str, str],
        limit: int = 10 * 1024 * 1024,
    ) -> "StdioTransport":
        PIPE = asyncio.subprocess.PIPE
        process = await asyncio.create_subprocess_shell(
            command,
            stdin=PIPE,
            stdout=PIPE,
            stderr=PIPE,
            env=env,
            cwd=str(cwd),
            limit=limit,
        )
        return cls(process)

    @property
    def process(self) -> asyncio.subprocess.Process:
        return self._process

    async def read_line(self) -> bytes:
        if self._process.stdout is None:
            return b""
        return await self._process.stdout.readline()

    def write(self, data: bytes) -> None:
        if self._process.stdin is not None:
            self._process.stdin.write(data)

    async def close(self) -> None:
        with suppress(OSError):
            self._process.terminate()

    @property
    def exit_code(self) -> int | None:
        return self._process.returncode

    async def failure_details(self) -> str:
        if self._process.stderr is None:
            return ""
        try:
            data = await self._process.stderr.read()
        except Exception:
            return ""
        return data.decode("utf-8", "replace")


class WebSocketTransport(Transport):
    """Transport backed by a WebSocket connection to a remote ACP agent.

    Each WebSocket message carries one or more newline-delimited JSON-RPC
    frames. Outgoing writes are serialized through an internal queue so the
    synchronous ``write`` API matches the stdio transport.

    Connection parameters (URL, headers, max frame size) are retained so
    :meth:`reconnect` can re-establish the link after a transient drop.
    """

    def __init__(
        self,
        url: str,
        headers: dict[str, str],
        max_size: int,
    ) -> None:
        self._url = url
        self._headers = headers
        self._max_size = max_size
        self._ws: Any = None
        self._send_queue: asyncio.Queue[bytes | None] | None = None
        self._send_task: asyncio.Task | None = None
        self._read_buffer = bytearray()
        self._closed = False
        self._last_error: str = ""

    @classmethod
    async def connect(
        cls,
        url: str,
        headers: dict[str, str] | None = None,
        max_size: int = 50 * 1024 * 1024,
    ) -> "WebSocketTransport":
        transport = cls(url, dict(headers or {}), max_size)
        await transport._open()
        return transport

    async def _open(self) -> None:
        """Establish the WebSocket connection and start the send loop."""
        import websockets

        expanded_url = expand_env(self._url)
        expanded_headers = {
            key: expand_env(value) for key, value in self._headers.items()
        }

        connect_kwargs: dict[str, Any] = {"max_size": self._max_size}
        if expanded_headers:
            connect_kwargs["additional_headers"] = expanded_headers

        self._ws = await websockets.connect(expanded_url, **connect_kwargs)
        self._send_queue = asyncio.Queue()
        self._read_buffer = bytearray()
        self._last_error = ""
        self._closed = False
        self._send_task = asyncio.create_task(self._send_loop(self._send_queue))

    async def _close_current(self) -> None:
        """Tear down the current connection without touching `_closed`."""
        if self._send_task is not None and self._send_queue is not None:
            self._send_queue.put_nowait(None)
            with suppress(asyncio.CancelledError, Exception):
                await self._send_task
        self._send_task = None
        self._send_queue = None
        if self._ws is not None:
            with suppress(Exception):
                await self._ws.close()
        self._ws = None

    async def _send_loop(self, queue: "asyncio.Queue[bytes | None]") -> None:
        ws = self._ws
        while True:
            data = await queue.get()
            if data is None:
                return
            if ws is None:
                continue
            try:
                await ws.send(data.decode("utf-8"))
            except Exception as error:
                if not self._last_error:
                    self._last_error = str(error)
                return

    async def read_line(self) -> bytes:
        if self._ws is None:
            return b""
        while b"\n" not in self._read_buffer:
            try:
                msg = await self._ws.recv()
            except Exception as error:
                if not self._last_error:
                    self._last_error = str(error)
                if self._read_buffer:
                    line = bytes(self._read_buffer) + b"\n"
                    self._read_buffer.clear()
                    return line
                return b""
            if isinstance(msg, str):
                msg = msg.encode("utf-8")
            self._read_buffer.extend(msg)
        idx = self._read_buffer.index(b"\n")
        line = bytes(self._read_buffer[: idx + 1])
        del self._read_buffer[: idx + 1]
        return line

    def write(self, data: bytes) -> None:
        if self._closed or self._send_queue is None:
            return
        self._send_queue.put_nowait(data)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._close_current()

    async def reconnect(self) -> None:
        """Close the current WebSocket and open a new one with the same params."""
        if self._closed:
            raise RuntimeError("Transport is closed; cannot reconnect")
        await self._close_current()
        await self._open()

    async def failure_details(self) -> str:
        return self._last_error
