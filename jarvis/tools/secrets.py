"""Protection for credential files: `.env`, `.env.local`, `google_token.json`.

These files hold live credentials, so they are the one thing on disk that
must never reach a tool message. Anything that lands in the transcript is on
its way to a third-party API and into `traces/` — deleting the message after
the fact does not undo that. The only fix is rotating the key.

Three layers, because there is more than one way to read a file:

  1. `read_file` refuses them by name.
  2. `run_readonly` / `run_command` refuse a command that names one, including
     a dotted glob that could expand onto one (`cat .env*`).
  3. Whatever a command prints anyway is scrubbed in `dispatch()` before it
     becomes a tool message.

Layer 3 is the one that matters. The leak this was written for was
`grep -RIn 'provider|openrouter' ~/projects/Jarvis`, which never names `.env`
and printed the key anyway; layers 1 and 2 would both have waved it through.

Layer 3 is a backstop against accidents, not a sandbox. A model that wanted to
defeat it could still encode a value past it (base64, reversed, one character
per line). The real boundary is filesystem permissions on the key itself; this
buys the time to notice.

Coverage: every **credential env file** (the owner's `.env*` rule, below:
`.env` and `.env.<anything>` at any depth, except a name ending `.example`)
and the use-but-never-see credential bundles — `google_token.json` (Gmail),
`onshape_keys.json` (CAD), `discord_token.json`, and `spotify_token.json`.
`.env.example` and `.env.local.example` are meant to be read, and are the only
env files Jarvis may write (ENV-1, 2026-10-12).
"""

from __future__ import annotations

import json
import os
import re
import stat as _stat
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from .. import config

# The fixed names: `.env`, `.env.local` and the credential bundles. Every
# *other* credential env file (`.env.production`, `.env.staging.local`, …) is
# covered by the name rule (`is_env_credential_name`), which `is_protected`
# asks too — so this set is the bundles plus the two env names that were here
# first, not the whole of what is protected (ENV-1, 2026-10-12).
PROTECTED_NAMES = frozenset(
    {
        ".env",
        ".env.local",
        "google_token.json",
        "onshape_keys.json",
        "discord_token.json",
        "spotify_token.json",
    }
)

# Values shorter than this are things like `DEBUG=1` or `PORT=8080`. Redacting
# them would mangle unrelated output for no gain.
MIN_SECRET_LEN = 8

REDACTED = "[redacted: value from a protected credential file]"

_MAX_FILE_BYTES = 100_000
_JARVIS_ROOT = Path(__file__).resolve().parents[2]
_HEADER = re.compile(r"^==>\s*(?P<path>.+?)\s*<==$")


def refusal(name: str) -> str:
    return (
        f"Error: {name} is protected. It holds live credentials, and anything "
        "read from it would be sent to the model API and written to the trace "
        "files. Ask the user for the value you need instead."
    )


def write_refusal(name: str) -> str:
    """What a write tool says to a credential env file (ENV-1): the owner
    edits those in the HUD; Jarvis writes only the `*.example` ones."""
    return (
        f"Error: {name} is protected: it is (or leads to) a credential env file. Jarvis "
        "never reads or writes one; the owner edits it in the HUD's editor. Only env files whose name "
        "ends in .example (.env.example, .env.local.example) are Jarvis's to "
        "write — put placeholders there and tell the owner what to set."
    )


def is_protected(path: str | Path) -> bool:
    """True if `path`'s own name is a credential file's, at any depth: a
    bundle or `.env`/`.env.local` (`PROTECTED_NAMES`), or any credential env
    file by the owner's `.env*` rule (`is_env_credential_name`).

    By name only — no filesystem, so it answers for a path that does not
    exist and for a name from elsewhere (a grep line's attribution). Ask
    `is_credential` about a real path: it also follows symlinks."""
    name = PurePosixPath(str(path)).name
    return name in PROTECTED_NAMES or is_env_credential_name(name)


def is_credential(path: str | Path) -> bool:
    """`is_protected`, plus a credential env file reached through a symlink
    (`is_env_credential`: every hop of the last component and where it
    resolves). What v1's file tools ask about a path they are handed."""
    return is_protected(path) or is_env_credential(path)


# --- credential env files (2026-10-11) ---------------------------------------
#
# The owner: "I need to be able to edit .env* files in the hud but Jarvis
# should never be able to read them", and Jarvis may write only the ones whose
# name ends in `.example` (docs/plans/2026-10-11-env-files.md). So a
# **credential env file** is any file named `.env` or `.env.<anything>`, at any
# depth, except a name ending in `.example`: `.env`, `.env.local`,
# `.env.production`, `.env.development.local` and `.env.prod.bak` are
# credential; `.env.example` and `.env.local.example` are not; nor are
# `.envrc` or `myapp.env`, which are not `.env` or `.env.<anything>`.
#
# **This is the one definition.** The HUD's editor (owner-editable, owner-only
# routes) and every refusal on Jarvis's side ask it; nothing keeps a copy,
# because two copies of a rule drift and the looser one decides.
#
# Case-insensitive, because Windows drives are (`/mnt/c/…/.ENV.Production` is
# the same file as `.env.production` there), and stricter that way everywhere
# else. A name Windows would trim (trailing dots or spaces: `.env ` is `.env`
# on NTFS) is judged both as written and as trimmed, and is credential if
# either is.
_ENV_NAME = re.compile(r"\.env(?:\..*)?", re.IGNORECASE | re.DOTALL)


def is_env_credential_name(name: str) -> bool:
    """The name rule alone, on one file name: no directory, no filesystem.

    For names that are not paths on this machine (an attachment's name).
    `is_env_credential` is the one to ask about a path."""
    if not isinstance(name, str) or not name:
        return False
    for candidate in {name, name.rstrip(" .")}:
        folded = candidate.casefold()
        if _ENV_NAME.fullmatch(folded) and not folded.endswith(".example"):
            return True
    return False


_SYMLINK_HOPS = 40


