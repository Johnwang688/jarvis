"""Owner live checks: the store, the deployed test and the runbook import
(owner inbox design `docs/plans/2026-10-24-owner-inbox-design.md`, IN-5,
IN-6, §3.2, §4, §5 "Check", §6; inbox package B2).

A check is a list of steps, each a `do` and an `expect`, that only the owner
can run: an acceptance runbook for something merged. An agent posts one
with `owner_post(kind="check", …)` (`decisions.OwnerPosts`, the same binding,
caps, scrub and provenance as a decision), or the owner imports one from a
runbook under a project's `docs/plans/` (§6). **A step's text is text only**:
nothing in a check runs anything, and the HUD's "copy" puts a step's words
on the clipboard and nothing more.

**Results are the owner's alone (IN-6).** `CheckStore.mark` records a
verdict (`pass`, `fail` or `skip`) and a note for one step. Its one caller is
the inbox's owner-only route `POST /inbox/checks/<id>/steps/<n>`
(`inbox.mark_check_step`): no tool, MCP entry, CLI path or API-listener route
reaches it (`tests/v2/archive_check.py`, `tests/v2/inbox_checks_check.py`).
A step marked again is replaced (the owner's own correction); nothing ever
clears a result. When every step has a verdict the check is done, and the
row moves to the "For your information" band for `DONE_WINDOW`, where the
owner can export the receipt as Markdown.

**Deployed gating (IN-6).** A check that names pull requests can be marked
only when every one of them is in the build that runs (`Deployment`). The
test is local git only, never the network: a PR's merge commit is the merge
commit whose message starts "Merge pull request #<n> from " (GitHub's
merge-commit message), found in the checkout's HEAD or its remote branches.
It is deployed when that commit is an ancestor of the commit the daemon
started from (captured once at start, `Deployment.attach_git`) and, if the
PR changed anything under `hud/`, of the commit the installed HUD was built
from (`hud_install`'s manifest `git_head`). Anything git cannot say (a
squash merge, a PR not fetched, the dev HUD, an unattached daemon in a test)
is "not confirmed deployed", and the step is not marked: it fails closed.

**The files.** `checks/<id>.json` in the data root's `inbox` folder, with
the decisions' safe I/O (`decisions.DecisionStore`: dir fds, O_NOFOLLOW,
O_NONBLOCK reads, an O_EXCL temp file fsynced then replaced, 0700 folders
and 0600 files). The tree is protected state (`files._inbox_tree`, and the
`checks` folder is named in `_protected_state_paths`).

SELF_PROTECTED (`jarvis/tools/files.py`): it decides what an agent may put in
front of the owner as a check and that only the owner records its results.
"""
from __future__ import annotations

from datetime import timedelta
import errno
import logging
import os
from pathlib import Path
import re
import secrets as pysecrets
import stat
import threading
import time
import unicodedata
from typing import Any

from .decisions import (CapReached, DecisionStore, _ident, _int, _parse_time,
                        display_text)

LOG = logging.getLogger(__name__)

# -- the bounds; pinned by tests/v2/inbox_checks_check.py ---------------------------------
OPEN_PER_THREAD = 5          # open (not done) checks one chat may have waiting
PER_THREAD_DAY = 10          # posted by one chat in any rolling 24 hours
PER_DAY = 20                 # posted by agents in all in any rolling 24 hours
DAY = timedelta(hours=24)
TITLE_MAX = 200
STEPS_MIN, STEPS_MAX = 1, 30
DO_MAX = 1000
EXPECT_MAX = 1000
PRS_MAX = 10
PR_NUMBER_MAX = 10_000_000
NOTE_MAX = 1000              # the owner's note on one step
KEEP = 500                   # files kept; the oldest done go first
DONE_WINDOW = timedelta(days=7)   # how long a done check stays as a row
RUNBOOK_MAX = 512 * 1024     # a runbook larger than this is not imported
PATH_MAX = 1024
_FILE_MAX = 256 * 1024       # a check file larger than this is not one of ours

VERDICTS = ("pass", "fail", "skip")
_FIELDS = frozenset({"id", "kind", "thread_id", "task_id", "project_id", "created_by", "title", "prs",
                     "steps", "results", "deployed_sha", "created_at", "imported_from"})
ID_RE = re.compile(r"^c[0-9a-f]{12}$")
_NAME_RE = re.compile(r"^(c[0-9a-f]{12})\.json$")
_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")


