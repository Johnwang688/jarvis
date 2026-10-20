"""Read the reviewer's machine-readable block, and derive the run flags."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import config

FENCE = re.compile(r"```(?:json)?[ \t]*\n(.*?)```", re.S)
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "file_change", "edit_file", "write_file"}
EDIT_MARKERS = ("sed -i", "git apply", "git stash", "patch -p", "perl -pi")
TEST_MARKERS = ("_check.py", "pytest", "vitest", "unittest")


@dataclass
class Parsed:
    findings: list[dict] = field(default_factory=list)
    ran_tests: bool | None = None
    tried_mutant: bool | None = None
    errors: list[str] = field(default_factory=list)
    found_block: bool = False


def _finding(raw, i: int, errors: list[str]) -> dict | None:
    if not isinstance(raw, dict):
        errors.append(f"finding {i}: not an object")
        return None
    sev, title = raw.get("severity"), raw.get("title")
    if sev not in config.SEVERITIES:
        errors.append(f"finding {i}: bad severity {sev!r}")
        return None
    if not isinstance(title, str) or not title.strip():
        errors.append(f"finding {i}: no title")
        return None
    line = raw.get("line")
    if isinstance(line, bool) or not (line is None or isinstance(line, int)):
        line = None
    return {"severity": sev, "file": str(raw.get("file") or ""), "line": line,
            "title": title.strip(), "scenario": str(raw.get("scenario") or "").strip()}


def parse(text: str) -> Parsed:
    """The last fenced block that is a JSON object with a `findings` list."""
    out = Parsed()
    for block in reversed(FENCE.findall(text or "")):
        try:
            doc = json.loads(block)
        except json.JSONDecodeError:
            continue
        if not isinstance(doc, dict) or not isinstance(doc.get("findings"), list):
            continue
        out.found_block = True
        for i, raw in enumerate(doc["findings"], 1):
            f = _finding(raw, i, out.errors)
            if f:
                out.findings.append(f)
        for key in ("ran_tests", "tried_mutant"):
            if isinstance(doc.get(key), bool):
                setattr(out, key, doc[key])
        return out
    out.errors.append("no findings block")
    return out


def flags(tool_calls: list, parsed: Parsed) -> dict:
    """ran_tests / tried_mutant. Observed from tool calls when there are any
    (a model's say-so is not enough for 'tried a mutant': it must also have
    edited something); the self-report otherwise."""
    if not tool_calls:
        return {"ran_tests": bool(parsed.ran_tests), "tried_mutant": bool(parsed.tried_mutant),
                "observed": False}
    saw_tests = any(any(m in args for m in TEST_MARKERS) for _, args in tool_calls)
    saw_edit = any(name in EDIT_TOOLS or any(m in args for m in EDIT_MARKERS) for name, args in tool_calls)
    return {"ran_tests": saw_tests, "tried_mutant": bool(parsed.tried_mutant) and saw_edit, "observed": True}


def render(findings: list[dict]) -> str:
    """Plain text for a fix prompt or a blinded judge: no model names anywhere."""
    out = []
    for f in findings:
        where = f["file"] + (f":{f['line']}" if f["line"] is not None else "")
        out.append(f"[{f['severity']}] {where} - {f['title']}\n    {f['scenario']}")
    return "\n".join(out)
