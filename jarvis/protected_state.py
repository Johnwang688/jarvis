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


def hud_trees() -> set[Path]:
    """The installed HUD's folders (`files._hud_roots`: the install root, and
    `hud/dist` under JARVIS_HUD_DEV=1). Not files but trees: a command that
    writes anything at or below one — `cp`, `rsync`, a redirect — is **asked**
    (review of PR #44, MED-2), the same way an uncertain gate-state write is;
    the write tools refuse it outright. Empty when a root cannot be resolved —
    the write tools refuse then, and a shell write is not the boundary."""
    from .tools.files import _hud_roots

    try:
        return _hud_roots()
    except (OSError, RuntimeError):
        return set()


def _in_tree(text: str, trees: set[Path]) -> Path | None:
    """The HUD tree `text` lies in (or is), or one it holds within two levels
    (`rm -rf ~/.local/share/jarvis`), else None."""
    if not trees:
        return None
    if _GLOB.search(text):
        text = str(Path(*[p for p in Path(text).parts if not _GLOB.search(p)] or ["."]))
    resolved = _resolve(text)
    for tree in trees:
        if resolved == tree or tree in resolved.parents:
            return tree
        if resolved in tree.parents and len(tree.relative_to(resolved).parts) <= 2:
            return tree
    return None


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
    hud: bool = False              # inside the installed HUD's tree (always uncertain: asks)

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
        tree = _in_tree(text, hud_trees())
        if tree is not None:
            seen.touches.append(Touch(tree, False, f"a redirect into {text}", hud=True))

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
        tree = _in_tree(text, hud_trees())
        if tree is not None and kind != "reader":
            seen.touches.append(Touch(tree, False, f"{stem} on {text}", hud=True))
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
    if touch.hud:
        return (f"{touch.why}: {touch.path} holds the installed HUD, the code the "
                "approval window runs, so this never runs unasked and no allowlist "
                "entry covers it; `jarvis hud install` is how a build gets there")
    if touch.opaque:
        return (f"this line is too complex to judge ({touch.why}), so whether it "
                "writes Jarvis's own configuration cannot be told; it never runs "
                "unasked and no allowlist entry covers it")
    return (f"{touch.why}: {touch.name} is Jarvis's own configuration, so this "
            "never runs unasked and no allowlist entry covers it")


# --- owner-only operations (decision C-1; review of PR #44, MED-2) ---------------
#
# Some `jarvis` commands are the owner's and the lead's alone — today `jarvis
# hud install`, the one thing that writes the installed HUD, the code the
# approval window runs (an agent can edit `hud/src`, so letting it install
# would undo C-1). They run them from their own shells, outside Jarvis's gate,
# so refusing them to Jarvis costs nothing. **DENY, never asked**, through
# this one detector: `rules.decide` asks it (so v1's `static_verdict`/
# `command_verdict` and v2's layer 1, which both start from `rules.decide`,
# refuse it) and `run_command` refuses it even with no approver.
#
# **The extension point is `OWNER_OPS`** (and `OWNER_OP_MODULES` for a command
# whose implementing module can also be run or called directly): add a path
# there and every spelling below covers it.
#
# What counts as running one, judged per command and never by words merely
# appearing on the line (a DENY cannot be approved, so a false one blocks the
# owner too — review of PR #44 at 62db94f):
#   * the **command word** — after assignments, `env` (`-S` split, `-C`,
#     `-u`), `builtin`, and the wrappers and runners in `_RUNNERS` (`nohup`,
#     `timeout`, `nice`, `flock FILE`, `xargs`, `systemd-run`, `uv run`,
#     `uvx`, `poetry/pdm/hatch/rye/pipx run`, `ssh HOST …`) — is `jarvis` (any
#     path to it, a `$VAR` the line set to it, or a link the line made to it
#     with `ln`), or a Python run with `-m jarvis` / `-m jarvis.__main__`, and
#     its argv, options skipped, begins with an owner path; behind `xargs`, a
#     *prefix* of one is enough (its input may complete it);
#   * a Python run of an owner module itself: `-m jarvis.v2.hud_install`, or
#     the file `hud_install.py`;
#   * Python source — `-c`, a here-string or a here-document fed to it — that
#     *invokes* an owner op: calls the module's writers (`main()`,
#     `install(…)`, …) under whatever name it was imported as, runs the module
#     or the CLI with `runpy`, calls the CLI's entry with the path's words, or
#     hands a command line that is one to `os.system`/`subprocess`. Importing
#     the module and reading from it (`hud_install.builds(…)`) is not;
#   * a shell or `eval` handed a string (`-c` in any cluster), its standard
#     input from a here-document or here-string, and `$(…)`/backticks — each
#     a command line of its own, judged the same way.
# Not seen — the stated limits: a link made earlier under another name
# (`~/bin/jv hud install`), words built at run time (`jarvis $(echo hud)
# install`, a word from the environment rather than the line's own
# assignments), `runpy` or `sys.argv` assembled at run time
# (`run_module("jar"+"vis")`), a Makefile, alias, function or script file. None of this is a
# boundary; the installed HUD's real protection is that the write tools and
# v2's grants refuse its folder, and a shell write into it asks.