def _str_or_none(value, limit) -> bool:
    return value is None or (isinstance(value, str) and len(value) <= limit)


def valid(record: Any, name: str | None = None) -> dict | None:
    """`record` if it has exactly the shape `CheckStore.create` writes (§3.2
    plus `kind`, `thread_id`, `task_id`, `project_id` and `created_at`), else
    None: anything else on disk is never a row, never counted, never marked."""
    if not isinstance(record, dict) or set(record) != _FIELDS:
        return None
    rid = record["id"]
    if not isinstance(rid, str) or not ID_RE.match(rid) or (name is not None and name != f"{rid}.json"):
        return None
    if record["kind"] != "check":
        return None
    title, prs, steps, results = record["title"], record["prs"], record["steps"], record["results"]
    if not isinstance(title, str) or not title or not isinstance(record["created_by"], str):
        return None
    if (not isinstance(prs, list) or len(prs) > PRS_MAX
            or not all(_int(n) and 0 < n < PR_NUMBER_MAX for n in prs) or len(set(prs)) != len(prs)):
        return None
    if not isinstance(steps, list) or not STEPS_MIN <= len(steps) <= STEPS_MAX:
        return None
    for step in steps:
        if (not isinstance(step, dict) or set(step) != {"do", "expect"} or not isinstance(step["do"], str)
                or not step["do"] or not isinstance(step["expect"], str)):
            return None
    if not isinstance(results, list) or len(results) > len(steps):
        return None
    seen = set()
    for result in results:
        if not isinstance(result, dict) or set(result) != {"step", "verdict", "note", "at"}:
            return None
        if not (_int(result["step"]) and 0 <= result["step"] < len(steps)) or result["step"] in seen:
            return None
        seen.add(result["step"])
        if result["verdict"] not in VERDICTS or not isinstance(result["note"], str):
            return None
        if _parse_time(result["at"]) is None:
            return None
    thread_id = record["thread_id"]
    if thread_id is not None and _ident(thread_id) is None:
        return None
    for key in ("task_id", "project_id"):
        if record[key] is not None and _ident(record[key]) is None:
            return None
    sha = record["deployed_sha"]
    if sha is not None and not (isinstance(sha, str) and _SHA_RE.match(sha)):
        return None
    if _parse_time(record["created_at"]) is None:
        return None
    if not _str_or_none(record["imported_from"], PATH_MAX):
        return None
    # An agent's check has its asking chat; an imported one has none.
    if (thread_id is None) == (record["imported_from"] is None):
        return None
    return record


def is_done(record: dict) -> bool:
    """Every step has a verdict."""
    return len({r["step"] for r in record["results"]}) == len(record["steps"])


def done_at(record: dict):
    """When the last verdict that completed it was recorded, or None."""
    if not is_done(record):
        return None
    times = [_parse_time(r["at"]) for r in record["results"]]
    return max(t for t in times if t is not None)


