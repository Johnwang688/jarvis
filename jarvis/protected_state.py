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
from dataclasses import dataclass, field, replace
from fnmatch import fnmatch
from pathlib import Path

try:                     # `re`'s own parser: what a pattern will run as (`_regex_work`)
    from re import _constants as _sre_c, _parser as _sre_parse
except ImportError:      # pragma: no cover — Python before 3.11
    import sre_constants as _sre_c
    import sre_parse as _sre_parse

from . import config, rules


def _resolve(path: str | Path) -> Path:
    # Within one line's analysis (`rules.work_scope`) a name resolves once:
    # the readings of its wrappers and runners hand the same operands to
    # each level (review of PR #75).
    return _memo(("resolve", str(path)), lambda: _resolve_now(path))


def _resolve_now(path: str | Path) -> Path:
    try:
        return Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return Path(os.path.abspath(os.path.expanduser(str(path))))


def _memo(key: tuple, compute):
    """`compute()`, once per key within one line's analysis (`rules.work_scope`):
    the filesystem does not change under one verdict."""
    work = rules._WORK.get()
    if work is None:
        return compute()
    cache = work.cache
    if key in cache:
        return cache[key]
    value = compute()
    if len(cache) < 200_000:
        cache[key] = value
    return value


def _under(path: Path, folder: Path) -> bool:
    """`folder in path.parents`, for absolute resolved paths, as text."""
    top = str(folder)
    return str(path).startswith(top if top.endswith("/") else top + "/") and path != folder


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

    def roots() -> set[Path]:
        try:
            return _hud_roots()
        except (OSError, RuntimeError):
            return set()
    return _memo(("hud_trees",), roots)


def _in_tree(text: str, trees: set[Path]) -> Path | None:
    """The HUD tree `text` lies in (or is), or one it holds within two levels
    (`rm -rf ~/.local/share/jarvis`), else None."""
    if not trees:
        return None
    return _memo(("in_tree", text, id(trees)), lambda: _in_tree_now(text, trees))


def _in_tree_now(text: str, trees: set[Path]) -> Path | None:
    if _GLOB.search(text):
        text = str(Path(*[p for p in Path(text).parts if not _GLOB.search(p)] or ["."]))
    resolved = _resolve(text)
    for tree in trees:
        if resolved == tree or _under(resolved, tree):
            return tree
        if _under(tree, resolved) and len(tree.relative_to(resolved).parts) <= 2:
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
# Every shell rules knows by name (`rules.SHELLS`; delta review of PR #75,
# M-B: `ksh93`, `rbash`, `tcsh`, `nu` … were unknown stems here, so a copy
# onto the allowlist in `uv run tcsh -c '…'` was not looked at), and `eval`.
_SHELLS = rules.SHELLS | {"eval"}
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
    env: bool = False              # a credential env file (ENV-1): certain is refused, like the allowlist

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class _Budget:
    """What one analysis shares across every level of it: the substitution
    count, and whether a write target may be resolved on the filesystem to
    judge it a credential env file (`follow_links`; off for `rules.decide`'s
    name-only reading, which has no folder to resolve in)."""
    spans: int = 0
    follow_links: bool = True
    walked: dict = field(default_factory=dict)  # readings of wrappers and runners judged


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
    return _memo(("expand", token, str(state.cwd), tuple(state.variables.items())),
                 lambda: _expand_now(token, state))


def _expand_now(token: str, state: _State) -> str | None:
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
    return _memo(("exact", text, id(protected), id(ids)), lambda: _exact_now(text, protected, ids))


def _exact_now(text: str, protected: set[Path], ids: dict) -> Path | None:
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
    return _memo(("ancestor", text, id(protected)), lambda: _ancestor_now(text, protected))


def _ancestor_now(text: str, protected: set[Path]) -> list[tuple[Path, tuple[str, ...]]]:
    if _GLOB.search(text):
        return []
    resolved = _resolve(text)
    return [(path, path.relative_to(resolved).parts)
            for path in protected if resolved != path and _under(path, resolved)]


# --- credential env files (ENV-1, 2026-10-12) ---------------------------------
#
# The owner's `.env*` rule: Jarvis never reads a credential env file and
# writes only the `*.example` ones (docs/plans/2026-10-11-env-files.md). A
# shell **write** onto one is DENY — never run, never asked, under an
# allowlist entry and under `--dangerously-skip-permissions` alike — through
# this module's own parse of where a line writes (redirects, writers'
# operands, a copier's destination, the reader hazards' files), with the
# name judged by the one rule, `secrets.is_env_credential_name`, and a
# target that exists resolved through its symlinks (`is_env_credential`).
# A write it cannot pin down — a glob that only *may* expand onto one
# (`rm *.production`), a name whose ending is an unknown variable
# (`.env.$STAGE` may be `.env.example`) — asks. Reads are the secrets
# layer's (`protected_in_command`), as for `.env` since 2026-07-30.

def _secrets():
    """Late: `jarvis.tools` imports the shell tool, which imports this module."""
    from .tools import secrets

    return secrets


_EXPANSION = re.compile(r"\$\{[^}]*\}|\$\w+|\$.")


def _env_target(operand: str, text: str | None, state: _State, verb: str) -> Touch | None:
    """The credential env file a write of `operand` lands on, or None.
    `text` is the operand expanded (None when a variable or the folder is
    unknown); `verb` heads the reason ("a redirect onto", "cp on")."""
    return _memo(("env_target", operand, text, verb, state.budget.follow_links),
                 lambda: _env_target_now(operand, text, state, verb))


def _env_target_now(operand: str, text: str | None, state: _State, verb: str) -> Touch | None:
    s = _secrets()
    shown = text if text is not None else operand
    name = os.path.basename(shown.rstrip("/")) or shown
    where = Path(shown)
    if "$" in name:
        # Only when unexpanded: try the unknown parts as nothing and as a word.
        if any(s.is_env_credential_name(_EXPANSION.sub(fill, name)) for fill in ("", "x")):
            return Touch(where, False, f"{verb} {operand}, which may be a credential env file",
                         env=True)
        return None
    if s.is_env_credential_name(name):
        return Touch(where, True, f"{verb} {name}", env=True)
    if _GLOB.search(name):
        # Only a dot-led glob can expand onto one (`secrets.env_glob_reaches`,
        # exact); `rm *.production` never reaches `.env.production` in bash.
        if s.env_glob_reaches(name):
            # Spelled from `.env`, whatever it expands to (or, matching
            # nothing, the literal name bash leaves) is a credential env file.
            return Touch(where, name.casefold().startswith(".env"),
                         f"{verb} {name}, a glob that reaches credential env files", env=True)
        return None
    if text is not None and state.budget.follow_links and _memo(
            ("env_credential", text), lambda: s.is_env_credential(text)):
        return Touch(where, True, f"{verb} {name}, which leads to a credential env file", env=True)
    return None


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


def _peel(tokens: list[str], state: _State) -> tuple[list[str], Path | None, list[str]]:
    """Strip subshell punctuation and wrappers; return the real command, any
    directory `env -C` would run it in, and the words before `unwrap`'s naive
    peel of the wrappers (what `rules.starts` reads, `_started_touches`).

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
            # `-u NAME`, `-a NAME`, `-f FILE` take the next word: `env -u ls
            # cp …` runs cp, not ls (2026-10-12).
            k += 2 if rules.takes_value("env", rest[k]) else 1
        if split is None:
            tokens = rest[k:]
            break
        try:
            words = shlex.split(split)
        except ValueError:
            words = split.split()
        tokens = ["env"] + words + rest[k:]
    return rules.unwrap(tokens), chdir, tokens


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
            name, eq, value = token[2:].partition("=")
            if name in ("command", "commands"):
                # fish's `--command`, nu's `--commands` (delta review of PR #75, M-B).
                return ([value] if eq else tokens[i:i + 1]), False
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


def _execs_a_writer(tokens: list[str]) -> bool:
    """Whether a find's `-exec`/`-execdir`/`-ok`/`-okdir` hands what it finds
    to a program that may write it (review of PR #75, HIGH 2: only the word
    right after `-exec` was looked at, so `-exec env rm {} +`, `-exec nohup
    rm`, `-exec timeout 5 rm`, `-exec uv run rm`, `-exec xargs rm` and `-exec
    sed -i … {} +` read as reads). The program is judged through rules' one
    table, as every command here is."""
    return _exec_effects(tokens)[0]


def _exec_effects(tokens: list[str]) -> tuple[bool, list[str]]:
    """(whether a find's `-exec` programs may write what it finds, the files
    they write by name). A reader run by `-exec` with a named output — `sort
    -o F`, `tree -o F`, `xxd IN F`, `uniq IN F`, `less -o F`, `grep
    --save-config=F`, a sed `w F` — writes F (delta review of PR #75, M-A:
    those were DENY on main and only asked here), behind `env`, `nohup` or
    `uv run` too; so does a wrapper's own output file (`-exec time -o F`)."""
    given, named = False, []
    for k, token in enumerate(tokens):
        if token not in rules._FIND_EXEC:
            continue
        j = k + 1
        while j < len(tokens) and tokens[j] not in (";", "+"):
            j += 1
        g, n = _given_effects(tokens[k + 1:j], 0)
        given = given or g
        named += [w for w in n if w not in named]
    return given, named


def _writes_what_it_is_given(words: list[str], hops: int) -> bool:
    """True unless this command only reads what it is handed: a reader with
    no writing form on the line, or a wrapper or runner whose every program
    is one. Anything else — a writer, a shell or interpreter, a reader that
    edits its operands (`sed -i`, `sort -o {}`), a program this module does
    not know, a command line handed to a shell, past a bound — may write."""
    return _given_effects(words, hops)[0]


def _given_effects(words: list[str], hops: int) -> tuple[bool, list[str]]:
    """(`_writes_what_it_is_given`, the files the command writes by name:
    a reader's named outputs, a wrapper's own output file)."""
    while words and re.fullmatch(r"[A-Za-z_]\w*=.*", words[0]):
        words = words[1:]
    if not words:
        return False, []
    stem = rules._basename(words[0])
    if stem in _READERS:
        hazard = rules.reader_hazard(words)
        if hazard is None:
            return False, []
        named = [w for w in hazard.writes if "{}" not in w]
        return (hazard.operands or any("{}" in w for w in hazard.writes)), named
    if hops >= rules.PROGRAM_DEPTH:
        return True, []
    try:
        started = rules.starts(words)
    except rules.TooComplex:
        return True, []
    if started is None:
        return True, []
    named = list(started.writes)
    if started.strings or started.joined:
        return True, named
    if not started.programs:
        return False, named
    given = False
    for program in started.programs:
        g, n = _given_effects(list(program), hops + 1)
        given = given or g
        named += [w for w in n if w not in named]
    return given, named


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


def _redirect_touches(segment: str, local: _State, protected: set[Path], ids: dict,
                      seen: _Seen) -> None:
    """Every protected file a redirect in this segment writes, into `seen`:
    gate state, the installed HUD's tree, and credential env files."""
    _target_touches(_redirect_targets(segment), local, protected, ids, seen, "a redirect")


def _target_touches(targets, local: _State, protected: set[Path], ids: dict, seen: _Seen,
                    what: str, certain: bool = True) -> None:
    """Every protected file one of `targets` — files something writes by name,
    as a redirect does — names, into `seen`: a certain write of a gate file or
    a credential env file spelled out, as for `tee FILE`. `what` says who
    writes (`a redirect`, `time's -o`); `certain` False when the parse that
    found them was in doubt."""
    for target in targets:
        text = _expand(target, local)
        env = _env_target(target, text, local, f"{what} onto")
        if env is not None:
            seen.touches.append(env if certain else replace(env, certain=False))
        if text is None:
            for path in protected:
                if Path(target).name == path.name:
                    seen.touches.append(Touch(path, False, f"{what} into {target}"))
            continue
        hit = _exact(text, protected, ids)
        if hit is not None:
            seen.touches.append(Touch(hit, certain, f"{what} onto {hit.name}"))
        tree = _in_tree(text, hud_trees())
        if tree is not None:
            seen.touches.append(Touch(tree, False, f"{what} into {text}", hud=True))


# `git checkout -- PATH` and `git restore PATH` overwrite PATH with a stored
# version (ENV-1 review, NIT 3): for the env rule they write their operands.
_GIT_PATH_WRITERS = frozenset({"checkout", "restore"})


def _git_subcommand(tokens: list[str]) -> str | None:
    """git's subcommand: the first word after git's own options (`-C DIR`,
    `-c K=V`, `--git-dir=…` …)."""
    i = 1
    while i < len(tokens):
        token = tokens[i]
        if token in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        return token
    return None


def _edits_in_place(stem: str, tokens: list[str]) -> bool:
    """`perl -pi`/`-i.bak` (an `i` in a short-option cluster) and `awk`/`gawk
    -i inplace` rewrite the files they are handed (ENV-1 review, NIT 3)."""
    if stem == "perl":
        for token in tokens[1:]:
            if not token.startswith("-") or token.startswith("--") or token == "-":
                continue
            for letter in token[1:]:
                if letter == "i":
                    return True
                if letter in "MmIeExCdDF0l":     # the rest of the cluster is a value
                    break
        return False
    if stem in ("awk", "gawk"):
        for k, token in enumerate(tokens[1:], 1):
            if token in ("-i", "--include") and k + 1 < len(tokens) and tokens[k + 1] == "inplace":
                return True
            if token in ("-iinplace", "--include=inplace"):
                return True
    return False


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
        # A redirect with no command still opens its target for writing:
        # `>.env.x`, or the `} > .env.x` left of `{ …; } > .env.x` (ENV-1
        # review, NIT 3).
        _redirect_touches(segment, state, protected, ids, seen)
        return seen

    # `D=~/.config/jarvis` on its own, or `export D=…`: remembered for the
    # segments after it, which is when the shell would expand it.
    words = raw[1:] if raw[0] in ("export", "declare", "local", "readonly") else raw
    if words and all(re.fullmatch(r"[A-Za-z_]\w*=.*", w) for w in words):
        _redirect_touches(segment, state, protected, ids, seen)
        for word in words:
            name, value = word.split("=", 1)
            expanded = _expand(value, state) if value else ""
            if expanded is not None:
                state.variables[name] = expanded
            else:
                state.variables.pop(name, None)
        return seen

    tokens, chdir, lead = _peel(raw, state)
    if not tokens:
        _redirect_touches(segment, state, protected, ids, seen)
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
    _redirect_touches(segment, local, protected, ids, seen)
    # `env` (and `builtin`) are peeled here rather than walked through the
    # table: what they start is still behind a wrapper (M-B).
    bare = list(words)
    while bare and re.fullmatch(r"[A-Za-z_]\w*=.*", bare[0]):
        bare.pop(0)
    _command(tokens, lead, parsed, segment, local, state, protected, ids, depth, seen, 0,
             wrapped=len(lead) < len(bare))
    return seen


