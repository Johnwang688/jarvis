"""The gate's own state files, and whether a shell command writes one.

**One copy of this idea, for v1 and v2.** `jarvis.permissions` (v1's gate),
`tools/shell.py`'s `run_command` and `jarvis.v2.permissions` (layer 1) all ask
this module, and the set of files is the one v1's write tools already refuse
(`tools/files.py:_protected_state`). CLAUDE.md's recurring
lesson is that two copies of one rule drift and the more permissive copy
decides, and that is exactly how this hole existed: v2 had a detector, v1 had
none, and `cp /tmp/x ~/.config/jarvis/allowlist.json` was a plain `cp` — ALLOW
under `rules.decide`, auto-approved on every human-backed v1 surface.

What counts as gate state, resolved through symlinks:

  * `allowlist.json` — what runs without anyone being asked. The approver
    itself reads it on every call, so this is the one file whose write is
    refused outright (`gate_paths`).
  * `models.json`, `provider_defaults.json` — which model he thinks with.
  * `routing.json` — which provider and model v2 work routes to.
  * `discord_guild.json` — where Jarvis may create and move channels.

`command_touch()` answers "does this line write one of them, and how sure are
we". **It is not a boundary**, and the reason is the one `rules.py`'s DENY
section already gives: it is token matching over a shell line, and a shell has
more spellings than any matcher has patterns. A path assembled at run time
(`python -c "open('allow'+'list.json','w')"`, `cp x "$(cat where)"`), a brace
expansion, a script file or Makefile that writes the path itself, a program
told where to write by its own config — none of those is visible here.
(Symlinks and existing hard links *are* seen: paths are resolved and
compared by inode.)
What it buys is that the **obvious** spellings are never auto-approved and the
allowlist's are never put to the owner as a yes/no. The insides of `$(…)`,
backticks and process substitution are analysed as command lines of their
own, and rules.py already makes any substitution ASK. The real boundary is
filesystem permissions — the gate's state owned by someone the agent's process
is not — and that is recorded as the residual gap.

Every path is read from `config` per call, so a suite that repoints
`ALLOWLIST_PATH` and friends at a temp directory protects the temp files.

This module is SELF_PROTECTED (tools/files.py): it decides what never runs.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from . import config, rules


def _resolve(path: str | Path) -> Path:
    try:
        return Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return Path(os.path.abspath(os.path.expanduser(str(path))))


def gate_paths() -> set[Path]:
    """The files a write to which is refused outright, never asked.

    Only the allowlist. It is the one piece of state the approver itself
    consults, so a yes to `cp backup.json allowlist.json` is a yes to entries
    the owner never saw — the `.env` rule: approving a command is not consent
    to what it does.
    """
    return {_resolve(config.ALLOWLIST_PATH)}


def protected_paths() -> set[Path]:
    """Every gate-state file, resolved — the set v1's write tools refuse.

    Not a copy: it *is* `jarvis.tools.files._protected_state()`, so the write
    tools, this shell check and v2's layer 1 can never disagree about which
    files are the gate's. Imported late because `jarvis.tools` imports the
    shell tool, which imports this module.
    """
    from .tools.files import _protected_state

    return _protected_state()


# --- how a command relates to a path ------------------------------------------

# Stems whose ordinary use writes, moves, links, deletes or re-permissions the
# paths they are given. Naming a protected file to one of these is a write.
_WRITERS = frozenset({
    "cp", "mv", "install", "ln", "link", "tee", "truncate", "shred", "rm",
    "unlink", "rmdir", "mkdir", "touch", "chmod", "chown", "chgrp", "rsync",
    "scp", "sftp", "dd", "sponge", "patch", "tar", "unzip", "gzip", "gunzip",
    "bzip2", "xz", "zstd", "curl", "wget", "vi", "vim", "nvim", "nano", "emacs",
    "ed", "ex", "code", "setfacl", "chattr",
})
# Programs that run code. Naming a protected file to one — as a script
# argument, or anywhere in inline source — is treated as a write, because what
# the program does with it is not knowable from here.
_INTERPRETERS = frozenset({
    "python", "python3", "perl", "ruby", "node", "deno", "bun", "php",
    "awk", "gawk", "mawk",
})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "eval"})
# Stems that only read the paths they are given (in the forms `rules.py`
# already recognises — `sed -i` and `find -delete` are caught by
# `rules.writes_anyway` before this list is consulted).
_READERS = frozenset({
    "cat", "head", "tail", "less", "more", "wc", "grep", "egrep", "fgrep", "rg",
    "jq", "ls", "stat", "file", "diff", "cmp", "md5sum", "sha1sum", "sha256sum",
    "sha512sum", "b2sum", "cksum", "du", "realpath", "readlink", "tree", "bat",
    "echo", "printf", "test", "[", "true", "pwd", "which", "dirname", "basename",
    "sort", "cut", "column", "xxd", "od", "hexdump", "strings", "nl", "tac",
    "base64", "sed", "find", "date",
})

# Copiers read their sources and write one destination, so naming a protected
# file as a *source* is a read: `cp ~/.config/jarvis/allowlist.json ~/backup/`
# is a backup, and refusing it would be the over-reach this project keeps
# warning about. Only the two whose option grammar is modelled here get that
# distinction — GNU `cp` and `install`. `rsync` and `scp` have far larger
# grammars (`--log-file=`, `--backup-dir`, `-T DIR` all write) and are already
# never auto-approved, so every operand they are given counts as a write. `mv`
# is not a copier — moving a file away is a write to it — and neither is `ln`,
# because a hard link made now is a write path later.
#
# **The destination has to be parsed, not guessed** (review round 2,
# 2026-10-08). "The last operand" was the whole rule, and all of these really
# write the allowlist in bash while the detector read it as a copy *source*:
# `cp x ~/.config/jarvis/allowlist.json # backup`, `… ${NOTHING}`, `… "$@"`,
# `… {fd}>/dev/null`, `cp -bt ~/.config/jarvis /tmp/allowlist.json` and
# `cp -t$HOME/.config/jarvis …`. So: comments and named-fd redirects are
# stripped, words that expand to nothing are dropped, short-option clusters and
# long-option abbreviations are parsed as getopt would, and — the part that
# matters when the model of the grammar is wrong — **anything ambiguous fails
# closed**: an unresolved expansion, an unparseable line or an option we do
# not know turns every operand into a write candidate.
_COPIER_SHORT_ARG = {"cp": "St", "install": "gmoSt"}     # short options with a value
_COPIER_ALL_WRITE = {"cp": "ls", "install": "d"}         # link / directory modes
_COPIER_LONG = {                                         # name -> takes a value
    "cp": {
        "archive": 0, "attributes-only": 0, "backup": 0, "copy-contents": 0,
        "debug": 0, "dereference": 0, "force": 0, "interactive": 0, "link": 0,
        "no-clobber": 0, "no-dereference": 0, "preserve": 0, "no-preserve": 1,
        "parents": 0, "recursive": 0, "reflink": 0, "remove-destination": 0,
        "sparse": 0, "strip-trailing-slashes": 0, "symbolic-link": 0,
        "suffix": 1, "target-directory": 1, "no-target-directory": 0,
        "update": 0, "verbose": 0, "one-file-system": 0, "context": 0,
        "keep-directory-symlink": 0, "help": 0, "version": 0,
    },
    "install": {
        "backup": 0, "compare": 0, "directory": 0, "group": 1, "mode": 1,
        "owner": 1, "preserve-timestamps": 0, "strip": 0, "strip-program": 1,
        "suffix": 1, "target-directory": 1, "no-target-directory": 0,
        "verbose": 0, "preserve-context": 0, "context": 0, "debug": 0,
        "help": 0, "version": 0,
    },
}
_COPIER_ALL_WRITE_LONG = {"cp": {"link", "symbolic-link"}, "install": {"directory"}}
_COPIERS = frozenset(_COPIER_LONG)
_GLOB = re.compile(r"[*?\[]")

# Substitution analysis is bounded, and the bound fails closed (review round 2).
# The first version re-scanned from just past every opener, so `$(` nested or
# repeated a few thousand times cost cubic time inside the synchronous permit —
# over a minute, during which nothing else could be approved.
_MAX_SPANS = 64      # substitutions examined per command line, in total
_MAX_DEPTH = 3       # nesting of `$(…)` / `sh -c` analysed before giving up


@dataclass(frozen=True)
class Touch:
    """One protected file a command line reaches.

    `certain` means the write provably lands on that file — a redirect onto
    it, `cp` onto it, `ln` to it. Uncertain means the line *might*: a
    directory copy into the gate's folder, an unknown program handed the
    path, a relative name under a working directory nobody can track.
    `opaque` means the line could not be judged at all (too many or too deeply
    nested substitutions); it is uncertain by construction and always asks.
    """
    path: Path
    certain: bool
    why: str
    opaque: bool = False

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class _Budget:
    spans: int = 0


@dataclass
class _State:
    cwd: Path | None                       # None: a `cd` we could not follow
    variables: dict[str, str] = field(default_factory=dict)
    budget: _Budget = field(default_factory=_Budget)


def _opaque(why: str) -> Touch:
    return Touch(_resolve(config.ALLOWLIST_PATH), False, why, opaque=True)


def _expand(token: str, state: _State) -> str | None:
    """The token with `~`, `$HOME` and line-local variables expanded, made
    absolute; None if a variable is left that cannot be known."""
    def local(match: re.Match) -> str:
        name = match.group(1) or match.group(2)
        return state.variables.get(name, match.group(0))
    text = re.sub(r"\$\{(\w+)\}|\$(\w+)", local, token)
    text = os.path.expandvars(text)
    if "$" in text:
        return None
    text = os.path.expanduser(text)
    if not os.path.isabs(text):
        if state.cwd is None:
            return None
        text = str(state.cwd / text)
    return os.path.normpath(text)


_PURE_EXPANSION = re.compile(r"\$(?:\{([@*]|\w+)\}|([@*])|(\w+))")


def _vanishes(token: str, state: _State) -> bool:
    """A word that is nothing but an expansion of nothing: `$@`/`$*` (no
    positional parameters under `bash -c`), a positional `$1`, or a variable
    that is empty or unset on this line and in the environment. bash drops the
    word, so it must not be mistaken for the copy's destination.

    Empty counts as much as unset: after `X=`, `cp a b $X` is `cp a b`, while
    a naive expansion turns `$X` into "" — the working directory — and the
    copy's destination silently becomes `.`."""
    match = _PURE_EXPANSION.fullmatch(token)
    if not match:
        return False
    name = match.group(1) or match.group(2) or match.group(3)
    if name in ("@", "*") or name.isdigit():
        return True
    return not state.variables.get(name, os.environ.get(name))


