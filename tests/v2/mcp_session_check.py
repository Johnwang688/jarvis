"""Free checks for WP-1: per-session jarvis-mcp caller tokens (Phase 0).

Plan `docs/plans/2026-10-10-terminal-read-mcp-plan.md` §1, §2, §4 and §7.1,
§7.2, §7.7; decisions file beside it. No CLI, no network, no API key:

- the daemon runs on port 0 (three ephemeral listeners), never 8402/8403/8405;
- every config path and the data folder point into a temp directory;
- the Claude and Codex providers the daemon drives are fakes registered under
  those names that capture the run copy of the brief and, from inside
  `send()`, start the **real** relay (`python -m jarvis.v2.mcp_session`)
  with exactly the environment the brief hands it — so the whole chain
  (mint → run copy → relay → `/mcp/*` → bridge → `tools.dispatch`) runs;
- the real `ClaudeProvider` runs over a fake SDK client, and its command line
  is built by the installed SDK's own transport; the real `CodexProvider`
  runs over `codex_provider_check`'s fake app-server (real pipes, fake
  binary). Tokens there are placeholders shaped like the real thing.

Two probe tools are registered for the run and offered as session tools:
`mcp_probe_read` (read-only, foreground-only) and `mcp_probe_send`
(`dangerous=True`). Production ships `session_tools.SESSION_TOOLS` empty,
which a check below asserts in a fresh interpreter.

Run: PYTHONPATH=. .venv/bin/python tests/v2/mcp_session_check.py
"""
from __future__ import annotations

import ast
import asyncio
import json
import http.client as httpclient
import logging
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from contextlib import ExitStack
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Annotated
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from jarvis import config, runtime, tools  # noqa: E402
from jarvis.v2 import bridge, callers, mcp_session, session_tools  # noqa: E402
from jarvis.v2 import daemon as daemon_mod  # noqa: E402
from jarvis.v2.approvals import PendingApprovals  # noqa: E402
from jarvis.v2.model import PermissionProfile, ProviderName, Role, Thread  # noqa: E402
from jarvis.v2.permissions import PermitContext, build_permit, deny_all  # noqa: E402
from jarvis.v2.provider import (Brief, BriefRefused, Decision, Event,  # noqa: E402
                                EventKind as K, SessionHandle, Usage, UserMessage)
from jarvis.v2.providers import codex, codex_config  # noqa: E402
from jarvis.v2.stores import Stores  # noqa: E402

CLAUDE, CODEX, FAST = ProviderName.CLAUDE, ProviderName.CODEX, ProviderName.FAST
# Shaped like a caller token, minted by nobody: for the provider-level checks
# that never go through a daemon.
PLACEHOLDER = "jmcp_" + "PlaceholderTokenNotMintedByAnyDaemon0123456"[:43]
assert callers.TOKEN_RE.fullmatch(PLACEHOLDER)
READ, SEND = "mcp_probe_read", "mcp_probe_send"
APPROVAL_TIMEOUT = 5.0

# What the probes saw, in the order they ran.
CALLS: list[dict] = []


def mcp_probe_read(note: Annotated[str, "Anything; it is echoed back"] = "") -> str:
    """Read-only probe for the jarvis-mcp session suite. Echoes `note`."""
    CALLS.append({"tool": READ, "caller": runtime.caller(), "depth": runtime.depth(),
                  "tools": runtime.parent_tools(), "pid": os.getpid(), "note": note})
    return f"PROBE-MARKER {note}"


def mcp_probe_send(to: Annotated[str, "Who to send to"]) -> str:
    """Dangerous probe for the jarvis-mcp session suite. 'Sends' to `to`."""
    CALLS.append({"tool": SEND, "caller": runtime.caller(), "pid": os.getpid(), "to": to})
    return f"sent to {to}"


def setUpModule():
    tools.tool(mcp_probe_read)
    tools.tool(dangerous=True)(mcp_probe_send)
    session_tools.register(session_tools.SessionTool(READ, read_only=True, foreground_only=True))
    session_tools.register(session_tools.SessionTool(SEND))


def tearDownModule():
    for name in (READ, SEND):
        session_tools.unregister(name)
        tools.REGISTRY.pop(name, None)


def patch_config(stack: ExitStack, root: Path) -> None:
    creds = root / "creds"
    for name, value in {
        "V2_DATA_DIR": root / "v2",
        "ALLOWLIST_PATH": root / "cfg" / "allowlist.json",
        "MODELS_PATH": root / "cfg" / "models.json",
        "PROVIDER_DEFAULTS_PATH": root / "cfg" / "provider_defaults.json",
        "ROUTING_PATH": root / "cfg" / "routing.json",
        "DISCORD_GUILD_PATH": root / "cfg" / "discord_guild.json",
        "V2_ALWAYS_ASK": root / "cfg" / "always-ask.json",
        "V2_CREDENTIAL_DIRS": (str(creds / ".ssh"),),
        "SESSIONS_DIR": root / "sessions",
        "CODEX_CATALOG_PATH": root / "cfg" / "codex-models.json",
    }.items():
        stack.enter_context(patch.object(config, name, value))


# --- a fake CLI provider that runs the real relay ------------------------------


@dataclass
class Opened:
    thread_id: str
    brief: Brief
    permit: object


class FakeCli:
    """Registered as `claude` or `codex`. Captures the run brief each session
    is started with; `script[thread_id]` runs inside `send()` (where a real
    CLI would call its MCP server) and may answer "fatal"."""

    def __init__(self, name):
        self.name = ProviderName(name)
        self.opened: list[Opened] = []
        self.models: list[tuple] = []
        self.script: dict = {}
        self.fail_start = False
        self.closed = 0
        # (entered, release) events: hold `start` (a session still opening)
        # or `close` (a session still closing) until the test releases it.
        self.start_gate = None
        self.close_gate = None

    def health(self):
        return True, "fake"

    def start(self, thread, brief, permit):
        self.opened.append(Opened(thread.id, brief, permit))
        if self.start_gate is not None:
            self.start_gate[0].set()
            self.start_gate[1].wait(10)
        if self.fail_start:
            raise BriefRefused("fake start failure")
        return SessionHandle(thread.id, self.name, f"fake:{thread.id}:{len(self.opened)}",
                             {"brief": brief, "permit": permit})

    resume = start

    def send(self, h, message):
        yield Event(K.TURN_STARTED, h.thread_id)
        action = self.script.get(h.thread_id)
        outcome = action(h, message) if action is not None else None
        if outcome == "fatal":
            yield Event(K.ERROR, h.thread_id, {"message": "fake fatal", "fatal": True})
            return
        yield Event(K.TEXT, h.thread_id, {"text": "done"})
        yield Event(K.TURN_FINISHED, h.thread_id, {"stop": "end"})

    def interrupt(self, h):
        pass

    def close(self, h):
        if self.close_gate is not None:
            self.close_gate[0].set()
            self.close_gate[1].wait(5)
        self.closed += 1

    def usage(self, h):
        return Usage()

    def answer(self, h, req_id, decision):
        raise ValueError("the fake asks nothing")

    def set_model(self, h, model, effort):
        self.models.append((h.thread_id, model, effort))


def entry_of(brief: Brief) -> dict:
    return brief.mcp_servers["jarvis"]


def token_of(brief: Brief) -> str:
    return entry_of(brief)["env"][bridge.TOKEN_VARIABLE]


