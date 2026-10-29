"""What code knows about one pull request (PM-B of
`docs/plans/2026-10-28-pr-inbox-merge.md`: PM-1, PM-2, PM-3, PM-9 to PM-12).

**Reads, by the daemon only (PM-1).** `read_pr(repo, number)` makes every
call through `inbox_prs.run_bounded`: an exact argv, no shell, cwd `/`, its
own process group, a timeout and an output cap, stderr never read. `gh`
runs with `chat_git._owner_env()` and nothing else:

1. `gh pr view N --repo O/R --json <VIEW_FIELDS>`;
2. `gh pr diff N --repo O/R`, at most `DIFF_CAP` (past it, the file list only);
3. `gh api --method GET repos/O/R/commits/<head>/check-runs?per_page=100`;
4. for at most `LOG_JOBS` failing Actions jobs (the job id from a validated
   `details_url`): `gh run view --job <id> --repo O/R --log-failed`, the last
   `LOG_LINES` lines kept;
5. `gh repo view O/R --json mergeCommitAllowed,squashMergeAllowed,viewerPermission`,
   cached for `REPO_CACHE_S`;
6. only when the PR is DIRTY, its conflicting files: a credentialed
   `git fetch` of exactly the base branch and `refs/pull/N/head` into a
   daemon-only bare mirror (`V2_DATA_DIR/pr-mirrors/<key>.git`, 0700; never
   the Codex-readable `chat-git-mirrors`), then
   `git merge-tree --write-tree --name-only -z --no-messages <base> <head>`
   and `git cat-file blob <tree>:<path>` for the marker regions of at most
   `CONFLICT_FILES` files. No checkout is made, no owner repository is
   touched.

**No merge driver or filter can run (PM-1.6).** A merge driver runs only
when an attribute names it *and* config defines it. The local git calls
(`init`, `merge-tree`, `cat-file`) close both sides: attributes are read
from the empty tree (`--attr-source`), never from the PR's own
`.gitattributes`, and `core.attributesFile` is `/dev/null`, with no system
attributes; config is the mirror's own (made here with no template) and the
command line only — an empty HOME, `GIT_CONFIG_GLOBAL=/dev/null`, no system
config — so even the owner's global merge drivers and LFS filters are out
of reach. A mirror holding `info/attributes` is refused. `merge.renormalize`
is off (it would run clean filters). On top, gitops' hardening: no hooks,
fsmonitor off, no submodule recursion, `ext::` refused. The fetch (the one
credentialed git) adds `chat_git._credential_pins()` and allows HTTPS only.

**Validation.** The repo is `OWNER/REPO` by `chat_git.github_repo`'s rule,
the number a positive int, SHAs 40 hex, branch names `valid_branch`'s. A
malformed answer is "not read" for that part, never a guess (`PrFacts.read`).

**Scrub (PM-3, use-but-never-see).** Every text read (title, body, paths,
check names and output, patches, logs, conflict regions) goes through
`scrub` before it is stored or returned: `untrusted.strip_invisible`,
`secrets.redact_inline`, `redact.for_display` (which runs `secrets.scrub`
and every credential shape) — `chat_reads.scrub`, the owner-display scrub.
A credential-named path (`secrets.is_protected` on any component) keeps
its name; its patch and its conflict regions are withheld unread. Nothing
read is ever logged: a log line names an exception's type only.

**What code says (PM-9 to PM-12).** `facts_line` (trusted: validated
fields, untrusted names cleaned so they cannot draw a separator or a code
label), `flags` (PM-10's safety-file families), `SECRET_SCANNERS` (PM-11;
the GitHub App slug decides, never a check's name, which any workflow can
choose), `decide`/`actions` (PM-12's table, a pure function of the facts,
the hold and whether the phone is limited) and `model_input` (the capped
text the summary model reads; PM-C fences it and makes the call).

Nothing here is wired into the daemon: `FactsWorker` is the one-at-a-time
worker (PM-2) a later package attaches. Tests: `tests/v2/pr_facts_check.py`.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import threading
import time
from typing import Callable

from .approvals import clean_line
from .inbox_prs import GhFailed

LOG = logging.getLogger(__name__)

# -- PM-2: caps ---------------------------------------------------------------------------

CALL_TIMEOUT_S = 30.0
PASS_TIMEOUT_S = 180.0
MAX_WAITING = 50
VIEW_CAP = 2 * 1024 * 1024
DIFF_CAP = 2 * 1024 * 1024
CHECKS_CAP = 4 * 1024 * 1024
LOG_CAP = 4 * 1024 * 1024
REPO_CAP = 64 * 1024
GIT_CAP = 1024 * 1024
BLOB_CAP = 1024 * 1024
REPO_CACHE_S = 3600.0
LOG_JOBS = 3
LOG_LINES = 80
LOG_MARGIN_LINES = 200          # scrubbed with the tail, so a key's header is seen
CONFLICT_FILES = 3
NAMES_MAX = 1000

MODEL_CAP = 20_000
TITLE_CAP = 300
BODY_CAP = 2_000
LOGS_CAP = 6_000                # logs and check output, in all
CONFLICTS_CAP = 3_000
FILE_PATCH_CAP = 4_000
CHECK_TEXT_CAP = 1_000

VIEW_FIELDS = ("number", "title", "body", "baseRefName", "baseRefOid", "headRefName",
               "headRefOid", "files", "isCrossRepository", "mergeStateStatus",
               "statusCheckRollup", "reviewDecision",
               # Beyond PM-1's list: a PR closed, merged or made a draft since
               # the poll offers nothing, and the totals the facts line shows
               # (gh's `files` stops at 100).
               "state", "isDraft", "additions", "deletions", "changedFiles")
REPO_FIELDS = ("mergeCommitAllowed", "squashMergeAllowed", "viewerPermission")
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# Each part's state in `PrFacts.read`: "ok", or one of these.
READ_REASONS = {
    "missing": "gh or git is not installed",
    "timeout": "did not answer in time",
    "too_large": "the answer was too large",
    "login": "gh is not logged in",
    "exit": "the command failed",
    "malformed": "the answer could not be read",
    "error": "could not be run",
    "out_of_time": "the read ran out of time",
    "not_needed": "not needed",
    "refused": "the mirror could not be used",
}

_SHA = re.compile(r"[0-9a-f]{40}")
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,99}")
_MERGE_STATES = frozenset({"BEHIND", "BLOCKED", "CLEAN", "DIRTY", "DRAFT", "HAS_HOOKS",
                           "UNKNOWN", "UNSTABLE"})
_PR_STATES = frozenset({"OPEN", "CLOSED", "MERGED"})
_REVIEWS = frozenset({"APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED"})
_PERMISSIONS = frozenset({"ADMIN", "MAINTAIN", "WRITE", "TRIAGE", "READ", "NONE"})
_CAN_WRITE = frozenset({"ADMIN", "MAINTAIN", "WRITE"})
_FAILED = frozenset({"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE",
                     "ERROR", "STALE"})
_PASSED = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})

# -- PM-11: secret scanners --------------------------------------------------------------

# GitHub App slug -> the check names it posts (shown in docs and tests; the
# slug decides). A check run's app comes from GitHub, so a workflow cannot
# pass for a scanner by naming its job like one; a commit status can be
# posted under any name, so a status is never labelled one.
SECRET_SCANNERS: dict[str, tuple[str, ...]] = {
    "gitguardian": ("GitGuardian Security Checks",),
}
ACTIONS_APP = "github-actions"

# -- PM-10: safety files ----------------------------------------------------------------

FLAG_SELF_PROTECTED = "self-protected"
FLAG_AGENT = "agent instructions"
FLAG_CI = "CI"
FLAG_SCANNER = "secret-scanner config"
FLAG_CREDENTIAL = "credential file"
AGENT_FILES = frozenset({"claude.md", "claude.local.md", "agents.md"})
AGENT_DIRS = frozenset({".claude"})
CI_PREFIXES = (".github/workflows/", ".github/actions/")
SCANNER_CONFIGS = frozenset({".gitguardian.yaml", ".gitguardian.yml", ".gitguardian",
                             "gitguardian.yaml", "gitguardian.yml", ".gitleaks.toml",
                             ".gitleaksignore", ".secrets.baseline", ".trufflehog.yaml",
                             ".trufflehog.yml", ".talismanrc"})

# Named only in the model input (PM-2): lock and generated files.
LOCK_FILES = frozenset({"package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
                        "bun.lockb", "bun.lock", "cargo.lock", "poetry.lock", "uv.lock",
                        "pipfile.lock", "gemfile.lock", "composer.lock", "go.sum", "flake.lock",
                        "packages.lock.json", "pubspec.lock", "mix.lock", "podfile.lock"})
GENERATED_SUFFIXES = (".min.js", ".min.css", ".map", ".snap", ".pb.go", "_pb2.py", ".lock")

# -- PM-12: actions ------------------------------------------------------------------------

MERGE, MERGE_AS_IS, FIX, UPDATE = "merge", "merge_as_is", "fix", "update"


# -- the facts -------------------------------------------------------------------------------

@dataclass(frozen=True)
class FileChange:
    path: str                       # scrubbed and cleaned, for display
    additions: int | None
    deletions: int | None
    flags: tuple[str, ...]          # PM-10 families, from the raw path
    credential: bool


@dataclass(frozen=True)
class Check:
    name: str                       # scrubbed and cleaned
    state: str                      # "failing" (only failing checks are kept)
    scanner: bool | None            # None: the check runs could not be read
    app: str | None
    title: str = ""
    summary: str = ""
    job: int | None = None


@dataclass(frozen=True)
class Patch:
    path: str                       # scrubbed and cleaned
    kind: str                       # "shown", "lock", "binary", "withheld", "unreadable"
    text: str = ""                  # scrubbed, at most FILE_PATCH_CAP
    cut: bool = False


@dataclass(frozen=True)
class Log:
    name: str
    text: str                       # scrubbed, the last LOG_LINES lines


@dataclass(frozen=True)
class Conflict:
    path: str
    regions: str                    # scrubbed marker regions, "" when withheld
    withheld: bool = False


@dataclass(frozen=True)
class PrFacts:
    repo: str
    number: int
    read: dict = field(default_factory=dict)     # part -> "ok" or a READ_REASONS key
    state: str | None = None
    draft: bool | None = None
    title: str = ""
    body: str = ""
    body_cut: bool = False
    base: str | None = None
    base_oid: str | None = None
    branch: str | None = None
    head: str | None = None
    fork: bool | None = None
    merge_state: str = "UNKNOWN"
    checks: str | None = None                    # passing, failing, pending, none
    pending: int = 0
    review: str | None = None
    files: tuple[FileChange, ...] = ()
    files_total: int | None = None
    additions: int | None = None
    deletions: int | None = None
    failing: tuple[Check, ...] = ()
    patches: tuple[Patch, ...] = ()
    logs: tuple[Log, ...] = ()
    conflicts: tuple[str, ...] = ()
    regions: tuple[Conflict, ...] = ()
    merge_commit: bool | None = None
    squash: bool | None = None
    permission: str | None = None

    def ok(self, part: str) -> bool:
        return self.read.get(part) == "ok"

    @property
    def files_complete(self) -> bool:
        """Whether every changed path was seen (PM-10 fails closed if not)."""
        return (self.ok("files") and self.files_total is not None
                and len(self.files) >= self.files_total)


# -- scrubbing (PM-3) ------------------------------------------------------------------------

def scrub(text) -> str:
    """The owner-display scrub (`chat_reads.scrub`): invisible characters,
    inline credentials, Jarvis's own values and every credential shape out;
    `chat_reads.WITHHELD` if it cannot run."""
    from .chat_reads import scrub as display_scrub
    return display_scrub(text)


def scrub_head(text: str, cap: int, margin: int = 16_384) -> tuple[str, bool]:
    """(the scrubbed head of `text`, at most `cap` characters; whether any was
    left out). The scrub runs on a head `margin` past the cap, doubled until
    its scrubbed form reaches that far or is the whole text, so a credential
    the head's end cuts in two lies past the cut (`redact.for_display_capped`'s
    rule)."""
    if not text:
        return "", False
    size = cap + margin
    while True:
        head = text[:size]
        out = scrub(head)
        if size >= len(text) or len(out) >= cap + margin:
            break
        size *= 2
    whole = size >= len(text)
    if len(out) > cap:
        return out[:cap], True
    return out, not whole


def _tail_lines(text: str, lines: int, cap: int) -> str:
    """The last `lines` lines of `text`, scrubbed with `LOG_MARGIN_LINES` lines
    before them (so a key block the tail starts inside is still seen whole),
    at most `cap` characters, cut at a line."""
    raw = text.splitlines()[-(lines + LOG_MARGIN_LINES):]
    joined = "\n".join(raw)
    if len(joined) > 256 * 1024:
        joined = joined[-256 * 1024:]
        joined = joined.split("\n", 1)[1] if "\n" in joined else ""
    out = scrub(joined).splitlines()[-lines:]
    kept, size = [], 0
    for line in reversed(out):
        if size + len(line) + 1 > cap:
            break
        kept.append(line)
        size += len(line) + 1
    return "\n".join(reversed(kept))


_SHOWN_UNDERSCORE = frozenset("·•∙⋅・‧･⸱ꞏ•·<>﹤﹥＜＞")


def shown(text, cap: int = 120) -> str:
    """Untrusted text (already scrubbed) as one piece of a code-built line:
    one line, capped, no separator dot (it could forge a facts-line part),
    `[`/`]` as `(`/`)` (only code writes "[secret scanner]"), no quotation
    or angle brackets."""
    from .chat_reads import _card_char
    import unicodedata
    line = clean_line(unicodedata.normalize("NFC", str(text or "")), cap=10 * cap)
    out = []
    for ch in line:
        if ch == "[":
            out.append("(")
        elif ch == "]":
            out.append(")")
        elif ch in _SHOWN_UNDERSCORE:
            out.append("_")
        else:
            out.append(_card_char(ch))
    return clean_line(" ".join("".join(out).split()), cap=cap)


# -- validation ------------------------------------------------------------------------------

def valid_repo(repo) -> bool:
    """`OWNER/REPO` by `chat_git.github_repo`'s rule, and neither part
    starting with "-" (GitHub allows neither; an argv word never looks like
    an option)."""
    from .chat_git import github_repo
    return (isinstance(repo, str) and github_repo(f"https://github.com/{repo}") == repo
            and not any(part.startswith("-") for part in repo.split("/")))


def valid_number(number) -> bool:
    return isinstance(number, int) and not isinstance(number, bool) and 0 < number < 10 ** 10


def valid_sha(value) -> bool:
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def _branch(value) -> str | None:
    from .chat_git import valid_branch
    return value if isinstance(value, str) and len(value) <= 200 and valid_branch(value) else None


def _count(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 10 ** 9 \
        else None


def _json(data: bytes):
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise GhFailed("malformed") from None


# -- PM-10: flags ------------------------------------------------------------------------------

def _credential_path(path: str) -> bool:
    """Any component credential-named by the shared rule (`secrets.is_protected`:
    `.env*` but `*.example`, the bundles; case and Windows trimming)."""
    from jarvis.tools.secrets import is_protected
    return any(part and is_protected(part) for part in path.split("/"))


def path_flags(path: str) -> tuple[str, ...]:
    """PM-10's families for one changed path (the raw path, before display)."""
    from jarvis.tools.files import SELF_PROTECTED
    out = []
    folded = path.casefold()
    parts = [p.rstrip(" .") for p in folded.split("/")]
    name = parts[-1] if parts else ""
    if folded in {p.casefold() for p in SELF_PROTECTED}:
        out.append(FLAG_SELF_PROTECTED)
    if name in AGENT_FILES or any(p in AGENT_DIRS for p in parts[:-1]):
        out.append(FLAG_AGENT)
    if folded.startswith(CI_PREFIXES):
        out.append(FLAG_CI)
    if name in SCANNER_CONFIGS:
        out.append(FLAG_SCANNER)
    if _credential_path(path):
        out.append(FLAG_CREDENTIAL)
    return tuple(out)


