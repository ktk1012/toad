import asyncio
import weakref

from contextlib import suppress
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any, cast, NamedTuple
from copy import deepcopy

import rich.repr

from textual.content import Content
from textual.message import Message
from textual.message_pump import MessagePump


from toad import jsonrpc
import toad
from toad.agent_schema import Agent as AgentData
from toad.agent import (
    AgentBase,
    AgentReady,
    AgentFail,
    AgentReconnecting,
    AgentReconnected,
)
from toad.acp import protocol
from toad.acp import api
from toad.acp.api import API
from toad.acp import messages
from toad.acp.prompt import build as build_prompt
from toad.acp.transport import (
    EnvVarError,
    StdioTransport,
    Transport,
    WebSocketTransport,
)
from toad.db import DB
from toad import paths
from toad import constants
from toad.answer import Answer

PROTOCOL_VERSION = 1


class Mode(NamedTuple):
    """An agent mode."""

    id: str
    name: str
    description: str | None


def generate_datetime_filename(
    prefix: str, suffix: str, datetime_format: str | None = None
) -> str:
    """Generate a filename which includes the current date and time.

    Useful for ensuring a degree of uniqueness when saving files.

    Args:
        prefix: Prefix to attach to the start of the filename, before the timestamp string.
        suffix: Suffix to attach to the end of the filename, after the timestamp string.
            This should include the file extension.
        datetime_format: The format of the datetime to include in the filename.
            If None, the ISO format will be used.
    """
    if datetime_format is None:
        dt = datetime.now().isoformat()
    else:
        dt = datetime.now().strftime(datetime_format)

    file_name_stem = f"{prefix} {dt}"
    for reserved in ' <>:"/\\|?*.':
        file_name_stem = file_name_stem.replace(reserved, "_")
    return file_name_stem + suffix


