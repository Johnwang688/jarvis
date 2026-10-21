"""Read the reviewer's machine-readable block, and derive the run flags."""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field

from . import config

FENCE = re.compile(r"```(?:json)?[ \t]*\n(.*?)```", re.S)

# Tools that edit a file by themselves.
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "file_change", "edit_file", "write_file", "apply_patch"}
# Tools whose argument is a shell command; only these can "run the tests" or
# make a shell edit.
SHELL_TOOLS = {"Bash", "shell", "command_execution", "run_command", "run_readonly", "local_shell"}

TEST_MARKERS = ("_check.py", "pytest", "vitest", "unittest", "test_", "_test.py", ".test.ts")
INTERPRETERS = re.compile(r"^(?:python(?:\d+(?:\.\d+)?)?|pytest|py\.test|vitest|node|npx|uv|bun|deno)$")
SHELLS = {"bash", "sh", "zsh", "dash"}
WRAPPERS = {"env", "timeout", "nice", "nohup", "time", "command", "exec", "stdbuf", "systemd-run", "sudo", "xargs"}
SOURCE_RE = re.compile(r"\.(?:py|pyi|ts|tsx|js|jsx|mjs|cjs|json|toml|md|sh|css|html)$")
REDIRECT_RE = re.compile(r"(?<![<>\d&])(?:\d?>>?|&>>?)\s*(?!&)(?!/dev/)([\w./@+-]+)")
PY_WRITE_RE = re.compile(r"write_text\(|write_bytes\(|\bopen\([^)]*['\"][wax]\+?b?['\"]|\.write\(|os\.replace\(|shutil\.(?:copy|move)")


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


# --- what the tool calls show ----------------------------------------------------------


def command_of(name: str, args: str) -> str | None:
    """The shell command a tool call ran, or None for a non-shell tool."""
    if name not in SHELL_TOOLS:
        return None
    try:
        doc = json.loads(args)
    except (json.JSONDecodeError, TypeError):
        return args
    if isinstance(doc, dict):
        cmd = doc.get("command") if "command" in doc else doc.get("cmd")
        if isinstance(cmd, list):
            return " ".join(map(str, cmd))
        return str(cmd or "")
    return args


def split_segments(command: str) -> list[str]:
    """Split on unquoted `;`, `&&`, `||`, `|`, `&` and newlines."""
    out, cur, quote, i = [], [], None, 0
    text = command or ""
    while i < len(text):
        c = text[i]
        if quote:
            cur.append(c)
            if c == "\\" and quote == '"' and i + 1 < len(text):
                cur.append(text[i + 1])
                i += 1
            elif c == quote:
                quote = None
        elif c in "'\"":
            quote = c
            cur.append(c)
        elif c == "\\" and i + 1 < len(text):
            cur += [c, text[i + 1]]
            i += 1
        elif c in ";|&\n":
            if c == "&" and (text[i - 1:i] in (">", "<") or text[i + 1:i + 2] == ">"):
                cur.append(c)              # 2>&1, &>file: a redirect, not a separator
            else:
                out.append("".join(cur))
                cur = []
                if text[i + 1:i + 2] == c and c in "|&":
                    i += 1
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    return [seg for seg in (x.strip() for x in out) if seg]


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, comments=False, posix=True)
    except ValueError:
        return segment.split()


def _strip_wrappers(tokens: list[str]) -> list[str]:
    i = 0
    while i < len(tokens):
        t = tokens[i]
        base = t.rsplit("/", 1)[-1]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t):
            i += 1
        elif base in WRAPPERS:
            i += 1
            while i < len(tokens) and (tokens[i].startswith("-") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[i])
                                       or (base == "timeout" and re.match(r"^\d", tokens[i]))):
                i += 1
            if i < len(tokens) and tokens[i] == "--":
                i += 1
        else:
            break
    return tokens[i:]