class CheckStore(DecisionStore):
    """`checks/` in `<root>`'s `inbox` folder: the decisions' safe I/O
    (`DecisionStore._open`, `_read`, `_write`, `_all`), its own records."""

    _SUB = "checks"
    _NAMES = _NAME_RE
    _ID = ID_RE
    _valid = staticmethod(valid)
    _MAX = _FILE_MAX

    @staticmethod
    def _order(record):
        return (_parse_time(record["created_at"]), record["id"])

    def __init__(self, root, *, clock=None):
        super().__init__(root, clock=clock)

    # -- writes ------------------------------------------------------------------------

    def create(self, *, thread_id: str | None, project_id: str | None, task_id: str | None,
               created_by: str, title: str, prs: list[int], steps: list[dict],
               imported_from: str | None = None) -> dict:
        """Write one new check, or raise `CapReached` (nothing written). The
        caller has already validated, scrubbed and cleaned every field. The
        per-chat and daily caps count agents' checks (a `thread_id`); an
        owner's import counts only towards `KEEP`."""
        with self._lock:
            fd = self._open(create=True)
            try:
                existing = self._all(fd)
                now = self.clock()
                if thread_id is not None:
                    open_here = sum(1 for r in existing if r["thread_id"] == thread_id and not is_done(r))
                    if open_here >= OPEN_PER_THREAD:
                        raise CapReached(
                            f"Error: this chat already has {OPEN_PER_THREAD} checks waiting on the owner, "
                            "the most one chat may; wait for the owner to run one before posting another.")
                    since = now - DAY
                    posted = [r for r in existing if r["thread_id"] is not None
                              and (_parse_time(r["created_at"]) or now) > since]
                    if sum(1 for r in posted if r["thread_id"] == thread_id) >= PER_THREAD_DAY:
                        raise CapReached(
                            f"Error: this chat has posted {PER_THREAD_DAY} checks in the last 24 hours, "
                            "the most one chat may; try again later.")
                    if len(posted) >= PER_DAY:
                        raise CapReached(
                            f"Error: {PER_DAY} checks have been posted to the owner in the last 24 hours, "
                            "the most Jarvis allows; try again later.")
                files = sum(1 for name in os.listdir(fd) if _NAME_RE.match(name))
                if files >= KEEP:
                    finished = sorted((r for r in existing if is_done(r)),
                                      key=lambda r: (done_at(r), r["id"]))
                    for old in finished[:files - KEEP + 1]:
                        try:
                            os.unlink(f"{old['id']}.json", dir_fd=fd)
                        except FileNotFoundError:
                            pass
                    files = sum(1 for name in os.listdir(fd) if _NAME_RE.match(name))
                    if files >= KEEP:
                        raise CapReached(
                            f"Error: {KEEP} checks wait on the owner, the most Jarvis keeps; nothing more "
                            "is posted until some are done.")
                taken = {r["id"] for r in existing}
                rid = "c" + pysecrets.token_hex(6)
                while rid in taken:
                    rid = "c" + pysecrets.token_hex(6)
                record = {"id": rid, "kind": "check", "thread_id": thread_id, "task_id": task_id,
                          "project_id": project_id, "created_by": created_by, "title": title,
                          "prs": list(prs), "steps": [{"do": s["do"], "expect": s["expect"]} for s in steps],
                          "results": [], "deployed_sha": None,
                          "created_at": now.isoformat(timespec="seconds"), "imported_from": imported_from}
                if valid(record) is None:
                    raise ValueError("a check that would not read back was refused")
                self._write(fd, record)
                return record
            finally:
                os.close(fd)

    def mark(self, check_id: str, step: int, verdict: str, note: str, *, sha: str | None) -> dict:
        """Record the owner's verdict on one step (the inbox's owner-only route
        only). A step marked again is replaced. LookupError for an unknown
        check, IndexError for a step it does not have, ValueError for a
        verdict that is not one. `sha` is the build the deployed test
        confirmed (None when the check names no PR and the build is unknown)."""
        if verdict not in VERDICTS:
            raise ValueError("verdict must be pass, fail or skip")
        if not isinstance(note, str) or len(note) > NOTE_MAX:
            raise ValueError("the note is not text within its cap")
        if sha is not None and not _SHA_RE.match(sha):
            sha = None
        with self._lock:
            fd = self._open(create=False)
            if fd is None:
                raise LookupError("no such check")
            try:
                record = self._read(fd, f"{check_id}.json") if ID_RE.match(check_id) else None
                if record is None:
                    raise LookupError("no such check")
                if not (_int(step) and 0 <= step < len(record["steps"])):
                    raise IndexError("no such step")
                record = dict(record)
                results = [r for r in record["results"] if r["step"] != step]
                results.append({"step": step, "verdict": verdict, "note": note,
                                "at": self.clock().isoformat(timespec="seconds")})
                record["results"] = sorted(results, key=lambda r: r["step"])
                record["deployed_sha"] = sha
                if valid(record) is None:
                    raise ValueError("a result that would not read back was refused")
                self._write(fd, record)
                return record
            finally:
                os.close(fd)


# -- the post's fields (owner_post, kind "check") ------------------------------------------

