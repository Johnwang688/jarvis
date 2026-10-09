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
import re
import secrets as _secrets
import threading
import time
from typing import Any, Callable
import unicodedata

from jarvis import permissions
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
    HUD renders text literally — and escaped by the Discord renderer."""
    out, space = [], False
    for ch in str(text).replace("`", ""):
        if ch.isspace() or unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp"):
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


# v2 command tools, by the name each provider uses. See `v1_request`.
_V2_COMMAND_TOOLS = ("Bash", "BashOutput", "shell", "local_shell", "run_command")


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
    if tool not in _V2_COMMAND_TOOLS:
        return tool, dict(args)
    value = args.get("command", args.get("cmd", args.get("script", "")))
    if isinstance(value, (list, tuple)):
        value = " ".join(str(part) for part in value)
    return "run_command", {"command": str(value or "")}


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

    def to_json(self) -> dict:
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


@dataclass
class _Pending:
    request: ApprovalRequest
    event: threading.Event = field(default_factory=threading.Event)
    decision: Decision | None = None
    resolution: str = ""
    announced: bool = False             # resolve() already told on_resolve


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

    def ask(self, request: ApprovalRequest) -> Decision:
        """Block up to `timeout_s`. DENY on timeout, shutdown or any error."""
        with self._lock:
            if self._closed:
                return self._record(request, Decision.DENY, "shutdown")
            request.req_id = self._new_id()
            request.code = self._new_code()
            request.asked_at = utcnow()
            item = _Pending(request)
            self._pending[request.req_id] = item

        if self._on_request is not None:
            try:
                request.timeout_s = self.timeout_s
                self._on_request(request)
            except Exception:       # a surface that cannot be told must not grant
                LOG.exception("Cannot announce approval %s", request.req_id)
                with self._lock:
                    self._pending.pop(request.req_id, None)
                return self._record(request, Decision.DENY, "nowhere-to-ask")

        item.event.wait(self.timeout_s)
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
                decision, resolution, announced = None, "timeout", False
        if decision is None:
            decision, resolution = Decision.DENY, resolution or "timeout"
        if not announced:
            # A timeout or a shutdown is an outcome every surface must hear:
            # without it a Discord post kept its buttons and the gateway kept
            # the request's channel forever (found in review, 2026-10-08).
            self._announce(request, decision, resolution)
        return self._record(request, decision, resolution)

    # -- the answering side ------------------------------------------------

    def resolve(self, req_id: str, decision: Decision | str, *, always: bool = False):
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
            item.decision = decision
            item.resolution = decision.value
            entry = None
            if always and decision is Decision.ALLOW:
                if not item.request.allowlistable:
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
            LOG.exception("Cannot announce resolution of %s", request.req_id)

    def _record(self, request: ApprovalRequest, decision: Decision, resolution: str) -> Decision:
        self.decisions.append({
            "req_id": request.req_id, "tool": request.tool, "origin": request.origin,
            "decision": decision.value, "resolution": resolution, "at": utcnow(),
        })
        return decision
