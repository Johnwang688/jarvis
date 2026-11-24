"""Run a bounded, non-interactive Claude Code job with an explicit tool list."""

import json
import logging
import os
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Event
from typing import Literal

LOG = logging.getLogger(__name__)
# None: a job loads no settings, so no hook judges its tools, and a built-in Read would be a
# credential route. Jobs read through Jarvis's own MCP tools, which check every path.
ALLOWED_TOOLS: frozenset[str] = frozenset()
MODELS = frozenset({"haiku", "sonnet", "opus"})
MIN_CLI = "2.1.325"

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_MCP_NAME = re.compile(r"[A-Za-z0-9_-]+")
_ENV_KEY = re.compile(r"JARVIS_[A-Z0-9_]+")
_GUARDS = {"JARVIS_JOB", "JARVIS_REVIEWER", "CLAUDE_CODE_DISABLE_CLAUDE_MDS"}
_VERSION_TIMEOUT = 3.0
_VERSION_CAP = 4096
_STDOUT_CAP = 8 * 1024 * 1024
_STDERR_CAP = 16 * 1024
_IO_CHUNK = 64 * 1024
_POLL_INTERVAL = 0.05
_TERM_GRACE = 0.2
_REAP_TIMEOUT = 1.0
_TOKEN_LIMIT = 10**12

Outcome = Literal["ok", "error", "timeout", "cancelled"]


@dataclass(frozen=True)
class JobResult:
    name: str
    model: str
    ok: bool = False
    outcome: Outcome = "error"
    text: str = field(default="", repr=False)
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    seconds: float = 0.0
    session_id: str | None = None


def resolve_cli() -> str | None:
    from jarvis.v2.providers.claude import resolve_cli as resolve

    return resolve()


def _job_env(name: str, review: bool, extra: dict[str, str]) -> dict[str, str]:
    from jarvis.v2.providers.claude import ISOLATED_ENV_KEEP, ISOLATED_ENV_KEEP_PREFIXES

    env = {key: value for key, value in os.environ.items()
           if key in ISOLATED_ENV_KEEP or key.startswith(ISOLATED_ENV_KEEP_PREFIXES)}
    env.update(extra)
    env.update(JARVIS_JOB=name, CLAUDE_CODE_DISABLE_CLAUDE_MDS="1")
    if review:
        env["JARVIS_REVIEWER"] = "1"
    return env


def _tool_args(tools, servers: dict) -> tuple[str, str]:
    try:
        if isinstance(tools, (str, bytes)):
            raise TypeError
        requested = tuple(tools)
    except TypeError as exc:
        raise ValueError("tools must be a collection of tool names") from exc
    builtins = []
    for tool in requested:
        if not isinstance(tool, str):
            raise ValueError("invalid job tool")
        if tool in ALLOWED_TOOLS:
            builtins.append(tool)
            continue
        server, delim, method = tool.removeprefix("mcp__").partition("__")
        if not (tool.startswith("mcp__") and delim and server in servers and _MCP_NAME.fullmatch(method)):
            raise ValueError("invalid job tool")
    return ",".join(builtins), ",".join(requested)


