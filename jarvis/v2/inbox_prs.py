"""The owner inbox's PR source (inbox B3, IN-4; design
`docs/plans/2026-10-24-owner-inbox-design.md` IN-7, the owner's answer to
§10 question 4 on 2026-10-27: **every open, non-draft PR in each project's
GitHub repository**, not only the ones Jarvis pushed).

**Where it runs.** In the daemon process, never in an agent's shell, on a
thread of its own (`PrSource`): every `POLL_INTERVAL_S` (5 minutes), and
`DEBOUNCE_S` after a task moves into DONE, FAILED or BLOCKED
(`Inbox.observe` hands each `task_status_changed` to `task_changed`, which
remembers each task's last state and pokes only on such a transition; a
poke only sets an event). A poll lists each repository with the owner's
`gh` login. `GET /inbox` reads only the last finished poll's snapshot under
this module's own lock, which no `gh` call ever holds, so a slow `gh` never
stalls the inbox, and `Daemon.start()` only starts the thread. Nothing runs
until `attach_system()` (daemon2's `main()` only), as `remote_secure` does:
an embedded or a test daemon never runs `gh` and has no PR rows.

**Which repository.** Each project that is not archived: its root's git
metadata validated without running git in the repository
(`codex_config.git_dirs` reads files only), then `remote.origin.url` read
by one hardened `git config --file <private copy> --no-includes` run in an
empty home, never in the repository (`chat_git._origin_url`), then
`chat_git.github_repo` (github.com only, `OWNER/REPO`). Anything else is
skipped: not a GitHub repository. Two projects on one repository list it
once. At most `REPOS_MAX` repositories are read; the rest are named in the
"incomplete" row below, never dropped silently.

**How `gh` is called.** Read-only and exact: `gh pr list --repo OWNER/REPO
--state open --search draft:false --limit PR_LIMIT --json <FIELDS>` (drafts
left out by the query, so they never use up the limit, and dropped again
here), no shell, in `/`, with
`chat_git._owner_env()` (the owner's HOME and PATH, no GIT_*, prompts and
pagers off) and nothing else. Its error output is never read (it could
carry a token); a timeout (`GH_TIMEOUT_S`), output past `OUTPUT_CAP`, a
non-zero exit, JSON that does not parse or a PR that does not have exactly
the shape below is a failure of that repository, in this module's own
words (`REASONS`).

**What a row says.** Rank 5 (`RANK_PR`). `title` and `blocks` are built
here from validated fields only: the number, the merge state, the checks
summary (`passing`, `failing`, `pending` or `none`), the repository, the
author's login (when it has GitHub's login shape) and the branch (when
`chat_git.valid_branch` accepts it). A PR from a fork says "from a fork"
and shows its branch only as `fork:<branch>`, never as a bare branch name. The PR's own title
is untrusted text: scrubbed, cleaned as decisions are
(`decisions.display_text`), scrubbed again and capped, and only in
`detail.title`. `detail.url` is the PR's URL only when it is exactly
`https://github.com/<OWNER>/<REPO>/pull/<number>` for the repository that
was asked and the PR's own number, else None (and the HUD links nothing).
A PR row takes no answer: the inbox's answer route refuses it with 400.
Merging stays where it was: the owner's own `gh`, or an agent's card under
the merge gate.

**Failure is visible.** A repository whose list fails has no PR rows (the
last good list is **dropped, not kept stale**: a PR merged meanwhile must
never show as waiting), and one row says so: `prx:list`, rank 5, "PR list
unavailable", naming how many repositories, with each repository, its
reason and when it last read well in `detail`. So an empty list is never
presented as "nothing waiting". A list that may be cut off (exactly
`PR_LIMIT` PRs came back) or a repository past `REPOS_MAX` that was not
read is named in the same row ("PR list incomplete" when nothing failed),
also counted.

**Freshness.** Each poll replaces the snapshot. A PR merged or closed is
gone after the next poll. `inbox_changed` `{ids, reason: "pr_poll"}` is
published only when the set of rows or a row changed, and carries no text.

SELF_PROTECTED (`jarvis/tools/files.py`), as `inbox.py`: it decides what the
owner sees as waiting. Tests: `tests/v2/inbox_prs_check.py`.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
import re
import selectors
import signal
import subprocess
import threading
import time

from .approvals import clean_line

LOG = logging.getLogger(__name__)

RANK_PR = 5
POLL_INTERVAL_S = 300.0
# A `task_status_changed` polls this long after it (more coalesce), and
# never sooner than `MIN_GAP_S` after the last poll.
DEBOUNCE_S = 10.0
MIN_GAP_S = 30.0
GH_TIMEOUT_S = 30.0
OUTPUT_CAP = 2 * 1024 * 1024
PR_LIMIT = 100
REPOS_MAX = 50
FIELDS = ("number", "title", "headRefName", "headRefOid", "isDraft", "url",
          "statusCheckRollup", "mergeStateStatus", "createdAt", "author", "isCrossRepository")
# A task moving into one of these may have opened or merged a PR: poll soon.
POKE_STATES = frozenset({"done", "failed", "blocked"})
_TASKS_SEEN_MAX = 2048
TITLE_MAX = 300

# Each failure in this module's own words: never `gh`'s output.
REASONS = {
    "missing": "gh (the GitHub CLI) is not installed",
    "timeout": "gh did not answer in time",
    "too_large": "gh's answer was too large",
    "login": "gh is not logged in",
    "exit": "gh failed",
    "malformed": "gh's answer could not be read",
    "error": "gh could not be run",
    # Not failures: the list is read, but may not be whole.
    "cut_off": f"it has {PR_LIMIT} or more open PRs; only the first {PR_LIMIT} are shown",
    "skipped": f"not read: Jarvis reads at most {REPOS_MAX} repositories",
}
_INCOMPLETE = frozenset({"cut_off", "skipped"})
_DETAIL_REPOS_MAX = 100

# A GitHub login, or gh's "app/<name>" for an app's PRs.
_LOGIN = re.compile(r"^(?:app/)?[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?$")
_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_URL = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]{1,100})/([A-Za-z0-9_.-]{1,100})/pull/([1-9][0-9]{0,9})$")
_MERGE_STATES = frozenset({"BEHIND", "BLOCKED", "CLEAN", "DIRTY", "DRAFT", "HAS_HOOKS", "UNKNOWN", "UNSTABLE"})
_FAILED = frozenset({"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR",
                     "STALE"})
_PASSED = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})


class GhFailed(Exception):
    """One repository's list could not be read; `code` is a `REASONS` key."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code if code in REASONS else "error"


