"""Codex 0.153.4 app-server adapter, with native tools and broker callbacks.

Transport/config/accounting patterns originate in jarvis-trading-firm. No
model turn is retried automatically: a lost response may have executed tools.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
from typing import Iterator

from jarvis import config
from jarvis.v2.model import ProviderName, Thread
from jarvis.v2.provider import (Brief, BriefRefused, Decision, Event, EventKind,
                               PermissionCallback, SessionHandle, Usage, UserMessage)
from . import codex_config
from .codex_rpc import RpcError, RpcProcess, RpcTimeout
from .codex_usage import Accounting

CODEX_PIN = "0.153.4"
START_TIMEOUT = 30.0
INTERRUPT_TIMEOUT = 10.0
_auth_lock = threading.Lock()
_state_lock = threading.Lock()
_open_homes: set[Path] = set()


@dataclass
class _Pending:
    question: bool
    ready: threading.Event = field(default_factory=threading.Event)
    value: Decision | str | None = None


@dataclass
class _Session:
    rpc: RpcProcess
    brief: Brief
    permit: PermissionCallback
    home: Path
    thread_id: str
    accounting: Accounting
    turn_id: str | None = None
    pending: dict[str, _Pending] = field(default_factory=dict)
    items: dict[str, dict] = field(default_factory=dict)
    closed: threading.Event = field(default_factory=threading.Event)
    cancelled: threading.Event = field(default_factory=threading.Event)
    mutex: threading.RLock = field(default_factory=threading.RLock)
    sending: threading.Lock = field(default_factory=threading.Lock)
    interrupt_at: float | None = None
    # A model/effort change waiting for the next turn/start (set_model).
    override: dict | None = None


class CodexProvider:
    name = ProviderName.CODEX

    def health(self) -> tuple[bool, str]:
        binary = shutil.which("codex")
        if binary is None:
            return False, "codex is not on PATH"
        # No credential contents are read. login status is a local CLI probe.
        env = codex_config.clean_env(Path.home(), codex_config.owner_home())
        try:
            version = subprocess.run([binary, "--version"], env=env, capture_output=True,
                                     text=True, timeout=10)
            found = version.stdout.strip().removeprefix("codex-cli ")
            if version.returncode or found != CODEX_PIN:
                return False, f"codex {found}, pinned {CODEX_PIN}"
            login = subprocess.run([binary, "login", "status"], env=env, capture_output=True,
                                   text=True, timeout=10)
            if login.returncode or "logged in using chatgpt" not in (login.stdout + login.stderr).lower():
                return False, "codex requires a ChatGPT login; API-key billing refused"
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"codex health failed ({type(exc).__name__})"
        return True, f"codex {CODEX_PIN}, ChatGPT login"

    def start(self, thread: Thread, brief: Brief, permit: PermissionCallback) -> SessionHandle:
        return self._open(thread, brief, permit, resume=False)

    def resume(self, thread: Thread, brief: Brief, permit: PermissionCallback) -> SessionHandle:
        return self._open(thread, brief, permit, resume=True)

    def _open(self, thread, brief, permit, *, resume):
        codex_config.validate(brief)
        native_id = (thread.provider_session_id or "").removeprefix("codex:")
        if resume and (not (thread.provider_session_id or "").startswith("codex:") or not native_id):
            raise BriefRefused("Codex resume requires a codex:<thread id> session")
        ok, reason = self.health()
        if not ok:
            raise BriefRefused(reason)
        # Also serialize preparation, account inspection and refresh-prone startup.
        with _auth_lock:
            candidate = (Path(config.V2_DATA_DIR) / "codex" / thread.id).resolve()
            with _state_lock:
                if candidate in _open_homes:
                    raise BriefRefused("Codex thread already has an open session")
            argv, env, home = codex_config.prepare(thread.id, brief)
            home = home.resolve()
            with _state_lock:
                if home in _open_homes:
                    raise BriefRefused("Codex thread already has an open session")
                _open_homes.add(home)
            rpc = RpcProcess(argv, cwd=brief.cwd, env=env)
            try:
                rpc.start().initialize()
                account = rpc.request("account/read", {"refreshToken": False}).get("account") or {}
                if account.get("type") != "chatgpt":
                    raise BriefRefused("Codex requires ChatGPT authentication; API-key billing refused")
                effective = rpc.request("config/read", {"includeLayers": False})["config"]
                required = {"forced_login_method": "chatgpt", "model_provider": "openai",
                            "sandbox_mode": "workspace-write", "approval_policy": "on-request",
                            "approvals_reviewer": "auto_review"}
                if any(effective.get(k) != v for k, v in required.items()):
                    raise BriefRefused("Codex effective config changed authentication or permissions")
                params = {"cwd": brief.cwd, "modelProvider": "openai", "sandbox": "workspace-write",
                          "approvalPolicy": "on-request", "approvalsReviewer": "auto_review",
                          # baseInstructions replaces the native prompt. The additive
                          # developer field preserves it, AGENTS.md and skill loading.
                          "developerInstructions": brief.system_append}
                if brief.model:
                    params["model"] = brief.model
                if brief.effort:
                    params["config"] = {"model_reasoning_effort": brief.effort}
                if resume:
                    accounting = Accounting(home / "usage.json", native_id, True)
                    params.update(threadId=native_id, excludeTurns=True)
                else:
                    params["allowProviderModelFallback"] = False
                result = rpc.request("thread/resume" if resume else "thread/start", params)
                returned_id = result["thread"]["id"]
                if not isinstance(returned_id, str) or not returned_id or (resume and returned_id != native_id):
                    raise RpcError("Codex returned a mismatched thread identity")
                if ((brief.model and result.get("model") != brief.model)
                        or (brief.effort and result.get("reasoningEffort") != brief.effort)
                        or result.get("modelProvider", "openai") != "openai"
                        or result.get("approvalPolicy") != "on-request"
                        or result.get("approvalsReviewer") != "auto_review"
                        or result.get("sandbox", {}).get("type") != "workspaceWrite"):
                    raise BriefRefused("Codex did not honor the requested model, effort or sandbox")
                if not resume:
                    accounting = Accounting(home / "usage.json", returned_id, False)
                accounting.save()
                return SessionHandle(thread.id, self.name, "codex:" + returned_id,
                                     _Session(rpc, brief, permit, home, returned_id, accounting))
            except BaseException:
                rpc.close()
                with _state_lock:
                    _open_homes.discard(home)
                raise

    def _event(self, h, kind, **data):
        return Event(kind, h.thread_id, data)

    def _cooldown(self, model, *, failed=False, success=False):
        """Shared durable model backoff; the ledger can consume retry_at from ERROR."""
        path = Path(config.V2_DATA_DIR) / "codex" / "cooldown.json"
        try:
            values = json.loads(path.read_text())
        except FileNotFoundError:
            values = {}
        except (OSError, ValueError) as exc:
            raise RpcError("Codex cooldown state unreadable") from exc
        key = model or "default"
        old = values.get(key, {})
        if success:
            values.pop(key, None)
        elif failed:
            attempts = old.get("attempts", 0) + 1
            values[key] = {"attempts": attempts, "retry_at": time.time() + min(900 * 2 ** min(attempts - 1, 3), 7200)}
        if failed or success:
            codex_config.atomically_write(path, json.dumps(values))
        value = values.get(key, {})
        return value if value.get("retry_at", 0) > time.time() else None

    def send(self, h: SessionHandle, message: UserMessage) -> Iterator[Event]:
        s = h.native
        if not s.sending.acquire(blocking=False):
            raise ValueError("Codex session already has an active send")
        acquired, submitted, completed = False, False, False
        try:
            while not acquired:
                if s.closed.is_set():
                    raise RpcError("Codex session is closed")
                if s.cancelled.is_set():
                    yield self._event(h, EventKind.TURN_FINISHED, stop="interrupted")
                    return
                acquired = _auth_lock.acquire(timeout=0.05)
            if s.closed.is_set():
                raise RpcError("Codex session is closed")
            if not s.accounting.complete:
                raise RpcError("Codex previous turn accounting incomplete; start a fresh thread")
            if s.brief.max_turns is not None and s.accounting.turns >= s.brief.max_turns:
                yield self._event(h, EventKind.TURN_FINISHED, stop="max_turns")
                return
            cooldown = self._cooldown(s.brief.model)
            if cooldown:
                yield self._event(h, EventKind.ERROR, message="Codex model cooling", fatal=False, **cooldown)
                yield self._event(h, EventKind.TURN_FINISHED, stop="error")
                return
            inputs = [{"type": "text", "text": message.text, "text_elements": []}]
            for img in message.images:
                inputs.append({"type": "image", "url": f"data:{img['mime']};base64,{img['b64']}"})
            s.accounting.complete = False
            s.accounting.turns += 1
            s.accounting.save()
            s.items.clear()
            submitted = True
            start_deadline = time.monotonic() + START_TIMEOUT
            params = {"threadId": s.thread_id, "input": inputs}
            # turn/start's `model` and `effort` override "this turn and
            # subsequent turns" (TurnStartParams, generated from 0.153.4), so
            # a change rides the next turn and then sticks to the thread.
            override, s.override = s.override, None
            params.update(override or {})
            request = s.rpc.send("turn/start", params)
            started, response_seen, capacity, saw_usage = False, False, False, False
            early = deque()
            while True:
                if not response_seen and time.monotonic() >= start_deadline:
                    raise RpcError("Codex turn/start response deadline expired")
                if s.interrupt_at is not None and time.monotonic() - s.interrupt_at >= INTERRUPT_TIMEOUT:
                    raise RpcError("Codex turn interrupt deadline expired")
                if s.closed.is_set():
                    raise RpcError("Codex session closed during turn")
                if response_seen and early:
                    msg = early.popleft()
                else:
                    try:
                        msg = s.rpc.next_message(timeout=0.1)
                    except RpcTimeout:
                        continue
                if "method" not in msg:
                    if msg.get("id") == request:
                        if "error" in msg:
                            raise RpcError("Codex turn/start failed; submission outcome unknown")
                        turn_id = msg["result"]["turn"]["id"]
                        if not isinstance(turn_id, str) or not turn_id:
                            raise RpcError("Codex returned an invalid turn identity")
                        if s.turn_id is not None and s.turn_id != turn_id:
                            raise RpcError("Codex turn identity changed")
                        s.turn_id, response_seen = turn_id, True
                        if s.cancelled.is_set():
                            self.interrupt(h)
                        if not started:
                            started = True
                            yield self._event(h, EventKind.TURN_STARTED)
                    elif "error" in msg:
                        raise RpcError("Codex control request failed")
                    continue
                method, params = msg["method"], msg.get("params", {})
                if "id" in msg:
                    if (params.get("threadId") != s.thread_id
                            or not isinstance(params.get("turnId"), str) or not params["turnId"]
                            or (s.turn_id and params.get("turnId") != s.turn_id)):
                        s.rpc.reply(msg["id"], error={"code": -32602, "message": "Request identity mismatch"})
                        raise RpcError("Codex server request identity mismatch")
                    if s.turn_id is None:
                        s.turn_id = params.get("turnId")
                    if not started:
                        started = True
                        yield self._event(h, EventKind.TURN_STARTED)
                    yield from self._server_request(h, msg)
                    # Human latency is not a turn/start transport timeout.
                    if not response_seen:
                        start_deadline = time.monotonic() + START_TIMEOUT
                    continue
                if not response_seen:
                    if len(early) >= 4096:
                        raise RpcError("Too many Codex events before turn/start reply")
                    if method in ("item/started", "item/fileChange/patchUpdated") and params.get("threadId") == s.thread_id:
                        item = params.get("item") or {"id": params["itemId"], "type": "fileChange", "changes": params.get("changes", [])}
                        s.items[item["id"]] = item
                    early.append(msg)
                    continue
                if params.get("threadId") not in (None, s.thread_id):
                    continue
                tid = params.get("turnId", (params.get("turn") or {}).get("id"))
                if tid is not None and tid != s.turn_id:
                    continue  # resume may replay previous-turn counters and items
                if method == "error":
                    err = params.get("error", {})
                    info = err.get("codexErrorInfo")
                    if info in ("serverOverloaded", "rateLimitExceeded", "usageLimitExceeded"):
                        capacity = True
                    yield self._event(h, EventKind.ERROR, message=err.get("message", "Codex turn error"),
                                      fatal=False, provider_reported=err)
                elif method == "thread/tokenUsage/updated" and tid == s.turn_id:
                    s.accounting.update(params["tokenUsage"])
                    saw_usage |= any(params["tokenUsage"]["total"].values())
                    u = s.accounting.usage()
                    yield self._event(h, EventKind.USAGE, input=u.input_tokens, output=u.output_tokens,
                                      cached=u.cached_tokens, cost_usd=None, provider_reported=params["tokenUsage"])
                elif method == "turn/completed":
                    status = params["turn"]["status"]
                    err = params["turn"].get("error") or {}
                    capacity |= err.get("codexErrorInfo") in ("serverOverloaded", "rateLimitExceeded", "usageLimitExceeded")
                    if status == "completed" and not saw_usage:
                        raise RpcError("Codex completed without token usage; accounting incomplete")
                    if capacity:
                        retry = self._cooldown(s.brief.model, failed=True)
                        yield self._event(h, EventKind.ERROR, message="Codex model cooling", fatal=False, **retry)
                    elif status == "completed":
                        self._cooldown(s.brief.model, success=True)
                    s.accounting.complete = status == "completed"
                    s.accounting.save()
                    completed = True
                    yield self._event(h, EventKind.TURN_FINISHED,
                                      stop={"completed": "end", "interrupted": "interrupted"}.get(status, "error"))
                    return
                else:
                    yield from self._notification(h, method, params)
        except (RpcError, OSError, KeyError, TypeError, ValueError, AttributeError) as exc:
            self.close(h)
            yield self._event(h, EventKind.ERROR, message=str(exc), fatal=True)
        finally:
            # A consumer abandoning the iterator must not leave a model mutating
            # files while another session acquires the shared-login lock.
            if submitted and not completed:
                self.close(h)
            s.turn_id = None
            s.interrupt_at = None
            s.cancelled.clear()
            with s.mutex:
                s.pending.clear()
            if acquired:
                _auth_lock.release()
            s.sending.release()

    def _tool(self, item):
        if item.get("type") == "commandExecution":
            return "shell", {"command": item.get("command"), "cwd": item.get("cwd")}
        return "apply_patch", {"paths": [c["path"] for c in item.get("changes", [])],
                               "changes": item.get("changes", [])}

    def _notification(self, h, method, p):
        s = h.native
        if method == "model/rerouted" and s.brief.model:
            raise RpcError("Codex substituted the requested model")
        if method == "account/updated" and p.get("authMode") not in (None, "chatgpt"):
            raise RpcError("Codex changed away from ChatGPT authentication")
        if method == "account/rateLimits/updated":
            # Verified from 0.153.4 generate-ts: AccountRateLimitsUpdatedNotification
            # carries a sparse RateLimitSnapshot, independent of any turn id.
            u = s.accounting.usage()
            yield self._event(h, EventKind.USAGE, input=u.input_tokens, output=u.output_tokens,
                              cached=u.cached_tokens, cost_usd=None,
                              provider_reported={"rate_limits": p["rateLimits"]})
        elif method == "item/agentMessage/delta":
            yield self._event(h, EventKind.TEXT_DELTA, text=p["delta"])
        elif method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
            yield self._event(h, EventKind.THINKING, text=p["delta"])
        elif method == "turn/plan/updated":
            yield self._event(h, EventKind.PLAN_UPDATED, plan=p["plan"], explanation=p.get("explanation"))
        elif method in ("item/started", "item/completed", "item/fileChange/patchUpdated"):
            item = p.get("item") or {"id": p.get("itemId"), "type": "fileChange", "changes": p.get("changes", [])}
            s.items[item["id"]] = item
            kind = item["type"]
            done = method == "item/completed"
            if kind in ("commandExecution", "fileChange"):
                name, args = self._tool(item)
                if method == "item/started":
                    yield self._event(h, EventKind.TOOL_STARTED, call_id=item["id"], name=name, args=args)
                elif done:
                    if name == "shell":
                        summary = f"exit code {item.get('exitCode')}"
                        ok = item.get("status") == "completed" and item.get("exitCode") == 0
                    else:
                        changes = item.get("changes", [])
                        lines = [line for c in changes for line in c.get("diff", "").splitlines()]
                        added = sum(x.startswith("+") and not x.startswith("+++") for x in lines)
                        removed = sum(x.startswith("-") and not x.startswith("---") for x in lines)
                        summary = f"{len(changes)} files, +{added}/-{removed} lines"
                        ok = item.get("status") == "completed"
                    yield self._event(h, EventKind.TOOL_FINISHED, call_id=item["id"], name=name, ok=ok, summary=summary)
            elif done and kind == "agentMessage":
                yield self._event(h, EventKind.TEXT, text=item["text"])
            elif done and kind == "reasoning":
                yield self._event(h, EventKind.THINKING, text="\n".join(item.get("summary", []) + item.get("content", [])))
            elif done and kind == "plan":
                yield self._event(h, EventKind.PLAN_UPDATED, text=item["text"])
        elif method == "item/autoApprovalReview/completed" and p.get("review", {}).get("status") == "denied":
            item = s.items.get(p.get("targetItemId"), {})
            action = p.get("action", {})
            if action.get("type") in ("command", "execve", "writeStdin"):
                name, args = "shell", dict(action)
            elif action.get("type") == "applyPatch":
                name, args = "apply_patch", {"paths": action.get("files", []), **action}
            elif action:
                name, args = action.get("type", "unknown"), dict(action)
            else:
                name, args = self._tool(item)
            yield self._event(h, EventKind.REVIEWER_DECLINED, tool=name, args=args,
                              command=args.get("command"), reason=p["review"].get("rationale") or "Reviewer declined")

    def _server_request(self, h, msg):
        s = h.native
        method, p, rid = msg["method"], msg.get("params", {}), msg["id"]
        req_id = str(rid)
        if method == "item/permissions/requestApproval":
            s.rpc.reply(rid, {"permissions": {}, "scope": "turn"})
            return
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            shell = method == "item/commandExecution/requestApproval"
            item = s.items.get(p.get("itemId"), {})
            name, args = self._tool(item if item else {"type": "commandExecution" if shell else "fileChange"})
            args.update(p)
            args["req_id"] = req_id
            pending = _Pending(False)
            with s.mutex:
                if req_id in s.pending:
                    raise RpcError("Duplicate Codex request id")
                s.pending[req_id] = pending
            yield self._event(h, EventKind.APPROVAL_REQUESTED, req_id=req_id, tool=name, args=args, command=args.get("command"))
            try:
                # Deliberately on the send caller's thread: broker context survives.
                decision = s.permit(name, args, s.brief)
                decision = Decision(decision)
            except Exception:
                decision = Decision.DENY
            with s.mutex:
                if pending.ready.is_set():
                    decision = pending.value
                if s.closed.is_set() or s.cancelled.is_set() or s.rpc.failed.is_set():
                    decision = Decision.DENY
                s.pending.pop(req_id, None)
            s.rpc.reply(rid, {"decision": "accept" if decision == Decision.ALLOW else "decline"})
            yield self._event(h, EventKind.APPROVAL_RESOLVED, req_id=req_id, decision=decision.value)
            if s.rpc.failed.is_set():
                raise RpcError("Codex transport failed during approval")
            return
        if method in ("item/tool/requestUserInput", "tool/requestUserInput"):
            answers = {}
            questions = p.get("questions", [])
            for index, q in enumerate(questions):
                # Each question needs its own answer; never repeat one answer over a bundle.
                qid = req_id if len(questions) == 1 else f"{req_id}:{index}"
                pending = _Pending(True)
                with s.mutex:
                    s.pending[qid] = pending
                yield self._event(h, EventKind.QUESTION, req_id=qid, text=q["question"],
                                  options=[o["label"] for o in q.get("options") or []])
                while not pending.ready.wait(0.05):
                    if s.closed.is_set() or s.rpc.failed.is_set():
                        raise RpcError("Codex transport closed while awaiting an answer")
                    if s.cancelled.is_set():
                        break
                with s.mutex:
                    s.pending.pop(qid, None)
                if s.cancelled.is_set():
                    break
                answers[q["id"]] = {"answers": [pending.value]}
            s.rpc.reply(rid, {"answers": answers})
            return
        s.rpc.reply(rid, error={"code": -32601, "message": "Unsupported server request"})
        yield self._event(h, EventKind.ERROR, message=f"Unexpected Codex server request: {method}", fatal=False)

    def set_model(self, h: SessionHandle, model: str | None, effort: str | None) -> None:
        """Change the model and effort from the next turn on (decisions A1).

        Nothing is sent now: the pair rides the next `turn/start`, whose
        `model`/`effort` fields override the thread from that turn on. The
        session's brief is updated in memory so the model cooldown and the
        `model/rerouted` refusal judge the model actually asked for; the
        saved brief on disk is never rewritten.
        """
        s = h.native
        if s is None or s.closed.is_set():
            raise ValueError("Codex session is closed")
        if not model:
            raise ValueError("Codex needs a named model to switch to")
        with s.mutex:
            s.override = {"model": model, **({"effort": effort} if effort else {})}
            s.brief = replace(s.brief, model=model, effort=effort)

    def interrupt(self, h: SessionHandle) -> None:
        s = h.native
        s.cancelled.set()
        if s.interrupt_at is None:
            s.interrupt_at = time.monotonic()
        if s.turn_id is not None and not s.closed.is_set():
            # Only send reads responses. Concurrent writers serialize whole JSON lines.
            s.rpc.send("turn/interrupt", {"threadId": s.thread_id, "turnId": s.turn_id})

    def answer(self, h: SessionHandle, req_id: str, decision: Decision | str) -> None:
        s = h.native
        with s.mutex:
            pending = s.pending.get(req_id)
            if pending is None or pending.ready.is_set() or s.closed.is_set():
                raise ValueError(f"Unknown or resolved Codex request id: {req_id}")
            if pending.question:
                if type(decision) is not str:
                    raise ValueError("A Codex question requires text")
            else:
                decision = Decision(decision)
            pending.value = decision
            pending.ready.set()

    def usage(self, h: SessionHandle) -> Usage:
        return h.native.accounting.usage()

    def close(self, h: SessionHandle) -> None:
        s = h.native
        with s.mutex:
            if s.closed.is_set():
                return
            s.closed.set()
            s.pending.clear()
        s.rpc.close()
        with _state_lock:
            _open_homes.discard(s.home)
