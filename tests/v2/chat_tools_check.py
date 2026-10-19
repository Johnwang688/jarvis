"""C1 of the chat-to-chat plan: the four read tools on all three providers.

Plan `docs/plans/2026-10-19-chat-to-chat-plan.md` §4.1, §4.2 and the C1 test
list; the decisions file beside it (CC-8: `jarvis/v2/tools/chats.py` and
`jarvis/v2/chat_relay.py` join SELF_PROTECTED). Free: no CLI, no network, no
key. Every daemon here runs on port 0 over a temporary V2_DATA_DIR and
temporary config; the Claude and Codex chats are scripted fakes, and the fast
path is the real `FastPathProvider` with `llm.chat` scripted.

What it pins, each with the mutation it is meant to catch:

- the schemas are generated from `Annotated` hints (invariant 6), exactly the
  parameters §4.1 names [a hand-written schema, a renamed or untyped param];
- the tools are EXPLICIT_ONLY (no v1 surface holds them), PRIVATE_RESULT,
  PARALLEL_SAFE and not dangerous [left out of any of those sets];
- SELF_PROTECTED holds `jarvis/v2/tools/chats.py` and `jarvis/v2/chat_relay.py`
  (CC-8) [either entry left out];
- no caller, or no installed service, is a refusal, and a stopped daemon
  uninstalls its service [no caller check; no service check; stop() leaves
  it installed];
- cross-provider reads through the real bridge: a Claude chat reads a fast
  chat and a Codex chat, a Codex chat reads a Claude chat [not offered to a
  provider; read the wrong thread];
- the tool summary in the reader's own `log.jsonl` (and on the bus) is the
  fixed word `read` for all three providers, never the read text [the
  hard-coded private-name lists in claude.py/codex.py left as they are];
- the HUD label of a chat_read row names the target chat (`"chat"` in
  `tool_calls._KEYS`) [key not added].

The tool signatures assumed (plan §4.1):

    chat_list(query: Annotated[str, ...] = "", limit: Annotated[int, ...] = 20) -> str
    chat_summary(chat: Annotated[str, ...]) -> str
    chat_search(query: Annotated[str, ...], chat: Annotated[str, ...] = "",
                limit: Annotated[int, ...] = 20) -> str
    chat_read(chat: Annotated[str, ...], last: Annotated[int, ...] = 20) -> str

Run: PYTHONPATH=. .venv/bin/python tests/v2/chat_tools_check.py
"""
from __future__ import annotations

import contextlib
import contextvars
import inspect
import json
import queue
import secrets
import sys
import tempfile
import threading
import time
import typing
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import mcp_session_check as M  # noqa: E402  (helpers only; not its setUpModule)
from jarvis import config, llm, runtime, tools  # noqa: E402
from jarvis.agent import CONTEXT_BLOCK_PREFIX  # noqa: E402
from jarvis.v2 import daemon as daemon_mod  # noqa: E402
from jarvis.v2 import session_tools  # noqa: E402
from jarvis.v2.approvals import PendingApprovals  # noqa: E402
from jarvis.v2.model import ProviderName, Role  # noqa: E402
from jarvis.v2.provider import Event, EventKind as K, SessionHandle, UserMessage  # noqa: E402
from jarvis.v2.providers.fastpath import FastPathProvider  # noqa: E402
from jarvis.v2.stores import Stores  # noqa: E402

CLAUDE, CODEX, FAST = ProviderName.CLAUDE, ProviderName.CODEX, ProviderName.FAST
READS = ("chat_list", "chat_summary", "chat_search", "chat_read")
REFUSAL = "Error: chats can be read only inside a Jarvis HUD chat"
FENCE = "[untrusted chat text "
CHAT_DESC = 'Chat id from chat_list, or its exact title ("Project / Title" when titles repeat)'