def run_relay(entry: dict, calls=(), *, cwd: Path, timeout: float = 30.0):
    """Start the real relay as a CLI would — `command` + `args`, the entry's
    `env` over a bare PATH — and speak MCP to it. ({id: response}, stderr)."""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(cwd), **entry["env"]}
    proc = subprocess.Popen([entry["command"], *entry["args"]], cwd=str(cwd), env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "fake-cli", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    for index, (name, arguments) in enumerate(calls):
        requests.append({"jsonrpc": "2.0", "id": 10 + index, "method": "tools/call",
                         "params": {"name": name, "arguments": arguments}})
    want = {r["id"] for r in requests if "id" in r}
    timer = threading.Timer(timeout, proc.kill)
    timer.start()
    got: dict = {}
    try:
        proc.stdin.write("".join(json.dumps(r) + "\n" for r in requests))
        proc.stdin.flush()
        while want - got.keys():
            line = proc.stdout.readline()
            if not line:
                break
            message = json.loads(line)
            got[message.get("id")] = message
        proc.stdin.close()
        err = proc.stderr.read()
        proc.wait(10)
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
    return got, err


def http(port, method, path, *, token=None, body=None, raw=None, headers=None,
         authorizations=None):
    """One request; (status, json). `authorizations` sends several headers."""
    conn = httpclient.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        conn.putrequest(method, path)
        sent = {"Content-Type": "application/json"} if data is not None else {}
        if token is not None:
            sent["Authorization"] = "Bearer " + token
        sent.update(headers or {})
        for key, value in sent.items():
            conn.putheader(key, value)
        for value in authorizations or ():
            conn.putheader("Authorization", value)
        if data is not None:
            conn.putheader("Content-Length", str(len(data)))
        conn.endheaders(data)
        response = conn.getresponse()
        payload = response.read()
        try:
            value = json.loads(payload) if payload else None
        except ValueError:
            value = payload.decode(errors="replace")
        return response.status, value
    finally:
        conn.close()


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        try:
            self.lines.append(record.getMessage())
        except Exception:  # noqa: BLE001
            self.lines.append(str(record.msg))
        if record.exc_info:
            self.lines.append(logging.Formatter().formatException(record.exc_info))


def files_holding(root: Path, needle: str) -> list[str]:
    hits = []
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            try:
                if needle.encode() in path.read_bytes():
                    hits.append(str(path))
            except OSError:
                pass
    return hits


# --- the daemon harness -----------------------------------------------------------


class DaemonHarness(unittest.TestCase):
    profile = "auto"

    def setUp(self):
        CALLS.clear()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="mcp-session-")))
        patch_config(self.stack, self.root)
        (self.root / "project").mkdir()
        self.stores = Stores()
        self.claude, self.codex, self.fast = FakeCli(CLAUDE), FakeCli(CODEX), FakeCli(FAST)
        self.d = self.make_daemon()
        self.events = self.d.bus.subscribe()
        self.project = self.stores.projects.create("test", str(self.root / "project"),
                                                   profile=PermissionProfile(self.profile))
        self.stderr: list[str] = []

    def make_daemon(self, stores=None, providers=None):
        holder = {}
        approvals = PendingApprovals(
            timeout_s=APPROVAL_TIMEOUT,
            on_request=lambda request: holder["d"]._approval_requested(request),
            on_resolve=lambda request, decision, resolution:
                holder["d"]._approval_resolved(request, decision, resolution))
        d = daemon_mod.Daemon(stores or self.stores,
                              providers or {CLAUDE: self.claude, CODEX: self.codex, FAST: self.fast},
                              None, 0, approvals)
        holder["d"] = d
        d.start()
        self.addCleanup(d.stop)
        return d

    # -- helpers

    def chat(self, provider=CLAUDE, d=None):
        return (d or self.d).open_thread(self.project.id, Role.CHAT, provider, {})

    def fake(self, provider):
        return {CLAUDE: self.claude, CODEX: self.codex, FAST: self.fast}[ProviderName(provider)]

    def run_brief(self, thread):
        return [o for o in self.fake(thread.provider).opened if o.thread_id == thread.id][-1].brief

    def relay_in_turn(self, thread, calls, *, d=None):
        """Script the thread's next turn to run the relay; send; wait; return
        what the relay got back."""
        out = {}

        def action(h, message):
            # The brief of the session running now: a send may have resumed
            # the thread (and minted a new token) on its way here.
            brief = self.run_brief(thread)
            out["got"], err = run_relay(entry_of(brief), calls, cwd=self.root)
            self.stderr.append(err)
            out["turn_id"] = (d or self.d)._sessions[thread.id].turn_id

        self.fake(thread.provider).script[thread.id] = action
        turn_id = (d or self.d).send(thread.id, UserMessage("go"))
        self.wait_idle(thread.id, d=d)
        self.assertEqual(out.get("turn_id"), turn_id)
        return out["got"], turn_id

    def wait_idle(self, thread_id, d=None, timeout=30.0):
        d = d or self.d
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with d._lock:
                session = d._sessions.get(thread_id)
                if session is None or session.worker is None:
                    return
            time.sleep(0.01)
        self.fail("turn did not finish")

    def call(self, token, name=READ, arguments=None, *, port=None, **kw):
        return http(port or self.d.port, "POST", "/mcp/call", token=token,
                    body={"name": name, "arguments": arguments or {"note": "n"}}, **kw)

    def drain(self):
        out = []
        while True:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                return out

    def resolver(self, decision, seen, thread_id):
        """Answer the first approval the broker holds for `thread_id`."""
        def run():
            deadline = time.monotonic() + APPROVAL_TIMEOUT
            while time.monotonic() < deadline:
                for request in self.d.approvals.pending():
                    if request.thread_id == thread_id:
                        seen.append(request)
                        self.d.resolve_approval(request.req_id, decision)
                        return
                time.sleep(0.01)
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        return worker


# --- 7.1: tokens ----------------------------------------------------------------


class CallerTokenChecks(unittest.TestCase):
    def session(self, thread_id="aaaa0001", provider=CLAUDE):
        return SimpleNamespace(thread=Thread(thread_id, "p", Role.CHAT, provider), caller_key=None)

    def test_mint_resolve_revoke(self):
        registry = callers.CallerTokens()
        a, b = self.session("aaaa0001"), self.session("bbbb0002", CODEX)
        ta, tb = registry.mint(a), registry.mint(b)
        self.assertRegex(ta, r"^jmcp_[A-Za-z0-9_\-]{43}$")
        self.assertNotEqual(ta, tb)
        caller = registry.resolve(ta)
        self.assertIs(caller.session, a)
        self.assertEqual((caller.thread_id, caller.provider), ("aaaa0001", "claude"))
        self.assertEqual(registry.resolve(tb).provider, "codex")
        self.assertEqual(a.caller_key, callers._digest(ta))
        # No raw token anywhere in the registry: digests only.
        held = repr(vars(registry)) + repr(list(registry._by_digest))
        self.assertNotIn(ta, held)
        self.assertNotIn(tb, held)
        # A second mint for a session replaces its first.
        ta2 = registry.mint(a)
        self.assertIsNone(registry.resolve(ta))
        self.assertIs(registry.resolve(ta2).session, a)
        registry.revoke(a)
        self.assertIsNone(registry.resolve(ta2))
        self.assertIsNone(a.caller_key)
        self.assertIs(registry.resolve(tb).session, b)
        registry.mint(a)
        registry.revoke_all()
        self.assertEqual(registry.live(), 0)
        self.assertIsNone(registry.resolve(tb))

    def test_resolve_refuses_every_other_shape(self):
        registry = callers.CallerTokens()
        token = registry.mint(self.session())
        for bad in (None, 7, "", token[:-1], token + "x", token.replace("jmcp_", "jmcq_"),
                    " " + token, token.upper(), "jmcp_" + "A" * 43):
            self.assertIsNone(registry.resolve(bad), bad)
        self.assertIsNotNone(registry.resolve(token))


