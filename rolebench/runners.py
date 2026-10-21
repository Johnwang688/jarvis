"""Harness adapters: claude, codex, and the Jarvis loop, each inside a sandbox cell.

Every agent process runs in `rolebench.sandbox`: bwrap in a named, 3 GB
`systemd-run` unit, an allowlisted environment, and only the login its CLI
needs. The Claude Code stream parser is `longbench.harness`'s. The Jarvis loop
runs as a subprocess worker (`rolebench.jarvis_worker`) from a copy of the code
with no `.env` and no cases.

Every runner has the same shape, so tests inject fakes:

    runner(model=, prompt=, cwd=, timeout_s=, max_turns=, budget_usd=, out_dir=, run_id=) -> RunResult
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config, sandbox

# The claude cell's tools: an allowlist. No web, no Task/agents, no messaging,
# triggers, crons, notifications, artifacts or workflows.
CLAUDE_TOOLS = "Bash,Read,Edit,Write,Grep,Glob"
CLAUDE_READ_ONLY_TOOLS = "Read,Grep,Glob"
STDOUT_KEEP = 5_000_000
LOGIN_MARGIN_S = 600


def scrubbed_env(base=None) -> dict:
    """The harness's own environment minus tokens (the cells use `sandbox.env_for`)."""
    return {k: v for k, v in (os.environ if base is None else base).items() if k not in config.SCRUBBED_ENV}


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
    crashed: bool = False          # no result from the agent process at all
    spend_usd: float | None = None  # the jarvis worker's own running total
    models_used: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def summary(self) -> dict:
        return {"harness": self.harness, "model": self.model, "cost_usd": self.cost_usd,
                "tokens": self.tokens, "token_split": token_split(self.harness, self.tokens),
                "turns": self.turns, "tool_calls": len(self.tool_calls),
                "duration_s": round(self.duration_s, 1), "error": self.error, "timed_out": self.timed_out,
                "crashed": self.crashed, "spend_usd": self.spend_usd, "models_used": self.models_used,
                "multi_model": len(self.models_used) > 1}


def token_split(harness: str, tokens: dict) -> dict:
    """{"input", "cached", "output"}: input excludes cached reads in every harness.
    Codex's input_tokens already include cached_input_tokens; Claude's do not;
    OpenRouter's prompt_tokens include cached_tokens."""
    t = tokens or {}
    if harness == "codex":
        cached = int(t.get("cached_input_tokens") or 0)
        return {"input": max(0, int(t.get("input_tokens") or 0) - cached), "cached": cached,
                "output": int(t.get("output_tokens") or 0)}
    if harness == "jarvis":
        cached = int(t.get("cached_tokens") or 0)
        return {"input": max(0, int(t.get("prompt_tokens") or 0) - cached), "cached": cached,
                "output": int(t.get("completion_tokens") or 0)}
    if harness == "claude":
        return {"input": int(t.get("input_tokens") or 0) + int(t.get("cache_creation_input_tokens") or 0),
                "cached": int(t.get("cache_read_input_tokens") or 0), "output": int(t.get("output_tokens") or 0)}
    return {"input": int(t.get("input_tokens") or 0), "cached": 0, "output": int(t.get("output_tokens") or 0)}


def _io_dir(run_id: str) -> Path:
    root = config.home() / "io"
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="io-", dir=root))


# --- logins: never refreshed inside a cell ------------------------------------------


def _jwt_exp(token) -> float | None:
    if not isinstance(token, str) or token.count(".") != 2:
        return None
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        exp = json.loads(base64.urlsafe_b64decode(part)).get("exp")
        return float(exp) if isinstance(exp, (int, float)) else None
    except Exception:
        return None


def login_problem(harness: str, timeout_s: float, now: float | None = None) -> str | None:
    """Why the CLI login might be refreshed during this run, or None.

    The login is bound read-only into the cell, so a refresh there could rotate
    the token without saving it. A login that would expire inside the run is
    refused instead; refresh it outside the bench (any ordinary CLI call)."""
    now = time.time() if now is None else now
    need = now + timeout_s + LOGIN_MARGIN_S
    if harness in ("claude", "judge"):
        path = sandbox.claude_credentials()
        try:
            exp = json.loads(path.read_text(encoding="utf-8"))["claudeAiOauth"]["expiresAt"] / 1000
        except Exception:
            return f"cannot read the Claude login's expiry ({path.name})"
        return None if exp > need else "the Claude login expires within this run: refresh it outside the bench first"
    if harness == "codex":
        path = sandbox.codex_auth()
        try:
            exp = _jwt_exp(json.loads(path.read_text(encoding="utf-8"))["tokens"]["access_token"])
        except Exception:
            return f"cannot read the Codex login's expiry ({path.name})"
        if exp is None:
            return "the Codex login has no readable expiry"
        return None if exp > need else "the Codex login expires within this run: refresh it outside the bench first"
    return None