def _started_touches(lead: list[str], parsed: bool, segment: str, local: _State, state: _State,
                     protected: set[Path], ids: dict, depth: int, seen: _Seen, hops: int) -> bool:
    """What the wrapper or runner at the front of `lead` starts — read from
    rules' one table (`rules.starts`) — each judged as a command of its own,
    into `seen`; a command line it hands a shell (`npm exec -c`, `bun exec`,
    pnpm's shell mode, `watch`) as a line (2026-10-12). `unwrap`'s naive peel
    read `env -u ls cp …` as `ls`, and nothing read past `uv run`, `npx` or
    `npm exec -c`, so a copy onto the allowlist behind one was a read or an
    unknown. A reading that is only one of several (an option the table does
    not know) is not provable: its writes are uncertain. False when the bound
    was hit (the caller stops, as `_nested` does).

    Bounded by the line's work budget (`rules.charge`; review of PR #75: an
    unknown option offered n readings per level and each was walked again,
    `npx --x ` sixty times took 139 s here), each reading walked once; past
    the budget, a cap or `PROGRAM_DEPTH` the line is too complex to judge —
    an opaque touch, which asks and is never covered."""
    if not lead or (rules._basename(lead[0]) not in rules._WRAPPER_OPTS
                    and rules._basename(lead[0]) not in rules._RUNNER_SPECS):
        return True
    try:
        rules.charge(len(lead))
        started = rules.starts(lead)
    except rules.TooComplex:
        seen.touches.append(_opaque("what its wrappers and runners start is too complex to judge"))
        return False
    if started is not None and started.writes:
        # A file the wrapper itself writes — `time -o FILE`, `script FILE`,
        # `systemd-run -p StandardOutput=file:FILE` — is a write, as `tee
        # FILE` is (delta review of PR #75, H-B: `time -o <allowlist> -f
        # '<entries>' true` ran unasked; the table had made `-o`'s value an
        # option value, where the old peel had read it as the program).
        stem_of = rules._basename(lead[0])
        _target_touches(started.writes, local, protected, ids, seen, f"{stem_of}'s output file",
                        certain=started.writes_certain)
        seen.writes = True
    if started is None or not (started.programs or started.strings):
        return True
    if hops >= rules.PROGRAM_DEPTH:
        seen.touches.append(_opaque("wrappers and runners nested too deeply to judge"))
        return False
    inner = _Seen()
    lines = list(started.strings) + [" ".join(p) for p in started.programs if started.joined]
    if lines and not _nested(lines, local, state, protected, ids, depth, inner):
        seen.touches += inner.touches
        return False
    walked = local.budget.walked
    for program in started.programs if not started.joined else ():
        # A reading met again (another reading of an unknown option offers
        # the same tail) gives what it gave before; it is not walked again.
        # **Keyed on the segment's whole text** (delta review of PR #75, H-A):
        # a reading takes more from its segment than its own words — a shell
        # reads its here-string from it — and a key without it judged `uv run
        # sh <<< '<a write onto the allowlist>'` as the `uv run sh <<< 'true'`
        # before it, which ran unasked. Every input a reading takes is in the
        # key: the segment, how it parsed, the depth, the folder, the variables.
        key = (segment, parsed, depth, program, hops + 1, local.cwd,
               tuple(sorted(local.variables.items())))
        one = walked.get(key)
        if one is None:
            try:
                rules.charge(len(program))      # judging a reading costs its words
            except rules.TooComplex:
                seen.touches += inner.touches
                seen.touches.append(_opaque("what its wrappers and runners start is too complex "
                                            "to judge"))
                return False
            one = _Seen()
            words, chdir, more = _peel(list(program), local)
            if words:
                here = _State(chdir if chdir is not None else local.cwd, local.variables,
                              local.budget)
                _command(words, more, parsed, segment, here, state, protected, ids, depth, one,
                         hops + 1)
            walked[key] = one
        inner.touches += one.touches
        inner.writes = inner.writes or one.writes
        inner.read |= one.read
    if not started.sure:
        inner.touches = [replace(t, certain=False) for t in inner.touches]
    seen.touches += inner.touches
    seen.writes = seen.writes or inner.writes
    seen.read |= inner.read
    return True


def _command(tokens: list[str], lead: list[str], parsed: bool, segment: str, local: _State,
             state: _State, protected: set[Path], ids: dict, depth: int, seen: _Seen,
             hops: int, wrapped: bool = False) -> None:
    """One command — `tokens`, its wrappers peeled by `unwrap`; `lead`, the
    same words before that peel — into `seen`. Redirections are the
    segment's, judged once by the caller."""
    stem = rules._basename(tokens[0])
    if not _started_touches(lead, parsed, segment, local, state, protected, ids, depth, seen,
                            hops):
        return

    # A shell handed a string is a command line in its own right — from `-c`
    # (in any cluster), `eval`, or a here-string on its standard input.
    if stem in _SHELLS:
        strings, stdin = _shell_strings(tokens)
        inline = bool(strings)
        if stdin:
            strings += _here_strings(segment)
        if strings:
            if not _nested(strings, local, state, protected, ids, depth, seen):
                return
            if inline:
                return
    elif (hops > 0 or wrapped) and not rules.known_program(stem):
        # A program no table knows, started by a wrapper or a runner and
        # handed `-c`/`-e`/`--command STRING`, may be a shell this module
        # does not know by name: its string is a command line too (delta
        # review of PR #75, M-B).
        strings = rules.command_strings(tokens)
        if strings and not _nested(strings, local, state, protected, ids, depth, seen):
            return

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
        return
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
    # A reader in a writing form writes only what it writes (PR #24's
    # follow-up 1): every operand for `sed -i` and `find -delete`, or for
    # `find -exec` of a writing program, a shell or an interpreter (code
    # handed a file is a write here, as everywhere in this module); a named
    # output (`sort -o F`, a sed `w F`, `uniq IN OUT`). One that only runs
    # something (`grep --pager`, `rg --pre`, `find -exec cat`), writes some
    # other file (`less --save-marks`), or was matched by an abbreviation
    # another implementation reads differently (`grep --p`) writes nothing
    # provable: what it is given is an uncertain touch — asked, never refused
    # unasked.
    exec_given, exec_named = (_exec_effects(tokens) if stem in ("find", "bfs")
                              else (False, []))
    partial = (hazard is not None and stem not in _WRITERS | _INTERPRETERS | _SHELLS
               and not hazard.operands and not exec_given)
    named = set(hazard.writes) if hazard is not None else set()
    # What a find's `-exec` program writes by name is find's write too, and a
    # provable one (`-exec sort -o F {} \;`; delta review of PR #75, M-A).
    named |= set(exec_named)
    written = written + [w for w in exec_named if w not in written]

    def provable(operand: str) -> bool:
        return not partial or operand in named

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

    # Credential env files (ENV-1): only what a writing program is handed —
    # a writer's operands, a reader's writing form (`sed -i`, `sort -o`, a
    # sed `w FILE`), a copier's destination. An interpreter or an unknown
    # program handed one is not judged a writer of it here: the secrets layer
    # refuses any line naming one, and "writes a credential env file" would
    # misdescribe `node --env-file=.env.production server.js`. Three that
    # rewrite their operands without a writing stem count too: `git checkout
    # -- PATH`/`git restore PATH`, `perl -pi`, `awk -i inplace`.
    env_writer = (stem in _WRITERS or hazard is not None or _edits_in_place(stem, tokens)
                  or (stem == "git" and _git_subcommand(tokens) in _GIT_PATH_WRITERS))
    if env_writer and split is not None and read_only and written:
        # `cp /tmp/.env.production dir/` writes `dir/.env.production`: a
        # source named like one lands under its own name in a folder.
        dest = written[0]
        dest_text = _expand(dest, local)
        into = (dest.endswith("/") or os.path.basename(dest) in (".", "..")
                or (dest_text is not None and local.budget.follow_links
                    and os.path.isdir(dest_text)))
        if into:
            for source in read_only:
                env = _env_target(os.path.basename(source.rstrip("/")) or source, None, local,
                                  f"{stem} into {dest}:")
                if env is not None:
                    seen.touches.append(env)
    everyone = written + read_only
    for operand in written:
        text = _expand(operand, local)
        if env_writer:
            env = _env_target(operand, text, local, f"{stem} on")
            if env is not None:
                seen.touches.append(env if provable(operand) else replace(env, certain=False))
        if text is None:
            # `$UNSET/allowlist.json`, or a relative name after a `cd` that
            # could not be followed: the right name, an unknowable place.
            for path in protected:
                if Path(operand).name == path.name:
                    seen.touches.append(Touch(path, False, f"{stem} given {operand}"))
            continue
        hit = _exact(text, protected, ids)
        if hit is not None:
            seen.touches.append(Touch(hit, kind == "writer" and provable(operand),
                                      f"{stem} on {hit.name}"))
            continue
        tree = _in_tree(text, hud_trees())
        if tree is not None and kind != "reader":
            seen.touches.append(Touch(tree, False, f"{stem} on {text}", hud=True))
            continue
        ancestors = _ancestor_of(text, protected)
        if not ancestors:
            continue        # (the names below cost O(operands) each: only when needed)
        others = [o for o in everyone if o is not operand]
        other_names = {rules._basename(o.rstrip("/")) for o in others}
        for path, rel in ancestors:
            first = rel[0]
            if first in other_names or (stem == "find" and path.name in other_names):
                # `cp /tmp/x/allowlist.json ~/.config/jarvis/`, or `find
                # ~/.config/jarvis -name allowlist.json -exec cp …`.
                lands = (kind == "writer" and (len(rel) == 1 or stem == "find")
                         and provable(operand))
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
    if "\x00" in command:
        # No shell can be handed it, and resolving a path with one raises:
        # too complex to judge, never a ValueError out of the gate.
        return [_opaque("the line holds a NUL byte, which no shell can be handed")]
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
    with rules.work_scope():
        return _analyse(command, _State(base), protected, _identities(protected)).touches


def command_touch(command: str, cwd: str | Path | None = None) -> Touch | None:
    """The most serious protected file this line reaches, or None.

    Order: a certain write to the allowlist or to a credential env file (both
    refused, `denies`), any other certain write, then the uncertain ones in
    the same order.
    """
    touches = command_touches(command, cwd)
    if not touches:
        return None
    gate = gate_paths()
    return min(touches, key=lambda t: (not t.certain, not (t.env or t.path in gate)))


def denies(touch: Touch | None) -> bool:
    """True for a touch no one may approve: a write that provably lands on
    the allowlist, or on a credential env file (ENV-1). The one test v1's
    gate, `run_command` and v2's layer 1 share."""
    return (touch is not None and touch.certain
            and (touch.env or touch.path in gate_paths()))


def refused(command: str, cwd: str | Path | None = None) -> Touch | None:
    """The touch that makes this line unrunnable by Jarvis, or None: a write
    that provably lands on the allowlist or on a credential env file.
    Everything else this module sees is an ASK, decided by the caller."""
    touch = command_touch(command, cwd)
    return touch if denies(touch) else None


def refusal(touch: Touch) -> str:
    if touch.env:
        return (f"{touch.why}: a credential env file is the owner's, edited in the HUD's "
                "editor; Jarvis never reads or writes one, asked or not (only names ending "
                "in .example, like .env.example, are Jarvis's to write)")
    return (f"{touch.why}: {touch.name} is the approval gate's own allowlist, and "
            "a command that writes it would decide what runs without anyone "
            "being asked. Only the owner changes it, by hand")


def env_write_refusal(command: str) -> str | None:
    """`rules.decide`'s reading (ENV-1): the refusal for a line that provably
    writes a credential env file **by name** — no folder, no filesystem, so
    the same line gets the same verdict wherever it is judged — else None.
    A write behind a symlink is seen where the folder is known
    (`command_touches(command, cwd)`: v1's `static_verdict`, `run_command`,
    v2's layer 1 with the brief's folder)."""
    if "\x00" in (command or ""):
        return None         # `rules.decide` refuses such a line before asking this
    text = _ansi_c(rules.join_continuations(command or ""))
    # Cheap first: a line that cannot spell `.env` once its quotes and
    # backslashes are gone names no env file (`.e''nv` and `.e\nv` do).
    if ".env" not in text.casefold().translate(_UNQUOTE):
        return None
    state = _State(Path("/"), budget=_Budget(follow_links=False))
    with rules.work_scope():
        touches = [t for t in _analyse(command, state, set(), {}).touches if t.env and t.certain]
    return refusal(touches[0]) if touches else None


_UNQUOTE = str.maketrans("", "", "'\"\\")


def ask_reason(touch: Touch) -> str:
    if touch.env:
        return (f"{touch.why}: this may write a credential env file, which only the owner "
                "changes; it never runs unasked and no allowlist entry covers it")
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
#     `-u`), `builtin`, and every wrapper and runner in rules' one table
#     (`rules.starts`: `nohup`, `timeout`, `nice`, `flock FILE`, `xargs`,
#     `systemd-run`, `uv run`, `uvx`, `npx`, `npm exec`/`-c`, `pnpm exec`/
#     `dlx`, `yarn exec`/`dlx`, `bun x`/`exec`, `poetry/pdm/hatch/rye/pipx
#     run`, and `ssh HOST …`, whose host may be this one) — is `jarvis` (any
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
                            ("restart", "_restart", "rebuild", "pull", "npm_ci", "systemctl",
                             "hand_off")),
}

# The daemon's systemd unit, changed directly (2026-10-11): `systemctl
# --user restart jarvis-daemon2` was ALLOW in `rules.decide` — run with nobody
# asked on every human-backed surface — and `stop` ends it for good (a clean
# exit is not one `Restart=on-failure` restarts).
#
# DENY: a changing verb whose units may be the daemon's — named, globbed,
# brace-expanded, behind a line-local `$VAR`, or an expansion only known at
# run time; any `.slice`/`.target` of the user manager (the unit has
# Requires= on app.slice and basic.target, so stopping, restarting, killing
# or freezing either reaches it, and starting exit.target or shutdown.target
# stops it); `--marked` (it restarts whatever was marked); the user manager's
# `exit`/`isolate`/`default`/`rescue`/… ; and the same through D-Bus
# (`busctl`, `dbus-send`, `gdbus`, `qdbus`). ASK, never auto-approved:
# `set-environment`, `unset-environment`, `import-environment` (the daemon
# reads that environment at its next start). Neither: `reset-failed` (a
# recovery verb that stops nothing) and every read.
#
# Killing the daemon's process is the kill rule's, below (the owner's
# decision on review M2): a pattern that matches its command line at
# `daemon2` is DENY; a literal `kill <pid>` and kills by process name are not
# seen. `pkill -9 -f 'jarvis daemon2'` is in effect a restart
# (Restart=on-failure brings it back in 5 s); a plain SIGTERM stops it until
# `jarvis restart`.
DAEMON_UNIT = "jarvis-daemon2"
_DAEMON_UNIT_NAMES = (DAEMON_UNIT, DAEMON_UNIT + ".service")
# The prefilter's words: a line that changes the unit names one of these (or
# `daemon2`, an op word, which every spelling of the unit holds).
_DAEMON_WORDS = ("systemctl", "busctl", "dbus-send", "gdbus", "qdbus")
# The slices and targets a glob is tried against: what the live unit requires
# (`systemctl --user list-dependencies jarvis-daemon2`: app.slice, -.slice,
# basic.target), what conflicts with it, and the user manager's standard
# targets. A literal `.slice`/`.target` needs no list — any one counts.
_REACHES_DAEMON = ("app.slice", "-.slice", "background.slice", "session.slice", "basic.target",
                   "default.target", "exit.target", "shutdown.target", "sockets.target",
                   "paths.target", "timers.target")

