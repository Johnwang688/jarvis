"""An isolated repo for one case: nothing in it shows the fix.

Commit 1 is `git archive <base>`. A review case adds commit 2, the PR diff
(`base..head`) plus the seeded patch if any, so `git diff HEAD~1` is the change
under review. An implement case is commit 1 only, with the hidden tests
removed; `restore_hidden` puts them back in a *copy* at grading time.

The directory name is random (no case id, model or run id in a path a model
sees). `shas()` records the base and change commits at build time; everything
after an agent ran (reset, diff, review copy, judge copy) works from those, not
from whatever HEAD the agent left.

An agent had write access to the snapshot's `.git`. Before the harness runs git
there again, `sanitize_git` puts back a known config (no hooks, no filters, no
fsmonitor, no aliases) and refuses a `.git` that is not a plain directory, and
every harness git call also passes `-c core.hooksPath=/dev/null
-c core.fsmonitor=false`.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import config

# The bench's own files (cases hold the answers) never ride into a snapshot.
STRIP = ("rolebench", "docs/plans/2026-10-20-role-bench-plan.md",
         "docs/plans/2026-10-20-role-bench-decisions.md")
SAFE_GIT = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "core.sshCommand=false",
            "-c", "diff.external=", "-c", "protocol.allow=never", "-c", "protocol.file.allow=always")
GIT_CONFIG = """\
[core]
\trepositoryformatversion = 0
\tfilemode = true
\tbare = false
\tlogallrefupdates = true
"""
CHANGE_DIFF = ".rolebench-change.diff"


class SnapshotError(RuntimeError):
    pass


def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in config.SCRUBBED_ENV and not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
               GIT_AUTHOR_NAME="rolebench", GIT_AUTHOR_EMAIL="rolebench@localhost",
               GIT_COMMITTER_NAME="rolebench", GIT_COMMITTER_EMAIL="rolebench@localhost")
    return env


def git(cwd: Path, *args: str, input: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess:
    done = subprocess.run(["git", *SAFE_GIT, "-C", str(cwd), *args], input=input, capture_output=True, env=_env())
    if check and done.returncode:
        raise SnapshotError(f"git {' '.join(args[:3])} failed: {done.stderr.decode(errors='replace')[:300]}")
    return done


def _out(cwd: Path, *args: str) -> str:
    return git(cwd, *args).stdout.decode().strip()


def _apply(snap: Path, patch: bytes) -> None:
    excludes = [f"--exclude={p}{'/*' if '.' not in p.rsplit('/', 1)[-1] else ''}" for p in STRIP]
    done = git(snap, "apply", "--index", "--binary", "--whitespace=nowarn", *excludes, "-", input=patch, check=False)
    if done.returncode:
        raise SnapshotError(f"patch does not apply: {done.stderr.decode(errors='replace')[:300]}")


def new_name() -> str:
    """A neutral, random directory name."""
    return secrets.token_hex(8)


def build(case: dict, run_id: str, repo: Path | None = None, out_root: Path | None = None) -> Path:
    """`run_id` is for the caller's records only: it never names the directory."""
    repo = repo or config.source_repo()
    snap = (out_root or config.snap_root()) / new_name()
    if snap.exists():
        raise SnapshotError(f"{snap} already exists")
    snap.mkdir(parents=True)
    try:
        archive = subprocess.run(["git", *SAFE_GIT, "-C", str(repo), "archive", "--format=tar", case["base"]],
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
        _fixtures(snap, case)
        git(snap, "init", "-q", "-b", "main")
        git(snap, "add", "-A")
        git(snap, "commit", "-q", "-m", "base", "--no-gpg-sign")
        if case["kind"] == "review":
            diff = subprocess.run(["git", *SAFE_GIT, "-C", str(repo), "diff", "--binary", "--no-ext-diff", "--no-textconv",
                                   case["base"], case["head"]], capture_output=True, env=_env())
            if diff.returncode:
                raise SnapshotError(f"git diff failed: {diff.stderr.decode(errors='replace')[:200]}")
            _apply(snap, diff.stdout)
            if case.get("seeded"):
                _apply(snap, case["seeded"]["patch"].encode())
            if git(snap, "diff", "--cached", "--quiet", check=False).returncode == 0:
                raise SnapshotError("the change under review is empty")
            git(snap, "commit", "-q", "-m", "change under review", "--no-gpg-sign")
        git(snap, "gc", "-q", "--prune=now")
        problems = check(snap, case)
        if problems:
            raise SnapshotError("; ".join(problems))
        _link_node_modules(snap, repo)
    except Exception:
        shutil.rmtree(snap, ignore_errors=True)
        raise
    return snap


def shas(snap: Path) -> dict:
    """{"base", "change"} at build time: the root commit and HEAD (equal for implement)."""
    head = _out(snap, "rev-parse", "HEAD")
    roots = _out(snap, "rev-list", "--max-parents=0", "HEAD").split()
    return {"base": roots[-1] if roots else head, "change": head}


def check(snap: Path, case: dict, expect: dict | None = None) -> list[str]:
    """Hygiene problems with a snapshot (empty list = clean). With `expect`
    (the build-time shas), also that HEAD is still the change commit."""
    bad = []
    gitdir = snap / ".git"
    if gitdir.is_symlink() or not gitdir.is_dir():
        return ["snapshot .git is not a plain directory"]
    expected = 2 if case["kind"] == "review" else 1
    if git(snap, "remote", check=False).stdout.strip():
        bad.append("snapshot has a remote")
    if (gitdir / "objects" / "info" / "alternates").exists():
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
    if expect:
        head = git(snap, "rev-parse", "HEAD", check=False).stdout.decode().strip()
        if head != expect["change"]:
            bad.append(f"HEAD moved: {head[:12]} is not the change {expect['change'][:12]}")
    for rel in case.get("hidden_tests", []):
        if (snap / rel).exists():
            bad.append(f"hidden test present: {rel}")
    keep = {f.split("/")[0] for f in case.get("fixtures", []) if f.startswith("rolebench/")}
    for rel in STRIP:
        if (snap / rel).exists() and not (rel in keep and _only_fixtures(snap / rel, case)):
            bad.append(f"bench file present: {rel}")
    return bad


def _only_fixtures(path: Path, case: dict) -> bool:
    """`rolebench/` may exist in a snapshot only to hold the case's fixtures."""
    wanted = {str((Path(snap_rel))) for snap_rel in case.get("fixtures", [])}
    found = {str(p.relative_to(path.parent)) for p in path.rglob("*") if p.is_file()}
    return found <= wanted


def _fixtures(snap: Path, case: dict) -> None:
    """Sanitized inputs the brief refers to. A path under `rolebench/cases/` is
    read from the cases directory; it is copied to the same path in the
    snapshot (the strip above removed everything else under `rolebench/`)."""
    for rel in case.get("fixtures", []):
        prefix = "rolebench/cases/"
        source = (config.cases_dir() / rel[len(prefix):]) if rel.startswith(prefix) else (config.cases_dir() / "fixtures" / rel)
        if not source.is_file():
            raise SnapshotError(f"fixture {rel} not found at {source}")
        (snap / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, snap / rel)


def _lock_hash(hud: Path) -> str | None:
    lock = hud / "package-lock.json"
    try:
        return hashlib.sha256(lock.read_bytes()).hexdigest() if lock.is_file() else None
    except OSError:
        return None


def _lock_marker() -> Path:
    return config.node_modules_dir().parent / "package-lock.sha256"


def _read_marker() -> str | None:
    try:
        return _lock_marker().read_text(encoding="utf-8").strip()
    except OSError:
        return None


def ensure_node_modules(repo: Path | None = None, want_lock: str | None = None) -> Path | None:
    """The bench's own copy of hud/node_modules, made once from the source
    checkout (or the owner's), then only ever mounted read-only. The source's
    package-lock.json hash is recorded beside it; a copy that no longer matches
    the case's lock is rebuilt rather than silently reused stale."""
    dest = config.node_modules_dir()
    if dest.is_dir():
        if want_lock is None or _read_marker() == want_lock:
            return dest
        shutil.rmtree(dest, ignore_errors=True)       # stale against this case's lock
        _lock_marker().unlink(missing_ok=True)
    for base in (repo or config.source_repo(), Path(config.real_home()) / "projects" / "Jarvis"):
        hud = Path(base) / "hud"
        nm = hud / "node_modules"
        if nm.is_dir() and not nm.is_symlink():
            lock = _lock_hash(hud)
            if want_lock is not None and lock is not None and lock != want_lock:
                continue                              # this source's lock is not the case's
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + f".tmp-{os.getpid()}")
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.copytree(nm, tmp, symlinks=True)
            try:
                os.replace(tmp, dest)
            except OSError:
                shutil.rmtree(tmp, ignore_errors=True)    # another process made it first
            if dest.is_dir():
                if lock is not None:
                    _lock_marker().write_text(lock, encoding="utf-8")
                return dest
            return None
    return None


