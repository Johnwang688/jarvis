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
RANGES_CAP = 20
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
    under `root`: a folder that neither holds `root` nor sits inside it can
    hold no path the routes look at (every path is inside the root, lexically
    and resolved — `hud_api.scope`). The judgement is still the policy's own
    `is_withheld`; this only skips folders it could never match, so a listing
    of 50,000 files does not compare each against every credential folder."""
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

_META = re.compile(r"([\\.+*?()|\[\]{}^$])")


def _escape(text: str) -> str:
    """A literal for both regex engines (ripgrep's and Python's): only the
    metacharacters they share are escaped — Rust's regex refuses some
    escapes `re.escape` writes."""
    return _META.sub(r"\\\1", text)


def _utf16(text: str, index: int) -> int:
    """A code-point offset as the window counts it (UTF-16 units)."""
    return len(text[:index].encode("utf-16-le")) // 2


def search(project, body) -> dict:
    """`POST /projects/{id}/search` (see the module note)."""
    from .daemon import _object
    body = _object(body, ("query", "regex", "case", "word", "glob", "ignore"), ("query",))
    query = body["query"]
    if not isinstance(query, str) or not query or len(query) > QUERY_CAP or "\x00" in query:
        fail(400, f"query must be 1 to {QUERY_CAP} characters")
    flags = {}
    for key, default in (("regex", False), ("case", False), ("word", False), ("ignore", True)):
        value = body.get(key, default)
        if not isinstance(value, bool):
            fail(400, f"{key} must be true or false")
        flags[key] = value
    glob = body.get("glob", "") or ""
    if not isinstance(glob, str) or len(glob) > 200 or "\x00" in glob or "\n" in glob:
        fail(400, "glob must be a short pattern")
    if getattr(project, "inbox", False):
        fail(400, "find in files is not offered on the Inbox: it is your whole home folder. "
                  "Search a project instead.")
    raw_root = Path(project.root).expanduser()
    root = raw_root.resolve()
    if not root.is_dir():
        fail(404, "the project folder is not there")
    policy = _policy_for(root)
    if policy.is_withheld(raw_root, root):
        fail(403, "a credential folder: it is never searched")

    if flags["regex"]:
        pattern, fixed = query, False
        try:
            py = re.compile(query, 0 if flags["case"] else re.IGNORECASE)
        except re.error as exc:
            fail(400, f"not a regular expression: {exc}")
    else:
        pattern, fixed = query, True
        py = re.compile(re.escape(query), 0 if flags["case"] else re.IGNORECASE)
    if flags["word"]:
        inner = query if flags["regex"] else _escape(query)
        pattern, fixed = rf"\b(?:{inner})\b", False
        py = re.compile(rf"\b(?:{query if flags['regex'] else re.escape(query)})\b",
                        0 if flags["case"] else re.IGNORECASE)

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

    found = search_bounded(pattern, root, glob=glob, case_insensitive=not flags["case"], fixed=fixed,
                           respect_ignore=flags["ignore"], hidden=True, skip=frozenset(H.SKIP_DIRS),
                           limit=SEARCH_CAP, timeout=SEARCH_SECONDS, max_columns=LINE_CAP,
                           keep=lambda hit: where(hit.path) is not None)
    if found.error:
        if "regex" in found.error.lower() or "parse" in found.error.lower():
            fail(400, "not a regular expression ripgrep accepts: " + found.error.split("\n")[-1][:200])
        fail(500, found.error)
    matches = []
    for hit in found.hits:
        rel = where(hit.path)
        if rel is None or hit.line is None:
            continue
        text = hit.text[:LINE_CAP]
        ranges = [[_utf16(text, m.start()), _utf16(text, m.end())]
                  for _, m in zip(range(RANGES_CAP), py.finditer(text)) if m.end() > m.start()]
        matches.append({"path": rel, "line": hit.line, "text": text, "ranges": ranges})
    return {"matches": matches, "files": len({m["path"] for m in matches}),
            "truncated": found.truncated, "timed_out": found.timed_out, "backend": found.backend,
            "limit": SEARCH_CAP}


# -- explorer operations --------------------------------------------------------------

def _check_name(project, name: str) -> None:
    """Names the explorer may write: every component a real name, nothing
    invisible, and on a Windows drive nothing Windows cannot hold
    (`folders`' own rules)."""
    if not isinstance(name, str) or not name or len(name) > 1024 or name.startswith("/"):
        fail(400, "a path relative to the project")
    parts = name.split("/")
    for part in parts:
        if part in ("", "."):
            fail(400, "every part of the path must be a name")  # `..` is scope's 403
        if len(part.encode("utf-8", "replace")) > folders.MAX_COMPONENT:
            fail(400, "a name is too long")
        if folders._control(part) or any(ord(c) < 32 for c in part):
            fail(400, "a name may not hold control or invisible characters")
    if re.match(r"^/mnt/[a-zA-Z](/|$)", str(Path(project.root)) + "/"):
        for part in parts:
            if folders._WINDOWS_RESERVED.match(part) or folders._WINDOWS_RESERVED.match(part.split(".")[0]):
                fail(400, f"`{part}` is a name Windows reserves")
            if set(part) & folders._WINDOWS_BAD_CHARS or part.endswith((".", " ")):
                fail(400, f"`{part}` is not a name Windows can hold")


def _judge(project, policy, name: str):
    """`(raw, resolved, verdict)` for a path the explorer may touch, else a refusal."""
    _check_name(project, name)
    raw, resolved = H.scope(project, name)
    roots = [Path(project.root).expanduser().resolve()] + [Path(p).expanduser().resolve()
                                                            for p in project.extra_dirs]
    if resolved in roots or Path(os.path.abspath(raw)) in roots:
        fail(403, "that is a project folder itself")
    verdict = policy.classify(raw, resolved)
    if verdict.kind in REFUSED_KINDS:
        fail(403, "refused: " + (verdict.reason or "the editor never changes this"))
    if v1_files._hud_build(resolved) or v1_files._git_internal(resolved):
        fail(403, "refused: the editor never changes git's internals or the HUD it is served from")
    return raw, resolved, verdict


def _holds_protected(raw: Path, resolved: Path, policy) -> str | None:
    """Why a folder may not be renamed or deleted, or None.

    It may not hold the gate's state, a credential folder or login file, the
    install root or build the HUD is served from, or Jarvis's checkout's
    safety layer — checked by absolute path, wherever they are below it — nor
    anything the classifier refuses, found by a bounded walk (symlinks not
    followed, `SKIP_DIRS` not entered). Past `FOLDER_WALK_CAP` entries it is
    refused rather than judged in part."""
    try:
        hud = v1_files._hud_roots()
    except (OSError, RuntimeError):
        return "cannot tell what it holds"
    anchors = list(policy.gate) + list(editor_policy.credential_dirs()) + list(policy.login_files) + list(hud)
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
    root = Path(project.root).expanduser().resolve()
    policy = _policy_for(root)
    daemon._active()
    try:
        with _FS_LOCK:
            result = _fs_locked(daemon, project, policy, op, name, body.get("to"))
    except PermissionError as exc:
        fail(403, f"the filesystem refused it ({type(exc).__name__})")
    except OSError as exc:
        if exc.errno in (errno.EEXIST, errno.ENOTEMPTY):
            fail(409, "something is already there")
        if exc.errno in (errno.EINVAL, errno.EXDEV, errno.ENOTDIR, errno.EISDIR):
            fail(400, f"that cannot be done here ({errno.errorcode.get(exc.errno, 'error')})")
        fail(500, f"the filesystem said no ({type(exc).__name__})")
    LOG.info("editor fs: %s", op)
    return 200, result


def _fs_locked(daemon, project, policy, op, name, to):
    raw, resolved, verdict = _judge(project, policy, name)
    if op in ("new_file", "new_folder"):
        if os.path.lexists(raw):
            fail(409, "something is already there")
        parent = raw.parent
        parent.mkdir(parents=True, exist_ok=True)
        # Made, then judged again where it really is (a parent made above
        # could have been swapped for a symlink in between).
        H.scope(project, name)
        if op == "new_folder":
            os.mkdir(raw, 0o755)
            return {"ok": True, "op": op, "path": name, "abs": str(raw.resolve())}
        mode = 0o600 if verdict.kind == editor_policy.ENV else 0o644
        fd = os.open(raw, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        os.close(fd)
        os.chmod(raw, mode)
        made = editor_policy.Policy().classify(raw, raw.resolve())
        return {"ok": True, "op": op, "path": name, "abs": str(raw.resolve()), "kind": made.kind}

    if not os.path.lexists(raw):
        fail(404, "nothing is there")
    is_dir = raw.is_dir() and not raw.is_symlink()
    if is_dir:
        why = _holds_protected(raw, resolved, policy)
        if why:
            fail(403, "refused: " + why)

    if op == "delete":
        from . import trash as _trash
        bin_ = _trash.Trash(Path(project.root), recycle=daemon.trash.recycle, is_windows=daemon.trash.is_windows)
        try:
            where = bin_.put(Path(os.path.abspath(raw)))
        except _trash.TrashError as exc:
            fail(409, f"not moved to the trash: {exc}")
        return {"ok": True, "op": op, "path": name, "abs": str(resolved), "dir": is_dir,
                "trash": {"where": where.get("where"), "location": where.get("location")}}

    # rename (or move)
    to_raw, to_resolved, to_verdict = _judge(project, policy, to)
    if os.path.lexists(to_raw) and not _same_but_case(raw, to_raw):
        fail(409, f"{to} already exists: a rename never replaces anything")
    if not to_raw.parent.is_dir():
        fail(400, "the folder it would go into is not there")
    if _under(to_resolved, resolved):
        fail(400, "a folder cannot go inside itself")
    if verdict.kind == editor_policy.ENV and to_verdict.kind != editor_policy.ENV:
        fail(403, "an env file keeps an env name here: renamed to one Jarvis can read, its "
                  "credentials would be his to read. Rename it in your own shell if you mean that.")
    os.rename(raw, to_raw)
    after = editor_policy.Policy().classify(to_raw, to_raw.resolve())
    return {"ok": True, "op": op, "path": to, "from": name, "abs": str(to_raw.resolve()),
            "from_abs": str(resolved), "dir": is_dir, "kind": after.kind}


__all__ = ["list_files", "search", "fs", "search_refused"]