# systemctl's own option grammar, systemd 259 — every option in `systemctl
# --help`, the compatibility flags it hides, and the getopt string in
# /usr/bin/systemctl (`hC:t:p:P:alqvfs:H:M:n:o:iTr.::`). Each long option was
# checked against the installed binary with `systemctl --user --NAME
# --version` (2026-10-11, review of PR #48): a flag prints the version, a
# value-taking option swallows it. Parsed the way getopt_long parses it —
# short clusters whose last value-taking letter takes the rest or the next
# word (`-qp status restart …` was read as the verb `status`), unique prefixes
# of long names (`--us` is `--user`), options anywhere (permutation), `--`.
# 1 takes a value; 2 takes an optional one, glued only (`-.`, which is why
# `-.slice` needs a `--` before it).
_SYSTEMCTL_SHORT = {"C": 1, "t": 1, "p": 1, "P": 1, "s": 1, "H": 1, "M": 1, "n": 1, "o": 1,
                    ".": 2, "h": 0, "a": 0, "l": 0, "q": 0, "v": 0, "f": 0, "i": 0, "T": 0, "r": 0}
_SYSTEMCTL_LONG = {
    **dict.fromkeys(("capsule", "host", "machine", "type", "state", "property", "job-mode",
                     "check-inhibitors", "signal", "kill-whom", "kill-value", "kill-subgroup",
                     "what", "message", "preset-mode", "root", "image", "image-policy", "lines",
                     "output", "boot-loader-menu", "boot-loader-entry", "reboot-argument",
                     "timestamp", "when", "drop-in", "legend"), 1),
    **dict.fromkeys(("help", "version", "system", "user", "all", "failed", "full", "reverse",
                     "before", "after", "with-dependencies", "show-transaction", "show-types",
                     "value", "wait", "no-block", "no-wall", "dry-run", "quiet", "verbose",
                     "no-warn", "no-reload", "no-pager", "no-ask-password", "global", "runtime",
                     "force", "plain", "recursive", "firmware-setup", "now", "read-only", "mkdir",
                     "marked", "stdin", "no-legend", "fail", "irreversible", "ignore-dependencies",
                     "ignore-inhibitors"), 0),
}
_SYSTEMCTL_SHORT_LONG = {"h": "help", "a": "all", "l": "full", "q": "quiet", "v": "verbose",
                         "f": "force", "r": "recursive", "T": "show-transaction", "C": "capsule"}
# Verbs that only read.
_SYSTEMCTL_READS = frozenset({
    "status", "show", "cat", "help", "is-active", "is-enabled", "is-failed", "list-units",
    "list-unit-files", "list-dependencies", "list-jobs", "list-timers", "list-sockets",
    "list-paths", "list-automounts", "list-machines", "show-environment", "get-default",
    "is-system-running", "whoami", "log-level", "log-target", "service-watchdogs"})
# Verbs that change state but stop nothing: clearing a failed state is how a
# unit at its start limit is recovered.
_SYSTEMCTL_HARMLESS = frozenset({"reset-failed", "daemon-reload"})
# The manager's environment, which the daemon reads at its next start: asked.
_SYSTEMCTL_ENVIRONMENT = frozenset({"set-environment", "unset-environment", "import-environment"})
# Verbs that stop every unit of the *user* manager, the daemon's among them.
_SYSTEMCTL_USER_STOPS_ALL = frozenset({"exit", "isolate", "default", "rescue", "emergency", "halt",
                                       "poweroff", "reboot", "kexec", "soft-reboot", "switch-root"})
# D-Bus methods of systemd's manager and unit objects that change a unit or
# the manager (`busctl`, `dbus-send`, `gdbus`, `qdbus`).
_SYSTEMD_METHODS = frozenset({
    "StartUnit", "StartUnitReplace", "StartUnitWithFlags", "StopUnit", "ReloadUnit",
    "RestartUnit", "TryRestartUnit", "ReloadOrRestartUnit", "ReloadOrTryRestartUnit",
    "EnqueueUnitJob", "KillUnit", "QueueSignalUnit", "CleanUnit", "FreezeUnit", "ThawUnit",
    "SetUnitProperties", "BindMountUnit", "MountImageUnit", "EnqueueMarkedJobs", "Exit",
    "Reboot", "SoftReboot", "PowerOff", "Halt", "KExec", "SwitchRoot", "SetEnvironment",
    "UnsetEnvironment", "UnsetAndSetEnvironment", "DisableUnitFiles", "DisableUnitFilesWithFlags",
    "DisableUnitFilesWithFlagsAndInstallInfo", "MaskUnitFiles", "LinkUnitFiles",
    "RevertUnitFiles", "SetDefaultTarget", "PresetAllUnitFiles", "AttachProcessesToUnit",
    "AbandonScope", "Reexecute", "Start", "Stop", "Reload", "Restart", "TryRestart",
    "ReloadOrRestart", "ReloadOrTryRestart", "EnqueueJob", "Kill", "QueueSignal", "Clean",
    "Freeze", "Thaw", "SetProperties"})
_DBUS_TOOLS = frozenset({"busctl", "dbus-send", "gdbus", "qdbus", "qdbus6", "qdbus-qt5",
                         "qdbus-qt6"})
_DAEMON_UNIT_WHY = (
    "this {how} — the v2 daemon, the process that runs Jarvis's agents and holds the owner's "
    "pending approvals, Jarvis's own control plane. Only the owner stops or restarts it, from "
    "their own terminal (`jarvis restart`); Jarvis never does, asked or not")
_BRACE_CAP = 256


_BRACE = re.compile(r"(?<!\$)\{([^{}]*)\}")


def _brace_expand(word: str) -> list[str] | None:
    """Bash's brace expansion of one word — `{a,b}` lists (nested) and
    `{1..3}`/`{a..e}` sequences, never `${…}` — in bash's order, or None past
    `_BRACE_CAP` words or expansions."""
    if "{" not in word:
        return [word]
    pending, done, steps = [word], [], 0
    while pending:
        current = pending.pop()
        found = next(((m, parts) for m in _BRACE.finditer(current)
                      if (parts := _brace_parts(m.group(1))) is not None), None)
        if found is None:
            done.append(current)
            continue
        m, parts = found
        steps += 1
        if steps > _BRACE_CAP or len(pending) + len(done) + len(parts) > _BRACE_CAP:
            return None
        head, tail = current[:m.start()], current[m.end():]
        pending.extend(head + part + tail for part in reversed(parts))
    return done


def _brace_parts(inside: str) -> list[str] | None:
    """The alternatives one `{…}` (with no braces inside) expands to, or None
    when bash leaves it as it is (no comma and no sequence)."""
    if "," in inside:
        return inside.split(",")
    m = re.fullmatch(r"(-?\d+)\.\.(-?\d+)(?:\.\.(-?\d+))?", inside)
    if m:
        start, end = int(m.group(1)), int(m.group(2))
        step = abs(int(m.group(3) or 1)) or 1
        if abs(end - start) // step > _BRACE_CAP:
            return [str(start)] * (_BRACE_CAP + 1)
        rng = range(start, end + 1, step) if start <= end else range(start, end - 1, -step)
        return [str(n) for n in rng]
    m = re.fullmatch(r"([A-Za-z])\.\.([A-Za-z])(?:\.\.(-?\d+))?", inside)
    if m:
        start, end = ord(m.group(1)), ord(m.group(2))
        step = abs(int(m.group(3) or 1)) or 1
        rng = range(start, end + 1, step) if start <= end else range(start, end - 1, -step)
        return [chr(n) for n in rng]
    return None


# A brace, or a comma, that was quoted or escaped on the line: bash never
# expands it, so `pkill -f 'daemo.{0,1}2'` hands pkill that regex whole
# (review 2 of PR #48 — expanded, it was judged as `daemo.02` and `daemo.12`,
# and one pattern could become 256 to match). `_owner_line` marks them
# before splitting a segment into words, `_unit_words` expands only the
# unmarked ones, and `_owner_command` hands every other path the plain text.
_QUOTED = str.maketrans({"{": "", "}": "", ",": ""})
_UNQUOTED = str.maketrans({"": "{", "": "}", "": ","})


def _mark_quoted(text: str) -> str:
    """`text` with each quoted or backslash-escaped `{`, `}` and `,` marked."""
    if "{" not in text:
        return text
    out, quote, i, n = [], "", 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\" and quote != "'" and i + 1 < n:
            out.append(c + text[i + 1].translate(_QUOTED))
            i += 2
            continue
        if quote:
            if c == quote:
                quote = ""
            out.append(c if c == '"' or c == "'" else c.translate(_QUOTED))
        else:
            if c in "'\"":
                quote = c
            out.append(c)
        i += 1
    return "".join(out)


def _unmark(word: str) -> str:
    return word.translate(_UNQUOTED)


def _unit_words(tokens: list[str], state: "_Owner") -> list[str] | None:
    """A command's arguments as the shell will hand them over: the line's own
    `$VAR`s put in and split on whitespace (an expansion it did not assign
    stays as it is, and counts as any words), unquoted braces expanded
    (`_mark_quoted`). None past the brace cap."""
    out: list[str] = []
    for token in tokens:
        word = _owner_word(token, state, environ=False)
        expanded = _brace_expand(word)
        if expanded is None:
            return None
        for one in expanded:
            one = _unmark(one)
            out.extend(one.split() if word != token else [one])
    return out


def _wild_word(word: str) -> bool:
    """A word only known when it runs: an expansion the line did not assign
    (it may split into several words, or vanish)."""
    return "$" in word or "`" in word


def _may_be_daemon(word: str, user: bool) -> bool:
    """Whether a unit operand may name the daemon's unit, or (in the user
    manager) a slice or target that reaches it. A glob can only become names
    it matches, so it is tried against them."""
    if _wild_word(word):
        return True
    base = rules._basename(word.rstrip("/")) or word
    if _GLOB.search(word):
        names = _DAEMON_UNIT_NAMES + (_REACHES_DAEMON if user else ())
        return any(fnmatch(name, pattern) for name in names for pattern in {word, base})
    if base in _DAEMON_UNIT_NAMES:
        return True
    return user and base.endswith((".slice", ".target"))


@dataclass
class _Systemctl:
    operands: list[str]
    user: bool = False        # the user manager (`--user`, `--capsule`)
    marked: bool = False
    unknown: bool = False     # an option the table does not know: the verb is a guess
    wild_head: bool = False   # an expansion before or at the verb: the verb is not known
    info: bool = False        # --help/--version: nothing runs
    properties: list[str] = field(default_factory=list)   # `-p`/`-P`/`--property` values

    @property
    def verb(self) -> str | None:
        return self.operands[0] if self.operands else None

    @property
    def units(self) -> list[str]:
        return self.operands[1:]


def _systemctl_parse(words: list[str]) -> _Systemctl:
    """systemctl's arguments as getopt_long reads them (`_SYSTEMCTL_LONG`)."""
    out = _Systemctl([])
    i = 0
    while i < len(words):
        word = words[i]
        i += 1
        if not out.operands and _wild_word(word):
            out.wild_head = True
        if word == "--":
            if not out.operands and any(_wild_word(w) for w in words[i:i + 1]):
                out.wild_head = True
            out.operands.extend(words[i:])
            break
        if word.startswith("--"):
            name, eq, _ = word[2:].partition("=")
            exact = [n for n in _SYSTEMCTL_LONG if n == name]
            matches = exact or [n for n in _SYSTEMCTL_LONG if name and n.startswith(name)]
            if len(matches) != 1:
                out.unknown = True
                continue
            full = matches[0]
            value = word[2:].partition("=")[2]
            if _SYSTEMCTL_LONG[full] and not eq:
                if i >= len(words):
                    out.unknown = True
                    break
                if not out.operands and _wild_word(words[i]):
                    out.wild_head = True
                value = words[i]
                i += 1
            elif eq and not _SYSTEMCTL_LONG[full]:
                out.unknown = True
            if full == "property":
                out.properties.extend(value.split(","))
            out.user = {"user": True, "capsule": True, "system": False}.get(full, out.user)
            out.marked = out.marked or full == "marked"
            out.info = out.info or full in ("help", "version")
            continue
        if word.startswith("-") and len(word) > 1:
            letters = word[1:]
            for k, letter in enumerate(letters):
                takes = _SYSTEMCTL_SHORT.get(letter)
                if takes is None:
                    out.unknown = True
                    break
                full = _SYSTEMCTL_SHORT_LONG.get(letter, "")
                out.user = out.user or full == "capsule"
                out.info = out.info or full == "help"
                if takes == 2:
                    break
                if takes == 1:
                    value = letters[k + 1:]
                    if k == len(letters) - 1:
                        if i >= len(words):
                            out.unknown = True
                        elif not out.operands and _wild_word(words[i]):
                            out.wild_head = True
                        value = words[i] if i < len(words) else ""
                        i += 1
                    if letter in "pP":
                        out.properties.extend(value.split(","))
                    break
            continue
        out.operands.append(word)
    return out