def refuse_fields(title, steps, prs) -> str | None:
    """Why these arguments are refused, or None. Fails closed; never cuts."""
    if not isinstance(title, str) or not title.strip():
        return "Error: a check needs a title: a nonempty string naming what it checks."
    if len(title) > TITLE_MAX:
        return (f"Error: the title is {len(title):,} characters and the most is {TITLE_MAX}; "
                "shorten it (nothing is cut).")
    if not isinstance(steps, list) or not STEPS_MIN <= len(steps) <= STEPS_MAX:
        count = len(steps) if isinstance(steps, list) else "no"
        return f"Error: give between {STEPS_MIN} and {STEPS_MAX} steps (got {count})."
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or set(step) != {"do", "expect"}:
            return f'Error: step {index} must be an object with exactly "do" and "expect".'
        do, expect = step["do"], step["expect"]
        if not isinstance(do, str) or not do.strip():
            return f'Error: step {index} has no "do": say what the owner does.'
        if not isinstance(expect, str) or not expect.strip():
            return f'Error: step {index} has no "expect": say what the owner should see.'
        if len(do) > DO_MAX:
            return (f"Error: step {index}'s do is {len(do):,} characters and the most is {DO_MAX}; "
                    "shorten it (nothing is cut).")
        if len(expect) > EXPECT_MAX:
            return (f"Error: step {index}'s expect is {len(expect):,} characters and the most is "
                    f"{EXPECT_MAX}; shorten it (nothing is cut).")
    if prs is None:
        return None
    if not isinstance(prs, list) or len(prs) > PRS_MAX:
        return f"Error: prs must be a list of at most {PRS_MAX} pull request numbers."
    for n in prs:
        if not _int(n) or not 0 < n < PR_NUMBER_MAX:
            return "Error: each pr must be a pull request's number, such as 301."
    if len(set(prs)) != len(prs):
        return "Error: a pull request is named twice in prs."
    return None


# -- deployed gating (IN-6) ----------------------------------------------------------------

DEPLOYED, NOT_DEPLOYED, UNCONFIRMED, NO_PRS = "deployed", "not deployed", "not confirmed deployed", "no prs"
_CACHE_S = 60.0
_CACHE_MAX = 256