@rich.repr.auto
class Agent(AgentBase):
    """An agent that speaks the APC (https://agentclientprotocol.com/overview/introduction) protocol."""

    _RECONNECT_MAX_ATTEMPTS: int = 3
    """Maximum WebSocket reconnect attempts before giving up.

    TODO(remote-agent): expose this (and `_RECONNECT_BACKOFF`) as an optional
    field on the agent TOML schema so operators can tune retries without
    patching source.
    """
    _RECONNECT_BACKOFF: tuple[float, ...] = (1.0, 2.0, 4.0)
    """Per-attempt delay (seconds). The last value is reused if attempts exceed the tuple."""

    # --- Terminal env var blocklist ----------------------------------------
    # These names / patterns are silently stripped from any `env` an agent
    # supplies to terminal/create. The block applies to all transports as
    # defence-in-depth: a buggy local agent shouldn't smuggle `LD_PRELOAD`
    # into a shell any more than a hostile remote one should.
    _TERMINAL_ENV_BLOCKED_EXACT: frozenset[str] = frozenset(
        {
            # Executable search path (hijacks every subsequent command)
            "PATH",
            # Shell startup files auto-sourced by sh / bash
            "BASH_ENV",
            "ENV",
            # Dynamic linker manipulation (Linux / macOS)
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "LD_AUDIT",
            "LD_DEBUG",
            "DYLD_INSERT_LIBRARIES",
            "DYLD_LIBRARY_PATH",
            "DYLD_FALLBACK_LIBRARY_PATH",
            "DYLD_FORCE_FLAT_NAMESPACE",
            # Language runtime hijacks
            "PYTHONPATH",
            "PYTHONSTARTUP",
            "PYTHONHOME",
            "NODE_OPTIONS",
            "NODE_PATH",
            "RUBYOPT",
            "RUBYLIB",
            "PERL5LIB",
            "PERL5OPT",
            # Outbound network redirection
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            # CA cert bundle override (enables trusted MITM)
            "SSL_CERT_FILE",
            "REQUESTS_CA_BUNDLE",
            "CURL_CA_BUNDLE",
            "NODE_EXTRA_CA_CERTS",
            # GCP credentials file pointer
            "GOOGLE_APPLICATION_CREDENTIALS",
        }
    )
    _TERMINAL_ENV_BLOCKED_PREFIXES: tuple[str, ...] = (
        "LD_",
        "DYLD_",
        # Cloud credential families
        "AWS_",
        "AZURE_",
        "GCP_",
    )
    _TERMINAL_ENV_BLOCKED_SUFFIXES: tuple[str, ...] = (
        "_SECRET",
        "_TOKEN",
        "_PASSWORD",
        "_PRIVATE_KEY",
        "_API_KEY",
    )

    def __init__(
        self,
        project_root: Path,
        agent: AgentData,
        session_id: str | None,
        session_pk: int | None = None,
    ) -> None:
        """

        Args:
            project_root: Project root path.
            command: Command to launch agent.
        """
        super().__init__(project_root)

        self._agent_data = agent
        self.session_id = session_id

        self.server = jsonrpc.Server()
        self.server.expose_instance(self)

        self._agent_task: asyncio.Task | None = None
        self._task: asyncio.Task | None = None
        self._transport: Transport | None = None
        self._stopping: bool = False
        # Track id-bearing MethodCalls so we can fail their futures on
        # transport loss. WeakSet so completed calls drop out automatically.
        self._pending_calls: weakref.WeakSet[jsonrpc.MethodCall] = weakref.WeakSet()
        self.done_event = asyncio.Event()

        self.agent_capabilities: protocol.AgentCapabilities = {
            "loadSession": False,
            "promptCapabilities": {
                "audio": False,
                "embeddedContent": False,
                "image": False,
            },
        }
        self.auth_methods: list[protocol.AuthMethod] = []
        self.session_pk: int | None = session_pk
        self.tool_calls: dict[str, protocol.ToolCall] = {}
        self._message_target: MessagePump | None = None

        self._terminal_count: int = 0

        log_filename: str = generate_datetime_filename(f"{agent['name']}", ".txt")
        if log_path := os.environ.get("TOAD_LOG"):
            self._log_file_path = Path(log_path).resolve().absolute()
            with suppress(OSError):
                self._log_file_path.unlink(missing_ok=True)
        else:
            self._log_file_path = paths.get_log() / log_filename

    @property
    def transport_kind(self) -> str:
        """The transport carrying ACP for this agent ('stdio' or 'websocket')."""
        return self._agent_data.get("transport", "stdio")

    @property
    def trust_level(self) -> str:
        """Effective trust level: 'trusted' or 'untrusted'.

        An explicit ``trust_level`` entry in the agent TOML wins. Otherwise
        stdio agents default to ``trusted`` (they are launched from a binary
        on the user's machine and share the user's process boundary) and
        websocket agents default to ``untrusted`` (they run elsewhere and
        the connection alone does not imply authorisation).
        """
        explicit = self._agent_data.get("trust_level")
        if explicit in ("trusted", "untrusted"):
            return explicit
        return "trusted" if self.transport_kind == "stdio" else "untrusted"

    @property
    def command(self) -> str | None:
        """The command used to launch the agent, or `None` if there isn't one.

        Only meaningful for the stdio transport; returns `None` for remote
        (websocket) agents.
        """
        if self.transport_kind != "stdio":
            return None
        run_command = self._agent_data.get("run_command")
        if not run_command:
            return None
        return toad.get_os_matrix(run_command)

    @property
    def supports_load_session(self) -> bool:
        """Does the agent support loading sessions?"""
        return self.agent_capabilities.get("loadSession", False)

    def __rich_repr__(self) -> rich.repr.Result:
        yield self.project_root_path
        yield "transport", self.transport_kind
        if self.transport_kind == "websocket":
            yield "agent_endpoint", self._agent_data.get("agent_endpoint")
        else:
            yield "command", self.command

    def log(self, line: str) -> None:
        """Write text to the agent log file.

        Args:
            line: Text to be logged.

        """
        if self._message_target is not None:
            self._message_target.call_later(self._log, line)

    async def _log(self, line: str) -> None:
        """Write text to the agent log file.

        Intended to be called from `log`

        Args:
            line: Text to be logged.
        """

        if self._message_target is None:
            return

        def write_log(log_file_path: Path, line: str):
            """Write log in a thread."""
            try:
                with log_file_path.open("at") as log_file:
                    log_file.write(f"{line.rstrip()}\n")
            except OSError:
                pass

        await asyncio.to_thread(write_log, self._log_file_path, line)

    def get_info(self) -> Content:
        agent_name = self._agent_data["name"]
        return Content(agent_name)

    async def start(self, message_target: MessagePump | None = None) -> None:
        """Start the agent."""
        self._message_target = message_target
        try:
            await asyncio.to_thread(
                self._log_file_path.parent.mkdir, parents=True, exist_ok=True
            )
        except OSError:
            pass
        self._agent_task = asyncio.create_task(self._run_agent())

    def send(self, request: jsonrpc.Request) -> None:
        """Send a request to the agent.

        This is called automatically, if you go through `self.request`.

        Args:
            request: JSONRPC request object.

        """
        assert self._transport is not None, "Transport should be present here"

        self.log(f"[client] {request.body}")
        # Track id-bearing calls so their futures can be failed if the
        # transport drops before the response arrives.
        for call in request._calls:
            if call.id is not None and not call.future.done():
                self._pending_calls.add(call)
        self._transport.write(b"%s\n" % request.body_json)

    def _cancel_pending_calls(self, reason: str) -> None:
        """Fail every outstanding :class:`MethodCall` future with ``reason``.

        Called when the transport drops so any code ``await``-ing a response
        (for example :meth:`acp_session_prompt`) wakes up promptly with an
        :class:`jsonrpc.APIError` instead of hanging forever. After a
        reconnect the agent server is a fresh instance and will not reply
        to the old request IDs, so the in-flight futures must be released.
        """
        pending = list(self._pending_calls)
        self._pending_calls.clear()
        for call in pending:
            if call.future.done():
                continue
            try:
                call.future.set_exception(
                    jsonrpc.APIError(-32000, reason, None)
                )
            except asyncio.InvalidStateError:
                pass

    def request(self) -> jsonrpc.Request:
        """Create a request object."""
        return API.request(self.send)

    def post_message(self, message: Message) -> bool:
        """Post a message to the message target (the Conversation).

        Args:
            message: Message object.

        Returns:
            `True` if the message was posted successfully, or `False` if it wasn't.
        """
        if (message_target := self._message_target) is None:
            return False
        return message_target.post_message(message)

    @staticmethod
    def _format_token_count(count: int) -> str:
        abs_count = abs(count)
        for factor, suffix in ((1_000_000, "m"), (1_000, "k")):
            if abs_count >= factor:
                return f"{count / factor:.1f}{suffix}".replace(".0", "")
        return str(count)

    @staticmethod
    def _format_usage_update_status_line(update: protocol.UsageUpdate) -> str:
        used = update["used"]
        size = update["size"]
        percentage = round(used / size * 100) if size else 0
        status_line = (
            f"Context {Agent._format_token_count(used)} / "
            f"{Agent._format_token_count(size)} ({percentage}%)"
        )
        if cost := update.get("cost"):
            amount = cost["amount"]
            currency = cost["currency"]
            if currency == "USD":
                status_line += f" · ${amount:.2f}"
            else:
                status_line += f" · {amount:.2f} {currency}"
        return status_line

    @jsonrpc.expose("session/update")
    def rpc_session_update(
        self,
        sessionId: str,
        update: protocol.SessionUpdate,
        _meta: dict[str, Any] | None = None,
    ):
        """Agent requests an update.

        https://agentclientprotocol.com/protocol/schema
        """
        status_line: str | None = None
        if _meta and (field_meta := _meta.get("field_meta")) is not None:
            if (
                open_hands_metrics := field_meta.get("openhands.dev/metrics")
            ) is not None:
                status_line = open_hands_metrics.get("status_line")

        match update:
            case {
                "sessionUpdate": "user_message_chunk",
                "content": {"type": type, "text": text},
            }:
                if text:
                    self.post_message(messages.UserMessage(type, text))

            case {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": type, "text": text},
            }:
                if text:
                    self.post_message(messages.Update(type, text))

            case {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": type, "text": text},
            }:
                if text:
                    self.post_message(messages.Thinking(type, text))

            case {
                "sessionUpdate": "tool_call",
                "toolCallId": tool_call_id,
            }:
                self.tool_calls[tool_call_id] = update
                self.post_message(messages.ToolCall(update))

            case {"sessionUpdate": "plan", "entries": entries}:
                self.post_message(messages.Plan(entries))

            case {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tool_call_id,
            }:
                if tool_call_id in self.tool_calls:
                    current_tool_call = self.tool_calls[tool_call_id]
                    for key, value in update.items():
                        if value is not None:
                            current_tool_call[key] = value

                    self.post_message(
                        messages.ToolCallUpdate(deepcopy(current_tool_call), update)
                    )
                else:
                    # The agent can send a tool call update, without previously sending the tool call *rolls eyes*
                    current_tool_call: protocol.ToolCall = {
                        "sessionUpdate": "tool_call",
                        "toolCallId": tool_call_id,
                        "title": "Tool call",
                    }
                    for key, value in update.items():
                        if value is not None:
                            current_tool_call[key] = value

                    self.tool_calls[tool_call_id] = current_tool_call
                    self.post_message(messages.ToolCall(current_tool_call))

            case {
                "sessionUpdate": "available_commands_update",
                "availableCommands": available_commands,
            }:
                self.post_message(messages.AvailableCommandsUpdate(available_commands))

            case {"sessionUpdate": "current_mode_update", "currentModeId": mode_id}:
                self.post_message(messages.ModeUpdate(mode_id))

            case {"sessionUpdate": "usage_update", "used": int(), "size": int()}:
                status_line = self._format_usage_update_status_line(
                    cast(protocol.UsageUpdate, update)
                )

        if status_line is not None:
            self.post_message(messages.UpdateStatusLine(status_line))

    @jsonrpc.expose("session/request_permission")
    async def rpc_request_permission(
        self,
        sessionId: str,
        options: list[protocol.PermissionOption],
        toolCall: protocol.ToolCallUpdatePermissionRequest,
        _meta: dict | None = None,
    ) -> protocol.RequestPermissionResponse:
        """Agent requests permission to make a tool call.

        Args:
            sessionId: The session ID.
            options: A list of permission options (potential replies).
            toolCall: The tool or tools the agent is requesting permission to call.
            _meta: Optional meta information.

        Returns:
            The response to the permission request.
        """
        result_future: asyncio.Future[Answer] = asyncio.Future()
        tool_call_id = toolCall["toolCallId"]

        permission_tool_call = toolCall.copy()
        permission_tool_call.pop("sessionUpdate", None)
        tool_call = cast(protocol.ToolCall, permission_tool_call)
        if tool_call_id in self.tool_calls:
            self.tool_calls[tool_call_id] |= tool_call
        else:
            self.tool_calls[tool_call_id] = deepcopy(tool_call)

        tool_call = deepcopy(self.tool_calls[tool_call_id])

        message = messages.RequestPermission(options, tool_call, result_future)
        self.post_message(message)
        await result_future
        ask_result = result_future.result()

        request_permission_outcome: protocol.OutcomeSelected = {
            "optionId": ask_result.id,
            "outcome": "selected",
        }
        result: protocol.RequestPermissionResponse = {
            "outcome": request_permission_outcome
        }
        return result

    def _resolve_project_path(self, path: str) -> Path:
        """Resolve ``path`` inside the project root, rejecting traversal.

        The agent-supplied ``path`` is joined under :attr:`project_root_path`
        and canonicalised via :meth:`Path.resolve`, which normalises ``..``
        components and follows symlinks. The result must be contained within
        the resolved project root; otherwise a JSON-RPC error is raised so
        the agent cannot read or write files outside the workspace.

        This guard applies uniformly to every transport — including local
        stdio agents — because it is cheap and defence-in-depth: a trusted
        local agent that mishandles a user-supplied relative path still gets
        protected.
        """
        project_root = self.project_root_path.resolve()
        candidate = (self.project_root_path / path).resolve()
        if not candidate.is_relative_to(project_root):
            raise jsonrpc.JSONRPCError(
                f"Path {path!r} is outside the project root",
                code=jsonrpc.ErrorCode.INVALID_PARAMS,
            )
        return candidate

    @jsonrpc.expose("fs/read_text_file")
    def rpc_read_text_file(
        self,
        sessionId: str,
        path: str,
        line: int | None = None,
        limit: int | None = None,
    ) -> dict[str, str]:
        """Read a file in the project.

        https://agentclientprotocol.com/protocol/file-system#reading-files
        """
        read_path = self._resolve_project_path(path)
        try:
            text = read_path.read_text(encoding="utf-8", errors="ignore")
        except IOError:
            text = ""
        if line is not None:
            line = max(0, line - 1)
            if limit is None:
                text = "\n".join(text.splitlines()[line:])
            else:
                text = "\n".join(text.splitlines()[line : line + limit])
        return {"content": text}

    @jsonrpc.expose("fs/write_text_file")
    def rpc_write_text_file(self, sessionId: str, path: str, content: str) -> None:
        """Write a file in the project.

        https://agentclientprotocol.com/protocol/file-system#writing-files
        """
        write_path = self._resolve_project_path(path)
        write_path.write_text(content, encoding="utf-8", errors="ignore")

    def _filter_terminal_env(self, env: dict[str, str]) -> dict[str, str]:
        """Strip high-risk env vars an agent tries to inject into a shell.

        Blocks loader hijacks (``LD_*`` / ``DYLD_*``), interpreter hijacks
        (``PYTHONPATH`` / ``NODE_OPTIONS`` / ...), shell rc files
        (``BASH_ENV``), proxy / CA bundle redirection, and cloud-credential
        families (``AWS_*`` / ``*_TOKEN`` / ``*_SECRET`` / ...). Rejected
        entries are written to the agent log so the block is debuggable.

        Applied uniformly to stdio and websocket transports — a local
        agent should not need these either; if one legitimately does, it
        can pass them through its own launch environment instead of the
        ACP ``env`` field.
        """
        cleaned: dict[str, str] = {}
        for name, value in env.items():
            upper = name.upper()
            if upper in self._TERMINAL_ENV_BLOCKED_EXACT:
                self.log(f"[security] blocked agent env (exact match): {name}")
                continue
            if any(
                upper.startswith(prefix)
                for prefix in self._TERMINAL_ENV_BLOCKED_PREFIXES
            ):
                self.log(f"[security] blocked agent env (prefix): {name}")
                continue
            if any(
                upper.endswith(suffix)
                for suffix in self._TERMINAL_ENV_BLOCKED_SUFFIXES
            ):
                self.log(f"[security] blocked agent env (suffix): {name}")
                continue
            cleaned[name] = value
        return cleaned

    # https://agentclientprotocol.com/protocol/schema#createterminalrequest
    @jsonrpc.expose("terminal/create")
    async def rpc_terminal_create(
        self,
        command: str,
        _meta: dict | None = None,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: list[protocol.EnvVariable] | None = None,
        outputByteLimit: int | None = None,
        sessionId: str | None = None,
    ) -> protocol.CreateTerminalResponse:
        # Assign a terminal id
        self._terminal_count = self._terminal_count + 1
        terminal_id = f"terminal-{self._terminal_count}"

        raw_env = (
            {variable["name"]: variable["value"] for variable in env} if env else {}
        )
        terminal_env = self._filter_terminal_env(raw_env)
        result_future: asyncio.Future[bool] = asyncio.Future()
        self.post_message(
            messages.CreateTerminal(
                terminal_id,
                command=command,
                args=args,
                cwd=cwd,
                env=terminal_env,
                output_byte_limit=outputByteLimit,
                result_future=result_future,
            )
        )
        await result_future
        if not result_future.result():
            raise jsonrpc.JSONRPCError("Failed to create a terminal.")
        return {"terminalId": terminal_id}

    # https://agentclientprotocol.com/protocol/schema#killterminalcommandrequest
    @jsonrpc.expose("terminal/kill")
    def rpc_terminal_kill(
        self, sessionID: str, terminalId: str, _meta: dict | None = None
    ) -> protocol.KillTerminalCommandResponse:
        self.post_message(messages.KillTerminal(terminalId))
        return {}

    # https://agentclientprotocol.com/protocol/schema#terminal%2Foutput
    @jsonrpc.expose("terminal/output")
    async def rpc_terminal_output(
        self, sessionId: str, terminalId: str, _meta: dict | None = None
    ) -> protocol.TerminalOutputResponse:
        from toad.widgets.terminal_tool import ToolState

        result_future: asyncio.Future[ToolState] = asyncio.Future()

        if not self.post_message(messages.GetTerminalState(terminalId, result_future)):
            raise RuntimeError("Unable to get terminal output")

        await result_future
        terminal_state = result_future.result()

        result: protocol.TerminalOutputResponse = {
            "output": terminal_state.output,
            "truncated": terminal_state.truncated,
        }
        if (return_code := terminal_state.return_code) is not None:
            result["exitStatus"] = {"exitCode": return_code}
        return result

    # https://agentclientprotocol.com/protocol/schema#terminal%2Frelease
    @jsonrpc.expose("terminal/release")
    def rpc_terminal_release(
        self, sessionId: str, terminalId: str, _meta: dict | None = None
    ) -> protocol.ReleaseTerminalResponse:
        self.post_message(messages.ReleaseTerminal(terminalId))
        return {}

    # https://agentclientprotocol.com/protocol/schema#terminal%2Fwait-for-exit
    @jsonrpc.expose("terminal/wait_for_exit")
    async def rpc_terminal_wait_for_exit(
        self, sessionId: str, terminalId: str, _meta: dict | None = None
    ) -> protocol.WaitForTerminalExitResponse:
        result_future: asyncio.Future[tuple[int, str | None]] = asyncio.Future()
        if not self.post_message(
            messages.WaitForTerminalExit(terminalId, result_future)
        ):
            raise RuntimeError("Unable to wait for terminal exit; no terminal found")

        await result_future
        return_code, signal = result_future.result()
        return {"exitCode": return_code, "signal": signal}

    async def _open_stdio_transport(self) -> StdioTransport | None:
        """Open a stdio transport; post AgentFail and return None on error."""
        command = self.command
        if command is None:
            self.post_message(
                AgentFail("Failed to start agent; no run command for this OS")
            )
            return None
        env = os.environ.copy()
        env["TOAD_CWD"] = str(Path("./").absolute())
        try:
            return await StdioTransport.connect(
                command,
                cwd=self.project_root_path,
                env=env,
            )
        except Exception as error:
            self.post_message(AgentFail("Failed to start agent", details=str(error)))
            return None

    async def _open_websocket_transport(self) -> WebSocketTransport | None:
        """Open a websocket transport; post AgentFail and return None on error."""
        endpoint = self._agent_data.get("agent_endpoint")
        if not endpoint:
            self.post_message(
                AgentFail(
                    "Failed to start agent",
                    details="websocket transport requires an 'agent_endpoint' field",
                )
            )
            return None
        headers = self._agent_data.get("headers") or {}
        try:
            return await WebSocketTransport.connect(endpoint, headers=headers)
        except EnvVarError as error:
            self.post_message(
                AgentFail(
                    "Failed to start agent",
                    details=str(error),
                )
            )
            return None
        except Exception as error:
            self.post_message(
                AgentFail(
                    "Failed to connect to remote agent",
                    details=str(error),
                )
            )
            return None

    async def _read_loop(self, transport: Transport) -> None:
        """Read newline-delimited JSON-RPC frames from the transport until EOF.

        Dispatches method calls on this Agent's RPC server and routes responses
        back through the :data:`API` singleton.
        """
        tasks: set[asyncio.Task] = set()

        async def call_jsonrpc(request: jsonrpc.JSONObject | jsonrpc.JSONList) -> None:
            try:
                if (result := await self.server.call(request)) is not None:
                    result_json = json.dumps(result).encode("utf-8")
                    transport.write(b"%s\n" % result_json)
            finally:
                if (task := asyncio.current_task()) is not None:
                    tasks.discard(task)

        try:
            while line := await transport.read_line():
                if not line.strip():
                    continue

                try:
                    line_str = line.decode("utf-8")
                except Exception as error:
                    self.log(f"[error] Unable to decode utf-8 from agent: {error}")
                    continue

                self.log(f"[agent] {line_str}")
                try:
                    agent_data: jsonrpc.JSONType = json.loads(line_str)
                except Exception as error:
                    self.log(f"[error] failed to decode JSON from agent: {error}")
                    continue

                if isinstance(agent_data, dict):
                    if "result" in agent_data or "error" in agent_data:
                        API.process_response(agent_data)
                        continue

                elif isinstance(agent_data, list):
                    if not all(isinstance(datum, dict) for datum in agent_data):
                        self.log(f"[error] Agent sent invalid data: {agent_data!r}")
                        continue
                    if all(
                        isinstance(datum, dict)
                        and ("result" in datum or "error" in datum)
                        for datum in agent_data
                    ):
                        API.process_response(agent_data)
                        continue

                if not isinstance(agent_data, dict):
                    self.log("[error] Invalid JSON from agent {agent_data!r}")
                    continue

                assert isinstance(agent_data, dict)
                tasks.add(asyncio.create_task(call_jsonrpc(agent_data)))
                await asyncio.sleep(0)
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    _REINIT_TIMEOUT_SECONDS: float = 30.0
    """Upper bound on how long ``session/load`` and ``initialize`` may take
    after a reconnect. Prevents a silent agent server from stalling the
    reconnect sequence indefinitely."""

    async def _try_reconnect(self, transport: Transport) -> bool:
        """Attempt to reconnect the websocket transport and resume the session.

        Returns:
            True on success (transport is live and session resumed),
            False if all attempts are exhausted or resume is not possible.

        TODO(remote-agent): once auto-reconnect gives up and posts
        ``AgentFail``, there is no UI affordance to retry without tearing
        down the conversation. Expose a public ``Agent.try_reconnect()``
        entry point and wire a "reconnect" action into the conversation
        widget so users can recover without losing scrollback.
        """
        if not isinstance(transport, WebSocketTransport):
            return False
        if self._stopping:
            return False
        if not self.supports_load_session or self.session_id is None:
            # Without session resume we can't transparently reattach.
            return False

        for attempt in range(1, self._RECONNECT_MAX_ATTEMPTS + 1):
            delay = self._RECONNECT_BACKOFF[
                min(attempt - 1, len(self._RECONNECT_BACKOFF) - 1)
            ]
            self.post_message(
                AgentReconnecting(attempt, self._RECONNECT_MAX_ATTEMPTS, delay)
            )
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise

            if self._stopping:
                return False

            try:
                await transport.reconnect()
            except Exception as error:
                self.log(f"[reconnect] attempt {attempt} connect failed: {error}")
                continue

            # The reinit below awaits responses that arrive on the fresh
            # transport, so we need something pumping `read_line()`. Spin up
            # a short-lived reader task; the caller's outer loop takes over
            # once we return.
            pump_task = asyncio.create_task(self._read_loop(transport))
            try:
                try:
                    await asyncio.wait_for(
                        self.acp_initialize(),
                        timeout=self._REINIT_TIMEOUT_SECONDS,
                    )
                    await asyncio.wait_for(
                        self.acp_load_session(),
                        timeout=self._REINIT_TIMEOUT_SECONDS,
                    )
                except Exception as error:
                    self.log(
                        f"[reconnect] attempt {attempt} session resume failed: "
                        f"{error}"
                    )
                    # Drop orphaned reinit MethodCall futures so they cannot
                    # resolve against a stale request id after the next
                    # attempt opens yet another fresh connection.
                    self._cancel_pending_calls(
                        "Reconnect session resume failed"
                    )
                    continue
            finally:
                pump_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await pump_task

            self.post_message(AgentReconnected(attempt))
            return True

        return False

    async def _run_agent(self) -> None:
        """Task to communicate with the agent over the configured transport."""

        transport_kind = self.transport_kind
        if transport_kind == "stdio":
            transport = await self._open_stdio_transport()
        elif transport_kind == "websocket":
            transport = await self._open_websocket_transport()
        else:
            self.post_message(
                AgentFail(
                    "Failed to start agent",
                    details=f"Unknown transport: {transport_kind!r}",
                )
            )
            return

        if transport is None:
            return

        self._transport = transport
        self._task = asyncio.create_task(self.run())

        while True:
            await self._read_loop(transport)

            # Transport just died. Wake up anyone ``await``-ing a response
            # (their request will never be answered on a replacement
            # connection, if there even is one).
            self._cancel_pending_calls("Agent connection reset")

            if self._stopping:
                break

            if not await self._try_reconnect(transport):
                break
            # Reconnected — re-enter the read loop with the live transport.

        exit_code = transport.exit_code
        if exit_code:
            fail_details = await transport.failure_details()
            self.post_message(
                AgentFail(
                    f"Agent returned a failure code: [b]{exit_code}",
                    details=fail_details,
                )
            )
        elif not self._stopping and (fail_details := await transport.failure_details()):
            self.post_message(
                AgentFail(
                    "Agent connection lost",
                    details=fail_details,
                    help="disconnected",
                )
            )

        self._transport = None

    async def try_reconnect(self) -> tuple[bool, str | None]:
        """Attempt to restart the transport after an :class:`AgentFail`.

        Intended for user-initiated recovery (e.g. a ``/toad:reconnect``
        slash command) once the automatic backoff sequence has given up.
        A fresh ``_run_agent`` task is spawned, which will open a new
        transport, run :meth:`acp_initialize`, and call
        :meth:`acp_load_session` if a session id is available.

        Returns:
            ``(True, None)`` if a new agent task was started. The caller
            should watch for :class:`AgentReady` / :class:`AgentFail` to
            learn the outcome.
            ``(False, reason)`` if preconditions weren't met.
        """
        if self._stopping:
            return False, "Agent is shutting down"
        if self._agent_task is not None and not self._agent_task.done():
            return False, "Agent is already connecting"
        # Session resume requires both a session id and a server that
        # understands session/load. Without both, reconnecting would
        # silently drop the existing conversation context; make the
        # user explicitly close and start a new session instead.
        if self.session_id is None:
            return False, "No session to resume; start a new session instead"
        if not self.supports_load_session:
            return False, (
                f"{self._agent_data['name']} does not support resuming sessions"
            )

        self._transport = None
        self._pending_calls.clear()
        self._agent_task = asyncio.create_task(self._run_agent())
        return True, None

    async def stop(self) -> None:
        """Gracefully stop the agent transport."""
        self._stopping = True
        if self.session_pk is not None:
            db = DB()
            await db.session_update_last_used(self.session_pk)

        if self._transport is not None:
            with suppress(Exception):
                await self._transport.close()

        # Release any awaiters still parked on in-flight requests so the
        # conversation can shut down promptly.
        self._cancel_pending_calls("Agent stopped")

    async def run(self) -> None:
        """The main logic of the Agent."""
        if constants.ACP_INITIALIZE:
            try:
                # Boilerplate to initialize comms
                await self.acp_initialize()

                if self.session_id is None:
                    # Create a new session
                    await self.acp_new_session()
                else:
                    # Load existing session
                    if not self.agent_capabilities.get("loadSession", False):
                        self.post_message(
                            AgentFail(
                                "Resume not supported",
                                f"{self._agent_data['name']} does not currently support resuming sessions.",
                                help="no_resume",
                            )
                        )
                        return
                    await self.acp_load_session()
                    if self.session_pk is not None:
                        db = DB()
                        await db.session_update_last_used(self.session_pk)
            except jsonrpc.APIError as error:
                if isinstance(error.data, dict):
                    reason = str(
                        error.data.get("reason") or "Failed to initialize agent"
                    )
                    details = str(
                        error.data.get("details") or error.data.get("error") or ""
                    )
                else:
                    reason = "Failed to initialize agent"
                    details = ""
                self.post_message(AgentFail(reason, details))

        self.post_message(AgentReady())

    async def send_prompt(self, prompt: str) -> str | None:
        """Send a prompt to the agent.

        !!! note
            This method blocks as it may defer to a thread to read resources.

        Args:
            prompt: Prompt text.
        """
        prompt_content_blocks = await asyncio.to_thread(
            build_prompt, self.project_root_path, prompt
        )
        return await self.acp_session_prompt(prompt_content_blocks)

    async def acp_initialize(self):
        """Initialize agent."""
        with self.request():
            initialize_response = api.initialize(
                PROTOCOL_VERSION,
                {
                    "fs": {
                        "readTextFile": True,
                        "writeTextFile": True,
                    },
                    "terminal": True,
                },
                {
                    "name": toad.NAME,
                    "title": toad.TITLE,
                    "version": toad.get_version(),
                },
            )

        response = await initialize_response.wait()
        assert response is not None

        # Store agents capabilities
        if agent_capabilities := response.get("agentCapabilities"):
            self.agent_capabilities = agent_capabilities
        if auth_methods := response.get("authMethods"):
            self.auth_methods = auth_methods

    async def acp_new_session(self) -> None:
        """Create a new session."""
        with self.request():
            session_new_response = api.session_new(
                str(self.project_root_path),
                [],
            )
        response = await session_new_response.wait()
        assert response is not None
        self.session_id = response["sessionId"]

        if self.supports_load_session:
            db = DB()
            session_name = "New Session"
            self.session_pk = await db.session_new(
                session_name,
                self._agent_data["name"],
                self._agent_data["identity"],
                self.session_id,
                protocol="acp",
                meta={
                    "cwd": str(self.project_root_path),
                    "agent_data": self._agent_data,
                },
            )

        if (modes := response.get("modes", None)) is not None:
            current_mode = modes["currentModeId"]
            available_modes = modes["availableModes"]
            modes_update = {
                mode["id"]: Mode(
                    mode["id"], mode["name"], mode.get("description", None)
                )
                for mode in available_modes
            }
            self.post_message(messages.SetModes(current_mode, modes_update))

    async def acp_load_session(self) -> None:
        assert self.session_id is not None, "Session id must be set"
        cwd = str(self.project_root_path)
        if self.session_pk is not None:
            db = DB()
            if (session := await db.session_get(self.session_pk)) is not None:
                if session["meta_json"]:
                    meta = json.loads(session["meta_json"])
                    if session_cwd := meta.get("cwd", None):
                        cwd = session_cwd
                    if agent_data := meta.get("agent_data"):
                        self._agent_data = agent_data

        with self.request():
            session_load_response = api.session_load(cwd, [], self.session_id)
        response = await session_load_response.wait()

        if (modes := response.get("modes", None)) is not None:
            current_mode = modes["currentModeId"]
            available_modes = modes["availableModes"]
            modes_update = {
                mode["id"]: Mode(
                    mode["id"], mode["name"], mode.get("description", None)
                )
                for mode in available_modes
            }
            self.post_message(messages.SetModes(current_mode, modes_update))

    async def acp_session_prompt(
        self, prompt: list[protocol.ContentBlock]
    ) -> str | None:
        """Send the prompt to the agent.

        Returns:
            The stop reason.

        """
        with self.request():
            session_prompt = api.session_prompt(prompt, self.session_id)
        try:
            result = await session_prompt.wait()
        except jsonrpc.APIError as error:
            details = ""
            match error.data:
                case {"details": details}:
                    pass

            self.post_message(
                AgentFail(
                    "Failed to send prompt" or error.message,
                    (
                        str(details)
                        if details
                        else f"{self._agent_data['name']} returned an error"
                    ),
                )
            )
            return None
        except jsonrpc.JSONRPCError as error:
            self.post_message(
                AgentFail(
                    "Failed to send prompt" or error.message,
                    (error.message or f"{self._agent_data['name']} returned an error"),
                )
            )
            return None

        assert result is not None
        return result.get("stopReason")

    async def acp_session_set_mode(self, mode_id: str) -> str | None:
        """Update the current mode with the agent."""
        with self.request():
            response = api.session_set_mode(self.session_id, mode_id)
        try:
            await response.wait()
        except jsonrpc.APIError as error:
            match error.data:
                case {"details": details}:
                    return details if isinstance(details, str) else "Failed to set mode"
            return "Failed to set mode"
        else:
            return None

    async def set_mode(self, mode_id: str) -> str | None:
        return await self.acp_session_set_mode(mode_id)

    async def set_session_name(self, name: str) -> None:
        if self.session_pk is None:
            return
        db = DB()
        await db.session_update_title(self.session_pk, name)

    async def acp_session_cancel(self) -> bool:
        with self.request():
            response = api.session_cancel(self.session_id, {})
        try:
            await response.wait()
        except jsonrpc.APIError:
            # No-op if there is nothing to cancel
            return False
        return True

    async def cancel(self) -> bool:
        return await self.acp_session_cancel()