def _identities(paths: set[Path]) -> dict[tuple[int, int], Path]:
    """(device, inode) of each protected file that exists, so a hard link or a
    symlink under any other name still matches."""
    out = {}
    for path in paths:
        try:
            st = os.stat(path)
        except OSError:
            continue
        out[(st.st_dev, st.st_ino)] = path
    return out


def _exact(text: str, protected: set[Path], ids: dict) -> Path | None:
    if _GLOB.search(text):
        # Component by component: fnmatch's `*` crosses `/`, and `rm ~/*.json`
        # does not reach ~/.config/jarvis/models.json.
        parts = Path(text).parts
        k = next(i for i, part in enumerate(parts) if _GLOB.search(part))
        prefix, rest = _resolve(Path(*parts[:k])), parts[k:]
        for path in protected:
            try:
                rel = path.relative_to(prefix).parts
            except ValueError:
                continue
            if len(rel) == len(rest) and all(fnmatch(a, b) for a, b in zip(rel, rest)):
                return path
        return None
    resolved = _resolve(text)
    if resolved in protected:
        return resolved
    try:
        st = os.stat(text)
    except OSError:
        return None
    return ids.get((st.st_dev, st.st_ino))


def _ancestor_of(text: str, protected: set[Path]) -> list[tuple[Path, tuple[str, ...]]]:
    if _GLOB.search(text):
        return []
    resolved = _resolve(text)
    return [(path, path.relative_to(resolved).parts)
            for path in protected if resolved != path and resolved in path.parents]