class Deployment:
    """Whether a check's pull requests are in the build that runs. Local git
    only (never `gh`, never the network), and only once attached
    (`attach_git`, from the daemon's `main()`): an embedded or test daemon is
    never attached, so every check that names a PR is "not confirmed
    deployed" there, which refuses its marks (fail closed). Tests hand in
    their own `git` and heads."""

    def __init__(self, *, git=None, code_head=None, hud_head=None, clock=time.monotonic):
        self._git = git                 # (args) -> (code, stdout) | None
        self.code_head = code_head      # the commit the daemon started from
        self._hud_head = hud_head       # () -> the installed HUD's git_head | None
        self.clock = clock
        self._lock = threading.Lock()
        self._cache: dict[tuple, tuple[float, dict]] = {}

    def attach_git(self) -> None:
        """The real git and heads: the checkout's HEAD now (the code this
        daemon runs, read at start) and the installed HUD's manifest."""
        from . import hud_install

        def hud_head():
            from .hud_api import hud_served
            served = hud_served()
            return served.get("git_head") if served.get("state") in ("installed", "fallback") else None
        self._git = hud_install._git
        self.code_head = hud_install.git_head()
        self._hud_head = hud_head

    def _run(self, *args) -> tuple[int, str] | None:
        if self._git is None:
            return None
        try:
            return self._git(*args)
        except Exception as exc:  # noqa: BLE001 — git that cannot say is "not confirmed"
            LOG.info("git could not say whether a PR is deployed (%s)", type(exc).__name__)
            return None

    def merge_commit(self, number: int) -> str | None:
        """The merge commit of PR `number` in this checkout, or None."""
        out = self._run("log", "--merges", "-1", "--format=%H", "--fixed-strings",
                        f"--grep=Merge pull request #{number} from ", "HEAD", "--remotes")
        if out is None or out[0] != 0:
            return None
        sha = out[1].strip().split("\n")[0].strip()
        return sha if _SHA_RE.match(sha) else None

    def _ancestor(self, commit: str, head: str | None) -> bool | None:
        if not head or not _SHA_RE.match(head):
            return None
        out = self._run("merge-base", "--is-ancestor", commit, head)
        if out is None or out[0] not in (0, 1):
            return None
        return out[0] == 0

    def _touches_hud(self, commit: str) -> bool | None:
        out = self._run("diff", "--quiet", "--no-ext-diff", "--no-textconv", f"{commit}^1", commit,
                        "--", "hud")
        if out is None or out[0] not in (0, 1):
            return None
        return out[0] == 1

    def status(self, prs) -> dict:
        """`{state, merged, sha, text}`: `state` one of DEPLOYED,
        NOT_DEPLOYED, UNCONFIRMED and NO_PRS (nothing to gate); `merged`
        whether every PR has a merge commit here (the rank-7 order); `sha`
        the daemon's start commit when the state allows a mark; `text` one
        line built by code, naming PR numbers only. Never raises."""
        prs = tuple(n for n in (prs or ()) if _int(n))
        if not prs:
            return {"state": NO_PRS, "merged": False, "sha": self.code_head if self._ok(self.code_head) else None,
                    "text": ""}
        now = self.clock()
        with self._lock:
            hit = self._cache.get(prs)
            if hit is not None and now - hit[0] < _CACHE_S:
                return dict(hit[1])
        try:
            out = self._status(prs)
        except Exception as exc:  # noqa: BLE001
            LOG.info("The deployed test failed (%s)", type(exc).__name__)
            out = {"state": UNCONFIRMED, "merged": False, "sha": None,
                   "text": "Not confirmed deployed: Jarvis could not tell which build runs."}
        with self._lock:
            if len(self._cache) >= _CACHE_MAX:
                self._cache.clear()
            self._cache[prs] = (now, out)
        return dict(out)

    @staticmethod
    def _ok(sha) -> bool:
        return isinstance(sha, str) and bool(_SHA_RE.match(sha))

    def _status(self, prs) -> dict:
        names = ", ".join(f"#{n}" for n in prs)
        if self._git is None or not self._ok(self.code_head):
            return {"state": UNCONFIRMED, "merged": False, "sha": None,
                    "text": f"Not confirmed deployed: Jarvis cannot read which build runs, so {names} "
                            "cannot be checked yet."}
        merges = {n: self.merge_commit(n) for n in prs}
        unmerged = [n for n, sha in merges.items() if sha is None]
        merged = not unmerged
        if unmerged:
            return {"state": UNCONFIRMED, "merged": False, "sha": None,
                    "text": "Not confirmed deployed: no merge commit for "
                            + ", ".join(f"#{n}" for n in unmerged) + " in this checkout."}
        hud_head = None
        hud_read = False
        missing, unknown = [], []
        for n, sha in merges.items():
            in_code = self._ancestor(sha, self.code_head)
            if in_code is None:
                unknown.append(n)
                continue
            if not in_code:
                missing.append(n)
                continue
            touches = self._touches_hud(sha)
            if touches is None:
                unknown.append(n)
                continue
            if touches:
                if not hud_read:
                    hud_read = True
                    try:
                        hud_head = self._hud_head() if self._hud_head is not None else None
                    except Exception:  # noqa: BLE001
                        hud_head = None
                in_hud = self._ancestor(sha, hud_head)
                if in_hud is None:
                    unknown.append(n)
                elif not in_hud:
                    missing.append(n)
        if missing:
            return {"state": NOT_DEPLOYED, "merged": merged, "sha": None,
                    "text": "Not deployed yet: " + ", ".join(f"#{n}" for n in missing)
                            + " is merged but not in the running daemon or installed HUD."}
        if unknown:
            return {"state": UNCONFIRMED, "merged": merged, "sha": None,
                    "text": "Not confirmed deployed: Jarvis cannot tell whether "
                            + ", ".join(f"#{n}" for n in unknown) + " is in the running build."}
        return {"state": DEPLOYED, "merged": True, "sha": self.code_head,
                "text": f"Deployed: {names} " + ("is" if len(prs) == 1 else "are") + " in the running build."}

    @staticmethod
    def markable(status: dict) -> bool:
        return status.get("state") in (DEPLOYED, NO_PRS)


# -- the runbook import (§6) ----------------------------------------------------------------

class ImportRefused(ValueError):
    """A path or file the import refuses; the message is the sentence to show."""


_FILLERS = frozenset("ᅟᅠㅤﾠ")


