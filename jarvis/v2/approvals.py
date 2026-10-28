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
import secrets as _secrets
import shlex
import threading
import time
from typing import Any, Callable

from jarvis import permissions, untrusted
from .model import utcnow
from .provider import Decision

LOG = logging.getLogger(__name__)

LOCAL_TIMEOUT_S = 120.0     # a card in front of the owner
REMOTE_TIMEOUT_S = 600.0    # a DM: you have to get your phone out (v1's 10 minutes)
# PH-10 (phone design): while a phone session was active in the last 24 h, a
# broker on the local wait waits the remote one, as with Discord attached.

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


def card_text(value, *, command: bool = True):
    """A card's command, reason or headline as shown and published: typed
    credentials withheld (`secrets.redact_command`; the owner's decision,
    2026-10-15), never a summary, and nothing withheld that runs. The
    request itself keeps the command as typed — that is what is judged and
    what an "Always" is minted from. A redaction that fails shows the text
    as it is: a card that hid code would be worse than one that shows a
    secret.

    Around `redact_command` (scrub hardening, 2026-10-25): the display
    scrub's credential shapes and labelled values (`redact.CardPass`), each
    only where the shell reads no structure and a labelled value not on a
    line that may run text; shown as `[withheld: <kind>]`. That pass failing
    leaves `redact_command`'s card. A command and a headline are judged as
    a command's (`command=True`: only the formats `credential_patterns`
    knows, never at a command's word or on a line that runs; review of #314,
    H1); a reason, which never runs, as text (`command=False`), as
    `log.jsonl` judges it."""
    if not isinstance(value, str) or not value:
        return value
    from . import redact
    extra = redact.CardPass()
    try:
        before = extra.hide(value, command=command)
    except Exception:  # noqa: BLE001
        LOG.warning("approval card shape pass failed")
        before = value
    try:
        from jarvis.tools import secrets as _v1_secrets
        return extra.show(_v1_secrets.redact_command(before), command=command)
    except Exception:  # noqa: BLE001
        LOG.warning("approval card redaction failed; shown as typed")
        return value


def card_args(args):
    """A card's arguments, each string as `card_text`; structure kept. A
    string under `command`/`cmd` is judged as a command; any other (a
    `Write`'s `content`, an `Edit`'s `new_string`) is text the shell never
    runs, so a private key's whole block and every shape in it go
    (`redact.card_spans`)."""
    from . import redact
    extra = redact.CardPass()
    top = not isinstance(args, dict)
    try:
        before = redact.map_strings(args, extra.hide, command=top)
    except Exception:  # noqa: BLE001
        LOG.warning("approval card shape pass failed")
        before = args
    try:
        from jarvis.tools import secrets as _v1_secrets
        return redact.map_strings(_v1_secrets.redact_value(before), extra.show, command=top)
    except Exception:  # noqa: BLE001
        LOG.warning("approval card redaction failed; shown as typed")
        return args


def label(origin_id: str, name: str = "") -> str:
    """The attribution string, sanitized as v1 `tasks._label` does.

    `name` is model-chosen text that lands verbatim on an authorization
    surface, so it must not be able to fake a second line, reorder or hide
    text (bidi overrides, zero-width characters) or break a DM's formatting.
    `clean_line` does all of that: whitespace and unseen characters collapse,
    backticks go, and the result is capped at 60 with "…".
    """
    clean = clean_line(name, cap=60)
    origin_id = clean_line(origin_id, cap=60)
    if not origin_id:
        return f'"{clean}"' if clean else ""
    return f'task {origin_id} "{clean}"' if clean else f"task {origin_id}"


# v2 command tools, by the name each provider uses: real shells. See
# `v1_request` and `is_command_tool`. Claude's `Monitor` runs a shell command
# too (a watch whose stdout lines are events) and `PowerShell` is its Windows
# shell; both were missing until 2026-10-15, so a Monitor `cat .env` reached
# the reviewer with layer 1 never reading it. Only these map to v1's
# `run_command` for the allowlist and rules' static ALLOW (layer 4).
V2_COMMAND_TOOLS = frozenset({
    "Bash", "BashOutput", "Monitor", "PowerShell",     # Claude
    "shell", "local_shell", "exec_command",             # Codex
    "run_command", "run_readonly",                      # fast path / jarvis-mcp
})
_V2_COMMAND_TOOLS = V2_COMMAND_TOOLS
# Tools whose `script` is code in another language, not a shell line (Claude's
# Workflow runs JavaScript). They keep main's treatment exactly: never read
# as a shell line, allowlisted as a whole tool (lead's decision on #211's
# review, F2; the owner may revisit).
CODE_TOOLS = frozenset({"Workflow", "AppifactRepl", "REPL"})
# The argument keys a command line travels in, whatever the tool, and the
# keys an unknown tool may carry its argv in beside it.
COMMAND_KEYS = ("command", "cmd", "script")
_ARGV_KEYS = ("args", "argv", "arguments")


