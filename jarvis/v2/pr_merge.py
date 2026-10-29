"""The owner's Merge, Update and Hold on an inbox PR row (PM-D of
`docs/plans/2026-10-28-pr-inbox-merge.md`: PM-12 to PM-16, the owner's Q1,
Q3 and Q4).

The owner's words of 2026-10-28: "I just need to be able to get an agents
summary of why it was blocked and get my ok to merge". The row (PM-C,
`pr_summary`) shows code's facts, code's flags, the model's summary and
code's actions; this module is what the owner's OK does.

**The routes (PM-15), the owner's own act (PM-16).**

    HUD (owner_only on FACE_PORT, the strength of `POST /approvals`: the
    HUD's listener, the HUD's Origin and its own Host; the id in the body,
    since a PR id holds "/" and "#"):
      POST /inbox/pr/merge   {id, head, card, action?}   action "merge" or "merge_as_is"
      POST /inbox/pr/update  {id, head, card}
      POST /inbox/pr/hold    {id, note} sets; {id, clear: true} clears
      POST /inbox/pr/fix     501 (PM-E)
    Phone (the remote listener, past `remote_auth.owner_status`):
      POST /m/api/inbox/pr/options  {id, action}  a challenge bound to `subject(...)`
      POST /m/api/inbox/pr          {id, action, head, card, assertion}

They never touch `PendingApprovals`, `merge_gate` or `remote_answer`'s
approval paths. No tool, MCP entry or API-listener route reaches them: the
HUD's are behind `inbox.route`'s `owner_only`, the phone's behind a session
and a passkey (`tests/v2/archive_check.py` greps every tool's source).

**What an OK binds (PM-13).** The row carries `head` and `card`
(`pr_summary.card_key`: sha256 over the facts parts, the summary, the
method and the offered actions, exactly as the row shows them). Under a
per-PR lock (a second action on the same PR while one runs is a 409, never
a second run) the PR is read again (`pr_facts.read_pr`), and the action runs
only when: no hold is set (checked before and after the read); the PR is
open, not a draft, and its head is still `head`; `pr_facts.decide` still
offers the action; and the card recomputed from the fresh facts and the
row's current summary equals `card`. Anything else is a 409 "changed since
you read it", a re-read is poked, and nothing runs.

**What runs.** Exactly, with no shell, stdin and stderr closed, its own
process group, the owner's gh login (`chat_git._owner_env`, gh's cache in a
private folder made for the act: `pr_facts.gh_env`), through
`inbox_prs.run_bounded`:

    gh pr merge N --repo O/R --merge --match-head-commit <head>
    gh pr merge N --repo O/R --squash --match-head-commit <head>   only when the card's method is squash (PM-14)
    gh pr update-branch N --repo O/R                                  BEHIND only

Never `--admin`, `--auto`, `--rebase` or `--delete-branch`. Its output is
never read and its error output never opened. The result comes from one
more read (`gh pr view N --repo O/R --json state,headRefOid`): merged, head
moved, not mergeable (Update: updated or not updated). A receipt goes to
`<data root>/inbox/pr-actions.jsonl` (0600: repo, number, head, action,
method, via, time, gh's exit word and the result), and a poll is poked.

**Hold (Q3).** `<data root>/inbox/pr-holds.json` (0600, folder 0700):
`{"owner/repo#N": {"note", "set_at"}}`, the note scrubbed, cleaned and
capped at `NOTE_MAX`. A hold hides every action on the row
(`pr_facts.decide(..., hold=note)`), and the row shows it; the act checks it
under the lock. Set and cleared at the desk only: no phone route. A file
that cannot be read holds every PR (fails closed) until it reads again.

**The phone (PM-15, Q1).** Each action takes a fresh passkey assertion
bound to `subject(repo, number, head, card, action)`: `pr:` and the first
56 hex of sha256("jarvis-pr-v1\\n" repo \\n number \\n head \\n card \\n
action), its own subject family and challenge budget in `remote_auth`; an
assertion of another family is never one of these, nor the reverse. The
assertion is verified first; then every §4.1 check runs again. A limited
phone may Merge and Update, except a PR flagged by PM-10 (or whose files
were not all seen), which is "answer it at the desk"; so is a PR whose
facts or file list the phone's scrub would change.

SELF_PROTECTED (`jarvis/tools/files.py`): it decides when the owner's OK
merges a pull request. Tests: `tests/v2/pr_merge_check.py`, mutants in
`tests/v2/pr_merge_mutants.py`.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import stat
import threading
import weakref

from . import pr_facts
from .inbox_prs import GhFailed

LOG = logging.getLogger("jarvis.pr_merge")

MERGE, MERGE_AS_IS, UPDATE, FIX = pr_facts.MERGE, pr_facts.MERGE_AS_IS, pr_facts.UPDATE, pr_facts.FIX
MERGE_ACTIONS = (MERGE, MERGE_AS_IS)
ACTIONS = (MERGE, MERGE_AS_IS, UPDATE)
METHOD_FLAGS = {"merge": "--merge", "squash": "--squash"}      # never rebase (PM-14)

CHANGED = "changed since you read it"
AT_DESK = "answer it at the desk"
BUSY = "another action on this pull request is running"
GONE = "this pull request is no longer in the inbox"
NO_GH = "pull request actions need the owner's gh, which this daemon has not attached"
NO_FIX = "Fix is not available yet"

HOLDS_FILE = "pr-holds.json"
RECEIPTS_FILE = "pr-actions.jsonl"
NOTE_MAX = 200
HOLDS_MAX = 500
HOLDS_CAP = 1024 * 1024
UNREADABLE = "the hold list could not be read"

READ_BUDGET_S = 90.0            # the fresh read before an act (PM-2's pass, shorter)
ACT_TIMEOUT_S = 60.0
ACT_CAP = 64 * 1024             # gh's output is discarded; past this it is killed
RESULT_TIMEOUT_S = 30.0
RESULT_CAP = 64 * 1024

SUBJECT_DOMAIN = "jarvis-pr-v1"
_CARD = re.compile(r"[0-9a-f]{64}")
_KEY = re.compile(r"[a-z0-9_.-]{1,100}/[a-z0-9_.-]{1,100}#[1-9][0-9]{0,9}")
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


class Refused(Exception):
    def __init__(self, status: int, word: str):
        super().__init__(word)
        self.status = status
        self.word = word


def _changed(why: str = "") -> Refused:
    return Refused(409, f"{CHANGED}: {why}" if why else CHANGED)


# -- what runs ---------------------------------------------------------------------------

def merge_argv(gh: str, repo: str, number: int, head: str, method: str) -> list[str]:
    """PM-13's merge, exactly. `method` is "merge" or "squash" (KeyError otherwise)."""
    return [gh, "pr", "merge", str(number), "--repo", repo, METHOD_FLAGS[method],
            "--match-head-commit", head]