def is_env_credential(path: str | Path) -> bool:
    """True if `path` is a credential env file (see above), by its own name,
    the name of every symlink its last component passes through, or the name
    of the file it finally resolves to.

    Symlinks are resolved both ways: a symlink named `.env.example` that
    points at `.env` is credential (the bytes it reaches are), and a symlink
    named `.env` is credential whatever it points at (it is read as `.env`).
    A path that does not exist is judged by its name. A symlink that cannot
    be followed (unreadable, a loop past 40 hops) is credential: a guard that
    cannot tell refuses."""
    try:
        raw = Path(os.fspath(path))
    except TypeError:
        return False
    if is_env_credential_name(raw.name):
        return True
    try:
        current = Path(os.path.abspath(raw))
        for _ in range(_SYMLINK_HOPS):
            if not current.is_symlink():
                break
            target = Path(os.readlink(current))
            current = target if target.is_absolute() else current.parent / target
            if is_env_credential_name(current.name):
                return True
        else:
            return True  # still a symlink after 40 hops: cannot tell
        return is_env_credential_name(Path(os.path.realpath(raw)).name)
    except (OSError, RuntimeError, ValueError):
        return True


# Each protected name as it appears in a raw command line, bounded so it is a
# filename rather than a fragment of a longer one.
#
# The token loop below treats every shlex token as a path and takes its
# basename, which misses every shape where the filename is not a token of its
# own — and there are a lot of them:
#
#     cat<.env                       an operator glued to the name
#     sh -c 'cat .env'               the payload is one token
#     env sh -c 'cat .env'           ditto, wearing a wrapper
#     python -c "open('.env')"       ditto, in another language
#     F=.env; cat $F                 the name never touches the binary
#     cat $(echo .env)               nor does it here
#
# So the raw line is scanned too. The boundaries are what keep `.env.example`
# and `.envrc` readable: a match may not be preceded or followed by a name
# character, and `.` counts as one — `.env.local` matches its own entry, and
# `.env` inside `.env.example` matches nothing.
#
# It does cost a false positive: `git commit -m "ignore .env"` is now refused
# for naming a file it only mentions. That is the deliberate direction. This
# layer's stated job is that approving a command is not consent to what it
# prints, and a clear refusal the model can rephrase past is cheaper than a
# credential in the transcript, which is only fixable by rotating the key.
_NAME_CHAR = r"A-Za-z0-9_.\-"
_RAW_NAMES = [
    (name, re.compile(rf"(?<![{_NAME_CHAR}]){re.escape(name)}(?![{_NAME_CHAR}])"))
    for name in sorted(PROTECTED_NAMES)
]

# Every credential env file in a raw line (ENV-1): `.env`, then optionally
# `.` and the rest of the word — up to whitespace, a quote, a shell operator,
# a path separator, or `:`/`,`/`=`/`}` (a volume spec, a brace list, an
# assignment). The word found is then judged by the one name rule, which is
# what keeps `.env.example` and `.env.local.example` readable and makes
# `.env.production`, `.env.example.bak` and `.env.$STAGE` credential. The
# left boundary is `_NAME_CHAR`'s, so `myapp.env.production` and `x.env` are
# not env files (their names do not start `.env`), and the empty-tail case
# keeps `.envrc` and `.env-cmdrc` out.
_ENV_IN_TEXT = re.compile(
    rf"(?<![{_NAME_CHAR}*?\]])(\.env(?:\.[^\s'\"`;&|<>()/\\:,=}}]*)?)(?![A-Za-z0-9_\-])",
    re.IGNORECASE)
# (A glob character just before `.env` makes it the tail of a pattern, not a
# name: `find -name '*.env.ts'` is a non-dot-led glob, judged as one.)

# --- globs onto credential files (ENV-1 review, MED-1) ---------------------------
#
# **A glob is judged structurally: can it match a credential env name?** Not
# against a sample of names (the first version did, and `*.test`, `*.bak`,
# `ls *dev*`, `find -name '*test*'` and Claude's `**/*template*` were all
# refused, unapprovably, because `.env.test` and friends were in the sample).
#
# * **Only a dot-led glob can reach one.** Every credential env name starts
#   with `.`, and bash expands a `*`, `?` or bracket at the start of a name
#   onto no dotfile (the dot must be matched by a literal `.`; `dotglob` is
#   off unless a script sets it — not modelled here, and stated as a limit).
#   So `*.production` and `*test*` never expand onto `.env.production`. A
#   leading bracket set that holds `.` is treated as dot-led anyway.
# * **The test is exact**, not a list: the glob's language (literals, `?`,
#   bracket sets, `*`) is intersected with the rule's — `.env`, or `.env.`
#   then anything not ending `.example`, case-folded, plus a name Windows
#   would trim to `.env` — by walking both at once (`_glob_meets_env`). So
#   `.e?v.custom`, `.[e]nv.custom`, `.*.custom` and `.e*.custom` are refused
#   (the review's LOW-1), while `.env.*.example`, `.git*` and `.bash*` are
#   not. Brace lists are expanded first (bash's reading,
#   `protected_state._brace_expand`), and one too large to expand is refused.
# * **A pattern a program expands itself** — an option value that names
#   files (`find -name`, `rg -g`/`-g<glued>`, `grep --include`, fd's `-g`,
#   `zip -i`), a git pathspec after `--`, a `tar --wildcards`/`unzip`/`7z`
#   member, Claude's Grep `glob` — matches a leading dot with `*`, so it is
#   judged by `pattern_reaches`: main's rule over `.env`/`.env.local`
#   (`*.local`, `*env*`) as a floor, the dot-led exact test, and — when it
#   names `env` and the command reads what it selects — the exact test
#   alone (the PR #60 delta review). A `*test*` or `*.py` is not refused for
#   being able to match `.env.test`: a recursive read that happens to cover
#   an env file is the class of `grep -r foo .` — the scrub's job and a
#   stated limit — and refusing `--include='*.py'` while allowing `grep -r`
#   would protect nothing.
# * **A word bash does not expand** (quoted) keeps main's rule over `.env`
#   and `.env.local` whatever it is; **a line that turns dotglob on**
#   (`shopt -s dotglob`, `GLOBIGNORE`, zsh's `globdots` or `(D)`) has every
#   glob word judged by the exact test alone; and **a string another program
#   runs as a command line** (`-c`, `eval`, `env -S`, `watch`, `ssh host`, a
#   here-string to a shell, a command line inside `node -e`/`perl -e`) is
#   judged piece by piece and as a line of its own.
# * The credential *bundles* (`google_token.json` …) are not dotfiles, so a
#   bash word that is a glob is still matched against them by name, as on
#   main (`cat google_token.*`, `cat *.json`).

