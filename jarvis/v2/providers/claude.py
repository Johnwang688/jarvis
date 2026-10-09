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
passed through unchanged as an **equivalent** figure; what it means is the
ledger's decision (§8.3), not this provider's.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import subprocess
import threading
import queue
import uuid
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
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
from ..model import PermissionProfile, ProviderName, Thread
from ..provider import (
    Brief,
    BriefRefused,
    Decision,
    Event,
    EventKind,
    PermissionCallback,
    SessionHandle,
    SessionLost,
    SteerRefused,
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

# How much of a tool result goes into TOOL_FINISHED.summary. A surface renders
# this in a ticker; the transcript already holds the whole thing.
SUMMARY_CHARS = 200

# Kept only to enrich a fatal error. Never emitted on its own, always scrubbed.
STDERR_LINES = 40

# The exact text the CLI puts in a tool result when the auto-mode classifier
# refuses a call — §6.1's `reviewer_declined` trigger. Verified 2026-09-15
# against the installed binary (2.1.273), which holds it as a single template
# constant; the classifier's own words follow "Reason: ".
CLASSIFIER_DENIAL_PREFIX = (
    "Permission for this action was denied by the Claude Code auto mode classifier"
)
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

# Anything that looks like a bearer credential, redacted before a string can
# reach an event. `secrets.scrub` covers the files Jarvis owns; the CLI's own
# OAuth bundle (`~/.claude/.credentials.json`) is not one of them, and this
# provider is the first thing in the codebase whose child process holds it.
_TOKENISH = re.compile(r"\b(?:sk|oat|rt)[-_][A-Za-z0-9_\-]{12,}", re.IGNORECASE)
_JWTISH = re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]+")
_REDACTED = "[redacted]"


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
    return _JWTISH.sub(_REDACTED, out)


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
    usage: Usage = field(default_factory=Usage)
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

    def emit(self, kind: EventKind, **data: Any) -> None:
        """Put an event on the turn's queue. Silent outside a turn by design.

        A hook can only fire inside a turn, so `events is None` means the
        consumer already stopped reading — dropping is right, blocking is not.
        """
        events = self.events
        if events is not None:
            events.put(Event(kind, self.thread_id, data))


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

    def start(self, thread: Thread, brief: Brief, permit: PermissionCallback) -> SessionHandle:
        return self._open(thread, brief, permit, resume=False)

    def resume(self, thread: Thread, brief: Brief, permit: PermissionCallback) -> SessionHandle:
        if not thread.provider_session_id:
            raise BriefRefused("Claude resume needs the thread's provider_session_id")
        return self._open(thread, brief, permit, resume=True)

    def _open(
        self, thread: Thread, brief: Brief, permit: PermissionCallback, *, resume: bool
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
        )
        options = self._options(brief, session, resume=resume, session_id=session_id)
        try:
            session.client = _client_factory(options)
            _await(loop, session.client.connect(), CONNECT_TIMEOUT_S)
        except BaseException as exc:  # noqa: BLE001 — a failed start must not leak a thread
            _stop_loop(loop, runner)
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
            "setting_sources": list(SETTING_SOURCES),
            "permission_mode": "auto" if auto else "default",
            "include_partial_messages": True,
            "strict_mcp_config": True,
            "mcp_servers": dict(brief.mcp_servers),
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
        """

        async def pre_tool(input_data, tool_use_id, context):  # noqa: ANN001
            data = input_data or {}
            name = str(data.get("tool_name") or "")
            args = data.get("tool_input")
            args = dict(args) if isinstance(args, dict) else {}
            decision = await self._decide(session, name, args)
            if decision is Decision.ALLOW and session.brief.profile is PermissionProfile.AUTO:
                return {}
            if decision is Decision.ALLOW:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                        "permissionDecisionReason": "approved by Jarvis",
                    }
                }
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    # The model reads this, so it says what happened rather
                    # than only that something did.
                    "permissionDecisionReason": _safe(
                        f"Jarvis denied {name}. The owner's rules or the owner "
                        "refused this call; do not retry it or work around it."
                    ),
                }
            }

        return pre_tool

    def _can_use_tool(self, session: _Session):
        async def can_use_tool(tool_name, input_data, context):  # noqa: ANN001
            args = dict(input_data) if isinstance(input_data, dict) else {}
            decision = await self._decide(session, str(tool_name or ""), args)
            if decision is Decision.ALLOW:
                return PermissionResultAllow()
            return PermissionResultDeny(message=_safe("Jarvis denied this call"))

        return can_use_tool

    async def _decide(self, session: _Session, name: str, args: dict) -> Decision:
        """Ask `permit`, announcing the question so a stalled turn is visible.

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
        session.emit(
            EventKind.APPROVAL_REQUESTED,
            req_id=req_id,
            tool=name,
            args=args,
            command=_command(args),
        )
        try:
            decision = await self._race(session, pending, name, args)
        finally:
            with session.mutex:
                session.pending.pop(req_id, None)
        session.emit(EventKind.APPROVAL_RESOLVED, req_id=req_id, decision=decision.value)
        return decision

    async def _race(
        self, session: _Session, pending: _Pending, name: str, args: dict
    ) -> Decision:
        def ask() -> Decision:
            try:
                return Decision(session.permit(name, dict(args), session.brief))
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
        """Resolve a question this session is blocked on, out of band."""
        session = _native(h)
        with session.mutex:
            pending = session.pending.get(req_id)
            if pending is None or pending.ready.is_set():
                raise ValueError(f"Unknown or resolved Claude request id: {req_id}")
            if isinstance(decision, str) and not isinstance(decision, Decision):
                decision = Decision(decision)
            pending.value = Decision(decision)
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
            session.sending.release()

    def _run(self, h: SessionHandle, session: _Session, message: UserMessage) -> Iterator[Event]:
        events: queue.Queue = queue.Queue()
        session.events = events
        session.interrupted = False
        session.tools_seen.clear()
        with session.mutex:
            session.accepting = False
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
                await session.client.query(_prompt(message, prompt_uid, later=behind))
                await self._read(session, events, prompt_uid)
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
            raise SteerRefused("session closed")
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
                raise SteerRefused("no turn of this message's is under way")
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

    async def _read(self, session: _Session, events: queue.Queue, prompt_uid: str) -> None:
        """Read this message's turn off the CLI, steers included.

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
          steer never shifts every later send by one (review of PR #22).

        When this turn's result arrives with one of its steers not yet seen to
        drain, the turn holds while the CLI shows any sign of life, for its
        fresh turn: STEER_QUIET_S of silence or STEER_HOLD_MAX_S in all end
        the hold, and the steer then counts as folded in silently — or, if its
        turn is merely late, as the next send's leftover. A CLI that never
        echoes leaves nothing to tell apart: every turn is the message's own,
        every unseen steer is taken as folded, as before.

        Reads go through the session's pump, one message per request, never
        cancelled mid-read: a hold that ends leaves its read outstanding for
        the next turn, and nothing the CLI said is ever lost.
        """
        loop = asyncio.get_running_loop()
        generation = session.generation
        started = False          # the message's own CLI turn has begun
        owner = None             # who opened the CLI turn under way (None: none is)
        held = None              # the TURN_FINISHED this send hands back
        candidate = None         # a result that was a leftover's, or this message's
        candidate_until = None
        hold_began = hold_quiet = None
        restarted = False
        while True:
            timeout = None
            if hold_began is not None:
                timeout = max(0.0, min(hold_quiet, hold_began + STEER_HOLD_MAX_S) - loop.time())
            elif candidate_until is not None:
                timeout = max(0.0, candidate_until - loop.time())
            pump = self._pump(session)
            try:
                item = await pump.next(timeout)
            except asyncio.TimeoutError:
                if hold_began is None and candidate is not None:
                    # The message's own start never showed: the CLI did not
                    # echo it, so the result taken for a leftover's was its.
                    held = candidate
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
                with session.mutex:
                    session.prompt_echo = True
                    # Every `next` steer drains before a message that starts a
                    # turn: an earlier one still unseen now was folded silently.
                    for other, written in list(session.steering.items()):
                        if written < generation:
                            del session.steering[other]
                    session.accepting = True
                await self._stop_if_interrupted(session)
                continue
            if uid is not None:
                with session.mutex:
                    written = session.steering.pop(uid, None)
                if written is not None:
                    if owner is None:
                        # Not folded: the steer starts a CLI turn of its own.
                        owner = "steer" if written == generation else "leftover"
                        hold_began = None
                        await self._stop_if_interrupted(session)
                    continue
            if owner is None and isinstance(msg, (AssistantMessage, StreamEvent)):
                owner = "unknown"                  # a turn whose start was not echoed
                hold_began = None
                if not started and not self._leftover_possible(session, generation):
                    started = True
                    with session.mutex:
                        session.accepting = True
                await self._stop_if_interrupted(session)
            finished = self._take(session, events, msg)
            if finished is None:
                continue
            ended, owner = owner, None
            if not started:
                if ended == "leftover":
                    continue                       # the message's own turn is still to come
                if self._leftover_possible(session, generation):
                    candidate, candidate_until = finished, loop.time() + LEFTOVER_WAIT_S
                    continue
                started = True                     # nothing else could have run
            held = finished
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
        if held is not None:
            events.put(held)

    async def _stop_if_interrupted(self, session: _Session) -> None:
        """A CLI turn has just begun after the owner pressed stop (a steer the
        CLI held, or the message itself queued behind a leftover). Stop means
        stop: that turn is interrupted too, and read to its end."""
        if session.interrupted:
            with contextlib.suppress(Exception):
                await session.client.interrupt()

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
        """One SDK message becomes zero or more §5.2 events."""
        out: list[Event] = []
        if isinstance(msg, SystemMessage):
            self._note_session_id(session, (msg.data or {}).get("session_id"))
            return out
        if isinstance(msg, StreamEvent):
            self._note_session_id(session, msg.session_id)
            text = _delta_text(msg.event)
            if text:
                out.append(Event(EventKind.TEXT_DELTA, session.thread_id, {"text": text}))
            return out
        if isinstance(msg, AssistantMessage):
            self._note_session_id(session, msg.session_id)
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
                    session.tools_seen[block.id] = (block.name, args)
                    out.append(
                        Event(
                            EventKind.TOOL_STARTED,
                            session.thread_id,
                            {"call_id": block.id, "name": block.name, "args": args},
                        )
                    )
            return out
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
        return out

    def _tool_result(self, session: _Session, block: Any) -> list[Event]:
        if not isinstance(block, ToolResultBlock):
            return []
        name, args = session.tools_seen.get(block.tool_use_id, ("", {}))
        text = _safe(_result_text(block.content))
        declined = text.startswith(CLASSIFIER_DENIAL_PREFIX)
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
                        "args": args,
                        "command": _command(args),
                        "reason": match.group("reason").strip()
                        if match
                        else text[:SUMMARY_CHARS],
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
                    "ok": not bool(block.is_error) and not declined,
                    "summary": text[:SUMMARY_CHARS],
                },
            )
        )
        return out

    def _result(self, session: _Session, msg: ResultMessage) -> list[Event]:
        reported = dict(msg.usage or {})
        cost = msg.total_cost_usd
        turn = Usage(
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
            total.cost_usd = (total.cost_usd or 0.0) + float(cost)
        return [
            Event(
                EventKind.USAGE,
                session.thread_id,
                {
                    "input": turn.input_tokens,
                    "output": turn.output_tokens,
                    "cached": turn.cached_tokens,
                    # An *equivalent* figure on a subscription, where nothing
                    # is billed (R1). Passed through unchanged; what it means
                    # is the ledger's call, not this provider's.
                    "cost_usd": cost,
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
            # CLI process still held: a steer queued there dies with it.
            self._stop_pump(session)
            with session.mutex:
                session.steering.clear()
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
        session.closed = True
        with session.mutex:
            for pending in session.pending.values():
                if not pending.ready.is_set():
                    pending.value = Decision.DENY
                    pending.ready.set()
            session.pending.clear()
            session.steering.clear()
        self._stop_pump(session)
        try:
            _await(session.loop, session.client.disconnect(), CONTROL_TIMEOUT_S)
        except Exception:  # noqa: BLE001 — close must not raise
            pass
        _stop_loop(session.loop, session.runner)


# --- helpers ----------------------------------------------------------------


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