# -- running gh -----------------------------------------------------------------------

def gh_argv(gh: str, repo: str) -> list[str]:
    return [gh, "pr", "list", "--repo", repo, "--state", "open", "--search", "draft:false",
            "--limit", str(PR_LIMIT), "--json", ",".join(FIELDS)]


def run_bounded(argv: list[str], env: dict, *, timeout: float, cap: int,
                stop: threading.Event | None = None) -> bytes:
    """`argv`'s standard output, read with a deadline and a size bound, or
    GhFailed. No shell, stdin and stderr closed to it, its own process
    group (killed whole on a timeout, an overflow or `stop`)."""
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=env, cwd="/",
                                start_new_session=True, close_fds=True)
    except FileNotFoundError:
        raise GhFailed("missing") from None
    except (OSError, ValueError):
        raise GhFailed("error") from None

    def kill():
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait(2)
        except subprocess.TimeoutExpired:
            pass

    deadline = time.monotonic() + timeout
    out = bytearray()
    fd = proc.stdout.fileno()
    try:
        with selectors.DefaultSelector() as sel:
            sel.register(fd, selectors.EVENT_READ)
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    kill()
                    raise GhFailed("timeout")
                if stop is not None and stop.is_set():
                    kill()
                    raise GhFailed("timeout")
                if not sel.select(min(left, 0.2)):
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                out += chunk
                if len(out) > cap:
                    kill()
                    raise GhFailed("too_large")
        try:
            code = proc.wait(max(0.05, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            kill()
            raise GhFailed("timeout") from None
    finally:
        proc.stdout.close()
        if proc.poll() is None:
            kill()
    if code == 4:
        raise GhFailed("login")         # gh's "authentication required"
    if code != 0:
        raise GhFailed("exit")
    return bytes(out)


# -- reading its answer -------------------------------------------------------------------

def checks_summary(rollup) -> str:
    """`passing`, `failing`, `pending` or `none`, from `statusCheckRollup`.
    A value this does not know counts as pending, never as passing."""
    if rollup is None or rollup == []:
        return "none"
    if not isinstance(rollup, list):
        raise GhFailed("malformed")
    failing = pending = False
    for item in rollup:
        if not isinstance(item, dict):
            raise GhFailed("malformed")
        if "state" in item and "conclusion" not in item:          # a StatusContext
            state = item.get("state")
            if state in _FAILED:
                failing = True
            elif state != "SUCCESS":
                pending = True
            continue
        status, conclusion = item.get("status"), item.get("conclusion")   # a CheckRun
        if status != "COMPLETED":
            pending = True
        elif conclusion in _FAILED:
            failing = True
        elif conclusion not in _PASSED:
            pending = True
    return "failing" if failing else "pending" if pending else "passing"


def clean_title(text: str) -> str:
    """A PR's title as the owner is shown it: decisions B1's treatment
    (scrub, `display_text`, scrub again), one line, capped."""
    from . import chat_reads
    from .decisions import display_text
    once = chat_reads.scrub(text)
    if once == chat_reads.WITHHELD:
        return once
    out = chat_reads.scrub(display_text(once, lines=False))
    return clean_line(out, TITLE_MAX)


def pr_url(url, repo: str, number: int) -> str | None:
    """The PR's URL when it is exactly this repository's PR `number`, else None."""
    if not isinstance(url, str):
        return None
    match = _URL.fullmatch(url)
    if not match:
        return None
    owner, name = repo.split("/", 1)
    if (match.group(1).lower(), match.group(2).lower()) != (owner.lower(), name.lower()):
        return None
    return url if int(match.group(3)) == number else None


def parse(data: bytes, repo: str) -> list[dict]:
    """The open, non-draft PRs in `gh`'s answer (`read`'s list)."""
    return read(data, repo)[0]


def read(data: bytes, repo: str) -> tuple[list[dict], bool]:
    """(the open, non-draft PRs in `gh`'s answer, each validated; whether the
    list may be cut off: `PR_LIMIT` PRs came back). GhFailed ("malformed")
    when the answer, or any PR in it, is not of that shape."""
    from .chat_git import valid_branch
    try:
        items = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise GhFailed("malformed") from None
    if not isinstance(items, list) or len(items) > PR_LIMIT:
        raise GhFailed("malformed")
    out, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise GhFailed("malformed")
        number, draft = item.get("number"), item.get("isDraft")
        if (not isinstance(number, int) or isinstance(number, bool) or not 0 < number < 10**10
                or not isinstance(draft, bool) or number in seen):
            raise GhFailed("malformed")
        seen.add(number)
        title, branch, head = item.get("title"), item.get("headRefName"), item.get("headRefOid")
        state = item.get("mergeStateStatus")
        if not isinstance(title, str) or not isinstance(branch, str) or not isinstance(head, str):
            raise GhFailed("malformed")
        if not isinstance(state, str) or len(title) > 10_000:
            raise GhFailed("malformed")
        author, fork = item.get("author"), item.get("isCrossRepository")
        if not isinstance(fork, bool) or not (author is None or isinstance(author, dict)):
            raise GhFailed("malformed")
        login = author.get("login") if isinstance(author, dict) else None
        checks = checks_summary(item.get("statusCheckRollup"))
        if draft:
            continue
        created = item.get("createdAt")
        try:
            at = datetime.fromisoformat(created.replace("Z", "+00:00")) if isinstance(created, str) else None
        except ValueError:
            at = None
        if at is not None and at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        out.append({
            "repo": repo, "number": number,
            "branch": branch if len(branch) <= 120 and valid_branch(branch) else None,
            "author": login if isinstance(login, str) and _LOGIN.fullmatch(login) else None,
            "fork": fork,
            "head": head if _SHA.fullmatch(head) else None,
            "checks": checks,
            "merge_state": state if state in _MERGE_STATES else "UNKNOWN",
            "title": clean_title(title),
            "url": pr_url(item.get("url"), repo, number),
            "created_at": at.astimezone(timezone.utc).isoformat(timespec="seconds") if at else None,
        })
    return out, len(items) >= PR_LIMIT


# -- the rows -------------------------------------------------------------------------------

def _headline(pr) -> str:
    n = pr["number"]
    if pr["merge_state"] == "DIRTY":
        return f"PR #{n} has merge conflicts"
    if pr["checks"] == "failing":
        return f"PR #{n} has failing checks"
    if pr["merge_state"] == "BEHIND":
        return f"PR #{n} is behind its base branch"
    if pr["checks"] == "pending":
        return f"PR #{n} waits on its checks"
    if pr["merge_state"] == "BLOCKED":
        return f"PR #{n} is held by its branch rules"
    if pr["merge_state"] in ("CLEAN", "HAS_HOOKS", "UNSTABLE"):
        return f"PR #{n} is ready to merge"
    return f"PR #{n} waits on you"


def pr_row(pr, project_id) -> dict:
    from .inbox import _row
    if pr["fork"]:
        # Never a bare branch name: a fork's `main` is not this repo's `main`.
        branch = f"from a fork (fork:{pr['branch']})" if pr["branch"] else "from a fork"
    else:
        branch = f"branch {pr['branch']}" if pr["branch"] else "branch name not shown"
    author = f"by {pr['author']}" if pr["author"] else "by an author not shown"
    blocks = f"{pr['repo']} · {author} · {branch} · checks {pr['checks']}"
    return _row("pr", RANK_PR, f"pr:{pr['repo']}#{pr['number']}", _headline(pr), blocks,
                project_id=project_id, asked_at=pr["created_at"],
                detail={"repo": pr["repo"], "number": pr["number"], "title": pr["title"],
                        "branch": pr["branch"], "author": pr["author"], "fork": pr["fork"],
                        "head": pr["head"], "checks": pr["checks"],
                        "merge_state": pr["merge_state"], "url": pr["url"]})


def unavailable_row(failures: dict) -> dict:
    """One row for every repository that could not be read, or was read
    only in part (`cut_off`) or not at all (`skipped`). "unavailable" when any
    failed, else "incomplete". Counted either way (rank 5)."""
    from .inbox import _row
    n = len(failures)
    repos = sorted(failures)
    failed = any(failures[r]["code"] not in _INCOMPLETE for r in repos)
    word = "unavailable" if failed else "incomplete"
    title = (f"PR list {word} for {repos[0]}" if n == 1
             else f"PR list {word} for {n} repositories")
    blocks = ("open pull requests may be waiting; they cannot be read now" if failed
              else "more open pull requests may be waiting; not all could be listed")
    shown = repos[:_DETAIL_REPOS_MAX]
    return _row("pr_unavailable", RANK_PR, "prx:list", title, blocks,
                detail={"repos": [{"repo": r, "code": failures[r]["code"],
                                   "reason": REASONS[failures[r]["code"]],
                                   "last_read": failures[r]["last_read"]} for r in shown],
                        "more": n - len(shown)})


# -- the source --------------------------------------------------------------------------

def project_repos(daemon) -> dict[str, list[str]]:
    """{OWNER/REPO: [project ids]} for every project that is not archived and
    whose root is a GitHub repository; never runs a hook or executable config."""
    from .chat_git import Refused, _origin_url, github_repo
    from .daemon import safe_list
    from .providers.codex_config import git_dirs
    out: dict[str, list[str]] = {}
    for project in sorted(safe_list(daemon.stores.projects), key=lambda p: p.id):
        if project.archived or not isinstance(project.root, str) or not project.root.startswith("/"):
            continue
        try:
            dirs = git_dirs(project.root)
            if not dirs:
                continue
            repo = github_repo(_origin_url(dirs[-1]))
        except (Refused, OSError, ValueError, RuntimeError):
            continue
        except Exception as exc:  # noqa: BLE001 — one project never stops the poll
            LOG.warning("inbox PRs: cannot read a project's origin (%s)", type(exc).__name__)
            continue
        if repo is None:
            continue
        key = next((k for k in out if k.lower() == repo.lower()), repo)
        out.setdefault(key, []).append(project.id)
    return out


class PrSource:
    """The poll and its snapshot. `list_repo(repo) -> bytes` reads one
    repository (`gh` by default, once attached); tests inject their own or
    point `gh` at a fake."""

    def __init__(self, daemon, *, clock=None, interval=POLL_INTERVAL_S, debounce=DEBOUNCE_S,
                 min_gap=MIN_GAP_S, timeout=GH_TIMEOUT_S, cap=OUTPUT_CAP):
        self.daemon = daemon
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.interval, self.debounce, self.min_gap = interval, debounce, min_gap
        self.timeout, self.cap = timeout, cap
        self.gh: str | None = None
        self.attached = False
        self.repos = project_repos           # a seam for tests
        self._lock = threading.Lock()        # the snapshot only; never held across gh
        self._poll_lock = threading.Lock()   # one poll at a time
        self._rows: dict[str, dict] = {}
        self._good: dict[str, str] = {}      # repo -> when it last read well
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_poll = 0.0
        self._task_states: dict[str, str] = {}

    # -- attaching and the schedule -------------------------------------------------

    def attach_system(self) -> None:
        """daemon2's `main()` only: the owner's `gh`, found on PATH."""
        from jarvis import gitops
        self.gh = gitops.gh_binary()
        self.attached = True

    def list_repo(self, repo: str) -> bytes:
        if self.gh is None:
            raise GhFailed("missing")
        from .chat_git import _owner_env
        return run_bounded(gh_argv(self.gh, repo), _owner_env(), timeout=self.timeout,
                           cap=self.cap, stop=self._stop)

    def start(self) -> None:
        """Starts the thread when attached; never polls on the caller's thread."""
        if not self.attached:
            return
        if self._thread is not None:
            if self._thread.is_alive():
                return          # running, or a stopped one not yet gone: never two
            self._thread = None
        self._stop.clear()

        def loop():
            while not self._stop.is_set():
                self._wake.clear()
                try:
                    self.poll()
                except Exception:  # noqa: BLE001 — never kills the loop
                    LOG.exception("inbox PRs: a poll broke")
                self._wake.wait(self.interval)
                if self._stop.is_set():
                    break
                if self._wake.is_set():
                    gap = self.min_gap - (time.monotonic() - self._last_poll)
                    self._stop.wait(max(self.debounce, gap))

        self._thread = threading.Thread(target=loop, name="jarvis-inbox-prs", daemon=True)
        self._thread.start()

    def poke(self) -> None:
        """Poll soon (debounced). Never blocks."""
        self._wake.set()

    def task_changed(self, record) -> None:
        """A `task_status_changed`: poke only when the task's state changed into
        one of `POKE_STATES` (the event carries the new state only, and fires
        on plain saves too, so the last state seen is remembered here)."""
        data = record.get("data") if isinstance(record, dict) else None
        task_id = record.get("task_id") if isinstance(record, dict) else None
        state = data.get("state") if isinstance(data, dict) else None
        if not isinstance(task_id, str) or not isinstance(state, str):
            return
        with self._lock:
            before = self._task_states.get(task_id)
            if len(self._task_states) >= _TASKS_SEEN_MAX and task_id not in self._task_states:
                self._task_states.clear()
            self._task_states[task_id] = state
        if state != before and state in POKE_STATES:
            self.poke()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(5)
            # One still running (a gh that would not die) stays named, so
            # `start` never runs a second loop beside it.
            self._thread = thread if thread.is_alive() else None

    # -- the poll -------------------------------------------------------------------

    def poll(self) -> list[str]:
        """One poll: read every repository, replace the snapshot, and publish
        `inbox_changed` when a row came, went or changed. Returns those ids."""
        with self._poll_lock:
            self._last_poll = time.monotonic()
            found = list(self.repos(self.daemon).items())
            repos, skipped = dict(found[:REPOS_MAX]), [r for r, _ in found[REPOS_MAX:]]
            now = self.clock().isoformat(timespec="seconds")
            fresh: dict[str, dict] = {}
            failures: dict[str, dict] = {r: {"code": "skipped", "last_read": None} for r in skipped}
            for repo, project_ids in repos.items():
                if self._stop.is_set():
                    return []
                try:
                    prs, cut_off = read(self.list_repo(repo), repo)
                except GhFailed as exc:
                    failures[repo] = {"code": exc.code, "last_read": self._good.get(repo)}
                    continue
                except Exception as exc:  # noqa: BLE001 — never a guess
                    LOG.warning("inbox PRs: reading a repository broke (%s)", type(exc).__name__)
                    failures[repo] = {"code": "error", "last_read": self._good.get(repo)}
                    continue
                self._good[repo] = now
                if cut_off:
                    failures[repo] = {"code": "cut_off", "last_read": now}
                project_id = project_ids[0] if len(project_ids) == 1 else None
                for pr in prs:
                    row = pr_row(pr, project_id)
                    fresh[row["id"]] = row
            if self._stop.is_set():
                return []           # shutting down: a cut-short gh is no "timeout" to show
            # Repositories no longer read are forgotten.
            self._good = {r: t for r, t in self._good.items() if r in repos}
            if failures:
                row = unavailable_row(failures)
                fresh[row["id"]] = row
            with self._lock:
                old, self._rows = self._rows, fresh
            changed = sorted({i for i in old.keys() | fresh.keys() if old.get(i) != fresh.get(i)})
            # Inbox B4: a PR that left a repository read in full (merged,
            # closed) forgets the owner's snooze or dismissal, so if it comes
            # back (reopened) it shows. A failed or cut-off read is no leave:
            # its PRs keep their marks.
            left = [i for i, r in old.items() if i not in fresh and r.get("kind") == "pr"
                    and (r.get("detail") or {}).get("repo") in repos
                    and (r.get("detail") or {}).get("repo") not in failures]
            forget = getattr(getattr(getattr(self.daemon, "inbox", None), "marks", None), "forget", None)
            if left and forget is not None:
                try:
                    forget(left)
                except Exception as exc:  # noqa: BLE001 — a mark is never worth a poll
                    LOG.warning("inbox PRs: cannot forget a closed PR's mark (%s)", type(exc).__name__)
        if changed:
            try:
                self.daemon.bus.publish({"kind": "inbox_changed",
                                         "data": {"ids": changed, "reason": "pr_poll"}})
            except Exception as exc:  # noqa: BLE001
                LOG.warning("inbox PRs: cannot publish (%s)", type(exc).__name__)
        return changed

    def rows(self, hidden=frozenset()) -> list[dict]:
        """The last poll's rows; no I/O, never waits on `gh`. A project
        archived since then hides its rows (an unavailable row stays)."""
        import copy
        with self._lock:
            rows = list(self._rows.values())
        return [copy.deepcopy(r) for r in rows if r.get("project_id") not in hidden]
