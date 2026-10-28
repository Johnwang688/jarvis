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
`onshape_keys.json` (CAD), `discord_token.json`, `spotify_token.json`, and
the phone's push credentials `vapid_key.pem` and `push_subscriptions.json`.
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
        # The phone's Web Push (P-6, `v2/push.py`): the VAPID private key
        # that signs every push, and the subscriptions, whose endpoints are
        # capabilities and whose keys decrypt what is sent to the phone.
        "vapid_key.pem",
        "push_subscriptions.json",
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
# Every shell `rules.SHELLS` names (delta review of PR #75, M-B; this module
# imports only config, so the list is spelled out here and rules_check holds
# the two equal), and busybox.
_SHELL_STEMS = frozenset({"sh", "bash", "rbash", "dash", "zsh", "ksh", "ksh93", "lksh", "oksh",
                          "mksh", "posh", "ash", "sash", "yash", "fish", "csh", "tcsh", "nu",
                          "xonsh", "elvish", "ion", "osh", "ysh", "busybox"})
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

    # The run's working folder first, when one is bound (the v2 fast path's
    # project, decision F-6 of 2026-10-14): its commands run there, so its
    # `.env` is the one whose values a command's output could carry.
    from .. import runtime

    bound = runtime.working_dir()
    home = Path.home()
    for start in ([Path(bound)] if bound else []) + [here]:
        for directory in [start, *start.parents][:12]:
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
    values: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip("'\"")
        if _secret_entry(key, value):
            values.add(value)

    # Longest first, so a value that contains another is replaced whole.
    return tuple(sorted(values, key=len, reverse=True))


# --- which values of an env file are secrets (ENV-1 review, MED-2) -------------
#
# Every credential env file, `.env` and `.env.local` included (the owner's
# answer, 2026-10-12; they were scrubbed whole, every value of 8+ characters,
# from 2026-07-30 until then), holds settings as much as secrets —
# `NODE_ENV=production`, `API_URL=https://…` — and redacting those everywhere mangled ordinary output ("Deploy to
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
#
# PR #80's review: a joined name ending in PASSWORD, PASSWD or CREDS counts
# (`PGPASSWORD`, libpq's own variable, `MYSQLPASSWORD`, `USERCREDS`), and CRED,
# CREDS and MNEMONIC are key parts (`DB_CREDS`, a wallet's `MNEMONIC`).
_SECRET_KEY_PARTS = frozenset({
    "key", "keys", "token", "tokens", "secret", "secrets", "pass", "passwd", "password",
    "passwords", "pwd", "credential", "credentials", "cred", "creds", "private", "salt", "dsn",
    "pw", "pin", "pins", "seed", "seeds", "passphrase", "mnemonic",
})
_JOINED_ENDINGS = ("key", "keys", "secret", "secrets", "pass", "password", "passwords", "passwd",
                   "token", "tokens", "pw", "pwd", "pin", "pins", "seed", "seeds",
                   "creds", "credential", "credentials")
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


