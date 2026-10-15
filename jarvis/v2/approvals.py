"""The v2 approval broker — design §6 layer 5, and §6.1's counterpart.

`PendingApprovals.ask()` blocks the thread that called it while the owner is
asked somewhere else, and **denies on every failure path**: timeout, shutdown,
a surface that raises, an answer that cannot be parsed. That is v1
`face/approvals.py`'s design, kept verbatim, because every one of its failure
modes was found by something going wrong rather than by imagination.

What is new here, and why:

* **The whole command travels with the request.** v1 learned this the hard way
  when remote approval shipped: a card that names a tool teaches the owner to
  approve without reading, and a DM that summarises a command is a DM that
  authorises something the owner did not see. `ApprovalRequest.command` is the
  exact string, never a digest and never a summary.
* **`human_backed` is a property of the asker, not of the command.** It is the
  v1 invariant that nearly shipped broken (CLAUDE.md, *Command rules*): a
  background thread nobody is watching gets `DenyAll`, whose `human_backed` is
  False, and §6 layer 4 refuses to honour an ALLOW verdict through it. Auto
  approval is a convenience for a surface where the owner is present.
* **`always=True` mints an allowlist entry through v1 `entry_for`,** which
  raises `NotAllowlistable` on a compound or empty command. When it raises the
  approval still stands and no entry is written — v1's rule, because one click
  must not become a blanket grant per stem that happened to share the line.
* **The escape hatch never mints one at all** (§6.1): a command the reviewer
  refused is not a command to auto-approve next time. `ask()` callers that came
  from the hatch pass `allowlistable=False` and `resolve(always=True)` is then
  a plain ALLOW.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import math
import re
import secrets as _secrets
import threading
import time
from typing import Any, Callable

from jarvis import permissions, untrusted
from .model import utcnow
from .provider import Decision

LOG = logging.getLogger(__name__)

LOCAL_TIMEOUT_S = 120.0     # a card in front of the owner
REMOTE_TIMEOUT_S = 600.0    # a DM: you have to get your phone out (v1's 10 minutes)

_CODE_CHARS = "23456789abcdefghjkmnpqrstuvwxyz"   # no 0/o/1/l/i: it is typed back


def clean_line(text, cap: int = 120) -> str:
    """One display line from provider- or model-supplied text, `label()`'s
    rule generalised: every run of whitespace, control or format characters
    (newlines, NUL, zero-width and bidi overrides) becomes one space, backticks
    go, and the result is capped with "…". Markdown is left alone here — the
    HUD renders text literally — and escaped by the Discord renderer. The
    character set is `untrusted.UNSEEN_CATEGORIES`, shared with the folder
    check and fetched web text."""
    out, space = [], False
    for ch in str(text).replace("`", ""):
        if ch.isspace() or untrusted.unseen(ch):
            space = True
            continue
        if space and out:
            out.append(" ")
        space = False
        out.append(ch)
    line = "".join(out)
    return line if len(line) <= cap else line[:max(1, cap - 1)].rstrip() + "…"


def label(origin_id: str, name: str = "") -> str:
    """The attribution string, sanitized as v1 `tasks._label` does.

    `name` is model-chosen text that lands verbatim on an authorization
    surface, so it must not be able to fake a second line or break a DM's
    formatting: collapse whitespace, strip backticks, cap the length.
    """
    clean = re.sub(r"\s+", " ", str(name).replace("`", "")).strip()[:60]
    origin_id = re.sub(r"\s+", " ", str(origin_id).replace("`", "")).strip()[:60]
    if not origin_id:
        return f'"{clean}"' if clean else ""
    return f'task {origin_id} "{clean}"' if clean else f"task {origin_id}"


# v2 command tools, by the name each provider uses. See `v1_request` and
# `is_command_tool`. Claude's `Monitor` runs a shell command too (a watch
# whose stdout lines are events) and `PowerShell` is its Windows shell; both
# were missing until 2026-10-15, so a Monitor `cat .env` reached the reviewer
# with layer 1 never reading it.
V2_COMMAND_TOOLS = frozenset({
    "Bash", "BashOutput", "Monitor", "PowerShell",     # Claude
    "shell", "local_shell", "exec_command",             # Codex
    "run_command", "run_readonly",                      # fast path / jarvis-mcp
})
_V2_COMMAND_TOOLS = V2_COMMAND_TOOLS
# The argument keys a command line travels in, whatever the tool.
COMMAND_KEYS = ("command", "cmd", "script")


def _command_value(value) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)) and value and all(isinstance(v, str) for v in value):
        return " ".join(value)       # Codex hands an argv array
    return None


def is_command_tool(tool: str, args: dict | None) -> bool:
    """Whether this call is judged as a shell command (2026-10-15, fail
    closed): a known command tool, or **any** tool whose arguments carry a
    command line under a command key — a tool a newer CLI adds (Monitor was
    one) is a command tool from its first call, not a reviewer's guess. A
    non-text value under such a key is not a line and is not read as one."""
    if tool in V2_COMMAND_TOOLS:
        return True
    if not isinstance(args, dict):
        return False
    for key in COMMAND_KEYS:
        text = _command_value(args.get(key))
        if text is not None and text.strip():
            return True
    return False


def command_text(args: dict | None) -> str:
    """The command line in a tool's arguments, whatever the provider calls
    it. More than one command key with text (only an unknown tool could send
    that) is every one of them, one per line, so none goes unjudged."""
    if not isinstance(args, dict):
        return ""
    texts = [text for key in COMMAND_KEYS
             if (text := _command_value(args.get(key))) is not None and text.strip()]
    return "\n".join(dict.fromkeys(texts))


def v1_request(tool: str, args: dict) -> tuple[str, dict]:
    """This request as v1's allowlist understands it.

    **This is a hole closed, not a convenience.** v1 `entry_for` allowlists by
    *stem* for the tools it knows carry a command line — `run_command` and
    `run_readonly` — and allowlists the **whole tool** for everything else. The
    v2 providers call their command tool `Bash` or `shell`, which v1 has never
    heard of, so an ALWAYS on one `Bash: pnpm build` would have minted
    `{"tool": "Bash"}` — a prefix-less blanket grant over every future command,
    which is exactly the wildcard `entry_for` was changed in 2026-08-17 to
    refuse. Mapping the name back means the owner's yes becomes
    `{"tool": "run_command", "prefix": "pnpm"}`: one stem, every segment
    checked, as it has been since that round.

    The command is normalised too, because Codex hands an argv array where
    Claude hands a string and v1 reads `args["command"]` as text.
    """
    if not is_command_tool(tool, args):
        return tool, dict(args)
    # Any command-bearing tool (Monitor included) mints a stem entry, never
    # a whole-tool `{"tool": "Monitor"}` grant over every later command.
    return "run_command", {"command": command_text(args)}


@dataclass
class ApprovalRequest:
    """One question for the owner. `command` is the entire command, never a summary."""
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    command: str | None = None
    reason: str = ""
    layer: str = ""                     # which §6 layer produced the question
    thread_id: str | None = None
    task_id: str | None = None
    provider: str | None = None
    origin: str = ""                    # sanitized attribution (see `label`)
    allowlistable: bool = True          # False from the escape hatch (§6.1)
    # A line every surface shows *first*, above the tool and its arguments —
    # set only for a Codex approval that widens the sandbox ("SANDBOX
    # WIDENING: network on · write /home"), which is never allowlistable.
    headline: str = ""
    # Where on Discord to ask, when the asker is not a task (B2: `/project
    # new` asks in the channel it was typed in). Set by daemon code only — no
    # provider or tool builds a request with it — and still the DM if that
    # post fails. The answer then counts there and only there.
    discord_channel_id: str | None = None
    timeout_s: float | None = None      # set by the broker when asked; the card shows it
    req_id: str = ""
    code: str = ""
    asked_at: str = ""
    # Browser cards contain owner page/action details. Their public projection
    # is deliberately small; only the owner browser route serves details.
    hud_only: bool = False
    browser: dict[str, Any] | None = field(default=None, repr=False)
    max_wait_s: float | None = None
    _hud_private: bool = field(default=False, init=False, repr=False)

    @property
    def private(self) -> bool:
        # Publication and answer constraints cannot weaken while an ask waits.
        return bool(self.hud_only or self._hud_private)

    def to_json(self) -> dict:
        if self.private:
            return {
                "req_id": self.req_id, "code": self.code, "tool": self.tool,
                "layer": self.layer, "thread_id": self.thread_id,
                "asked_at": self.asked_at, "allowlistable": False,
                "timeout_s": self.timeout_s, "hud_only": True,
            }
        return {
            "req_id": self.req_id, "code": self.code, "tool": self.tool,
            "args": self.args, "command": self.command, "reason": self.reason,
            "layer": self.layer, "thread_id": self.thread_id,
            "task_id": self.task_id, "provider": self.provider,
            "origin": self.origin, "asked_at": self.asked_at,
            "allowlistable": self.allowlistable, "timeout_s": self.timeout_s,
            "headline": self.headline,
            "discord_channel_id": self.discord_channel_id,
        }

    def owner_json(self) -> dict:
        """Private detail projection; call only behind the owner HUD boundary."""
        if not self.private:
            return self.to_json()
        return {**self.to_json(), "args": self.args, "command": self.command,
                "reason": self.reason, "headline": self.headline,
                "browser": self.browser}


@dataclass
class _Pending:
    request: ApprovalRequest
    event: threading.Event = field(default_factory=threading.Event)
    decision: Decision | None = None
    resolution: str = ""
    announced: bool = False             # resolve() already told on_resolve
    hud_only: bool = False              # pinned when asked; never weakened later


class DenyAll:
    """The asker for a strict-profile task or an unbound context.

    `human_backed` is False, so §6 layer 4 will not honour an ALLOW verdict
    through it: a background thread cannot auto-run an allowlisted command
    purely because nobody is there to object.
    """

    human_backed = False

    def __init__(self, reason: str = "nobody is watching this thread"):
        self.reason = reason

    def ask(self, request: ApprovalRequest) -> Decision:
        LOG.info("Denying %s: %s", request.tool, self.reason)
        return Decision.DENY

    def pending(self) -> list[ApprovalRequest]:
        return []


class PendingApprovals:
    """Blocks the asking thread until a surface answers, or denies trying."""

    human_backed = True

    def __init__(
        self,
        *,
        remote: bool = False,
        timeout_s: float | None = None,
        on_request: Callable[[ApprovalRequest], None] | None = None,
        on_resolve: Callable[[ApprovalRequest, Decision, str], None] | None = None,
    ):
        # WP10 sets `remote=True` when a Discord surface is attached: a DM the
        # owner has to reach for their phone to answer needs v1's ten minutes,
        # and a card in front of them does not.
        self.remote = remote
        self.timeout_s = float(timeout_s) if timeout_s is not None else (
            REMOTE_TIMEOUT_S if remote else LOCAL_TIMEOUT_S)
        self._on_request = on_request
        self._on_resolve = on_resolve
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._closed = False
        self.decisions: list[dict[str, Any]] = []

    # -- the asking side ---------------------------------------------------

    def ask(self, request: ApprovalRequest, *,
            cancelled: Callable[[], bool] | None = None) -> Decision:
        """Block for a bounded owner answer; cancellation callbacks run unlocked."""
        request._hud_private = request.private
        wait_s = self.timeout_s
        if request.private:
            wait_s = min(wait_s, LOCAL_TIMEOUT_S)
        if request.max_wait_s is not None:
            value = request.max_wait_s
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0):
                return self._record(request, Decision.DENY, "invalid-timeout")
            wait_s = min(wait_s, value)
        def stopped():
            if cancelled is None:
                return False
            try:
                return bool(cancelled())
            except Exception:
                return True
        if stopped():
            return self._record(request, Decision.DENY, "cancelled")
        with self._lock:
            if self._closed:
                return self._record(request, Decision.DENY, "shutdown")
            request.req_id = self._new_id()
            request.code = self._new_code()
            request.asked_at = utcnow()
            item = _Pending(request, hud_only=request.private)
            self._pending[request.req_id] = item

        if self._on_request is not None:
            try:
                request.timeout_s = wait_s
                self._on_request(request)
            except Exception:       # a surface that cannot be told must not grant
                if request.private:
                    LOG.warning("Cannot announce private approval %s", request.req_id)
                else:
                    LOG.exception("Cannot announce approval %s", request.req_id)
                with self._lock:
                    self._pending.pop(request.req_id, None)
                return self._record(request, Decision.DENY, "nowhere-to-ask")

        cancelled_wait = False
        if cancelled is None:
            item.event.wait(wait_s)
        else:
            deadline = time.monotonic() + wait_s
            while not item.event.is_set():
                if stopped():
                    cancelled_wait = True
                    break
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                item.event.wait(min(0.1, left))
        with self._lock:
            self._pending.pop(request.req_id, None)
            if item.event.is_set():
                # Answered (resolve) or released (shutdown). Read under the
                # lock, so an answer that lands as the wait times out is the
                # one taken — never a timeout reported over an announced yes.
                decision, resolution, announced = item.decision, item.resolution, item.announced
            else:
                # Timed out. Setting the event means a resolve() racing this
                # finds the request gone and refuses, rather than answering it.
                item.event.set()
                decision, resolution, announced = None, "cancelled" if cancelled_wait else "timeout", False
        if decision is None:
            decision, resolution = Decision.DENY, resolution or "timeout"
        if not announced:
            # A timeout or a shutdown is an outcome every surface must hear:
            # without it a Discord post kept its buttons and the gateway kept
            # the request's channel forever (found in review, 2026-10-08).
            self._announce(request, decision, resolution)
        return self._record(request, decision, resolution)

    # -- the answering side ------------------------------------------------

    def resolve(self, req_id: str, decision: Decision | str, *, always: bool = False,
                hud: bool = False):
        """Resolve exactly once. A second resolve is refused.

        `always=True` also writes the persistent allowlist through v1
        `entry_for`. If that raises `NotAllowlistable` the approval still
        stands and no entry is minted — v1's rule, kept.
        """
        decision = Decision(decision)
        with self._lock:
            item = self._pending.get(req_id)
            if item is None or item.event.is_set():
                raise ValueError(f"unknown or already-resolved approval {req_id!r}")
            if item.hud_only and decision is Decision.ALLOW and hud is not True:
                raise ValueError("this approval requires an answer in the owner HUD")
            item.decision = decision
            item.resolution = decision.value
            entry = None
            if always and decision is Decision.ALLOW:
                if item.hud_only or not item.request.allowlistable:
                    item.resolution = "allow (no standing rule from this path)"
                else:
                    try:
                        entry = permissions.add_allow(*v1_request(item.request.tool,
                                                                 item.request.args))
                        item.resolution = "approved-always"
                    except permissions.NotAllowlistable as exc:
                        item.resolution = f"allow (not allowlistable: {exc})"
                    except OSError as exc:
                        item.resolution = f"allow (allowlist not written: {exc})"
            item.announced = True
            item.event.set()
            request, resolution = item.request, item.resolution
        self._announce(request, decision, resolution)
        return entry

    def resolve_code(self, code: str, decision: Decision | str, *, always: bool = False):
        """Answer by the 4-character code the Discord half shows (v1's rule).

        A bare code with several asks open would be a guess, so it must name
        exactly one open request or nothing is resolved.
        """
        with self._lock:
            matches = [p.request.req_id for p in self._pending.values()
                       if p.request.code == code and not p.event.is_set()]
        if len(matches) != 1:
            raise ValueError(f"no single open approval with code {code!r}")
        return self.resolve(matches[0], decision, always=always)

    def pending(self) -> list[ApprovalRequest]:
        with self._lock:
            return [p.request for p in self._pending.values() if not p.event.is_set()]

    def run_if_idle(self, commit: Callable[[], bool]) -> bool:
        """Linearize a short lifecycle commitment against approval admission.

        Internal coordination only: commit may validate/mark an in-memory
        Browser activity token. It must not perform IO, wait, publish, call
        other services or re-enter this broker. Lock order is broker ->
        Browser activity. Approval policy and answering remain unchanged.
        """
        with self._lock:
            if self._closed or any(not item.event.is_set() for item in self._pending.values()):
                return False
            return bool(commit())

    def owner_details(self, req_id: str) -> dict:
        """One still-pending private card. The HTTP caller enforces owner access."""
        with self._lock:
            item = self._pending.get(req_id)
            if item is None or item.event.is_set() or not item.hud_only:
                raise ValueError("no pending Browser approval")
            return item.request.owner_json()

    def shutdown(self) -> None:
        """Release every waiter with a denial. Idempotent."""
        with self._lock:
            self._closed = True
            items = list(self._pending.values())
            self._pending.clear()
        for item in items:
            if not item.event.is_set():
                item.decision = Decision.DENY
                item.resolution = "shutdown"
                item.event.set()

    # -- internals ---------------------------------------------------------

    def _new_id(self) -> str:
        # 72 bits, one-shot: an answer cannot be replayed onto a later card
        # than the one the owner read.
        return _secrets.token_urlsafe(9)

    def _new_code(self) -> str:
        taken = {p.request.code for p in self._pending.values()}
        for _ in range(64):
            code = "".join(_secrets.choice(_CODE_CHARS) for _ in range(4))
            if code not in taken:
                return code
        return _secrets.token_hex(2)

    def _announce(self, request: ApprovalRequest, decision: Decision, resolution: str) -> None:
        if self._on_resolve is None:
            return
        try:
            self._on_resolve(request, decision, resolution)
        except Exception:
            if request.private:
                LOG.warning("Cannot announce private resolution %s", request.req_id)
            else:
                LOG.exception("Cannot announce resolution of %s", request.req_id)

    def _record(self, request: ApprovalRequest, decision: Decision, resolution: str) -> Decision:
        self.decisions.append({
            "req_id": request.req_id, "tool": request.tool, "origin": "" if request.private else request.origin,
            "decision": decision.value, "resolution": resolution, "at": utcnow(),
        })
        return decision
