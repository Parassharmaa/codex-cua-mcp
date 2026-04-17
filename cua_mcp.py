#!/usr/bin/env python3
"""Expose OpenAI Codex's Computer Use (CUA) tools as a standalone MCP server.

Codex bundles Computer Use tools (list_apps, click, type_text, get_app_state,
etc.) for its own internal use. This broker spawns `codex app-server` as a
subprocess, discovers the CUA tools over Codex's internal JSON-RPC protocol,
and re-exposes them over the standard MCP newline-delimited stdio transport
so any MCP client (Claude Code, etc.) can drive them.

Python 3.9+ stdlib only — no external dependencies.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_CODEX_BIN = Path("/Applications/Codex.app/Contents/Resources/codex")
DEFAULT_MODEL = "gpt-5.4"
DEFAULT_SANDBOX = "danger-full-access"
DEFAULT_APPROVAL = "never"
SUPPORTED_MCP_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
CUA_TOOL_SENTINELS = {"list_apps", "get_app_state", "click"}
SERVER_INFO = {"name": "codex-cua-mcp", "version": "0.1.0"}

INIT_TIMEOUT = 15.0
LIST_TIMEOUT = 10.0
THREAD_START_TIMEOUT = 15.0
TOOL_CALL_TIMEOUT = 120.0


class ProtocolError(RuntimeError):
    def __init__(self, message: str, code: int = -32000) -> None:
        super().__init__(message)
        self.code = code


class InvalidParamsError(ProtocolError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code=-32602)


def resolve_codex_bin() -> Path:
    env = os.environ.get("CODEX_BIN")
    return Path(env).expanduser() if env else DEFAULT_CODEX_BIN


class AppServerBridge:
    """JSON-RPC bridge to a `codex app-server` subprocess."""

    def __init__(self, codex_bin: Path, debug: bool) -> None:
        self.debug = debug
        self.proc = subprocess.Popen(
            [str(codex_bin), "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._stdout_queue: queue.Queue = queue.Queue()
        self._stderr_lines: deque = deque(maxlen=200)
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        self._next_id = 1

    def _pump_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._stdout_queue.put(line)
        self._stdout_queue.put(None)

    def _pump_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                self._stderr_lines.append(text)

    def _log(self, message: str) -> None:
        if self.debug:
            print(f"[bridge] {message}", file=sys.stderr)

    @property
    def stderr_tail(self) -> List[str]:
        return list(self._stderr_lines)[-20:]

    def send(self, payload: Dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self._log(f"send {payload}")
        self.proc.stdin.write((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def receive(self, timeout: float) -> Dict[str, Any]:
        try:
            line = self._stdout_queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError(
                f"No response from codex app-server within {timeout}s. stderr tail: {self.stderr_tail}"
            ) from exc
        if line is None:
            raise ProtocolError(
                f"codex app-server exited unexpectedly. stderr tail: {self.stderr_tail}"
            )
        message = json.loads(line.decode("utf-8"))
        self._log(f"recv {message}")
        return message

    def request(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = INIT_TIMEOUT) -> Dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        payload: Dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        self.send(payload)

        deadline = time.monotonic() + timeout
        while True:
            remaining = max(0.01, deadline - time.monotonic())
            message = self.receive(remaining)
            if message.get("id") == request_id:
                return message

    def initialize(self) -> Dict[str, Any]:
        return self.request(
            "initialize",
            {
                "clientInfo": {"name": SERVER_INFO["name"], "version": SERVER_INFO["version"]},
                "capabilities": {"optOutNotificationMethods": ["thread/started"]},
            },
            timeout=INIT_TIMEOUT,
        )

    def list_mcp_servers(self) -> Dict[str, Any]:
        return self.request("mcpServerStatus/list", {"detail": "toolsAndAuthOnly"}, timeout=LIST_TIMEOUT)

    def start_thread(self, cwd: str, model: str, sandbox: str, approval_policy: str) -> Dict[str, Any]:
        return self.request(
            "thread/start",
            {
                "cwd": cwd,
                "approvalPolicy": approval_policy,
                "sandbox": sandbox,
                "model": model,
                "ephemeral": True,
            },
            timeout=THREAD_START_TIMEOUT,
        )

    def call_mcp_tool(self, server: str, thread_id: str, tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return self.request(
            "mcpServer/tool/call",
            {"server": server, "threadId": thread_id, "tool": tool, "arguments": arguments},
            timeout=TOOL_CALL_TIMEOUT,
        )

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass


def unwrap_result(message: Dict[str, Any]) -> Any:
    if "error" in message:
        raise ProtocolError(json.dumps(message["error"], indent=2))
    if "result" not in message:
        raise ProtocolError(f"Response did not include a result: {json.dumps(message, indent=2)}")
    return message["result"]


def find_cua_server(status_response: Dict[str, Any]) -> Tuple[str, Dict[str, Dict[str, Any]]]:
    for server in status_response["data"]:
        tool_names = set(server["tools"].keys())
        if CUA_TOOL_SENTINELS.issubset(tool_names):
            return server["name"], server["tools"]

    available = [server["name"] for server in status_response["data"]]
    raise ProtocolError(f"Could not find a Computer Use MCP server. Available: {available}")


class CuaBroker:
    """Brokers the CUA tools exposed through `codex app-server`."""

    def __init__(self, codex_bin: Path, cwd: str, model: str, sandbox: str, approval_policy: str, debug: bool) -> None:
        self.codex_bin = codex_bin
        self.cwd = cwd
        self.model = model
        self.sandbox = sandbox
        self.approval_policy = approval_policy
        self.debug = debug
        self.bridge: Optional[AppServerBridge] = None
        self.server_name: Optional[str] = None
        self.thread_id: Optional[str] = None
        self.tool_map: Dict[str, Dict[str, Any]] = {}

    def ensure_connected(self) -> None:
        if self.bridge is not None and self.server_name is not None and self.thread_id is not None:
            return

        bridge = AppServerBridge(self.codex_bin, debug=self.debug)
        try:
            unwrap_result(bridge.initialize())
            statuses = unwrap_result(bridge.list_mcp_servers())
            server_name, tool_map = find_cua_server(statuses)
            thread_result = unwrap_result(
                bridge.start_thread(
                    cwd=self.cwd,
                    model=self.model,
                    sandbox=self.sandbox,
                    approval_policy=self.approval_policy,
                )
            )
        except Exception:
            bridge.close()
            raise

        self.bridge = bridge
        self.server_name = server_name
        self.thread_id = thread_result["thread"]["id"]
        self.tool_map = tool_map

    def close(self) -> None:
        if self.bridge is not None:
            self.bridge.close()
        self.bridge = None
        self.server_name = None
        self.thread_id = None
        self.tool_map = {}

    def list_tool_definitions(self) -> List[Dict[str, Any]]:
        self.ensure_connected()
        return [self.tool_map[name] for name in sorted(self.tool_map.keys())]

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(arguments, dict):
            raise InvalidParamsError("Tool arguments must be a JSON object.")
        self.ensure_connected()
        assert self.bridge is not None and self.server_name is not None and self.thread_id is not None

        if tool_name not in self.tool_map:
            available = sorted(self.tool_map.keys())
            raise InvalidParamsError(f"Unknown tool '{tool_name}'. Available: {available}")

        response = self.bridge.call_mcp_tool(self.server_name, self.thread_id, tool_name, arguments)
        return unwrap_result(response)


class StdioJsonRpcChannel:
    """Newline-delimited JSON-RPC over stdin/stdout (MCP stdio transport)."""

    def __init__(self, debug: bool) -> None:
        self.debug = debug
        self._stdin = sys.stdin.buffer
        self._stdout = sys.stdout.buffer

    def _log(self, message: str) -> None:
        if self.debug:
            print(f"[mcp] {message}", file=sys.stderr)

    def read_message(self) -> Optional[Dict[str, Any]]:
        while True:
            line = self._stdin.readline()
            if line == b"":
                return None
            payload = line.decode("utf-8").strip()
            if payload:
                self._log(f"recv {payload}")
                return json.loads(payload)

    def write_message(self, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        self._stdout.write(body + b"\n")
        self._stdout.flush()
        self._log(f"send {body.decode('utf-8')}")


class McpBrokerServer:
    """Serves MCP over stdio, forwarding tool calls to the CUA broker."""

    def __init__(self, broker: CuaBroker, debug: bool) -> None:
        self.broker = broker
        self.debug = debug
        self.channel = StdioJsonRpcChannel(debug=debug)

    def run(self) -> int:
        try:
            while True:
                message = self.channel.read_message()
                if message is None:
                    return 0
                self.handle_message(message)
        finally:
            self.broker.close()

    def handle_message(self, message: Dict[str, Any]) -> None:
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}

        if method is None:
            if request_id is not None:
                self.send_error(request_id, -32600, "Invalid Request: missing method")
            return

        if request_id is None:
            if self.debug and method not in {"notifications/initialized", "notifications/cancelled"}:
                print(f"[mcp] ignoring notification {method}", file=sys.stderr)
            return

        try:
            result = self.dispatch(method, params)
        except ProtocolError as exc:
            self.send_error(request_id, exc.code, str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"
            data: Dict[str, Any] = {"message": detail}
            if self.broker.bridge is not None:
                data["stderrTail"] = self.broker.bridge.stderr_tail
            self.send_error(request_id, -32603, detail, data)
            return

        self.channel.write_message({"jsonrpc": "2.0", "id": request_id, "result": result})

    def dispatch(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if method == "initialize":
            return self.handle_initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self.broker.list_tool_definitions()}
        if method == "tools/call":
            tool_name = params.get("name")
            if not isinstance(tool_name, str) or not tool_name:
                raise InvalidParamsError("tools/call requires a string 'name'.")
            arguments = params.get("arguments", {})
            return self.broker.call_tool(tool_name, arguments)
        if method == "resources/list":
            return {"resources": []}
        if method == "resources/templates/list":
            return {"resourceTemplates": []}
        if method == "prompts/list":
            return {"prompts": []}
        raise ProtocolError(f"Method not found: {method}")

    def handle_initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        requested = params.get("protocolVersion")
        protocol_version = (
            requested
            if isinstance(requested, str) and requested in SUPPORTED_MCP_PROTOCOL_VERSIONS
            else SUPPORTED_MCP_PROTOCOL_VERSIONS[0]
        )
        return {
            "protocolVersion": protocol_version,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"listChanged": False, "subscribe": False},
                "prompts": {"listChanged": False},
            },
            "serverInfo": SERVER_INFO,
            "instructions": "Broker that forwards OpenAI Codex Computer Use tool calls over MCP.",
        }

    def send_error(self, request_id: Any, code: int, message: str, data: Optional[Dict[str, Any]] = None) -> None:
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        if data is not None:
            payload["error"]["data"] = data
        self.channel.write_message(payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Expose OpenAI Codex's Computer Use (CUA) tools over MCP.",
    )
    parser.add_argument("--debug", action="store_true", help="Log bridge/MCP traffic to stderr.")
    parser.add_argument(
        "--codex-bin",
        type=Path,
        default=None,
        help=f"Path to codex binary (default: $CODEX_BIN or {DEFAULT_CODEX_BIN}).",
    )
    parser.add_argument("--cwd", default=str(Path.home()), help="Working directory for the app-server thread.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--sandbox", default=DEFAULT_SANDBOX)
    parser.add_argument("--approval-policy", default=DEFAULT_APPROVAL)

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("serve", help="Run as a stdio MCP server (default).")
    subparsers.add_parser("tools", help="Print tool schemas and exit.")
    call_parser = subparsers.add_parser("call", help="Call a tool directly (for debugging).")
    call_parser.add_argument("tool_name")
    call_parser.add_argument("--args-json", default="{}")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    command = args.command or "serve"

    codex_bin = args.codex_bin if args.codex_bin is not None else resolve_codex_bin()
    broker = CuaBroker(
        codex_bin=codex_bin,
        cwd=args.cwd,
        model=args.model,
        sandbox=args.sandbox,
        approval_policy=args.approval_policy,
        debug=args.debug,
    )
    atexit.register(broker.close)

    try:
        if command == "serve":
            return McpBrokerServer(broker=broker, debug=args.debug).run()
        if command == "tools":
            print(json.dumps({"tools": broker.list_tool_definitions()}, indent=2))
            return 0
        if command == "call":
            try:
                arguments = json.loads(args.args_json)
            except json.JSONDecodeError as exc:
                print(f"Invalid --args-json: {exc}", file=sys.stderr)
                return 2
            if not isinstance(arguments, dict):
                print("--args-json must decode to a JSON object.", file=sys.stderr)
                return 2
            result = broker.call_tool(args.tool_name, arguments)
            print(json.dumps({"tool": args.tool_name, "result": result}, indent=2))
            return 0
        raise SystemExit(f"Unhandled command: {command}")
    finally:
        broker.close()


if __name__ == "__main__":
    raise SystemExit(main())
