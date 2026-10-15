"""Shell access.

Split in two on purpose. Read-only commands run unattended; anything that can
change the system is a separate `dangerous` tool that the agent loop gates
behind user approval. Narrow, typed tools are what make that gate possible —
one catch-all `run_command` would leave the harness nothing to reason about.
"""

from __future__ import annotations

import re
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

# The only variables `run_readonly` lets a line set before its program: the
# locale and the time zone, with a plain value (no `$`, no quote, no space).
_SAFE_ENV_NAME = re.compile(r"LC_[A-Z]+|LANG|LANGUAGE|TZ")
_SAFE_ENV_VALUE = re.compile(r"[A-Za-z0-9_.:@+,/-]*")

TIMEOUT_S = 60


def _run(command: str) -> str:
    # Started in the run's working folder: the v2 fast path's project
    # (`runtime.working_dir()`, decision F-6 of 2026-10-14). The gate-state
    # check judged the line in that same folder (`protected_state`), so what
    # was judged is what runs. Unbound — every v1 surface — is None, which
    # inherits the process's directory, as before.
    from .. import runtime

    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=TIMEOUT_S,
            cwd=runtime.working_dir(),
            # Nothing to read from: a program that would take stdin (`rg TODO`
            # with no folder named, in a process whose stdin is a pipe) reads
            # an empty input or searches its folder, rather than hanging
            # until the timeout.
            stdin=subprocess.DEVNULL,
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
    if "\x00" in command:
        return "Error: the command holds a NUL byte, which no shell can be handed."
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
    # Parameter expansion (review 2 of PR #182): `cat $HOME/.ssh/id` names a
    # file this check never sees as written, so no `$` bash would expand —
    # bare or inside double quotes — is run here. Structural, not a parser.
    if rules.first_unquoted(command, ("$",), double_is_quote=False):
        return ("Error: `$` expands to text this check cannot see. Single-quote a literal "
                "`$` (e.g. grep 'foo$' file), or use run_command.")

    # Judge the command that actually runs, not the costume it arrived in.
    # `env` is on the allowlist because printing the environment is a read, and
    # that made `env -C /tmp sh -c 'cat .env'` a read-only call: the wrapper was
    # the only thing looked at, and `-c`'s payload is one shlex token that
    # nothing parsed. rules.py has unwrapped `env`/`nohup`/`timeout` since
    # 2026-08-10; this allowlist never got the same treatment.
    #
    # The wrappers' options are read from rules' one table (review of PR #75,
    # HIGH 3): `env -u ls rm -rf build` used to be judged as `ls` and run
    # here — rm and all. A line whose program cannot be told is not run.
    peeled: list[str] = []
    written: list[str] = []
    sure = rules.unwrap_sure(tokens, peeled, written)
    if sure is None:
        return ("Error: which program this runs cannot be told from its wrappers' options. "
                "Use run_command.")
    # A wrapper that writes a file of its own — `time -o FILE` (delta review
    # of PR #75, H-B: `time -o <allowlist> -f '<entries>' ls` ran here) — is
    # not a read, whatever it runs.
    if written:
        return ("Error: a wrapper on this line writes a file it names "
                f"({written[0][:80]}), so it is not a read. Use run_command.")
    # xargs hands its program arguments from a file (`-a list`) this check
    # never sees: `xargs -a list cat` reads whatever `list` names. Here,
    # with no pipe and no input, that is the only thing xargs adds.
    if "xargs" in peeled:
        return ("Error: xargs passes its program arguments this check cannot see. "
                "Use run_command.")
    # An assignment before the program — the shell's (`X=1 ls`) or env's
    # (`env X=1 ls`) — sets what it runs with, and some names make any
    # program load code: `LD_PRELOAD=./x.so ls` ran here, with nobody asked
    # (found in the third delta review of PR #75, older than the PR; also
    # `LD_AUDIT`, `GCONV_PATH`, `PATH` …). Only the locale and time zone may
    # be set; a line whose program is not a plain tail of its words (`env
    # -S`) sets nothing here.
    tail = len(tokens) - len(sure)
    if tail < 0 or tokens[tail:] != sure:
        return ("Error: what this runs, or what it runs with, cannot be told. "
                "Use run_command.")
    for word in tokens[:tail]:
        parts = rules.assignment(word)
        if parts is not None and not (_SAFE_ENV_NAME.fullmatch(parts[0]) and parts[1] is None
                                      and _SAFE_ENV_VALUE.fullmatch(parts[3])):
            return (f"Error: setting {parts[0]} changes what the program runs with, so it is "
                    "not a read. Use run_command.")
    tokens = sure
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
    # What it reads, judged as `read_file` judges it (review of PR #182, H1):
    # the allowlist above says which *program* runs, never which files, so
    # `cat ~/.config/gh/hosts.yml`, a grep of the Browser profile and `cat`
    # of a symlink into ~/.ssh were all a read. Not overridable by approval.
    _recursive, follows = _search_modes(binary, tokens)
    if follows:
        return f"Error: {follows}. Symlinks named on the line are judged; nested ones are not."
    reached = _credential_operand(binary, tokens)
    if reached:
        return (f"Error: {reached} is in a credential folder, or holds one this search would "
                "read (SSH, cloud and Git logins, "
                "Jarvis's own credentials or the owner's Browser profile), which Jarvis does "
                "not read. Ask the owner for what you need instead.")

    return _run(command)


# How many files one operand's glob may name before the line is refused as
# too broad to judge, rather than judged in part.
_GLOB_CAP = 2000