def flagged(facts: PrFacts) -> bool:
    """True when any changed path is a safety file, or when not every path
    could be checked (fails closed: a limited phone answers neither)."""
    return not facts.files_complete or any(f.flags for f in facts.files)


def flags_line(facts: PrFacts, limit: int = 5) -> str | None:
    """The amber line, or None. Built by code; the model cannot remove it."""
    hits = [f for f in facts.files if f.flags]
    parts = [f"{f.path} ({', '.join(f.flags)})" for f in hits[:limit]]
    if len(hits) > limit:
        parts.append(f"and {len(hits) - limit} more")
    if not facts.files_complete:
        parts.append("not every changed file could be checked")
    return ("touches safety files: " + "; ".join(parts)) if parts else None


# -- parsing ------------------------------------------------------------------------------------

def _rollup(items) -> tuple[str, list[dict], int]:
    """(summary, failing items as {name, kind}, pending count)."""
    from .inbox_prs import checks_summary
    summary = checks_summary(items)
    failing, pending = [], 0
    for item in items or ():
        if "state" in item and "conclusion" not in item:
            name, kind = item.get("context"), "status"
            state = item.get("state")
            bad, waiting = state in _FAILED, state != "SUCCESS" and state not in _FAILED
        else:
            name, kind = item.get("name"), "check"
            status, conclusion = item.get("status"), item.get("conclusion")
            bad = status == "COMPLETED" and conclusion in _FAILED
            waiting = status != "COMPLETED" or (conclusion not in _FAILED and conclusion not in _PASSED)
        if not isinstance(name, str):
            name = ""
        if bad:
            failing.append({"name": name, "kind": kind})
        elif waiting:
            pending += 1
    return summary, failing, pending


