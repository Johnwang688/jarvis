"""Checks for the v2 ClaudeProvider. Free — no `claude` process, no tokens.

The whole suite runs against a fake client injected through
`claude._client_factory`, scripted with the **real** SDK dataclasses from the
installed `claude-agent-sdk`. Constructing the genuine types is the point: a
hand-rolled stand-in would keep passing after the SDK renamed a field, which is
the failure this provider is most exposed to — it is a translation layer and
almost nothing else.

Four things are under test, in descending order of how badly they fail:

  **The gate (§6, R2).** Under `auto` the `PreToolUse` hook is the only thing
  that sees every tool call, so a hook that does not fire, does not reach
  `permit`, or reports an allow where the owner said no is the whole permission
  system quietly not working. `allowed_tools` must stay empty forever, because
  an entry there is auto-approved *before* any callback (R1).

  **The escape hatch (§6.1).** A classifier refusal has to come out as
  `REVIEWER_DECLINED` carrying the exact command, *and* as a
  `TOOL_FINISHED{ok: False}` — a refused call must never render as one that
  happened.

  **The event stream (§5.2).** A surface must not be able to tell which
  provider produced an event, so the order and the `data` keys are asserted
  exactly, and the generator must terminate on every path including a raise.

  **The credential rule.** The child process holds the owner's login. No
  event, reason or exception string may carry a credential value — checked by
  grepping every string the provider produced for a planted fake token.

Run:  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python tests/v2/claude_provider_check.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    PermissionResultAllow,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from claude_agent_sdk import UserMessage as SdkUserMessage  # noqa: E402

from jarvis.v2.model import PermissionProfile, ProviderName, Role, Thread  # noqa: E402
from jarvis.v2.provider import (  # noqa: E402
    Brief,
    BriefRefused,
    Decision,
    EventKind,
    SessionLost,
    UserMessage,
)
from jarvis.v2.providers import claude  # noqa: E402

FAKE_TOKEN = "sk-ant-oat01-FAKEFAKEFAKEFAKEFAKEFAKEFAKE0123456789"
SESSION_ID = "11111111-2222-3333-4444-555555555555"

_failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"ok  {label}")
    else:
        print(f"FAIL {label}")
        _failures.append(label)


def eq(got, want, label: str) -> None:
    check(got == want, f"{label} (got {got!r}, want {want!r})")


# --- the fake client --------------------------------------------------------


class Hook:
    """A scripted point where the CLI would call the PreToolUse hook."""

    def __init__(self, tool_name: str, tool_input: dict, tool_use_id: str = "call-1"):
        self.tool_name = tool_name
        self.tool_input = tool_input
        self.tool_use_id = tool_use_id


class Permission:
    """A scripted point where the CLI would call `can_use_tool`."""

    def __init__(self, tool_name: str, tool_input: dict):
        self.tool_name = tool_name
        self.tool_input = tool_input


class Raise:
    def __init__(self, exc: BaseException):
        self.exc = exc


class Pause:
    """Block the scripted stream until `event` is set (used for interrupt)."""

    def __init__(self, event: threading.Event):
        self.event = event


class FakeClient:
    """Everything `ClaudeSDKClient` is to this provider, and nothing more."""

    instances: list["FakeClient"] = []

    def __init__(self, options, script=None):
        self.options = options
        self.script = list(script or [])
        self.queries: list = []
        self.hook_results: list = []
        self.permission_results: list = []
        self.connected = False
        self.interrupts = 0
        self.disconnects = 0
        FakeClient.instances.append(self)

    async def connect(self):
        self.connected = True

    async def query(self, prompt, session_id: str = "default"):
        if hasattr(prompt, "__aiter__"):
            self.queries.append([msg async for msg in prompt])
        else:
            self.queries.append(prompt)

    async def interrupt(self):
        self.interrupts += 1

    async def disconnect(self):
        self.disconnects += 1
        self.connected = False

    async def receive_response(self):
        script, self.script = self.script, []
        for item in script:
            if isinstance(item, Raise):
                raise item.exc
            if isinstance(item, Pause):
                # Polled, not waited on in an executor thread: a parked
                # non-daemon thread is joined at interpreter exit, and a suite
                # that hangs on shutdown is a suite nobody runs.
                while not item.event.is_set():
                    await asyncio.sleep(0.02)
                continue
            if isinstance(item, Hook):
                matcher = self.options.hooks["PreToolUse"][0]
                data = {
                    "hook_event_name": "PreToolUse",
                    "tool_name": item.tool_name,
                    "tool_input": item.tool_input,
                    "tool_use_id": item.tool_use_id,
                }
                self.hook_results.append(
                    await matcher.hooks[0](data, item.tool_use_id, {"signal": None})
                )
                continue
            if isinstance(item, Permission):
                self.permission_results.append(
                    await self.options.can_use_tool(
                        item.tool_name, item.tool_input, {"signal": None}
                    )
                )
                continue
            yield item

    async def receive_messages(self):
        """What the provider reads (one reader per client, across turns): the
        script, then the end of the stream. A new reader takes a new script."""
        async for item in self.receive_response():
            yield item


@contextlib.contextmanager
def fake(script=None):
    """Install the fake factory for the duration of a check."""
    original = claude._client_factory
    FakeClient.instances = []
    claude._client_factory = lambda options: FakeClient(options, script)
    try:
        yield
    finally:
        claude._client_factory = original


def thread(thread_id: str = "t1", session_id: str | None = None) -> Thread:
    return Thread(
        id=thread_id,
        project_id="p1",
        role=Role.IMPLEMENTER,
        provider=ProviderName.CLAUDE,
        provider_session_id=session_id,
    )


def brief(**kw) -> Brief:
    kw.setdefault("role", Role.IMPLEMENTER)
    kw.setdefault("cwd", "/tmp/jarvis-wp3")
    return Brief(**kw)


def allow(*_a, **_k) -> Decision:
    return Decision.ALLOW


def result_message(**kw) -> ResultMessage:
    base = dict(
        subtype="success",
        duration_ms=10,
        duration_api_ms=8,
        is_error=False,
        num_turns=1,
        session_id=SESSION_ID,
        total_cost_usd=0.25,
        usage={
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 40,
        },
    )
    base.update(kw)
    return ResultMessage(**base)


def prompt_texts(client) -> list:
    """The text of every turn's own message the client was given, in order
    (steers, which carry priority `next`, left out)."""
    out = []
    for q in client.queries:
        if isinstance(q, list) and q and isinstance(q[0], dict):
            if q[0].get("priority") != "next":
                out.append(q[0]["message"]["content"])
        else:
            out.append(q)
    return out


def drain(provider, handle, text: str = "hello") -> list:
    return list(provider.send(handle, UserMessage(text=text)))


def kinds(events) -> list[str]:
    return [e.kind.value for e in events]


def strings(events) -> str:
    """Every string any event carries, for the credential grep."""
    return json.dumps([[e.kind.value, e.data] for e in events], default=str)


# --- health -----------------------------------------------------------------


@contextlib.contextmanager
def credentials(payload):
    """Point the provider at a throwaway credential file, never the owner's."""
    with tempfile.TemporaryDirectory(prefix="jarvis-wp3-creds-") as tmp:
        path = Path(tmp) / ".credentials.json"
        if payload is not None:
            path.write_text(json.dumps(payload))
        previous = os.environ.get("JARVIS_CLAUDE_CREDENTIALS")
        os.environ["JARVIS_CLAUDE_CREDENTIALS"] = str(path)
        try:
            yield path
        finally:
            if previous is None:
                os.environ.pop("JARVIS_CLAUDE_CREDENTIALS", None)
            else:
                os.environ["JARVIS_CLAUDE_CREDENTIALS"] = previous


@contextlib.contextmanager
def cli(version: str | None, *, on_path: bool = True, returncode: int = 0,
        bundled: str | None = None):
    """A fake `claude` binary: no process is ever spawned.

    `on_path=False` means the resolver found nothing, so the SDK's bundled CLI
    would run; `bundled` is the version that CLI reports (None: not even that).
    """
    find, run, bundled_version = claude.find_cli, claude.subprocess.run, claude.bundled_version
    claude.find_cli = lambda **kw: ("/fake/bin/claude", "path") if on_path else (None, "bundled")
    claude.bundled_version = lambda: bundled

    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(argv, returncode, f"{version} (Claude Code)\n", "")

    claude.subprocess.run = fake_run
    claude.reset_cli_cache()
    try:
        yield
    finally:
        claude.find_cli = find
        claude.bundled_version = bundled_version
        claude.subprocess.run = run
        claude.reset_cli_cache()


