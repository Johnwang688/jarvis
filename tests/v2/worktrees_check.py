"""Free lifecycle checks; git repositories and Stores live under /tmp, HOME unchanged.

Run: PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /home/johnw/projects/Jarvis/.venv/bin/python tests/v2/worktrees_check.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from jarvis.v2 import worktrees as w
from jarvis.v2.stores import Stores


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True,
                            text=True, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def fixture(root: Path):
    repo = root / "repo with spaces"
    repo.mkdir(parents=True)
    git(repo, "init", "--template=", "-b", "main")
    git(repo, "config", "user.name", "Worktree Test")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "config", "core.hooksPath", "/dev/null")
    (repo / "tracked.txt").write_text("owner content\n")
    (repo / ".gitignore").write_bytes(b"*.ignored\r\n# preserve bytes\r\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Initial fixture")
    stores = Stores(root / "stores")
    project = stores.projects.create("Fixture", str(repo))
    return repo, stores, project


def refused(call, *contains: str) -> w.WorktreeError:
    try:
        call()
    except w.WorktreeError as exc:
        assert all(text in str(exc) for text in contains), str(exc)
        return exc
    raise AssertionError("expected WorktreeError")


def create_adopt(root: Path) -> None:
    repo, stores, project = fixture(root)
    before = (repo / ".gitignore").read_bytes()
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir()
    exclude.write_bytes(b"# no trailing newline")
    task = stores.tasks.create(project.id, "Build: the WORKTREE!")
    assert w.ensure(task, project, stores) is task
    path = Path(task.worktree)
    assert path == repo / ".jarvis" / "worktrees" / task.id
    assert task.branch == f"jarvis/{task.id}-build-the-worktree"
    snapshot = replace(task)
    assert w.ensure(task, project, stores) == snapshot
    assert w.ensure(task, project, stores) == snapshot
    assert stores.tasks.get(task.id) == task
    entries = stores.tasks.read_journal(task.id)
    assert [e["event"] for e in entries] == ["worktree_created", "worktree_adopted", "worktree_adopted"]
    assert all(e["path"] == task.worktree and e["branch"] == task.branch
               and e["base_ref"] == "refs/heads/main" for e in entries)
    assert exclude.read_bytes() == b"# no trailing newline\n.jarvis/\n"
    assert (repo / ".gitignore").read_bytes() == before
    assert git(repo, "status", "--porcelain") == ""
    # A separately started interpreter must find the journal, without default Stores.
    script = ("import sys; from pathlib import Path; from jarvis.v2.stores import Stores; "
              "from jarvis.v2.worktrees import status; "
              "s=Stores(Path(sys.argv[1])); t=s.tasks.get(sys.argv[2]); "
              "assert status(t).ahead == status(t).behind == 0")
    result = subprocess.run([sys.executable, "-c", script, str(stores.root), task.id],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    print("ok  create/adopt: persisted, idempotent, restart-safe journal, exclude only, owner clean")


def slugs_and_isolation(root: Path) -> None:
    repo, stores, project = fixture(root)
    for brief, slug in (("Ünicode café 東京 -- OK!", "nicode-caf-ok"),
                        ("A" * 39 + "!ignored suffix", "a" * 39),
                        ("", ""), ("東京 ☃ !!!", "")):
        task = w.ensure(stores.tasks.create(project.id, brief), project, stores)
        assert task.branch == f"jarvis/{task.id}-{slug}"
    tasks = [stores.tasks.create(project.id, "Same brief") for _ in range(2)]
    barrier = Barrier(2)

    def start(task):
        barrier.wait(timeout=5)
        return w.ensure(task, project, stores)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(start, tasks))
    assert first.worktree != second.worktree and first.branch != second.branch
    (Path(first.worktree) / "only-first.txt").write_text("isolated")
    (Path(first.worktree) / "tracked.txt").write_text("worker changes")
    assert not (Path(second.worktree) / "only-first.txt").exists()
    assert not (repo / "only-first.txt").exists()
    assert (repo / "tracked.txt").read_text() == "owner content\n"
    assert (Path(second.worktree) / "tracked.txt").read_text() == "owner content\n"
    print("ok  slugs: unicode/long/empty; concurrent tasks have isolated directories and branches")


def status_checks(root: Path) -> None:
    repo, stores, project = fixture(root)
    task = stores.tasks.create(project.id, "Status")
    assert w.status(task) == w.WorktreeStatus(False, None)
    w.ensure(task, project, stores)
    path = Path(task.worktree)
    state = w.status(task)
    assert state.exists and not state.dirty and state.ahead == state.behind == 0
    assert state.last_commit == git(path, "log", "-1", "--format=%h %s")
    (path / "new.txt").write_text("change")
    assert w.status(task).dirty
    git(path, "add", "new.txt")
    assert w.status(task).dirty
    git(path, "commit", "-m", "Task change")
    state = w.status(task)
    assert not state.dirty and state.ahead == 1 and state.behind == 0
    git(repo, "commit", "--allow-empty", "-m", "Owner change")
    assert w.status(task).ahead == w.status(task).behind == 1
    # Adoption after the project switches branches must preserve the original base.
    git(repo, "checkout", "-b", "different-base")
    git(repo, "commit", "--allow-empty", "-m", "Different base change")
    w.ensure(task, project, stores)
    assert w.status(task).behind == 1
    assert stores.tasks.read_journal(task.id)[-1]["base_ref"] == "refs/heads/main"
    print("ok  status: clean, untracked/staged dirty, ahead/behind, last commit, original base retained")


def removal(root: Path) -> None:
    repo, stores, project = fixture(root)
    task = w.ensure(stores.tasks.create(project.id, "Dirty"), project, stores)
    path, branch = Path(task.worktree), task.branch
    (path / "precious.txt").write_text("do not lose")
    refused(lambda: w.remove(task, stores), "uncommitted", "precious.txt")
    assert path.exists() and stores.tasks.get(task.id).worktree == str(path)
    w.remove(task, stores, force=True)
    assert not path.exists() and task.worktree is None and task.branch == branch
    assert not w.status(task).exists
    assert branch not in git(repo, "branch", "--format=%(refname:short)").splitlines()
    assert stores.tasks.read_journal(task.id)[-1]["force"] is True
    assert stores.tasks.get(task.id) == task
    task = w.ensure(stores.tasks.create(project.id, "Unique commit"), project, stores)
    path = Path(task.worktree)
    git(path, "commit", "--allow-empty", "-m", "Precious unique commit")
    commit = git(path, "rev-parse", "--short", "HEAD")
    refused(lambda: w.remove(task, stores), "commits not on any other branch", commit, "Precious unique commit")
    w.remove(task, stores, force=True)
    assert not path.exists()
    # Reachable on another branch, even when not merged to current owner HEAD.
    task = w.ensure(stores.tasks.create(project.id, "Retained elsewhere"), project, stores)
    path = Path(task.worktree)
    git(path, "commit", "--allow-empty", "-m", "Retained commit")
    git(repo, "branch", "keeper", task.branch)
    w.remove(task, stores)
    assert not path.exists() and stores.tasks.read_journal(task.id)[-1]["force"] is False
    task = w.ensure(stores.tasks.create(project.id, "Ignored data"), project, stores)
    (Path(task.worktree) / "precious.ignored").write_text("still owner data")
    refused(lambda: w.remove(task, stores), "precious.ignored")
    w.remove(task, stores, force=True)
    print("ok  remove: dirty files/unique commits named and retained; force and merged cleanup succeed")


def deleted_directory(root: Path) -> None:
    repo, stores, project = fixture(root)
    for unique in (False, True):
        task = w.ensure(stores.tasks.create(project.id, "Deleted by hand"), project, stores)
        path = Path(task.worktree)
        if unique:
            git(path, "commit", "--allow-empty", "-m", "Still precious after deletion")
        shutil.rmtree(path)
        assert not w.status(task).exists
        if unique:
            refused(lambda: w.remove(task, stores), "Still precious after deletion")
        w.remove(task, stores, force=unique)
        assert str(path) not in git(repo, "worktree", "list", "--porcelain")
        assert task.branch not in git(repo, "branch", "--format=%(refname:short)").splitlines()
    stale = w.ensure(stores.tasks.create(project.id, "Stale registration"), project, stores)
    shutil.rmtree(stale.worktree)
    other = w.ensure(stores.tasks.create(project.id, "Prunes first"), project, stores)
    assert stale.worktree not in git(repo, "worktree", "list", "--porcelain")
    assert Path(other.worktree).exists()
    print("ok  manually deleted directories: prune registration, preserve unique commits until force")


def non_git(root: Path) -> None:
    root.mkdir()
    stores = Stores(root / "stores")
    project = stores.projects.create("Plain", str(root))
    task = w.ensure(stores.tasks.create(project.id, "Plain directory"), project, stores)
    path = Path(task.worktree)
    assert path == root / ".jarvis" / "tasks" / task.id and task.branch is None
    assert w.status(task) == w.WorktreeStatus(True, None)
    assert w.ensure(task, project, stores) is task
    assert stores.tasks.read_journal(task.id)[-1]["event"] == "worktree_adopted"
    (path / "keep.txt").write_text("plain work")
    assert w.status(task).dirty
    refused(lambda: w.remove(task, stores), "non-empty", "lose files")
    w.remove(task, stores, force=True)
    assert not path.exists() and task.worktree is None
    task = w.ensure(stores.tasks.create(project.id, "Empty"), project, stores)
    w.remove(task, stores)
    assert task.worktree is None
    w.prune(project)
    print("ok  non-git: create/adopt/status, refuse non-empty, force or empty removal")


def nested_project(root: Path) -> None:
    repo, stores, _ = fixture(root)
    checkout = root / "owner worktree"
    git(repo, "worktree", "add", "-b", "owner-work", str(checkout))
    project = stores.projects.create("Linked", str(checkout))
    before = (checkout / ".gitignore").read_bytes()
    task = w.ensure(stores.tasks.create(project.id, "Nested worktree"), project, stores)
    w.ensure(task, project, stores)
    assert Path(task.worktree).is_relative_to(checkout / ".jarvis")
    assert (repo / ".git" / "info" / "exclude").read_text().splitlines().count(".jarvis/") == 1
    assert (checkout / ".gitignore").read_bytes() == before
    assert git(checkout, "status", "--porcelain") == ""
    assert not w.status(task).dirty
    assert stores.tasks.read_journal(task.id)[0]["base_ref"] == "refs/heads/owner-work"
    w.remove(task, stores)
    assert checkout.exists()
    # Roots inside a checkout also work.
    subdir = repo / "subproject"
    subdir.mkdir()
    project = stores.projects.create("Subdir", str(subdir))
    task = w.ensure(stores.tasks.create(project.id, "Subdirectory root"), project, stores)
    assert Path(task.worktree).is_relative_to(subdir)
    assert not w.status(task).dirty
    w.remove(task, stores)
    # A detached project HEAD uses its immutable commit as the base.
    git(checkout, "checkout", "--detach")
    project = stores.projects.create("Detached", str(checkout))
    task = w.ensure(stores.tasks.create(project.id, "Detached base"), project, stores)
    assert stores.tasks.read_journal(task.id)[0]["base_ref"] == git(checkout, "rev-parse", "HEAD")
    assert w.status(task).ahead == 0
    # Even a project rooted in a linked checkout's subdirectory must never
    # adopt or remove the owner's surrounding checkout.
    nested = checkout / "nested"
    nested.mkdir()
    project = stores.projects.create("Nested owner", str(nested))
    mistaken = stores.tasks.create(project.id, "Wrong path", worktree=str(checkout), branch="owner-work")
    refused(lambda: w.ensure(mistaken, project, stores), "owner's checkout")
    refused(lambda: w.remove(mistaken, stores, force=True), "owner's checkout")
    assert (checkout / "tracked.txt").read_text() == "owner content\n"
    print("ok  linked/subdirectory/detached project roots: common exclude, correct base, owner preserved")


def failures(root: Path) -> None:
    repo, stores, project = fixture(root)
    task = stores.tasks.create(project.id, "Failure")
    with patch.object(w.subprocess, "run", return_value=subprocess.CompletedProcess(
            ["git"], 128, "", "fatal: simulated git failure\n")):
        exc = refused(lambda: w.ensure(task, project, stores), "simulated git failure")
        assert exc.stderr == "fatal: simulated git failure\n"
    with patch.object(w.subprocess, "run", side_effect=FileNotFoundError("git missing")):
        refused(lambda: w.ensure(task, project, stores), "git missing")
    # Real Git error (branch collision) must surface its own stderr.
    branch = f"jarvis/{task.id}-failure"
    git(repo, "branch", branch)
    refused(lambda: w.ensure(task, project, stores), "already exists", branch)
    assert task.worktree is None and not stores.tasks.read_journal(task.id)
    run = subprocess.run

    def checked_run(argv, **kwargs):
        assert isinstance(argv, list) and argv[0] == "git"
        assert set(kwargs) == {"cwd", "capture_output", "text", "check"}
        assert kwargs["capture_output"] is True and kwargs["text"] is True and kwargs["check"] is False
        assert Path(kwargs["cwd"]).is_relative_to(root)
        return run(argv, **kwargs)

    with patch.object(w.subprocess, "run", side_effect=checked_run):
        task = w.ensure(stores.tasks.create(project.id, "Explicit argv"), project, stores)
        w.status(task)
        w.remove(task, stores)
    print("ok  failures: real/mocked stderr, missing git, explicit argv and subprocess flags")


def main() -> int:
    home = os.environ.get("HOME")
    owner_data = Path.home() / ".local" / "share" / "jarvis"

    def guard_owner_data(event, args):
        # Fail even on reads: these checks have no reason to access owner data.
        for value in args:
            if isinstance(value, (str, bytes, os.PathLike)):
                value = os.fsdecode(value)
                if value.startswith(str(owner_data)):
                    raise AssertionError(f"owner data accessed: {event}: {value}")

    sys.addaudithook(guard_owner_data)
    for check in (create_adopt, slugs_and_isolation, status_checks, removal,
                  deleted_directory, non_git, nested_project, failures):
        with tempfile.TemporaryDirectory() as tmp:
            check(Path(tmp) / "fixture")
    assert os.environ.get("HOME") == home
    print("ok  HOME unchanged; owner ~/.local/share/jarvis never accessed")
    print("\nall v2 worktree checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