def segments(command: str, depth: int = 0) -> list[list[str]]:
    """Each simple command as tokens, wrappers stripped, `bash -c '...'` unwrapped."""
    out = []
    for seg in split_segments(command):
        toks = _strip_wrappers(_tokens(seg))
        if not toks:
            continue
        base = toks[0].rsplit("/", 1)[-1]
        if base in SHELLS and depth < 3:
            for j, t in enumerate(toks[1:], 1):
                if t.startswith("-") and "c" in t.lstrip("-") and j + 1 < len(toks):
                    out += segments(toks[j + 1], depth + 1)
                    break
            else:
                out.append(toks)
            continue
        out.append(toks)
    return out


def runs_tests(command: str) -> bool:
    for toks in segments(command):
        base = toks[0].rsplit("/", 1)[-1]
        if not INTERPRETERS.match(base):
            continue
        rest = " ".join(toks[1:])
        if base in ("npx", "bun") and "vitest" not in rest and "jest" not in rest:
            continue
        if base == "uv" and not (toks[1:2] == ["run"] and any(m in rest for m in TEST_MARKERS)):
            continue
        if base in ("pytest", "py.test", "vitest") or any(m in rest for m in TEST_MARKERS):
            return True
    return False


def edits_files(command: str) -> bool:
    """A shell command that changes a file: sed/perl in place (any flag
    position), cp/mv/install/tee onto a source file, a redirect to one (heredoc
    writes included), git apply/patch, or a python snippet that writes."""
    for m in REDIRECT_RE.finditer(command or ""):
        if SOURCE_RE.search(m.group(1)):
            return True
    for toks in segments(command):
        base = toks[0].rsplit("/", 1)[-1]
        args = toks[1:]
        if base == "sed" and any(a == "--in-place" or a.startswith("--in-place=") or re.match(r"^-[A-Za-z]*i", a)
                                 for a in args):
            return True
        if base == "perl" and any(re.match(r"^-(?![IMmed])[A-Za-z0-9]*i", a) for a in args):
            return True
        if base in ("cp", "mv", "install", "rsync", "ln") and args and SOURCE_RE.search(args[-1]) \
                and not args[-1].startswith("/tmp/"):
            return True
        if base == "tee" and any(SOURCE_RE.search(a) for a in args if not a.startswith("-")):
            return True
        if base == "git" and args[:1] in (["apply"], ["am"]) or (base == "git" and args[:2] == ["stash", "pop"]):
            return True
        if base == "patch" or base == "apply_patch":
            return True
        if INTERPRETERS.match(base) and base.startswith("python") and any(a in ("-c", "-") or a.startswith("<<") for a in args):
            if PY_WRITE_RE.search(command):
                return True
    if re.search(r"\bpython[\d.]*\s+(?:-\s*)?<<", command or "") and PY_WRITE_RE.search(command or ""):
        return True
    return False


def flags(tool_calls: list, parsed: Parsed) -> dict:
    """ran_tests / tried_mutant. Observed from the tool calls when there are any
    (a model's say-so is not enough for 'tried a mutant': it must also have
    edited something); the self-report otherwise. Only shell tools count for
    tests and shell edits: a Read or Grep that mentions `_check.py` or
    `write_text` is not a test run or an edit."""
    if not tool_calls:
        return {"ran_tests": bool(parsed.ran_tests), "tried_mutant": bool(parsed.tried_mutant),
                "observed": False}
    saw_tests = saw_edit = False
    for name, args in tool_calls:
        if name in EDIT_TOOLS:
            saw_edit = True
            continue
        command = command_of(name, args)
        if command is None:
            continue
        saw_tests = saw_tests or runs_tests(command)
        saw_edit = saw_edit or edits_files(command)
    return {"ran_tests": saw_tests, "tried_mutant": bool(parsed.tried_mutant) and saw_edit, "observed": True}


def render(findings: list[dict]) -> str:
    """Plain text for a fix prompt or a blinded judge: no model names anywhere."""
    out = []
    for f in findings:
        where = f["file"] + (f":{f['line']}" if f["line"] is not None else "")
        out.append(f"[{f['severity']}] {where} - {f['title']}\n    {f['scenario']}")
    return "\n".join(out)