class BridgeChecks(DaemonHarness):
    def test_claude_chat_round_trip_through_the_real_relay(self):
        thread = self.chat(CLAUDE)
        brief = self.run_brief(thread)
        entry = entry_of(brief)
        token = token_of(brief)
        self.assertRegex(token, r"^jmcp_[A-Za-z0-9_\-]{43}$")
        self.assertEqual(entry["args"], ["-m", "jarvis.v2.mcp_session"])
        self.assertTrue(Path(entry["command"]).is_absolute())
        self.assertEqual(entry["env"][bridge.PORT_VARIABLE], str(self.d.port))
        self.assertEqual(entry["env"]["PYTHONPATH"], str(config.REPO_ROOT.resolve()))
        self.assertNotIn("tools", entry, "per-tool approval is Codex's only")
        # The saved brief never holds the server or the token.
        saved = (self.stores.threads.path(thread.id).with_name("brief.json")).read_text()
        self.assertNotIn(token, saved)
        self.assertEqual(json.loads(saved)["mcp_servers"], {})
        got, turn_id = self.relay_in_turn(thread, [(READ, {"note": "hello"}), ("get_datetime", {})])
        init = got[1]["result"]
        self.assertEqual(init["capabilities"], {"tools": {}}, "no listChanged")
        listed = {t["name"]: t for t in got[2]["result"]["tools"]}
        self.assertEqual(set(listed), {READ, SEND})
        spec = tools.REGISTRY[READ].spec()["function"]
        self.assertEqual(listed[READ]["inputSchema"], spec["parameters"])
        self.assertEqual(listed[READ]["description"], spec["description"])
        self.assertEqual(listed[READ]["annotations"],
                         {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False})
        self.assertEqual(listed[SEND]["annotations"], {"readOnlyHint": False})
        result = got[10]["result"]
        self.assertEqual(result, {"content": [{"type": "text", "text": "PROBE-MARKER hello"}],
                                  "isError": False})
        # Not offered (registered, but not a session tool): refused, not run.
        self.assertTrue(got[11]["result"]["isError"])
        self.assertIn("not offered", got[11]["result"]["content"][0]["text"])
        # It ran in the daemon, with the caller bound, depth 0, and the
        # session's offered names as the toolset.
        self.assertEqual(len(CALLS), 1)
        call = CALLS[0]
        self.assertEqual(call["pid"], os.getpid(), "the tool ran in the daemon, not the relay")
        self.assertEqual(call["caller"], {"thread_id": thread.id, "project_id": self.project.id,
                                          "provider": "claude", "turn_id": turn_id})
        self.assertEqual(call["depth"], 0)
        self.assertEqual(call["tools"], frozenset({READ, SEND}))
        self.assertEqual(self.stderr, [""], "the relay writes nothing to stderr")

    def test_codex_run_brief_marks_read_only_tools_approve(self):
        thread = self.chat(CODEX)
        entry = entry_of(self.run_brief(thread))
        self.assertEqual(entry["tools"], {READ: {"approval_mode": "approve"}})
        text = codex_config.config_text(self.run_brief(thread))
        server = tomllib.loads(text)["mcp_servers"]["jarvis"]
        self.assertEqual(server["tools"][READ]["approval_mode"], "approve")
        self.assertNotIn(SEND, server["tools"])
        got, _ = self.relay_in_turn(thread, [(READ, {"note": "cx"})])
        self.assertEqual(got[10]["result"]["content"][0]["text"], "PROBE-MARKER cx")
        self.assertEqual(CALLS[0]["caller"]["provider"], "codex")

    def test_every_refusal_is_403_and_runs_nothing(self):
        thread = self.chat(CLAUDE)
        token = token_of(self.run_brief(thread))
        stranger = "jmcp_" + "Z" * 43
        cases = {
            "no header": dict(),
            "basic": dict(headers={"Authorization": "Basic " + token}),
            "empty bearer": dict(headers={"Authorization": "Bearer "}),
            "two tokens": dict(headers={"Authorization": f"Bearer {token} {token}"}),
            "two headers": dict(authorizations=["Bearer " + token, "Bearer " + token]),
            "random jmcp token": dict(token=stranger),
            "token in the query": dict(),
            "origin header": dict(token=token, headers={"Origin": f"http://127.0.0.1:{self.d.port}"}),
            "cross origin": dict(token=token, headers={"Origin": "http://evil.example"}),
        }
        for label, kw in cases.items():
            with self.subTest(case=label):
                path = f"/mcp/call?token={token}" if label == "token in the query" else "/mcp/call"
                status, value = http(self.d.port, "POST", path,
                                     body={"name": READ, "arguments": {}}, **kw)
                self.assertEqual(status, 403, value)
                if label not in ("cross origin",):
                    self.assertEqual(value, {"error": callers.FORBIDDEN})
                status, value = http(self.d.port, "GET", "/mcp/tools", **kw)
                self.assertEqual(status, 403, value)
        self.assertEqual(CALLS, [])
        # The real one works.
        status, value = self.call(token)
        self.assertEqual((status, value["text"]), (200, "PROBE-MARKER n"))

    def test_mcp_lives_on_the_api_listener_only(self):
        thread = self.chat(CLAUDE)
        token = token_of(self.run_brief(thread))
        for port in (self.d.face_port, self.d.workshop_port):
            status, _ = self.call(token, port=port)
            self.assertEqual(status, 404, port)
            status, _ = http(port, "GET", "/mcp/tools", token=token)
            self.assertEqual(status, 404, port)
        self.assertEqual(CALLS, [])
        self.assertEqual(self.call(token)[0], 200)

    def test_bad_requests(self):
        thread = self.chat(CLAUDE)
        token = token_of(self.run_brief(thread))
        port = self.d.port
        self.assertEqual(http(port, "POST", "/mcp/call", token=token,
                              body={"name": "get_datetime", "arguments": {}})[0], 404)
        self.assertEqual(http(port, "POST", "/mcp/call", token=token,
                              body={"name": "no_such_tool", "arguments": {}})[0], 404)
        self.assertEqual(http(port, "POST", "/mcp/call", token=token,
                              body={"name": READ, "arguments": ["x"]})[0], 400)
        self.assertEqual(http(port, "POST", "/mcp/call", token=token, body=["x"])[0], 400)
        self.assertEqual(http(port, "POST", "/mcp/call", token=token,
                              body={"name": READ, "arguments": {}, "extra": 1})[0], 400)
        self.assertEqual(http(port, "POST", "/mcp/call", token=token, raw=b"{nope")[0], 400)
        big = {"name": READ, "arguments": {"note": "x" * (17 * 1024)}}
        self.assertEqual(http(port, "POST", "/mcp/call", token=token, body=big)[0], 413)
        self.assertEqual(http(port, "GET", "/mcp/tools?x=1", token=token)[0], 400)
        self.assertEqual(http(port, "GET", "/mcp/call", token=token)[0], 404)
        self.assertEqual(http(port, "POST", "/mcp/other", token=token, body={})[0], 404)
        self.assertEqual(CALLS, [])

    # -- the revoke table (§1.4)

    def test_close_revokes(self):
        thread = self.chat(CLAUDE)
        token = token_of(self.run_brief(thread))
        self.assertEqual(self.call(token)[0], 200)
        self.d.close_thread(thread.id)
        self.assertIsNone(self.d.callers.resolve(token), "close must revoke, not only forget")
        self.assertEqual(self.d.callers.live(), 0)
        self.assertEqual(self.call(token)[0], 403)

    def test_a_fatal_turn_revokes_and_a_resume_mints_a_new_token(self):
        thread = self.chat(CLAUDE)
        old = token_of(self.run_brief(thread))
        self.claude.script[thread.id] = lambda h, m: "fatal"
        self.d.send(thread.id, UserMessage("boom"))
        self.wait_idle(thread.id)
        deadline = time.monotonic() + 5
        while thread.id in self.d._sessions and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertNotIn(thread.id, self.d._sessions, "a fatal turn drops the session")
        self.assertIsNone(self.d.callers.resolve(old))
        self.assertEqual(self.call(old)[0], 403)
        # The next message resumes the session with a token of its own.
        got, _ = self.relay_in_turn(thread, [(READ, {"note": "again"})])
        new = token_of(self.run_brief(thread))
        self.assertNotEqual(new, old)
        self.assertEqual(got[10]["result"]["content"][0]["text"], "PROBE-MARKER again")
        self.assertEqual(self.call(old)[0], 403)
        self.assertEqual(self.call(new)[0], 200)

    def test_a_failed_open_keeps_no_token(self):
        self.claude.fail_start = True
        with self.assertRaises(BriefRefused):
            self.chat(CLAUDE)
        token = token_of(self.claude.opened[-1].brief)
        self.assertIsNone(self.d.callers.resolve(token))
        self.assertEqual(self.d.callers.live(), 0)

    def test_stop_revokes_all_and_a_restart_knows_none(self):
        thread = self.chat(CLAUDE)
        token = token_of(self.run_brief(thread))
        self.assertEqual(self.d.callers.live(), 1)
        self.d.stop()
        self.assertEqual(self.d.callers.live(), 0)
        again = self.make_daemon()
        self.assertEqual(self.call(token, port=again.port)[0], 403)
        self.assertEqual(CALLS, [])

    def test_a_model_switch_keeps_the_token(self):
        thread = self.chat(CLAUDE)
        token = token_of(self.run_brief(thread))
        self.d.set_thread_model(thread.id, {"effort": "low"})
        got, _ = self.relay_in_turn(thread, [(READ, {"note": "switched"})])
        self.assertEqual(self.claude.models[-1][2], "low", "the switch reached the provider")
        self.assertEqual(len(self.claude.opened), 1, "no reopen, so no new token")
        self.assertEqual(got[10]["result"]["content"][0]["text"], "PROBE-MARKER switched")
        self.assertEqual(self.d.callers.live(), 1)
        self.assertEqual(self.call(token)[0], 200)

    def test_stop_revokes_a_session_still_opening(self):
        """A session whose provider is still starting has no handle, so
        `stop()` closes nothing for it: only `revoke_all` takes its token."""
        self.claude.start_gate = (threading.Event(), threading.Event())
        failure = {}

        def opener():
            try:
                self.chat(CLAUDE)
            except Exception as exc:  # noqa: BLE001 — stopping refuses it
                failure["exc"] = exc

        worker = threading.Thread(target=opener, daemon=True)
        worker.start()
        try:
            self.assertTrue(self.claude.start_gate[0].wait(5))
            token = token_of(self.claude.opened[-1].brief)
            self.assertIsNotNone(self.d.callers.resolve(token), "minted while opening")
            self.d.stop()
            self.assertIsNone(self.d.callers.resolve(token),
                              "stop revokes a session still opening")
            self.assertEqual(self.d.callers.live(), 0)
        finally:
            self.claude.start_gate[1].set()
            worker.join(10)
        self.assertIsInstance(failure.get("exc"), daemon_mod.DaemonError)
        self.assertEqual(self.d.callers.live(), 0)

    def test_a_closing_session_is_refused_even_if_its_revoke_was_missed(self):
        thread = self.chat(CLAUDE)
        token = token_of(self.run_brief(thread))
        self.claude.close_gate = (threading.Event(), threading.Event())
        with patch.object(self.d.callers, "revoke", lambda session: None):
            closer = threading.Thread(target=self.d.close_thread, args=(thread.id,), daemon=True)
            closer.start()
            try:
                self.assertTrue(self.claude.close_gate[0].wait(5), "the close is under way")
                self.assertIs(self.d._sessions.get(thread.id).closing, True)
                status, _ = self.call(token)
            finally:
                self.claude.close_gate[1].set()
                closer.join(10)
        self.assertEqual(status, 403, "a closing session answers no call")
        self.assertEqual(CALLS, [])

    def test_the_identity_check_holds_when_a_revoke_is_missed(self):
        thread = self.chat(CLAUDE)
        old = token_of(self.run_brief(thread))
        # A revoke that did not happen: the old token stays in the registry.
        with patch.object(self.d.callers, "revoke", lambda session: None):
            self.d.close_thread(thread.id)
            self.d.resume_thread(thread.id)
        self.assertIsNotNone(self.d.callers.resolve(old), "the missed revoke is real")
        self.assertEqual(self.call(old)[0], 403, "a stale token never reaches a newer session")
        self.assertEqual(self.call(token_of(self.run_brief(thread)))[0], 200)
        self.assertEqual(len(CALLS), 1)

    def test_one_chats_token_never_reaches_another_chats_turn(self):
        a, b = self.chat(CLAUDE), self.chat(CLAUDE)
        token_a = token_of(self.run_brief(a))
        hold, started = threading.Event(), threading.Event()

        def busy(h, m):
            started.set()
            hold.wait(10)

        self.claude.script[b.id] = busy
        turn_b = self.d.send(b.id, UserMessage("wait"))
        self.assertTrue(started.wait(5))
        try:
            self.assertEqual(self.call(token_a)[0], 200)
        finally:
            hold.set()
        self.wait_idle(b.id)
        caller = CALLS[0]["caller"]
        self.assertEqual(caller["thread_id"], a.id)
        self.assertIsNone(caller["turn_id"], "A has no turn running; B's is not A's")
        self.assertNotEqual(caller["turn_id"], turn_b)

    # -- dangerous tools (decisions D-3 detail)

    def test_a_dangerous_tool_asks_the_owner_every_time(self):
        thread = self.chat(CLAUDE)
        token = token_of(self.run_brief(thread))
        # A standing allowlist entry for the tool changes nothing.
        config.ALLOWLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.ALLOWLIST_PATH.write_text(json.dumps([{"tool": SEND}]))
        for decision in (Decision.DENY, Decision.ALLOW):
            with self.subTest(decision=decision.value):
                CALLS.clear()
                self.drain()
                seen = []
                worker = self.resolver(decision, seen, thread.id)
                status, value = self.call(token, SEND, {"to": "bob"})
                worker.join(5)
                self.assertEqual(status, 200, value)
                self.assertEqual(len(seen), 1, "the owner was asked")
                self.assertFalse(seen[0].allowlistable, "no Always for a dangerous Jarvis tool")
                self.assertEqual(seen[0].tool, SEND)
                cards = [e for e in self.drain() if e["kind"] == "approval_requested"]
                self.assertEqual(len(cards), 1, "a card (or DM) went out")
                if decision is Decision.DENY:
                    self.assertEqual(CALLS, [], "a deny runs nothing")
                    self.assertTrue(value["isError"])
                    self.assertTrue(value["text"].startswith("The user declined"))
                else:
                    self.assertEqual([c["tool"] for c in CALLS], [SEND])
                    self.assertEqual(value, {"text": "sent to bob", "isError": False})
        # With nobody answering, the broker times out: denied, nothing ran.
        CALLS.clear()
        with patch.object(self.d.approvals, "timeout_s", 0.2):
            status, value = self.call(token, SEND, {"to": "eve"})
        self.assertTrue(value["isError"])
        self.assertEqual(CALLS, [])

    # -- leaks (§1.7)

    def test_the_token_leaks_nowhere(self):
        capture = Capture()
        root_logger = logging.getLogger()
        level = root_logger.level
        root_logger.addHandler(capture)
        root_logger.setLevel(logging.DEBUG)
        try:
            thread = self.chat(CLAUDE)
            cx = self.chat(CODEX)
            tokens = [token_of(self.run_brief(thread)), token_of(self.run_brief(cx))]
            self.relay_in_turn(thread, [(READ, {"note": "leak"})])
            seen = []
            worker = self.resolver(Decision.DENY, seen, thread.id)
            self.call(tokens[0], SEND, {"to": "x"})
            worker.join(5)
            self.call(tokens[0], arguments={"note": "y"},
                      headers={"Origin": f"http://127.0.0.1:{self.d.port}"})
            self.call("jmcp_" + "Q" * 43)
            routes = [http(self.d.port, "GET", path)[1] for path in
                      ("/threads", f"/threads/{thread.id}/log", f"/threads/{cx.id}/log",
                       "/status", "/approvals", "/activity")]
            self.d.close_thread(thread.id)
            self.d.close_thread(cx.id)
        finally:
            root_logger.removeHandler(capture)
            root_logger.setLevel(level)
        events = self.drain()
        self.assertEqual(self.events.dropped, 0)
        for token in tokens:
            self.assertEqual(files_holding(config.V2_DATA_DIR, token), [],
                             "brief.json, log.jsonl, decisions.jsonl and every other file")
            self.assertNotIn(token, json.dumps(events), "the bus")
            self.assertNotIn(token, "\n".join(capture.lines), "the daemon log at DEBUG")
            self.assertNotIn(token, json.dumps(routes), "route responses")
            self.assertNotIn(token, "".join(self.stderr), "the relay's stderr")
        self.assertTrue((config.V2_DATA_DIR / "decisions.jsonl").exists())
        self.assertTrue(any(e["kind"] == "approval_requested" for e in events))

    def test_nothing_is_minted_while_no_session_tool_exists(self):
        saved = dict(session_tools.SESSION_TOOLS)
        session_tools.SESSION_TOOLS.clear()
        try:
            for provider in (CLAUDE, CODEX):
                thread = self.chat(provider)
                self.assertNotIn("jarvis", self.run_brief(thread).mcp_servers)
            self.assertEqual(self.d.callers.live(), 0)
        finally:
            session_tools.SESSION_TOOLS.update(saved)

    def test_session_tools_ship_empty(self):
        out = subprocess.run(
            [sys.executable, "-c", "from jarvis.v2 import session_tools as s; "
                                   "print(len(s.SESSION_TOOLS), len(s.for_provider('claude')), "
                                   "len(s.for_provider('codex')))"],
            cwd=str(ROOT), env={**os.environ, "PYTHONPATH": str(ROOT)},
            capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.split(), ["0", "0", "0"], out.stderr)


