"""The editor's navigation routes (ED-7): quick open's file list, find in
files, and the explorer's new file, new folder, rename and delete.

Editor plan §2.8, decisions E-7/E-8 and the owner's `.env*` rule. **Every
route here is owner-only** (`projects.owner_only`, checked by `hud_api.route`
before anything is read): the HUD listener and the HUD's own page, never the
API listener every tool's HTTP client uses. No tool, MCP tool or fast-path
tool reaches this module.

Nothing here keeps a copy of a rule. What a path is comes from
`editor_policy` (the one classifier: gate, withheld, safety, env), where a
path may go from `hud_api.scope` (the project's roots, no `..`, no
`SKIP_DIRS` component, lexical *and* resolved), the protected-file filter of
a search from `tools.search.refused` (the one `grep_files` uses), names
Windows cannot hold from `folders`, and a delete goes to `trash.Trash.put` —
a move, never an unlink.

- `GET /projects/{id}/files`: every file quick open may offer, relative to
  the project root. `git ls-files --cached --others --exclude-standard` (git
  run hardened, `gitops._git`) in a repository, else a walk; dotfiles are
  listed, `SKIP_DIRS` and symlinked folders are never entered, a credential
  folder is never listed or entered, a withheld file or the gate's own state
  is never named, and a symlink is listed only when it lands on a regular
  file inside the project. At most 50,000 files, 20 folders deep and 5 s.
- `POST /projects/{id}/search`: find in files on the core `grep_files` also
  uses (`tools.search.search_bounded`). At most 2,000 matches and 10 s;
  `.gitignore` respected unless `ignore: false`. Refused on the Inbox (it is
  the whole home folder). A hit is shown only when `hud_api.scope` places it
  in the project and `editor_policy` says it is neither withheld nor the
  gate's state.
- `POST /projects/{id}/fs`: `{op: new_file | new_folder | rename | delete,
  path, to?}`. Each path must pass `scope` and must not be the gate's state,
  a withheld file or folder, the safety layer (SELF_PROTECTED, git's
  internals, the HUD install root); a folder that holds any of those is
  refused too. A rename never replaces anything (refused when the target
  exists). An env file is the owner's: the owner may create one (0600) and
  rename one to another env name, never to a name Jarvis can read.

None of this is a boundary against a program running as the owner.
"""
from __future__ import annotations

import errno
import logging
import os
from pathlib import Path, PurePosixPath
import re
import stat as _stat
import threading
import time

from jarvis import config
from jarvis.tools import files as v1_files
from jarvis.tools.search import refused as search_refused, search_bounded
from jarvis.tools.secrets import is_env_credential, is_env_credential_name
from . import editor_policy, folders, hud_api as H

LOG = logging.getLogger(__name__)

LIST_CAP = 50_000
LIST_DEPTH = 20
LIST_SECONDS = 5.0
SEARCH_CAP = 2_000
SEARCH_SECONDS = 10.0
QUERY_CAP = 1_000
LINE_CAP = 1_000
# A folder renamed or deleted is walked to see what it holds; past this many
# entries it is refused rather than judged in part.
FOLDER_WALK_CAP = 20_000
OPS = ("new_file", "new_folder", "rename", "delete")
# One explorer operation at a time. Not the daemon's own lock: a delete on a
# Windows drive waits on the Recycle Bin (PowerShell, up to 2 min), and the
# daemon must not wait with it.
_FS_LOCK = threading.Lock()
# What the editor never creates, renames, moves or deletes.
REFUSED_KINDS = (editor_policy.GATE, editor_policy.WITHHELD, editor_policy.SAFETY)


def fail(status, text):
    H.fail(status, text)


def _under(path: Path, directory: Path) -> bool:
    return path == directory or directory in path.parents


def _policy_for(root: Path) -> editor_policy.Policy:
    """The one classifier, narrowed to the credential folders that can matter
    under `root` — **for quick open's listing only**, which walks the root and
    nothing else and admits a symlink only when it lands inside the root, so a
    folder that neither holds `root` nor sits inside it can match nothing
    there. The judgement is still the policy's own `is_withheld`; this skips
    folders it could never match, so 50,000 files are not each compared with
    every credential folder. Search and the explorer use the whole policy: a
    project's extra folders are roots too (review of PR #53)."""
    policy = editor_policy.Policy()
    policy.credential_dirs = [d for d in policy.credential_dirs if _under(d, root) or _under(root, d)]
    return policy


