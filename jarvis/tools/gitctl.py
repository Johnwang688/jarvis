"""Git for agents that cannot ask: three narrow tools over `jarvis/gitops.py`.

None of these is `dangerous=True`, and that is the point of them. A background
workflow's approver denies everything, so a dangerous tool is unreachable
there; what makes these safe to hand over is that every call is judged by rule
before git runs (jarvis/gitops.py), writes are confined to worktrees this
module makes on `jarvis/` branches, and the destructive operations are not
approvable by anyone. The foreground still has `run_command` for anything else.

Lazy imports: gitops reaches the secrets layer and the model client, and
importing it at module load would cycle through the tools package.
"""

from __future__ import annotations

import shlex
from typing import Annotated

from . import tool


@tool
def git(
    path: Annotated[str, "A repository or worktree directory. Writes need a worktree from git_worktree."],
    args: Annotated[list[str], "git arguments, subcommand first, e.g. [\"commit\", \"-m\", \"fix typo\"]"],
) -> str:
    """Run one git command, judged by rule before it runs.

    Reads (status, log, diff, show, blame, grep, branch -a …) work in any
    repository. Anything that changes a repository works only inside a worktree
    made by git_worktree, and anything that moves a branch only while it is on a
    jarvis/ branch: add, rm, mv, commit, switch -c jarvis/x, merge, rebase,
    cherry-pick, fetch, pull, push of that branch to a configured remote.

    Refused, whatever the reason: hard resets, force pushes, deleting branches
    or tags, clean, discarding uncommitted changes (checkout/restore of files),
    stash push/pop/drop, gc/prune, history rewriting, config and remote changes,
    interactive commands, and any option that names a program to run or a file
    to write. A refusal is final: do the work another way or say in your report
    that the owner must. Global options (-C, -c, --git-dir) are not accepted —
    pass the directory as `path`.
    """
    from .. import gitops

    if isinstance(args, str):
        try:
            args = shlex.split(args)
        except ValueError as exc:
            return f"Error: could not read the arguments: {exc}"
    return gitops.run(path, args)


@tool
def git_worktree(
    repo: Annotated[str, "Path of the repository (or one of its worktrees) to work on"],
    branch: Annotated[str, "Name for the new branch. Lives under jarvis/ — 'fix-typo' becomes jarvis/fix-typo"],
    base: Annotated[str, "Commit or branch to start from. Default: the remote's default branch, else HEAD"] = "",
) -> str:
    """Make a private worktree and a new jarvis/ branch to do git work in.

    Returns the worktree path. Edit files under it with the file tools, then use
    the git tool with that path to add and commit. Worktrees live outside the
    repository, so the owner's checkout and its branches are never touched. The
    branch must be new; a worktree that already exists is reported, not reused
    silently. Nothing here removes a worktree — the owner prunes them.
    """
    from .. import gitops

    return gitops.create_worktree(repo, branch, base)


@tool
def git_pull_request(
    worktree: Annotated[str, "A worktree made by git_worktree, on its jarvis/ branch, with the work committed"],
    title: Annotated[str, "Pull request title, one line"],
    body: Annotated[str, "What changed and why, what was tested, and anything the reviewer should look at"],
    base: Annotated[str, "Branch to merge into. Default: the remote's default branch"] = "",
    draft: Annotated[bool, "Open it as a draft"] = False,
) -> str:
    """Push the worktree's branch and open a pull request for someone else to review.

    This is how work reaches main: nothing here merges, and a push of any other
    branch, a force push, or a push to main is refused. Commit first —
    uncommitted changes are not in the pull request. Needs the GitHub CLI (gh)
    to be signed in; if it is not, the branch is pushed and the error says so.
    """
    from .. import gitops

    return gitops.open_pull_request(worktree, title, body, base=base, draft=bool(draft))