# --- claude -----------------------------------------------------------------------


def claude_argv(model: str, max_turns: int, *, read_only: bool = False, binary: str | None = None) -> list[str]:
    argv = [binary or str(sandbox.BIN / "claude"), "-p", "--output-format", "stream-json", "--verbose",
            "--model", model, "--max-turns", str(max_turns), "--no-session-persistence",
            # the login, but no MCP servers and no user settings (the cell's HOME is empty anyway)
            "--strict-mcp-config", "--setting-sources", "project,local"]
    if read_only:
        argv += ["--permission-mode", "default", "--tools", CLAUDE_READ_ONLY_TOOLS]
    else:
        argv += ["--permission-mode", "bypassPermissions", "--tools", CLAUDE_TOOLS]
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
            usage_by_model = message.get("modelUsage")
            if isinstance(usage_by_model, dict):
                result.models_used = sorted(usage_by_model)
                result.raw["modelUsage"] = usage_by_model
    return result


def run_claude(*, model, prompt, cwd, timeout_s, max_turns, budget_usd=None, out_dir=None,
               read_only=False, run_id=None, launch=None, profile=None) -> RunResult:
    profile = profile or ("judge" if read_only else "claude")
    if not sandbox.claude_binary():
        return RunResult("claude", model, error="claude CLI not found on PATH", crashed=True)
    problem = login_problem("claude", timeout_s)
    if problem:
        return RunResult("claude", model, error=f"refused: {problem}", crashed=True)
    cell = sandbox.Cell(profile, Path(cwd), work_ro=read_only)
    started = time.time()
    try:
        rc, out, err, timed_out = (launch or sandbox.launch)(
            claude_argv(model, max_turns, read_only=read_only), cell=cell,
            run_id=run_id or f"claude-{os.getpid()}-{int(started)}", timeout_s=timeout_s, stdin=prompt)
    except sandbox.SandboxError as exc:
        return RunResult("claude", model, error=f"sandbox: {exc}", crashed=True)
    result = parse_claude_stream(out, model)
    result.duration_s, result.timed_out = time.time() - started, timed_out
    result.crashed = result.turns is None
    if timed_out:
        result.error = result.error or f"timed out after {timeout_s}s"
    elif result.turns is None and rc:
        result.error = result.error or (err or "").strip()[:400] or f"exit {rc}"
    if out_dir:
        Path(out_dir, "stdout.jsonl").write_text(out[:STDOUT_KEEP], encoding="utf-8")
    return result


# --- codex ------------------------------------------------------------------------


