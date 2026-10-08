"""Free app-server checks: FakeRpc launches a local Brain over real stdio pipes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis import config
from jarvis.v2.model import PermissionProfile, ProviderName, Role, Thread
from jarvis.v2.provider import Brief, BriefRefused, Decision, EventKind as K, UserMessage
from jarvis.v2.providers import codex, codex_config
from jarvis.v2.providers.codex_rpc import RpcError, RpcProcess

UNIT = {"inputTokens": 100, "outputTokens": 20, "cachedInputTokens": 40}


def peer(script_path, log_path, thread_name):
    """The child Brain knows only protocol JSON and temporary fixture paths."""
    script = json.loads(Path(script_path).read_text())
    settings = tomllib.loads((Path(os.environ["CODEX_HOME"]) / "config.toml").read_text())
    native, turn, count = "native-" + thread_name, None, 0
    raw = {k: 0 for k in UNIT}
    mode = script.get("mode", "normal")

    def log(m):
        with open(log_path, "a") as f:
            f.write(json.dumps({"peer": thread_name, **m}) + "\n")

    def emit(m):
        print(json.dumps(m), flush=True)

    def event(method, **params):
        emit({"method": method, "params": {"threadId": native, "turnId": turn, **params}})

    def finish(status="completed", error=None):
        event("turn/completed", turn={"id": turn, "status": status, "error": error})

    def usage():
        nonlocal raw
        raw = {k: raw[k] + UNIT[k] for k in UNIT}
        event("thread/tokenUsage/updated", tokenUsage={"total": raw, "last": UNIT})

    def approval(method="item/commandExecution/requestApproval"):
        emit({"id": "approval", "method": method, "params": {
            "threadId": native, "turnId": turn, "itemId": "cmd", "command": "echo ok", "cwd": os.getcwd()}})
        if mode == "flood":
            for _ in range(600):
                event("item/agentMessage/delta", delta="x" * 2048)
            Path(script["flood_done"]).touch()
        if mode == "approval_death":
            sys.exit(2)

    for line in sys.stdin:
        m = json.loads(line)
        log(m)
        method, p = m.get("method"), m.get("params", {})
        result = {}
        if method == "initialize":
            assert p["capabilities"]["experimentalApi"] is True
            result = {"userAgent": "fake-codex"}
        elif method == "initialized":
            continue
        elif method == "account/read":
            result = {"account": {"type": script.get("account", "chatgpt")}}
        elif method == "config/read":
            result = {"config": settings}
        elif method in ("thread/start", "thread/resume"):
            if method == "thread/resume":
                native = p["threadId"]
                count = 1
                if script.get("retained"):
                    raw = dict(UNIT)
                event("thread/tokenUsage/updated", tokenUsage={"total": UNIT, "last": UNIT}, turnId="old")
            result = {"thread": {"id": native}, "model": settings.get("model", "fake-model"),
                      "reasoningEffort": settings.get("model_reasoning_effort"),
                      "approvalPolicy": "on-request", "approvalsReviewer": "auto_review",
                      "sandbox": {"type": "workspaceWrite"}}
        elif method == "turn/start":
            if mode == "start_stall":
                continue
            count += 1
            turn = f"turn-{count}"
            if mode == "rate_limits":
                # Account updates have no thread/turn identity, and can arrive
                # before the turn/start response.
                emit({"method": "account/rateLimits/updated", "params": {"rateLimits": {
                    "limitId": "codex", "primary": {"usedPercent": 42,
                    "windowDurationMins": 300, "resetsAt": 1900000000}, "secondary": None}}})
            if mode == "early_approval":
                approval()
                reply = json.loads(sys.stdin.readline())
                log(reply)
                assert reply["result"]["decision"] == "accept"
            emit({"id": m["id"], "result": {"turn": {"id": turn, "status": "inProgress"}}})
            event("turn/started", turn={"id": turn, "status": "inProgress"})
            if mode == "death":
                sys.exit(2)
            if mode == "malformed":
                print("not json -- private text", flush=True)
                continue
            if mode in ("hold", "interrupt_stall"):
                continue
            if mode in ("approval", "flood", "deny", "approval_death", "file_approval"):
                item = {"id": "cmd", "type": "commandExecution", "command": "echo ok", "cwd": os.getcwd(), "status": "inProgress"}
                if mode == "file_approval":
                    item = {"id": "cmd", "type": "fileChange", "changes": [{"path": "a.txt", "diff": "+hi"}], "status": "inProgress"}
                event("item/started", item=item)
                approval("item/fileChange/requestApproval" if mode == "file_approval" else "item/commandExecution/requestApproval")
                continue
            if mode in ("question", "question_old", "multi_question", "question_death"):
                questions = [{"id": "q1", "question": "Which color?", "options": [{"label": "Blue"}]}]
                if mode == "multi_question":
                    questions.append({"id": "q2", "question": "Which size?", "options": []})
                emit({"id": 71, "method": "tool/requestUserInput" if mode == "question_old" else "item/tool/requestUserInput",
                      "params": {"threadId": native, "turnId": turn, "itemId": "q", "questions": questions}})
                if mode == "question_death":
                    time.sleep(0.05)
                    sys.exit(2)
                continue
            if mode in ("permissions", "unknown", "wrong_identity"):
                emit({"id": "special", "method": "item/permissions/requestApproval" if mode == "permissions" else "unrecognized/tool",
                      "params": {"threadId": "wrong" if mode == "wrong_identity" else native, "turnId": turn}})
                continue
            if mode == "capacity":
                finish("failed", {"codexErrorInfo": "serverOverloaded", "message": "capacity"})
                continue
            if mode == "full":
                event("item/agentMessage/delta", delta="Hello")
                event("item/reasoning/summaryTextDelta", delta="Think")
                event("item/reasoning/textDelta", delta="More")
                for item in [
                    {"id": "cmd", "type": "commandExecution", "command": "echo ok", "cwd": os.getcwd(), "status": "inProgress"},
                    {"id": "patch", "type": "fileChange", "changes": [{"path": "ok.txt", "diff": "--- a\n+++ b\n-old\n+ok\n+yes"}], "status": "inProgress"},
                ]:
                    event("item/started", item=item)
                    item = {**item, "status": "completed"}
                    if item["type"] == "commandExecution":
                        item["exitCode"] = 0
                    event("item/completed", item=item)
                event("turn/plan/updated", explanation="one step", plan=[{"step": "write", "status": "completed"}])
                event("item/autoApprovalReview/completed", targetItemId="cmd", review={"status": "denied", "rationale": "declined"})
            event("item/completed", item={"type": "agentMessage", "id": "answer", "text": "Done", "phase": "final_answer"})
            if mode != "no_usage":
                usage()
            if mode == "decreasing":
                event("thread/tokenUsage/updated", tokenUsage={"total": {k: 0 for k in UNIT}, "last": {k: 0 for k in UNIT}})
            if mode == "bad_baseline":
                event("thread/tokenUsage/updated", tokenUsage={"total": {k: v * 2 for k, v in UNIT.items()}, "last": UNIT})
            if mode == "duplicate_usage":
                event("thread/tokenUsage/updated", tokenUsage={"total": raw, "last": UNIT})
            finish()
            continue
        elif method == "turn/interrupt":
            if mode == "interrupt_stall":
                continue
            emit({"id": m["id"], "result": {}})
            usage()
            finish("interrupted")
            continue
        elif method is None:
            usage()
            finish()
            continue
        else:
            raise AssertionError(method)
        emit({"id": m["id"], "result": result})


class Brain:
    def __init__(self, root):
        self.root = root
        self.log = root / "protocol.jsonl"
        self.script = {"mode": "normal"}
        self.rpcs = []

    def rpc(self, argv, **kwargs):
        fake = FakeRpc(self, argv, **kwargs)
        self.rpcs.append(fake)
        return fake

    def calls(self, method=None):
        values = [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []
        return [v for v in values if method is None or v.get("method") == method]

    def wait_call(self, method, n=1):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if len(self.calls(method)) >= n:
                return
            time.sleep(0.01)
        raise AssertionError(f"No {method} #{n}")


class FakeRpc(RpcProcess):
    """Only the executable is replaced. All transport code and pipes are real."""
    def __init__(self, brain, argv, **kwargs):
        self.original_argv = argv
        self.replies = []
        index = len(brain.rpcs)
        script = brain.root / f"script-{index}.json"
        script.write_text(json.dumps(brain.script))
        super().__init__([sys.executable, "-B", "-u", str(Path(__file__).resolve()), "--peer", str(script), str(brain.log), str(index)], **kwargs)


    def reply(self, request_id, result=None, **kwargs):
        self.replies.append({"id": request_id, "result": result, **kwargs})
        return super().reply(request_id, result, **kwargs)


class Checks(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="wp4-")))
        self.auth = self.root / "owner"
        self.auth.mkdir()
        (self.auth / "auth.json").write_text("FAKE AUTH: must never be read by provider")
        (self.auth / "skills").mkdir()
        self.work = self.root / "work"
        self.work.mkdir()
        self.brain = Brain(self.root)
        self.stack.enter_context(patch.object(config, "V2_DATA_DIR", self.root / "data"))
        self.stack.enter_context(patch.object(codex_config, "owner_home", return_value=self.auth))
        self.stack.enter_context(patch.object(codex.shutil, "which", return_value="/fake/codex"))
        self.stack.enter_context(patch.object(codex, "RpcProcess", self.brain.rpc))
        self.health_patch = self.stack.enter_context(patch.object(codex.CodexProvider, "health", return_value=(True, "fake")))
        self.provider = codex.CodexProvider()
        self.brief = Brief(Role.IMPLEMENTER, str(self.work), system_append="Append this role", model="fake-model", effort="high")
        self.thread = Thread("test", "p", Role.IMPLEMENTER, ProviderName.CODEX)
        self.handles = []
        self.addCleanup(self.close_handles)

    def close_handles(self):
        for h in self.handles:
            self.provider.close(h)
        for rpc in self.brain.rpcs:
            assert all(not t.is_alive() for t in rpc._readers), "stuck RPC pipe reader"

    def start(self, mode="normal", permit=lambda *_: Decision.ALLOW, thread=None, brief=None):
        self.brain.script["mode"] = mode
        h = self.provider.start(thread or self.thread, brief or self.brief, permit)
        self.handles.append(h)
        return h

    def send(self, h):
        return list(self.provider.send(h, UserMessage("Go")))

    def test_config_and_shapes(self):
        brief = replace(self.brief, mcp_servers={"jarvis.local": {"command": "python", "args": ["-m", "jarvis.mcp"], "env": {"MODE": "test"}},
                                                "remote": {"url": "https://example.invalid/mcp", "enabled": True}})
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "secret", "OPENAI_API_KEY": "secret", "DISCORD_TOKEN": "secret", "CUSTOM_SECRET": "secret"}):
            h = self.start(brief=brief)
        home = h.native.home
        cfg = tomllib.loads((home / "config.toml").read_text())
        self.assertEqual(cfg["mcp_servers"], brief.mcp_servers)
        for key, value in {"model": "fake-model", "model_reasoning_effort": "high", "sandbox_mode": "workspace-write",
                           "approval_policy": "on-request", "approvals_reviewer": "auto_review", "web_search": "live"}.items():
            self.assertEqual(cfg[key], value)
        self.assertNotIn("features", cfg)
        self.assertNotIn("project_doc_max_bytes", cfg)
        self.assertEqual(cfg["projects"][str(self.work)]["trust_level"], "trusted")
        self.assertEqual((home / "auth.json").resolve(), self.auth / "auth.json")
        self.assertEqual((home / "skills").resolve(), self.auth / "skills")
        self.assertEqual(home.stat().st_mode & 0o777, 0o700)
        self.assertEqual((home / "config.toml").stat().st_mode & 0o777, 0o600)
        self.assertFalse(any("KEY" in k or "SECRET" in k or "TOKEN" in k for k in h.native.rpc.env))
        p = self.brain.calls("thread/start")[0]["params"]
        self.assertEqual(p["developerInstructions"], brief.system_append)
        self.assertNotIn("baseInstructions", p)
        self.assertNotIn("environments", p)
        self.assertEqual(p["cwd"], str(self.work))
        self.assertTrue(h.provider_session_id.startswith("codex:native-"))
        events = list(self.provider.send(h, UserMessage("Look", [{"b64": "YWJj", "mime": "image/png"}])))
        self.assertEqual(events[-1].kind, K.TURN_FINISHED)
        self.assertEqual(self.brain.calls("turn/start")[0]["params"]["input"][1], {"type": "image", "url": "data:image/png;base64,YWJj"})

    def test_a_named_skill_is_a_directive_on_turn_start(self):
        """S1: Codex has the skill installed, so it is told to use it. (A native
        skill input item is the pending spike; the directive works either way.)"""
        h = self.start()
        list(self.provider.send(h, UserMessage("keep it short", skill="morning-briefing")))
        self.assertEqual(self.brain.calls("turn/start")[0]["params"]["input"][0]["text"],
                         'Use the "morning-briefing" skill for this request.\n\nkeep it short')
        list(self.provider.send(h, UserMessage("plain")))
        self.assertEqual(self.brain.calls("turn/start")[1]["params"]["input"][0]["text"], "plain")
        self.provider.close(h)

    def test_set_model_rides_the_next_turn_start(self):
        """Decisions A1: turn/start's model/effort override "this turn and
        subsequent turns" (TurnStartParams, 0.153.4), so a change is sent
        with the next turn only, and the thread keeps it after that."""
        h = self.start()
        self.send(h)
        self.assertNotIn("model", self.brain.calls("turn/start")[0]["params"])
        self.provider.set_model(h, "gpt-5.6-sol", "xhigh")
        self.assertEqual(len(self.brain.calls("turn/start")), 1, "set_model sends nothing by itself")
        self.assertEqual((h.native.brief.model, h.native.brief.effort), ("gpt-5.6-sol", "xhigh"))
        self.send(h)
        second = self.brain.calls("turn/start")[1]["params"]
        self.assertEqual((second["model"], second["effort"]), ("gpt-5.6-sol", "xhigh"))
        self.send(h)
        third = self.brain.calls("turn/start")[2]["params"]
        self.assertNotIn("model", third)
        self.assertNotIn("effort", third)
        # A model/rerouted notification is judged against the model asked for.
        self.assertEqual(h.native.brief.model, "gpt-5.6-sol")
        with self.assertRaises(ValueError):
            self.provider.set_model(h, None, "high")
        self.provider.close(h)
        with self.assertRaises(ValueError):
            self.provider.set_model(h, "gpt-5.5", "high")

    def test_refused_briefs_and_api_account(self):
        for change in ({"profile": PermissionProfile.STRICT}, {"profile": PermissionProfile.ASK}, {"allowed_tools": []}, {"always_ask": ["git push"]}):
            with self.assertRaises(BriefRefused):
                self.start(brief=replace(self.brief, **change))
        self.brain.script["account"] = "apiKey"
        with self.assertRaises(BriefRefused):
            self.start()
        self.assertFalse(self.brain.calls("thread/start"))

    def test_account_rate_limits_before_turn_response(self):
        h = self.start("rate_limits")
        events = self.send(h)
        reports = [e for e in events if "rate_limits" in e.data.get("provider_reported", {})]
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].kind, K.USAGE)
        self.assertEqual(reports[0].data["provider_reported"]["rate_limits"]["primary"]["usedPercent"], 42)
        self.assertEqual(events[-1].data["stop"], "end")
        self.assertEqual(self.provider.usage(h).work_tokens, 80)

    def test_full_exact_events(self):
        h = self.start("full")
        events = self.send(h)
        self.assertEqual([e.kind for e in events], [K.TURN_STARTED, K.TEXT_DELTA, K.THINKING, K.THINKING,
            K.TOOL_STARTED, K.TOOL_FINISHED, K.TOOL_STARTED, K.TOOL_FINISHED, K.PLAN_UPDATED,
            K.REVIEWER_DECLINED, K.TEXT, K.USAGE, K.TURN_FINISHED])
        self.assertTrue(all(e.thread_id == "test" for e in events))
        self.assertEqual(events[1].data, {"text": "Hello"})
        self.assertEqual(events[4].data, {"call_id": "cmd", "name": "shell", "args": {"command": "echo ok", "cwd": str(self.work)}})
        self.assertEqual(events[5].data, {"call_id": "cmd", "name": "shell", "ok": True, "summary": "exit code 0"})
        self.assertEqual(events[6].data["args"]["paths"], ["ok.txt"])
        self.assertEqual(events[7].data, {"call_id": "patch", "name": "apply_patch", "ok": True, "summary": "1 files, +2/-1 lines"})
        self.assertEqual(events[8].data["plan"], [{"step": "write", "status": "completed"}])
        self.assertEqual(events[9].data["reason"], "declined")
        self.assertEqual(events[-2].data, {"input": 100, "output": 20, "cached": 40, "cost_usd": None,
                                         "provider_reported": {"total": UNIT, "last": UNIT}})
        self.assertEqual(events[-1].data, {"stop": "end"})
        self.assertEqual(self.provider.usage(h).work_tokens, 80)

    def test_blocked_callback_drains_real_pipe(self):
        reached, release = threading.Event(), threading.Event()
        flood_done = self.root / "flood-finished"
        self.brain.script["flood_done"] = str(flood_done)
        callback_thread = []
        def permit(*args):
            callback_thread.append(threading.get_ident())
            reached.set()
            self.assertTrue(release.wait(3))
            return Decision.ALLOW
        h = self.start("flood", permit)
        with ThreadPoolExecutor(1) as pool:
            ids = []
            def run():
                ids.append(threading.get_ident())
                return self.send(h)
            future = pool.submit(run)
            try:
                self.assertTrue(reached.wait(2))
                deadline = time.monotonic() + 2
                while not flood_done.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(flood_done.exists(), "peer blocked on a pipe while permission callback waited")
                self.assertFalse(future.done())
            finally:
                release.set()
            events = future.result(3)
        self.assertEqual(callback_thread, ids)
        self.assertEqual([e.kind for e in events[:4]], [K.TURN_STARTED, K.TOOL_STARTED, K.APPROVAL_REQUESTED, K.APPROVAL_RESOLVED])
        self.assertEqual(sum(e.kind == K.TEXT_DELTA for e in events), 600)
        self.assertEqual(self.brain.calls()[-1]["result"], {"decision": "accept"})

    def test_deny_exception_file_and_answer_approval(self):
        for mode, callback in [("deny", lambda *_: Decision.DENY), ("file_approval", lambda *_: Decision.ALLOW),
                               ("approval", lambda *_: (_ for _ in ()).throw(RuntimeError("oops")))]:
            h = self.start(mode, callback, thread=replace(self.thread, id=mode))
            events = self.send(h)
            self.assertEqual(events[2].kind, K.APPROVAL_REQUESTED)
            self.assertEqual(events[3].data["decision"], "allow" if mode == "file_approval" else "deny")
            if mode == "file_approval":
                self.assertEqual(events[2].data["tool"], "apply_patch")
                self.assertEqual(events[2].data["args"]["paths"], ["a.txt"])
        h = self.start("approval", thread=replace(self.thread, id="answer"))
        events = []
        for e in self.provider.send(h, UserMessage("go")):
            events.append(e)
            if e.kind == K.APPROVAL_REQUESTED:
                self.provider.answer(h, e.data["req_id"], Decision.DENY)
                with self.assertRaises(ValueError):
                    self.provider.answer(h, e.data["req_id"], Decision.ALLOW)
        self.assertEqual(events[3].data["decision"], "deny")

    def test_permissions_and_unknown_fail_closed(self):
        for mode in ("permissions", "unknown", "wrong_identity"):
            with self.subTest(mode=mode):
                calls = []
                h = self.start(mode, lambda *a: calls.append(a), thread=replace(self.thread, id=mode))
                events = self.send(h)
                self.assertFalse(calls)
                reply = next(m for m in h.native.rpc.replies if m["id"] == "special")
                if mode == "permissions":
                    self.assertEqual(reply["result"], {"permissions": {}, "scope": "turn"})
                    self.assertNotIn(K.APPROVAL_REQUESTED, [e.kind for e in events])
                else:
                    self.assertIn("error", reply)
                    self.assertTrue(any(e.kind == K.ERROR for e in events))

    def test_questions_both_spellings_and_bundle(self):
        for mode in ("question", "question_old", "multi_question"):
            h = self.start(mode, thread=replace(self.thread, id=mode))
            for e in self.provider.send(h, UserMessage("Go")):
                if e.kind == K.QUESTION:
                    self.assertIn(e.data["text"], ("Which color?", "Which size?"))
                    self.provider.answer(h, e.data["req_id"], "Blue" if e.data["text"] == "Which color?" else "Large")
            self.assertEqual(e.kind, K.TURN_FINISHED)
            reply = self.brain.calls()[-1]["result"]
            self.assertEqual(reply["answers"]["q1"], {"answers": ["Blue"]})
            if mode == "multi_question":
                self.assertEqual(reply["answers"]["q2"], {"answers": ["Large"]})
            with self.assertRaises(ValueError):
                self.provider.answer(h, "not-pending", "text")

    def test_interrupt_and_waiting_session_cancel(self):
        h = self.start("hold")
        h2 = self.start(thread=replace(self.thread, id="second"))
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.send, h)
            self.brain.wait_call("turn/start")
            second = pool.submit(self.send, h2)
            time.sleep(0.1)
            self.provider.interrupt(h2)
            self.assertEqual(second.result(2)[-1].data, {"stop": "interrupted"})
            self.provider.interrupt(h)
            events = first.result(2)
            self.assertEqual(events[-1].data, {"stop": "interrupted"})
            self.assertEqual(self.provider.usage(h).input_tokens, 100)
        self.assertEqual(len(self.brain.calls("turn/start")), 1)

    def test_resume_offsets_retained_reset_replay(self):
        for retained in (True, False):
            thread = replace(self.thread, id=f"resume-{retained}")
            h = self.start("duplicate_usage", thread=thread)
            self.send(h)
            self.assertEqual(self.provider.usage(h).input_tokens, 100)
            self.provider.close(h)
            thread.provider_session_id = h.provider_session_id
            self.brain.script.update(mode="normal", retained=retained)
            h = codex.CodexProvider().resume(thread, self.brief, lambda *_: Decision.ALLOW)
            self.handles.append(h)
            events = self.send(h)
            self.assertEqual(events[-1].kind, K.TURN_FINISHED, events)
            self.assertEqual(self.provider.usage(h).input_tokens, 200)
            self.assertEqual(self.provider.usage(h).cached_tokens, 80)
            self.assertIsNone(self.provider.usage(h).cost_usd)
            self.send(h)
            self.assertEqual(self.provider.usage(h).input_tokens, 300)
            params = self.brain.calls("thread/resume")[-1]["params"]
            self.assertTrue(params["excludeTurns"])
            self.assertEqual(params["threadId"], thread.provider_session_id.removeprefix("codex:"))

    def test_usage_corruption_and_decrease(self):
        h = self.start("decreasing")
        events = self.send(h)
        self.assertEqual(events[-1].kind, K.ERROR)
        self.assertTrue(events[-1].data["fatal"])
        self.assertEqual(self.provider.usage(h).input_tokens, 100)
        self.thread.provider_session_id = h.provider_session_id
        with self.assertRaises(RpcError):
            self.provider.resume(self.thread, self.brief, lambda *_: Decision.ALLOW)
        from jarvis.v2.providers.codex_usage import Accounting
        for raw, last in [({k: v * 2 for k, v in UNIT.items()}, UNIT),
                          ({**UNIT, "inputTokens": True}, UNIT),
                          ({**UNIT, "cachedInputTokens": 101}, UNIT)]:
            accounting = Accounting(self.root / "usage.json", "t", False)
            with self.assertRaises(RpcError):
                accounting.update({"total": raw, "last": last})

    def test_two_open_sessions_serialize_turns(self):
        h1 = self.start("hold")
        h2 = self.start(thread=replace(self.thread, id="second"))
        self.assertTrue(all(r.process.poll() is None for r in self.brain.rpcs))
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.send, h1)
            self.brain.wait_call("turn/start")
            second = pool.submit(self.send, h2)
            time.sleep(0.15)
            self.assertEqual(len(self.brain.calls("turn/start")), 1)
            self.provider.interrupt(h1)
            self.assertEqual(first.result(2)[-1].data["stop"], "interrupted")
            self.assertEqual(second.result(2)[-1].data["stop"], "end")
        self.assertEqual(len(self.brain.calls("turn/start")), 2)

    def test_transport_death_questions_and_cleanup(self):
        for mode in ("death", "malformed", "question_death", "approval_death"):
            h = self.start(mode, lambda *_: (time.sleep(0.1) or Decision.ALLOW), thread=replace(self.thread, id=mode))
            with ThreadPoolExecutor(1) as pool:
                events = pool.submit(self.send, h).result(3)
            self.assertEqual(events[-1].kind, K.ERROR, events)
            self.assertTrue(events[-1].data["fatal"])
            self.assertNotIn("private text", events[-1].data["message"])
            self.assertIsNotNone(h.native.rpc.process.poll())
            self.assertTrue(all(not t.is_alive() for t in h.native.rpc._readers))

    def test_early_approval_and_abandoned_generator(self):
        h = self.start("early_approval")
        events = self.send(h)
        self.assertEqual(sum(e.kind == K.TURN_STARTED for e in events), 1)
        self.assertEqual(events[-1].data, {"stop": "end"})
        h = self.start("hold", thread=replace(self.thread, id="abandoned"))
        stream = self.provider.send(h, UserMessage("Go"))
        self.assertEqual(next(stream).kind, K.TURN_STARTED)
        stream.close()
        self.assertTrue(h.native.closed.is_set())
        self.assertFalse(codex._auth_lock.locked())

    def test_capacity_cooldown_and_turn_cap(self):
        h = self.start("capacity")
        self.assertEqual(self.send(h)[-1].data, {"stop": "error"})
        h2 = self.start(thread=replace(self.thread, id="second"))
        events = self.send(h2)
        self.assertEqual(events[0].data["message"], "Codex model cooling")
        self.assertIn("retry_at", events[0].data)
        self.assertEqual(len(self.brain.calls("turn/start")), 1)
        h3 = self.start(thread=replace(self.thread, id="cap"), brief=replace(self.brief, max_turns=0))
        self.assertEqual(self.send(h3)[0].data, {"stop": "max_turns"})

    def test_stalled_control_requests_are_bounded(self):
        with patch.object(codex, "START_TIMEOUT", 0.15), patch.object(codex, "INTERRUPT_TIMEOUT", 0.15):
            h = self.start("start_stall")
            with ThreadPoolExecutor(1) as pool:
                events = pool.submit(self.send, h).result(2)
            self.assertTrue(events[-1].data["fatal"])
            h = self.start("interrupt_stall", thread=replace(self.thread, id="stall"))
            stream = self.provider.send(h, UserMessage("Go"))
            next(stream)
            self.provider.interrupt(h)
            self.assertTrue(list(stream)[-1].data["fatal"])

    def test_queue_overflow_fails_closed(self):
        from jarvis.v2.providers import codex_rpc
        done = self.root / "flood-done"
        self.brain.script["flood_done"] = str(done)
        with patch.object(codex_rpc.config, "CODEX_RPC_QUEUE_LIMIT", 16):
            h = self.start("flood", lambda *_: (time.sleep(0.2) or Decision.ALLOW))
        events = self.send(h)
        self.assertEqual(events[-1].kind, K.ERROR)
        self.assertTrue(events[-1].data["fatal"])
        self.assertTrue(h.native.rpc._overflow.is_set())
        self.assertFalse(any(r.get("result") == {"decision": "accept"} for r in h.native.rpc.replies))

    def test_close_unblocks_question_and_auth_waiter(self):
        h = self.start("question")
        h2 = self.start(thread=replace(self.thread, id="waiter"))
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.send, h)
            deadline = time.monotonic() + 2
            while not h.native.pending and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(h.native.pending)
            second = pool.submit(self.send, h2)
            self.provider.close(h2)
            self.assertTrue(second.result(2)[-1].data["fatal"])
            self.provider.close(h)
            self.assertTrue(first.result(2)[-1].data["fatal"])

    def test_early_approval_human_wait_is_not_rpc_timeout(self):
        with patch.object(codex, "START_TIMEOUT", 0.05):
            h = self.start("early_approval", lambda *_: (time.sleep(0.15) or Decision.ALLOW))
            events = self.send(h)
        self.assertEqual(events[-1].data, {"stop": "end"})

    def test_missing_usage_fails_closed(self):
        h = self.start("no_usage")
        events = self.send(h)
        self.assertEqual(events[-1].kind, K.ERROR)
        self.assertTrue(events[-1].data["fatal"])
        self.assertIn("accounting incomplete", events[-1].data["message"])

    def test_private_path_and_duplicate_session_protection(self):
        with self.assertRaises(BriefRefused):
            self.start(thread=replace(self.thread, id="../escape"))
        h = self.start()
        before = (h.native.home / "config.toml").read_text()
        with self.assertRaises(BriefRefused):
            self.start(brief=replace(self.brief, model="other"))
        self.assertEqual((h.native.home / "config.toml").read_text(), before)

    def test_incomplete_turn_cannot_be_certified_by_later_success(self):
        h = self.start("hold")
        stream = self.provider.send(h, UserMessage("Go"))
        next(stream)
        self.provider.interrupt(h)
        self.assertEqual(list(stream)[-1].data, {"stop": "interrupted"})
        self.assertTrue(self.send(h)[-1].data["fatal"])
        self.assertEqual(len(self.brain.calls("turn/start")), 1)


class TransportChecks(unittest.TestCase):
    def test_stderr_drains_and_stubborn_peer_is_killed(self):
        script = """