def _strip_comment(segment: str) -> str:
    """The segment without a trailing `# comment`. A `#` starts a comment only
    unquoted and at the start of a word — `a#b` and `${#x}` are not comments."""
    for i in rules.unquoted_indices(segment):
        if segment[i] == "#" and (i == 0 or segment[i - 1] in " \t"):
            return segment[:i]
    return segment


def _redirect_targets(segment: str) -> list[str]:
    """Every word the segment opens for output — `rules.redirections()`, the
    one reading of bash's redirect grammar. This module had its own copy
    until 2026-10-09, and it had drifted: after a duplication it skipped one
    character too many (`2>&1>FILE`, `>&2>FILE` never saw FILE), and it read
    `>& FILE` with a space as a redirect to nothing."""
    targets: list[str] = []
    for redirect in rules.redirections(segment):
        if redirect.writes and redirect.word:
            word = rules._tokens(redirect.word)
            if word:
                targets.append(word[0])
    return targets


def _here_strings(segment: str) -> list[str]:
    """The text of every here-string (`<<< text`) in the segment, unquoted:
    what a shell or an interpreter reading standard input will run."""
    out = []
    for redirect in rules.redirections(segment):
        if redirect.op == "<<<" and redirect.word:
            out.append(" ".join(rules._tokens(redirect.word)))
    return out


def _operands(tokens: list[str]) -> list[str]:
    """Arguments that may be paths, for a program whose grammar is not
    modelled: plain words, the value side of `of=…` / `--output=…`, and the
    path glued onto a short option (`mv -t$HOME/.config/jarvis …`,
    `-o/tmp/x`). Over-approximating is the safe direction — a candidate that
    names no protected file costs nothing."""
    out = []
    for token in tokens:
        if token.startswith("--"):
            if "=" in token:
                out.append(token.split("=", 1)[1])
            continue
        if token.startswith("-") and len(token) > 2:
            glued = re.search(r"[/~$.]", token[2:])
            if glued:
                out.append(token[2 + glued.start():])
            continue
        if token.startswith("-"):
            continue
        if re.match(r"[A-Za-z_]\w*=", token):
            out.append(token.split("=", 1)[1])
            continue
        out.append(token)
    return out