_RESTART_WHY = (
    "this restarts the v2 daemon (`jarvis restart`), the process that runs Jarvis's agents and "
    "holds the owner's pending approvals — Jarvis's own control plane. Only the owner restarts "
    "it, from their own terminal; Jarvis never does, asked or not")
OWNER_OPS: dict[tuple[str, ...], str] = {
    ("hud", "install"): (
        "this runs `jarvis hud install`, which writes the installed HUD — the code the "
        "approval window runs. Only the owner or the lead runs it, from their own shell "
        "(decision C-1); Jarvis never does, asked or not"),
    # `jarvis hud rebuild` and `jarvis restart` (2026-10-11, `v2/owner_ops.py`).
    ("hud", "rebuild"): (
        "this runs `jarvis hud rebuild`, which installs a new HUD — the code the approval "
        "window runs — and can pull the checkout and restart the v2 daemon. Only the owner runs "
        "it, from their own terminal (decision C-1); Jarvis never does, asked or not"),
    ("restart",): _RESTART_WHY,
    ("daemon2", "restart"): _RESTART_WHY,
}
# A module that implements an owner op: (the op's path, the functions whose
# call performs it). Running the module, or calling one of those from inline
# source, is the op.
OWNER_OP_MODULES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "jarvis.v2.hud_install": (("hud", "install"),
                              ("main", "install", "install_checkout", "npm_build", "prune")),
    "jarvis.v2.owner_ops": (("hud", "rebuild"),
                            ("restart", "_restart", "rebuild", "pull", "npm_ci", "systemctl")),
}

# The daemon's systemd unit, changed directly (2026-10-11): `systemctl
# --user restart jarvis-daemon2` was ALLOW in `rules.decide` — run with nobody
# asked on every human-backed surface — and `stop` ends it for good (a clean
# exit is not one `Restart=on-failure` restarts). Any verb that changes the
# unit, named or matched by a glob, and the user manager's `exit`/`isolate`,
# which stop every user unit. Not seen, and not decided here: a `kill` of its
# pid, and `pkill -f jarvis`, which the owner's 2026-08-09 matrix (rules_check)
# keeps ALLOW as ordinary process control — it matches the daemon too.
DAEMON_UNIT = "jarvis-daemon2"
_DAEMON_UNIT_NAMES = (DAEMON_UNIT, DAEMON_UNIT + ".service")
_DAEMON_WORDS = ("systemctl",)
_SYSTEMCTL_VALUES = frozenset({
    "-t", "--type", "-p", "--property", "-s", "--signal", "-H", "--host", "-M", "--machine",
    "-n", "--lines", "-o", "--output", "--state", "--kill-whom", "--kill-value", "--root",
    "--image", "--image-policy", "--job-mode", "--what", "--timestamp", "--check-inhibitors",
    "--when", "--message", "--drop-in", "--preset-mode", "--boot-loader-menu",
    "--boot-loader-entry", "--reboot-argument", "--marked"})