# --- 7.2: task workers never get it -----------------------------------------------


class TaskWorkerChecks(DaemonHarness):
    def task_brief(self, role, task_id, **kw):
        return Brief(role=role, cwd=str(self.root / "project"), task_id=task_id, **kw)

    def test_runner_briefs_never_get_a_jarvis_server(self):
        task = self.stores.tasks.create(self.project.id, "do a thing")
        for provider in (CLAUDE, CODEX):
            for role in (Role.ORCHESTRATOR, Role.IMPLEMENTER, Role.REVIEWER, Role.RESEARCHER):
                with self.subTest(provider=provider.value, role=role.value):
                    thread = self.d.open_thread(self.project.id, role, provider,
                                                self.task_brief(role, task.id))
                    brief = self.run_brief(thread)
                    self.assertNotIn("jarvis", brief.mcp_servers)
                    if provider == CODEX:
                        parsed = tomllib.loads(codex_config.config_text(brief))
                        self.assertNotIn("jarvis", parsed.get("mcp_servers", {}),
                                         "a task thread's Codex config names no jarvis server")
        # A chat-role brief that has a task id is a task's thread.
        thread = self.d.open_thread(self.project.id, Role.CHAT, CLAUDE,
                                    self.task_brief(Role.CHAT, task.id))
        self.assertNotIn("jarvis", self.run_brief(thread).mcp_servers)
        # The fast path runs in-process: no server, no token.
        fast = self.chat(FAST)
        self.assertNotIn("jarvis", self.run_brief(fast).mcp_servers)
        self.assertEqual(self.d.callers.live(), 0)

    def test_open_refuses_a_non_chat_brief_naming_jarvis(self):
        task = self.stores.tasks.create(self.project.id, "do a thing")
        server = {"jarvis": {"command": "/bin/true", "args": []}}
        for role in (Role.IMPLEMENTER, Role.CHAT):
            with self.subTest(role=role.value):
                with self.assertRaises(BriefRefused):
                    self.d.open_thread(self.project.id, role, CLAUDE,
                                       self.task_brief(role, task.id, mcp_servers=server))
        self.assertEqual([o.thread_id for o in self.claude.opened], [],
                         "refused before any provider started")

    def test_a_token_forced_onto_a_task_session_is_refused(self):
        task = self.stores.tasks.create(self.project.id, "do a thing")
        thread = self.d.open_thread(self.project.id, Role.IMPLEMENTER, CLAUDE,
                                    self.task_brief(Role.IMPLEMENTER, task.id))
        forced = self.d.callers.mint(self.d._sessions[thread.id])
        self.assertEqual(self.call(forced)[0], 403)
        self.assertEqual(http(self.d.port, "GET", "/mcp/tools", token=forced)[0], 403)
        self.assertEqual(CALLS, [])