def _copier_split(stem: str, args: list[str]) -> tuple[list[str], list[str], bool] | None:
    """(written, read, fully understood) for `cp`/`install`; None otherwise.

    Parsed the way GNU getopt reads it: short-option clusters (`-bt DIR`),
    glued values (`-tDIR`, `-t$HOME/…`), long options and their unique
    abbreviations (`--target=DIR`, `--targ DIR`), and `--`. Option values are
    write candidates (`--suffix`, `-m`: never a protected path in ordinary
    use, so counting them costs nothing). A link or directory mode writes
    every operand. An option this table does not know means the parse is not
    trusted, so the caller fails closed.
    """
    if stem not in _COPIERS:
        return None
    shorts, longs = _COPIER_SHORT_ARG[stem], _COPIER_LONG[stem]
    all_write, all_write_long = _COPIER_ALL_WRITE[stem], _COPIER_ALL_WRITE_LONG[stem]
    operands: list[str] = []
    values: list[str] = []
    target: str | None = None
    everything = False
    understood = True
    ended = False
    i = 0
    while i < len(args):
        token = args[i]
        i += 1
        if ended or token == "-" or not token.startswith("-"):
            operands.append(token)
            continue
        if token == "--":
            ended = True
            continue
        if token.startswith("--"):
            name, has_value, value = token[2:].partition("=")
            matches = [o for o in longs if o == name] or [o for o in longs if o.startswith(name)]
            if len(matches) != 1:
                understood = False
                if has_value:
                    values.append(value)
                continue
            option = matches[0]
            everything = everything or option in all_write_long
            if longs[option]:
                if not has_value and i < len(args):
                    value, i = args[i], i + 1
                if option == "target-directory":
                    target = value
                else:
                    values.append(value)
            elif has_value:
                values.append(value)            # `--backup=numbered` and friends
            continue
        for k, letter in enumerate(token[1:], 1):
            everything = everything or letter in all_write
            if letter in shorts:
                value = token[k + 1:]
                if not value and i < len(args):
                    value, i = args[i], i + 1
                if letter == "t":
                    target = value
                else:
                    values.append(value)
                break
    if everything:
        written = operands + values + ([target] if target is not None else [])
        return written, [], understood
    if target is not None:
        return [target] + values, operands, understood
    if not operands:
        return values, [], understood
    return [operands[-1]] + values, operands[:-1], understood


@dataclass
class _Seen:
    touches: list[Touch] = field(default_factory=list)
    writes: bool = False                           # runs a writing stem
    read: set[Path] = field(default_factory=set)   # protected files only read


def _env_split(rest: list[str], k: int) -> tuple[str, int] | None:
    """If `rest[k]` is env's `-S`/`--split-string`, (its value, the index
    after it); else None. Every spelling getopt and clap accept: `-S V`,
    `-SV`, inside a cluster (`-iS V`), `--split-string=V`, and any prefix of
    the long name (`--split V`)."""
    token = rest[k]
    if token.startswith("--"):
        name, eq, value = token[2:].partition("=")
        if not name or not "split-string".startswith(name):
            return None
        if eq:
            return value, k + 1
        return (rest[k + 1], k + 2) if k + 1 < len(rest) else ("", k + 1)
    if not token.startswith("-"):
        return None
    letters = token[1:]
    for idx, letter in enumerate(letters):
        if letter == "S":
            value = letters[idx + 1:]
            if value:
                return value, k + 1
            return (rest[k + 1], k + 2) if k + 1 < len(rest) else ("", k + 1)
        if letter in "Cuaf":        # the rest of the cluster is that option's value
            return None
    return None


