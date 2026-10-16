"""Free app-server checks: FakeRpc launches a local Brain over real stdio pipes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
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


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "subagents" / "codex"
SEEN = object()     # `Checks.scripted`: start the item a file approval is about


def fixture_turn(name):
    """One recorded SV-0 parent turn (Codex 0.161.0, `record_codex.py`): what
    Codex sent from the turn/start reply to the parent's turn/completed, every
    child notification and server request included, in wire order. The
    parent's thread and turn ids and the work folder become placeholders the
    replaying peer fills in; children keep their recorded ids."""
    rows = [json.loads(line) for line in (FIXTURES / f"{name}.jsonl").read_text().splitlines()]
    start = next(r["data"] for r in rows if r["src"] == "rpc_out" and r["data"].get("method") == "turn/start")
    thread, turn, msgs = start["params"]["threadId"], None, []
    for r in rows:
        d = r["data"]
        if r["src"] != "rpc_in":
            continue
        if "method" not in d:
            if d.get("id") == start["id"]:
                turn = d["result"]["turn"]["id"]
                msgs.append({**d, "id": "$START"})
            continue
        if turn is None:
            continue
        msgs.append(d)
        if d["method"] == "turn/completed" and d["params"]["threadId"] == thread:
            break
    text = json.dumps(msgs).replace(thread, "$THREAD").replace(turn, "$TURN")
    return json.loads(re.sub(r"<TMP>/work-[A-Za-z0-9_]+", "$CWD", text))


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
            if script.get("model_stall"):       # never answers: the deadline must
                continue
            if script.get("model_error"):
                emit({"id": m["id"], "error": {"code": -32000, "message": "private detail"}})
                continue
            pages = script.get("model_pages", [[{
                "id": "fake-model", "model": "fake-model", "displayName": "Fake Model",
                "hidden": False, "inputModalities": ["text"],
                "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
                "defaultReasoningEffort": "high",
            }]])
            cursor = str(p.get("cursor", "0"))
            index = int(cursor) % len(pages) if cursor.isdigit() else 0
            result = {"data": pages[index],
                      "nextCursor": str(index + 1) if index + 1 < len(pages) else None}
            if "model_next" in script:      # a cursor that repeats
                result["nextCursor"] = script["model_next"]
            elif script.get("model_endless"):
                result["nextCursor"] = str(int(cursor) + 1 if cursor.isdigit() else 1)
        elif method == "account/rateLimits/read":
            if script.get("rate_error"):
                emit({"id": m["id"], "error": {"code": -32000, "message": "private detail"}})
                continue
            result = script.get("rate_limits", {"rateLimits": None, "rateLimitsByLimitId": {}})
        elif method == "config/read":
            result = {"config": {**settings, **script.get("config_override", {})}}
        elif method in ("thread/start", "thread/resume"):
            if method == "thread/resume":
                native = p["threadId"]
                count = 1
                if script.get("retained"):
                    raw = dict(UNIT)
                event("thread/tokenUsage/updated", tokenUsage={"total": UNIT, "last": UNIT}, turnId="old")
            result = {"thread": {"id": native}, "model": settings.get("model", "fake-model"),
                      "reasoningEffort": settings.get("model_reasoning_effort"),
                      "approvalPolicy": "on-request",
                      "approvalsReviewer": script.get("reviewer", p.get("approvalsReviewer")),
                      "sandbox": script.get("sandbox", {"type": "workspaceWrite", "networkAccess": False,
                                                       "writableRoots": settings.get("sandbox_workspace_write", {}).get("writable_roots", [])})}
        elif method == "turn/start":
            if mode == "start_stall":
                continue
            count += 1
            turn = f"turn-{count}"
            if mode == "replay":
                # A recorded SV-0 parent turn (fixture_turn): every message
                # Codex 0.161.0 sent, children's included, with the parent's
                # ids and work folder as placeholders. A server request waits
                # for its reply, as Codex does.
                for out in (script["replays"][count - 1] if "replays" in script else script["replay"]):
                    out = json.loads(json.dumps(out).replace("$THREAD", native).replace("$TURN", turn)
                                     .replace("$CWD", os.getcwd()))
                    if "$WAIT" in out:
                        # Where the recorder sent a request itself (SV-4: the
                        # child's `turn/interrupt`): wait for the provider's,
                        # and answer it. One aimed at another thread is the
                        # orchestrator's: its turn ends interrupted, as Codex's would.
                        while True:
                            line = sys.stdin.readline()
                            if not line:
                                return
                            got = json.loads(line)
                            log(got)
                            if got.get("method") == out["$WAIT"]:
                                break
                        if (got.get("params") or {}).get("threadId") != out["thread"]:
                            emit({"id": got["id"], "result": {}})
                            finish("interrupted")
                            break
                        emit({"id": got["id"], "error": out["error"]} if "error" in out
                             else {"id": got["id"], "result": {}})
                        continue
                    if out.get("id") == "$START":
                        out["id"] = m["id"]
                    emit(out)
                    if "method" in out and "id" in out:
                        answer = sys.stdin.readline()
                        if not answer:
                            return
                        log(json.loads(answer))
                continue
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
            if (mode in ("hold", "interrupt_stall", "steer", "steer_unsupported", "steer_errors",
                         "steer_silent")
                    or (mode in ("steer_late", "stop_once", "stop_once_quiet") and count == 1)):
                continue
            if mode in ("approval", "flood", "deny", "approval_death", "file_approval"):
                item = {"id": "cmd", "type": "commandExecution", "command": "echo ok", "cwd": os.getcwd(), "status": "inProgress"}
                if mode == "file_approval":
                    item = {"id": "cmd", "type": "fileChange", "changes": [{"path": "a.txt", "diff": "+hi", "kind": {"type": "add"}}], "status": "inProgress"}
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
                for changes in script.get("updates") or []:
                    event("item/fileChange/patchUpdated", itemId=script["item"]["id"], changes=changes)
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
            if mode in ("full", "declined", "tool_failed"):
                event("item/agentMessage/delta", delta="Hello")
                event("item/reasoning/summaryTextDelta", delta="Think")
                event("item/reasoning/textDelta", delta="More")
                for item in [
                    {"id": "cmd", "type": "commandExecution", "command": "echo ok", "cwd": os.getcwd(), "status": "inProgress"},
                    {"id": "patch", "type": "fileChange", "changes": [{"path": "ok.txt", "diff": "--- a\n+++ b\n-old\n+ok\n+yes", "kind": {"type": "update", "move_path": None}}], "status": "inProgress"},
                ]:
                    event("item/started", item=item)
                    item = {**item, "status": "declined" if mode == "declined" else "failed" if mode == "tool_failed" else "completed"}
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
            # `odd_status`: a TurnStatus that is not an ending (`inProgress`)
            # or one 0.161.0 does not define (`paused`).
            finish(script.get("odd_status", "paused") if mode == "odd_status" else "completed")
            continue
        elif method == "turn/steer":
            # The 0.153.4/0.161.0 shape: {threadId, input, expectedTurnId} ->
            # {turnId}, refused when there is no such active turn.
            if mode == "steer_unsupported":
                emit({"id": m["id"], "error": {"code": -32601, "message": "Method not found"}})
                continue
            if mode == "steer_errors":
                # 0.161.0's own steer refusals, and an unknown variant that is
                # not the method: each in turn, then the turn completes.
                errors = script["steer_errors"]
                n = script["_seen"] = script.get("_seen", 0) + 1
                code, text = errors[min(n, len(errors)) - 1]
                emit({"id": m["id"], "error": {"code": code, "message": text}})
                if n == len(errors):
                    usage()
                    finish()
                continue
            if mode == "steer_silent":
                continue                    # never answered
            if mode == "steer_late":
                # The turn completes first; the refusal arrives after it.
                usage()
                finish()
                emit({"id": m["id"], "error": {"code": -32600, "message": "no active turn to steer"}})
                continue
            if turn is None or p.get("expectedTurnId") != turn:
                emit({"id": m["id"], "error": {"code": -32600, "message": "no active turn to steer"}})
                continue
            emit({"id": m["id"], "result": {"turnId": turn}})
            text = p["input"][0]["text"]
            event("item/completed", item={"type": "agentMessage", "id": "steered",
                                          "text": f"steered: {text}", "phase": "final_answer"})
            usage()
            finish()
            continue
        elif method == "turn/interrupt":
            if mode == "interrupt_stall":
                continue
            if mode in ("stop_once", "stop_once_quiet"):
                # The owner's Stop as Codex 0.161.0 answers it (SV-0's
                # `parent_interrupt`, seq 26-31): the usage of a sampling
                # request that finished is reported, then the reply, then
                # `turn/completed` `interrupted`. `_quiet`: stopped before
                # any request finished, so no usage at all for the turn.
                if mode == "stop_once":
                    usage()
                emit({"id": m["id"], "result": {}})
                finish("interrupted")
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
        self.stack.enter_context(patch.object(config, "CHAT_WORKTREES_DIR", self.root / "chat-worktrees"))
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

    def git(self, *args, cwd=None):
        # Empty temporary HOME and disabled global/system config: fixtures
        # cannot read the owner's credentials, hooks or repository settings.
        return subprocess.run(["git", "-C", str(cwd or self.work), *args], check=True,
                              capture_output=True, text=True,
                              env={"PATH": os.environ["PATH"], "HOME": str(self.root),
                                   "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})

    def repo(self):
        self.git("init", "-q")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "--allow-empty", "-qm", "fixture")
        return self.work / ".git"

    def chat(self):
        return replace(self.brief, role=Role.CHAT)

    def test_chat_and_task_reviewer_selection_on_start_and_resume(self):
        for brief, expected in ((self.chat(), "user"), (self.brief, "auto_review"),
                                (replace(self.chat(), task_id="task"), "auto_review")):
            with self.subTest(role=brief.role, task=brief.task_id):
                thread = replace(self.thread, id=f"reviewer-{len(self.brain.rpcs)}")
                h = self.start(thread=thread, brief=brief)
                self.send(h)
                cfg = tomllib.loads((h.native.home / "config.toml").read_text())
                self.assertEqual(cfg["approvals_reviewer"], expected)
                self.assertEqual(self.brain.calls("thread/start")[-1]["params"]["approvalsReviewer"], expected)
                self.provider.close(h)
                thread.provider_session_id = h.provider_session_id
                resumed = self.provider.resume(thread, brief, lambda *a, **k: Decision.DENY)
                self.handles.append(resumed)
                self.assertEqual(self.brain.calls("thread/resume")[-1]["params"]["approvalsReviewer"], expected)
                self.provider.close(resumed)

    def test_chat_reviewer_mismatch_fails_closed_on_start_and_resume(self):
        for mismatch in ("config", "receipt"):
            for resume in (False, True):
                for wrong in ("auto_review", "guardian_subagent", None):
                    with self.subTest(mismatch=mismatch, resume=resume, wrong=wrong):
                        self.brain.script.clear()
                        thread = replace(self.thread, id=f"mismatch-{len(self.brain.rpcs)}")
                        if resume:
                            h = self.start(thread=thread, brief=self.chat())
                            self.send(h)
                            self.provider.close(h)
                            thread.provider_session_id = h.provider_session_id
                        if mismatch == "config":
                            self.brain.script["config_override"] = {"approvals_reviewer": wrong}
                        else:
                            self.brain.script["reviewer"] = wrong
                        before = len(self.brain.rpcs)
                        operation = self.provider.resume if resume else self.provider.start
                        with self.assertRaises(BriefRefused):
                            operation(thread, self.chat(), lambda *a, **k: Decision.ALLOW)
                        self.assertEqual(len(self.brain.rpcs), before + 1)
                        self.assertIsNotNone(self.brain.rpcs[-1].process.poll())
        self.brain.script.clear()

    def test_chat_git_config_and_instructions(self):
        gitdir = self.repo()
        h = self.start(brief=self.chat())
        cfg = tomllib.loads((h.native.home / "config.toml").read_text())
        self.assertEqual(cfg["sandbox_workspace_write"],
                         {"writable_roots": [str(gitdir), str(config.CHAT_WORKTREES_DIR)],
                          "network_access": False})
        self.assertEqual(config.CHAT_WORKTREES_DIR.stat().st_mode & 0o777, 0o700)
        params = self.brain.calls("thread/start")[0]["params"]
        self.assertEqual(params["developerInstructions"],
                         self.brief.system_append + "\n\n" + codex_config.git_note(self.chat()))
        self.assertIn("shared", params["developerInstructions"])
        for words in ("no GitHub login", "chat_git_push / chat_pr_create / chat_pr_merge / chat_pr_close",
                      "chat_git_fetch", "Do not request escalation for git push or gh"):
            self.assertIn(words, params["developerInstructions"])
        self.assertNotIn("escalated permissions", params["developerInstructions"])

    def test_chat_linked_worktree_and_nested_cwd_include_common_dir(self):
        common = self.repo()
        linked = self.root / "linked"
        self.git("worktree", "add", "-q", "--detach", str(linked))
        nested = linked / "nested"
        nested.mkdir()
        gitdir = common / "worktrees" / "linked"
        brief = replace(self.chat(), cwd=str(nested))
        self.assertEqual(codex_config.git_roots(brief),
                         [str(gitdir), str(common), str(config.CHAT_WORKTREES_DIR)])

    def test_chat_can_open_inside_its_worktree_root(self):
        common = self.repo()
        root = config.CHAT_WORKTREES_DIR
        root.mkdir()
        linked = root / "continued"
        self.git("worktree", "add", "-q", "--detach", str(linked))
        brief = replace(self.chat(), cwd=str(linked))
        self.assertEqual(codex_config.git_roots(brief)[-1], str(root))
        self.assertIn(str(common), codex_config.git_roots(brief))

    def test_chat_outside_repo_only_gets_worktree_root(self):
        roots = codex_config.git_roots(self.chat())
        self.assertEqual(roots, [str(config.CHAT_WORKTREES_DIR)])
        self.assertIn("No writable repository git metadata", codex_config.git_note(self.chat()))

    def test_task_has_no_extra_roots_or_git_instructions_or_directory(self):
        self.repo()
        h = self.start()
        cfg = tomllib.loads((h.native.home / "config.toml").read_text())
        self.assertNotIn("sandbox_workspace_write", cfg)
        self.assertFalse(config.CHAT_WORKTREES_DIR.exists())
        self.assertEqual(codex_config.git_note(self.brief), "")
        self.assertEqual(codex_config.git_roots(replace(self.chat(), task_id="task")), [])
        self.assertEqual(self.brain.calls("thread/start")[0]["params"]["developerInstructions"],
                         self.brief.system_append)
        # A bad chat-only override does not disable otherwise valid tasks.
        with patch.object(config, "CHAT_WORKTREES_DIR", Path("/")):
            self.assertEqual(codex_config.git_roots(self.brief), [])

    def test_common_chat_role_tells_both_providers_to_use_worktrees(self):
        from jarvis.v2.roles import brief_for
        note = brief_for(Role.CHAT).system_append
        self.assertIn("checkout is shared", note)
        self.assertIn("separate git worktree", note)
        self.assertNotIn("checkout is shared", brief_for(Role.IMPLEMENTER).system_append)

    def test_resume_also_gets_chat_git_instructions(self):
        self.repo()
        h = self.start(brief=self.chat())
        self.provider.close(h)
        thread = replace(self.thread, provider_session_id=h.provider_session_id, role=Role.CHAT)
        resumed = self.provider.resume(thread, self.chat(), lambda *_: Decision.ALLOW)
        self.handles.append(resumed)
        self.assertIn(codex_config.git_note(self.chat()),
                      self.brain.calls("thread/resume")[0]["params"]["developerInstructions"])

    def test_git_directory_cannot_overlap_credentials_or_protected_state(self):
        gitdir = self.repo()
        for protected in (gitdir, gitdir / "nested", self.work):
            with self.subTest(credential=protected), patch.object(config, "credential_dirs", return_value=(str(protected),)):
                self.assertEqual(codex_config.git_dirs(str(self.work)), [])
        for protected in (gitdir, gitdir / "models.json"):
            with self.subTest(state=protected), patch.object(config, "MODELS_PATH", protected):
                self.assertEqual(codex_config.git_dirs(str(self.work)), [])
        for name in ("V2_DATA_DIR", "HUD_DIR"):
            with self.subTest(setting=name), patch.object(config, name, gitdir):
                self.assertEqual(codex_config.git_dirs(str(self.work)), [])

    def test_git_symlinks_and_nonregular_pointers_are_refused(self):
        gitdir = self.repo()
        moved = self.root / "metadata"
        gitdir.rename(moved)
        gitdir.symlink_to(moved, target_is_directory=True)
        self.assertEqual(codex_config.git_dirs(str(self.work)), [])
        gitdir.unlink()
        os.mkfifo(gitdir)
        self.assertEqual(codex_config.git_dirs(str(self.work)), [])
        gitdir.unlink()
        gitdir.write_text("gitdir: " + str(moved))
        objects = moved / "objects"
        moved_objects = self.root / "objects"
        objects.rename(moved_objects)
        objects.symlink_to(moved_objects, target_is_directory=True)
        self.assertEqual(codex_config.git_dirs(str(self.work)), [])

    def test_git_pointer_malformed_oversized_or_arbitrary_directory_refused(self):
        pointer = self.work / ".git"
        for value in ("gitdir: /", "gitdir: " + str(self.root), "gitdir: ",
                      "wrong", "gitdir: ../metadata\nextra",
                      "gitdir: /" + "a" * codex_config._GITFILE_CAP, "gitdir: /bad\0path"):
            with self.subTest(value=value[:60]):
                pointer.write_text(value)
                self.assertEqual(codex_config.git_dirs(str(self.work)), [])

    def test_linked_common_pointer_symlink_or_wrong_backlink_refused(self):
        common = self.repo()
        linked = self.root / "linked"
        self.git("worktree", "add", "-q", "--detach", str(linked))
        gitdir = common / "worktrees" / "linked"
        (gitdir / "gitdir").write_text(str(self.work / ".git"))
        self.assertEqual(codex_config.git_dirs(str(linked)), [])
        (gitdir / "gitdir").write_text(str(linked / ".git"))
        pointer = gitdir / "commondir"
        text = self.root / "common-pointer"
        text.write_text("../..")
        pointer.unlink()
        pointer.symlink_to(text)
        self.assertEqual(codex_config.git_dirs(str(linked)), [])

    def test_worktree_root_validation_precedes_any_creation_or_chmod(self):
        self.repo()
        unsafe = [Path("/"), Path("relative"), self.work, self.work / "nested",
                  self.auth, self.auth / "nested", self.root / "data",
                  self.root / "data" / "nested"]
        for target in unsafe:
            with self.subTest(target=target), patch.object(config, "CHAT_WORKTREES_DIR", target):
                with self.assertRaises(BriefRefused):
                    codex_config.prepare("invalid", self.chat(), "/fake/codex")
        self.assertEqual(self.work.stat().st_mode & 0o777, 0o755)
        self.assertFalse((self.work / "nested").exists())
        self.assertFalse((self.auth / "nested").exists())
        # A protected file anywhere beneath an override makes the whole root unsafe.
        target = self.root / "protected-holder"
        with patch.object(config, "CHAT_WORKTREES_DIR", target), \
                patch.object(config, "MODELS_PATH", target / "models.json"):
            with self.assertRaises(BriefRefused):
                codex_config.prepare("invalid", self.chat(), "/fake/codex")
        self.assertFalse(target.exists())

    def test_protected_git_pointer_is_rejected_before_metadata_is_read(self):
        self.work.joinpath(".git").write_text("gitdir: " + str(self.auth))
        read = codex_config._small_text
        seen = []
        def observed(path):
            seen.append(path)
            return read(path)
        with patch.object(codex_config, "_small_text", side_effect=observed):
            self.assertEqual(codex_config.git_dirs(str(self.work)), [])
        self.assertEqual(seen, [self.work / ".git"])

    def test_credential_env_names_and_uncertain_scans_refuse_roots(self):
        gitdir = self.repo()
        # Empty, synthetic name only; no real credentials or env contents.
        (gitdir / ".env.fixture").touch()
        self.assertEqual(codex_config.git_dirs(str(self.work)), [])
        root = config.CHAT_WORKTREES_DIR
        root.mkdir()
        (root / ".env.fixture").touch()
        with self.assertRaises(BriefRefused):
            codex_config.chat_worktrees_dir()
        with patch("jarvis.v2.permissions._env_file_within", return_value=(None, "scan limit")):
            self.assertEqual(codex_config.git_dirs(str(self.work)), [])
            with self.assertRaises(BriefRefused):
                codex_config.chat_worktrees_dir()

    def test_worktree_root_symlink_ancestor_or_file_refused(self):
        target = self.root / "real"
        target.mkdir()
        link = self.root / "link"
        link.symlink_to(target, target_is_directory=True)
        file = self.root / "file"
        file.write_text("ordinary fixture")
        for root in (link, link / "child", file):
            with self.subTest(root=root), patch.object(config, "CHAT_WORKTREES_DIR", root):
                with self.assertRaises(BriefRefused):
                    codex_config.prepare("invalid", self.chat(), "/fake/codex")
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)
        self.assertFalse((target / "child").exists())

    def test_existing_worktree_root_is_private_and_config_survives_mcp_removal(self):
        root = config.CHAT_WORKTREES_DIR
        root.mkdir(mode=0o755)
        brief = replace(self.chat(), mcp_servers={"fixture": {"command": "fixture"}})
        h = self.start(brief=brief)
        self.assertEqual(root.stat().st_mode & 0o777, 0o700)
        codex_config.strip_mcp_servers(h.native.home / "config.toml")
        cfg = tomllib.loads((h.native.home / "config.toml").read_text())
        self.assertNotIn("mcp_servers", cfg)
        self.assertFalse(cfg["sandbox_workspace_write"]["network_access"])
        self.assertEqual(cfg["sandbox_workspace_write"]["writable_roots"], [str(root)])

    def test_changed_effective_network_roots_or_returned_network_are_refused(self):
        for script in (
            {"config_override": {"sandbox_workspace_write": {"network_access": True}}},
            {"config_override": {"sandbox_workspace_write": {"writable_roots": ["/"]}}},
            {"sandbox": {"type": "workspaceWrite", "networkAccess": True}},
        ):
            with self.subTest(script=script):
                self.brain.script.update(script)
                try:
                    with self.assertRaises(BriefRefused):
                        self.start()
                finally:
                    for key in script:
                        self.brain.script.pop(key)

    def test_returned_roots_must_match_for_chats_tasks_start_and_resume(self):
        # Native 0.161 returns only configured extra roots here; cwd and the
        # default temporary roots are implicit, not entries in writableRoots.
        for role in (Role.CHAT, Role.IMPLEMENTER):
            brief = replace(self.brief, role=role)
            for resume in (False, True):
                with self.subTest(role=role, resume=resume):
                    if resume:
                        initial = self.start(brief=brief)
                        self.provider.close(initial)
                        thread = replace(self.thread, role=role, provider_session_id=initial.provider_session_id)
                    self.brain.script["sandbox"] = {
                        "type": "workspaceWrite", "networkAccess": False, "writableRoots": ["/"]}
                    try:
                        with self.assertRaises(BriefRefused):
                            if resume:
                                self.provider.resume(thread, brief, lambda *_: Decision.ALLOW)
                            else:
                                self.start(brief=brief)
                    finally:
                        self.brain.script.pop("sandbox")
        # Dropping the promised grants is also a refused chat receipt.
        self.brain.script["sandbox"] = {"type": "workspaceWrite", "networkAccess": False}
        with self.assertRaises(BriefRefused):
            self.start(brief=self.chat())

    def test_example_env_names_do_not_block_a_later_chat(self):
        root = config.CHAT_WORKTREES_DIR
        root.mkdir()
        tree = root / "existing-worktree"
        tree.mkdir()
        (tree / ".env.example").write_text("EXAMPLE=value")
        h = self.start(brief=self.chat())
        self.provider.close(h)
        resumed = self.provider.resume(
            replace(self.thread, role=Role.CHAT, provider_session_id=h.provider_session_id),
            self.chat(), lambda *_: Decision.ALLOW)
        self.handles.append(resumed)
        self.assertEqual(codex_config.git_roots(self.chat()), [str(root)])

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
        self.assertEqual(reports[0].data["partial_turns"], 0)
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

    def test_account_metadata_holds_the_login_lock_and_never_waits_on_a_turn(self):
        """PR #20 review: the metadata peer shares Codex's login lock with
        session start-up; a turn holds that lock for its whole length, so a
        HUD read during one is refused at once (`MetadataBusy`), before the
        CLI is probed or anything is spawned."""
        seen, original = [], self.brain.rpc

        def factory(argv, **kwargs):
            rpc = original(argv, **kwargs)
            real = rpc.request

            def request(method, *args, **kw):
                seen.append((method, codex._auth_lock.locked()))
                return real(method, *args, **kw)
            rpc.request = request
            return rpc

        with patch.object(codex, "RpcProcess", factory):
            self.provider.account_metadata()
        self.assertIn(("model/list", True), seen)
        self.assertTrue(all(locked for _, locked in seen), seen)
        self.assertFalse(codex._auth_lock.locked(), "released afterwards")
        spawned, probes = len(self.brain.rpcs), []
        with patch.object(codex.CodexProvider, "_probe",
                          lambda _self: (probes.append(1), ("/fake/codex", "fake"))[1]):
            self.assertTrue(codex._auth_lock.acquire(timeout=5))
            try:
                started = time.monotonic()
                with self.assertRaises(codex.MetadataBusy):
                    self.provider.account_metadata()
                self.assertLess(time.monotonic() - started, 1)
            finally:
                codex._auth_lock.release()
        self.assertEqual((probes, len(self.brain.rpcs)), ([], spawned))

    QUOTA = {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 11, "windowDurationMins": 300}}}

    def test_account_metadata_refuses_a_looping_or_endless_catalog(self):
        # A cursor seen before is caught at its second use; one that never
        # repeats stops at the page cap. Either way the catalog half is
        # refused (logged by operation and class), the quota half still
        # lands, and the lock is released.
        for script, pages in (({"model_next": "again"}, 2),
                              ({"model_endless": True}, codex.MODEL_PAGE_CAP)):
            self.brain.script = {"mode": "normal", "rate_limits": self.QUOTA, **script}
            before = len(self.brain.calls("model/list"))
            with self.subTest(script=script), self.assertLogs("jarvis.v2.providers.codex", "WARNING") as logged:
                result = self.provider.account_metadata()
            self.assertIsNone(result["models"])
            self.assertEqual(result["rate_limits"], self.QUOTA, "a failed model/list keeps the quota")
            self.assertEqual(" ".join(logged.output).count("model/list failed (RpcError)"), 1)
            self.assertEqual(len(self.brain.calls("model/list")) - before, pages)
            self.assertFalse(codex._auth_lock.locked())

    def test_account_metadata_halves_are_independent(self):
        self.brain.script = {"mode": "normal", "rate_limits": self.QUOTA, "model_error": True}
        with self.assertLogs("jarvis.v2.providers.codex", "WARNING") as logged:
            result = self.provider.account_metadata()
        self.assertEqual((result["models"], result["rate_limits"]), (None, self.QUOTA))
        self.assertNotIn("private detail", " ".join(logged.output), "operation and class only")
        self.brain.script = {"mode": "normal", "rate_error": True}
        result = self.provider.account_metadata()
        self.assertEqual(([r["model"] for r in result["models"]], result["rate_limits"]), (["fake-model"], None))
        self.brain.script = {"mode": "normal", "rate_error": True, "model_error": True}
        with self.assertRaises(codex.RpcError):
            self.provider.account_metadata()
        self.assertFalse(codex._auth_lock.locked())

    def test_account_metadata_has_one_overall_deadline(self):
        """PR #20 review: the session held the login lock across up to 23
        RPCs at 30 s each. One deadline now bounds all of it, so a stalled
        app-server keeps a turn from starting for seconds, not minutes."""
        self.brain.script = {"mode": "normal", "model_stall": True}
        started = time.monotonic()
        with self.assertRaises(codex.RpcError):
            self.provider.account_metadata(deadline_s=1.0)
        self.assertLess(time.monotonic() - started, 4, "bounded by the deadline, not 30 s per call")
        self.assertFalse(codex._auth_lock.locked())
        self.assertEqual(codex.METADATA_DEADLINE_S, 10.0)

    def test_cancel_metadata_ends_a_read_in_flight(self):
        """PR #20 re-review: `Daemon.stop` left a metadata app-server running
        to exit on EOF. `cancel_metadata` ends the read at the transport's
        next poll; the session closes its app-server and frees the lock, and
        no new read starts — not even a probe."""
        self.brain.script = {"mode": "normal", "model_stall": True}
        outcome = {}

        def read():
            try:
                outcome["result"] = self.provider.account_metadata()
            except Exception as exc:
                outcome["error"] = exc
        worker = threading.Thread(target=read, daemon=True)
        worker.start()
        self.brain.wait_call("model/list")
        started = time.monotonic()
        self.provider.cancel_metadata()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertLess(time.monotonic() - started, 2, "well inside the 10 s deadline")
        self.assertIsInstance(outcome.get("error"), RpcError)
        self.assertFalse(codex._auth_lock.locked())
        self.assertTrue(self.brain.rpcs[-1]._closed.is_set(), "its app-server is closed")
        probes = []
        with patch.object(codex.CodexProvider, "_probe",
                          lambda _self: (probes.append(1), ("/fake/codex", "fake"))[1]):
            with self.assertRaises(codex.RpcCancelled):
                self.provider.account_metadata()
        self.assertEqual(probes, [])

    def test_the_login_lock_is_released_even_if_close_raises(self):
        original = self.brain.rpc

        def factory(argv, **kwargs):
            rpc = original(argv, **kwargs)
            real_close = rpc.close

            def close():
                real_close()
                raise OSError("close failed")
            rpc.close = close
            return rpc

        with patch.object(codex, "RpcProcess", factory), self.assertRaises(OSError):
            self.provider.account_metadata()
        self.assertFalse(codex._auth_lock.locked(), "a raising close() cannot leak the lock")

    def test_tool_outcomes_from_item_status(self):
        for mode, expected in (("declined", "denied"), ("tool_failed", "failed")):
            with self.subTest(mode=mode):
                h = self.start(mode)
                finishes = [e for e in self.send(h) if e.kind is K.TOOL_FINISHED]
                self.assertEqual([e.data["name"] for e in finishes], ["shell", "apply_patch"])
                self.assertEqual([e.data["outcome"] for e in finishes], [expected, expected])
                self.assertTrue(all(e.data["ok"] is False for e in finishes))
                self.provider.close(h)

    def test_full_exact_events(self):
        h = self.start("full")
        events = self.send(h)
        self.assertEqual([e.kind for e in events], [K.TURN_STARTED, K.TEXT_DELTA, K.THINKING, K.THINKING,
            K.TOOL_STARTED, K.TOOL_FINISHED, K.TOOL_STARTED, K.TOOL_FINISHED, K.PLAN_UPDATED,
            K.REVIEWER_DECLINED, K.TEXT, K.USAGE, K.TURN_FINISHED])
        self.assertTrue(all(e.thread_id == "test" for e in events))
        self.assertEqual(events[1].data, {"text": "Hello"})
        self.assertEqual(events[4].data, {"call_id": "cmd", "name": "shell", "args": {"command": "echo ok", "cwd": str(self.work)}})
        self.assertEqual(events[5].data, {"call_id": "cmd", "name": "shell", "ok": True, "outcome": "ok", "summary": "exit code 0"})
        self.assertEqual(events[6].data["args"]["paths"], ["ok.txt"])
        self.assertEqual(events[7].data, {"call_id": "patch", "name": "apply_patch", "ok": True, "outcome": "ok", "summary": "1 files, +2/-1 lines"})
        self.assertEqual(events[8].data["plan"], [{"step": "write", "status": "completed"}])
        self.assertEqual(events[9].data["reason"], "declined")
        self.assertEqual(events[-2].data, {"input": 100, "output": 20, "cached": 40, "cost_usd": None,
                                         "provider_reported": {"total": UNIT, "last": UNIT}, "partial_turns": 0})
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

    def scripted(self, method, params, permit, *, item=SEEN, no_turn=False, brief=None, updates=None):
        if item is SEEN:
            # A file approval names no paths: Codex first starts the
            # fileChange item it is about (a benign one here) or it is
            # declined unasked. Pass item=None for an unseen item.
            item = ({"id": params.get("itemId"), "type": "fileChange", "status": "inProgress",
                     "changes": [{"path": "notes.txt", "diff": "+x", "kind": {"type": "add"}}]}
                    if method == "item/fileChange/requestApproval" and isinstance(params, dict) else None)
        self.brain.script.update(method=method, params=params, item=item, no_turn=no_turn, updates=updates)
        name = f"s{len(self.brain.rpcs)}"
        h = self.start("scripted", permit, thread=replace(self.thread, id=name), brief=brief)
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

    def test_chat_bare_approvals_require_owner_even_for_rules_allow(self):
        from jarvis.v2.approvals import PendingApprovals
        from jarvis.v2.permissions import L_WIDENING, deny_all
        brief = self.chat()
        shapes = [(CMD, {"itemId": "c", "command": "git status"}),
                  (CMD, {"itemId": "c", "command": "docker build .", "kind": "command",
                         "additionalPermissions": {"network": {"enabled": False}}}),
                  ("item/fileChange/requestApproval", {"itemId": "f"})]
        for method, params in shapes:
            with self.subTest(method=method, params=params):
                for asker in (deny_all("no owner surface"), PendingApprovals(timeout_s=.01),
                              RecordingAsker(Decision.DENY)):
                    _, _, reply = self.scripted(method, params, self.real_permit(asker), brief=brief)
                    self.assertEqual(reply["result"], {"decision": "decline"})
                    if isinstance(asker, RecordingAsker):
                        self.assertEqual(len(asker.seen), 1)
                        self.assertEqual(asker.seen[0].layer, L_WIDENING)
                        self.assertFalse(asker.seen[0].allowlistable)
                asker = RecordingAsker(Decision.ALLOW)
                _, _, reply = self.scripted(method, params, self.real_permit(asker), brief=brief)
                self.assertEqual(reply["result"], {"decision": "accept"})
                self.assertEqual(len(asker.seen), 1)
                self.assertIn("owner approval required", asker.seen[0].headline)
                self.assertFalse(asker.seen[0].allowlistable)
                # An older callback that cannot receive the widening keyword
                # must not silently approve this through its positional API.
                _, _, reply = self.scripted(method, params, lambda *a: Decision.ALLOW, brief=brief)
                self.assertEqual(reply["result"], {"decision": "decline"})

    def test_owner_only_merge_card_says_what_it_does(self):
        """Owner decision 2026-10-15: a Codex task's merge or close reaches
        the owner with a card that leads with it, at layer 2, with no Always,
        and declined with no human. A chat's escalated merge, close or push
        has no GitHub login to run with: CG-1 declines it unasked and points
        Codex at the Jarvis git tools (a merge spelled that way included,
        never asked first). A task's branch push is still the reviewer's."""
        from jarvis.v2 import chat_git
        from jarvis.v2.permissions import L_ALWAYS_ASK, deny_all
        cases = (("/bin/bash -lc 'gh pr merge 5 --squash'", "merges a pull request"),
                 ("/bin/bash -lc 'gh pr close 5'", "closes a pull request"),
                 ("/bin/bash -lc 'git push origin HEAD:main'", "pushes to main"),
                 ("/bin/bash -lc 'git push --force origin topic'", "force-pushes"))
        for command, words in cases:
            params = {"itemId": "c", "command": command}
            with self.subTest(command=command, role="task"):
                asker = RecordingAsker(Decision.ALLOW)
                _, _, reply = self.scripted(CMD, params, self.real_permit(asker), brief=self.brief)
                self.assertEqual(reply["result"], {"decision": "accept"})
                self.assertEqual(len(asker.seen), 1)
                self.assertTrue(asker.seen[0].headline.startswith(words), asker.seen[0].headline)
                self.assertEqual(asker.seen[0].layer, L_ALWAYS_ASK)
                self.assertFalse(asker.seen[0].allowlistable)
                _, _, reply = self.scripted(CMD, params, self.real_permit(deny_all("nobody")),
                                            brief=self.brief)
                self.assertEqual(reply["result"], {"decision": "decline"})
            with self.subTest(command=command, role="chat"):
                asker = RecordingAsker(Decision.ALLOW)
                _, events, reply = self.scripted(CMD, params, self.real_permit(asker), brief=self.chat())
                self.assertEqual(reply["result"], {"decision": "decline"})
                self.assertEqual(asker.seen, [], "declined unasked, never a card first")
                self.assertTrue(any(e.kind == K.ERROR and chat_git.POINTER in e.data["message"]
                                    for e in events), "the owner sees the pointer")
        push = {"itemId": "c", "command": "/bin/bash -lc 'git push -u origin topic'"}
        asker = RecordingAsker(Decision.ALLOW)
        _, _, reply = self.scripted(CMD, push, self.real_permit(asker), brief=self.chat())
        self.assertEqual(reply["result"], {"decision": "decline"})
        self.assertEqual(asker.seen, [])
        asker = RecordingAsker(Decision.ALLOW)
        _, _, reply = self.scripted(CMD, push, self.real_permit(asker), brief=self.brief)
        self.assertEqual(reply["result"], {"decision": "accept"})
        self.assertEqual(asker.seen, [])

    def test_cg1_raw_network_git_and_gh_are_declined_with_the_pointer(self):
        """CG-1 (owner, 2026-10-15): a Codex chat's escalated gh, or git
        push/fetch/pull/ls-remote to a remote, cannot succeed without a login.
        Declined unasked, logged at the chat-git layer, the pointer shown;
        a line it cannot be sure of still takes the ordinary widening ask."""
        import json as _json
        from jarvis.v2 import chat_git
        from jarvis.v2.permissions import L_CHAT_GIT, L_WIDENING, deny_all
        for command in ("git push -u origin topic", "/bin/bash -lc 'gh pr create --fill'",
                        "gh pr merge 3 --squash", "git fetch origin", "git pull --rebase",
                        "git ls-remote origin", "env GH_PAGER=cat gh api user",
                        "cd sub && git -C . push", "npm test && git push"):
            with self.subTest(command=command):
                asker = RecordingAsker(Decision.ALLOW)
                _, events, reply = self.scripted(CMD, {"itemId": "c", "command": command},
                                                 self.real_permit(asker), brief=self.chat())
                self.assertEqual(reply["result"], {"decision": "decline"})
                self.assertEqual(asker.seen, [])
                self.assertTrue(any(e.kind == K.ERROR and chat_git.POINTER in e.data["message"]
                                    for e in events))
                log = [_json.loads(line) for line in
                       (config.V2_DATA_DIR / "decisions.jsonl").read_text().splitlines()]
                self.assertEqual((log[-1]["layer"], log[-1]["reason"]), (L_CHAT_GIT, chat_git.POINTER))
        for command in ("$GIT push origin topic", "git fetch /srv/mirror.git x", "git status",
                        "docker build ."):
            with self.subTest(unsure=command):
                asker = RecordingAsker(Decision.ALLOW)
                _, events, reply = self.scripted(CMD, {"itemId": "c", "command": command},
                                                 self.real_permit(asker), brief=self.chat())
                self.assertEqual(reply["result"], {"decision": "accept"})
                self.assertEqual(len(asker.seen), 1, "the ordinary widening card")
                self.assertEqual(asker.seen[0].layer, L_WIDENING)
                self.assertFalse(any(e.kind == K.ERROR for e in events))
                _, _, reply = self.scripted(CMD, {"itemId": "c", "command": command},
                                            self.real_permit(deny_all("nobody")), brief=self.chat())
                self.assertEqual(reply["result"], {"decision": "decline"})

    def test_clean_env_is_exact_plus_the_owners_identity(self):
        """Codex's environment gains only GIT_AUTHOR_*/GIT_COMMITTER_* (CG-0):
        never a PATH change, GIT_CONFIG_* or GH_* — no login, helper or token
        path. An identity value that is not one printable line is dropped."""
        base = {"HOME", "CODEX_HOME", "PATH", "LANG", "TZ", "TMPDIR"}
        ident = {"GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"}
        home = self.root / "ownerhome"
        home.mkdir()
        leaky = {"HOME": str(home), "GH_CONFIG_DIR": str(home / "gh"), "GH_TOKEN": "x",
                 "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"), "GITHUB_TOKEN": "x",
                 "PATH": f"{home}/.local/bin:/usr/bin:/bin", "XDG_CONFIG_HOME": str(home / "xdg")}
        cases = (('[user]\n\tname = Owner Name\n\temail = owner@example.invalid\n',
                  {"GIT_AUTHOR_NAME": "Owner Name", "GIT_COMMITTER_NAME": "Owner Name",
                   "GIT_AUTHOR_EMAIL": "owner@example.invalid",
                   "GIT_COMMITTER_EMAIL": "owner@example.invalid"}),
                 ('[user]\n\tname = "Two\\nLines"\n\temail = owner@example.invalid\n',
                  {"GIT_AUTHOR_EMAIL": "owner@example.invalid",
                   "GIT_COMMITTER_EMAIL": "owner@example.invalid"}),
                 ('[user]\n\tname = "Tab\\there"\n\temail = "' + "x" * 250 + '"\n', {}),
                 ("", {}))
        for text, want in cases:
            with self.subTest(config=text[:40]):
                (home / ".gitconfig").write_text(text)
                codex_config._IDENTITY = None
                try:
                    with patch.dict(os.environ, leaky):
                        env = codex_config.clean_env(self.root / "h")
                finally:
                    codex_config._IDENTITY = None
                self.assertEqual(set(env), base | set(want))
                self.assertEqual({k: env[k] for k in ident if k in env}, want)
                self.assertEqual(env["PATH"], "/usr/local/bin:/usr/bin:/bin")
                self.assertFalse(any(k.startswith(("GH_", "GIT_CONFIG", "GITHUB")) for k in env))

    def test_chat_explicit_network_and_unknown_grants_still_require_owner(self):
        for extra in ({"networkApprovalContext": {"host": "example.invalid", "protocol": "https"}},
                      {"additionalPermissions": {"network": {"enabled": True}}},
                      {"futureGrant": {"scope": "unknown"}}):
            asker = RecordingAsker(Decision.DENY)
            _, _, reply = self.scripted(CMD, {"itemId": "c", "command": "git status", **extra},
                                         self.real_permit(asker), brief=self.chat())
            self.assertEqual(reply["result"], {"decision": "decline"})
            self.assertEqual(len(asker.seen), 1)
            self.assertFalse(asker.seen[0].allowlistable)
        asker = RecordingAsker(Decision.ALLOW)
        permit = self.real_permit(asker)
        for params in ({"itemId": "c", "command": "sudo rm -rf /"},
                       {"itemId": "c", "command": "git status",
                        "additionalPermissions": {"fileSystem": {"write": [str(self.creds)]}}}):
            _, _, reply = self.scripted(CMD, params, permit, brief=self.chat())
            self.assertEqual(reply["result"], {"decision": "decline"})
        self.assertEqual(asker.seen, [], "hard denials precede every owner approval")

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

    def rename(self, move, *, kind=None, path="notes.txt"):
        """A fileChange item renaming `path` (generate-ts 0.161.0:
        FileUpdateChange.kind = {type: update, move_path})."""
        kind = kind if kind is not None else {"type": "update", "move_path": move}
        return {"id": "patch", "type": "fileChange", "status": "inProgress",
                "changes": [{"path": path, "diff": "@@\n-old\n+new", "kind": kind}]}

    def test_file_rename_destination_is_judged_by_layer_1(self):
        """A rename's destination is a file written: refused unasked when it
        is a credential file or the gate's own state, in a chat and in a task
        (whose plain file approvals no human sees under AUTO)."""
        file_approval = "item/fileChange/requestApproval"
        asker = RecordingAsker(Decision.ALLOW)
        permit = self.real_permit(asker)
        gate_state = str(config.ALLOWLIST_PATH)         # the hermetic allowlist real_permit set
        for label, move in (("credential file", str(self.work / ".env")),
                            ("credential file, relative", ".env.local"),
                            ("gate state", gate_state)):
            for brief in (self.chat(), self.brief):
                with self.subTest(case=label, role=brief.role):
                    _, events, reply = self.scripted(file_approval, {"itemId": "patch"}, permit,
                                                     item=self.rename(move), brief=brief)
                    self.assertEqual(reply["result"], {"decision": "decline"})
                    self.assertEqual(asker.seen, [], "refused by layer 1, never asked")
                    asked = next(e.data for e in events if e.kind == K.APPROVAL_REQUESTED)
                    self.assertEqual(asked["args"]["paths"], ["notes.txt", move])
                    self.assertEqual(events[-1].data, {"stop": "end"})
        # A benign rename is asked with both of its paths on the card.
        _, events, reply = self.scripted(file_approval, {"itemId": "patch"}, permit,
                                         item=self.rename("docs/notes.md"), brief=self.chat())
        self.assertEqual(reply["result"], {"decision": "accept"})
        self.assertEqual(asker.seen[-1].args["paths"], ["notes.txt", "docs/notes.md"])
        self.assertIn("docs/notes.md", json.dumps(asker.seen[-1].to_json()))

    @staticmethod
    def change(path, kind=None, move=None):
        kind = kind or ({"type": "update", "move_path": move} if move else {"type": "add"})
        return {"path": path, "diff": "+x", "kind": kind}

    def test_file_approval_is_judged_on_every_version_of_its_item(self):
        """`item/fileChange/patchUpdated` (session path): an approval is
        judged on the item as updated, on the union of every path any version
        reported (a later, partial update cannot hide an earlier destination),
        and declined if any version could not be read. A request's own
        `paths` never replaces them."""
        file_approval = "item/fileChange/requestApproval"
        start = lambda *changes: {"id": "patch", "type": "fileChange", "status": "inProgress",
                                  "changes": list(changes)}
        refused = [
            ("a destination added by an update", start(self.change("notes.txt")),
             [[self.change("notes.txt", move=".env")]], {"itemId": "patch"}),
            ("a destination an update later drops", start(self.change("notes.txt", move=".env")),
             [[self.change("notes.txt")]], {"itemId": "patch"}),
            ("a request naming its own paths", start(self.change("notes.txt", move=".env")),
             [], {"itemId": "patch", "paths": ["notes.txt"]}),
        ]
        for label, item, updates, params in refused:
            for brief in (self.chat(), self.brief):
                with self.subTest(case=label, role=brief.role):
                    asker = RecordingAsker(Decision.ALLOW)
                    _, events, reply = self.scripted(file_approval, params, self.real_permit(asker),
                                                     item=item, updates=updates, brief=brief)
                    self.assertEqual(reply["result"], {"decision": "decline"})
                    self.assertEqual(asker.seen, [], "refused by layer 1, never asked")
                    asked = next(e.data for e in events if e.kind == K.APPROVAL_REQUESTED)
                    self.assertEqual(asked["args"]["paths"], ["notes.txt", ".env"])
        # An unreadable version is not forgotten when a readable one follows.
        for brief in (self.chat(), self.brief):
            with self.subTest(case="an unreadable version, then a readable one", role=brief.role):
                asker = RecordingAsker(Decision.ALLOW)
                _, events, reply = self.scripted(
                    file_approval, {"itemId": "patch"}, self.real_permit(asker),
                    item=start(self.change("notes.txt", kind={"type": "copy"})),
                    updates=[[self.change("notes.txt")]], brief=brief)
                self.assertEqual(reply["result"], {"decision": "decline"})
                self.assertEqual(asker.seen, [])
                self.assertEqual([e.data["message"] for e in events if e.kind == K.ERROR],
                                 ["Codex approval request declined: a change's kind cannot be read"])
        # A benign item that grew: asked, with every file on the card.
        asker = RecordingAsker(Decision.ALLOW)
        _, events, reply = self.scripted(file_approval, {"itemId": "patch"}, self.real_permit(asker),
                                         item=start(self.change("docs/a.md")),
                                         updates=[[self.change("docs/a.md"), self.change("docs/b.md")],
                                                  [self.change("docs/b.md", move="docs/c.md")]],
                                         brief=self.chat())
        self.assertEqual(reply["result"], {"decision": "accept"})
        self.assertEqual(asker.seen[-1].args["paths"], ["docs/a.md", "docs/b.md", "docs/c.md"])

    def test_file_change_kind_that_cannot_be_read_is_declined(self):
        file_approval = "item/fileChange/requestApproval"
        shapes = {
            "no kind": self.rename(None, kind={}),
            "kind not an object": self.rename(None, kind="update"),
            "unknown kind": self.rename(None, kind={"type": "rename", "to": ".env"}),
            "unknown kind type alone": self.rename(None, kind={"type": "copy"}),
            "move_path not a string": self.rename(None, kind={"type": "update", "move_path": [".env"]}),
            "move_path on an add": self.rename(None, kind={"type": "add", "move_path": ".env"}),
            "unknown field on the kind": self.rename(None, kind={"type": "update", "move_path": None,
                                                                 "copy_path": ".env"}),
            "a change with no path": {"id": "patch", "type": "fileChange", "status": "inProgress",
                                      "changes": [{"diff": "+x", "kind": {"type": "add"}}]},
            "changes not a list": {"id": "patch", "type": "fileChange", "status": "inProgress",
                                   "changes": {"path": ".env"}},
        }
        for label, item in shapes.items():
            with self.subTest(shape=label):
                if label == "no kind":
                    item["changes"][0].pop("kind")
                calls = []

                def permit(*a, **k):
                    calls.append(a)
                    return Decision.ALLOW
                _, events, reply = self.scripted(file_approval, {"itemId": "patch"}, permit,
                                                 item=item, brief=self.chat())
                self.assertEqual(reply["result"], {"decision": "decline"})
                self.assertEqual(calls, [], "never put to the gate")
                errors = [e.data for e in events if e.kind == K.ERROR]
                self.assertEqual(len(errors), 1, errors)
                self.assertFalse(errors[0]["fatal"])
                self.assertTrue(errors[0]["message"].startswith("Codex approval request declined: "), errors)
                self.assertEqual(events[-1].data, {"stop": "end"})

    def test_file_approval_without_a_readable_file_item_is_declined_unasked(self):
        """Owner rule: unknown approvals fail closed. A file approval carries
        no paths, so with no fileChange item naming a file there is nothing
        for layer 1 to judge: declined unasked, in a chat and in a task (whose
        AUTO fallback once accepted it with nobody asked), with a notice."""
        from jarvis.v2.permissions import deny_all
        file_approval = "item/fileChange/requestApproval"
        command_item = {"id": "patch", "type": "commandExecution", "command": "echo ok",
                        "cwd": str(self.work), "status": "inProgress"}
        empty = {"id": "patch", "type": "fileChange", "status": "inProgress", "changes": []}
        cases = [("unseen item", file_approval, None, "its item was not seen"),
                 ("a command item", file_approval, command_item, "its item is not a fileChange"),
                 ("no changes", file_approval, empty, "its item names no file"),
                 # The other way round: a command approval naming a patch
                 # would be judged as apply_patch, never as its command.
                 ("a command approval naming a file item", CMD, self.rename("docs/x.md"),
                  "its item is not a commandExecution")]
        for label, method, item, why in cases:
            for brief in (self.chat(), self.brief):
                for asker in (deny_all("nobody"), RecordingAsker(Decision.ALLOW)):
                    with self.subTest(case=label, role=brief.role, asker=type(asker).__name__):
                        params = {"itemId": "patch"} if method == file_approval else {
                            "itemId": "patch", "command": "echo ok"}
                        _, events, reply = self.scripted(method, params, self.real_permit(asker),
                                                         item=item, brief=brief)
                        self.assertEqual(reply["result"], {"decision": "decline"})
                        if isinstance(asker, RecordingAsker):
                            self.assertEqual(asker.seen, [])
                        self.assertNotIn(K.APPROVAL_REQUESTED, [e.kind for e in events])
                        errors = [e.data for e in events if e.kind == K.ERROR]
                        self.assertEqual(errors, [{"fatal": False,
                                                   "message": f"Codex approval request declined: {why}"}])
                        self.assertEqual(events[-1].data, {"stop": "end"})
        # A command approval with an unseen item is unchanged: it carries its
        # command, which is judged and put to the owner.
        asker = RecordingAsker(Decision.ALLOW)
        _, events, reply = self.scripted(CMD, {"itemId": "unseen", "command": "echo ok"},
                                         self.real_permit(asker), brief=self.chat())
        self.assertEqual(reply["result"], {"decision": "accept"})
        self.assertEqual(asker.seen[-1].command, "echo ok")

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

    # --- Codex sub-agents' server requests (SV-2a), from the SV-0 recordings ---

    ASKER = "/root/sv0_asker"
    UNKNOWN = "01a140cb-0000-7000-8000-unannounced0"

    def replay(self, name, permit, *, edit=None, brief=None):
        msgs = fixture_turn(name)
        if edit is not None:
            msgs = edit(msgs)
        self.brain.script["replay"] = msgs
        h = self.start("replay", permit, thread=replace(self.thread, id=f"r{len(self.brain.rpcs)}"),
                       brief=brief or self.chat())
        # An active deadline: a request left waiting on nobody (a child's
        # question put to the owner) must fail the test, not hang it.
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.send, h)
            try:
                return h, future.result(timeout=20)
            except TimeoutError:
                self.provider.close(h)
                future.result(timeout=5)
                self.fail(f"the replayed {name} turn never finished")

    @staticmethod
    def request_at(msgs):
        return next(i for i, m in enumerate(msgs) if "id" in m and "method" in m)

    @staticmethod
    def spawn_at(msgs, path):
        return next(i for i, m in enumerate(msgs) if m.get("method") == "item/started"
                    and m["params"]["item"].get("type") == "subAgentActivity"
                    and m["params"]["item"].get("kind") == "started"
                    and m["params"]["item"].get("agentPath") == path)

    @staticmethod
    def sid_of(h, path):
        """The sid of the known child whose label is `path` (SV-3: a child's
        approval names its sid, not its label)."""
        return "cx:" + next(tid for tid, label in h.native.children.items() if label == path)

    def answer_to(self, h, rid=0):
        replies = [m for m in h.native.rpc.replies if m["id"] == rid]
        self.assertEqual(len(replies), 1, replies)
        return replies[0]

    def assert_parent_went_on(self, h, events):
        """The orchestrator's turn ran to its own end, and no child's usage
        reached its accounting (SV-0 finding 13: 110 x 4 tokens)."""
        fatal = [e.data for e in events if e.kind == K.ERROR and e.data.get("fatal")]
        self.assertEqual(fatal, [])
        self.assertEqual(events[-1].kind, K.TURN_FINISHED)
        self.assertEqual(events[-1].data, {"stop": "end"})
        self.assertIn("SV0 parent: approval done at step 3.",
                      [e.data["text"] for e in events if e.kind == K.TEXT])
        self.assertEqual(h.native.accounting.total["inputTokens"], 400)

    def counting(self, decision=Decision.ALLOW):
        calls = []

        def permit(*args, **kwargs):
            calls.append((args, kwargs))
            return decision
        return permit, calls

    def test_child_approval_goes_through_the_gate_named_and_never_ends_the_turn(self):
        from jarvis.v2.permissions import deny_all
        cases = [("no human", "approval", deny_all("nobody"), "decline"),
                 ("owner says no", "approval", RecordingAsker(Decision.DENY), "decline"),
                 ("owner says yes", "approval_accept", RecordingAsker(Decision.ALLOW), "accept")]
        for label, name, asker, want in cases:
            for brief in (self.chat(), self.brief):     # an owner chat, and a task (auto_review)
                with self.subTest(case=label, role=brief.role):
                    h, events = self.replay(name, self.real_permit(asker), brief=brief)
                    self.assert_parent_went_on(h, events)
                    self.assertEqual(self.answer_to(h)["result"], {"decision": want})
                    asked = next(e.data for e in events if e.kind == K.APPROVAL_REQUESTED)
                    # The child's sid (the owner's SV-3 decision), never its label.
                    self.assertEqual(asked["subagent"], self.sid_of(h, self.ASKER))
                    self.assertRegex(asked["subagent"], r"^cx:[A-Za-z0-9_-]{1,128}$")
                    if isinstance(asker, RecordingAsker):
                        # The owner's card carries it too, for the HUD's link.
                        self.assertEqual(asker.seen[-1].subagent, asked["subagent"])
                        self.assertEqual(asker.seen[-1].to_json()["subagent"], asked["subagent"])
                    self.assertEqual(asked["command"], "/bin/bash -lc 'touch /sv0-outside-workspace'")
                    resolved = next(e.data for e in events if e.kind == K.APPROVAL_RESOLVED)
                    self.assertEqual(resolved["decision"], "allow" if want == "accept" else "deny")
                    if isinstance(asker, RecordingAsker):
                        # Asked as a widening every time, naming the sub-agent,
                        # with no Always — even for a task's auto_review.
                        shown = asker.seen[-1]
                        self.assertTrue(shown.headline.startswith(
                            "SANDBOX WIDENING: Codex sub-agent ‹/root/sv0_asker› (not the orchestrator) "
                            "asks to run beyond its sandbox"),
                            shown.headline)
                        self.assertFalse(shown.allowlistable)
        # A callback that would allow anything ordinary is not a gate for a
        # sub-agent: it cannot take the widening keyword, so the child is declined.
        h, events = self.replay("approval_accept", lambda *a: Decision.ALLOW)
        self.assert_parent_went_on(h, events)
        self.assertEqual(self.answer_to(h)["result"], {"decision": "decline"})

    def test_child_request_before_the_turn_start_reply_keeps_the_turn_identity(self):
        """A child's turnId is its own: it never becomes the session's turn,
        or the turn/start reply would read as an identity change."""
        from jarvis.v2.permissions import deny_all

        def late_start(msgs):
            start = msgs.pop(0)
            msgs.insert(self.request_at(msgs) + 1, start)
            return msgs
        h, events = self.replay("approval", self.real_permit(deny_all("nobody")), edit=late_start)
        self.assert_parent_went_on(h, events)
        self.assertEqual(self.answer_to(h)["result"], {"decision": "decline"})

    def test_child_approval_is_judged_on_its_own_item(self):
        asker = RecordingAsker(Decision.ALLOW)

        def no_item(msgs):
            return [m for m in msgs if not (m.get("method") == "item/started"
                                            and m["params"]["item"].get("type") == "commandExecution")]
        h, events = self.replay("approval_accept", self.real_permit(asker), edit=no_item)
        self.assert_parent_went_on(h, events)
        self.assertEqual(self.answer_to(h)["result"], {"decision": "decline"})
        self.assertEqual(asker.seen, [])
        self.assertIn("its item was not seen (from Codex sub-agent ‹/root/sv0_asker›)",
                      [e.data["message"] for e in events if e.kind == K.ERROR][0])

        def env_write(msgs, change=None):
            # The child's item becomes a write to a credential file: a file
            # approval names no paths, so only the item tells layer 1.
            change = change or {"path": "$CWD/.env", "diff": "+TOKEN=x", "kind": {"type": "add"}}
            i = self.request_at(msgs)
            req = msgs[i]
            item_at = next(j for j, m in enumerate(msgs) if m.get("method") == "item/started"
                           and m["params"]["item"].get("id") == req["params"]["itemId"])
            msgs[item_at]["params"]["item"] = {"type": "fileChange", "id": req["params"]["itemId"],
                                               "status": "inProgress",
                                               "changes": [change]}
            msgs[i] = {"method": "item/fileChange/requestApproval", "id": 0, "params": {
                k: req["params"][k] for k in ("threadId", "turnId", "itemId", "reason")}}
            return msgs
        h, events = self.replay("approval_accept", self.real_permit(asker), edit=env_write)
        self.assert_parent_went_on(h, events)
        self.assertEqual(self.answer_to(h)["result"], {"decision": "decline"})
        self.assertEqual(asker.seen, [], "a protected write is refused unasked")
        self.assertEqual([e for e in events if e.kind == K.ERROR], [])
        asked = next(e.data for e in events if e.kind == K.APPROVAL_REQUESTED)
        self.assertEqual(asked["args"]["paths"], [str(self.work / ".env")])

        # A child's rename onto a credential file (#196's move_path, on the
        # child path): the destination is judged, refused unasked.
        rename = {"path": "notes.txt", "diff": "@@\n-a\n+b", "kind": {"type": "update", "move_path": ".env"}}
        h, events = self.replay("approval_accept", self.real_permit(asker),
                                edit=lambda msgs: env_write(msgs, rename))
        self.assert_parent_went_on(h, events)
        self.assertEqual(self.answer_to(h)["result"], {"decision": "decline"})
        self.assertEqual(asker.seen, [], "a rename onto .env is refused unasked")
        self.assertEqual(next(e.data for e in events if e.kind == K.APPROVAL_REQUESTED)["args"]["paths"],
                         ["notes.txt", ".env"])

        def unreadable(changes, method=CMD):
            def edit(msgs):
                msgs = env_write(msgs)
                i = self.request_at(msgs)
                msgs[i]["method"] = method
                item = next(m for m in msgs if m.get("method") == "item/started"
                            and m["params"]["item"].get("id") == msgs[i]["params"]["itemId"])
                item["params"]["item"]["changes"] = changes
                return msgs
            return edit
        file_approval = "item/fileChange/requestApproval"
        for label, edit, why in (
                ("a command approval naming a file item", unreadable([]), "its item is not a commandExecution"),
                ("a file approval whose item writes nothing", unreadable([], file_approval), "its item names no file"),
                ("changes not a list", unreadable("x", file_approval), "changes is not a list"),
                ("a change with no path", unreadable([{"diff": "+x", "kind": {"type": "add"}}], file_approval),
                 "a change names no path"),
                ("an unreadable kind", unreadable([{"path": "a.txt", "diff": "+x", "kind": {"type": "copy"}}],
                                                  file_approval), "a change's kind cannot be read"),
                ("a move_path that is not a path", unreadable(
                    [{"path": "a.txt", "diff": "+x", "kind": {"type": "update", "move_path": [".env"]}}],
                    file_approval), "a change's move_path cannot be read")):
            with self.subTest(case=label):
                h, events = self.replay("approval_accept", self.real_permit(asker), edit=edit)
                self.assert_parent_went_on(h, events)
                self.assertEqual(self.answer_to(h)["result"], {"decision": "decline"})
                self.assertEqual(asker.seen, [])
                self.assertEqual([e.data["message"] for e in events if e.kind == K.ERROR],
                                 [f"Codex approval request declined: {why} (from Codex sub-agent ‹/root/sv0_asker›)"])

    def test_recorded_file_approvals_show_the_rename_destination(self):
        """Codex 0.161.0's own file approvals (SV-2a recordings): a rename out
        of the workspace, by the session and by a child. Both are asked, with
        the destination on the card; the child's names the sub-agent."""
        for name, child in (("patch_approval", None), ("patch_approval_child", "/root/sv0_patcher")):
            with self.subTest(fixture=name):
                asker = RecordingAsker(Decision.DENY)
                h, events = self.replay(name, self.real_permit(asker))
                self.assertEqual(events[-1].data, {"stop": "end"})
                self.assertEqual([e for e in events if e.kind == K.ERROR], [])
                self.assertEqual(self.answer_to(h)["result"], {"decision": "decline"})
                shown = asker.seen[-1]
                self.assertEqual(shown.args["paths"], [str(self.work / "notes.txt"), "/sv0-outside/notes.txt"])
                asked = next(e.data for e in events if e.kind == K.APPROVAL_REQUESTED)
                self.assertEqual(asked.get("subagent"), self.sid_of(h, child) if child else None)
                self.assertEqual(shown.subagent, self.sid_of(h, child) if child else "")
                self.assertEqual("subagent" in shown.to_json(), bool(child))
                if child:
                    self.assertIn(f"Codex sub-agent ‹{child}› (not the orchestrator)", shown.headline)

    def test_file_approval_before_the_turn_start_reply_sees_every_version(self):
        """The recorded session file approval (patch_approval, Codex 0.161.0)
        with an update that adds a credential destination, all of it arriving
        before the turn/start reply: the early path keeps the update too."""
        def early_update(msgs):
            i = self.request_at(msgs)
            req = msgs[i]["params"]
            update = {"method": "item/fileChange/patchUpdated", "params": {
                "threadId": req["threadId"], "turnId": req["turnId"], "itemId": req["itemId"],
                "changes": [self.change("$CWD/notes.txt", move="$CWD/.env")]}}
            msgs.insert(i, update)
            start = msgs.pop(0)
            msgs.insert(self.request_at(msgs) + 1, start)
            return msgs
        asker = RecordingAsker(Decision.ALLOW)
        h, events = self.replay("patch_approval", self.real_permit(asker), edit=early_update)
        self.assertEqual(events[-1].data, {"stop": "end"})
        self.assertEqual(self.answer_to(h)["result"], {"decision": "decline"})
        self.assertEqual(asker.seen, [], "the update's .env destination is refused unasked")
        asked = next(e.data for e in events if e.kind == K.APPROVAL_REQUESTED)
        self.assertEqual(asked["args"]["paths"], [str(self.work / "notes.txt"), "/sv0-outside/notes.txt",
                                                  str(self.work / ".env")])

    UNION_PROBE = """
import json, sys, time
from jarvis.v2.providers import codex
n = int(sys.argv[1])
changes = [{"path": f"src/f{i}.txt", "diff": "+x", "kind": {"type": "add"}} for i in range(n)]
out = {}
for shape in ("cumulative", "one file each"):
    prev = None
    started = time.thread_time()
    for k in range(1, n + 1):
        version = changes[:k] if shape == "cumulative" else changes[k - 1:k]
        prev = codex._remember(prev, {"id": "p", "type": "fileChange", "changes": version})
    seen = prev[codex._PATHS_SEEN]
    out[shape] = {"cpu": time.thread_time() - started, "paths": len(seen.paths), "problem": seen.problem}
print(json.dumps(out))
"""

    def test_the_union_of_an_items_versions_is_bounded_work(self):
        """5000 updates to one item, each resending every change so far (a
        streamed patch may), or adding one file each: `_remember` runs on the
        turn loop's thread, so the whole series must cost under 0.4 CPU s,
        and it ends past judging. Run in a child process with an
        active deadline: a quadratic-per-update union never returns in time."""
        from tests import loadclock
        loadclock.pinned()
        self.assertEqual((codex.CHANGE_PATH_CAP, codex.CHANGE_WORK_CAP), (4096, 1_000_000))
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
        try:
            run = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[2]); "
                                  "exec(sys.argv[3])", "5000", env["PYTHONPATH"], self.UNION_PROBE],
                                 capture_output=True, text=True, timeout=30, env=env)
        except subprocess.TimeoutExpired:
            self.fail("5000 updates to one item took over 30 s on the turn loop's thread")
        self.assertEqual(run.returncode, 0, run.stderr[-2000:])
        out = json.loads(run.stdout)
        cumulative, single = out["cumulative"], out["one file each"]
        self.assertEqual(cumulative["problem"], codex.TOO_MUCH_WORK)
        self.assertEqual((single["paths"], single["problem"]), (4097, codex.TOO_MANY_PATHS))
        # Idle on the owner's machine (2026-10-15): 183 CPU ms cumulative, 3
        # one file each. The old list-scan union cost 1.72 s here (4.3x the
        # bound) and no work cap 1.46 s (3.6x): both past loadclock's 3x, so
        # caught under any load.
        for shape, result in out.items():
            held = loadclock.judge(result["cpu"], 0.4)
            self.assertLess(held.idle, 0.4, f"{shape}: {held}")

    def test_a_file_item_past_judging_is_declined_unasked(self):
        file_approval = "item/fileChange/requestApproval"
        many = lambda n: [self.change(f"src/f{i}.txt") for i in range(n)]
        # At the cap: judged and asked. One past it: declined, nobody asked.
        asker = RecordingAsker(Decision.ALLOW)
        _, _, reply = self.scripted(file_approval, {"itemId": "patch"}, self.real_permit(asker),
                                    item={"id": "patch", "type": "fileChange", "status": "inProgress",
                                          "changes": many(4096)}, brief=self.chat())
        self.assertEqual(reply["result"], {"decision": "accept"})
        self.assertEqual(len(asker.seen[-1].args["paths"]), 4096)
        for label, item, updates in (
                ("one version past the cap", many(4097), None),
                # 5000 files arriving in 50 incremental updates through the
                # transport, under an active deadline. (5000 one-file updates
                # are timed in-process above: a burst of that many messages
                # races the 4096-message RPC queue, the known limit SV-6 owns.)
                ("5000 files in 50 incremental updates", many(100),
                 [many(5000)[k:k + 100] for k in range(100, 5000, 100)])):
            for brief in (self.chat(), self.brief):
                with self.subTest(case=label, role=brief.role):
                    asker = RecordingAsker(Decision.ALLOW)
                    with ThreadPoolExecutor(1) as pool:
                        future = pool.submit(self.scripted, file_approval, {"itemId": "patch"},
                                             self.real_permit(asker), brief=brief, updates=updates,
                                             item={"id": "patch", "type": "fileChange",
                                                   "status": "inProgress", "changes": item})
                        _, events, reply = future.result(timeout=60)
                    self.assertEqual(reply["result"], {"decision": "decline"})
                    self.assertEqual(asker.seen, [])
                    self.assertEqual([e.data["message"] for e in events if e.kind == K.ERROR],
                                     [f"Codex approval request declined: {codex.TOO_MANY_PATHS}"])
                    self.assertEqual(events[-1].data, {"stop": "end"})

    def test_the_card_shows_the_items_changes_never_the_requests(self):
        asker = RecordingAsker(Decision.ALLOW)
        item = {"id": "patch", "type": "fileChange", "status": "inProgress",
                "changes": [self.change("docs/real.md")]}
        _, _, reply = self.scripted("item/fileChange/requestApproval",
                                    {"itemId": "patch", "changes": [{"path": "harmless.txt", "diff": "+fake"}]},
                                    self.real_permit(asker), item=item, brief=self.chat())
        self.assertEqual(reply["result"], {"decision": "accept"})
        self.assertEqual(asker.seen[-1].args["changes"], item["changes"])
        self.assertEqual(asker.seen[-1].args["paths"], ["docs/real.md"])

    def test_child_file_approval_is_judged_on_every_version_of_its_item(self):
        """The same for a child: its `patchUpdated` counts, the union of its
        item's paths is judged, and an unreadable version is not forgotten."""
        def file_item(started, updates):
            def edit(msgs):
                i = self.request_at(msgs)
                req = msgs[i]
                at = next(j for j, m in enumerate(msgs) if m.get("method") == "item/started"
                          and m["params"]["item"].get("id") == req["params"]["itemId"])
                ids = {k: req["params"][k] for k in ("threadId", "turnId")}
                msgs[at]["params"]["item"] = {"type": "fileChange", "id": req["params"]["itemId"],
                                              "status": "inProgress", "changes": started}
                msgs[i] = {"method": "item/fileChange/requestApproval", "id": 0,
                           "params": {**ids, "itemId": req["params"]["itemId"]}}
                msgs[at + 1:at + 1] = [{"method": "item/fileChange/patchUpdated", "params": {
                    **ids, "itemId": req["params"]["itemId"], "changes": changes}} for changes in updates]
                return msgs
            return edit
        cases = [
            ("a destination added by an update", [self.change("notes.txt")],
             [[self.change("notes.txt", move=".env")]], None),
            ("a destination an update later drops", [self.change("notes.txt", move=".env")],
             [[self.change("notes.txt")]], None),
            ("an unreadable version, then a readable one", [self.change("notes.txt", kind={"type": "copy"})],
             [[self.change("notes.txt")]], "a change's kind cannot be read"),
        ]
        for label, started, updates, why in cases:
            with self.subTest(case=label):
                asker = RecordingAsker(Decision.ALLOW)
                h, events = self.replay("approval_accept", self.real_permit(asker), edit=file_item(started, updates))
                self.assert_parent_went_on(h, events)
                self.assertEqual(self.answer_to(h)["result"], {"decision": "decline"})
                self.assertEqual(asker.seen, [])
                errors = [e.data["message"] for e in events if e.kind == K.ERROR]
                if why:
                    self.assertEqual(errors, [f"Codex approval request declined: {why} "
                                              "(from Codex sub-agent ‹/root/sv0_asker›)"])
                else:
                    self.assertEqual(errors, [])
                    asked = next(e.data for e in events if e.kind == K.APPROVAL_REQUESTED)
                    self.assertEqual(asked["args"]["paths"], ["notes.txt", ".env"])

    def test_child_elicitation_clock_question_and_others_are_never_fatal(self):
        def swap(method, params):
            def edit(msgs):
                i = self.request_at(msgs)
                identity = {k: msgs[i]["params"][k] for k in ("threadId", "turnId")}
                msgs[i] = {"method": method, "id": 0, "params": {**identity, **params}}
                return msgs
            return edit
        child_only = {"turnId": None}
        cases = [
            ("elicitation", swap(codex.MCP_ELICITATION, {**child_only, "serverName": "jarvis", "message": "ok?"}),
             lambda r: self.assertEqual(r["result"], {"action": "decline", "content": None}), codex.MCP_DECLINED),
            ("clock", swap("currentTime/read", child_only),
             lambda r: self.assertEqual(set(r["result"]), {"currentTimeAt"}), None),
            ("question", swap("item/tool/requestUserInput", {"itemId": "q", "questions": [
                {"id": "q1", "question": "Which color?", "options": [{"label": "Blue"}]}]}),
             lambda r: self.assertIn("error", r), "question was not shown"),
            ("mcp approval as a question", swap("item/tool/requestUserInput", {"itemId": "q", "questions": [
                {"id": "mcp_tool_call_approval_1", "question": "Allow?", "options": []}]}),
             lambda r: self.assertIn("error", r), codex.MCP_DECLINED),
            ("permissions", swap("item/permissions/requestApproval", {"itemId": "p", "permissions": {"network": True}}),
             lambda r: self.assertEqual(r["result"], {"permissions": {}, "scope": "turn"}), None),
            ("unknown method", swap("unrecognized/tool", {}),
             lambda r: self.assertIn("error", r), "Unexpected Codex server request refused"),
            ("approval naming no turn", swap(CMD, {"turnId": None}),
             lambda r: self.assertIn("error", r), "it names no turn"),
        ]
        for label, edit, check, notice in cases:
            with self.subTest(case=label):
                permit, calls = self.counting()
                h, events = self.replay("approval", permit, edit=edit)
                self.assert_parent_went_on(h, events)
                check(self.answer_to(h))
                self.assertEqual(calls, [], "never put to the gate, never accepted")
                self.assertNotIn(K.QUESTION, [e.kind for e in events])
                errors = [e.data["message"] for e in events if e.kind == K.ERROR]
                if notice is None:
                    self.assertEqual(errors, [])
                else:
                    self.assertEqual(len(errors), 1, errors)
                    self.assertIn(notice, errors[0])
                    self.assertIn("(from Codex sub-agent ‹/root/sv0_asker›)", errors[0])

    def test_unknown_thread_request_is_still_fatal(self):
        def retarget(method=None, drop_turn=False):
            def edit(msgs):
                i = self.request_at(msgs)
                msgs[i]["params"]["threadId"] = self.UNKNOWN
                if method:
                    msgs[i]["method"] = method
                if drop_turn:
                    msgs[i]["params"].pop("turnId")
                return msgs
            return edit

        def announced_elsewhere(msgs):
            msgs[self.spawn_at(msgs, self.ASKER)]["params"]["threadId"] = self.UNKNOWN
            return msgs

        def announced_completed_only(msgs):
            msgs[self.spawn_at(msgs, self.ASKER)]["params"]["item"]["kind"] = "completed"
            return msgs

        cases = [("approval", retarget()), ("elicitation", retarget(codex.MCP_ELICITATION, True)),
                 ("clock", retarget("currentTime/read", True)),
                 ("announced on another thread", announced_elsewhere),
                 ("announced only by item/completed", announced_completed_only),
                 ("past CHILD_LIMIT", None)]
        for label, edit in cases:
            with self.subTest(case=label), ExitStack() as limit:
                if edit is None:    # no room for another child: it stays unknown
                    limit.enter_context(patch.object(codex, "CHILD_LIMIT", 0))
                permit, calls = self.counting()
                h, events = self.replay("approval_accept", permit, edit=edit)
                self.assertIn("error", self.answer_to(h))
                self.assertEqual(calls, [])
                self.assertTrue(events[-1].data.get("fatal"), events[-1])
                self.assertIn("identity mismatch", events[-1].data["message"])

    def test_session_request_is_unchanged(self):
        def as_parent(turn="$TURN"):
            def edit(msgs):
                i = self.request_at(msgs)
                msgs[i]["params"].update(threadId="$THREAD", turnId=turn)
                return msgs
            return edit
        asker = RecordingAsker(Decision.ALLOW)
        h, events = self.replay("approval_accept", self.real_permit(asker), edit=as_parent())
        self.assert_parent_went_on(h, events)
        self.assertEqual(self.answer_to(h)["result"], {"decision": "accept"})
        asked = next(e.data for e in events if e.kind == K.APPROVAL_REQUESTED)
        self.assertNotIn("subagent", asked)
        self.assertEqual(asker.seen[-1].subagent, "")
        self.assertNotIn("subagent", asker.seen[-1].to_json())
        self.assertTrue(asker.seen[-1].headline.startswith("SANDBOX WIDENING: Codex chat requests"),
                        asker.seen[-1].headline)
        # Another turn's request from the session's own thread is still fatal.
        permit, calls = self.counting()
        h, events = self.replay("approval_accept", permit, edit=as_parent("another-turn"))
        self.assertIn("error", self.answer_to(h))
        self.assertEqual(calls, [])
        self.assertTrue(events[-1].data.get("fatal"))

    def test_child_sid_reaches_only_a_callback_that_takes_it(self):
        """SV-3: the sid is handed only to a permit that takes `subagent_sid`.
        One that does not is called exactly as before (a raise would deny),
        so the sid never changes an answer."""
        seen = []

        def narrow(tool, args, brief, *, widening=None, grants=(), also_commands=()):
            seen.append(widening)
            return Decision.ALLOW
        h, events = self.replay("approval_accept", narrow)
        self.assert_parent_went_on(h, events)
        self.assertEqual(self.answer_to(h)["result"], {"decision": "accept"})
        self.assertEqual(len(seen), 1)
        self.assertIn(self.ASKER, seen[0])
        permit, calls = self.counting()
        h, events = self.replay("approval_accept", permit)
        self.assertEqual(self.answer_to(h)["result"], {"decision": "accept"})
        self.assertEqual(calls[-1][1]["subagent_sid"], self.sid_of(h, self.ASKER))

    def test_grandchild_request_is_known_through_its_parent_child(self):
        from jarvis.v2.permissions import deny_all
        inner = "/root/sv0_outer/sv0_inner"
        template = fixture_turn("approval")[self.request_at(fixture_turn("approval"))]

        def ask_from_grandchild(msgs):
            at = next(i for i, m in enumerate(msgs) if m.get("method") == "item/started"
                      and m["params"]["item"].get("type") == "commandExecution")
            item = msgs[at]["params"]
            request = json.loads(json.dumps(template))
            request["params"].update(threadId=item["threadId"], turnId=item["turnId"],
                                     itemId=item["item"]["id"], command=item["item"]["command"])
            msgs.insert(at + 1, request)
            return msgs

        def nameless_outer(msgs):
            msgs = ask_from_grandchild(msgs)
            del msgs[self.spawn_at(msgs, "/root/sv0_outer")]
            return msgs
        h, events = self.replay("nested", self.real_permit(deny_all("nobody")), edit=ask_from_grandchild)
        self.assertEqual(self.answer_to(h)["result"], {"decision": "decline"})
        self.assertEqual(events[-1].data, {"stop": "end"})
        self.assertEqual(next(e.data for e in events if e.kind == K.APPROVAL_REQUESTED)["subagent"],
                         self.sid_of(h, inner))
        # Announced on a thread nobody announced: unknown, and still fatal.
        h, events = self.replay("nested", self.real_permit(deny_all("nobody")), edit=nameless_outer)
        self.assertIn("error", self.answer_to(h))
        self.assertTrue(events[-1].data.get("fatal"))

    def test_child_label_is_restricted_and_never_cut_from_the_headline(self):
        """The label is the model's `agentPath`: only [A-Za-z0-9_./-] survive,
        so it cannot pose as headline parts, and however long it is the
        headline still says whose request it is."""
        cases = {
            # name -> (agentPath, label shown)
            "mimics headline parts": ("/root/a · network off · read-only git status",
                                      "/root/a_network_off_read-only_git_status"),
            "control and bidi": ("/root/x`\n‮APPROVED", "/root/x_APPROVED"),
            "eighty characters": ("/root/" + "y" * 74, "/root/" + "y" * 33 + "…"),
            "nothing usable": ("···", "unnamed"),
        }
        for label, (path, shown) in cases.items():
            with self.subTest(case=label):
                asker = RecordingAsker(Decision.DENY)

                def rename(msgs):
                    msgs[self.spawn_at(msgs, self.ASKER)]["params"]["item"]["agentPath"] = path
                    return msgs
                h, events = self.replay("approval", self.real_permit(asker), edit=rename)
                self.assert_parent_went_on(h, events)
                self.assertEqual(next(e.data for e in events if e.kind == K.APPROVAL_REQUESTED)["subagent"],
                                 self.sid_of(h, shown))
                self.assertEqual(asker.seen[-1].subagent, self.sid_of(h, shown))
                headline = asker.seen[-1].headline
                self.assertIn(f"Codex sub-agent ‹{shown}› (not the orchestrator) asks to run beyond its sandbox",
                              headline)
                self.assertNotIn("·", headline.split("›")[0])

    def test_child_approval_never_borrows_another_threads_item(self):
        """A child's approval is judged only on an item that child started:
        never a sibling's, never the session's."""
        sibling = "01a140cb-0000-7000-8000-000000sibling"

        def naming(owner):
            def edit(msgs):
                i = self.request_at(msgs)
                spawn = msgs[self.spawn_at(msgs, self.ASKER)]
                borrowed = {"type": "commandExecution", "id": "borrowed", "command": "echo harmless",
                            "cwd": "$CWD", "status": "inProgress"}
                if owner == "sibling":
                    announce = json.loads(json.dumps(spawn))
                    announce["params"]["item"].update(id="call_spawn_sibling", agentThreadId=sibling,
                                                      agentPath="/root/sibling")
                    msgs.insert(i, announce)
                    i += 1
                tid = sibling if owner == "sibling" else "$THREAD"
                msgs.insert(i, {"method": "item/started", "params": {
                    "item": borrowed, "threadId": tid, "turnId": "sibling-turn" if owner == "sibling" else "$TURN"}})
                msgs[i + 1]["params"]["itemId"] = "borrowed"
                msgs[i + 1]["params"]["command"] = "echo harmless"
                return msgs
            return edit
        for owner in ("sibling", "session"):
            with self.subTest(owner=owner):
                asker = RecordingAsker(Decision.ALLOW)
                h, events = self.replay("approval_accept", self.real_permit(asker), edit=naming(owner))
                self.assert_parent_went_on(h, events)
                self.assertEqual(self.answer_to(h)["result"], {"decision": "decline"})
                self.assertEqual(asker.seen, [])
                self.assertIn("its item was not seen", [e.data["message"] for e in events if e.kind == K.ERROR][0])

    def test_child_items_are_bounded_and_dropped_when_finished(self):
        def extra_items(n, *, complete_own=False):
            def edit(msgs):
                i = self.request_at(msgs)
                req = msgs[i]["params"]
                own = next(m for m in msgs if m.get("method") == "item/started"
                           and m["params"]["item"].get("id") == req["itemId"])
                added = []
                for k in range(n):
                    other = json.loads(json.dumps(own))
                    other["params"]["item"]["id"] = f"later-{k}"
                    added.append(other)
                if complete_own:
                    done = json.loads(json.dumps(own))
                    done["method"] = "item/completed"
                    added.append(done)
                msgs[i:i] = added
                return msgs
            return edit
        for label, edit, limit in (("pushed out past CHILD_ITEM_LIMIT", extra_items(2), 2),
                                   ("its item completed", extra_items(0, complete_own=True), 512)):
            with self.subTest(case=label), patch.object(codex, "CHILD_ITEM_LIMIT", limit):
                asker = RecordingAsker(Decision.ALLOW)
                h, events = self.replay("approval_accept", self.real_permit(asker), edit=edit)
                self.assert_parent_went_on(h, events)
                self.assertEqual(self.answer_to(h)["result"], {"decision": "decline"})
                self.assertEqual(asker.seen, [])
                self.assertLessEqual(len(h.native.child_items), limit)
        # Within the limit, the item stays and the owner is asked.
        with patch.object(codex, "CHILD_ITEM_LIMIT", 2):
            asker = RecordingAsker(Decision.ALLOW)
            h, events = self.replay("approval_accept", self.real_permit(asker), edit=extra_items(1))
            self.assertEqual(self.answer_to(h)["result"], {"decision": "accept"})
            # Only what an approval is judged on is kept.
            self.assertTrue(all(set(item) <= {"id", "type", "command", "cwd", "changes", codex._PATHS_SEEN}
                                for _, item in h.native.child_items.values()))

    def test_child_limit_forgets_the_oldest_finished_child_never_a_busy_one(self):
        old = "01a140cb-0000-7000-8000-0000000000old"

        def older_child(finished):
            def edit(msgs):
                at = self.spawn_at(msgs, self.ASKER)
                announce = json.loads(json.dumps(msgs[at]))
                announce["params"]["item"].update(id="call_spawn_old", agentThreadId=old, agentPath="/root/old")
                added = [announce, {"method": "turn/started", "params": {"threadId": old, "turn": {"id": "old-turn"}}}]
                if finished:
                    added.append({"method": "turn/completed", "params": {
                        "threadId": old, "turn": {"id": "old-turn", "status": "completed"}}})
                msgs[at:at] = added
                return msgs
            return edit
        with patch.object(codex, "CHILD_LIMIT", 1):
            from jarvis.v2.permissions import deny_all
            h, events = self.replay("approval", self.real_permit(deny_all("nobody")), edit=older_child(True))
            self.assert_parent_went_on(h, events)
            self.assertEqual(self.answer_to(h)["result"], {"decision": "decline"})
            self.assertEqual(list(h.native.children), ["01a140cb-981d-7112-8ad9-7f893a0cde4b"])
            # A child still running is never forgotten: the newcomer stays unknown.
            h, events = self.replay("approval", self.real_permit(deny_all("nobody")), edit=older_child(False))
            self.assertIn("error", self.answer_to(h))
            self.assertTrue(events[-1].data.get("fatal"))

    def test_child_request_in_a_later_turn_is_still_known(self):
        """Children outlive the turn that spawned them (SV-0): a child
        announced in turn 1 may ask in turn 2."""
        from jarvis.v2.permissions import deny_all
        first = fixture_turn("approval")
        i = self.request_at(first)
        request, item = first[i], first[i - 1]
        self.assertEqual(item["params"]["item"]["id"], request["params"]["itemId"])
        child = request["params"]["threadId"]
        # Turn 1: everything but the child's item and request.
        turn1 = [m for k, m in enumerate(first) if k not in (i - 1, i)]
        usage = next(m for m in reversed(first) if m.get("method") == "thread/tokenUsage/updated"
                     and m["params"]["threadId"] == "$THREAD")
        more = json.loads(json.dumps(usage))
        more["params"]["tokenUsage"]["total"].update(inputTokens=500, outputTokens=50, totalTokens=550)
        turn2 = [{**first[0]}, item, request, more,
                 {"method": "turn/completed", "params": {"threadId": "$THREAD",
                                                        "turn": {"id": "$TURN", "status": "completed"}}}]
        self.brain.script["replays"] = [turn1, turn2]
        h = self.start("replay", self.real_permit(deny_all("nobody")),
                       thread=replace(self.thread, id="later"), brief=self.chat())
        events1 = self.send(h)
        self.assertEqual(events1[-1].data, {"stop": "end"})
        self.assertIn(child, h.native.children)
        events2 = self.send(h)
        self.assertEqual(events2[-1].data, {"stop": "end"})
        self.assertEqual(self.answer_to(h)["result"], {"decision": "decline"})
        self.assertEqual(next(e.data for e in events2 if e.kind == K.APPROVAL_REQUESTED)["subagent"],
                         self.sid_of(h, "/root/sv0_asker"))

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

    # -- steering (2026-10-08) ------------------------------------------------

    def _running(self, h):
        deadline = time.monotonic() + 3
        while h.native.turn_id is None:
            if time.monotonic() >= deadline:
                raise AssertionError("the turn never started")
            time.sleep(0.01)

    def test_steer_appends_to_the_running_turn(self):
        """`turn/steer` with the verified shape lands in the turn that is
        running: one turn/start, the model answers the steer, one finish."""
        h = self.start("steer")
        with ThreadPoolExecutor(1) as pool:
            running = pool.submit(self.send, h)
            self._running(h)
            self.assertIs(self.provider.steer(h, UserMessage("use the other file", [{"b64": "YWJj", "mime": "image/png"}])), True)
            events = running.result(5)
        params = self.brain.calls("turn/steer")[0]["params"]
        self.assertEqual(params, {"threadId": "native-0", "expectedTurnId": "turn-1", "input": [
            {"type": "text", "text": "use the other file", "text_elements": []},
            {"type": "image", "url": "data:image/png;base64,YWJj"}]})
        self.assertEqual([e.data["text"] for e in events if e.kind == K.TEXT], ["steered: use the other file"])
        self.assertEqual(events[-1].data, {"stop": "end"})
        self.assertEqual(len(self.brain.calls("turn/start")), 1)
        self.assertFalse(any(e.kind == K.ERROR for e in events), events)

    def test_steer_is_refused_with_no_turn_or_mid_approval(self):
        """No turn running, or a turn waiting on an approval: refused (the
        daemon queues it), and nothing is sent — a steer must never read as
        the answer to what the turn is waiting on."""
        from jarvis.v2.provider import SteerRefused
        h = self.start("approval", permit=lambda *_: Decision.ALLOW)
        with self.assertRaises(SteerRefused) as idle:
            self.provider.steer(h, UserMessage("too early"))
        self.assertEqual(idle.exception.fallback, "queue")
        asked, release = threading.Event(), threading.Event()

        def permit(*_a, **_k):
            asked.set()
            self.assertTrue(release.wait(3))
            return Decision.ALLOW

        h2 = self.start("approval", permit=permit, thread=replace(self.thread, id="asking"))
        with ThreadPoolExecutor(1) as pool:
            running = pool.submit(self.send, h2)
            self.assertTrue(asked.wait(3))
            with self.assertRaises(SteerRefused) as waiting:
                self.provider.steer(h2, UserMessage("actually do X"))
            self.assertEqual(waiting.exception.fallback, "queue")
            release.set()
            events = running.result(5)
        self.assertEqual(self.brain.calls("turn/steer"), [])
        self.assertEqual(events[-1].kind, K.TURN_FINISHED)

    def test_a_codex_without_turn_steer_falls_back_to_interrupt(self):
        """A Codex that answers "method not found" (none verified so far;
        0.153.4 and 0.161.0 both have turn/steer) is steered by interrupting:
        refused with the `interrupt` fallback, and not asked again."""
        from jarvis.v2.provider import SteerRefused
        h = self.start("steer_unsupported")
        with ThreadPoolExecutor(1) as pool:
            running = pool.submit(self.send, h)
            self._running(h)
            with self.assertRaises(SteerRefused) as first:
                self.provider.steer(h, UserMessage("redirect"))
            self.assertEqual(first.exception.fallback, "interrupt")
            with self.assertRaises(SteerRefused) as second:
                self.provider.steer(h, UserMessage("again"))
            self.assertEqual(second.exception.fallback, "interrupt")
            self.assertEqual(len(self.brain.calls("turn/steer")), 1, "asked once, then remembered")
            self.provider.interrupt(h)
            events = running.result(5)
        self.assertEqual(events[-1].data, {"stop": "interrupted"})
        self.assertFalse(any(e.kind == K.ERROR for e in events), "a refused steer is not the turn's failure")

    def test_steer_refusals_queue_and_never_mark_codex_unable_to_steer(self):
        """Review of PR #22, finding 4: 0.161.0's own steer errors, and an
        unknown variant that is not the method, are refusals (queue) — none of
        them may switch the session to the interrupt fallback for good."""
        from jarvis.v2.provider import SteerRefused
        errors = [(-32600, "no active turn to steer"),
                  (-32600, "expected active turn id `turn-1` but found `turn-2`"),
                  (-32600, "cannot steer a review/compact turn"),
                  (-32600, "Invalid request: unknown variant `localVideo`, expected one of `text`, `image`")]
        self.brain.script["steer_errors"] = errors
        h = self.start("steer_errors")
        with ThreadPoolExecutor(1) as pool:
            running = pool.submit(self.send, h)
            self._running(h)
            try:
                for code, text in errors:
                    with self.subTest(text=text):
                        with self.assertRaises(SteerRefused) as refused:
                            self.provider.steer(h, UserMessage("redirect"))
                        self.assertEqual(refused.exception.fallback, "queue")
                        self.assertFalse(h.native.steer_unsupported)
                self.assertEqual(running.result(5)[-1].data, {"stop": "end"})
            except BaseException:
                # A failure must not leave the held turn (and the pool) hanging.
                self.provider.interrupt(h)
                raise
        self.assertEqual(len(self.brain.calls("turn/steer")), len(errors), "asked every time")

    def test_a_steer_codex_never_answers_is_unknown_not_requeued(self):
        """Review of PR #22, finding 3: no answer within STEER_TIMEOUT may mean
        Codex took it and answered late. Counted as delivered (logged), never
        refused — a refusal would have the daemon queue it and deliver it
        twice. Round 2: still unanswered when the turn ends, the log says it
        may not have reached the model."""
        h = self.start("steer_silent")
        with patch.object(codex, "STEER_TIMEOUT", 0.2), ThreadPoolExecutor(1) as pool:
            running = pool.submit(self.send, h)
            self._running(h)
            with self.assertLogs(codex.LOG, level="WARNING") as logged:
                try:
                    self.assertIs(self.provider.steer(h, UserMessage("maybe taken")), False)
                finally:
                    self.provider.interrupt(h)      # whatever happened, the held turn ends
                events = running.result(5)
            log = "\n".join(logged.output)
            self.assertIn("taken as delivered", log)
            self.assertIn("unanswered when the turn ended; it may not have reached the model", log)
            self.assertFalse(h.native.steer_unsupported)
            self.assertEqual(events[-1].data, {"stop": "interrupted"})
        self.assertEqual(len(self.brain.calls("turn/steer")), 1)

    def test_unconfirmed_steer_late_answers_do_not_change_the_returned_receipt(self):
        for refused in (False, True):
            with self.subTest(refused=refused):
                h = self.start("steer_silent", thread=replace(self.thread, id="late-"+str(refused)))
                with patch.object(codex, "STEER_TIMEOUT", 0.05), ThreadPoolExecutor(1) as pool:
                    running = pool.submit(self.send, h)
                    self._running(h)
                    try:
                        receipt = self.provider.steer(h, UserMessage("unconfirmed owner words"))
                        self.assertIs(receipt, False)
                        rid, waiter = next(iter(h.native.steers.items()))
                        self.assertTrue(waiter.abandoned)
                        answer = {"id": rid, "error": {"code": -32600, "message": "no active turn to steer"}} if refused else {"id": rid, "result": {"turnId": h.native.turn_id}}
                        self.assertTrue(self.provider._steer_answered(h.native, answer))
                        self.assertTrue(waiter.ready.is_set())
                        self.assertEqual(waiter.ok, not refused)
                        self.assertIs(receipt, False)  # no later callback can grant Browser authority
                        self.assertNotIn(rid, h.native.steers)
                        self.assertFalse(h.native.steer_unsupported)
                    finally:
                        self.provider.interrupt(h)
                    events = running.result(5)
                self.assertEqual(events[-1].data, {"stop": "interrupted"})
                self.assertFalse(any(e.kind == K.ERROR for e in events), events)

    def test_a_late_steer_refusal_never_fails_the_next_turn(self):
        """The turn completes before Codex answers the steer: the waiter is
        released (refused, so the daemon queues the message), and the late
        refusal is ignored by the next turn rather than failing it."""
        from jarvis.v2.provider import SteerRefused
        h = self.start("steer_late")
        with ThreadPoolExecutor(1) as pool:
            running = pool.submit(self.send, h)
            self._running(h)
            with self.assertRaises(SteerRefused):
                self.provider.steer(h, UserMessage("too late"))
            self.assertEqual(running.result(5)[-1].data, {"stop": "end"})
        events = self.send(h)
        self.assertEqual(events[-1].data, {"stop": "end"}, events)
        self.assertFalse(any(e.kind == K.ERROR for e in events), events)

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

    def saved_usage(self, h):
        return json.loads((h.native.home / "usage.json").read_text())

    def test_a_stopped_turn_closes_its_accounting(self):
        """The owner's Stop, then another message (2026-10-15). Codex 0.161.0
        ends a stopped turn with `turn/completed` `interrupted`, after the
        usage it counted (`stop_once`) or with none (`stop_once_quiet`). The
        next send runs on the same session, and the thread still resumes:
        it used to answer "previous turn accounting incomplete", and every
        resume after that was refused."""
        for mode, units in (("stop_once", 2), ("stop_once_quiet", 1)):
            thread = replace(self.thread, id=mode)
            h = self.start(mode, thread=thread)
            before = len(self.brain.calls("turn/start"))
            stream = self.provider.send(h, UserMessage("Go"))
            self.assertEqual(next(stream).kind, K.TURN_STARTED)
            self.provider.interrupt(h)
            stopped = list(stream)
            self.assertEqual(stopped[-1].data, {"stop": "interrupted"}, mode)
            # The ledger hears of it at once, with the totals already reported.
            usage = [e.data for e in stopped if e.kind == K.USAGE]
            self.assertEqual(usage[-1], dict(input=100 * (units - 1), output=20 * (units - 1),
                                             cached=40 * (units - 1), cost_usd=None,
                                             provider_reported={}, partial_turns=1), mode)
            self.assertTrue(all(u["partial_turns"] == 0 for u in usage[:-1]), (mode, usage))
            self.assertIs(stopped[-2], [e for e in stopped if e.kind == K.USAGE][-1], mode)
            saved = self.saved_usage(h)
            self.assertEqual((saved["complete"], saved["turns"], saved["partial_turns"]), (True, 1, 1), mode)
            events = self.send(h)
            self.assertTrue(all(e.data["partial_turns"] == 1 for e in events if e.kind == K.USAGE), mode)
            self.assertFalse([e for e in events if e.kind == K.ERROR], (mode, events))
            self.assertEqual(events[-1].data, {"stop": "end"}, mode)
            self.assertFalse(h.native.closed.is_set())
            self.assertEqual(len(self.brain.calls("turn/start")), before + 2, mode)
            # Only what Codex reported, nothing made up for the stopped turn.
            self.assertEqual(self.provider.usage(h).input_tokens, 100 * units, mode)
            saved = self.saved_usage(h)
            self.assertEqual((saved["complete"], saved["turns"], saved["partial_turns"]), (True, 2, 1), mode)
            self.provider.close(h)
            thread = replace(thread, provider_session_id=h.provider_session_id)
            self.brain.script["mode"] = "normal"
            resumed = self.provider.resume(thread, self.brief, lambda *_: Decision.ALLOW)
            self.handles.append(resumed)
            self.assertEqual(resumed.native.accounting.partial_turns, 1)
            self.assertEqual(self.send(resumed)[-1].data, {"stop": "end"}, mode)
            self.assertEqual(self.provider.usage(resumed).input_tokens, 100 * (units + 1), mode)

    def test_a_failed_turn_leaves_the_session_to_its_cooldown(self):
        """A capacity failure ends `failed`, and its cooldown is what the next
        send on that session meets: "model cooling", not a fatal error."""
        h = self.start("capacity")
        failed = self.send(h)
        self.assertEqual(failed[-1].data, {"stop": "error"})
        self.assertEqual([e.data["partial_turns"] for e in failed if e.kind == K.USAGE], [1])
        self.assertEqual(self.saved_usage(h)["partial_turns"], 1)
        events = self.send(h)
        self.assertEqual(events[0].data["message"], "Codex model cooling")
        self.assertFalse(events[0].data["fatal"])
        self.assertEqual(events[-1].data, {"stop": "error"})
        self.assertFalse(h.native.closed.is_set())
        self.assertEqual(len(self.brain.calls("turn/start")), 1)

    def test_an_unfinished_turn_cannot_be_certified_by_later_success(self):
        """What still fails closed: a turn Jarvis never saw end (the stream
        abandoned, the transport lost) or one ending in a status Codex does
        not define. Usage Codex sent may have been missed, so the session
        takes no further turn and the thread does not resume."""
        h = self.start("hold", thread=replace(self.thread, id="abandoned"))
        stream = self.provider.send(h, UserMessage("Go"))
        next(stream)
        stream.close()
        self.assertTrue(h.native.closed.is_set())
        self.assertFalse(self.saved_usage(h)["complete"])
        thread = replace(self.thread, id="abandoned", provider_session_id=h.provider_session_id)
        with self.assertRaisesRegex(RpcError, "resume accounting"):
            self.provider.resume(thread, self.brief, lambda *_: Decision.ALLOW)
        for n, status in enumerate(("inProgress", "paused"), 1):
            self.brain.script["odd_status"] = status
            h = self.start("odd_status", thread=replace(self.thread, id=f"odd-{status}"))
            self.assertEqual(self.send(h)[-1].data, {"stop": "error"}, status)
            self.assertFalse(self.saved_usage(h)["complete"], status)
            self.assertEqual(self.saved_usage(h)["partial_turns"], 0, status)
            events = self.send(h)
            self.assertTrue(events[-1].data["fatal"], status)
            self.assertIn("previous turn accounting incomplete", events[-1].data["message"])
            self.assertEqual(len(self.brain.calls("turn/start")), 1 + n, status)

    def test_a_saved_partial_turn_count_is_checked(self):
        from jarvis.v2.providers.codex_usage import Accounting
        path = self.root / "usage.json"
        zero = dict.fromkeys(("inputTokens", "outputTokens", "cachedInputTokens"), 0)
        base = dict(thread_id="t", total=zero, reported=zero, turns=2, complete=True)
        path.write_text(json.dumps(base))
        self.assertEqual(Accounting(path, "t", True).partial_turns, 0, "files from before it")
        for bad in (-1, 3, True, "1", None):
            path.write_text(json.dumps({**base, "partial_turns": bad}))
            with self.assertRaises(RpcError, msg=bad):
                Accounting(path, "t", True)


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
