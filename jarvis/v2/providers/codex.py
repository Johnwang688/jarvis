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
- a file approval is judged on every path its item's changes write, in
  every version the item reported (`_remember`: `item/started` and each
  `patchUpdated`, bounded by CHANGE_PATH_CAP/CHANGE_WORK_CAP), a rename's
  destination (`kind.move_path`) included, never on a `paths` or `changes`
  the request supplies, and declined unasked, in every profile, when its
  item was never seen, is not a `fileChange`, names no file or is past
  those caps, or a change's path or kind cannot be read
  (`_change_paths`): the request itself names no paths (Codex 0.161 starts
  the item first, recorded in `patch_approval*.jsonl`). An approval naming
  an item of the other kind is declined too;
- every filesystem grant a widening opens is judged by layer 1 first: one
  that is or contains protected state or touches a credential directory is
  DENY, unasked; and the headline, built from Codex-supplied strings, is one
  cleaned and capped line;
- an approval that would **widen the sandbox** (extra permissions, a
  managed-network prompt, a grant root, terminal input) is put to a human
  every time, with no Always (`permit(..., widening=...)`), and declined with
  no human. The reply is a bare `{decision}`, so the grant cannot be split off;
- owner chats select native `user` review and verify it in effective config
  and start/resume receipts. Every command/file approval callback in a chat
  is owner-required widening, even without an explicit grant field: a bare
  callback does not prove execution stays sandboxed. Ordinary sandboxed
  work runs without callbacks. Tasks retain `auto_review` and its existing
  fallback for plain approvals (design R8);
- a non-null approval field outside the verified schema
  (`_KNOWN_APPROVAL_FIELDS`) counts as a widening — it may be a new grant —
  so a human is asked, shown it, and offered no Always;
- a server request from another thread is handled only when a
  `subAgentActivity` announcement on this session's thread (or a known
  child's) named that thread as a sub-agent: then it is never fatal, and its
  command/file approval is always an owner-asked widening naming the
  sub-agent (CHILD_LIMIT). Any other thread's request is a fatal identity
  mismatch.

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
import hashlib
import inspect
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Iterator

from jarvis import config
from jarvis.v2.model import ProviderName, Thread
from jarvis.v2.provider import (Brief, BriefRefused, Decision, Event, EventKind,
                               PermissionCallback, SessionHandle, SteerRefused, Usage,
                               UserMessage)
from ..approvals import clean_line
from . import codex_cli, codex_config
from .codex_rpc import RpcCancelled, RpcError, RpcProcess, RpcTimeout
from .codex_usage import Accounting

LOG = logging.getLogger(__name__)
START_TIMEOUT = 30.0
INTERRUPT_TIMEOUT = 10.0
# How long `steer` waits for Codex to answer `turn/steer`. The app-server
# answers at once (it only appends to the active turn's input), so this is a
# backstop for a wedged transport, not a latency.
STEER_TIMEOUT = 10.0
# JSON-RPC's "method not found": a Codex without `turn/steer` (both verified
# schemas, 0.153.4 and 0.161.0, have it), which the daemon steers by
# interrupting instead.
_NO_METHOD = -32601
_STALE_STEERS = 64
# One deadline for a whole account-metadata session, which holds `_auth_lock`
# and so keeps every Codex turn from starting while it runs.
METADATA_DEADLINE_S = 10.0
# The same, for a one-shot turn (`one_shot`, the chat titler): it holds the
# lock as long, so no Codex turn waits behind it for more than this.
ONE_SHOT_DEADLINE_S = 20.0
# The only items a one-shot turn may produce; any other is a tool.
_ONE_SHOT_ITEMS = frozenset({"userMessage", "agentMessage", "reasoning"})
# `item/<this>/…` notifications a one-shot turn may carry: lifecycle, and the
# deltas of the items above. Any other (`commandExecution`, `fileChange`,
# `mcpToolCall`, `autoApprovalReview` …) is a tool.
_ONE_SHOT_ITEM_METHODS = frozenset({"started", "completed"}) | _ONE_SHOT_ITEMS
MODEL_PAGE_LIMIT = 100
MODEL_PAGE_CAP = 20
# Held while a session prepares and starts, and by `send` for a whole turn.
_auth_lock = threading.Lock()
_state_lock = threading.Lock()
_open_homes: set[Path] = set()


# Codex asking to approve an MCP tool call (plan 2026-10-10 §4.2). The
# jarvis server's exact offered tools are `approval_mode = "approve"` in the
# private config, so none should arrive; if one does — an elicitation, or a
# `requestUserInput` marked as an MCP tool approval — it is declined with a
# non-fatal ERROR, never a QUESTION for the owner, never an accept.
MCP_ELICITATION = "mcpServer/elicitation/request"
MCP_DECLINED = ("Codex asked to approve a Jarvis tool call; declined "
                "(Jarvis decides its own tools)")
# What marks a `requestUserInput` as an MCP tool approval (strings in the
# 0.161 binary: the question id `mcp_tool_call_approval…`, and the
# `codex_approval_kind` metadata key).
_MCP_APPROVAL_MARKS = ("mcp_tool_call_approval", "codex_approval_kind")


# Codex sub-agents (SV-2a, 2026-10-15; SV-0 findings 8-11). A child thread
# runs on this session's app-server connection, is announced only by a
# `subAgentActivity` `item/started` (kind `started`, `agentThreadId`,
# `agentPath`) on this session's thread or a known child's, and sends its
# server requests here under its own `threadId`. A known child's request is
# never fatal to the orchestrator's turn: a command/file approval goes
# through the same `permit` gate as the session's own, always as an
# owner-asked widening that names the sub-agent (no Always; deny-all,
# strict, a timeout or no human declines); an MCP elicitation is declined;
# a clock read is answered; a question is refused; anything else is refused
# as for the session. A thread that is neither this session's nor a known
# child stays a fatal identity mismatch.
# Past CHILD_LIMIT the oldest child whose turn has completed is forgotten to
# make room; with none, a new child is not registered (it fails closed).
CHILD_LIMIT = 256
CHILD_ITEM_LIMIT = 512      # the children's in-progress command/file items
# The label is the model-chosen `agentPath`, shown on an approval card: only
# these characters survive (anything else becomes "_"), so it cannot mimic
# the headline's own " · " parts, and it is capped so the child part of the
# headline (PART_CAP) is never cut.
CHILD_LABEL_CAP = 40
_CHILD_LABEL_UNSAFE = re.compile(r"[^A-Za-z0-9_./-]+")
CHILD_UNNAMED = "unnamed"


def _child_label(path) -> str:
    if not isinstance(path, str):
        return CHILD_UNNAMED
    label = _CHILD_LABEL_UNSAFE.sub("_", path).strip("_")
    if len(label) > CHILD_LABEL_CAP:
        label = label[:CHILD_LABEL_CAP - 1] + "…"
    return label or CHILD_UNNAMED


def _from_child(child: str | None) -> str:
    return f" (from Codex sub-agent ‹{child}›)" if child else ""


# What a child says and does (SV-2, 2026-10-15; SV-0 findings 8, 12, 13). A
# known child's notifications arrive under its own `threadId`; they are the
# child's, never the orchestrator's: SUBAGENT (its lifecycle) and
# SUBAGENT_EVENT (one thing it said or did), the kinds SV-1 made for Claude's,
# recorded apart by the daemon (`subagents.py`). Its sid is "cx:<threadId>".
# Its status is its own turn's: `turn/started` running, `turn/completed`
# done/failed/stopped (the parent's `subAgentActivity` `completed` is absent
# for a failed or externally interrupted child, so it is never read as the
# end). Its tokens are its own thread's totals, never the session's
# accounting. Its words are scrubbed by the record (`subagents.prepare`), as
# Claude's are. Deltas, reasoning, plans and its own brief are not passed on;
# a tool is its name and the arguments the daemon labels it from, then its
# outcome — never a result. A child outlives the turn that spawned it, so
# the turn's end never ends one; the daemon ends them with the session.
CHILD_TEXT_CHARS = 4000      # a child's text or failure, as the record keeps it
CHILD_CALL_LIMIT = 256       # a child's tool calls started and not yet finished
_SID_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_CHILD_END = {"completed": "done", "failed": "failed", "interrupted": "stopped"}
_CHILD_TOOL_ITEMS = ("commandExecution", "fileChange", "mcpToolCall")


def child_sid(thread_id) -> str:
    """A Codex child's sid: "cx:" and its threadId. An id the pattern does not
    fit is hashed, never dropped (the same rule as Claude's `cl:`)."""
    text = str(thread_id or "")
    if _SID_ID.fullmatch(text):
        return "cx:" + text
    return "cx:h" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:32]