def _peel(tokens: list[str], state: _State) -> tuple[list[str], Path | None]:
    """Strip subshell punctuation and wrappers; return the real command and
    any directory `env -C` would run it in.

    `env -S STRING` splits STRING into words and reads them as more of its own
    arguments — a whole command line in one word (2026-10-09). It used to be
    taken for the command's *name*, a word ending in whatever the path's last
    component was, so the line behind it was never looked at. `builtin` is
    bash's way of running a builtin by name (`builtin eval …`) and is peeled
    the same way.
    """
    tokens = list(tokens)
    while tokens and tokens[0] in ("(", "{", "!"):
        tokens = tokens[1:]
    if tokens and tokens[0].startswith("(") and len(tokens[0]) > 1:
        tokens[0] = tokens[0][1:]
    chdir = None
    for _ in range(8):
        if tokens and tokens[0] == "builtin":
            tokens = tokens[1:]
            continue
        if not tokens or rules._basename(tokens[0]) != "env":
            break
        rest, k = tokens[1:], 0
        split = None
        while k < len(rest) and (rest[k].startswith("-") or "=" in rest[k]):
            found = _env_split(rest, k)
            if found is not None:
                split, k = found
                break
            if rest[k] in ("-C", "--chdir") and k + 1 < len(rest):
                target = _expand(rest[k + 1], state)
                chdir = Path(target) if target else None
                k += 2
                continue
            if rest[k].startswith("--chdir="):
                target = _expand(rest[k].split("=", 1)[1], state)
                chdir = Path(target) if target else None
            k += 1
        if split is None:
            tokens = rest[k:]
            break
        try:
            words = shlex.split(split)
        except ValueError:
            words = split.split()
        tokens = ["env"] + words + rest[k:]
    return rules.unwrap(tokens), chdir


_SHELL_VALUE_LONGS = frozenset({"rcfile", "init-file"})


def _shell_strings(tokens: list[str]) -> tuple[list[str], bool]:
    """(the command strings a shell runs, whether it reads them from stdin).

    **The inline flag is found in a cluster too** (finding 1, 2026-10-09).
    The check was `"-c" in tokens`, so `bash -lc`, `sh -ec`, `bash -xc`,
    `bash -cx` and `zsh -fc` hid their string from this module entirely,
    and `bash -c -- STRING` analysed the `--`. Read the way the shells read
    it: option clusters starting `-` or `+`, a `c` in a `-` cluster making
    the first operand a command string; `-o`/`-O`/`+o`/`+O` and `--rcfile`/
    `--init-file` take the next word; `--` or a lone `-` ends the options.

    With `-c`, **every** operand is analysed, not just the first — the rest
    are `$0`, `$1`…, and checking text that will not run can only add a
    refusal. Without it, an operand is a script file (not visible here); no
    operand, or `-s`, means the commands come from standard input.
    """
    if not tokens:
        return [], False
    if rules._basename(tokens[0]) == "eval":
        return ([" ".join(tokens[1:])] if len(tokens) > 1 else []), False
    inline = stdin = False
    operands: list[str] = []
    i = 1
    while i < len(tokens):
        token = tokens[i]
        i += 1
        if token in ("--", "-"):
            operands = tokens[i:]
            break
        if token.startswith("--"):
            if token[2:] in _SHELL_VALUE_LONGS:
                i += 1
            continue
        if len(token) > 1 and token[0] in "-+":
            for letter in token[1:]:
                if letter == "c" and token[0] == "-":
                    inline = True
                elif letter == "s" and token[0] == "-":
                    stdin = True
                elif letter in "oO":
                    i += 1
            continue
        operands = tokens[i - 1:]
        break
    if inline:
        return [o for o in operands if o.strip()], False
    return [], stdin or not operands


def _strip_closers(tokens: list[str]) -> list[str]:
    """`(cd d; cp a b)` leaves `b)` as the last token; the paren is syntax.
    Only an *unbalanced* closer is stripped, so `${NOTHING}` keeps its brace."""
    tokens = [t for t in tokens if t not in (")", "}")]
    if tokens:
        last = tokens[-1]
        while last.endswith(")") and last.count(")") > last.count("("):
            last = last[:-1]
        while last.endswith("}") and last.count("}") > last.count("{"):
            last = last[:-1]
        tokens[-1] = last
    return tokens


def _kind(stem: str, tokens: list[str], hazard: rules.Hazard | None) -> str:
    """writer / reader / unknown, for the paths this command is handed."""
    if stem in _WRITERS or stem in _INTERPRETERS or stem in _SHELLS:
        return "writer"
    # A reader in a writing form — `sed -i` or a `w` in its script, `sort -o`,
    # `find -delete`/`-exec`, `uniq IN OUT`, `xxd IN OUT`, `tree -o` …:
    # rules.READER_HAZARDS, the one table of those forms.
    if hazard is not None:
        return "writer"
    if stem in _READERS:
        return "reader"
    # An unrecognised stem may be a wrapper this module does not know
    # (`timeout -s KILL 5 cp …` leaves `KILL` as the stem): the first writing
    # word behind it decides.
    if any(rules._basename(t) in _WRITERS for t in tokens[1:]):
        return "writer"
    return "unknown"