# --- 7.7: the gate ----------------------------------------------------------------


class Asker:
    human_backed = True

    def __init__(self, decision=Decision.DENY):
        self.decision, self.seen = decision, []

    def ask(self, request):
        self.seen.append(request)
        return self.decision


class GateChecks(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="mcp-gate-")))
        patch_config(self.stack, self.root)
        self.cwd = self.root / "project"
        self.cwd.mkdir()

    def brief(self, profile, **kw):
        kw.setdefault("role", Role.CHAT)
        return Brief(cwd=str(self.cwd), profile=profile, **kw)

    def decisions(self):
        path = config.V2_DATA_DIR / "decisions.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_a_read_only_session_tool_is_allowed_at_its_own_layer(self):
        name = session_tools.CLAUDE_PREFIX + READ
        for profile in (PermissionProfile.AUTO, PermissionProfile.ASK):
            with self.subTest(profile=profile.value):
                asker = Asker()
                brief = self.brief(profile)
                permit = build_permit(PermitContext("abcd1234", brief, provider="claude"), asker)
                self.assertEqual(permit(name, {"note": "x"}, brief), Decision.ALLOW)
                self.assertEqual(asker.seen, [], "the broker is never asked")
                row = self.decisions()[-1]
                self.assertEqual((row["tool"], row["layer"], row["decision"]),
                                 (name, "jarvis-tool", "allow"))

    def test_an_always_ask_rule_naming_it_still_asks(self):
        name = session_tools.CLAUDE_PREFIX + READ
        asker = Asker(Decision.DENY)
        brief = self.brief(PermissionProfile.AUTO, always_ask=[name])
        permit = build_permit(PermitContext("abcd1234", brief, provider="claude"), asker)
        self.assertEqual(permit(name, {}, brief), Decision.DENY)
        self.assertEqual(len(asker.seen), 1)
        self.assertEqual(self.decisions()[-1]["layer"], "always-ask")

    def test_the_layer_is_for_an_owners_chat_and_read_only_tools_only(self):
        ask = PermissionProfile.ASK
        cases = [
            ("a task's brief", self.brief(ask, role=Role.IMPLEMENTER, task_id="aaaa1111"), READ),
            ("a chat with a task", self.brief(ask, task_id="aaaa1111"), READ),
            ("a tool that writes", self.brief(ask), SEND),
            ("not a session tool", self.brief(ask), "get_datetime"),
        ]
        for label, brief, tool in cases:
            with self.subTest(case=label):
                asker = Asker(Decision.DENY)
                permit = build_permit(PermitContext("abcd1234", brief, provider="claude"), asker)
                self.assertEqual(permit(session_tools.CLAUDE_PREFIX + tool, {}, brief), Decision.DENY)
                self.assertEqual(len(asker.seen), 1, "asked, as any other tool under ask")
                self.assertNotEqual(self.decisions()[-1]["layer"], "jarvis-tool")

    def test_a_dangerous_jarvis_tool_is_the_owners_every_time(self):
        config.ALLOWLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.ALLOWLIST_PATH.write_text(json.dumps([{"tool": SEND}]))
        brief = self.brief(PermissionProfile.AUTO)
        for decision in (Decision.ALLOW, Decision.DENY):
            asker = Asker(decision)
            permit = build_permit(PermitContext("abcd1234", brief, provider="claude"), asker)
            self.assertEqual(permit(SEND, {"to": "x"}, None, dangerous_tool=True), decision)
            self.assertEqual(len(asker.seen), 1, "AUTO and an allowlist entry still ask")
            self.assertFalse(asker.seen[0].allowlistable)
            self.assertEqual(self.decisions()[-1]["layer"], "human")
        permit = build_permit(PermitContext("abcd1234", brief, provider="claude"), deny_all("x"))
        self.assertEqual(permit(SEND, {"to": "x"}, None, dangerous_tool=True), Decision.DENY)
        strict = self.brief(PermissionProfile.STRICT)
        asker = Asker(Decision.ALLOW)
        permit = build_permit(PermitContext("abcd1234", strict, provider="codex"), asker)
        self.assertEqual(permit(SEND, {"to": "x"}, strict, dangerous_tool=True), Decision.DENY)
        self.assertEqual(asker.seen, [])