def health_checks() -> None:
    print("\n-- health (no process, throwaway credentials)")
    provider = claude.ClaudeProvider()
    future = int((time.time() + 86_400) * 1000)
    past = int((time.time() - 86_400) * 1000)

    with cli("2.1.290", on_path=False), credentials({"claudeAiOauth": {"expiresAt": future}}):
        ok, reason = provider.health()
        check(not ok and "PATH" in reason, f"missing binary is unavailable: {reason}")

    with cli("2.1.290", on_path=False, bundled="2.1.273"), \
            credentials({"claudeAiOauth": {"expiresAt": future}}):
        ok, reason = provider.health()
        check(not ok and "2.1.273" in reason and "bundled" in reason and "older" in reason,
              f"with no install, the SDK's bundled 2.1.273 is named and is below the floor "
              f"(it refuses claude-opus-5-5): {reason}")
    check(claude.parse_version(claude.CLAUDE_MIN) >= (2, 1, 280),
          "the floor is at least 2.1.280, the default model's stated requirement")

    with cli("1.9.0"), credentials({"claudeAiOauth": {"expiresAt": future}}):
        ok, reason = provider.health()
        check(
            not ok and claude.CLAUDE_MIN in reason and "1.9.0" in reason and "older" in reason,
            f"an older version is refused, naming both: {reason}",
        )

    with cli("2.1.290"), credentials({"claudeAiOauth": {"expiresAt": future}}):
        ok, reason = provider.health()
        check(ok and "2.1.290" in reason, f"a version between floor and verified is ok: {reason}")

    with cli("2.1.290"), credentials(
        {"claudeAiOauth": {"expiresAt": past, "refreshTokenExpiresAt": past}}
    ):
        ok, reason = provider.health()
        check(not ok and "expired" in reason, f"expired login is unavailable: {reason}")

    with cli("2.1.290"), credentials(
        # The access token lapses every hour and is refreshed silently; the
        # grant is what "logged in" means. A health check that cried wolf
        # hourly would be one nobody reads.
        {"claudeAiOauth": {"expiresAt": past, "refreshTokenExpiresAt": future}}
    ):
        ok, reason = provider.health()
        check(ok, f"a lapsed access token is not an expired login: {reason}")

    with cli("2.1.290"), credentials({"claudeAiOauth": {"subscriptionType": "max"}}):
        ok, reason = provider.health()
        check(ok and "unknown" in reason, f"absent expiry is unknown, not refused: {reason}")

    with cli("2.1.290"), credentials(None):
        ok, reason = provider.health()
        check(not ok and "login" in reason, f"no bundle at all is unavailable: {reason}")

    with cli("2.1.290"), credentials({"claudeAiOauth": {"expiresAt": future}}):
        ok, _ = provider.health()
        check(ok, "binary + pin + live login is available")

    # Never spends tokens: the only subprocess is `--version`.
    seen: list = []
    with cli("2.1.290"), credentials({"claudeAiOauth": {"expiresAt": future}}):
        run = claude.subprocess.run

        def spy(argv, **kw):
            seen.append(list(argv))
            return run(argv, **kw)

        claude.subprocess.run = spy
        provider.health()
        provider.health()
        provider.health()
    check(
        len(seen) == 1 and seen[0][1:] == ["--version"],
        f"health spawns only `claude --version`, once however often /status asks: {seen}",
    )
    version_checks()


@contextlib.contextmanager
def strict(on: bool):
    previous = claude.config.CLAUDE_STRICT
    claude.config.CLAUDE_STRICT = on
    try:
        yield
    finally:
        claude.config.CLAUDE_STRICT = previous


def version_checks() -> None:
    print("\n-- the version is a floor, never a pin")
    provider = claude.ClaudeProvider()
    future = int((time.time() + 86_400) * 1000)
    login = {"claudeAiOauth": {"expiresAt": future}}

    eq(claude.parse_version("2.1.295"), (2, 1, 295), "parse: a plain version")
    eq(claude.parse_version("3.0.0-beta.1"), (3, 0, 0), "parse: a pre-release suffix is tolerated")
    check(claude.parse_version("2.1.10") > claude.parse_version("2.1.9"),
          "parse: compared as numbers, not strings")
    for junk in ("", "2.1", "claude", "v", "2.x.1"):
        check(claude.parse_version(junk) is None, f"parse: {junk!r} is not a version")
    check(claude.parse_version(claude.CLAUDE_MIN) <= claude.parse_version(claude.CLAUDE_VERIFIED),
          "the floor is not above the verified version")

    for newer in ("2.2.0", "3.0.0"):
        with cli(newer), credentials(login), strict(False), captured_log() as logs:
            first = provider.health()
            second = provider.health()
        warnings = [r.getMessage() for r in logs if r.levelno == logging.WARNING]
        check(first[0] and second[0] and newer in first[1],
              f"{newer} (newer than verified) is healthy: {first[1]}")
        check(len(warnings) == 1 and newer in warnings[0] and claude.CLAUDE_VERIFIED in warnings[0],
              f"{newer} warns exactly once per process: {warnings}")

    with cli(claude.CLAUDE_VERIFIED), credentials(login), strict(False), captured_log() as logs:
        ok, _ = provider.health()
    check(ok and not [r for r in logs if r.levelno == logging.WARNING],
          "the verified version itself is healthy and warns nothing")

    with cli("2.1.279"), credentials(login), strict(False):
        ok, reason = provider.health()
    check(not ok and "2.1.279" in reason and claude.CLAUDE_MIN in reason
          and "update Claude Code" in reason, f"one patch below the floor is refused: {reason}")

    # Numbers, not strings: a string comparison gets 2.1.1000 and 2.1.30 wrong.
    for version, healthy in (("2.1.1000", True), ("2.10.0", True), ("2.1.30", False)):
        with cli(version), credentials(login), strict(False):
            ok, reason = provider.health()
        check(ok is healthy, f"{version} is {'healthy' if healthy else 'refused'}: {reason}")

    with cli("garbage"), credentials(login), strict(False):
        ok, reason = provider.health()
    check(not ok and "could not read a version" in reason and "garbage" in reason,
          f"an unparseable --version is refused with a sentence: {reason}")

    with cli("2.2.0"), credentials(login), strict(True):
        ok, reason = provider.health()
    check(not ok and "JARVIS_CLAUDE_STRICT" in reason and "2.2.0" in reason,
          f"strict mode refuses a newer minor: {reason}")
    with cli("2.1.400"), credentials(login), strict(True):
        ok, reason = provider.health()
    check(ok, f"strict mode accepts the verified major.minor: {reason}")
    with cli("2.1.1"), credentials(login), strict(True):
        ok, reason = provider.health()
    check(not ok and "older" in reason, f"strict mode still enforces the floor: {reason}")

    # A failed probe is retried after the backoff, never cached as the answer.
    now = [1000.0]
    clock = claude._clock
    claude._clock = lambda: now[0]
    try:
        with cli("2.1.290", returncode=1), credentials(login), strict(False):
            calls: list = []
            failing = claude.subprocess.run

            def counted(argv, **kw):
                calls.append(list(argv))
                return failing(argv, **kw)

            claude.subprocess.run = counted
            ok, reason = provider.health()
            check(not ok and "--version" in reason and "retrying" in reason,
                  f"a failed probe is not-ok and says it will retry: {reason}")
            provider.health()
            now[0] += claude.PROBE_RETRY_S - 1
            provider.health()
            eq(len(calls), 1, "inside the backoff, a failed probe is not re-spawned")

            def recovered(argv, **kw):
                calls.append(list(argv))
                return subprocess.CompletedProcess(argv, 0, "2.1.290 (Claude Code)\n", "")

            claude.subprocess.run = recovered
            now[0] += 2
            ok, reason = provider.health()
            check(ok and "2.1.290" in reason, f"after the backoff the probe is retried and recovers: {reason}")
            provider.health()
            now[0] += 10 * claude.PROBE_RETRY_S
            provider.health()
            eq(len(calls), 2, "a successful probe stays cached")
    finally:
        claude._clock = clock


# --- which claude -------------------------------------------------------------

FAKE_CLI_SCRIPT = "#!/bin/sh\necho '0.0.0 (fake claude, never meant to run)'\n"