# Verbs that only read; every other verb naming the unit changes it
# (start, stop, restart, kill, disable, mask, edit, set-property, …).
_SYSTEMCTL_READS = frozenset({
    "status", "show", "cat", "help", "is-active", "is-enabled", "is-failed", "list-units",
    "list-unit-files", "list-dependencies", "list-jobs", "list-timers", "list-sockets",
    "list-paths", "list-automounts", "list-machines", "show-environment", "get-default",
    "is-system-running", "whoami", "log-level", "log-target", "service-watchdogs"})
# Verbs that stop every unit of the *user* manager, the daemon's among them.
_SYSTEMCTL_USER_STOPS_ALL = frozenset({"exit", "isolate"})
_DAEMON_UNIT_WHY = (
    "this {how} — the v2 daemon, the process that runs Jarvis's agents and holds the owner's "
    "pending approvals, Jarvis's own control plane. Only the owner stops or restarts it, from "
    "their own terminal (`jarvis restart`); Jarvis never does, asked or not")


def _names_daemon_unit(word: str) -> bool:
    return any(fnmatch(name, word) for name in _DAEMON_UNIT_NAMES)


def _systemctl_op(tokens: list[str], appends: bool = False) -> str | None:
    """`systemctl … VERB … jarvis-daemon2` with a verb that changes the
    unit; `systemctl --user exit|isolate`; or, behind `xargs`, a changing
    verb whose units its input supplies."""
    args, k, verb = tokens[1:], 0, None
    user = False
    while k < len(args):
        token = args[k]
        if token == "--":
            k += 1
            break
        if token.startswith("-") and len(token) > 1:
            name = token.split("=", 1)[0]
            user = user or name == "--user"
            k += 2 if (name in _SYSTEMCTL_VALUES and "=" not in token) else 1
            continue
        verb = token
        k += 1
        break
    if verb is None and k < len(args):
        verb, k = args[k], k + 1
    if verb is None or verb in _SYSTEMCTL_READS:
        return None
    user = user or "--user" in args[k:]
    if user and verb in _SYSTEMCTL_USER_STOPS_ALL:
        return _DAEMON_UNIT_WHY.format(how=f"stops every user unit (`systemctl --user {verb}`), "
                                           f"{DAEMON_UNIT} among them")
    if any(_names_daemon_unit(word) for word in args[k:] if not word.startswith("-")):
        return _DAEMON_UNIT_WHY.format(how=f"changes systemd's {DAEMON_UNIT} (`systemctl {verb}`)")
    if appends:
        return _DAEMON_UNIT_WHY.format(how=f"runs `systemctl {verb}` on units its input names "
                                           f"(`xargs`), {DAEMON_UNIT} perhaps")
    return None


_CLI_MODULES = frozenset({"jarvis", "jarvis.__main__"})
_LAUNCHERS = frozenset({"jarvis", "jarvis.exe"})
_PYTHON = re.compile(r"(?:python|pypy)[0-9.]*")
_OWNER_DEPTH = 4
_OWNER_BUDGET = 64 * 1024

# Wrappers and runners that run the command in their arguments: the options
# that take a value, how many positionals precede the command, a subcommand
# that must come first (`uv run`), whether the rest is one command *string*
# (ssh), and whether its input is appended as more arguments (xargs).
@dataclass(frozen=True)
class _Runner:
    values: frozenset = frozenset()
    positionals: int = 0
    subcommand: tuple = ()
    joined: bool = False
    appends: bool = False


_RUN_VALUES = frozenset({"--directory", "--project", "--with", "--with-requirements", "--with-editable",
                         "--python", "-p", "--package", "--env-file", "--extra", "--group", "--index",
                         "--default-index", "--from", "--index-url", "--extra-index-url", "-w",
                         "--only-group", "--no-group", "--cache-dir", "--config-file", "-e", "--env",
                         "--spec", "--pip-args"})
