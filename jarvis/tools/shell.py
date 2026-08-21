"""Shell access.

Split in two on purpose. Read-only commands run unattended; anything that can
change the system is a separate `dangerous` tool that the agent loop gates
behind user approval. Narrow, typed tools are what make that gate possible —
one catch-all `run_command` would leave the harness nothing to reason about.
"""

from __future__ import annotations

import shlex
import subprocess
from typing import Annotated

from .. import rules
from . import tool
from .secrets import protected_in_command, refusal

READ_ONLY = {
    "ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "file", "stat",
    "du", "df", "date", "whoami", "hostname", "uname", "pwd", "which", "env",
    "ps", "uptime", "git", "tree", "echo", "ss",
}

# Anything that would let a second command in. `run_readonly` is not gated at
# all — it is `dangerous=False`, so no rule is consulted, no approval is asked
# and no allowlist entry is needed — which means this string is the *entire*
# boundary. It has to be complete.
#
# It was not (found 2026-08-17). A bare **newline** and a bare **`&`** both
# separate commands and neither was listed, so `run_readonly("ls\ntouch PWNED")`
# and `run_readonly("ls & touch PWNED")` ran the second command and returned
# `[exit 0]`: arbitrary execution routed straight around run_command's approval
# gate. `rules.segments()` already knew newlines separate commands — its split
# regex has always included `\n` — while this hand-copied idea of the same
# list did not. `<` joins them because input redirection also glues a filename
# to a binary (`cat<.env`), which is how it slipped past the secrets check.
#
# **The scan is quote-aware, and it borrows `rules.py`'s scanner to be so**
# (2026-08-17, same day, one round later). It was a raw substring test, so it
# refused `grep 'a&b' f.txt` and `git log --grep='fix & bug'` — arguments that
# happen to contain a metacharacter, which the shell will never treat as one.
# That is the same mistake in the same idea that `segments()` had just been
# fixed for, in a second hand-written copy of it; the fix is to stop having a
# second copy. An operator *outside* quotes is refused exactly as before.
_OPERATORS = (">", ">>", "<", "|", ";", "&", "\n", "\r")

# The two the shell expands *inside* double quotes, so a quote is not a reason
# to stop looking for them: `grep "$(cat .env)" f` is still substitution.
_SUBSTITUTIONS = ("$(", "`")

# Read-only git, as an **allowlist**. It used to be a denylist of seven writing
# subcommands, which left `git rm`, `git mv`, `git restore`, `git pull`, `git
# stash`, `git apply`, `git am`, `git cherry-pick`, `git revert`, `git gc` and
# `git config --global` all running unattended through the "read-only" tool —
# verified: `run_readonly("git rm -f f.txt")` deleted the file and returned
# `[exit 0]`. rules.py had already made the opposite (correct) choice for the
# same problem, pairing `git` in _NEVER_AUTO_STEMS with an explicit allowlist.
GIT_READONLY = {
    "status", "log", "diff", "show", "blame", "describe", "shortlog", "grep",
    "rev-parse", "rev-list", "ls-files", "ls-tree", "ls-remote", "cat-file",
    "reflog", "whatchanged", "count-objects", "check-ignore", "merge-base",
    "name-rev", "symbolic-ref", "var", "help", "version", "annotate",
    "diff-tree", "diff-files", "diff-index",
}
# Subcommands whose *listing* form is a read and whose argument form writes:
# `git branch` lists, `git branch -D x` deletes.
#
# The first cut of this rule allowed them only when **every** remaining token
# started with `-`, and that was too blunt (measured 2026-08-17): ten pure
# reads were refused, because a read can carry a value too. `git branch
# --contains HEAD`, `git branch --list 'feat*'`, `git tag -l 'v*'`, `git
# remote get-url origin` and `git config --get user.name` all name something
# and all only look. run_readonly refusing them sends the model to
# `run_command`, which asks the owner to approve a read — the wrong prompt for
# the wrong reason, and the fastest way to train an owner to click yes.
#
# So the subcommands are split by *how* they say "read", and each form is
# named. Anything not named is still refused: this list is the whole boundary
# for an ungated tool, so it stays an allowlist.
GIT_LIST_ONLY = {"branch", "tag"}
GIT_WRITE_FLAGS = {
    "-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy",
    "--edit", "-e", "--unset", "--unset-all", "--add", "--replace-all",
    "--set-upstream", "--set-upstream-to", "-u", "--prune", "-f", "--force",
}

