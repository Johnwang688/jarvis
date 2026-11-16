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
    line = tokens
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
    # Paths or settings read from another file are never on the line to be
    # judged below (2026-11-16): `file -f list`, `env -f vars ls`, `env -C
    # ~/.ssh cat id_rsa`. docs/plans/2026-11-16-run-readonly-list-files.md.
    elsewhere = _wrapper_elsewhere(line, tail) or _reads_elsewhere(binary, tokens)
    if elsewhere:
        return (f"Error: {elsewhere}, which this check cannot judge. Use run_command "
                "(it asks), or name the files on the line.")
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


# --- what a line reads from somewhere other than the line (2026-11-16) ---------
#
# `_credential_operand` judges every word as a path. A program that takes the
# paths it opens from another file opens files that are never words: PR #388's
# verifier had `file -f list.txt` print a protected file named only inside
# list.txt. The audit that followed (installed binaries, plan of 2026-11-16)
# found the same class in five more forms, two of them worse: `env -f vars
# ls` loads PATH or LD_PRELOAD from a file, which ran any program here, and
# `env -C ~/.ssh cat id_rsa` moves the folder its relative operands resolve
# in, which printed the key. Options whose file *is* the input (`grep -f`
# patterns, `date -f` dates, `ss -F` filters, `du -X` patterns) are not this
# class: the file named on the line is the whole read, and it is judged.
#
# Long options are matched by any unambiguous prefix (GNU getopt and uutils'
# clap both take one); a prefix that is ambiguous is refused too, which only
# errs toward refusing.

# The wrappers `run_readonly` peels whose own options are not a read: per
# stem, (short options that take a value, long options that take one, the
# refused ones as {letter: (long spelling, why)}). env (uutils 0.10.0): a
# file of variables, another folder, more options spliced in. ionice
# (util-linux): `-p/-P/-u` act on processes already running — `ionice -c 3
# -p PID cat x` set PID's I/O class before failing on `cat` (Sol verifier,
# 2026-11-16).
_WRAPPER_REFUSED = {
    "env": ("uCSaf", ("--unset", "--chdir", "--split-string", "--argv0", "--file"), {
        "f": ("--file", "env -f/--file sets variables (PATH, LD_PRELOAD …) read from a file"),
        "C": ("--chdir", "env -C/--chdir runs the program in another folder, where its "
                         "relative paths resolve differently from where they were judged"),
        "S": ("--split-string", "env -S/--split-string splits its argument into more options"),
    }),
    "ionice": ("cn", ("--class", "--classdata"), {
        "p": ("--pid", "ionice -p/--pid changes processes already running"),
        "P": ("--pgid", "ionice -P/--pgid changes processes already running"),
        "u": ("--uid", "ionice -u/--uid changes processes already running"),
    }),
}

# file 5.46: the short options that take a value.
_FILE_VALUED_SHORT = "efFmP"
_FILE_FILES_FROM = "file -f/--files-from opens the paths listed in a file"


def _long_prefix(name: str, full: str) -> bool:
    """`name` (an option as written, without `=value`) is `full` or a prefix
    of it that getopt or clap would take for it."""
    return len(name) > 2 and full.startswith(name)


def _wrapper_options(stem: str, words: list[str]) -> str | None:
    """Why one wrapper's options (the words after it) are not a read, or None.
    Stops at its program."""
    valued_short, valued_long, refused = _WRAPPER_REFUSED[stem]
    i = 0
    while i < len(words):
        word = words[i]
        i += 1
        if word == "--":
            return None
        # A bare `-` is env's old spelling of `-i`, and an assignment ends
        # nothing: options after either are still scanned.
        if word == "-" or rules.assignment(word) is not None:
            continue
        if word.startswith("--"):
            name = word.split("=", 1)[0]
            for _letter, (full, why) in refused.items():
                if _long_prefix(name, full):
                    return why
            if "=" not in word and any(_long_prefix(name, full) for full in valued_long):
                i += 1
            continue
        if word.startswith("-") and len(word) > 1:
            cluster = word[1:]
            for k, char in enumerate(cluster):
                if char in refused:
                    return refused[char][1]
                if char in valued_short:
                    if k == len(cluster) - 1:
                        i += 1
                    break
            continue
        return None                         # the program
    return None


def _wrapper_elsewhere(line: list[str], tail: int) -> str | None:
    """Why an env or ionice on this line, as a wrapper or as the program (any
    word up to and including the program word), is not a read: variables from
    a file, another folder, spliced options, other processes changed; or None.
    `line` is the command's words as written; `line[tail]` is the program."""
    for i in range(min(tail + 1, len(line))):
        stem = line[i].rsplit("/", 1)[-1]
        if stem in _WRAPPER_REFUSED:
            why = _wrapper_options(stem, line[i + 1:])
            if why:
                return why
    return None


def _unsafe_magic(path: str) -> bool:
    """A magic file libmagic would load that is a symlink, or lies in a
    credential folder. A planted `~/.magic -> ~/.ssh/id_rsa` was read by a
    plain `file x`, its first line quoted in a parse warning (Sol verifier,
    2026-11-16)."""
    import os

    from .files import credential_folder_judge

    return os.path.islink(path) or (os.path.isfile(path) and credential_folder_judge()(path))


