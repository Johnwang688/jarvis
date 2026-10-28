"""ClaudeProvider — Claude Code behind the v2 provider interface (design §5.3).

The SDK (`claude-agent-sdk` 0.2.153) spawns a `claude` binary — the owner's
installed one, because `resolve_cli()` says so — and inherits its subscription
login, so a worker here is the owner's own Claude
Code running headless in a task worktree. Three things about that shape decide
almost every line below.

**The gate is a `PreToolUse` hook, not `can_use_tool`** (R2, answered by
`tests/spikes/r1_sdk_login.py`). Under `permission_mode="auto"` the CLI's
classifier settles every call and the permission callback is consulted for
*nothing*, while the hook fires for every tool use. So §6's five layers reach
Claude through the hook. Under `auto` an ALLOW returns `{}` rather than an
explicit allow decision: layer 3 *is* the classifier, and an explicit allow
skips it (the SDK says so, and it would also skip `can_use_tool`). Under `ask`
layer 3 is skipped by definition (§6.2), so there the same ALLOW is explicit.

**`allowed_tools` is never used for anything gated** (R1). Entries there are
auto-approved *before* any callback, which would silently unbuild the gate.
`Brief.allowed_tools` therefore maps to the SDK's `tools` option — which
restricts what exists rather than what runs unasked — and `allowed_tools` is
left empty on every request.

**The SDK is async and this interface is not.** Each handle owns a private
event loop on its own thread; `send()` runs the turn as a coroutine that pushes
`Event`s into a queue the generator drains synchronously — the fastpath
pattern, for the fastpath reason: `TEXT_DELTA` has to arrive *during* the model
call, not in a list at the end. The turn coroutine's `finally` always posts the
sentinel, so the generator cannot hang, and an SDK exception becomes
`ERROR{fatal: True}` and then stops.

**Which `claude` runs is chosen here, never left to the SDK** (2026-10-08).
With `cli_path` unset the SDK spawns the CLI bundled inside its wheel (2.1.273
in 0.2.153), which lags the owner's self-updating install and refuses newer
models outright ("does not support this model; version 2.1.280 or newer is
required"). `resolve_cli()` picks the owner's real install once per process,
and every `ClaudeAgentOptions` this module builds carries it. The version is
deliberately not pinned: the install updates itself, and that is the point.

A note on money: `ResultMessage.total_cost_usd` is reported even on a
subscription, where nothing is billed (R1: 0.28 for a three-tool turn). It is
an **equivalent** figure; what it means is the ledger's decision (§8.3), not
this provider's. It is also the CLI process's **running total** for the
conversation, not the turn's cost (2026-10-12: thread 447ff25e read $109.97
for $23.22), and a resumed CLI continues from the total its transcript saved.
So each result's USAGE carries what that result added (`running_cost.step`,
measured from the last figure seen on this conversation, a resume from the
total the thread saved), and the figure itself rides along as `cost_total`
for the daemon to save. Tokens need none of this: `usage` is per turn.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import queue
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ConversationResetMessage,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage as SdkUserMessage,
)

from ... import config
from ...tools import secrets
from ..model import PermissionProfile, ProviderName, Role, Thread
from .. import running_cost
from ..provider import (
    takes_keyword as _takes_keyword,
    Brief,
    BriefRefused,
    Decision,
    Event,
    EventKind,
    PermissionCallback,
    SessionHandle,
    SessionLost,
    SteerRefused,
    SubagentNotLive,
    SubagentStopDeclined,
    SubagentStopUnsupported,
    Usage,
    UserMessage,
)

# A floor, never a pin (owner, 2026-10-08): the installed CLI updates itself,
# so health accepts anything at or above CLAUDE_MIN — 2.2 and 3.x included —
# and only warns, once per process, past CLAUDE_VERIFIED. CLAUDE_MIN is the
# oldest CLI the default model (`claude-opus-5-5`) accepts — it answers older
# ones with "version 2.1.280 or newer is required" — which also puts the SDK's
# bundled 2.1.273 below the floor, so that fallback reads as unhealthy rather
# than healthy-but-failing. (The R1–R3 spikes and the classifier-denial text
# were verified on 2.1.273.) CLAUDE_VERIFIED is the newest version known to
# work end to end.
# `JARVIS_CLAUDE_STRICT=1` restores the old major.minor match.
CLAUDE_MIN = "2.1.280"
CLAUDE_VERIFIED = "2.1.295"

# The CLI kills a hook that does not answer in time and treats it as no
# decision, so a slow owner would read as "the gate did not fire". The broker's
# own remote timeout is 10 minutes (a Discord DM to a phone), so anything under
# that turns an owner who is merely walking to their desk into a silent
# fall-through. `HookMatcher.timeout` is in **seconds** and defaults to 60
# (verified in the installed SDK's `types.py`); the SDK itself applies no
# timeout of its own to the callback — it awaits it — so this number is the
# whole budget.
HOOK_TIMEOUT_S = 660.0

# How long to wait for connect/disconnect/interrupt control requests.
CONNECT_TIMEOUT_S = 120.0
CONTROL_TIMEOUT_S = 60.0

# Steering (2026-10-08, review of PR #22). When a turn's result arrives while
# one of its steers has not been seen to drain, the CLI either folded it in
# without saying so or will run it as a fresh turn. The turn waits for that
# fresh turn while the CLI shows any sign of life, ending after
# STEER_QUIET_S of silence or STEER_HOLD_MAX_S in all. Correctness does not
# rest on these numbers: a fresh turn that starts later still is recognised and
# absorbed by the next send (`_read`), never mistaken for that send's answer.
STEER_QUIET_S = 3.0
STEER_HOLD_MAX_S = 120.0
# After a result frame that may have been a leftover steer turn's rather than
# the message's own (one whose start the CLI did not echo), how long to wait
# for the message's own turn to show itself before taking that result as its.
LEFTOVER_WAIT_S = 10.0

# A conversation past the model's context window (2026-10-12). The CLI answers
# with a synthetic API error whose text starts with this, and a result with
# `is_error`. The provider then runs the CLI's own `/compact` once, and sends
# the message again only when nothing of it can have run (`_context_full`);
# COMPACT_TIMEOUT_S bounds the compaction (one on a 1M-token conversation took
# 53 s live), polled every INTERRUPT_POLL_S so the owner's Stop ends it at
# once. Otherwise the owner is told in one sentence what happened.
CONTEXT_FULL_PREFIX = "prompt is too long"
COMPACT_TIMEOUT_S = 600.0
INTERRUPT_POLL_S = 0.25
# Compacted, the message sent again, and it still did not fit.
CONTEXT_STILL_FULL = (
    "This conversation is still too long for the model's context window after "
    "compacting it; start a New thread to go on."
)
# Compacted, but nothing re-sent: some of the turn may already have run.
CONTEXT_COMPACTED_SEND_AGAIN = (
    "This conversation filled the model's context window partway through, so it "
    "was compacted; nothing was re-sent, because part of the work may already "
    "have run — send your message again to go on."
)
# The compaction did not finish (no compact boundary, an error, the time limit).
CONTEXT_NOT_COMPACTED = (
    "This conversation is too long for the model's context window and compacting "
    "it did not finish; send your message again to retry, or start a New thread."
)

# How much of a tool result goes into TOOL_FINISHED.summary. A surface renders
# this in a ticker; the transcript already holds the whole thing.
SUMMARY_CHARS = 200

# Kept only to enrich a fatal error. Never emitted on its own, always scrubbed.
STDERR_LINES = 40

# --- native sub-agents (SV-1, 2026-10-15) ------------------------------------
# Claude Code's Agent children, as CLI 2.1.295 shows them (SV-0 findings,
# docs/plans/2026-10-15-sv0-findings.md). A child's messages arrive whole —
# never as deltas — tagged with `parent_tool_use_id`, the id of the Agent call
# that spawned it; its lifecycle is the `task_*` system messages, keyed by
# `task_id`, which is also the `agent_id` its PreToolUse hooks carry. Each
# child is a `sid` ("cl:<spawn call id>"), and everything it says or does is a
# SUBAGENT_EVENT, never the parent's TEXT or TOOL_*: a child's prose is not
# the orchestrator's answer. Background is the CLI's default, so a child
# outlives its turn as a matter of course and is never stopped for it.
CHILD_LIMIT = 256            # children remembered per session (oldest finished out)
CHILD_TOOL_LIMIT = 1024      # their tool calls, for the results' names
CHILD_LABEL_CAP = 40
CHILD_UNNAMED = "unnamed"
CHILD_NAME_CHARS = 200       # the lifecycle's name, before the daemon cleans it
CHILD_RESULT_CHARS = 4000    # the CLI's closing summary of a child
CHILD_BRIEF_CHARS = 8000     # its brief (`task_started.prompt`), before the record caps it at 4000
# A label on an approval card: only these characters survive (anything else
# becomes "_"), so it cannot draw the card's own " · " parts or close its ‹ ›.
_CHILD_LABEL_UNSAFE = re.compile(r"[^A-Za-z0-9 _./-]+")
_SID_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
# task_updated.patch.status and task_notification.status, to a row's status.
_CHILD_TERMINAL = {"completed": "done", "failed": "failed", "killed": "stopped",
                   "stopped": "stopped"}
CHILD_LIVE = ("running", "backgrounded")

# The owner's Stop on one child (SV-4, 2026-10-15). `stop_task(task_id)` was
# shown offline to stop exactly one agent, foreground or background, with the
# orchestrator's turn and a sibling going on (SV-0 findings 2 and 7:
# `task_updated` `killed`, then `task_notification` `stopped`), on this CLI
# version; the owner enabled it on that evidence. Empty turns per-child Stop
# off. A floor, like CLAUDE_VERIFIED: a newer CLI keeps it, a CLI Jarvis has
# seen to be older does not. STOP_CONTROL_TIMEOUT_S bounds the wait for the
# CLI to take the request (a full SDK buffer could hold the answer back; SV-6
# drains it between turns only for a watched session); the stop itself is
# confirmed only by the CLI's own `killed`/`stopped`.
CLAUDE_STOP_VERIFIED = "2.1.295"
STOP_CONTROL_TIMEOUT_S = 10.0
OWNER_STOPPED = "stopped by the owner"

# --- between turns (SV-6, 2026-10-16) ----------------------------------------
# Between sends an idle reader keeps reading the CLI's stream, but only for a
# session the daemon watches (`watch`): a sub-agent's messages and the task
# messages go to the watcher at once; a CLI turn nobody sent (a finished
# background agent's notice, SV-0 finding 6) is *offered* to the daemon, which
# adopts it as a turn of its own (`adopt`); anything else main-thread is held,
# in order, for the next reader (`session.held`), which judges it by `_read`'s
# rules as before. The design: docs/plans/2026-10-13-subagent-view.md, "SV-6
# design". Every reader runs on the session's own loop and a turn parks the
# idle reader before it reads, so the stream has one reader at a time.
# HELD_LIMIT bounds what is held: past it a held stream event (a delta) is
# dropped; a whole message, a result or a task message never is.
HELD_LIMIT = 4096
# How long an adopted CLI turn waits for the session's turn slot, and how long
# a switch or a close waits for the idle reader to stop.
ADOPT_WAIT_S = 10.0
PARK_TIMEOUT_S = 10.0

# The exact text the CLI puts in a tool result when the auto-mode classifier
# refuses a call — §6.1's `reviewer_declined` trigger. Verified 2026-09-15
# against the installed binary (2.1.273), which holds it as a single template
# constant; the classifier's own words follow "Reason: ".
CLASSIFIER_DENIAL_PREFIX = (
    "Permission for this action was denied by the Claude Code auto mode classifier"
)
# Verified in CLI 2.1.295: PreToolUse permissionDecisionReason becomes
# tool_result.content verbatim (with hook context appended).
HOOK_DENIAL_PREFIX = "Jarvis denied "
HOOK_REFUSAL_PREFIX = "Jarvis refuses "
HOOK_DENIAL_MESSAGE = HOOK_DENIAL_PREFIX + "this call"
# The hook's reason for an allow the owner's Bypass switch made (`set_bypass`).
BYPASS_REASON = "the owner turned the auto-mode classifier off for this chat"
# What the switch covers: the shell and the file-editing tools, where the
# classifier's false refusals were. Every other tool (WebFetch, MCP servers,
# Monitor, ...) keeps the classifier with the switch on.
BYPASS_TOOLS = frozenset({"Bash", "Edit", "Write", "MultiEdit", "NotebookEdit"})
_REASON = re.compile(
    r"Reason:\s*(?P<reason>.+?)\.\s+If you have other tasks", re.DOTALL
)

# What `--effort` accepts (SDK `EffortLevel`). An off-ladder value is refused
# rather than passed through: the CLI would reject it at spawn, one turn later
# and nowhere near the brief that asked for it.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# Claude Code's own prompt, CLAUDE.md and skills all hang off the preset. A
# bare string prompt would replace them, which is how a worker loses the
# project's conventions without anything being logged.
SETTING_SOURCES = ["user", "project"]

# An **isolated** session (`start(..., isolated=True)`; only the Diff tab's
# quick review asks for one, 2026-10-15, review H1 of #219) reads no settings
# file at all: `--setting-sources=` empty, so no project or user
# `settings.json` (their hooks, which would run unsandboxed before any reply,
# `env`, `apiKeyHelper`, `enabledPlugins`), no CLAUDE.md as instructions — the
# CLI gates CLAUDE.md discovery on the same sources (2.1.295), and
# CLAUDE_CODE_DISABLE_CLAUDE_MDS=1 says so twice. The login is the OAuth
# credentials file, which is not a setting source. Its environment is the
# daemon's with every variable not in ISOLATED_ENV_KEEP (or a prefix in
# ISOLATED_ENV_KEEP_PREFIXES) blanked: the SDK can only add to the inherited
# environment, never remove from it, so each is set to "" — Jarvis's `.env`
# keys (OPENROUTER_API_KEY and the like) and ANTHROPIC_* included, so the CLI
# authenticates with the subscription login only.
ISOLATED_SETTING_SOURCES: list[str] = []
ISOLATED_ENV_KEEP = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TERM", "TZ", "TMPDIR",
    "TMP", "TEMP", "PWD", "CLAUDE_CONFIG_DIR", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS"})
ISOLATED_ENV_KEEP_PREFIXES = ("LC_", "XDG_", "WSL")


def isolated_env(environ=None) -> dict[str, str]:
    """The `env` an isolated session's CLI gets on top of the daemon's: every
    variable outside the keep list blanked, and CLAUDE.md discovery off."""
    environ = os.environ if environ is None else environ
    env = {key: "" for key in environ
           if key not in ISOLATED_ENV_KEEP and not key.startswith(ISOLATED_ENV_KEEP_PREFIXES)}
    env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] = "1"
    return env

# Anything that looks like a bearer credential, redacted before a string can
# reach an event. `secrets.scrub` covers the files Jarvis owns; the CLI's own
# OAuth bundle (`~/.claude/.credentials.json`) is not one of them, and this
# provider is the first thing in the codebase whose child process holds it.
# The shapes live in `redact.py` (shared with the sub-agent record's scrub,
# SV-2 review); a jarvis-mcp caller token is among them, since the CLI's
# stderr could quote its MCP config back at us.
from ..redact import JMCPISH as _JMCPISH, JWTISH as _JWTISH, REDACTED as _REDACTED, \
    TOKENISH as _TOKENISH  # noqa: E402


def _private_result(canonical: str) -> bool:
    """A jarvis tool whose result never reaches a summary (`PRIVATE_RESULT`)."""
    from ..session_tools import private_result
    return private_result(canonical)


def _safe(text: Any) -> str:
    """Every string this module puts in an event, a reason or an exception.

    Two layers, because they cover different things. `secrets.scrub` knows the
    credential files Jarvis owns and the values inside them. The regexes know
    the shape of a bearer token, which is what a child process's stderr or a
    model-written argument might carry from somewhere Jarvis has never heard
    of — including the CLI's own login.
    """
    out = secrets.scrub(str(text or ""))
    out = _TOKENISH.sub(_REDACTED, out)
    out = _JMCPISH.sub(_REDACTED, out)
    return _JWTISH.sub(_REDACTED, out)


# --- the MCP config carrier (plan 2026-10-10 §1.3) ---------------------------
#
# The SDK puts a dict `mcp_servers` on the CLI's command line as one JSON
# argument (`--mcp-config '<json>'`, env included), and a command line is
# world-readable (`ps`, /proc/<pid>/cmdline): a chat's jarvis-mcp caller token
# would show to anything that ran `ps aux`. So the servers go in a private
# file instead and the SDK is handed its path, which it passes as
# `--mcp-config <path>`. Written once per session, reused by `set_model`'s
# reconnect, deleted by `close()`; `Daemon.start` sweeps any a crash left.
#
# **One file per session, never per thread** (PR #35 review): a thread's old
# session can still be closing — the daemon drops a lost session under its
# lock and closes it outside — while the next send opens a new session on the
# same thread. With one `mcp.json` per thread, the old session's close deleted
# the new session's carrier. Each session writes `mcp-<nonce>.json` and only
# ever removes its own.

_THREAD_DIR = re.compile(r"[A-Za-z0-9_-]{1,64}")
# A session's carrier, and the pattern the start-up sweep removes (with the
# temporary `.mcp-*` a crash may leave mid-write).
MCP_CONFIG_NAME = re.compile(r"mcp-[0-9a-f]{32}\.json")


def mcp_config_dir(thread_id: str) -> Path:
    """`V2_DATA_DIR/claude/<thread_id>/`, where a thread's sessions keep
    their carriers."""
    if not isinstance(thread_id, str) or not _THREAD_DIR.fullmatch(thread_id):
        raise BriefRefused("invalid Claude thread id for an MCP config")
    return Path(config.V2_DATA_DIR) / "claude" / thread_id


def write_mcp_config(thread_id: str, servers: dict) -> Path:
    """The servers as `{"mcpServers": …}` in a file of this session's own
    (`mcp-<nonce>.json`), 0600 in 0700 directories, written aside and renamed
    into place; never through a symlink."""
    path = mcp_config_dir(thread_id) / f"mcp-{uuid.uuid4().hex}.json"
    for directory in (path.parent.parent, path.parent):
        if directory.is_symlink():
            raise BriefRefused("the Claude MCP config directory must not be a symlink")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
    if path.is_symlink():
        raise BriefRefused("the Claude MCP config must not be a symlink")
    data = json.dumps({"mcpServers": servers}, ensure_ascii=True, allow_nan=False)
    fd, tmp = tempfile.mkstemp(prefix=".mcp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def remove_mcp_config(path: Path | None) -> None:
    if path is None:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        LOG.warning("Cannot remove a Claude MCP config (%s)", type(exc).__name__)


# The seam the free tests replace: everything that would spawn a `claude`
# process goes through here, so a suite never needs one.
def _client_factory(options: ClaudeAgentOptions) -> Any:
    return ClaudeSDKClient(options=options)


# --- which claude -----------------------------------------------------------

LOG = logging.getLogger(__name__)

# Never a Windows binary or shim. `/mnt/<drive>` is the Windows side of WSL,
# where npm's `claude` is a shell script that launches the *Windows* CLI with a
# Windows home and a Windows login; these suffixes are its other spellings.
_FOREIGN_SUFFIXES = (".cmd", ".bat", ".ps1", ".exe")
_FOREIGN_ROOTS = ("/mnt",)  # a tuple so the free suite can point it at a temp dir


def _under_mnt(path: Path) -> bool:
    text = str(path)
    return any(text == root or text.startswith(root.rstrip("/") + "/") for root in _FOREIGN_ROOTS)


def _usable(path: Path) -> bool:
    """A real, executable `claude` on the Linux side, and nothing else."""
    try:
        for candidate in (path, path.resolve()):
            if candidate.name.lower().endswith(_FOREIGN_SUFFIXES) or _under_mnt(candidate):
                return False
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def find_cli(
    *,
    configured: str | None = None,
    path_env: str | None = None,
    home: Path | None = None,
) -> tuple[str | None, str]:
    """(path, source) for the `claude` to drive. Only looks at files.

    Order: an explicit `config.CLAUDE_CLI` (env `JARVIS_CLAUDE_CLI`) that is an
    executable file; the first usable `claude` on PATH (relative entries and
    anything under `/mnt/` are skipped); `~/.local/bin/claude`, because a
    systemd unit's PATH rarely has it; else `(None, "bundled")`, which leaves
    the SDK to its own bundled CLI. The three inputs default to the live
    config, PATH and home; a test passes its own.
    """
    configured = config.CLAUDE_CLI if configured is None else configured
    path_env = os.environ.get("PATH", "") if path_env is None else path_env
    home = Path.home() if home is None else home
    if configured:
        explicit = Path(configured).expanduser()
        if not explicit.is_absolute():
            # Checked here against the daemon's cwd, but the SDK spawns it with
            # `cwd=<task worktree>`: a relative path would run whatever `claude`
            # the worktree holds — model-writable, and outside the PreToolUse
            # gate. Refused rather than made absolute against an arbitrary cwd.
            LOG.warning("JARVIS_CLAUDE_CLI=%s is not an absolute path; ignoring it", configured)
        elif explicit.is_file() and os.access(explicit, os.X_OK):
            # Never `resolve()`d: a self-updating install is a symlink that the
            # updater re-points, and the link is what should be run.
            if any(_under_mnt(p) or p.name.lower().endswith(_FOREIGN_SUFFIXES)
                   for p in (explicit, explicit.resolve())):
                LOG.warning("JARVIS_CLAUDE_CLI=%s looks like a Windows binary or shim; "
                            "using it because it was set explicitly", configured)
            return str(explicit), "config"
        else:
            LOG.warning("JARVIS_CLAUDE_CLI=%s is not an executable file; ignoring it", configured)
    for entry in path_env.split(os.pathsep):
        if not entry or not os.path.isabs(entry):
            continue
        candidate = Path(entry) / "claude"
        if _usable(candidate):
            return str(candidate), "path"
    local = home / ".local" / "bin" / "claude"
    if _usable(local):
        return str(local), "local-bin"
    return None, "bundled"


def bundled_version() -> str | None:
    """The SDK's bundled CLI version, read from its own constant. No process."""
    try:
        from claude_agent_sdk._cli_version import __cli_version__

        return str(__cli_version__)
    except Exception:  # noqa: BLE001 — a private module may move; this feeds a log line
        return None