# Subcommands that dispatch on a *sub*-subcommand, with the reading forms named.
# `""` means the bare invocation reads: `git remote` lists remotes, `git notes`
# lists notes. It is deliberately absent for `stash`, whose bare form is `git
# stash push` — the one member of this family where doing nothing writes.
GIT_SUBSUB_READS: dict[str, frozenset[str]] = {
    "remote": frozenset({"", "get-url", "show"}),
    "worktree": frozenset({"", "list"}),
    "submodule": frozenset({"", "status", "summary"}),
    "stash": frozenset({"list", "show"}),
    "bisect": frozenset({"log"}),
    "notes": frozenset({"", "list", "show"}),
}

# Read-only flags that consume the token after them, so the value they name is
# not mistaken for the thing being created. `git branch --contains HEAD` reads;
# `git branch HEAD` would create a branch called HEAD.
GIT_READ_FLAGS_WITH_VALUE = frozenset({
    "--contains", "--no-contains", "--merged", "--no-merged", "--points-at",
    "--sort", "--format", "--color", "--column", "--abbrev", "--get",
    "--get-all", "--get-regexp", "--get-urlmatch", "--type", "--default",
    "--show-origin", "--show-scope", "--file", "--blob",
})
# Flags after which a positional argument is a *pattern* to match, not a name
# to create: `git tag -l 'v*'` lists, `git tag v1` tags. Kept to the two
# spellings that actually mean "list": `-a` is `--all` for `git branch` but
# `--annotate` for `git tag`, so treating it as a listing flag would have made
# `git tag -a v1` — which creates a tag — look like a read.
GIT_LISTING_FLAGS = frozenset({"-l", "--list"})
# `git config` writes by simply being given a name and a value, so nothing but
# an explicit reading flag makes it safe. `git config user.name x` sets it;
# there is no flag to refuse.
GIT_CONFIG_READ_FLAGS = frozenset({
    "--get", "--get-all", "--get-regexp", "--get-urlmatch", "-l", "--list",
    "--name-only", "--show-origin", "--show-scope", "--get-colorbool",
    "--get-color", "--type", "--default",
})
# git's own options that consume the next token, so the subcommand is not
# mistaken for their value (`git -C /repo status` is a read).
_GIT_GLOBAL_WITH_VALUE = {"-c", "-C", "--exec-path", "--git-dir", "--work-tree", "--namespace"}

TIMEOUT_S = 60


def _positionals(rest: list[str]) -> list[str]:
    """The arguments that are not flags, and not the value of a reading flag.

    `--contains HEAD` names a commit to filter *by*; `feature` on its own names
    a branch to create. Telling those apart is the whole of the branch/tag
    rule, so the flags that eat their next token are listed rather than
    guessed at. An attached `--flag=value` consumes nothing.
    """
    out: list[str] = []
    skip = False
    for token in rest:
        if skip:
            skip = False
            continue
        if token.startswith("-"):
            if token in GIT_READ_FLAGS_WITH_VALUE:
                skip = True
            continue
        out.append(token)
    return out


def _git_verdict(tokens: list[str]) -> str | None:
    """None if this git invocation only reads; otherwise the refusal."""
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in _GIT_GLOBAL_WITH_VALUE:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        break
    if index >= len(tokens):
        return None  # bare `git`, or global flags only — prints help
    sub = tokens[index].lower()
    rest = tokens[index + 1 :]
    if sub in GIT_READONLY:
        return None

    if sub in GIT_SUBSUB_READS:
        # These dispatch again: `git remote get-url` reads, `git remote add`
        # writes. Judge the sub-subcommand, and a write flag anywhere still
        # refuses (`git remote show --prune` is not a show).
        subsub = next((t.lower() for t in rest if not t.startswith("-")), "")
        if subsub in GIT_SUBSUB_READS[sub] and not any(
            t in GIT_WRITE_FLAGS for t in rest
        ):
            return None

    elif sub == "config":
        # Nothing to refuse in `git config user.name x` except the absence of a
        # reading flag, so that absence is what refuses it.
        if not any(t in GIT_WRITE_FLAGS for t in rest) and (
            any(t.split("=", 1)[0] in GIT_CONFIG_READ_FLAGS for t in rest)
            or not rest
            or all(t.startswith("-") for t in rest)
        ):
            return None

    elif sub in GIT_LIST_ONLY:
        # `branch`, `tag`: a leftover positional is a name to create unless a
        # listing flag turned positionals into patterns.
        if not any(t in GIT_WRITE_FLAGS for t in rest) and (
            any(t in GIT_LISTING_FLAGS for t in rest) or not _positionals(rest)
        ):
            return None

    return (
        f"Error: 'git {sub}' is not a read-only git subcommand. Use run_command "
        "if this really needs to change the repository."
    )


