"""Content search across files.

`run_readonly` already allows grep, but it forbids shell operators, so there is
no way to write `rg -n foo | head -50`: the model gets the whole dump or
nothing, and the dump then lives in the transcript forever. That made searching
a codebase one of the most expensive things Jarvis could do, and every one of
those tokens pushed the compactor closer.

So the limiting is inside the tool instead of downstream of it. `mode` picks how
much shape comes back (which files / how many / the lines themselves) and
`max_results` bounds it, so the model can ask a narrow question and get a narrow
answer.

Deliberately its own module rather than part of `files.py`: `files.py` is in
SELF_PROTECTED and can only be changed by the owner by hand. That boundary
exists to protect the *write* guard, and there is no reason to freeze a
read-only search tool alongside it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from fnmatch import fnmatch
from pathlib import Path
from typing import Annotated

from . import tool
from .secrets import is_protected

MODES = ("content", "files", "count")
TIMEOUT_S = 30
# Skip anything this large rather than pulling it into memory line by line;
# a hit inside a 50MB artifact is not what anybody is looking for.
MAX_FILE_BYTES = 2_000_000

# Directories neither backend descends into. **One list, used by both**, which
# is the whole point: this tool had two implementations with two different
# ideas of what to skip, so the answer depended on whether `rg` happened to be
# installed on the machine.
SKIP_DIRS = frozenset(
    {
        "node_modules", "__pycache__", ".venv", "venv", ".git", "site-packages",
        ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    }
)


def _skip_dir(name: str) -> bool:
    # Hidden directories are skipped by both backends — ripgrep does it by
    # default, and the fallback matches it here rather than inventing its own
    # rule.
    return name.startswith(".") or name in SKIP_DIRS


def _rg_args(
    pattern: str, path: str, glob: str, mode: str, context_lines: int, case_insensitive: bool
) -> list[str]:
    """The ripgrep invocation, built to match `_python_search` exactly.

    **`--no-ignore` is the load-bearing flag.** ripgrep respects `.gitignore`
    by default, and this repo gitignores `memory/*.md`, `avatars/`, `designs/`
    and `traces/` — so with ripgrep installed, searching Jarvis's own long-term
    memory silently returned nothing, while the pure-Python fallback found it.
    A tool whose answer depends on which binaries happen to be on the machine
    is worse than a slow one.

    Version control is not a relevance filter for an agent: what is worth
    committing and what is worth searching are different questions, and memory
    is the case that proves it. So ignore files are disabled and the skipping
    is done explicitly, by the same `SKIP_DIRS` the fallback walks with.

    **`--null` and `--with-filename` are the other two load-bearing flags**, and
    they are here for the protected-file filter rather than for output. ripgrep
    normally separates the path from the rest of a line with `:` for a match and
    `-` for a *context* line, and omits the path entirely when it was handed a
    single file — so a filter that split each line on its first `:` was reading
    a line number, or half a path, whenever either of those applied. `--null`
    puts a NUL between the path and everything after it, which no path can
    contain, and `--with-filename` guarantees the path is there at all. See
    `_rg_lines`.
    """
    args = [
        "rg", "--color", "never", "--no-messages", "--no-ignore",
        "--null", "--with-filename",
    ]
    if mode == "files":
        args.append("--files-with-matches")
    elif mode == "count":
        args.append("--count")
    else:
        args += ["--line-number", "--no-heading"]
        if context_lines > 0:
            args += ["--context", str(context_lines)]
    if case_insensitive:
        args.append("--ignore-case")
    # Matches the fallback's own ceiling, so one backend cannot surface a hit
    # inside a huge artifact that the other skipped.
    args += ["--max-filesize", str(MAX_FILE_BYTES)]
    for name in sorted(SKIP_DIRS):
        args += ["--glob", f"!{name}/"]
    if glob:
        args += ["--glob", glob]
    args += ["--regexp", pattern, "--", path]
    return args


def _rg_lines(stdout: str, mode: str) -> list[str]:
    """ripgrep's NUL-separated output, filtered per *file* and re-rendered.

    This is the half of the tool that has to agree with `_python_search`, and it
    used to disagree in three separate ways — all of them the same mistake, of
    filtering by the first colon-delimited field of a line instead of by which
    file the line came from:

      * a **context** line is attributed with `-`, not `:`, so every
        `google_token.json-3-  "refresh_token": …` inside a `context_lines`
        window sailed through the filter;
      * given a **single file** as `path`, rg prints no filename at all, so the
        field being checked was a *line number* and `grep_files('KEY', '.env')`
        returned the file verbatim;
      * in **count** mode on a single file the whole output is a bare number,
        which turned a protected file into a working match oracle: ask for
        `^OPENROUTER_API_KEY=sk-or-v1-FAKEL` and the count says whether the
        guess is right, one character at a time, with no secret value ever in
        the output for `dispatch()`'s scrub to catch.

    The fallback never had any of these, because it filters whole files before
    reading them (`is_protected(target)`), which is the only check that is
    actually about the thing being protected. So this now does the same: take
    the path off each record, decide once, and re-render the shape the fallback
    produces. A record we cannot attribute to a file is dropped — this is a
    credential filter, so "unrecognised" has to mean "withheld".
    """
    if mode == "files":
        # `--files-with-matches --null` NUL-*terminates* each path and emits no
        # newlines at all, so this stream is split differently from the others.
        return [p for p in stdout.split("\0") if p.strip() and not is_protected(p)]

    out: list[str] = []
    for record in stdout.splitlines():
        if not record.strip():
            continue
        path, sep, rest = record.partition("\0")
        if not sep or is_protected(path):
            continue
        if mode == "count":
            out.append(f"{path}:{rest}")
            continue
        # `rest` is `<lineno><sep><text>`; that separator is `:` for a match and
        # `-` for a context line, and it is also what joined the path before
        # --null replaced it. Reuse it so the rendered shape is byte-identical
        # to the fallback's.
        digits = 0
        while digits < len(rest) and rest[digits].isdigit():
            digits += 1
        join = rest[digits] if digits and digits < len(rest) and rest[digits] in ":-" else ":"
        out.append(f"{path}{join}{rest}")
    return out


def _python_search(
    pattern: str, root: Path, glob: str, mode: str, context_lines: int, case_insensitive: bool
) -> list[str]:
    """Fallback for machines without ripgrep. Same output shapes, slower."""
    flags = re.IGNORECASE if case_insensitive else 0
    regex = re.compile(pattern, flags)

    targets: list[Path] = []
    if root.is_file():
        targets = [root]
    else:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not _skip_dir(d)]
            for name in filenames:
                if name.startswith(".") or (glob and not fnmatch(name, glob)):
                    continue
                targets.append(Path(dirpath) / name)

    out: list[str] = []
    for target in targets:
        if is_protected(target):
            continue
        try:
            if target.stat().st_size > MAX_FILE_BYTES:
                continue
            lines = target.read_text(encoding="utf-8", errors="strict").splitlines()
        except (OSError, UnicodeDecodeError):
            continue  # binary or unreadable — not a search result

        hits = [i for i, line in enumerate(lines) if regex.search(line)]
        if not hits:
            continue
        if mode == "files":
            out.append(str(target))
            continue
        if mode == "count":
            out.append(f"{target}:{len(hits)}")
            continue
        shown: set[int] = set()
        for i in hits:
            for j in range(max(0, i - context_lines), min(len(lines), i + context_lines + 1)):
                shown.add(j)
        for j in sorted(shown):
            sep = ":" if j in hits else "-"
            out.append(f"{target}{sep}{j + 1}{sep}{lines[j]}")
    return out


@tool
def grep_files(
    pattern: Annotated[str, "Regular expression to search for"],
    path: Annotated[str, "File or directory to search in"] = ".",
    glob: Annotated[str, "Only search files matching this glob, e.g. '*.py'"] = "",
    mode: Annotated[
        str,
        "'content' for the matching lines (default), 'files' for just the file "
        "paths, 'count' for how many matches each file has",
    ] = "content",
    context_lines: Annotated[int, "Lines of context around each match, content mode only"] = 0,
    max_results: Annotated[int, "Most lines or files to return (default 100)"] = 100,
    case_insensitive: Annotated[bool, "Ignore case"] = False,
) -> str:
    """Search file contents for a regular expression.

    Prefer this over `run_readonly grep` — it bounds its own output, so a broad
    search costs a summary instead of a dump. Start with mode='files' to find
    out *where* something lives, then read or grep those files specifically.
    """
    if mode not in MODES:
        return f"Error: mode must be one of {', '.join(MODES)}."
    root = Path(path).expanduser().resolve()
    if not root.exists():
        return f"Error: {root} does not exist."
    try:
        re.compile(pattern)
    except re.error as exc:
        return f"Error: {pattern!r} is not a valid regular expression ({exc})."

    limit = max(1, int(max_results or 100))
    context_lines = max(0, min(int(context_lines or 0), 10))

    lines: list[str]
    if shutil.which("rg"):
        try:
            proc = subprocess.run(
                _rg_args(pattern, str(root), glob, mode, context_lines, case_insensitive),
                capture_output=True,
                text=True,
                timeout=TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return f"Error: search timed out after {TIMEOUT_S}s. Narrow `path` or `glob`."
        # rg exits 1 for "no matches", which is an answer rather than a failure.
        if proc.returncode not in (0, 1):
            return f"Error: search failed: {(proc.stderr or '').strip()[:300]}"
        lines = _rg_lines(proc.stdout, mode)
    else:
        try:
            lines = _python_search(pattern, root, glob, mode, context_lines, case_insensitive)
        except re.error as exc:
            return f"Error: {exc}"

    if not lines:
        return f"No matches for {pattern!r} under {root}."

    shown = lines[:limit]
    body = "\n".join(shown)
    if len(lines) > limit:
        body += (
            f"\n\n[showing {limit} of {len(lines)} results. Narrow the pattern, "
            "pass a glob, or raise max_results.]"
        )
    return body