_cli_lock = threading.Lock()
_cli_resolved: tuple[str | None, str] | None = None
# realpath -> version. `~/.local/bin/claude` is a symlink into a versioned
# directory that the CLI's own updater re-points, so the key is the real file:
# an update is noticed at the next look, and nothing is re-probed otherwise.
_cli_versions: dict[str, str | None] = {}


# While nothing is found, look again this often, so installing Claude Code
# after the daemon started does not need a restart. A found CLI is kept for
# the life of the process.
RESOLVE_RETRY_S = 60.0
_cli_resolved_at = 0.0


def resolve_cli() -> str | None:
    """The `cli_path` every client in this module is built with.

    A found CLI is resolved once per process. None means the SDK's bundled
    CLI: one warning is logged, and the search is repeated at most every
    `RESOLVE_RETRY_S` until something turns up.
    """
    global _cli_resolved, _cli_resolved_at
    with _cli_lock:
        first = _cli_resolved is None
        retry = (not first and _cli_resolved[0] is None
                 and _clock() - _cli_resolved_at >= RESOLVE_RETRY_S)
        if first or retry:
            path, source = find_cli()
            _cli_resolved, _cli_resolved_at = (path, source), _clock()
            if path is None and first:
                version = bundled_version()
                LOG.warning(
                    "no installed claude found (JARVIS_CLAUDE_CLI, PATH, ~/.local/bin); "
                    "falling back to the SDK's bundled CLI%s, which may refuse newer models; "
                    "looking again every %d s",
                    f" {version}" if version else "", int(RESOLVE_RETRY_S),
                )
            elif path is not None:
                LOG.info("claude CLI: %s (from %s)", path, source)
        return _cli_resolved[0]


# realpath -> monotonic time of the last failed probe. A failure is never
# cached as an answer: a one-off timeout must not read as "broken" until the
# daemon restarts. It only holds off the next attempt for this long, so a
# genuinely broken binary is not re-spawned on every `/status`.
PROBE_RETRY_S = 60.0
_cli_failed: dict[str, float] = {}
_warned_newer = False


def _clock() -> float:
    """The probe backoff's clock. A seam, so the free suite can move time."""
    import time

    return time.monotonic()


def reset_cli_cache() -> None:
    """Forget the resolution, the probes and the warning. For tests."""
    global _cli_resolved, _warned_newer
    with _cli_lock:
        _cli_resolved = None
        _cli_versions.clear()
        _cli_failed.clear()
        _warned_newer = False


def _probe_version(path: str) -> str | None:
    """`<path> --version`: a success once per real binary, a failure retried
    after `PROBE_RETRY_S`."""
    key = os.path.realpath(path)
    with _cli_lock:
        if key in _cli_versions:
            return _cli_versions[key]
        failed_at = _cli_failed.get(key)
        if failed_at is not None and _clock() - failed_at < PROBE_RETRY_S:
            return None
    try:
        probe = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20)
        found = (probe.stdout or "").strip().split(" ")[0] if probe.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        found = ""
    with _cli_lock:
        if found:
            _cli_versions[key] = found
            _cli_failed.pop(key, None)
        else:
            _cli_failed[key] = _clock()
    return found or None