def _systemctl_verdict(tokens: list[str], appends: bool, state: "_Owner") -> tuple[str, str] | None:
    """("deny" | "ask", why) for a `systemctl` command, or None."""
    words = _unit_words(tokens[1:], state)
    if words is None:
        return "deny", _DAEMON_UNIT_WHY.format(
            how=f"runs systemctl with more brace expansions than are judged here, "
                f"{DAEMON_UNIT} perhaps among them")
    parsed = _systemctl_parse(words)
    if parsed.wild_head:
        # `systemctl --user $ARGS`: the verb, and its units, are only known
        # when it runs.
        return "deny", _DAEMON_UNIT_WHY.format(
            how=f"runs systemctl with a verb only known when it runs (an expansion), "
                f"which may change {DAEMON_UNIT}")
    if parsed.info and not parsed.unknown:
        return None
    verb = parsed.verb
    if parsed.unknown or (verb is not None and _GLOB.search(verb)):
        # An option the table does not know (it may have taken a value, so
        # any operand may be the verb), or a verb a glob stands for: judge
        # every operand as the verb and as a unit.
        risky = any(_may_be_daemon(w, True) or any(fnmatch(v, w) for v in _SYSTEMCTL_USER_STOPS_ALL)
                    for w in parsed.operands)
        if risky or appends:
            return "deny", _DAEMON_UNIT_WHY.format(
                how=f"runs systemctl with an option or verb not known here, on words that may "
                    f"name {DAEMON_UNIT}")
        return None
    if verb is None:
        if appends:
            return "deny", _DAEMON_UNIT_WHY.format(
                how=f"runs systemctl with a verb and units its input names (`xargs`), "
                    f"{DAEMON_UNIT} perhaps")
        return None
    if verb in _SYSTEMCTL_READS or verb in _SYSTEMCTL_HARMLESS:
        return None
    if verb in _SYSTEMCTL_ENVIRONMENT:
        return "ask", (f"`systemctl {verb}` changes the service manager's environment, which "
                       f"{DAEMON_UNIT} reads at its next start")
    if parsed.user and verb in _SYSTEMCTL_USER_STOPS_ALL:
        return "deny", _DAEMON_UNIT_WHY.format(
            how=f"stops every user unit (`systemctl --user {verb}`), {DAEMON_UNIT} among them")
    if parsed.marked:
        return "deny", _DAEMON_UNIT_WHY.format(
            how=f"runs `systemctl {verb} --marked`, which acts on every marked unit, "
                f"{DAEMON_UNIT} perhaps")
    hit = next((u for u in parsed.units if _may_be_daemon(u, parsed.user)), None)
    if hit is not None:
        if _GLOB.search(hit) and not _wild_word(hit):
            how = (f"changes the units matching {hit} (`systemctl {verb}`), which may be "
                   f"{DAEMON_UNIT} or a slice or target it requires")
        elif rules._basename(hit) in _DAEMON_UNIT_NAMES or _wild_word(hit):
            how = f"changes systemd's {DAEMON_UNIT} (`systemctl {verb}`)"
        else:
            how = (f"changes the user manager's {rules._basename(hit)} (`systemctl {verb}`), "
                   f"which {DAEMON_UNIT} requires or which stops it")
        return "deny", _DAEMON_UNIT_WHY.format(how=how)
    if appends:
        return "deny", _DAEMON_UNIT_WHY.format(
            how=f"runs `systemctl {verb}` on units its input names (`xargs`), {DAEMON_UNIT} perhaps")
    return None


def _systemctl_op(tokens: list[str], appends: bool = False, state: "_Owner | None" = None) -> str | None:
    """The refusal for a `systemctl` command that may change the daemon's
    unit, or None (an ask is `systemctl_asks`'s)."""
    verdict = _systemctl_verdict(tokens, appends, state if state is not None else _Owner())
    return verdict[1] if verdict and verdict[0] == "deny" else None


def systemctl_asks(tokens: list[str]) -> str | None:
    """Why a `systemctl` command must be asked although `systemctl --user` is
    otherwise auto-approved (`set-environment` and its kin), or None.
    `rules.decide` asks it."""
    try:
        verdict = _systemctl_verdict(list(tokens), False, _Owner())
    except Exception:     # noqa: BLE001 — a parse failure is not a verdict
        return None
    return verdict[1] if verdict and verdict[0] == "ask" else None


def _dbus_op(tokens: list[str], state: "_Owner") -> str | None:
    """A D-Bus call (`busctl`, `dbus-send`, `gdbus`, `qdbus`) that names the
    daemon's unit, or that calls systemd with a method that changes a unit
    or the manager. A read (`busctl get-property`, a `Get`/`GetAll` call …)
    changes nothing (review 2 of PR #48); one that names the unit prints its
    PID, a source for a fed `kill` (`_note_kill`)."""
    words = _unit_words(tokens[1:], state)
    if words is None:
        words = tokens[1:]
    escaped = DAEMON_UNIT.replace("-", "_2d")
    names = any(DAEMON_UNIT in word or escaped in word for word in words)
    tool = rules._basename(tokens[0])
    if _dbus_read("qdbus" if tool.startswith("qdbus") else tool, words):
        if names:
            state.kills.sources.append(lambda: "a D-Bus read of its unit")
        return None
    if names:
        return _DAEMON_UNIT_WHY.format(how=f"calls systemd over D-Bus on {DAEMON_UNIT}")
    systemd = any("systemd1" in w for w in words)
    if not systemd:
        return None
    for word in words:
        parts = set(re.split(r"[.=:/\s]+", word))
        if parts & _SYSTEMD_METHODS or _wild_word(word):
            method = next(iter(parts & _SYSTEMD_METHODS), "a method only known when it runs")
            return _DAEMON_UNIT_WHY.format(
                how=f"calls systemd over D-Bus ({method}), which can stop or restart "
                    f"{DAEMON_UNIT}")
    return None


# Killing the daemon by pattern (2026-10-11, the owner's decision on PR #48's
# M2): `pkill -f daemon2` stopped it with nobody asked — a plain SIGTERM is a
# clean exit, which `Restart=on-failure` does not restart. DENY:
#   * `pkill -f` and `killall -r` whose pattern matches one of the daemon's
#     command lines **at `daemon2`** — a match whose span overlaps that word —
#     so `jarvis daemon2`, `daemo[n]2`, `daemon.`, `jarvis d` and `n2$` are
#     refused, while `jarvis` and `python -m jarvis` alone, which do not reach
#     the word, keep the owner's 2026-08-09 matrix (`pkill -f jarvis` stays
#     ALLOW);
#   * a `kill` **fed** PIDs on the same line — through `$(…)`, backticks,
#     `<(…)`, a variable or `xargs` — by a source that yields the daemon's:
#     `pgrep -f`/`pidof` with such a pattern, `ps` (or a grep over
#     `/proc/*/cmdline` itself) filtered by such a `grep`, and (review 2 of
#     PR #48) `systemctl show|status` of its unit, a D-Bus read of it, its
#     cgroup's `cgroup.procs`, `pgrep --cgroup|--pidfile` naming the unit,
#     `lsof`/`fuser` on one of its ports, and `ss -p`/`netstat -p` filtered
#     onto one;
#   * `pkill --cgroup|--pidfile` naming the unit, and `fuser -k` on one of its
#     ports, which kill it outright.
# A pattern only known when it runs counts as matching, and so does one that
# will not compile on a line naming `daemon2`. One whose matching cost
# cannot be bounded (`_regex_work`) is never matched: it is too complex to
# judge (`COSTLY`), which asks — never DENY (the delta review of PR #48) —
# unless a literal it needs rules the daemon out first (`_required_runs`).
# **A source is judged only when a fed `kill` is on the line**:
# `pgrep -f daemon2` alone is a read and is never matched, and `pgrep -f
# daemon2 && kill -9 4321` kills a literal PID nothing on the line fed it.
# Matching by process *name* (`pkill python`, `killall python`, `pkill`
# without `-f`) reaches the daemon too, but never at `daemon2` — its names are
# `python` and `jarvis` — so it is left alone, the stated limit with a `kill`
# of a literal PID, which nothing here can see.
_KILL_WORDS = ("kill", "pgrep", "pidof", "fuser")
_DAEMON_NAMES = ("python", "python3", "jarvis")
_SIGNALS = frozenset({
    "HUP", "INT", "QUIT", "ILL", "TRAP", "ABRT", "IOT", "BUS", "FPE", "KILL", "USR1", "SEGV", "USR2",
    "PIPE", "ALRM", "TERM", "STKFLT", "CHLD", "CLD", "CONT", "STOP", "TSTP", "TTIN", "TTOU", "URG",
    "XCPU", "XFSZ", "VTALRM", "PROF", "WINCH", "POLL", "IO", "PWR", "SYS", "UNUSED", "RTMIN",
    "RTMAX"})
# pkill's and pgrep's options (procps-ng 4.0.4, `--help`, and which letters
# each binary refuses): 1 takes a value.
_PKILL_SHORT = {**dict.fromkeys("qgGOPstuUFr", 1), **dict.fromkeys("HecfinoxLAhV", 0)}
_PGREP_SHORT = {**dict.fromkeys("dgGOPstuUFr", 1), **dict.fromkeys("lavwcfinoxLAHhV", 0)}
_PROC_LONG = {
    **dict.fromkeys(("pgroup", "group", "older", "parent", "session", "signal", "terminal", "euid",
                     "uid", "pidfile", "runstates", "cgroup", "ns", "nslist"), 1),
    **dict.fromkeys(("count", "full", "ignore-case", "newest", "oldest", "exact", "logpidfile",
                     "ignore-ancestors", "require-handler", "help", "version"), 0)}
_PKILL_LONG = {**_PROC_LONG, "queue": 1, "echo": 0}
_PGREP_LONG = {**_PROC_LONG, "delimiter": 1, "list-name": 0, "list-full": 0, "inverse": 0,
               "lightweight": 0}
_PROC_SHORT_LONG = {"F": "pidfile"}
# killall (psmisc 23.7): its options with a value.
_KILLALL_SHORT = {**dict.fromkeys("yosunZ", 1), **dict.fromkeys("eIgilqrvVw", 0)}
_KILLALL_LONG = {**dict.fromkeys(("younger-than", "older-than", "signal", "user", "ns", "context"), 1),
                 **dict.fromkeys(("exact", "ignore-case", "process-group", "interactive", "list",
                                  "quiet", "regexp", "verbose", "version", "wait", "help"), 0)}
# grep (GNU grep 3.12; ugrep reads the same patterns): options with a value,
# and the long ones whose value may only be glued (`--color=always`).
_GREP_SHORT_VALUE = frozenset("efmABCdD")
_GREP_LONG = {
    **dict.fromkeys(("regexp", "file", "max-count", "after-context", "before-context", "context",
                     "devices", "directories", "exclude", "include", "exclude-dir", "exclude-from",
                     "label", "binary-files", "group-separator"), 1),
    **dict.fromkeys(("fixed-strings", "basic-regexp", "extended-regexp", "perl-regexp",
                     "ignore-case", "no-ignore-case", "invert-match", "word-regexp", "line-regexp",
                     "count", "color", "colour", "files-with-matches", "files-without-match",
                     "only-matching", "quiet", "silent", "no-messages", "byte-offset",
                     "with-filename", "no-filename", "line-number", "initial-tab", "null",
                     "null-data", "text", "recursive", "dereference-recursive", "line-buffered",
                     "no-group-separator", "help", "version"), 0)}
_POSIX_CLASSES = {"alpha": "a-zA-Z", "digit": "0-9", "alnum": "a-zA-Z0-9", "upper": "A-Z",
                  "lower": "a-z", "space": r"\s", "blank": r" \t", "punct": r"!-/:-@\[-`{-~",
                  "xdigit": "0-9A-Fa-f", "print": " -~", "graph": "!-~", "cntrl": r"\x00-\x1f\x7f",
                  "word": r"\w"}
_PATTERN_CAP = 1024        # past it a kill pattern is too complex to judge (asked)
# What matching one pattern may cost, and every pattern on one line together,
# in `_regex_work`'s units (review 2 of PR #48). Python's `re` has no timeout
# and holds the GIL while it matches, so a match past these would freeze the
# whole daemon, every turn and the approval broker with it — `.?` thirty times
# took days. The per-pattern budget fits two unbounded quantifiers on the
# owner's checkout (`node.*vite.*dev`), and the worst pattern under it matches
# in about 10 ms (`redos_checks` in tests/rules_check.py holds every one under
# 50 ms). Parsing costs too: a line's patterns share `_LINE_PATTERN_CHARS`.
_REGEX_BUDGET = 1 << 19
_LINE_REGEX_BUDGET = 1 << 20
_LINE_PATTERN_CHARS = 4096
_PID_PROPERTIES = frozenset({"MainPID", "ControlPID", "ExecMainPID"})
# D-Bus calls that only read: busctl's and gdbus's reading verbs, and the
# methods that return state (`Get`, `GetAll`, `Introspect` …).
_BUSCTL_READS = frozenset({"list", "status", "monitor", "capture", "tree", "introspect",
                           "get-property", "wait", "help"})
_GDBUS_READS = frozenset({"introspect", "monitor", "wait", "help"})
_DBUS_READ_METHODS = frozenset({
    "Get", "GetAll", "Introspect", "Ping", "GetMachineId", "GetUnit", "GetUnitByPID",
    "GetUnitByInvocationID", "GetUnitByControlGroup", "GetUnitProcesses", "GetUnitFileState",
    "GetUnitFileLinks", "GetDefaultTarget", "GetJob", "ListUnits", "ListUnitsFiltered",
    "ListUnitsByPatterns", "ListUnitsByNames", "ListUnitFiles", "ListUnitFilesByPatterns",
    "ListJobs", "Dump"})


def _daemon_command_lines() -> tuple[str, ...]:
    """The daemon's command lines as `pkill -f` reads them: the unit's, and
    the other ways it is started."""
    return (f"{Path(config.REPO_ROOT)}/.venv/bin/python -m jarvis daemon2",
            "python3 -m jarvis daemon2", "python -m jarvis daemon2", "jarvis daemon2")


def _daemon_ports() -> frozenset[int]:
    """The ports whose holder may be the daemon: the control plane's
    (`config.control_ports()`) and the workshop's, which the v2 daemon binds
    as its preview listener (8403)."""
    return config.control_ports() | {8403, config.WORKSHOP_PORT}


def _signal_word(word: str) -> str | None:
    """The signal a `-SIG` word names (`-9`, `-KILL`, `-SIGTERM`), or None."""
    if not re.fullmatch(r"-[A-Za-z0-9+]+", word):
        return None
    name = word[1:].upper()
    name = name[3:] if name.startswith("SIG") else name
    if name.isdigit() or name in _SIGNALS or re.fullmatch(r"RTM(?:IN|AX)[+-]?\d*", name):
        return name
    return None


_REPEATS = tuple(getattr(_sre_c, name) for name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT")
                 if hasattr(_sre_c, name))
_PLAIN_OPS = tuple(getattr(_sre_c, name) for name in ("LITERAL", "NOT_LITERAL", "ANY", "IN", "AT",
                                                      "CATEGORY") if hasattr(_sre_c, name))


def _regex_choices(items, n: int, cap: int) -> int:
    """An upper bound on the ways a backtracking matcher can try a parsed
    pattern against `n` characters, saturating at `cap`: a sequence
    multiplies, an alternation adds, an unbounded repeat counts as the text
    (n+1 lengths), `?` as 2, `{m,k}` as k−m+1, and a repeat of something
    that itself chooses as (k−m+1)·inner^k — nested repeats multiply out, so
    `(.*)*` saturates. A back-reference, or anything not understood here,
    saturates too: a cost that cannot be bounded is never run."""
    total = 1
    for op, av in getattr(items, "data", items):
        total = min(cap, total * _regex_node(op, av, n, cap))
        if total >= cap:
            return cap
    return total


