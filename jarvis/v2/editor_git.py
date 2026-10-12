"""The editor's git (ED-9): HEAD's text for the gutter, and the explorer's colours.

Editor plan §2.8 ("Git gutter, explorer git colours"), decisions E-7/E-8 and
the owner's `.env*` rule. **Both routes are owner-only** (`projects.owner_only`,
checked by `hud_api.route` before anything else, like every editor route):
the HUD listener and the HUD's own page, never the API listener every tool's
HTTP client uses. No tool, MCP tool or fast-path tool reaches this module.

- `GET /projects/{id}/git/base?path=`: the text HEAD holds for a project
  file, for the gutter's diff against the *buffer* (the window diffs; this
  only reads). The path is placed by `hud_api.scope` and judged by
  `editor_policy` **before git runs**: a withheld file, the gate's state and
  every credential env file (`secrets.is_env_credential`) are refused — and
  so is a file whose **name in git** is one of those (a staged rename: `git
  mv .env notes.txt` must not serve `.env`'s HEAD text as `notes.txt`'s). A
  repository whose own config names a program for git to run is refused
  (`gitops._risky_config`, as `gitops` refuses it everywhere). HEAD's blob is
  read by its object id (`cat-file blob`), so no path-based attribute —
  textconv, a filter, an eol conversion — applies; it is capped at
  `BASE_CAP` (the editor's own read cap) before it is read, and a blob that
  is not text answers `binary`: no gutter. A file HEAD does not hold
  (untracked or newly added) answers `new`: no gutter either.
- `GET /projects/{id}/git/status`: `git status --porcelain=v2 -z` at the
  project's root, cached for `STATUS_TTL` (2 s) per root and run once at a
  time per root, at most `STATUS_SECONDS` and `STATUS_CAP` entries. Paths
  are made project-relative, and a path outside the root, under a
  `SKIP_DIRS` folder, withheld (`editor_policy`) or the gate's state is
  dropped before anything is returned.

Git runs only through `gitops._git` — the hardened runner: an absolute system
git, every `GIT_*` variable dropped, hooks, fsmonitor, the pager and auto-gc
off, `GIT_OPTIONAL_LOCKS=0` (a status never rewrites the index) — and a
repository's own config is read for programs first. Not covered, as for
`gitops`: the owner's own global git configuration (a filter driver set
globally, such as git-lfs's, still runs on `git status`).

None of this is a boundary against a program running as the owner.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path, PurePosixPath
import threading
import time

from jarvis.tools.secrets import is_env_credential
from . import editor_policy, hud_api as H

LOG = logging.getLogger(__name__)

# HEAD's text is capped at the editor's own read cap: a file the editor cannot
# open has no gutter to draw.
BASE_CAP = H.FILE_CAP
BASE_SECONDS = 5.0
STATUS_TTL = 2.0
STATUS_SECONDS = 5.0
STATUS_CAP = 20_000
# How much of a blob the not-text test looks at for a NUL (git's own rule).
NUL_PROBE = editor_policy.NUL_PROBE
_CACHE_MAX = 256
REGULAR_MODES = frozenset({"100644", "100755"})

REFUSED_GATE = ("Jarvis's own permission state: its committed text is not served here "
                "(the gutter is off for it).")
REFUSED_WITHHELD = "a credential file: its committed text is never sent to this window."
REFUSED_ENV = "an env file: its committed text is not served here (the gutter is off for it)."


def fail(status, text):
    H.fail(status, text)


def _git(cwd: Path, args, timeout: float):
    from jarvis import gitops
    return gitops._git(cwd, args, timeout=timeout)


def _risky(cwd: Path) -> str:
    from jarvis import gitops
    return gitops._risky_config(cwd)


def _risky_refusal(key: str) -> str:
    return (f"this repository's own git config sets '{key}', which names a program for git to run "
            "or moves where it works: the editor's git colours and gutter are off here.")


def _gate_paths() -> tuple[set[Path], set[Path]]:
    """The gate's state, resolved and as configured (lexical, absolute)."""
    from jarvis.tools.files import _protected_state_paths
    from .permissions import protected_paths
    return protected_paths(), {Path(os.path.abspath(p.expanduser())) for p in _protected_state_paths()}


def refused(policy: editor_policy.Policy, raw: Path, resolved: Path, gate=None) -> str | None:
    """Why HEAD's text for this path is never served, or None.

    The gate's state (resolved or as configured), anything `editor_policy`
    withholds (with no gate exemption: `withheld_anywhere`) and every
    credential env file, as written or where it resolves."""
    resolved_gate, lexical_gate = gate or _gate_paths()
    lexical = Path(os.path.abspath(raw))
    if resolved in resolved_gate or lexical in lexical_gate or lexical in resolved_gate:
        return REFUSED_GATE
    if policy.withheld_anywhere(raw, resolved):
        return REFUSED_WITHHELD
    if is_env_credential(raw) or is_env_credential(resolved):
        return REFUSED_ENV
    return None


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return Path(os.path.abspath(path))