_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+.][0-9A-Za-z.+-]*)?$")


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    """`"2.1.295"` -> (2, 1, 295). None for anything that is not a version."""
    match = _VERSION.match((text or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None  # type: ignore[return-value]


def _cli_refusal(exc: BaseException) -> bool:
    """The SDK raises a bare Exception carrying the CLI's error control
    response (`_internal/query.py`): the CLI's refusal. Its own "Control
    request timeout" is not one (the CLI may still act on the request)."""
    return type(exc) is Exception and not _sdk_timeout(exc)


def _sdk_timeout(exc: BaseException) -> bool:
    return type(exc) is Exception and str(exc).startswith("Control request timeout")


def _stop_outcome(exc: BaseException | None) -> str:
    """How one Stop's answer settles the owner's mark, in time or late alike:
    none is `taken`; the SDK's own control timeout is `unknown` (the CLI may
    still act on it); anything else, the CLI's refusal or a request that was
    not delivered, is `refused`."""
    if exc is None:
        return "taken"
    return "unknown" if _sdk_timeout(exc) else "refused"


def _stop_settled(session: Any, child: Any, outcome: str) -> None:
    """One owner Stop on `child` has its answer (`taken`, `refused` or
    `unknown`). The owner's mark comes off only when this one was refused,
    no other Stop of it is still on its way and none was taken: whatever
    ends the child later is then not the owner's doing (review of #216's
    delta, LOW 1)."""
    with session.mutex:
        child.owner_stops = max(0, child.owner_stops - 1)
        if outcome == "taken":
            child.owner_stop_taken = True
        elif outcome == "refused" and not child.owner_stops and not child.owner_stop_taken:
            child.owner_stop = False


def _late_stop(session: Any, child: Any, sid: str, done: Any) -> None:
    """The late outcome of a Stop whose answer the route stopped waiting for,
    settled by the same rule as one in time (`_stop_outcome`)."""
    try:
        error = done.exception()
    except BaseException:  # noqa: BLE001 — cancelled with the loop
        _stop_settled(session, child, "unknown")
        return
    _stop_settled(session, child, _stop_outcome(error))
    if error is None:
        LOG.info("claude %s: the CLI took the stop of %s late", session.thread_id, sid)
    else:
        LOG.warning("claude %s: the stop of %s ended without an answer (%s)",
                    session.thread_id, sid, type(error).__name__)


def _known_cli_version() -> tuple[int, int, int] | None:
    """The version of the CLI in use, if Jarvis has already read it (no
    process is spawned here: a route asks this). None when unknown."""
    with _cli_lock:
        resolved = _cli_resolved
        path = resolved[0] if resolved else None
        found = _cli_versions.get(os.path.realpath(path)) if path else None
    if path is None and resolved is not None:
        found = bundled_version()
    return parse_version(found) if found else None


def _version_problem(found: str) -> str | None:
    """Why this CLI version is not acceptable, or None if it is.

    A floor, not a pin: anything at or above `CLAUDE_MIN` is healthy, 2.2 and
    3.x included, because the install updates itself and a health check that
    failed on every minor release would route Claude away for no reason. A
    version newer than `CLAUDE_VERIFIED` logs one warning per process. With
    `JARVIS_CLAUDE_STRICT=1`, the old rule returns: the major.minor must match
    the verified one.
    """
    global _warned_newer
    version = parse_version(found)
    if version is None:
        return f"could not read a version from `claude --version` (got {found!r})"
    minimum, verified = parse_version(CLAUDE_MIN), parse_version(CLAUDE_VERIFIED)
    if version < minimum:
        return (f"claude {found} is older than {CLAUDE_MIN}, the oldest version "
                "Jarvis was verified with; update Claude Code")
    if config.CLAUDE_STRICT and version[:2] != verified[:2]:
        return (f"claude {found}: JARVIS_CLAUDE_STRICT=1 requires "
                f"{verified[0]}.{verified[1]}.x (verified {CLAUDE_VERIFIED})")
    if version > verified:
        with _cli_lock:
            first, _warned_newer = not _warned_newer, True
        if first:
            LOG.warning("claude %s is newer than %s, the last version Jarvis was "
                        "verified with; running it anyway", found, CLAUDE_VERIFIED)
    return None


def cli_info() -> dict:
    """`{path, version, source}` of the CLI in use, as `/status` reports it.

    `path` is None when the SDK's bundled CLI is in use; its version then comes
    from the SDK's constant rather than a process.
    """
    path = resolve_cli()
    source = _cli_resolved[1] if _cli_resolved else "bundled"
    version = _probe_version(path) if path else bundled_version()
    return {"path": path, "version": version, "source": source}


def usage_agent_version() -> str:
    """The version the usage meter's User-Agent claims: the CLI actually in
    use, or `CLAUDE_VERIFIED` when that cannot be read as a version. Never
    raises — a User-Agent is not worth failing the meter over."""
    try:
        version = cli_info().get("version")
    except Exception:  # noqa: BLE001
        version = None
    return version if parse_version(version) else CLAUDE_VERIFIED


# --- health -----------------------------------------------------------------


def _credentials_path() -> Path:
    """The CLI's credential bundle. An env override exists for the tests only.

    Read for exactly one number. Never opened anywhere else in this module.
    """
    override = os.environ.get("JARVIS_CLAUDE_CREDENTIALS")
    return Path(override) if override else Path.home() / ".claude" / ".credentials.json"


def _login_expiry_ms() -> int | None:
    """When the login stops working, in epoch milliseconds, or None if unknown.

    **Which field, and why it matters.** The bundle carries two: `expiresAt`
    (the access token, refreshed silently roughly hourly) and
    `refreshTokenExpiresAt` (the grant itself). Judging the login by the access
    token would report "expired" for most of every hour on a perfectly healthy
    machine, and a health check that cries wolf is one nobody reads — the same
    failure as an allowlist that silently stops matching. So the refresh
    token's expiry is the login's expiry when it is present, and the access
    token's is the fallback.

    Nothing but these integers is ever read out of the file, and no value from
    it is returned, logged or put in an exception.
    """
    path = _credentials_path()
    try:
        raw = path.read_text()
    except OSError:
        return None
    try:
        oauth = json.loads(raw).get("claudeAiOauth") or {}
    except ValueError:
        return None
    for key in ("refreshTokenExpiresAt", "expiresAt"):
        value = oauth.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
    return None


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


# --- session state ----------------------------------------------------------


@dataclass
class _Pending:
    """One outstanding permission question, answerable out of band."""

    ready: threading.Event = field(default_factory=threading.Event)
    value: Decision | None = None


@dataclass
class _Child:
    """One native sub-agent, as this session knows it (SV-1)."""

    sid: str
    status: str = "running"
    task_id: str | None = None
    parent_sid: str | None = None
    depth: int = 1
    background: bool = False
    name: str = ""
    kind: str = ""
    last_tool: str = ""
    tokens: int | None = None
    tool_uses: int | None = None
    duration_ms: int | None = None
    stop_reason: str | None = None
    # A status Jarvis inferred (a foreground child still live when its turn
    # ended), which the CLI's own word, arriving later, replaces.
    inferred: bool = False
    # The owner asked the CLI to stop it (SV-4): its `stopped` then says so.
    owner_stop: bool = False
    # The owner's Stops still on their way, and whether the CLI took one:
    # a refusal clears `owner_stop` only when neither (`_stop_settled`).
    owner_stops: int = 0
    owner_stop_taken: bool = False


@dataclass
class _Session:
    client: Any
    brief: Brief
    permit: PermissionCallback
    thread_id: str
    loop: asyncio.AbstractEventLoop
    runner: threading.Thread
    session_id: str | None = None
    handle: SessionHandle | None = None
    # One turn at a time per conversation: two concurrent sends would
    # interleave two turns into one transcript.
    sending: threading.Lock = field(default_factory=threading.Lock)
    events: queue.Queue | None = None
    pending: dict[str, _Pending] = field(default_factory=dict)
    mutex: threading.RLock = field(default_factory=threading.RLock)
    interrupted: bool = False
    closed: bool = False
    tools_seen: dict[str, tuple[str, dict]] = field(default_factory=dict)
    denied_tools: set[str] = field(default_factory=set)
    denial_without_id: bool = False
    # What this handle's results added (the per-result deltas, summed).
    usage: Usage = field(default_factory=Usage)
    # The CLI's running cost total as the last result on this conversation
    # left it: what the next result's figure is measured from. 0.0 for a new
    # session; on a resume, the total the thread saved (the CLI continues from
    # what its transcript saved); None when that is unknown. Kept across a
    # model switch, whose new process resumes the same conversation.
    cost_seen: float | None = 0.0
    stderr: deque = field(default_factory=lambda: deque(maxlen=STDERR_LINES))
    # Whether the CLI holds this session on disk yet — true after a resume or
    # once a turn has produced a result. Decides how `set_model` reconnects.
    resumable: bool = False
    # Owner steering (2026-10-08). `accepting` is true while the turn's own
    # CLI turn is under way and can still take a steer. `steering` maps the
    # uuid of every steer written to the CLI and not yet seen to drain to the
    # turn (`generation`) that wrote it; it outlives that turn, so a steer the
    # CLI runs late is recognised by the next one. `prompt_echo` is whether
    # this CLI echoes the message that starts a turn (seen once is enough):
    # without it nothing can be told apart, and the provider behaves as if
    # every steer folded. `pump` is the one reader of the client's stream.
    accepting: bool = False
    steering: dict = field(default_factory=dict)
    generation: int = 0
    prompt_echo: bool = False
    pump: Any = None
    # Unseen steers of a turn the owner stopped. If the CLI still runs one
    # as a late turn, it is interrupted and its words dropped (review of PR
    # #22, round 2): stop means stop, even across sends.
    stopped: set = field(default_factory=set)
    # The private file the brief's MCP servers are carried in (plan
    # 2026-10-10 §1.3), or None when the brief has none.
    mcp_config: Path | None = None
    # No settings file, no CLAUDE.md, a scrubbed environment (`start(...,
    # isolated=True)`, the quick review's). Kept across a model switch.
    isolated: bool = False
    # The owner's Bypass switch (2026-10-21, `set_bypass`): while on, a call
    # Jarvis's own gate allowed gets an explicit allow under AUTO instead of
    # `{}`, so the CLI's auto-mode classifier does not judge it. Never stored:
    # a reopened or resumed session starts with it off. Kept across a model
    # switch, which keeps this object. It counts only inside a turn the
    # owner's own message started (`owner_turn`, set by `_run`, cleared when
    # `send` ends): a relayed or reply turn from another chat, a turn the CLI
    # starts itself (`adopt`) and a background sub-agent still working after
    # the turn keep the classifier. And only for BYPASS_TOOLS.
    bypass: bool = False
    owner_turn: bool = False
    # Native sub-agents (SV-1). They outlive turns (background is the CLI's
    # default), so none of this is cleared when a turn starts: `children` by
    # sid, oldest first; `tasks` maps a task id (= a hook's `agent_id`) to its
    # sid; `child_tools` names the children's calls for their results; and
    # `child_denied` holds their calls the hook refused, whose results may
    # come a turn later.
    children: OrderedDict = field(default_factory=OrderedDict)
    tasks: dict = field(default_factory=dict)
    child_tools: OrderedDict = field(default_factory=OrderedDict)
    child_denied: OrderedDict = field(default_factory=OrderedDict)
    # Between turns (SV-6). `watch` is the daemon's feed while it watches
    # this session (`ClaudeProvider.watch`), else None. `idle` is the idle
    # reader's task and `reading` the turn's task that holds the stream, or
    # None (both touched only on the session's loop). `held` is what a reader read and
    # left, in order, for the next one; `offer` is the origin of a CLI turn
    # the idle reader holds for the daemon to adopt, or None.
    watch: Any = None
    idle: Any = None
    reading: Any = None
    held: deque = field(default_factory=deque)
    offer: str | None = None
    held_dropped: int = 0

    def emit(self, kind: EventKind, **data: Any) -> None:
        """Put an event on the turn's queue. Silent outside a turn by design.

        `events is None` means no send is reading: the turn's consumer already
        stopped, or nothing runs but a background sub-agent, whose hook can
        fire between turns (SV-0 finding 6). Dropping is right, blocking is
        not: the gate still asks `permit`, and the broker's card is its own.
        A gate question asked between turns goes to the daemon's watcher when
        it watches (SV-6), for the audit log only: nothing reads it as an
        answer, and the decision is `permit`'s alone.
        """
        events = self.events
        if events is not None:
            events.put(Event(kind, self.thread_id, data))
            return
        watch = self.watch
        if watch is not None and kind in (EventKind.APPROVAL_REQUESTED, EventKind.APPROVAL_RESOLVED):
            watch.put(Event(kind, self.thread_id, data))


# --- the provider -----------------------------------------------------------


class ClaudeProvider:
    """Claude Code as a v2 provider. See the module docstring."""

    name = ProviderName.CLAUDE

    # -- health

    def cli_info(self) -> dict:
        """`{path, version, source}` of the CLI this provider spawns. The daemon
        reads this for `/status` and its start-up log."""
        return cli_info()

    def health(self) -> tuple[bool, str]:
        """(ok, reason). Binary, version floor, login. Never spends a token.

        The login check reads one integer out of the credential bundle and
        nothing else — see `_login_expiry_ms`. A bundle with no expiry field at
        all is reported as *unknown* rather than refused: a machine
        authenticating some other way (an `ANTHROPIC_API_KEY`, a managed
        keychain) has no bundle to read, and refusing to run because a file
        this provider does not own has changed shape would be a worse failure
        than the one it is guarding against.

        The binary is `resolve_cli()`'s, the same one every session spawns, and
        its `--version` runs once per real file (see `_probe_version`), not on
        every `/status`; a failed probe is retried after `PROBE_RETRY_S`. The
        version is judged against a floor, never a pin (`_version_problem`).
        """
        info = cli_info()
        found = info["version"]
        if info["path"] is None:
            if not found:
                return False, "no claude CLI found (JARVIS_CLAUDE_CLI, PATH, ~/.local/bin)"
            label = f"{found} (SDK bundled)"
        elif not found:
            return False, (f"`{info['path']} --version` failed; "
                           f"retrying in {int(PROBE_RETRY_S)} s")
        else:
            label = found
        problem = _version_problem(found)
        if problem is not None:
            return False, problem + (" (SDK bundled)" if info["path"] is None else "")
        expiry = _login_expiry_ms()
        if expiry is None:
            if not _credentials_path().exists():
                return False, f"claude {label}: no login found; run `claude login`"
            return True, f"claude {label}, login expiry unknown"
        if expiry <= _now_ms():
            return False, f"claude {label}: login expired; run `claude login`"
        return True, f"claude {label}, subscription login"

    # -- lifecycle

    def start(self, thread: Thread, brief: Brief, permit: PermissionCallback, *,
              isolated: bool = False) -> SessionHandle:
        """`isolated`: no settings file, no CLAUDE.md, a scrubbed environment
        (see ISOLATED_SETTING_SOURCES). Only the Diff tab's quick review sets
        it; every other session is unchanged."""
        return self._open(thread, brief, permit, resume=False, isolated=isolated)

    def resume(self, thread: Thread, brief: Brief, permit: PermissionCallback) -> SessionHandle:
        if not thread.provider_session_id:
            raise BriefRefused("Claude resume needs the thread's provider_session_id")
        return self._open(thread, brief, permit, resume=True)

    def _open(
        self, thread: Thread, brief: Brief, permit: PermissionCallback, *, resume: bool,
        isolated: bool = False,
    ) -> SessionHandle:
        session_id = thread.provider_session_id if resume else str(uuid.uuid4())
        loop = asyncio.new_event_loop()
        runner = threading.Thread(
            target=_run_loop, args=(loop,), name=f"jarvis-claude-{thread.id}", daemon=True
        )
        runner.start()
        session = _Session(
            client=None,
            brief=brief,
            permit=permit,
            thread_id=thread.id,
            loop=loop,
            runner=runner,
            session_id=session_id,
            resumable=resume,
            cost_seen=(running_cost.figure(getattr(thread, "provider_cost_total", None))
                       if resume else 0.0),
            isolated=isolated is True,
        )
        try:
            if brief.mcp_servers:
                session.mcp_config = write_mcp_config(thread.id, dict(brief.mcp_servers))
            options = self._options(brief, session, resume=resume, session_id=session_id)
            session.client = _client_factory(options)
            _await(loop, session.client.connect(), CONNECT_TIMEOUT_S)
        except BaseException as exc:  # noqa: BLE001 — a failed start must not leak a thread
            _stop_loop(loop, runner)
            remove_mcp_config(session.mcp_config)
            if isinstance(exc, BriefRefused):
                raise BriefRefused(_safe(exc)) from None
            raise BriefRefused(f"claude session failed to start: {_safe(exc)}") from None
        handle = SessionHandle(
            thread_id=thread.id,
            provider=self.name,
            provider_session_id=session_id,
            native=session,
        )
        session.handle = handle
        return handle

    def _options(
        self, brief: Brief, session: _Session, *, resume: bool, session_id: str | None
    ) -> ClaudeAgentOptions:
        """The Brief, mapped onto the SDK. Refuses rather than narrows (§5.1)."""
        if brief.profile is PermissionProfile.STRICT:
            raise BriefRefused(
                "the strict profile is firm-style confinement (native tools off, "
                "no network, deny-all); Claude Code has no such mode — route "
                "strict tasks to the Codex provider (design §6.2)"
            )
        if brief.effort is not None and brief.effort not in EFFORT_LEVELS:
            raise BriefRefused(
                f"effort {brief.effort!r} is not one of {', '.join(EFFORT_LEVELS)}"
            )
        if brief.mcp_servers and session.mcp_config is None:
            raise BriefRefused("the brief's MCP servers have no config file")
        auto = brief.profile is PermissionProfile.AUTO
        kwargs: dict[str, Any] = {
            "cwd": brief.cwd,
            # The preset, never a bare string: a string *replaces* Claude
            # Code's own prompt, and with it CLAUDE.md and the skills listing.
            "system_prompt": {
                "type": "preset",
                "preset": "claude_code",
                **({"append": brief.system_append} if brief.system_append else {}),
            },
            "setting_sources": list(ISOLATED_SETTING_SOURCES if session.isolated else SETTING_SOURCES),
            "permission_mode": "auto" if auto else "default",
            "include_partial_messages": True,
            "strict_mcp_config": True,
            # A path, never a dict: the SDK puts a dict on the command line
            # as JSON, env and caller token included (plan 2026-10-10 §1.3).
            # The file is the brief's servers, written by `_open`.
            "mcp_servers": str(session.mcp_config) if session.mcp_config is not None else {},
            # R1: an entry here is auto-approved before any callback runs.
            # Nothing gated may ever appear in it, and everything is gated.
            "allowed_tools": [],
            "hooks": {
                "PreToolUse": [
                    HookMatcher(
                        matcher=None,  # every tool, not a name pattern
                        hooks=[self._pre_tool_hook(session)],
                        timeout=HOOK_TIMEOUT_S,
                    )
                ]
            },
            "stderr": session.stderr.append,
            # The CLI echoes each stdin user message back when it drains into
            # a turn, under the uuid we gave it. That echo is how a steer is
            # known to have been taken in (`_read`), which is what keeps one
            # turn's events from leaking into the next.
            "extra_args": {"replay-user-messages": None},
        }
        cli_path = resolve_cli()
        if session.isolated:
            kwargs["env"] = isolated_env()
        if brief.origin_tag is not None:
            kwargs["env"] = {**kwargs.get("env", {}), "JARVIS_ORIGIN_TAG": brief.origin_tag}
        if cli_path is not None:
            # The owner's own install, never the SDK's bundled copy, which
            # lags it and refuses newer models. None leaves the SDK to its
            # bundled CLI — the last resort `resolve_cli` already warned of.
            kwargs["cli_path"] = cli_path
        if brief.model:
            kwargs["model"] = brief.model
        if brief.effort:
            kwargs["effort"] = brief.effort
        if brief.max_turns is not None:
            kwargs["max_turns"] = brief.max_turns
        if brief.allowed_tools is not None:
            # `tools` restricts what the model *has*; `allowed_tools` would
            # restrict what runs unasked. A role brief means the former.
            kwargs["tools"] = list(brief.allowed_tools)
        if not auto:
            # The SDK-level backstop for the cases the CLI's own rules send to
            # a prompt. The hook settles almost everything first; this exists
            # so a path that reaches the prompt still reaches the owner.
            kwargs["can_use_tool"] = self._can_use_tool(session)
        if resume:
            kwargs["resume"] = session_id
        else:
            # Minting the id is what lets `start()` return a handle that can
            # already be resumed: the CLI does not announce a session id until
            # the first turn (verified against 2.1.273 — no init message
            # arrives at connect, and `get_server_info()` carries no id).
            kwargs["session_id"] = session_id
        return ClaudeAgentOptions(**kwargs)

    # -- the gate

    def _pre_tool_hook(self, session: _Session):
        """§6's five layers, as the one thing that sees every tool call.

        `{}` on ALLOW under `auto` is the load-bearing detail: it is *not* an
        allow decision, so the classifier (layer 3) still runs. An explicit
        allow would skip it — and would also skip `can_use_tool`, per the SDK.
        Under `ask` layer 3 is skipped by design, so ALLOW is explicit there.

        Two exceptions, both for a Jarvis session tool on this session's
        jarvis server (`session_tools`, plan 2026-10-10 §3.4/§4.1): one marked
        `foreground_only` is denied outright when a sub-agent calls it (the
        hook input carries `agent_id`), without asking `permit`; and one
        that `permit` delegates gets an explicit allow under both profiles.
        This does not approve execution: the daemon bridge asks once for each
        dangerous call. Nondangerous tools retain named always-ask checks.
        """

        async def pre_tool(input_data, tool_use_id, context):  # noqa: ANN001
            data = input_data or {}
            name = str(data.get("tool_name") or "")
            args = data.get("tool_input")
            args = dict(args) if isinstance(args, dict) else {}
            agent_id = data.get("agent_id")
            # A Jarvis session tool (`mcp__jarvis__<tool>`, plan 2026-10-10).
            from .. import session_tools
            jarvis = session_tools.claude_session_tool(session.brief, name)
            if jarvis is not None and jarvis.foreground_only and data.get("agent_id"):
                _note_denied(session, tool_use_id, agent_id)
                # A sub-agent's call (the hook input names its agent): refused
                # without asking `permit`, so no one is ever asked about it.
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            f"{HOOK_REFUSAL_PREFIX}{jarvis.name} for a sub-agent: it runs only "
                            "in the conversation itself. Do not retry it from a sub-agent."
                        ),
                    }
                }
            if jarvis is not None and jarvis.owner_gated:
                from ..browser.drive import admission_for_tool, OFF
                if jarvis.name != "hud_browser_act" or not admission_for_tool():
                    _note_denied(session, tool_use_id, agent_id)
                    return {"hookSpecificOutput": {
                        "hookEventName": "PreToolUse", "permissionDecision": "deny",
                        "permissionDecisionReason": HOOK_REFUSAL_PREFIX + OFF}}
            # A sub-agent's call is decided exactly as the orchestrator's
            # would be; the card only says whose it is (SV-1).
            # The hook input's `cwd` is the shell's folder, from the CLI and
            # not the model: where a push with no refspec is judged.
            shell_cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else None
            decision = await self._decide(session, name, args, agent_id=agent_id,
                                          shell_cwd=shell_cwd,
                                          tool_use_id=tool_use_id if isinstance(tool_use_id, str) else None)
            if decision is Decision.ALLOW and jarvis is not None:
                # Explicit under both profiles (D-5): under ASK it pre-empts the
                # CLI's own prompt, under AUTO it skips the classifier, whose
                # job here the daemon does on every call. Never "approved":
                # nobody approved anything.
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                        "permissionDecisionReason": ("a read-only Jarvis tool" if jarvis.read_only
                                                     else "the daemon checks this Jarvis tool"),
                    }
                }
            if decision is Decision.ALLOW and session.brief.profile is PermissionProfile.AUTO:
                if not (session.bypass and session.owner_turn and name in BYPASS_TOOLS):
                    return {}
                # The owner's Bypass switch: Jarvis's gate (layers 1, 2, 4 and
                # the owner's cards) has already allowed this call, and the
                # owner has said the classifier is not to judge it.
                _log_bypass(session, name, args)
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                        "permissionDecisionReason": BYPASS_REASON,
                    }
                }
            if decision is Decision.ALLOW:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                        "permissionDecisionReason": "approved by Jarvis",
                    }
                }
            _note_denied(session, tool_use_id, agent_id)
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    # The model reads this, so it says what happened rather
                    # than only that something did.
                    "permissionDecisionReason": _safe(
                        f"{HOOK_DENIAL_PREFIX}{name}. The owner's rules or the owner "
                        "refused this call; do not retry it or work around it."
                    ),
                }
            }

        return pre_tool

    def _can_use_tool(self, session: _Session):
        async def can_use_tool(tool_name, input_data, context):  # noqa: ANN001
            args = dict(input_data) if isinstance(input_data, dict) else {}
            agent_id = getattr(context, "agent_id", None)
            # The CLI asking again about a call the owner just approved on a
            # card (Claude AUTO: the hook's `{}` lets the classifier run, and
            # it may ask too): answered from that yes, never a second card —
            # only for the same tool_use_id, tool and identical arguments,
            # once and briefly (`permissions._OwnerYes`).
            tool_use_id = getattr(context, "tool_use_id", None)
            recall = getattr(session.permit, "recall", None)
            if isinstance(tool_use_id, str) and tool_use_id and callable(recall):
                try:
                    if recall(tool_use_id, str(tool_name or ""), dict(args)) is True:
                        return PermissionResultAllow()
                except Exception:  # noqa: BLE001 — a failing recall decides nothing
                    pass
            decision = await self._decide(session, str(tool_name or ""), args, agent_id=agent_id)
            if decision is Decision.ALLOW:
                return PermissionResultAllow()
            _note_denied(session, getattr(context, "tool_use_id", None), agent_id)
            return PermissionResultDeny(message=_safe(HOOK_DENIAL_MESSAGE))

        return can_use_tool

    async def _decide(self, session: _Session, name: str, args: dict, *,
                      agent_id: Any = None, shell_cwd: str | None = None,
                      tool_use_id: str | None = None) -> Decision:
        """Ask `permit`, announcing the question so a stalled turn is visible.

        A call from a sub-agent (`agent_id`, from the hook input or the
        permission context) is asked about exactly as the orchestrator's
        would be. Only the card changes: `permit` is told the child's label
        (`subagent`, when it takes the keyword), and the logged question
        names its sid. Nothing here can make a child's answer looser — or
        stricter — than its parent's (SV-1, the plan's Q7).

        `permit` may block for minutes while a human is asked, and it is a
        *synchronous* callable, so it runs on a worker thread — blocking this
        session's event loop would stop the CLI's stdout being read and make
        `interrupt()` unanswerable for the whole wait.

        An out-of-band `answer()` races it and wins, which is the one place
        this provider goes further than its Codex sibling: the abandoned
        `permit` call keeps its thread until it returns on its own (Python
        cannot cancel it), but the turn is released.
        """
        req_id = uuid.uuid4().hex[:12]
        pending = _Pending()
        with session.mutex:
            session.pending[req_id] = pending
        private_act = name in ("hud_browser_act", "mcp__jarvis__hud_browser_act")
        sid, label = self._asker(session, agent_id)
        session.emit(
            EventKind.APPROVAL_REQUESTED,
            req_id=req_id,
            tool=name,
            args={} if private_act else args,
            command=None if private_act else _command(args),
            **({"subagent": sid} if sid else {}),
        )
        try:
            extra = {k: v for k, v in (("shell_cwd", shell_cwd), ("tool_use_id", tool_use_id))
                     if v is not None}
            decision = await (self._race(session, pending, name, args, **extra) if label is None
                              else self._race(session, pending, name, args, label, sid, **extra))
        finally:
            with session.mutex:
                session.pending.pop(req_id, None)
        session.emit(EventKind.APPROVAL_RESOLVED, req_id=req_id, decision=decision.value)
        return decision

    @staticmethod
    def _asker(session: _Session, agent_id: Any) -> tuple[str | None, str | None]:
        """(sid, card label) of the sub-agent a call came from, or (None,
        None) for the orchestrator's own. The hook's `agent_id` is the
        child's `task_id`, whose `task_started` always comes first (SV-0
        finding 3); one not seen yet is still a sub-agent's, unnamed."""
        if not agent_id:
            return None, None
        with session.mutex:
            sid = session.tasks.get(str(agent_id))
            child = session.children.get(sid) if sid else None
            name = child.name if child is not None else None
        return sid, child_label(name)

    async def _race(
        self, session: _Session, pending: _Pending, name: str, args: dict,
        label: str | None = None, sid: str | None = None,
        shell_cwd: str | None = None, tool_use_id: str | None = None,
    ) -> Decision:
        def ask() -> Decision:
            try:
                # Each keyword only to a callback that takes it: one that does
                # not would raise, and a raise denies.
                extra: dict = {}
                if label is not None and _takes_subagent(session.permit):
                    extra["subagent"] = label
                    # The child's sid too, for the card's link to its row
                    # (SV-3), when the callback takes it: like the label, it
                    # decides nothing.
                    if sid and _takes_keyword(session.permit, "subagent_sid"):
                        extra["subagent_sid"] = sid
                if shell_cwd is not None and _takes_keyword(session.permit, "shell_cwd"):
                    extra["shell_cwd"] = shell_cwd
                if tool_use_id is not None and _takes_keyword(session.permit, "tool_use_id"):
                    extra["tool_use_id"] = tool_use_id
                return Decision(session.permit(name, dict(args), session.brief, **extra))
            except Exception:  # noqa: BLE001 — an unreadable answer is a denial
                return Decision.DENY

        async def answered() -> Decision:
            # Polled rather than waited on in a thread: a thread parked on an
            # answer that never comes is a thread the interpreter joins at
            # exit, which turns "the owner never replied" into "the daemon
            # will not shut down".
            while not pending.ready.is_set():
                await asyncio.sleep(0.05)
            return pending.value or Decision.DENY

        asked = asyncio.ensure_future(_in_thread(session.loop, ask))
        waiter = asyncio.ensure_future(answered())
        try:
            done, _ = await asyncio.wait({asked, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not waiter.done():
                waiter.cancel()
        if pending.ready.is_set() and pending.value is not None:
            return pending.value
        if asked in done:
            try:
                return asked.result()
            except Exception:  # noqa: BLE001
                return Decision.DENY
        # The owner answered out of band and `permit` is still blocked. Python
        # cannot cancel it, so its thread is left to finish on its own — it is
        # a daemon, so it holds nothing up. (WP4 asked for a cancellable
        # request context; this is the half of it a provider can build.)
        return Decision.DENY

    def answer(self, h: SessionHandle, req_id: str, decision: Decision | str) -> None:
        """Withdraw a permission this session is blocked on, out of band.

        Only a DENY. A yes comes from `permit` — the broker, whose card only
        the owner answers (BR-0) — never from here: `POST /threads/<id>/answer`
        reaches this method from any client of the API listener, and an ALLOW
        here used to beat a card the owner had not answered. The Codex
        provider has always refused it the same way.
        """
        session = _native(h)
        with session.mutex:
            pending = session.pending.get(req_id)
            if pending is None or pending.ready.is_set():
                raise ValueError(f"Unknown or resolved Claude request id: {req_id}")
            decision = Decision(decision)
            if decision is not Decision.DENY:
                raise ValueError("A Claude tool use is decided by the approval broker; "
                                 "answer() can only deny it")
            pending.value = decision
            pending.ready.set()

    # -- the turn

    def send(self, h: SessionHandle, message: UserMessage) -> Iterator[Event]:
        session = h.native
        if session is None or session.closed:
            yield Event(
                EventKind.ERROR, h.thread_id, {"message": "session closed", "fatal": True}
            )
            return
        if not session.sending.acquire(blocking=False):
            # Not fatal: a second send refused is not the session failing, and
            # a fatal error makes the daemon drop and close the session — the
            # one whose turn is still running. The daemon never sends twice on
            # one handle; `steer` is how a message reaches a running turn.
            yield Event(
                EventKind.ERROR,
                h.thread_id,
                {"message": "a turn is already running on this thread", "fatal": False},
            )
            return
        try:
            yield from self._run(h, session, message)
        finally:
            session.events = None
            with session.mutex:
                session.accepting = False
                session.owner_turn = False
            session.sending.release()

    def _run(self, h: SessionHandle, session: _Session, message: UserMessage) -> Iterator[Event]:
        events: queue.Queue = queue.Queue()
        session.events = events
        session.interrupted = False
        session.tools_seen.clear()
        session.denied_tools.clear()
        session.denial_without_id = False
        with session.mutex:
            session.accepting = False
            # Whether the owner's Bypass switch may count in this turn: only
            # the owner's own message (HUD or Discord), never another chat's,
            # and never the owner's answer to a decision (inbox B1, review of
            # PR #309): that turn is the agent's to run on its own judgement.
            # Nor any message from the phone (via "phone"): a decision
            # answered there (P-4) or words typed into the chat there (P-5).
            # The phone carries no desk authority (design §11 question 2).
            session.owner_turn = (getattr(message, "origin", None) == "owner"
                                  and getattr(message, "via", None) not in ("inbox", "phone"))
            session.generation += 1
            # A steer an earlier turn wrote and never saw drain may still be
            # waiting in the CLI: then this message goes in at priority
            # `later`, which the CLI never folds into a running turn, so it
            # starts a turn of its own after whatever runs first.
            behind = self._leftover_possible(session, session.generation)
        prompt_uid = str(uuid.uuid4())
        done = object()

        async def turn() -> None:
            try:
                # The idle reader stops before anything is written or read
                # (SV-6): from here this turn is the stream's one reader, and
                # whatever the idle reader held it reads first.
                await self._take_stream(session)
                session.offer = None
                await session.client.query(_prompt(message, prompt_uid, later=behind))
                held, full, resend = await self._read(session, events, prompt_uid)
                if full:
                    held = await self._context_full(session, events, message, held, resend)
                for event in self._end_foreground(session):
                    events.put(event)
                if held is not None:
                    events.put(held)
            except BaseException as exc:  # noqa: BLE001 — reported, never swallowed
                events.put(
                    Event(
                        EventKind.ERROR,
                        session.thread_id,
                        {"message": _fatal_message(session, exc), "fatal": True},
                    )
                )
            finally:
                with session.mutex:
                    session.accepting = False
                self._give_stream(session)
                # The generator must never hang, whatever happened above.
                events.put(done)

        # Scheduled before TURN_STARTED is handed out, so the message reaches
        # the CLI whatever the consumer does next.
        future = asyncio.run_coroutine_threadsafe(turn(), session.loop)
        finished = False
        try:
            yield Event(EventKind.TURN_STARTED, h.thread_id)
            while True:
                item = events.get()
                if item is done:
                    break
                if item.kind in (EventKind.TURN_FINISHED, EventKind.ERROR):
                    finished = True
                yield item
        finally:
            if not finished and not future.done():
                # A consumer that abandons the iterator must not leave a model
                # editing files in the worktree.
                self.interrupt(h)
            future.cancel()

    # -- steering (2026-10-08)

    def steer(self, h: SessionHandle, message: UserMessage) -> None:
        """Write `message` into the running turn, as Claude Code does with a
        message typed while it works.

        It goes to the CLI's stdin as a user message with our uuid and
        priority `next`: the CLI queues it and folds it into the turn at its
        next boundary ("The user sent a new message while you were working")
        — never inside a tool call, never as an answer to the PreToolUse hook
        the turn may be blocked in. A turn that ends before that boundary has
        the CLI run it as a fresh turn instead, which `_read` keeps inside
        this turn (or, if it starts late, the next send absorbs). Accepted
        only once the turn's own CLI turn has visibly begun, so a steer can
        never reach the CLI ahead of the message it steers; refused (the
        daemon queues it) otherwise, or when the write fails.
        """
        session = h.native
        if session is None or session.closed:
            raise SteerRefused("session closed", before_write=True)
        uid = str(uuid.uuid4())
        payload = {
            "type": "user",
            "message": {"role": "user", "content": _content(message)},
            "parent_tool_use_id": None,
            "uuid": uid,
            "priority": "next",
        }

        async def one() -> Any:
            yield payload

        with session.mutex:
            if not session.accepting:
                raise SteerRefused("no turn of this message's is under way", before_write=True)
            # Registered before the write, so a result frame read while the
            # write is in flight already knows a steer is on its way.
            session.steering[uid] = session.generation
        try:
            _await(session.loop, session.client.query(one()), CONTROL_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 — the turn goes on; the daemon queues it
            with session.mutex:
                session.steering.pop(uid, None)
            raise SteerRefused(f"claude did not take the message: {_safe(exc)}") from None

    @staticmethod
    def _leftover_possible(session: _Session, generation: int) -> bool:
        """Could a CLI turn for an earlier turn's steer still come? Only when
        this CLI is known to echo the messages that start turns — without
        that, nothing could tell such a turn from the message's own."""
        return session.prompt_echo and any(g < generation for g in session.steering.values())

    async def _read(
        self, session: _Session, events: queue.Queue, prompt_uid: str
    ) -> tuple[Event | None, bool, bool]:
        """Read this message's turn off the CLI, steers included. -> the
        TURN_FINISHED this send hands back (None if the stream ended first),
        whether that result said the conversation is past the model's
        context window (`_context_full`), and whether the message may then be
        sent again: only if the overflowing CLI turn was the message's own and
        used no tool and took in no steer.

        The CLI runs turns one after another, and with `--replay-user-messages`
        echoes, under our uuid, each stdin message as it drains: the one that
        starts a turn first, before anything that turn says. So every CLI turn
        has an owner, and this tracks it:

        - **the message's own** — its echo starts the turn (`started`); its
          result frame is the result this send is waiting for;
        - **a steer of this turn's** that the CLI ran as a fresh turn because
          the turn ended before it could fold it — read as part of this turn;
        - **a leftover**: a steer an earlier turn wrote whose fresh turn
          started too late for that turn to see. It is read here, its words
          reach the owner, and its result does not end this turn — so a late
          steer never shifts every later send by one (review of PR #22). A
          leftover of a turn the owner stopped is interrupted as it starts
          and its words dropped: stop means stop (round 2);
        - **nobody's**: once this CLI is known to echo, a CLI turn with no
          echo before the message's own (a background task's notice) is read,
          but its result is never taken for this message's (round 2);
        - **injected**, known whatever the CLI is known to do: a CLI turn the
          CLI itself says somebody else started — the user message it replays
          and the turn's result carry an `origin` whose kind is not "human"
          (a background task's notice, a channel or peer message). Read, and
          its result never ends this turn (2026-10-12: a resumed CLI replays
          a queued task notice as a turn of its own before the message's, and
          on a fresh session that empty result used to be taken as the
          message's answer — the owner saw nothing until the next send).

        When this turn's result arrives with one of its steers not yet seen to
        drain, the turn holds while the CLI shows any sign of life, for its
        fresh turn: STEER_QUIET_S of silence or STEER_HOLD_MAX_S in all end
        the hold, and the steer then counts as folded in silently — or, if its
        turn is merely late, as the next send's leftover. A CLI that never
        echoes leaves nothing to tell apart: every turn is the message's own,
        every unseen steer is taken as folded, as before.

        Reads go through the session's pump, one message per request, never
        cancelled mid-read: a hold that ends leaves its read outstanding for
        the next turn, and nothing the CLI said is ever lost. What the idle
        reader held between turns (SV-6) is read first, in order, and judged
        by these same rules.
        """
        loop = asyncio.get_running_loop()
        generation = session.generation
        started = False          # the message's own CLI turn has begun
        guessed = False          # ... taken from an unechoed CLI turn, not seen
        owner = None             # who opened the CLI turn under way (None: none is)
        held = None              # the TURN_FINISHED this send hands back
        held_full = False        # ... whether it said the context is full
        held_resend = False      # ... and whether the message may be sent again
        candidate = None         # a result that was a leftover's, or this message's
        candidate_full = False
        candidate_until = None
        hold_began = hold_quiet = None
        restarted = False
        turn_full = False        # the CLI turn under way said "Prompt is too long"
        turn_work = False        # ... or used a tool, or took in a steer
        while True:
            timeout = None
            if hold_began is not None:
                timeout = max(0.0, min(hold_quiet, hold_began + STEER_HOLD_MAX_S) - loop.time())
            elif candidate_until is not None:
                timeout = max(0.0, candidate_until - loop.time())
            try:
                item = await self._next(session, timeout)
            except asyncio.TimeoutError:
                if hold_began is None and candidate is not None:
                    # The message's own start never showed: the CLI did not
                    # echo it, so the result taken for a leftover's was its.
                    held, held_full, held_resend = candidate, candidate_full, False
                break
            if item is _END:
                session.pump = None
                if not restarted:
                    # A reader whose stream ended is replaced once (a test
                    # double ends with its script); a dead CLI ends again.
                    restarted = True
                    continue
                break
            if isinstance(item, _Raised):
                session.pump = None
                raise item.exc
            restarted = False
            msg = item
            if hold_began is not None:
                hold_quiet = loop.time() + STEER_QUIET_S       # any frame is life
            uid = str(msg.uuid).lower() if isinstance(msg, SdkUserMessage) and msg.uuid else None
            if uid is not None and uid == prompt_uid:
                started, owner, candidate, candidate_until = True, "prompt", None, None
                guessed = False
                hold_began = None
                with session.mutex:
                    session.prompt_echo = True
                    # Every `next` steer drains before a message that starts a
                    # turn: an earlier one still unseen now was folded silently.
                    for other, written in list(session.steering.items()):
                        if written < generation:
                            del session.steering[other]
                            session.stopped.discard(other)
                    session.accepting = True
                await self._stop_if_interrupted(session)
                continue
            if uid is not None:
                with session.mutex:
                    written = session.steering.pop(uid, None)
                if written is not None:
                    with session.mutex:
                        was_stopped = uid in session.stopped
                        session.stopped.discard(uid)
                    if owner is None:
                        # Not folded: the steer starts a CLI turn of its own.
                        owner = "steer" if written == generation else "leftover"
                        hold_began = None
                        if was_stopped:
                            # The owner stopped the turn that sent it, and the
                            # CLI ran it late anyway: stopped again, unheard.
                            owner = "stopped"
                            with contextlib.suppress(Exception):
                                await session.client.interrupt()
                        else:
                            await self._stop_if_interrupted(session)
                    else:
                        turn_work = True           # folded into the CLI turn under way
                    continue
            if (owner is None and isinstance(msg, SdkUserMessage)
                    and _injected(getattr(msg, "origin", None))):
                # The CLI replays the message that starts a turn somebody else
                # started (a background task's notice) with its origin: that
                # CLI turn is never this message's, whatever this CLI is known
                # to echo. One injected mid-turn is part of the turn under way.
                owner = "injected"
                hold_began = None
                await self._stop_if_interrupted(session)
                continue
            if isinstance(msg, AssistantMessage):
                if any(isinstance(b, ToolUseBlock) for b in msg.content):
                    turn_work = True               # a sub-agent's tools count too
                if getattr(msg, "parent_tool_use_id", None) is None and _context_full_message(msg):
                    turn_full = True               # never a sub-agent's
            if (owner is None and isinstance(msg, (AssistantMessage, StreamEvent))
                    and not getattr(msg, "parent_tool_use_id", None)):
                # A sub-agent's message (tagged) starts no CLI turn: a
                # background child talks between turns as a matter of course.
                owner = "unknown"                  # a turn whose start was not echoed
                hold_began = None
                # Once this CLI is known to echo the message that starts a
                # turn, an unechoed turn before this message's echo is someone
                # else's: a background task's notice, a late steer (review of
                # PR #22, round 2). Only a CLI that never echoes starts here.
                if (not started and not session.prompt_echo
                        and not self._leftover_possible(session, generation)):
                    started = guessed = True
                    with session.mutex:
                        session.accepting = True
                await self._stop_if_interrupted(session)
            if owner == "stopped" and not isinstance(msg, ResultMessage):
                # A stopped steer's late turn: its words never reach the owner
                # (what it did — a tool, its cost — still does).
                for event in self._translate(session, msg):
                    if event.kind not in (EventKind.TEXT, EventKind.TEXT_DELTA, EventKind.THINKING):
                        events.put(event)
                continue
            finished = self._take(session, events, msg)
            if finished is None:
                continue
            ended, owner = owner, None
            full = _context_full_result(msg, said=turn_full)
            with session.mutex:
                # A steer of this turn written and not seen to drain may have
                # been folded in silently: work of its own, as one echoed is.
                work = turn_work or any(g == generation for g in session.steering.values())
            turn_full = turn_work = False
            kind = _injected(getattr(msg, "origin", None))
            if ended in (None, "unknown") and kind is not None:
                # The result says so even when nothing before it did (a
                # transcript-only notice the CLI replays nothing of).
                ended = "injected"
            if ended == "injected":
                LOG.info("claude %s: read a CLI turn of origin %s; not this message's",
                         session.thread_id, _safe(kind or "?")[:40])
                if guessed:
                    # It was taken for the message's own start on a CLI not
                    # yet known to echo: it was not, so that start is to come.
                    started = guessed = False
                    with session.mutex:
                        session.accepting = False
            if ended in ("leftover", "stopped", "injected"):
                # An earlier turn's steer, or a turn somebody else started,
                # never ends this message's turn.
                if held is None:
                    continue                       # the message's own turn is still to come
                # It ran in this turn's hold: the result stays the message's,
                # and the hold goes on while a steer of this turn is unseen.
                with session.mutex:
                    pending = any(g == generation for g in session.steering.values())
                if not pending:
                    break
                hold_began = loop.time()
                hold_quiet = hold_began + STEER_QUIET_S
                continue
            if not started:
                if session.prompt_echo or self._leftover_possible(session, generation):
                    candidate, candidate_until = finished, loop.time() + LEFTOVER_WAIT_S
                    candidate_full = full
                    continue
                started = True                     # nothing else could have run
            # Sent again only from the message's own CLI turn, and only if it
            # did nothing a second run would repeat (review of PR #57: a
            # `git push` ran, then the context filled, and the push was sent
            # again; a steer's own turn overflowed, and the answered message
            # was sent again in its place).
            held, held_full, held_resend = finished, full, ended == "prompt" and not work
            with session.mutex:
                session.accepting = False
                pending = any(g == generation for g in session.steering.values())
            if not pending:
                break
            hold_began = loop.time()
            hold_quiet = hold_began + STEER_QUIET_S
        with session.mutex:
            session.accepting = False
            if not session.prompt_echo:
                session.steering.clear()           # nothing to recognise them by later
            elif session.interrupted:
                session.stopped.update(u for u, g in session.steering.items() if g == generation)
        full = held_full and held is not None
        return held, full, full and held_resend

    async def _context_full(
        self, session: _Session, events: queue.Queue, message: UserMessage,
        held: Event | None, resend: bool,
    ) -> Event | None:
        """The message's result said the conversation no longer fits the
        model's context window ("Prompt is too long"). It is not a reply: run
        the CLI's own `/compact` once. Send the message again only when
        `resend` says the overflowing CLI turn was the message's own and did
        nothing a second run would repeat (no tool, no steer); otherwise, and
        when compacting fails or does not help, say what happened in one
        sentence and end the turn as an error. Stop means stop: the turn ends
        as interrupted, at once. The session is kept — the CLI's conversation
        is the same on a resume. -> the TURN_FINISHED to hand back."""
        if session.interrupted:
            return _as_interrupted(held)
        LOG.info("claude %s: the conversation is past the context window; compacting once",
                 session.thread_id)
        compacted = await self._compact(session, events)
        if session.interrupted:
            return _as_interrupted(held)
        if not compacted:
            note = CONTEXT_NOT_COMPACTED
        elif not resend:
            LOG.info("claude %s: compacted; the message is not sent again "
                     "(its turn may already have done work)", session.thread_id)
            note = CONTEXT_COMPACTED_SEND_AGAIN
        else:
            uid = str(uuid.uuid4())
            with session.mutex:
                behind = self._leftover_possible(session, session.generation)
            await session.client.query(_prompt(message, uid, later=behind))
            again, full, _ = await self._read(session, events, uid)
            if not full:
                return again
            held = again if again is not None else held
            if session.interrupted:
                return _as_interrupted(held)
            LOG.info("claude %s: still past the context window after compacting",
                     session.thread_id)
            note = CONTEXT_STILL_FULL
        events.put(Event(EventKind.ERROR, session.thread_id, {"message": note, "fatal": False}))
        return held

    async def _compact(self, session: _Session, events: queue.Queue,
                       why: list | None = None) -> bool:
        """Run the CLI's `/compact` (the stream-json form the Agent SDK
        documents) and read it to its result. -> whether a compact boundary
        came back. Shown as one tool call, so the wait is visible; bounded by
        COMPACT_TIMEOUT_S, after which the compaction is interrupted; and the
        owner's Stop is looked for every INTERRUPT_POLL_S, so it ends the wait
        at once (`interrupt()` has already told the CLI). A read left waiting
        stays the pump's, for the next turn. `why`, when given, gets one
        sentence saying why it did not compact (not for a Stop)."""
        uid = str(uuid.uuid4())
        call_id = f"compact-{uid[:8]}"
        events.put(Event(EventKind.TOOL_STARTED, session.thread_id,
                         {"call_id": call_id, "name": "compact", "args": {}}))
        compacted = False
        try:
            await session.client.query(_command_prompt("/compact", uid))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + COMPACT_TIMEOUT_S
            while True:
                if session.interrupted:
                    compacted = False
                    break
                left = deadline - loop.time()
                if left <= 0:
                    with contextlib.suppress(Exception):
                        await session.client.interrupt()
                    if why is not None:
                        why.append(f"Claude Code's /compact did not finish within "
                                   f"{COMPACT_TIMEOUT_S:g} s, so it was interrupted.")
                    break
                try:
                    item = await self._next(session, min(left, INTERRUPT_POLL_S))
                except asyncio.TimeoutError:
                    continue
                if item is _END:
                    session.pump = None
                    if why is not None:
                        why.append("Claude Code's stream ended before /compact finished.")
                    break
                if isinstance(item, _Raised):
                    session.pump = None
                    raise item.exc
                if isinstance(item, SystemMessage) and item.subtype == "compact_boundary":
                    compacted = True
                if (isinstance(item, ResultMessage)
                        and _injected(getattr(item, "origin", None)) is not None):
                    self._take(session, events, item)      # somebody else's turn
                    continue
                if self._take(session, events, item) is not None:
                    compacted = compacted and not item.is_error
                    if not compacted and why is not None:
                        said = _safe(getattr(item, "result", None) or "").strip()[:160]
                        why.append("Claude Code's /compact ended without compacting"
                                   + (f": {said}" if said else "."))
                    break
        finally:
            events.put(Event(EventKind.TOOL_FINISHED, session.thread_id, {
                "call_id": call_id, "name": "compact", "ok": compacted,
                "outcome": "ok" if compacted else "failed",
                "summary": "conversation compacted" if compacted else "compaction did not finish",
            }))
        LOG.info("claude %s: compaction %s", session.thread_id,
                 "finished" if compacted else "did not finish")
        return compacted

    async def _stop_if_interrupted(self, session: _Session) -> None:
        """A CLI turn has just begun after the owner pressed stop (a steer the
        CLI held, or the message itself queued behind a leftover). Stop means
        stop: that turn is interrupted too, and read to its end."""
        if session.interrupted:
            with contextlib.suppress(Exception):
                await session.client.interrupt()

    # -- between turns (SV-6) -------------------------------------------------

    async def _next(self, session: _Session, timeout: float | None) -> Any:
        """The next message for the reader that holds the stream now: what an
        earlier reader held, first and in order, then the stream itself."""
        try:
            return session.held.popleft()
        except IndexError:          # nothing held (or a close just cleared it)
            return await self._pump(session).next(timeout)

    async def _park(self, session: _Session) -> None:
        """Stop the idle reader (on the loop) and wait until it has. Its one
        wait on the stream is `_Pump.next`, which leaves its request standing
        when cancelled, and it routes each message without awaiting anything,
        so a message is never half-taken: nothing is read twice or lost."""
        task, session.idle = session.idle, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.wait({task})

    async def _take_stream(self, session: _Session) -> None:
        """A turn takes the stream: marked as the reader first, so nothing
        starts an idle reader while it parks the one running, and the idle
        reader is parked before the turn writes or reads anything. It stays
        parked until `_give_stream`."""
        session.reading = asyncio.current_task()
        await self._park(session)

    def _give_stream(self, session: _Session) -> None:
        """A turn is done with the stream (on the loop): the idle reader takes
        it again, if the daemon watches this session. Only the turn that holds
        the stream gives it back."""
        if session.reading is not asyncio.current_task():
            return
        session.reading = None
        self._start_idle(session)

    def _start_idle(self, session: _Session) -> None:
        """Start the idle reader (on the loop), unless a turn holds the
        stream, one already runs, nobody watches or the session closed."""
        if (session.closed or session.watch is None or session.reading
                or (session.idle is not None and not session.idle.done())):
            return
        session.idle = asyncio.ensure_future(self._idle(session))

    async def _idle(self, session: _Session) -> None:
        """The between-turn reader (SV-6). First what a turn left held, judged
        again, then the stream, one message at a time, each routed by
        `_route_idle`. It never makes a pump: a stream that ended, or a client
        a switch replaced, waits for the next turn, which reports it as
        before."""
        try:
            left, session.held = list(session.held), deque()
            for index, item in enumerate(left):
                if not self._route_one(session, item):
                    session.held.extend(left[index + 1:])
                    return
            pump = session.pump
            while (not session.closed and pump is not None and pump is session.pump
                   and not pump.ended and pump.client is session.client):
                item = await pump.next(None)
                if not self._route_one(session, item):
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the next turn reads on, as before SV-6
            LOG.exception("claude %s: the idle reader failed; the next turn reads on",
                          session.thread_id)

    def _route_one(self, session: _Session, item: Any) -> bool:
        """`_route_idle`, but a message it cannot route (a shape `_translate`
        raises on) is held for the next turn — which meets it as it did before
        SV-6 — and reading goes on: one bad message neither drops data nor
        stops the drain (review of #234, LOW 4)."""
        try:
            return self._route_idle(session, item)
        except Exception:  # noqa: BLE001
            LOG.exception("claude %s: a message read between turns could not be routed; "
                          "held for the next turn", session.thread_id)
            if not (session.held and session.held[-1] is item):
                session.held.append(item)
            return True

    def _route_idle(self, session: _Session, item: Any) -> bool:
        """One message read between turns, routed without awaiting anything.
        -> False when reading stops: the stream ended or raised, which is held
        for the next turn to report exactly as it did before SV-6.

        - Nobody watches: everything is held (the next send attributes it as
          before; the buffer is drained all the same).
        - A sub-agent's message or a task message: translated and handed to
          the watcher now — the child's row moves while the chat is idle.
        - Behind something already held: held, in order.
        - A main-thread system message (`init`, `status`), or a message of
          no turn at all (`_of_no_turn`: the `rate_limit_event` the CLI
          writes whenever the account's limit info changes, a background
          child's API calls included): read, nothing more. Never held: a
          held message holds everything behind it, the CLI's own turn
          included, until the owner's next message (the owner's report of
          2026-10-20, thread c5ee7933).
        - A CLI turn nobody sent: offered to the daemon (`adopt`), and held.
        - Anything else (a late steer's echo, a stray result): held for the
          next send's `_read`, whose rules judge it as before."""
        if item is _END or isinstance(item, _Raised):
            if isinstance(item, _Raised):
                LOG.warning("claude %s: the CLI's stream failed between turns (%s); the next "
                            "message reports it", session.thread_id, type(item.exc).__name__)
            else:
                LOG.info("claude %s: the CLI's stream ended between turns", session.thread_id)
            session.held.append(item)
            return False
        watch = session.watch
        if watch is None:
            self._hold(session, item)
            return True
        if _subagent_message(item):
            for event in self._translate(session, item):
                watch.put(event)
            return True
        if session.held:
            self._hold(session, item)
            return True
        if isinstance(item, SystemMessage) or _of_no_turn(item):
            _note_no_turn_kind(session, item)
            self._translate(session, item)
            return True
        origin = self._cli_turn(session, item)
        self._hold(session, item)
        if origin is not None:
            session.offer = origin
            watch.put(Event(EventKind.TURN_STARTED, session.thread_id, {"self_started": origin}))
            return True
        # What waits for the next message from here on, named (no content):
        # anything read behind it waits too.
        LOG.info("claude %s: holding a %s read between turns for the next message",
                 session.thread_id, type(item).__name__)
        uid = str(item.uuid).lower() if isinstance(item, SdkUserMessage) and item.uuid else None
        with session.mutex:
            stopped = uid is not None and uid in session.stopped
        if stopped:
            # A steer of a turn the owner stopped, run late by the CLI: stop
            # means stop, now rather than at the next send (whose `_read`
            # still drops its words).
            asyncio.ensure_future(self._stop_quietly(session))
        return True

    @staticmethod
    def _cli_turn(session: _Session, item: Any) -> str | None:
        """The origin of a CLI turn nobody sent that `item` opens, or None.
        Only on a CLI known to echo the messages that start our turns: without
        that, a late steer's turn could not be told from one."""
        if not session.prompt_echo:
            return None
        if isinstance(item, SdkUserMessage):
            uid = str(item.uuid).lower() if item.uuid else None
            with session.mutex:
                if uid is not None and uid in session.steering:
                    return None
            return _injected(getattr(item, "origin", None))
        if isinstance(item, (StreamEvent, AssistantMessage)):
            return "background"
        if isinstance(item, ResultMessage):
            return _injected(getattr(item, "origin", None))
        return None

    @staticmethod
    def _hold(session: _Session, item: Any) -> None:
        """Hold one message for the next reader. Past HELD_LIMIT a stream
        event (a live delta) is dropped; a whole message never is."""
        if len(session.held) >= HELD_LIMIT and isinstance(item, StreamEvent):
            session.held_dropped += 1
            if session.held_dropped == 1:
                LOG.warning("claude %s: %d messages held between turns; dropping live deltas",
                            session.thread_id, len(session.held))
            return
        session.held.append(item)

    @staticmethod
    async def _stop_quietly(session: _Session) -> None:
        with contextlib.suppress(Exception):
            await session.client.interrupt()

    def watch(self, h: SessionHandle) -> Iterator[Event]:
        """The daemon's feed for this session between turns (SV-6), for as
        long as it lives: a sub-agent's SUBAGENT and SUBAGENT_EVENT; a gate
        question asked between turns (APPROVAL_REQUESTED/RESOLVED, for the
        log); and TURN_STARTED `{self_started: origin}` when the CLI starts a
        turn nobody sent, which the daemon may take up with `adopt`. Ends when
        the session closes. One watcher per session. While it watches, the
        session's stream is read between turns too, so the SDK's buffer never
        fills and a hook is never left unanswered (the 2026-10-12 stall)."""
        session = h.native
        if session is None:
            return
        feed: queue.Queue = queue.Queue()
        with session.mutex:
            if session.closed:
                return
            if session.watch is not None:
                raise ValueError("this Claude session is already watched")
            session.watch = feed
        # A turn that ended before the watcher came started no idle reader:
        # one starts now, on the loop, where a turn's start and end are
        # decided, so never beside a turn that reads.
        with contextlib.suppress(RuntimeError):
            session.loop.call_soon_threadsafe(self._start_idle, session)
        try:
            while True:
                try:
                    item = feed.get(timeout=1.0)
                except queue.Empty:
                    if session.closed:
                        return
                    continue
                if item is _CLOSED:
                    return
                yield item
        finally:
            with session.mutex:
                if session.watch is feed:
                    session.watch = None

    def cli_turn_pending(self, h: SessionHandle) -> bool:
        """Whether the idle reader holds a CLI turn nobody sent, for `adopt`.
        Reads one field; never blocks (the daemon asks under its lock)."""
        session = h.native
        return session is not None and not session.closed and session.offer is not None

    def adopt(self, h: SessionHandle) -> Iterator[Event]:
        """Read the CLI turn the idle reader offered, as a turn of the thread
        (SV-6): TURN_STARTED `{self_started: origin}`, then its words, tools,
        gate questions and usage, then its TURN_FINISHED — the events a send
        yields, with no message of ours. Yields nothing when no turn is
        offered by the time it holds the stream (a send read it first). A
        steer is refused while it runs, so an owner message waits for it."""
        session = h.native
        if session is None or session.closed:
            return
        if not session.sending.acquire(timeout=ADOPT_WAIT_S):
            return
        try:
            yield from self._run_cli(h, session)
        finally:
            session.events = None
            with session.mutex:
                session.accepting = False
            session.sending.release()

    def _run_cli(self, h: SessionHandle, session: _Session) -> Iterator[Event]:
        events: queue.Queue = queue.Queue()
        session.events = events
        done = object()

        async def turn() -> None:
            try:
                await self._take_stream(session)
                origin, session.offer = session.offer, None
                if origin is None or session.closed:
                    return
                session.interrupted = False
                session.tools_seen.clear()
                session.denied_tools.clear()
                session.denial_without_id = False
                events.put(Event(EventKind.TURN_STARTED, session.thread_id, {"self_started": origin}))
                held = await self._read_cli(session, events)
                for event in self._end_foreground(session):
                    events.put(event)
                if held is not None:
                    events.put(held)
            except BaseException as exc:  # noqa: BLE001 — reported, never swallowed
                events.put(Event(EventKind.ERROR, session.thread_id,
                                 {"message": _fatal_message(session, exc), "fatal": True}))
            finally:
                self._give_stream(session)
                events.put(done)

        future = asyncio.run_coroutine_threadsafe(turn(), session.loop)
        finished = False
        try:
            while True:
                item = events.get()
                if item is done:
                    break
                if item.kind in (EventKind.TURN_FINISHED, EventKind.ERROR):
                    finished = True
                yield item
        finally:
            if not finished and not future.done():
                self.interrupt(h)
            future.cancel()

    # -- the Bypass switch (2026-10-21) -----------------------------------------

    def bypass_refusal(self, h: SessionHandle) -> str | None:
        """Why this session cannot take the Bypass switch, or None. Only an
        owner's chat on the auto profile has a classifier to switch off: the
        ask profile already skips it, and a task's workers keep theirs."""
        session = h.native
        if session is None or session.closed:
            return "This chat's Claude session is closed; send a message to resume it first."
        brief = session.brief
        if brief.role != Role.CHAT or brief.task_id is not None:
            return "Only an owner's chat can bypass the classifier; a task's workers keep it."
        if brief.profile is not PermissionProfile.AUTO:
            return "This chat is on the ask profile, which has no classifier: every call already asks you."
        return None

    def set_bypass(self, h: SessionHandle, on: bool) -> None:
        """The owner's Bypass switch: whether the auto-mode classifier judges
        this session's calls. Takes effect at the next tool call, mid-turn
        included. Jarvis's own gate is untouched either way: its DENY rules,
        always-ask list, owner-only checks and cards still run first, and
        only a call they allowed skips the classifier — a BYPASS_TOOLS call,
        in a turn the owner's own message started (`owner_turn`). The daemon calls this
        from the owner's HUD route alone (`Daemon.set_bypass`)."""
        said = self.bypass_refusal(h)
        if said and on:
            raise BriefRefused(said)
        session = h.native
        if session is not None:
            with session.mutex:
                session.bypass = bool(on)

    def bypassed(self, h: SessionHandle) -> bool:
        session = h.native
        return bool(session is not None and not session.closed and session.bypass)

    # -- the Compact button (2026-10-19) ----------------------------------------

    def compact_refusal(self, h: SessionHandle) -> str | None:
        """Why `compact` would not run now, in one plain sentence, or None.
        Reads two fields; sends nothing (the daemon asks under its lock)."""
        session = h.native
        if session is None or session.closed:
            return "This chat's Claude session is closed; send a message to resume it first."
        if session.offer is not None:
            return "Claude Code is running a turn of its own; compact when it ends."
        return None

    def compact(self, h: SessionHandle) -> Iterator[Event]:
        """The owner's Compact button: the CLI's own `/compact` (`_compact`,
        the same one a context-full turn runs) as a turn with no message, in
        the shape of `adopt`/`_run_cli`. Takes `sending` without blocking (a
        turn holds it: ends "error", nothing written), takes the stream,
        clears `interrupted`, and ends with TURN_FINISHED {"stop",
        "compacted", "reason"}: a boundary is "end"; COMPACT_TIMEOUT_S passing
        (interrupted for it) or a result with no boundary is "error" with a
        reason; the owner's Stop is "interrupted", seen within
        INTERRUPT_POLL_S. A steer meanwhile is refused (`accepting` stays
        False), so the daemon queues it. Usage and cost flow through `_take`,
        once."""
        session = h.native
        if session is None or session.closed:
            yield Event(EventKind.TURN_STARTED, h.thread_id)
            yield Event(EventKind.TURN_FINISHED, h.thread_id,
                        {"stop": "error", "compacted": False, "reason": "session closed"})
            return
        if not session.sending.acquire(blocking=False):
            yield Event(EventKind.TURN_STARTED, h.thread_id)
            yield Event(EventKind.TURN_FINISHED, h.thread_id,
                        {"stop": "error", "compacted": False,
                         "reason": "a turn is already running on this thread"})
            return
        try:
            yield from self._run_compact(h, session)
        finally:
            session.events = None
            with session.mutex:
                session.accepting = False
            session.sending.release()

    def _run_compact(self, h: SessionHandle, session: _Session) -> Iterator[Event]:
        events: queue.Queue = queue.Queue()
        session.events = events
        done = object()

        async def turn() -> None:
            try:
                await self._take_stream(session)
                if session.offer is not None:
                    # The CLI began a turn of its own after the daemon asked:
                    # it runs first (`adopt`), and nothing is written here.
                    events.put(Event(EventKind.TURN_FINISHED, session.thread_id, {
                        "stop": "error", "compacted": False,
                        "reason": "Claude Code started a turn of its own; compact when it ends."}))
                    return
                session.interrupted = False
                session.tools_seen.clear()
                session.denied_tools.clear()
                session.denial_without_id = False
                why: list = []
                compacted = await self._compact(session, events, why)
                for event in self._end_foreground(session):
                    events.put(event)
                if session.interrupted:
                    end = {"stop": "interrupted", "compacted": False, "reason": None}
                elif compacted:
                    end = {"stop": "end", "compacted": True, "reason": None}
                else:
                    end = {"stop": "error", "compacted": False,
                           "reason": why[0] if why else "Claude Code's /compact did not finish."}
                events.put(Event(EventKind.TURN_FINISHED, session.thread_id, end))
            except BaseException as exc:  # noqa: BLE001 — reported, never swallowed
                events.put(Event(EventKind.ERROR, session.thread_id,
                                 {"message": _fatal_message(session, exc), "fatal": True}))
            finally:
                self._give_stream(session)
                events.put(done)

        future = asyncio.run_coroutine_threadsafe(turn(), session.loop)
        finished = False
        try:
            yield Event(EventKind.TURN_STARTED, h.thread_id)
            while True:
                item = events.get()
                if item is done:
                    break
                if item.kind in (EventKind.TURN_FINISHED, EventKind.ERROR):
                    finished = True
                yield item
        finally:
            if not finished and not future.done():
                self.interrupt(h)
            future.cancel()

    async def _read_cli(self, session: _Session, events: queue.Queue) -> Event | None:
        """Read one CLI turn nobody sent, held messages first, to its result.
        -> its TURN_FINISHED, or None when the stream ended first (a dead CLI:
        the next message reports it, as a send would). Nothing here waits on
        a message of ours, and nothing is sent again."""
        while True:
            item = await self._next(session, None)
            if item is _END:
                session.pump = None
                return None
            if isinstance(item, _Raised):
                session.pump = None
                raise item.exc
            if isinstance(item, SdkUserMessage) and item.uuid and not item.parent_tool_use_id:
                uid = str(item.uuid).lower()
                with session.mutex:
                    # A steer the CLI folded into its own turn has been seen.
                    session.steering.pop(uid, None)
                    session.stopped.discard(uid)
            finished = self._take(session, events, item)
            if finished is not None:
                return finished

    def _pump(self, session: _Session) -> "_Pump":
        """The session's reader of its client's stream (on the session's loop)."""
        pump = session.pump
        if pump is None or pump.ended or pump.client is not session.client:
            pump = _Pump(session.client)
            session.pump = pump
        return pump

    def _stop_pump(self, session: _Session) -> None:
        """Stop the reader before its client goes away (switch or close)."""
        pump, session.pump = session.pump, None
        if pump is not None and pump.task is not None:
            with contextlib.suppress(RuntimeError):
                session.loop.call_soon_threadsafe(pump.task.cancel)

    def _take(self, session: _Session, events: queue.Queue, msg: Any) -> Event | None:
        """Translate one message onto the turn's queue. -> the TURN_FINISHED
        of a result frame, held back for `_read` to place; else None."""
        finished = None
        for event in self._translate(session, msg):
            if event.kind is EventKind.TURN_FINISHED:
                finished = event
            else:
                events.put(event)
        return finished

    def _translate(self, session: _Session, msg: Any) -> list[Event]:
        """One SDK message becomes zero or more §5.2 events.

        A message tagged with `parent_tool_use_id` is a sub-agent's: what it
        says or does comes out only as SUBAGENT_EVENT (`_child`), never as the
        parent's TEXT, THINKING or TOOL_* — the transcript the owner reads as
        Jarvis's reply, Discord, a task's result and voice all read those. A
        tagged stream event (CLI 2.1.295 sends none) is dropped."""
        out: list[Event] = []
        if isinstance(msg, SystemMessage):
            self._note_session_id(session, (msg.data or {}).get("session_id"))
            return self._task(session, msg)
        if isinstance(msg, StreamEvent):
            self._note_session_id(session, msg.session_id)
            if getattr(msg, "parent_tool_use_id", None):
                return out                         # never a sub-agent's delta
            text = _delta_text(msg.event)
            if text:
                out.append(Event(EventKind.TEXT_DELTA, session.thread_id, {"text": text}))
            return out
        if (isinstance(msg, (AssistantMessage, SdkUserMessage))
                and getattr(msg, "parent_tool_use_id", None)):
            return self._child(session, msg, msg.parent_tool_use_id)
        if isinstance(msg, AssistantMessage):
            self._note_session_id(session, msg.session_id)
            if _context_full_message(msg):
                # "Prompt is too long" is the CLI's error, not a reply: the
                # turn handles it (`_context_full`) and the owner is told.
                return out
            return self._blocks(session, msg, session.tools_seen)
        if isinstance(msg, SdkUserMessage):
            content = msg.content
            if isinstance(content, list):
                for block in content:
                    out.extend(self._tool_result(session, block))
            return out
        if isinstance(msg, ResultMessage):
            self._note_session_id(session, msg.session_id)
            session.resumable = True
            out.extend(self._result(session, msg))
            return out
        if isinstance(msg, ConversationResetMessage):
            # The CLI zeroes its running totals here (a `/clear`): the next
            # result's figure starts again from nothing.
            with session.mutex:
                session.cost_seen = 0.0
            return out
        return out

    def _blocks(self, session: _Session, msg: AssistantMessage, seen: dict) -> list[Event]:
        """An assistant message's blocks as TEXT, THINKING and TOOL_STARTED,
        each call's name and arguments kept in `seen` for its result."""
        out: list[Event] = []
        for block in msg.content:
            if isinstance(block, TextBlock):
                if block.text:
                    out.append(
                        Event(EventKind.TEXT, session.thread_id, {"text": _safe(block.text)})
                    )
            elif isinstance(block, ThinkingBlock):
                if block.thinking:
                    out.append(
                        Event(
                            EventKind.THINKING,
                            session.thread_id,
                            {"text": _safe(block.thinking)},
                        )
                    )
            elif isinstance(block, ToolUseBlock):
                args = dict(block.input) if isinstance(block.input, dict) else {}
                seen[block.id] = (block.name, args)
                out.append(
                    Event(
                        EventKind.TOOL_STARTED,
                        session.thread_id,
                        {"call_id": block.id, "name": block.name,
                         "args": {} if block.name in ("hud_browser_act", "mcp__jarvis__hud_browser_act") else args},
                    )
                )
        return out

    # -- native sub-agents (SV-1)

    def _child(self, session: _Session, msg: Any, parent_id: Any) -> list[Event]:
        """A sub-agent's message: built exactly as the parent's would be
        (`_safe`, the private summaries, the denial mapping), then each event
        wrapped as SUBAGENT_EVENT under the child's sid. A sid not yet known
        (its `task_started` lost to the bound, or a CLI that sends none) is
        started here; past the bound it is not remembered, and its events
        are still the child's, never the parent's."""
        sid = child_sid(parent_id)
        out: list[Event] = []
        with session.mutex:
            known = sid in session.children
        if not known:
            child = self._remember(session, _Child(sid=sid))
            if child is not None:
                out.append(self._lifecycle(session, child, "started"))
        inner: list[Event] = []
        if isinstance(msg, AssistantMessage):
            self._note_session_id(session, msg.session_id)
            if not _context_full_message(msg):
                with session.mutex:
                    inner = self._blocks(session, msg, session.child_tools)
                    while len(session.child_tools) > CHILD_TOOL_LIMIT:
                        session.child_tools.popitem(last=False)
        elif isinstance(msg.content, list):
            for block in msg.content:
                inner.extend(self._tool_result(session, block, seen=session.child_tools))
        for event in inner:
            out.append(Event(EventKind.SUBAGENT_EVENT, session.thread_id,
                             {"sid": sid, "event": {"kind": event.kind.value, "data": event.data}}))
        return out

    def _remember(self, session: _Session, child: _Child) -> _Child | None:
        """Add a child, making room by forgetting the oldest finished one. With
        CHILD_LIMIT children all live, the newcomer is not remembered."""
        with session.mutex:
            if len(session.children) >= CHILD_LIMIT:
                done = next((sid for sid, c in session.children.items()
                             if c.status not in CHILD_LIVE), None)
                if done is None:
                    LOG.warning("claude %s: %d sub-agents live; not tracking another",
                                session.thread_id, len(session.children))
                    return None
                gone = session.children.pop(done)
                if gone.task_id and session.tasks.get(gone.task_id) == done:
                    del session.tasks[gone.task_id]
            session.children[child.sid] = child
            if child.task_id:
                session.tasks[child.task_id] = child.sid
            return child

    def _lifecycle(self, session: _Session, child: _Child, op: str) -> Event:
        data: dict[str, Any] = {"sid": child.sid, "op": op, "status": child.status,
                                "depth": child.depth, "background": child.background}
        for key in ("task_id", "parent_sid", "name", "kind", "last_tool", "tokens",
                    "tool_uses", "duration_ms", "stop_reason"):
            value = getattr(child, key)
            if value not in (None, ""):
                data[key] = value
        return Event(EventKind.SUBAGENT, session.thread_id, data)

    def _task(self, session: _Session, msg: Any) -> list[Event]:
        """A `task_*` system message, read by `subtype` and its raw `data`
        (so an SDK that does not model them still reads them). Only a
        `local_agent` task is a sub-agent; a long Bash (`local_bash`, SV-0)
        and anything else emits nothing."""
        subtype = getattr(msg, "subtype", None)
        data = msg.data if isinstance(getattr(msg, "data", None), dict) else {}
        task_id = data.get("task_id")
        task_id = str(task_id) if isinstance(task_id, str) and task_id else None
        if subtype == "task_started":
            spawn = data.get("tool_use_id")
            if data.get("task_type") != "local_agent" or task_id is None or not spawn:
                return []
            sid = child_sid(spawn)
            with session.mutex:
                child = session.children.get(sid)
                parent_sid = session.tasks.get(str(data.get("parent_task_id") or ""))
                parent = session.children.get(parent_sid) if parent_sid else None
            new = child is None
            if new:
                child = _Child(sid=sid)
            child.task_id = task_id
            child.parent_sid = parent_sid
            depth = _count(data.get("spawn_depth"))
            child.depth = depth if depth else (parent.depth + 1 if parent is not None else 1)
            child.background = data.get("is_backgrounded") is True
            child.name = _safe(data.get("description") or "")[:CHILD_NAME_CHARS]
            child.kind = _safe(data.get("subagent_type") or "")[:CHILD_NAME_CHARS]
            if child.status in CHILD_LIVE:
                child.status = "backgrounded" if child.background else "running"
            if new:
                if self._remember(session, child) is None:
                    return []
            else:
                with session.mutex:
                    session.tasks[task_id] = sid
            event = self._lifecycle(session, child, "started" if new else "progress")
            brief = data.get("prompt")
            if isinstance(brief, str) and brief.strip():
                # The orchestrator's brief to the child (the owner's SV-3
                # decision: shown collapsed at the top of its pane). Carried
                # on this one lifecycle event only, never kept here; the
                # record scrubs and caps it and serves it on the owner's
                # transcript route alone, never the bus or the log.
                # Scrubbed whole, then cut: a credential across the cut is
                # never left half-shown (review of #212, LOW-5).
                event.data["brief"] = _safe(brief)[:CHILD_BRIEF_CHARS]
            return [event]
        with session.mutex:
            sid = session.tasks.get(task_id) if task_id else None
            if sid is None and data.get("tool_use_id"):
                candidate = child_sid(data.get("tool_use_id"))
                if candidate in session.children:
                    sid = candidate
            child = session.children.get(sid) if sid else None
        if child is None:
            return []
        if subtype == "task_progress":
            self._usage(child, data.get("usage"))
            tool = data.get("last_tool_name")
            if isinstance(tool, str) and tool:
                child.last_tool = _safe(tool)[:CHILD_NAME_CHARS]
            if child.status not in CHILD_LIVE:
                return []
            return [self._lifecycle(session, child, "progress")]
        if subtype == "task_updated":
            patch = data.get("patch") if isinstance(data.get("patch"), dict) else {}
            status = _CHILD_TERMINAL.get(str(patch.get("status") or ""))
            if status is not None:
                return self._finish(session, child, status)
            if patch.get("is_backgrounded") is True and child.status == "running":
                child.background, child.status = True, "backgrounded"
                return [self._lifecycle(session, child, "progress")]
            return []
        if subtype == "task_notification":
            self._usage(child, data.get("usage"))
            status = _CHILD_TERMINAL.get(str(data.get("status") or ""))
            if status is None:
                return []
            out = self._finish(session, child, status)
            if not out:
                # Already ended (task_updated said so first): the closing
                # numbers still count.
                out = [self._lifecycle(session, child, "progress")]
            summary = data.get("summary")
            if isinstance(summary, str) and summary.strip():
                # The CLI's closing word on the child (its final text, the
                # spawn description when stopped, the error when it failed):
                # the child's, so its transcript's, never the parent's.
                out.append(Event(EventKind.SUBAGENT_EVENT, session.thread_id, {
                    "sid": child.sid, "event": {"kind": "result", "data": {
                        "text": _safe(summary)[:CHILD_RESULT_CHARS], "status": status}}}))
            return out
        return []

    @staticmethod
    def _usage(child: _Child, usage: Any) -> None:
        if not isinstance(usage, dict):
            return
        for key, field_name in (("total_tokens", "tokens"), ("tool_uses", "tool_uses"),
                                ("duration_ms", "duration_ms")):
            value = _count(usage.get(key))
            if value is not None:
                setattr(child, field_name, value)

    def _finish(self, session: _Session, child: _Child, status: str,
                reason: str | None = None, *, inferred: bool = False) -> list[Event]:
        """A child's end, said once. The CLI's word replaces one Jarvis
        inferred; nothing replaces the CLI's."""
        if child.status not in CHILD_LIVE and not child.inferred:
            return []
        if child.status not in CHILD_LIVE and inferred:
            return []
        child.status, child.inferred = status, inferred
        child.stop_reason = reason if inferred else None
        if status == "stopped" and child.owner_stop and not inferred:
            child.stop_reason = OWNER_STOPPED
        return [self._lifecycle(session, child, "finished")]

    def _end_foreground(self, session: _Session) -> list[Event]:
        """The message's turn is over: a top-level child that was not
        backgrounded cannot still be running (the CLI waits for a foreground
        Agent call, and its stop or interrupt says `stopped` first, SV-0).
        One still live is stopped, inferred. Background children are left
        alone: outliving the turn is what they are for."""
        with session.mutex:
            live = [c for c in session.children.values()
                    if c.status == "running" and not c.background and c.parent_sid is None]
        out: list[Event] = []
        for child in live:
            out.extend(self._finish(session, child, "stopped", "the turn ended", inferred=True))
        return out

    @staticmethod
    def _tool_summary(name: str, text: str) -> str:
        canonical = name.removeprefix("mcp__jarvis__")
        if _private_result(canonical):
            from jarvis import tools
            return tools.private_summary(canonical, text)
        return text[:SUMMARY_CHARS]

    def _tool_result(self, session: _Session, block: Any, *, seen: dict | None = None) -> list[Event]:
        if not isinstance(block, ToolResultBlock):
            return []
        name, args = (session.tools_seen if seen is None else seen).get(block.tool_use_id, ("", {}))
        text = _safe(_result_text(block.content))
        declined = text.startswith(CLASSIFIER_DENIAL_PREFIX)
        child_denied = getattr(session, "child_denied", None)
        denied = (declined or block.tool_use_id in session.denied_tools
                  or (child_denied is not None and child_denied.pop(block.tool_use_id, None) is not None))
        # Older SDK contexts may omit the id. Only an error can then be a
        # hook refusal; successful output bearing those words stays success.
        if (session.denial_without_id and block.is_error
                and text.startswith((HOOK_DENIAL_PREFIX, HOOK_REFUSAL_PREFIX))):
            denied = True
            session.denial_without_id = False
        session.denied_tools.discard(block.tool_use_id)
        private_read = _private_result(name.removeprefix("mcp__jarvis__"))
        private_act = name in ("hud_browser_act", "mcp__jarvis__hud_browser_act")
        summary = self._tool_summary(name, text)
        if private_read:
            if denied:
                summary = self._tool_summary(name, "Refused")
            elif block.is_error and summary not in ("refused", "error", "stopped"):
                summary = self._tool_summary(name, "Error")
        out: list[Event] = []
        if declined:
            # §6.1: the owner's judgement outranks a classifier, but only
            # explicitly and per command — so the command travels with the
            # event rather than a summary of it.
            match = _REASON.search(text)
            out.append(
                Event(
                    EventKind.REVIEWER_DECLINED,
                    session.thread_id,
                    {
                        "tool": name,
                        "args": {} if private_act else args,
                        "command": None if private_act else _command(args),
                        "reason": (summary if private_read
                                   else match.group("reason").strip() if match
                                   else text[:SUMMARY_CHARS]),
                    },
                )
            )
        out.append(
            Event(
                EventKind.TOOL_FINISHED,
                session.thread_id,
                {
                    "call_id": block.tool_use_id,
                    "name": name,
                    "ok": not bool(block.is_error) and not declined and not (private_act and summary == "stopped"),
                    "outcome": "denied" if denied else "failed" if block.is_error or (private_act and summary == "stopped") else "ok",
                    "summary": summary,
                },
            )
        )
        return out

    def _result(self, session: _Session, msg: ResultMessage) -> list[Event]:
        reported = dict(msg.usage or {})
        # Every result is translated here exactly once, whoever's CLI turn it
        # ends (the message's, a steer's, a background notice's, a stopped
        # one's, `/compact`'s), so each adds exactly what it spent.
        running = running_cost.figure(msg.total_cost_usd)
        with session.mutex:
            unknown = session.cost_seen is None
            cost, session.cost_seen = running_cost.step(session.cost_seen, running)
            seen = session.cost_seen
        if unknown and seen is not None:
            LOG.info("claude %s: resumed with no saved cost total; the first figure is "
                     "taken as the baseline, its own cost not counted", session.thread_id)
        turn = Usage(
            # `usage` is the main loop's, per turn (the CLI's own description:
            # "per-turn in streaming-input sessions"): no running total here.
            input_tokens=int(reported.get("input_tokens") or 0),
            output_tokens=int(reported.get("output_tokens") or 0),
            cached_tokens=int(reported.get("cache_read_input_tokens") or 0),
            cost_usd=cost,
        )
        total = session.usage
        total.input_tokens += turn.input_tokens
        total.output_tokens += turn.output_tokens
        total.cached_tokens += turn.cached_tokens
        if cost is not None:
            total.cost_usd = (total.cost_usd or 0.0) + cost
        return [
            Event(
                EventKind.USAGE,
                session.thread_id,
                {
                    "input": turn.input_tokens,
                    "output": turn.output_tokens,
                    "cached": turn.cached_tokens,
                    # An *equivalent* figure on a subscription, where nothing
                    # is billed (R1): what this result added to the CLI's
                    # running total, or None when that cannot be told. What
                    # it means is the ledger's call, not this provider's.
                    "cost_usd": cost,
                    # The running total it is measured from now, for the
                    # daemon to save: a resume starts from it.
                    "cost_total": seen,
                    "provider_reported": reported,
                },
            ),
            Event(
                EventKind.TURN_FINISHED,
                session.thread_id,
                {"stop": _stop_reason(session, msg)},
            ),
        ]

    def _note_session_id(self, session: _Session, session_id: Any) -> None:
        """The CLI's own id wins over the one we minted, if they ever differ."""
        if not session_id or session.session_id == session_id:
            return
        session.session_id = str(session_id)
        if session.handle is not None:
            session.handle.provider_session_id = session.session_id

    # -- control

    def set_model(self, h: SessionHandle, model: str | None, effort: str | None) -> None:
        """Move this conversation onto another model or effort (decisions A1).

        Called between turns only — the daemon calls it at the start of the
        next turn, before the message is sent. Claude Code fixes `--effort` at
        spawn, so the change is a **resume with new options**: the client is
        disconnected and a new one connects to the same session id with the
        new model and effort. The conversation is the CLI's session on disk,
        so nothing is lost but the seconds a reconnect costs.

        Refuses rather than narrows (§5.1): an effort off the CLI's ladder,
        or a turn still running, raises — and a reconnect that fails connects
        the old model again and raises `BriefRefused`, so the thread keeps
        working on the model it had and the owner is told why. If the old
        model will not connect either, the old client is already
        disconnected and putting it back would leave a session that looks
        open and answers nothing: the session is closed instead and
        `SessionLost` raised, so the caller drops the handle and the next
        message resumes the conversation cleanly.
        """
        session = _native(h)
        if session.closed:
            raise ValueError("this Claude session is closed")
        if effort is not None and effort not in EFFORT_LEVELS:
            raise BriefRefused(f"effort {effort!r} is not one of {', '.join(EFFORT_LEVELS)}")
        if not session.sending.acquire(blocking=False):
            raise ValueError("a turn is running; the model changes at the next one")
        try:
            old_brief, old_client = session.brief, session.client
            brief = replace(old_brief, model=model, effort=effort)
            resume = session.resumable

            def connect(target: Brief) -> Any:
                options = self._options(target, session, resume=resume, session_id=session.session_id)
                client = _client_factory(options)
                _await(session.loop, client.connect(), CONNECT_TIMEOUT_S)
                return client

            # The old client's reader goes with it, and so does anything the old
            # CLI process still held: a steer queued there dies with it, and
            # so does every sub-agent it ran (SV-1): stopped here, so a dead
            # child holds no CHILD_LIMIT slot (`live_subagents` tells the daemon).
            # The idle reader stops first (SV-6), and what it held of the old
            # CLI's stream — a turn it offered included — goes with that CLI;
            # the next turn's end starts it again on the new client.
            with contextlib.suppress(Exception):
                _await(session.loop, self._park(session), PARK_TIMEOUT_S)
            session.held.clear()
            session.offer = None
            self._stop_pump(session)
            with session.mutex:
                session.steering.clear()
                session.stopped.clear()
                for child in session.children.values():
                    if child.status in CHILD_LIVE:
                        child.status, child.stop_reason = "stopped", "the session restarted"
            try:
                _await(session.loop, old_client.disconnect(), CONTROL_TIMEOUT_S)
            except Exception:  # noqa: BLE001 — an old client that will not go quietly still goes
                pass
            lost = False
            try:
                session.client = connect(brief)
            except BaseException as exc:  # noqa: BLE001
                reason = f"claude could not switch to {model or 'its default model'}: {_safe(exc)}"
                try:
                    session.client = connect(old_brief)
                except BaseException as again:  # noqa: BLE001
                    lost = True
                    reason += f"; reconnecting the old model failed too: {_safe(again)}"
                else:
                    raise BriefRefused(reason) from None
            else:
                session.brief = brief
        finally:
            session.sending.release()
        if lost:
            # The old client was disconnected above and neither connect took:
            # close the session (deny anything pending, stop its loop) rather
            # than hand back a client that can no longer answer.
            session.client = old_client
            self.close(h)
            raise SessionLost(reason + "; the session is closed and resumes with the next message")

    def live_subagents(self, h: SessionHandle) -> set[str]:
        """The sids of the sub-agents this session's CLI still runs, as far
        as it has said (SV-1). A closed session runs none."""
        session = h.native
        if session is None or session.closed:
            return set()
        with session.mutex:
            return {sid for sid, child in session.children.items() if child.status in CHILD_LIVE}

    def subagent_stop_refusal(self, h: SessionHandle, sid: str) -> str | None:
        """Why the owner's Stop cannot be sent to child `sid` now, in one
        plain sentence, or None when it can (SV-4). Sends nothing."""
        try:
            self._stoppable(h, sid)
        except (SubagentNotLive, SubagentStopUnsupported) as exc:
            return str(exc)
        return None

    def _stoppable(self, h: SessionHandle, sid: str) -> tuple[_Session, _Child]:
        session = h.native
        if session is None or session.closed:
            raise SubagentNotLive("This chat's Claude session is closed, so it runs no sub-agent.")
        with session.mutex:
            child = session.children.get(sid)
            if child is None or child.status not in CHILD_LIVE:
                raise SubagentNotLive("This sub-agent is not running in this chat's session.")
            task_id = child.task_id
        if not CLAUDE_STOP_VERIFIED:
            raise SubagentStopUnsupported(
                "Claude Code can stop this sub-agent only by stopping the whole turn.")
        if not task_id:
            raise SubagentStopUnsupported(
                "Claude Code has not given this sub-agent a task id, so it can be stopped "
                "only by stopping the whole turn.")
        known = _known_cli_version()
        if known is not None and known < parse_version(CLAUDE_STOP_VERIFIED):
            raise SubagentStopUnsupported(
                f"This Claude Code is older than {CLAUDE_STOP_VERIFIED}, where stopping one "
                "sub-agent was verified; stop the whole turn instead.")
        return session, child

    def stop_subagent(self, h: SessionHandle, sid: str) -> None:
        """The owner's Stop on one child (SV-4): `stop_task(task_id)`, never
        `interrupt()`, so the orchestrator's turn and every sibling go on
        (SV-0 finding 2). Returns once the CLI took the request; the child's
        end is the CLI's own later `killed`/`stopped`, read like any other and
        then said as "stopped by the owner". Raises `SubagentNotLive` or
        `SubagentStopUnsupported` before anything is sent; the CLI's own
        refusal (an error control response) raises `SubagentStopDeclined`
        with its reason; a request the CLI did not answer within
        STOP_CONTROL_TIMEOUT_S raises TimeoutError (it may still act on it).

        On that timeout the request is **not** cancelled: cancelling the
        coroutine would skip the SDK's own cleanup and leave its private
        `pending_control_responses` entry behind. It is left to finish, so
        the SDK's own 60 s control timeout (or the CLI's late answer) removes
        that entry the way the SDK means it to; the late outcome is logged.

        The owner's mark (`owner_stop`, which makes the child's end "stopped
        by the owner") is set before the request goes. Each answer, in time
        or late (`_late_stop`), settles it by one rule (`_stop_outcome`,
        `_stop_settled`): taken keeps it; the SDK's own control timeout keeps
        it (the CLI may still act); a refusal or an undelivered request takes
        it off, unless another Stop of the child is still on its way or was
        taken."""
        session, child = self._stoppable(h, sid)
        task_id = child.task_id
        with session.mutex:
            child.owner_stop = True
            child.owner_stops += 1
        future = asyncio.run_coroutine_threadsafe(session.client.stop_task(task_id), session.loop)
        try:
            future.result(STOP_CONTROL_TIMEOUT_S)
        except concurrent.futures.TimeoutError:
            future.add_done_callback(
                lambda done, child=child, sid=sid: _late_stop(session, child, sid, done))
            raise TimeoutError(f"Claude Code did not answer the stop within "
                               f"{int(STOP_CONTROL_TIMEOUT_S)} s") from None
        except BaseException as exc:
            # Refused or not delivered: whatever stops it later is not this
            # (unless another Stop of it is still on its way, or was taken).
            _stop_settled(session, child, _stop_outcome(exc))
            if _cli_refusal(exc):
                raise SubagentStopDeclined(_safe(str(exc))[:160] or "no reason given") from None
            raise
        _stop_settled(session, child, "taken")

    def interrupt(self, h: SessionHandle) -> None:
        session = h.native
        if session is None or session.closed:
            return
        session.interrupted = True
        try:
            _await(session.loop, session.client.interrupt(), CONTROL_TIMEOUT_S)
        except Exception:  # noqa: BLE001 — a failed interrupt must not raise at a surface
            pass

    def usage(self, h: SessionHandle) -> Usage:
        session = h.native
        if session is None:
            return Usage()
        total = session.usage
        return Usage(
            input_tokens=total.input_tokens,
            output_tokens=total.output_tokens,
            cached_tokens=total.cached_tokens,
            cost_usd=total.cost_usd,
        )

    def close(self, h: SessionHandle) -> None:
        """Disconnect and stop the loop thread. Idempotent."""
        session = h.native
        if session is None or session.closed:
            return
        with session.mutex:
            session.closed = True
            # The watcher's feed ends (SV-6): set under the lock `watch`
            # registers under, so a watcher is either told or never starts.
            feed = session.watch
            if feed is not None:
                feed.put(_CLOSED)
            for pending in session.pending.values():
                if not pending.ready.is_set():
                    pending.value = Decision.DENY
                    pending.ready.set()
            session.pending.clear()
            session.steering.clear()
            session.stopped.clear()
        # The idle reader stops before its pump and its loop do, so no task
        # is left pending on a loop that is about to stop.
        with contextlib.suppress(Exception):
            _await(session.loop, self._park(session), PARK_TIMEOUT_S)
        session.held.clear()
        session.offer = None
        self._stop_pump(session)
        try:
            _await(session.loop, session.client.disconnect(), CONTROL_TIMEOUT_S)
        except Exception:  # noqa: BLE001 — close must not raise
            pass
        _stop_loop(session.loop, session.runner)
        # The carrier goes with the session: its token is already revoked.
        remove_mcp_config(session.mcp_config)
        session.mcp_config = None


# --- helpers ----------------------------------------------------------------


def _log_bypass(session: _Session, name: str, args: dict) -> None:
    """The decision log's line for a call the owner's Bypass switch kept from
    the classifier. The gate's own line for the call is already written (its
    layer-5 "the provider's own reviewer decides"); this one says that the
    reviewer was not asked. A failure to log decides nothing."""
    try:
        from ..permissions import ApprovalRecord, _digest, _safe_command, log_decision
        command = _command(args)
        brief = session.brief
        log_decision(ApprovalRecord(
            tool=name, args_digest=_digest(args), layer="owner-bypass", decision="allow",
            reason=BYPASS_REASON, command=_safe_command(command) if command else None,
            thread_id=session.thread_id, task_id=brief.task_id, provider=ProviderName.CLAUDE.value))
    except Exception:  # noqa: BLE001
        LOG.warning("Could not log a bypassed call on %s", session.thread_id, exc_info=True)


def _note_denied(session: _Session, tool_use_id: Any, agent_id: Any = None) -> None:
    """A call the gate refused: its result reads as `denied`. A sub-agent's is
    kept apart, across turns, since its result may come after its turn."""
    if not tool_use_id:
        session.denial_without_id = True
        return
    session.denied_tools.add(tool_use_id)
    if agent_id:
        with session.mutex:
            session.child_denied[tool_use_id] = True
            while len(session.child_denied) > CHILD_TOOL_LIMIT:
                session.child_denied.popitem(last=False)


def _native(h: SessionHandle) -> _Session:
    if h.native is None:
        raise ValueError("this Claude session is closed")
    return h.native


def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


def _await(loop: asyncio.AbstractEventLoop, coro: Any, timeout: float) -> Any:
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)


def _in_thread(loop: asyncio.AbstractEventLoop, fn) -> Any:
    """`asyncio.to_thread`, but on a **daemon** thread we own.

    The default executor's threads are joined at interpreter exit, so a
    `permit` that blocks for ten minutes would block shutdown for ten minutes —
    and one that never returns would block it forever. The gate is allowed to
    take a long time; the process is not allowed to be held hostage by it.
    """
    future = loop.create_future()

    def run() -> None:
        try:
            value, error = fn(), None
        except BaseException as exc:  # noqa: BLE001
            value, error = None, exc

        def settle() -> None:
            if future.done():
                return
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(value)

        loop.call_soon_threadsafe(settle)

    threading.Thread(target=run, name="jarvis-claude-permit", daemon=True).start()
    return future


def _stop_loop(loop: asyncio.AbstractEventLoop, runner: threading.Thread) -> None:
    loop.call_soon_threadsafe(loop.stop)
    runner.join(timeout=CONTROL_TIMEOUT_S)
    try:
        loop.close()
    except RuntimeError:
        pass


def child_sid(tool_use_id: Any) -> str:
    """A sub-agent's id: "cl:" and the spawning Agent call's id. An id the
    pattern does not fit is hashed, never dropped: a tagged message must
    still land with a child, whatever its parent id looks like."""
    text = str(tool_use_id or "")
    if _SID_ID.fullmatch(text):
        return "cl:" + text
    return "cl:h" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:32]


def child_label(name: Any) -> str:
    """The sub-agent's name as an approval card shows it: scrubbed, reduced to
    `[A-Za-z0-9 _./-]` (anything else "_"), one space at a time, capped."""
    if not isinstance(name, str):
        return CHILD_UNNAMED
    label = " ".join(_CHILD_LABEL_UNSAFE.sub("_", " ".join(_safe(name).split())).split()).strip(" _")
    if len(label) > CHILD_LABEL_CAP:
        label = label[:CHILD_LABEL_CAP - 1].rstrip() + "…"
    return label or CHILD_UNNAMED


def _takes_subagent(permit: Any) -> bool:
    """Whether `permit` takes the `subagent` keyword. Only such a callback is
    handed it: one that does not would raise, and a raise denies — the label
    must never change an answer, either way."""
    return _takes_keyword(permit, "subagent")


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _command(args: dict) -> str | None:
    """The shell command in a tool's arguments, if it has one."""
    value = args.get("command")
    return value if isinstance(value, str) else None


def _prompt(message: UserMessage, uid: str, *, later: bool = False) -> Any:
    """A v2 `UserMessage` as what `client.query()` accepts: one stdin user
    message carrying `uid`, which the CLI echoes back when the message starts
    its turn (`--replay-user-messages`) — how `_read` knows which CLI turn is
    this message's. `later` sends it at priority `later`, which the CLI never
    folds into a turn already running (2.1.295 folds only `now`/`next`), for
    when a late steer turn may still be ahead of it; otherwise the CLI's own
    default applies, as before.

    A named skill (`/skill`) becomes a directive: Claude Code has the skill
    installed natively (`jarvis skills link`), so it is told to use it and its
    Skill tool loads it. Whether a headless `/<name>` prompt would invoke it
    directly is the S1 spike still pending; the directive works either way.
    """
    payload = {
        "type": "user",
        "message": {"role": "user", "content": _content(message)},
        "parent_tool_use_id": None,
        "uuid": uid,
    }
    if later:
        payload["priority"] = "later"

    async def one() -> Any:
        yield payload

    return one()


def _command_prompt(text: str, uid: str) -> Any:
    """A CLI slash command (`/compact`) as one stdin user message — the form
    the Agent SDK documents for slash commands in streaming input."""
    payload = {
        "type": "user",
        "message": {"role": "user", "content": text},
        "parent_tool_use_id": None,
        "uuid": uid,
    }

    async def one() -> Any:
        yield payload

    return one()


def _injected(origin: Any) -> str | None:
    """The kind of a CLI turn somebody else started, or None for the owner's.

    The CLI stamps `origin` on the user message it replays for a turn it
    started itself (a background task's notice, a channel or peer message) and
    on that turn's result; the SDK documents it as how a streaming-input
    consumer tells its own prompt's result from an injected turn's. A prompt
    of ours carries none, or `{"kind": "human"}`."""
    if not isinstance(origin, dict):
        return None
    kind = origin.get("kind")
    if not isinstance(kind, str) or kind == "human":
        return None
    return kind


def _too_long(text: Any) -> bool:
    return isinstance(text, str) and text.strip().lower().startswith(CONTEXT_FULL_PREFIX)


def _context_full_message(msg: AssistantMessage) -> bool:
    """The CLI's synthetic API error for a conversation past the context
    window: an assistant message with `error` set (or the CLI's synthetic
    model) whose text is "Prompt is too long…". A reply of the model's that
    merely starts with those words carries neither."""
    if getattr(msg, "error", None) is None and getattr(msg, "model", None) != "<synthetic>":
        return False
    return any(isinstance(b, TextBlock) and _too_long(b.text) for b in msg.content)


def _context_full_result(msg: Any, *, said: bool = False) -> bool:
    """A result frame that ended on "Prompt is too long": `is_error`, and the
    words in the result itself or (`said`) in the CLI turn's own synthetic
    error. A turn the CLI recovered from — the error, then a success — is
    not full, and neither is one whose only such error was a sub-agent's."""
    if not isinstance(msg, ResultMessage) or not msg.is_error:
        return False
    return said or _too_long(msg.result) or any(_too_long(e) for e in (msg.errors or []))


def _as_interrupted(held: Event | None) -> Event | None:
    """The owner's Stop landed: the turn finishes as interrupted, whatever
    the result it holds said."""
    if held is None:
        return None
    return Event(held.kind, held.thread_id, {**held.data, "stop": "interrupted"})


def _content(message: UserMessage) -> Any:
    """A message's user content: its text (a skill's directive applied) as a
    plain string, or text and image blocks when it carries an image."""
    text = message.text
    if message.skill:
        from ..commands import skill_directive

        text = skill_directive(message.skill, message.text)
    if not message.images:
        return text
    content: list[dict] = []
    if text:
        content.append({"type": "text", "text": text})
    for image in message.images:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": image.get("mime") or "image/png",
                    "data": image.get("b64") or "",
                },
            }
        )
    return content