def _regex_node(op, av, n: int, cap: int) -> int:
    if op in _PLAIN_OPS:
        return 1
    if op in _REPEATS:
        low, high, sub = av
        most = n if high == _sre_c.MAXREPEAT else min(high, n)
        if low > most:
            return 1                   # it cannot match: sre checks the width first
        inner = _regex_choices(sub, n, cap)
        out = most - low + 1
        if inner > 1:
            for _ in range(most):
                out *= inner
                if out >= cap:
                    return cap
        return min(cap, out)
    if op is _sre_c.SUBPATTERN:
        return _regex_choices(av[-1], n, cap)
    if op is _sre_c.BRANCH:
        return min(cap, sum(_regex_choices(branch, n, cap) for branch in av[1]))
    if op in (_sre_c.ASSERT, _sre_c.ASSERT_NOT):
        return _regex_choices(av[1], n, cap)
    if op is getattr(_sre_c, "ATOMIC_GROUP", None):
        return _regex_choices(av, n, cap)
    if op is _sre_c.GROUPREF_EXISTS:
        return min(cap, _regex_choices(av[1], n, cap)
                   + (_regex_choices(av[2], n, cap) if av[2] is not None else 1))
    return cap


def _regex_work(tree, runs: list[tuple[int, int]], cap: int) -> int:
    """What matching a parsed pattern costs, bounded from above: per text of
    length n, the choices it can try (`_regex_choices`) times the starts it is
    tried at, summed over `runs` [(n, starts)], saturating at `cap`."""
    total = 0
    for n, starts in runs:
        total += _regex_choices(tree, n, cap) * starts
        if total >= cap:
            return cap
    return total


def _required_text(items, out: list[str], run: list[str]) -> None:
    """The literal runs every match of a parsed pattern contains: consecutive
    literal characters of its top-level sequence, a plain group's included
    (`(ab)c` is `abc`). Anything else — a repeat, an alternation, a class, an
    anchor, a lookaround — ends a run; it is never treated as text."""
    for op, av in getattr(items, "data", items):
        if op is _sre_c.LITERAL:
            run.append(chr(av))
        elif op is _sre_c.SUBPATTERN and not av[1] and not av[2]:
            _required_text(av[-1], out, run)
        else:
            if run:
                out.append("".join(run))
                run.clear()


def _required_runs(tree) -> list[str]:
    out: list[str] = []
    run: list[str] = []
    _required_text(tree, out, run)
    if run:
        out.append("".join(run))
    return out


def _ere(pattern: str) -> str:
    """POSIX ERE (pkill, killall -r, grep -E) in Python's spelling: bracket
    classes and GNU word boundaries."""
    pattern = re.sub(r"\[:(\w+):\]", lambda m: _POSIX_CLASSES.get(m.group(1), m.group(0)), pattern)
    return pattern.replace(r"\<", r"\b").replace(r"\>", r"\b")


def _bre(pattern: str) -> str:
    """POSIX BRE with GNU's escapes (grep's default) as ERE: `\\(`, `\\|`,
    `\\+`, `\\?`, `\\{` are the operators, and the bare characters are text."""
    out, i, n = [], 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "[":
            j = i + 1 + (pattern[i + 1:i + 2] == "^")
            j += pattern[j:j + 1] == "]"
            while j < n and pattern[j] != "]":
                if pattern[j] == "[" and pattern[j + 1:j + 2] in (":", ".", "="):
                    end = pattern.find(pattern[j + 1] + "]", j + 2)
                    j = end + 1 if end != -1 else j
                j += 1
            out.append(pattern[i:j + 1])
            i = j + 1
            continue
        if c == "\\" and i + 1 < n:
            nxt = pattern[i + 1]
            out.append(nxt if nxt in "(){}|+?" else c + nxt)
            i += 2
            continue
        out.append("\\" + c if c in "(){}|+?" else c)
        i += 1
    return "".join(out)


# What `_daemon_match` answers when a pattern's matching cost is past what is
# judged here (`_regex_work` against the budgets) or it is too long to parse:
# not "matches" and not "does not match" — **too complex to judge**, which is
# ASK, never covered by an allowlist entry and always-ask in v2, and never
# DENY (the delta review of PR #48: `pkill -f 'node.*vite.*--port.*5173'` and
# every kill pattern with three unbounded quantifiers was an unapprovable
# DENY, though it could never match the daemon).
COSTLY = "too costly to judge"


def _daemon_spans(full: bool = True) -> list[tuple[str, int, int]]:
    """The daemon's command lines (or, not `full`, its process names) as a
    process pattern reads them, each with the span of `daemon2` a selection
    must reach — (-1, -1) where there is none."""
    texts = _daemon_command_lines() if full else _DAEMON_NAMES
    return [(t, t.find("daemon2"), t.find("daemon2") + 7) if "daemon2" in t else (t, -1, -1)
            for t in texts]


def _socket_spans() -> list[tuple[str, int, int]]:
    """The daemon's listening sockets as `ss -ltnp` and `netstat -tlnp` print
    them, each with the span of its port a selection must reach."""
    out = []
    for port in sorted(_daemon_ports()):
        for text in (f'LISTEN 0 5 127.0.0.1:{port} 0.0.0.0:* users:(("python3",pid=4242,fd=5))',
                     f"tcp 0 0 127.0.0.1:{port} 0.0.0.0:* LISTEN 4242/python3"):
            at = text.find(f":{port}") + 1
            out.append((text, at, at + len(str(port))))
    return out


def _holds(run: str, text: str, ci: bool) -> bool:
    """Whether `text` holds the literal `run` (case-folded under IGNORECASE;
    a non-ASCII run is assumed held, since `re`'s folding is not `lower()`'s)."""
    if not ci:
        return run in text
    return not run.isascii() or run.lower() in text.lower()


def _daemon_match(pattern: str, *, how: str, full: bool = True, icase: bool = False,
                  literal: bool = False, syntax: str = "ere", line: str = "",
                  budget: list[int] | None = None,
                  spans: list[tuple[str, int, int]] | None = None):
    """Whether a process pattern matches the daemon: `how` is "overlap" (a
    match whose span overlaps the part of a text a selection must reach —
    `daemon2` on its command lines, or a port, `spans`), "exact" (a whole
    text) or "anywhere". True or False when it can be told; None when it
    cannot and every caller fails closed — an expansion only known when it
    runs, or a pattern that will not compile on a line that names `daemon2`
    (one that will not compile anywhere else selects nothing: the tool
    refuses it); and `COSTLY` when matching it would cost more than is judged
    here — the caller asks rather than refusing.

    Before any cost is counted, a text that lacks a literal run every match
    of the pattern contains (`_required_runs`: `node` in `node.*vite`) is set
    aside, so a pattern that cannot match the daemon is told so cheaply,
    however costly matching it would be."""
    if _wild_word(pattern):
        return None
    spans = spans if spans is not None else _daemon_spans(full)
    if how == "overlap":
        spans = [s for s in spans if s[1] >= 0]
        if not spans:
            return False
    if literal:
        source = re.escape(pattern)
    elif len(pattern) > _PATTERN_CAP:
        return COSTLY
    else:
        source = {"bre": lambda p: _ere(_bre(p)), "python": str}.get(syntax, _ere)(pattern)
    flags = re.IGNORECASE if icase else 0
    if budget is not None:
        # Parsing costs too (about 2 µs a character): a line's patterns share
        # `_LINE_PATTERN_CHARS` of it, so a word that expands to 256 patterns
        # cannot buy a long parse.
        if len(source) > budget[1]:
            return COSTLY
        budget[1] -= len(source)
    try:
        tree = _sre_parse.parse(source, flags)
        ci = bool((flags | getattr(getattr(tree, "state", None), "flags", 0)) & re.IGNORECASE)
        runs = _required_runs(tree)
        spans = [s for s in spans if all(_holds(r, s[0], ci) for r in runs)]
        if not spans:
            return False
        work = _regex_work(tree, [(len(t), end if how == "overlap" else 1 if how == "exact"
                                   else len(t) + 1) for t, _, end in spans], _REGEX_BUDGET + 1)
        if work > _REGEX_BUDGET or (budget is not None and work > budget[0]):
            return COSTLY
        rx = re.compile(source, flags)
    except Exception:     # noqa: BLE001 — `re` refuses it (re.error, OverflowError, RecursionError)
        if "daemon2" in re.sub(r"[\\()\[\]{}|*+?.^$'\"]", "", line or pattern):
            return None
        return False
    if budget is not None:
        budget[0] -= work
    for text, at, end in spans:
        if how == "exact":
            if rx.fullmatch(text):
                return True
            continue
        if how == "anywhere":
            if rx.search(text):
                return True
            continue
        for start in range(end):
            m = rx.match(text, start)
            if m and m.start() < end and m.end() > at:
                return True
    return False


def _selects_daemon(pattern: str, *, full: bool, exact: bool = False, icase: bool = False,
                    inverse: bool = False, literal: bool = False, line: str = "",
                    budget: list[int] | None = None):
    """Whether a pkill/pgrep/killall/pidof pattern selects the daemon at
    `daemon2` (`_daemon_match`); `inverse` (pgrep -v) selects it when nothing
    matches. True, False, or `COSTLY`; what cannot be told selects it."""
    found = _daemon_match(pattern, how=("exact" if exact else "anywhere" if inverse else "overlap"),
                          full=full, icase=icase, literal=literal, line=line, budget=budget)
    if found is None:
        return True
    if found == COSTLY:
        return COSTLY
    return (not found) if inverse else found


def _too_complex(what: str) -> str:
    return (f"it is too complex to judge whether {what} matches the v2 daemon "
            f"(`jarvis daemon2`, the process that holds the owner's approvals): matching its "
            f"pattern would cost more than is judged here, since Python's `re` cannot be stopped "
            f"mid-match. Asked, never covered by an allowlist entry")


def _judge_patterns(results, kills: "_Kills", what: str) -> bool:
    """Whether one of `results` (`_selects_daemon` answers, read lazily)
    selects the daemon. One that is `COSTLY` selects nothing; it marks the
    line too complex to judge (`kills.unsure`), which asks."""
    costly = False
    for result in results:
        if result is True:
            return True
        costly = costly or result == COSTLY
    if costly and not kills.unsure:
        kills.unsure = _too_complex(what)
    return False


def _proc_args(words: list[str], short: dict, long: dict, signals: bool,
               signal_letter: str = "") -> tuple[set, list, bool, dict]:
    """(the options seen, by long name or letter; the operands; whether an
    option was not known; the value-taking options' values, by long name)
    for pkill, pgrep and killall."""
    seen, operands, unknown, values, i = set(), [], False, {}, 0
    while i < len(words):
        word = words[i]
        i += 1
        if word == "--":
            operands.extend(words[i:])
            break
        if signals and _signal_word(word):
            seen.add("signal=" + _signal_word(word))
            continue
        if word.startswith("--") and len(word) > 2:
            name, eq, value = word[2:].partition("=")
            matches = [n for n in long if n == name] or [n for n in long if n.startswith(name)]
            if len(matches) != 1:
                unknown = True
                operands.append(word)
                continue
            full = matches[0]
            if long[full] and not eq:
                value = words[i] if i < len(words) else ""
                i += 1
            seen.add(full)
            if long[full]:
                values.setdefault(full, []).append(value)
            if full == "signal":
                seen.add("signal=" + (value or "").upper().removeprefix("SIG"))
            continue
        if word.startswith("-") and len(word) > 1:
            for k, letter in enumerate(word[1:]):
                takes = short.get(letter)
                if takes is None:
                    unknown = True
                    operands.append(word[1 + k:])     # it may be a pattern after all
                    break
                seen.add(letter)
                if takes:
                    value = word[2 + k:] or (words[i] if i < len(words) else "")
                    if not word[2 + k:]:
                        i += 1
                    values.setdefault(_PROC_SHORT_LONG.get(letter, letter), []).append(value)
                    if letter == signal_letter:
                        seen.add("signal=" + value.upper().removeprefix("SIG"))
                    break
            continue
        operands.append(word)
    return seen, operands, unknown, values


def _names_unit(value: str) -> bool:
    """Whether an option's value — a cgroup path, a PID file — may name the
    daemon's unit: an expansion only known when it runs, `daemon2` in it, or
    a glob the unit's name matches."""
    if _wild_word(value) or "daemon2" in value:
        return True
    return bool(_GLOB.search(value)) and any(
        fnmatch(name, part) for part in value.split("/") if part for name in _DAEMON_UNIT_NAMES)


def _kill_why(tool: str) -> str:
    return _DAEMON_UNIT_WHY.format(
        how=f"kills processes by a pattern that matches the v2 daemon's command line at "
            f"`daemon2` ({tool})")


def _fed_why(source: str) -> str:
    return _DAEMON_UNIT_WHY.format(
        how=f"kills the v2 daemon's process: a `kill` on this line is fed the PIDs of {source}")


def _pkill_op(tokens: list[str], state: "_Owner", appends: bool) -> str | None:
    words = _unit_words(tokens[1:], state)
    if words is None:
        return _kill_why("`pkill`, with more brace expansions than are judged here")
    seen, operands, unknown, values = _proc_args(words, _PKILL_SHORT, _PKILL_LONG, signals=True)
    if "signal=0" in seen or {"h", "V", "help", "version"} & seen:
        return None
    if any(_names_unit(v) for v in values.get("cgroup", []) + values.get("pidfile", [])):
        return _DAEMON_UNIT_WHY.format(
            how="kills the processes of the v2 daemon's unit (`pkill --cgroup`/`--pidfile` naming it)")
    full = unknown or bool({"f", "full"} & seen)
    if not operands:
        return _kill_why("`xargs pkill`, its pattern from the input") if appends and full else None
    kills = state.kills
    exact, icase = bool({"x", "exact"} & seen), bool({"i", "ignore-case"} & seen)
    if _judge_patterns((_selects_daemon(p, full=full, exact=exact, icase=icase, line=kills.line,
                                        budget=kills.work) for p in operands), kills, "`pkill -f`"):
        return _kill_why("`pkill -f`")
    return None


def _killall_op(tokens: list[str], state: "_Owner", appends: bool) -> str | None:
    words = _unit_words(tokens[1:], state)
    if words is None:
        return _kill_why("`killall`, with more brace expansions than are judged here")
    seen, operands, unknown, _ = _proc_args(words, _KILLALL_SHORT, _KILLALL_LONG, signals=True,
                                            signal_letter="s")
    if "signal=0" in seen or {"l", "list", "V", "version", "help"} & seen:
        return None
    regex = unknown or bool({"r", "regexp"} & seen)
    if not regex:
        return None            # by name: the daemon's names never hold `daemon2`
    if not operands:
        return _kill_why("`xargs killall -r`, its pattern from the input") if appends else None
    kills = state.kills
    icase = bool({"I", "ignore-case"} & seen)
    if _judge_patterns((_selects_daemon(p, full=True, icase=icase, line=kills.line, budget=kills.work)
                        for p in operands), kills, "`killall -r`"):
        return _kill_why("`killall -r`")
    return None


