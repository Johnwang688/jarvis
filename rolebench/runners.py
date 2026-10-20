"""Harness adapters: claude, codex, and the Jarvis loop.

The Claude Code stream parser and the Jarvis loop are `longbench.harness`'s,
reused as they are. What this adds: a scrubbed environment, a memory-capped
scope, a wall timeout that kills the whole process group, a codex adapter, and
a subprocess worker for the Jarvis loop (`run_jarvis` does `os.chdir` and edits
`config`, which three concurrent in-process runs would trample).

Every runner has the same shape, so tests inject fakes:

    runner(model=, prompt=, cwd=, timeout_s=, max_turns=, budget_usd=, out_dir=) -> RunResult
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config

CLAUDE_DISALLOWED = ["WebFetch", "WebSearch", "Bash(gh:*)", "Bash(curl:*)", "Bash(wget:*)",
                     "Bash(git push:*)"]
STDOUT_KEEP = 5_000_000


def scrubbed_env(base=None) -> dict:
    """The environment a run gets: no tokens, no API keys."""
    return {k: v for k, v in (os.environ if base is None else base).items() if k not in config.SCRUBBED_ENV}


def wrap_scope(argv: list[str]) -> list[str]:
    return ["systemd-run", "--user", "--scope", "-q", "-p", f"MemoryMax={config.MEMORY_MAX}",
            "-p", "MemorySwapMax=0", *argv]


@dataclass
class RunResult:
    harness: str
    model: str
    text: str = ""
    cost_usd: float | None = None
    tokens: dict = field(default_factory=dict)
    turns: int | None = None
    tool_calls: list = field(default_factory=list)      # (name, args json/command)
    duration_s: float = 0.0
    error: str = ""
    timed_out: bool = False
    raw: dict = field(default_factory=dict)

    def summary(self) -> dict:
        return {"harness": self.harness, "model": self.model, "cost_usd": self.cost_usd,
                "tokens": self.tokens, "turns": self.turns, "tool_calls": len(self.tool_calls),
                "duration_s": round(self.duration_s, 1), "error": self.error, "timed_out": self.timed_out}


def run_scoped(argv: list[str], *, cwd, env: dict, timeout_s: float, stdin: str | None = None,
               wrap=wrap_scope) -> tuple[int | None, str, str, bool]:
    """(returncode, stdout, stderr, timed_out). The whole process group dies on timeout."""
    proc = subprocess.Popen(wrap(argv) if wrap else argv, cwd=cwd, env=env, text=True,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    try:
        out, err = proc.communicate(stdin, timeout=timeout_s)
        return proc.returncode, out, err, False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, err = proc.communicate()
        return None, out or "", err or "", True


# --- claude -----------------------------------------------------------------------


def claude_argv(model: str, max_turns: int, *, read_only: bool = False) -> list[str]:
    binary = shutil.which("claude") or "claude"
    argv = [binary, "-p", "--output-format", "stream-json", "--verbose", "--model", model,
            "--max-turns", str(max_turns), "--no-session-persistence",
            # the real login, but none of the owner's hooks, plugins or MCP servers
            "--strict-mcp-config", "--setting-sources", "project,local"]
    if read_only:
        argv += ["--permission-mode", "default", "--tools", "Read,Grep,Glob"]
    else:
        argv += ["--permission-mode", "bypassPermissions", "--disallowedTools", *CLAUDE_DISALLOWED]
    return argv


def parse_claude_stream(stdout: str, model: str) -> RunResult:
    from longbench.harness import _absorb_claude_stream
    from longbench.spec import RunRecord
    record = RunRecord(harness="claude", model=model)
    _absorb_claude_stream(record, stdout, 0)
    result = RunResult("claude", model, cost_usd=record.cost_usd, turns=record.turns,
                       tool_calls=list(record.tool_calls), error=record.error)
    for line in stdout.splitlines():
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("type") == "result":
            result.text = str(message.get("result", ""))
            usage = message.get("usage") or {}
            result.tokens = {k: int(usage.get(k) or 0) for k in
                             ("input_tokens", "output_tokens", "cache_read_input_tokens",
                              "cache_creation_input_tokens")}
    return result


def run_claude(*, model, prompt, cwd, timeout_s, max_turns, budget_usd=None, out_dir=None,
               read_only=False, wrap=wrap_scope, env=None) -> RunResult:
    if not shutil.which("claude"):
        return RunResult("claude", model, error="claude CLI not found on PATH")
    started = time.time()
    rc, out, err, timed_out = run_scoped(claude_argv(model, max_turns, read_only=read_only), cwd=cwd,
                                         env=env or scrubbed_env(), timeout_s=timeout_s, stdin=prompt, wrap=wrap)
    result = parse_claude_stream(out, model)
    result.duration_s, result.timed_out = time.time() - started, timed_out
    if timed_out:
        result.error = result.error or f"timed out after {timeout_s}s"
    elif result.turns is None and rc:
        result.error = result.error or (err or "").strip()[:400]
    if out_dir:
        Path(out_dir, "stdout.jsonl").write_text(out[:STDOUT_KEEP], encoding="utf-8")
    return result


# --- codex ------------------------------------------------------------------------


def codex_argv(model: str, cwd, last_message: Path) -> list[str]:
    return [shutil.which("codex") or "codex", "exec", "-m", model, "-c", "model_reasoning_effort=high",
            "-s", "workspace-write", "-c", "sandbox_workspace_write.network_access=false",
            "--json", "--skip-git-repo-check", "--ephemeral", "-C", str(cwd),
            "-o", str(last_message), "-"]


def parse_codex_events(stdout: str, model: str) -> RunResult:
    result = RunResult("codex", model)
    turns = 0
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "item.completed":
            item = event.get("item") or {}
            it = item.get("type")
            if it == "agent_message":
                result.text = str(item.get("text") or "")
            elif it == "command_execution":
                result.tool_calls.append(("shell", str(item.get("command") or "")))
            elif it:
                result.tool_calls.append((it, json.dumps(item, sort_keys=True)[:500]))
        elif kind == "turn.completed":
            turns += 1
            for key, value in (event.get("usage") or {}).items():
                if isinstance(value, int) and not isinstance(value, bool):
                    result.tokens[key] = result.tokens.get(key, 0) + value
        elif kind in ("error", "turn.failed"):
            message = event.get("message") or (event.get("error") or {}).get("message") or kind
            result.error = str(message)[:300]
    result.turns = turns or None
    return result


def run_codex(*, model, prompt, cwd, timeout_s, max_turns=None, budget_usd=None, out_dir=None,
              wrap=wrap_scope, env=None) -> RunResult:
    if not shutil.which("codex"):
        return RunResult("codex", model, error="codex CLI not found on PATH")
    last = Path(tempfile.mkdtemp(prefix="rolebench-codex-")) / "last.txt"
    started = time.time()
    rc, out, err, timed_out = run_scoped(codex_argv(model, cwd, last), cwd=cwd, env=env or scrubbed_env(),
                                         timeout_s=timeout_s, stdin=prompt, wrap=wrap)
    result = parse_codex_events(out, model)
    if not result.text and last.exists():
        result.text = last.read_text(encoding="utf-8", errors="replace")
    shutil.rmtree(last.parent, ignore_errors=True)
    result.duration_s, result.timed_out = time.time() - started, timed_out
    if timed_out:
        result.error = result.error or f"timed out after {timeout_s}s"
    elif not result.text and rc:
        result.error = result.error or (err or "").strip()[:400]
    if out_dir:
        Path(out_dir, "stdout.jsonl").write_text(out[:STDOUT_KEEP], encoding="utf-8")
    return result


# --- the Jarvis loop --------------------------------------------------------------


def openrouter_key() -> str | None:
    """The key, from the environment or the checkout's .env. Never printed."""
    if os.environ.get("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]
    candidates = [os.environ.get("ROLEBENCH_ENV_FILE"), config.source_repo() / ".env",
                  Path.home() / "projects" / "Jarvis" / ".env"]
    for path in candidates:
        if not path:
            continue
        try:
            for line in Path(path).read_text(encoding="utf-8").splitlines():
                key, _, value = line.strip().partition("=")
                if key.strip() == "OPENROUTER_API_KEY" and value.strip():
                    return value.strip().strip("'\"")
        except OSError:
            continue
    return None


def run_jarvis(*, model, prompt, cwd, timeout_s, max_turns, budget_usd=None, out_dir=None,
               wrap=wrap_scope, env=None) -> RunResult:
    key = openrouter_key()
    if not key:
        return RunResult("jarvis", model, error="no OPENROUTER_API_KEY in the environment or .env")
    out_file = Path(tempfile.mkdtemp(prefix="rolebench-jarvis-")) / "result.json"
    spec = {"model": model, "prompt": prompt, "workspace": str(cwd), "max_turns": max_turns,
            "budget_usd": budget_usd, "provider": config.PROVIDER_PINS.get(model, ""),
            "price": config.PRICE_OVERRIDES.get(model), "out": str(out_file)}
    spec_file = out_file.parent / "spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    worker_env = dict(env or scrubbed_env())
    # The key goes over stdin, not the environment: /proc/<pid>/environ is
    # readable by the shell the loop gives the model.
    worker_env["PYTHONPATH"] = os.pathsep.join(
        [str(config.REPO_ROOT)] + ([worker_env["PYTHONPATH"]] if worker_env.get("PYTHONPATH") else []))
    argv = [config.python_bin(), "-m", "rolebench.jarvis_worker", str(spec_file)]
    started = time.time()
    rc, out, err, timed_out = run_scoped(argv, cwd=config.REPO_ROOT, env=worker_env, timeout_s=timeout_s,
                                         stdin=key + "\n", wrap=wrap)
    result = RunResult("jarvis", model)
    try:
        doc = json.loads(out_file.read_text(encoding="utf-8"))
        result.text, result.cost_usd = doc.get("text", ""), doc.get("cost_usd")
        result.tokens, result.turns = doc.get("tokens", {}), doc.get("turns")
        result.tool_calls = [tuple(c) for c in doc.get("tool_calls", [])]
        result.error = doc.get("error", "")
    except (OSError, json.JSONDecodeError):
        result.error = (err or "").strip()[-400:] or "worker produced no result"
    shutil.rmtree(out_file.parent, ignore_errors=True)
    result.duration_s, result.timed_out = time.time() - started, timed_out
    if timed_out:
        result.error = result.error or f"timed out after {timeout_s}s"
    return result


REGISTRY = {"claude": run_claude, "codex": run_codex, "jarvis": run_jarvis}