_GLOB_CAP = 512                       # a longer dot-led glob is refused unjudged
_EXAMPLE = ".example"
# The characters the rule names, and one stand-in for every other character.
_OTHER = "\x00"
_RELEVANT = frozenset(".envxampl ")
_ALPHABET = tuple(sorted(_RELEVANT)) + (_OTHER,)
_OTHER_PROBES = "bcdfghijkoqrstuwyz0123456789_-+@~,#%"


def _glob_elements(pattern: str) -> list[tuple[str, str]]:
    """A (case-folded) glob as elements: ("lit", c), ("?", ""), ("*", ""),
    ("set", "[...]"). A `[` with no closing `]` is a literal, as in bash."""
    out: list[tuple[str, str]] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if not out or out[-1][0] != "*":
                out.append(("*", ""))
            i += 1
        elif c == "?":
            out.append(("?", ""))
            i += 1
        elif c == "[":
            j = i + 1
            if j < n and pattern[j] in "!^":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:
                out.append(("lit", "["))
                i += 1
            else:
                body = pattern[i:j + 1]
                if body.startswith("[^"):
                    body = "[!" + body[2:]          # bash reads `^` as `!`
                out.append(("set", body))
                i = j + 1
        elif c == "\\" and i + 1 < n:
            out.append(("lit", pattern[i + 1]))
            i += 2
        else:
            out.append(("lit", c))
            i += 1
    return out


def _element_chars(element: tuple[str, str]) -> tuple[str, ...]:
    """The alphabet letters (and `_OTHER`) one element can stand for."""
    kind, value = element
    if kind in ("?", "*"):
        return _ALPHABET
    if kind == "lit":
        return (value if value in _RELEVANT else _OTHER,)
    chars = [c for c in sorted(_RELEVANT) if fnmatchcase(c, value)]
    if any(fnmatchcase(c, value) for c in _OTHER_PROBES):
        chars.append(_OTHER)
    return tuple(chars)


def _kmp_step(k: int, c: str) -> int:
    """How much of `.example` is matched after `c`, from `k` matched."""
    text = _EXAMPLE[:k] + c
    for length in range(min(len(text), len(_EXAMPLE)), 0, -1):
        if text.endswith(_EXAMPLE[:length]):
            return length
    return 0


def _env_step(state, c: str):
    """The credential-name automaton: `.env`, then `.` and a tail tracked by
    how much of `.example` it ends with, or trailing spaces/dots (a name
    Windows trims to `.env`). None is dead."""
    prefix = ".env"
    if isinstance(state, int):                      # read prefix[:state]
        if state < 4:
            return state + 1 if c == prefix[state] else None
        if c == ".":
            return ("tail", 1)
        return "trim" if c == " " else None
    if state == "trim":
        return "trim" if c in " ." else None
    return ("tail", _kmp_step(state[1], c))


def _env_accepts(state) -> bool:
    return state == 4 or state == "trim" or (
        isinstance(state, tuple) and state[1] != len(_EXAMPLE))


def _glob_meets_env(elements: list[tuple[str, str]]) -> bool:
    """Is some string both matched by the glob and a credential env name?"""
    start = (0, 0)
    seen = {start}
    stack = [start]
    m = len(elements)
    while stack:
        i, state = stack.pop()
        if i == m:
            if _env_accepts(state):
                return True
            continue
        kind = elements[i][0]
        moves = []
        if kind == "*":
            moves.append((i + 1, state))
        for c in _element_chars(elements[i]):
            nxt = _env_step(state, c)
            if nxt is not None:
                moves.append((i if kind == "*" else i + 1, nxt))
        for move in moves:
            if move not in seen:
                seen.add(move)
                stack.append(move)
    return False


def _dot_led(elements: list[tuple[str, str]]) -> bool:
    if not elements:
        return False
    kind, value = elements[0]
    return (kind == "lit" and value == ".") or (kind == "set" and fnmatchcase(".", value))


def env_glob_reaches(pattern: str, *, dot_led_only: bool = True) -> bool:
    """Could this glob (one file name's worth) expand onto a credential env
    file? As bash expands it, only a dot-led glob can (see above); then the
    test is exact. `dot_led_only=False` is the exact test alone — for a shell
    with `dotglob` on, or a program whose `*` matches a leading dot."""
    if not isinstance(pattern, str) or not pattern:
        return False
    if "{" in pattern:
        from .. import protected_state

        variants = protected_state._brace_expand(pattern)
        if variants is None:
            return True                    # too large to expand: cannot tell, refuse
        if len(variants) > 1 or variants[0] != pattern:
            return any(is_env_credential_name(v)
                       or env_glob_reaches(v, dot_led_only=dot_led_only) for v in variants)
    if not any(c in pattern for c in "*?["):
        return False
    elements = _glob_elements(pattern.casefold())
    if dot_led_only and not _dot_led(elements):
        return False
    if len(pattern) > _GLOB_CAP:
        return True
    return _glob_meets_env(elements)


_BUNDLE_NAMES = frozenset(n for n in PROTECTED_NAMES if not is_env_credential_name(n))
# Where a word splits into the pieces `protected_in_command` also judges.
_PIECES = re.compile(r"[\s;&|<>()`'\"=:,]+")


def env_credential_in_text(text: str) -> str | None:
    """The first credential env file named anywhere in `text` (a raw command
    line, a script, an argument), or None. The raw-line half of
    `protected_in_command`; terminal_read's guard asks it too."""
    for match in _ENV_IN_TEXT.finditer(text):
        if is_env_credential_name(match.group(1)):
            return match.group(1)
    return None