def _note_kill(tokens: list[str], state: "_Owner", appends: bool) -> None:
    """A `kill` that sends a signal (not `-0`, not a listing) to PIDs fed to
    it on this line — an operand only known when it runs (`$(…)`, backticks,
    `$P`, `<(…)`) or `xargs` input — marks the line; its sources are judged
    at the line's end. A kill of literal PIDs or jobs (`kill -9 4321`, `kill
    %1`) is fed by nothing on the line (review 2 of PR #48)."""
    words = tokens[1:]
    i = 0
    while i < len(words):
        word = words[i]
        if word in ("-l", "-L", "--list", "--table") or word.startswith("--list="):
            return
        if word in ("-s", "-n", "--signal") and i + 1 < len(words):
            if words[i + 1].upper().removeprefix("SIG") == "0":
                return
            i += 2
            continue
        if re.fullmatch(r"-(?:s|n|-signal=)?(?:SIG)?0", word, re.IGNORECASE):
            return
        i += 1
    if appends or any(_wild_word(w) or w.startswith(("<(", ">(")) for w in words):
        state.kills.kill = True


def _note_pid_source(stem: str, tokens: list[str], state: "_Owner", appends: bool) -> None:
    """`pgrep` or `pidof` on the line: whether the PIDs it prints include the
    daemon's, judged only if a fed `kill` is on the line (`pidof`'s names as
    fixed text, failing closed)."""
    words = _unit_words(tokens[1:], state)
    kills = state.kills
    if words is None:
        kills.sources.append(lambda: f"`{stem}`, with more brace expansions than are judged here")
        return
    if stem == "pidof":
        names, i = [], 0
        while i < len(words):
            if words[i] in ("-o", "-d") and i + 1 < len(words):
                i += 2
                continue
            if not words[i].startswith("-") or words[i] == "-":
                names.append(words[i])
            i += 1
        kills.sources.append(lambda: "`pidof`" if _judge_patterns(
            (_selects_daemon(name, full=True, literal=True, line=kills.line, budget=kills.work)
             for name in names), kills, "a `pidof` feeding a `kill`") else None)
        return
    seen, operands, unknown, values = _proc_args(words, _PGREP_SHORT, _PGREP_LONG, signals=False)
    if {"h", "V", "help", "version"} & seen:
        return
    if any(_names_unit(v) for v in values.get("cgroup", []) + values.get("pidfile", [])):
        kills.sources.append(lambda: "`pgrep --cgroup`/`--pidfile` naming the daemon's unit")
        return
    full = unknown or bool({"f", "full"} & seen)
    if not operands:
        if appends:
            kills.sources.append(lambda: "`xargs pgrep`, its pattern from the input")
        return
    exact, icase = bool({"x", "exact"} & seen), bool({"i", "ignore-case"} & seen)
    inverse = bool({"v", "inverse"} & seen)
    kills.sources.append(lambda: "`pgrep -f` with a pattern that matches its command line at `daemon2`"
                         if _judge_patterns(
                             (_selects_daemon(p, full=full, exact=exact, icase=icase, inverse=inverse,
                                              line=kills.line, budget=kills.work) for p in operands),
                             kills, "a `pgrep -f` feeding a `kill`")
                         else None)


# A file a grep reads that lists processes by their command lines: every
# process's `/proc/<pid>/cmdline` (or `comm`, `status`, `stat`, `environ`),
# by glob, by PID, behind a variable (`$p/cmdline`), or the whole of /proc
# read recursively. `grep -l daemon2 /proc/*/cmdline | cut -d/ -f3 | xargs
# kill` is pgrep spelled long (the delta review of PR #48).
_PROC_FILE = re.compile(r"(?:^|/)proc/+(?:[^/]*[*?\[\]][^/]*|\d+|\$[^/]*|\{[^/]*\})/+"
                        r"(?:cmdline|comm|status|stat|environ)$")


def _proc_listing(word: str) -> bool:
    return (bool(_PROC_FILE.search(word)) or bool(re.fullmatch(r"/+proc/*", word))
            or (_wild_word(word) and bool(re.search(r"/(?:cmdline|comm|status|stat)$", word))))


def _note_grep(tokens: list[str], state: "_Owner") -> None:
    """A `grep` on the line: kept with its patterns, for a process or socket
    listing it may filter (`ps aux | grep daemon2 | awk '{print $2}' | xargs
    kill`); one that reads `/proc/*/cmdline` itself is such a listing."""
    stem = rules._basename(tokens[0])
    words = _unit_words(tokens[1:], state) or tokens[1:]
    syntax = {"egrep": "ere", "fgrep": "fixed"}.get(stem, "bre")
    patterns: list[str] = []
    operands: list[str] = []
    icase = invert = False
    i = 0
    while i < len(words):
        word = words[i]
        i += 1
        if word == "--":
            operands.extend(words[i:])
            break
        if word.startswith("--") and len(word) > 2:
            name, eq, value = word[2:].partition("=")
            matches = [n for n in _GREP_LONG if n == name] or [n for n in _GREP_LONG if n.startswith(name)]
            full = matches[0] if len(matches) == 1 else ""
            if full and _GREP_LONG[full] and not eq:
                value = words[i] if i < len(words) else ""
                i += 1
            if full == "regexp":
                patterns.append(value)
            elif full == "file":
                patterns.append("$FILE")             # read from a file: not known here
            syntax = {"fixed-strings": "fixed", "extended-regexp": "ere", "basic-regexp": "bre",
                      "perl-regexp": "python"}.get(full, syntax)
            icase = (icase or full == "ignore-case") and full != "no-ignore-case"
            invert = invert or full == "invert-match"
            continue
        if word.startswith("-") and len(word) > 1 and not word[1:].isdigit():
            for k, letter in enumerate(word[1:]):
                if letter in _GREP_SHORT_VALUE:
                    value = word[2 + k:] or (words[i] if i < len(words) else "")
                    if not word[2 + k:]:
                        i += 1
                    if letter == "e":
                        patterns.append(value)
                    elif letter == "f":
                        patterns.append("$FILE")
                    break
                syntax = {"F": "fixed", "E": "ere", "G": "bre", "P": "python"}.get(letter, syntax)
                icase = icase or letter in "iy"
                invert = invert or letter == "v"
            continue
        operands.append(word)
    files = operands if patterns else operands[1:]
    if not patterns and operands:
        patterns.append(operands[0])
    if patterns:
        state.kills.greps.append((patterns, syntax, icase, invert))
    if any(_proc_listing(f) for f in files):
        state.kills.listings.add("processes")


def _socket_source(stem: str, tokens: list[str], state: "_Owner") -> None:
    """`ss -p` or `netstat -p` on the line — they print each socket's PID —
    is a socket listing a grep may filter onto the daemon's port (`ss -ltnp |
    grep :8405 | … | xargs kill`); `ss -p` with a filter naming one of its
    ports (`sport = :8405`) is a source by itself."""
    words = _unit_words(tokens[1:], state) or tokens[1:]
    processes = any(w in ("--processes", "--program", "--programs")
                    or (re.fullmatch(r"-[A-Za-z]+", w) and "p" in w[1:]) for w in words)
    if not processes:
        return
    if stem == "ss":
        ports = _daemon_ports()
        for word in words:
            if word.startswith("-"):
                continue
            named = {int(n) for n in re.findall(r"(?<![\d.])(\d{2,5})(?![\d.])", word)}
            if _wild_word(word) or named & ports:
                state.kills.sources.append(lambda: "`ss -p` filtered to the daemon's port")
                return
    state.kills.listings.add("sockets")


def _grep_chain(kills: "_Kills", spans: list[tuple[str, int, int]]):
    """Whether the line's greps, between them, pass on one of `spans`' texts
    and select it at its span: every grep keeps it (a match anywhere, or none
    for `-v`) and one that is not `-v` matches it there. True, False, or
    `COSTLY` when only a pattern too costly to match stands between them and
    an answer. What cannot be told keeps and selects (fails closed)."""
    def tell(pattern, syntax, icase, how):
        found = _daemon_match(pattern, how=how, icase=icase, literal=syntax == "fixed", syntax=syntax,
                              line=kills.line, budget=kills.work, spans=spans)
        return True if found is None else found

    def any_of(results):
        results = list(results)
        if True in results:
            return True
        return COSTLY if COSTLY in results else False

    sure_keep, sure_at, maybe_at = True, False, False
    for patterns, syntax, icase, invert in kills.greps:
        hit = any_of(tell(p, syntax, icase, "anywhere") for p in patterns)
        if hit == COSTLY:
            sure_keep = False
        elif hit == invert:
            return False                         # this grep drops the daemon's line
        if not invert:
            at = any_of(tell(p, syntax, icase, "overlap") for p in patterns)
            sure_at = sure_at or at is True
            maybe_at = maybe_at or at == COSTLY
    if sure_keep and sure_at:
        return True
    return COSTLY if (sure_at or maybe_at) else False


def _listings_filtered_by_grep(kills: "_Kills") -> str | None:
    """A process or socket listing on the line (`ps`, a grep over
    `/proc/*/cmdline`, `ss -p`, `netstat -p`) and greps that select the
    daemon in it (`_grep_chain`): processes at `daemon2`, sockets at one of
    its ports. One too costly to judge marks the line `kills.unsure`."""
    if not kills.greps:
        return None
    for kind, spans, name in (
            ("processes", _daemon_spans, "a process listing (`ps`, `/proc/*/cmdline`) filtered by a "
                                         "`grep` that matches its command line at `daemon2`"),
            ("sockets", _socket_spans, "a socket listing (`ss -p`, `netstat -p`) filtered by a `grep` "
                                       "on the daemon's port")):
        if kind not in kills.listings:
            continue
        found = _grep_chain(kills, spans())
        if found is True:
            return name
        if found == COSTLY and not kills.unsure:
            kills.unsure = _too_complex(f"a `grep` over {kind} feeding a `kill`")
    return None


def _note_systemctl_source(tokens: list[str], state: "_Owner") -> None:
    """`systemctl show` of the unit's MainPID, ControlPID or ExecMainPID (or
    of every property), and `systemctl status` of it or of the whole user
    manager: each prints the daemon's PID."""
    words = _unit_words(tokens[1:], state)
    if words is None:
        return
    parsed = _systemctl_parse(words)
    verbs = parsed.operands if parsed.unknown else parsed.operands[:1]
    units = parsed.operands if parsed.unknown else parsed.units
    daemon = any(_may_be_daemon(u, parsed.user) for u in units)
    shows = "show" in verbs and daemon and (
        not parsed.properties or any(p in _PID_PROPERTIES or _wild_word(p) for p in parsed.properties))
    status = "status" in verbs and (daemon or (parsed.user and not units))
    if shows or status:
        state.kills.sources.append(lambda: "`systemctl show`/`status` of its unit")


def _cgroup_source(segment: str) -> bool:
    """A segment that reads the daemon's cgroup's PIDs: `cgroup.procs` (or
    `cgroup.threads`) under a path naming the unit, or behind a glob or an
    expansion that may."""
    flat = segment.replace("'", "").replace('"', "").replace("\\", "")
    if "cgroup.procs" not in flat and "cgroup.threads" not in flat:
        return False
    return "daemon2" in flat or "$" in flat or "`" in flat or bool(_GLOB.search(flat))


def _port_spec_hits(spec: str, ports: frozenset[int]) -> bool:
    """Whether a list of ports and ranges (`8405`, `8400-8410`, `8402,8405`)
    may hold one of `ports`: a word only known when it runs may."""
    if _wild_word(spec):
        return True
    for item in spec.split(","):
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", item.strip())
        if m:
            low, high = int(m.group(1)), int(m.group(2) or m.group(1))
            if any(low <= port <= high for port in ports):
                return True
    return False


def _lsof_source(tokens: list[str], state: "_Owner") -> None:
    """`lsof -i` that lists the holders of one of the daemon's ports — an
    address whose port is one of them, or none (every socket)."""
    words = _unit_words(tokens[1:], state) or tokens[1:]
    ports = _daemon_ports()
    specs: list[str] = []
    i = 0
    while i < len(words):
        word = words[i]
        i += 1
        if not word.startswith("-") or word.startswith("--") or len(word) < 2:
            continue
        letters = word[1:]
        for k, letter in enumerate(letters):
            if letter == "i":
                rest = letters[k + 1:]
                if rest:
                    specs.append(rest)
                elif i < len(words) and not words[i].startswith(("-", "+")):
                    specs.append(words[i])
                    i += 1
                else:
                    specs.append("")
                break
            if letter in "cdDgkoprsSTux":
                break                                  # the rest is that option's value
    for spec in specs:
        m = re.search(r":([^:\]]*)$", spec)
        if m is None or _port_spec_hits(m.group(1), ports) or _wild_word(spec):
            state.kills.sources.append(lambda: "`lsof -i` on the daemon's port")
            return


def _fuser_op(tokens: list[str], state: "_Owner") -> str | None:
    """`fuser` on one of the daemon's ports (`8405/tcp`, `-n tcp 8405`): with
    `-k` it kills the daemon; without, it prints its PID."""
    words = _unit_words(tokens[1:], state) or tokens[1:]
    ports = _daemon_ports()
    kill = listing = False
    namespace = ""
    hit = False
    i = 0
    while i < len(words):
        word = words[i]
        i += 1
        if word.startswith("-") and len(word) > 1 and not _signal_word(word):
            for k, letter in enumerate(word[1:]):
                if letter == "n":
                    namespace = word[2 + k:] or (words[i] if i < len(words) else "")
                    if not word[2 + k:]:
                        i += 1
                    break
                kill = kill or letter == "k"
                listing = listing or letter in "lV"
            continue
        if word.startswith("-"):
            continue                                   # a signal
        m = re.fullmatch(r"([^,/]*)(?:,[^,/]*(?:,[^,/]*)?)?(?:/(tcp|udp))?", word)
        if (m and m.group(1) and (m.group(2) or namespace in ("tcp", "udp") or _wild_word(namespace))
                and _port_spec_hits(m.group(1), ports)):
            hit = True
    if listing or not hit:
        return None
    if kill:
        return _DAEMON_UNIT_WHY.format(how="kills whatever holds the v2 daemon's port (`fuser -k`)")
    state.kills.sources.append(lambda: "`fuser` on the daemon's port")
    return None