def parse_view(data: bytes, repo: str, number: int) -> dict:
    """`gh pr view`'s answer, validated. GhFailed("malformed") when the core
    is not of the expected shape; a malformed file list or checks rollup is
    "not read" for that part only."""
    from .inbox_prs import clean_title
    item = _json(data)
    if not isinstance(item, dict) or item.get("number") != number:
        raise GhFailed("malformed")
    title, body = item.get("title"), item.get("body")
    fork, draft, state = item.get("isCrossRepository"), item.get("isDraft"), item.get("state")
    if not isinstance(title, str) or len(title) > 10_000 or not (body is None or isinstance(body, str)):
        raise GhFailed("malformed")
    if not isinstance(fork, bool) or not isinstance(draft, bool) or state not in _PR_STATES:
        raise GhFailed("malformed")
    merge_state = item.get("mergeStateStatus")
    review = item.get("reviewDecision")
    out = {
        "state": state, "draft": draft, "fork": fork,
        "title": clean_title(title),
        "base": _branch(item.get("baseRefName")),
        "base_oid": item.get("baseRefOid") if valid_sha(item.get("baseRefOid")) else None,
        "branch": _branch(item.get("headRefName")),
        "head": item.get("headRefOid") if valid_sha(item.get("headRefOid")) else None,
        "merge_state": merge_state if merge_state in _MERGE_STATES else "UNKNOWN",
        "review": review if review in _REVIEWS else None,
        "additions": _count(item.get("additions")),
        "deletions": _count(item.get("deletions")),
        "files_total": _count(item.get("changedFiles")),
        "read": {},
    }
    body = body or ""
    out["body"], out["body_cut"] = scrub_head(body[:1_000_000], BODY_CAP)
    try:
        summary, failing, pending = _rollup(item.get("statusCheckRollup"))
        out.update(checks=summary, failing_names=failing, pending=pending)
        out["read"]["rollup"] = "ok"
    except GhFailed:
        out.update(checks=None, failing_names=[], pending=0)
        out["read"]["rollup"] = "malformed"
    files = item.get("files")
    parsed = []
    if isinstance(files, list) and len(files) <= 10_000:
        for entry in files:
            path = entry.get("path") if isinstance(entry, dict) else None
            if not isinstance(path, str) or not path or len(path) > 4096 or "\x00" in path:
                parsed = None
                break
            parsed.append(FileChange(shown(scrub(path), 200), _count(entry.get("additions")),
                                     _count(entry.get("deletions")), path_flags(path),
                                     _credential_path(path)))
    else:
        parsed = None
    if parsed is None:
        out["files"], out["read"]["files"] = (), "malformed"
    else:
        out["files"], out["read"]["files"] = tuple(parsed), "ok"
        if out["files_total"] is None:
            out["files_total"] = len(parsed)
    return out