def _signal_group(pid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


@dataclass
class _Output:
    stdout_cap: int
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    overflow: bool = False
    stderr_truncated: bool = False

    def read(self, fd: int, stream: str) -> bool:
        """Read one chunk. Return whether the pipe can still deliver bytes."""
        chunk = os.read(fd, _IO_CHUNK)
        if not chunk:
            return False
        if stream == "stdout":
            remaining = self.stdout_cap - len(self.stdout)
            self.stdout.extend(chunk[:remaining])
            self.overflow = len(chunk) > remaining
        else:
            self.stderr.extend(chunk)
            if len(self.stderr) > _STDERR_CAP:
                del self.stderr[:-_STDERR_CAP]
                self.stderr_truncated = True
        return True


def _capture(argv, *, cwd, env, prompt, timeout, cancel, stdout_cap):
    deadline = time.monotonic() + timeout
    output = _Output(stdout_cap)
    with selectors.DefaultSelector() as selector:
        proc = subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=subprocess.PIPE if prompt else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
        reason = None
        try:
            for stream in (proc.stdout, proc.stderr):
                os.set_blocking(stream.fileno(), False)
            selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
            selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
            remaining = memoryview(prompt)
            if proc.stdin is not None:
                os.set_blocking(proc.stdin.fileno(), False)
                selector.register(proc.stdin, selectors.EVENT_WRITE, "stdin")
            while True:
                # Observe exit without reaping. The PID still identifies our
                # group when we signal it, even if the leader already exited.
                if os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT):
                    break
                if cancel is not None and cancel.is_set():
                    reason = "cancelled"
                    break
                left = deadline - time.monotonic()
                if left <= 0:
                    reason = "timeout"
                    break
                for key, _ in selector.select(min(left, _POLL_INTERVAL)):
                    try:
                        if key.data == "stdin":
                            try:
                                remaining = remaining[os.write(key.fd, remaining[:_IO_CHUNK]):]
                            except BrokenPipeError:
                                more = False
                            else:
                                more = bool(remaining)
                        else:
                            more = output.read(key.fd, key.data)
                    except BlockingIOError:
                        continue
                    if not more:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                if output.overflow:
                    break
        finally:
            # The leader and any MCP processes share this group. Closing
            # pipes alone cannot stop a child that has closed its own copies.
            if reason in ("timeout", "cancelled"):
                _signal_group(proc.pid, signal.SIGTERM)
                time.sleep(_TERM_GRACE)
            _signal_group(proc.pid, signal.SIGKILL)
            try:
                # Drain only queued bytes plus any last writes before SIGKILL.
                # Bound these reads even if a pipe stays open after cleanup.
                for stream, label in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
                    for _ in range(1 + _STDOUT_CAP // _IO_CHUNK):
                        if stream.closed or output.overflow:
                            break
                        try:
                            if not output.read(stream.fileno(), label):
                                break
                        except BlockingIOError:
                            break
            finally:
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream is not None:
                        stream.close()
                returncode = proc.wait(timeout=_REAP_TIMEOUT)
    return reason, returncode, output


def _error(message: str, output: _Output) -> str:
    if not output.stderr or output.stderr_truncated:
        # Truncation can discard the start of a multiline credential.
        return message
    from jarvis.v2.redact import for_display

    tail = output.stderr.decode("utf-8", "replace")
    try:
        clean = " ".join(for_display(tail).split())
    except Exception:
        return message
    clean = "".join(char for char in clean if char.isprintable())
    return f"{message}: {clean[-300:]}" if clean else message


def _tokens(value) -> int:
    return value if type(value) is int and 0 <= value < _TOKEN_LIMIT else 0


def _result(result: JobResult, reason, returncode: int, output: _Output) -> JobResult:
    if reason == "cancelled":
        return replace(result, outcome=reason, error="Claude job cancelled.")
    if reason == "timeout":
        return replace(result, outcome=reason, error="Claude job timed out.")
    if output.overflow:
        return replace(result, error="Claude job exceeded its output limit.")
    try:
        reply = json.loads(output.stdout.decode("utf-8"))
    except (ValueError, RecursionError):
        reply = None
    if not isinstance(reply, dict) or reply.get("type") != "result":
        return replace(result, error=_error("Claude job returned no valid result", output))

    usage = reply.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    input_tokens = sum(_tokens(usage.get(key)) for key in (
        "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    cost = reply.get("total_cost_usd")
    session_id = reply.get("session_id")
    result = replace(
        result, input_tokens=min(input_tokens, _TOKEN_LIMIT - 1),
        output_tokens=_tokens(usage.get("output_tokens")),
        cost_usd=float(cost) if type(cost) in (int, float) and 0 <= cost <= sys.float_info.max else None,
        session_id=session_id if isinstance(session_id, str) else None,
    )
    text = reply.get("result")
    if (returncode != 0 or reply.get("subtype") != "success" or reply.get("is_error") is not False
            or not isinstance(text, str)):
        return replace(result, error=_error("Claude job failed", output))
    return replace(result, ok=True, outcome="ok", text=text)


def _run(result, *, prompt, tool_args, mcp_json, cwd, timeout, env, cancel, cli):
    from jarvis import config
    from jarvis.v2.providers.claude import parse_version

    if cancel is not None and cancel.is_set():
        return replace(result, outcome="cancelled", error="Claude job cancelled.")
    cli = resolve_cli() if cli is None else cli
    if not cli:
        return replace(result, error="Claude CLI was not found.")
    try:
        reason, returncode, output = _capture(
            [cli, "--version"], cwd=cwd, env=env, prompt=b"", timeout=_VERSION_TIMEOUT,
            cancel=cancel, stdout_cap=_VERSION_CAP,
        )
        if reason == "cancelled" or (cancel is not None and cancel.is_set()):
            return replace(result, outcome="cancelled", error="Claude job cancelled.")
        version = None
        if reason is None and returncode == 0 and not output.overflow:
            parts = output.stdout.decode("utf-8").split(maxsplit=1)
            version = parse_version(parts[0]) if parts else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        version = None
    if version is None or version < parse_version(MIN_CLI):
        return replace(result, error=f"Claude CLI version {MIN_CLI} or newer is required.")

    try:
        jobs = config.V3_DIR / "jobs"
        jobs.mkdir(mode=0o700, parents=True, exist_ok=True)
        if jobs.is_symlink():
            return replace(result, error="Claude job directory is a symbolic link.")
        with tempfile.TemporaryDirectory(prefix="job-", dir=jobs) as directory:
            if cancel is not None and cancel.is_set():
                return replace(result, outcome="cancelled", error="Claude job cancelled.")
            fd, path = tempfile.mkstemp(prefix="mcp-", suffix=".json", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(mcp_json)

            builtins, allowed = tool_args
            argv = [
                cli, "-p", "--output-format", "json", "--model", result.model,
                "--permission-mode", "dontAsk", "--strict-mcp-config", "--mcp-config", path,
                "--setting-sources=", "--no-session-persistence", "--disable-slash-commands",
                "--tools", builtins,
            ]
            if allowed:
                argv.extend(("--allowedTools", allowed))
            if cancel is not None and cancel.is_set():
                return replace(result, outcome="cancelled", error="Claude job cancelled.")
            capture = _capture(argv, cwd=cwd, env=env, prompt=prompt, timeout=timeout,
                               cancel=cancel, stdout_cap=_STDOUT_CAP)
        return _result(result, *capture)
    except (OSError, subprocess.TimeoutExpired):
        return replace(result, error="Could not run or clean up the Claude job.")


def run_job(
    name: str, prompt: str, *, model: str, tools=(), mcp_servers=None,
    cwd: str | os.PathLike, timeout: float, env_extra=None, review: bool = False,
    cancel: Event | None = None, cli: str | None = None,
) -> JobResult:
    """Run one job. Invalid caller input raises ValueError."""
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("invalid job name")
    if not isinstance(model, str) or model not in MODELS:
        raise ValueError("unsupported job model")
    if not isinstance(prompt, str) or not prompt or "\x00" in prompt:
        raise ValueError("invalid job prompt")
    try:
        prompt_bytes = prompt.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("job prompt must be UTF-8 encodable") from exc
    if type(timeout) not in (int, float) or not 0 < timeout <= sys.float_info.max:
        raise ValueError("job timeout must be positive and finite")
    try:
        cwd = Path(cwd)
        if not cwd.is_absolute() or not cwd.is_dir():
            raise ValueError("job cwd must be an existing absolute directory")
    except (TypeError, OSError) as exc:
        raise ValueError("invalid job cwd") from exc
    if cli is not None and (not isinstance(cli, str) or not os.path.isabs(cli) or "\x00" in cli):
        raise ValueError("job CLI must be an absolute path")
    if type(review) is not bool or (cancel is not None and not isinstance(cancel, Event)):
        raise ValueError("invalid job review or cancellation argument")

    extra = {} if env_extra is None else env_extra
    if not isinstance(extra, dict) or any(
        not isinstance(key, str) or not _ENV_KEY.fullmatch(key) or key in _GUARDS
        or not isinstance(value, str) or "\x00" in value
        for key, value in extra.items()
    ):
        raise ValueError("invalid job environment override")
    servers = {} if mcp_servers is None else mcp_servers
    if not isinstance(servers, dict) or any(
        not isinstance(key, str) or not _MCP_NAME.fullmatch(key) or "__" in key or not isinstance(value, dict)
        for key, value in servers.items()
    ):
        raise ValueError("invalid job MCP servers")
    try:
        mcp_json = json.dumps({"mcpServers": servers}, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("job MCP config must be JSON encodable") from exc
    tool_args = _tool_args(tools, servers)

    start = time.monotonic()
    result = _run(JobResult(name, model), prompt=prompt_bytes, tool_args=tool_args,
                  mcp_json=mcp_json, cwd=cwd, timeout=timeout,
                  env=_job_env(name, review, extra), cancel=cancel, cli=cli)
    result = replace(result, seconds=time.monotonic() - start)
    LOG.info("Claude job %s model=%s outcome=%s seconds=%.3f input_tokens=%d output_tokens=%d",
             name, model, result.outcome, result.seconds, result.input_tokens, result.output_tokens)
    return result