def _dbus_read(tool: str, words: list[str]) -> bool:
    """Whether a D-Bus command only reads: busctl's and gdbus's reading
    verbs, or a call whose method returns state (`Get`, `GetAll`,
    `Introspect` …). False when it cannot be told."""
    positionals = []
    method = ""
    i = 0
    while i < len(words):
        word = words[i]
        i += 1
        if word.startswith("-"):
            name, eq, value = word.partition("=")
            if tool == "gdbus" and name in ("-m", "--method"):
                method = value if eq else (words[i] if i < len(words) else "")
                i += 0 if eq else 1
            elif not eq and name in ("-H", "--host", "-M", "--machine", "-a", "--address", "-d",
                                     "--dest", "-o", "--object-path", "-t", "--timeout", "--bus"):
                i += 1
            continue
        positionals.append(word)
    verb = positionals[0] if positionals else ""
    if tool == "busctl":
        if verb in _BUSCTL_READS:
            return True
        method = positionals[4] if verb == "call" and len(positionals) > 4 else ""
    elif tool == "gdbus":
        if verb in _GDBUS_READS:
            return True
        method = method if verb == "call" else ""
    elif tool == "dbus-send":
        method = positionals[1] if len(positionals) > 1 else ""
    else:                                              # qdbus: SERVICE PATH METHOD …
        if len(positionals) < 3:
            return True                                # a listing
        method = positionals[2]
    return method.rpartition(".")[2] in _DBUS_READ_METHODS


_CLI_MODULES = frozenset({"jarvis", "jarvis.__main__"})
_LAUNCHERS = frozenset({"jarvis", "jarvis.exe"})
_PYTHON = re.compile(r"(?:python|pypy)[0-9.]*")
_OWNER_DEPTH = 4
_OWNER_BUDGET = 64 * 1024

# Which wrappers and runners run the command in their arguments, and how
# their options are read, is rules' one table (`rules.starts`, ssh included
# with `remote=True`): this module kept a second copy (`_RUNNERS`) until
# 2026-10-12, which knew neither npx nor `npm exec`, `pnpm`, `yarn` or
# `bun x`, read `xargs -i` and `watch -d` as taking the next word, and so let
# `npx jarvis hud install` and `yarn exec jarvis restart` through.
_PEEL_READINGS = 32      # readings of one command followed, at most
_PEEL_HOPS = 12          # wrappers and runners peeled inside one another


@dataclass
class _Kills:
    """Per command line, shared by every level of it: a `kill` that sends a
    signal to PIDs fed to it, the PID sources on the line (each judged only
    when there is such a kill — a callable naming the source when it yields
    the daemon's PIDs), the process and socket listings on it and the greps
    that may filter them, the line, whether it holds a kill word at all, the
    regex budget its patterns share, and — when a kill's pattern was too
    costly to judge and nothing on the line provably selects the daemon —
    why it must be asked (`kill_unsure`)."""
    kill: bool = False
    sources: list = field(default_factory=list)
    line: str = ""
    armed: bool = False
    listings: set = field(default_factory=set)
    greps: list = field(default_factory=list)
    work: list[int] = field(default_factory=lambda: [_LINE_REGEX_BUDGET, _LINE_PATTERN_CHARS])
    unsure: str | None = None


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
    kills: _Kills = field(default_factory=_Kills)

    def child(self) -> "_Owner":
        return _Owner(dict(self.variables), set(self.launchers), budget=self.budget,
                      filtered=self.filtered, kills=self.kills)


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


# A word whose value is only known when it runs: an expansion the line did not
# assign (unset ones vanish), `$@`/`$*`, a glob, a brace expansion.
_WILD = re.compile(r"\$|[*?\[]|\{[^{}]*(?:,|\.\.)[^{}]*\}")


def _could_spell(words: list[str], path: tuple[str, ...], open_ended: bool) -> bool:
    """Whether `words` can begin with `path` once the shell is done with them:
    a wild word may become no word, the next word, or several (`$@`, a glob,
    `{hud,install}`); a plain one only itself; an open end (xargs input) may
    supply whatever is still missing."""
    if not path:
        return True
    if not words:
        return open_ended
    head, rest = words[0], words[1:]
    if _WILD.search(head):
        return _could_spell(rest, path, open_ended) or _could_spell(words, path[1:], open_ended) \
            or _could_spell(rest, path[1:], open_ended)
    return head == path[0] and _could_spell(rest, path[1:], open_ended)


def _owner_path(argv: list[str], open_ended: bool) -> str | None:
    """The refusal for a jarvis argv that is, or may become, an owner op —
    fed more words by xargs, or once the shell expands a word only known at
    run time (review of PR #44 at 75f14c2: `python3 -m jarvis hud $E install`
    with `$E` unset, `h${UNSET}ud`, `h?d`, `{hud,install}` all ran)."""
    words = [t for t in argv if not t.startswith("-")]
    for path, why in OWNER_OPS.items():
        if tuple(words[:len(path)]) == path:
            return why
        if _could_spell(words, path, open_ended):
            if any(_WILD.search(w) for w in words):
                return why + (" (its words are only known when it runs — an expansion, a glob or a "
                              "brace — and could spell it; write the command out)")
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


# Modules that run another module or script (review of PR #48: `python3 -m
# runpy jarvis hud install` scored ALLOW): how each is told what to run — its
# options that take a value, the option that says "a module", and whether that
# option takes the module as its value. The module or script it runs is judged
# as `-m` and a script are. An option a runner's table does not know may have
# taken a value, so then every word left is tried as the target.
@dataclass(frozen=True)
class _ModuleRunner:
    values: frozenset = frozenset()
    module_flag: frozenset = frozenset()     # the next positional is a module
    module_value: frozenset = frozenset()    # the option's value is the module
    flags: frozenset = frozenset()


_MODULE_RUNNERS: dict[str, _ModuleRunner] = {
    "runpy": _ModuleRunner(module_flag=frozenset({""})),          # `-m runpy MODULE …`
    "cProfile": _ModuleRunner(frozenset({"-o", "--outfile", "-s", "--sort"}), frozenset({"-m"}),
                              flags=frozenset({"-h", "--help"})),
    "profile": _ModuleRunner(frozenset({"-o", "--outfile", "-s", "--sort"}), frozenset({"-m"}),
                             flags=frozenset({"-h", "--help"})),
    "pdb": _ModuleRunner(frozenset({"-c", "--command", "-p", "--pid"}), module_value=frozenset({"-m"}),
                         flags=frozenset({"-h", "--help"})),
    "trace": _ModuleRunner(frozenset({"-f", "--file", "-C", "--coverdir", "--ignore-module",
                                      "--ignore-dir"}), frozenset({"--module"}),
                           flags=frozenset({"-c", "--count", "-t", "--trace", "-l", "--listfuncs",
                                            "-T", "--trackcalls", "-r", "--report", "-R",
                                            "--no-report", "-m", "--missing", "-s", "--summary",
                                            "-g", "--timing", "-h", "--help", "--version"})),
    "coverage": _ModuleRunner(frozenset({"--concurrency", "--context", "--data-file", "--source",
                                         "--include", "--omit", "--rcfile", "--debug",
                                         "--save-signal", "--core"}), frozenset({"-m", "--module"}),
                              flags=frozenset({"-a", "--append", "--branch", "-L", "--pylib", "-p",
                                               "--parallel-mode", "--timid", "-h", "--help"})),
}


def _runner_targets(name: str, args: list[str]) -> list[tuple[str, str | None, list[str]]]:
    """What a module runner runs: [(kind, what, argv)], one entry when its
    options parse, one per remaining word when one does not."""
    runner = _MODULE_RUNNERS[name]
    if name == "coverage":
        if not args or args[0] != "run":
            return []
        args = args[1:]
    if name == "runpy" or "" in runner.module_flag:
        return [("module", args[0], args[1:])] if args else []
    module, i = False, 0
    while i < len(args):
        token = args[i]
        if token == "--":
            i += 1
            break
        if not token.startswith("-") or token == "-":
            break
        option, eq, value = token.partition("=")
        if option in runner.module_value:
            target = value if eq else (args[i + 1] if i + 1 < len(args) else "")
            return [("module", target, args[i + (1 if eq else 2):])]
        if option in runner.module_flag:
            module = True
            i += 1
            continue
        if option in runner.values:
            i += 1 if eq else 2
            continue
        if option in runner.flags:
            i += 1
            continue
        if re.fullmatch(r"-[A-Za-z]{2,}", token):        # a short cluster: judge it whole
            if any(f"-{c}" in runner.module_value for c in token[1:]):
                return [("module", w, args[k + 1:]) for k, w in enumerate(args[i + 1:], i + 1)]
            module = module or any(f"-{c}" in runner.module_flag for c in token[1:])
            if all(f"-{c}" in runner.flags | runner.module_flag for c in token[1:]):
                i += 1
                continue
        # Not known here: it may have taken a value, so try every word left.
        return [(kind, w, args[k + 1:]) for k, w in enumerate(args[i + 1:], i + 1)
                for kind in ("module", "script")]
    if i >= len(args):
        return []
    return [("module" if module else "script", args[i], args[i + 1:])]


def _python_targets(args: list[str], state: "_Owner") -> list[tuple[str, str | None, list[str]]]:
    """`_python_parse`, with module runners peeled (`-m runpy`, `-m cProfile
    -m`, `-m pdb -c continue -m`, `-m trace --module`, `-m coverage run -m`),
    each module name with the line's own `$VAR`s put in."""
    found: list[tuple[str, str | None, list[str]]] = []
    todo = [_python_parse(args)]
    for _ in range(64):
        if not todo:
            break
        kind, what, argv = todo.pop()
        if kind == "module":
            what = _owner_word(what or "", state, environ=False)
            if what in _MODULE_RUNNERS:
                todo.extend(_runner_targets(what, argv))
                continue
        found.append((kind, what, argv))
    return found


_CALL_SITES = ("system", "popen", "run", "call", "check_call", "check_output", "Popen", "getoutput",
               "getstatusoutput", "spawnlp", "execlp", "execvp")
# A Python list or tuple of two or more string literals.
_STR_LIST = re.compile(r"[\[(]\s*(?:[rbuRBU]?(?:'[^'\n]*'|\"[^\"\n]*\")\s*,\s*)+"
                       r"[rbuRBU]?(?:'[^'\n]*'|\"[^\"\n]*\")\s*,?\s*[\])]")


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
                if bits and bits[0] == "*":          # every writer, under its own name
                    funcs.update(map(re.escape, writers))
                elif bits and bits[0] in writers:
                    funcs.add(re.escape(bits[2] if len(bits) == 3 and bits[1] == "as" else bits[0]))
        writer = "|".join(map(re.escape, writers))
        # `importlib.import_module("jarvis.v2.owner_ops")` / `__import__(…)`:
        # the name it is bound to is the module; and since what it returns can
        # be reached any way (`getattr`, `__import__(…).v2.owner_ops`), a
        # writer's name anywhere beside it counts (review of PR #48).
        literal = r"\s*\(\s*[rbuf]*['\"]" + re.escape(module) + r"['\"]"
        dynamic = re.search(r"\b(?:import_module|__import__)" + literal, source)
        for m in re.finditer(r"\b(\w+)\s*=\s*(?:[\w.]+\s*\.\s*)?(?:import_module|__import__)" + literal,
                             source):
            names.add(re.escape(m.group(1)))
        if dynamic and re.search(r"\b(?:" + writer + r")\b", source):
            return OWNER_OPS[path]
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
    if depth < _OWNER_DEPTH:
        # An argv list of string literals, judged as the command it spells:
        # `["systemctl", "--user", "restart", "jarvis-daemon2"]`, `["python",
        # "-m", "jarvis", "restart"]` (review of PR #48).
        for m in _STR_LIST.finditer(source):
            words = [text for _, text in re.findall(r"[rbuRBU]?(['\"])(.*?)\1", m.group(0))]
            why = _owner_command(words, "", state.child(), depth + 1) if words else None
            if why:
                return why
    # A command line handed to a shell from Python: `os.system("jarvis …")`.
    if depth < _OWNER_DEPTH:
        call = r"\b(?:" + "|".join(_CALL_SITES) + r")\s*\(\s*[rbuf]*(['\"])(.*?)\1"
        for m in re.finditer(call, source, re.S):
            why = _owner_line(m.group(2), state.child(), depth + 1)
            if why:
                return why
    return None


def _peel_runner(tokens: list[str], state: _Owner, appends: bool = False
                 ) -> list[tuple[list[str], bool, bool, bool]]:
    """Every reading of (the command a line of words runs, whether more words
    may be appended to it from input, whether it is one command *string*,
    whether a wrapper or runner started it). `appends`: words from input may
    be added to the line itself (`parallel`, `fd -x`).
    Peels assignments, `builtin` and `env` here, and every wrapper and runner
    through rules' one table (`rules.starts`, ssh included), as far as they
    nest. An option the table does not know may or may not take the next
    word, and both readings are followed — so a short table can only add a
    refusal, never hide a command.

    Each reading is followed once, within the line's work budget
    (`rules.charge`); past the budget, `_PEEL_READINGS` or `_PEEL_HOPS` it
    raises `rules.TooComplex` rather than stopping quietly (review of PR #75,
    L2): the caller marks the line too complex to judge, which is asked."""
    tokens = [t for t in tokens]
    while tokens and tokens[0] in ("(", "{", "!"):
        tokens = tokens[1:]
    if tokens and tokens[0].startswith("(") and len(tokens[0]) > 1:
        tokens[0] = tokens[0][1:]
    readings: list[tuple[list[str], bool, bool, bool]] = []
    todo: list[tuple[list[str], bool, int]] = [(tokens, appends, 0)]
    seen: set = set()
    while todo:
        if len(readings) >= _PEEL_READINGS:
            raise rules.TooComplex
        tokens, appends, hops = todo.pop()
        if (tuple(tokens), appends) in seen:
            continue
        seen.add((tuple(tokens), appends))
        done = False
        while True:
            while tokens and re.fullmatch(r"[A-Za-z_]\w*=.*", tokens[0]):
                tokens = tokens[1:]
            if tokens and tokens[0] == "builtin":
                tokens = tokens[1:]
                continue
            if not tokens:
                break
            stem = rules._basename(tokens[0])
            if stem != "env" and stem not in rules._WRAPPER_OPTS and \
                    stem not in rules._RUNNER_SPECS and stem not in rules._REMOTE_OPTS:
                break
            hops += 1
            if hops > _PEEL_HOPS:
                raise rules.TooComplex
            rules.charge(len(tokens))
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
                    k += 2 if rules.takes_value("env", rest[k]) else 1
                if split is not None:
                    try:
                        words = shlex.split(split)
                    except ValueError:
                        words = split.split()
                    tokens = ["env"] + words + rest[k:]
                    continue
                tokens = rest[k:]
                continue
            started = rules.starts(tokens, remote=True)
            if started is None or not (started.programs or started.strings):
                break
            readings += [([text], False, True, True) for text in started.strings]
            for program in started.programs:
                if started.joined:
                    readings.append((list(program), appends or started.appends, True, True))
                else:
                    todo.append((list(program), appends or started.appends, hops))
            done = True
            break
        if not done:
            readings.append((tokens, appends, False, hops > 0))
    return readings