class AskProfileChatChecks(DaemonHarness):
    profile = "ask"

    def test_a_read_in_an_ask_profile_chat_raises_no_card(self):
        """What the Claude hook does, done by the fake: `permit` for the tool
        (the daemon's real §6 permit over the run brief), then the call."""
        thread = self.chat(CLAUDE)
        brief = self.run_brief(thread)
        permit = self.claude.opened[-1].permit
        answers = []
        self.drain()

        def action(h, message):
            answers.append(permit(session_tools.CLAUDE_PREFIX + READ, {"note": "q"}, None))
            got, err = run_relay(entry_of(brief), [(READ, {"note": "q"})], cwd=self.root)
            answers.append(got[10]["result"]["content"][0]["text"])

        self.claude.script[thread.id] = action
        self.d.send(thread.id, UserMessage("go"))
        self.wait_idle(thread.id)
        self.assertEqual(answers, [Decision.ALLOW, "PROBE-MARKER q"])
        kinds = [e["kind"] for e in self.drain()]
        self.assertNotIn("approval_requested", kinds)
        row = [json.loads(line) for line in
               (config.V2_DATA_DIR / "decisions.jsonl").read_text().splitlines()][-1]
        self.assertEqual((row["layer"], row["thread_id"]), ("jarvis-tool", thread.id))


# --- the Claude provider: the carrier, the command line, the hook --------------