def _run(command: str) -> str:
    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {TIMEOUT_S}s."

    parts = []
    if proc.stdout.strip():
        parts.append(proc.stdout.strip())
    if proc.stderr.strip():
        parts.append(f"[stderr]\n{proc.stderr.strip()}")
    parts.append(f"[exit {proc.returncode}]")
    output = "\n".join(parts)
    return output[:20_000] if len(output) <= 20_000 else output[:20_000] + "\n[truncated]"


@tool
def run_readonly(
    command: Annotated[str, "A read-only shell command, e.g. 'ls -la ~/projects'"],
) -> str:
    """Run a shell command that only inspects the system and changes nothing.

    Rejects anything outside a known-safe allowlist; use run_command for the rest.
    """
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        return f"Error: could not parse command: {exc}"
    if not tokens:
        return "Error: empty command."

    protected = protected_in_command(command)
    if protected:
        return refusal(protected)

    if rules.first_unquoted(command, _OPERATORS) or rules.first_unquoted(
        command, _SUBSTITUTIONS, double_is_quote=False
    ):
        return "Error: shell operators are not allowed here. Use run_command."

    # Judge the command that actually runs, not the costume it arrived in.
    # `env` is on the allowlist because printing the environment is a read, and
    # that made `env -C /tmp sh -c 'cat .env'` a read-only call: the wrapper was
    # the only thing looked at, and `-c`'s payload is one shlex token that
    # nothing parsed. rules.py has unwrapped `env`/`nohup`/`timeout` since
    # 2026-08-10; this allowlist never got the same treatment.
    tokens = rules.unwrap(tokens)
    if not tokens:
        return "Error: empty command."
    binary = tokens[0].rsplit("/", 1)[-1]
    if binary not in READ_ONLY:
        return (
            f"Error: {binary!r} is not on the read-only allowlist. "
            "Use run_command if this really needs to change the system."
        )
    # A read-only staple in its writing form. `find` is allowlisted because
    # searching is a read, and `find . -exec rm -rf {} \;` is the same binary
    # with the same name on the same list. This had been caught only as a side
    # effect — the raw operator scan tripped on the `;` — so making that scan
    # quote- and escape-aware (correct: the shell does not treat `\;` as a
    # separator either) removed the only thing standing in front of it, while
    # `find / -delete` and `find . -exec rm {} +` carry no separator at all and
    # had never been caught. Judged by name and flag now, where it belongs, and
    # by `rules.py`'s list rather than a second copy of it.
    writing = rules.writes_anyway(tokens)
    if writing:
        return (
            f"Error: '{binary} {writing}' writes to the filesystem, so it is not a "
            "read. Use run_command if this really needs to change things."
        )

    if binary == "git":
        problem = _git_verdict(tokens)
        if problem:
            return problem

    return _run(command)


@tool(dangerous=True)
def run_command(
    command: Annotated[str, "The shell command to run"],
    reason: Annotated[str, "One line on why this is needed, shown to the user"],
) -> str:
    """Run any shell command. Requires the user to approve it first.

    Use for anything that installs, modifies, deletes, or sends.
    """
    # Not overridable by approval. The prompt shows the command, not what it
    # will print, so approving `grep -R key ~/projects` is not consent to put
    # a live credential in the transcript.
    protected = protected_in_command(command)
    if protected:
        return refusal(protected)

    return _run(command)