def glob_reaches(pattern: str) -> bool:
    """Could this bash word (one file name's worth) expand onto a credential
    file? A credential env file by `env_glob_reaches` (dot-led, exact), or a
    credential bundle by name, as on main (`cat google_token.*`; only with
    some literal substance, so a bare `grep -R foo *` stays usable).

    Brace lists are expanded first, as bash does; one too large to expand is
    refused (a guard that cannot tell refuses)."""
    if not isinstance(pattern, str) or not pattern:
        return False
    if "{" in pattern:
        from .. import protected_state

        variants = protected_state._brace_expand(pattern)
        if variants is None:
            return True
        if len(variants) > 1 or variants[0] != pattern:
            return any(is_protected(v) or glob_reaches(v) for v in variants)
    return env_glob_reaches(pattern) or _bundle_glob(pattern)


def _names_glob(pattern: str, names) -> bool:
    """Main's glob rule: a glob with some literal substance that matches one
    of `names` (`google_token.*`, `*.json`, `*.local`, `*env*`)."""
    if not any(c in pattern for c in "*?["):
        return False
    if not re.sub(r"[*?\[\]]", "", pattern):
        return False
    folded = pattern.casefold()
    return any(fnmatchcase(name, folded) for name in names)


def _bundle_glob(pattern: str) -> bool:
    """A glob matching a credential bundle's name — main's rule, unchanged."""
    return _names_glob(pattern, _BUNDLE_NAMES)


_ENV_FLOOR_NAMES = frozenset(PROTECTED_NAMES - _BUNDLE_NAMES)      # `.env`, `.env.local`


def main_env_glob(pattern: str) -> bool:
    """Main's glob rule over the two env names main protected (`.env`,
    `.env.local`): `*.local`, `*env*`, `.env*`. The floor under every
    pattern bash does not expand alone — a quoted word, a pattern a program
    expands itself, Claude's Grep glob — so a security change does not loosen
    what main refused (PR #60 delta review). (The bundles keep main's whole
    rule on every shell word, `_bundle_glob`; they live in Jarvis's config
    folder, so a project search's `*.json` is not held to them.)"""
    return _names_glob(pattern, _ENV_FLOOR_NAMES)


# Options whose value (the next word, or after `=`) is a file-name pattern a
# program matches against names: find's, ripgrep's, grep's, fd's, zip's.
_NAME_PATTERN_OPTIONS = frozenset({
    "-name", "-iname", "-path", "-ipath", "-wholename", "-iwholename", "-lname", "-ilname",
    "-g", "--glob", "--iglob", "--include", "--exclude", "--include-dir", "--exclude-dir",
})
# The short one that may be glued to its value: `rg -g'.e?v*'`, `rg -g.e?v*`.
_GLUED_PATTERN_OPTION = "-g"
_WORD_BREAK = frozenset(";&|<>()")
# `-c` alone or in a short-option cluster (`-lc`, `-ec`): a shell's (or an
# interpreter's) inline program follows.
_INLINE_FLAG = re.compile(r"-[A-Za-z]*c[A-Za-z]*")
# An interpreter's inline program: `node -e`/`-p`, `perl -ne`/`-E`, `ruby -e`,
# `php -r`, `--eval`, `--print`.
_CODE_FLAG = re.compile(r"-[A-Za-z]*[eE][A-Za-z]*|--eval|--print|-p|-r")
_CODE_LITERAL = re.compile(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"|`((?:[^`\\]|\\.)*)`",
                           re.DOTALL)
_SHELL_STEMS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "mksh", "ash", "fish", "busybox"})
_INTERPRETER_STEMS = frozenset({"node", "nodejs", "perl", "ruby", "php", "deno", "bun",
                                "python", "python2", "python3", "lua"})
# Programs that run a command line they are handed as a string.
_STRING_RUNNERS = frozenset({"watch", "ssh", "autossh", "mosh"})
# Wrappers stepped over to find what a segment runs (with their options and
# a duration: `timeout 5`, `nice -n 5` is approximate, and only decides
# which context a word is judged in — main's floor holds in every context).
_WRAPPERS = frozenset({"sudo", "doas", "nohup", "time", "command", "builtin", "exec", "nice",
                       "ionice", "stdbuf", "timeout", "env", "chronic", "unbuffer"})
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=.*", re.DOTALL)
_DURATION = re.compile(r"\d+(?:\.\d+)?[smhd]?")
# What reads the files a name pattern selects (fix 2): an action run on each
# match (find's `-exec`/`-ok`, fd's `-x`/`-X`), `xargs` anywhere on the line,
# a content search, or an archiver. Any action counts, not a list of readers:
# this only ever decides a pattern that names `env`.
_EXEC_WORDS = frozenset({"-exec", "-execdir", "-ok", "-okdir", "-x", "--exec", "-X",
                         "--exec-batch"})
_SEARCHERS = frozenset({"grep", "egrep", "fgrep", "zgrep", "rg", "ag", "ack", "ugrep"})
_ARCHIVERS = frozenset({"tar", "bsdtar", "zip", "unzip", "7z", "7za", "7zz"})
# A line that makes bash's (or zsh's) globs match a leading dot (fix 1):
# `shopt -s dotglob`, `bash -O dotglob`, `GLOBIGNORE=…`, zsh's `setopt
# globdots` (any case, any underscores) and its `(D)` glob qualifier.
_DOTGLOB_WORDS = ("dotglob", "globdots", "globignore")
_ZSH_DOT_QUALIFIER = re.compile(r"(?<=[*?\]])\([^()\s]*D[^()\s]*\)")
_MAX_DEPTH = 3                     # strings run by a shell, inside strings run by a shell


def _dotglob_on(command: str) -> bool:
    folded = command.casefold().replace("_", "")
    return any(word in folded for word in _DOTGLOB_WORDS) or bool(
        _ZSH_DOT_QUALIFIER.search(command))


class _Word(NamedTuple):
    value: str          # the word as bash passes it, quotes removed
    expands: bool       # a glob character stands outside quotes (bash expands it)
    seg: int            # which command of the line (`;`, `&`, `|`, `(`, `)`, newline)
    op: str             # the operator just before it (`<<<` marks a here-string)