# --- credentials typed into a command line (2026-10-15) -----------------------
#
# A tool call's label (the HUD's tool rows, a Claude or Codex child's rows),
# an approval card (the HUD's and Discord's, v1's and v2's), the thread's
# `log.jsonl`, the v2 decision log and v1's tool lines show or keep the
# command a model ran. The scrub withholds values it *knows* (the env files
# above) and the label the shapes `credential_patterns` knows; neither saw a
# credential typed inline whose value is neither: `export MY_API_TOKEN=…`,
# `curl -H "Authorization: Bearer …"`, `curl -u user:pass`, `--password=…`,
# `mysql -pS3cret`, `https://tok@host`. `redact_inline` withholds the
# **value** and keeps the name (`MY_API_TOKEN=[redacted]`), by the env files'
# own name rule: a value `_secret_entry` would withhold from a `.env` under a
# name the env rule reads as a credential's (or a credential-bearing
# setting's: AUTH, SESSION, COOKIE) is withheld from a command under it.
# `NODE_ENV=…`, `PATH=…`, `CI=1`, `AUTH_TOKEN_TTL=3600` stay.
#
# **Display and persistence only.** Nothing that judges a command (the rules,
# layer 1, the permit, the allowlist, the escape hatch) reads through this:
# they hold the command as typed, and an "Always" is minted from it.
#
# **A withheld value never hides code that runs** (the owner's condition for
# redacting cards, 2026-10-15). Three rules, each erring towards showing:
#
# 1. Nothing is withheld that the shell would read as structure. Every
#    replacement is checked against the line's own quoting (`_Mask`): the
#    span removed must start and end in the same quoting, and hold no
#    unquoted blank, operator (`;&|()<>`), `$` or backtick and no `$` or
#    backtick inside double quotes. `echo 'T="a' ; rm -rf ~ ; echo '"'` and
#    `echo "T='$(rm -rf ~)'"` keep every character.
# 2. A value the shell would expand stays: unquoted or double-quoted with
#    `$` or a backtick (`$'…'` is a literal).
# 3. **A variable's value stays when the line may run it**, marked
#    `[shown: this value may be run]`: every one when the line re-parses
#    text as code (`eval`, `sh|bash|python … -c`, `-e`, `source`/`.`,
#    `<(…)`, `<<<`, `xargs`, `exec`, a pipe into a shell or interpreter) or
#    runs a variable as a command (`$X`, `${X}` at a command's place); the
#    ones the line names (`$X`) when it also writes a file (`>`, `tee`),
#    starts a shell or interpreter or a `./program`. A program on the line
#    can still read the value from its environment, which no card shows.
#
# So does a file reference: a name ending FILE/PATH/DIR (`PASSWORD_FILE=…`,
# `--token-file`) or a value shaped like a path (`--key ./client-key.pem`).
# Not a parser: a value split across words by quoting can still show.
INLINE_REDACTED = "[redacted]"
RUN_MARKER = "[shown: this value may be run]"
# One shell word: quoted pieces (`$'…'` too) and unquoted runs, glued. The
# unquoted run is atomic, so the outer repetition cannot backtrack through it.
_UNQUOTED = r"(?:\\.|\$(?!')|[^\s'\"`;&|<>()\\$])+"
_WORD = (r"(?:\$'(?:[^'\\\n]|\\.)*'|'[^'\n]*'|\"(?:[^\"\\\n]|\\.)*\"|(?>" + _UNQUOTED + "))+")
_WORD_RE = re.compile(_WORD)
_WORD_PIECE = re.compile(r"\$'((?:[^'\\\n]|\\.)*)'|'([^'\n]*)'|\"((?:[^\"\\\n]|\\.)*)\"|(" + _UNQUOTED + ")")
# `NAME=value` anywhere a word starts: `export`, `env`, `set`, `declare -x`,
# a command's prefix, `docker -e`, `--build-arg`, a query's `&token=`.
# Not after `-` (an option, below), `$`, an identifier character, or a `/`
# or `^` (a sed or regex program: `s/TOKEN=.*/TOKEN=x/`).
_INLINE_ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9_$\-/^])(?P<name>[A-Za-z_][A-Za-z0-9_]*)\+?=(?P<value>" + _WORD + ")")
# PowerShell's `$env:NAME = "value"`; fish's `set -gx NAME value`; Windows'
# `setx NAME value`; `npm|pnpm|yarn config set KEY value`.
_INLINE_PS = re.compile(r"(?i)\$env:(?P<name>[A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*(?P<value>" + _WORD + ")")
_INLINE_SET = re.compile(
    r"(?<![\w-])(?:set(?:[ \t]+-[A-Za-z]+)*|setx)[ \t]+(?P<name>[A-Za-z_][A-Za-z0-9_]*)[ \t]+"
    r"(?P<value>" + _WORD + ")")
_INLINE_CONFIG = re.compile(
    r"(?<![\w-])(?:npm|pnpm|yarn)[ \t]+config[ \t]+set[ \t]+(?P<name>[^\s=]+)[ \t]+(?P<value>" + _WORD + ")")
# `--password=value`, `--api-key value`.
_INLINE_OPTION = re.compile(
    r"(?<![\w-])--(?P<name>[A-Za-z][A-Za-z0-9_-]*)(?:=(?P<value>" + _WORD + r")|[ \t]+(?P<spaced>" + _WORD + "))")
# A header's value never runs past a blank, a quote or an operator.
_HEADER_VALUE = r"[^\s'\"\\;&|()<>$`]+"
# `Authorization: Bearer value` wherever it is written (curl, wget, httpie);
# the scheme word stays, and so does a quoted expansion after it.
_AUTH_SCHEMES = ("bearer", "basic", "token", "bot", "digest", "negotiate", "ntlm", "apikey")
_INLINE_AUTH = re.compile(
    r"(?i)(?<![\w-])(?:proxy-)?authorization[ \t]*:[ \t]*['\"]?"
    r"(?:(?:" + "|".join(_AUTH_SCHEMES) + r")[ \t]+(?:\\?['\"])?)?(?P<value>" + _HEADER_VALUE + ")")
# Any other header with a credential name: `-H 'X-Api-Key: value'`.
_INLINE_HEADER = re.compile(
    r"(?<![\w-])(?:-H|--header)[ \t]*=?[ \t]*['\"]?(?P<name>[A-Za-z][A-Za-z0-9_-]*)[ \t]*:[ \t]*"
    r"(?P<value>" + _HEADER_VALUE + ")")
# A cookie header is a session: its first pair (the rest are named pairs
# the assignment rule judges; a value never holds a `;`).
_INLINE_COOKIE = re.compile(r"(?i)(?<![\w-])cookie[ \t]*:[ \t]*(?P<value>" + _HEADER_VALUE + ")")
# `"password": "value"` in a JSON body.
_INLINE_JSON = re.compile(
    r"\"(?P<name>[A-Za-z_][A-Za-z0-9_.\-]*)\"[ \t]*:[ \t]*\"(?P<value>(?:[^\"\\\n]|\\.)*)\"")
