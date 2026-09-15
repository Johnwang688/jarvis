"""Per-task checkouts. Only Git metadata and .jarvis directories are written.

The private worktree gitdir holds a store locator so status(task) can read the
journal without a Stores parameter, including after a daemon restart.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from .model import Project, Task
from .stores import Stores

_lock = threading.RLock()


class WorktreeError(Exception):
    """Git or a lifecycle safety check failed; stderr is preserved for Git errors."""

    def __init__(self, message: str, *, stderr: str = ""):
        super().__init__(message)
        self.stderr = stderr


@dataclass
class WorktreeStatus:
    exists: bool
    branch: str | None
    dirty: bool = False
    ahead: int = 0
    behind: int = 0
    last_commit: str | None = None


def _git(cwd: Path, *args: str) -> str:
    try:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                                text=True, check=False)
    except OSError as exc:
        raise WorktreeError(f"git in {cwd}: {exc}") from exc
    if result.returncode:
        raise WorktreeError(f"git {' '.join(args)} in {cwd}: {result.stderr.strip()}",
                            stderr=result.stderr)
    return result.stdout


def _is_git(root: Path) -> bool:
    try:
        return _git(root, "rev-parse", "--is-inside-work-tree").strip() == "true"
    except WorktreeError as exc:
        # This is the one expected negative probe; all other failures propagate.
        if exc.stderr.startswith("fatal: not a git repository"):
            return False
        raise


def _gitdir(root: Path, flag: str) -> Path:
    return (root / _git(root, "rev-parse", flag).strip()).resolve()


def _registrations(root: Path) -> dict[Path, str | None]:
    found = {}
    for record in _git(root, "worktree", "list", "--porcelain", "-z").split("\0\0"):
        fields = dict(field.split(" ", 1) for field in record.split("\0") if " " in field)
        if "worktree" in fields:
            ref = fields.get("branch")
            found[Path(fields["worktree"]).resolve()] = ref.removeprefix("refs/heads/") if ref else None
    return found


def _exclude(root: Path) -> None:
    path = _gitdir(root, "--git-common-dir") / "info" / "exclude"
    data = path.read_bytes() if path.exists() else b""
    if b".jarvis/" not in data.splitlines():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as handle:
            handle.write((b"\n" if data and not data.endswith(b"\n") else b"") + b".jarvis/\n")


def _entry(task: Task, stores: Stores) -> dict:
    for entry in reversed(stores.tasks.read_journal(task.id)):
        if entry["event"] in ("worktree_created", "worktree_adopted") and entry["path"] == task.worktree:
            return entry
    return {}


def _base(root: Path) -> str:
    # rev-parse succeeds for both symbolic and detached HEADs.
    ref = _git(root, "rev-parse", "--symbolic-full-name", "HEAD").strip()
    return ref if ref != "HEAD" else _git(root, "rev-parse", "HEAD").strip()


def _protect_owner(root: Path, path: Path) -> None:
    checkout = Path(_git(root, "rev-parse", "--show-toplevel").strip()).resolve()
    if path in (checkout, _gitdir(root, "--git-common-dir").parent):
        raise WorktreeError(f"Refusing to manage owner's checkout: {path}")


def prune(project: Project) -> None:
    """Forget manually deleted worktrees immediately, retaining their branches."""
    with _lock:
        root = Path(project.root).resolve()
        if _is_git(root):
            _git(root, "worktree", "prune", "--expire", "now")


def ensure(task: Task, project: Project, stores: Stores) -> Task:
    with _lock:
        stores.tasks.path(task.id)  # Validate opaque ids before using them in paths.
        if task.project_id != project.id:
            raise WorktreeError(f"Task {task.id} does not belong to project {project.id}")
        root = Path(project.root).resolve()
        git = _is_git(root)
        base_ref = None
        event = "worktree_created"
        if git:
            prune(project)
            _exclude(root)
            registered = _registrations(root)
            path = Path(task.worktree).resolve() if task.worktree else root / ".jarvis" / "worktrees" / task.id
            _protect_owner(root, path)
            if path.is_dir() and path in registered:
                if task.branch is not None and task.branch != registered[path]:
                    raise WorktreeError(f"{path}: branch changed from {task.branch} to {registered[path]}")
                base_ref = _entry(task, stores).get("base_ref") or _base(root)
                event = "worktree_adopted"
                branch = registered[path]
            else:
                slug = re.sub(r"[^a-z0-9]+", "-", task.brief[:40].lower()).strip("-")
                branch = f"jarvis/{task.id}-{slug}"
                base_ref = _base(root)
                path.parent.mkdir(parents=True, exist_ok=True)
                _git(root, "worktree", "add", "-b", branch, str(path), "HEAD")
            locator = _gitdir(path, "--git-dir") / "jarvis-store"
            locator.write_text(str(stores.root.resolve()), encoding="utf-8")
        else:
            path = root / ".jarvis" / "tasks" / task.id
            event = "worktree_adopted" if path.is_dir() else "worktree_created"
            path.mkdir(parents=True, exist_ok=True)
            branch = None
        task.worktree, task.branch = str(path), branch
        stores.tasks.save(task)
        stores.tasks.journal(task.id, event, path=task.worktree, branch=branch, base_ref=base_ref)
        return task


def _changes(path: Path) -> str:
    # Ignored files can also contain work the owner would lose on removal.
    return _git(path, "status", "--porcelain", "--untracked-files=all", "--ignored=matching").strip()


def status(task: Task) -> WorktreeStatus:
    with _lock:
        path = Path(task.worktree) if task.worktree else None
        if path is None or not path.is_dir():
            return WorktreeStatus(False, task.branch)
        if task.branch is None:
            return WorktreeStatus(True, None, any(path.iterdir()))
        branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD").strip()
        locator = _gitdir(path, "--git-dir") / "jarvis-store"
        try:
            stores = Stores(Path(locator.read_text(encoding="utf-8")))
        except OSError as exc:
            raise WorktreeError(f"Cannot locate worktree journal: {locator}: {exc}") from exc
        base_ref = _entry(task, stores).get("base_ref")
        if not base_ref:
            raise WorktreeError(f"Task {task.id}: no base ref recorded in worktree journal")
        behind, ahead = map(int, _git(path, "rev-list", "--left-right", "--count",
                                     f"{base_ref}...HEAD").split())
        return WorktreeStatus(True, branch, bool(_changes(path)), ahead, behind,
                              _git(path, "log", "-1", "--format=%h %s").strip() or None)


def _unique_commits(root: Path, branch: str) -> str:
    ref = f"refs/heads/{branch}"
    others = _git(root, "for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes").splitlines()
    if ref not in others:
        return ""
    return _git(root, "log", "--format=%h %s", ref, "--not",
                *(other for other in others if other != ref)).strip()


def remove(task: Task, stores: Stores, *, force: bool = False) -> None:
    with _lock:
        if task.worktree is None:
            return
        path = Path(task.worktree).resolve()
        project = stores.projects.get(task.project_id)
        if project is None:
            raise WorktreeError(f"Task {task.id}: project {task.project_id} not found")
        root = Path(project.root).resolve()
        if task.branch is None:
            if path != root / ".jarvis" / "tasks" / task.id:
                raise WorktreeError(f"Refusing to remove unexpected task directory: {path}")
            if path.exists():
                if not force and any(path.iterdir()):
                    raise WorktreeError(f"{path}: non-empty directory; would lose files")
                shutil.rmtree(path)
        else:
            _protect_owner(root, path)
            registered = _registrations(root)
            if path.exists() and (path not in registered or registered[path] != task.branch):
                raise WorktreeError(f"{path}: not registered on task branch {task.branch}")
            losses = []
            if path.exists():
                changes = _changes(path)
                if changes:
                    losses.append(f"uncommitted or untracked changes (including ignored files):\n{changes}")
            commits = _unique_commits(root, task.branch)
            if commits:
                losses.append(f"commits not on any other branch:\n{commits}")
            if losses and not force:
                raise WorktreeError(f"{path}: would lose " + "\n".join(losses))
            if path.exists():
                args = ["worktree", "remove"] + (["--force"] if force else [])
                _git(root, *args, str(path))
            else:
                prune(project)
            branches = _git(root, "for-each-ref", "--format=%(refname)", "refs/heads").splitlines()
            if f"refs/heads/{task.branch}" in branches:
                # Reachability against *all* other branches was checked above;
                # git branch -d only checks upstream/HEAD and is too restrictive.
                _git(root, "branch", "-D", "--", task.branch)
        stores.tasks.journal(task.id, "worktree_removed", path=str(path), branch=task.branch, force=force)
        task.worktree = None
        stores.tasks.save(task)
