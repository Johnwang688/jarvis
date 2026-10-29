"""Per-task checkouts. Only Git metadata and .jarvis directories are written.

The base ref a worktree was cut from lives in the task's journal, so status()
takes the Stores it was recorded in; nothing is written into the gitdir.
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


def _prune(root: Path) -> None:
    with _lock:
        root = root.resolve()
        if _is_git(root):
            _git(root, "worktree", "prune", "--expire", "now")


def prune(project: Project) -> None:
    """Forget manually deleted worktrees immediately, retaining their branches."""
    _prune(Path(project.root))


def task_root(task: Task, project: Project | None) -> str | None:
    """The root a task works against: the one it was started under, pinned on
    the task (decisions B5), else its project's current root."""
    return task.root or (project.root if project is not None else None)


def ensure(task: Task, project: Project, stores: Stores) -> Task:
    with _lock:
        stores.tasks.path(task.id)  # Validate opaque ids before using them in paths.
        if task.project_id != project.id:
            raise WorktreeError(f"Task {task.id} does not belong to project {project.id}")
        # Pinned on first use: a project root changed afterwards does not move
        # this task's worktree, its commits, or its removal to another repo.
        task.root = task_root(task, project)
        root = Path(task.root).resolve()
        git = _is_git(root)
        base_ref = None
        event = "worktree_created"
        if git:
            _prune(root)
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
        else:
            path = root / ".jarvis" / "tasks" / task.id
            event = "worktree_adopted" if path.is_dir() else "worktree_created"
            path.mkdir(parents=True, exist_ok=True)
            branch = None
        task.worktree, task.branch = str(path), branch
        stores.tasks.save(task)
        stores.tasks.journal(task.id, event, path=task.worktree, branch=branch, base_ref=base_ref)
        return task


PR_FETCH_TIMEOUT_S = 120.0


def _hardened(root: Path, *args: str, timeout: float = PR_FETCH_TIMEOUT_S) -> str:
    """git with gitops' hardening (no hooks, no fsmonitor, no submodule
    recursion, no `ext::`) and no GIT_* from the daemon's environment."""
    from jarvis import gitops
    code, out, err = gitops._git(root, list(args), timeout=timeout)
    if code:
        raise WorktreeError(f"git {args[0] if args else ''} in {root} failed", stderr=err)
    return out


def ensure_pr(task: Task, project: Project, stores: Stores, *, mirror: Path, pull_ref: str,
              number: int, head: str) -> Task:
    """A PR fix's worktree (PM-18 of `docs/plans/2026-10-28-pr-inbox-merge.md`).

    The bound head is fetched from the daemon's PR mirror (`pr_facts`,
    filled by the caller under the mirror's lock) into the project's
    repository by a local fetch that uses no credentials (no helper, the
    file transport only, gitops' hardening) straight onto a new branch
    `jarvis/<task>-pr<N>`; that branch must then name exactly `head`, or it
    is deleted and nothing is made. A worktree is added on it where
    `ensure` would put one, so the runner adopts it, and `head` is the
    recorded base (status counts the fix's own commits). A repository whose
    own config names a program (`gitops._risky_config`) is refused.
    The task pushes `HEAD:refs/heads/<the PR's branch>` (`pr_fix`'s gate)."""
    with _lock:
        stores.tasks.path(task.id)
        if task.project_id != project.id:
            raise WorktreeError(f"Task {task.id} does not belong to project {project.id}")
        if not (isinstance(head, str) and re.fullmatch(r"[0-9a-f]{40}", head)):
            raise WorktreeError("the bound head is not a commit id")
        if not (isinstance(number, int) and not isinstance(number, bool) and number > 0):
            raise WorktreeError("not a pull request number")
        if task.worktree:
            raise WorktreeError(f"Task {task.id} already has a worktree")
        task.root = task_root(task, project)
        root = Path(task.root).resolve()
        if not _is_git(root):
            raise WorktreeError(f"{root} is not a git repository")
        from jarvis import gitops
        risk = gitops._risky_config(root)
        if risk:
            raise WorktreeError(f"the repository's own git config names a program ({risk}); "
                                "a fix is not run there")
        _prune(root)
        _exclude(root)
        path = root / ".jarvis" / "worktrees" / task.id
        _protect_owner(root, path)
        if path.exists() or path in _registrations(root):
            raise WorktreeError(f"{path} already exists")
        branch = f"jarvis/{task.id}-pr{number}"
        ref = f"refs/heads/{branch}"
        _hardened(root, "-c", "credential.helper=", "-c", "protocol.allow=never",
                  "-c", "protocol.file.allow=always", "fetch", "--no-tags",
                  "--no-write-fetch-head", "--quiet", str(Path(mirror).resolve()),
                  f"{pull_ref}:{ref}")
        got = _hardened(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip()
        if got != head:
            _hardened(root, "update-ref", "-d", ref)
            raise WorktreeError("the fetched head is not the one the owner said Fix to")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _hardened(root, "worktree", "add", str(path), branch)
        except WorktreeError:
            _hardened(root, "update-ref", "-d", ref)
            raise
        task.worktree, task.branch = str(path), branch
        stores.tasks.save(task)
        stores.tasks.journal(task.id, "worktree_created", path=task.worktree, branch=branch,
                             base_ref=head, pr=number)
        return task


def _changes(path: Path) -> str:
    # Ignored files (caches, build output) do not count: a worktree that ran
    # its tests must still be removable without force, or force becomes routine.
    return _git(path, "status", "--porcelain", "--untracked-files=all").strip()


def status(task: Task, stores: Stores) -> WorktreeStatus:
    with _lock:
        path = Path(task.worktree) if task.worktree else None
        if path is None or not path.is_dir():
            return WorktreeStatus(False, task.branch)
        if task.branch is None:
            return WorktreeStatus(True, None, any(path.iterdir()))
        branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD").strip()
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
        project = stores.projects.get(task.project_id) if task.root is None else None
        if task.root is None and project is None:
            raise WorktreeError(f"Task {task.id}: project {task.project_id} not found")
        root = Path(task_root(task, project)).resolve()
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
                    losses.append(f"uncommitted or untracked changes:\n{changes}")
            commits = _unique_commits(root, task.branch)
            if commits:
                losses.append(f"commits not on any other branch:\n{commits}")
            if losses and not force:
                raise WorktreeError(f"{path}: would lose " + "\n".join(losses))
            if path.exists():
                args = ["worktree", "remove"] + (["--force"] if force else [])
                _git(root, *args, str(path))
            else:
                _prune(root)
            branches = _git(root, "for-each-ref", "--format=%(refname)", "refs/heads").splitlines()
            if f"refs/heads/{task.branch}" in branches:
                # Reachability against *all* other branches was checked above;
                # git branch -d only checks upstream/HEAD and is too restrictive.
                _git(root, "branch", "-D", "--", task.branch)
        stores.tasks.journal(task.id, "worktree_removed", path=str(path), branch=task.branch, force=force)
        task.worktree = None
        stores.tasks.save(task)
