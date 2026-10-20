"""An isolated repo for one case: nothing in it shows the fix.

Commit 1 is `git archive <base>`. A review case adds commit 2, the PR diff
(`base..head`) plus the seeded patch if any, so `git diff HEAD~1` is the change
under review. An implement case is commit 1 only, with the hidden tests
removed; `restore_hidden` puts them back in a *copy* at grading time.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import config

# The bench's own files (cases hold the answers) never ride into a snapshot.
STRIP = ("rolebench", "docs/plans/2026-10-20-role-bench-plan.md",
         "docs/plans/2026-10-20-role-bench-decisions.md")


class SnapshotError(RuntimeError):
    pass


def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in config.SCRUBBED_ENV}
    env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
               GIT_AUTHOR_NAME="rolebench", GIT_AUTHOR_EMAIL="rolebench@localhost",
               GIT_COMMITTER_NAME="rolebench", GIT_COMMITTER_EMAIL="rolebench@localhost")
    return env


def git(cwd: Path, *args: str, input: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess:
    done = subprocess.run(["git", "-C", str(cwd), *args], input=input, capture_output=True, env=_env())
    if check and done.returncode:
        raise SnapshotError(f"git {' '.join(args[:3])} failed: {done.stderr.decode(errors='replace')[:300]}")
    return done


def _apply(snap: Path, patch: bytes) -> None:
    excludes = [f"--exclude={p}{'/*' if '.' not in p.rsplit('/', 1)[-1] else ''}" for p in STRIP]
    done = git(snap, "apply", "--index", "--binary", "--whitespace=nowarn", *excludes, "-", input=patch, check=False)
    if done.returncode:
        raise SnapshotError(f"patch does not apply: {done.stderr.decode(errors='replace')[:300]}")


def build(case: dict, run_id: str, repo: Path | None = None, out_root: Path | None = None) -> Path:
    repo = repo or config.source_repo()
    snap = (out_root or config.snap_root()) / f"{case['id']}-{run_id}"
    if snap.exists():
        raise SnapshotError(f"{snap} already exists")
    snap.mkdir(parents=True)
    try:
        archive = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", case["base"]],
                                 capture_output=True, env=_env())
        if archive.returncode:
            raise SnapshotError(f"git archive {case['base']}: {archive.stderr.decode(errors='replace')[:200]}")
        subprocess.run(["tar", "-x", "-C", str(snap)], input=archive.stdout, check=True)
        for rel in STRIP:
            target = snap / rel
            shutil.rmtree(target) if target.is_dir() else target.unlink(missing_ok=True)
        if case["kind"] == "implement":
            for rel in case["hidden_tests"]:
                (snap / rel).unlink(missing_ok=True)
        git(snap, "init", "-q", "-b", "main")
        git(snap, "add", "-A")
        git(snap, "commit", "-q", "-m", "base", "--no-gpg-sign")
        if case["kind"] == "review":
            diff = subprocess.run(["git", "-C", str(repo), "diff", "--binary", "--no-ext-diff", "--no-textconv",
                                   case["base"], case["head"]], capture_output=True, env=_env())
            if diff.returncode:
                raise SnapshotError(f"git diff failed: {diff.stderr.decode(errors='replace')[:200]}")
            _apply(snap, diff.stdout)
            if case.get("seeded"):
                _apply(snap, case["seeded"]["patch"].encode())
            if git(snap, "diff", "--cached", "--quiet", check=False).returncode == 0:
                raise SnapshotError("the change under review is empty")
            git(snap, "commit", "-q", "-m", "change under review", "--no-gpg-sign")
        problems = check(snap, case)
        if problems:
            raise SnapshotError("; ".join(problems))
    except Exception:
        shutil.rmtree(snap, ignore_errors=True)
        raise
    return snap


def check(snap: Path, case: dict) -> list[str]:
    """Hygiene problems with a snapshot (empty list = clean)."""
    bad = []
    expected = 2 if case["kind"] == "review" else 1
    if git(snap, "remote", check=False).stdout.strip():
        bad.append("snapshot has a remote")
    if (snap / ".git" / "objects" / "info" / "alternates").exists():
        bad.append("snapshot borrows objects from another repo")
    refs = git(snap, "for-each-ref", "--format=%(refname)", check=False).stdout.decode().split()
    if refs != ["refs/heads/main"]:
        bad.append(f"unexpected refs: {refs}")
    objs = git(snap, "cat-file", "--batch-all-objects", "--batch-check", check=False).stdout.decode().splitlines()
    commits = [o for o in objs if o.split()[1:2] == ["commit"]]
    if len(commits) != expected:
        bad.append(f"{len(commits)} commit objects, expected {expected}")
    for key in ("head", "merged"):
        sha = case.get(key)
        if sha and git(snap, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0:
            bad.append(f"snapshot contains the {key} commit")
    for rel in case.get("hidden_tests", []):
        if (snap / rel).exists():
            bad.append(f"hidden test present: {rel}")
    for rel in STRIP:
        if (snap / rel).exists():
            bad.append(f"bench file present: {rel}")
    return bad


def restore_hidden(snap: Path, case: dict, repo: Path | None = None) -> Path:
    """Copy the snapshot and put the hidden tests (from the merged commit) in
    the copy. The implementer's own tree is never shown them."""
    repo = repo or config.source_repo()
    grade = Path(tempfile.mkdtemp(prefix="rolebench-grade-", dir=snap.parent))
    shutil.rmtree(grade)
    shutil.copytree(snap, grade, symlinks=True)
    for rel in case["hidden_tests"]:
        shown = subprocess.run(["git", "-C", str(repo), "show", f"{case['merged']}:{rel}"],
                               capture_output=True, env=_env())
        if shown.returncode:
            shutil.rmtree(grade, ignore_errors=True)
            raise SnapshotError(f"hidden test {rel} not at {case['merged']}")
        (grade / rel).parent.mkdir(parents=True, exist_ok=True)
        (grade / rel).write_bytes(shown.stdout)
    return grade