def update_argv(gh: str, repo: str, number: int) -> list[str]:
    """PM-12's Update: GitHub merges the base into the PR's branch."""
    return [gh, "pr", "update-branch", str(number), "--repo", repo]


def result_argv(gh: str, repo: str, number: int) -> list[str]:
    return [gh, "pr", "view", str(number), "--repo", repo, "--json", "state,headRefOid"]


def subject(repo: str, number: int, head: str, card: str, action: str) -> str:
    """The phone's passkey subject for one action (PM-15): `pr:` and the
    first 56 hex of sha256("jarvis-pr-v1\\n" repo \\n number \\n head \\n
    card \\n action). No part can hold a newline (each is validated)."""
    text = "\n".join((SUBJECT_DOMAIN, repo, str(number), head, card, action))
    return "pr:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:56]


# -- ids ---------------------------------------------------------------------------------

def parse_id(row_id) -> tuple[str, int]:
    """(repo, number) of a `pr:OWNER/REPO#N` id, or a 400."""
    if not isinstance(row_id, str) or len(row_id) > 400 or not row_id.startswith("pr:"):
        raise Refused(400, "id must be a pull request row's id")
    repo, sep, number = row_id[3:].rpartition("#")
    if not sep or not number.isascii() or not number.isdigit() or number.startswith("0"):
        raise Refused(400, "id must be a pull request row's id")
    n = int(number)
    if not pr_facts.valid_repo(repo) or not pr_facts.valid_number(n):
        raise Refused(400, "id must be a pull request row's id")
    return repo, n