# §4.1, parameter by parameter: (name, JSON type, default or REQUIRED, description).
REQUIRED = object()
SCHEMAS = {
    "chat_list": [
        ("query", "string", "", "Words to match in a chat's title or project (default: every open chat)"),
        ("limit", "integer", 20, "How many chats to list, most recently active first (default 20, at most 50)"),
    ],
    "chat_summary": [
        ("chat", "string", REQUIRED, CHAT_DESC),
    ],
    "chat_search": [
        ("query", "string", REQUIRED, "Word or phrase to look for in what the owner and the chats have said"),
        ("chat", "string", "", "Search only this chat: its id or exact title (default: every open chat)"),
        ("limit", "integer", 20, "Most matching lines to return (default 20, at most 50)"),
    ],
    "chat_read": [
        ("chat", "string", REQUIRED, CHAT_DESC),
        ("last", "integer", 20, "How many of its latest messages to read (default 20, at most 100)"),
    ],
}


def chats_module():
    """The C1 tool module. Imported per test, so a tree without it fails
    each test on its own, for that reason, rather than the whole file."""
    from jarvis.v2.tools import chats
    return chats


def mark(tag: str) -> str:
    return f"{tag}-MARK-{secrets.token_hex(4)}"


# --- the tools themselves -----------------------------------------------------------


class SchemaChecks(unittest.TestCase):
    def test_schemas_are_generated_from_annotated_hints(self):
        chats = chats_module()
        for name, params in SCHEMAS.items():
            with self.subTest(tool=name):
                self.assertIn(name, tools.REGISTRY)
                entry = tools.REGISTRY[name]
                func = getattr(chats, name)
                self.assertIs(entry.func, func, "registered by @tool from tools/chats.py")
                hints = typing.get_type_hints(func, include_extras=True)
                signature = inspect.signature(func)
                self.assertEqual(list(signature.parameters), [p[0] for p in params])
                for pname, jtype, default, description in params:
                    hint = hints[pname]
                    self.assertIs(typing.get_origin(hint), typing.Annotated,
                                  f"{name}.{pname} is Annotated (invariant 6)")
                    self.assertEqual(tools._json_type(hint),
                                     {"type": jtype, "description": description})
                    actual = signature.parameters[pname].default
                    if default is REQUIRED:
                        self.assertIs(actual, inspect.Parameter.empty, f"{name}.{pname} is required")
                    else:
                        self.assertEqual(actual, default)
                expected = {
                    "type": "object",
                    "properties": {p: {"type": t, "description": d} for p, t, _, d in params},
                    "required": [p for p, _, default, _ in params if default is REQUIRED],
                    "additionalProperties": False,
                }
                self.assertEqual(entry.schema, expected, "the schema is the hints', not hand-written")
                self.assertTrue(entry.description.strip(), "a docstring describes it")
                self.assertFalse(entry.dangerous)

    def test_flags_explicit_only_private_and_parallel_safe(self):
        chats_module()
        defaults = set(tools.default_names())
        for name in READS:
            with self.subTest(tool=name):
                self.assertIn(name, tools.EXPLICIT_ONLY, "no v1 surface holds it")
                self.assertNotIn(name, defaults)
                self.assertIn(name, tools.PRIVATE_RESULT, "summaries are fixed words")
                self.assertIn(name, tools.PARALLEL_SAFE)
                self.assertTrue(tools.parallelizable(name))
        secret = mark("PRIVATE")
        fenced = f"{FENCE}abc from HUD chat aaaa0001 \"x\" (Claude, project p)]\n{secret}\n[end of chat text abc]"
        for name in READS:
            self.assertEqual(tools.private_summary(name, fenced), "read")
            self.assertEqual(tools.private_summary(name, REFUSAL + "."), "error")

    def test_self_protected_holds_both_new_modules(self):
        from jarvis.tools.files import SELF_PROTECTED
        for rel in ("jarvis/v2/tools/chats.py", "jarvis/v2/chat_relay.py"):
            with self.subTest(rel=rel):
                self.assertTrue((ROOT / rel).is_file(), f"{rel} exists")
                self.assertIn(rel, SELF_PROTECTED, "CC-8")

    def test_the_hud_label_names_the_target_chat(self):
        from jarvis.v2 import tool_calls
        self.assertEqual(tool_calls._KEYS[-1], "chat", "appended, so no older row's label moves")
        self.assertIn("a1b2c3d4", tool_calls.label("chat_read", {"chat": "a1b2c3d4", "last": 5}, ""))
        self.assertIn("a1b2c3d4", tool_calls.label("chat_summary", {"chat": "a1b2c3d4"}, ""))
        # chat_search keeps showing its query.
        self.assertIn("deploy", tool_calls.label("chat_search", {"query": "deploy", "chat": "a1b2c3d4"}, ""))