def _point_node_modules(tree: Path, nm: Path) -> None:
    hud = tree / "hud"
    if hud.is_symlink() or not hud.is_dir():
        return
    link = hud / "node_modules"
    if link.is_symlink() or link.is_file():
        link.unlink()
    elif link.is_dir():
        shutil.rmtree(link)
    link.symlink_to(nm)


def _link_node_modules(snap: Path, repo: Path) -> None:
    """TypeScript suites need hud/node_modules: a link to the bench's copy,
    excluded from git so it is never part of a diff or a commit."""
    if not (snap / "hud").is_dir():
        return
    nm = ensure_node_modules(repo, want_lock=_lock_hash(snap / "hud"))
    if nm is None:
        return
    _point_node_modules(snap, nm)
    exclude = snap / ".git" / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    with exclude.open("a", encoding="utf-8") as fh:
        fh.write("\nhud/node_modules\n")


def sanitize_git(snap: Path) -> None:
    """Undo what an agent could have planted in `.git` for the harness's own git
    to run: config (filters, aliases, fsmonitor, hooksPath), hooks, attributes."""
    gitdir = snap / ".git"
    if gitdir.is_symlink() or not gitdir.is_dir():
        raise SnapshotError("snapshot .git is not a plain directory")
    cfg = gitdir / "config"
    if cfg.is_dir() and not cfg.is_symlink():
        shutil.rmtree(cfg)
    else:
        cfg.unlink(missing_ok=True)
    cfg.write_text(GIT_CONFIG, encoding="utf-8")
    hooks = gitdir / "hooks"
    if hooks.is_symlink() or hooks.is_file():
        hooks.unlink()
    elif hooks.is_dir():
        shutil.rmtree(hooks)
    for rel in ("info/attributes", "objects/info/alternates", "commondir"):
        (gitdir / rel).unlink(missing_ok=True)


