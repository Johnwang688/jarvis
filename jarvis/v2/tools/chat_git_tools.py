"""Push and pull-request tools for Codex chats (owner decision CG-0, 2026-10-15).

A Codex chat has no network and no GitHub login. These tools are offered to
Codex owner chats only, foreground only (`session_tools.py`), and run in the
daemon through the jarvis-mcp bridge. The four that publish or change a pull
request are `dangerous`: the owner is asked every time, with no Always, and
merges and closes carry merge_gate's headline. `chat_git_fetch` only reads
the pinned remote into the daemon's mirror. Everything they do is
`chat_git.execute`; see `jarvis/v2/chat_git.py` for how the login stays out
of Codex's reach.

In `tools.EXPLICIT_ONLY`: no surface that takes "the whole registry" holds them.
"""
from __future__ import annotations

from typing import Annotated

from ...tools import EXPLICIT_ONLY, tool
from .. import chat_git

_WORKTREE = ("Absolute path of the git checkout: this chat's own folder, or a worktree of the "
             "same repository under the chat worktrees folder. Its current branch is used.")


@tool(dangerous=True)
def chat_git_push(worktree: Annotated[str, _WORKTREE]) -> str:
    """Push the worktree's current branch to its GitHub origin, with the owner's
    login, after the owner approves (asked every time). Commit first. Refused for a
    detached HEAD, main/master/prod*/release* or the remote's default branch, a
    worktree outside this chat's repository, or an origin that is not github.com or
    differs from the repository's pinned origin. Never forces or deletes."""
    return chat_git.execute("chat_git_push", {"worktree": worktree})


@tool(dangerous=True)
def chat_pr_create(
    worktree: Annotated[str, _WORKTREE],
    title: Annotated[str, "Pull request title, one line."],
    body: Annotated[str, "Pull request description (markdown)."] = "",
    base: Annotated[str, "Branch to merge into; empty for the repository's default."] = "",
    draft: Annotated[bool, "Open it as a draft."] = False,
) -> str:
    """Push the worktree's current branch (as chat_git_push does) and open a pull
    request for it on GitHub, after the owner approves (asked every time). The same
    refusals as chat_git_push apply."""
    return chat_git.execute("chat_pr_create", {"worktree": worktree, "title": title,
                                               "body": body, "base": base, "draft": draft})


@tool(dangerous=True)
def chat_pr_merge(
    worktree: Annotated[str, _WORKTREE],
    number: Annotated[int, "The pull request number."],
    method: Annotated[str, "squash (default), merge or rebase."] = "squash",
) -> str:
    """Merge a pull request on the repository's pinned GitHub origin. The owner
    decides every merge (the card says "merges a pull request")."""
    return chat_git.execute("chat_pr_merge", {"worktree": worktree, "number": number,
                                              "method": method})


@tool(dangerous=True)
def chat_pr_close(
    worktree: Annotated[str, _WORKTREE],
    number: Annotated[int, "The pull request number."],
) -> str:
    """Close a pull request (without merging) on the repository's pinned GitHub
    origin. The owner decides every close."""
    return chat_git.execute("chat_pr_close", {"worktree": worktree, "number": number})


@tool
def chat_git_fetch(worktree: Annotated[str, _WORKTREE]) -> str:
    """Fetch the repository's pinned GitHub origin into Jarvis's local mirror of it,
    with the owner's login. Returns the mirror's path; then run
    `git fetch <mirror> '+refs/remotes/origin/*:refs/remotes/origin/*'` yourself,
    which needs no network. Refused until a first push has pinned the origin."""
    return chat_git.execute("chat_git_fetch", {"worktree": worktree})


EXPLICIT_ONLY.update(chat_git.TOOLS)