# -- quick open -------------------------------------------------------------------

def list_files(project) -> dict:
    """`GET /projects/{id}/files`: what quick open may offer (see the module note)."""
    raw_root = Path(project.root).expanduser()
    root = raw_root.resolve()
    if not root.is_dir():
        fail(404, "the project folder is not there")
    policy = _policy_for(root)
    if policy.is_withheld(raw_root, root):
        fail(403, "a credential folder: its contents are never listed")
    deadline = time.monotonic() + LIST_SECONDS
    out: list[str] = []
    env: list[str] = []
    state = {"truncated": False}

    def admit(rel: str, is_link: bool) -> bool:
        """Add one file (relative, POSIX); False once a bound is reached."""
        if len(out) >= LIST_CAP or time.monotonic() > deadline:
            state["truncated"] = True
            return False
        raw = raw_root / rel
        if is_link:
            try:
                resolved = raw.resolve()
            except (OSError, RuntimeError):
                return True
            if not _under(resolved, root) or not resolved.is_file():
                return True  # a symlink out of the project, or not to a file
            if any(p in H.SKIP_DIRS for p in resolved.relative_to(root).parts):
                return True
            env_file = is_env_credential(raw)
        else:
            resolved = root / rel
            env_file = is_env_credential_name(raw.name)
        if resolved in policy.gate or policy.is_withheld(raw, resolved):
            return True
        out.append(rel)
        if env_file:
            env.append(rel)
        return True

    source = _git_listing(root, admit, policy, raw_root)
    if source is None:
        source = "walk"
        _walk_listing(root, raw_root, admit, policy, deadline, state)
    return {"files": out, "env": env, "truncated": state["truncated"], "source": source}


def _dir_ok(rel_parts: tuple[str, ...], raw_root: Path, root: Path, policy, cache: dict) -> bool:
    """A folder (relative parts) quick open may look inside: no SKIP_DIRS
    name, not withheld — judged once per folder, parents first."""
    if not rel_parts:
        return True
    if rel_parts in cache:
        return cache[rel_parts]
    ok = (_dir_ok(rel_parts[:-1], raw_root, root, policy, cache)
          and rel_parts[-1] not in H.SKIP_DIRS
          and not policy.is_withheld(raw_root.joinpath(*rel_parts), root.joinpath(*rel_parts)))
    cache[rel_parts] = ok
    return ok


def _git_listing(root: Path, admit, policy, raw_root: Path) -> str | None:
    """The repository's own idea of its files, or None when it is not one."""
    from jarvis import gitops
    try:
        code, stdout, _ = gitops._git(
            root, ["-c", "core.quotePath=false", "ls-files", "--cached", "--others",
                   "--exclude-standard", "-z", "--"], timeout=LIST_SECONDS)
    except FileNotFoundError:
        return None
    if code != 0:
        return None
    cache: dict = {}
    seen: set[str] = set()
    for rel in stdout.split("\0"):
        if not rel or rel in seen:
            continue
        seen.add(rel)
        parts = PurePosixPath(rel).parts
        if ".." in parts or rel.startswith("/") or "�" in rel:
            continue
        if not _dir_ok(tuple(parts[:-1]), raw_root, root, policy, cache) or parts[-1] in H.SKIP_DIRS:
            continue
        try:
            st = os.lstat(root / rel)
        except OSError:
            continue  # in the index, gone from the disk
        link = _stat.S_ISLNK(st.st_mode)
        if not link and not _stat.S_ISREG(st.st_mode):
            continue  # a submodule's gitlink, a socket …
        if not admit(rel, link):
            break
    return "git"


def _walk_listing(root: Path, raw_root: Path, admit, policy, deadline: float, state: dict) -> None:
    """A walk for a folder that is not a repository: dotfiles listed,
    SKIP_DIRS, credential folders and symlinked folders never entered."""
    stack: list[tuple[Path, tuple[str, ...]]] = [(root, ())]
    while stack:
        directory, parts = stack.pop()
        if time.monotonic() > deadline:
            state["truncated"] = True
            return
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            continue
        subdirs = []
        for entry in entries:
            rel_parts = parts + (entry.name,)
            rel = "/".join(rel_parts)
            try:
                link = entry.is_symlink()
                if not link and entry.is_dir(follow_symlinks=False):
                    if (entry.name in H.SKIP_DIRS or len(rel_parts) > LIST_DEPTH
                            or policy.is_withheld(raw_root / rel, root / rel)):
                        continue
                    subdirs.append((Path(entry.path), rel_parts))
                    continue
                if not link and not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if not admit(rel, link):
                return
        stack.extend(reversed(subdirs))