import json, signal, sys
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
for line in sys.stdin:
    m = json.loads(line)
    sys.stderr.write('discard me' * 100000)
    sys.stderr.flush()
    print(json.dumps({'id': m['id'], 'result': {}}), flush=True)
"""
        rpc = RpcProcess([sys.executable, "-B", "-u", "-c", script])
        try:
            self.assertEqual(rpc.request("probe", timeout=2), {})
        finally:
            before = time.monotonic()
            rpc.close()
        self.assertLess(time.monotonic() - before, 2)
        self.assertIsNotNone(rpc.process.poll())
        self.assertTrue(all(not t.is_alive() for t in rpc._readers))


class HealthChecks(unittest.TestCase):
    def test_health(self):
        provider = codex.CodexProvider()
        with patch.object(codex.shutil, "which", return_value=None), patch.object(codex.subprocess, "run") as run:
            self.assertFalse(provider.health()[0])
            run.assert_not_called()
        def result(stdout="", stderr="", code=0):
            return subprocess.CompletedProcess([], code, stdout, stderr)
        for replies, expected in [([result("codex-cli 0.1")], (False, "codex 0.1, pinned 0.153.4")),
                                  ([result("codex-cli 0.153.4"), result("", "Not logged in", 1)], None),
                                  ([result("codex-cli 0.153.4"), result("Logged in using API key")], None),
                                  ([result("codex-cli 0.153.4"), result("", "Logged in using ChatGPT")], True)]:
            with patch.object(codex.shutil, "which", return_value="/fake/codex"), patch.object(codex.subprocess, "run", side_effect=replies) as run:
                actual = provider.health()
                if expected is True:
                    self.assertTrue(actual[0])
                elif expected:
                    self.assertEqual(actual, expected)
                else:
                    self.assertFalse(actual[0])
                self.assertTrue(all("app-server" not in c.args[0] for c in run.call_args_list))
        with patch.object(codex.shutil, "which", return_value="/fake/codex"), patch.object(codex.subprocess, "run", side_effect=subprocess.TimeoutExpired("codex", 1)):
            self.assertFalse(provider.health()[0])


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--peer":
        peer(*sys.argv[2:])
    else:
        unittest.main(verbosity=2)
