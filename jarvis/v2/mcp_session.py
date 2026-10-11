"""jarvis-mcp for one chat session: a stdlib-only relay to the daemon's bridge.

A Claude or Codex chat's CLI starts this as its `jarvis` MCP server (`python
-m jarvis.v2.mcp_session`, plan `docs/plans/2026-10-10-terminal-read-mcp-
plan.md` §1.5). It speaks newline-delimited JSON-RPC on stdio, and for every
tool request asks the daemon's API listener over loopback HTTP with the
session's caller token. **It never runs a tool**: it imports nothing from
`jarvis.tools` or `jarvis.v2.tools`, holds no registry, and contains no
dispatch — what the daemon answers is all it can say.

- The token arrives in `JARVIS_MCP_TOKEN`, and is removed from `os.environ`
  at start, so nothing this process might start would inherit it. The
  daemon's port is `JARVIS_DAEMON_PORT`.
- It never writes the token, or any tool result, to stderr — Claude Code and
  Codex keep an MCP server's stderr in their own logs. stderr is pointed at
  nothing.
- `initialize` declares tools without `listChanged`: the list is read once,
  when the CLI connects (D-4).
- `tools/list` is the daemon's `GET /mcp/tools`, or an empty list on any
  failure. `tools/call` is `POST /mcp/call`, or an `Error:` result on any
  failure: the session ended, or Jarvis restarted.

`jarvis mcp` (`jarvis/v2/mcp.py`), the owner's own external jarvis-mcp, is a
different program and is unchanged.
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import threading

PROTOCOL_VERSION = "2025-06-18"
PROTOCOL_VERSIONS = frozenset({PROTOCOL_VERSION, "2025-03-26", "2024-11-05"})
TOKEN_VARIABLE = "JARVIS_MCP_TOKEN"
PORT_VARIABLE = "JARVIS_DAEMON_PORT"
LIST_TIMEOUT_S = 10.0
# Longer than the approval broker's remote timeout (ten minutes): a dangerous
# tool waits on the owner, and the relay must not give up first.
CALL_TIMEOUT_S = 900.0
MAX_RESPONSE = 8 * 1024 * 1024

NOT_CONNECTED = ("Error: this jarvis-mcp is not connected to a Jarvis chat "
                 "(the session ended or Jarvis restarted).")
_REFUSALS = {
    400: "Error: Jarvis refused the arguments: they must be a JSON object.",
    404: "Error: this chat is not offered that Jarvis tool.",
    413: "Error: the arguments are too large for a Jarvis tool (16 KiB at most).",
}


class Relay:
    def __init__(self, token: str, port: int | None):
        self._token = token
        self._port = port

    def _request(self, method: str, path: str, body: dict | None, timeout: float):
        """`(status, payload)` from the daemon, or None on any failure."""
        if not self._token or self._port is None:
            return None
        conn = http.client.HTTPConnection("127.0.0.1", self._port, timeout=timeout)
        try:
            headers = {"Authorization": "Bearer " + self._token,
                       "Accept": "application/json"}
            data = None
            if body is not None:
                data = json.dumps(body, ensure_ascii=True, allow_nan=False).encode()
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=data, headers=headers)
            response = conn.getresponse()
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                return None
            return response.status, json.loads(raw)
        except Exception:  # noqa: BLE001 — every failure is "not connected"
            return None
        finally:
            conn.close()

    def list_tools(self) -> dict:
        got = self._request("GET", "/mcp/tools", None, LIST_TIMEOUT_S)
        if got is None or got[0] != 200 or not isinstance(got[1], dict):
            return {"tools": []}
        listed = got[1].get("tools")
        tools = []
        for entry in listed if isinstance(listed, list) else []:
            if (isinstance(entry, dict) and isinstance(entry.get("name"), str)
                    and isinstance(entry.get("description"), str)
                    and isinstance(entry.get("inputSchema"), dict)):
                tool = {"name": entry["name"], "description": entry["description"],
                        "inputSchema": entry["inputSchema"]}
                if isinstance(entry.get("annotations"), dict):
                    tool["annotations"] = entry["annotations"]
                tools.append(tool)
        return {"tools": tools}

    def call_tool(self, name: str, arguments: dict) -> dict:
        got = self._request("POST", "/mcp/call", {"name": name, "arguments": arguments},
                            CALL_TIMEOUT_S)
        if got is None:
            return _result(NOT_CONNECTED, error=True)
        status, payload = got
        if status != 200 or not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            return _result(_REFUSALS.get(status, NOT_CONNECTED), error=True)
        result = _result(payload["text"], error=payload.get("isError") is True)
        image = payload.get("image")
        if (isinstance(image, dict) and isinstance(image.get("data"), str)
                and isinstance(image.get("mimeType"), str)):
            result["content"].append({"type": "image", "data": image["data"],
                                      "mimeType": image["mimeType"]})
        return result


def _result(text: str, *, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": error}


class RPCError(Exception):
    def __init__(self, code: int, message: str):
        self.code, self.message = code, message


def _route(relay: Relay, method: str, params: dict):
    if method == "initialize":
        version = params.get("protocolVersion")
        if not isinstance(version, str):
            raise RPCError(-32602, "protocolVersion must be a string")
        # No `listChanged`: the list is read once, when the CLI connects.
        return {"protocolVersion": version if version in PROTOCOL_VERSIONS else PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "jarvis", "version": "2.0"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return relay.list_tools()
    if method == "tools/call":
        name, arguments = params.get("name"), params.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            raise RPCError(-32602, "tools/call requires a name and an arguments object")
        return relay.call_tool(name, arguments)
    raise RPCError(-32601, "Method not found")


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(relay: Relay, message):
    """One JSON-RPC message → its response, or None for a notification."""
    if (not isinstance(message, dict) or message.get("jsonrpc") != "2.0"
            or not isinstance(message.get("method"), str)
            or ("id" in message and (isinstance(message["id"], bool)
                                    or not isinstance(message["id"], (str, int))))):
        return _error(None, -32600, "Invalid Request")
    if "id" not in message:
        return None                         # notifications get no answer and do nothing
    request_id = message["id"]
    try:
        params = message.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise RPCError(-32602, "params must be an object")
        return {"jsonrpc": "2.0", "id": request_id,
                "result": _route(relay, message["method"], params)}
    except RPCError as exc:
        return _error(request_id, exc.code, exc.message)
    except Exception:  # noqa: BLE001
        return _error(request_id, -32603, "Internal error")


def _reject_constant(value):
    raise ValueError(f"Not a JSON constant: {value}")


def serve(relay: Relay, source, sink) -> None:
    """Read requests until EOF. A tool call runs on its own thread, so a call
    that waits on the owner's approval does not hold up `ping` or a listing;
    responses are written whole, one at a time."""
    lock = threading.Lock()
    workers = []

    def write(response) -> None:
        if response is None:
            return
        line = json.dumps(response, ensure_ascii=True, allow_nan=False) + "\n"
        with lock:
            try:
                sink.write(line)
                sink.flush()
            except (OSError, ValueError):
                pass

    for line in source:
        try:
            message = json.loads(line, parse_constant=_reject_constant)
        except (ValueError, UnicodeError):
            write(_error(None, -32700, "Parse error"))
            continue
        if isinstance(message, dict) and message.get("method") == "tools/call" and "id" in message:
            worker = threading.Thread(target=lambda m=message: write(handle(relay, m)),
                                      name="jarvis-mcp-call", daemon=True)
            workers.append(worker)
            worker.start()
            continue
        write(handle(relay, message))
    # EOF: the CLI is gone and nobody reads an answer. A call still waiting
    # (on the owner, say) is not waited for; the daemon finishes it alone.
    for worker in workers:
        worker.join(0.5)


def _port(value) -> int | None:
    try:
        port = int(str(value))
    except (TypeError, ValueError):
        return None
    return port if 0 < port < 65536 else None


def main() -> int:
    # The token leaves the environment before anything else runs, so no
    # process this one starts can inherit it.
    token = os.environ.pop(TOKEN_VARIABLE, "") or ""
    port = _port(os.environ.get(PORT_VARIABLE))
    # Nothing on stderr, ever: the CLI logs an MCP server's stderr.
    try:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    except OSError:
        pass
    relay = Relay(token, port)
    del token
    # Protocol writes go to the original stdout; anything else that prints
    # would corrupt the stream, so fd 1 is pointed at the (silenced) stderr.
    sys.stdout.flush()
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8")
    try:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    except OSError:
        pass
    sys.stdout = sys.stderr
    try:
        with protocol:
            serve(relay, sys.stdin, protocol)
    except BaseException:  # noqa: BLE001 — leave quietly; a traceback goes to stderr
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