# -- find in files ------------------------------------------------------------------
#
# **The owner's pattern is never evaluated in the daemon** (review of PR #53,
# HIGH 1): ripgrep matches and reports every highlight span, in its own
# process, under the deadline (`tools.search.search_bounded`). Without ripgrep
# a regular expression is refused with a sentence; a literal is escaped text.
# One window's newer search cancels its older one (`key`), and at most
# `MAX_SEARCHES` run at once.

MAX_SEARCHES = 4
_SEARCH_LOCK = threading.Lock()
_SEARCHES: dict[str, threading.Event] = {}
_KEY = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _search_slot(key: str | None) -> tuple[str, threading.Event]:
    """Register a search; a running one from the same window is cancelled."""
    ev = threading.Event()
    with _SEARCH_LOCK:
        if key and key in _SEARCHES:
            _SEARCHES[key].set()
            del _SEARCHES[key]
        running = sum(1 for e in _SEARCHES.values() if not e.is_set())
        if running >= MAX_SEARCHES:
            fail(429, "too many searches are running at once: wait for one to finish")
        slot = key or f"anon-{id(ev)}"
        _SEARCHES[slot] = ev
    return slot, ev


def _search_done(slot: str, ev: threading.Event) -> None:
    with _SEARCH_LOCK:
        if _SEARCHES.get(slot) is ev:
            del _SEARCHES[slot]


def search(project, body) -> dict:
    """`POST /projects/{id}/search` (see the module note)."""
    from .daemon import _object
    body = _object(body, ("query", "regex", "case", "word", "glob", "ignore", "key"), ("query",))
    query = body["query"]
    if not isinstance(query, str) or not query or len(query) > QUERY_CAP or "\x00" in query:
        fail(400, f"query must be 1 to {QUERY_CAP} characters")
    flags = {}
    for name, default in (("regex", False), ("case", False), ("word", False), ("ignore", True)):
        value = body.get(name, default)
        if not isinstance(value, bool):
            fail(400, f"{name} must be true or false")
        flags[name] = value
    glob = body.get("glob", "") or ""
    if not isinstance(glob, str) or len(glob) > 200 or "\x00" in glob or "\n" in glob:
        fail(400, "glob must be a short pattern")
    key = body.get("key")
    if key is not None and not (isinstance(key, str) and _KEY.fullmatch(key)):
        fail(400, "key must be 1 to 64 letters, digits, - or _")
    if getattr(project, "inbox", False):
        fail(400, "find in files is not offered on the Inbox: it is your whole home folder. "
                  "Search a project instead.")
    raw_root = Path(project.root).expanduser()
    root = raw_root.resolve()
    if not root.is_dir():
        fail(404, "the project folder is not there")
    policy = editor_policy.Policy()
    if policy.is_withheld(raw_root, root):
        fail(403, "a credential folder: it is never searched")

    verdicts: dict[str, str | None] = {}

    def where(path: str) -> str | None:
        """The hit's path relative to the project, or None: not shown."""
        if path in verdicts:
            return verdicts[path]
        rel = None
        try:
            relative = Path(path).relative_to(root)
            raw, resolved = H.scope(project, str(relative))
            if _under(resolved, root):
                verdict = policy.classify(raw, resolved)
                if verdict.kind not in (editor_policy.WITHHELD, editor_policy.GATE, editor_policy.ENV):
                    rel = relative.as_posix()
        except Exception:  # noqa: BLE001 — scope's 403s and anything odd: not shown
            rel = None
        verdicts[path] = rel
        return rel

    slot, cancel = _search_slot(key)
    try:
        found = search_bounded(query, root, regex=flags["regex"], word=flags["word"], glob=glob,
                               case_insensitive=not flags["case"], respect_ignore=flags["ignore"],
                               hidden=True, skip=frozenset(H.SKIP_DIRS), limit=SEARCH_CAP,
                               timeout=SEARCH_SECONDS, keep=lambda hit: where(hit.path) is not None,
                               cancel=cancel)
    finally:
        _search_done(slot, cancel)
    if found.error:
        if found.error_kind == "no_regex":
            fail(400, found.error)
        if found.error_kind == "regex":
            fail(400, "not a regular expression ripgrep accepts: " + found.error.splitlines()[-1][:200])
        fail(500, found.error)
    matches = []
    for hit in found.hits:
        rel = where(hit.path)
        if rel is None or hit.line is None:
            continue
        text = hit.text[:LINE_CAP]
        # Spans are UTF-16 columns (the window's): judged against the cut
        # text's length in those units, never in code points.
        width = len(text.encode("utf-16-le", errors="surrogatepass")) // 2
        ranges = [[a, z] for a, z in hit.ranges if z <= width]
        matches.append({"path": rel, "line": hit.line, "text": text, "ranges": ranges})
    return {"matches": matches, "files": len({m["path"] for m in matches}),
            "truncated": found.truncated, "timed_out": found.timed_out, "cancelled": found.cancelled,
            "partial": found.partial, "backend": found.backend, "limit": SEARCH_CAP}