# --- the daemon harness --------------------------------------------------------------


class ScriptedCli(M.FakeCli):
    """A Claude or Codex chat whose turn runs `script[thread_id](h, message)`
    — which may return extra events, emitted before its answer — and answers
    `texts[thread_id]`."""

    def __init__(self, name):
        super().__init__(name)
        self.texts: dict[str, str] = {}

    def send(self, h, message):
        yield Event(K.TURN_STARTED, h.thread_id)
        action = self.script.get(h.thread_id)
        extra = action(h, message) if action is not None else None
        for event in extra or ():
            yield event
        yield Event(K.TEXT, h.thread_id, {"text": self.texts.get(h.thread_id, "done")})
        yield Event(K.TURN_FINISHED, h.thread_id, {"stop": "end"})


def _reply(text="", calls=None):
    message: dict = {"content": text or None}
    if calls:
        message["tool_calls"] = [{"id": f"c{i}", "type": "function",
                                  "function": {"name": n, "arguments": json.dumps(a)}}
                                 for i, (n, a) in enumerate(calls)]
    return llm.Reply(message=message, finish_reason="tool_calls" if calls else "stop",
                     model="stub", latency_s=0.0, cost_usd=0.0)


class Harness(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="chat-tools-")))
        M.patch_config(self.stack, self.root)
        for name, value in dict(SKILLS_DIR=self.root / "skills", SPILL_DIR=self.root / "spill",
                                MEMORY_DIR=self.root / "memory",
                                MODEL_CACHE_PATH=self.root / "cfg" / "catalog.json",
                                AVATAR_ENV="jarvis").items():
            self.stack.enter_context(patch.object(config, name, value))
        (self.root / "skills").mkdir()
        (self.root / "project").mkdir()
        # The production catalog: the chat reads are offered as they ship.
        self.assertTrue(session_tools.SESSION_TOOLS, "the production session catalog is loaded")
        self.llm = []                 # what each fast-path request saw
        self.fast_script = None       # (model, messages, tools) -> Reply
        real_chat = llm.chat
        llm.chat = self._chat
        self.addCleanup(setattr, llm, "chat", real_chat)
        self.stores = Stores()
        self.claude, self.codex = ScriptedCli(CLAUDE), ScriptedCli(CODEX)
        self.d = self.make_daemon()
        self.bus = self.d.bus.subscribe()
        self.project = self.stores.projects.create("School", str(self.root / "project"))

    def _chat(self, model, messages, tools=None, on_delta=None, **kw):
        if tools is None:
            return _reply("a title")
        self.llm.append({"names": [t["function"]["name"] for t in tools], "messages": list(messages)})
        if self.fast_script is not None:
            return self.fast_script(model, messages, tools)
        return _reply("done")

    def make_daemon(self):
        holder = {}
        approvals = PendingApprovals(
            timeout_s=5.0,
            on_request=lambda request: holder["d"]._approval_requested(request),
            on_resolve=lambda request, decision, resolution:
                holder["d"]._approval_resolved(request, decision, resolution))
        d = daemon_mod.Daemon(self.stores, {CLAUDE: self.claude, CODEX: self.codex,
                                            FAST: FastPathProvider()}, None, 0, approvals)
        holder["d"] = d
        d.start()
        self.addCleanup(d.stop)
        return d

    def chat(self, provider):
        return self.d.open_thread(self.project.id, Role.CHAT, provider, {})

    def token(self, thread):
        fake = {CLAUDE: self.claude, CODEX: self.codex}[ProviderName(thread.provider)]
        brief = [o for o in fake.opened if o.thread_id == thread.id][-1].brief
        return M.token_of(brief)

    def wait_idle(self, thread_id, timeout=30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.d._lock:
                session = self.d._sessions.get(thread_id)
                if session is None or session.worker is None:
                    return
            time.sleep(0.01)
        self.fail("turn did not finish")

    def turn(self, thread, text):
        self.d.send(thread.id, UserMessage(text))
        self.wait_idle(thread.id)

    def call(self, thread, name, arguments):
        """One bridge call with this chat's own caller token: (status, body)."""
        return M.http(self.d.port, "POST", "/mcp/call", token=self.token(thread),
                      body={"name": name, "arguments": arguments})

    def answer_fast(self, text):
        self.fast_script = lambda model, messages, tools: _reply(text)

    def populated(self):
        """A fast, a Claude and a Codex chat, each with one owner message and
        one answer carrying its own marker."""
        out = {}
        for provider in (FAST, CLAUDE, CODEX):
            thread = self.chat(provider)
            owner, answer = mark(provider.value + "-OWNER"), mark(provider.value + "-ANSWER")
            if provider is FAST:
                self.answer_fast(answer)
            else:
                {CLAUDE: self.claude, CODEX: self.codex}[provider].texts[thread.id] = answer
            self.turn(thread, owner)
            out[provider] = SimpleNamespace(thread=thread, owner=owner, answer=answer)
        self.fast_script = None
        return out

    def log_text(self, thread):
        return (config.V2_DATA_DIR / "threads" / thread.id / "log.jsonl").read_text()

    def log_records(self, thread):
        return [json.loads(line) for line in self.log_text(thread).splitlines() if line.strip()]

    def bus_records(self):
        out = []
        while True:
            try:
                out.append(self.bus.get_nowait())
            except queue.Empty:
                return out


# --- refusals ------------------------------------------------------------------------


def _args(name, chat_id):
    return {"chat_list": {}, "chat_summary": {"chat": chat_id},
            "chat_search": {"query": "MARK"}, "chat_read": {"chat": chat_id}}[name]


class RefusalChecks(Harness):
    def run_tool(self, name, arguments, caller=None):
        chats = chats_module()

        def run():
            if caller is not None:
                runtime.bind_caller(*caller)
            return getattr(chats, name)(**arguments)
        return contextvars.Context().run(run)

    def test_no_caller_is_refused_with_the_service_installed(self):
        rows = self.populated()
        target = rows[CLAUDE].thread
        for name in READS:
            with self.subTest(tool=name):
                out = self.run_tool(name, _args(name, target.id))
                self.assertTrue(str(out).startswith(REFUSAL), out)
                self.assertNotIn(rows[CLAUDE].answer, str(out))
        # The control: the same calls with a caller bound do read.
        caller = (rows[CODEX].thread.id, self.project.id, "codex", None)
        read = self.run_tool("chat_read", {"chat": target.id}, caller)
        self.assertTrue(read.startswith(FENCE), read)
        self.assertIn(rows[CLAUDE].answer, read)
        listed = self.run_tool("chat_list", {}, caller)
        self.assertIn(target.id, listed)

    def test_no_service_is_refused_with_a_caller_bound(self):
        rows = self.populated()
        caller = (rows[CODEX].thread.id, self.project.id, "codex", None)
        self.assertTrue(self.run_tool("chat_list", {}, caller).startswith(FENCE),
                        "installed while the daemon runs")
        self.d.stop()      # uninstalls the service
        for name in READS:
            with self.subTest(tool=name):
                out = self.run_tool(name, _args(name, rows[CLAUDE].thread.id), caller)
                self.assertTrue(str(out).startswith(REFUSAL), out)
                self.assertNotIn(rows[CLAUDE].answer, str(out))


class NoDaemonRefusal(unittest.TestCase):
    def test_a_bound_caller_with_no_daemon_ever_started_is_refused(self):
        chats = chats_module()
        for name in READS:
            with self.subTest(tool=name):
                def run():
                    runtime.bind_caller("aaaa0001", "p1", "claude", "turn")
                    return getattr(chats, name)(**_args(name, "aaaa0002"))
                out = contextvars.Context().run(run)
                self.assertTrue(str(out).startswith(REFUSAL), out)


# --- cross-provider reads through the real bridge ---------------------------------------


class CrossProviderReads(Harness):
    def setUp(self):
        super().setUp()
        self.rows = self.populated()

    def read(self, reader, name, arguments):
        status, body = self.call(reader, name, arguments)
        self.assertEqual(status, 200, body)
        self.assertFalse(body["isError"], body)
        return body["text"]

    def test_claude_reads_a_fast_chat_and_a_codex_chat(self):
        claude = self.rows[CLAUDE].thread
        for provider in (FAST, CODEX):
            with self.subTest(target=provider.value):
                row = self.rows[provider]
                text = self.read(claude, "chat_read", {"chat": row.thread.id})
                self.assertTrue(text.startswith(FENCE), text[:120])
                self.assertIn(row.thread.id, text)
                self.assertIn(row.owner, text)
                self.assertIn(row.answer, text)
                other = self.rows[CODEX if provider is FAST else FAST]
                self.assertNotIn(other.answer, text, "only the chat it named")
                summary = self.read(claude, "chat_summary", {"chat": row.thread.id})
                self.assertIn(row.answer, summary)
        listed = self.read(claude, "chat_list", {})
        for provider in (FAST, CLAUDE, CODEX):
            self.assertIn(self.rows[provider].thread.id, listed)
        self.assertIn("(this chat)", listed)
        found = self.read(claude, "chat_search", {"query": "ANSWER-MARK"})
        self.assertIn(self.rows[FAST].answer, found)
        self.assertIn(self.rows[CODEX].answer, found)

    def test_codex_reads_a_claude_chat(self):
        codex, row = self.rows[CODEX].thread, self.rows[CLAUDE]
        text = self.read(codex, "chat_read", {"chat": row.thread.id})
        self.assertTrue(text.startswith(FENCE), text[:120])
        self.assertIn(row.owner, text)
        self.assertIn(row.answer, text)
        self.assertNotIn(self.rows[FAST].answer, text)
        found = self.read(codex, "chat_search", {"query": "ANSWER-MARK", "chat": row.thread.id})
        self.assertIn(row.answer, found)
        self.assertNotIn(self.rows[FAST].answer, found)


# --- private summaries in the reader's own log ------------------------------------------


def codex_native(thread_id):
    native = SimpleNamespace(thread_id="native-parent", turn_id="native-turn", mcp_calls=set(),
                             mcp_items={}, items={}, mutex=threading.RLock(),
                             closed=threading.Event(), cancelled=threading.Event(),
                             sending=threading.Lock())
    native.sending.acquire()
    return SessionHandle(thread_id, CODEX, "codex:native-parent", native)


class PrivateSummaries(Harness):
    """Each provider's reader turn reads a chat whose answer is a marker. The
    read reaches the model; the reader's `log.jsonl` and the bus carry only
    the fixed summary `read`."""

    def setUp(self):
        super().setUp()
        self.rows = self.populated()
        self.bus_records()

    def assert_private(self, reader, results, secret, names):
        self.assertTrue(results and all(secret in r for r in results),
                        f"the read reached the model: {[r[:160] for r in results]}")
        records = self.log_records(reader)
        finished = [r for r in records if r.get("kind") == "tool_finished"]
        self.assertEqual(sorted(r["data"]["name"] for r in finished), sorted(names), finished)
        for record in finished:
            self.assertEqual(record["data"]["summary"], "read", record)
            self.assertEqual(record["data"]["outcome"], "ok", record)
        self.assertNotIn(secret, self.log_text(reader), "the read text stays out of the reader's log")
        published = [r for r in self.bus_records()
                     if r.get("kind") == "tool_finished" and r.get("thread_id") == reader.id]
        self.assertEqual(len(published), len(names), published)
        self.assertTrue(all(r["data"]["summary"] == "read" for r in published), published)
        self.assertNotIn(secret, json.dumps(published))

    def test_fast_path_reads_are_logged_as_read(self):
        reader, target = self.rows[FAST].thread, self.rows[CLAUDE]

        def script(model, messages, tools):
            real = [m for m in messages if not (m.get("role") == "user" and
                                                str(m.get("content", "")).startswith(CONTEXT_BLOCK_PREFIX))]
            if real[-1].get("role") != "tool":
                return _reply(calls=[("chat_read", {"chat": target.thread.id}),
                                     ("chat_summary", {"chat": target.thread.id}),
                                     ("chat_search", {"query": "ANSWER-MARK", "chat": target.thread.id})])
            return _reply("done")
        self.fast_script = script
        self.turn(reader, "what is the claude chat doing?")
        held = set(self.llm[-1]["names"])
        self.assertTrue(set(READS) <= held, sorted(held))
        results = [m["content"] for m in self.llm[-1]["messages"] if m.get("role") == "tool"]
        self.assert_private(reader, results, target.answer, ["chat_read", "chat_summary", "chat_search"])

    def test_claude_reads_are_logged_as_read(self):
        from jarvis.v2.providers import claude
        reader, target = self.rows[CLAUDE].thread, self.rows[FAST]
        results = []

        def action(h, message):
            status, body = self.call(reader, "chat_read", {"chat": target.thread.id})
            text = body.get("text", "") if isinstance(body, dict) else str(body)
            results.append(text if status == 200 else f"HTTP {status}: {text}")
            name = "mcp__jarvis__chat_read"
            started = Event(K.TOOL_STARTED, h.thread_id,
                            {"call_id": "c1", "name": name, "args": {"chat": target.thread.id}})
            session = SimpleNamespace(tools_seen={"c1": (name, {"chat": target.thread.id})},
                                      denied_tools=set(), denial_without_id=False,
                                      thread_id=h.thread_id)
            block = claude.ToolResultBlock(tool_use_id="c1", content=text, is_error=False)
            return [started, *claude.ClaudeProvider()._tool_result(session, block)]
        self.claude.script[reader.id] = action
        self.turn(reader, "read the fast chat")
        self.assert_private(reader, results, target.answer, ["mcp__jarvis__chat_read"])

    def test_codex_reads_are_logged_as_read(self):
        from jarvis.v2.providers import codex
        reader, target = self.rows[CODEX].thread, self.rows[CLAUDE]
        results = []

        def action(h, message):
            status, body = self.call(reader, "chat_read", {"chat": target.thread.id})
            text = body.get("text", "") if isinstance(body, dict) else str(body)
            results.append(text if status == 200 else f"HTTP {status}: {text}")
            provider, handle = codex.CodexProvider(), codex_native(h.thread_id)
            item = {"id": "call", "type": "mcpToolCall", "server": "jarvis", "tool": "chat_read",
                    "arguments": {"chat": target.thread.id}, "status": "completed",
                    "result": {"content": [{"type": "text", "text": text}]}}
            params = {"threadId": "native-parent", "turnId": "native-turn", "item": item}
            return [*provider._notification(handle, "item/started", params),
                    *provider._notification(handle, "item/completed", params)]
        self.codex.script[reader.id] = action
        self.turn(reader, "read the claude chat")
        self.assert_private(reader, results, target.answer, ["chat_read"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