def fake_exe(path: Path, *, executable: bool = True) -> Path:
    """A stand-in `claude`. Never executed by the resolver, which only stats."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FAKE_CLI_SCRIPT)
    path.chmod(0o755 if executable else 0o644)
    return path


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@contextlib.contextmanager
def captured_log():
    handler, logger = Capture(), logging.getLogger(claude.__name__)
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def resolver_checks() -> None:
    print("\n-- which claude: the resolver (temp dirs, a fake PATH, nothing spawned)")
    spawned: list = []
    run = claude.subprocess.run
    claude.subprocess.run = lambda argv, **kw: spawned.append(list(argv)) or subprocess.CompletedProcess(argv, 1, "", "")
    previous_roots = claude._FOREIGN_ROOTS
    try:
        with tempfile.TemporaryDirectory(prefix="jarvis-cli-") as tmp:
            root = Path(tmp).resolve()
            # The Windows side of WSL, relocated so a test never touches /mnt.
            claude._FOREIGN_ROOTS = (str(root / "mnt"),)
            shim = fake_exe(root / "mnt" / "c" / "npm" / "claude")
            real = fake_exe(root / "usr" / "bin" / "claude")
            explicit = fake_exe(root / "opt" / "claude-explicit")
            home = root / "home"
            local = fake_exe(home / ".local" / "bin" / "claude")
            empty = root / "empty"
            empty.mkdir()
            path_env = os.pathsep.join([str(shim.parent), str(real.parent)])

            eq(claude.find_cli(configured=str(explicit), path_env=path_env, home=home),
               (str(explicit), "config"), "an explicit JARVIS_CLAUDE_CLI wins over PATH and ~/.local/bin")

            with captured_log() as logs:
                got = claude.find_cli(configured=str(root / "missing"), path_env=path_env, home=home)
            eq(got, (str(real), "path"), "an explicit path that is not there falls through")
            check(any(r.levelno == logging.WARNING and "JARVIS_CLAUDE_CLI" in r.getMessage() for r in logs),
                  "...and says it was ignored")
            noexec = fake_exe(root / "opt" / "claude-noexec", executable=False)
            eq(claude.find_cli(configured=str(noexec), path_env=path_env, home=home),
               (str(real), "path"), "an explicit path that is not executable falls through")

            # A relative JARVIS_CLAUDE_CLI is checked against the daemon's cwd
            # but would be spawned in the task worktree's: refused outright.
            cwd = os.getcwd()
            try:
                os.chdir(root)
                fake_exe(root / "bin" / "claude")
                with captured_log() as logs:
                    got = claude.find_cli(configured="bin/claude", path_env=path_env, home=home)
                eq(got, (str(real), "path"), "a relative JARVIS_CLAUDE_CLI is refused, even when it exists")
                check(any("not an absolute path" in r.getMessage() for r in logs),
                      "...with a warning saying why")
                got = claude.find_cli(configured="./bin/claude", path_env=str(empty), home=empty)
                eq(got, (None, "bundled"), "...including a ./ spelling")

                # A relative PATH entry is skipped even when it holds a real
                # executable `claude` under the cwd.
                eq(claude.find_cli(configured="", path_env=os.pathsep.join(["bin", str(empty)]), home=empty),
                   (None, "bundled"), "a relative PATH entry holding an executable claude is skipped")
            finally:
                os.chdir(cwd)

            # An explicit /mnt/ (or .cmd) path is the owner's call: used, with a warning.
            for odd in (shim, fake_exe(root / "opt" / "claude.cmd")):
                with captured_log() as logs:
                    got = claude.find_cli(configured=str(odd), path_env=path_env, home=home)
                eq(got, (str(odd), "config"), f"an explicit {odd.name} under {odd.parent.name}/ is used")
                check(any("Windows" in r.getMessage() for r in logs), "...with a warning")

            # An explicit symlink is kept as the link, never resolved: the
            # self-updater re-points it.
            link = root / "opt" / "claude-link"
            link.symlink_to(explicit)
            eq(claude.find_cli(configured=str(link), path_env=path_env, home=home),
               (str(link), "config"), "an explicit symlink is returned as the link, not its target")

            eq(claude.find_cli(configured="", path_env=path_env, home=home),
               (str(real), "path"), "a /mnt/ shim first on PATH is skipped for the real one behind it")
            eq(claude.find_cli(configured="", path_env=str(shim.parent), home=empty),
               (None, "bundled"), "a /mnt/ shim alone is never used")

            # A clean-looking PATH entry whose `claude` is a link into /mnt/.
            linkdir = root / "linkbin"
            linkdir.mkdir()
            (linkdir / "claude").symlink_to(shim)
            eq(claude.find_cli(configured="", path_env=os.pathsep.join([str(linkdir), str(real.parent)]),
                               home=home),
               (str(real), "path"), "a symlink into /mnt/ is skipped too")
            cmddir = root / "cmdbin"
            fake_exe(cmddir / "claude.cmd")
            (cmddir / "claude").symlink_to(cmddir / "claude.cmd")
            eq(claude.find_cli(configured="", path_env=str(cmddir), home=empty),
               (None, "bundled"), "a .cmd shim behind a `claude` name is skipped")

            plain = root / "plainbin"
            fake_exe(plain / "claude", executable=False)
            eq(claude.find_cli(configured="", path_env=os.pathsep.join(["relative/bin", str(plain)]),
                               home=home),
               (str(local), "local-bin"),
               "a non-executable claude and a relative PATH entry are skipped; ~/.local/bin is the fallback")
            eq(claude.find_cli(configured="", path_env="", home=home),
               (str(local), "local-bin"), "with nothing on PATH (a systemd unit), ~/.local/bin/claude is used")
            eq(claude.find_cli(configured="", path_env=str(empty), home=empty),
               (None, "bundled"), "with nothing anywhere, None: the SDK's bundled CLI")

            # resolve_cli: once per process, and one warning naming the bundled CLI.
            env_before = {k: os.environ.get(k) for k in ("PATH", "HOME")}
            cli_before = claude.config.CLAUDE_CLI
            try:
                os.environ["PATH"], os.environ["HOME"] = str(empty), str(empty)
                claude.config.CLAUDE_CLI = ""
                claude.reset_cli_cache()
                with captured_log() as logs:
                    first = claude.resolve_cli()
                    second = claude.resolve_cli()
                warnings = [r.getMessage() for r in logs if r.levelno == logging.WARNING]
                check(first is None and second is None, "resolve_cli: nothing found is None")
                bundled = claude.bundled_version()
                check(len(warnings) == 1 and "bundled" in warnings[0]
                      and (bundled is None or bundled in warnings[0]),
                      f"resolve_cli: exactly one warning, naming the bundled CLI {bundled}: {warnings}")
                info = claude.cli_info()
                eq((info["path"], info["version"], info["source"]), (None, bundled, "bundled"),
                   "cli_info: the bundled version comes from the SDK's constant")

                # Nothing found is not cached for life: installing Claude Code
                # later is picked up, throttled to RESOLVE_RETRY_S.
                now = [5000.0]
                clock = claude._clock
                claude._clock = lambda: now[0]
                calls = []
                find = claude.find_cli
                claude.find_cli = lambda **kw: calls.append(kw) or find(**kw)
                try:
                    claude.reset_cli_cache()
                    with captured_log() as logs:
                        claude.resolve_cli()
                        os.environ["PATH"] = path_env        # "installed" now
                        now[0] += claude.RESOLVE_RETRY_S - 1
                        inside = claude.resolve_cli()
                        now[0] += 2
                        after = claude.resolve_cli()
                        now[0] += 10 * claude.RESOLVE_RETRY_S
                        later = claude.resolve_cli()
                    warnings = [r for r in logs if r.levelno == logging.WARNING]
                finally:
                    claude._clock = clock
                    claude.find_cli = find
                    os.environ["PATH"] = str(empty)
                check(inside is None, "resolve_cli: inside the throttle, None is not re-searched")
                eq(after, str(real), "resolve_cli: after the throttle, a newly installed CLI is found")
                eq((later, len(calls)), (str(real), 2), "resolve_cli: once found, it is not searched again")
                eq(len(warnings), 1, "resolve_cli: the bundled fallback warns once, not per retry")

                os.environ["PATH"] = path_env
                claude.reset_cli_cache()
                calls: list = []
                find = claude.find_cli
                claude.find_cli = lambda **kw: calls.append(kw) or find(**kw)
                try:
                    for _ in range(3):
                        got = claude.resolve_cli()
                finally:
                    claude.find_cli = find
                eq((got, len(calls)), (str(real), 1), "resolve_cli: resolved once, then cached")

                claude.config.CLAUDE_CLI = str(explicit)
                claude.reset_cli_cache()
                eq(claude.resolve_cli(), str(explicit), "resolve_cli reads config.CLAUDE_CLI")
            finally:
                claude.config.CLAUDE_CLI = cli_before
                for key, value in env_before.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                claude.reset_cli_cache()
    finally:
        claude._FOREIGN_ROOTS = previous_roots
        claude.subprocess.run = run
    eq(spawned, [], "the resolver and the bundled path spawn nothing")


def probe_and_status_checks() -> None:
    print("\n-- the version probe, /status and the usage meter's User-Agent")
    from jarvis.v2.daemon import _cli_status

    # The probe cache is keyed on the real file: re-pointing the symlink (what
    # the self-updater does) is noticed. Nothing is executed — the fake `run`
    # reads the version out of whichever file the link points at.
    run = claude.subprocess.run

    def fake_run(argv, **kw):
        version = Path(os.path.realpath(argv[0])).read_text().split()[-1]
        return subprocess.CompletedProcess(argv, 0, f"{version} (Claude Code)\n", "")

    claude.subprocess.run = fake_run
    claude.reset_cli_cache()
    try:
        with tempfile.TemporaryDirectory(prefix="jarvis-cli-probe-") as tmp:
            root = Path(tmp)
            (root / "v1").write_text("#!/bin/sh\n# 2.1.290\n")
            (root / "v2").write_text("#!/bin/sh\n# 2.1.299\n")
            link = root / "claude"
            link.symlink_to(root / "v1")
            eq(claude._probe_version(str(link)), "2.1.290", "probe: the version of the file the link points at")
            link.unlink()
            link.symlink_to(root / "v2")
            eq(claude._probe_version(str(link)), "2.1.299",
               "probe: re-pointing the link (a self-update) is noticed — the cache is keyed on the realpath")
    finally:
        claude.subprocess.run = run
        claude.reset_cli_cache()

    provider = claude.ClaudeProvider()
    with cli("2.1.290"):
        eq(provider.cli_info(), {"path": "/fake/bin/claude", "version": "2.1.290", "source": "path"},
           "ClaudeProvider.cli_info reports the resolved CLI")
        eq(_cli_status(provider), {"path": "/fake/bin/claude", "version": "2.1.290"},
           "the daemon's _cli_status reads the real provider: path and version only")
        eq(claude.usage_agent_version(), "2.1.290", "the usage meter's User-Agent claims the CLI in use")
    with cli("2.1.290", on_path=False, bundled="2.1.273"):
        eq(_cli_status(provider), {"path": None, "version": "2.1.273"},
           "_cli_status: the bundled fallback reports no path and the bundled version")
    with cli("garbage"):
        eq(claude.usage_agent_version(), claude.CLAUDE_VERIFIED,
           "an unreadable version falls back to CLAUDE_VERIFIED in the User-Agent")


def cli_options_checks(cli_path: str) -> None:
    print("\n-- every client is built with the resolved cli_path")
    provider = claude.ClaudeProvider()
    with fake([result_message()]):
        handle = provider.start(thread(), brief(), allow)
        eq(str(FakeClient.instances[-1].options.cli_path), cli_path, "start: options carry cli_path")
        drain(provider, handle, "one")
        provider.set_model(handle, "claude-sonnet-5-5", "low")
        eq(str(FakeClient.instances[-1].options.cli_path), cli_path,
           "set_model: the reconnected client carries the same cli_path")
        provider.close(handle)
    with fake():
        handle = provider.resume(thread(session_id=SESSION_ID), brief(), allow)
        eq(str(FakeClient.instances[-1].options.cli_path), cli_path, "resume: options carry cli_path")
        provider.close(handle)
    with fake(), cli("2.1.290", on_path=False):
        handle = provider.start(thread(), brief(), allow)
        check(FakeClient.instances[-1].options.cli_path is None,
              "nothing found: cli_path is left unset, so the SDK uses its bundled CLI")
        provider.close(handle)


# --- options ----------------------------------------------------------------


def options_checks() -> None:
    print("\n-- options built from the brief")
    provider = claude.ClaudeProvider()

    with fake():
        handle = provider.start(
            thread(),
            brief(
                system_append="ROLE BRIEF",
                model="claude-opus-4-5",
                effort="high",
                max_turns=12,
                mcp_servers={"jarvis": {"type": "stdio", "command": "jarvis-mcp"}},
            ),
            allow,
        )
        options = FakeClient.instances[0].options
        eq(
            options.system_prompt,
            {"type": "preset", "preset": "claude_code", "append": "ROLE BRIEF"},
            "system prompt is the preset with the role brief appended",
        )
        eq(options.setting_sources, ["user", "project"], "setting sources load CLAUDE.md")
        eq(options.allowed_tools, [], "allowed_tools stays empty (R1: it bypasses the gate)")
        eq(options.permission_mode, "auto", "AUTO maps to permission_mode auto")
        check(options.can_use_tool is None, "AUTO installs no can_use_tool (R2: never consulted)")
        matchers = options.hooks["PreToolUse"]
        check(
            len(matchers) == 1 and matchers[0].matcher is None,
            "one PreToolUse matcher, matching every tool",
        )
        check(
            matchers[0].timeout >= 600,
            f"hook timeout is at least 600s (is {matchers[0].timeout})",
        )
        eq(options.cwd, "/tmp/jarvis-wp3", "cwd is the task worktree")
        eq(options.model, "claude-opus-4-5", "model comes from the brief")
        eq(options.effort, "high", "effort comes from the brief")
        eq(options.max_turns, 12, "max_turns comes from the brief")
        check(options.strict_mcp_config is True, "strict MCP config")
        eq(
            options.mcp_servers,
            {"jarvis": {"type": "stdio", "command": "jarvis-mcp"}},
            "MCP servers come from the brief",
        )
        check(options.include_partial_messages is True, "partial messages on, for TEXT_DELTA")
        eq(options.session_id, handle.provider_session_id, "start mints the session id it reports")
        check(options.resume is None, "start does not resume")
        provider.close(handle)

    with fake():
        handle = provider.start(thread(), brief(profile=PermissionProfile.ASK), allow)
        options = FakeClient.instances[0].options
        eq(options.permission_mode, "default", "ASK maps to permission_mode default")
        check(options.can_use_tool is not None, "ASK installs can_use_tool")
        check("PreToolUse" in (options.hooks or {}), "ASK keeps the hook: always-ask is layer 2")
        provider.close(handle)

    with fake():
        try:
            provider.start(thread(), brief(profile=PermissionProfile.STRICT), allow)
            check(False, "STRICT is refused")
        except BriefRefused as exc:
            check("strict" in str(exc).lower(), f"STRICT is refused: {exc}")

    with fake():
        try:
            provider.start(thread(), brief(effort="turbo"), allow)
            check(False, "an off-ladder effort is refused")
        except BriefRefused as exc:
            check("turbo" in str(exc), f"an off-ladder effort is refused: {exc}")

    with fake():
        handle = provider.start(thread(), brief(allowed_tools=["Bash", "Read"]), allow)
        options = FakeClient.instances[0].options
        eq(options.tools, ["Bash", "Read"], "a role toolset restricts `tools`, never `allowed_tools`")
        eq(options.allowed_tools, [], "...and still auto-approves nothing")
        provider.close(handle)

    with fake():
        handle = provider.resume(thread(session_id=SESSION_ID), brief(), allow)
        options = FakeClient.instances[0].options
        eq(options.resume, SESSION_ID, "resume passes the thread's session id")
        check(options.session_id is None, "resume does not also mint one")
        eq(handle.provider_session_id, SESSION_ID, "the handle keeps the resumed id")
        provider.close(handle)

    with fake():
        try:
            provider.resume(thread(session_id=None), brief(), allow)
            check(False, "resume without a session id is refused")
        except BriefRefused as exc:
            check("resume" in str(exc), f"resume without a session id is refused: {exc}")


# --- the event stream -------------------------------------------------------


def sequence_checks() -> None:
    print("\n-- the event sequence for one scripted turn")
    provider = claude.ClaudeProvider()
    script = [
        SystemMessage(subtype="init", data={"session_id": SESSION_ID}),
        StreamEvent(
            uuid="u1",
            session_id=SESSION_ID,
            event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}},
        ),
        StreamEvent(
            uuid="u2",
            session_id=SESSION_ID,
            event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "lo"}},
        ),
        StreamEvent(
            uuid="u3",
            session_id=SESSION_ID,
            event={"type": "message_start", "message": {}},
        ),
        AssistantMessage(
            content=[
                ThinkingBlock(thinking="weighing it up", signature="sig"),
                TextBlock(text="Hello"),
                ToolUseBlock(id="call-1", name="Bash", input={"command": "echo hi"}),
            ],
            model="claude-opus-4-5",
            session_id=SESSION_ID,
        ),
        SdkUserMessage(
            content=[ToolResultBlock(tool_use_id="call-1", content="hi\n", is_error=False)]
        ),
        result_message(),
    ]
    with fake(script):
        handle = provider.start(thread(), brief(), allow)
        events = drain(provider, handle)
        eq(
            kinds(events),
            [
                "turn_started",
                "text_delta",
                "text_delta",
                "thinking",
                "text",
                "tool_started",
                "tool_finished",
                "usage",
                "turn_finished",
            ],
            "the exact event sequence",
        )
        by_kind = {e.kind.value: e for e in events}
        deltas = [e.data["text"] for e in events if e.kind is EventKind.TEXT_DELTA]
        eq(deltas, ["Hel", "lo"], "deltas arrive in order and carry only their text")
        check(
            len(deltas) == 2,
            "a non-text stream event (message_start) produces nothing",
        )
        eq(by_kind["text"].data, {"text": "Hello"}, "TEXT is the finished block")
        eq(
            by_kind["tool_started"].data,
            {"call_id": "call-1", "name": "Bash", "args": {"command": "echo hi"}},
            "TOOL_STARTED keys match the sibling providers",
        )
        eq(
            by_kind["tool_finished"].data,
            # "hi", not "hi\n": every string goes through the scrub, and v1's
            # `_drop_attributed_lines` rejoins on newlines. A ticker summary
            # can lose a trailing newline; a credential may not survive one.
            {"call_id": "call-1", "name": "Bash", "ok": True, "summary": "hi"},
            "TOOL_FINISHED is keyed to the call id",
        )
        usage = by_kind["usage"].data
        eq(
            {k: usage[k] for k in ("input", "output", "cached", "cost_usd")},
            {"input": 100, "output": 20, "cached": 40, "cost_usd": 0.25},
            "USAGE carries the SDK's own figures",
        )
        eq(
            usage["provider_reported"]["cache_read_input_tokens"],
            40,
            "the raw usage dict rides along",
        )
        eq(by_kind["turn_finished"].data, {"stop": "end"}, "a clean turn ends")
        check(
            all(e.thread_id == "t1" for e in events),
            "every event carries the thread id",
        )
        sent = FakeClient.instances[0].queries
        eq(prompt_texts(FakeClient.instances[0]), ["hello"], "the turn's message is its text")
        check(len(sent) == 1 and len(str(sent[0][0].get("uuid") or "")) == 36
              and "priority" not in sent[0][0],
              "one stdin message, carrying the uuid its echo is matched by, at the CLI's "
              "default priority")
        provider.close(handle)

    print("\n-- tool results that did not succeed")
    with fake(
        [
            AssistantMessage(
                content=[ToolUseBlock(id="c2", name="Read", input={"file_path": "/nope"})],
                model="m",
                session_id=SESSION_ID,
            ),
            SdkUserMessage(
                content=[ToolResultBlock(tool_use_id="c2", content="ENOENT", is_error=True)]
            ),
            result_message(),
        ]
    ):
        handle = provider.start(thread(), brief(), allow)
        events = drain(provider, handle)
        finished = [e for e in events if e.kind is EventKind.TOOL_FINISHED][0]
        check(finished.data["ok"] is False, "an is_error result is not ok")
        provider.close(handle)

    print("\n-- images ride the turn as content blocks")
    with fake([result_message()]):
        handle = provider.start(thread(), brief(), allow)
        list(
            provider.send(
                handle,
                UserMessage(text="look", images=[{"b64": "QUJD", "mime": "image/png"}]),
            )
        )
        sent = FakeClient.instances[0].queries[0][0]
        eq(
            sent["message"]["content"],
            [
                {"type": "text", "text": "look"},
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
                },
            ],
            "text and image become one multimodal user message",
        )
        provider.close(handle)


# --- the gate ---------------------------------------------------------------


def hook_checks() -> None:
    print("\n-- the PreToolUse hook is the gate (§6, R2)")
    provider = claude.ClaudeProvider()
    seen: list = []

    def permit(name, args, b):
        seen.append((name, dict(args), b.profile))
        return Decision.ALLOW if name == "Read" else Decision.DENY

    script = [
        Hook("Read", {"file_path": "/x"}, "call-r"),
        Hook("Bash", {"command": "rm -rf /"}, "call-b"),
        result_message(),
    ]
    with fake(script):
        handle = provider.start(thread(), brief(), permit)
        events = drain(provider, handle)
        client = FakeClient.instances[0]
        eq(
            [(n, a) for n, a, _ in seen],
            [("Read", {"file_path": "/x"}), ("Bash", {"command": "rm -rf /"})],
            "permit sees the tool name and the tool input",
        )
        eq(seen[0][2], PermissionProfile.AUTO, "permit sees the brief")
        eq(client.hook_results[0], {}, "ALLOW under auto returns {} so the classifier still runs")
        denial = client.hook_results[1]["hookSpecificOutput"]
        eq(denial["hookEventName"], "PreToolUse", "the deny names its event")
        eq(denial["permissionDecision"], "deny", "DENY is an explicit deny")
        check(
            "Bash" in denial["permissionDecisionReason"],
            f"the model is told what was denied: {denial['permissionDecisionReason']}",
        )
        approval = [e for e in events if e.kind is EventKind.APPROVAL_REQUESTED]
        resolved = [e for e in events if e.kind is EventKind.APPROVAL_RESOLVED]
        eq(len(approval), 2, "each question is announced, so a stalled turn is visible")
        eq(
            [e.data["tool"] for e in approval], ["Read", "Bash"], "the announcement names the tool"
        )
        eq(
            approval[1].data["command"],
            "rm -rf /",
            "a shell call carries its whole command, never a summary",
        )
        eq([e.data["decision"] for e in resolved], ["allow", "deny"], "and each is resolved")
        eq(
            [e.data["req_id"] for e in approval],
            [e.data["req_id"] for e in resolved],
            "request and resolution share one id",
        )
        provider.close(handle)

    print("\n-- ASK: the hook allows explicitly, and can_use_tool backs it up")
    with fake([Hook("Bash", {"command": "ls"}), Permission("Bash", {"command": "ls"}), result_message()]):
        handle = provider.start(thread(), brief(profile=PermissionProfile.ASK), allow)
        drain(provider, handle)
        client = FakeClient.instances[0]
        decision = client.hook_results[0]["hookSpecificOutput"]
        eq(
            decision["permissionDecision"],
            "allow",
            "under ask layer 3 is skipped, so ALLOW is explicit",
        )
        check(
            isinstance(client.permission_results[0], PermissionResultAllow),
            "can_use_tool answers with the SDK's own allow type",
        )
        provider.close(handle)

    print("\n-- a raising permit denies")
    with fake([Hook("Bash", {"command": "ls"}), result_message()]):
        def boom(*_a, **_k):
            raise RuntimeError("broker unreachable")

        handle = provider.start(thread(), brief(), boom)
        drain(provider, handle)
        out = FakeClient.instances[0].hook_results[0]["hookSpecificOutput"]
        eq(out["permissionDecision"], "deny", "an unreachable broker denies, as everywhere")
        provider.close(handle)


def answer_checks() -> None:
    print("\n-- answer() resolves a blocked question out of band")
    provider = claude.ClaudeProvider()
    released = threading.Event()
    asked = threading.Event()

    def permit(name, args, b):
        asked.set()
        released.wait(10)
        return Decision.ALLOW

    with fake([Hook("Bash", {"command": "ls"}), result_message()]):
        handle = provider.start(thread(), brief(), permit)
        events = []
        stream = provider.send(handle, UserMessage(text="go"))
        events.append(next(stream))  # turn_started
        request = next(stream)  # approval_requested
        eq(request.kind.value, "approval_requested", "the question is announced before the wait")
        check(asked.wait(5), "permit is called on a worker thread, not the session's loop")
        provider.answer(handle, request.data["req_id"], Decision.DENY)
        rest = list(stream)
        released.set()
        resolved = [e for e in rest if e.kind is EventKind.APPROVAL_RESOLVED][0]
        eq(resolved.data["decision"], "deny", "the owner's out-of-band answer wins the race")
        out = FakeClient.instances[0].hook_results[0]["hookSpecificOutput"]
        eq(out["permissionDecision"], "deny", "...and reaches the hook")
        try:
            provider.answer(handle, request.data["req_id"], Decision.ALLOW)
            check(False, "a spent request id is dead")
        except ValueError:
            check(True, "a spent request id is dead")
        provider.close(handle)


# --- the escape hatch -------------------------------------------------------


def reviewer_declined_checks() -> None:
    print("\n-- a classifier refusal is the D7 escape hatch (§6.1)")
    provider = claude.ClaudeProvider()
    declined = (
        claude.CLASSIFIER_DENIAL_PREFIX
        + ". Reason: [Auto-Mode Bypass]. If you have other tasks that don't depend "
        "on this action, continue working on those."
    )
    script = [
        AssistantMessage(
            content=[ToolUseBlock(id="c9", name="Bash", input={"command": "vercel --prod"})],
            model="m",
            session_id=SESSION_ID,
        ),
        SdkUserMessage(
            content=[ToolResultBlock(tool_use_id="c9", content=declined, is_error=True)]
        ),
        result_message(),
    ]
    with fake(script):
        handle = provider.start(thread(), brief(), allow)
        events = drain(provider, handle)
        eq(
            kinds(events),
            ["turn_started", "tool_started", "reviewer_declined", "tool_finished", "usage", "turn_finished"],
            "REVIEWER_DECLINED arrives with the TOOL_FINISHED, never instead of it",
        )
        decline = [e for e in events if e.kind is EventKind.REVIEWER_DECLINED][0]
        eq(decline.data["tool"], "Bash", "the decline names the tool")
        eq(
            decline.data["command"],
            "vercel --prod",
            "the whole command travels with it — the owner must see it to approve it",
        )
        eq(decline.data["args"], {"command": "vercel --prod"}, "and its arguments")
        eq(decline.data["reason"], "[Auto-Mode Bypass]", "the classifier's own reason is extracted")
        finished = [e for e in events if e.kind is EventKind.TOOL_FINISHED][0]
        check(
            finished.data["ok"] is False,
            "a refused call must never render as one that happened",
        )
        provider.close(handle)

    print("\n-- an ordinary error is not a reviewer decline")
    with fake(
        [
            AssistantMessage(
                content=[ToolUseBlock(id="c9", name="Bash", input={"command": "false"})],
                model="m",
                session_id=SESSION_ID,
            ),
            SdkUserMessage(
                content=[ToolResultBlock(tool_use_id="c9", content="exit 1", is_error=True)]
            ),
            result_message(),
        ]
    ):
        handle = provider.start(thread(), brief(), allow)
        events = drain(provider, handle)
        check(
            not any(e.kind is EventKind.REVIEWER_DECLINED for e in events),
            "a failing command is not a policy decision",
        )
        provider.close(handle)


# --- control ----------------------------------------------------------------


def interrupt_checks() -> None:
    print("\n-- interrupt")
    provider = claude.ClaudeProvider()
    gate = threading.Event()
    with fake(
        [
            Pause(gate),
            result_message(subtype="success", is_error=False, terminal_reason="aborted_streaming"),
        ]
    ):
        handle = provider.start(thread(), brief(), allow)
        stream = provider.send(handle, UserMessage(text="go"))
        eq(next(stream).kind.value, "turn_started", "the turn starts")

        def cancel():
            time.sleep(0.05)
            provider.interrupt(handle)
            gate.set()

        threading.Thread(target=cancel, daemon=True).start()
        rest = list(stream)
        eq(rest[-1].data["stop"], "interrupted", "a cancelled turn finishes as interrupted")
        eq(FakeClient.instances[0].interrupts, 1, "the SDK's own interrupt is sent")
        provider.close(handle)

    print("\n-- stop reasons")
    for kw, want in (
        (dict(subtype="error_max_turns", is_error=True), "max_turns"),
        (dict(subtype="success", is_error=True), "error"),
        (dict(subtype="success", is_error=False, terminal_reason="max_turns"), "max_turns"),
        (dict(subtype="success", is_error=False), "end"),
    ):
        with fake([result_message(**kw)]):
            handle = provider.start(thread(), brief(), allow)
            events = drain(provider, handle)
            eq(events[-1].data["stop"], want, f"{kw} finishes as {want}")
            provider.close(handle)


def error_checks() -> None:
    print("\n-- a raising client")
    provider = claude.ClaudeProvider()
    with fake([Raise(RuntimeError("the CLI died"))]):
        handle = provider.start(thread(), brief(), allow)
        events = drain(provider, handle)
        eq(kinds(events), ["turn_started", "error"], "a raise becomes one fatal error and stops")
        check(events[-1].data["fatal"] is True, "the error is fatal")
        check("the CLI died" in events[-1].data["message"], "and says what happened")
        provider.close(handle)

    print("\n-- a closed session, and a second concurrent turn")
    with fake([result_message()]):
        handle = provider.start(thread(), brief(), allow)
        provider.close(handle)
        events = drain(provider, handle)
        eq(kinds(events), ["error"], "sending on a closed session is one fatal error")
        provider.close(handle)
        check(FakeClient.instances[0].disconnects == 1, "close is idempotent")

    gate = threading.Event()
    with fake([Pause(gate), result_message()]):
        handle = provider.start(thread(), brief(), allow)
        first = provider.send(handle, UserMessage(text="a"))
        next(first)
        second = list(provider.send(handle, UserMessage(text="b")))
        eq(kinds(second), ["error"], "a second concurrent turn on one handle is refused")
        # 2026-10-08: never fatal. A fatal error makes the daemon drop and
        # close the session, and the session here is the one still running.
        check(second[0].data.get("fatal") is False,
              "the refusal is not fatal, so it can never drop the running session")
        gate.set()
        rest = list(first)
        check(rest and rest[-1].kind is EventKind.TURN_FINISHED,
              "the running turn carries on to its own finish")
        provider.close(handle)

    print("\n-- a failed start leaves nothing running")
    before = threading.active_count()

    def explode(options):
        raise RuntimeError("no binary")

    original = claude._client_factory
    claude._client_factory = explode
    try:
        try:
            provider.start(thread(), brief(), allow)
            check(False, "a failed connect raises BriefRefused")
        except BriefRefused:
            check(True, "a failed connect raises BriefRefused")
    finally:
        claude._client_factory = original
    time.sleep(0.2)
    check(
        threading.active_count() <= before,
        f"...and leaks no loop thread ({before} -> {threading.active_count()})",
    )


def usage_checks() -> None:
    print("\n-- usage() sums the turns since start")
    provider = claude.ClaudeProvider()
    with fake():
        handle = provider.start(thread(), brief(), allow)
        client = FakeClient.instances[0]
        eq(provider.usage(handle).cost_usd, None, "a session with no turn has no cost")
        for _ in range(2):
            client.script = [result_message()]
            drain(provider, handle)
        total = provider.usage(handle)
        eq(total.input_tokens, 200, "input tokens sum")
        eq(total.output_tokens, 40, "output tokens sum")
        eq(total.cached_tokens, 80, "cached tokens sum")
        eq(round(total.cost_usd, 4), 0.5, "the equivalent cost sums")
        eq(total.work_tokens, (200 - 80) + 40, "work_tokens is §8.3's figure")
        provider.close(handle)

    with fake([result_message(total_cost_usd=None)]):
        handle = provider.start(thread(), brief(), allow)
        events = drain(provider, handle)
        usage = [e for e in events if e.kind is EventKind.USAGE][0]
        check(usage.data["cost_usd"] is None, "a missing cost stays None, never 0")
        provider.close(handle)


def isolation_checks() -> None:
    print("\n-- two handles are isolated")
    provider = claude.ClaudeProvider()
    seen: list = []
    with fake():
        one = provider.start(thread("t1"), brief(cwd="/tmp/one"), lambda n, a, b: seen.append(("one", n)) or Decision.ALLOW)
        two = provider.start(thread("t2"), brief(cwd="/tmp/two"), lambda n, a, b: seen.append(("two", n)) or Decision.ALLOW)
        check(one.provider_session_id != two.provider_session_id, "each gets its own session id")
        FakeClient.instances[0].script = [Hook("Read", {}), result_message()]
        FakeClient.instances[1].script = [result_message(usage={"input_tokens": 7, "output_tokens": 1})]
        first = drain(provider, one)
        second = drain(provider, two)
        eq(seen, [("one", "Read")], "one handle's hook does not fire on the other")
        check(
            all(e.thread_id == "t1" for e in first) and all(e.thread_id == "t2" for e in second),
            "events are attributed to their own thread",
        )
        eq(provider.usage(two).input_tokens, 7, "usage is per handle")
        eq(provider.usage(one).input_tokens, 100, "...on both sides")
        provider.close(one)
        provider.close(two)


def skill_checks() -> None:
    print("\n-- a named skill (S1) reaches Claude as a directive to use it")
    provider = claude.ClaudeProvider()
    with fake():
        handle = provider.start(thread("sk"), brief(), lambda n, a, b: Decision.ALLOW)
        FakeClient.instances[0].script = [result_message()]
        list(provider.send(handle, UserMessage(text="keep it short", skill="morning-briefing")))
        eq(prompt_texts(FakeClient.instances[0])[-1],
           'Use the "morning-briefing" skill for this request.\n\nkeep it short',
           "the prompt is the directive, then the request")
        FakeClient.instances[0].script = [result_message()]
        list(provider.send(handle, UserMessage(text="", skill="morning-briefing")))
        eq(prompt_texts(FakeClient.instances[0])[-1],
           'Use the "morning-briefing" skill for this request.', "no request: the directive alone")
        FakeClient.instances[0].script = [result_message()]
        list(provider.send(handle, UserMessage(text="plain")))
        eq(prompt_texts(FakeClient.instances[0])[-1], "plain", "no skill: the text, untouched")
        provider.close(handle)


# --- the credential rule ----------------------------------------------------


def secrets_checks() -> None:
    print("\n-- no credential value reaches any string this provider produces")
    provider = claude.ClaudeProvider()

    # Layer 1: the shape of a bearer token, wherever it came from.
    script = [
        AssistantMessage(
            content=[
                TextBlock(text=f"I found {FAKE_TOKEN} in the env"),
                ToolUseBlock(id="c1", name="Bash", input={"command": "env"}),
            ],
            model="m",
            session_id=SESSION_ID,
        ),
        SdkUserMessage(
            content=[
                ToolResultBlock(tool_use_id="c1", content=f"ANTHROPIC_KEY={FAKE_TOKEN}\n")
            ]
        ),
        StreamEvent(
            uuid="u1",
            session_id=SESSION_ID,
            event={
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": FAKE_TOKEN},
            },
        ),
        result_message(),
    ]
    with fake(script):
        handle = provider.start(thread(), brief(), allow)
        events = drain(provider, handle)
        blob = strings(events)
        check(FAKE_TOKEN not in blob, "a token-shaped value never reaches an event")
        check("[redacted]" in blob, "...and the redaction is visible rather than silent")
        provider.close(handle)

    # Layer 2: an exception path, with the child's stderr carrying it.
    with fake([Raise(RuntimeError(f"auth failed for {FAKE_TOKEN}"))]):
        handle = provider.start(thread(), brief(), allow)
        session = handle.native
        session.stderr.append(f"debug: bearer {FAKE_TOKEN}\n")
        events = drain(provider, handle)
        message = events[-1].data["message"]
        check(FAKE_TOKEN not in message, "nor an exception message")
        check("RuntimeError" in message, "...while the diagnosis survives")
        provider.close(handle)

    # Layer 3: `secrets.scrub`, which knows the files Jarvis owns.
    with tempfile.TemporaryDirectory(prefix="jarvis-wp3-env-") as tmp:
        env_value = "sh0rt-but-real-looking-credential-value-1234"
        Path(tmp, ".env").write_text(f"OPENROUTER_API_KEY={env_value}\n")
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            with fake(
                [
                    AssistantMessage(
                        content=[TextBlock(text=f"the key is {env_value}")],
                        model="m",
                        session_id=SESSION_ID,
                    ),
                    result_message(),
                ]
            ):
                handle = provider.start(thread(), brief(), allow)
                events = drain(provider, handle)
                check(
                    env_value not in strings(events),
                    "a value from a protected file never reaches an event either",
                )
                provider.close(handle)
        finally:
            os.chdir(cwd)

    # And the one file this provider reads: only an integer comes out of it.
    with credentials({"claudeAiOauth": {"accessToken": FAKE_TOKEN, "expiresAt": 1}}):
        expiry = claude._login_expiry_ms()
        eq(expiry, 1, "the credential bundle yields one integer and nothing else")
        with cli("2.1.290"):
            ok, reason = claude.ClaudeProvider().health()
            check(
                FAKE_TOKEN not in reason and not ok,
                f"and the health reason carries no credential: {reason}",
            )


# --- main -------------------------------------------------------------------


def set_model_checks() -> None:
    """Decisions A1: a chat thread's model or effort changes between turns by
    resuming the same session with new options — and keeps the gate."""
    provider = claude.ClaudeProvider()
    chat = Thread(id="chat", project_id="p1", role=Role.CHAT, provider=ProviderName.CLAUDE)
    with fake([result_message()]):
        handle = provider.start(chat, brief(role=Role.CHAT, model="claude-opus-5-5", effort="high"), allow)
        first = FakeClient.instances[-1]
        eq((first.options.model, first.options.effort), ("claude-opus-5-5", "high"), "set_model: opened on the chosen pair")
        drain(provider, handle, "one")
        provider.set_model(handle, "claude-sonnet-5-5", "low")
        second = FakeClient.instances[-1]
        check(second is not first and first.disconnects == 1, "set_model: the old client is disconnected, a new one connected")
        eq((second.options.model, second.options.effort), ("claude-sonnet-5-5", "low"), "set_model: the new client runs the new pair")
        eq(second.options.resume, SESSION_ID, "set_model: it resumes the same session (the conversation is kept)")
        check(second.options.hooks["PreToolUse"][0].hooks and second.options.allowed_tools == [],
              "set_model: the PreToolUse gate travels to the new client, allowed_tools stays empty")
        eq(second.options.permission_mode, "auto", "set_model: the profile's permission mode is unchanged")
        drain(provider, handle, "two")
        eq(prompt_texts(second), ["two"], "set_model: the next turn goes to the new client")
        eq(handle.native.brief.model, "claude-sonnet-5-5", "set_model: the session's brief carries the new model")

        count = len(FakeClient.instances)
        try:
            provider.set_model(handle, "claude-sonnet-5-5", "ultra")
        except BriefRefused as exc:
            check("ultra" in str(exc), "set_model: an off-ladder effort is refused by name")
        else:
            check(False, "set_model: an off-ladder effort is refused")
        eq(len(FakeClient.instances), count, "set_model: a refused effort spawns nothing")

        handle.native.sending.acquire()
        try:
            provider.set_model(handle, "claude-opus-5-5", "high")
        except ValueError as exc:
            check("turn" in str(exc), "set_model: refused while a turn is running")
        else:
            check(False, "set_model: refused while a turn is running")
        finally:
            handle.native.sending.release()
        provider.close(handle)

    # A reconnect that fails puts the old pair back and says so.
    original = claude._client_factory

    def failing(options):
        client = FakeClient(options, [result_message()])
        if options.model == "claude-fable-5-1":
            async def refuse():
                raise RuntimeError("model not available on this plan")
            client.connect = refuse
        return client

    FakeClient.instances = []
    claude._client_factory = failing
    try:
        fresh = provider.start(Thread(id="chat2", project_id="p1", role=Role.CHAT,
                                      provider=ProviderName.CLAUDE),
                               brief(role=Role.CHAT, model="claude-opus-5-5", effort="high"), allow)
        try:
            provider.set_model(fresh, "claude-fable-5-1", "high")
        except BriefRefused as exc:
            check("claude-fable-5-1" in str(exc) and "not available" in str(exc),
                  "set_model: a failed switch is refused with the reason")
        else:
            check(False, "set_model: a failed switch raises")
        restored = FakeClient.instances[-1]
        eq((restored.options.model, restored.connected), ("claude-opus-5-5", True),
           "set_model: the old model is reconnected after a failed switch")
        eq(restored.options.resume, None, "set_model: before any turn there is nothing to resume")
        eq(restored.options.session_id, fresh.provider_session_id, "set_model: the same session id is kept")
        eq(fresh.native.brief.model, "claude-opus-5-5", "set_model: the brief keeps the old model")
        provider.close(fresh)

        # Neither the new model nor the old one connects: the old client is
        # already disconnected, so it must not be put back as if it worked.
        def nothing_connects(options):
            client = FakeClient(options, [result_message()])
            if options.model in ("claude-fable-5-1", "claude-opus-5-5") and getattr(
                    nothing_connects, "armed", False):
                async def refuse():
                    raise RuntimeError("login expired")
                client.connect = refuse
            return client

        claude._client_factory = nothing_connects
        lost = provider.start(Thread(id="chat3", project_id="p1", role=Role.CHAT,
                                     provider=ProviderName.CLAUDE),
                              brief(role=Role.CHAT, model="claude-opus-5-5", effort="high"), allow)
        old = lost.native.client
        nothing_connects.armed = True
        try:
            provider.set_model(lost, "claude-fable-5-1", "high")
        except SessionLost as exc:
            check("login expired" in str(exc) and "resumes" in str(exc),
                  "set_model: a lost session says why and that the next message resumes it")
        except BriefRefused:
            check(False, "set_model: a double failure is SessionLost, not a plain refusal")
        else:
            check(False, "set_model: a double failure raises")
        check(lost.native.closed, "set_model: a session that cannot reconnect is closed, not restored")
        check(old.disconnects >= 1 and not old.connected,
              "set_model: the disconnected old client is not treated as live")
        events = list(provider.send(lost, UserMessage("after")))
        check(len(events) == 1 and events[0].kind == EventKind.ERROR and "closed" in events[0].data["message"],
              "set_model: a send on the lost session reports it closed (the daemon resumes instead)")
        provider.close(lost)   # idempotent
    finally:
        claude._client_factory = original


class Echo:
    """The CLI's replay echo (`--replay-user-messages`) of the newest steer
    written to stdin: it has drained into a turn."""


class PromptEcho:
    """The CLI's replay echo of the newest turn message: its turn has begun."""