def parse_check_runs(data: bytes) -> list[dict]:
    """The check runs, each {name, app, conclusion, status, title, summary,
    details_url}, with types checked; GhFailed("malformed") otherwise."""
    item = _json(data)
    runs = item.get("check_runs") if isinstance(item, dict) else None
    if not isinstance(runs, list) or len(runs) > 1000:
        raise GhFailed("malformed")
    out = []
    for run in runs:
        if not isinstance(run, dict) or not isinstance(run.get("name"), str):
            raise GhFailed("malformed")
        app = run.get("app")
        slug = app.get("slug") if isinstance(app, dict) else None
        output = run.get("output") if isinstance(run.get("output"), dict) else {}
        out.append({
            "name": run["name"],
            "app": slug if isinstance(slug, str) and _SLUG.fullmatch(slug) else None,
            "status": run.get("status") if isinstance(run.get("status"), str) else None,
            "conclusion": run.get("conclusion") if isinstance(run.get("conclusion"), str) else None,
            "title": output.get("title") if isinstance(output.get("title"), str) else "",
            "summary": output.get("summary") if isinstance(output.get("summary"), str) else "",
            "details_url": run.get("details_url") if isinstance(run.get("details_url"), str) else None,
        })
    return out


def job_id(url, repo: str) -> int | None:
    """An Actions job id from a details URL that is exactly
    `https://github.com/<repo>/actions/runs/<run>/job/<job>` (any case of
    the repository's name), else None."""
    if not isinstance(url, str):
        return None
    match = re.fullmatch(r"https://github\.com/([A-Za-z0-9_.-]{1,100})/([A-Za-z0-9_.-]{1,100})"
                         r"/actions/runs/([1-9][0-9]{0,19})/job/([1-9][0-9]{0,19})", url)
    if not match or f"{match.group(1)}/{match.group(2)}".lower() != repo.lower():
        return None
    return int(match.group(4))


def _enrich(failing_names: list[dict], runs: list[dict] | None, repo: str) -> tuple[Check, ...]:
    """Each failing rollup item as a Check: a check run matched by name to a
    failing run in the check-runs answer takes its app, output and job."""
    out = []
    for item in failing_names:
        name = shown(scrub(item["name"]), 80) or "(unnamed check)"
        if item["kind"] == "status":
            out.append(Check(name, "failing", False, None))     # never a scanner (PM-11)
            continue
        match = None
        for run in runs or ():
            if run["name"] == item["name"] and run["conclusion"] and run["conclusion"].upper() in _FAILED:
                match = run
                break
        if runs is None:
            out.append(Check(name, "failing", None, None))
        elif match is None:
            out.append(Check(name, "failing", False, None))
        else:
            app = match["app"]
            out.append(Check(
                name, "failing", app in SECRET_SCANNERS, app,
                shown(scrub(match["title"][:2000]), 200),
                scrub_head(match["summary"][:200_000], CHECK_TEXT_CAP)[0],
                job_id(match["details_url"], repo) if app == ACTIONS_APP else None))
    return tuple(out)


# -- the diff ----------------------------------------------------------------------------------

def _unquote(text: str) -> str | None:
    """A git C-quoted path ("a/b\\303\\251") decoded; None if malformed."""
    if len(text) < 2 or not (text.startswith('"') and text.endswith('"')):
        return None
    body, out, i = text[1:-1], bytearray(), 0
    simple = {"\\": b"\\", '"': b'"', "t": b"\t", "n": b"\n", "a": b"\a", "b": b"\b",
              "f": b"\f", "r": b"\r", "v": b"\v"}
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out += ch.encode("utf-8")
            i += 1
            continue
        nxt = body[i + 1:i + 2]
        if nxt in simple:
            out += simple[nxt]
            i += 2
        elif re.fullmatch(r"[0-7]{3}", body[i + 1:i + 4] or ""):
            out.append(int(body[i + 1:i + 4], 8) & 0xFF)
            i += 4
        else:
            return None
    return out.decode("utf-8", "replace")


def _side(text: str, prefix: str) -> str | None:
    text = text.rstrip("\t")
    if text == "/dev/null":
        return None
    if text.startswith('"'):
        text = _unquote(text) or ""
    return text[len(prefix):] if text.startswith(prefix) and len(text) > len(prefix) else None


