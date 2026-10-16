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
import select
import socket
import threading
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
    Codex every exact offered tool delegates to the daemon: annotations
    remain truthful and the bridge applies the session gate before execution."""
    from jarvis import config
    from .model import ProviderName
    from . import session_tools
    entry = {"command": _python(), "args": ["-m", RELAY_MODULE],
             "env": {TOKEN_VARIABLE: token, PORT_VARIABLE: str(int(port)),
                     "PYTHONPATH": str(config.REPO_ROOT.resolve())}}
    name = provider.value if isinstance(provider, ProviderName) else str(provider)
    if name == ProviderName.CODEX.value:
        approve = {tool.name: {"approval_mode": "approve"}
                   for tool in session_tools.for_provider(ProviderName.CODEX)}
        if approve:
            entry["tools"] = approve
            entry["enabled_tools"] = sorted(approve)
            entry["tool_timeout_sec"] = 900
    return entry


def _api_listener(handler, daemon) -> bool:
    """Only the API listener serves `/mcp/*`. The HUD's (owner Origin) and the
    preview's never do, whatever the request carries."""
    server = handler.server
    return (not getattr(server, "preview_only", False)
            and server is getattr(daemon, "_server", None)
            and server.server_address[1] == daemon.port)


def _annotations(entry) -> dict:
    if entry.owner_gated:
        return {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}
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
    unknown = set(value) - {"name", "arguments", "codex_context"}
    if unknown:
        raise APIError(400, "unknown fields: " + ", ".join(sorted(unknown)))
    if not isinstance(value.get("name"), str) or not value["name"]:
        raise APIError(400, "name must be a nonempty string")
    arguments = value.get("arguments", {})
    if not isinstance(arguments, dict):
        raise APIError(400, "arguments must be a JSON object")
    return {"name": value["name"], "arguments": arguments,
            "codex_context": value.get("codex_context")}


def _request_alive(connection):
    """Consumed-body socket lifetime; peek only, without a watchdog or IO wait."""
    lock = threading.Lock()
    def alive():
        with lock:
            try:
                readable, _, _ = select.select([connection], [], [], 0)
                if not readable:
                    return True
                return bool(connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT))
            except BlockingIOError:
                return True
            except (OSError, ValueError, TypeError):
                return False
    return alive


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
    return 200, call(daemon, session, caller, body["name"], body["arguments"],
                     codex_context=body["codex_context"],
                     request_alive=_request_alive(handler.connection))


def _unasked_refusal():
    """`tools.Refusal` for the permit call just made when it was a DENY put
    to nobody (`permissions.last_verdict`): the model reads why — a chat git
    tool's `Refused` text, the strict profile — never "the user declined",
    which is kept for the owner's no (#223's review, LOW 1). False otherwise."""
    from jarvis import tools
    from .permissions import last_verdict
    from .provider import Decision
    verdict = last_verdict()
    if (verdict is not None and verdict.decision is Decision.DENY and not verdict.asked
            and verdict.reason):
        return tools.Refusal(verdict.reason)
    return False


def _approver(session, current):
    """The session's own gate for a dangerous tool: its permit, asked with
    `dangerous_tool=True` — the owner decides every call. A permit that cannot
    take the keyword, that raises, or that answers anything but ALLOW, denies;
    a DENY nobody was asked about carries its reason (`_unasked_refusal`).
    Deliberately not `jarvis_human_backed`: no rules ALLOW stands in for it."""
    from .permissions import last_verdict
    from .provider import Decision
    permit = getattr(session, "permit", None)

    def approve(entry, arguments):
        if permit is None or not current():
            return False
        last_verdict()      # nothing from an earlier call is read as this one's
        try:
            decision = Decision(permit(entry.name, dict(arguments), None, dangerous_tool=True))
        except Exception:  # noqa: BLE001 — an unreadable answer is a denial
            return False
        if decision is not Decision.ALLOW:
            return _unasked_refusal()
        # The broker wait may outlive this turn or this exact session. Check
        # under the daemon lock again before dispatch can enter the tool.
        return current()

    return approve


def _delegated(permit, name: str, arguments: dict, current):
    """A Codex chat's nondangerous Jarvis tool: the session's exact permit
    must ALLOW it first. None when it may run, else the refusal (the
    permit's reason when it refused unasked, `_unasked_refusal`)."""
    from jarvis import tools
    from .permissions import last_verdict
    from .provider import Decision
    last_verdict()
    try:
        allowed = (current() and permit is not None
                   and Decision(permit(name, dict(arguments), None)) is Decision.ALLOW)
    except Exception:  # a missing or malformed permit never delegates
        allowed = False
    if allowed and current():
        return None
    refusal = _unasked_refusal() if not allowed else False
    if isinstance(refusal, tools.Refusal):        # falsy, as an approver's no is
        return tools.ToolResult(refusal.text)
    return tools.ToolResult("Refused: this chat did not permit the Jarvis tool call.")