@dataclass
class _Raised:
    """An exception the reading pump caught, carried to the reader to raise."""

    exc: BaseException


_END = object()   # the client's stream ended
_CLOSED = object()   # the session closed: the watcher's feed ends (SV-6)


def _of_no_turn(item: Any) -> bool:
    """A message that belongs to no CLI turn: neither words nor a turn's start
    or end (`rate_limit_event`, `conversation_reset`, and any kind a newer SDK
    adds). Only the turn-shaped kinds — user, assistant, stream event,
    result — and system messages take part in deciding whose turn is under
    way; this one is read wherever it comes, by `_translate`, and `_read`
    gives it no part in ownership either — though, as any frame does, it
    still counts as the CLI's sign of life during a steer hold (it extends
    `hold_quiet`). Between turns the idle reader passes it by only when
    nothing is held: behind a held message it is held too, in order (a
    `conversation_reset` must not zero `cost_seen` ahead of a held result)."""
    return not isinstance(item, (SdkUserMessage, AssistantMessage, StreamEvent,
                                 ResultMessage, SystemMessage))


# Kinds of no turn the provider knows and expects between turns. Any other
# (a Message class a newer SDK adds: `claude-agent-sdk` is not pinned) is
# still passed by, and named once in the journal, so a kind that should have
# been read is not dropped unseen.
_KNOWN_NO_TURN = (RateLimitEvent, ConversationResetMessage)
_NO_TURN_NAMED: set = set()