def _command_value(value, join=shlex.join) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)) and value and all(isinstance(v, str) for v in value):
        # An argv array (an unknown or MCP tool may send one), quoted as the
        # one line it is: a plain join read `["bash", "-lc", "pkill -f
        # daemon2"]` as `bash -lc pkill -f daemon2`, whose inline source is
        # only `pkill`, and layer 1 never saw the kill (#211 bench, LOW).
        # `judged_commands` still judges the plain join too.
        return join(value)
    return None


def is_command_tool(tool: str, args: dict | None) -> bool:
    """Whether this call is *judged* as a shell command at layers 1 and 2
    (2026-10-15, fail closed): a known command tool, or **any** tool whose
    arguments carry a command line under a command key — a tool a newer CLI
    adds (Monitor was one) is judged from its first call. Not CODE_TOOLS.
    Being judged is not being trusted: only `is_shell_tool` reaches the
    allowlist and rules' static ALLOW."""
    if tool in CODE_TOOLS:
        return False
    if tool in V2_COMMAND_TOOLS:
        return True
    if not isinstance(args, dict):
        return False
    for key in COMMAND_KEYS:
        text = _command_value(args.get(key))
        if text is not None and text.strip():
            return True
    return False


def is_shell_tool(tool: str) -> bool:
    """A real shell, by name: the only tools whose line the allowlist's stems
    and rules' static ALLOW may answer for (#211 review, F1)."""
    return tool in V2_COMMAND_TOOLS


def is_unknown_command_tool(tool: str, args: dict | None) -> bool:
    """A command-bearing tool that is not a known shell (`mcp__x__exec`):
    judged at layers 1 and 2, never allowlisted, never Always."""
    return is_command_tool(tool, args) and not is_shell_tool(tool)


def unreadable_command(tool: str, args: dict | None) -> str | None:
    """#211 review, F3: "deny" when a known shell's command key is present
    but holds no text (empty, or a value that is not text); "ask" when an
    unknown tool's command key holds a value that is not text; else None."""
    if tool in CODE_TOOLS or not isinstance(args, dict):
        return None
    present = [key for key in COMMAND_KEYS if key in args]
    if not present:
        return None
    if tool in V2_COMMAND_TOOLS:
        return "deny" if not command_text(args).strip() else None
    if any(args[key] is not None and _command_value(args[key]) is None for key in present):
        return "ask"
    return None


def _fields(args: dict | None, join=shlex.join) -> list[str]:
    if not isinstance(args, dict):
        return []
    texts = [text for key in COMMAND_KEYS
             if (text := _command_value(args.get(key), join)) is not None and text.strip()]
    if len(texts) == 1:
        for key in _ARGV_KEYS:
            argv = args.get(key)
            if isinstance(argv, (list, tuple)) and argv and all(isinstance(a, str) for a in argv):
                texts[0] = texts[0] + " " + join(argv)
                break
    return list(dict.fromkeys(texts))


def command_fields(args: dict | None) -> list[str]:
    """Each command line in a tool's arguments, one per command key with
    text, duplicates dropped; an argv list beside a lone command
    (`{"command": "cat", "args": [".env"]}`) is part of it."""
    return _fields(args)


def command_text(args: dict | None) -> str:
    """The command line in a tool's arguments, whatever the provider calls
    it: what a card shows, the log keeps and an Always is minted from. More
    than one command key with text is every one of them, one per line. That
    joined text is **not** the only reading judged: an unclosed quote, a
    trailing backslash or a here-document in one field carries into the
    next, so a refused line in the second read as an `echo` argument (#211
    bench, MED). See `judged_commands`."""
    return "\n".join(command_fields(args))


def judged_commands(args: dict | None) -> tuple[str, ...]:
    """Every reading of a call's command the gate judges, so each must pass
    on its own and a DENY in any of them wins (#211 bench):

    * the joined text (`command_text`), and each field by itself when there
      is more than one, so nothing left open in one field hides the next;
    * the same with an argv array's words joined plainly, as before argv was
      quoted: the rules do not open `bash -c '…'` source for every refusal
      (`sudo` in it is asked, not refused), and a plain join had exposed it.
    """
    readings: list[str] = []
    for join in (shlex.join, " ".join):
        fields = _fields(args, join)
        readings.append("\n".join(fields))
        if len(fields) > 1:
            readings.extend(fields)
    return tuple(dict.fromkeys(readings))


def is_multi_command(args: dict | None) -> bool:
    """More than one command line in one call: no real shell sends that, so
    the allowlist and rules' static ALLOW never answer it and the permit's
    card offers no Always (#211 bench, MED)."""
    return len(command_fields(args)) > 1