# -- explorer operations --------------------------------------------------------------

def _check_name(name: str) -> None:
    """Names the explorer may write: relative to the project (never `/` or
    `~`), every component a real name, nothing invisible. The Windows rules
    are judged where the path really lands (`_check_windows`)."""
    if not isinstance(name, str) or not name or len(name) > 1024 or name.startswith(("/", "~")):
        fail(400, "a path relative to the project")
    for part in name.split("/"):
        if part in ("", "."):
            fail(400, "every part of the path must be a name")  # `..` is scope's 403
        if len(part.encode("utf-8", "replace")) > folders.MAX_COMPONENT:
            fail(400, "a name is too long")
        if folders._control(part) or any(ord(c) < 32 for c in part):
            fail(400, "a name may not hold control or invisible characters")


def _check_windows(name: str, resolved: Path) -> None:
    """On a Windows drive — judged by where the path resolves, not by the
    project's root — nothing Windows reserves or cannot hold (`folders`' rules)."""
    if not re.match(r"^/mnt/[a-zA-Z](/|$)", str(resolved)):
        return
    for part in name.split("/"):
        if folders._WINDOWS_RESERVED.match(part) or folders._WINDOWS_RESERVED.match(part.split(".")[0]):
            fail(400, f"`{part}` is a name Windows reserves")
        if set(part) & folders._WINDOWS_BAD_CHARS or part.endswith((".", " ")):
            fail(400, f"`{part}` is not a name Windows can hold")


def _project_roots(daemon) -> list[Path]:
    """Every project's folders (archived ones included), resolved."""
    out = []
    for project in daemon.stores.projects.list():
        for root in [project.root, *getattr(project, "extra_dirs", [])]:
            try:
                out.append(Path(root).expanduser().resolve())
            except (OSError, RuntimeError):
                continue
    return out


def _judge(daemon, project, policy, name: str, *, moves: bool):
    """`(raw, resolved, verdict)` for a path the explorer may touch, else a
    refusal. `moves`: a rename (either side) or a delete — the path may not be
    any project's folder or hold one."""
    _check_name(name)
    raw, resolved = H.scope(project, name)
    _check_windows(name, resolved)
    lexical = Path(os.path.abspath(raw))
    for jarvis_dir in editor_policy.jarvis_dirs():
        if _under(resolved, jarvis_dir) or _under(lexical, jarvis_dir):
            fail(403, "refused: that is Jarvis's own records, which the editor never changes")
    if moves:
        for root in _project_roots(daemon):
            if _under(root, resolved) or _under(root, lexical):
                fail(403, "refused: that is a project's folder, or holds one")
    verdict = policy.classify(raw, resolved)
    if verdict.kind in REFUSED_KINDS:
        fail(403, "refused: " + (verdict.reason or "the editor never changes this"))
    if v1_files._hud_build(resolved) or v1_files._git_internal(resolved):
        fail(403, "refused: the editor never changes git's internals or the HUD it is served from")
    return raw, resolved, verdict


