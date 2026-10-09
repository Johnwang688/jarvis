"""Codex app-server adapter, with native tools and broker callbacks.

The protocol was generated from and verified against codex-cli 0.153.4 and
0.161.0 (docs/codex-briefs/codex-0.161-protocol-notes.md). Any version at or
above codex_cli.CODEX_MIN runs; one newer than CODEX_VERIFIED runs with a
warning. What keeps a newer version from loosening the gate is narrower than
"anything not understood is refused", and worth stating exactly:

- an unrecognised server *method* gets a JSON-RPC error and never reaches the
  permit callback; a request whose params are not an object, or whose
  handling raises, is answered with an error too;
- a command/file approval whose fields have the wrong types, or that names
  an approval `kind` we cannot describe, is declined without asking anyone;
- a command approval is judged on the item's real command (a null field
  never erases it) and declined if there is none; a plain approval naming a
  different command from its item is declined, while a `writeStdin` or
  `approvalId` one (where a difference is expected) is a widening with both
  commands judged by layer 1 and shown;
- every filesystem grant a widening opens is judged by layer 1 first: one
  that is or contains protected state or touches a credential directory is
  DENY, unasked; and the headline, built from Codex-supplied strings, is one
  cleaned and capped line;
- an approval that would **widen the sandbox** (extra permissions, a
  managed-network prompt, a grant root, terminal input) is put to a human
  every time, with no Always (`permit(..., widening=...)`), and declined with
  no human. The reply is a bare `{decision}`, so the grant cannot be split
  off; and a request reaches us only after Codex's reviewer passed it, so
  without this an AUTO brief's "the reviewer decides" meant nobody did;
- a plain in-sandbox approval under AUTO is still accepted on the strength
  of Codex's reviewer (design R8) — that is the AUTO contract, not a gap
  this adapter closes;
- a non-null approval field outside the verified schema
  (`_KNOWN_APPROVAL_FIELDS`) counts as a widening — it may be a new grant —
  so a human is asked, shown it, and offered no Always.

What is *not* covered: a field we know whose *meaning* a newer Codex
broadens, and runtime behaviour the schema does not describe. Re-diff the
protocol (the notes say how) before trusting a version far past
CODEX_VERIFIED.

Transport/config/accounting patterns originate in jarvis-trading-firm. No
model turn is retried automatically: a lost response may have executed tools.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Iterator

from jarvis import config
from jarvis.v2.model import ProviderName, Thread
from jarvis.v2.provider import (Brief, BriefRefused, Decision, Event, EventKind,
                               PermissionCallback, SessionHandle, Usage, UserMessage)
from ..approvals import clean_line
from . import codex_cli, codex_config
from .codex_rpc import RpcError, RpcProcess, RpcTimeout
from .codex_usage import Accounting

START_TIMEOUT = 30.0
INTERRUPT_TIMEOUT = 10.0
METADATA_LOCK_TIMEOUT = 0.05
MODEL_PAGE_LIMIT = 100
MODEL_PAGE_CAP = 20
_auth_lock = threading.Lock()
_state_lock = threading.Lock()
_open_homes: set[Path] = set()


class MetadataBusy(RpcError):
    """`account_metadata` declined: a turn holds the shared login lock. The
    HUD backs off briefly (`hud_api.CODEX_METADATA_BUSY_S`) instead of the
    full TTL, and never by matching this message."""


@dataclass
class _Pending:
    question: bool
    ready: threading.Event = field(default_factory=threading.Event)
    value: Decision | str | None = None


@dataclass
class _Request:
    """One server request: answered at most once, refused if never answered."""
    rid: object = None
    replied: bool = False


def _approval_problem(p: dict) -> str | None:
    """Why a command/file approval request cannot be read, or None.

    Checked against the 0.153.4 and 0.161.0 schemas (identical for both):
    itemId is a required string; the fields shown to the owner are strings
    or null; a permission grant riding the approval is an object or null.
    """
    if not isinstance(p.get("itemId"), str) or not p["itemId"]:
        return "missing itemId"
    for key in ("command", "cwd", "reason", "grantRoot", "approvalId"):
        if p.get(key) is not None and not isinstance(p[key], str):
            return f"{key} is not a string"
    if p.get("additionalPermissions") is not None and not isinstance(p["additionalPermissions"], dict):
        return "additionalPermissions is not an object"
    if p.get("networkApprovalContext") is not None and not isinstance(p["networkApprovalContext"], dict):
        return "networkApprovalContext is not an object"
    if p.get("kind") is not None and not isinstance(p["kind"], str):
        return "kind is not a string"
    if p.get("availableDecisions") is not None and not isinstance(p["availableDecisions"], list):
        return "availableDecisions is not a list"
    return None


# Approval kinds that are a plain command run inside the sandbox. Anything
# else widens what the sandbox lets through; a kind not listed at all is one
# this adapter cannot describe to the owner, so it is declined outright.
_PLAIN_KINDS = (None, "command")
_WIDENING_KINDS = {"writeStdin": "input to a running terminal (writeStdin)"}
# Every approval field in the 0.153.4 and 0.161.0 schemas (identical in both).
# A non-null field outside this set is one a newer Codex added and we cannot
# judge — it might be another grant riding the accept — so it counts as a
# widening: a human is asked, shown the field, and offered no Always.
_KNOWN_APPROVAL_FIELDS = frozenset({
    "threadId", "turnId", "itemId", "startedAtMs", "approvalId", "reason",
    "command", "cwd", "commandActions", "environmentId", "kind", "availableDecisions",
    "additionalPermissions", "networkApprovalContext",
    "proposedExecpolicyAmendment", "proposedNetworkPolicyAmendments",   # proposals only:
    # they apply only on an acceptWith…Amendment answer, which we never send.
    "grantRoot",
})


def _content(value) -> bool:
    """Whether a permission overlay asks for anything. None, False, and empty
    containers ask for nothing; `network: {enabled: false}` narrows."""
    if value is None or value is False:
        return False
    if isinstance(value, dict):
        return any(_content(v) for v in value.values())
    if isinstance(value, (list, tuple, str)):
        return len(value) > 0
    return True


def _compact(value) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return text if len(text) <= 300 else text[:299] + "…"


# The headline is built from Codex-supplied strings (paths, hosts, commands)
# and lands first on an authorization surface, so every part is one cleaned
# line (approvals.clean_line: no newlines or control characters, no
# backticks) and the whole is capped. The full detail stays in `args`.
PART_CAP, HEADLINE_CAP = 120, 400
_GLOB = re.compile(r"[*?\[{]")


def _headline(parts: list[str]) -> str | None:
    if not parts:
        return None
    parts = [clean_line(part, PART_CAP) for part in parts]
    line, shown = "SANDBOX WIDENING: ", 0
    for part in parts:
        joined = line + (" · " if shown else "") + part
        if shown and len(joined) > HEADLINE_CAP - 16:
            break
        line, shown = joined, shown + 1
    if shown < len(parts):
        line += f" … (+{len(parts) - shown} more)"
    return line


def _place(path: str, cwd: str) -> str:
    return path if os.path.isabs(os.path.expanduser(path)) else os.path.join(cwd, path)


def _fs_path(path, cwd: str) -> tuple[str, str | None]:
    """One FileSystemPath (0.161.0 schema): its words, and the directory or
    file it covers (None for the sandbox's own minimal set). A shape we cannot
    place covers "/", so layer 1 judges it as the widest grant it could be."""
    if isinstance(path, dict):
        if path.get("type") == "path" and isinstance(path.get("path"), str):
            return path["path"], _place(path["path"], cwd)
        if path.get("type") == "glob_pattern" and isinstance(path.get("pattern"), str):
            literal = _GLOB.split(path["pattern"], 1)[0]
            base = literal if literal.endswith("/") or not literal else os.path.dirname(literal) or ""
            return path["pattern"], _place(base or ".", cwd)
        if path.get("type") == "special" and isinstance(path.get("value"), dict):
            value = path["value"]
            kind = value.get("kind")
            sub = value.get("subpath") if isinstance(value.get("subpath"), str) else None
            words = f"<{kind}>" + (f"/{sub}" if sub else "")
            if kind == "minimal":
                return words, None
            if kind == "root":
                return words, "/" + (sub or "")
            if kind == "project_roots":
                return words, os.path.join(cwd, sub) if sub else cwd
            if kind in ("tmpdir", "slash_tmp"):
                return words, "/tmp"
            if kind == "unknown" and isinstance(value.get("path"), str):
                return f"{value['path']}" + (f"/{sub}" if sub else ""), _place(value["path"], cwd)
    return _compact(path), "/"


def _describe_permissions(extra: dict, cwd: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Words for each grant in an AdditionalPermissionProfile, and each
    filesystem grant as (access, path) for layer 1's ancestor check."""
    parts, grants = [], []
    for key, value in extra.items():
        if not _content(value):
            continue
        if key == "network" and isinstance(value, dict) and set(value) <= {"enabled"}:
            parts.append("network on" if value.get("enabled") is True else f"network {_compact(value)}")
        elif key == "fileSystem" and isinstance(value, dict):
            for sub, paths in value.items():
                if not _content(paths) or sub == "globScanMaxDepth":
                    continue
                if sub in ("read", "write") and isinstance(paths, list) and all(isinstance(x, str) for x in paths):
                    parts.append(f"{sub} {', '.join(paths)}")
                    grants.extend((sub, _place(x, cwd)) for x in paths)
                elif sub == "entries" and isinstance(paths, list):
                    for entry in paths:
                        if isinstance(entry, dict) and isinstance(entry.get("access"), str):
                            words, where = _fs_path(entry.get("path"), cwd)
                            parts.append(f"{entry['access']} {words}")
                            if where is not None and entry["access"] != "deny":
                                access = "read" if entry["access"] == "read" else "write"
                                grants.append((access, where))
                        else:
                            parts.append(f"filesystem {_compact(entry)}")
                            grants.append(("write", "/"))
                else:
                    parts.append(f"filesystem {sub} {_compact(paths)}")
                    grants.append(("write", "/"))
        else:
            parts.append(f"{key} {_compact(value)}")
    if not parts:
        parts.append(f"additional permissions {_compact(extra)}")
    return parts, grants


def _widening(p: dict, cwd: str) -> tuple[list[str], list[tuple[str, str]], str | None]:
    """``(parts, grants, refusal)`` for one approval request.

    ``parts`` describe what accepting would let through beyond the sandbox
    (extra permissions riding on the approval, a managed-network prompt, a
    grant root, terminal input, a field we do not know); the reply is a bare
    ``{decision}``, so such a grant is accepted whole or not at all.
    ``grants`` are the filesystem paths it would open, for layer 1.
    ``refusal`` is set for a kind this adapter cannot describe, which is
    declined without asking anyone.
    """
    parts, grants = [], []
    kind = p.get("kind")
    if kind not in _PLAIN_KINDS:
        if kind not in _WIDENING_KINDS:
            return [], [], f"unknown approval kind {clean_line(kind, 40)!r}"
        parts.append(_WIDENING_KINDS[kind])
    if _content(p.get("additionalPermissions")):
        words, more = _describe_permissions(p["additionalPermissions"], cwd)
        parts.extend(words)
        grants.extend(more)
    net = p.get("networkApprovalContext")
    if net is not None:
        host, protocol = net.get("host"), net.get("protocol")
        parts.append(f"network access to {protocol or '?'}://{host or '?'}"
                     if isinstance(host, str) and (protocol is None or isinstance(protocol, str))
                     else f"network access {_compact(net)}")
    if p.get("grantRoot") is not None:
        parts.append(f"grant root {p['grantRoot']}")
        grants.append(("write", _place(p["grantRoot"], cwd)))
    for key in sorted(k for k, v in p.items() if v is not None and k not in _KNOWN_APPROVAL_FIELDS):
        parts.append(f"unrecognised field {str(key)[:60]}={_compact(p[key])}")
    return parts, grants, None


def _same_command(a: str, b: str) -> bool:
    return " ".join(a.split()) == " ".join(b.split())


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
        binary, reason = self._probe()
        return binary is not None, reason

    def _probe(self) -> tuple[str | None, str]:
        """``(binary, reason)``: the realpath to launch, or None and why not.

        The binary is resolved once (codex_cli.resolve) and its realpath is
        what is version-checked *and* launched, so the auto-updater flipping
        ~/.codex/packages/standalone/current between the two cannot swap the
        binary that was checked for one that was not.
        """
        found, reason = codex_cli.resolve()
        if found is None:
            return None, reason
        binary = os.path.realpath(found)
        # No credential contents are read. login status is a local CLI probe.
        env = codex_config.clean_env(Path.home(), codex_config.owner_home())
        try:
            version = subprocess.run([binary, "--version"], env=env, capture_output=True,
                                     text=True, timeout=10)
            if version.returncode:
                return None, f"codex at {found} failed to report its version"
            ok, reason = codex_cli.check(version.stdout, found)
            if not ok:
                return None, reason
            login = subprocess.run([binary, "login", "status"], env=env, capture_output=True,
                                   text=True, timeout=10)
            if login.returncode or "logged in using chatgpt" not in (login.stdout + login.stderr).lower():
                return None, "codex requires a ChatGPT login; API-key billing refused"
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, f"codex health failed ({type(exc).__name__})"
        return binary, f"{reason}, ChatGPT login"

    def account_metadata(self) -> dict:
        """Read the logged-in account's model catalog and quota snapshot.

        This uses a short-lived app-server rather than starting a model turn.
        Preparation and account inspection share the same lock as session
        startup, but a HUD read never waits behind a turn that is opening.
        """
        # A turn holds the lock for its whole length (`send`), so test it
        # before the probe: probing first ran `codex --version` and `codex
        # login status` on every HUD read during a turn, to give up anyway.
        if _auth_lock.locked():
            raise MetadataBusy("a Codex turn is running")
        binary, reason = self._probe()
        if binary is None:
            raise BriefRefused(reason)
        if not _auth_lock.acquire(timeout=METADATA_LOCK_TIMEOUT):
            raise MetadataBusy("a Codex turn is starting")
        rpc = None
        try:
            argv, env, home = codex_config.prepare_metadata(binary)
            rpc = RpcProcess(argv, cwd=str(home), env=env)
            rpc.start().initialize()
            account_result = rpc.request("account/read", {"refreshToken": False})
            if not isinstance(account_result, dict):
                raise RpcError("Codex account response is invalid")
            account = account_result.get("account") or {}
            if not isinstance(account, dict):
                raise RpcError("Codex account response is invalid")
            if account.get("type") != "chatgpt":
                raise BriefRefused("Codex requires ChatGPT authentication; API-key billing refused")

            rows, cursor, seen = [], None, set()
            for _ in range(MODEL_PAGE_CAP):
                params = {"includeHidden": False, "limit": MODEL_PAGE_LIMIT}
                if cursor is not None:
                    params["cursor"] = cursor
                page = rpc.request("model/list", params)
                if not isinstance(page, dict) or not isinstance(page.get("data"), list):
                    raise RpcError("Codex model catalog response is invalid")
                if any(not isinstance(row, dict) for row in page["data"]):
                    raise RpcError("Codex model catalog contains an invalid row")
                rows.extend(page["data"])
                next_cursor = page.get("nextCursor")
                if next_cursor is None:
                    break
                if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen:
                    raise RpcError("Codex model catalog pagination is invalid")
                seen.add(next_cursor)
                cursor = next_cursor
            else:
                raise RpcError("Codex model catalog exceeded the pagination limit")
            if not rows:
                raise RpcError("Codex model catalog is empty")

            rate_limits = rpc.request("account/rateLimits/read")
            if not isinstance(rate_limits, dict):
                raise RpcError("Codex rate-limit response is invalid")
            return {"models": rows, "rate_limits": rate_limits}
        finally:
            if rpc is not None:
                rpc.close()
            _auth_lock.release()

    def start(self, thread: Thread, brief: Brief, permit: PermissionCallback) -> SessionHandle:
        return self._open(thread, brief, permit, resume=False)

    def resume(self, thread: Thread, brief: Brief, permit: PermissionCallback) -> SessionHandle:
        return self._open(thread, brief, permit, resume=True)

    def _open(self, thread, brief, permit, *, resume):
        codex_config.validate(brief)
        native_id = (thread.provider_session_id or "").removeprefix("codex:")
        if resume and (not (thread.provider_session_id or "").startswith("codex:") or not native_id):
            raise BriefRefused("Codex resume requires a codex:<thread id> session")
        binary, reason = self._probe()
        if binary is None:
            raise BriefRefused(reason)
        # Also serialize preparation, account inspection and refresh-prone startup.
        with _auth_lock:
            candidate = (Path(config.V2_DATA_DIR) / "codex" / thread.id).resolve()
            with _state_lock:
                if candidate in _open_homes:
                    raise BriefRefused("Codex thread already has an open session")
            argv, env, home = codex_config.prepare(thread.id, brief, binary)
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
            text = message.text
            if message.skill:
                # Codex has the skill installed (`jarvis skills link`), so it is
                # told to use it — the same directive Claude gets. A native
                # skill input item on turn/start is the S1 spike still pending.
                from ..commands import skill_directive

                text = skill_directive(message.skill, message.text)
            inputs = [{"type": "text", "text": text, "text_elements": []}]
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
            # subsequent turns" (TurnStartParams; unchanged 0.153.4 → 0.161.0), so
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
                    if not isinstance(params, dict):
                        s.rpc.reply(msg["id"], error={"code": -32602, "message": "Malformed request params"})
                        raise RpcError("Codex server request params are not an object")
                    if method == "currentTime/read":
                        # A side-effect-free clock read (experimental API). It
                        # carries a threadId and no turnId, so it is answered
                        # here rather than failing the turn-identity check.
                        if params.get("threadId") != s.thread_id:
                            s.rpc.reply(msg["id"], error={"code": -32602, "message": "Request identity mismatch"})
                            raise RpcError("Codex server request identity mismatch")
                        s.rpc.reply(msg["id"], {"currentTimeAt": int(time.time())})
                        continue
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
                    pending_request = _Request()
                    try:
                        yield from self._server_request(h, msg, pending_request)
                    except BaseException:
                        # Whatever broke — a shape we could not read, a dead
                        # transport, an abandoned generator — the request is
                        # answered with an error, never left to be guessed at.
                        if not pending_request.replied:
                            self._refuse(s, msg["id"])
                        raise
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
            # Verified from generate-ts (0.153.4, 0.161.0): AccountRateLimitsUpdatedNotification
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

    @staticmethod
    def _reply(s, request, result=None, **kwargs):
        if request.replied:
            raise RpcError("Codex server request answered twice")
        request.replied = True
        s.rpc.reply(request.rid, result, **kwargs)

    @staticmethod
    def _refuse(s, rid):
        try:
            s.rpc.reply(rid, error={"code": -32603, "message": "Request refused"})
        except Exception:
            pass  # the transport is already gone; closing the session refuses it

    def _server_request(self, h, msg, request):
        s = h.native
        method, p, rid = msg["method"], msg.get("params", {}), msg["id"]
        request.rid = rid
        req_id = str(rid)
        if method == "item/permissions/requestApproval":
            # An empty grant: a permissions expansion is never approved here.
            self._reply(s, request, {"permissions": {}, "scope": "turn"})
            return
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            problem = _approval_problem(p)
            if problem:
                # Fail closed on a shape we cannot read: decline, and never
                # put a half-understood request in front of the owner.
                self._reply(s, request, {"decision": "decline"})
                yield self._event(h, EventKind.ERROR, fatal=False,
                                  message=f"Codex approval request declined: {problem}")
                return
            cwd = p.get("cwd") if isinstance(p.get("cwd"), str) and p["cwd"] else s.brief.cwd
            parts, grants, refusal = _widening(p, cwd)
            if refusal:
                self._reply(s, request, {"decision": "decline"})
                yield self._event(h, EventKind.ERROR, fatal=False,
                                  message=f"Codex approval request declined: {refusal}")
                return
            shell = method == "item/commandExecution/requestApproval"
            item = s.items.get(p.get("itemId"), {})
            name, args = self._tool(item if item else {"type": "commandExecution" if shell else "fileChange"})
            # Only non-null values: a `command: null` must not erase the item's
            # real command and leave the never-approvable rules judging "".
            args.update({k: v for k, v in p.items() if v is not None})
            declined, also = None, []
            item_command = item.get("command") if isinstance(item.get("command"), str) else None
            if shell and (not isinstance(args.get("command"), str) or not args["command"].strip()):
                declined = "no command to judge"
            elif shell and item_command is not None and not _same_command(item_command, args["command"]):
                if p.get("kind") in _PLAIN_KINDS and p.get("approvalId") is None:
                    # A plain approval naming a different command than the
                    # item runs: judging either alone could pass the one that runs.
                    declined = "its command differs from the item's"
                else:
                    # Expected shapes: writeStdin carries the text typed into a
                    # running terminal, and an approvalId marks a subcommand
                    # (the zsh exec bridge). Both commands are judged by layer
                    # 1 and a human sees both.
                    args["item_command"] = item_command
                    also.append(item_command)
                    parts.append(f"command {args['command']} (in running {item_command})")
            if declined:
                self._reply(s, request, {"decision": "decline"})
                yield self._event(h, EventKind.ERROR, fatal=False,
                                  message=f"Codex approval request declined: {declined}")
                return
            line = _headline(parts)
            if line:
                args["sandbox_widening"] = line
            args["req_id"] = req_id
            pending = _Pending(False)
            with s.mutex:
                if req_id in s.pending:
                    raise RpcError("Duplicate Codex request id")
                s.pending[req_id] = pending
            yield self._event(h, EventKind.APPROVAL_REQUESTED, req_id=req_id, tool=name, args=args, command=args.get("command"))
            try:
                # Deliberately on the send caller's thread: broker context survives.
                # A widening goes as a keyword the model cannot reach; a
                # callback that does not take it raises, and that denies.
                decision = (s.permit(name, args, s.brief, widening=line, grants=tuple(grants),
                                     also_commands=tuple(also)) if line
                            else s.permit(name, args, s.brief))
                decision = Decision(decision)
            except Exception:
                decision = Decision.DENY
            with s.mutex:
                # answer() can only take an approval *back* (DENY); a yes comes
                # from the permit callback alone, never from a racing answer.
                if pending.ready.is_set() and pending.value == Decision.DENY:
                    decision = Decision.DENY
                if s.closed.is_set() or s.cancelled.is_set() or s.rpc.failed.is_set():
                    decision = Decision.DENY
                s.pending.pop(req_id, None)
            self._reply(s, request, {"decision": "accept" if decision == Decision.ALLOW else "decline"})
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
            self._reply(s, request, {"answers": answers})
            return
        # Anything else — including an approval- or permission-shaped method a
        # newer Codex adds — is refused with an error, never approved.
        self._reply(s, request, error={"code": -32601, "message": "Unsupported server request"})
        yield self._event(h, EventKind.ERROR, message=f"Unexpected Codex server request refused: {str(method)[:80]}",
                          fatal=False)

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
                # Approvals are decided by the permit callback (the broker),
                # which runs the never-approvable rules. A direct answer may
                # only withdraw one; it can never turn a refusal into a yes.
                decision = Decision(decision)
                if decision != Decision.DENY:
                    raise ValueError("A Codex approval is decided by the approval broker; "
                                     "answer() can only deny it")
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