# URLs anywhere in the text. The scheme is bounded and must start a word:
# `[A-Za-z][…]*://` tried at every letter of a long token is quadratic
# (delta review of PR #84).
_URL_IN_TEXT = re.compile(r"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]{0,31}://[^\s'\"<>`]+")
# Options that take a credential only for their own program, judged where
# the segment runs it (its command word, past assignments and wrappers),
# at the first occurrence: curl's `-u user:pass` (the user stays) and
# `-b`/`--cookie name=value` (a bare `-b file` is a file), mysql's glued
# `-pVALUE` (a spaced `-p db` is the database), mongosh's `-p`, `docker|
# podman|nerdctl login -p`, `gh secret set --body`, and, among their own
# options only (before what they run), sshpass' `-p` and redis-cli's `-a`.
_SEGMENTS = re.compile(r"\|\||&&|[;|\n]")
_USERPASS, _COOKIE_DATA = "userpass", "cookie"
_MYSQL = frozenset({"mysql", "mysqldump", "mysqladmin", "mysqlimport", "mysqlcheck", "mysqlshow",
                    "mysqlslap", "mariadb", "mariadb-dump", "mariadb-admin"})
# (programs, subcommand words, kind, pattern, option words that take the
# next word — None: the whole rest of the segment is searched).
_PROGRAM_FLAGS = (
    (frozenset({"curl"}), (), _USERPASS,
     re.compile(r"(?<![\w-])(?:-u|--user|-U|--proxy-user)(?:[ \t]+|=)?(?P<value>" + _WORD + ")"), None),
    (frozenset({"curl"}), (), _COOKIE_DATA,
     re.compile(r"(?<![\w-])(?:-b|--cookie)(?:[ \t]+|=)?(?P<value>" + _WORD + ")"), None),
    (_MYSQL, (), "", re.compile(r"(?<![\w-])-p(?P<value>" + _WORD + ")"), None),
    (frozenset({"mongo", "mongosh"}), (), "",
     re.compile(r"(?<![\w-])-p(?:[ \t]+)?(?P<value>" + _WORD + ")"), None),
    (frozenset({"docker", "podman", "nerdctl"}), ("login",), "",
     re.compile(r"(?<![\w-])-p(?:[ \t]+)?(?P<value>" + _WORD + ")"), None),
    (frozenset({"gh"}), ("secret", "set"), "",
     re.compile(r"(?<![\w-])(?:--body|-b)(?:[ \t]+|=)?(?P<value>" + _WORD + ")"), None),
    (frozenset({"sshpass"}), (), "",
     re.compile(r"(?<![\w-])-p(?:[ \t]+)?(?P<value>" + _WORD + ")"),
     frozenset({"-f", "-d", "-p", "-P"})),
    (frozenset({"redis-cli"}), (), "",
     re.compile(r"(?<![\w-])-a(?:[ \t]+)?(?P<value>" + _WORD + ")"),
     frozenset({"-h", "-p", "-s", "-a", "-u", "-n", "-r", "-i", "-d", "-D", "-e",
                "--user", "--pass", "--askpass", "--tls", "--cacert", "--cert", "--key"})),
)
_PROGRAM_WORD = re.compile(r"(?<![\w.-])(?:" + "|".join(sorted(
    (re.escape(p) for programs, *_ in _PROGRAM_FLAGS for p in programs), key=len, reverse=True))
    + r")(?![\w.-])")
_FILE_PARTS = frozenset({"file", "files", "path", "dir"})
# A name for a program to run, not a secret: its value stays, as a file's
# does (`GIT_ASKPASS`, `SUDO_ASKPASS`, `BORG_PASSCOMMAND`,
# `CREDENTIAL_HELPER`, `credential_process`).
_EXEC_ENDINGS = ("askpass", "command", "cmd", "helper", "process", "program", "exec", "hook", "script")
# Rule 3. A line that re-parses text as code, or runs a variable as a
# command: every variable's value stays.
_RUNNERS = (r"(?:sh|bash|rbash|dash|zsh|ksh|mksh|ash|fish|csh|tcsh|busybox|python\d*(?:\.\d+)?|"
            r"node|nodejs|deno|bun|perl|ruby|php|lua|pwsh|powershell|cmd|osascript|tclsh|Rscript)")
# A runner's word: its name alone or after a path's `/`, never scanning a
# prefix (`\S*/` before it, tried at every position, is quadratic).
_RUNNER_WORD = re.compile(r"(?i)(?:(?<=/)|(?<![^\s;&|(`'\"]))" + _RUNNERS + r"(?![\w.-])")
_EXEC_OPTION = re.compile(r"(?<![\w-])(?:-[A-Za-z]*[cCeE][A-Za-z]*|--eval|/[cCkK])(?![\w-])")
_REPARSE = re.compile(
    r"(?i)(?<![\w.-])(?:eval|source|exec|xargs|iex|invoke-expression|start-process|trap|watch|parallel"
    r"|alias|ssh|autossh|mosh|su|runuser|script)(?![\w-])"
    r"|(?<!\S)-(?:exec|execdir|ok|okdir)(?![\w-])"
    r"|&[ \t]*\$(?i:env):"
    r"|(?<![\w.-])env[ \t]+(?:-[^\s;&|]+[ \t]+)*-S"
    r"|(?:^|[;&|(\n`{]|\$\(|\bthen|\bdo|\belse)[ \t]*\.[ \t]"      # `.` as `source`, at a command's place
    r"|<\(|<<<"
    r"|\|[ \t&]*(?:(?:sudo|env|exec|command|nohup|time)[ \t]+)*(?:[^\s|;&]*/)?" + _RUNNERS + r"(?![\w.-])")