_RUNNERS: dict[str, _Runner] = {
    "nohup": _Runner(), "setsid": _Runner(), "command": _Runner(), "chronic": _Runner(),
    "time": _Runner(frozenset({"-f", "--format", "-o", "--output"})),
    "exec": _Runner(frozenset({"-a"})),
    "nice": _Runner(frozenset({"-n", "--adjustment"})),
    "ionice": _Runner(frozenset({"-c", "--class", "-n", "--classdata", "-p", "--pid", "-P", "--pgid",
                                 "-u", "--uid"})),
    "stdbuf": _Runner(frozenset({"-i", "-o", "-e", "--input", "--output", "--error"})),
    "timeout": _Runner(frozenset({"-s", "--signal", "-k", "--kill-after"}), positionals=1),
    "flock": _Runner(frozenset({"-w", "--timeout", "-E", "--conflict-exit-code"}), positionals=1),
    "watch": _Runner(frozenset({"-n", "--interval", "-d", "--differences", "-q", "--equexit"}), joined=True),
    "xargs": _Runner(frozenset({"-I", "-i", "-n", "--max-args", "-L", "-l", "--max-lines", "-d",
                                "--delimiter", "-a", "--arg-file", "-E", "-e", "--eof", "-P",
                                "--max-procs", "-s", "--max-chars", "--process-slot-var"}), appends=True),
    "systemd-run": _Runner(frozenset({"-p", "--property", "-u", "--unit", "--description", "-E",
                                      "--setenv", "--working-directory", "-M", "--machine", "--slice",
                                      "-H", "--host", "--uid", "--gid", "--nice", "--on-calendar",
                                      "--on-active", "--timer-property", "--path-property",
                                      "--socket-property", "--service-type", "--expand-environment"})),
    "uv": _Runner(_RUN_VALUES, subcommand=("run", "tool run")),
    "uvx": _Runner(_RUN_VALUES),
    "poetry": _Runner(_RUN_VALUES, subcommand=("run",)),
    "pdm": _Runner(_RUN_VALUES, subcommand=("run",)),
    "hatch": _Runner(_RUN_VALUES, subcommand=("run",)),
    "rye": _Runner(_RUN_VALUES, subcommand=("run",)),
    "pipx": _Runner(_RUN_VALUES, subcommand=("run",)),
    "ssh": _Runner(frozenset({"-p", "-i", "-l", "-o", "-F", "-J", "-b", "-c", "-D", "-E", "-e", "-I",
                              "-L", "-m", "-O", "-Q", "-R", "-S", "-W", "-w", "-B"}), positionals=1,
                   joined=True),
}


@dataclass
class _Owner:
    """Per command line: the line's own assignments, links made to the
    launcher, and the here-document bodies still to be handed out."""
    variables: dict[str, str] = field(default_factory=dict)
    launchers: set[str] = field(default_factory=set)
    bodies: list[str] = field(default_factory=list)
    # Characters the nested parses (substitutions, shell strings, source) may
    # still spend — shared by every level, so a line cannot make the parse
    # cost more than about twice its own length. Past it, nothing deeper is
    # judged here; `rules.decide` already ASKs any substitution, and no
    # allowlist entry covers one, so what is not seen is asked, never run.
    budget: list[int] = field(default_factory=lambda: [_OWNER_BUDGET])
    filtered: bool = True          # the per-segment cheap test (off only to test it)

    def child(self) -> "_Owner":
        return _Owner(dict(self.variables), set(self.launchers), budget=self.budget,
                      filtered=self.filtered)


def _owner_substitutions(text: str) -> list[str]:
    """The command lines inside `$(…)`, backticks and `<(…)`/`>(…)` that bash
    would run — **not** inside single quotes. (`_substitutions` ignores
    quoting on purpose, for refusals where checking text that never runs can
    only add a refusal; a false DENY here would block the owner's own
    `git commit -m 'run `jarvis hud install` after merging'`.)"""
    if "(" not in text and "`" not in text:
        return []
    live = set(rules.unquoted_indices(text, double_is_quote=False))
    out: list[str] = []
    i, n = 0, len(text)
    while i < n and len(out) < _MAX_SPANS:
        opener = (2 if i in live and text.startswith(("$(", "<(", ">("), i)
                  and not text.startswith("$((", i) else 0)
        if opener:
            depth, j = 1, i + 2
            while j < n and depth:
                depth += {"(": 1, ")": -1}.get(text[j], 0)
                j += 1
            out.append(text[i + 2:j - 1] if depth == 0 else text[i + 2:])
            i = j
            continue
        if i in live and text[i] == "`":
            j = text.find("`", i + 1)
            while j > 0 and text[j - 1] == "\\":
                j = text.find("`", j + 1)
            out.append(text[i + 1:j] if j != -1 else text[i + 1:])
            i = j + 1 if j != -1 else n
            continue
        i += 1
    return out


