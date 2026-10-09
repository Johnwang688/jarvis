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
from jarvis.v2.providers import codex, codex_cli, codex_config
from jarvis.v2.providers.codex_rpc import RpcError, RpcProcess

UNIT = {"inputTokens": 100, "outputTokens": 20, "cachedInputTokens": 40}
CMD = "item/commandExecution/requestApproval"
# mode -> (server request method, params(thread, turn)). Every one must be
# refused or declined, and none may reach the permission callback.
FAIL_CLOSED = {
    "unknown_approval": ("item/networkAccess/requestApproval",
                         lambda t, u: {"threadId": t, "turnId": u, "itemId": "net", "host": "example.com"}),
    "unknown_permission": ("item/sandboxEscape/requestPermissions",
                           lambda t, u: {"threadId": t, "turnId": u, "itemId": "x", "permissions": {"network": True}}),
    "approval_no_item": (CMD, lambda t, u: {"threadId": t, "turnId": u, "command": "rm -rf ~"}),
    "approval_item_list": (CMD, lambda t, u: {"threadId": t, "turnId": u, "itemId": ["cmd"], "command": "echo ok"}),
    "approval_command_list": (CMD, lambda t, u: {"threadId": t, "turnId": u, "itemId": "cmd", "command": ["rm", "-rf", "~"]}),
    "file_approval_bad_root": ("item/fileChange/requestApproval",
                               lambda t, u: {"threadId": t, "turnId": u, "itemId": "patch", "grantRoot": {"path": "/"}}),
    "approval_bad_grant": (CMD, lambda t, u: {"threadId": t, "turnId": u, "itemId": "cmd", "command": "echo ok",
                                              "additionalPermissions": "everything"}),
    "params_list": (CMD, lambda t, u: ["threadId", t, "turnId", u]),
    "bad_question": ("item/tool/requestUserInput",
                     lambda t, u: {"threadId": t, "turnId": u, "itemId": "q", "questions": [{"id": "q1"}]}),
}


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
        elif method == "model/list":
            pages = script.get("model_pages", [[{
                "id": "fake-model", "model": "fake-model", "displayName": "Fake Model",
                "hidden": False, "inputModalities": ["text"],
                "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
                "defaultReasoningEffort": "high",
            }]])
            index = int(p.get("cursor", "0"))
            result = {"data": pages[index],
                      "nextCursor": str(index + 1) if index + 1 < len(pages) else None}
        elif method == "account/rateLimits/read":
            result = script.get("rate_limits", {"rateLimits": None, "rateLimitsByLimitId": {}})
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
            if mode == "scripted":
                # One server request built by the test; the turn ends when
                # its reply arrives (the `method is None` branch below).
                if script.get("item"):
                    event("item/started", item=script["item"])
                identity = {"threadId": native} if script.get("no_turn") else {"threadId": native, "turnId": turn}
                emit({"id": "special", "method": script["method"], "params": {**identity, **script["params"]}})
                continue
            if mode in FAIL_CLOSED:
                # Fail-closed shapes (2026-10-08): an approval Codex might add
                # later, approvals we cannot parse, params that are not an
                # object, and a question with no question in it.
                emit({"id": "special", "method": FAIL_CLOSED[mode][0], "params": FAIL_CLOSED[mode][1](native, turn)})
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


