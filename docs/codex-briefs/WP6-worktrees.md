# WP6 — a git worktree per task

You are Codex, implementing work package 6 of the Jarvis v2 redesign, in a
git worktree on branch `jarvis/wp6-worktrees`. Claude leads; this brief is
your entire scope. Read first, in this order:

1. `docs/jarvis-v2-design.md` — §7 (workers and worktrees), §4, §14 row WP6.
2. `jarvis/v2/model.py` (fixed; `Task.worktree` / `Task.branch` are the
   fields you fill) and `jarvis/v2/stores.py` (merged WP1; use
   `TaskStore.save` and `TaskStore.journal`, do not modify it).
3. `docs/codex-briefs/WP1-notes.md` for the house conventions Codex already
   established, and `tests/v2/stores_check.py` for the test style.

## Deliverable: `jarvis/v2/worktrees.py`

- `ensure(task: Task, project: Project, stores: Stores) -> Task`. For a git
  project (`project.root` is inside a git work tree): create
  `<root>/.jarvis/worktrees/<task.id>` on a new branch `jarvis/<task.id>-<slug>`
  from the project's current `HEAD`, where `<slug>` is the first 40 chars of
  the brief, lowercased, `[^a-z0-9]+` → `-`, trimmed. If the task already has
  a worktree that exists and is registered with `git worktree list`, adopt it
  and return unchanged. For a non-git project: create
  `<root>/.jarvis/tasks/<task.id>/` and set `branch=None`. Either way set
  `task.worktree` (absolute path) and `task.branch`, save the task, and
  journal `worktree_created` (or `worktree_adopted`) with path and branch.
- **Never edit the owner's tracked files.** Keep `.jarvis/` out of their
  status by appending `.jarvis/` to `<gitdir>/info/exclude` (idempotent),
  never to `.gitignore`. Resolve the git dir with `git rev-parse --git-common-dir`
  so this works when the project root is itself a worktree.
- `status(task) -> WorktreeStatus` dataclass: `exists`, `branch`, `dirty`
  (uncommitted or untracked changes), `ahead`/`behind` relative to the branch
  the worktree was created from (record that base ref in the journal entry
  and read it back), `last_commit` (short hash + subject, or None).
- `remove(task, stores, *, force: bool = False) -> None`. Refuses with a
  `WorktreeError` naming what would be lost if the worktree is dirty or has
  commits not on any other branch, unless `force=True` (that flag is the
  owner's explicit yes, §7). Removes the worktree (`git worktree remove`)
  and, only when force or fully merged, the branch; a non-git task dir is
  removed the same way (refuse if non-empty unless force). Journals
  `worktree_removed` with `force`. Clears `task.worktree`; keeps
  `task.branch` as a record.
- `prune(project)` runs `git worktree prune` so a directory deleted by hand
  does not leave a stale registration; `ensure` calls it before adopting.
- Every git call is `subprocess.run([...], cwd=..., capture_output=True,
  text=True, check=False)` with an explicit argv — no shell, no string
  interpolation into a command. A non-zero exit becomes `WorktreeError`
  carrying stderr. `git` missing → `WorktreeError`.
- Two concurrent `ensure` calls for two tasks on one repo must yield two
  distinct directories and branches; serialize on a module lock.

## Tests: `tests/v2/worktrees_check.py`

Temp git repos built in the test (`git init`, one commit), temp `Stores`
root, `HOME` left alone. Cover: create → adopt idempotent; slug rules
(unicode, long, empty brief); two tasks on one repo isolated (a file written
in one is absent in the other and in the main checkout); `info/exclude` gets
exactly one `.jarvis/` line across repeated calls and `.gitignore` is
byte-identical before and after; status dirty/clean/ahead; remove refuses a
dirty worktree and one with an unmerged commit, naming the loss, and force
removes both; remove after a hand-deleted directory (prune path); non-git
project fallback; project root that is itself a worktree; git failure
surfaces stderr in the error; nothing under the owner's real
`~/.local/share/jarvis` is touched.

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /home/johnw/projects/Jarvis/.venv/bin/python tests/v2/worktrees_check.py`

## Rules

Stay inside `jarvis/v2/worktrees.py`, `tests/v2/`, and
`docs/codex-briefs/WP6-notes.md`. Do not edit `model.py`, `stores.py`,
v1 code or CLAUDE.md; propose changes in the notes instead. No new
dependencies. When green, commit on this branch with a message starting
`v2 WP6: worktree per task` ending with
`Co-Authored-By: Codex <noreply@openai.com>`. Do not push. Finish with
`WP6-notes.md` (under 60 lines): what you built, the test command and its
last lines, workarounds, proposed interface changes.
