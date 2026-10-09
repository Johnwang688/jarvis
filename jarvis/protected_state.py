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
allowlist's are never put to the owner as a yes/no; command substitution was
already ASK, so nothing hidden that way auto-runs either. The real boundary is
filesystem permissions — the gate's state owned by someone the agent's process
is not — and that is recorded as the residual gap.

Every path is read from `config` per call, so a suite that repoints
`ALLOWLIST_PATH` and friends at a temp directory protects the temp files.

This module is SELF_PROTECTED (tools/files.py): it decides what never runs.
"""

from __future__ import annotations

import os
import re
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
# warning about. `mv` is not here — moving a file away is a write to it — and
# neither is `ln`, because a hard link made now is a write path later.
_COPIERS = frozenset({"cp", "rsync", "scp", "install"})
_GLOB = re.compile(r"[*?\[]")


@dataclass(frozen=True)
class Touch:
    """One protected file a command line reaches.

    `certain` means the write provably lands on that file — a redirect onto
    it, `cp` onto it, `ln` to it. Uncertain means the line *might*: a
    directory copy into the gate's folder, an unknown program handed the
    path, a relative name under a working directory nobody can track.
    """
    path: Path
    certain: bool
    why: str

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class _State:
    cwd: Path | None                       # None: a `cd` we could not follow
    variables: dict[str, str] = field(default_factory=dict)


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


def _redirect_targets(segment: str) -> list[str]:
    """Every word the segment redirects output into (`>`, `>>`, `>|`, `&>`,
    `<>`, glued or spaced). File-descriptor duplication (`2>&1`) is not one."""
    syntax = set(rules.unquoted_indices(segment))
    targets: list[str] = []
    i = 0
    while i < len(segment):
        if i not in syntax or segment[i] != ">":
            i += 1
            continue
        j = i + 1
        if segment[j:j + 1] in (">", "|"):
            j += 1
        while segment[j:j + 1] in (" ", "\t"):
            j += 1
        if segment[j:j + 1] == "&":
            k = j + 1
            while segment[k:k + 1].isdigit():
                k += 1
            if k > j + 1 or segment[k:k + 1] == "-":
                i = k + 1
                continue
            j += 1  # bash's `>&word` is `&>word`
        start, quote = j, ""
        while j < len(segment):
            ch = segment[j]
            if quote:
                if ch == quote:
                    quote = ""
            elif ch in "'\"":
                quote = ch
            elif ch in " \t;|&<>()":
                break
            j += 1
        word = rules._tokens(segment[start:j])
        if word:
            targets.append(word[0])
        i = max(j, i + 1)
    return targets


def _operands(tokens: list[str]) -> list[str]:
    """Arguments that may be paths: plain words, and the value side of
    `of=…` / `--output=…`. Bare flags are not paths."""
    out = []
    for token in tokens:
        if token.startswith("-"):
            if "=" in token:
                out.append(token.split("=", 1)[1])
            continue
        if re.match(r"[A-Za-z_]\w*=", token):
            out.append(token.split("=", 1)[1])
            continue
        out.append(token)
    return out


def _destination(stem: str, tokens: list[str], operands: list[str]) -> str | None:
    """For a copying program, the one operand it writes; None if it writes
    every operand (or links them, which is a write by another name)."""
    if stem not in _COPIERS:
        return None
    if stem == "cp" and any(t in ("-l", "-s", "--link", "--symbolic-link") or
                            (re.fullmatch(r"-[a-zA-Z]+", t) and ("l" in t or "s" in t))
                            for t in tokens[1:]):
        return None
    for k, token in enumerate(tokens[1:], 1):
        if token in ("-t", "--target-directory") and k + 1 < len(tokens):
            return tokens[k + 1]
        if token.startswith("--target-directory="):
            return token.split("=", 1)[1]
    return operands[-1] if operands else None


def _peel(tokens: list[str], state: _State) -> tuple[list[str], Path | None]:
    """Strip subshell punctuation and wrappers; return the real command and
    any directory `env -C` would run it in."""
    tokens = list(tokens)
    while tokens and tokens[0] in ("(", "{", "!"):
        tokens = tokens[1:]
    if tokens and tokens[0].startswith("(") and len(tokens[0]) > 1:
        tokens[0] = tokens[0][1:]
    chdir = None
    if tokens and rules._basename(tokens[0]) == "env":
        rest, k = tokens[1:], 0
        while k < len(rest) and (rest[k].startswith("-") or "=" in rest[k]):
            if rest[k] in ("-C", "--chdir") and k + 1 < len(rest):
                target = _expand(rest[k + 1], state)
                chdir = Path(target) if target else None
                k += 2
                continue
            if rest[k].startswith("--chdir="):
                target = _expand(rest[k].split("=", 1)[1], state)
                chdir = Path(target) if target else None
            k += 1
        tokens = rest[k:]
    return rules.unwrap(tokens), chdir


@dataclass
class _Seen:
    touches: list[Touch] = field(default_factory=list)
    writes: bool = False                           # runs a writing stem
    read: set[Path] = field(default_factory=set)   # protected files only read


def _strip_closers(tokens: list[str]) -> list[str]:
    """`(cd d; cp a b)` leaves `b)` as the last token; the paren is syntax."""
    if tokens and tokens[-1].endswith((")", "}")) and tokens[-1] not in (")", "}"):
        tokens = tokens[:-1] + [tokens[-1].rstrip(")}")]
    return [t for t in tokens if t not in (")", "}")]


def _kind(stem: str, tokens: list[str]) -> str:
    """writer / reader / unknown, for the paths this command is handed."""
    if stem in _WRITERS or stem in _INTERPRETERS or stem in _SHELLS:
        return "writer"
    if rules.writes_anyway(tokens):  # `sed -i`, `find -delete`, `find -exec`
        return "writer"
    if stem == "sort" and any(t in ("-o", "--output") or t.startswith(("-o", "--output="))
                              for t in tokens[1:]):
        return "writer"
    if stem in _READERS:
        return "reader"
    # An unrecognised stem may be a wrapper this module does not know
    # (`timeout -s KILL 5 cp …` leaves `KILL` as the stem): the first writing
    # word behind it decides.
    if any(rules._basename(t) in _WRITERS for t in tokens[1:]):
        return "writer"
    return "unknown"


def _segment(segment: str, state: _State, protected: set[Path], ids: dict,
             depth: int) -> _Seen:
    seen = _Seen()
    raw = _strip_closers(rules._tokens(segment))
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

    local = _State(chdir if chdir is not None else state.cwd, state.variables)

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

    # A shell handed a string is a command line in its own right.
    if stem in _SHELLS and depth < 3:
        inline = None
        if stem == "eval":
            inline = " ".join(tokens[1:])
        elif "-c" in tokens[1:]:
            idx = tokens.index("-c")
            inline = tokens[idx + 1] if idx + 1 < len(tokens) else None
        if inline:
            inner = _analyse(inline, _State(local.cwd, dict(state.variables)),
                             protected, ids, depth + 1)
            seen.touches += inner.touches
            seen.writes = inner.writes
            seen.read = inner.read
            return seen

    kind = _kind(stem, tokens)
    seen.writes = kind == "writer"
    if kind == "writer" and stem not in _WRITERS | _INTERPRETERS | _SHELLS | _READERS:
        # Name the writing word behind an unknown wrapper, not the wrapper.
        stem = next((rules._basename(t) for t in tokens[1:]
                     if rules._basename(t) in _WRITERS), stem)
    operands = _operands(tokens[1:])

    # Inline source names its files inside a string no tokenizer splits. A
    # full path, or a bare name while standing in the gate's own folder, is a
    # write; a bare name anywhere else may be a project's own models.json, so
    # it asks rather than refuses.
    if stem in _INTERPRETERS:
        suffixes = tuple(p.name for p in protected)
        source = " ".join(t for t in tokens[1:] if not (t in operands and t.endswith(suffixes)))
        for path in protected:
            if path.name not in source:
                continue
            spelled = (str(path) in source or "/.config/jarvis/" + path.name in source
                       or (local.cwd is not None and _resolve(local.cwd) == path.parent))
            seen.touches.append(Touch(path, spelled, f"{stem} source that names {path.name}"))

    destination = _destination(stem, tokens, operands)
    for operand in operands:
        text = _expand(operand, local)
        if text is None:
            # `$UNSET/allowlist.json`, or a relative name after a `cd` that
            # could not be followed: the right name, an unknowable place.
            for path in protected:
                if Path(operand).name == path.name and kind != "reader":
                    seen.touches.append(Touch(path, False, f"{stem} given {operand}"))
            continue
        is_source = destination is not None and operand != destination
        hit = _exact(text, protected, ids)
        if hit is not None:
            if kind == "reader" or is_source:
                seen.read.add(hit)
            else:
                seen.touches.append(Touch(hit, kind == "writer", f"{stem} on {hit.name}"))
            continue
        if kind == "reader" or is_source:
            continue
        others = [o for o in operands if o != operand]
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


def _analyse(command: str, state: _State, protected: set[Path], ids: dict,
             depth: int = 0) -> _Seen:
    total = _Seen()
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


def command_touches(command: str) -> list[Touch]:
    """Every protected file this command line reaches, as far as can be seen."""
    if not command or not command.strip():
        return []
    protected = protected_paths()
    try:
        cwd: Path | None = Path.cwd()
    except OSError:
        cwd = None
    return _analyse(command, _State(cwd), protected, _identities(protected)).touches


def command_touch(command: str) -> Touch | None:
    """The most serious protected file this line reaches, or None.

    Order: a certain write to the allowlist, any other certain write, then the
    uncertain ones in the same order.
    """
    touches = command_touches(command)
    if not touches:
        return None
    gate = gate_paths()
    return min(touches, key=lambda t: (not t.certain, t.path not in gate))


def refused(command: str) -> Touch | None:
    """The touch that makes this line unrunnable by Jarvis, or None: a write
    that provably lands on the allowlist. Everything else this module sees is
    an ASK, decided by the caller."""
    touch = command_touch(command)
    if touch is not None and touch.certain and touch.path in gate_paths():
        return touch
    return None


def refusal(touch: Touch) -> str:
    return (f"{touch.why}: {touch.name} is the approval gate's own allowlist, and "
            "a command that writes it would decide what runs without anyone "
            "being asked. Only the owner changes it, by hand")


def ask_reason(touch: Touch) -> str:
    return (f"{touch.why}: {touch.name} is Jarvis's own configuration, so this "
            "never runs unasked and no allowlist entry covers it")