def _shell_words(command: str) -> list[_Word]:
    """The command's words as bash splits them, unquoted, each with whether
    a glob character in it stands outside quotes (so bash expands it), which
    command of the line it belongs to, and the operator before it. Approximate
    where bash is subtle (`$'…'`, here-documents): this decides only where,
    and how, a glob is judged."""
    words: list[_Word] = []
    value: list[str] = []
    expands = False
    started = False
    quote = ""
    seg = 0
    pending = ""
    word_op = ""
    i, n = 0, len(command)

    def begin() -> None:
        nonlocal started, word_op, pending
        if not started:
            started, word_op, pending = True, pending, ""

    def end() -> None:
        nonlocal value, expands, started
        if started:
            words.append(_Word("".join(value), expands, seg, word_op))
        value, expands, started = [], False, False

    while i < n:
        c = command[i]
        if quote:
            if c == quote:
                quote = ""
            elif c == "\\" and quote == '"' and i + 1 < n:
                i += 1
                value.append(command[i])
            else:
                value.append(c)
        elif c in "'\"":
            begin()
            quote = c
        elif c == "\\" and i + 1 < n:
            begin()
            i += 1
            value.append(command[i])
        elif c.isspace() or c in _WORD_BREAK:
            end()
            after_redirect = i > 0 and command[i - 1] in "<>"
            if c == "\n" or (c in ";|()" and not (c == "|" and after_redirect)):
                seg, pending = seg + 1, ""
            elif c == "&" and not (after_redirect or (i + 1 < n and command[i + 1] == ">")):
                seg, pending = seg + 1, ""
            elif not c.isspace():
                pending += c
        else:
            begin()
            if c in "*?[{":
                expands = True
            value.append(c)
        i += 1
    end()
    return words


def _stem_at(values: list[str]) -> int:
    """Where a segment's program is: past assignments and wrappers (and their
    options and durations). `env -S` is a program of its own here."""
    i = 0
    while i < len(values):
        value = values[i]
        name = PurePosixPath(value).name
        if _ASSIGNMENT.fullmatch(value) or (i and (value.startswith("-")
                                                   or _DURATION.fullmatch(value))):
            i += 1
            continue
        if name in _WRAPPERS and not (name == "env" and any(
                v in ("-S", "--split-string") or v.startswith(("-S", "--split-string="))
                for v in values[i + 1:])):
            i += 1
            continue
        return i
    return len(values)


def _program_patterns(values: list[str], at: int, stem: str) -> set[int]:
    """The words a program expands as patterns itself (fix 4, the lead's
    option A): a pathspec after `--` in a git command, a member of `tar
    --wildcards` (bsdtar always), a `zip -i`/`-x` pattern, an `unzip` or `7z`
    member."""
    out: set[int] = set()
    rest = range(at + 1, len(values))
    if stem == "git":
        if "--" in values[at + 1:]:
            j = values.index("--", at + 1)
            out.update(range(j + 1, len(values)))
    elif stem == "bsdtar" or (stem == "tar" and any(v.startswith("--wildcards") for v in values)):
        out.update(k for k in rest if not values[k].startswith("-"))
    elif stem == "zip":
        listing = False
        for k in rest:
            if values[k] in ("-i", "-x", "--include", "--exclude"):
                listing = True
            elif values[k].startswith("-"):
                listing = False
            elif listing:
                out.add(k)
    elif stem in ("unzip", "7z", "7za", "7zz"):
        positional = [k for k in rest if not values[k].startswith("-")]
        out.update(positional[1 if stem == "unzip" else 2:])
    return out


def pattern_reaches(name: str, reads: bool) -> bool:
    """A file-name pattern a program matches against names (`find -name`,
    `rg -g`, `grep --include`, a git pathspec, a `zip -i` pattern), whose `*`
    matches a leading dot. Refused when:

    * main refused it (`main_env_glob`: `*.local`, `*env*`, `.env*`), quoted or
      not — the floor;
    * it is dot-led and can match a credential env name (exact);
    * or it names `env` and can match one, and the command reads what it
      selects (fix 2: `find -name '*.env.production' -exec cat`).

    A pattern like `*test*` or `*.py` is not refused for being able to match
    `.env.test` or `.env.py` alone: that would refuse `grep -r foo
    --include='*.py' .` and `find -name '*.py' -exec grep`, which read no
    more than `grep -r foo .` (the scrub's job, a stated limit)."""
    if main_env_glob(name) or env_glob_reaches(name):
        return True
    return reads and "env" in name.casefold() and env_glob_reaches(name, dot_led_only=False)


def _judge_string(text: str, dotglob: bool, depth: int) -> str | None:
    """A string another shell runs (`sh -c '…'`, `eval`, `env -S`, `watch`,
    `ssh host '…'`, a here-string): its pieces by name and the dot-led
    glob test, then the string as a command line of its own."""
    for piece in _PIECES.split(text):
        name = PurePosixPath(piece).name if piece else ""
        if name and (is_protected(name)
                     or env_glob_reaches(name, dot_led_only=not dotglob)):
            return name
    if depth < _MAX_DEPTH:
        return _judge_line(text, dotglob or _dotglob_on(text), depth + 1)
    return None


def _judge_code(code: str, dotglob: bool, depth: int) -> str | None:
    """An interpreter's inline program (`node -e`, `perl -e`): each string
    literal that holds a command line (`execSync('cat .e?v.x')`,
    `system("…")`, backticks). Not the program's own text, which is full of
    regexes (`/(.*)foo/`) a shell never expands."""
    for match in _CODE_LITERAL.finditer(code):
        literal = next(g for g in match.groups() if g is not None)
        if any(c.isspace() for c in literal):
            hit = _judge_string(literal, dotglob, depth)
            if hit:
                return hit
    return None


