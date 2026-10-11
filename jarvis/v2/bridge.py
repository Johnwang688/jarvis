"""The daemon side of jarvis-mcp for chat sessions: `GET /mcp/tools`, `POST /mcp/call`.

Plan `docs/plans/2026-10-10-terminal-read-mcp-plan.md` §1.5, decisions D-1
and D-3. A Claude or Codex chat's CLI starts the relay (`mcp_session.py`) with
its session's caller token; the relay forwards `tools/list` and `tools/call`
here, and the tool runs **in the daemon**, never in the relay:

    model → CLI → relay → POST /mcp/call (Bearer jmcp_…) → bridge
          → the registry's dispatch, approve=<the session's gate>

Rules, each tested in `tests/v2/mcp_session_check.py`:

- **The API listener only.** `/mcp/*` is 404 on the HUD listener (whose
  Origin means "the owner") and on the preview listener; any request carrying
  an `Origin` is 403 (`callers.authenticate`). The token rides in the
  `Authorization` header, never the URL: paths are logged.
- **Only what the session is offered** (`session_tools.for_provider`): any
  other name is 404, built from `tools.REGISTRY[name].spec()` so a schema
  here equals the fast path's by construction.
- **One call of the registry's `dispatch`** (in `call`; a test counts it),
  in a fresh `contextvars.Context()` with
  `runtime.caller()` bound to the session's thread and current turn, depth 0,
  and the session's offered names bound as the toolset — so the dispatch fix
  that makes `dispatch()` enforce a bound toolset holds here before and after
  it lands.
- **The session's own gate.** A non-dangerous tool runs. A dangerous one goes
  through the session's permit with `dangerous_tool=True` — layer 1, then the
  owner every time through the broker (a card or a DM), no Always — and is
  never auto-approved (decisions, D-3 detail). The approver carries no
  `jarvis_human_backed` flag, so a v1 rules ALLOW cannot stand in for the
  owner's yes either.
- **Refusals:** 403 (`callers.FORBIDDEN`), 404 for a tool not offered, 400
  for arguments that are not an object, 413 for a body over 16 KiB.
- **Nothing about a call is logged** but its tool name and thread id at
  DEBUG; no argument, result or token reaches the log or the bus here.
"""
from __future__ import annotations

import contextvars
import json
import logging
from pathlib import Path
import sys

LOG = logging.getLogger(__name__)

MAX_BODY = 16 * 1024
SERVER_NAME = "jarvis"
RELAY_MODULE = "jarvis.v2.mcp_session"
TOKEN_VARIABLE = "JARVIS_MCP_TOKEN"
PORT_VARIABLE = "JARVIS_DAEMON_PORT"


def _python() -> str:
    # absolute(), not resolve(): resolving the venv's symlink loses its deps
    # (the same rule as `jarvis mcp`'s print_config).
    return str(Path(sys.executable).absolute())


def server_entry(provider, port: int, token: str) -> dict:
    """The `mcp_servers["jarvis"]` entry for one chat session's run brief.

    `python -m jarvis.v2.mcp_session`: a stdlib-only relay that imports no
    tool. The token is in `env`, which each provider carries off the command
    line — Claude in a private file, Codex in its private `config.toml`. On
    Codex, each read-only session tool is `approval_mode = "approve"`, so
    Codex does not ask about a read the daemon has already judged (§4.2)."""
    from jarvis import config
    from .model import ProviderName
    from . import session_tools
    entry = {"command": _python(), "args": ["-m", RELAY_MODULE],
             "env": {TOKEN_VARIABLE: token, PORT_VARIABLE: str(int(port)),
                     "PYTHONPATH": str(config.REPO_ROOT.resolve())}}
    name = provider.value if isinstance(provider, ProviderName) else str(provider)
    if name == ProviderName.CODEX.value:
        approve = {tool.name: {"approval_mode": "approve"}
                   for tool in session_tools.for_provider(ProviderName.CODEX) if tool.read_only}
        if approve:
            entry["tools"] = approve
    return entry


def _api_listener(handler, daemon) -> bool:
    """Only the API listener serves `/mcp/*`. The HUD's (owner Origin) and the
    preview's never do, whatever the request carries."""
    server = handler.server
    return (not getattr(server, "preview_only", False)
            and server is getattr(daemon, "_server", None)
            and server.server_address[1] == daemon.port)


def _annotations(entry) -> dict:
    if entry.read_only:
        return {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}
    return {"readOnlyHint": False}


