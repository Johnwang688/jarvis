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

from .. import protected_state, rules
from . import tool
from .secrets import protected_in_command, refusal

# `git` is deliberately not here (2026-10-09). This tool is ungated, so its
# allowlist is the whole boundary, and git is a program whose *flags* run other
# programs and write files: judging the subcommand alone passed every one of
# them, and the tool's holders include background workflows and sub-agents. Git
# is the typed `git` tool's job (jarvis/gitops.py), which judges the whole
# invocation, pins how git is run, and confines writes to worktrees of its own.
READ_ONLY = {
    "ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "file", "stat",
    "du", "df", "date", "whoami", "hostname", "uname", "pwd", "which", "env",
    "ps", "uptime", "tree", "echo", "ss",
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

TIMEOUT_S = 60


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
    # Judged as bash will read it. A backslash-newline is deleted before bash
    # splits anything, so `$\<newline>(` is a command substitution: this tool
    # ran one (2026-10-09), because the substitution scan below looked for
    # `$(` in text that only contained it once joined.
    command = rules.join_continuations(command)
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
    if binary == "git":
        return (
            "Error: git is not run through run_readonly. Use the git tool, e.g. "
            "git(path='/path/to/repo', args=['status']) — it reads any repository, and "
            "commits only in a worktree made by git_worktree."
        )
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
    #
    # That list is `rules.READER_HAZARDS` since 2026-10-09, the one table of
    # every reader's writing and running forms, and the audit that built it
    # found five on this allowlist that nothing here looked at: `tree -o`/`-R`
    # and `ss -D` write files, `date -s` and `hostname NAME` set system state,
    # `file -C` writes a compiled magic file, `ss -K` closes sockets — and
    # `env` with `-S`/`--split-string` glued into one word runs that word as a
    # command line, which this tool executed.
    writing = rules.writes_anyway(tokens)
    if writing:
        return (
            f"Error: {binary} {writing}, so it is not a read. Use run_command if "
            "this really needs to change things."
        )

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

    # The allowlist is refused here as well as in dispatch()'s verdict, so the
    # refusal holds on a path that never consults one (an approver of None,
    # which dispatch() treats as "run unguarded"). One detector, two callers —
    # see jarvis/protected_state.py.
    gate_write = protected_state.refused(command)
    if gate_write is not None:
        return f"Refused: {protected_state.refusal(gate_write)}."

    return _run(command)