def _owner_word(token: str, state: _Owner, environ: bool = True) -> str:
    """A word with the line's own `$VAR`/`${VAR}` assignments put in (and,
    for the command word only, the environment's)."""
    return re.sub(r"\$\{?(\w+)\}?",
                  lambda m: state.variables.get(m.group(1), os.environ.get(m.group(1), "") if environ else
                                                m.group(0)), token)


def _is_launcher(token: str, state: _Owner) -> bool:
    word = _owner_word(token, state)
    return (rules._basename(word) in _LAUNCHERS or word in state.launchers
            or (bool(word) and rules._basename(word) in {rules._basename(l) for l in state.launchers}
                and "/" in word))


def _owner_path(argv: list[str], open_ended: bool) -> str | None:
    """The refusal for a jarvis argv that is (or, fed more words by xargs,
    may become) an owner op."""
    words = [t for t in argv if not t.startswith("-")]
    for path, why in OWNER_OPS.items():
        if tuple(words[:len(path)]) == path:
            return why
        if open_ended and len(words) < len(path) and tuple(words) == path[:len(words)]:
            return why + " (its input may complete the command)"
    return None


def _python_parse(args: list[str]) -> tuple[str, str | None, list[str]]:
    """How a Python is told what to run: ("module", name, argv), ("source",
    text, argv), ("script", path, argv) or ("stdin", None, argv)."""
    i = 0
    while i < len(args):
        token = args[i]
        i += 1
        if token == "-":
            return "stdin", None, args[i:]
        if token == "--":
            break
        if token.startswith("--"):
            if token in ("--check-hash-based-pycs",):
                i += 1
            continue
        if not token.startswith("-") or len(token) == 1:
            return "script", token, args[i:]
        letters = token[1:]
        for k, letter in enumerate(letters):
            rest = letters[k + 1:]
            if letter in "mc":
                value = rest if rest else (args[i] if i < len(args) else "")
                if not rest:
                    i += 1
                return ("module" if letter == "m" else "source"), value, args[i:]
            if letter in "WX":
                if not rest:
                    i += 1
                break
    if i < len(args):
        return "script", args[i], args[i + 1:]
    return "stdin", None, []


_CALL_SITES = ("system", "popen", "run", "call", "check_call", "check_output", "Popen", "getoutput",
               "getstatusoutput", "spawnlp", "execlp", "execvp")