def _python_target_op(kind: str, what: str | None, argv: list[str], segment: str, state: _Owner,
                      depth: int, appends: bool) -> str | None:
    """The refusal for what a Python is told to run (`_python_targets`)."""
    if kind == "module":
        what = _owner_word(what or "", state, environ=False)
        if what in _CLI_MODULES:
            return _owner_path([_owner_word(t, state, environ=False) for t in argv], appends)
        if what in OWNER_OP_MODULES:
            return OWNER_OPS[OWNER_OP_MODULES[what][0]]
        return None
    if kind == "script":
        script = _owner_word(what or "", state, environ=False)
        stem_of = rules._basename(script)
        for module, (path, _) in OWNER_OP_MODULES.items():
            if stem_of == module.rpartition(".")[2] + ".py":
                return OWNER_OPS[path]
        # The CLI run as a script: its console-script launcher
        # (`python3 ~/.local/bin/jarvis restart`) or the package itself.
        if (stem_of in _LAUNCHERS or script in state.launchers
                or (stem_of == "__main__.py" and rules._basename(os.path.dirname(script)) == "jarvis")):
            return _owner_path([_owner_word(t, state, environ=False) for t in argv], appends)
        return None
    source = what if kind == "source" else ""
    if kind == "stdin":
        source = "\n".join(_here_strings(segment) + _take_bodies(segment, state))
    return _source_invokes(source or "", state, depth)


def _owner_command(tokens: list[str], segment: str, state: _Owner, depth: int,
                   appends: bool = False) -> str | None:
    for words, more, joined, behind in _peel_runner(tokens, state, appends):
        why = _owner_reading_of(words, more, joined, segment, state, depth, behind)
        if why:
            return why
    return None


def _exec_blocks(tokens: list[str], openers, closers) -> list[list[str]]:
    """The commands a find's `-exec`/`-ok` or fd's `-x`/`-X` runs: the words
    after each opener, up to a closer or the end."""
    blocks = []
    for k, token in enumerate(tokens):
        if token in openers:
            j = k + 1
            while j < len(tokens) and tokens[j] not in closers:
                j += 1
            blocks.append(tokens[k + 1:j])
    return blocks


_PARALLEL_ENDS = frozenset({":::", "::::", ":::+", "::::+"})


def _owner_runs_given(stem: str, tokens: list[str], segment: str, state: _Owner,
                      depth: int) -> str | None:
    """An owner op that `find -exec`, `fd -x` or `parallel` runs (delta review
    of PR #75, LOW: they run their command as the runners do, so it is the
    same DENY). fd and parallel append what they find or are given; parallel
    hands its command to a shell, and which word starts it is not parsed
    (its options are many), so every word that is not an option is tried."""
    if depth >= _OWNER_DEPTH:
        return None
    if stem in ("find", "bfs"):
        for block in _exec_blocks(tokens, rules._FIND_EXEC, (";", "+")):
            why = _owner_command(block, segment, state, depth + 1)
            if why:
                return why
        return None
    if stem in ("fd", "fdfind"):
        for block in _exec_blocks(tokens, ("-x", "--exec", "-X", "--exec-batch"), (";",)):
            why = _owner_command(block, segment, state, depth + 1, appends=True)
            if why:
                return why
        return None
    if stem == "parallel":
        end = next((k for k, t in enumerate(tokens) if t in _PARALLEL_ENDS), len(tokens))
        for start in range(1, end):
            if tokens[start].startswith("-"):
                continue
            rules.charge(end - start)
            why = (_owner_command(tokens[start:end], segment, state, depth + 1, appends=True)
                   or _owner_line(" ".join(tokens[start:end]), state, depth + 1))
            if why:
                return why
    return None


def _owner_reading_of(tokens: list[str], appends: bool, joined: bool, segment: str,
                      state: _Owner, depth: int, behind: bool = False) -> str | None:
    """`_owner_command` for one reading of what the command runs. `behind`:
    a wrapper or runner started it."""
    # `marked` keeps quoted braces marked (`_mark_quoted`) for the paths that
    # expand braces as bash does (`_unit_words`); every other path, and every
    # nested command line, reads the plain text.
    marked, tokens = tokens, [_unmark(t) for t in tokens]
    if not tokens:
        return None
    if joined:
        return _owner_line(" ".join(tokens), state, depth + 1) if depth < _OWNER_DEPTH else None
    # The command word as bash runs it (review of PR #48): the line's own
    # `$VAR`s put in (`S=systemctl; $S --user …`) and a brace expanded
    # (`{systemctl,--user} …`); an expansion that vanishes hands the command
    # to the next word.
    head = _owner_word(tokens[0], state)
    if head != tokens[0] or "{" in head:
        words = _brace_expand(head) or [head]
        if head != tokens[0]:
            words = [part for word in words for part in word.split()]
        tokens, marked = words + tokens[1:], words + marked[1:]
        if not tokens:
            return None
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
        why = _systemctl_op(marked, appends, state)
        if why is None:
            _note_systemctl_source(marked, state)
        return why
    if stem in _DBUS_TOOLS:
        return _dbus_op(marked, state)
    if stem == "pkill":
        return _pkill_op(marked, state, appends)
    if stem == "killall":
        return _killall_op(marked, state, appends)
    if stem == "kill":
        _note_kill(tokens, state, appends)
        return None
    if stem in ("pgrep", "pidof"):
        _note_pid_source(stem, marked, state, appends)
        return None
    if stem == "fuser":
        return _fuser_op(marked, state)
    if stem == "lsof":
        _lsof_source(marked, state)
        return None
    if stem == "ps":
        state.kills.listings.add("processes")
        return None
    if stem in ("ss", "netstat"):
        _socket_source(stem, marked, state)
        return None
    if stem in ("grep", "egrep", "fgrep"):
        _note_grep(marked, state)
        return None
    if _PYTHON.fullmatch(stem) or stem == "coverage":
        targets = (_python_targets(tokens[1:], state) if stem != "coverage"
                   else _runner_targets("coverage", tokens[1:]))
        for kind, what, argv in targets:
            why = _python_target_op(kind, what, argv, segment, state, depth, appends)
            if why:
                return why
        return None
    if stem in ("find", "bfs", "fd", "fdfind", "parallel"):
        return _owner_runs_given(stem, tokens, segment, state, depth)
    if stem in _SHELLS and depth < _OWNER_DEPTH:
        strings, stdin = _shell_strings(tokens)
        if stdin:
            strings = strings + _here_strings(segment) + _take_bodies(segment, state)
        for text in strings:
            why = _owner_line(text, state, depth + 1)
            if why:
                return why
    elif behind and depth < _OWNER_DEPTH and not rules.known_program(stem):
        # A program no table knows, started by a wrapper or runner and handed
        # `-c`/`-e`/`--command STRING`, may be a shell: its string is a line
        # (delta review of PR #75, M-B).
        for text in rules.command_strings(tokens):
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
                return why + (" — a backtick or $(…) runs its command, even inside double quotes; "
                              "single-quote the text to only mention it")
    for segment in rules.segments(command):
        # The same cheap test per segment: one that cannot hold an op (and
        # sets no variable, reads no here-document and names no launcher a
        # later `ln` could alias) is not parsed — on a long line this is
        # where the time goes.
        # A line that kills is parsed whole: any segment may be the source
        # that feeds the kill its PIDs (`lsof -ti :8405 | xargs kill`).
        if state.filtered and not (state.kills.armed or _could_be_owner_op(segment) or "<<" in segment
                                   or "jarvis" in segment or "=" in segment or state.variables
                                   or state.launchers):
            continue
        segment = _strip_comment(segment)
        if _cgroup_source(segment):
            state.kills.sources.append(lambda: "its cgroup's `cgroup.procs`")
        words_only = _mark_quoted(rules.strip_redirections(segment))
        try:
            raw = shlex.split(words_only)
        except ValueError:
            raw = words_only.split()
        raw = _strip_reserved(_strip_closers(raw))
        if not raw:
            continue
        words = raw[1:] if raw[0] in ("export", "declare", "local", "readonly") else raw
        if words and all(re.fullmatch(r"[A-Za-z_]\w*=.*", w) for w in words):
            for word in words:
                name, value = word.split("=", 1)
                state.variables[name] = _unmark(_owner_word(value, state))
            continue
        why = _owner_command(raw, segment, state, depth)
        if why:
            return why
    if depth == 0 and state.kills.kill:
        # A `kill` fed PIDs, on this line, by a source that yields the
        # daemon's: `kill $(pgrep -f daemon2)`, `pgrep -f daemon2 | xargs
        # kill`, `lsof -ti :8405 | xargs kill`. The sources are judged only
        # now — `pgrep -f …` alone is a read, and its pattern never runs.
        for source in state.kills.sources:
            name = source()
            if name:
                return _fed_why(name)
        name = _listings_filtered_by_grep(state.kills)
        if name:
            return _fed_why(name)
    return None


# Shell reserved words a command may follow (review 2 of PR #48: `if true;
# then systemctl --user restart jarvis-daemon2; fi` split into a segment
# whose first word was `then`, and was not judged).
_RESERVED = frozenset({"if", "then", "else", "elif", "while", "until", "do", "!", "{", "coproc"})


def _strip_reserved(tokens: list[str]) -> list[str]:
    """The command a segment runs, past the shell's reserved words (`then
    jarvis restart`, `do pkill -f daemon2`, `! cmd`, `{ cmd`, `coproc cmd`)
    and a `case` pattern (`case x in a) cmd`, `b) cmd`)."""
    for _ in range(32):
        if not tokens:
            break
        head = tokens[0]
        if head in _RESERVED:
            tokens = tokens[1:]
        elif head == "case":
            tokens = tokens[tokens.index("in") + 1:] if "in" in tokens else []
        elif head.endswith(")") and head.count(")") > head.count("("):
            tokens = tokens[1:]                      # a case pattern: `a)`, `*)`, `(a|b)`
        elif head.startswith("(") and head.endswith(")") and len(tokens) > 1 and head.count("(") == 1:
            tokens = tokens[1:]                      # `(a)` — a case pattern with its paren
        else:
            break
    return tokens


def runs_jarvis(tokens: list[str]) -> str | None:
    """What of Jarvis's own this command runs — "the jarvis CLI" or a module
    `python -m jarvis…` — after the same wrappers and runners `owner_op`
    peels; None for anything else. `rules.decide` asks it so that Jarvis's
    own CLI is never auto-approved in any spelling (review of PR #44 at
    75f14c2: `python -m jarvis …` and `uv run jarvis …` were ALLOW, and the
    CLI has human-only verbs — `auth`, `voice clone`, `discord commands
    --sync`, the owner ops)."""
    state = _Owner()
    try:
        with rules.work_scope():
            readings = _peel_runner(list(tokens), state)
    except rules.TooComplex:
        rules.exhaust()     # past the budget or a cap: the line is asked (review of PR #75)
        return None
    except Exception:     # noqa: BLE001 — a parse failure is not a verdict
        return None
    for tokens, _, joined, _behind in readings:
        found = None if joined or not tokens else _runs_jarvis_reading(tokens, state)
        if found:
            return found
    return None


def _runs_jarvis_reading(tokens: list[str], state: _Owner) -> str | None:
    if _is_launcher(tokens[0], state):
        return "the jarvis CLI"
    stem = rules._basename(tokens[0])
    if _PYTHON.fullmatch(stem) or stem == "coverage":
        # Module runners peeled (`-m runpy jarvis …`, `-m cProfile -m jarvis …`).
        targets = (_python_targets(tokens[1:], state) if stem != "coverage"
                   else _runner_targets("coverage", tokens[1:]))
        for kind, what, _ in targets:
            if kind == "module" and what and (what.split(".")[0] == "jarvis" or _WILD.search(what)):
                return f"Jarvis's own `{what}`"
            if kind == "script" and what and rules._basename(what) in _LAUNCHERS:
                return "the jarvis CLI"
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
            or any(word in flat for word in _DAEMON_WORDS)
            or any(word in flat for word in _KILL_WORDS)):
        return True
    # The CLI with a word only known at run time (`jarvis h${E}ud install`).
    if "jarvis" in flat and _WILD.search(flat):
        return True
    # A word the line assembles from its own variables (`x=h; jarvis ${x}ud
    # install`) need not spell the op's word anywhere: such a line is parsed.
    return "$" in flat and re.search(r"(?:^|[\s;&|(])[A-Za-z_]\w*=", flat) is not None


def owner_reading(command: str, *, prefilter: bool = True) -> tuple[str | None, str | None]:
    """(why this line runs an owner-only operation — a refusal, never an ask —
    or None; and, when it does not, why it must be asked whatever stands: a
    kill on it whose pattern is too costly to judge against the daemon, and
    nothing on it provably selects the daemon, or None). `prefilter=False`
    turns both cheap tests off (the line's and each segment's), for the test
    that holds them to the full parse."""
    if not command or not command.strip():
        return None, None
    if prefilter and not _could_be_owner_op(command):
        return None, None
    flat = command.replace("\\\n", "")
    for ch in ("'", '"', "\\"):
        flat = flat.replace(ch, "")
    kills = _Kills(line=command, armed="kill" in flat or "fuser" in flat)
    try:
        with rules.work_scope():
            why = _owner_line(command, _Owner(filtered=prefilter, kills=kills), 0)
    except RecursionError:
        return None, None
    except rules.TooComplex:
        # What its wrappers and runners start is past the work budget or a
        # cap (review of PR #75, L2): not judged here, so the line is too
        # complex to judge — `rules.decide` asks it, and `rules.runners_unsure`
        # keeps every allowlist entry and v2's reviewer off it.
        rules.exhaust()
        return None, None
    return why, (None if why else kills.unsure)


def owner_op(command: str, *, prefilter: bool = True) -> str | None:
    """Why this line runs an owner-only `jarvis` operation, or None — a
    refusal, never an ask (`owner_reading`)."""
    return owner_reading(command, prefilter=prefilter)[0]


def kill_unsure(command: str) -> str | None:
    """Why no allowlist entry and no v2 profile may answer for this line
    without asking: a kill on it is too costly to judge against the daemon
    (`COSTLY`; the delta review of PR #48 — too complex to judge is ASK,
    never DENY). `rules.decide` asks it through `owner_reading`;
    `permissions.allows` and v2's layer 2 ask it here."""
    return owner_reading(command)[1]


def installs_hud(command: str, cwd: str | Path | None = None) -> str | None:
    """`owner_op`, under the name the first callers used."""
    return owner_op(command)