# -- HEAD's text ---------------------------------------------------------------------

def _answer(name: str, state: str, head: str | None = None, **extra) -> dict:
    return {"path": name, "state": state, "head": head, "text": None, **extra}


def _renamed_from(toplevel: Path, rel: str) -> str | None | bool:
    """Where a staged rename (or copy) of `rel` came from in HEAD, None when
    it is not one, False when git could not say (fail closed)."""
    code, out, _ = _git(toplevel, ["diff-index", "--cached", "-M", "-z", "--name-status",
                                   "--no-ext-diff", "--no-textconv", "HEAD", "--"], BASE_SECONDS)
    if code != 0:
        return False
    fields = out.split("\0")
    i = 0
    while i < len(fields):
        status = fields[i]
        if not status:
            i += 1
            continue
        if status[0] in "RC":
            if i + 2 >= len(fields):
                return False
            orig, new = fields[i + 1], fields[i + 2]
            if new == rel:
                return orig
            i += 3
        else:
            i += 2
    return None


def base(project, name: str) -> dict:
    """`GET /projects/{id}/git/base?path=`: what HEAD holds for this file."""
    raw, resolved = H.scope(project, name)
    policy = editor_policy.Policy()
    gate = _gate_paths()
    why = refused(policy, Path(raw), Path(resolved), gate)
    if why:
        fail(403, why)
    if not resolved.is_file():
        fail(404, "file not found")
    code, out, _ = _git(resolved.parent, ["rev-parse", "--show-toplevel"], BASE_SECONDS)
    if code != 0 or not out.strip():
        return _answer(name, "no_repo")
    toplevel = _resolve(Path(out.rstrip("\n")))
    try:
        rel = resolved.relative_to(toplevel).as_posix()
    except ValueError:
        return _answer(name, "no_repo")
    if not rel or ".git" in PurePosixPath(rel).parts:
        return _answer(name, "not_a_file")
    risky = _risky(toplevel)
    if risky:
        fail(403, _risky_refusal(risky))
    code, out, _ = _git(toplevel, ["rev-parse", "-q", "--verify", "HEAD^{commit}"], BASE_SECONDS)
    head = out.strip() if code == 0 else ""
    if not head:
        return _answer(name, "no_head")
    # The name in git: a staged rename serves its source's HEAD text, so the
    # source is judged as the file itself is.
    origin = _renamed_from(toplevel, rel)
    if origin is False:
        return _answer(name, "unavailable", head)
    git_path = rel
    if origin:
        parts = PurePosixPath(origin).parts
        if origin.startswith("/") or ".." in parts or ".git" in parts:
            fail(403, "a rename git reports from outside the repository")
        where = toplevel / origin
        why = refused(policy, where, _resolve(where), gate)
        if why:
            fail(403, why)
        git_path = origin
    code, out, _ = _git(toplevel, ["--literal-pathspecs", "ls-tree", "-l", "-z", "--full-tree",
                                   "HEAD", "--", git_path], BASE_SECONDS)
    if code != 0:
        return _answer(name, "unavailable", head)
    entry = next((e for e in out.split("\0") if e), None)
    if entry is None:
        return _answer(name, "new", head)
    meta, _, listed = entry.partition("\t")
    fields = meta.split()
    if listed != git_path or len(fields) != 4:
        return _answer(name, "new", head)
    mode, kind, oid, size = fields
    if kind != "blob" or mode not in REGULAR_MODES:
        return _answer(name, "not_a_file", head)
    try:
        nbytes = int(size)
    except ValueError:
        return _answer(name, "unavailable", head)
    if nbytes > BASE_CAP:
        return _answer(name, "too_large", head, size=nbytes)
    # By object id: no path, so no attribute (textconv, a filter, eol) applies.
    code, text, _ = _git(toplevel, ["cat-file", "blob", oid], BASE_SECONDS)
    if code != 0:
        return _answer(name, "unavailable", head)
    if "\x00" in text[:NUL_PROBE] or "�" in text:
        return _answer(name, "binary", head, size=nbytes)
    return {"path": name, "state": "ok", "head": head, "text": text, "size": nbytes,
            "renamed": bool(origin)}


# -- the explorer's colours --------------------------------------------------------------

_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}


def _lock_for(key: str) -> threading.Lock:
    with _CACHE_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def clear_cache() -> None:
    with _CACHE_GUARD:
        _CACHE.clear()


def _no_repo() -> dict:
    return {"repo": False, "head": None, "branch": None, "files": {}, "dirs": {}, "truncated": False}


def _xy(xy: str) -> str:
    x, y = (xy + "..")[:2]
    if "D" in (x, y):
        return "deleted"
    if x == "A":
        return "added"
    return "modified"