def listing(provider) -> list[dict]:
    """The tools a chat on `provider` is offered, as MCP describes a tool."""
    from jarvis import tools
    from . import session_tools
    out = []
    for entry in session_tools.for_provider(provider):
        spec = tools.REGISTRY[entry.name].spec()["function"]
        out.append({"name": spec["name"], "description": spec["description"],
                    "inputSchema": spec["parameters"], "annotations": _annotations(entry)})
    return out


def _body(handler) -> dict:
    from .daemon import APIError
    if handler.headers.get("Transfer-Encoding"):
        raise APIError(400, "chunked bodies are not supported")
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        raise APIError(400, "invalid body length") from None
    if length < 0:
        raise APIError(400, "invalid body length")
    if length > MAX_BODY:
        raise APIError(413, "request body exceeds 16 KiB")
    data = handler.rfile.read(length)
    if len(data) != length:
        raise APIError(400, "incomplete request body")
    try:
        value = json.loads(data) if data else None
    except (ValueError, UnicodeError):
        raise APIError(400, "body must be a JSON object") from None
    if not isinstance(value, dict):
        raise APIError(400, "body must be a JSON object")
    unknown = set(value) - {"name", "arguments"}
    if unknown:
        raise APIError(400, "unknown fields: " + ", ".join(sorted(unknown)))
    if not isinstance(value.get("name"), str) or not value["name"]:
        raise APIError(400, "name must be a nonempty string")
    arguments = value.get("arguments", {})
    if not isinstance(arguments, dict):
        raise APIError(400, "arguments must be a JSON object")
    return {"name": value["name"], "arguments": arguments}


def route(handler, daemon, parts, query):
    """`/mcp/*`, or None for every other path."""
    if not parts or parts[0] != "mcp":
        return None
    from .daemon import APIError, _object
    if not _api_listener(handler, daemon):
        raise APIError(404, "route not found")
    method = handler.command
    if not ((parts == ["mcp", "tools"] and method == "GET")
            or (parts == ["mcp", "call"] and method == "POST")):
        raise APIError(404, "route not found")
    from .callers import authenticate
    caller, session = authenticate(handler, daemon)
    _object(query, ())
    provider = session.thread.provider
    if parts[1] == "tools":
        return 200, {"tools": listing(provider)}
    body = _body(handler)
    return 200, call(daemon, session, caller, body["name"], body["arguments"])


def _approver(session):
    """The session's own gate for a dangerous tool: its permit, asked with
    `dangerous_tool=True` — the owner decides every call. A permit that cannot
    take the keyword, that raises, or that answers anything but ALLOW, denies.
    Deliberately not `jarvis_human_backed`: no rules ALLOW stands in for it."""
    from .provider import Decision
    permit = getattr(session, "permit", None)

    def approve(entry, arguments) -> bool:
        if permit is None:
            return False
        try:
            decision = Decision(permit(entry.name, dict(arguments), None, dangerous_tool=True))
        except Exception:  # noqa: BLE001 — an unreadable answer is a denial
            return False
        return decision is Decision.ALLOW

    return approve


def call(daemon, session, caller, name: str, arguments: dict) -> dict:
    """Run one offered tool for `session`, in the daemon. `{text, isError}`
    (plus `image` for a tool that returns one)."""
    from jarvis import runtime, tools
    from .daemon import APIError
    from . import session_tools
    thread = session.thread
    offered = session_tools.for_provider(thread.provider)
    if name not in {entry.name for entry in offered}:
        raise APIError(404, "this chat is not offered that tool")
    with daemon._lock:
        # The turn this call belongs to: the running one, or none between
        # turns (a CLI turn nobody sent, a call while the session opens).
        turn_id = session.turn_id if session.worker is not None else None
    who = {"thread_id": thread.id, "project_id": thread.project_id,
           "provider": caller.provider, "turn_id": turn_id}
    names = frozenset(entry.name for entry in offered)
    approve = _approver(session)
    LOG.debug("jarvis-mcp call %s for thread %s", name, thread.id)

    def run():
        runtime.bind(caller=who, depth=0, tool_names=names, approve=approve)
        return tools.dispatch(name, json.dumps(arguments), approve=approve)

    result = contextvars.Context().run(run)
    text = result.text
    payload = {"text": text,
               "isError": text.startswith(("Error:", "Refused:", "The user declined"))}
    if result.image_b64 is not None:
        payload["image"] = {"data": result.image_b64, "mimeType": result.mime}
    return payload