def _nested(strings: list[str], local: _State, state: _State, protected: set[Path],
            ids: dict, depth: int, seen: _Seen) -> bool:
    """Analyse each string as a command line of its own, one level down, into
    `seen`. False when the bound was hit: the line is then recorded as too
    complex to judge (an ASK, never an ALLOW) and the caller stops."""
    for text in strings:
        if depth >= _MAX_DEPTH or state.budget.spans >= _MAX_SPANS:
            seen.touches.append(_opaque("shell strings nested too deeply to judge"))
            seen.writes = True
            return False
        state.budget.spans += 1
        inner = _analyse(text, _State(local.cwd, dict(state.variables), state.budget),
                         protected, ids, depth + 1)
        seen.touches += inner.touches
        seen.writes = seen.writes or inner.writes
        seen.read |= inner.read
    return True


def _segment(segment: str, state: _State, protected: set[Path], ids: dict,
             depth: int) -> _Seen:
    seen = _Seen()
    segment = _strip_comment(segment)
    # The command's own words: every redirection — `2>&1`, `> /dev/null`,
    # `{fd}>file`, a here-string — is cut out of the text first, so a copier's
    # destination is never a redirect's target. (It was, once: in `cp x
    # ALLOWLIST 2>&1` the last token was `2>&1`, and the allowlist read as a
    # copy *source*.) Redirect targets are judged on the full text below.
    words_only = rules.strip_redirections(segment)
    try:
        raw = shlex.split(words_only)
        parsed = True
    except ValueError:
        raw, parsed = words_only.split(), False
    raw = _strip_closers(raw)
    if not raw:
        return seen

    # `D=~/.config/jarvis` on its own, or `export D=…`: remembered for the
    # segments after it, which is when the shell would expand it.
    words = raw[1:] if raw[0] in ("export", "declare", "local", "readonly") else raw
    if words and all(re.fullmatch(r"[A-Za-z_]\w*=.*", w) for w in words):
        for word in words:
            name, value = word.split("=", 1)
            expanded = _expand(value, state) if value else ""
            if expanded is not None:
                state.variables[name] = expanded
            else:
                state.variables.pop(name, None)
        return seen

    tokens, chdir = _peel(raw, state)
    if not tokens:
        return seen
    stem = rules._basename(tokens[0])

    if stem in ("cd", "pushd", "popd"):
        if stem == "popd" or (len(tokens) > 1 and tokens[1] == "-"):
            state.cwd = None
        elif len(tokens) == 1:
            state.cwd = Path.home()
        else:
            target = _expand(tokens[1], state)
            state.cwd = Path(target) if target else None
        return seen

    local = _State(chdir if chdir is not None else state.cwd, state.variables, state.budget)

    # A redirect writes its target whatever the stem is: `echo x>~/…/allowlist.json`.
    for target in _redirect_targets(segment):
        text = _expand(target, local)
        if text is None:
            for path in protected:
                if Path(target).name == path.name:
                    seen.touches.append(Touch(path, False, f"a redirect into {target}"))
            continue
        hit = _exact(text, protected, ids)
        if hit is not None:
            seen.touches.append(Touch(hit, True, f"a redirect onto {hit.name}"))

    # A shell handed a string is a command line in its own right — from `-c`
    # (in any cluster), `eval`, or a here-string on its standard input.
    if stem in _SHELLS:
        strings, stdin = _shell_strings(tokens)
        inline = bool(strings)
        if stdin:
            strings += _here_strings(segment)
        if strings:
            if not _nested(strings, local, state, protected, ids, depth, seen):
                return seen
            if inline:
                return seen

    hazard = rules.reader_hazard(tokens)
    kind = _kind(stem, tokens, hazard)
    seen.writes = seen.writes or kind == "writer"
    # A shell or an interpreter run *by* this command is a command line of its
    # own: behind a wrapper this module does not know (`flock LOCK sh -c …`,
    # `timeout -s KILL 5 python3 -c …`), or as `find`'s `-exec` program.
    # Readers are left alone — `grep bash notes.txt` runs no bash.
    behind = None
    if stem not in _WRITERS | _INTERPRETERS | _SHELLS | _READERS:
        behind = next((k for k, t in enumerate(tokens[1:], 1)
                       if rules._basename(t) in _SHELLS | _INTERPRETERS), None)
    elif stem == "find":
        execs = [k for k, t in enumerate(tokens) if t in ("-exec", "-execdir", "-ok", "-okdir")]
        behind = next((k + 1 for k in execs if k + 1 < len(tokens)
                       and rules._basename(tokens[k + 1]) in _SHELLS | _INTERPRETERS), None)
    if behind is not None and not _nested([shlex.join(tokens[behind:])], local, state,
                                          protected, ids, depth, seen):
        return seen
    if kind == "writer" and stem not in _WRITERS | _INTERPRETERS | _SHELLS | _READERS:
        # Name the writing word behind an unknown wrapper, not the wrapper.
        stem = next((rules._basename(t) for t in tokens[1:]
                     if rules._basename(t) in _WRITERS), stem)

    # Words that expand to nothing are not arguments, and their presence means
    # the line is not fully known.
    args = tokens[1:]
    kept = [t for t in args if not _vanishes(t, local)]
    vanished = len(kept) != len(args)

    split = _copier_split(stem, kept)
    if split is None:
        written, read_only = _operands(kept), []
    else:
        written, read_only, understood = split
        # Fail closed: if anything about the copy is uncertain, every operand
        # is a write candidate, as for `mv` and `tee`. A protected path spelled
        # out is then a certain write; one behind an unknown is uncertain.
        if (vanished or not parsed or not understood
                or any(_expand(o, local) is None for o in written + read_only)):
            written, read_only = written + read_only, []
    if kind == "reader":
        written, read_only = [], written + read_only
    if hazard is not None:
        # Files a sed script writes (`w FILE`, `s/…/…/w FILE`) live inside the
        # script word, where no operand rule can see them.
        written = written + list(hazard.writes)

    # Inline source names its files inside a string no tokenizer splits. A
    # full path, or a bare name while standing in the gate's own folder, is a
    # write; a bare name anywhere else may be a project's own models.json, so
    # it asks rather than refuses. A here-string is source too, and so is a
    # sed script that runs commands (`e`).
    if stem in _INTERPRETERS or (hazard is not None and hazard.runs):
        suffixes = tuple(p.name for p in protected)
        source = " ".join([t for t in args if not (t in written and t.endswith(suffixes))]
                          + _here_strings(segment))
        for path in protected:
            if path.name not in source:
                continue
            spelled = (str(path) in source or "/.config/jarvis/" + path.name in source
                       or (local.cwd is not None and _resolve(local.cwd) == path.parent))
            seen.touches.append(Touch(path, spelled, f"{stem} source that names {path.name}"))

    for operand in read_only:
        text = _expand(operand, local)
        hit = _exact(text, protected, ids) if text is not None else None
        if hit is not None:
            seen.read.add(hit)

    everyone = written + read_only
    for operand in written:
        text = _expand(operand, local)
        if text is None:
            # `$UNSET/allowlist.json`, or a relative name after a `cd` that
            # could not be followed: the right name, an unknowable place.
            for path in protected:
                if Path(operand).name == path.name:
                    seen.touches.append(Touch(path, False, f"{stem} given {operand}"))
            continue
        hit = _exact(text, protected, ids)
        if hit is not None:
            seen.touches.append(Touch(hit, kind == "writer", f"{stem} on {hit.name}"))
            continue
        others = [o for o in everyone if o is not operand]
        other_names = {rules._basename(o.rstrip("/")) for o in others}
        for path, rel in _ancestor_of(text, protected):
            first = rel[0]
            if first in other_names or (stem == "find" and path.name in other_names):
                # `cp /tmp/x/allowlist.json ~/.config/jarvis/`, or `find
                # ~/.config/jarvis -name allowlist.json -exec cp …`.
                lands = kind == "writer" and (len(rel) == 1 or stem == "find")
                seen.touches.append(Touch(path, lands, f"{stem} into {text}"))
            elif any(_GLOB.search(n) and (fnmatch(path.name, n) or fnmatch(first, n))
                     for n in other_names):
                seen.touches.append(Touch(path, False, f"{stem} into {text}"))
            elif len(rel) <= 2:
                # The gate's folder itself, or the one above it.
                seen.touches.append(
                    Touch(path, False, f"{stem} on {text}, which holds {path.name}"))
            elif any(o.endswith(("/.", "/*")) or (stem == "rsync" and o.endswith("/"))
                     for o in others):
                seen.touches.append(Touch(path, False, f"{stem} copies a tree into {text}"))
    return seen