def _note_no_turn_kind(session: Any, item: Any) -> None:
    """Log, once per process and kind, a message of no turn of a kind the
    provider does not know (its type name only, never its content)."""
    if isinstance(item, SystemMessage) or isinstance(item, _KNOWN_NO_TURN):
        return
    kind = type(item).__name__
    if kind in _NO_TURN_NAMED:
        return
    _NO_TURN_NAMED.add(kind)
    LOG.info("claude %s: passed by a %s read between turns (a kind this provider "
             "does not know)", session.thread_id, kind)


def _subagent_message(item: Any) -> bool:
    """A sub-agent's message (tagged with `parent_tool_use_id`) or a task
    message (`task_*`): what the idle reader hands the watcher at once."""
    if isinstance(item, (AssistantMessage, SdkUserMessage, StreamEvent)):
        return bool(getattr(item, "parent_tool_use_id", None))
    if isinstance(item, SystemMessage):
        return str(getattr(item, "subtype", "") or "").startswith("task_")
    return False


class _Pump:
    """The one reader of a client's message stream, on the session's loop.

    It reads one message per request and never ahead: the stream advances only
    when the turn asks for the next message, exactly as iterating it directly
    did, so nothing the SDK runs while producing a message (a hook, in a test
    double) moves relative to the events before it. A turn that stops waiting
    (a hold ending) leaves its request outstanding, and the next turn takes
    the answer: a wait is timed on our own queue, never by cancelling a read
    the SDK is in the middle of, so nothing the CLI says is lost between turns.
    Cancelled only when its client goes away.
    """

    def __init__(self, client: Any):
        self.client = client
        self.requests: asyncio.Queue = asyncio.Queue()
        self.items: asyncio.Queue = asyncio.Queue()
        self.asked = False
        self.ended = False
        self.task = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        stream = self.client.receive_messages().__aiter__()
        while True:
            await self.requests.get()
            try:
                item = await stream.__anext__()
            except StopAsyncIteration:
                self.items.put_nowait(_END)
                return
            except Exception as exc:  # noqa: BLE001 — handed to the reader to raise
                self.items.put_nowait(_Raised(exc))
                return
            self.items.put_nowait(item)

    async def next(self, timeout: float | None) -> Any:
        """The next message; `asyncio.TimeoutError` after `timeout` seconds,
        with the request left standing for whoever asks next."""
        if not self.asked:
            self.requests.put_nowait(None)
            self.asked = True
        item = await asyncio.wait_for(self.items.get(), timeout)
        self.asked = False
        if item is _END or isinstance(item, _Raised):
            self.ended = True
        return item