def _chunk_paths(lines: list[str]) -> tuple[str | None, set[str]]:
    """(the chunk's path, every path it names). The path is None when it
    cannot be read; the set decides whether the patch is withheld."""
    names: set[str] = set()
    header = lines[0][len("diff --git "):]
    main = None
    if not header.startswith('"'):
        n = len(header)
        if n % 2 == 1 and header[n // 2] == " ":
            left, right = header[:n // 2], header[n // 2 + 1:]
            if left.startswith("a/") and right.startswith("b/") and left[2:] == right[2:]:
                main = left[2:]
    else:
        names.add(header)            # quoted pair: judged as written too
    for word in header.split(" "):
        names.add(word[2:] if word[:2] in ("a/", "b/") else word)
    plus = minus = renamed = None
    for line in lines[1:]:
        if line.startswith("@@"):
            break
        if line.startswith("+++ "):
            plus = _side(line[4:], "b/")
        elif line.startswith("--- "):
            minus = _side(line[4:], "a/")
        else:
            for word in ("rename from ", "rename to ", "copy from ", "copy to "):
                if line.startswith(word):
                    value = line[len(word):]
                    value = _unquote(value) if value.startswith('"') else value
                    if value:
                        names.add(value)
                        if word in ("rename to ", "copy to "):
                            renamed = value
    for value in (plus, minus):
        if value:
            names.add(value)
    path = plus or renamed or main or minus
    if path:
        names.add(path)
    return path, names


def _generated(path: str) -> bool:
    name = PurePosixPath(path).name.casefold()
    parts = path.casefold().split("/")
    return (name in LOCK_FILES or name.endswith(GENERATED_SUFFIXES)
            or any(p in ("node_modules", "dist", "vendor") for p in parts[:-1]))


def parse_diff(text: str) -> tuple[Patch, ...]:
    """`gh pr diff`'s text as one Patch per file, each scrubbed and capped.
    A credential-named path (any name the chunk gives) keeps its name and is
    withheld unread; a chunk whose path cannot be read is withheld too."""
    chunks: list[list[str]] = []
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            chunks.append([line])
        elif chunks:
            chunks[-1].append(line)
    out = []
    for lines in chunks:
        path, names = _chunk_paths(lines)
        if path is None:
            out.append(Patch("(a path that could not be read)", "unreadable"))
            continue
        name = shown(scrub(path), 200)
        if any(_credential_path(n) for n in names if n):
            out.append(Patch(name, "withheld"))
            continue
        if any(line.startswith(("Binary files ", "GIT binary patch")) for line in lines[1:]):
            out.append(Patch(name, "binary"))
            continue
        if _generated(path):
            out.append(Patch(name, "lock"))
            continue
        start = next((i for i, line in enumerate(lines) if line.startswith("@@")), None)
        if start is None:
            out.append(Patch(name, "shown", "(no content change)"))
            continue
        body, cut = scrub_head("\n".join(lines[start:]).rstrip("\n"), FILE_PATCH_CAP)
        out.append(Patch(name, "shown", body, cut))
    return tuple(out)


# -- conflicts ---------------------------------------------------------------------------------

def parse_merge_tree(data: bytes) -> tuple[str, tuple[str, ...]]:
    """(the result tree, the conflicting paths) from `merge-tree -z
    --name-only --no-messages`: `<tree>\\0<path>\\0...`. GhFailed otherwise."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise GhFailed("malformed") from None
    parts = text.split("\x00")
    tree = parts[0].strip()
    if not valid_sha(tree):
        raise GhFailed("malformed")
    names = [p for p in parts[1:] if p]
    if len(names) > NAMES_MAX or any(len(n) > 4096 or "\n" in n for n in names):
        raise GhFailed("malformed")
    seen, out = set(), []
    for name in names:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return tree, tuple(out)


def marker_regions(text: str, cap: int = CONFLICTS_CAP) -> str:
    """The conflict-marker regions (`<<<<<<<` to `>>>>>>>`) of a scrubbed blob."""
    out, inside, size = [], False, 0
    for line in text.splitlines():
        if line.startswith("<<<<<<<"):
            inside = True
        if inside:
            if size + len(line) + 1 > cap:
                out.append("…")
                break
            out.append(line)
            size += len(line) + 1
        if line.startswith(">>>>>>>"):
            inside = False
    return "\n".join(out)


# -- the mirror (PM-1.6) ---------------------------------------------------------------------

_mirror_locks: dict[str, threading.Lock] = {}
_mirror_guard = threading.Lock()


def _mirror_lock(key: str) -> threading.Lock:
    with _mirror_guard:
        return _mirror_locks.setdefault(key, threading.Lock())


class MirrorRefused(Exception):
    pass


def mirrors_root() -> Path:
    """`V2_DATA_DIR/pr-mirrors`, 0700, no symlink on the way; never the
    Codex-readable chat-git mirrors' folder."""
    from jarvis import config
    base = Path(config.V2_DATA_DIR).expanduser()
    path = base / "pr-mirrors"
    for folder in (path, *path.parents):
        if folder.is_symlink():
            raise MirrorRefused("symlink")
    try:
        chat = Path(config.chat_git_mirrors_dir()).expanduser().resolve()
    except (OSError, RuntimeError):
        raise MirrorRefused("chat mirrors") from None
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.mkdir(mode=0o700, exist_ok=True)
    path.chmod(0o700)
    real = path.resolve()
    if real == chat or chat in real.parents or real in chat.parents:
        raise MirrorRefused("chat mirrors")
    return path


def mirror_key(repo: str) -> str:
    return hashlib.sha256(repo.lower().encode("utf-8")).hexdigest()[:40]


def empty_home() -> Path:
    home = mirrors_root() / "empty-home"
    if home.is_symlink():
        raise MirrorRefused("symlink")
    home.mkdir(mode=0o700, exist_ok=True)
    home.chmod(0o700)
    return home


def local_env() -> dict[str, str]:
    """For git runs that read the mirror: `chat_git._owner_env()` with its
    HOME and XDG folders replaced by an empty one, and no global, system or
    attribute config of the owner's (no merge driver or filter defined)."""
    from .chat_git import _owner_env
    env = {k: v for k, v in _owner_env().items()
           if k not in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
                        "XDG_CACHE_HOME", "GH_CONFIG_DIR")}
    env.update({"HOME": str(empty_home()), "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_ATTR_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0", "LANG": "C.UTF-8"})
    return env


def local_pins() -> list[str]:
    """Global options for every local git run on the mirror: attributes
    from the empty tree only, no attributes file, no renormalize; then
    gitops' hardening (hooks, fsmonitor, submodules, `ext::`)."""
    from jarvis import gitops
    return [f"--attr-source={EMPTY_TREE}", *gitops._HARDENING,
            "-c", "core.attributesFile=/dev/null", "-c", "merge.renormalize=false"]


def fetch_pins() -> list[str]:
    """The credentialed fetch's pins: gitops' hardening, `_credential_pins`,
    then HTTPS as the only transport."""
    from jarvis import gitops
    from .chat_git import _credential_pins
    return [*gitops._HARDENING, *_credential_pins(), "-c", "protocol.allow=never",
            "-c", "protocol.https.allow=always", "-c", "protocol.file.allow=never"]


# -- reading ---------------------------------------------------------------------------------

Runner = Callable[..., bytes]


class RepoCache:
    """`gh repo view`'s answer per repository, for `REPO_CACHE_S`."""

    def __init__(self):
        self._lock = threading.Lock()
        self._data: dict[str, tuple[float, dict]] = {}

    def get(self, repo: str, at: float) -> dict | None:
        with self._lock:
            hit = self._data.get(repo.lower())
        return hit[1] if hit and at - hit[0] < REPO_CACHE_S else None

    def put(self, repo: str, at: float, value: dict) -> None:
        with self._lock:
            self._data[repo.lower()] = (at, value)


_REPO_CACHE = RepoCache()


class _Pass:
    """One PR's reads under one deadline."""

    def __init__(self, run: Runner, now: Callable[[], float], budget: float, stop):
        self.run, self.now, self.stop = run, now, stop
        self.deadline = now() + budget

    def call(self, argv: list[str], env: dict, cap: int, ok_codes=(0,)) -> bytes:
        left = self.deadline - self.now()
        if left <= 0:
            raise GhFailed("timeout")
        return self.run(argv, env, timeout=min(CALL_TIMEOUT_S, left), cap=cap, stop=self.stop,
                        ok_codes=ok_codes)

    def out_of_time(self) -> bool:
        return self.deadline - self.now() <= 0


def _reason(exc: GhFailed) -> str:
    return exc.code if exc.code in READ_REASONS else "error"


def view_argv(gh: str, repo: str, number: int) -> list[str]:
    return [gh, "pr", "view", str(number), "--repo", repo, "--json", ",".join(VIEW_FIELDS)]


def diff_argv(gh: str, repo: str, number: int) -> list[str]:
    return [gh, "pr", "diff", str(number), "--repo", repo]


def checks_argv(gh: str, repo: str, head: str) -> list[str]:
    return [gh, "api", "--method", "GET", f"repos/{repo}/commits/{head}/check-runs?per_page=100"]


def log_argv(gh: str, repo: str, job: int) -> list[str]:
    return [gh, "run", "view", "--job", str(job), "--repo", repo, "--log-failed"]


def repo_argv(gh: str, repo: str) -> list[str]:
    return [gh, "repo", "view", repo, "--json", ",".join(REPO_FIELDS)]


def init_argv(git: str, mirror: Path) -> list[str]:
    return [git, *local_pins(), "init", "--bare", "--quiet", "--template=", str(mirror)]


def fetch_argv(git: str, mirror: Path, repo: str, base: str, number: int) -> list[str]:
    from jarvis import gitops
    return [git, *fetch_pins(), *gitops._with_submodule_flags([
        "--git-dir", str(mirror), "fetch", "--no-tags", "--no-write-fetch-head", "--quiet",
        f"https://github.com/{repo}.git", f"+refs/heads/{base}:refs/heads/{base}",
        f"+refs/pull/{number}/head:refs/pull/{number}/head"])]


def merge_tree_argv(git: str, mirror: Path, base: str, head: str) -> list[str]:
    return [git, *local_pins(), "--git-dir", str(mirror), "merge-tree", "--write-tree",
            "--name-only", "-z", "--no-messages", base, head]


def blob_argv(git: str, mirror: Path, tree: str, path: str) -> list[str]:
    return [git, *local_pins(), "--git-dir", str(mirror), "cat-file", "blob", f"{tree}:{path}"]


def ensure_mirror(p: _Pass, git: str, repo: str) -> Path:
    """The repository's bare mirror, made on first use (no template), 0700."""
    path = mirrors_root() / f"{mirror_key(repo)}.git"
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise MirrorRefused("not a folder")
    if not (path / "HEAD").is_file():
        p.call(init_argv(git, path), local_env(), GIT_CAP)
    path.chmod(0o700)
    if os.path.lexists(path / "info" / "attributes"):
        raise MirrorRefused("attributes")
    return path


def read_conflicts(p: _Pass, git: str, repo: str, number: int, base: str, base_oid: str,
                   head: str) -> tuple[tuple[str, ...], tuple[Conflict, ...]]:
    """(the conflicting paths, scrubbed; marker regions of up to CONFLICT_FILES)."""
    from .chat_git import _owner_env
    with _mirror_lock(mirror_key(repo)):
        mirror = ensure_mirror(p, git, repo)
        p.call(fetch_argv(git, mirror, repo, base, number), _owner_env(), GIT_CAP)
        tree, names = parse_merge_tree(p.call(merge_tree_argv(git, mirror, base_oid, head),
                                              local_env(), GIT_CAP, ok_codes=(0, 1)))
        regions = []
        for name in names:
            if len(regions) >= CONFLICT_FILES or p.out_of_time():
                break
            label = shown(scrub(name), 200)
            if _credential_path(name):
                regions.append(Conflict(label, "", True))
                continue
            if name.startswith(("./", "../")):
                continue
            try:
                blob = p.call(blob_argv(git, mirror, tree, name), local_env(), BLOB_CAP)
            except GhFailed:
                continue
            text = scrub(blob.decode("utf-8", "replace"))
            regions.append(Conflict(label, marker_regions(text)))
    return tuple(shown(scrub(n), 200) for n in names), tuple(regions)


def _not_read(repo: str, number: int, reason: str) -> PrFacts:
    parts = ("view", "diff", "checks", "logs", "repo", "conflicts")
    return PrFacts(repo, number, read={k: reason for k in parts})


def read_pr(repo: str, number: int, *, run: Runner | None = None, gh: str | None = None,
            git: str | None = None, now: Callable[[], float] = time.monotonic,
            repo_cache: RepoCache | None = None, budget: float = PASS_TIMEOUT_S,
            stop: threading.Event | None = None) -> PrFacts:
    """Everything code reads about `repo`'s PR `number` (PM-1), scrubbed
    (PM-3); each part's outcome in `read`. ValueError for a repo or number
    that is not valid (the caller's bug, nothing is run)."""
    from jarvis import gitops
    from .chat_git import _owner_env
    from .inbox_prs import run_bounded
    if not valid_repo(repo) or not valid_number(number):
        raise ValueError("not a valid OWNER/REPO and pull request number")
    run = run or run_bounded
    gh = gh if gh is not None else gitops.gh_binary()
    cache = repo_cache if repo_cache is not None else _REPO_CACHE
    if not gh:
        return _not_read(repo, number, "missing")
    p = _Pass(run, now, budget, stop)
    env = _owner_env()
    read: dict[str, str] = {}
    try:
        view = parse_view(p.call(view_argv(gh, repo, number), env, VIEW_CAP), repo, number)
    except GhFailed as exc:
        return _not_read(repo, number, _reason(exc))
    read["view"] = "ok"
    read.update(view.pop("read"))
    failing_names = view.pop("failing_names")
    head = view["head"]

    patches: tuple[Patch, ...] = ()
    try:
        patches = parse_diff(p.call(diff_argv(gh, repo, number), env, DIFF_CAP)
                             .decode("utf-8", "replace"))
        read["diff"] = "ok"
    except GhFailed as exc:
        read["diff"] = _reason(exc)

    runs = None
    if head is None:
        read["checks"] = "malformed"
    else:
        try:
            runs = parse_check_runs(p.call(checks_argv(gh, repo, head), env, CHECKS_CAP))
            read["checks"] = "ok"
        except GhFailed as exc:
            read["checks"] = _reason(exc)
    failing = _enrich(failing_names, runs, repo)

    logs = []
    jobs = [c for c in failing if c.job is not None][:LOG_JOBS]
    read["logs"] = "ok" if jobs else "not_needed"
    for check in jobs:
        try:
            data = p.call(log_argv(gh, repo, check.job), env, LOG_CAP)
            logs.append(Log(check.name, _tail_lines(data.decode("utf-8", "replace"), LOG_LINES,
                                                    LOGS_CAP)))
        except GhFailed as exc:
            read["logs"] = _reason(exc)

    settings = cache.get(repo, now())
    if settings is None:
        try:
            item = _json(p.call(repo_argv(gh, repo), env, REPO_CAP))
            if not isinstance(item, dict):
                raise GhFailed("malformed")
            merge_commit, squash = item.get("mergeCommitAllowed"), item.get("squashMergeAllowed")
            permission = item.get("viewerPermission")
            if not isinstance(merge_commit, bool) or not isinstance(squash, bool) \
                    or permission not in _PERMISSIONS:
                raise GhFailed("malformed")
            settings = {"merge_commit": merge_commit, "squash": squash, "permission": permission}
            cache.put(repo, now(), settings)
            read["repo"] = "ok"
        except GhFailed as exc:
            read["repo"] = _reason(exc)
    else:
        read["repo"] = "ok"

    conflicts: tuple[str, ...] = ()
    regions: tuple[Conflict, ...] = ()
    if view["merge_state"] != "DIRTY":
        read["conflicts"] = "not_needed"
    elif view["base"] is None or view["base_oid"] is None or head is None:
        read["conflicts"] = "malformed"
    elif p.out_of_time():
        read["conflicts"] = "out_of_time"
    else:
        try:
            gitbin = git or gitops.git_binary()
            conflicts, regions = read_conflicts(p, gitbin, repo, number, view["base"],
                                                view["base_oid"], head)
            read["conflicts"] = "ok"
        except GhFailed as exc:
            read["conflicts"] = _reason(exc)
        except FileNotFoundError:
            read["conflicts"] = "missing"
        except (MirrorRefused, OSError) as exc:
            LOG.warning("PR facts: the mirror could not be used (%s)", type(exc).__name__)
            read["conflicts"] = "refused"

    return PrFacts(repo=repo, number=number, read=read, patches=patches, failing=failing,
                   logs=tuple(logs), conflicts=conflicts, regions=regions,
                   **(settings or {}), **view)


# -- PM-9: the facts line ---------------------------------------------------------------------

def method(facts: PrFacts) -> str | None:
    """PM-14: "merge" where merge commits are on, else "squash" where squash
    is, else None (also when the settings were not read). Never rebase."""
    if not facts.ok("repo"):
        return None
    if facts.merge_commit:
        return "merge"
    if facts.squash:
        return "squash"
    return None


def _names(items: list[str], limit: int = 4) -> str:
    out = items[:limit]
    if len(items) > limit:
        out.append(f"+{len(items) - limit} more")
    return ", ".join(out)


def facts_line(facts: PrFacts) -> str:
    """PM-9's line, from validated fields only, e.g.
    `O/R #N · feat → main · checks: failing (test, GitGuardian Security Checks
    [secret scanner]) · conflicts: a.py, b.py · 12 files +340 −20 · head
    1a2b3c4 · merge commit`."""
    parts = [f"{facts.repo} #{facts.number}"]
    if not facts.ok("view"):
        parts.append("not read")
        return " · ".join(parts)
    if facts.state != "OPEN":
        parts.append(facts.state.lower())
    if facts.draft:
        parts.append("draft")
    head = facts.branch and (f"fork:{facts.branch}" if facts.fork else facts.branch)
    parts.append(f"{head or 'branch not shown'} → {facts.base or 'base not shown'}")
    if facts.checks is None:
        parts.append("checks: not read")
    elif facts.checks == "failing":
        names = [c.name + (" [secret scanner]" if c.scanner else "") for c in facts.failing]
        parts.append(f"checks: failing ({_names(names)})" if names else "checks: failing")
    elif facts.checks == "pending":
        parts.append(f"checks: pending ({facts.pending})" if facts.pending else "checks: pending")
    else:
        parts.append(f"checks: {facts.checks}")
    if facts.merge_state == "DIRTY":
        if facts.ok("conflicts"):
            parts.append(f"conflicts: {_names(list(facts.conflicts), 5)}" if facts.conflicts
                         else "conflicts: none found")
        else:
            parts.append("conflicts: not read")
    elif facts.merge_state == "BEHIND":
        parts.append(f"behind {facts.base or 'its base'}")
    elif facts.merge_state == "BLOCKED":
        parts.append("blocked by branch rules")
    elif facts.merge_state in ("UNKNOWN", "DRAFT"):
        parts.append("mergeability unknown")
    if facts.review == "CHANGES_REQUESTED":
        parts.append("changes requested")
    elif facts.review == "REVIEW_REQUIRED":
        parts.append("review required")
    if facts.files_total is not None:
        added = "?" if facts.additions is None else str(facts.additions)
        removed = "?" if facts.deletions is None else str(facts.deletions)
        word = "file" if facts.files_total == 1 else "files"
        parts.append(f"{facts.files_total} {word} +{added} −{removed}")
    else:
        parts.append("files not read")
    parts.append(f"head {facts.head[:7]}" if facts.head else "head not shown")
    if facts.fork:
        parts.append("from a fork")
    how = method(facts)
    parts.append({"merge": "merge commit", "squash": "squash merge"}.get(how)
                 or ("no merge method allowed" if facts.ok("repo") else "merge method not read"))
    bundle = model_input(facts)
    if bundle.trimmed:
        parts.append(f"summary read {bundle.files_read} of {bundle.files_total} files")
    return " · ".join(parts)


# -- PM-12: buttons by state ------------------------------------------------------------------

@dataclass(frozen=True)
class Decision:
    actions: tuple[str, ...]
    why: str                    # code's words for the row, "" when nothing to say
    method: str | None = None   # PM-14, for Merge and Merge as is


def _hold_note(hold) -> str:
    note = shown(scrub(hold), 200) if isinstance(hold, str) and hold.strip() else ""
    return f"on hold: {note}" if note else "on hold"


def decide(facts: PrFacts, hold=None, limited: bool = False) -> Decision:
    """PM-12's table, from the facts alone (never the model's text). `hold`
    (Q3): any value but None or False hides every action, and a string is
    shown as its note. `limited`: the phone is not fully checked, so a PR
    that touches safety files (or whose files were not all checked) gets
    nothing (Q1)."""
    how = method(facts)
    if not facts.ok("view"):
        return Decision((), "the pull request could not be read")
    if facts.state != "OPEN":
        return Decision((), f"the pull request is {facts.state.lower()}")
    if facts.draft:
        return Decision((), "the pull request is a draft")
    if facts.head is None:
        return Decision((), "its head commit could not be read")
    if hold is not None and hold is not False:
        return Decision((), _hold_note(hold))

    failing = facts.checks == "failing"
    scanners = [c for c in facts.failing if c.scanner]
    unknown = any(c.scanner is None for c in facts.failing)
    scanner_only = failing and bool(facts.failing) and len(scanners) == len(facts.failing)
    state = facts.merge_state
    actions: list[str] = []
    why = ""
    if facts.checks is None:
        why = "its checks could not be read"
    elif state == "DIRTY":
        actions.append(FIX)
        why = "it has merge conflicts"
    elif failing and scanner_only:
        if state == "UNSTABLE":
            actions.append(MERGE_AS_IS)
            why = "only a secret scanner fails; an agent never silences one"
        elif state == "BLOCKED":
            why = ("a required secret scanner fails: only an admin override could merge it, "
                   "and Jarvis never uses one")
        else:
            why = "only a secret scanner fails; an agent never silences one"
    elif failing:
        if unknown:
            why = "its failing checks could not be read in full"
        else:
            actions.append(FIX)
            why = "checks fail"
        if state == "UNSTABLE":
            actions.append(MERGE_AS_IS)
    elif facts.checks == "pending":
        why = "waits on its checks"
    elif state == "BEHIND":
        actions.append(UPDATE)
        why = f"behind {facts.base or 'its base'}; a fresh Merge is needed after the update"
    elif state in ("CLEAN", "HAS_HOOKS"):
        actions.append(MERGE)
    elif state == "UNSTABLE":
        actions.append(MERGE_AS_IS)
        why = "GitHub reports a check that is not passing"
    elif state == "BLOCKED":
        if facts.review == "CHANGES_REQUESTED":
            why = "GitHub requires the requested changes to be resolved"
        elif facts.review == "REVIEW_REQUIRED":
            why = "GitHub requires an approving review"
        else:
            why = "GitHub's branch rules hold it"
    else:
        why = "GitHub has not worked out whether it can merge"

    notes = []
    if facts.fork and (FIX in actions or UPDATE in actions):
        actions = [a for a in actions if a not in (FIX, UPDATE)]
        notes.append("from a fork: no Fix or Update")
    if actions and not facts.ok("repo"):
        actions = []
        notes.append("the repository's settings could not be read")
    elif actions and facts.permission not in _CAN_WRITE:
        actions = []
        notes.append("the owner's login cannot write to this repository")
    if how is None and any(a in (MERGE, MERGE_AS_IS) for a in actions):
        actions = [a for a in actions if a not in (MERGE, MERGE_AS_IS)]
        notes.append("no merge method is allowed")
    if limited and actions and flagged(facts):
        actions = []
        notes.append("touches safety files: answer it at the desk or on a fully checked phone")
    why = "; ".join(x for x in [why, *notes] if x)
    uses = how if any(a in (MERGE, MERGE_AS_IS) for a in actions) else None
    return Decision(tuple(actions), why, uses)


def actions(facts: PrFacts, hold=None, limited: bool = False) -> tuple[str, ...]:
    return decide(facts, hold, limited).actions


# -- the model's input ----------------------------------------------------------------------

@dataclass(frozen=True)
class Bundle:
    text: str                   # at most MODEL_CAP; PM-C puts it in one fence
    files_read: int             # files whose patch is in it whole
    files_total: int
    trimmed: bool


def model_input(facts: PrFacts) -> Bundle:
    """The capped, scrubbed text the summary model reads (PM-2), a pure
    function of the facts: title, body, failing checks with their output
    and logs, conflict regions, then the diff with what is left. Lock and
    generated files, binaries and withheld patches are named only."""
    out: list[str] = []
    trimmed = False
    out.append(f"Title: {facts.title[:TITLE_CAP]}")
    if facts.body:
        out.append("Description:\n" + facts.body[:BODY_CAP])
        trimmed |= facts.body_cut
    if facts.failing:
        room, lines = LOGS_CAP, ["Failing checks:"]
        for check in facts.failing:
            label = check.name + (" [secret scanner]" if check.scanner else "")
            text = "\n".join(x for x in (f"- {label}", check.title, check.summary) if x)
            if len(text) > room:
                text, trimmed = text[:max(0, room)], True
            room -= len(text) + 1
            lines.append(text)
        for log in facts.logs:
            text = f"Log of {log.name} (last lines):\n{log.text}"
            if len(text) > room:
                text = text[len(text) - max(0, room):] if room > 0 else ""
                trimmed = True
            room -= len(text) + 1
            if text:
                lines.append(text)
        out.append("\n".join(lines))
    if facts.regions:
        room, lines = CONFLICTS_CAP, ["Conflicts:"]
        for conflict in facts.regions:
            body = "(credential file: withheld)" if conflict.withheld else conflict.regions
            text = f"--- {conflict.path}\n{body}"
            if len(text) > room:
                text, trimmed = text[:max(0, room)], True
            room -= len(text) + 1
            if text:
                lines.append(text)
        out.append("\n".join(lines))
    head = "\n\n".join(out)
    room = MODEL_CAP - len(head) - len("\n\nChanges:\n")
    stats = {f.path: f for f in facts.files}
    lines, read = [], 0
    patches = list(facts.patches)
    named = {p.path for p in patches}
    for patch in patches:
        info = stats.get(patch.path)
        counts = f" (+{info.additions} −{info.deletions})" if info and info.additions is not None \
            and info.deletions is not None else ""
        note = {"lock": "lock or generated file, not shown", "binary": "binary, not shown",
                "withheld": "credential file, patch withheld",
                "unreadable": "path could not be read, patch withheld"}.get(patch.kind)
        text = f"--- {patch.path}{counts}" + (f" ({note})" if note else f"\n{patch.text}")
        if room <= 0:
            trimmed = True
            break
        if len(text) > room:
            lines.append(text[:room])
            trimmed, room = True, 0
            continue
        lines.append(text)
        room -= len(text) + 1
        if patch.kind == "shown" and not patch.cut:
            read += 1
        else:
            trimmed = True
    others = [f.path for f in facts.files if f.path not in named]
    if others:
        trimmed = True
        listed = ("Other changed files: " + ", ".join(others))[:max(0, room)]
        if listed:
            lines.append(listed)
    total = facts.files_total if facts.files_total is not None else max(len(patches), len(facts.files))
    if read < total:
        trimmed = True
    text = head + ("\n\nChanges:\n" + "\n".join(lines) if lines else "")
    return Bundle(text[:MODEL_CAP], read, total, trimmed)


# -- PM-2: one PR at a time ------------------------------------------------------------------

class FactsWorker:
    """One worker thread: reads one PR at a time, at most `max_waiting`
    waiting (a PR already waiting is not queued twice), and hands each
    PrFacts to `on_facts`. Never started by anything in this package; a
    later work package attaches it."""

    def __init__(self, on_facts: Callable[[PrFacts], None], *,
                 read: Callable[..., PrFacts] = read_pr, max_waiting: int = MAX_WAITING):
        self.on_facts, self.read, self.max_waiting = on_facts, read, max_waiting
        self._lock = threading.Lock()
        self._queue: deque[tuple[str, int]] = deque()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def submit(self, repo: str, number: int) -> bool:
        """Queue a read; False when invalid, already waiting or full. Never blocks on a read."""
        if not valid_repo(repo) or not valid_number(number):
            return False
        key = (repo, number)
        with self._lock:
            if key in self._queue:
                return True
            if len(self._queue) >= self.max_waiting:
                return False
            self._queue.append(key)
        self._wake.set()
        return True

    def waiting(self) -> int:
        with self._lock:
            return len(self._queue)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="jarvis-pr-facts", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                key = self._queue.popleft() if self._queue else None
                if key is None:
                    self._wake.clear()
            if key is None:
                self._wake.wait(1.0)
                continue
            try:
                facts = self.read(*key, stop=self._stop)
            except Exception as exc:  # noqa: BLE001 — never kills the worker; nothing read is logged
                LOG.warning("PR facts: a read broke (%s)", type(exc).__name__)
                continue
            if self._stop.is_set():
                return
            try:
                self.on_facts(facts)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("PR facts: the consumer broke (%s)", type(exc).__name__)


__all__ = ["Bundle", "Check", "Conflict", "Decision", "FactsWorker", "FileChange", "Log", "Patch",
           "PrFacts", "RepoCache", "SECRET_SCANNERS", "actions", "decide", "facts_line", "flagged",
           "flags_line", "method", "model_input", "parse_diff", "path_flags", "read_pr", "scrub"]