def open_quoting(args: dict | None) -> str | None:
    """The first field of a call with more than one command line whose
    quoting does not close (an unclosed quote, a trailing backslash), else
    None. Which line such a field joins depends on how the tool combines
    its fields, so it cannot be judged: asked at layer 2 (#211 bench, MED)."""
    fields = command_fields(args)
    if len(fields) < 2:
        return None
    for text in fields:
        try:
            shlex.split(text)
        except ValueError:
            return text
    return None


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
    if not is_shell_tool(tool):
        # Not a shell: as on main, the whole tool. An unknown command-bearing
        # tool never reaches the allowlist (permit layer 4 skips it) and its
        # cards are never allowlistable, so no entry is read or written for
        # it here (#211 review, F1).
        return tool, dict(args)
    # A real shell (Monitor included) mints a stem entry, never a whole-tool
    # `{"tool": "Monitor"}` grant over every later command.
    return "run_command", {"command": command_text(args)}


@dataclass
class ApprovalRequest:
    """One question for the owner. `command` is the entire command, never a
    summary, as typed; `to_json` is what a card shows (`card_text`)."""
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
    # The native sub-agent whose call this is (SV-3): its sid, so the HUD can
    # link the card to the child's row. Set by `permissions.build_permit`
    # from a provider's `subagent_sid`; an id, never a word the model chose.
    subagent: str = ""
    # Display only (chat-to-chat C6): the relayed turn a card names,
    # {"title", "provider", "thread_id", "kind"} (`PermitContext.relayed`).
    # Decides nothing; kind "started": "turn started by a message from chat
    # ‹title› (Provider · id)", kind "running": "while a turn started by …
    # runs".
    relayed_from: dict | None = None
    # Quiet hours (QH, quiet_defer.py), set by daemon code only, never shown
    # on a card: the folder the gate judged the call in (a refspec-less push
    # is judged by it), part of a deferral's digest; whether an Approve and
    # retry may ever answer it (False for every ask that is asked each time);
    # and an ask the owner just started at the HUD, never deferred.
    cwd: str | None = None
    grantable: bool = True
    owner_initiated: bool = False
    _hud_private: bool = field(default=False, init=False, repr=False)
    _card: dict | None = field(default=None, init=False, repr=False, compare=False)
    # Quiet hours (QH, `quiet_defer.QuietDesk.settle`): set by the broker
    # only, to ("deferred", the agent's words) or ("granted", the log's
    # words) when the ask was settled with no card. Read by the permit.
    outcome: tuple | None = field(default=None, init=False, repr=False, compare=False)

    def card(self) -> dict:
        """The redacted projection a card shows (`card_text`/`card_args`),
        computed once per request: every publish and `GET /approvals`
        reuses it."""
        if self._card is None:
            self._card = {"args": card_args(self.args), "command": card_text(self.command),
                          "reason": card_text(self.reason, command=False),
                          "headline": card_text(self.headline)}
        return self._card

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
            "args": self.card()["args"], "command": self.card()["command"],
            "reason": self.card()["reason"],
            "layer": self.layer, "thread_id": self.thread_id,
            "task_id": self.task_id, "provider": self.provider,
            "origin": self.origin, "asked_at": self.asked_at,
            "allowlistable": self.allowlistable, "timeout_s": self.timeout_s,
            "headline": self.card()["headline"],
            "discord_channel_id": self.discord_channel_id,
            **({"subagent": self.subagent} if self.subagent else {}),
            **({"relayed_from": {k: clean_line(str(self.relayed_from.get(k) or ""), 80)
                                 for k in ("title", "provider", "thread_id", "kind")}}
               if isinstance(self.relayed_from, dict) and self.relayed_from else {}),
        }

    def owner_json(self) -> dict:
        """Private detail projection; call only behind the owner HUD boundary."""
        if not self.private:
            return self.to_json()
        return {**self.to_json(), **self.card(), "browser": self.browser}


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
        # PH-10: set by the daemon to "a phone session was seen in the last
        # 24 h" (`RemoteAuth.recently_active`). Read at each ask; an error
        # counts as no phone.
        self.phone_active: Callable[[], bool] | None = None
        # QH (quiet_defer.py): attached by daemon2's `main()` only
        # (`Daemon.attach_quiet_desk`). Its `settle` runs under the lock below
        # at each ask: the owner's single-use inbox grant for exactly this
        # call, or a deferral while the owner sleeps. None: every ask asks.
        self.quiet_desk = None
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
        wait_s = self.wait_s()
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
        desk = self.quiet_desk
        with self._lock:
            if self._closed:
                return self._record(request, Decision.DENY, "shutdown")
            request.req_id = self._new_id()
            request.code = self._new_code()
            request.asked_at = utcnow()
            # QH: settled here, under the lock, before any card exists: a
            # grant is taken once however many identical asks race for it.
            settled = self._settle_quietly(desk, request)
            if settled is None:
                item = _Pending(request, hud_only=request.private)
                self._pending[request.req_id] = item
        if settled is not None:
            # No card, no Discord post, no wait: the desk records and announces.
            request.outcome = settled
            if settled[0] == "granted":
                # The use is written before the call may run; if it cannot be,
                # the grant is not used (spent all the same in this process).
                if self._tell_desk(desk.granted, request) is not True:
                    request.outcome = ("deferred", "The owner's grant for this call could not be recorded, "
                                       "so it was not used and the call did not run. Do not retry it now.")
                    return self._record(request, Decision.DENY, "grant-unrecorded")
                return self._record(request, Decision.ALLOW, "granted from the inbox")
            self._tell_desk(desk.deferred, request)
            return self._record(request, Decision.DENY, "deferred")

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

    def wait_s(self) -> float:
        """How long the next ask waits (PH-10): the broker's own wait, or the
        remote one when the broker is on the local default and a phone
        session was active in the last 24 h."""
        wait = self.timeout_s
        if wait == LOCAL_TIMEOUT_S and self.phone_active is not None:
            try:
                if self.phone_active() is True:
                    wait = REMOTE_TIMEOUT_S
            except Exception:  # noqa: BLE001 — no phone: the local wait
                pass
        return wait

    def resolve(self, req_id: str, decision: Decision | str, *, always: bool = False,
                owner_verified: bool = False):
        """Resolve exactly once. A second resolve is refused.

        `always=True` also writes the persistent allowlist through v1
        `entry_for`. If that raises `NotAllowlistable` the approval still
        stands and no entry is minted — v1's rule, kept.

        `owner_verified=True` is HUD authority (phone design PH-7, O-3): it
        alone lets a HUD-only card be allowed. Exactly two kinds of caller
        pass True: the HUD's own answer routes (the HUD listener's owner test
        and its Host) and the phone's answer route after a fresh per-answer
        passkey assertion while every §4.1 check holds (`remote_answer`).
        Discord, tools and everything else leave it False
        (`tests/v2/remote_answer_check.py` greps the callers).
        """
        decision = Decision(decision)
        with self._lock:
            item = self._pending.get(req_id)
            if item is None or item.event.is_set():
                raise ValueError(f"unknown or already-resolved approval {req_id!r}")
            if item.hud_only and decision is Decision.ALLOW and owner_verified is not True:
                raise ValueError("this approval requires an answer in the owner HUD")
            item.decision = decision
            item.resolution = decision.value
            entry = None
            if always and decision is Decision.ALLOW:
                if (item.hud_only or not item.request.allowlistable
                        or is_unknown_command_tool(item.request.tool, item.request.args)):
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

    def always_rule(self, req_id: str) -> tuple[dict | None, str]:
        """A dry run of `resolve(always=True)` for one pending request (phone
        design PH-7: Always shows the rule it would write first): the entry
        it would write, or None, and the resolution it would record, by the
        same tests in the same order. Writes nothing. ValueError for a
        request that is not pending."""
        with self._lock:
            item = self._pending.get(req_id)
            if item is None or item.event.is_set():
                raise ValueError(f"unknown or already-resolved approval {req_id!r}")
            request, hud_only = item.request, item.hud_only
        if (hud_only or not request.allowlistable
                or is_unknown_command_tool(request.tool, request.args)):
            return None, "allow (no standing rule from this path)"
        try:
            return permissions.entry_for(*v1_request(request.tool, request.args)), "approved-always"
        except permissions.NotAllowlistable as exc:
            return None, f"allow (not allowlistable: {exc})"

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

    @staticmethod
    def _settle_quietly(desk, request) -> tuple | None:
        """QH: ("granted"|"deferred", words), or None to ask. Anything else,
        or a desk that raises, asks as usual; a grant never reaches a
        HUD-only card."""
        if desk is None:
            return None
        try:
            settled = desk.settle(request)
        except Exception:  # noqa: BLE001
            LOG.warning("Quiet-hours check failed; asking as usual")
            return None
        if (isinstance(settled, tuple) and len(settled) == 2 and isinstance(settled[1], str) and settled[1]
                and (settled[0] == "deferred" or (settled[0] == "granted" and not request.private))):
            return settled
        return None

    @staticmethod
    def _tell_desk(call, request):
        try:
            return call(request)
        except Exception:  # noqa: BLE001 — a deferral already stands; a grant then fails closed
            LOG.warning("Cannot record a quiet-hours outcome")
            return None

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