def call(daemon, session, caller, name: str, arguments: dict, *, codex_context=None, request_alive=None) -> dict:
    """Run one offered tool for `session`, in the daemon. `{text, isError}`
    (plus `image` for a tool that returns one)."""
    from jarvis import runtime, tools
    from .daemon import APIError
    from . import session_tools
    thread = session.thread
    offered = session_tools.for_provider(thread.provider)
    entry = next((entry for entry in offered if entry.name == name), None)
    if entry is None:
        raise APIError(404, "this chat is not offered that tool")
    from .callers import FORBIDDEN
    from .model import Role

    def live_session():
        return (not daemon._stopping and daemon._sessions.get(thread.id) is session
                and caller.session is session and caller.thread_id == thread.id
                and caller.provider == thread.provider.value and session.caller_key is not None
                and not session.closing and not session.retired and not session.lost
                and thread.role == Role.CHAT and thread.task_id is None
                and session.brief.role == Role.CHAT and session.brief.task_id is None)

    with daemon._lock:
        if not live_session():
            raise APIError(403, FORBIDDEN)
        # A CLI may call between turns; it gets an off-desk slot. Capture
        # worker identity as well as turn id so a successor cannot inherit it.
        worker = session.worker
        turn_id = session.turn_id if worker is not None else None
        desk = session.desk.slot if worker is not None else {"present": False}
        browser_call = name in ("hud_browser_view", "hud_browser_act")
        drive_turn = getattr(session, "drive_turn", None) if worker is not None else None

    native_current = None
    if caller.provider == "codex" and entry.foreground_only:
        # The bearer token authenticates the owning chat, not which native
        # agent used its relay. Admit exact host thread/turn/call identity
        # before dispatch. No provider callbacks while holding daemon locks.
        admit = getattr(session.provider, "admit_foreground_mcp", None)
        try:
            native_current = admit(session.handle, codex_context) if callable(admit) else None
        except Exception:
            native_current = None
        if not callable(native_current):
            return {"text": "Refused: this Jarvis tool is only available to the current foreground chat turn.",
                    "isError": True}

    if entry.owner_gated:
        from .browser.drive import admission_for_tool, OFF
        if name != "hud_browser_act" or not admission_for_tool():
            return {"text": "Refused: " + OFF, "isError": True}

    def current():
        with daemon._lock:
            live = (live_session() and session.worker is worker
                    and (session.turn_id if session.worker is not None else None) == turn_id
                    and not session.cancelled.is_set()
                    and (not browser_call or getattr(session, "drive_turn", None) is drive_turn)
                    and (drive_turn is None or not drive_turn.closed))
        if not live:
            return False
        if browser_call and request_alive is not None:
            try:
                if not request_alive():
                    return False
            except Exception:
                return False
        try:
            return native_current is None or native_current() is True
        except Exception:
            return False
    who = {"thread_id": thread.id, "project_id": thread.project_id,
           "provider": caller.provider, "turn_id": turn_id}
    names = frozenset(entry.name for entry in offered)
    approve = _approver(session, current)
    LOG.debug("jarvis-mcp call %s for thread %s", name, thread.id)

    def dispatch():
        runtime.bind(caller=who, depth=0, tool_names=names, approve=approve,
                     desk=desk, should_stop=lambda: not current())
        if entry.owner_gated and drive_turn is None:
            return tools.ToolResult("Refused: this Browser action needs the current owner chat turn.")
        # Codex native approval delegates transport for every offered tool.
        # Unlike Claude it has no PreToolUse hook, so nondangerous entries
        # still need the exact session permit (STRICT/named always-ask).
        # Dangerous entries take dispatch's existing single owner gate.
        if caller.provider == "codex" and not tools.REGISTRY[name].dangerous:
            refused = _delegated(getattr(session, "permit", None), name, arguments, current)
            if refused is not None:
                return refused
        if not current() and not entry.desk_only:
            return tools.ToolResult("Refused: this chat turn is no longer current.")
        return tools.dispatch(name, json.dumps(arguments), approve=approve)

    def run():
        from .browser.drive_state import bind_turn
        with bind_turn(drive_turn if browser_call else None):
            return dispatch()

    result = contextvars.Context().run(run)
    text = result.text
    payload = {"text": text,
               "isError": (text.startswith(("Error:", "Refused:", "The user declined"))
                           or (name == "hud_browser_act" and text.startswith("Stopped:")))}
    if result.image_b64 is not None:
        payload["image"] = {"data": result.image_b64, "mimeType": result.mime}
    return payload