def _delta_text(event: Any) -> str:
    """The text of a raw Anthropic stream event, or "" if it carries none."""
    if not isinstance(event, dict):
        return ""
    if event.get("type") != "content_block_delta":
        return ""
    delta = event.get("delta")
    if not isinstance(delta, dict) or delta.get("type") != "text_delta":
        return ""
    return _safe(delta.get("text") or "")


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


_ABORTED = ("aborted_streaming", "aborted_tools")


def _stop_reason(session: _Session, msg: ResultMessage) -> str:
    terminal = msg.terminal_reason
    if session.interrupted or terminal in _ABORTED:
        return "interrupted"
    if msg.subtype == "error_max_turns" or terminal == "max_turns":
        return "max_turns"
    if msg.is_error:
        return "error"
    return "end"


def _fatal_message(session: _Session, exc: BaseException) -> str:
    """What the surface is told when the SDK raises.

    The child's stderr is the only place a real diagnosis usually lives, so a
    bounded tail of it rides along — scrubbed, like everything else here. It is
    never emitted on its own: stderr from a process holding the owner's login
    is not a stream to broadcast.
    """
    message = f"{type(exc).__name__}: {_safe(exc)}"
    tail = _safe("".join(session.stderr)).strip()
    if tail:
        message = f"{message}\n{tail[-1000:]}"
    return message