_VAR_AT_COMMAND = re.compile(
    r"(?:^|[;&|(\n{`]|\$\()[ \t]*(?:(?:[A-Za-z_]\w*=[^\s;&|]*|sudo|env|exec|nohup|time|command|builtin|"
    r"then|do|else|!|nice)[ \t]+)*[\"']?\$(?![Ee][Nn][Vv]:)")
# The ones the line names, when it also writes a file or starts a program
# that could read one back as code.
_PROGRAM_PATH = r"(?:^|[;&|(\n`{]|\$\()[ \t]*(?:(?:sudo|env|exec|nohup|time|command)[ \t]+)*\.{0,2}/[^\s;&|]"
_RUN_LATER = re.compile(r">|(?<![\w.-])tee(?![\w-])|" + _PROGRAM_PATH)
# A `./program` or `/path/program` at a command's place (SEC-6's rule 3).
_RUN_PROGRAM = re.compile(_PROGRAM_PATH)


def _runs_code(text: str) -> bool:
    """A segment that starts a shell or interpreter with an option that runs
    its argument (`bash -lc`, `python3 -c`, `perl -e`, `node --eval`, `cmd /c`).
    Linear: each segment is searched once for each."""
    last = 0
    for sep in [*_SEGMENTS.finditer(text), None]:
        end = sep.start() if sep is not None else len(text)
        runner = _RUNNER_WORD.search(text, last, end)
        if runner is not None and _EXEC_OPTION.search(text, runner.end(), end):
            return True
        if sep is not None:
            last = sep.end()
    return False


_REFERENCE = re.compile(r"\$\{?!?([A-Za-z_][A-Za-z0-9_]*)|(?i:\$env:)([A-Za-z_][A-Za-z0-9_]*)"
                        r"|%([A-Za-z_][A-Za-z0-9_]*)%")


# Never withheld, whatever the quoting looks like (delta review of #205,
# HIGH): a value holding a blank, an operator, an expansion, a glob or a
# leading `~`. Every real command needs one of these to be a command, so a
# value the quoting model misread can never swallow one.
_UNSAFE_VALUE = re.compile(r"[\s;&|<>()$`{}*?\[]")


def _unsafe_literal(value: str) -> bool:
    return bool(_UNSAFE_VALUE.search(value)) or value.startswith("~")


class _Mask:
    """Rule 1: where the shell reads `text` as structure. `safe(a, b)` says
    whether `text[a:b]` can be replaced without hiding any: it starts and
    ends in the same quoting, holds nothing active and, for a value, no
    character `_UNSAFE_VALUE` names. Where the model stops being sure, it
    fails closed: a comment (a word-start `#`, to the end of its line) is
    all active, and so is everything after a here-document's `<<`, a
    backtick, or a `$(` inside double quotes (whose inner quoting nests)."""

    def __init__(self, text: str):
        n = len(text)
        state = bytearray(n + 1)          # quoting before each character
        active = [0] * (n + 1)            # active characters in text[:i]
        q = count = i = 0                 # q: 0 none, 1 '…', 2 "…", 3 $'…'
        lost = False                      # from here on, nothing is withheld
        while i < n:
            c = text[i]
            state[i] = q
            if not lost:
                if q == 0 and c == "#" and (i == 0 or text[i - 1].isspace() or text[i - 1] in ";&|()"):
                    end = text.find("\n", i)
                    end = n if end == -1 else end
                    for k in range(i, end):
                        state[k] = 0
                        count += 1
                        active[k + 1] = count
                    i = end
                    continue
                if c == "`" and q in (0, 2) or (q == 2 and c == "$" and text[i + 1:i + 2] == "(") \
                        or (q == 0 and text.startswith("<<", i) and not text.startswith("<<<", i)):
                    lost = True
            if lost:
                count += 1
                active[i + 1] = count
                i += 1
                continue
            pair = (c == "\\" and q != 1) or (c == "$" and q == 0 and text[i + 1:i + 2] == "'")
            if pair and i + 1 < n:
                # An escape, or `$'` opening an ANSI-C quote: two characters
                # of quoting, neither of them active.
                if c == "$":
                    q = 3
                state[i + 1] = state[i]
                active[i + 1] = active[i + 2] = count
                i += 2
                continue
            hot = False
            if q in (1, 3):
                if c == "'":
                    q = 0
            elif q == 2:
                if c == '"':
                    q = 0
                elif c in "$`":
                    hot = True
            elif c == "'":
                q = 1
            elif c == '"':
                q = 2
            elif c.isspace() or c in ";&|()<>$`":
                hot = True
            count += hot
            active[i + 1] = count
            i += 1
        state[n] = q
        self.text, self.state, self.active = text, state, active

    def safe(self, a: int, b: int, *, value: bool = True) -> bool:
        if a >= b:
            return True
        # `$'` opens a quote, not an expansion; the rest is judged as typed.
        if value and _unsafe_literal(self.text[a:b].replace("$'", "'")):
            return False
        return self.state[a] == self.state[b] and self.active[b] == self.active[a]