# Short options that take a value (the rest of their cluster, or the next
# word), per searcher: the value is never read as more options.
_VALUED_SHORT = {"grep": "efmABCdD", "rg": "ABCEMTdefgjmrt"}
# grep's long spellings of recursion, and of recursion that follows every
# symlink. GNU getopt takes any unambiguous prefix, so a long option that is
# a prefix of one of these counts as it (review 2 of PR #182).
_GREP_RECURSIVE = ("--recursive", "--dereference-recursive", "--directories")
_GREP_FOLLOWS = "--dereference-recursive"


def _search_modes(binary: str, tokens: list[str]) -> tuple[bool, str | None]:
    """(recursive, refusal) for a searcher's options.

    Recursive: `rg` always; grep with `-r`, `-R`, `-d`/`--directories` naming
    anything but `read` or `skip`, or a long option that is a prefix of
    `--recursive`, `--dereference-recursive` or `--directories`. A recursive
    search reads every file below the folders it names, so the caller judges
    those folders as holders of a credential folder.

    The modes that follow *every* symlink below them (grep `-R` and
    `--dereference-recursive`; rg `-L` and `--follow`) are refused outright: a
    symlink deep in a tree (`link -> ~/.ssh`) is not on the line to be
    judged. Plain `grep -r` and `rg` follow only the symlinks named on the
    line, which are judged.
    """
    if binary not in _VALUED_SHORT:
        return False, None
    follows_msg = (f"a {binary} that follows every symlink below the folders it searches "
                   f"({'-R' if binary == 'grep' else '-L/--follow'}); use "
                   f"{'grep -r' if binary == 'grep' else 'rg'}, which follows only those named")
    recursive = binary == "rg"
    valued = _VALUED_SHORT[binary]
    words = tokens[1:]
    i = 0
    while i < len(words):
        word = words[i]
        i += 1
        if word == "--":
            break
        if word.startswith("--"):
            name, eq, value = word.partition("=")
            if binary == "rg":
                if name == "--follow" or (len(name) > 2 and "--follow".startswith(name)):
                    return recursive, follows_msg
                continue
            if len(name) > 2 and _GREP_FOLLOWS.startswith(name):
                return True, follows_msg
            if len(name) > 2 and any(full.startswith(name) for full in _GREP_RECURSIVE):
                recursive = True
            continue
        if not word.startswith("-") or word == "-":
            continue
        cluster = word[1:]
        for k, char in enumerate(cluster):
            if binary == "grep" and char == "R":
                return True, follows_msg
            if binary == "rg" and char == "L":
                return True, follows_msg
            if binary == "grep" and char == "r":
                recursive = True
            if char in valued:
                value = cluster[k + 1:]
                if not value and i < len(words):
                    value = words[i]
                    i += 1
                if binary == "grep" and char == "d" and not _grep_dir_action_harmless(value):
                    recursive = True
                break
    return recursive, None


def _grep_dir_action_harmless(value: str) -> bool:
    """`-d ACTION` that is surely `read` or `skip` (grep takes an unambiguous
    prefix); anything else, `recurse` in any spelling included, is not."""
    return bool(value) and ("read".startswith(value) and len(value) >= 3
                            or "skip".startswith(value))


def _credential_operand(binary: str, tokens: list[str]) -> str | None:
    """The first operand of a `run_readonly` line that is, or reaches into, a
    credential folder (`config.credential_dirs()`), or None.

    Every word after the program is judged as a path where the shell would
    find it: `~` expanded, a relative word placed in the run's working folder
    (`runtime.working_dir()`, else the process's own), a glob expanded, and a
    symlink followed (`files.credential_folder_judge`, the read tools' own
    judge). Patterns and other non-paths simply judge as nothing. A recursive
    search (`grep -r`, `rg`) of a folder that *holds* a credential folder
    reads it too, so that is refused as well. A glob too broad to judge is
    refused rather than judged in part.
    """
    import glob
    import os

    from .. import runtime
    from .files import _credential_roots, credential_folder_judge

    judge = credential_folder_judge()
    base = runtime.working_dir() or os.getcwd()
    roots = [str(root) for root in _credential_roots()]
    recursive, _follows = _search_modes(binary, tokens)
    words = []
    for word in tokens[1:]:
        if word.startswith("-"):
            if "=" not in word:
                continue
            word = word.split("=", 1)[1]
        if word:
            text = os.path.expanduser(word)
            words.append((word, text if os.path.isabs(text) else os.path.join(base, text)))
    if recursive and not any(os.path.exists(path) for _word, path in words):
        # `rg session`, `grep -r x`: no folder named, so the one they search
        # is the one they run in.
        words.append((".", base))
    for word, path in words:
        if glob.has_magic(path):
            found = []
            for match in glob.iglob(path):
                found.append(match)
                if len(found) > _GLOB_CAP:
                    return word
        else:
            found = [path]
        for candidate in found:
            if judge(candidate):
                return word
            if recursive:
                real = os.path.realpath(candidate)
                if any(root == real or root.startswith(real.rstrip(os.sep) + os.sep)
                       for root in roots):
                    return word
    return None


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

    # The allowlist — and since ENV-1 a write onto a credential env file,
    # symlinks included — is refused here as well as in dispatch()'s verdict,
    # so the refusal holds on a path that never consults one (an approver of
    # None, which dispatch() treats as "run unguarded"). One detector, two
    # callers — see jarvis/protected_state.py.
    gate_write = protected_state.refused(command)
    if gate_write is not None:
        return f"Refused: {protected_state.refusal(gate_write)}."
    # The HUD installer, likewise (decision C-1): never Jarvis's to run.
    installer = protected_state.owner_op(command)
    if installer:
        return f"Refused: {installer}."

    return _run(command)
