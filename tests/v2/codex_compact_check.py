"""WP-B of the HUD Compact button: Codex's native compaction, replayed.

Plan: docs/plans/2026-10-19-compact-button-plan.md, §1 "Codex" and §6 WP-B.
The fake app-server below replays the server side of the one live recording,
tests/v2/fixtures/compact/codex-0.161.0.jsonl (WP0, codex-cli 0.161.0): its
two ordinary turns answer the session's two `turn/start`s, and its
compaction answers `thread/compact/start`. Only the executable is replaced:
the provider's transport, pipes and accounting are the real ones.

The interface these checks define (plan §1, the optional pair that follows
`adopt` in the `Provider` comment block):

- ``CodexProvider.compact_refusal(h) -> str | None``: cheap and sends
  nothing. None when `compact(h)` would run now; otherwise one plain
  sentence. After Codex answered `thread/compact/start` with JSON-RPC
  "method not found" (-32601, `codex._NO_METHOD`) it is always
  `NO_COMPACT`, without asking Codex again (a session flag, sibling of
  `steer_unsupported`).
- ``CodexProvider.compact(h) -> Iterator[Event]``: a turn with no message.
  It sends exactly one `thread/compact/start {"threadId": <native id>}` (the
  0.161.0 `ThreadCompactStartParams` hold nothing else), which Codex
  answers `{}` with no turn id. The compaction's turn id is learned from
  the first `turn/started` on the session's thread; the recorded one is
  new, neither ordinary turn's. It yields, in order:
    TURN_STARTED (first);
    TOOL_STARTED {"name": "compact", "call_id": c, ...} and later
    TOOL_FINISHED {"name": "compact", "call_id": c, "ok": bool, ...};
    any USAGE (the same events `send` makes from `thread/tokenUsage/updated`
    and `account/rateLimits/updated`);
    TURN_FINISHED {"stop": "end"|"error"|"interrupted", "compacted": bool,
                   "reason": str | None} (last).
  A `contextCompaction` item, or the deprecated `thread/compacted`
  notification, is the success. A compaction turn that completes without
  token usage is not fatal (`send` raises "completed without token usage";
  compaction must not). -32601 ends it `error` with reason `NO_COMPACT`. A
  `turn/completed` that `failed` ends it `error`, compacted false, with a
  reason that is one scrubbed line. `interrupt(h)` ends it: with the turn id
  known, `turn/interrupt` names the compaction's turn; a Stop before
  `turn/started` interrupts as soon as the id is learned. A `steer` during
  a compaction is refused (`SteerRefused`, fallback "queue") and never
  reaches Codex as `turn/steer`. The session stays open and its accounting
  complete afterwards: the next `send` runs.

Mutations these are meant to catch (each test's docstring names its own):
M1 no new-turn-id learning (the compaction's events filtered out as another
turn's: it never ends); M2 the "completed without token usage" raise kept
in compaction mode; M3 -32601 not remembered (compact_refusal None after);
M4 interrupt with a stale or missing turn id (no interrupt after a Stop
before turn/started, or one naming an ordinary turn); M5 steer allowed
during compaction.

No live Codex, no network, no real ~/.codex: a temporary owner home, a
temporary V2_DATA_DIR, and this file as the app-server (`--peer`).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import select
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from contextlib import ExitStack
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis import config
from jarvis.v2.model import ProviderName, Role, Thread
from jarvis.v2.provider import Brief, Decision, EventKind as K, SteerRefused, UserMessage
from jarvis.v2.providers import codex, codex_config
from jarvis.v2.providers.codex_rpc import RpcProcess

from codex_provider_check import active_profile, legacy_sandbox, read_back

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "compact" / "codex-0.161.0.jsonl"
NO_COMPACT = "This Codex has no thread/compact/start; update Codex to compact."
# A key-shaped string built at runtime, never a real one.
FAKE_KEY = "ghp_" + "A" * 36


def recorded():
    """The fixture as (thread id, [(request method, [what Codex sent until
    the next request])]) for each `turn/start` and `thread/compact/start`."""
    rows = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
    thread = next(r["msg"]["result"]["thread"]["id"] for r in rows
                  if r["dir"] == "recv" and r["msg"].get("id") == 2)
    segments, current = [], None
    for r in rows:
        m = r["msg"]
        if r["dir"] == "send":
            current = None
            if m.get("method") in ("turn/start", "thread/compact/start"):
                current = []
                segments.append((m["method"], current))
        elif current is not None:
            current.append(m)
    return thread, segments


THREAD, SEGMENTS = recorded()
OK_TURNS = [next(m["result"]["turn"]["id"] for m in msgs if "result" in m)
            for method, msgs in SEGMENTS if method == "turn/start"]
COMPACTION = next(msgs for method, msgs in SEGMENTS if method == "thread/compact/start")
COMPACT_TURN = next(m["params"]["turn"]["id"] for m in COMPACTION if m.get("method") == "turn/started")
# The recorded cumulative usage after the compaction (unchanged by it).
AFTER = next(m["params"]["tokenUsage"]["total"] for m in COMPACTION
             if m.get("method") == "thread/tokenUsage/updated")


class _Lines:
    """stdin lines with a timeout (select on the raw fd, our own buffer)."""
    def __init__(self):
        self.buf = b""

    def next(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while b"\n" not in self.buf:
            left = None if deadline is None else max(0.0, deadline - time.monotonic())
            ready, _, _ = select.select([0], [], [], left)
            if not ready:
                return ""            # timed out
            chunk = os.read(0, 65536)
            if not chunk:
                return None          # EOF
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode()


def compaction_script(mode):
    """What Codex sends for `thread/compact/start`, per mode: the reply, then
    the notifications (recorded, or the recording changed one way)."""
    reply, notes = COMPACTION[0], COMPACTION[1:]
    assert reply == {"id": 5, "result": {}}, reply
    if mode == "no_usage":
        notes = [m for m in notes if m.get("method") != "thread/tokenUsage/updated"]
    elif mode == "deprecated":
        # The deprecated ContextCompactedNotification instead of the item.
        out = []
        for m in notes:
            if m.get("method") == "item/started" and m["params"]["item"]["type"] == "contextCompaction":
                continue
            if m.get("method") == "item/completed" and m["params"]["item"]["type"] == "contextCompaction":
                out.append({"method": "thread/compacted",
                            "params": {"threadId": THREAD, "turnId": COMPACT_TURN}})
                continue
            out.append(m)
        notes = out
    elif mode == "failed":
        out = []
        for m in notes:
            if m.get("method") == "item/completed":
                continue        # the compaction never finished
            if m.get("method") == "turn/completed":
                m = json.loads(json.dumps(m))
                m["params"]["turn"]["status"] = "failed"
                m["params"]["turn"]["error"] = {
                    "message": "context window exceeded\nwhile compacting\x1b[31m " + FAKE_KEY,
                    "codexErrorInfo": "other", "additionalDetails": None}
            out.append(m)
        notes = out
    return reply, notes


def peer(script_path, log_path):
    """The fake app-server: the provider's start handshake, then the
    recording. Every message the provider sends is logged."""
    script = json.loads(Path(script_path).read_text())
    settings = tomllib.loads((Path(os.environ["CODEX_HOME"]) / "config.toml").read_text())
    lines = _Lines()
    turn_segments = [msgs for method, msgs in SEGMENTS if method == "turn/start"]
    state = {"turns": 0, "total": None}

    def log(m):
        with open(log_path, "a") as f:
            f.write(json.dumps(m) + "\n")

    def emit(m):
        if m.get("method") == "thread/tokenUsage/updated":
            state["total"] = m["params"]["tokenUsage"]["total"]
        print(json.dumps(m), flush=True)

    def read(timeout=None):
        line = lines.next(timeout)
        if line:
            m = json.loads(line)
            log(m)
            return m
        return line

    def wait(cue, limit, turn):
        """Until `cue` exists ("cue"), a `turn/interrupt` naming `turn`
        arrives (the message), or `limit` passes ("timeout"). A steer is
        refused as 0.161.0 refuses one into a compaction; an interrupt
        naming any other turn (or sent before there is one) is refused."""
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            if cue and Path(cue).exists():
                return "cue"
            m = read(0.02)
            if m is None:
                sys.exit(0)
            if not m:
                continue
            if m.get("method") == "turn/steer":
                emit({"id": m["id"], "error": {"code": -32600, "message": "cannot steer a compact turn"}})
            elif m.get("method") == "turn/interrupt":
                if turn is not None and m["params"] == {"threadId": THREAD, "turnId": turn}:
                    return m
                emit({"id": m["id"], "error": {"code": -32600, "message": "no active turn to interrupt"}})
            elif "id" in m and "method" in m:
                emit({"id": m["id"], "error": {"code": -32601, "message": "Method not found"}})
        return "timeout"

    def synthetic_turn(request):
        """A turn past the recording: its usage continues the totals."""
        tid = f"turn-extra-{state['turns']}"
        total = dict(state["total"])
        last = {k: 0 for k in total}
        for k, v in (("inputTokens", 100), ("outputTokens", 20), ("cachedInputTokens", 40),
                     ("totalTokens", 120)):
            total[k] = total.get(k, 0) + v
            last[k] = v
        emit({"id": request["id"], "result": {"turn": {"id": tid, "items": [], "status": "inProgress",
                                                        "error": None}}})
        ident = {"threadId": THREAD, "turnId": tid}
        emit({"method": "turn/started", "params": {"threadId": THREAD, "turn": {"id": tid, "items": [],
                                                                               "status": "inProgress"}}})
        emit({"method": "item/completed", "params": {**ident, "item": {
            "type": "agentMessage", "id": f"msg-{tid}", "text": "Done", "phase": "final_answer"}}})
        emit({"method": "thread/tokenUsage/updated", "params": {**ident, "tokenUsage": {"total": total, "last": last}}})
        emit({"method": "turn/completed", "params": {"threadId": THREAD, "turn": {
            "id": tid, "items": [], "status": "completed", "error": None}}})

    def compaction(request):
        mode = script.get("compact", "replay")
        if mode == "no_method":
            emit({"id": request["id"], "error": {"code": -32601, "message": "Method not found"}})
            return
        reply, notes = compaction_script(mode)
        emit({**reply, "id": request["id"]})
        held = mode in ("hold_before_started", "hold_after_started")
        if mode == "hold_before_started":
            # Codex has answered `{}`; its turn has not started yet.
            # Then on, cue or not (a timeout goes on; the test says what was wrong).
            wait(script["cue_started"], 5, None)
        first = next((i for i, m in enumerate(notes) if m.get("method") == "item/started"),
                     next(i for i, m in enumerate(notes) if m.get("method") == "turn/started"))
        for m in notes[:first + 1]:
            emit(m)         # through turn/started and the item's start, if any
        if held:
            got = wait(script.get("cue_finish"), 5, COMPACT_TURN)
            if isinstance(got, dict):
                # A Stop, as 0.161.0 ends a stopped turn: the reply, then
                # `turn/completed` `interrupted` (no usage was counted).
                emit({"id": got["id"], "result": {}})
                emit({"method": "thread/status/changed",
                      "params": {"threadId": THREAD, "status": {"type": "idle"}}})
                emit({"method": "turn/completed", "params": {"threadId": THREAD, "turn": {
                    "id": COMPACT_TURN, "items": [], "status": "interrupted", "error": None}}})
                return
        for m in notes[first + 1:]:
            emit(m)

    while True:
        m = read(None)
        if m is None:
            return
        if not m:
            continue
        method, p = m.get("method"), m.get("params", {})
        result = {}
        if method == "initialize":
            result = {"userAgent": "fake-codex/0.161.0"}
        elif method == "initialized":
            continue
        elif method == "account/read":
            result = {"account": {"type": "chatgpt"}}
        elif method == "config/read":
            result = {"config": read_back(settings)}
        elif method == "thread/start":
            result = {"thread": {"id": THREAD}, "model": settings.get("model", "fake-model"),
                      "reasoningEffort": settings.get("model_reasoning_effort"),
                      "approvalPolicy": "on-request",
                      "approvalsReviewer": p.get("approvalsReviewer"),
                      "sandbox": legacy_sandbox(settings),
                      "activePermissionProfile": active_profile(settings)}
        elif method == "turn/start":
            state["turns"] += 1
            if state["turns"] <= len(turn_segments):
                for out in turn_segments[state["turns"] - 1]:
                    emit({**out, "id": m["id"]} if "result" in out else out)
            else:
                synthetic_turn(m)
            continue
        elif method == "thread/compact/start":
            compaction(m)
            continue
        elif method == "turn/interrupt":
            pass                            # no turn to stop: answered, nothing happens
        elif method == "turn/steer":
            emit({"id": m["id"], "error": {"code": -32600, "message": "no active turn to steer"}})
            continue
        elif "id" in m:
            emit({"id": m["id"], "error": {"code": -32601, "message": "Method not found"}})
            continue
        else:
            continue
        emit({"id": m["id"], "result": result})


class Brain:
    def __init__(self, root):
        self.root = root
        self.log = root / "protocol.jsonl"
        self.script = {}
        self.rpcs = []

    def rpc(self, argv, **kwargs):
        fake = FakeRpc(self, argv, **kwargs)
        self.rpcs.append(fake)
        return fake

    def calls(self, method=None):
        values = [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []
        return [v for v in values if method is None or v.get("method") == method]

    def wait_call(self, method, n=1, limit=5.0):
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            if len(self.calls(method)) >= n:
                return
            time.sleep(0.01)
        raise AssertionError(f"No {method} #{n}")


class FakeRpc(RpcProcess):
    """Only the executable is replaced. All transport code and pipes are real."""
    def __init__(self, brain, argv, **kwargs):
        script = brain.root / f"script-{len(brain.rpcs)}.json"
        script.write_text(json.dumps(brain.script))
        super().__init__([sys.executable, "-B", "-u", str(Path(__file__).resolve()), "--peer",
                          str(script), str(brain.log)], **kwargs)


class CompactChecks(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="cb-codex-")))
        self.auth = self.root / "owner"
        self.auth.mkdir()
        (self.auth / "auth.json").write_text("FAKE AUTH: must never be read by provider")
        (self.auth / "skills").mkdir()
        self.work = self.root / "work"
        self.work.mkdir()
        self.brain = Brain(self.root)
        self.stack.enter_context(patch.object(config, "V2_DATA_DIR", self.root / "data"))
        self.stack.enter_context(patch.object(config, "CHAT_WORKTREES_DIR", self.root / "chat-worktrees"))
        self.stack.enter_context(patch.dict(os.environ, {"JARVIS_WINDOWS_USERS": str(self.root / "no-mnt-c" / "Users")}))
        self.stack.enter_context(patch.object(codex_config, "owner_home", return_value=self.auth))
        self.stack.enter_context(patch.object(codex, "RpcProcess", self.brain.rpc))
        self.stack.enter_context(patch.object(codex.CodexProvider, "_probe", return_value=("/fake/codex", "fake")))
        self.provider = codex.CodexProvider()
        self.brief = Brief(Role.IMPLEMENTER, str(self.work), model="fake-model", effort="high")
        self.thread = Thread("test", "p", Role.IMPLEMENTER, ProviderName.CODEX)
        self.handles = []
        self.addCleanup(self.close_handles)

    def close_handles(self):
        for h in self.handles:
            self.provider.close(h)
        for rpc in self.brain.rpcs:
            assert all(not t.is_alive() for t in rpc._readers), "stuck RPC pipe reader"

    # -- helpers --------------------------------------------------------------

    def session(self, compact="replay", **script):
        """A session that has run the recording's two ordinary turns."""
        self.assertTrue(callable(getattr(self.provider, "compact", None)),
                        "CodexProvider has no compact(h) (WP-B)")
        self.assertTrue(callable(getattr(self.provider, "compact_refusal", None)),
                        "CodexProvider has no compact_refusal(h) (WP-B)")
        self.brain.script = {"compact": compact,
                             "cue_started": str(self.root / "cue-started"),
                             "cue_finish": str(self.root / "cue-finish"), **script}
        h = self.provider.start(self.thread, self.brief, lambda *_: Decision.ALLOW)
        self.handles.append(h)
        for _ in OK_TURNS:
            events = list(self.provider.send(h, UserMessage("Reply with the single word OK.")))
            self.assertEqual(events[-1].data, {"stop": "end"}, events)
        self.assertEqual(self.provider.usage(h).input_tokens, AFTER["inputTokens"])
        return h

    def cue(self, name):
        (self.root / f"cue-{name}").touch()

    def begin(self, h):
        """Drain `compact(h)` on a thread of its own, as the daemon does."""
        run = {"events": [], "error": None, "done": threading.Event()}

        def go():
            try:
                for event in self.provider.compact(h):
                    run["events"].append(event)
            except BaseException as exc:  # noqa: BLE001 — reported by `end`
                run["error"] = exc
            finally:
                run["done"].set()
        threading.Thread(target=go, daemon=True).start()
        return run

    def end(self, h, run, limit=10.0):
        """The compaction's events once it ends; a hang closes the session
        (so nothing is left running) and fails."""
        if not run["done"].wait(limit):
            self.provider.close(h)
            run["done"].wait(5)
            self.fail(f"the compaction never ended; events so far: {run['events']}")
        if run["error"] is not None:
            raise run["error"]
        return run["events"]

    def compact(self, h, limit=10.0):
        return self.end(h, self.begin(h), limit)

    def until_turn(self, h, turn, limit=5.0):
        deadline = time.monotonic() + limit
        while h.native.turn_id != turn:
            if time.monotonic() >= deadline:
                self.fail(f"the compaction's turn id was never learned (turn_id={h.native.turn_id!r})")
            time.sleep(0.01)

    def finished(self, events):
        self.assertTrue(events, "no events")
        self.assertEqual(events[-1].kind, K.TURN_FINISHED, events)
        self.assertEqual([e.kind for e in events].count(K.TURN_FINISHED), 1, events)
        return events[-1].data

    def tool_pairs(self, events):
        """Every `compact` TOOL_STARTED has its TOOL_FINISHED, after it."""
        started = [e for e in events if e.kind == K.TOOL_STARTED]
        finished = [e for e in events if e.kind == K.TOOL_FINISHED]
        self.assertTrue(all(e.data.get("name") == "compact" for e in started + finished), events)
        self.assertEqual([e.data["call_id"] for e in started], [e.data["call_id"] for e in finished], events)
        for s, f in zip(started, finished):
            self.assertLess(events.index(s), events.index(f))
        return started, finished

    def no_fatal(self, h, events):
        self.assertFalse([e for e in events if e.kind == K.ERROR and e.data.get("fatal")], events)
        self.assertFalse(h.native.closed.is_set(), "the session was closed")

    def next_send_runs(self, h):
        """The session goes on: accounting complete, the next turn runs."""
        events = list(self.provider.send(h, UserMessage("and now?")))
        self.assertFalse([e for e in events if e.kind == K.ERROR], events)
        self.assertEqual(events[-1].data, {"stop": "end"}, events)

    def compact_starts(self):
        return self.brain.calls("thread/compact/start")

    # -- the checks -------------------------------------------------------------

    def test_success_replays_the_recording(self):
        """[M1] The recorded compaction ends compacted. The turn id comes from
        the `turn/started` that follows the `{}` reply, a new one: without
        learning it, every event of the turn is filtered as another turn's
        and the compaction never ends. Exactly one `thread/compact/start`
        naming only the thread; the usage is the recording's (unchanged)."""
        h = self.session()
        self.assertIsNone(self.provider.compact_refusal(h))
        self.assertEqual(self.compact_starts(), [], "compact_refusal sends nothing")
        events = self.compact(h)
        self.assertEqual([c["params"] for c in self.compact_starts()], [{"threadId": THREAD}])
        self.assertEqual(len(self.compact_starts()), 1)
        self.assertEqual(events[0].kind, K.TURN_STARTED, events)
        done = self.finished(events)
        self.assertEqual(done["stop"], "end", done)
        self.assertIs(done["compacted"], True, done)
        self.assertIsNone(done.get("reason"), done)
        started, finished = self.tool_pairs(events)
        self.assertEqual(len(started), 1, events)
        self.assertIs(finished[0].data["ok"], True, finished[0].data)
        usage = [e.data for e in events if e.kind == K.USAGE]
        self.assertTrue(any(u["provider_reported"].get("total") for u in usage), usage)
        self.assertEqual((usage[-1]["input"], usage[-1]["output"], usage[-1]["cached"]),
                         (AFTER["inputTokens"], AFTER["outputTokens"], AFTER["cachedInputTokens"]))
        self.assertNotIn(COMPACT_TURN, OK_TURNS)
        self.assertEqual(self.brain.calls("turn/interrupt"), [])
        self.assertEqual(self.brain.calls("turn/steer"), [])
        self.no_fatal(h, events)
        self.assertIsNone(h.native.turn_id, "the turn id is cleared when the compaction ends")

    def test_the_deprecated_thread_compacted_is_success_too(self):
        """[M1] The deprecated `thread/compacted` notification (no item)
        counts as the compaction, as the plan says."""
        h = self.session("deprecated")
        done = self.finished(events := self.compact(h))
        self.assertEqual((done["stop"], done["compacted"]), ("end", True), done)
        self.tool_pairs(events)
        self.no_fatal(h, events)

    def test_a_codex_without_compact_start_is_told_and_remembered(self):
        """[M3] -32601 ends the compaction `error` with the sentence, the
        session survives, and from then on `compact_refusal` answers that
        sentence without sending anything."""
        h = self.session("no_method")
        events = self.compact(h)
        done = self.finished(events)
        self.assertEqual(done["stop"], "error", done)
        self.assertIs(done["compacted"], False, done)
        self.assertEqual(done["reason"], NO_COMPACT)
        self.tool_pairs(events)
        self.no_fatal(h, events)
        self.assertEqual(len(self.compact_starts()), 1)
        sent = len(self.brain.calls())
        self.assertEqual(self.provider.compact_refusal(h), NO_COMPACT)
        self.assertEqual(self.provider.compact_refusal(h), NO_COMPACT)
        time.sleep(0.1)
        self.assertEqual(len(self.brain.calls()), sent, "compact_refusal sent something")
        self.next_send_runs(h)
        self.assertEqual(self.provider.compact_refusal(h), NO_COMPACT, "still remembered after a send")
        self.assertEqual(len(self.compact_starts()), 1)

    def test_compact_refusal_on_a_closed_session_is_a_sentence(self):
        """A closed session cannot compact: one plain sentence, nothing sent."""
        h = self.session()
        self.provider.close(h)
        reason = self.provider.compact_refusal(h)
        self.assertIsInstance(reason, str)
        self.assertTrue(reason.strip())
        self.assertNotIn("\n", reason)
        self.assertEqual(self.compact_starts(), [])

    def test_stop_after_turn_started_interrupts_the_compactions_turn(self):
        """[M4] A Stop once the compaction's turn is known sends
        `turn/interrupt` naming that turn (never an ordinary turn's id):
        stop `interrupted`, compacted false, and the session goes on."""
        h = self.session("hold_after_started")
        run = self.begin(h)
        self.until_turn(h, COMPACT_TURN)
        self.provider.interrupt(h)
        events = self.end(h, run)
        self.assertEqual([c["params"] for c in self.brain.calls("turn/interrupt")],
                         [{"threadId": THREAD, "turnId": COMPACT_TURN}])
        done = self.finished(events)
        self.assertEqual(done["stop"], "interrupted", done)
        self.assertIs(done["compacted"], False, done)
        self.tool_pairs(events)
        self.no_fatal(h, events)
        self.next_send_runs(h)

    def test_stop_before_turn_started_interrupts_once_the_id_is_learned(self):
        """[M4] A Stop between the `{}` reply and `turn/started` has no turn
        to name yet. The compaction does not end there (Codex would compact
        on, unseen): it interrupts as soon as `turn/started` names the turn,
        with that id."""
        h = self.session("hold_before_started")
        run = self.begin(h)
        self.brain.wait_call("thread/compact/start")
        time.sleep(0.1)         # the `{}` reply has been read
        self.provider.interrupt(h)
        time.sleep(0.1)
        self.assertEqual(self.brain.calls("turn/interrupt"), [], "no turn id to name yet")
        self.cue("started")
        events = self.end(h, run)
        self.assertEqual([c["params"] for c in self.brain.calls("turn/interrupt")],
                         [{"threadId": THREAD, "turnId": COMPACT_TURN}])
        done = self.finished(events)
        self.assertEqual(done["stop"], "interrupted", done)
        self.assertIs(done["compacted"], False, done)
        self.tool_pairs(events)
        self.no_fatal(h, events)
        self.next_send_runs(h)

    def test_a_compaction_without_token_usage_is_not_fatal(self):
        """[M2] `send` refuses a completed turn with no usage ("completed
        without token usage; accounting incomplete") and closes the session;
        a compaction turn that reports none still compacted, and the session
        and its accounting go on."""
        h = self.session("no_usage")
        events = self.compact(h)
        done = self.finished(events)
        self.assertEqual((done["stop"], done["compacted"]), ("end", True), done)
        self.no_fatal(h, events)
        self.assertFalse([e for e in events if e.kind == K.ERROR], events)
        self.assertEqual(self.provider.usage(h).input_tokens, AFTER["inputTokens"])
        self.next_send_runs(h)

    def test_a_failed_compaction_says_why_in_one_scrubbed_line(self):
        """`turn/completed` `failed`: stop `error`, compacted false, and the
        reason is Codex's message as one line, a key-shaped string redacted."""
        h = self.session("failed")
        events = self.compact(h)
        done = self.finished(events)
        self.assertEqual(done["stop"], "error", done)
        self.assertIs(done["compacted"], False, done)
        reason = done.get("reason")
        self.assertIsInstance(reason, str)
        self.assertIn("context window exceeded", reason)
        self.assertNotIn(FAKE_KEY, reason)
        self.assertNotIn("\n", reason)
        self.assertNotIn("\x1b", reason)
        _, finished = self.tool_pairs(events)
        self.assertTrue(all(f.data["ok"] is False for f in finished), finished)
        self.assertFalse(any(FAKE_KEY in json.dumps(e.data, default=str) for e in events),
                         "the key reached an event")
        self.no_fatal(h, events)
        self.next_send_runs(h)

    def test_a_steer_during_compaction_never_reaches_codex(self):
        """[M5] A steer while the compaction runs, before its turn is known
        and after, is refused for the daemon to queue (never the `interrupt`
        fallback, which would stop the compaction) and is never sent as
        `turn/steer`. The compaction then completes."""
        h = self.session("hold_before_started")
        run = self.begin(h)
        self.brain.wait_call("thread/compact/start")
        with self.assertRaises(SteerRefused) as early:
            self.provider.steer(h, UserMessage("too early"))
        self.assertEqual(early.exception.fallback, "queue")
        self.cue("started")
        self.until_turn(h, COMPACT_TURN)
        with self.assertRaises(SteerRefused) as during:
            self.provider.steer(h, UserMessage("while it compacts"))
        self.assertEqual(during.exception.fallback, "queue")
        self.cue("finish")
        events = self.end(h, run)
        self.assertEqual(self.brain.calls("turn/steer"), [])
        done = self.finished(events)
        self.assertEqual((done["stop"], done["compacted"]), ("end", True), done)
        self.assertFalse(h.native.steer_unsupported)
        self.no_fatal(h, events)

    def test_the_late_mcp_status_after_the_compaction_is_harmless(self):
        """The recording ends with `mcpServer/startupStatus/updated` after
        the compaction's `turn/completed`. It is read by whatever reads next
        and changes nothing: the next turn runs clean, its usage continuing
        the recorded totals."""
        h = self.session()
        events = self.compact(h)
        self.assertEqual(self.finished(events)["compacted"], True)
        self.assertEqual(COMPACTION[-1]["method"], "mcpServer/startupStatus/updated")
        time.sleep(0.1)        # it has arrived and waits on the transport
        self.next_send_runs(h)
        self.assertEqual(self.provider.usage(h).input_tokens, AFTER["inputTokens"] + 100)
        self.assertFalse(h.native.closed.is_set())


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--peer":
        peer(*sys.argv[2:])
    else:
        unittest.main(verbosity=2)