def _holds_protected(daemon, raw: Path, resolved: Path, policy) -> str | None:
    """Why a folder may not be renamed or deleted, or None.

    It may not hold the gate's state, a credential folder or login file, the
    install root or build the HUD is served from, Jarvis's checkout's safety
    layer, Jarvis's own records or a project's folder — checked by absolute
    path, wherever they are below it — nor anything the classifier refuses,
    found by a bounded walk (symlinks not followed, `SKIP_DIRS` not entered).
    Past `FOLDER_WALK_CAP` entries it is refused rather than judged in part."""
    try:
        hud = v1_files._hud_roots()
    except (OSError, RuntimeError):
        return "cannot tell what it holds"
    anchors = (list(policy.gate) + list(editor_policy.credential_dirs()) + list(policy.login_files) + list(hud)
               + editor_policy.jarvis_dirs() + _project_roots(daemon))
    repo = Path(config.REPO_ROOT).resolve()
    anchors += [repo / rel for rel in v1_files.SELF_PROTECTED]
    for anchor in anchors:
        if _under(anchor, resolved):
            return "it holds a file or folder the editor never changes"
    seen = 0
    stack = [(raw, resolved)]
    while stack:
        here_raw, here = stack.pop()
        try:
            entries = list(os.scandir(here))
        except OSError:
            return "cannot read all of it"
        for entry in entries:
            seen += 1
            if seen > FOLDER_WALK_CAP:
                return f"it holds more than {FOLDER_WALK_CAP:,} entries; do this in a terminal"
            entry_raw, entry_res = here_raw / entry.name, here / entry.name
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                return "cannot read all of it"
            if is_dir and entry.name in H.SKIP_DIRS:
                continue
            verdict = policy.classify(entry_raw, entry_res)
            if verdict.kind in REFUSED_KINDS:
                return f"it holds {entry_raw.relative_to(raw).as_posix()}, which the editor never changes"
            if is_dir:
                stack.append((entry_raw, entry_res))
    return None


def _same_but_case(a: Path, b: Path) -> bool:
    """`b` exists only because the filesystem folds case (a Windows drive):
    `foo.txt` to `Foo.txt` is a rename of the file itself, not onto another."""
    try:
        return str(a) != str(b) and str(a).casefold() == str(b).casefold() and os.path.samefile(a, b)
    except OSError:
        return False


# -- acting through directory fds ------------------------------------------------------
#
# Every change is made relative to a folder opened component by component from
# the project's real root, each `O_DIRECTORY | O_NOFOLLOW` — so a folder on
# the way swapped for a link after the checks above cannot carry the change
# elsewhere — and a rename never replaces (`renameat2` with RENAME_NOREPLACE;
# where the filesystem cannot, a check just before). What remains: a delete
# goes to `trash.Trash.put`, which moves by path, after a re-check that the
# entry under that folder fd is the one judged (same device and inode).

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_RENAME_NOREPLACE = 1


def _open_parent(project, name: str, *, make: bool = False) -> tuple[int, str]:
    """An fd on the folder holding `name`, and its last component. `make`
    creates missing folders on the way (0755)."""
    fd = os.open(os.path.realpath(Path(project.root).expanduser()), os.O_RDONLY | os.O_DIRECTORY)
    parts = name.split("/")
    try:
        for part in parts[:-1]:
            try:
                nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not make:
                    raise
                os.mkdir(part, 0o755, dir_fd=fd)
                nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            fail(403, "refused: a folder on the way is a link or not a folder")
        raise
    return fd, parts[-1]