_ANSI_C = re.compile(r"\$'((?:[^'\\]|\\.)*)'")


def _ansi_c(text: str) -> str:
    """bash's `$'…'` quoting decoded, so `$'allow\\x6cist.json'` is read as the
    name it spells. shlex knows nothing of it and would hand back the escapes."""
    def decode(match: re.Match) -> str:
        try:
            value = match.group(1).encode("latin-1", "backslashreplace").decode(
                "unicode_escape")
        except (UnicodeError, ValueError):
            return match.group(0)
        return shlex.quote(value)
    return _ANSI_C.sub(decode, text)


def _substitutions(text: str, limit: int) -> tuple[list[str], bool]:
    """The command lines inside `$(…)`, `<(…)`, `>(…)` and backticks, at this
    level only, and whether there were more than `limit` of them.

    rules.py already makes a line with command substitution ASK, but a
    substitution that writes the allowlist must be refused, not asked, and a
    process substitution (`cat x > >(tee allowlist.json)`) is not substitution
    to rules.py at all. Over-approximate on purpose: quoting is ignored, so a
    `$(` inside single quotes is checked too — checking text that will not run
    can only add a refusal, never remove one.

    The scan resumes **past** each matched span, never just past its opener:
    a nested substitution is found when its parent's text is analysed one
    level down, so nesting costs one pass per level instead of one per opener.
    """
    found: list[str] = []
    i = 0
    while i < len(text):
        if text[i] in "$<>" and text[i + 1:i + 2] == "(":
            depth, j = 1, i + 2
            while j < len(text) and depth:
                depth += {"(": 1, ")": -1}.get(text[j], 0)
                j += 1
            inner = text[i + 2:j - 1] if depth == 0 else text[i + 2:]
            if inner.strip():
                found.append(inner)
            i = j
        elif text[i] == "`" and (i == 0 or text[i - 1] != "\\"):
            end = text.find("`", i + 1)
            inner = text[i + 1:] if end == -1 else text[i + 1:end]
            if inner.strip():
                found.append(inner)
            i = len(text) if end == -1 else end + 1
        else:
            i += 1
            continue
        if len(found) > limit:
            return found[:limit], True
    return found, False