class RecordingAsker:
    """A human-backed surface that answers on cue and keeps what it was shown."""
    human_backed = True

    def __init__(self, decision):
        self.decision, self.seen = decision, []

    def ask(self, request):
        self.seen.append(request)
        return self.decision


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
        self.stack.enter_context(patch.object(codex, "RpcProcess", self.brain.rpc))
        self.stack.enter_context(patch.object(codex.CodexProvider, "_probe", return_value=("/fake/codex", "fake")))
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

    def test_account_metadata_reads_paginated_models_and_quota_without_a_turn(self):
        self.brain.script.update(model_pages=[
            [{"id": "gpt-6.1-sol", "model": "gpt-6.1-sol", "displayName": "GPT-6.1 Sol",
              "hidden": False, "inputModalities": ["text", "image"],
              "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "max"}],
              "defaultReasoningEffort": "low"}],
            [{"id": "gpt-6-luna", "model": "gpt-6-luna", "displayName": "GPT-6 Luna",
              "hidden": False, "inputModalities": ["text"],
              "supportedReasoningEfforts": [{"reasoningEffort": "medium"}],
              "defaultReasoningEffort": "medium"}],
        ], rate_limits={"rateLimits": {"limitId": "codex", "primary": {
            "usedPercent": 19, "windowDurationMins": 300, "resetsAt": 1900000000}}})

        metadata = self.provider.account_metadata()

        self.assertEqual([row["model"] for row in metadata["models"]],
                         ["gpt-6.1-sol", "gpt-6-luna"])
        self.assertEqual(metadata["rate_limits"]["rateLimits"]["primary"]["usedPercent"], 19)
        calls = self.brain.calls()
        model_calls = [call for call in calls if call.get("method") == "model/list"]
        self.assertEqual([call["params"].get("cursor") for call in model_calls], [None, "1"])
        rate_call = next(call for call in calls if call.get("method") == "account/rateLimits/read")
        self.assertNotIn("params", rate_call, "old app-servers require a no-params request")
        self.assertFalse(self.brain.calls("thread/start"), "metadata must not spend a model turn")
        cfg = tomllib.loads((self.root / "data" / "codex" / "_metadata" / "config.toml").read_text())
        self.assertNotIn("model", cfg)
        self.assertNotIn("mcp_servers", cfg)

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

    def test_unknown_and_malformed_requests_fail_closed(self):
        """Relaxing the version pin must not weaken the gate: whatever a newer
        Codex sends that this adapter cannot read is refused or declined, and
        the permission callback is never asked to judge it."""
        for mode, (method, _) in FAIL_CLOSED.items():
            with self.subTest(mode=mode):
                calls = []
                h = self.start(mode, lambda *a: calls.append(a) or Decision.ALLOW, thread=replace(self.thread, id=mode))
                events = self.send(h)
                self.assertFalse(calls, "the permission callback was asked")
                replies = [m for m in h.native.rpc.replies if m["id"] == "special"]
                self.assertEqual(len(replies), 1, replies)
                reply = replies[0]
                self.assertNotEqual((reply.get("result") or {}).get("decision"), "accept")
                kinds = [e.kind for e in events]
                self.assertNotIn(K.APPROVAL_REQUESTED, kinds)
                self.assertNotIn(K.APPROVAL_RESOLVED, kinds)
                self.assertIn(K.ERROR, kinds)
                if mode.startswith(("approval_", "file_approval_")):
                    # A known approval we cannot parse is an explicit decline,
                    # and the turn carries on without it.
                    self.assertEqual(reply["result"], {"decision": "decline"})
                    self.assertEqual(events[-1].kind, K.TURN_FINISHED)
                    self.assertFalse(any(e.data.get("fatal") for e in events if e.kind == K.ERROR))
                else:
                    self.assertIn("error", reply)
                    self.assertIsNone(reply["result"])

    # -- 2026-10-08 review: approvals that widen the sandbox ------------------

    def real_permit(self, asker):
        """The daemon's own callback (build_permit), on hermetic config."""
        from jarvis.v2.permissions import PermitContext, build_permit
        home = self.root / "cfg"
        home.mkdir(exist_ok=True)
        self.creds = self.root / "creds"
        for name, value in {"ALLOWLIST_PATH": home / "allowlist.json", "MODELS_PATH": home / "models.json",
                            "PROVIDER_DEFAULTS_PATH": home / "provider_defaults.json",
                            "DISCORD_GUILD_PATH": home / "discord_guild.json",
                            "V2_ALWAYS_ASK": home / "always-ask.json",
                            "V2_CREDENTIAL_DIRS": (str(self.creds / ".ssh"), str(home))}.items():
            self.stack.enter_context(patch.object(config, name, value))
        return build_permit(PermitContext("abcd1234", self.brief, provider="codex"), asker)

    def scripted(self, method, params, permit, *, item=None, no_turn=False):
        self.brain.script.update(method=method, params=params, item=item, no_turn=no_turn)
        name = f"s{len(self.brain.rpcs)}"
        h = self.start("scripted", permit, thread=replace(self.thread, id=name))
        events = self.send(h)
        replies = [m for m in h.native.rpc.replies if m["id"] == "special"]
        self.assertEqual(len(replies), 1, replies)
        return h, events, replies[0]

    def test_sandbox_widening_is_asked_of_a_human_and_never_auto_accepted(self):
        from jarvis.v2.approvals import PendingApprovals
        from jarvis.v2.permissions import deny_all
        cmd = {"command": "docker build ."}
        grant = str(self.root / "grant")      # an ordinary directory: nothing protected below it
        shapes = [
            ("extra permissions", CMD, {**cmd, "itemId": "c", "additionalPermissions": {
                "network": {"enabled": True}, "fileSystem": {"write": [grant]}}}, ["network on", f"write {grant}"]),
            ("entries", CMD, {**cmd, "itemId": "c", "additionalPermissions": {"fileSystem": {"entries": [
                {"access": "write", "path": {"type": "path", "path": "/etc"}}]}}}, ["write /etc"]),
            ("terminal input", CMD, {**cmd, "itemId": "c", "kind": "writeStdin"}, ["writeStdin"]),
            ("network prompt", CMD, {**cmd, "itemId": "c", "networkApprovalContext": {
                "host": "example.com", "protocol": "https"}}, ["network access to https://example.com"]),
            ("grant root", "item/fileChange/requestApproval", {"itemId": "f", "grantRoot": grant}, [f"grant root {grant}"]),
            # A field a later Codex might add: we cannot judge it, so it is a
            # widening until someone re-diffs the protocol.
            ("unrecognised field", CMD, {**cmd, "itemId": "c", "sandboxEscape": {"anything": True}},
             ['unrecognised field sandboxEscape={"anything":true}']),
        ]
        for label, method, params, words in shapes:
            with self.subTest(shape=label):
                # No human (deny-all), and a human who never answers: decline.
                for asker in (deny_all("nobody"), PendingApprovals(timeout_s=0.05)):
                    _, events, reply = self.scripted(method, params, self.real_permit(asker))
                    self.assertEqual(reply["result"], {"decision": "decline"}, label)
                    self.assertEqual(events[-1].kind, K.TURN_FINISHED)
                # A human who says yes: accepted, and what they were shown leads
                # with the widening, offers no Always, and names every grant.
                asker = RecordingAsker(Decision.ALLOW)
                _, events, reply = self.scripted(method, params, self.real_permit(asker))
                self.assertEqual(reply["result"], {"decision": "accept"})
                self.assertEqual(len(asker.seen), 1)
                shown = asker.seen[0]
                self.assertTrue(shown.headline.startswith("SANDBOX WIDENING: "), shown.headline)
                for word in words:
                    self.assertIn(word, shown.headline)
                self.assertFalse(shown.allowlistable, "a widening must never offer Always")
                self.assertEqual(shown.args["sandbox_widening"], shown.headline)
                self.assertEqual(shown.to_json()["headline"], shown.headline)
                requested = next(e for e in events if e.kind == K.APPROVAL_REQUESTED)
                self.assertEqual(requested.data["args"]["sandbox_widening"], shown.headline)
                # A permit callback that cannot take the keyword denies.
                _, _, reply = self.scripted(method, params, lambda tool, args, brief: Decision.ALLOW)
                self.assertEqual(reply["result"], {"decision": "decline"})

    def test_a_grant_over_protected_state_is_refused_unasked(self):
        """Layer 1 for what a widening opens: a grant that is, or contains,
        Jarvis's permission state or a credential directory is DENY, never a
        question. Approving "grant root /" is not consent to what it reaches."""
        from jarvis.v2.permissions import denied_grant
        asker = RecordingAsker(Decision.ALLOW)
        permit = self.real_permit(asker)
        cfg = str(Path(config.ALLOWLIST_PATH).parent)
        (self.creds / ".ssh").mkdir(parents=True, exist_ok=True)
        # Protected state outside any credential dir, so only ancestry can
        # catch a grant above it.
        state = self.root / "state"
        self.stack.enter_context(patch.object(config, "MODELS_PATH", state / "models.json"))
        refused = [
            ("item/fileChange/requestApproval", {"itemId": "f", "grantRoot": "/"}),
            ("item/fileChange/requestApproval", {"itemId": "f", "grantRoot": cfg}),
            ("item/fileChange/requestApproval", {"itemId": "f", "grantRoot": str(self.root)}),   # contains both
            (CMD, {"itemId": "c", "command": "ls", "additionalPermissions": {"fileSystem": {"write": [cfg + "/allowlist.json"]}}}),
            (CMD, {"itemId": "c", "command": "ls", "additionalPermissions": {"fileSystem": {"write": [str(self.creds / ".ssh" / "id_x")]}}}),
            (CMD, {"itemId": "c", "command": "ls", "additionalPermissions": {"fileSystem": {"read": [str(self.creds)]}}}),
            (CMD, {"itemId": "c", "command": "ls", "additionalPermissions": {"fileSystem": {"entries": [
                {"access": "write", "path": {"type": "special", "value": {"kind": "root"}}}]}}}),
            (CMD, {"itemId": "c", "command": "ls", "additionalPermissions": {"fileSystem": {"entries": [
                {"access": "write", "path": {"type": "glob_pattern", "pattern": "/**"}}]}}}),
            (CMD, {"itemId": "c", "command": "ls", "additionalPermissions": {"fileSystem": {"entries": [
                {"access": "read", "path": {"type": "path", "path": str(self.creds / ".ssh")}}]}}}),
            (CMD, {"itemId": "c", "command": "ls", "additionalPermissions": {"fileSystem": {"mystery": ["x"]}}}),
            ("item/fileChange/requestApproval", {"itemId": "f", "grantRoot": str(state)}),
            ("item/fileChange/requestApproval", {"itemId": "f", "grantRoot": str(Path(config.REPO_ROOT) / "jarvis")}),
        ]
        for method, params in refused:
            with self.subTest(params=params):
                _, events, reply = self.scripted(method, params, permit)
                self.assertEqual(reply["result"], {"decision": "decline"})
                self.assertEqual(asker.seen, [], "a protected grant is never put to the owner")
        decisions = [json.loads(x) for x in (config.V2_DATA_DIR / "decisions.jsonl").read_text().splitlines()]
        self.assertTrue(all(d["layer"] == "deny" for d in decisions[-len(refused):]), decisions[-len(refused):])
        # An ordinary directory, and a read of one, still go to the owner.
        for params in ({"itemId": "f", "grantRoot": str(self.root / "work")},
                       {"itemId": "c", "command": "ls", "additionalPermissions": {"fileSystem": {
                           "read": [str(self.root / "work")], "write": ["relative/out"]}}}):
            method = CMD if "command" in params else "item/fileChange/requestApproval"
            _, _, reply = self.scripted(method, params, permit)
            self.assertEqual(reply["result"], {"decision": "accept"})
        self.assertEqual(len(asker.seen), 2)
        # Ancestry, not equality; reads only matter near credentials.
        self.assertIsNotNone(denied_grant(cfg + "/../", "write"))
        self.assertIsNotNone(denied_grant(cfg, "read"), "the config dir is a credential dir")
        self.assertIsNone(denied_grant(str(self.root / "work"), "read"))
        self.assertIsNotNone(denied_grant(str(self.creds), "read"))
        self.assertIsNotNone(denied_grant("", "write"), "an empty grant is the widest one")

    def test_a_differing_command_on_a_terminal_or_subcommand_asks(self):
        """writeStdin carries the text typed into a running terminal, and an
        approvalId marks a subcommand: both legitimately differ from the item.
        They are asked (both commands shown, both judged by layer 1), not
        declined; a plain approval that differs is still declined."""
        item = {"id": "c", "type": "commandExecution", "command": "bash -i", "cwd": "/", "status": "inProgress"}
        cases = [({"itemId": "c", "kind": "writeStdin", "command": "make test\n"}, ["writeStdin", "make test", "bash -i"]),
                 ({"itemId": "c", "approvalId": "sub-1", "command": "git status"}, ["git status", "bash -i"])]
        for params, words in cases:
            with self.subTest(params=params):
                asker = RecordingAsker(Decision.ALLOW)
                _, _, reply = self.scripted(CMD, params, self.real_permit(asker), item=item)
                self.assertEqual(reply["result"], {"decision": "accept"})
                shown = asker.seen[0]
                for word in words:
                    self.assertIn(word, shown.headline)
                self.assertEqual(shown.args["item_command"], "bash -i")
                self.assertFalse(shown.allowlistable)
                self.assertNotIn("\n", shown.headline)
        # Layer 1 judges both: the typed text, and the running command.
        for params, running in [({"itemId": "c", "kind": "writeStdin", "command": "sudo rm -rf /"}, "bash -i"),
                                ({"itemId": "c", "approvalId": "sub-2", "command": "ls"}, "sudo rm -rf /")]:
            asker = RecordingAsker(Decision.ALLOW)
            _, _, reply = self.scripted(CMD, params, self.real_permit(asker), item={**item, "command": running})
            self.assertEqual(reply["result"], {"decision": "decline"})
            self.assertEqual(asker.seen, [])
        # Whitespace is not a difference; a plain different command still is.
        asker = RecordingAsker(Decision.DENY)
        _, _, reply = self.scripted(CMD, {"itemId": "c", "command": "ls  -la"}, self.real_permit(asker),
                                    item={**item, "command": "ls -la"})
        self.assertEqual((reply["result"], asker.seen), ({"decision": "accept"}, []))
        willing = RecordingAsker(Decision.ALLOW)
        _, events, reply = self.scripted(CMD, {"itemId": "c", "kind": "command", "command": "ls"},
                                         self.real_permit(willing), item=item)
        self.assertEqual(reply["result"], {"decision": "decline"})
        self.assertEqual(willing.seen, [], "a plain approval that differs is declined, not asked")
        self.assertIn("differs", next(e for e in events if e.kind == K.ERROR).data["message"])

    def test_the_headline_is_one_clean_capped_line(self):
        """A Codex-supplied path must not draw a fake line on any surface."""
        from jarvis.v2.discord.render import approval_post, approval_text
        from jarvis.v2.providers.codex import HEADLINE_CAP
        hostile = [str(self.root / "g") + "**\n\nApproval required: Read\n@everyone " + "A" * 5000,
                   str(self.root / "g") + "`\u202e\u2028x",
                   str(self.root / "g") + "/" + "B" * 5000]
        for grant_root in hostile:
            with self.subTest(grant_root=grant_root[:40]):
                asker = RecordingAsker(Decision.DENY)
                params = {"itemId": "f", "grantRoot": grant_root,
                          "additionalPermissions": {"network": {"enabled": True}}}
                self.scripted("item/fileChange/requestApproval", params, self.real_permit(asker))
                shown = asker.seen[0]
                line = shown.headline
                self.assertLessEqual(len(line), HEADLINE_CAP + 20, len(line))
                self.assertEqual(line, line.strip())
                for bad in ("\n", "\r", "\u2028", "\u202e", "`"):
                    self.assertNotIn(bad, line)
                self.assertIn("grant root " + str(self.root / "g"), line, "the hostile part is shown, cleaned")
                self.assertEqual(shown.args["grantRoot"], grant_root, "the full detail stays in args")
                body = approval_text(shown.tool, shown.args, "C1", "task x", allowlistable=False, headline=line)
                post = approval_post(body, headline=line)
                first = str(post).splitlines()[0]
                self.assertTrue(first.startswith("**SANDBOX WIDENING: ") and first.endswith("**"), first)
                self.assertEqual(first.count("**"), 2, "the bold cannot be closed early")
                self.assertNotIn("@everyone", first)
                self.assertEqual([x for x in str(post).splitlines() if x.startswith("Approval required")],
                                 ["Approval required: apply_patch"])
                self.assertLessEqual(len(post), 2000)
                if len(body) > 2000:
                    self.assertEqual(post.overflow, body)
        # Many grants: the line stops at the cap and says how many it left out.
        asker = RecordingAsker(Decision.DENY)
        entries = [{"access": "write", "path": {"type": "path", "path": f"{self.root}/w{i}"}} for i in range(30)]
        self.scripted(CMD, {"itemId": "c", "command": "ls", "additionalPermissions": {"fileSystem": {"entries": entries}}},
                      self.real_permit(asker))
        line = asker.seen[0].headline
        self.assertLessEqual(len(line), HEADLINE_CAP + 20)
        self.assertRegex(line, r"… \(\+\d+ more\)$")
        shown = line.count(f"write {self.root}/w")
        self.assertEqual(int(line.rsplit("+", 1)[1].split()[0]), 30 - shown)

    def test_plain_command_approvals_are_unchanged(self):
        from jarvis.v2.permissions import deny_all
        for params in ({"itemId": "c", "command": "docker build ."},
                       {"itemId": "c", "command": "docker build .", "kind": "command",
                        "additionalPermissions": {"network": {"enabled": False}, "fileSystem": None}}):
            asker = RecordingAsker(Decision.DENY)
            _, events, reply = self.scripted(CMD, params, self.real_permit(asker))
            self.assertEqual(reply["result"], {"decision": "accept"}, "AUTO still defers to Codex's reviewer")
            self.assertEqual(asker.seen, [])
            self.assertNotIn("sandbox_widening", next(e for e in events if e.kind == K.APPROVAL_REQUESTED).data["args"])
            _, _, reply = self.scripted(CMD, params, self.real_permit(deny_all("nobody")))
            self.assertEqual(reply["result"], {"decision": "accept"})

    def test_unknown_kind_is_declined_without_asking(self):
        asker = RecordingAsker(Decision.ALLOW)
        calls = []
        for permit in (self.real_permit(asker), lambda *a, **k: calls.append(a) or Decision.ALLOW):
            _, events, reply = self.scripted(CMD, {"itemId": "c", "kind": "futureKind", "command": None}, permit)
            self.assertEqual(reply["result"], {"decision": "decline"})
            self.assertIn("unknown approval kind", next(e for e in events if e.kind == K.ERROR).data["message"])
        self.assertEqual((asker.seen, calls), ([], []))

    def test_permit_widening_keyword(self):
        """The permissions side on its own: strict denies unasked, a deny-all
        asker declines, a human is always asked even for an allowlisted or
        rules-ALLOW command, and Always is never offered."""
        from jarvis.v2.model import PermissionProfile
        from jarvis.v2.permissions import L_WIDENING, deny_all
        asker = RecordingAsker(Decision.ALLOW)
        permit = self.real_permit(asker)
        line = "SANDBOX WIDENING: network on"
        self.assertEqual(permit("shell", {"command": "git status"}, self.brief), Decision.ALLOW)
        self.assertEqual(asker.seen, [], "rules ALLOW still answers an ordinary command")
        self.assertEqual(permit("shell", {"command": "git status"}, self.brief, widening=line), Decision.ALLOW)
        self.assertEqual(len(asker.seen), 1, "a widening is asked even for a rules-ALLOW command")
        self.assertEqual((asker.seen[0].layer, asker.seen[0].allowlistable, asker.seen[0].headline),
                         (L_WIDENING, False, line))
        strict = replace(self.brief, profile=PermissionProfile.STRICT)
        self.assertEqual(permit("shell", {"command": "git status"}, strict, widening=line), Decision.DENY)
        self.assertEqual(len(asker.seen), 1, "strict refuses a widening without asking")
        self.assertEqual(permit("shell", {"command": "sudo rm -rf /"}, self.brief, widening=line), Decision.DENY)
        self.assertEqual(len(asker.seen), 1, "layer 1 still refuses first, unasked")
        self.assertEqual(self.real_permit(deny_all("x"))("shell", {"command": "ls"}, self.brief, widening=line),
                         Decision.DENY)

    def test_null_command_cannot_hide_the_items_command(self):
        """`command: null` on the approval used to overwrite the item's real
        command, so the never-approvable rules judged "" and AUTO accepted."""
        item = {"id": "c", "type": "commandExecution", "command": "sudo rm -rf /", "cwd": "/", "status": "inProgress"}
        asker = RecordingAsker(Decision.ALLOW)
        _, events, reply = self.scripted(CMD, {"itemId": "c", "command": None, "cwd": None},
                                         self.real_permit(asker), item=item)
        self.assertEqual(reply["result"], {"decision": "decline"})
        requested = next(e for e in events if e.kind == K.APPROVAL_REQUESTED)
        self.assertEqual(requested.data["args"]["command"], "sudo rm -rf /")
        self.assertEqual(next(e for e in events if e.kind == K.APPROVAL_RESOLVED).data["decision"], "deny")
        self.assertEqual(asker.seen, [], "layer 1 refuses without asking")
        # No command anywhere: declined, nobody asked.
        calls = []
        for permit in (self.real_permit(asker), lambda *a: calls.append(a) or Decision.ALLOW):
            _, events, reply = self.scripted(CMD, {"itemId": "c", "command": None}, permit)
            self.assertEqual(reply["result"], {"decision": "decline"})
            self.assertIn("no command", next(e for e in events if e.kind == K.ERROR).data["message"])
        # The approval naming a different command than the item runs: declined.
        _, events, reply = self.scripted(CMD, {"itemId": "c", "command": "echo ok"},
                                         lambda *a: calls.append(a) or Decision.ALLOW, item=item)
        self.assertEqual(reply["result"], {"decision": "decline"})
        self.assertEqual(calls, [])

    def test_answer_cannot_override_a_refusal(self):
        """`POST /threads/<id>/answer {decision: allow}` lands on provider.answer;
        it must never turn a hard-denied approval into `accept`."""
        item = {"id": "c", "type": "commandExecution", "command": "sudo rm -rf /", "cwd": "/", "status": "inProgress"}
        self.brain.script.update(method=CMD, params={"itemId": "c", "command": "sudo rm -rf /"}, item=item, no_turn=False)
        h = self.start("scripted", self.real_permit(RecordingAsker(Decision.ALLOW)), thread=replace(self.thread, id="race"))
        raced = []
        for e in self.provider.send(h, UserMessage("go")):
            if e.kind == K.APPROVAL_REQUESTED:
                with self.assertRaises(ValueError):
                    self.provider.answer(h, e.data["req_id"], Decision.ALLOW)
                with self.assertRaises(ValueError):
                    self.provider.answer(h, e.data["req_id"], "allow")
                raced.append(e)
        self.assertTrue(raced)
        reply = next(m for m in h.native.rpc.replies if m["id"] == "special")
        self.assertEqual(reply["result"], {"decision": "decline"})
        # A question still takes free text through answer().
        with self.assertRaises(ValueError):
            self.provider.answer(h, "nonexistent", "Blue")
        # Defence in depth: even a yes that reached the pending slot by some
        # other path never outranks the callback's no.
        holder = {}

        def refuse_after_a_planted_yes(tool, args, brief):
            pending = holder["h"].native.pending[args["req_id"]]
            pending.value = Decision.ALLOW
            pending.ready.set()
            return Decision.DENY
        self.brain.script.update(params={"itemId": "c", "command": "echo ok"}, item=None)
        holder["h"] = h = self.start("scripted", refuse_after_a_planted_yes, thread=replace(self.thread, id="planted"))
        self.send(h)
        reply = next(m for m in h.native.rpc.replies if m["id"] == "special")
        self.assertEqual(reply["result"], {"decision": "decline"})

    def test_current_time_read_is_answered_not_fatal(self):
        before = int(time.time())
        _, events, reply = self.scripted("currentTime/read", {}, lambda *a: Decision.DENY, no_turn=True)
        self.assertIsNone(reply.get("error"))
        self.assertTrue(before <= reply["result"]["currentTimeAt"] <= int(time.time()) + 1, reply)
        self.assertEqual(set(reply["result"]), {"currentTimeAt"})
        self.assertEqual(events[-1].kind, K.TURN_FINISHED)
        self.assertEqual(events[-1].data, {"stop": "end"})
        self.assertNotIn(K.ERROR, [e.kind for e in events])
        # Another thread's clock read is an identity mismatch: refused, fatal.
        _, events, reply = self.scripted("currentTime/read", {"threadId": "someone-else"},
                                         lambda *a: Decision.DENY, no_turn=True)
        self.assertIn("error", reply)
        self.assertTrue(events[-1].data.get("fatal"))

    def test_a_request_is_never_answered_twice(self):
        """A failure after the reply went out (the peer died mid-approval)
        must not send a second, refusing reply on top of the first."""
        h = self.start("approval_death", lambda *_: (time.sleep(0.1) or Decision.ALLOW),
                       thread=replace(self.thread, id="once"))
        with ThreadPoolExecutor(1) as pool:
            events = pool.submit(self.send, h).result(3)
        self.assertTrue(events[-1].data.get("fatal"))
        self.assertEqual(len([m for m in h.native.rpc.replies if m["id"] == "approval"]), 1,
                         h.native.rpc.replies)
        from jarvis.v2.providers.codex import _Request
        request = _Request(rid="x", replied=True)
        with self.assertRaises(RpcError):
            codex.CodexProvider._reply(h.native, request, {"decision": "accept"})

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