def _drop_other_refs(snap: Path) -> None:
    for ref in git(snap, "for-each-ref", "--format=%(refname)", check=False).stdout.decode().split():
        if ref != "refs/heads/main":
            git(snap, "update-ref", "-d", ref, check=False)
    git(snap, "symbolic-ref", "HEAD", "refs/heads/main", check=False)


def _prune(snap: Path) -> None:
    git(snap, "reflog", "expire", "--expire=now", "--all", check=False)
    git(snap, "gc", "-q", "--prune=now", check=False)


def reset(snap: Path, change: str) -> None:
    """Undo a reviewer: the tree, the branch and the object store back to the
    change commit; its mutants, commits, stashes and untracked files gone."""
    sanitize_git(snap)
    git(snap, "update-ref", "refs/heads/main", change)
    _drop_other_refs(snap)
    git(snap, "reset", "-q", "--hard", change)
    git(snap, "clean", "-fdxq", "-e", "hud/node_modules")
    _prune(snap)


def fold_commits(snap: Path, base: str) -> int:
    """An implementer's commits become working-tree changes on `base` again, so
    every diff is against the base and nothing hides behind a commit. Returns
    how many commits were folded."""
    sanitize_git(snap)
    head = git(snap, "rev-parse", "HEAD", check=False).stdout.decode().strip()
    folded = 0
    if head and head != base:
        listed = git(snap, "rev-list", "--count", f"{base}..{head}", check=False)
        folded = int(listed.stdout.decode().strip() or 0) if listed.returncode == 0 else 1
    git(snap, "update-ref", "refs/heads/main", base)
    _drop_other_refs(snap)
    git(snap, "reset", "-q", "--soft", base)
    git(snap, "reset", "-q")                      # unstage: the change lives in the working tree
    _prune(snap)
    return folded