class ClaudeProviderChecks(unittest.TestCase):
    def setUp(self):
        import claude_provider_check as cl
        from jarvis.v2.providers import claude
        self.cl, self.claude = cl, claude
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="mcp-claude-")))
        patch_config(self.stack, self.root)
        self.cwd = self.root / "work"
        self.cwd.mkdir()
        self.stack.enter_context(cl.fake())
        self.provider = claude.ClaudeProvider()

    def start(self, profile=PermissionProfile.AUTO, permit=None, thread_id="mcpcl001", servers=True):
        brief = Brief(Role.CHAT, str(self.cwd), profile=profile,
                      mcp_servers={"jarvis": bridge.server_entry(CLAUDE, 4242, PLACEHOLDER)}
                      if servers else {})
        handle = self.provider.start(Thread(thread_id, "p", Role.CHAT, CLAUDE), brief,
                                     permit or (lambda *a, **k: Decision.ALLOW))
        self.addCleanup(self.provider.close, handle)
        return handle, brief

    def test_the_command_line_carries_a_path_and_never_the_token(self):
        from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
        handle, _ = self.start()
        options = self.cl.FakeClient.instances[-1].options
        command = SubprocessCLITransport(
            "", replace(options, cli_path="/fake/bin/claude"))._build_command()
        self.assertNotIn(PLACEHOLDER, "\0".join(command), "no token on the command line")
        index = command.index("--mcp-config")
        carrier = Path(command[index + 1])
        self.assertEqual(carrier.parent, self.claude.mcp_config_dir("mcpcl001"))
        self.assertRegex(carrier.name, self.claude.MCP_CONFIG_NAME.pattern,
                         "a file of this session's own")
        self.assertEqual(json.loads(carrier.read_text())["mcpServers"]["jarvis"]["env"]
                         [bridge.TOKEN_VARIABLE], PLACEHOLDER)
        self.assertEqual(carrier.stat().st_mode & 0o777, 0o600)
        self.assertEqual(carrier.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(carrier.parent.parent.stat().st_mode & 0o777, 0o700)
        self.assertIn("--strict-mcp-config", command)
        self.assertNotIn(PLACEHOLDER, json.dumps(options.env), "the CLI's own environment")
        # A model switch reconnects with the same file; close removes it.
        self.provider.set_model(handle, "claude-sonnet-5-5", "high")
        self.assertEqual(self.cl.FakeClient.instances[-1].options.mcp_servers, str(carrier))
        self.assertTrue(carrier.exists())
        self.provider.close(handle)
        self.assertFalse(carrier.exists())

    def test_a_closing_session_never_removes_its_successors_carrier(self):
        """PR #35 review: the daemon drops a lost session under its lock and
        closes it outside; a send meanwhile opens the next session on the same
        thread. The old session's close must remove its own carrier only."""
        entered, release = threading.Event(), threading.Event()
        clients = []
        base = self.cl.FakeClient

        class SlowClose(base):
            async def disconnect(self):
                if self is clients[0]:
                    entered.set()
                    while not release.is_set():
                        await asyncio.sleep(0.01)
                await super().disconnect()

        def factory(options):
            client = SlowClose(options)
            clients.append(client)
            return client

        with patch.object(self.claude, "_client_factory", factory):
            old, _ = self.start(thread_id="mcpcl010")
            old_file = Path(clients[0].options.mcp_servers)
            closer = threading.Thread(target=self.provider.close, args=(old,), daemon=True)
            closer.start()
            try:
                self.assertTrue(entered.wait(5), "the old session's close is under way")
                new, _ = self.start(thread_id="mcpcl010")
                new_file = Path(clients[-1].options.mcp_servers)
                self.assertTrue(new_file.exists())
            finally:
                release.set()
                closer.join(10)
            self.assertFalse(closer.is_alive())
            self.assertTrue(new_file.exists(),
                            "the old session's close removed the new session's carrier")
            self.assertEqual(json.loads(new_file.read_text())["mcpServers"]["jarvis"]["env"]
                             [bridge.TOKEN_VARIABLE], PLACEHOLDER)
            self.assertNotEqual(old_file, new_file, "one carrier per session")
            self.assertFalse(old_file.exists(), "the old session removed its own")
            self.provider.close(new)
            self.assertFalse(new_file.exists())

    def test_a_failed_open_removes_its_carrier(self):
        class Unreachable(self.cl.FakeClient):
            async def connect(self):
                raise RuntimeError("the CLI did not start")

        with patch.object(self.claude, "_client_factory", lambda options: Unreachable(options)):
            with self.assertRaises(BriefRefused):
                self.start(thread_id="mcpcl011")
        folder = config.V2_DATA_DIR / "claude" / "mcpcl011"
        left = list(folder.iterdir()) if folder.exists() else []
        self.assertEqual(left, [], "a session that never opened keeps no carrier")

    def test_a_symlinked_carrier_directory_is_refused(self):
        target = self.root / "elsewhere"
        target.mkdir()
        (config.V2_DATA_DIR / "claude").mkdir(parents=True)
        (config.V2_DATA_DIR / "claude" / "mcpcl009").symlink_to(target, target_is_directory=True)
        with self.assertRaises(BriefRefused):
            self.start(thread_id="mcpcl009")
        self.assertEqual(list(target.iterdir()), [])

    def test_safe_redacts_a_caller_token(self):
        self.assertNotIn(PLACEHOLDER, self.claude._safe(f"mcp env JARVIS_MCP_TOKEN={PLACEHOLDER}"))

    def hook(self, handle, name, agent_id=None):
        options = self.cl.FakeClient.instances[-1].options
        data = {"hook_event_name": "PreToolUse", "tool_name": name, "tool_input": {"note": "x"},
                "tool_use_id": "c1", "session_id": "s", "transcript_path": "", "cwd": str(self.cwd)}
        if agent_id:
            data["agent_id"] = agent_id
        coro = options.hooks["PreToolUse"][0].hooks[0](data, "c1", {"signal": None})
        return self.claude._await(handle.native.loop, coro, 10)

    def real_permit(self, brief, asker):
        permit = build_permit(PermitContext("mcpcl001", brief, provider="claude"), asker)
        calls = []

        def counted(*a, **k):
            calls.append(a)
            return permit(*a, **k)
        return counted, calls

    def test_the_hook_allows_a_read_only_jarvis_tool_explicitly_under_both_profiles(self):
        name = session_tools.CLAUDE_PREFIX + READ
        for profile in (PermissionProfile.AUTO, PermissionProfile.ASK):
            with self.subTest(profile=profile.value):
                asker = Asker(Decision.DENY)
                brief = Brief(Role.CHAT, str(self.cwd), profile=profile)
                permit, calls = self.real_permit(brief, asker)
                handle, _ = self.start(profile, permit, thread_id=f"mcpcl{profile.value[:3]}")
                out = self.hook(handle, name)
                self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "allow")
                self.assertEqual(out["hookSpecificOutput"]["permissionDecisionReason"],
                                 "a read-only Jarvis tool")
                self.assertEqual(len(calls), 1, "permit decided it (its jarvis-tool layer)")
                self.assertEqual(asker.seen, [], "nobody was asked")

    def test_the_explicit_allow_is_exactly_the_read_only_tools(self):
        """Only a read-only session tool gets the hook's explicit allow: a
        tool that writes, ALLOWed by `permit`, keeps today's behaviour — `{}`
        under AUTO (the classifier still decides), "approved by Jarvis" under
        ASK."""
        offered = session_tools.for_provider(CLAUDE)
        self.assertEqual({t.name for t in offered}, {READ, SEND})
        for profile in (PermissionProfile.AUTO, PermissionProfile.ASK):
            handle, _ = self.start(profile, lambda *a, **k: Decision.ALLOW,
                                   thread_id=f"mcpro{profile.value[:3]}")
            for tool in offered:
                with self.subTest(profile=profile.value, tool=tool.name):
                    out = self.hook(handle, session_tools.CLAUDE_PREFIX + tool.name)
                    reason = (out.get("hookSpecificOutput") or {}).get("permissionDecisionReason")
                    if tool.read_only:
                        self.assertEqual(reason, "a read-only Jarvis tool")
                    elif profile is PermissionProfile.AUTO:
                        self.assertEqual(out, {}, "the classifier still decides a writing tool")
                    else:
                        self.assertEqual(reason, "approved by Jarvis")

    def test_the_hook_refuses_a_foreground_tool_for_a_sub_agent_without_permit(self):
        asker = Asker(Decision.ALLOW)
        brief = Brief(Role.CHAT, str(self.cwd))
        permit, calls = self.real_permit(brief, asker)
        handle, _ = self.start(permit=permit)
        out = self.hook(handle, session_tools.CLAUDE_PREFIX + READ, agent_id="sub-1")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("sub-agent", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(calls, [], "permit is never asked")
        # The same tool from the conversation itself is allowed.
        self.assertEqual(self.hook(handle, session_tools.CLAUDE_PREFIX + READ)
                         ["hookSpecificOutput"]["permissionDecision"], "allow")

    def test_no_explicit_allow_without_a_jarvis_server(self):
        handle, _ = self.start(servers=False, thread_id="mcpcl002")
        self.assertEqual(self.hook(handle, session_tools.CLAUDE_PREFIX + READ), {},
                         "AUTO and no jarvis server: the classifier still decides")


# --- the Codex provider: the config carrier and declining MCP approvals ---------


class CodexProviderChecks(unittest.TestCase):
    def setUp(self):
        import codex_provider_check as cpc
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="mcp-codex-")))
        self.auth = self.root / "owner"
        self.auth.mkdir()
        (self.auth / "auth.json").write_text("FAKE AUTH: never read")
        self.work = self.root / "work"
        self.work.mkdir()
        self.brain = cpc.Brain(self.root)
        self.stack.enter_context(patch.object(config, "V2_DATA_DIR", self.root / "data"))
        self.stack.enter_context(patch.object(codex_config, "owner_home", return_value=self.auth))
        self.stack.enter_context(patch.object(codex, "RpcProcess", self.brain.rpc))
        self.stack.enter_context(patch.object(codex.CodexProvider, "_probe",
                                              return_value=("/fake/codex", "fake")))
        self.provider = codex.CodexProvider()
        self.handles = []
        self.addCleanup(lambda: [self.provider.close(h) for h in self.handles])
        self.brief = Brief(Role.CHAT, str(self.work), model="fake-model", effort="high",
                           mcp_servers={"jarvis": bridge.server_entry(CODEX, 4242, PLACEHOLDER)})

    def start(self, mode="normal", thread_id="mcpcx001", **script):
        self.brain.script.update(mode=mode, **script)
        h = self.provider.start(Thread(thread_id, "p", Role.CHAT, CODEX), self.brief,
                                lambda *a, **k: Decision.ALLOW)
        self.handles.append(h)
        return h

    def test_a_failed_open_strips_its_config(self):
        """`prepare` writes the config (token and all) before the app-server
        is asked anything; an open that then fails keeps no token on disk."""
        self.brain.script["account"] = "apiKey"     # refused after the config is written
        with self.assertRaises(BriefRefused):
            self.start(thread_id="mcpcx009")
        path = config.V2_DATA_DIR / "codex" / "mcpcx009" / "config.toml"
        self.assertTrue(self.brain.calls("account/read"), "it got as far as the app-server")
        if path.exists():
            text = path.read_text()
            self.assertNotIn(PLACEHOLDER, text)
            self.assertNotIn("mcp_servers", tomllib.loads(text))

    def test_the_token_is_in_the_private_config_only_and_leaves_on_close(self):
        h = self.start()
        home = h.native.home
        path = home / "config.toml"
        server = tomllib.loads(path.read_text())["mcp_servers"]["jarvis"]
        self.assertEqual(server["env"][bridge.TOKEN_VARIABLE], PLACEHOLDER)
        self.assertEqual(server["tools"], {READ: {"approval_mode": "approve"}})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(home.stat().st_mode & 0o777, 0o700)
        rpc = h.native.rpc
        self.assertNotIn(PLACEHOLDER, "\0".join(rpc.original_argv), "the app-server command line")
        self.assertNotIn(PLACEHOLDER, json.dumps(rpc.env), "the app-server environment")
        self.provider.close(h)
        text = path.read_text()
        self.assertNotIn(PLACEHOLDER, text)
        self.assertNotIn("mcp_servers", tomllib.loads(text))
        self.assertEqual(tomllib.loads(text)["model"], "fake-model", "the rest is kept")

    def scripted(self, method, params, *, no_turn=False, thread_id="mcpcx002"):
        h = self.start("scripted", thread_id=thread_id, method=method, params=params,
                       item=None, no_turn=no_turn)
        events: list = []

        def run():
            events.extend(self.provider.send(h, UserMessage("go")))

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(15)
        if worker.is_alive():
            # A request put to the owner as a question waits for an answer
            # forever: that is this check failing, not hanging.
            self.provider.close(h)
            worker.join(5)
            self.fail("the turn waited on the owner: the request became a question")
        replies = [m for m in h.native.rpc.replies if m["id"] == "special"]
        self.assertEqual(len(replies), 1, replies)
        return events, replies[0]

    def assert_declined(self, events, reply):
        self.assertNotIn(K.QUESTION, [e.kind for e in events], "never a question")
        errors = [e for e in events if e.kind == K.ERROR]
        self.assertEqual(len(errors), 1)
        self.assertIs(errors[0].data["fatal"], False)
        self.assertEqual(errors[0].data["message"], codex.MCP_DECLINED)
        self.assertEqual(events[-1].kind, K.TURN_FINISHED)
        self.assertEqual(events[-1].data["stop"], "end")
        self.assertNotIn("accept", json.dumps(reply))

    def test_an_elicitation_is_declined_without_asking(self):
        params = {"serverName": "jarvis", "mode": "form", "message": "Allow mcp_probe_read?",
                  "requestedSchema": {"type": "object", "properties": {}},
                  "_meta": {"codex_approval_kind": "mcp_tool_call"}}
        for no_turn in (True, False):
            with self.subTest(no_turn=no_turn):
                events, reply = self.scripted(codex.MCP_ELICITATION, params, no_turn=no_turn,
                                              thread_id=f"mcpcx0{int(no_turn)}e")
                self.assertEqual(reply["result"], {"action": "decline", "content": None})
                self.assert_declined(events, reply)

    def test_an_mcp_approval_question_is_declined_not_asked(self):
        shapes = {
            "question id": {"itemId": "q", "questions": [
                {"id": "mcp_tool_call_approval_call-7", "header": "Approve",
                 "question": "Allow jarvis to run mcp_probe_read?",
                 "options": [{"label": "Allow"}, {"label": "Cancel"}]}]},
            "metadata": {"itemId": "q", "_meta": {"codex_approval_kind": "mcp_tool_call"},
                         "questions": [{"id": "q1", "question": "Run the tool?",
                                        "options": [{"label": "Run the tool and continue"}]}]},
        }
        for label, params in shapes.items():
            with self.subTest(shape=label):
                events, reply = self.scripted("item/tool/requestUserInput", params,
                                              thread_id="mcpcx" + label[:3].replace(" ", ""))
                self.assertIsNone(reply["result"])
                self.assertIn("error", reply)
                self.assert_declined(events, reply)