def _source_invokes(source: str, state: _Owner, depth: int) -> str | None:
    """Python source that performs an owner op — not one that only names or
    reads from the module (review of PR #44 at 62db94f)."""
    if not source:
        return None
    for module, (path, writers) in OWNER_OP_MODULES.items():
        parent, _, last = module.rpartition(".")
        dotted = re.escape(module).replace(r"\.", r"\s*\.\s*")
        names = {dotted}
        for m in re.finditer(r"\bimport\s+" + dotted + r"(?:\s+as\s+(\w+))?", source):
            names.add(re.escape(m.group(1)) if m.group(1) else dotted)
        for m in re.finditer(r"\bfrom\s+" + re.escape(parent).replace(r"\.", r"\s*\.\s*")
                             + r"\s+import\s+([^\n;]+)", source):
            for item in m.group(1).replace("(", " ").replace(")", " ").split(","):
                bits = item.split()
                if bits and bits[0] == last:
                    names.add(re.escape(bits[2] if len(bits) == 3 and bits[1] == "as" else last))
        funcs = set()
        for m in re.finditer(r"\bfrom\s+" + dotted + r"\s+import\s+([^\n;]+)", source):
            for item in m.group(1).replace("(", " ").replace(")", " ").split(","):
                bits = item.split()
                if bits and bits[0] in writers:
                    funcs.add(re.escape(bits[2] if len(bits) == 3 and bits[1] == "as" else bits[0]))
        writer = "|".join(map(re.escape, writers))
        if re.search(r"(?:" + "|".join(names) + r")\s*\.\s*(?:" + writer + r")\s*\(", source):
            return OWNER_OPS[path]
        if funcs and re.search(r"(?<![\w.])(?:" + "|".join(funcs) + r")\s*\(", source):
            return OWNER_OPS[path]
        if re.search(r"\brun_module\s*\(\s*['\"]" + re.escape(module) + r"['\"]", source):
            return OWNER_OPS[path]
        if re.search(r"\brun_path\s*\([^)]*" + re.escape(last) + r"\.py", source):
            return OWNER_OPS[path]
    # The CLI itself, with an owner path's words: `runpy.run_module("jarvis")`
    # with `sys.argv` set, `jarvis.__main__.main([...])`, `cmd_hud(…install…)`.
    cli = re.search(r"\brun_module\s*\(\s*['\"]jarvis(?:\.__main__)?['\"]"
                    r"|\b(?:from\s+jarvis(?:\s*\.\s*__main__)?\s+import|import\s+jarvis\s*\.\s*__main__)"
                    r"|\bjarvis\s*\.\s*__main__\s*\.", source)
    for path, why in OWNER_OPS.items():
        literals = r"\s*,\s*".join(r"['\"]" + re.escape(word) + r"['\"]" for word in path)
        if cli and re.search(literals, source):
            return why
        if re.search(r"\bcmd_" + re.escape(path[0]) + r"\s*\(", source) and all(
                re.search(r"['\"]" + re.escape(word) + r"['\"]", source) for word in path[1:]):
            return why
        # An argv list naming the CLI: `["jarvis", "hud", "install"]`.
        if re.search(r"['\"](?:[^'\"\s]*/)?jarvis['\"]\s*,\s*" + literals, source):
            return why
    # A command line handed to a shell from Python: `os.system("jarvis …")`.
    if depth < _OWNER_DEPTH:
        call = r"\b(?:" + "|".join(_CALL_SITES) + r")\s*\(\s*[rbuf]*(['\"])(.*?)\1"
        for m in re.finditer(call, source, re.S):
            why = _owner_line(m.group(2), state.child(), depth + 1)
            if why:
                return why
    return None


def _peel_runner(tokens: list[str], state: _Owner) -> tuple[list[str], bool, bool]:
    """(the command a line of words runs, whether more words may be appended
    to it from input, whether it is one command *string*). Peels assignments,
    `builtin`, `env` and everything in `_RUNNERS`, as far as they nest."""
    tokens = [t for t in tokens]
    while tokens and tokens[0] in ("(", "{", "!"):
        tokens = tokens[1:]
    if tokens and tokens[0].startswith("(") and len(tokens[0]) > 1:
        tokens[0] = tokens[0][1:]
    appends = joined = False
    for _ in range(12):
        while tokens and re.fullmatch(r"[A-Za-z_]\w*=.*", tokens[0]):
            tokens = tokens[1:]
        if tokens and tokens[0] == "builtin":
            tokens = tokens[1:]
            continue
        if not tokens:
            break
        stem = rules._basename(tokens[0])
        if stem == "env":
            rest, k = tokens[1:], 0
            split = None
            while k < len(rest) and (rest[k].startswith("-") or "=" in rest[k]):
                if rest[k] == "--":
                    k += 1
                    break
                found = _env_split(rest, k)
                if found is not None:
                    split, k = found
                    break
                k += 2 if rest[k] in ("-C", "--chdir", "-u", "--unset") else 1
            if split is not None:
                try:
                    words = shlex.split(split)
                except ValueError:
                    words = split.split()
                tokens = ["env"] + words + rest[k:]
                continue
            tokens = rest[k:]
            continue
        runner = _RUNNERS.get(stem)
        if runner is None:
            break
        rest = tokens[1:]
        if runner.subcommand:
            matched = next((sub for sub in runner.subcommand
                            if rest[:len(sub.split())] == sub.split()), None)
            if matched is None:
                break
            rest = rest[len(matched.split()):]
        k, positionals = 0, runner.positionals
        while k < len(rest):
            token = rest[k]
            if token == "--":
                k += 1
                break
            if token.startswith("-") and len(token) > 1:
                name = token.split("=", 1)[0]
                k += 2 if (name in runner.values and "=" not in token) else 1
                continue
            if stem == "timeout" and re.fullmatch(r"[\d.]+[smhd]?", token) and positionals:
                positionals -= 1
                k += 1
                continue
            if positionals:
                positionals -= 1
                k += 1
                continue
            break
        tokens = rest[k:]
        if stem == "flock" and tokens and tokens[0] in ("-c", "--command"):
            return tokens[1:2], False, True
        appends = appends or runner.appends
        if runner.joined:
            return tokens, appends, True
    return tokens, appends, joined