def hold_key(repo: str, number: int) -> str:
    return f"{repo.lower()}#{number}"


# -- private files -----------------------------------------------------------------------

def _prepare_dir(folder: Path) -> None:
    folder.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkdir(folder, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(folder)
    if not stat.S_ISDIR(info.st_mode):
        raise OSError("the inbox folder is not a plain directory")
    if stat.S_IMODE(info.st_mode) != 0o700:
        os.chmod(folder, 0o700)


def _read_file(path: Path, cap: int) -> bytes | None:
    """The file's bytes (None when absent); OSError for a link, a folder or
    anything too large."""
    flags = os.O_RDONLY | _NONBLOCK | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > cap:
            raise OSError("not a plain file of a sane size")
        chunks, left = [], cap + 1
        while left > 0 and (chunk := os.read(fd, min(1 << 16, left))):
            chunks.append(chunk)
            left -= len(chunk)
        data = b"".join(chunks)
        if len(data) > cap:
            raise OSError("too large")
        return data
    finally:
        os.close(fd)


class HoldsBroken(Exception):
    pass


class PrHolds:
    """`<inbox>/pr-holds.json`: the owner's holds (Q3). Read lazily, kept in
    memory, written whole (0600). One per file (`holds_at`)."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._holds: dict[str, dict] | None = None
        self.broken: str | None = None

    def _load(self) -> None:
        """Under `_lock`. A file that cannot be read marks the store broken
        (every PR held) and is tried again on the next call."""
        if self._holds is not None and self.broken is None:
            return
        try:
            data = _read_file(self.path, HOLDS_CAP)
            holds: dict[str, dict] = {}
            if data is not None:
                value = json.loads(data.decode("utf-8"))
                if not isinstance(value, dict) or len(value) > HOLDS_MAX:
                    raise ValueError("not a holds object")
                for key, item in value.items():
                    if (not isinstance(key, str) or not _KEY.fullmatch(key) or not isinstance(item, dict)
                            or set(item) != {"note", "set_at"} or not isinstance(item["note"], str)
                            or len(item["note"]) > NOTE_MAX or not isinstance(item["set_at"], str)
                            or len(item["set_at"]) > 40):
                        raise ValueError("not a hold")
                    holds[key] = {"note": item["note"], "set_at": item["set_at"]}
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            if self.broken is None:
                LOG.warning("PR holds: the file cannot be read (%s); every pull request is held",
                            type(exc).__name__)
            self._holds, self.broken = {}, type(exc).__name__
            return
        self._holds, self.broken = holds, None

    def get(self, repo: str, number: int) -> dict | None:
        """{note, set_at} of the hold on this PR, or None. While the file
        cannot be read, every PR is held (`UNREADABLE`)."""
        with self._lock:
            self._load()
            if self.broken is not None:
                return {"note": UNREADABLE, "set_at": None}
            hold = self._holds.get(hold_key(repo, number))
            return dict(hold) if hold is not None else None

    def _save(self, holds: dict) -> None:
        try:
            from .pr_summary import _write_private
            _prepare_dir(self.path.parent)
            if os.path.lexists(self.path) and not stat.S_ISREG(os.lstat(self.path).st_mode):
                raise OSError("not a plain file")
            _write_private(self.path, json.dumps(holds, ensure_ascii=True, sort_keys=True).encode())
            os.chmod(self.path, 0o600)
        except OSError as exc:
            LOG.warning("PR holds: the file cannot be written (%s)", type(exc).__name__)
            raise HoldsBroken(type(exc).__name__) from None

    def set(self, repo: str, number: int, note: str, at: str) -> dict:
        with self._lock:
            self._load()
            if self.broken is not None:
                raise HoldsBroken(self.broken)
            key = hold_key(repo, number)
            if key not in self._holds and len(self._holds) >= HOLDS_MAX:
                raise Refused(409, f"at most {HOLDS_MAX} pull requests can be held")
            holds = {**self._holds, key: {"note": note, "set_at": at}}
            self._save(holds)
            self._holds = holds
            return dict(holds[key])

    def clear(self, repo: str, number: int) -> bool:
        with self._lock:
            self._load()
            if self.broken is not None:
                raise HoldsBroken(self.broken)
            key = hold_key(repo, number)
            if key not in self._holds:
                return False
            holds = {k: v for k, v in self._holds.items() if k != key}
            self._save(holds)
            self._holds = holds
            return True


_HOLDS: dict[str, PrHolds] = {}
_HOLDS_LOCK = threading.Lock()


def holds_at(inbox_dir) -> PrHolds:
    """The one `PrHolds` of `<inbox_dir>/pr-holds.json` in this process
    (the row builder and the routes share it)."""
    path = str(Path(inbox_dir) / HOLDS_FILE)
    with _HOLDS_LOCK:
        if len(_HOLDS) > 64:
            _HOLDS.clear()
        found = _HOLDS.get(path)
        if found is None:
            found = _HOLDS[path] = PrHolds(path)
        return found


def clean_note(note) -> str:
    """The owner's hold note as stored: scrubbed, one line, capped."""
    if not isinstance(note, str):
        raise Refused(400, "note must be a string")
    return pr_facts.shown(pr_facts.scrub(note), NOTE_MAX)


def write_receipt(inbox_dir, record: dict) -> bool:
    """One line appended to `<inbox_dir>/pr-actions.jsonl` (0600, never
    through a link). False when it could not be written (logged)."""
    folder = Path(inbox_dir)
    try:
        _prepare_dir(folder)
        fd = os.open(folder / RECEIPTS_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT
                     | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | _NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("not a plain file")
            os.fchmod(fd, 0o600)
            os.write(fd, (json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        return True
    except OSError as exc:
        LOG.warning("PR actions: the receipt cannot be written (%s)", type(exc).__name__)
        return False


# -- the act ------------------------------------------------------------------------------

def _source(daemon):
    source = getattr(getattr(daemon, "inbox", None), "prs", None)
    if source is None:
        raise Refused(409, GONE)
    return source


def inbox_dir(daemon) -> Path:
    from jarvis import config
    root = getattr(getattr(daemon, "stores", None), "root", None)
    return Path(root if root is not None else config.V2_DATA_DIR) / "inbox"


def phone_withheld(facts_parts: list, files) -> bool:
    """Whether the phone's scrub (`remote_api.scrub_structure`) would change
    the facts or the file list (then: answer it at the desk)."""
    from .remote_api import scrub_structure
    return scrub_structure({"facts": facts_parts, "files": files})[1]


class PrActions:
    """The owner's Merge and Update for one daemon. Made on the first action
    (`actions_for`), never at start. `run` and `read` are seams for tests
    (default `inbox_prs.run_bounded` and `pr_facts.read_pr`); `gh` defaults
    to the poll's own (attached by daemon2's `main()` only)."""

    def __init__(self, daemon, *, run=None, read=None, gh: str | None = None, now=None):
        self.daemon = daemon
        self.run = run
        self.read = read
        self.gh = gh
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()

    def _lock_for(self, key: str) -> threading.Lock:
        with self._locks_lock:
            lock = self._locks.get(key)
            if lock is None:
                if len(self._locks) > 1024:
                    self._locks = {k: v for k, v in self._locks.items() if v.locked()}
                lock = self._locks[key] = threading.Lock()
            return lock

    def _gh(self) -> str:
        gh = self.gh or getattr(_source(self.daemon), "gh", None)
        if not gh:
            raise Refused(503, NO_GH)
        return gh

    def row(self, row_id: str) -> dict:
        """The PR row the owner reads now (the poll's snapshot), or 409."""
        from .projects import archived_project_ids
        try:
            hidden = archived_project_ids(self.daemon.stores)
        except Exception:  # noqa: BLE001 — as inbox.rows
            hidden = set()
        row = next((r for r in _source(self.daemon).rows(hidden)
                    if r.get("id") == row_id and r.get("kind") == "pr"), None)
        if row is None:
            raise Refused(409, GONE)
        return row

    def reread(self, repo: str, number: int) -> None:
        """Poke a fresh read of this PR and a poll (never waits)."""
        source = getattr(getattr(self.daemon, "inbox", None), "prs", None)
        summaries = getattr(source, "summaries", None)
        try:
            if summaries is not None and hasattr(summaries, "reread"):
                summaries.reread(repo, number)
            if source is not None:
                source.poke()
        except Exception as exc:  # noqa: BLE001 — a poke is never worth an error
            LOG.warning("PR actions: no re-read poked (%s)", type(exc).__name__)

    # -- the checks --------------------------------------------------------------------

    def _fresh(self, repo: str, number: int, gh: str):
        read = self.read or pr_facts.read_pr
        try:
            return read(repo, number, run=self.run, gh=gh, budget=READ_BUDGET_S)
        except Exception as exc:  # noqa: BLE001 — not read is never a guess
            LOG.warning("PR actions: the read broke (%s)", type(exc).__name__)
            return pr_facts._not_read(repo, number, "error")

    def judge(self, facts, action: str, head: str, card: str, row_id: str, *, via: str,
              limited: bool) -> "pr_facts.Decision":
        """PM-13's checks on a fresh read; the Decision, or Refused."""
        from .pr_summary import card_key, files_detail
        if not facts.ok("view") or facts.head is None:
            raise _changed("it could not be read again")
        if facts.state != "OPEN":
            raise _changed(f"it is {str(facts.state).lower()}")
        if facts.draft:
            raise _changed("it is a draft")
        if facts.head != head:
            raise _changed("its head moved")
        if limited and pr_facts.flagged(facts):
            raise Refused(409, "touches safety files: " + AT_DESK)
        decision = pr_facts.decide(facts)
        if action not in decision.actions:
            raise _changed("that action is not offered now")
        if action in MERGE_ACTIONS and decision.method not in METHOD_FLAGS:
            raise _changed("no merge method")
        parts = [p.as_dict() for p in pr_facts.facts_parts(facts)]
        summary = (self.row(row_id).get("detail") or {}).get("summary")
        shown = {"facts": parts, "summary": summary, "method": decision.method,
                 "actions": list(decision.actions)}
        if not hmac.compare_digest(card_key(shown), card):
            raise _changed()
        if via == "phone" and phone_withheld(parts, files_detail(facts)):
            raise Refused(409, "this pull request has withheld text: " + AT_DESK)
        return decision

    # -- running -----------------------------------------------------------------------

    def _call(self, argv: list[str], env: dict, timeout: float, cap: int) -> tuple[str, bytes]:
        """(gh's exit word, its output); never raises, never reads stderr."""
        from .inbox_prs import run_bounded
        run = self.run or run_bounded
        try:
            return "ok", run(argv, env, timeout=timeout, cap=cap)
        except GhFailed as exc:
            return exc.code, b""
        except Exception as exc:  # noqa: BLE001
            LOG.warning("PR actions: gh broke (%s)", type(exc).__name__)
            return "error", b""

    @staticmethod
    def _outcome(action: str, head: str, word: str, data: bytes) -> str:
        if word != "ok":
            return "not read again"
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return "not read again"
        state = value.get("state") if isinstance(value, dict) else None
        now = value.get("headRefOid") if isinstance(value, dict) else None
        if state not in ("OPEN", "CLOSED", "MERGED") or not pr_facts.valid_sha(now):
            return "not read again"
        if action in MERGE_ACTIONS:
            if state == "MERGED":
                return "merged"
            if now != head:
                return "head moved"
            return "closed" if state == "CLOSED" else "not mergeable"
        if state != "OPEN":
            return state.lower()
        return "updated" if now != head else "not updated"

    def act(self, action: str, row_id: str, head, card, *, via: str, limited: bool = False) -> dict:
        """One owner action, PM-13. Refused (409 "changed since you read
        it") when anything the OK bound has moved; nothing runs then."""
        if action not in ACTIONS:
            raise Refused(400, "action must be merge, merge_as_is or update")
        if not pr_facts.valid_sha(head):
            raise Refused(400, "head must be 40 hexadecimal characters")
        if not isinstance(card, str) or not _CARD.fullmatch(card):
            raise Refused(400, "card must be 64 hexadecimal characters")
        repo_id, number_id = parse_id(row_id)
        row = self.row(row_id)
        detail = row.get("detail") or {}
        repo, number = detail.get("repo"), detail.get("number")
        if (repo, number) != (repo_id, number_id) or not pr_facts.valid_repo(repo):
            raise Refused(409, GONE)
        gh = self._gh()
        lock = self._lock_for(hold_key(repo, number))
        if not lock.acquire(blocking=False):
            raise Refused(409, BUSY)
        try:
            return self._act(action, row_id, repo, number, head, card, gh, via=via, limited=limited)
        finally:
            lock.release()

    def _act(self, action, row_id, repo, number, head, card, gh, *, via, limited) -> dict:
        holds = holds_at(inbox_dir(self.daemon))
        try:
            if holds.get(repo, number) is not None:
                raise _changed("it is on hold")
            facts = self._fresh(repo, number, gh)
            decision = self.judge(facts, action, head, card, row_id, via=via, limited=limited)
            if holds.get(repo, number) is not None:            # set while it was read
                raise _changed("it is on hold")
        except Refused as exc:
            if exc.status == 409:
                self.reread(repo, number)
            raise
        method = decision.method if action in MERGE_ACTIONS else None
        argv = (merge_argv(gh, repo, number, head, method) if action in MERGE_ACTIONS
                else update_argv(gh, repo, number))
        home = None
        try:
            home = pr_facts.new_gh_home()
            env = pr_facts.gh_env(home)
            word, _ignored = self._call(argv, env, ACT_TIMEOUT_S, ACT_CAP)
            seen, data = self._call(result_argv(gh, repo, number), env, RESULT_TIMEOUT_S, RESULT_CAP)
        except (OSError, pr_facts.MirrorRefused) as exc:
            LOG.warning("PR actions: no private gh folder (%s)", type(exc).__name__)
            raise Refused(503, "the action could not be started") from None
        finally:
            if home is not None:
                pr_facts._remove_tree(home)
        result = self._outcome(action, head, seen, data)
        at = self.now().isoformat(timespec="seconds")
        receipt = {"repo": repo, "number": number, "head": head, "action": action, "method": method,
                   "via": via, "limited": bool(limited), "at": at, "gh": word, "result": result}
        saved = write_receipt(inbox_dir(self.daemon), receipt)
        LOG.info("PR actions: %s on a pull request by %s: %s", action, via, result)
        self.reread(repo, number)
        return {"ok": result in ("merged", "updated"), "result": result, "action": action,
                "method": method, "head": head, "receipt": saved}


_ACTORS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_ACTORS_LOCK = threading.Lock()


def actions_for(daemon) -> PrActions:
    with _ACTORS_LOCK:
        found = _ACTORS.get(daemon)
        if found is None:
            found = _ACTORS[daemon] = PrActions(daemon)
        return found


def install(daemon, actions: PrActions) -> None:
    """Tests: this daemon's actions use `actions` (fakes for gh)."""
    with _ACTORS_LOCK:
        _ACTORS[daemon] = actions


# -- hold ---------------------------------------------------------------------------------

def _refresh(daemon, row_id: str) -> None:
    source = getattr(getattr(daemon, "inbox", None), "prs", None)
    try:
        changed = source.refresh([row_id]) if source is not None else []
        if not changed:
            daemon.bus.publish({"kind": "inbox_changed", "data": {"ids": [row_id], "reason": "pr_hold"}})
    except Exception as exc:  # noqa: BLE001
        LOG.warning("PR holds: the row was not refreshed (%s)", type(exc).__name__)


def hold(daemon, body: dict) -> dict:
    """`POST /inbox/pr/hold` (desk only): `{id, note}` sets, `{id, clear:
    true}` clears."""
    row_id = body.get("id")
    repo, number = parse_id(row_id)
    store = holds_at(inbox_dir(daemon))
    try:
        if "clear" in body:
            if body["clear"] is not True or "note" in body:
                raise Refused(400, "send either note or clear: true")
            store.clear(repo, number)
            out = {"ok": True, "hold": None}
        else:
            if "note" not in body:
                raise Refused(400, "send either note or clear: true")
            note = clean_note(body["note"])
            actions_for(daemon).row(row_id)                 # a PR the inbox shows now
            out = {"ok": True, "hold": store.set(repo, number, note,
                                                 datetime.now(timezone.utc).isoformat(timespec="seconds"))}
    except HoldsBroken:
        raise Refused(503, "the hold list cannot be written or read; nothing was changed") from None
    _refresh(daemon, row_id)
    return out


# -- the HUD's routes ---------------------------------------------------------------------

def hud_route(handler, daemon, parts, query):
    """`POST /inbox/pr/{merge,update,hold,fix}`, mounted by `inbox.route`
    after its `owner_only`; None for any other path."""
    if len(parts) < 2 or parts[0] != "inbox" or parts[1] != "pr":
        return None
    from jarvis import config
    from .daemon import APIError, _object
    from .projects import owner_only
    owner_only(handler, daemon)
    if len(parts) != 3 or parts[2] not in ("merge", "update", "hold", "fix"):
        raise APIError(404, "route not found")
    if handler.command != "POST":
        raise APIError(405, "method not allowed")
    _object(query, ())
    if parts[2] == "fix":
        raise APIError(501, NO_FIX)
    # The strength of `POST /approvals`: the HUD listener's own Host too.
    if not config.own_host_header(handler.headers.get("Host", ""), daemon.face_port):
        raise APIError(403, "only the owner can do this, from the HUD")
    try:
        if parts[2] == "hold":
            body = _object(handler._body(), ("id", "note", "clear"), ("id",))
            return 200, hold(daemon, body)
        if parts[2] == "merge":
            body = _object(handler._body(), ("id", "head", "card", "action"), ("id", "head", "card"))
            action = body.get("action", MERGE)
            if action not in MERGE_ACTIONS:
                raise Refused(400, "action must be merge or merge_as_is")
        else:
            body = _object(handler._body(), ("id", "head", "card"), ("id", "head", "card"))
            action = UPDATE
        seen = getattr(daemon, "owner_seen", None)
        if callable(seen):
            seen()                      # QH-3: the owner at the HUD acted
        return 200, actions_for(daemon).act(action, body["id"], body["head"], body["card"], via="hud")
    except Refused as exc:
        raise APIError(exc.status, exc.word) from None


# -- the phone's routes -------------------------------------------------------------------

PHONE_OPTIONS = "/m/api/inbox/pr/options"
PHONE_ACT = "/m/api/inbox/pr"


def _phone_action(value) -> str:
    if value == FIX:
        raise Refused(501, NO_FIX)
    if value not in ACTIONS:
        raise Refused(400, "action must be merge, merge_as_is or update")
    return value


def _secure(daemon, handler, session) -> dict:
    from .remote_answer import _Refused as AnswerRefused, _monitor
    try:
        return _monitor(daemon).check(session=session, handler=handler)
    except AnswerRefused as exc:
        raise Refused(exc.status, exc.word) from None


def phone_options(handler, daemon, session, body: dict) -> dict:
    """The challenge for one action on one row, after the refusals that
    need no passkey; bound to the row's head and card as shown now."""
    if set(body) - {"id", "action"}:
        raise Refused(400, "bad request")
    action = _phone_action(body.get("action"))
    row_id = body.get("id")
    repo, number = parse_id(row_id)
    actor = actions_for(daemon)
    detail = actor.row(row_id).get("detail") or {}
    head, card = detail.get("head"), detail.get("card")
    if action not in (detail.get("actions") or []) or detail.get("hold") \
            or not pr_facts.valid_sha(head) or not isinstance(card, str) or not _CARD.fullmatch(card):
        raise _changed()
    if holds_at(inbox_dir(daemon)).get(repo, number) is not None:
        raise _changed("it is on hold")
    secure = _secure(daemon, handler, session)
    flags = detail.get("flags")
    if not secure.get("ok") and (not isinstance(flags, dict) or flags.get("flagged") is not False):
        raise Refused(409, "touches safety files: " + AT_DESK)
    if phone_withheld(detail.get("facts"), detail.get("files")):
        raise Refused(409, "this pull request has withheld text: " + AT_DESK)
    bound = subject(repo, number, head, card, action)
    return {"action": action, "head": head, "card": card,
            "publicKey": handler.remote_auth.answer_options(session, bound, action)}


def phone_act(handler, daemon, session, body: dict) -> dict:
    """One action, after its own assertion; then every §4.1 check again."""
    if set(body) - {"id", "action", "head", "card", "assertion"}:
        raise Refused(400, "bad request")
    action = _phone_action(body.get("action"))
    row_id, head, card = body.get("id"), body.get("head"), body.get("card")
    repo, number = parse_id(row_id)
    if not pr_facts.valid_sha(head) or not isinstance(card, str) or not _CARD.fullmatch(card):
        raise Refused(400, "bad request")
    assertion = body.get("assertion")
    if not isinstance(assertion, dict):
        raise Refused(401, "this action needs the passkey")
    # The assertion first, bound to this PR, its head, its card and this action.
    handler.remote_auth.answer_verify(session, assertion, subject(repo, number, head, card, action), action)
    seen = getattr(daemon, "owner_seen", None)
    if callable(seen):
        seen()                          # QH-3: a verified phone answer is presence
    secure = _secure(daemon, handler, session)
    limited = not secure.get("ok")
    out = actions_for(daemon).act(action, row_id, head, card, via="phone", limited=limited)
    return {**out, "authority": "limited" if limited else "hud"}


def phone_route(handler, path: str, session: dict, body: bytes):
    """(status, payload, headers) for the two phone paths, None otherwise.
    Reached only from `remote_auth.api_route`, past the session check."""
    if path not in (PHONE_OPTIONS, PHONE_ACT):
        return None
    if handler.command != "POST":
        return 405, {"error": "method not allowed"}, [("Allow", "POST")]
    daemon = getattr(handler, "daemon", None)
    auth = getattr(handler, "remote_auth", None)
    if daemon is None or auth is None:
        return 404, {"error": "not found"}, []
    from .remote_auth import WORDS, AuthError, _json_body
    try:
        value = _json_body(body)
        if path == PHONE_OPTIONS:
            return 200, phone_options(handler, daemon, session, value), []
        return 200, phone_act(handler, daemon, session, value), []
    except Refused as exc:
        from .remote_answer import _word
        return exc.status, {"error": _word(exc.word)}, []
    except AuthError as exc:
        word = exc.word if exc.status in (409, 429, 503) else WORDS.get(exc.status, "unauthorized")
        return exc.status, {"error": word}, []


__all__ = ["ACTIONS", "AT_DESK", "CHANGED", "PrActions", "PrHolds", "actions_for", "clean_note",
           "holds_at", "hud_route", "merge_argv", "phone_route", "subject", "update_argv"]