def codex_argv(model: str, cwd, last_message: Path, binary: str | None = None) -> list[str]:
    return [binary or str(sandbox.CODEX / "bin" / "codex"), "exec", "-m", model,
            "-c", "model_reasoning_effort=high",
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
    result.models_used = [model]
    return result


def run_codex(*, model, prompt, cwd, timeout_s, max_turns=None, budget_usd=None, out_dir=None,
              run_id=None, launch=None) -> RunResult:
    if not sandbox.codex_release():
        return RunResult("codex", model, error="codex CLI not found on PATH", crashed=True)
    problem = login_problem("codex", timeout_s)
    if problem:
        return RunResult("codex", model, error=f"refused: {problem}", crashed=True)
    io = _io_dir(run_id or "codex")
    cell = sandbox.Cell("codex", Path(cwd), io_dir=io)
    started = time.time()
    try:
        rc, out, err, timed_out = (launch or sandbox.launch)(
            codex_argv(model, cwd, sandbox.IO / "last.txt"), cell=cell,
            run_id=run_id or f"codex-{os.getpid()}-{int(started)}", timeout_s=timeout_s, stdin=prompt)
    except sandbox.SandboxError as exc:
        shutil.rmtree(io, ignore_errors=True)
        return RunResult("codex", model, error=f"sandbox: {exc}", crashed=True)
    result = parse_codex_events(out, model)
    last = io / "last.txt"
    if not result.text and last.is_file() and not last.is_symlink():
        result.text = last.read_text(encoding="utf-8", errors="replace")
    shutil.rmtree(io, ignore_errors=True)
    result.duration_s, result.timed_out = time.time() - started, timed_out
    result.crashed = result.turns is None and not result.text
    if timed_out:
        result.error = result.error or f"timed out after {timeout_s}s"
    elif not result.text and rc:
        result.error = result.error or (err or "").strip()[:400] or f"exit {rc}"
    if out_dir:
        Path(out_dir, "stdout.jsonl").write_text(out[:STDOUT_KEEP], encoding="utf-8")
    return result


# --- the Jarvis loop --------------------------------------------------------------


def openrouter_key() -> str | None:
    """The key, from the harness's environment or the checkout's .env. Never
    printed, never put in a cell's environment: it goes to the worker on stdin."""
    if os.environ.get("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]
    candidates = [os.environ.get("ROLEBENCH_ENV_FILE"), config.source_repo() / ".env",
                  Path(config.real_home()) / "projects" / "Jarvis" / ".env"]
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


def jarvis_spec(*, model, prompt, cwd, max_turns, budget_usd) -> dict:
    """What the worker is told. Raises config.UnknownModel for an unpriced Kimi id."""
    price = config.price_override(model)
    pin = config.provider_pin(model)
    return {"model": model, "prompt": prompt, "workspace": str(cwd), "max_turns": max_turns,
            "budget_usd": float(config.PER_RUN_CAP_USD if budget_usd is None else budget_usd),
            "prices": {model: list(price)} if price else {}, "pins": {model: pin} if pin else {},
            "io": str(sandbox.IO)}


def run_jarvis(*, model, prompt, cwd, timeout_s, max_turns, budget_usd=None, out_dir=None,
               run_id=None, launch=None, key_fn=None) -> RunResult:
    try:
        spec = jarvis_spec(model=model, prompt=prompt, cwd=cwd, max_turns=max_turns, budget_usd=budget_usd)
    except config.UnknownModel as exc:
        return RunResult("jarvis", model, error=f"refused: {exc}", crashed=True)
    key = (key_fn or openrouter_key)()
    if not key:
        return RunResult("jarvis", model, error="no OPENROUTER_API_KEY in the environment or .env", crashed=True)
    io = _io_dir(run_id or "jarvis")
    runtime = Path(tempfile.mkdtemp(prefix="runtime-", dir=io.parent))
    shutil.rmtree(runtime)
    started = time.time()
    try:
        sandbox.build_runtime(runtime)
        (io / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
        cell = sandbox.Cell("jarvis", Path(cwd), io_dir=io, runtime=runtime,
                            env={"PYTHONPATH": str(sandbox.RUNTIME)})
        rc, out, err, timed_out = (launch or sandbox.launch)(
            [sandbox.PYTHON, "-m", "rolebench.jarvis_worker", str(sandbox.IO / "spec.json")], cell=cell,
            run_id=run_id or f"jarvis-{os.getpid()}-{int(started)}", timeout_s=timeout_s, stdin=key + "\n")
    except sandbox.SandboxError as exc:
        shutil.rmtree(io, ignore_errors=True)
        shutil.rmtree(runtime, ignore_errors=True)
        return RunResult("jarvis", model, error=f"sandbox: {exc}", crashed=True)
    result = RunResult("jarvis", model)
    result.spend_usd = spend_from_stdout(out)         # streamed, so it survives a kill mid-call
    try:
        if (io / "result.json").is_symlink():
            raise OSError("result.json is a symlink")
        doc = json.loads((io / "result.json").read_text(encoding="utf-8"))
        result.text, result.cost_usd = doc.get("text", ""), doc.get("cost_usd")
        result.tokens, result.turns = doc.get("tokens", {}), doc.get("turns")
        result.tool_calls = [tuple(c) for c in doc.get("tool_calls", [])]
        result.error = doc.get("error", "")
        result.models_used = sorted(doc.get("models") or {})
        if isinstance(doc.get("spend_usd"), (int, float)):
            result.spend_usd = max(result.spend_usd or 0.0, float(doc["spend_usd"]))
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        result.crashed = True
        result.error = (err or "").strip()[-400:] or f"worker produced no result (exit {rc})"
    shutil.rmtree(io, ignore_errors=True)
    shutil.rmtree(runtime, ignore_errors=True)
    result.duration_s, result.timed_out = time.time() - started, timed_out
    if timed_out:
        result.error = result.error or f"timed out after {timeout_s}s"
    if out_dir:
        Path(out_dir, "stdout.jsonl").write_text((out or "")[:STDOUT_KEEP], encoding="utf-8")
        Path(out_dir, "stderr.txt").write_text((err or "")[-200_000:], encoding="utf-8")
    return result


def spend_from_stdout(out: str) -> float | None:
    """The last ROLEBENCH_SPEND line's total, or None."""
    from rolebench.jarvis_worker import SPEND_PREFIX
    usd = None
    for line in (out or "").splitlines():
        if line.startswith(SPEND_PREFIX):
            try:
                v = json.loads(line[len(SPEND_PREFIX):]).get("usd")
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    usd = float(v)
            except (ValueError, AttributeError):
                continue
    return usd


REGISTRY = {"claude": run_claude, "codex": run_codex, "jarvis": run_jarvis}