def _judge_segment(seg: list[_Word], dotglob: bool, reads_line: bool,
                   depth: int) -> str | None:
    values = [w.value for w in seg]
    at = _stem_at(values)
    stem = PurePosixPath(values[at]).name if at < len(values) else ""
    reads = (reads_line or stem in _SEARCHERS or stem in _ARCHIVERS
             or any(v in _EXEC_WORDS for v in values)
             or (stem == "git" and "grep" in values[at + 1:]))
    expanded = _program_patterns(values, at, stem)
    previous = ""
    for k, word in enumerate(seg):
        token = word.value.strip("\"'")
        name = PurePosixPath(token).name
        # A protected name as a word of its own, and a word matched against
        # the credential bundles' names — quoted or not, as on main.
        if is_protected(name) or _bundle_glob(name):
            return name
        # Main's floor: a word bash does not expand (quoted) that main's
        # glob rule refused (`echo '*.local'`, `zip -i '.e?v*'`).
        if not word.expands and main_env_glob(name):
            return name
        if previous in _NAME_PATTERN_OPTIONS or k in expanded:
            # A pattern the program expands itself (`find -name`, `rg -g`,
            # `grep --include`, `git grep -- PATHSPEC`, `tar --wildcards`).
            if pattern_reaches(name, reads):
                return name
        elif word.expands and env_glob_reaches(name, dot_led_only=not dotglob):
            # A word bash expands: only a dot-led glob reaches a dotfile,
            # unless the line turns dotglob on (fix 1).
            return name
        option, eq, value = token.partition("=")
        glued = ""
        if eq and option in _NAME_PATTERN_OPTIONS:
            glued = value
        elif token.startswith(_GLUED_PATTERN_OPTION) and not token.startswith("--") \
                and len(token) > len(_GLUED_PATTERN_OPTION):
            glued = token[len(_GLUED_PATTERN_OPTION):]           # fix 3: `-g.e?v*`
        if glued:
            name = PurePosixPath(glued).name
            if is_protected(name) or pattern_reaches(name, reads):
                return name
        # The words of a string another program runs as a command line (fix
        # 5), and an interpreter's inline program. Not every quoted string:
        # a jq filter or a commit message holding `.*env.*` is text.
        here = word.op.endswith("<<<")
        string = None
        if stem == "env" and token.startswith("-S") and len(token) > 2:
            string = token[2:]
        elif stem == "env" and token.startswith("--split-string="):
            string = token.partition("=")[2]
        elif any(c.isspace() for c in token) and (
                previous == "eval" or _INLINE_FLAG.fullmatch(previous)
                or (stem == "env" and previous in ("-S", "--split-string"))
                or (stem in _STRING_RUNNERS and k > at)):
            string = token
        elif here and stem in _SHELL_STEMS:
            string = token
        if string is not None:
            hit = _judge_string(string, dotglob, depth)
            if hit:
                return hit
        elif stem in _INTERPRETER_STEMS and k > at and (here or _CODE_FLAG.fullmatch(previous)):
            hit = _judge_code(token, dotglob, depth)
            if hit:
                return hit
        previous = token
    return None


def _judge_line(command: str, dotglob: bool, depth: int) -> str | None:
    words = _shell_words(command)
    reads_line = any(PurePosixPath(w.value).name == "xargs" for w in words)
    segments: dict[int, list[_Word]] = {}
    for word in words:
        segments.setdefault(word.seg, []).append(word)
    for seg in segments.values():
        hit = _judge_segment(seg, dotglob, reads_line, depth)
        if hit:
            return hit
    return None


def raw_names_in(text: str) -> str | None:
    """A protected name anywhere in `text`, by the raw-line rules alone: a
    bundle or `.env`/`.env.local` as a whole name (main's `_RAW_NAMES`), or a
    credential env file named from `.env` on. The last step of
    `protected_in_command`; v2 asks it of Claude's Grep `glob` and `path` too,
    so the two layers agree (`--include='*.env*'` and `glob: "*.env*"`)."""
    for name, pattern in _RAW_NAMES:
        if pattern.search(text):
            return name
    return env_credential_in_text(text)


def protected_in_command(command: str) -> str | None:
    """The protected filename a shell command references, if any: a bundle,
    `.env`/`.env.local`, or any credential env file by the owner's rule
    (ENV-1) — as a word of its own, a glob that could expand onto one, a
    pattern a program expands, a string another shell runs, or anywhere in
    the raw line.

    Read as bash reads it: a backslash-newline inside a name (`.e\\<newline>nv`)
    is deleted before bash sees the word, so it is deleted here too — through
    `rules.join_continuations`, the one implementation of that rule
    (2026-10-09).
    """
    from .. import rules

    command = rules.join_continuations(command)
    hit = _judge_line(command, _dotglob_on(command), 0)
    if hit:
        return hit
    return raw_names_in(command)


# --- value scrubbing -------------------------------------------------------

_cache: dict[Path, tuple[float, tuple[str, ...]]] = {}


def _search_dirs() -> list[Path]:
    """Where a `.env` relevant to this session could plausibly live.

    The working directory and its ancestors, plus the Jarvis checkout — not a
    filesystem sweep, which would cost more than it protects.
    """
    found: list[Path] = []
    seen: set[Path] = set()

    def add(directory: Path) -> None:
        if directory not in seen:
            seen.add(directory)
            found.append(directory)

    try:
        here = Path.cwd()
    except OSError:
        here = _JARVIS_ROOT

    home = Path.home()
    for directory in [here, *here.parents][:12]:
        add(directory)
        if directory == home:
            break
    add(home)
    add(_JARVIS_ROOT)
    return found


