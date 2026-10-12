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

from dataclasses import dataclass
import os
import re
import shutil
import subprocess
import threading
import time
from fnmatch import fnmatch
from pathlib import Path
from typing import Annotated, Callable, Iterable

from . import tool
from .secrets import is_env_credential, is_protected

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


def refused(path: str | Path) -> bool:
    """**The one protected-file filter** for a search: a credential bundle by
    name (`secrets.PROTECTED_NAMES`) or a credential env file
    (`secrets.is_env_credential`: `.env`, `.env.<anything>` but `*.example`,
    by its own name, a symlink's or where it resolves).

    `grep_files` (both backends, every mode) and the HUD's find in files
    (`jarvis/v2/editor_nav.py`, ED-7) both ask this and nothing else keeps a
    copy, so the two refuse exactly the same files — a parity the ED-7 suite
    pins. The HUD's route refuses more on top (`editor_policy`'s credential
    folders and login files), never less."""
    return is_protected(path) or is_env_credential(path)


@dataclass
class Hit:
    """One search result, before any rendering: what both callers share.

    `line` is 1-based (None in files and count modes, and for a record rg
    printed in a shape we could not split); `context` marks a context line;
    `count` is count mode's number."""
    path: str
    line: int | None = None
    text: str = ""
    context: bool = False
    count: int | None = None
    sep: str = ":"

    def render(self, mode: str) -> str:
        """grep_files' text shapes, byte-identical to what they always were."""
        if mode == "files":
            return self.path
        if mode == "count":
            return f"{self.path}:{self.count}"
        if self.line is None:
            return f"{self.path}{self.sep}{self.text}"
        return f"{self.path}{self.sep}{self.line}{self.sep}{self.text}"


def _skip_dir(name: str, hidden: bool = False, skip: frozenset = SKIP_DIRS) -> bool:
    # Hidden directories are skipped by both backends — ripgrep does it by
    # default, and the fallback matches it here rather than inventing its own
    # rule. `hidden` (the HUD's find in files) searches them, but never SKIP_DIRS.
    return (not hidden and name.startswith(".")) or name in skip


