"""Adapters: how a long-bench task gets in front of an agent, and what comes back.

Three of them. `claude` shells out to `claude -p` and reads the stream-json
protocol; `jarvis` imports the loop from this repo and runs it in-process;
`manual` writes the prompt out and waits for a human (or an agent session you
are already sitting in) to do the work by hand and press return.

**Parity is the whole job of this file.** The two automated adapters are given
the same prompt, the same working directory, the same turn ceiling, the same
dollar ceiling, and as close to the same toolset as the two harnesses have words
for. Anything given to one and not the other is a confound that shows up in the
score as if it were capability. The known-unequal parts are listed in the README
rather than papered over here.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from .spec import RunRecord

HARNESSES = ["claude", "jarvis", "manual"]

# Neither harness gets network tools: no task needs them, and a bench where one
# side can search the web and the other cannot is not measuring the loop.
CLAUDE_DISALLOWED = ["WebSearch", "WebFetch"]

# The closest Jarvis equivalent of Claude Code's default working set:
# read/write/edit, glob, grep, shell, a checklist, and sub-agents.
JARVIS_TOOLS = [
    "read_file", "write_file", "edit_file", "list_dir", "find_files", "grep_files",
    "run_readonly", "run_command", "get_datetime", "plan_write",
    "run_subagent", "run_fleet",
]

# Shell is the escape hatch from this bench's whole premise, and the first live
# run found it: Haiku scored 100% on `thread` in eight tool calls by writing a
# script, so none of the forty shards ever entered a context window. That is
# good engineering and a real capability — but it means the default mode
# measures *task success*, not context management.
#
# `--no-shell` takes it away from both harnesses symmetrically. The agent must
# then read files through its file tools, which is when compaction, truncation
# and spill actually get exercised. Neither mode is the "real" one; they measure
# different things and results from the two must never be pooled.
JARVIS_SHELL = ["run_readonly", "run_command"]
CLAUDE_SHELL = ["Bash", "BashOutput", "KillShell"]

DEFAULT_TIMEOUT_S = 3600


# ---------------------------------------------------------------------------
# claude -p
# ---------------------------------------------------------------------------


def run_claude(prompts: list[str], workspace: Path, model: str, max_turns: int,
               budget_usd: float | None = None, bare: bool = False,
               no_shell: bool = False, window: int | None = None,
               timeout_s: int = DEFAULT_TIMEOUT_S) -> RunRecord:
    binary = shutil.which("claude")
    record = RunRecord(harness="claude", model=model, window=window)
    if not binary:
        record.error = "claude CLI not found on PATH"
        return record

    disallowed = CLAUDE_DISALLOWED + (CLAUDE_SHELL if no_shell else [])
    base = [
        binary, "-p",
        "--output-format", "stream-json", "--verbose",
        "--model", model,
        "--permission-mode", "bypassPermissions",
        "--max-turns", str(max_turns),
        "--disallowedTools", *disallowed,
    ]
    if window:
        # The symmetric half of Jarvis's ContextPolicy(compact_at_tokens=…).
        # Claude Code accepts 100k–1M here, which is why the CLI refuses a
        # smaller window rather than quietly giving the two harnesses different
        # numbers.
        base += ["--autocompact", str(window)]
    if budget_usd:
        base += ["--max-budget-usd", str(budget_usd)]
    if bare:
        # Strips hooks, plugins, CLAUDE.md discovery and skills — the harness
        # with the user's personal configuration taken off it. Off by default,
        # because "the Claude Code harness" normally means the one people run.
        base.append("--bare")

    # Session persistence has to stay ON for the spec-drift turns: --resume is
    # what makes turn 2 land in the same conversation rather than a fresh one
    # that has never seen the migration.
    session_id = str(uuid.uuid4())
    started = time.time()
    for index, prompt in enumerate(prompts):
        argv = list(base)
        argv += ["--session-id", session_id] if index == 0 else ["--resume", session_id]
        argv.append(prompt)
        try:
            completed = subprocess.run(
                argv, cwd=workspace, capture_output=True, text=True, timeout=timeout_s
            )
        except subprocess.TimeoutExpired:
            record.error = f"turn {index + 1} timed out after {timeout_s}s"
            break
        _absorb_claude_stream(record, completed.stdout, index)
        if record.turns is None and completed.returncode != 0:
            record.error = record.error or (completed.stderr or "").strip()[:400]
            break
        if record.error:
            break

    record.duration_s = time.time() - started
    return record


def _absorb_claude_stream(record: RunRecord, stdout: str, index: int) -> None:
    """Fold one `claude -p` stream into the record. Costs and turns accumulate."""
    for line in stdout.splitlines():
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = message.get("type")
        if kind == "assistant":
            for block in message.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    record.tool_calls.append(
                        (block.get("name", "?"),
                         json.dumps(block.get("input", {}), sort_keys=True))
                    )
        elif kind == "result":
            record.cost_usd = (record.cost_usd or 0.0) + (message.get("total_cost_usd") or 0.0)
            record.turns = (record.turns or 0) + (message.get("num_turns") or 0)
            record.reply = str(message.get("result", ""))[:4000]
            record.raw[f"turn_{index + 1}"] = {
                "stop_reason": message.get("stop_reason"),
                "subtype": message.get("subtype"),
            }
            if message.get("is_error"):
                record.error = (f"turn {index + 1}: claude reported an error "
                                f"({message.get('subtype')})")


# ---------------------------------------------------------------------------
# the Jarvis loop, in process
# ---------------------------------------------------------------------------


def run_jarvis(prompts: list[str], workspace: Path, model: str, max_turns: int,
               budget_usd: float | None = None, no_shell: bool = False,
               window: int | None = None) -> RunRecord:
    record = RunRecord(harness="jarvis", model=model, window=window)
    try:
        from jarvis import agent as agent_mod
        from jarvis import config, context
        from jarvis.tools import files as files_mod
    except ImportError as exc:
        record.error = f"cannot import jarvis ({exc}) — run from the repo with .venv active"
        return record

    # Same sandbox reasoning as agent-bench: a bench must not be able to reach
    # the owner's memory, skills, sessions or allowlist, whatever the model asks.
    scratch = Path(tempfile.mkdtemp(prefix="longbench-jarvis-"))
    saved = {key: getattr(config, key) for key in
             ("MEMORY_DIR", "SKILLS_DIR", "SESSIONS_DIR", "ALLOWLIST_PATH")}
    cwd = Path.cwd()
    for name in ("memory", "skills", "sessions"):
        (scratch / name).mkdir()
    config.MEMORY_DIR = scratch / "memory"
    config.SKILLS_DIR = scratch / "skills"
    config.SESSIONS_DIR = scratch / "sessions"
    config.ALLOWLIST_PATH = scratch / "allowlist.json"
    files_mod._seen.clear()  # read-before-write must be earned inside the run

    calls: list[tuple[str, str]] = []
    spent = [0.0]
    stop = [False]

    def on_event(kind: str, data) -> None:
        if kind == "tool_start":
            name, raw = data
            try:
                args = json.loads(raw or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append((name, json.dumps(args, sort_keys=True)))
        elif kind == "cost" and budget_usd:
            # The goal runner's mid-turn ceiling, borrowed: a single long turn
            # can outspend a budget that is only checked when the turn ends.
            spent[0] = float(data) if isinstance(data, (int, float)) else spent[0]
            if spent[0] >= budget_usd:
                stop[0] = True

    policy = context.ContextPolicy()
    if window:
        policy.compact_at_tokens = window

    os.chdir(workspace)
    started = time.time()
    try:
        jarvis = agent_mod.Agent(
            model=model,
            system=config.SYSTEM_PROMPT,
            tool_names=[t for t in JARVIS_TOOLS
                        if not (no_shell and t in JARVIS_SHELL)],
            max_steps=max_turns,
            approve=lambda tool, args: True,  # parity with bypassPermissions
            on_event=on_event,
            should_stop=lambda: stop[0],
            policy=policy,
        )
        record.cost_usd = 0.0
        record.turns = 0
        for index, prompt in enumerate(prompts):
            # One Agent across every turn, so turn 2 inherits the transcript the
            # context manager left behind — which is the whole point of asking a
            # second question after the first one filled the window.
            turn = jarvis.run_turn(prompt)
            record.cost_usd += turn.cost_usd
            record.turns += turn.steps
            record.reply = (turn.text or "")[:4000]
            record.raw[f"turn_{index + 1}"] = {
                "stopped_early": bool(getattr(turn, "stopped_early", False)),
                "truncated": bool(getattr(turn, "truncated", False)),
            }
            if getattr(turn, "stopped_early", False):
                record.error = f"turn {index + 1} hit the {max_turns}-step cap"
            if stop[0]:
                record.error = record.error or "stopped on the dollar ceiling"
                break
    except Exception as exc:
        record.error = f"{type(exc).__name__}: {exc}"
    finally:
        record.duration_s = time.time() - started
        record.tool_calls = calls
        os.chdir(cwd)
        for key, value in saved.items():
            setattr(config, key, value)
        shutil.rmtree(scratch, ignore_errors=True)
    return record


# ---------------------------------------------------------------------------
# a human, or whatever session you are already in
# ---------------------------------------------------------------------------


def run_manual(prompts: list[str], workspace: Path, model: str, **_) -> RunRecord:
    record = RunRecord(harness="manual", model=model or "human")
    started = time.time()
    for index, prompt in enumerate(prompts):
        prompt_file = workspace.parent / f"PROMPT-{index + 1}.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        print("\n" + "=" * 72)
        print(f"workspace : {workspace}")
        print(f"prompt {index + 1}/{len(prompts)} : {prompt_file}")
        print("=" * 72)
        print(prompt)
        print("=" * 72)
        print("Do this in that directory, then press return for the next step.")
        try:
            input()
        except EOFError:
            break
    record.duration_s = time.time() - started
    return record


def run(harness: str, prompts: list[str], workspace: Path, model: str, max_turns: int,
        budget_usd: float | None = None, bare: bool = False,
        no_shell: bool = False, window: int | None = None) -> RunRecord:
    if isinstance(prompts, str):  # a single prompt is still a run of one turn
        prompts = [prompts]
    if harness == "claude":
        record = run_claude(prompts, workspace, model, max_turns, budget_usd,
                            bare, no_shell, window)
    elif harness == "jarvis":
        record = run_jarvis(prompts, workspace, model, max_turns, budget_usd,
                            no_shell, window)
    elif harness == "manual":
        record = run_manual(prompts, workspace, model)
    else:
        raise ValueError(f"unknown harness {harness!r} (expected one of {HARNESSES})")
    record.mode = "no-shell" if no_shell else "shell"
    return record


DEFAULT_MODELS = {
    "claude": "sonnet",
    "jarvis": "openai/gpt-5.6-luna",
    "manual": "human",
}