def _read_regular(path: Path) -> str | None:
    """The start of `path` as text if it is a regular file (through a
    symlink: `.env -> ../shared/.env` is common, and its values are the ones
    to withhold), else None. Opened non-blocking and judged by `fstat` before
    a byte is read, so a FIFO or a device named `.env.<x>` — now that a whole
    folder's env files are read — cannot hang or flood the scrub (the hazard
    terminal_read's guard met first, 2026-10-10)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        if not _stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks, size = [], 0
        while size < _MAX_FILE_BYTES:
            chunk = os.read(fd, _MAX_FILE_BYTES - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    except OSError:
        return None
    finally:
        os.close(fd)
    return b"".join(chunks).decode("utf-8", errors="replace")


def _values_in(path: Path) -> tuple[str, ...]:
    """Credential-looking values in one env file, read afresh every time
    (ENV-1): a cache keyed on the file's mtime served stale values for a
    rewrite made within one timestamp tick, and these files are small (read
    at most `_MAX_FILE_BYTES`)."""
    text = _read_regular(path)
    if text is None:
        return ()
    whole = path.name in _WHOLE_FILES

    values: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip("'\"")
        if whole:
            if len(value) >= MIN_SECRET_LEN:
                values.add(value)
        elif _secret_entry(key, value):
            values.add(value)

    # Longest first, so a value that contains another is replaced whole.
    return tuple(sorted(values, key=len, reverse=True))


# --- which values of an env file are secrets (ENV-1 review, MED-2) -------------
#
# `.env` and `.env.local` are scrubbed whole, as since 2026-07-30: every value
# of 8 characters or more (`_WHOLE_FILES`; changing that is the owner's call).
# Every other credential env file (`.env.production`, `.env.staging` …) holds
# settings as much as secrets — `NODE_ENV=production`, `API_URL=https://…` —
# and redacting those everywhere mangled ordinary output ("Deploy to
# [redacted…]", `NODE_ENV === '[redacted…]'`, and a write round trip would
# save the marker into the owner's code). So from those only:
#
# * the value of a **secret-named key** — a whole word or an `_`/`-`/`.`/
#   camelCase part of the key: KEY, TOKEN, SECRET, PASS, PASSWORD, PWD,
#   CREDENTIAL, PRIVATE, SALT, DSN (and their plurals) — at 4+ characters, a
#   boolean excepted (`TOKEN_REFRESH=true` would redact every `true`);
#   AUTH, SESSION and COOKIE name settings as often as secrets
#   (`SESSION_DRIVER=redis`, `COOKIE_SECURE=true`), so under those alone a
#   value counts at 4+ characters only when it is not plain (below);
# * a **secret-looking value** under any key: a URL carrying userinfo, a
#   query or a fragment (`DATABASE_URL=postgres://app:pw@db/app`), or 16+
#   characters that are not plain — not words, a number, a boolean, a path,
#   a host name or an e-mail address, nor a URL without those parts.
#
# The delta review (LOW-C) widened both halves. **A joined key name counts**:
# a part that ends with KEY, SECRET, PASS, TOKEN, PW, PWD, PIN or SEED
# (`APIKEY`, `SECRETKEY`, `SMTPPW`, `ADMINPIN`), or starts with SECRET, PASS,
# SEED or PRIVATE (`PASSPHRASE`, `SEEDPHRASE`, `PRIVATEKEY`) — the ends of a
# joined word, not any middle (`MAPPING` holds "pin", `KEYBOARD` starts with
# "key", `TOKENIZER` with "token"). PIN, SEED and PW count as whole parts too,
# and under a secret-named key a number counts (`ADMIN_PIN=48291357`). **A
# key that ends in a setting's suffix** (`_TTL`, `_TIMEOUT`, `_EXPIRES`,
# `_EXPIRY`, `_LENGTH`, `_SIZE`, `_COUNT`, `_URL`, `_HOST`, `_PORT`, `_PATH`,
# `_ENABLED`, `_DRIVER`, `_TYPE`, `_NAME`) with a plain value is a setting
# (`AUTH_TOKEN_TTL=3600` redacted every "3600"). **A webhook URL is a secret**
# (the secret is its path): a known webhook host (`hooks.slack.com`,
# Discord's `/api/webhooks/`, …), or a path segment of 16+ characters that
# mixes letters and digits.
_WHOLE_FILES = frozenset({".env", ".env.local"})
_SECRET_KEY_PARTS = frozenset({
    "key", "keys", "token", "tokens", "secret", "secrets", "pass", "passwd", "password",
    "passwords", "pwd", "credential", "credentials", "private", "salt", "dsn",
    "pw", "pin", "pins", "seed", "seeds", "passphrase",
})
_JOINED_ENDINGS = ("key", "keys", "secret", "secrets", "pass", "token", "tokens", "pw", "pwd",
                   "pin", "pins", "seed", "seeds")
_JOINED_STARTS = ("secret", "pass", "seed", "private")
_SETTING_SUFFIXES = frozenset({"ttl", "timeout", "expires", "expiry", "length", "size", "count",
                               "url", "host", "port", "path", "enabled", "driver", "type", "name"})
# Webhook hosts whose path is the credential, with the path it must start with.
_WEBHOOK_HOSTS = (("hooks.slack.com", "/"), ("hooks.zapier.com", "/"),
                  ("webhook.office.com", "/"), ("outlook.office.com", "/webhook"),
                  ("discord.com", "/api/webhooks/"), ("discordapp.com", "/api/webhooks/"),
                  ("api.telegram.org", "/bot"))
_MIXED_SEGMENT = re.compile(r"(?=[^/]*[A-Za-z])(?=[^/]*\d)[A-Za-z0-9_\-]{16,}")
_SETTING_KEY_PARTS = frozenset({"auth", "session", "sessions", "cookie", "cookies"})
_BOOLEANS = frozenset({"true", "false", "yes", "no", "on", "off", "null", "none",
                       "enabled", "disabled"})
_KEY_PARTS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
_NUMBER = re.compile(r"^[+-]?\d[\d_,]*(?:\.\d+)?$")
_PATH = re.compile(r"^(?:~|\.{1,2})?/[\w.\-/~ ]*$|^[A-Za-z]:[\\/][\w.\-\\/ ]*$")
_HOST = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z][a-z0-9-]*(?::\d+)?$")
_EMAIL = re.compile(r"^[\w.+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+$", re.IGNORECASE)
_WORD_SPLIT = re.compile(r"[ _\-.,/]+")


def _key_parts(key: str) -> list[str]:
    key = key.strip()
    if key.lower().startswith("export "):
        key = key[7:].strip()
    return [part.casefold() for part in _KEY_PARTS.findall(key)]


def _secret_named(parts: list[str]) -> bool:
    return any(part in _SECRET_KEY_PARTS or part.endswith(_JOINED_ENDINGS)
               or part.startswith(_JOINED_STARTS) for part in parts)


def _plain(value: str) -> bool:
    """A value that reads as a setting, not a secret."""
    if value.casefold() in _BOOLEANS or _NUMBER.match(value) or _PATH.match(value):
        return True
    if _HOST.match(value) or _EMAIL.match(value):
        return True
    if _URL.match(value):
        return not _url_carries_secret(value)
    words = [w for w in _WORD_SPLIT.split(value) if w]
    return bool(words) and all(
        w.isalpha() and len(w) <= 20 and (w.islower() or w.isupper() or w.istitle())
        for w in words)


def _url_carries_secret(value: str) -> bool:
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").casefold()
    except ValueError:
        return True
    if "@" in parts.netloc or parts.query or parts.fragment:
        return True
    path = parts.path or ""
    for webhook, prefix in _WEBHOOK_HOSTS:
        if (host == webhook or host.endswith("." + webhook)) and path.startswith(prefix) \
                and len(path) > len(prefix):
            return True
    return any(_MIXED_SEGMENT.fullmatch(segment) for segment in path.split("/"))


def _secret_entry(key: str, value: str) -> bool:
    """Is this value, under this key, a secret to withhold (see above)?"""
    if not value:
        return False
    parts = _key_parts(key)
    if parts and parts[-1] in _SETTING_SUFFIXES and _plain(value):
        return False                                # `AUTH_TOKEN_TTL=3600`
    if _secret_named(parts):
        return len(value) >= 4 and value.casefold() not in _BOOLEANS
    if set(parts) & _SETTING_KEY_PARTS and len(value) >= 4 and not _plain(value):
        return True
    if _URL.match(value):
        return _url_carries_secret(value)
    return len(value) >= 16 and not _plain(value)


_SENSITIVE_KEY = re.compile(r"token|secret|password|key", re.IGNORECASE)


def _json_values(path: Path) -> tuple[str, ...]:
    """Credential values in a JSON bundle, cached until the file changes.

    Only values under sensitive-looking keys — scrubbing every long string
    would also redact scope URLs, which legitimately appear in API errors.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _cache.pop(path, None)
        return ()

    cached = _cache.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    values: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if (
                    isinstance(value, str)
                    and len(value) >= MIN_SECRET_LEN
                    and _SENSITIVE_KEY.search(key)
                ):
                    values.add(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    try:
        walk(json.loads(path.read_text(encoding="utf-8")[:_MAX_FILE_BYTES]))
    except (OSError, json.JSONDecodeError):
        return ()

    ordered = tuple(sorted(values, key=len, reverse=True))
    _cache[path] = (mtime, ordered)
    return ordered


def env_files_in(directory: Path) -> tuple[Path, ...]:
    """Every credential env file directly in `directory`, by the one name
    rule (`.env`, `.env.local`, `.env.production`, … — never `.env.example`),
    sorted. Listed afresh on every call: a cache keyed on the folder's mtime
    missed a file made within one timestamp tick of the listing (measured:
    `.env.fifo` made right after `.env.staging` was not seen), and a value
    the scrub does not know is a value that reaches the transcript. A folder
    that cannot be listed holds none we can see."""
    try:
        with os.scandir(directory) as entries:
            return tuple(sorted(directory / entry.name for entry in entries
                                if is_env_credential_name(entry.name)))
    except OSError:
        return ()


def secret_values() -> list[str]:
    """Every credential-looking value in a reachable protected file: every
    credential env file in the working folder, its parents up to home, home
    and the Jarvis checkout (ENV-1: it used to read only `.env` and
    `.env.local` there, so a `.env.production` value went through), plus the
    credential bundles."""
    values: set[str] = set()
    for directory in _search_dirs():
        for path in env_files_in(directory):
            values.update(_values_in(path))
    values.update(_json_values(config.GOOGLE_TOKEN_PATH))
    values.update(_json_values(config.ONSHAPE_TOKEN_PATH))
    values.update(_json_values(config.DISCORD_TOKEN_PATH))
    values.update(_json_values(config.SPOTIFY_TOKEN_PATH))
    return sorted(values, key=len, reverse=True)


# Separators a tool can put between a path and the rest of an output line.
# **`-` is the one that was missing, and it is the common case**: grep's and
# ripgrep's *context* lines (`-A/-B/-C`, and `grep_files(context_lines=N)`) are
# attributed `path-N-text`, not `path:N:text`. Layer 3 only understood `:`, so
# `run_readonly("grep -rn -i -C2 openrouter <dir>")` returned
# `/x/.env.local-2-STRIPE_KEY=…` intact — and silently, because the trailing
# "[N line(s) … withheld]" counter still printed, so the output looked filtered.
# NUL (`grep -Z`) and tab are here for the same reason: any separator this
# parser does not know is a separator that hides the attribution.
#
# `:` is tried first so a path containing a `-` is not cut short, and the whole
# scan only fires when the separator is actually present — a bare `.env` line
# (from `ls -a`, say) is not an attribution and stays.
_SEPARATORS = (":", "\x00", "\t", "-")


def _attributed_to_protected(line: str) -> bool:
    """True if this output line names a protected file as its source."""
    for sep in _SEPARATORS:
        if sep not in line:
            continue
        head = line.split(sep, 1)[0].strip()
        if head and is_protected(head):
            return True
    return False


def _drop_attributed_lines(text: str) -> str:
    """Remove output lines a tool attributed to a protected file.

    Covers `grep -R` (`path:n:match`), grep's context lines (`path-n-text`) and
    multi-file `head`/`tail` (`==> path <==` followed by contents). The key
    names leak structure even once the values are gone, so the whole line goes.
    """
    kept: list[str] = []
    dropped = 0
    in_protected_block = False

    for line in text.splitlines():
        header = _HEADER.match(line.strip())
        if header:
            in_protected_block = is_protected(header.group("path"))
            if in_protected_block:
                dropped += 1
            else:
                kept.append(line)
            continue
        if in_protected_block:
            dropped += 1
            continue
        if _attributed_to_protected(line):
            dropped += 1
            continue
        kept.append(line)

    if dropped:
        kept.append(f"[{dropped} line(s) from a protected .env file were withheld]")
    return "\n".join(kept)


def scrub(text: str) -> str:
    """Strip protected-file content out of anything headed for the transcript."""
    if not text:
        return text
    out = _drop_attributed_lines(text)
    for value in secret_values():
        if value in out:
            out = out.replace(value, REDACTED)
    return out