def _rename_noreplace(src_fd: int, src: str, dst_fd: int, dst: str) -> None:
    import ctypes
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        fn = libc.renameat2
    except (OSError, AttributeError):
        fn = None
    if fn is not None:
        rc = fn(src_fd, os.fsencode(src), dst_fd, os.fsencode(dst), _RENAME_NOREPLACE)
        if rc == 0:
            return
        err = ctypes.get_errno()
        if err not in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
            raise OSError(err, os.strerror(err))
    # The filesystem cannot refuse to replace (9p on /mnt/c): check, then act.
    try:
        os.stat(dst, dir_fd=dst_fd, follow_symlinks=False)
    except FileNotFoundError:
        os.rename(src, dst, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
        return
    raise FileExistsError(errno.EEXIST, "exists")


def fs(daemon, project, body):
    """`POST /projects/{id}/fs` (see the module note)."""
    from .daemon import _object
    body = _object(body, ("op", "path", "to"), ("op", "path"))
    op, name = body["op"], body["path"]
    if op not in OPS:
        fail(400, "op must be one of " + ", ".join(OPS))
    if op == "rename":
        if not isinstance(body.get("to"), str):
            fail(400, "rename needs `to`")
    elif "to" in body:
        fail(400, f"{op} takes no `to`")
    # The whole classifier, never narrowed: a project's extra folders are its
    # roots too (review of PR #53).
    policy = editor_policy.Policy()
    daemon._active()
    try:
        with _FS_LOCK:
            result = _fs_locked(daemon, project, policy, op, name, body.get("to"))
    except PermissionError as exc:
        fail(403, f"the filesystem refused it ({type(exc).__name__})")
    except OSError as exc:
        if exc.errno in (errno.EEXIST, errno.ENOTEMPTY):
            fail(409, "something is already there")
        if exc.errno == errno.ENOENT:
            fail(404, "nothing is there")
        if exc.errno in (errno.EINVAL, errno.EXDEV, errno.ENOTDIR, errno.EISDIR):
            fail(400, f"that cannot be done here ({errno.errorcode.get(exc.errno, 'error')})")
        fail(500, f"the filesystem said no ({type(exc).__name__})")
    LOG.info("editor fs: %s", op)
    return 200, result


def _fs_locked(daemon, project, policy, op, name, to):
    moves = op in ("rename", "delete")
    raw, resolved, verdict = _judge(daemon, project, policy, name, moves=moves)
    if op in ("new_file", "new_folder"):
        if os.path.lexists(raw):
            fail(409, "something is already there")
        pfd, last = _open_parent(project, name, make=True)
        try:
            if op == "new_folder":
                os.mkdir(last, 0o755, dir_fd=pfd)
                return {"ok": True, "op": op, "path": name, "abs": str(raw.resolve())}
            mode = 0o600 if verdict.kind == editor_policy.ENV else 0o644
            fd = os.open(last, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=pfd)
            try:
                os.fchmod(fd, mode)
            finally:
                os.close(fd)
        finally:
            os.close(pfd)
        made = editor_policy.Policy().classify(raw, raw.resolve())
        return {"ok": True, "op": op, "path": name, "abs": str(raw.resolve()), "kind": made.kind}

    if not os.path.lexists(raw):
        fail(404, "nothing is there")
    is_dir = raw.is_dir() and not raw.is_symlink()
    # A folder named like an env file (a virtualenv's `.env`) is a folder:
    # the env kind is for files only (ED-2's rule).
    env_file = verdict.kind == editor_policy.ENV and not is_dir
    if is_dir:
        why = _holds_protected(daemon, raw, resolved, policy)
        if why:
            fail(403, "refused: " + why)

    if op == "delete":
        from . import trash as _trash
        pfd, last = _open_parent(project, name)
        try:
            judged = os.lstat(raw)
            now = os.stat(last, dir_fd=pfd, follow_symlinks=False)
        finally:
            os.close(pfd)
        if (judged.st_dev, judged.st_ino) != (now.st_dev, now.st_ino):
            fail(409, "it changed while it was being checked: try again")
        bin_ = _trash.Trash(Path(project.root), recycle=daemon.trash.recycle, is_windows=daemon.trash.is_windows)
        try:
            where = bin_.put_keeping_name(Path(os.path.abspath(raw)))
        except _trash.TrashError as exc:
            fail(409, f"not moved to the trash: {exc}")
        return {"ok": True, "op": op, "path": name, "abs": str(resolved), "dir": is_dir,
                "trash": {"where": where.get("where"), "location": where.get("location")}}

    # rename (or move)
    to_raw, to_resolved, to_verdict = _judge(daemon, project, policy, to, moves=True)
    if os.path.lexists(to_raw) and not _same_but_case(raw, to_raw):
        fail(409, f"{to} already exists: a rename never replaces anything")
    if not to_raw.parent.is_dir():
        fail(400, "the folder it would go into is not there")
    if _under(to_resolved, resolved):
        fail(400, "a folder cannot go inside itself")
    if env_file and to_verdict.kind != editor_policy.ENV:
        fail(403, "an env file keeps an env name here: renamed to one Jarvis can read, its "
                  "credentials would be his to read. Rename it in your own shell if you mean that.")
    sfd, slast = _open_parent(project, name)
    try:
        dfd, dlast = _open_parent(project, to)
        try:
            if _same_but_case(raw, to_raw):
                os.rename(slast, dlast, src_dir_fd=sfd, dst_dir_fd=dfd)
            else:
                _rename_noreplace(sfd, slast, dfd, dlast)
        finally:
            os.close(dfd)
    finally:
        os.close(sfd)
    after = editor_policy.Policy().classify(to_raw, to_raw.resolve())
    kind = after.kind if not is_dir or after.kind != editor_policy.ENV else editor_policy.TEXT
    return {"ok": True, "op": op, "path": to, "from": name, "abs": str(to_raw.resolve()),
            "from_abs": str(resolved), "dir": is_dir, "kind": kind}


__all__ = ["list_files", "search", "fs", "search_refused"]