# --- the start-up sweep (§1.3) --------------------------------------------------


class SweepChecks(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="mcp-sweep-")))
        patch_config(self.stack, self.root)

    def leave_carriers(self, data: Path):
        """What a crash leaves: a session carrier (`mcp-<nonce>.json`), the
        first round's per-thread `mcp.json`, a write cut short (`.mcp-*`), and
        a Codex config with the jarvis server. Plus one file that is not a
        carrier, which must survive. -> (carriers, codex config, bystander)."""
        folder = data / "claude" / "deadbeef"
        folder.mkdir(parents=True)
        body = json.dumps({"mcpServers": {"jarvis": {"env": {"T": PLACEHOLDER}}}})
        carriers = [folder / f"mcp-{'ab' * 16}.json", folder / "mcp.json", folder / ".mcp-x1y2"]
        for carrier in carriers:
            carrier.write_text(body)
        bystander = folder / "notes.txt"
        bystander.write_text("not a carrier")
        home = data / "codex" / "cafebabe"
        home.mkdir(parents=True)
        brief = Brief(Role.CHAT, str(self.root), model="m",
                      mcp_servers={"jarvis": bridge.server_entry(CODEX, 4242, PLACEHOLDER)})
        (home / "config.toml").write_text(codex_config.config_text(brief))
        return carriers, home / "config.toml", bystander

    def daemon(self, stores, port=0, face_port=None, workshop_port=None):
        d = daemon_mod.Daemon(stores, {FAST: FakeCli(FAST)}, None, port,
                              face_port=face_port, workshop_port=workshop_port)
        d.start()
        self.addCleanup(d.stop)
        return d

    def test_a_daemon_sweeps_its_own_data_folder(self):
        carriers, toml, bystander = self.leave_carriers(config.V2_DATA_DIR)
        self.daemon(Stores())
        for carrier in carriers:
            self.assertFalse(carrier.exists(), carrier.name)
        self.assertTrue(bystander.exists(), "only carriers are removed")
        self.assertNotIn(PLACEHOLDER, toml.read_text())
        self.assertEqual(tomllib.loads(toml.read_text())["model"], "m")

    def test_a_daemon_on_another_folder_touches_nothing(self):
        carriers, toml, _ = self.leave_carriers(config.V2_DATA_DIR)
        self.daemon(Stores(self.root / "elsewhere"))
        self.assertTrue(all(carrier.exists() for carrier in carriers))
        self.assertIn(PLACEHOLDER, toml.read_text())

    def test_a_second_daemon_that_cannot_bind_sweeps_nothing(self):
        """PR #35 review: the sweep used to run before the bind, so a second
        daemon started by hand on the live data folder deleted the live one's
        carriers and only then failed with "port is already in use". The bind
        is the single-instance lock; the sweep comes after it."""
        live = self.daemon(Stores())
        carriers, toml, _ = self.leave_carriers(config.V2_DATA_DIR)
        second = daemon_mod.Daemon(Stores(), {FAST: FakeCli(FAST)}, None, live.port,
                                   face_port=0, workshop_port=0)
        with self.assertRaises(daemon_mod.DaemonError):
            second.start()
        self.assertTrue(all(carrier.exists() for carrier in carriers),
                        "the live daemon's carriers are untouched")
        self.assertIn(PLACEHOLDER, toml.read_text())

    def test_log_paths_keep_no_query_on_mcp_routes(self):
        self.assertEqual(daemon_mod._log_path("/mcp/call?token=abc"), "/mcp/call")
        self.assertEqual(daemon_mod._log_path("/threads?project=ab12cd34"),
                         "/threads?project=ab12cd34")


# --- the relay on its own ---------------------------------------------------------


class RelayChecks(unittest.TestCase):
    def test_unconnected_the_relay_lists_nothing_and_errors_every_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            # A port nothing listens on (bound, then closed).
            import socket
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            entry = bridge.server_entry(CLAUDE, port, PLACEHOLDER)
            got, err = run_relay(entry, [(READ, {})], cwd=Path(tmp))
        self.assertEqual(got[1]["result"]["capabilities"], {"tools": {}})
        self.assertEqual(got[2]["result"], {"tools": []})
        self.assertEqual(got[10]["result"], {"content": [{"type": "text",
                                                          "text": mcp_session.NOT_CONNECTED}],
                                             "isError": True})
        self.assertEqual(err, "")

    def test_the_relay_drops_the_token_from_its_environment(self):
        read, write = os.pipe()
        code = ("import os; from jarvis.v2 import mcp_session as m; m.main(); "
                f"os.write({write}, b'still' if 'JARVIS_MCP_TOKEN' in os.environ else b'gone')")
        env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT),
               "JARVIS_MCP_TOKEN": PLACEHOLDER, "JARVIS_DAEMON_PORT": "9"}
        try:
            out = subprocess.run([sys.executable, "-c", code], env=env, input="",
                                 capture_output=True, text=True, timeout=60, pass_fds=(write,))
            os.close(write)
            write = None
            said = os.read(read, 16)
        finally:
            os.close(read)
            if write is not None:
                os.close(write)
        self.assertEqual(said, b"gone")
        self.assertNotIn(PLACEHOLDER, out.stderr + out.stdout)

    def test_the_relay_is_stdlib_only_and_never_dispatches(self):
        source = (ROOT / "jarvis" / "v2" / "mcp_session.py").read_text()
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, "no relative imports")
                imported.add((node.module or "").split(".")[0])
        imported.discard("__future__")
        self.assertEqual(imported - set(sys.stdlib_module_names), set(),
                         "nothing but the standard library")
        self.assertNotIn("dispatch(", source)
        self.assertNotIn("jarvis.tools", source.split('"""', 2)[-1])
        bridge_source = (ROOT / "jarvis" / "v2" / "bridge.py").read_text()
        self.assertEqual(bridge_source.count("tools.dispatch("), 1, "one dispatch, in the bridge")
        for name in ("bridge.py", "callers.py", "session_tools.py", "mcp_session.py"):
            text = (ROOT / "jarvis" / "v2" / name).read_text()
            for forbidden in ("terminals", "terminal_guard", "terminal_text", "credential_patterns"):
                self.assertNotIn(f"import {forbidden}", text)
                self.assertNotIn(f".{forbidden} import", text)

    def test_the_new_modules_are_self_protected(self):
        from jarvis.tools.files import SELF_PROTECTED
        for name in ("callers.py", "bridge.py", "session_tools.py", "mcp_session.py"):
            self.assertIn(f"jarvis/v2/{name}", SELF_PROTECTED)


# --- the fast path binds the caller -----------------------------------------------


class FastPathCallerChecks(DaemonHarness):
    def test_the_fast_path_binds_the_caller_for_its_turn(self):
        from jarvis.v2.providers.fastpath import FastPathProvider
        d = self.make_daemon(providers={FAST: FastPathProvider()})
        thread = d.open_thread(self.project.id, Role.CHAT, FAST, {})
        seen = []

        def run_turn(text, images=None):
            seen.append(runtime.caller())
            return SimpleNamespace(cost_usd=0.0, cancelled=False, stopped_early=False, text="ok")

        d._sessions[thread.id].handle.native.agent.run_turn = run_turn
        turn_id = d.send(thread.id, UserMessage("hello"))
        self.wait_idle(thread.id, d=d)
        self.assertEqual(seen, [{"thread_id": thread.id, "project_id": self.project.id,
                                 "provider": "fast", "turn_id": turn_id}])
        self.assertIsNone(runtime.caller(), "nothing leaks into the test's own context")


if __name__ == "__main__":
    unittest.main(verbosity=2)