def export(snap: Path, change: str, dest: Path, base: str | None = None) -> Path:
    """The tree of `change` with no `.git`: what the judge reads. With `base`,
    the change's diff is written beside it as CHANGE_DIFF."""
    sanitize_git(snap)
    dest.mkdir(parents=True)
    archive = git(snap, "archive", "--format=tar", change)
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, check=True)
    if base:
        diff = git(snap, "diff", "--no-ext-diff", "--no-textconv", base, change)
        (dest / CHANGE_DIFF).write_bytes(diff.stdout)
    return dest


def review_copy(snap: Path, base: str, dest: Path) -> Path:
    """A clean two-commit repo: `base`, then the implementer's working tree as
    the change under review. Built by clone (reachable objects only), so no
    commit the implementer made rides along."""
    sanitize_git(snap)
    done = subprocess.run(["git", *SAFE_GIT, "clone", "-q", "--no-local", "--no-hardlinks", "--branch", "main",
                           str(snap), str(dest)], capture_output=True, env=_env())
    if done.returncode:
        raise SnapshotError(f"review copy: {done.stderr.decode(errors='replace')[:300]}")
    git(dest, "remote", "remove", "origin")
    git(dest, "reset", "-q", "--hard", base)
    for child in dest.iterdir():
        if child.name != ".git":
            shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
    for child in snap.iterdir():
        if child.name == ".git":
            continue
        target = dest / child.name
        if child.is_dir() and not child.is_symlink():
            shutil.copytree(child, target, symlinks=True)
        else:
            shutil.copy2(child, target, follow_symlinks=False)
    exclude = dest / ".git" / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_text("hud/node_modules\n", encoding="utf-8")
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", "change under review", "--allow-empty", "--no-gpg-sign")
    _prune(dest)
    return dest


def _inside(root: Path, path: Path) -> bool:
    root_r = os.path.realpath(root)
    real = os.path.realpath(path)
    return real == root_r or real.startswith(root_r + os.sep)


def safe_write(root: Path, rel: str, data: bytes) -> None:
    """Write root/rel without following a symlink an agent planted on the way."""
    target = root / rel
    probe = root
    for part in Path(rel).parent.parts:
        probe = probe / part
        if probe.is_symlink() or (probe.exists() and not probe.is_dir()):
            probe.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not _inside(root, target.parent):
        raise SnapshotError(f"refusing to write {rel}: it resolves outside the copy")
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)
    target.write_bytes(data)


def _show(repo: Path, sha: str, rel: str) -> bytes | None:
    shown = subprocess.run(["git", *SAFE_GIT, "-C", str(repo), "show", f"{sha}:{rel}"],
                           capture_output=True, env=_env())
    return shown.stdout if shown.returncode == 0 else None


def restore_hidden(snap: Path, case: dict, repo: Path | None = None) -> Path:
    """Copy the snapshot; in the copy, put the hidden tests (from
    the merged commit) and the regression suites (from the base: an
    implementer's edit to a suite never grades itself), and point
    hud/node_modules at the bench's copy. The implementer's own tree is never
    shown the hidden tests."""
    repo = repo or config.source_repo()
    grade = Path(tempfile.mkdtemp(prefix="grade-", dir=snap.parent))
    shutil.rmtree(grade)
    shutil.copytree(snap, grade, symlinks=True)
    try:
        for rel in case["hidden_tests"]:
            data = _show(repo, case["merged"], rel)
            if data is None:
                raise SnapshotError(f"hidden test {rel} not at {case['merged']}")
            safe_write(grade, rel, data)
        for rel in case.get("regression_suites", []):
            data = _show(repo, case["base"], rel)
            if data is not None:
                safe_write(grade, rel, data)
        nm = config.node_modules_dir()
        if nm.is_dir():
            _point_node_modules(grade, nm)
    except Exception:
        shutil.rmtree(grade, ignore_errors=True)
        raise
    return grade