FAKE_CODEX = """#!/bin/sh
echo "$@" >> "$(dirname "$0")/calls.log"
case "$1" in
  --version) echo "codex-cli {version}" ;;
  login) echo "Logged in using ChatGPT" ;;
  *) exit 64 ;;
esac
"""


class CliDefaultsChecks(unittest.TestCase):
    """Unpatched module defaults and env parsing, in fresh interpreters."""

    def test_the_real_windows_shim_is_refused(self):
        self.assertEqual(codex_cli.WINDOWS_ROOTS, ("/mnt",))
        self.assertTrue(codex_cli._windows("/mnt/c/Users/johnw/AppData/Roaming/npm/codex"))
        self.assertTrue(codex_cli._windows("/mnt"))
        self.assertTrue(codex_cli._windows("/usr/local/bin/codex.cmd"))
        self.assertFalse(codex_cli._windows("/mntx/codex"))
        self.assertFalse(codex_cli._windows("/usr/local/bin/codex"))

    def test_env_is_parsed_into_config(self):
        code = "from jarvis import config; print(config.CODEX_STRICT, repr(config.CODEX_CLI))"
        root = str(Path(__file__).resolve().parents[2])
        for env, expected in [({"JARVIS_CODEX_STRICT": "1", "JARVIS_CODEX_CLI": "/opt/codex"}, "True '/opt/codex'"),
                              ({"JARVIS_CODEX_STRICT": "0"}, "False ''"),
                              ({"JARVIS_CODEX_STRICT": "yes"}, "False ''"),
                              ({}, "False ''")]:
            base = {k: v for k, v in os.environ.items() if not k.startswith("JARVIS_CODEX_")}
            out = subprocess.run([sys.executable, "-c", code], cwd=root, env={**base, **env},
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(out.stdout.strip(), expected, (env, out.stderr[-500:]))


class HealthChecks(unittest.TestCase):
    """The version floor and the binary resolver, against fake executables in a
    temp dir. Nothing here runs a real codex or touches the network."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="codex-cli-")))
        owner = self.root / "owner"
        owner.mkdir()
        self.stack.enter_context(patch.object(codex_config, "owner_home", return_value=owner))
        self.stack.enter_context(patch.object(config, "CODEX_CLI", ""))
        self.stack.enter_context(patch.object(config, "CODEX_STRICT", False))
        self.stack.enter_context(patch.object(codex_cli, "_announced", set()))
        self.stack.enter_context(patch.object(codex_cli, "WINDOWS_ROOTS", (str(self.root / "mnt"),)))
        self.stack.enter_context(patch.object(codex_cli, "local_bin",
                                              return_value=str(self.root / "home/.local/bin/codex")))
        self.path()
        self.provider = codex.CodexProvider()

    def fake(self, where, version="0.161.0"):
        path = self.root / where / "codex"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(FAKE_CODEX.format(version=version))
        path.chmod(0o755)
        return path

    def path(self, *dirs):
        self.stack.enter_context(patch.dict(os.environ, {"PATH": os.pathsep.join(str(self.root / d) for d in dirs)}))

    def calls(self, binary):
        log = Path(binary).parent / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def health(self, version, *, where="bin"):
        binary = self.fake(where, version)
        self.path(where)
        return binary, self.provider.health()

    def test_older_is_refused_before_any_login_probe(self):
        binary, (ok, reason) = self.health("0.153.3")
        self.assertFalse(ok)
        self.assertIn("older than 0.153.4", reason)
        self.assertIn(str(binary), reason)
        self.assertEqual(self.calls(binary), ["--version"])

    def test_floor_and_verified_are_accepted_quietly(self):
        for version in ("0.153.4", "0.158.2", "0.161.0"):
            with self.subTest(version=version), self.assertNoLogs(codex_cli.log, "WARNING"):
                binary, (ok, reason) = self.health(version, where=f"bin-{version}")
                self.assertTrue(ok, reason)
                self.assertEqual(reason, f"codex {version} ({binary}), ChatGPT login")
                self.assertEqual(self.calls(binary), ["--version", "login status"])

    def test_newer_is_accepted_with_one_warning_per_process(self):
        with self.assertLogs(codex_cli.log, "WARNING") as logs:
            for _ in range(3):
                binary, (ok, reason) = self.health("0.170.2")
                self.assertTrue(ok, reason)
        warnings = [r for r in logs.records if r.levelname == "WARNING"]
        self.assertEqual(len(warnings), 1, logs.output)
        message = warnings[0].getMessage()
        self.assertIn("0.170.2", message)
        self.assertIn("verified against", message)
        self.assertIn("0.161.0", message)
        self.assertIn("protocol verified up to 0.161.0", reason)

    def test_versions_are_parsed_not_string_compared(self):
        # "0.153.10" < "0.153.4" as strings; as versions it is newer.
        self.assertGreater(codex_cli.parse("codex-cli 0.153.10"), codex_cli.parse("codex-cli 0.153.4"))
        self.assertLess(codex_cli.parse("0.161.0-alpha.2"), codex_cli.parse("0.161.0"))
        self.assertGreater(codex_cli.parse("1.0.0"), codex_cli.parse("0.999.999"))
        self.assertIsNone(codex_cli.parse("codex-cli dev"))
        self.assertTrue(codex_cli.check("codex-cli 0.153.10", "/x")[0])
        self.assertFalse(codex_cli.check("codex-cli 0.153.4-rc.1", "/x")[0], "a pre-release sorts below the floor")
        ok, reason = codex_cli.check("codex-cli dev-build", "/x")
        self.assertFalse(ok)
        self.assertIn("unreadable version", reason)

    def test_strict_mode_restores_the_exact_match(self):
        with patch.object(config, "CODEX_STRICT", True):
            binary, (ok, reason) = self.health("0.170.2")
            self.assertFalse(ok)
            self.assertIn("JARVIS_CODEX_STRICT=1", reason)
            self.assertEqual(self.calls(binary), ["--version"])
            self.assertFalse(self.health("0.158.2", where="b2")[1][0], "strict accepts only a verified version")
            for version in codex_cli.VERIFIED:
                self.assertTrue(self.health(version, where=f"s-{version}")[1][0])

    def test_windows_shims_are_skipped(self):
        shim = self.fake("mnt/c/npm", "0.161.0")
        linked = self.root / "linkdir" / "codex"
        linked.parent.mkdir()
        linked.symlink_to(shim)                       # a Linux name for a /mnt/ target
        real = self.fake("bin", "0.161.0")
        self.path("mnt/c/npm", "linkdir", "bin")
        self.assertEqual(codex_cli.resolve(), (str(real), ""))
        self.path("mnt/c/npm", "linkdir")
        found, reason = codex_cli.resolve()
        self.assertIsNone(found)
        self.assertIn("not found", reason)
        self.assertFalse(self.provider.health()[0])
        self.assertEqual(self.calls(shim), [], "the Windows shim must never run")
        for name in ("codex.cmd", "codex.bat", "codex.exe"):
            pinned = self.root / "pins" / name
            pinned.parent.mkdir(exist_ok=True)
            pinned.write_text("@echo off\n")
            pinned.chmod(0o755)
            with patch.object(config, "CODEX_CLI", str(pinned)):
                self.assertIsNone(codex_cli.resolve()[0])
        with patch.object(config, "CODEX_CLI", str(shim)):
            found, reason = codex_cli.resolve()
            self.assertIsNone(found)
            self.assertIn("Windows", reason)
        self.assertEqual(self.calls(shim), [])

    def test_env_override_wins_and_never_falls_back(self):
        self.fake("bin", "0.161.0")
        pinned = self.fake("pinned", "0.160.0")
        self.path("bin")
        with patch.object(config, "CODEX_CLI", str(pinned)):
            self.assertEqual(codex_cli.resolve(), (str(pinned), ""))
            ok, reason = self.provider.health()
            self.assertTrue(ok, reason)
            self.assertIn("0.160.0", reason)
        with patch.object(config, "CODEX_CLI", str(self.root / "missing" / "codex")):
            found, reason = codex_cli.resolve()
            self.assertIsNone(found, "a broken override must not quietly fall back to PATH")
            self.assertIn("JARVIS_CODEX_CLI", reason)
        with patch.object(config, "CODEX_CLI", "codex"):
            found, reason = codex_cli.resolve()
            self.assertIsNone(found, "a relative override would mean whatever is in the cwd")
            self.assertIn("absolute", reason)
        # A relative PATH entry is never searched: chdir to a dir holding a
        # codex and list it as "." ahead of the real one.
        decoy = self.fake("decoy", "0.161.0")
        cwd = os.getcwd()
        os.chdir(decoy.parent)
        self.addCleanup(os.chdir, cwd)
        with patch.dict(os.environ, {"PATH": os.pathsep.join(["", ".", str(self.root / "bin")])}):
            self.assertEqual(codex_cli.resolve()[0], str(self.root / "bin" / "codex"))

    def test_local_bin_is_the_last_resort(self):
        self.assertIsNone(codex_cli.resolve()[0])
        local = self.fake("home/.local/bin", "0.161.0")
        self.assertEqual(codex_cli.resolve(), (str(local), ""))
        path_codex = self.fake("bin", "0.161.0")
        self.path("bin")
        self.assertEqual(codex_cli.resolve(), (str(path_codex), ""))

    def test_the_checked_binary_is_the_launched_binary(self):
        """~/.local/bin/codex is a link into the auto-updater's `current`; the
        realpath is resolved once, version-checked, and handed to prepare."""
        release = self.fake("releases/0.161.0/bin", "0.161.0")
        link = self.root / "bin" / "codex"
        link.parent.mkdir()
        link.symlink_to(release)
        self.path("bin")
        binary, reason = self.provider._probe()
        self.assertEqual(binary, str(release))
        self.assertIn(str(link), reason)
        work = self.root / "work"
        work.mkdir()
        (codex_config.owner_home() / "auth.json").write_text("FAKE")
        with patch.object(config, "V2_DATA_DIR", self.root / "data"):
            argv, _, _ = codex_config.prepare("t1", Brief(Role.IMPLEMENTER, str(work)), binary)
            self.assertEqual(argv[:2], [str(release), "app-server"])
            # Without a caller's binary, prepare resolves the same way.
            argv, _, _ = codex_config.prepare("t2", Brief(Role.IMPLEMENTER, str(work)))
            self.assertEqual(argv[0], str(release))
        self.assertNotIn("app-server", "\n".join(self.calls(release)))

    def test_newer_by_number_not_by_string(self):
        """0.1000.0 is newer than 0.161.0, though it sorts lower as text."""
        with self.assertLogs(codex_cli.log, "WARNING") as logs:
            _, (ok, reason) = self.health("0.1000.0")
        self.assertTrue(ok, reason)
        self.assertIn("protocol verified up to 0.161.0", reason)
        self.assertEqual(len(logs.records), 1)
        with self.assertNoLogs(codex_cli.log, "WARNING"):
            _, (ok, reason) = self.health("0.160.9", where="b9")
        self.assertNotIn("verified up to", reason)

    def test_the_launch_survives_a_flip_between_check_and_spawn(self):
        """The updater re-pointing ~/.local/bin/codex after the version check
        must not change what is launched: the checked realpath is."""
        good = self.fake("releases/0.161.0/bin", "0.161.0")
        bad = self.fake("releases/0.100.0/bin", "0.100.0")
        link = self.root / "bin" / "codex"
        link.parent.mkdir()
        link.symlink_to(good)
        self.path("bin")
        (codex_config.owner_home() / "auth.json").write_text("FAKE")
        work = self.root / "work"
        work.mkdir()
        real_prepare = codex_config.prepare

        def flip_then_prepare(*args, **kwargs):
            link.unlink()
            link.symlink_to(bad)
            return real_prepare(*args, **kwargs)

        launched = []

        class Capture:
            def __init__(self, argv, **_):
                launched.append(argv)

            def start(self):
                raise RpcError("stop before any protocol")

            def close(self):
                pass

        with patch.object(config, "V2_DATA_DIR", self.root / "data"), \
                patch.object(codex_config, "prepare", flip_then_prepare), patch.object(codex, "RpcProcess", Capture):
            with self.assertRaises(RpcError):
                self.provider.start(Thread("t", "p", Role.IMPLEMENTER, ProviderName.CODEX),
                                    Brief(Role.IMPLEMENTER, str(work)), lambda *_: Decision.ALLOW)
        self.assertEqual(os.path.realpath(link), str(bad), "the link really did flip")
        self.assertEqual(launched[0][0], str(good))
        self.assertEqual(self.calls(bad), [])

    def test_failures_are_reasons_not_exceptions(self):
        def result(stdout="", stderr="", code=0):
            return subprocess.CompletedProcess([], code, stdout, stderr)
        self.fake("bin", "0.161.0")
        self.path("bin")
        for replies, expected in [([result("codex-cli 0.161.0", code=1)], "failed to report its version"),
                                  ([result("codex-cli 0.161.0"), result("", "Not logged in", 1)], "ChatGPT login"),
                                  ([result("codex-cli 0.161.0"), result("Logged in using API key")], "ChatGPT login")]:
            with patch.object(codex.subprocess, "run", side_effect=replies) as run:
                ok, reason = self.provider.health()
                self.assertFalse(ok)
                self.assertIn(expected, reason)
                self.assertTrue(all("app-server" not in c.args[0] for c in run.call_args_list))
        with patch.object(codex.subprocess, "run", side_effect=subprocess.TimeoutExpired("codex", 1)):
            self.assertFalse(self.provider.health()[0])
        # A refused binary is a refused start: no session, no app-server.
        work = self.root / "w"
        work.mkdir()
        with patch.object(codex.subprocess, "run", side_effect=[result("codex-cli 0.150.0")]), \
                patch.object(codex, "RpcProcess") as rpc, patch.object(config, "V2_DATA_DIR", self.root / "data"):
            with self.assertRaises(BriefRefused) as caught:
                self.provider.start(Thread("t", "p", Role.IMPLEMENTER, ProviderName.CODEX),
                                    Brief(Role.IMPLEMENTER, str(work)), lambda *_: Decision.ALLOW)
            self.assertIn("older than 0.153.4", str(caught.exception))
            rpc.assert_not_called()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--peer":
        peer(*sys.argv[2:])
    else:
        unittest.main(verbosity=2)