def runbook_path(raw, projects) -> tuple[Path, Any]:
    """The runbook `raw` names, as (path, project), or `ImportRefused`.

    `raw` must be an absolute path, in NFC, with no control, format or filler
    character, no `.` or `..` part, naming a `.md` file directly inside
    `<root>/docs/plans/` of one of `projects` (non-archived). Judged lexically
    and again on the filesystem: none of `docs`, `plans` or the file may be a
    symlink, the file's realpath must be exactly the lexical path under the
    project root's realpath, and it must not be a credential file
    (`secrets.is_credential`)."""
    from jarvis import untrusted
    from jarvis.tools import secrets
    if not isinstance(raw, str) or not raw:
        raise ImportRefused("path must be a nonempty string")
    if len(raw) > PATH_MAX:
        raise ImportRefused(f"path is longer than {PATH_MAX} characters")
    if any(untrusted.unseen(ch) for ch in raw) or any(ch in _FILLERS for ch in raw):
        raise ImportRefused("path contains a control or invisible character")
    if any(ch.isspace() and ch != " " for ch in raw):
        raise ImportRefused("path contains whitespace other than a plain space")
    if unicodedata.normalize("NFC", raw) != raw:
        raise ImportRefused("path is not in normalised (NFC) form")
    if not raw.startswith("/"):
        raise ImportRefused("path must be absolute")
    parts = [p for p in raw.split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise ImportRefused("path may not contain `.` or `..`")
    path = Path("/" + "/".join(parts))
    if path.suffix.lower() != ".md" or path.name.startswith("."):
        raise ImportRefused("only a Markdown runbook (`.md`) can be imported")
    owner = None
    for project in projects:
        root_text = getattr(project, "root", None)
        if getattr(project, "archived", None) or not isinstance(root_text, str) or not root_text.startswith("/"):
            continue
        root = Path(os.path.normpath(root_text))
        if path.parent == root / "docs" / "plans":
            owner = project
            break
    if owner is None:
        raise ImportRefused("only a runbook directly inside a project's docs/plans/ folder can be imported")
    root = Path(os.path.normpath(owner.root))
    for hop in (root / "docs", root / "docs" / "plans", path):
        try:
            info = os.lstat(hop)
        except FileNotFoundError:
            raise ImportRefused("no such runbook") from None
        if stat.S_ISLNK(info.st_mode):
            raise ImportRefused("a symlink is not followed: docs/, plans/ and the runbook must be real")
    real_root = Path(os.path.realpath(root))
    if Path(os.path.realpath(path)) != real_root / "docs" / "plans" / path.name:
        raise ImportRefused("the runbook resolves outside its project's docs/plans/ folder")
    if secrets.is_credential(path):
        raise ImportRefused("a credential file is never read")
    return path, owner


def read_runbook(path: Path) -> bytes:
    """The runbook's bytes: opened O_NOFOLLOW | O_NONBLOCK, a regular file of
    at most `RUNBOOK_MAX` bytes, or `ImportRefused`."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise ImportRefused("no such runbook") from None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise ImportRefused("a symlink is not followed") from None
        raise ImportRefused("the runbook could not be opened") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ImportRefused("the runbook is not a regular file")
        if info.st_size > RUNBOOK_MAX:
            raise ImportRefused(f"the runbook is larger than {RUNBOOK_MAX // 1024} KiB")
        chunks, left = [], RUNBOOK_MAX + 1
        while left > 0 and (chunk := os.read(fd, min(1 << 16, left))):
            chunks.append(chunk)
            left -= len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) > RUNBOOK_MAX:
        raise ImportRefused(f"the runbook is larger than {RUNBOOK_MAX // 1024} KiB")
    return data


_HEADING = re.compile(r"^##\s+(?!#)(.*\S)\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_NUMBERED = re.compile(r"^(\s*)(\d{1,3})[.)]\s+(.*)$")
_PART = re.compile(r"^\s*\*\*(Part\s+[A-Za-z0-9]+)\b[^*]*\*\*\s*$")
_EXPECT = re.compile(r"\*{0,2}\s*Expect:\s*\*{0,2}\s*", re.IGNORECASE)
_PR_REF = re.compile(r"(?<![\w/])#(\d{1,7})\b")
_NEW_BLOCK = re.compile(r"^([-*+]\s|>|#|\|)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def _plain(text: str) -> str:
    """Markdown links become their text; the rest stays as written."""
    return _LINK.sub(r"\1", text)


EXPECT_ONLY = "Check that this holds."    # a step that is only an "Expect:"


def _join(lines: list[str]) -> str:
    """A step's lines as text: wrapped lines joined with a space, a bullet
    on a line of its own, a blank line a line break."""
    out = ""
    for line in lines:
        text = line.strip()
        if not text:
            out = out.rstrip(" ") + "\n" if out and not out.endswith("\n") else out
        elif re.match(r"[-*+]\s", text):
            out = (out.rstrip(" ") + "\n" if out and not out.endswith("\n") else out) + text
        else:
            out = out + (" " if out and not out.endswith("\n") else "") + text
    return out.strip()


def parse_runbook(text: str) -> list[dict]:
    """One draft per `##` heading: `{title, prs, steps: [{do, expect}]}`,
    the raw text only (the caller scrubs and judges it). A step is a
    numbered line (`1.` or `1)`) with the lines that continue it; its
    `expect` is everything after its first "Expect:" (bold or not), and its
    `do` what comes before. A `**Part X…**` line names the steps after it.
    Fenced code is never read as a heading or a step. No model is involved."""
    drafts: list[dict] = []
    current: dict | None = None
    step: list[str] | None = None
    part = ""
    fenced = False

    def close_step():
        nonlocal step
        if current is not None and step is not None:
            current["raw_steps"].append((part, step))
        step = None

    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if _FENCE.match(line):
            fenced = not fenced
            if step is not None:
                step.append(line)
            continue
        heading = None if fenced else _HEADING.match(line)
        if heading:
            close_step()
            part = ""
            current = {"title": heading.group(1), "raw_steps": []}
            drafts.append(current)
            continue
        if current is None:
            continue
        numbered = None if fenced else _NUMBERED.match(line)
        part_line = None if fenced else _PART.match(line)
        if part_line:
            close_step()
            part = part_line.group(1)
            continue
        if numbered:
            close_step()
            step = [numbered.group(3)]
            continue
        if step is not None:
            if not line.strip():
                step.append("")
            elif not line[:1].isspace() and _NEW_BLOCK.match(line):
                close_step()        # a bullet or a quote at the margin starts something else
            elif line[:1].isspace() or step[-1] != "":
                step.append(line.strip())
            else:
                close_step()
    close_step()
    out = []
    for draft in drafts:
        steps = []
        for part_name, lines in draft["raw_steps"]:
            body = _join(lines)
            pieces = [p.strip().rstrip("-").strip() for p in _EXPECT.split(_plain(body))]
            do = pieces[0] or EXPECT_ONLY
            # A step that expects several things in turn keeps them in order.
            expect = " Then: ".join(p for p in pieces[1:] if p)
            if part_name:
                do = f"({part_name}) {do}"
            steps.append({"do": do, "expect": expect})
        title = _plain(draft["title"]).strip()
        prs = []
        for match in _PR_REF.finditer(draft["title"]):
            n = int(match.group(1))
            if 0 < n < PR_NUMBER_MAX and n not in prs:
                prs.append(n)
        out.append({"title": title, "prs": prs[:PRS_MAX], "steps": steps})
    return out


def judge_draft(raw: dict) -> tuple[dict, str | None]:
    """One parsed draft as the owner is shown it, and why it cannot be saved
    (None when it can). Every field is scrubbed, cleaned and scrubbed again
    (`decisions._clean_field`), as an agent's check is; nothing is cut: a
    draft over a cap says so and cannot be saved. An imported step may have
    no "Expect:" (`expect` is then empty)."""
    from . import chat_reads
    from .approvals import clean_line
    from .decisions import _clean_field
    withheld = False

    def clean(text: str, lines: bool) -> str:
        nonlocal withheld
        out = _clean_field(text, lines)
        if out == chat_reads.WITHHELD:
            withheld = True
            return ""
        return display_text(out, lines=lines)
    title = clean_line(clean(" ".join(str(raw.get("title") or "").split()), False), TITLE_MAX + 40)
    steps = [{"do": clean(str(s.get("do") or ""), True), "expect": clean(str(s.get("expect") or ""), True)}
             for s in raw.get("steps") or ()]
    prs = [n for n in raw.get("prs") or () if _int(n) and 0 < n < PR_NUMBER_MAX][:PRS_MAX]
    draft = {"title": title, "prs": prs, "steps": steps}
    if withheld:
        return draft, "it could not be checked for credentials"
    if not title:
        return draft, "its heading has nothing to show"
    if len(title) > TITLE_MAX:
        return draft, f"its heading is longer than {TITLE_MAX} characters"
    if not steps:
        return draft, "it has no numbered steps"
    if len(steps) > STEPS_MAX:
        return draft, f"it has more than {STEPS_MAX} steps"
    for index, step in enumerate(steps):
        if not step["do"]:
            return draft, f"step {index + 1} has nothing to show"
        if len(step["do"]) > DO_MAX or len(step["expect"]) > EXPECT_MAX:
            return draft, f"step {index + 1} is longer than {DO_MAX} characters"
    return draft, None