class SteerClient(FakeClient):
    """A CLI-shaped stream: like the real one, `receive_messages` never ends on
    its own between turns. It plays the script as items appear and otherwise
    waits, so a frame the CLI sends late is read by whichever turn reads next."""

    def steers(self) -> list[dict]:
        return [q[0] for q in self.queries
                if isinstance(q, list) and q and q[0].get("priority") == "next"]

    def prompts(self) -> list[dict]:
        return [q[0] if isinstance(q, list) else {"message": {"content": q}}
                for q in self.queries
                if not (isinstance(q, list) and q and q[0].get("priority") == "next")]

    async def receive_response(self):
        """The SDK's own: the stream up to and including a result frame."""
        async for item in self.receive_messages():
            yield item
            if isinstance(item, ResultMessage):
                return

    async def receive_messages(self):
        while True:
            if not self.script:
                await asyncio.sleep(0.01)
                continue
            item = self.script.pop(0)
            if isinstance(item, Raise):
                raise item.exc
            if isinstance(item, Pause):
                while not item.event.is_set():
                    await asyncio.sleep(0.02)
                continue
            if isinstance(item, Echo):
                yield SdkUserMessage(content="(the steer)", uuid=self.steers()[-1]["uuid"])
                continue
            if isinstance(item, PromptEcho):
                prompts = self.prompts()
                yield SdkUserMessage(content="(the message)",
                                     uuid=prompts[-1].get("uuid") if prompts else None)
                continue
            yield item