def _rg_args(
    pattern: str, path: str, glob: str, mode: str, context_lines: int, case_insensitive: bool,
    *, respect_ignore: bool = False, hidden: bool = False, fixed: bool = False,
    skip: Iterable[str] = SKIP_DIRS, max_columns: int = 0,
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
    is done explicitly, by the same `SKIP_DIRS` the fallback walks with. (The
    HUD's find in files asks for `respect_ignore`, with a toggle: the owner's
    search is a person's, and build output is noise there — editor plan §2.8.)

    **`--null` and `--with-filename` are the other two load-bearing flags**, and
    they are here for the protected-file filter rather than for output. ripgrep
    normally separates the path from the rest of a line with `:` for a match and
    `-` for a *context* line, and omits the path entirely when it was handed a
    single file — so a filter that split each line on its first `:` was reading
    a line number, or half a path, whenever either of those applied. `--null`
    puts a NUL between the path and everything after it, which no path can
    contain, and `--with-filename` guarantees the path is there at all. See
    `_rg_lines`.

    The keyword-only options are the HUD's (ED-7); their defaults leave
    `grep_files`' invocation exactly as it was.
    """
    args = [
        "rg", "--color", "never", "--no-messages",
    ]
    if not respect_ignore:
        args.append("--no-ignore")
    args += ["--null", "--with-filename"]
    if hidden:
        args.append("--hidden")
    if mode == "files":
        args.append("--files-with-matches")
    elif mode == "count":
        args.append("--count")
    else:
        args += ["--line-number", "--no-heading"]
        if context_lines > 0:
            args += ["--context", str(context_lines)]
        if max_columns > 0:
            args += ["--max-columns", str(max_columns), "--max-columns-preview"]
    if case_insensitive:
        args.append("--ignore-case")
    if fixed:
        args.append("--fixed-strings")
    # Matches the fallback's own ceiling, so one backend cannot surface a hit
    # inside a huge artifact that the other skipped.
    args += ["--max-filesize", str(MAX_FILE_BYTES)]
    for name in sorted(skip):
        args += ["--glob", f"!{name}/"]
    if glob:
        args += ["--glob", glob]
    args += ["--regexp", pattern, "--", path]
    return args


def _rg_hits(stdout: str, mode: str) -> list[Hit]:
    """ripgrep's NUL-separated output, filtered per *file*, as records.

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
    reading them (`refused(target)`), which is the only check that is
    actually about the thing being protected. So this now does the same: take
    the path off each record, decide once, and keep the shape the fallback
    produces. A record we cannot attribute to a file is dropped — this is a
    credential filter, so "unrecognised" has to mean "withheld".
    """
    if mode == "files":
        # `--files-with-matches --null` NUL-*terminates* each path and emits no
        # newlines at all, so this stream is split differently from the others.
        return [Hit(p) for p in stdout.split("\0") if p.strip() and not refused(p)]

    out: list[Hit] = []
    verdicts: dict[str, bool] = {}
    for record in stdout.splitlines():
        hit = _rg_record(record, mode, verdicts)
        if hit is not None:
            out.append(hit)
    return out


def _rg_record(record: str, mode: str, verdicts: dict[str, bool]) -> Hit | None:
    """One line of rg's content or count output, or None (unattributable, or refused)."""
    if not record.strip():
        return None
    path, sep, rest = record.partition("\0")
    if not sep:
        return None
    if path not in verdicts:
        verdicts[path] = refused(path)
    if verdicts[path]:
        return None
    if mode == "count":
        try:
            return Hit(path, count=int(rest))
        except ValueError:
            return None
    # `rest` is `<lineno><sep><text>`; that separator is `:` for a match and
    # `-` for a context line, and it is also what joined the path before
    # --null replaced it. Keep it so the rendered shape is byte-identical to
    # the fallback's.
    digits = 0
    while digits < len(rest) and rest[digits].isdigit():
        digits += 1
    if digits and digits < len(rest) and rest[digits] in ":-":
        join = rest[digits]
        return Hit(path, line=int(rest[:digits]), text=rest[digits + 1:], context=join == "-", sep=join)
    return Hit(path, text=rest)


def _rg_lines(stdout: str, mode: str) -> list[str]:
    """`_rg_hits`, rendered as `grep_files` has always printed them."""
    return [h.render(mode) for h in _rg_hits(stdout, mode)]


def _python_hits(
    pattern: str, root: Path, glob: str, mode: str, context_lines: int, case_insensitive: bool,
    *, hidden: bool = False, fixed: bool = False, skip: frozenset = SKIP_DIRS,
    limit: int | None = None, deadline: float | None = None,
) -> tuple[list[Hit], bool]:
    """Fallback for machines without ripgrep. Same output shapes, slower.

    Returns the hits and whether a bound (`limit` hits, or `deadline`) cut it short."""
    flags = re.IGNORECASE if case_insensitive else 0
    regex = re.compile(re.escape(pattern) if fixed else pattern, flags)

    targets: list[Path] = []
    if root.is_file():
        targets = [root]
    else:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not _skip_dir(d, hidden, skip))
            for name in sorted(filenames):
                if (not hidden and name.startswith(".")) or (glob and not fnmatch(name, glob)):
                    continue
                targets.append(Path(dirpath) / name)
            if deadline is not None and time.monotonic() > deadline:
                return [], True

    out: list[Hit] = []
    for target in targets:
        if deadline is not None and time.monotonic() > deadline:
            return out, True
        if refused(target):
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
            out.append(Hit(str(target)))
            continue
        if mode == "count":
            out.append(Hit(str(target), count=len(hits)))
            continue
        shown: set[int] = set()
        for i in hits:
            for j in range(max(0, i - context_lines), min(len(lines), i + context_lines + 1)):
                shown.add(j)
        hit_set = set(hits)
        for j in sorted(shown):
            sep = ":" if j in hit_set else "-"
            out.append(Hit(str(target), line=j + 1, text=lines[j], context=sep == "-", sep=sep))
            if limit is not None and len(out) >= limit:
                return out, True
    return out, False


def _python_search(
    pattern: str, root: Path, glob: str, mode: str, context_lines: int, case_insensitive: bool
) -> list[str]:
    """Fallback for machines without ripgrep. Same output shapes, slower."""
    hits, _ = _python_hits(pattern, root, glob, mode, context_lines, case_insensitive)
    return [h.render(mode) for h in hits]


@dataclass
class Found:
    """A bounded search's answer (`search_bounded`)."""
    hits: list[Hit]
    truncated: bool = False      # stopped at `limit`
    timed_out: bool = False      # stopped at the deadline
    error: str | None = None
    backend: str = "rg"


def search_bounded(
    pattern: str, root: Path, *, glob: str = "", case_insensitive: bool = False,
    fixed: bool = False, respect_ignore: bool = True, hidden: bool = True,
    skip: frozenset = SKIP_DIRS, limit: int = 2000, timeout: float = 10.0,
    max_columns: int = 1000, use_rg: bool | None = None,
    keep: Callable[[Hit], bool] | None = None,
) -> Found:
    """Content search with **both** bounds a window needs (ED-7's find in
    files): at most `limit` hits and `timeout` seconds, and the one
    protected-file filter (`refused`) applied exactly as `grep_files`
    applies it — same records, same refusals. `keep` is the caller's own
    stricter filter on top (it is asked only about hits `refused` let
    through), never instead.

    ripgrep's output is read as it comes and the process is killed at the
    limit or the deadline, so a broad pattern over a large tree costs a
    bounded amount of memory and time, never the whole dump."""
    deadline = time.monotonic() + timeout
    if use_rg is None:
        use_rg = bool(shutil.which("rg"))
    if not use_rg:
        hits, cut = _python_hits(pattern, root, glob, "content", 0, case_insensitive, hidden=hidden,
                                 fixed=fixed, skip=skip, limit=None, deadline=deadline)
        kept = [h for h in hits if keep is None or keep(h)]
        timed = cut and time.monotonic() > deadline
        return Found(kept[:limit], truncated=len(kept) > limit, timed_out=timed, backend="python")
    args = _rg_args(pattern, str(root), glob, "content", 0, case_insensitive, respect_ignore=respect_ignore,
                    hidden=hidden, fixed=fixed, skip=skip, max_columns=max_columns)
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL)
    except OSError as exc:
        return Found([], error=f"search could not start ({type(exc).__name__})")
    timed = threading.Event()

    def expire():
        timed.set()
        proc.kill()

    timer = threading.Timer(max(0.0, deadline - time.monotonic()), expire)
    timer.daemon = True
    timer.start()
    found = Found([])
    verdicts: dict[str, bool] = {}
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            hit = _rg_record(raw.decode("utf-8", errors="replace").rstrip("\r\n"), "content", verdicts)
            if hit is None or (keep is not None and not keep(hit)):
                continue
            if len(found.hits) >= limit:
                found.truncated = True
                proc.kill()
                break
            found.hits.append(hit)
    finally:
        timer.cancel()
        try:
            proc.kill()
        except OSError:
            pass
        _, err = proc.communicate()
    found.timed_out = timed.is_set() and not found.truncated
    if proc.returncode not in (0, 1) and not found.truncated and not found.timed_out:
        found.error = "search failed: " + (err or b"").decode("utf-8", "replace").strip()[:300]
    return found


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