def _owner_command(tokens: list[str], segment: str, state: _Owner, depth: int) -> str | None:
    tokens, appends, joined = _peel_runner(tokens, state)
    if not tokens:
        return None
    if joined:
        return _owner_line(" ".join(tokens), state, depth + 1) if depth < _OWNER_DEPTH else None
    stem = rules._basename(tokens[0])
    if _is_launcher(tokens[0], state):
        return _owner_path([_owner_word(t, state, environ=False) for t in tokens[1:]], appends)
    if stem == "ln":
        # `ln -s …/jarvis /tmp/jv && /tmp/jv hud install`: the link is a launcher.
        operands = [t for t in tokens[1:] if not t.startswith("-")]
        if len(operands) >= 2 and rules._basename(_owner_word(operands[0], state)) in _LAUNCHERS:
            link = _owner_word(operands[-1], state)
            state.launchers.add(link.rstrip("/"))
        return None
    if stem == "systemctl":
        return _systemctl_op(tokens, appends)
    if _PYTHON.fullmatch(stem):
        kind, what, argv = _python_parse(tokens[1:])
        if kind == "module":
            if what in _CLI_MODULES:
                return _owner_path([_owner_word(t, state, environ=False) for t in argv], appends)
            if what in OWNER_OP_MODULES:
                return OWNER_OPS[OWNER_OP_MODULES[what][0]]
            return None
        if kind == "script":
            stem_of = rules._basename(what or "")
            for module, (path, _) in OWNER_OP_MODULES.items():
                if stem_of == module.rpartition(".")[2] + ".py":
                    return OWNER_OPS[path]
            return None
        source = what if kind == "source" else ""
        if kind == "stdin":
            source = "\n".join(_here_strings(segment) + _take_bodies(segment, state))
        return _source_invokes(source, state, depth)
    if stem in _SHELLS and depth < _OWNER_DEPTH:
        strings, stdin = _shell_strings(tokens)
        if stdin:
            strings = strings + _here_strings(segment) + _take_bodies(segment, state)
        for text in strings:
            why = _owner_line(text, state, depth + 1)
            if why:
                return why
    return None


def _take_bodies(segment: str, state: _Owner) -> list[str]:
    """The here-document bodies this segment's `<<` redirections read, in order."""
    count = sum(1 for r in rules.redirections(segment) if r.op in ("<<", "<<-"))
    taken, state.bodies = state.bodies[:count], state.bodies[count:]
    return taken