def _implicit_magic() -> str | None:
    """Why the magic `file` loads with no `-m` (libmagic 5.46: `$HOME/.magic.mgc`,
    else `$HOME/.magic`, else `$HOME/.magic/magic.mgc`) is not safe to read, or
    None."""
    import os

    home = os.path.expanduser("~")
    for name in (".magic.mgc", ".magic", os.path.join(".magic", "magic.mgc")):
        if _unsafe_magic(os.path.join(home, name)):
            return (f"file loads ~/{name} as magic, and it is a symlink or in a credential "
                    "folder")
    return None


def _magic_value(value: str) -> str | None:
    """Why `file -m VALUE` reads files that are not judged, or None. A colon
    list's later members are not words of their own, and a folder is read
    file by file, symlinks followed (`DIR/link -> ~/.ssh/id_rsa` printed the
    key in a parse warning), so the value must be one existing regular file."""
    import os

    from .. import runtime

    if ":" in value:
        return "file -m/--magic-file with a colon-separated list reads files not on the line"
    place = os.path.expanduser(value)
    if not os.path.isabs(place):
        place = os.path.join(runtime.working_dir() or os.getcwd(), place)
    if not value or not os.path.isfile(place):
        return ("file -m/--magic-file reads every file in a folder (or names no file), "
                "not only what is on the line")
    # libmagic tries the compiled companion first.
    if _unsafe_magic(place) or _unsafe_magic(place + ".mgc"):
        return ("file -m/--magic-file names a symlink or a file in a credential folder "
                "(or its compiled .mgc companion is one)")
    return None


def _reads_elsewhere(binary: str, tokens: list[str]) -> str | None:
    """Why this program, in this form, opens paths written in another file;
    or None. Every word is scanned as a possible option — a value is not
    skipped — so a word that could be either errs toward refusing."""
    words = tokens[1:]
    magic_named = False
    for i, word in enumerate(words):
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if word.startswith("--"):
            name, eq, value = word.partition("=")
            if binary in ("wc", "du") and _long_prefix(name, "--files0-from"):
                return f"{binary} --files0-from opens the paths listed in a file"
            if binary == "file":
                if _long_prefix(name, "--files-from"):
                    return _FILE_FILES_FROM
                if _long_prefix(name, "--magic-file"):
                    magic_named = True
                    why = _magic_value(value if eq else nxt)
                    if why:
                        return why
            # Any program that grows GNU's spelling of it.
            if name in ("--files0-from", "--files-from"):
                return f"{binary} {name} opens the paths listed in a file"
            continue
        if binary == "find" and word == "-files0-from":
            return "find -files0-from searches from the paths listed in a file"
        if binary == "file" and word.startswith("-") and len(word) > 1:
            cluster = word[1:]
            for k, char in enumerate(cluster):
                if char == "f":
                    return _FILE_FILES_FROM
                if char in _FILE_VALUED_SHORT:
                    if char == "m":
                        magic_named = True
                        why = _magic_value(cluster[k + 1:] or nxt)
                        if why:
                            return why
                    break
    if binary == "file" and not magic_named:
        return _implicit_magic()
    return None


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
    from .files import _credential_roots, credential_folder_holds, credential_folder_judge

    judge = credential_folder_judge()
    base = runtime.working_dir() or os.getcwd()
    roots = [str(root) for root in _credential_roots()]
    recursive, _follows = _search_modes(binary, tokens)
    def place(text: str) -> str:
        text = os.path.expanduser(text)
        return text if os.path.isabs(text) else os.path.join(base, text)

    words = []      # (as written, placed path) for an operand or a long option's value
    attached = []   # the same for every tail of a short-option cluster, and
    #                 whether it looks like a path
    for word in tokens[1:]:
        if word.startswith("--"):
            if "=" in word and word.split("=", 1)[1]:
                words.append((word, place(word.split("=", 1)[1])))
            continue
        if word.startswith("-") and len(word) > 1:
            # A value glued to a short option (`-f/abs`, `-nf/abs`) is never
            # a word of its own (review 3 of PR #182: `file -f<key>`, `date
            # -f<key>` and `ss -F<key>` printed it). No per-program table of
            # which letters take a value: every tail of the cluster is judged.
            # A `~` mid-word is literal to the shell, so judging it expanded
            # only errs toward refusing. Bare tails too (2026-11-16, Sol
            # verifier): `date -fkeylink` and `file -mkeylink`, `keylink` a
            # relative symlink to the key, printed it. Only path-like tails
            # take the recursive holds-a-credential-folder check.
            for k in range(2, len(word)):
                tail = word[k:]
                path_like = tail.startswith(("/", "~", ".")) or "/" in tail
                attached.append((word, place(tail), path_like))
            continue
        words.append((word, place(word)))
    if recursive and not any(os.path.exists(path) for _word, path in words):
        # `rg session`, `grep -r x`: no folder named, so the one they search
        # is the one they run in.
        words.append((".", base))
    for word, path, path_like in [(w, p, True) for w, p in words] + attached:
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
            if recursive and path_like:
                # The shared reading (`files.credential_folder_holds`): the
                # stores, case, kernel-order `..` and the Windows profiles
                # too (cred-dir reads, 2026-10-29).
                real = os.path.realpath(candidate)
                if any(root == real or root.startswith(real.rstrip(os.sep) + os.sep)
                       for root in roots) or credential_folder_holds(candidate):
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