@contextlib.contextmanager
def steer_fake(script):
    original = claude._client_factory
    FakeClient.instances = []
    claude._client_factory = lambda options: SteerClient(options, script)
    try:
        yield
    finally:
        claude._client_factory = original


@contextlib.contextmanager
def timings(**values):
    """Shorten the provider's waits for a check."""
    missing = object()
    saved = {name: getattr(claude, name, missing) for name in values}
    for name, value in values.items():
        setattr(claude, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is missing:
                delattr(claude, name)
            else:
                setattr(claude, name, value)


def text_of(events) -> list[str]:
    return [e.data["text"] for e in events if e.kind is EventKind.TEXT]


def assistant(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="claude-test")


def accepting(handle, timeout: float = 3.0) -> bool:
    """Until the turn's own CLI turn has begun and it takes steers."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if handle.native.accepting:
            return True
        time.sleep(0.005)
    return False


def steering_checks() -> None:
    """2026-10-08: a message sent while a turn runs is written into it (the
    CLI folds it in at its next boundary); a steer the CLI runs as a fresh
    turn stays inside the turn that took it; and one whose turn starts too
    late for that is absorbed by the next send, never mistaken for its answer
    (review of PR #22: the session used to stay off by one for good)."""
    print("\n-- steering a running turn")
    from jarvis.v2.provider import SteerRefused

    provider = claude.ClaudeProvider()
    with steer_fake([]), cli("2.1.290"):
        handle = provider.start(thread(), brief(), allow)
        client = FakeClient.instances[0]
        extra = client.options.extra_args or {}
        check("replay-user-messages" in extra and extra["replay-user-messages"] is None,
              "the CLI is asked to echo stdin messages (how a turn's start is known)")
        try:
            provider.steer(handle, UserMessage(text="too early"))
            check(False, "a steer with no turn running is refused")
        except SteerRefused as exc:
            check(exc.fallback == "queue", "a steer with no turn running is refused, to be queued")
        provider.close(handle)

    steer_order_checks()
    late_steer_checks()
    steering_more_checks()


def steer_order_checks() -> None:
    """Review of PR #22, finding 2: the turn's own message reaches stdin first.
    A steer is taken only once its CLI turn has begun; it used to be written
    ahead of the message it steers."""
    from jarvis.v2.provider import SteerRefused

    provider = claude.ClaudeProvider()
    gate, gate2 = threading.Event(), threading.Event()
    with timings(STEER_QUIET_S=0.2), \
            steer_fake([Pause(gate), PromptEcho(), Pause(gate2), assistant("ok"), result_message()]):
        handle = provider.start(thread(), brief(), allow)
        client = FakeClient.instances[0]
        stream = provider.send(handle, UserMessage(text="the turn's own message"))
        eq(next(stream).kind.value, "turn_started", "the turn starts")
        try:
            provider.steer(handle, UserMessage(text="sent as the turn starts"))
            check(False, "a steer before the turn's CLI turn began is refused")
        except SteerRefused:
            check(True, "a steer before the turn's CLI turn began is refused (the daemon queues it)")
        gate.set()
        check(accepting(handle), "once the message's echo is in, the turn takes steers")
        provider.steer(handle, UserMessage(text="and now a steer"))
        gate2.set()
        list(stream)
        order = [("steer" if q[0].get("priority") == "next" else "message", q[0]["message"]["content"])
                 if isinstance(q, list) else ("message", q) for q in client.queries]
        eq(order, [("message", "the turn's own message"), ("steer", "and now a steer")],
           "stdin order: the message, then the steer")
        provider.close(handle)



def steering_more_checks() -> None:
    """Folded, fresh, unechoed, a CLI that stops echoing, a failed write and
    a stop: each ends the turn once, with nothing read by the wrong send."""
    from jarvis.v2.provider import SteerRefused

    provider = claude.ClaudeProvider()

    # Folded into the turn: echoed while it runs, so its result ends it.
    gate = threading.Event()
    with steer_fake([PromptEcho(), Pause(gate), Echo(), assistant("switching to the other file"),
                     result_message()]):
        handle = provider.start(thread(), brief(), allow)
        client = FakeClient.instances[0]
        stream = provider.send(handle, UserMessage(text="fix the bug"))
        next(stream)
        check(accepting(handle), "the turn takes steers")
        provider.steer(handle, UserMessage(text="use the other file"))
        gate.set()
        events = list(stream)
        payload = client.steers()[0]
        eq((payload["type"], payload["message"], payload["priority"], payload["parent_tool_use_id"]),
           ("user", {"role": "user", "content": "use the other file"}, "next", None),
           "the steer is a stdin user message with priority next")
        check(isinstance(payload.get("uuid"), str) and len(payload["uuid"]) == 36,
              "carrying the uuid its echo is matched by")
        eq(kinds(events), ["text", "usage", "turn_finished"], "a folded steer: one finish")
        eq(text_of(events), ["switching to the other file"], "the echoes themselves are no event")
        provider.close(handle)

    # Not folded: the CLI ran it as a fresh turn right after the result.
    gate = threading.Event()
    with steer_fake([PromptEcho(), Pause(gate), assistant("done with the first ask"),
                     result_message(), Echo(), assistant("and now the steer"), result_message()]):
        handle = provider.start(thread(), brief(), allow)
        client = FakeClient.instances[0]
        stream = provider.send(handle, UserMessage(text="first"))
        next(stream)
        accepting(handle)
        provider.steer(handle, UserMessage(text="then this"))
        gate.set()
        events = list(stream)
        eq(kinds(events), ["text", "usage", "text", "usage", "turn_finished"],
           "the CLI's fresh turn for the steer is read inside the turn that took it")
        eq(text_of(events), ["done with the first ask", "and now the steer"],
           "both answers, in order")
        client.script.extend([PromptEcho(), assistant("the next message's answer"), result_message()])
        following = drain(provider, handle, "next")
        eq(text_of(following), ["the next message's answer"],
           "the next send reads its own response: nothing shifted by one turn")
        provider.close(handle)

    # Folded with no echo: the hold ends after quiet, a stray frame extending
    # it; the unseen steer is remembered, and the next message's own echo
    # settles it as folded.
    gate = threading.Event()
    with timings(STEER_QUIET_S=0.2), \
            steer_fake([PromptEcho(), Pause(gate), assistant("ok"), result_message(),
                        SystemMessage(subtype="status", data={})]):
        handle = provider.start(thread(), brief(), allow)
        client = FakeClient.instances[0]
        stream = provider.send(handle, UserMessage(text="go"))
        next(stream)
        accepting(handle)
        provider.steer(handle, UserMessage(text="quietly folded"))
        gate.set()
        started = time.monotonic()
        events = list(stream)
        waited = time.monotonic() - started
        eq(kinds(events), ["text", "usage", "turn_finished"],
           "an unechoed steer: the turn still finishes once")
        check(waited < 2, f"after the quiet window, not forever ({waited:.2f}s)")
        eq(len(handle.native.steering), 1, "the unseen steer is remembered past the turn")
        client.script.extend([PromptEcho(), assistant("two"), result_message()])
        second = drain(provider, handle, "second")
        eq(client.prompts()[-1].get("priority"), "later",
           "with a steer unaccounted for, the next message goes in at priority later")
        eq(text_of(second), ["two"], "and its own echo shows the steer folded: its turn reads normally")
        eq(handle.native.steering, {}, "the folded steer is forgotten once settled")
        provider.close(handle)

    # A CLI that echoed before but not this time: a result that might have
    # been a leftover's is taken as the message's own after LEFTOVER_WAIT_S.
    gate = threading.Event()
    with timings(STEER_QUIET_S=0.1, LEFTOVER_WAIT_S=0.2), \
            steer_fake([PromptEcho(), Pause(gate), assistant("one"), result_message()]):
        handle = provider.start(thread(), brief(), allow)
        client = FakeClient.instances[0]
        stream = provider.send(handle, UserMessage(text="go"))
        next(stream)
        accepting(handle)
        provider.steer(handle, UserMessage(text="unseen"))
        gate.set()
        list(stream)
        client.script.extend([assistant("two, unannounced"), result_message()])
        started = time.monotonic()
        second = drain(provider, handle, "second")
        eq(text_of(second), ["two, unannounced"], "a turn with no echo still ends")
        check(time.monotonic() - started < 2, "within the leftover wait, never a hang")
        provider.close(handle)

    # A write that fails is refused (queued by the daemon) and leaves no hold.
    gate = threading.Event()
    with steer_fake([PromptEcho(), Pause(gate), assistant("fine"), result_message()]):
        handle = provider.start(thread(), brief(), allow)
        client = FakeClient.instances[0]

        async def broken(prompt, session_id="default"):
            raise RuntimeError("stdin closed")

        stream = provider.send(handle, UserMessage(text="go"))
        next(stream)
        accepting(handle)
        original_query, client.query = client.query, broken
        try:
            provider.steer(handle, UserMessage(text="lost?"))
            check(False, "a steer whose write fails is refused")
        except SteerRefused as exc:
            check(exc.fallback == "queue" and "stdin closed" in str(exc),
                  f"a steer whose write fails is refused, to be queued: {exc}")
        client.query = original_query
        gate.set()
        started = time.monotonic()
        events = list(stream)
        check(time.monotonic() - started < 1 and kinds(events)[-1] == "turn_finished",
              "and the turn ends at its result, with no hold for a steer never sent")
        try:
            provider.steer(handle, UserMessage(text="after the end"))
            check(False, "a steer after the turn ended is refused")
        except SteerRefused:
            check(True, "a steer after the turn ended is refused")
        provider.close(handle)

    # Stop means stop: the owner interrupts while a steer waits in the CLI;
    # the fresh turn the CLI starts for it is interrupted as well.
    gate, gate2 = threading.Event(), threading.Event()
    with steer_fake([PromptEcho(), Pause(gate), result_message(terminal_reason="aborted_streaming"),
                     Echo(), Pause(gate2), result_message(terminal_reason="aborted_streaming")]):
        handle = provider.start(thread(), brief(), allow)
        client = FakeClient.instances[0]
        stream = provider.send(handle, UserMessage(text="go"))
        next(stream)
        accepting(handle)
        provider.steer(handle, UserMessage(text="queued in the CLI"))
        provider.interrupt(handle)
        gate.set()

        def release():
            deadline = time.monotonic() + 3
            while client.interrupts < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            gate2.set()

        threading.Thread(target=release, daemon=True).start()
        events = list(stream)
        eq(client.interrupts, 2, "the steer's fresh turn is interrupted too")
        eq(events[-1].data["stop"], "interrupted", "and the turn finishes as interrupted")
        eq(kinds(events).count("turn_finished"), 1, "once")
        provider.close(handle)



def late_steer_checks() -> None:
    """Review of PR #22, finding 1: the steer's fresh turn starts later than
    the hold (autocompact, hooks, a slow first token). The next send absorbs
    it — the owner still sees its answer — and reads its own, and every send
    after that is aligned. Before, every later turn read the one before it."""
    provider = claude.ClaudeProvider()
    # Review #1: the steer's fresh turn starts later than the hold. The next
    # send absorbs it — the owner still sees its answer — and reads its own,
    # and every send after that is aligned.
    gate, late = threading.Event(), threading.Event()
    with timings(STEER_QUIET_S=0.3), \
            steer_fake([PromptEcho(), Pause(gate), assistant("answer to first"), result_message(),
                        Pause(late), Echo(), assistant("answer to the steer"), result_message()]):
        handle = provider.start(thread(), brief(), allow)
        client = FakeClient.instances[0]
        stream = provider.send(handle, UserMessage(text="first"))
        next(stream)
        accepting(handle)
        provider.steer(handle, UserMessage(text="steer"))
        gate.set()
        first = list(stream)
        eq(text_of(first), ["answer to first"], "turn 1: its own answer")
        late.set()
        client.script.extend([PromptEcho(), assistant("answer to second"), result_message()])
        second = drain(provider, handle, "second")
        eq(text_of(second), ["answer to the steer", "answer to second"],
           "turn 2: the late steer's answer is absorbed, then its own")
        eq(kinds(second).count("turn_finished"), 1, "turn 2 finishes once, on its own result")
        client.script.extend([PromptEcho(), assistant("answer to third"), result_message()])
        third = drain(provider, handle, "third")
        eq(text_of(third), ["answer to third"], "turn 3: its own answer — no lasting off-by-one")
        provider.close(handle)

    unsent_turn_checks()
    stopped_late_steer_checks()


def unsent_turn_checks() -> None:
    """Review of PR #22, round 2, finding 1: a CLI turn nobody sent (a
    background task's notice) between two sends. Once the CLI is known to
    echo, an unechoed turn before a message's own echo is not that message's:
    it used to be taken as the answer, and every later send read the one
    before it."""
    provider = claude.ClaudeProvider()
    with steer_fake([PromptEcho(), assistant("one"), result_message(),
                     assistant("background notice"), result_message()]):
        handle = provider.start(thread(), brief(), allow)
        client = FakeClient.instances[0]
        eq(text_of(drain(provider, handle, "one")), ["one"], "unsent turn: the first send's own answer")
        client.script.extend([PromptEcho(), assistant("two"), result_message()])
        second = drain(provider, handle, "two")
        eq(text_of(second)[-1:], ["two"], "unsent turn: the next send ends on its own answer")
        eq(kinds(second).count("turn_finished"), 1, "unsent turn: and finishes once")
        client.script.extend([PromptEcho(), assistant("three"), result_message()])
        eq(text_of(drain(provider, handle, "three")), ["three"],
           "unsent turn: the send after it is aligned — no lasting off-by-one")
        provider.close(handle)


def stopped_late_steer_checks() -> None:
    """Review of PR #22, round 2, finding 2: the owner steers, then stops, and
    the steer's fresh CLI turn starts only after the hold. Stop means stop:
    when the next send reads that late turn, it is interrupted and its words
    never reach the owner. It used to run to the end and be shown as part of
    the next answer."""
    provider = claude.ClaudeProvider()
    gate, late = threading.Event(), threading.Event()
    with timings(STEER_QUIET_S=0.2), \
            steer_fake([PromptEcho(), Pause(gate),
                        result_message(terminal_reason="aborted_streaming"),
                        Pause(late), Echo(), assistant("the stopped steer ran anyway"),
                        result_message()]):
        handle = provider.start(thread(), brief(), allow)
        client = FakeClient.instances[0]
        stream = provider.send(handle, UserMessage(text="go"))
        next(stream)
        accepting(handle)
        provider.steer(handle, UserMessage(text="a steer the owner then stopped"))
        provider.interrupt(handle)
        gate.set()
        first = list(stream)
        eq(first[-1].data["stop"], "interrupted", "stopped steer: the turn finishes as interrupted")
        late.set()
        time.sleep(0.2)
        client.script.extend([PromptEcho(), assistant("two"), result_message()])
        second = drain(provider, handle, "two")
        eq(client.interrupts, 2, "stopped steer: its late CLI turn is interrupted too")
        eq(text_of(second), ["two"], "stopped steer: its words never reach the owner")
        eq(kinds(second).count("turn_finished"), 1, "stopped steer: the next send finishes once")
        eq(second[-1].data["stop"], "end", "stopped steer: and on its own result")
        client.script.extend([PromptEcho(), assistant("three"), result_message()])
        eq(text_of(drain(provider, handle, "three")), ["three"], "stopped steer: later sends aligned")
        provider.close(handle)


def main() -> int:
    # Hermetic: every client in this suite resolves to a fake executable in a
    # temp dir, never the owner's real `claude` (which nothing here may run).
    hermetic = tempfile.mkdtemp(prefix="jarvis-wp3-cli-")
    fake_cli = str(fake_exe(Path(hermetic) / "claude"))
    claude.config.CLAUDE_CLI = fake_cli
    claude.reset_cli_cache()
    resolver_checks()
    cli_options_checks(fake_cli)
    probe_and_status_checks()
    health_checks()
    options_checks()
    sequence_checks()
    hook_checks()
    answer_checks()
    reviewer_declined_checks()
    interrupt_checks()
    steering_checks()
    error_checks()
    usage_checks()
    isolation_checks()
    skill_checks()
    secrets_checks()
    set_model_checks()
    if _failures:
        print(f"\n{len(_failures)} check(s) FAILED:")
        for label in _failures:
            print(f"  - {label}")
        return 1
    print("\nall claude-provider checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