def status(project) -> dict:
    """`GET /projects/{id}/git/status`: the explorer's colours (see the module note)."""
    raw_root = Path(project.root).expanduser()
    root = _resolve(raw_root)
    if not root.is_dir():
        fail(404, "the project folder is not there")
    from .editor_nav import _policy_for
    policy = _policy_for(root)
    if policy.is_withheld(raw_root, root):
        fail(403, "a credential folder: its contents are never listed")
    key = str(root)
    with _lock_for(key):
        with _CACHE_GUARD:
            hit = _CACHE.get(key)
        if hit and time.monotonic() - hit[0] < STATUS_TTL:
            if isinstance(hit[1], tuple):
                fail(*hit[1])
            return hit[1]  # type: ignore[return-value]
        from .daemon import APIError
        try:
            result: object = _status_now(root, raw_root, policy)
        except APIError as exc:
            result = (exc.status, str(exc))
        with _CACHE_GUARD:
            if len(_CACHE) >= _CACHE_MAX:
                _CACHE.clear()
            _CACHE[key] = (time.monotonic(), result)
    if isinstance(result, tuple):
        fail(*result)
    return result  # type: ignore[return-value]


def _status_now(root: Path, raw_root: Path, policy) -> dict:
    code, out, _ = _git(root, ["rev-parse", "--show-toplevel"], STATUS_SECONDS)
    if code != 0 or not out.strip():
        return _no_repo()
    toplevel = _resolve(Path(out.rstrip("\n")))
    risky = _risky(root)
    if risky:
        fail(403, _risky_refusal(risky))
    # Never into a submodule: its own config is not the one `_risky` read, and
    # a status there would obey it (a filter it names would run).
    code, out, err = _git(root, ["status", "--porcelain=v2", "-z", "--branch", "--untracked-files=normal",
                                 "--ignored=traditional", "--renames", "--ignore-submodules=all", "--", "."],
                          STATUS_SECONDS)
    if code != 0:
        result = _no_repo()
        result.update(repo=True, truncated=True,
                      note="git status took too long" if code == 124 else "git status failed")
        return result
    resolved_gate, lexical_gate = _gate_paths()
    files: dict[str, str] = {}
    dirs: dict[str, str] = {}
    head = branch = None
    truncated = False
    records = out.split("\0")
    i = 0
    while i < len(records):
        rec = records[i]
        i += 1
        if not rec:
            continue
        if rec.startswith("# "):
            if rec.startswith("# branch.oid "):
                value = rec[len("# branch.oid "):]
                head = None if value == "(initial)" else value
            elif rec.startswith("# branch.head "):
                value = rec[len("# branch.head "):]
                branch = None if value == "(detached)" else value[:200]
            continue
        kind = rec[0]
        if kind == "1":
            fields = rec.split(" ", 8)
            if len(fields) != 9:
                continue
            path, state = fields[8], _xy(fields[1])
        elif kind == "2":
            fields = rec.split(" ", 9)
            i += 1  # the rename's source: never returned
            if len(fields) != 10:
                continue
            path, state = fields[9], ("deleted" if "D" in fields[1] else "renamed")
        elif kind == "u":
            fields = rec.split(" ", 10)
            if len(fields) != 11:
                continue
            path, state = fields[10], "conflict"
        elif kind in "?!" and rec[1:2] == " ":
            path, state = rec[2:], ("untracked" if kind == "?" else "ignored")
        else:
            continue
        is_dir = path.endswith("/")
        rel = _project_rel(toplevel, root, path.rstrip("/"))
        if rel is None:
            continue
        if rel and not _shown(policy, root, raw_root, rel, resolved_gate, lexical_gate):
            continue
        if len(files) + len(dirs) >= STATUS_CAP:
            truncated = True
            break
        if is_dir or not rel:
            dirs[rel] = state
        else:
            files[rel] = state
    return {"repo": True, "head": head, "branch": branch, "files": files, "dirs": dirs, "truncated": truncated}


def _project_rel(toplevel: Path, root: Path, path: str) -> str | None:
    """A repository path as the project's tree names it, or None when the
    project's tree does not show it."""
    if not path or path.startswith("/") or "�" in path:
        return None
    parts = PurePosixPath(path).parts
    if ".." in parts:
        return None
    where = toplevel.joinpath(*parts)
    try:
        rel = where.relative_to(root)
    except ValueError:
        # The project root inside an untracked or ignored folder git reported whole.
        try:
            root.relative_to(where)
        except ValueError:
            return None
        return ""
    return rel.as_posix() if rel.parts else ""


def _shown(policy, root: Path, raw_root: Path, rel: str, resolved_gate, lexical_gate) -> bool:
    """Judged before it is returned: never a `SKIP_DIRS` folder, a withheld
    file or folder, or the gate's state (lexically: a status is names only)."""
    parts = PurePosixPath(rel).parts
    if any(p in H.SKIP_DIRS for p in parts):
        return False
    for base in {root, Path(os.path.abspath(raw_root))}:
        where = base.joinpath(*parts)
        if where in resolved_gate or where in lexical_gate:
            return False
        if policy.withheld_anywhere(where, where):
            return False
    return True
