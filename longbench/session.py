"""Recover telemetry from a Claude Code session transcript.

`--harness claude` gets turns, cost and tool calls from the `-p` stream. A
session run by hand gets none of that — `grade` alone produces a bare
percentage, which next to a fully instrumented Jarvis row invites exactly the
wrong comparison. Claude Code writes an append-only JSONL transcript per session
under `~/.claude/projects/<escaped-cwd>/<session-id>.jsonl`, and everything the
report needs is already in it.

**Cost is deliberately not computed.** The transcript carries token counts, not
dollars, and a session on a subscription has no per-run dollar figure at all.
Inventing one from a hardcoded price table would produce a number that looks
like the `total_cost_usd` the automated path reports and is not the same thing.
Tokens are reported instead, and cost stays `None` so the report prints a dash.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .spec import RunRecord

PROJECTS = Path.home() / ".claude" / "projects"


def project_dir(workspace: Path) -> Path:
    """Claude Code's per-cwd transcript directory for `workspace`.

    The escaping is Claude Code's: every path separator and dot becomes a dash.
    """
    escaped = str(Path(workspace).resolve()).replace("/", "-").replace(".", "-")
    return PROJECTS / escaped


def find_session(workspace: Path, session_id: str | None = None) -> Path | None:
    """The transcript for `workspace` — named, or the most recently written."""
    directory = project_dir(workspace)
    if session_id:
        candidate = directory / f"{session_id}.jsonl"
        return candidate if candidate.exists() else None
    if not directory.is_dir():
        return None
    transcripts = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return transcripts[-1] if transcripts else None


def _stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def record_from_session(path: Path, model: str = "unknown",
                        harness: str = "claude") -> RunRecord:
    """Fold a Claude Code transcript into a RunRecord.

    Sub-agent (`Task`) work is counted separately rather than folded in: the
    `-p` stream does not surface a child's tool calls either, so including them
    here would make a hand-run session look busier than an automated one for
    the same behaviour.
    """
    record = RunRecord(harness=harness, model=model)
    tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    turns = sidechain_calls = 0
    first = last = None
    prompts = 0

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        stamp = _stamp(entry.get("timestamp"))
        if stamp:
            first = stamp if first is None or stamp < first else first
            last = stamp if last is None or stamp > last else last

        kind = entry.get("type")
        if kind == "user" and not entry.get("isSidechain"):
            # promptSource marks a real human turn; tool results are also
            # "user" entries and must not be counted as prompts.
            if entry.get("promptSource") or entry.get("promptId"):
                prompts += 1
        if kind != "assistant":
            continue

        message = entry.get("message") or {}
        sidechain = bool(entry.get("isSidechain"))
        if not sidechain:
            turns += 1
            usage = message.get("usage") or {}
            tokens["input"] += usage.get("input_tokens") or 0
            tokens["output"] += usage.get("output_tokens") or 0
            tokens["cache_read"] += usage.get("cache_read_input_tokens") or 0
            tokens["cache_creation"] += usage.get("cache_creation_input_tokens") or 0
            if not record.model or record.model == "unknown":
                record.model = message.get("model") or record.model

        for block in message.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if sidechain:
                sidechain_calls += 1
                continue
            record.tool_calls.append(
                (block.get("name", "?"),
                 json.dumps(block.get("input", {}), sort_keys=True))
            )

    record.turns = turns
    record.tokens = tokens
    record.duration_s = (last - first).total_seconds() if first and last else None
    record.raw = {
        "session": path.name,
        "prompts": prompts,
        "subagent_tool_calls": sidechain_calls,
    }
    # Cost stays None on purpose — see the module docstring.
    return record