def _masked_sub(pattern: re.Pattern, fn, text: str) -> str:
    """`pattern.sub(fn, text)`, keeping any replacement rule 1 refuses."""
    mask = None

    def guarded(match: re.Match) -> str:
        nonlocal mask
        whole = match.group(0)
        out = fn(match)
        if out == whole:
            return whole
        p = 0
        while p < len(whole) and p < len(out) and whole[p] == out[p]:
            p += 1
        s = 0
        while s < len(whole) - p and s < len(out) - p and whole[-1 - s] == out[-1 - s]:
            s += 1
        if mask is None:
            mask = _Mask(text)
        a, b = match.start() + p, match.end() - s
        if not mask.safe(a, b):
            return whole
        return out

    return pattern.sub(guarded, text)


def safe_url(value: str) -> str:
    """A URL without userinfo, query or fragment: scheme, host, port, path."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(value)
        return parts._replace(netloc=parts.netloc.rsplit("@", 1)[-1],
                              query="", fragment="").geturl()
    except ValueError:
        return "[redacted]"


def strip_url_credentials(text: str, *, query: bool = False) -> str:
    """Every URL in `text` without its userinfo (`https://tok@host`,
    `postgresql://u:pw@h/db`) and, with `query`, its query and fragment (the
    label's rule since TC-1). Each removal is a separate span, checked by
    rule 1 (`https://h/x?a;rm -rf ~` keeps its query); a value the shell
    expands stays."""
    if not text:
        return text
    from urllib.parse import urlsplit

    mask = None
    out, last = [], 0
    for match in _URL_IN_TEXT.finditer(text):
        url, base = match.group(), match.start()
        start = url.index("://") + 3
        rest = len(url)
        for sep in "/?#":
            at = url.find(sep, start)
            if at != -1:
                rest = min(rest, at)
        cuts = []
        at_sign = url.rfind("@", start, rest)
        if at_sign != -1:
            cuts.append((start, at_sign + 1))
        if query:
            tail = [i for i in (url.find("?", rest), url.find("#", rest)) if i != -1]
            if tail:
                cuts.append((min(tail), len(url)))
        if not cuts:
            continue
        try:
            urlsplit(url)
            broken = False
        except ValueError:
            broken = True
        if mask is None:
            mask = _Mask(text)
        if broken:
            # A malformed URL is withheld whole only as a value would be
            # (second delta review of #205): `http://u:p@[a]/{x,y}` keeps
            # its brace expansion, and its userinfo, in sight.
            if "$" not in url and "`" not in url and mask.safe(base, base + len(url)):
                out.append(text[last:base] + "[redacted]")
                last = match.end()
            continue
        pieces, pos = [], 0
        for a, b in cuts:
            # Userinfo is a value (`_UNSAFE_VALUE`); a query the label drops
            # is judged by the quoting alone.
            if "$" in url[a:b] or "`" in url[a:b] \
                    or not mask.safe(base + a, base + b, value=url[a] != "?" and url[a] != "#"):
                continue
            pieces.append(url[pos:a])
            pos = b
        pieces.append(url[pos:])
        out.append(text[last:base] + "".join(pieces))
        last = match.end()
    out.append(text[last:])
    return "".join(out)


def credential_name(name: str) -> bool:
    """Does this variable, option or header name read as a credential's or
    a credential-bearing setting's, by the env files' rule (`MY_API_TOKEN`,
    `PGPASSWORD`, `client-secret`, `SESSION_ID`)? Whether its value is
    withheld is `_secret_entry`'s call, as for an env file. `PWD`/`OLDPWD`
    are the shell's working folder, not a password."""
    if name in _SHELL_STATE:
        return False
    parts = _key_parts(name)
    return _secret_named(parts) or bool(set(parts) & _SETTING_KEY_PARTS)


def _literal(word: str) -> str | None:
    """A word's text without its quoting, or None when the shell would expand
    part of it (`$`, a backtick outside single quotes)."""
    out = []
    for match in _WORD_PIECE.finditer(word):
        ansi, single, double, bare = match.groups()
        if ansi is not None and "\\" in ansi:
            return None             # `$'a\nb'` decodes to what we cannot see
        if ansi is not None or single is not None:
            out.append(ansi if ansi is not None else single)
            continue
        piece = double if double is not None else bare
        if "$" in piece or "`" in piece:
            return None
        out.append(piece)
    return "".join(out)


def _withheld(name: str, value: str | None) -> bool:
    """Is `value`, typed under `name`, a credential to withhold? The env
    files' rule, less file references (see above)."""
    if value is None or not credential_name(name) or _unsafe_literal(value):
        return False
    parts = _key_parts(name)
    if parts and (parts[-1] in _FILE_PARTS or parts[-1].endswith(_EXEC_ENDINGS)):
        return False                                # `PASSWORD_FILE=…`, `GIT_ASKPASS=…`
    if "/" in value and _PATH.match(value):
        return False                                # `--key ./client-key.pem`
    return _secret_entry(name, value)


def _run_names(text: str):
    """Rule 3: the variables whose values the line may run — every one
    (True), the ones it names (a set), or none."""
    if _REPARSE.search(text) or _VAR_AT_COMMAND.search(text) or _runs_code(text):
        return True
    if _RUN_LATER.search(text) or _RUNNER_WORD.search(text):
        return {next(g for g in m.groups() if g) for m in _REFERENCE.finditer(text)}
    return frozenset()


def _already(match: re.Match, group: str = "value") -> bool:
    return match.string.startswith((REDACTED, INLINE_REDACTED), match.start(group))


def _redact_group(match: re.Match, group: str = "value") -> str:
    whole, start = match.group(0), match.start(0)
    a, b = match.span(group)
    return whole[:a - start] + INLINE_REDACTED + whole[b - start:]


def _named_value(run):
    """`NAME=value`: the value goes when the env files would withhold it,
    unless the line may run it (rule 3), which it then says."""
    def redact(match: re.Match) -> str:
        name = match.group("name")
        if _already(match) or not _withheld(name, _literal(match.group("value"))) \
                or match.string.startswith(" " + RUN_MARKER, match.end()):
            return match.group(0)
        if run is True or name in run:
            return match.group(0) + " " + RUN_MARKER
        return _redact_group(match)
    return redact


def _option_value(match: re.Match) -> str:
    name = match.group("name")
    group = "value" if match.group("value") is not None else "spaced"
    word = match.group(group)
    parts = _key_parts(name)
    if group == "spaced":
        # `--token value`, not `--token-file path`, `--password-stdin -u x`
        # or `--no-password db`: only a name that ends in a credential word
        # takes the next word, and an option is never it.
        if word.startswith("-") or not parts or parts[0] == "no" \
                or not _secret_named(parts[-1:]):
            return match.group(0)
    if _already(match, group) or not _withheld(name, _literal(word)):
        return match.group(0)
    return _redact_group(match, group)


def _header_value(match: re.Match) -> str:
    value = match.group("value")
    if _already(match):
        return match.group(0)
    if "name" in match.re.groupindex:
        if not _withheld(match.group("name"), value):
            return match.group(0)
    elif value.casefold() in _AUTH_SCHEMES:
        return match.group(0)                       # `Bearer` with its value elsewhere
    return _redact_group(match)


def _json_value(match: re.Match) -> str:
    value = match.group("value")
    if _already(match) or "$" in value or "`" in value \
            or not _withheld(match.group("name"), value):
        return match.group(0)
    return _redact_group(match)


def _program_value(kind: str):
    def redact(match: re.Match) -> str:
        word = match.group("value")
        value = _literal(word)
        if _already(match) or value is None or not value or word.startswith("-") \
                or _unsafe_literal(value):
            return match.group(0)
        if kind == _USERPASS:
            user, colon, password = value.partition(":")
            if not colon or not password:
                return match.group(0)               # curl asks for it
            start, a = match.start(0), match.start("value")
            return match.group(0)[:a - start] + user + ":" + INLINE_REDACTED
        if kind == _COOKIE_DATA and "=" not in value:
            return match.group(0)                   # `-b cookies.txt` is a file
        return _redact_group(match)
    return redact


def _program_flags(text: str) -> str:
    """The `_PROGRAM_FLAGS` edits, each found where its segment runs that
    program and each checked by rule 1 against the whole line."""
    edits = []
    if not _PROGRAM_WORD.search(text):
        return text
    bounds, last = [], 0
    for sep in _SEGMENTS.finditer(text):
        bounds.append((last, sep.start()))
        last = sep.end()
    bounds.append((last, len(text)))
    for seg_start, seg_end in bounds:
        if not _PROGRAM_WORD.search(text, seg_start, seg_end):
            continue                    # no program here: nothing to place
        words = list(_WORD_RE.finditer(text, seg_start, seg_end))
        values = [_literal(w.group()) or w.group() for w in words]
        at = _stem_at(values)
        if at >= len(values):
            continue
        program = PurePosixPath(values[at]).name
        for programs, subcommand, kind, pattern, value_options in _PROGRAM_FLAGS:
            if program not in programs:
                continue
            i = at + 1
            for sub in subcommand:
                while i < len(values) and values[i].startswith("-"):
                    i += 1
                if i >= len(values) or values[i] != sub:
                    break
                i += 1
            else:
                start, end = words[i - 1].end(), seg_end
                if value_options is not None:
                    # Only its own options: up to the first operand (what it runs).
                    while i < len(values) and values[i].startswith("-"):
                        i += 2 if values[i] in value_options else 1
                    end = words[i].start() if i < len(words) else seg_end
                match = pattern.search(text, start, end)
                if match is not None:
                    edits.append((match, _program_value(kind)(match)))
    if not edits:
        return text
    mask = _Mask(text)
    out, pos = [], 0
    for match, replaced in sorted(edits, key=lambda e: e[0].start()):
        whole = match.group(0)
        if replaced == whole or match.start() < pos:
            continue
        p = 0
        while p < min(len(whole), len(replaced)) and whole[p] == replaced[p]:
            p += 1
        s = 0
        while s < min(len(whole), len(replaced)) - p and whole[-1 - s] == replaced[-1 - s]:
            s += 1
        if not mask.safe(match.start() + p, match.end() - s):
            continue
        out.append(text[pos:match.start()] + replaced)
        pos = match.end()
    out.append(text[pos:])
    return "".join(out)


def redact_inline(text: str) -> str:
    """`text` (a command line, a tool argument) with URL userinfo and the
    value of every credential typed into it withheld, its name kept, and
    nothing withheld that runs (see above). For display and persistence;
    never for anything that judges the command. Linear in `text`."""
    if not text:
        return text
    run = _run_names(text)
    text = strip_url_credentials(text)
    named = _named_value(run)
    for pattern, fn in ((_INLINE_AUTH, _header_value), (_INLINE_COOKIE, _header_value),
                        (_INLINE_HEADER, _header_value), (_INLINE_JSON, _json_value),
                        (_INLINE_PS, named), (_INLINE_SET, named), (_INLINE_CONFIG, named),
                        (_INLINE_OPTION, _option_value), (_INLINE_ASSIGNMENT, named)):
        text = _masked_sub(pattern, fn, text)
    return _program_flags(text)


def _known_values(text: str) -> str:
    """The scrub's known values (env files, the process's credential-named
    variables), each occurrence replaced only where rule 1 allows, and no
    line dropped: the faithful half of `scrub`, for a command."""
    values = sorted(set(secret_values()) | set(_process_env_values()), key=len, reverse=True)
    for value in values:
        if value in text:
            text = _masked_sub(re.compile(re.escape(value)), lambda m: REDACTED, text)
    return text


# SEC-6 (owner, 2026-10-19): a private key's whole block on a card or in a
# log. A line break inside a key is a real one or the `\n` escape that
# `printf`, `echo -e`, `$'…'` and JSON read as one.
_KEY_BREAK = re.compile(r"\r?\n|(?:\\r)?\\n|\\r")
# All a block may hold: its header and footer, base64, a PGP checksum, PEM's
# `Proc-Type:`/`DEK-Info:` lines, blanks and those breaks. No operator, quote,
# expansion, glob or `~`.
_KEY_CHARS = re.compile(r"[A-Za-z0-9+/=:,\- \t\r\n\\]*")


def _key_material(span: str) -> bool:
    """Is `span` nothing but private-key blocks: each line a header (at its
    start), a footer (at its end), key material or blank? Rule 0's stand-in
    for a block, which always holds a blank: no line of it can be a command
    with arguments or an operator."""
    from ..v2.redact import _KEY_LINE, KEY_BEGIN, KEY_END

    if not _KEY_CHARS.fullmatch(span):
        return False
    for line in _KEY_BREAK.split(span):
        line = line.strip(" \t")
        head = KEY_BEGIN.match(line)
        if head is not None:
            line = line[head.end():]
        foot = KEY_END.search(line)
        if foot is not None:
            if line[foot.end():].strip(" \t"):
                return False
            line = line[:foot.start()]
        line = line.strip(" \t")
        if line and not _KEY_LINE.fullmatch(line):
            return False
    return True


def _quoted_runs(mask: _Mask):
    """Rule 1's data: each maximal span inside one quoting (`'…'`, `"…"`,
    `$'…'`) that holds nothing active and starts at its opening quote (not
    after a `$` or backtick inside double quotes, whose name or command
    follows), as (start, end). Where the mask fails closed (a comment,
    after a here-document's `<<`), there is none."""
    state, active, text = mask.state, mask.active, mask.text
    runs, start = [], None
    for k in range(len(text) + 1):
        if start is not None and (state[k] != state[start] or active[k] != active[start]):
            runs.append((start, k - 1))
            start = None
        if start is None and state[k]:
            start = k
    if start is not None:
        runs.append((start, len(text)))
    return [(a, b) for a, b in runs if b > a and text[a - 1] in "'\""]


def _key_blocks(text: str) -> str:
    """`text` with each private key's whole block as one `[redacted]`
    (`redact.private_key_blocks`, SEC-5), within rules 1-3: a block goes only
    from inside one quoted run (so an unterminated block ends at its quote),
    only when it is nothing but key material (`_key_material`), and not at
    all when the line, judged without the blocks, may run text (rule 3, plus
    any shell, interpreter or `./program` it starts). Anything else stays as
    typed. Text with no private-key header or footer is returned unchanged."""
    from ..v2.redact import private_key_blocks

    if private_key_blocks(text) == text:
        return text
    pieces, pos = [], 0
    for a, b in _quoted_runs(_Mask(text)):
        run = text[a:b]
        out = private_key_blocks(run)
        if out == run:
            continue
        p = 0
        while p < min(len(run), len(out)) and run[p] == out[p]:
            p += 1
        s = 0
        while s < min(len(run), len(out)) - p and run[-1 - s] == out[-1 - s]:
            s += 1
        if not _key_material(run[p:len(run) - s]):
            continue
        pieces += [text[pos:a + p], out[p:len(out) - s]]
        pos = b - s
    if not pieces:
        return text
    pieces.append(text[pos:])
    shown = "".join(pieces)
    if _run_names(shown) or _RUNNER_WORD.search(shown) or _RUN_PROGRAM.search(shown):
        return text
    return shown


def redact_command(text) -> str:
    """A command or tool argument as an approval card shows it and a log
    keeps it: whole, never truncated or summarised, with the known values,
    the credentials typed into it, a private key's whole block (`_key_blocks`)
    and the shapes `credential_patterns` knows withheld — and nothing
    withheld that the shell reads as structure or the line may run (rules
    1-3). Never for a judge."""
    from ..credential_patterns import PATTERNS

    text = _known_values(redact_inline(_key_blocks(str(text))))
    for _, pattern in PATTERNS:
        text = _masked_sub(pattern, lambda m: INLINE_REDACTED, text)
    return text


def redact_value(value, depth: int = 0):
    """A JSON-shaped value (a tool call's arguments, an event's data) with
    every string through `redact_command` and a credential-named key's value
    withheld as an env file's would be. Structure, keys and non-strings
    stay, so a record is still valid JSON."""
    if isinstance(value, str):
        return redact_command(value)
    if depth >= 32:
        return "[nested too deep to judge]"
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if isinstance(item, (str, int)) and not isinstance(item, bool) and isinstance(key, str) \
                    and _withheld(key, str(item)):
                out[key] = INLINE_REDACTED
            else:
                out[key] = redact_value(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_value(item, depth + 1) for item in value]
    return value


DISPLAY_SCAN = 4096


def for_display(text: str) -> str:
    """Model-supplied text headed for a one-line surface (v1's tool lines):
    the known values (`scrub`), the credentials typed inline
    (`redact_inline`), a private key's whole block (SEC-6) and every shape
    in `credential_patterns`. Only the first `DISPLAY_SCAN` characters are
    judged and kept."""
    from ..credential_patterns import PATTERNS
    from ..v2.redact import private_key_blocks

    # URLs first, as the label does: a known value inside a URL replaced by
    # the scrub's marker would break the URL apart.
    text = private_key_blocks(redact_inline(scrub(strip_url_credentials(str(text)[:DISPLAY_SCAN]))))
    for _, pattern in PATTERNS:
        text = pattern.sub(INLINE_REDACTED, text)
    return text


def tool_args_for_display(raw) -> str:
    """v1's one-line view of a tool call's raw JSON arguments (the chat
    REPL, the face, a task's or workflow's log, a goal's journal): each
    string argument through `for_display`, so a credential typed into a
    command is not shown or kept. Text that is not a JSON object is judged
    whole. A judge that fails withholds the arguments."""
    try:
        return _tool_args_for_display(raw)
    except Exception:  # noqa: BLE001 — unjudged text is not shown
        return "[arguments withheld]"


def _tool_args_for_display(raw) -> str:
    text = raw if isinstance(raw, str) else str(raw)
    try:
        parsed = json.loads(text) if len(text) <= 1_000_000 else None
    except (ValueError, RecursionError):
        parsed = None
    if not isinstance(parsed, dict):
        return for_display(text)

    def keyed(key, value, depth):
        # `{"password": "v"}`: a value under a credential-named key goes as
        # an env file's would (a number too: `{"pin": 48291357}`).
        if isinstance(value, (str, int)) and not isinstance(value, bool) \
                and _withheld(key, str(value)):
            return INLINE_REDACTED
        return walk(value, depth + 1)

    def walk(node, depth=0):
        if isinstance(node, str):
            return for_display(node)
        if depth >= 4:
            return "…"
        if isinstance(node, dict):
            return {str(k): keyed(str(k), v, depth) for k, v in list(node.items())[:32]}
        if isinstance(node, list):
            return [walk(v, depth + 1) for v in node[:32]]
        return node

    try:
        return json.dumps(walk(parsed), ensure_ascii=False)
    except (TypeError, ValueError):
        return for_display(text)


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


def _process_env_values() -> tuple[str, ...]:
    """Credential values in this process's own environment, by name, for
    `scrub` (never `secret_values`).

    `run_readonly("env")` (or `cat /proc/self/environ`) prints the process
    environment, and in the daemon that holds the provider keys it was
    started with (review of PR #182: the fast path's desk turn could print
    OPENROUTER_API_KEY; the same read was open on v1's surfaces before). Only
    a variable whose *name* reads as a credential (`*_API_KEY`, `*_TOKEN`,
    `*SECRET*` …) and whose value is at least 8 characters and not a plain
    setting is withheld — never PATH or LS_COLORS, which the length rule for
    unnamed values would catch."""
    found = []
    for key, value in os.environ.items():
        # `PWD`/`OLDPWD` read as "password" to the name rule, and their value
        # is the working folder — withholding it blanked every path in every
        # tool's output. A plain value (a path, a number, a host) is a
        # setting under any name.
        if key in _SHELL_STATE or _plain(value):
            continue
        if len(value) >= 8 and _secret_named(_key_parts(key)) and _secret_entry(key, value):
            found.append(value)
    return tuple(found)


_SHELL_STATE = frozenset({"PWD", "OLDPWD"})


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
    # The process's own credential-named environment values too, here only:
    # a tool's output is what `env` reaches. `secret_values()` — the
    # Browser's, gitops' and the action reviewer's list — stays the files'.
    for value in sorted(set(secret_values()) | set(_process_env_values()), key=len, reverse=True):
        if value in out:
            out = out.replace(value, REDACTED)
    return out
