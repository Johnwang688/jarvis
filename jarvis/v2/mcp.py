"""Small stdlib MCP stdio adapter for the existing Jarvis tool registry.

Uses newline-delimited JSON-RPC, never the daemon's conversation endpoints.
WP7 has no tool approval route; _approver is the single WP5 integration hook.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import queue
import sys
import threading

from jarvis import config, tools
from jarvis.tools.secrets import scrub

PROTOCOL_VERSION = "2025-06-18"
PROTOCOL_VERSIONS = frozenset({PROTOCOL_VERSION, "2024-11-05"})
MCP_TOOL_TIMEOUT = float(os.environ.get("MCP_TOOL_TIMEOUT", "120"))
if not math.isfinite(MCP_TOOL_TIMEOUT) or MCP_TOOL_TIMEOUT <= 0:
    raise ValueError("MCP_TOOL_TIMEOUT must be a positive finite number of seconds")

MCP_TOOLS = frozenset({
    "memory_list", "memory_read", "memory_search", "memory_write", "memory_delete",
    "session_list", "session_read", "session_search", "session_summary",
    "get_datetime", "gmail_search", "gmail_read", "gmail_send", "discord_dm_owner",
    "spotify_play", "spotify_status", "spotify_search", "spotify_pause",
    "cad_status", "cad_find_part",
})
_missing = MCP_TOOLS - tools.REGISTRY.keys()
if _missing:
    raise RuntimeError("MCP_TOOLS names tools that are not registered: " + ", ".join(sorted(_missing)))

_APPROVAL_REASON = "approval routing for jarvis-mcp lands in WP5"


def _approver(tool: tools.Tool, arguments: dict) -> bool:
    """WP5 hook: deny until approval routing for jarvis-mcp lands in WP5.

    Intentionally not human-backed: v1 allowlists must not bypass this denial.
    Replace this function with daemon approval routing when WP5 supplies it.
    """
    return False


def available_tools() -> list[str]:
    return [name for name in sorted(MCP_TOOLS)
            if (group := tools.group_of(name)) is None or group.is_available()]


def list_tools() -> dict:
    entries = []
    for name in available_tools():
        spec = tools.REGISTRY[name].spec()["function"]
        entries.append({"name": spec["name"], "description": spec["description"],
                        "inputSchema": spec["parameters"]})
    return {"tools": entries}


def _result(text: str, *, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def call_tool(name: str, arguments: dict) -> dict:
    completed: queue.Queue = queue.Queue(maxsize=1)

    def run():
        try:
            if name not in available_tools():
                completed.put(_result("Error: tool is not exposed or is unavailable.", error=True))
                return
            declined = False

            def approve(entry, args):
                nonlocal declined
                allowed = _approver(entry, args)
                declined = not allowed
                return allowed

            result = tools.dispatch(name, json.dumps(arguments), approve=approve)
            if declined:
                result.text = f"Refused: {_APPROVAL_REASON}."
            payload = _result(result.text, error=declined or result.text.startswith(
                ("Error:", "Refused:", "The user declined")))
            if result.image_b64 is not None:
                payload["content"].append({"type": "image", "data": result.image_b64,
                                           "mimeType": result.mime})
            completed.put(payload)
        except Exception as exc:
            completed.put(_result(scrub(f"Error: {type(exc).__name__}: {exc}"), error=True))

    # ThreadPoolExecutor waits for timed-out workers at interpreter exit.
    # Daemon threads keep both subsequent requests and EOF shutdown bounded.
    threading.Thread(target=run, name=f"mcp-{name}", daemon=True).start()
    try:
        return completed.get(timeout=MCP_TOOL_TIMEOUT)
    except queue.Empty:
        return _result(f"Error: tool timed out after {MCP_TOOL_TIMEOUT:g}s; "
                       "it is still running and may complete. Do not blindly retry writes.", error=True)


class RPCError(Exception):
    def __init__(self, code: int, message: str):
        self.code, self.message = code, message


def _route(method: str, params: dict) -> dict:
    if method == "initialize":
        version = params.get("protocolVersion")
        if not isinstance(version, str):
            raise RPCError(-32602, "protocolVersion must be a string")
        return {"protocolVersion": version if version in PROTOCOL_VERSIONS else PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "jarvis-mcp", "version": "2.0"}}
    if method in {"notifications/initialized", "ping"}:
        return {}
    if method == "tools/list":
        return list_tools()
    if method == "tools/call":
        name, arguments = params.get("name"), params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, dict):
            raise RPCError(-32602, "tools/call requires a name and an arguments object")
        return call_tool(name, arguments)
    raise RPCError(-32601, "Method not found")


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(message) -> dict | None:
    if (not isinstance(message, dict) or message.get("jsonrpc") != "2.0"
            or not isinstance(message.get("method"), str)
            or ("id" in message and (isinstance(message["id"], bool)
                                    or not isinstance(message["id"], (str, int))))):
        return _error(None, -32600, "Invalid Request")
    # JSON-RPC notifications never get responses and must not execute tools.
    if "id" not in message:
        return None
    request_id = message["id"]
    try:
        params = message.get("params", {})
        if not isinstance(params, dict):
            raise RPCError(-32602, "params must be an object")
        return {"jsonrpc": "2.0", "id": request_id, "result": _route(message["method"], params)}
    except RPCError as exc:
        return _error(request_id, exc.code, exc.message)
    except Exception:
        return _error(request_id, -32603, "Internal error")


def _reject_constant(value):
    raise ValueError(f"Not a JSON constant: {value}")


def serve(source, sink) -> None:
    for line in source:
        try:
            message = json.loads(line, parse_constant=_reject_constant)
        except (ValueError, UnicodeError):
            response = _error(None, -32700, "Parse error")
        else:
            response = handle(message)
        if response is not None:
            sink.write(json.dumps(response, ensure_ascii=True, allow_nan=False) + "\n")
            sink.flush()


def main() -> int:
    # Retain the original pipe solely for protocol writes. Redirect fd 1 as
    # well as Python stdout so even native/subprocess tool chatter is stderr.
    sys.stdout.flush()
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    try:
        with protocol:
            serve(sys.stdin.buffer, protocol)
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    return 0


def print_config() -> int:
    # absolute(), not resolve(): resolving the venv's symlink loses its deps.
    command = str(Path(sys.executable).absolute())
    args = ["-m", "jarvis", "mcp"]
    # An editable install may point at another worktree; no cwd assumption.
    env = {"PYTHONPATH": str(config.REPO_ROOT.resolve())}
    print("Claude Code (--mcp-config):")
    print(json.dumps({"mcpServers": {"jarvis": {"command": command, "args": args, "env": env}}}, indent=2))
    print("\nCodex (config.toml):")
    print("[mcp_servers.jarvis]")
    print(f"command = {json.dumps(command)}")
    print(f"args = {json.dumps(args)}")
    print("[mcp_servers.jarvis.env]")
    print(f"PYTHONPATH = {json.dumps(env['PYTHONPATH'])}")
    return 0