def _split_heredocs(command: str) -> tuple[str, list[tuple[str, bool]]]:
    """The command with every here-document body cut out, and the bodies in
    order. A body is data to the command that reads it — `cat > notes.md
    <<EOF` holding the words `jarvis hud install` runs nothing — and is judged
    only where a shell or a Python reads it as its program."""
    lines = command.split("\n")
    kept: list[str] = []
    bodies: list[tuple[str, bool]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        i += 1
        delimiters = []
        for r in (rules.redirections(line) if "<<" in line else ()):
            if r.op in ("<<", "<<-") and r.word:
                word = r.word.replace("'", "").replace('"', "").replace("\\", "")
                delimiters.append((word, r.op == "<<-", word == r.word))
        for word, tabs, expands in delimiters:
            body = []
            while i < len(lines):
                text = lines[i].lstrip("\t") if tabs else lines[i]
                i += 1
                if text == word:
                    break
                body.append(lines[i - 1])
            bodies.append(("\n".join(body), expands))
    return "\n".join(kept), bodies


def _owner_line(command: str, state: _Owner, depth: int) -> str | None:
    if depth:
        if state.budget[0] < len(command):
            return None
        state.budget[0] -= len(command)
    command = _ansi_c(rules.join_continuations(command))
    command, bodies = _split_heredocs(command)
    state.bodies = [body for body, _ in bodies] + state.bodies
    if depth < _OWNER_DEPTH:
        # The line's own substitutions, and those in a here-document whose
        # delimiter was not quoted — bash expands those bodies too.
        inner = _owner_substitutions(command)
        for body, expands in bodies:
            if expands:
                inner += _owner_substitutions(body.replace("'", " ").replace('"', " "))
        for text in inner:
            why = _owner_line(text, state.child(), depth + 1)
            if why:
                return why
    for segment in rules.segments(command):
        # The same cheap test per segment: one that cannot hold an op (and
        # sets no variable, reads no here-document and names no launcher a
        # later `ln` could alias) is not parsed — on a long line this is
        # where the time goes.
        if state.filtered and not (_could_be_owner_op(segment) or "<<" in segment or "jarvis" in segment
                                   or "=" in segment or state.variables or state.launchers):
            continue
        segment = _strip_comment(segment)
        words_only = rules.strip_redirections(segment)
        try:
            raw = shlex.split(words_only)
        except ValueError:
            raw = words_only.split()
        raw = _strip_closers(raw)
        if not raw:
            continue
        words = raw[1:] if raw[0] in ("export", "declare", "local", "readonly") else raw
        if words and all(re.fullmatch(r"[A-Za-z_]\w*=.*", w) for w in words):
            for word in words:
                name, value = word.split("=", 1)
                state.variables[name] = _owner_word(value, state)
            continue
        why = _owner_command(raw, segment, state, depth)
        if why:
            return why
    return None


def _could_be_owner_op(command: str) -> bool:
    """A cheap test the full parse can never beat: can this line hold an
    owner op at all? The full parse resolves quotes, backslashes,
    continuations, ANSI-C `$'…'` text and the line's own `$VAR`s, but every
    path it can find still spells, in the line's text once quotes and
    backslashes are gone, an op's first word (`hud`, `restart`, `daemon2`),
    an owner module's name (`hud_install`, `owner_ops` — a module named on
    its own spells no op word: `python -m jarvis.v2.owner_ops`), or
    `systemctl` — or goes through `xargs`, whose input may
    supply it. A line with none of them is skipped, so a
    pathological one (`$($($(…`) costs a substring search, not a parse.
    `owner_op_check` in tests/v2/hud_install_check.py holds the two to
    agreement over a corpus and every spelling."""
    if "$'" in command:
        return True
    # Continuations first (bash drops `\<newline>` before anything), then
    # every quote and backslash: `h"u"d`, `h\ud` and `hu\<newline>d` all
    # read as `hud` to the shell, and so they do here.
    flat = command.replace("\\\n", "")
    for ch in ("'", '"', "\\"):
        flat = flat.replace(ch, "")
    if ("xargs" in flat or any(path[0] in flat for path in OWNER_OPS)
            or any(module.rpartition(".")[2] in flat for module in OWNER_OP_MODULES)
            or any(word in flat for word in _DAEMON_WORDS)):
        return True
    # A word the line assembles from its own variables (`x=h; jarvis ${x}ud
    # install`) need not spell the op's word anywhere: such a line is parsed.
    return "$" in flat and re.search(r"(?:^|[\s;&|(])[A-Za-z_]\w*=", flat) is not None


def owner_op(command: str, *, prefilter: bool = True) -> str | None:
    """Why this line runs an owner-only `jarvis` operation, or None — a
    refusal, never an ask. `prefilter=False` turns both cheap tests off (the
    line's and each segment's), for the test that holds them to the full
    parse."""
    if not command or not command.strip():
        return None
    if prefilter and not _could_be_owner_op(command):
        return None
    try:
        return _owner_line(command, _Owner(filtered=prefilter), 0)
    except RecursionError:
        return None


def installs_hud(command: str, cwd: str | Path | None = None) -> str | None:
    """`owner_op`, under the name the first callers used."""
    return owner_op(command)
