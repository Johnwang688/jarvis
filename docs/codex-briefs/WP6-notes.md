# WP6 — a git worktree per task

## Built
- Added `worktrees.ensure`, `status`, `remove`, `prune`, `WorktreeStatus`,
  and `WorktreeError` with Git stderr retained on the exception.
- Git tasks get absolute `.jarvis/worktrees/<id>` paths and isolated branches;
  registered existing worktrees are adopted. Non-git tasks get plain dirs.
- Slugs truncate the brief to 40 characters before lowercasing/replacement;
  empty slugs retain the specified `jarvis/<id>-` branch format.
- Common Git `info/exclude` gets one `.jarvis/` line, preserving other bytes;
  tracked files and `.gitignore` stay untouched. Linked project roots work.
- Journal entries record path, branch and the original base ref; status reads
  that journal for ahead/behind and reports changes plus the latest commit.
- Removal names dirty files and commits unreachable from other local/remote
  branches. Force permits loss; otherwise both are retained. Ignored files
  also count as dirty. Branch names remain on task records after removal.
- Missing directories are pruned immediately; their surviving branches still
  receive the commit-loss check. Owner checkouts cannot be adopted/deleted.
- A module RLock serializes lifecycle operations, including concurrent ensure.

## Validation
Command (exit 0):
```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /home/johnw/projects/Jarvis/.venv/bin/python tests/v2/worktrees_check.py
```
Output's last lines:
```text
ok  linked/subdirectory/detached project roots: common exclude, correct base, owner preserved
ok  failures: real/mocked stderr, missing git, explicit argv and subprocess flags
ok  HOME unchanged; owner ~/.local/share/jarvis never accessed

all v2 worktree checks passed
```
Tests use temporary repositories/stores, keep HOME unchanged, and guard against
owner-data access. They also verify a fresh interpreter can resolve status.
`git diff --check` passed.

## Workarounds / proposed interfaces
- `status(task)` has no Stores argument. Each task's private Git worktree
  metadata contains a `jarvis-store` locator pointing to the explicit store
  root; the base ref itself is read from TaskStore's journal. No default store
  is opened. Consider an optional `stores` parameter in a future interface.
- Detached project HEADs record a commit hash as their comparison base.
- Only the expected "not a git repository" probe error selects the fallback;
  all other Git failures propagate. Missing Git is an error, even for non-git.
- Git's `branch -d` checks only upstream/HEAD. After checking reachability
  against all other branches, removal uses `-D` for the approved branch.
- Changed task branches are refused during adoption/removal. Branch collisions
  are reported, never reset; re-ensuring a manually deleted task may therefore
  require removing its retained branch through `remove` first.
- Locking is process-local. Filesystem/Git changes, task save and journal append
  are not one crash-atomic transaction (same store limitation as WP1).
- Fixed model/stores and design document were not edited; this is the WP6 handoff.