def _takes_keyword(permit, name: str) -> bool:
    """Whether `permit` takes the keyword `name` (or any keyword). Only such a
    callback is handed the child's sid (SV-3): one that does not would raise,
    and a raise denies — the sid must never change an answer."""
    try:
        params = inspect.signature(permit).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(q.kind is q.VAR_KEYWORD
               or (q.name == name and q.kind in (q.KEYWORD_ONLY, q.POSITIONAL_OR_KEYWORD))
               for q in params)


def _child_text(value) -> str:
    """A child's words bound for its record, capped. They are scrubbed in one
    place for both providers, `SubagentRecords.prepare` (`redact.scrub_text`),
    before memory, the file or a route sees them; nothing else reads them."""
    # Twice the record's cap: it scrubs before it cuts, so a credential is
    # never halved here into a shape no layer knows.
    return value[:CHILD_TEXT_CHARS * 2] if isinstance(value, str) else ""


def _count(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 1 << 53 else None


@dataclass
class _ChildView:
    """One Codex child as its record sees it (SV-2)."""
    sid: str
    name: str
    depth: int = 1
    parent_sid: str | None = None
    status: str = "running"
    announced: bool = False
    turn: str | None = None            # the child's own turn now running
    tokens: int | None = None
    tool_uses: int = 0
    duration_ms: int | None = None
    last_tool: str = ""
    stop_reason: str | None = None
    asked_to_stop: bool = False        # the orchestrator's interrupt_agent said so
    calls: dict = field(default_factory=dict)  # its open tool calls, oldest first


def _mcp_approval(params, depth: int = 0) -> bool:
    """Whether a server request's params carry an MCP-approval mark, in any
    key or string value, a few levels down. Wrongly true only declines a
    question; wrongly false would put an approval in front of the owner."""
    if depth > 6:
        return True
    if isinstance(params, str):
        return any(mark in params for mark in _MCP_APPROVAL_MARKS)
    if isinstance(params, dict):
        return any(_mcp_approval(k, depth + 1) or _mcp_approval(v, depth + 1)
                   for k, v in list(params.items())[:200])
    if isinstance(params, list):
        return any(_mcp_approval(v, depth + 1) for v in params[:200])
    return False


class MetadataBusy(RuntimeError):
    """A Codex session holds the login lock (a turn runs, or one is
    starting): account metadata is not read now. The HUD backs off and keeps
    its last snapshot."""


@dataclass
class _Pending:
    question: bool
    ready: threading.Event = field(default_factory=threading.Event)
    value: Decision | str | None = None


@dataclass
class _Steer:
    """One `turn/steer` in flight, answered by the turn loop that reads the
    transport."""
    ready: threading.Event = field(default_factory=threading.Event)
    ok: bool = False
    unsupported: bool = False
    reason: str = ""
    abandoned: bool = False     # its waiter timed out and counted it as delivered


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


# Command versus terminal-input shapes, not proof that execution stays in
# the sandbox. Owner chat callbacks always take the human widening path.
# A kind not listed at all cannot be described and is declined outright.
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


# The kinds a file change may have (generate-ts, 0.161.0 `PatchChangeKind`):
# `add` and `delete` touch `path`; `update` touches `path` and, when it
# renames, `move_path` as well. The destination is a file written like any
# other, so layer 1 must judge it and the owner must see it.
_CHANGE_KINDS = ("add", "delete", "update")


def _change_paths(changes) -> tuple[list[str], str | None]:
    """Every path a fileChange item's `changes` would write, rename
    destinations included, and why they cannot be read, or None.

    Lenient for display (whatever can be read is listed) and strict for an
    approval, which is declined on any problem: a destination we cannot
    find is one layer 1 cannot protect."""
    if not isinstance(changes, list):
        return [], "changes is not a list"
    paths: list[str] = []
    problem = None
    for change in changes:
        if not isinstance(change, dict) or not isinstance(change.get("path"), str) or not change["path"]:
            problem = problem or "a change names no path"
            continue
        paths.append(change["path"])
        kind = change.get("kind")
        if not isinstance(kind, dict) or kind.get("type") not in _CHANGE_KINDS:
            problem = problem or "a change's kind cannot be read"
            continue
        move = kind.get("move_path")
        if kind["type"] == "update" and isinstance(move, str) and move:
            paths.append(move)
        elif move is not None and (kind["type"] != "update" or not isinstance(move, str)):
            # A destination on a kind that has none, or one that is not a
            # path: something we do not understand is being moved.
            problem = problem or "a change's move_path cannot be read"
        if set(kind) - {"type", "move_path"}:
            # A field a newer Codex added to the kind may be another
            # destination (the schema has none): not guessed at.
            problem = problem or "a change's kind has an unknown field"
    return paths, problem


# Every path a fileChange item has ever reported, by `item/started`, each
# `item/fileChange/patchUpdated` and `item/completed`, and the first reason
# any version could not be read: kept on the stored item under this key. A
# later, partial update cannot hide an earlier destination from layer 1, and
# an unreadable version is not forgotten when a readable one follows.
_PATHS_SEEN = "_jarvis_paths_seen"
# This runs on the turn loop's thread for every update, so its cost is
# bounded per item: past CHANGE_PATH_CAP distinct paths, or once its
# updates have listed CHANGE_WORK_CAP changes in all (a streamed patch may
# resend every change each time), the item is past judging: it is marked so,
# later updates are not read, and its approval is declined unasked.
CHANGE_PATH_CAP = 4096
CHANGE_WORK_CAP = 1_000_000
TOO_MANY_PATHS = f"its item names more than {CHANGE_PATH_CAP} files"
TOO_MUCH_WORK = "its item's updates are too large to judge"


class _Seen:
    """Every path an item reported, in order (a dict as an ordered set),
    the first reason one of its versions could not be read, and how many
    changes its versions listed."""
    __slots__ = ("paths", "problem", "work", "full")

    def __init__(self):
        self.paths: dict[str, None] = {}
        self.problem: str | None = None
        self.work = 0
        self.full = False


def _remember(prev, item: dict) -> dict:
    """`item` to store, carrying what every version of it reported (`prev`
    is the version stored before it, if any). Linear in this version's
    changes, and nothing once the item is past judging."""
    if item.get("type") != "fileChange":
        return item
    seen = prev.get(_PATHS_SEEN) if isinstance(prev, dict) and prev.get("type") == "fileChange" else None
    if not isinstance(seen, _Seen):
        seen = _Seen()
    if not seen.full:
        changes = item.get("changes", [])
        seen.work += len(changes) if isinstance(changes, list) else 1
        if seen.work > CHANGE_WORK_CAP:
            seen.problem, seen.full = seen.problem or TOO_MUCH_WORK, True
        else:
            paths, problem = _change_paths(changes)
            seen.problem = seen.problem or problem
            seen.paths.update(dict.fromkeys(paths))
            if len(seen.paths) > CHANGE_PATH_CAP:
                seen.problem, seen.full = seen.problem or TOO_MANY_PATHS, True
    return {**item, _PATHS_SEEN: seen}


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
    # Only identities/completion flags; never retain raw MCP results here.
    mcp_items: dict[str, tuple[str, str, str, bool]] = field(default_factory=dict)
    mcp_calls: set[str] = field(default_factory=set)
    closed: threading.Event = field(default_factory=threading.Event)
    cancelled: threading.Event = field(default_factory=threading.Event)
    mutex: threading.RLock = field(default_factory=threading.RLock)
    sending: threading.Lock = field(default_factory=threading.Lock)
    interrupt_at: float | None = None
    # A model/effort change waiting for the next turn/start (set_model).
    override: dict | None = None
    # Owner steering (2026-10-08): `turn/steer` requests the turn loop has
    # yet to see answered, by request id; ids whose waiter gave up, so a late
    # answer is ignored rather than failing a later turn; and whether this
    # Codex lacks the method.
    steers: dict = field(default_factory=dict)
    stale_steers: deque = field(default_factory=lambda: deque(maxlen=_STALE_STEERS))
    steer_unsupported: bool = False
    # Sub-agents (SV-2a): child threadId -> its label (`agentPath`), oldest
    # first, for the session's life (children outlive the turn that spawned
    # them); which of them have no turn running (CHILD_LIMIT evicts those);
    # and their in-progress command/file items by item id -> (threadId,
    # item), so a child's approval is judged on what its item really runs or
    # writes. Written only by the turn loop that reads the transport.
    children: dict[str, str] = field(default_factory=dict)
    idle_children: set[str] = field(default_factory=set)
    child_items: dict[str, tuple[str, dict]] = field(default_factory=dict)
    # What each known child's record is told (SV-2), by threadId: made and
    # forgotten with its `children` entry, so it is bounded by CHILD_LIMIT.
    child_views: dict[str, _ChildView] = field(default_factory=dict)


class CodexProvider:
    name = ProviderName.CODEX

    def __init__(self) -> None:
        # Set by `cancel_metadata` at daemon shutdown: an account-metadata
        # app-server in flight stops at its next poll and is closed.
        self._metadata_stop = threading.Event()

    def cancel_metadata(self) -> None:
        """Daemon shutdown: stop any account-metadata read in flight (its
        transport polls this between messages, so it ends within ~50 ms and
        its own `finally` closes the app-server) and refuse new ones."""
        self._metadata_stop.set()

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

    def account_metadata(self, *, deadline_s: float = METADATA_DEADLINE_S) -> dict:
        """Read the logged-in account's model catalog and quota snapshot.

        A short-lived app-server, never a model turn. It prepares and talks to
        the account under `_auth_lock`, the lock session startup takes — and
        `send` holds that lock for a whole turn — so:

        - a held lock is `MetadataBusy` at once, **before** the probe (no
          `codex --version` / `login status` spawned behind a running turn),
          and the caller backs off (`HUDLedger`, ~30 s);
        - once it has the lock, the whole session has one deadline
          (`deadline_s`, ~10 s), so a stalled app-server cannot keep a turn
          from starting for longer than that;
        - the lock is released whatever `close()` does;
        - `cancel_metadata()` (daemon shutdown) ends a read in flight at the
          transport's next poll, and refuses a new one.

        The catalog and the quota are independent: either half failing is
        `None` in the result, with the other kept; both failing raises.
        Failures are logged by operation and error class only.
        """
        stop = self._metadata_stop
        if stop.is_set():
            raise RpcCancelled("Codex account metadata cancelled (shutting down)")
        if _auth_lock.locked():
            raise MetadataBusy("a Codex session holds the login lock")
        binary, reason = self._probe()
        if binary is None:
            raise BriefRefused(reason)
        if stop.is_set():
            raise RpcCancelled("Codex account metadata cancelled (shutting down)")
        if not _auth_lock.acquire(blocking=False):
            raise MetadataBusy("a Codex session holds the login lock")
        try:
            rpc = None
            try:
                deadline = time.monotonic() + deadline_s
                argv, env, home = codex_config.prepare_metadata(binary)
                rpc = RpcProcess(argv, cwd=str(home), env=env, stop=stop.is_set)
                rpc.start().initialize(deadline=deadline)
                account_result = rpc.request("account/read", {"refreshToken": False}, deadline=deadline)
                if not isinstance(account_result, dict):
                    raise RpcError("Codex account response is invalid")
                account = account_result.get("account") or {}
                if not isinstance(account, dict):
                    raise RpcError("Codex account response is invalid")
                if account.get("type") != "chatgpt":
                    raise BriefRefused("Codex requires ChatGPT authentication; API-key billing refused")
                rows = self._metadata_half("model/list", lambda: self._model_rows(rpc, deadline))
                rate_limits = self._metadata_half("account/rateLimits/read",
                                                  lambda: self._rate_limits(rpc, deadline))
                if rows is None and rate_limits is None:
                    raise RpcError("Codex account metadata unavailable")
                return {"models": rows, "rate_limits": rate_limits}
            finally:
                if rpc is not None:
                    rpc.close()
        finally:
            _auth_lock.release()

    @staticmethod
    def _metadata_half(operation, read):
        try:
            return read()
        except (RpcError, OSError, ValueError, TypeError) as exc:
            LOG.warning("Codex metadata %s failed (%s)", operation, type(exc).__name__)
            return None

    @staticmethod
    def _model_rows(rpc, deadline) -> list[dict]:
        rows, cursor, seen = [], None, set()
        for _ in range(MODEL_PAGE_CAP):
            params = {"includeHidden": False, "limit": MODEL_PAGE_LIMIT}
            if cursor is not None:
                params["cursor"] = cursor
            page = rpc.request("model/list", params, deadline=deadline)
            if not isinstance(page, dict) or not isinstance(page.get("data"), list):
                raise RpcError("Codex model catalog response is invalid")
            if any(not isinstance(row, dict) for row in page["data"]):
                raise RpcError("Codex model catalog contains an invalid row")
            rows.extend(page["data"])
            next_cursor = page.get("nextCursor")
            if next_cursor is None:
                break
            # A cursor seen before would page forever: refuse it at once.
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen:
                raise RpcError("Codex model catalog pagination is invalid")
            seen.add(next_cursor)
            cursor = next_cursor
        else:
            raise RpcError("Codex model catalog exceeded the pagination limit")
        if not rows:
            raise RpcError("Codex model catalog is empty")
        return rows

    @staticmethod
    def _rate_limits(rpc, deadline) -> dict:
        rate_limits = rpc.request("account/rateLimits/read", deadline=deadline)
        if not isinstance(rate_limits, dict):
            raise RpcError("Codex rate-limit response is invalid")
        return rate_limits

    def one_shot(self, text: str, *, model: str, effort: str | None = None,
                 instructions: str = "", deadline_s: float = ONE_SHOT_DEADLINE_S) -> str:
        """One tool-less model turn on a short-lived app-server, for the chat
        titler (titles.py): `text` in, the final agent message out.

        `account_metadata`'s shape, for the same reason — it holds
        `_auth_lock`, which every Codex turn needs to start: `MetadataBusy`
        at once if the lock is held (before the probe), one deadline for the
        whole session, the lock released whatever `close()` does, and
        `cancel_metadata()` ends it at the transport's next poll.

        And as little as a turn can be: a private home with no MCP servers
        and no skills (`codex_config.prepare_title`), a fresh empty temporary
        cwd, a read-only sandbox with the network off, approvals `never`, web
        search disabled, an ephemeral thread, and **no environment**
        (`environments: []`, experimental in 0.153.4 and 0.161.0: "Empty
        disables environment access", so the turn has no shell or file
        tools). The app-server has no "no tools at all" switch beyond that;
        so any tool item, any server request but a clock read, a model the
        reply did not honour, or a sandbox wider than read-only ends the
        session with an error, never a title, and nothing from that turn is
        used. Failures carry the error class only, never the text.

        **The limit, stated** (review of PR #62, L2): `environments: []` has
        not been verified against a live ChatGPT-auth turn. If such a turn
        ignores it, a read-only command can run before the turn is refused;
        its output is discarded."""
        stop = self._metadata_stop
        if stop.is_set():
            raise RpcCancelled("Codex one-shot cancelled (shutting down)")
        if _auth_lock.locked():
            raise MetadataBusy("a Codex session holds the login lock")
        binary, reason = self._probe()
        if binary is None:
            raise BriefRefused(reason)
        if stop.is_set():
            raise RpcCancelled("Codex one-shot cancelled (shutting down)")
        if not _auth_lock.acquire(blocking=False):
            raise MetadataBusy("a Codex session holds the login lock")
        try:
            rpc = None
            cwd = None
            try:
                deadline = time.monotonic() + deadline_s
                argv, env, home = codex_config.prepare_title(binary, model, effort)
                cwd = tempfile.mkdtemp(prefix="cwd-", dir=home / "tmp")
                rpc = RpcProcess(argv, cwd=cwd, env=env, stop=stop.is_set)
                rpc.start().initialize(deadline=deadline)
                account = rpc.request("account/read", {"refreshToken": False}, deadline=deadline)
                if not isinstance(account, dict) or not isinstance(account.get("account"), dict) \
                        or account["account"].get("type") != "chatgpt":
                    raise BriefRefused("Codex requires ChatGPT authentication; API-key billing refused")
                sandbox = {"type": "readOnly", "networkAccess": False}
                params = {"cwd": cwd, "modelProvider": "openai", "model": model,
                          "sandbox": "read-only", "approvalPolicy": "never",
                          "ephemeral": True, "environments": [],
                          "allowProviderModelFallback": False}
                if instructions:
                    params["developerInstructions"] = instructions
                if effort:
                    params["config"] = {"model_reasoning_effort": effort}
                started = rpc.request("thread/start", params, deadline=deadline)
                thread = started.get("thread") if isinstance(started, dict) else None
                native = thread.get("id") if isinstance(thread, dict) else None
                granted = started.get("sandbox") if isinstance(started, dict) else None
                if (not isinstance(native, str) or not native
                        or started.get("model") != model
                        or started.get("modelProvider", "openai") != "openai"
                        or started.get("approvalPolicy") != "never"
                        or not isinstance(granted, dict) or granted.get("type") != "readOnly"
                        or granted.get("networkAccess")):
                    raise BriefRefused("Codex did not honour the title turn's model or sandbox")
                turn = {"threadId": native, "input": [{"type": "text", "text": text}],
                        "approvalPolicy": "never", "sandboxPolicy": sandbox, "environments": []}
                if effort:
                    turn["effort"] = effort
                request = rpc.send("turn/start", turn, deadline=deadline)
                return self._one_shot_reply(rpc, native, request, deadline)
            finally:
                if rpc is not None:
                    rpc.close()
                if cwd is not None:
                    shutil.rmtree(cwd, ignore_errors=True)
        finally:
            _auth_lock.release()

    @staticmethod
    def _one_shot_reply(rpc, native: str, request, deadline: float) -> str:
        """Read one turn to its end: the final agent message, or an error."""
        text, turn_id = None, None
        while True:
            msg = rpc.next_message(deadline=deadline)
            if "method" not in msg:
                if msg.get("id") == request:
                    if "error" in msg or not isinstance(msg.get("result"), dict):
                        raise RpcError("Codex turn/start failed")
                    turn_id = ((msg["result"].get("turn") or {}).get("id")) or turn_id
                continue
            method, params = msg["method"], msg.get("params")
            if not isinstance(params, dict):
                params = {}
            if "id" in msg:
                if method == "currentTime/read" and params.get("threadId") == native:
                    rpc.reply(msg["id"], {"currentTimeAt": int(time.time())}, deadline=deadline)
                    continue
                # Nothing is asked or granted in a title turn.
                rpc.reply(msg["id"], error={"code": -32601, "message": "Not available in this session"},
                          deadline=deadline)
                raise RpcError("Codex asked for something during a one-shot turn")
            if method == "model/rerouted":
                raise RpcError("Codex substituted the requested model")
            if method == "account/updated" and params.get("authMode") not in (None, "chatgpt"):
                raise RpcError("Codex changed away from ChatGPT authentication")
            if params.get("threadId") not in (None, native):
                continue
            # Any sign of a tool ends the turn with an error and **nothing
            # from it is used** — not even agent text that came before (the
            # local `text` is dropped with the raise): if a ChatGPT-auth turn
            # ignored `environments: []`, what a read-only command read can
            # never become a title (review of PR #62, L2).
            if method.startswith("item/") and method.split("/")[1] not in _ONE_SHOT_ITEM_METHODS:
                raise RpcError("Codex used a tool during a one-shot turn")
            if method in ("item/started", "item/completed"):
                item = params.get("item")
                kind = item.get("type") if isinstance(item, dict) else None
                if kind not in _ONE_SHOT_ITEMS:
                    raise RpcError("Codex used a tool during a one-shot turn")
                if method == "item/completed" and kind == "agentMessage" and isinstance(item.get("text"), str):
                    text = item["text"]
            elif method == "turn/completed":
                status = (params.get("turn") or {}).get("status")
                if status != "completed":
                    raise RpcError("Codex one-shot turn did not complete")
                if text is None:
                    raise RpcError("Codex one-shot turn produced no message")
                return text

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
            if brief.origin_tag is not None:
                env["JARVIS_ORIGIN_TAG"] = brief.origin_tag
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
                effective = rpc.request("config/read", {"includeLayers": False, "cwd": brief.cwd})["config"]
                codex_config.validate_mcp(brief, effective)
                reviewer = codex_config.approvals_reviewer(brief)
                required = {"forced_login_method": "chatgpt", "model_provider": "openai",
                            "sandbox_mode": "workspace-write", "approval_policy": "on-request",
                            "approvals_reviewer": reviewer}
                if any(effective.get(k) != v for k, v in required.items()):
                    raise BriefRefused("Codex effective config changed authentication or permissions")
                workspace = effective.get("sandbox_workspace_write") or {}
                if workspace.get("network_access", False) is not False:
                    raise BriefRefused("Codex effective config enabled sandbox networking")
                expected_roots = codex_config.git_roots(brief)
                if workspace.get("writable_roots", []) != expected_roots:
                    raise BriefRefused("Codex effective config changed writable roots")
                params = {"cwd": brief.cwd, "modelProvider": "openai", "sandbox": "workspace-write",
                          "approvalPolicy": "on-request", "approvalsReviewer": reviewer,
                          # baseInstructions replaces the native prompt. The additive
                          # developer field preserves it, AGENTS.md and skill loading.
                          "developerInstructions": "\n\n".join(filter(None, (brief.system_append, codex_config.git_note(brief))))}
                if brief.model:
                    params["model"] = brief.model
                overrides = {}
                if brief.effort:
                    overrides["model_reasoning_effort"] = brief.effort
                server = codex_config.session_mcp(brief)
                if server is not None:
                    # A resumed native thread must use this session's new
                    # carrier and exact delegation map, never saved overrides.
                    overrides["mcp_servers.jarvis"] = server
                if overrides:
                    params["config"] = overrides
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
                        or result.get("approvalsReviewer") != reviewer
                        or result.get("sandbox", {}).get("type") != "workspaceWrite"
                        or result.get("sandbox", {}).get("writableRoots", []) != expected_roots
                        or result.get("sandbox", {}).get("networkAccess", False) is not False):
                    raise BriefRefused("Codex did not honor the requested model, effort or sandbox")
                if not resume:
                    accounting = Accounting(home / "usage.json", returned_id, False)
                accounting.save()
                return SessionHandle(thread.id, self.name, "codex:" + returned_id,
                                     _Session(rpc, brief, permit, home, returned_id, accounting))
            except BaseException:
                rpc.close()
                if brief.mcp_servers:
                    # The token the config carries died with this session.
                    try:
                        codex_config.strip_mcp_servers(home / "config.toml")
                    except Exception:  # noqa: BLE001
                        try:
                            os.unlink(home / "config.toml")
                        except OSError:
                            pass
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
            inputs = self._inputs(message)
            s.accounting.complete = False
            s.accounting.turns += 1
            s.accounting.save()
            s.items.clear()
            s.mcp_items.clear()
            s.mcp_calls.clear()
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
                    if "method" in msg and "id" not in msg:
                        # Read off the wire, before the early queue and the
                        # thread/turn filters: a child's announcement and its
                        # items count in the order Codex sent them (SV-2a).
                        self._note_child(s, msg["method"], msg.get("params"))
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
                    elif self._steer_answered(s, msg):
                        pass    # a steer's answer, refused or not: never the turn's failure
                    elif "error" in msg:
                        raise RpcError("Codex control request failed")
                    continue
                method, params = msg["method"], msg.get("params", {})
                if "id" in msg:
                    if not isinstance(params, dict):
                        s.rpc.reply(msg["id"], error={"code": -32602, "message": "Malformed request params"})
                        raise RpcError("Codex server request params are not an object")
                    # A known sub-agent's request (SV-2a) is never fatal; see
                    # CHILD_LIMIT. Unknown threads still fail closed below.
                    child = self._child(s, params)
                    if method == "currentTime/read":
                        # A side-effect-free clock read (experimental API). It
                        # carries a threadId and no turnId, so it is answered
                        # here rather than failing the turn-identity check.
                        # Purely informational, so a known child gets it too.
                        if params.get("threadId") != s.thread_id and child is None:
                            s.rpc.reply(msg["id"], error={"code": -32602, "message": "Request identity mismatch"})
                            raise RpcError("Codex server request identity mismatch")
                        s.rpc.reply(msg["id"], {"currentTimeAt": int(time.time())})
                        continue
                    if method == MCP_ELICITATION:
                        # Codex asking to approve an MCP tool call (2026-10-10,
                        # plan §4.2). It carries no turnId, so it is answered
                        # here, before the turn-identity check would kill the
                        # turn over it: declined, never a QUESTION, never an
                        # accept — Jarvis's own gate decides its tools.
                        # A known child's is declined the same way.
                        if params.get("threadId") not in (None, s.thread_id) and child is None:
                            s.rpc.reply(msg["id"], error={"code": -32602, "message": "Request identity mismatch"})
                            raise RpcError("Codex server request identity mismatch")
                        s.rpc.reply(msg["id"], {"action": "decline", "content": None})
                        if not started:
                            started = True
                            yield self._event(h, EventKind.TURN_STARTED)
                        yield self._event(h, EventKind.ERROR, fatal=False, message=MCP_DECLINED + _from_child(child))
                        continue
                    if child is None:
                        if (params.get("threadId") != s.thread_id
                                or not isinstance(params.get("turnId"), str) or not params["turnId"]
                                or (s.turn_id and params.get("turnId") != s.turn_id)):
                            s.rpc.reply(msg["id"], error={"code": -32602, "message": "Request identity mismatch"})
                            raise RpcError("Codex server request identity mismatch")
                        if s.turn_id is None:
                            s.turn_id = params.get("turnId")
                    # A child's turnId is its own: it never becomes s.turn_id.
                    if not started:
                        started = True
                        yield self._event(h, EventKind.TURN_STARTED)
                    pending_request = _Request()
                    try:
                        yield from self._server_request(h, msg, pending_request, child=child)
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
                        if item.get("type") != "mcpToolCall":
                            s.items[item["id"]] = _remember(s.items.get(item["id"]), item)
                    early.append(msg)
                    continue
                if (isinstance(params, dict) and isinstance(params.get("threadId"), str)
                        and params["threadId"] != s.thread_id and params["threadId"] in s.child_views):
                    # A known child's (SV-2): its own record, never the
                    # orchestrator's turn, text, tools or accounting.
                    yield from self._child_notification(h, method, params)
                    continue
                if isinstance(params, dict) and params.get("threadId") == s.thread_id:
                    # The orchestrator spawning a child (SV-2), whatever turn
                    # the item names: announced, then handled as before.
                    yield from self._announce(h, method, params)
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
                                      cached=u.cached_tokens, cost_usd=None, provider_reported=params["tokenUsage"],
                                      partial_turns=s.accounting.partial_turns)
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
                    # `turn/completed` is Codex's last word on a turn, and its
                    # usage counters are cumulative: whatever it counted for
                    # this turn has been reported by now (0.161.0 sends a
                    # stopped turn's usage before its `turn/completed`, SV-0
                    # `parent_interrupt`), and nothing later can add what it
                    # never counted. So an interrupted or failed turn closes
                    # the accounting as a completed one does: refusing the
                    # next send waited for numbers that never come, and left
                    # the owner's chat dead after a Stop (2026-10-15). Such a
                    # turn is counted in `partial_turns`, because a request
                    # cut off mid-stream may never have been reported at
                    # all. Any other status is unknown, and stays incomplete.
                    s.accounting.complete = status in ("completed", "interrupted", "failed")
                    if status in ("interrupted", "failed"):
                        s.accounting.partial_turns += 1
                    s.accounting.save()
                    completed = True
                    if status in ("interrupted", "failed"):
                        # Said at once, not on the next usage report (which
                        # may never come): the ledger and the HUD show these
                        # totals as a lower bound. No new numbers: the totals
                        # are the ones already reported.
                        u = s.accounting.usage()
                        yield self._event(h, EventKind.USAGE, input=u.input_tokens, output=u.output_tokens,
                                          cached=u.cached_tokens, cost_usd=None, provider_reported={},
                                          partial_turns=s.accounting.partial_turns)
                    yield from self._mcp_unfinished(h)
                    yield self._event(h, EventKind.TURN_FINISHED,
                                      stop={"completed": "end", "interrupted": "interrupted"}.get(status, "error"))
                    return
                else:
                    yield from self._notification(h, method, params)
        except (RpcError, OSError, KeyError, TypeError, ValueError, AttributeError) as exc:
            yield from self._mcp_unfinished(h)
            self.close(h)
            yield self._event(h, EventKind.ERROR, message=str(exc), fatal=True)
        finally:
            # A consumer abandoning the iterator must not leave a model mutating
            # files while another session acquires the shared-login lock.
            if submitted and not completed:
                self.close(h)
            with s.mutex:
                # Under the lock `steer` checks the turn id with, so a steer is
                # either sent into this turn or refused (the daemon queues it).
                s.turn_id = None
                s.pending.clear()
                for rid, waiter in s.steers.items():
                    waiter.reason = "the turn ended first"
                    if waiter.abandoned:
                        # Counted as delivered when its waiter gave up, and
                        # never answered since: only the log can say so
                        # (review of PR #22, round 2).
                        LOG.warning("Codex steer %s was unanswered when the turn ended; "
                                    "it may not have reached the model", rid)
                    waiter.ready.set()
                    s.stale_steers.append(rid)
                s.steers.clear()
            s.interrupt_at = None
            s.mcp_items.clear()
            s.cancelled.clear()
            if acquired:
                _auth_lock.release()
            s.sending.release()

    def _tool(self, item):
        if item.get("type") == "commandExecution":
            return "shell", {"command": item.get("command"), "cwd": item.get("cwd")}
        # Rename destinations (`kind.move_path`) are paths too: layer 1
        # judges every one, and the card shows them.
        return "apply_patch", {"paths": _change_paths(item.get("changes", []))[0],
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
                              provider_reported={"rate_limits": p["rateLimits"]},
                              partial_turns=s.accounting.partial_turns)
        elif method == "item/agentMessage/delta":
            yield self._event(h, EventKind.TEXT_DELTA, text=p["delta"])
        elif method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
            yield self._event(h, EventKind.THINKING, text=p["delta"])
        elif method == "turn/plan/updated":
            yield self._event(h, EventKind.PLAN_UPDATED, plan=p["plan"], explanation=p.get("explanation"))
        elif method in ("item/started", "item/completed", "item/fileChange/patchUpdated"):
            item = p.get("item") or {"id": p.get("itemId"), "type": "fileChange", "changes": p.get("changes", [])}
            kind = item["type"]
            done = method == "item/completed"
            if kind == "mcpToolCall":
                yield from self._mcp_item(h, method, p, item)
                return
            s.items[item["id"]] = _remember(s.items.get(item["id"]), item)
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
                    # generate-ts, Codex 0.161.0: CommandExecutionStatus and
                    # PatchApplyStatus both include `declined`.
                    outcome = "denied" if item.get("status") == "declined" else "ok" if ok else "failed"
                    yield self._event(h, EventKind.TOOL_FINISHED, call_id=item["id"], name=name,
                                      ok=ok, outcome=outcome, summary=summary)
            elif done and kind == "agentMessage":
                yield self._event(h, EventKind.TEXT, text=item["text"])
            elif done and kind == "reasoning":
                yield self._event(h, EventKind.THINKING, text="\n".join(item.get("summary", []) + item.get("content", [])))
            elif done and kind == "plan":
                yield self._event(h, EventKind.PLAN_UPDATED, text=item["text"])
        elif method == "item/autoApprovalReview/completed" and p.get("review", {}).get("status") == "denied":
            mcp = s.mcp_items.get(p.get("targetItemId"))
            action = p.get("action")
            action = action if isinstance(action, dict) else {}
            if mcp is not None or action.get("type") == "mcpToolCall":
                # The installed schema permits a missing/null targetItemId.
                # Early or unmatched reviews must not copy private rationale,
                # titles or action metadata into the daemon's event stream.
                if (p.get("threadId") != s.thread_id or not s.turn_id
                        or p.get("turnId") != s.turn_id):
                    return
                name = mcp[2] if mcp is not None else "mcp"
                if action.get("server") == "jarvis" and action.get("toolName") in ("terminal_read", "hud_browser_view", "hud_browser_act"):
                    name = action["toolName"]
                yield self._event(h, EventKind.REVIEWER_DECLINED, tool=name,
                                  args={}, command=None, reason="refused")
                return
            item = s.items.get(p.get("targetItemId"), {})
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

    def admit_foreground_mcp(self, h, context):
        """One native parent call -> its exact live handle/turn predicate.

        Called before terminal dispatch, outside daemon locks. A request can
        never establish a native turn: send's trusted start response does.
        One-shot IDs stay spent until an actual new turn, including failures.
        """
        s = h.native
        if (not isinstance(context, dict)
                or set(context) != {"thread_id", "turn_id", "call_id", "subagent"}
                or context.get("subagent") is not False
                or not all(isinstance(context.get(key), str) and 0 < len(context[key]) <= 128
                           for key in ("thread_id", "turn_id", "call_id"))):
            return None
        turn, thread, call = context["turn_id"], context["thread_id"], context["call_id"]

        def current():
            with s.mutex:
                return (h.native is s and not s.closed.is_set() and not s.cancelled.is_set()
                        and s.sending.locked() and s.thread_id == thread
                        and s.turn_id == turn)

        with s.mutex:
            if not current() or call in s.mcp_calls or len(s.mcp_calls) >= 4096:
                return None
            s.mcp_calls.add(call)
        return current

    def _mcp_item(self, h, method, params, item):
        """Only current, exact MCP lifecycle items; results stay private."""
        s = h.native
        if (params.get("threadId") != s.thread_id or not s.turn_id
                or params.get("turnId") != s.turn_id):
            return
        call_id, server, tool = (item.get(key) for key in ("id", "server", "tool"))
        if not all(isinstance(value, str) and value and len(value) <= 256
                   for value in (call_id, server, tool)):
            raise RpcError("Codex returned a malformed MCP identity")
        name = tool if server == "jarvis" else f"mcp__{server}__{tool}"
        previous = s.mcp_items.get(call_id)
        identity = (server, tool, name)
        if previous is not None and previous[:3] != identity:
            raise RpcError("Codex changed an MCP call identity")
        if previous is not None and previous[3]:
            return
        if previous is None:
            if len(s.mcp_items) >= 4096:
                raise RpcError("Too many Codex MCP calls in one turn")
            s.mcp_items[call_id] = (*identity, False)
            args = item.get("arguments")
            yield self._event(h, EventKind.TOOL_STARTED, call_id=call_id, name=name,
                              args=args if isinstance(args, dict) and not (server == "jarvis" and tool == "hud_browser_act") else {})
        if method != "item/completed":
            return
        s.mcp_items[call_id] = (*identity, True)
        private = server == "jarvis" and tool in ("terminal_read", "hud_browser_view", "hud_browser_act")
        result = item.get("result")
        content = result.get("content") if isinstance(result, dict) else None
        # Text blocks only, bounded before concatenating; structured metadata,
        # images and transport errors never become a terminal summary.
        parts, remaining = [], 400 if private else 200
        if isinstance(content, list):
            for block in content[:200]:
                if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"][:remaining])
                    remaining -= len(parts[-1])
                    if remaining <= 0:
                        break
        text = "".join(parts)
        denied = server == "jarvis" and text.startswith(("Refused", "The user declined"))
        failed = (item.get("status") != "completed" or item.get("error") is not None
                  or not isinstance(content, list) or (private and not text)
                  or (server == "jarvis" and text.startswith("Error:"))
                  or (server == "jarvis" and tool == "hud_browser_act" and text.startswith("Stopped:")))
        if private:
            from jarvis import tools
            summary = tools.private_summary(tool, "Refused" if denied else
                                            text if tool == "hud_browser_act" and text.startswith("Stopped:") else
                                            "Error" if failed else text)
        else:
            summary = text[:200] or ("error" if failed else "completed")
        yield self._event(h, EventKind.TOOL_FINISHED, call_id=call_id, name=name,
                          ok=not (denied or failed),
                          outcome="denied" if denied else "failed" if failed else "ok", summary=summary)

    def _mcp_unfinished(self, h):
        for call_id, (server, tool, name, done) in list(h.native.mcp_items.items()):
            if not done:
                h.native.mcp_items[call_id] = (server, tool, name, True)
                yield self._event(h, EventKind.TOOL_FINISHED, call_id=call_id, name=name,
                                  ok=False, outcome="failed", summary=("stopped" if tool == "hud_browser_act" else "refused" if tool == "hud_browser_view" else "error") if server == "jarvis" and tool in ("terminal_read", "hud_browser_view", "hud_browser_act") else "interrupted")

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

    @staticmethod
    def _child(s, params) -> str | None:
        """The label of the known sub-agent whose thread sent `params`, or
        None (this session's own thread, or one nobody announced)."""
        tid = params.get("threadId")
        if isinstance(tid, str) and tid != s.thread_id:
            return s.children.get(tid)
        return None

    @staticmethod
    def _note_child(s, method, p) -> None:
        """Learn sub-agents from one notification, as it is read (SV-2a).

        A child is registered only from a `subAgentActivity` `item/started`
        of kind `started` on this session's thread or a known child's (a
        grandchild is announced on its parent's thread) — never from
        `thread/started` or `collabAgentToolCall` (SV-0 findings 9, 10). A
        known child's command/file items are kept, bounded, so its approval
        is judged on its real command or paths. Never raises: a shape it
        cannot read registers nothing, and so leaves that thread unknown."""
        if not isinstance(p, dict) or not isinstance(p.get("threadId"), str):
            return
        tid = p["threadId"]
        if method in ("turn/started", "turn/completed"):
            if tid in s.children:
                if method == "turn/started":
                    s.idle_children.discard(tid)
                else:
                    s.idle_children.add(tid)
            return
        if method == "item/fileChange/patchUpdated":
            item = {"id": p.get("itemId"), "type": "fileChange", "changes": p.get("changes", [])}
        elif method in ("item/started", "item/completed") and isinstance(p.get("item"), dict):
            item = p["item"]
        else:
            return
        if item.get("type") == "subAgentActivity":
            agent = item.get("agentThreadId")
            if (method == "item/started" and item.get("kind") == "started"
                    and (tid == s.thread_id or tid in s.children)
                    and isinstance(agent, str) and agent and agent != s.thread_id
                    and agent not in s.children):
                if len(s.children) >= CHILD_LIMIT:
                    # Forget the oldest child with no turn running; a busy
                    # one is never dropped, so with none the newcomer stays
                    # unknown and its requests fail closed.
                    # Of those, one whose record has also ended first (SV-2):
                    # its last turn's end has been handled, not just read.
                    idle = next((c for c in s.children if c in s.idle_children
                                 and getattr(s.child_views.get(c), "status", "done") != "running"),
                                next((c for c in s.children if c in s.idle_children), None))
                    if idle is None:
                        return
                    del s.children[idle]
                    s.idle_children.discard(idle)
                    for iid in [i for i, (owner, _) in s.child_items.items() if owner == idle]:
                        del s.child_items[iid]
                    with s.mutex:
                        s.child_views.pop(idle, None)
                s.children[agent] = _child_label(item.get("agentPath"))
                # Its record (SV-2): announced when this message is handled,
                # in wire order, which may be after the early queue.
                parent = s.child_views.get(tid) if tid != s.thread_id else None
                with s.mutex:
                    s.child_views[agent] = _ChildView(
                        sid=child_sid(agent), name=s.children[agent],
                        depth=min(parent.depth + 1, 16) if parent is not None else 1,
                        parent_sid=parent.sid if parent is not None else None)
            return
        if (tid != s.thread_id and tid in s.children
                and item.get("type") in ("commandExecution", "fileChange")
                and isinstance(item.get("id"), str) and item["id"]):
            owner, prev = s.child_items.pop(item["id"], (None, None))
            if method == "item/completed":
                return      # no approval follows a finished item
            # Only what an approval is judged on is kept, with every path any
            # version of it reported (never another thread's).
            kept = {k: item[k] for k in ("id", "type", "command", "cwd", "changes") if k in item}
            s.child_items[item["id"]] = (tid, _remember(prev if owner == tid else None, kept))
            while len(s.child_items) > CHILD_ITEM_LIMIT:
                s.child_items.pop(next(iter(s.child_items)))

    # -- what a child says and does (SV-2) -------------------------------------

    def _life(self, h, view: _ChildView, op: str) -> Event:
        """A SUBAGENT lifecycle event for `view`: ids, status and counts, plus
        its cleaned label as the name (the daemon cleans it again)."""
        data = {"sid": view.sid, "op": op, "status": view.status, "depth": view.depth,
                "background": False, "name": view.name, "tool_uses": view.tool_uses}
        for key in ("parent_sid", "last_tool", "tokens", "duration_ms", "stop_reason"):
            value = getattr(view, key)
            if value not in (None, ""):
                data[key] = value
        return self._event(h, EventKind.SUBAGENT, **data)

    def _inner(self, h, view: _ChildView, kind: str, **data) -> Event:
        return self._event(h, EventKind.SUBAGENT_EVENT, sid=view.sid,
                           event={"kind": kind, "data": data})

    def _announce(self, h, method, p):
        """A `subAgentActivity` item/started on this session's thread or a
        known child's. `started` announces the child it names, once (its
        registration is `_note_child`'s); `interrupted` is the spawner's
        `interrupt_agent`, which says why the child's turn will end.
        `completed` is not read: a child's end is its own turn's."""
        s = h.native
        item = p.get("item")
        if (method != "item/started" or not isinstance(item, dict)
                or item.get("type") != "subAgentActivity"):
            return
        agent = item.get("agentThreadId")
        view = s.child_views.get(agent) if isinstance(agent, str) else None
        if view is None:
            return
        if item.get("kind") == "started" and not view.announced:
            view.announced = True
            yield self._life(h, view, "started")
        elif item.get("kind") == "interrupted":
            view.asked_to_stop = True

    @staticmethod
    def _child_tool(item) -> tuple[str, dict]:
        """A child's tool item as a name and the arguments its label is made
        from: a command and its folder, a patch's paths (never its diff), an
        MCP call's arguments (never `hud_browser_act`'s)."""
        kind = item.get("type")
        if kind == "commandExecution":
            return "shell", {k: item[k] for k in ("command", "cwd") if isinstance(item.get(k), str)}
        if kind == "fileChange":
            return "apply_patch", {"paths": _change_paths(item.get("changes", []))[0][:20]}
        server, tool = item.get("server"), item.get("tool")
        server = server if isinstance(server, str) else ""
        tool = tool if isinstance(tool, str) else ""
        name = tool if server == "jarvis" else f"mcp__{server}__{tool}"
        args = item.get("arguments")
        private = server == "jarvis" and tool == "hud_browser_act"
        return name[:256], (dict(args) if isinstance(args, dict) and not private else {})

    @staticmethod
    def _child_outcome(item) -> str:
        status = item.get("status")
        if status == "declined":
            return "denied"
        if item.get("type") == "commandExecution":
            return "ok" if status == "completed" and item.get("exitCode") == 0 else "failed"
        if item.get("type") == "mcpToolCall":
            if status != "completed" or item.get("error") is not None:
                return "failed"
            result = item.get("result")
            content = result.get("content") if isinstance(result, dict) else None
            first = content[0] if isinstance(content, list) and content else None
            text = first.get("text") if isinstance(first, dict) else None
            if item.get("server") == "jarvis" and isinstance(text, str):
                if text.startswith(("Refused", "The user declined")):
                    return "denied"
                if text.startswith(("Error:", "Stopped:")):
                    return "failed"
            return "ok"
        return "ok" if status == "completed" else "failed"

    def _child_notification(self, h, method, p):
        """One notification from a known child's thread, as that child's
        record: never the orchestrator's events, turn or accounting.
        Anything not understood is dropped, never shown as the parent's."""
        s = h.native
        view = s.child_views.get(p["threadId"])
        if view is None:
            return
        if not view.announced:
            # Its announcement was not handled first (it never is otherwise,
            # SV-0 finding 10): it is still a child, started here.
            view.announced = True
            yield self._life(h, view, "started")
        if method == "turn/started":
            turn = p.get("turn") if isinstance(p.get("turn"), dict) else {}
            view.turn = turn.get("id") if isinstance(turn.get("id"), str) else None
            if view.status != "running":
                # A finished child given more to do (sendInput, followupTask):
                # live again, on Codex's own word.
                view.status, view.stop_reason, view.asked_to_stop = "running", None, False
                yield self._life(h, view, "started")
            return
        if method == "turn/completed":
            turn = p.get("turn") if isinstance(p.get("turn"), dict) else {}
            ended = turn.get("id")
            if view.status != "running" or (isinstance(ended, str) and view.turn not in (None, ended)):
                return                      # said once; another turn's end is not this one's
            view.status = _CHILD_END.get(turn.get("status"), "failed")
            view.turn = None
            view.duration_ms = _count(turn.get("durationMs")) if _count(turn.get("durationMs")) is not None \
                else view.duration_ms
            if view.status == "stopped":
                view.stop_reason = ("the orchestrator stopped it" if view.asked_to_stop
                                    else "its turn was interrupted")
            yield self._life(h, view, "finished")
            error = turn.get("error") if isinstance(turn.get("error"), dict) else {}
            text = _child_text(error.get("message"))
            if view.status == "failed" and text.strip():
                # Codex's word on why (a failed child gets no parent-side
                # notice at all, SV-0): the child's, never the orchestrator's.
                yield self._inner(h, view, "result", text=text, status="failed")
            return
        if method == "thread/closed":
            if view.status == "running":
                view.status, view.stop_reason, view.turn = "stopped", "Codex closed it", None
                yield self._life(h, view, "finished")
            return
        if method == "thread/tokenUsage/updated":
            usage = p.get("tokenUsage") if isinstance(p.get("tokenUsage"), dict) else {}
            total = usage.get("total") if isinstance(usage.get("total"), dict) else {}
            tokens = _count(total.get("totalTokens"))
            if tokens is not None and tokens != view.tokens:
                view.tokens = tokens
                yield self._life(h, view, "progress")
            return
        if method == "item/autoApprovalReview/completed":
            review = p.get("review") if isinstance(p.get("review"), dict) else {}
            if review.get("status") != "denied":
                return
            # The tool's name only: never its command, arguments or the
            # reviewer's rationale (the hatch is the orchestrator's, SV-1).
            action = p.get("action") if isinstance(p.get("action"), dict) else {}
            kind = action.get("type")
            if kind in ("command", "execve", "writeStdin"):
                name = "shell"
            elif kind == "applyPatch":
                name = "apply_patch"
            elif kind == "mcpToolCall":
                name = action.get("toolName") if action.get("server") == "jarvis" else "mcp"
            else:
                owner, item = s.child_items.get(p.get("targetItemId"), (None, None)) \
                    if isinstance(p.get("targetItemId"), str) else (None, None)
                name = self._tool(item)[0] if owner == p["threadId"] and item else "unknown"
            yield self._inner(h, view, "reviewer_declined", tool=name if isinstance(name, str) else "mcp")
            return
        if method not in ("item/started", "item/completed") or not isinstance(p.get("item"), dict):
            return                          # deltas, plans, reasoning, status: not passed on
        item = p["item"]
        kind = item.get("type")
        if kind == "subAgentActivity":
            # A grandchild, announced on its parent child's thread.
            yield from self._announce(h, method, p)
            return
        if kind == "agentMessage":
            text = _child_text(item.get("text")) if method == "item/completed" else ""
            if text.strip():
                yield self._inner(h, view, "text", text=text)
            return
        call = item.get("id")
        if kind not in _CHILD_TOOL_ITEMS or not isinstance(call, str) or not 0 < len(call) <= 128:
            return
        name, args = self._child_tool(item)
        if method == "item/started":
            if call in view.calls:
                return
            while len(view.calls) >= CHILD_CALL_LIMIT:
                del view.calls[next(iter(view.calls))]     # the oldest open call
            view.calls[call] = None
            view.tool_uses += 1
            view.last_tool = name
            yield self._inner(h, view, "tool_started", call_id=call, name=name, args=args)
            yield self._life(h, view, "progress")
            return
        if call not in view.calls:
            return
        view.calls.pop(call, None)
        outcome = self._child_outcome(item)
        yield self._inner(h, view, "tool_finished", call_id=call, name=name,
                          ok=outcome == "ok", outcome=outcome)

    def live_subagents(self, h: SessionHandle) -> set[str]:
        """The sids of the children this session's Codex still runs, as far
        as it has said. A model switch rides the next turn/start (no new
        process), so it keeps them all; a closed session runs none."""
        s = h.native
        if s is None or s.closed.is_set():
            return set()
        with s.mutex:
            return {v.sid for v in s.child_views.values() if v.announced and v.status == "running"}

    def _server_request(self, h, msg, request, child: str | None = None):
        """Answer one server request. `child` is the label of the known
        sub-agent that sent it (SV-2a), or None for this session's own."""
        s = h.native
        method, p, rid = msg["method"], msg.get("params", {}), msg["id"]
        request.rid = rid
        req_id = str(rid)
        note = _from_child(child)
        if child is not None and (not isinstance(p.get("turnId"), str) or not p["turnId"]):
            # Every approval or question names its turn (0.161 schema). A
            # child's that does not is refused, not guessed at, and the
            # orchestrator's turn goes on.
            self._reply(s, request, error={"code": -32602, "message": "Request identity incomplete"})
            yield self._event(h, EventKind.ERROR, fatal=False,
                              message=f"Codex server request refused: it names no turn{note}")
            return
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
                                  message=f"Codex approval request declined: {problem}{note}")
                return
            cwd = p.get("cwd") if isinstance(p.get("cwd"), str) and p["cwd"] else s.brief.cwd
            parts, grants, refusal = _widening(p, cwd)
            if refusal:
                self._reply(s, request, {"decision": "decline"})
                yield self._event(h, EventKind.ERROR, fatal=False,
                                  message=f"Codex approval request declined: {refusal}{note}")
                return
            shell = method == "item/commandExecution/requestApproval"
            want = "commandExecution" if shell else "fileChange"
            if child is None:
                item = s.items.get(p.get("itemId"), {})
                item = item if isinstance(item, dict) else {}
            else:
                # Only that child's own item: a sibling's or the session's
                # would judge the wrong command or files.
                owner_tid, item = s.child_items.get(p["itemId"], (None, {}))
                if owner_tid != p.get("threadId") or not isinstance(item, dict) or not item:
                    self._reply(s, request, {"decision": "decline"})
                    yield self._event(h, EventKind.ERROR, fatal=False,
                                      message=f"Codex approval request declined: its item was not seen{note}")
                    return
            if item and item.get("type") != want:
                # An approval naming an item of the other kind would be
                # judged as that item (a command as a patch, or a patch as a
                # command): never, for the session or a child.
                self._reply(s, request, {"decision": "decline"})
                yield self._event(h, EventKind.ERROR, fatal=False,
                                  message=f"Codex approval request declined: its item is not a {want}{note}")
                return
            if not shell:
                # A file approval carries no paths: only its item says what it
                # writes. Unseen, unreadable (a rename's destination
                # included) or naming no file, there is nothing for layer 1
                # to judge, so it is declined unasked in every profile
                # (owner rule: unknown approvals fail closed). A command
                # approval carries its own command and is judged on it.
                # Judged on every path the item ever reported (`_remember`),
                # and declined if any version of it could not be read.
                paths, unreadable = _change_paths(item.get("changes", [])) if item else ([], None)
                seen = item.get(_PATHS_SEEN) if item else None
                if isinstance(seen, _Seen):
                    paths, unreadable = list(seen.paths), seen.problem
                why = ("its item was not seen" if not item else unreadable
                       or (None if paths else "its item names no file"))
                if why:
                    self._reply(s, request, {"decision": "decline"})
                    yield self._event(h, EventKind.ERROR, fatal=False,
                                      message=f"Codex approval request declined: {why}{note}")
                    return
            name, args = self._tool(item if item else {"type": "commandExecution" if shell else "fileChange"})
            # Only non-null values: a `command: null` must not erase the item's
            # real command and leave the never-approvable rules judging "".
            args.update({k: v for k, v in p.items() if v is not None})
            if not shell:
                # What layer 1 judges and the card shows: every path the item
                # ever reported and its latest changes — never a `paths` or
                # `changes` the request supplied.
                args["paths"] = list(paths)
                args["changes"] = item.get("changes", [])
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
                                  message=f"Codex approval request declined: {declined}{note}")
                return
            if child is not None:
                # A sub-agent's approval is always a widening, whatever the
                # reviewer: a human every time, told whose request it is, no
                # Always, and declined with no human (deny-all, strict, a
                # timeout). Never the AUTO fallback a task's own plain
                # approvals keep.
                parts.insert(0, f"Codex sub-agent ‹{child}› (not the orchestrator) "
                                "asks to run beyond its sandbox")
            elif codex_config.approvals_reviewer(s.brief) == "user":
                # With on-request/user, this callback may authorize a sandbox
                # escape even without an explicit permissions overlay. Never
                # let AUTO's "native reviewer decides" fallback answer it:
                # every chat callback takes the owner-only, no-Always path.
                parts.insert(0, "Codex chat requests execution beyond its ordinary sandbox; owner approval required")
            line = _headline(parts)
            if line:
                args["sandbox_widening"] = line
            args["req_id"] = req_id
            pending = _Pending(False)
            with s.mutex:
                if req_id in s.pending:
                    raise RpcError("Duplicate Codex request id")
                s.pending[req_id] = pending
            # A child's approval names its sid (the owner's SV-3 decision,
            # as Claude's do): on the logged gate question and, when the
            # callback takes it, on the owner's card, so the HUD can link
            # the card to the child's row. It decides nothing.
            sid = child_sid(p.get("threadId")) if child is not None else None
            yield self._event(h, EventKind.APPROVAL_REQUESTED, req_id=req_id, tool=name, args=args,
                              command=args.get("command"), **({"subagent": sid} if sid else {}))
            try:
                # Deliberately on the send caller's thread: broker context survives.
                # A widening goes as a keyword the model cannot reach; a
                # callback that does not take it raises, and that denies.
                extra = ({"subagent_sid": sid}
                         if sid and line and _takes_keyword(s.permit, "subagent_sid") else {})
                decision = (s.permit(name, args, s.brief, widening=line, grants=tuple(grants),
                                     also_commands=tuple(also), **extra) if line
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
        if method in ("item/tool/requestUserInput", "tool/requestUserInput") and _mcp_approval(p):
            # "MCP tool approval via request_user_input" (codex 0.161): an
            # approval in a question's clothes. Declined like an elicitation —
            # an error answer, so nothing reads as the owner's choice — never
            # put to the owner as a QUESTION and never accepted.
            self._reply(s, request, error={"code": -32603, "message": "Jarvis declines MCP tool approvals"})
            yield self._event(h, EventKind.ERROR, fatal=False, message=MCP_DECLINED + note)
            return
        if method in ("item/tool/requestUserInput", "tool/requestUserInput") and child is not None:
            # A sub-agent's question is not put to the owner, who could not
            # tell it from the orchestrator's (SV-3 has no view yet). Refused
            # with an error, as an MCP approval in a question is.
            self._reply(s, request, error={"code": -32603, "message": "Jarvis does not relay sub-agent questions"})
            yield self._event(h, EventKind.ERROR, fatal=False,
                              message=f"A Codex sub-agent's question was not shown and was refused{note}")
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
        yield self._event(h, EventKind.ERROR, message=f"Unexpected Codex server request refused: {str(method)[:80]}{note}",
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

    @staticmethod
    def _inputs(message: UserMessage) -> list[dict]:
        """A message as `UserInput` items (turn/start and turn/steer take the
        same shape in 0.153.4 and 0.161.0)."""
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
        return inputs

    def steer(self, h: SessionHandle, message: UserMessage) -> bool:
        """Append `message` to the running turn with `turn/steer`.

        The request is the app-server's own (verified in the 0.153.4 and
        0.161.0 schemas: `{threadId, input, expectedTurnId}` → `{turnId}`):
        Codex adds the input to the active turn and the model takes it at its
        next step — what typing while it works does in the Codex TUI. It is
        sent from the caller's thread and answered on the turn loop's, which
        is the one reading the transport; this waits for that answer.

        Refused, for the daemon to queue, when no turn is running or the turn
        is waiting on an approval or a question (a steer must never read as
        an answer), and when Codex refuses it (the turn ended first, a
        compaction turn). A Codex without the method is refused with the
        `interrupt` fallback, so the daemon interrupts the turn and runs the
        message next instead. No answer within STEER_TIMEOUT is *unknown*,
        not refused: it is taken as delivered and logged, never queued too.
        Returns False for that unconfirmed receipt, so Browser owner context
        cannot treat it as accepted; True means Codex answered successfully.
        """
        s = h.native
        if s is None or s.closed.is_set():
            raise SteerRefused("Codex session is closed")
        if s.steer_unsupported:
            raise SteerRefused("this Codex has no turn/steer", fallback="interrupt")
        inputs = self._inputs(message)
        waiter = _Steer()
        with s.mutex:
            if s.turn_id is None or s.cancelled.is_set():
                raise SteerRefused("no Codex turn is running")
            if s.pending:
                raise SteerRefused("the turn is waiting on an approval or a question")
            try:
                rid = s.rpc.send("turn/steer", {"threadId": s.thread_id, "input": inputs,
                                                "expectedTurnId": s.turn_id})
            except RpcError as exc:
                raise SteerRefused(f"Codex did not take the message ({type(exc).__name__})") from None
            # Registered under the lock the turn loop answers it under, so its
            # answer cannot be read before there is anyone to give it to.
            s.steers[rid] = waiter
        if not waiter.ready.wait(STEER_TIMEOUT):
            with s.mutex:
                if not waiter.ready.is_set():
                    # Unknown, not refused: Codex may have taken it and only
                    # its answer is late. Queueing it as well could deliver it
                    # twice, so it counts as steered; the waiter stays
                    # registered and the turn loop logs the answer when it
                    # comes (review of PR #22).
                    waiter.abandoned = True
                    LOG.warning("Codex did not answer turn/steer %s within %.0f s; "
                                "taken as delivered", rid, STEER_TIMEOUT)
                    return False
        if waiter.ok:
            return True
        if waiter.unsupported:
            s.steer_unsupported = True
            raise SteerRefused("this Codex has no turn/steer", fallback="interrupt")
        raise SteerRefused(f"Codex refused the steer: {waiter.reason or 'no reason given'}")

    @staticmethod
    def _steer_answered(s: _Session, msg: dict) -> bool:
        """True when `msg` answers a `turn/steer` (handed to its waiter), or a
        steer whose waiter already gave up (ignored: a late refusal must not
        fail whatever turn is reading the transport now)."""
        rid = msg.get("id")
        with s.mutex:
            waiter = s.steers.pop(rid, None)
            if waiter is None:
                return rid in s.stale_steers
        if "error" in msg:
            error = msg["error"] if isinstance(msg["error"], dict) else {}
            code = error.get("code") if isinstance(error.get("code"), int) else None
            text = error.get("message") if isinstance(error.get("message"), str) else ""
            # Only "this Codex has no such method" switches the session to the
            # interrupt fallback: JSON-RPC's method-not-found, or an invalid
            # request that names `turn/steer` itself. An unknown variant of
            # anything else (an input item) is a refusal like any other, and
            # so are 0.161.0's own steer errors — "no active turn to steer",
            # "expected active turn id … but found …", "cannot steer a
            # review/compact turn" — all of which queue the message.
            waiter.unsupported = code == _NO_METHOD or (code == -32600 and "turn/steer" in text)
            waiter.reason = clean_line(text, 160) if text else f"code {code}"
        else:
            waiter.ok = True
        if waiter.abandoned:
            # The steer's waiter gave up (`STEER_TIMEOUT`) and counted it as
            # delivered; only the log can say how it really went.
            if waiter.ok:
                LOG.info("Codex took the late-answered steer %s", rid)
            else:
                LOG.warning("Codex refused the late-answered steer %s (%s); the message "
                            "did not reach the turn", rid, waiter.reason)
        waiter.ready.set()
        return True

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
        if s.brief.mcp_servers:
            # A jarvis server's env holds the session's caller token (plan
            # §1.3): a closed session's config keeps none. The daemon has
            # already revoked it; this takes it off the disk.
            try:
                codex_config.strip_mcp_servers(s.home / "config.toml")
            except Exception as exc:  # noqa: BLE001 — close must not raise
                LOG.warning("Cannot strip MCP servers from a closed Codex config (%s); "
                            "removing it", type(exc).__name__)
                try:
                    os.unlink(s.home / "config.toml")
                except OSError:
                    pass
        with _state_lock:
            _open_homes.discard(s.home)