def _analyse(command: str, state: _State, protected: set[Path], ids: dict,
             depth: int = 0) -> _Seen:
    total = _Seen()
    # As bash reads it: `\<newline>` removed first, so a continuation in the
    # middle of a path or an operator hides nothing (2026-10-09).
    command = _ansi_c(rules.join_continuations(command))
    budget = state.budget
    inner, overflow = _substitutions(command, max(_MAX_SPANS - budget.spans, 0))
    if inner and depth >= _MAX_DEPTH:
        total.touches.append(_opaque("substitutions nested too deeply to judge"))
    else:
        for text in inner:
            budget.spans += 1
            nested = _analyse(text, _State(state.cwd, dict(state.variables), budget),
                              protected, ids, depth + 1)
            total.touches += nested.touches
    if overflow:
        total.touches.append(_opaque(f"more than {_MAX_SPANS} substitutions, too many to judge"))
    parts = [_segment(s, state, protected, ids, depth) for s in rules.segments(command)]
    for k, part in enumerate(parts):
        total.touches += part.touches
        total.writes = total.writes or part.writes
        total.read |= part.read
        # `echo ~/.config/jarvis/allowlist.json | xargs rm`: named in one
        # segment, written by another. Not provable, so not certain.
        if part.read and any(other.writes for j, other in enumerate(parts) if j != k):
            total.touches += [Touch(p, False, f"names {p.name} beside a command that writes")
                              for p in part.read]
    return total


def command_touches(command: str, cwd: str | Path | None = None) -> list[Touch]:
    """Every protected file this command line reaches, as far as can be seen.

    `cwd` is where the command will run. v1 runs commands in its own process,
    so it passes nothing and the process cwd is used; a v2 worker runs in its
    brief's folder, which is not the daemon's, and relative names have to be
    resolved there or `echo hi > models.json` means a different file.
    """
    if not command or not command.strip():
        return []
    protected = protected_paths()
    if cwd is None:
        try:
            base: Path | None = Path.cwd()
        except OSError:
            base = None
    else:
        base = Path(os.path.expanduser(str(cwd)))
        if not base.is_absolute():
            base = None  # a relative cwd is relative to nothing we know
    return _analyse(command, _State(base), protected, _identities(protected)).touches


def command_touch(command: str, cwd: str | Path | None = None) -> Touch | None:
    """The most serious protected file this line reaches, or None.

    Order: a certain write to the allowlist, any other certain write, then the
    uncertain ones in the same order.
    """
    touches = command_touches(command, cwd)
    if not touches:
        return None
    gate = gate_paths()
    return min(touches, key=lambda t: (not t.certain, t.path not in gate))


def refused(command: str, cwd: str | Path | None = None) -> Touch | None:
    """The touch that makes this line unrunnable by Jarvis, or None: a write
    that provably lands on the allowlist. Everything else this module sees is
    an ASK, decided by the caller."""
    touch = command_touch(command, cwd)
    if touch is not None and touch.certain and touch.path in gate_paths():
        return touch
    return None


def refusal(touch: Touch) -> str:
    return (f"{touch.why}: {touch.name} is the approval gate's own allowlist, and "
            "a command that writes it would decide what runs without anyone "
            "being asked. Only the owner changes it, by hand")


def ask_reason(touch: Touch) -> str:
    if touch.opaque:
        return (f"this line is too complex to judge ({touch.why}), so whether it "
                "writes Jarvis's own configuration cannot be told; it never runs "
                "unasked and no allowlist entry covers it")
    return (f"{touch.why}: {touch.name} is Jarvis's own configuration, so this "
            "never runs unasked and no allowlist entry covers it")
