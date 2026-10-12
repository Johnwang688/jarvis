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
# * **Option values that name files** (`find -name`, `rg -g`, `grep
#   --include`, `fd`, Claude's Glob `pattern` and Grep `glob`) get the same
#   test and nothing else: a non-dot-led `*test*` is allowed. A recursive read
#   that happens to cover an env file is the class of `grep -r foo .` — the
#   scrub's job and a stated limit — and refusing `--include='*test*'` while
#   allowing `grep -r` would protect nothing.
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


def env_glob_reaches(pattern: str) -> bool:
    """Could this glob (one file name's worth) expand onto a credential env
    file? Only a dot-led glob can (see above); then the test is exact."""
    if not isinstance(pattern, str) or not pattern:
        return False
    if "{" in pattern:
        from .. import protected_state

        variants = protected_state._brace_expand(pattern)
        if variants is None:
            return True                    # too large to expand: cannot tell, refuse
        if len(variants) > 1 or variants[0] != pattern:
            return any(is_env_credential_name(v) or env_glob_reaches(v) for v in variants)
    if not any(c in pattern for c in "*?["):
        return False
    elements = _glob_elements(pattern.casefold())
    if not _dot_led(elements):
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


def _bundle_glob(pattern: str) -> bool:
    """A glob with some literal substance that matches a credential bundle's
    name (`google_token.*`, `*.json`) — main's rule, unchanged."""
    if not any(c in pattern for c in "*?["):
        return False
    if not re.sub(r"[*?\[\]]", "", pattern):
        return False
    folded = pattern.casefold()
    return any(fnmatchcase(name, folded) for name in _BUNDLE_NAMES)


# Options whose value (the next word, or after `=`) is a file-name pattern a
# program matches against names: find's, ripgrep's, grep's, fd's.
_NAME_PATTERN_OPTIONS = frozenset({
    "-name", "-iname", "-path", "-ipath", "-wholename", "-iwholename", "-lname", "-ilname",
    "-g", "--glob", "--iglob", "--include", "--exclude", "--include-dir", "--exclude-dir",
})
_WORD_BREAK = frozenset(";&|<>()")
# `-c` alone or in a short-option cluster (`-lc`, `-ec`): a shell's (or an
# interpreter's) inline program follows.
_INLINE_FLAG = re.compile(r"-[A-Za-z]*c[A-Za-z]*")


def _shell_words(command: str) -> list[tuple[str, bool]]:
    """The command's words as bash splits them, unquoted, each with whether a
    glob character in it stands outside quotes (so bash expands it). Shell
    operators split words. Approximate where bash is subtle (`$'…'`,
    here-documents): this decides only where a glob is judged."""
    words: list[tuple[str, bool]] = []
    value: list[str] = []
    expands = False
    started = False
    quote = ""
    i, n = 0, len(command)

    def end() -> None:
        nonlocal value, expands, started
        if started:
            words.append(("".join(value), expands))
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
            quote, started = c, True
        elif c == "\\" and i + 1 < n:
            i += 1
            value.append(command[i])
            started = True
        elif c.isspace() or c in _WORD_BREAK:
            end()
        else:
            if c in "*?[{":
                expands = True
            value.append(c)
            started = True
        i += 1
    end()
    return words


def protected_in_command(command: str) -> str | None:
    """The protected filename a shell command references, if any: a bundle,
    `.env`/`.env.local`, or any credential env file by the owner's rule
    (ENV-1) — as a word of its own, a glob that could expand onto one, or
    anywhere in the raw line.

    Read as bash reads it: a backslash-newline inside a name (`.e\\<newline>nv`)
    is deleted before bash sees the word, so it is deleted here too — through
    `rules.join_continuations`, the one implementation of that rule
    (2026-10-09).
    """
    from .. import rules

    command = rules.join_continuations(command)
    words = _shell_words(command)
    previous = ""
    for token, expands in words:
        token = token.strip("\"'")
        name = PurePosixPath(token).name
        # A protected name as a word of its own, and a word matched against
        # the credential bundles' names — quoted or not, as on main.
        if is_protected(name):
            return name
        if _bundle_glob(name):
            return name
        # A credential env file's glob counts where it reaches files: a word
        # bash expands (a glob character outside quotes: `cat .e?v.custom`),
        # or a file-name pattern handed to a program that matches names
        # (`find -name '.env*'`, `rg -g`, `grep --include=.env.*`). A quoted
        # regex (`grep '.*error'`, `pkill -f '.*daemon2'`) is neither.
        if (expands or previous in _NAME_PATTERN_OPTIONS) and env_glob_reaches(name):
            return name
        option, eq, value = token.partition("=")
        if eq and option in _NAME_PATTERN_OPTIONS:
            name = PurePosixPath(value).name
            if is_protected(name) or env_glob_reaches(name):
                return name
        # The words of a string another shell runs (`sh -c 'cat .e?v.x'`,
        # `eval '…'`), judged by the name and the credential-env glob test
        # only. Not every quoted string: a jq filter or a commit message
        # holding `.*env.*` is text, not words a shell expands.
        if any(c.isspace() for c in token) and (
                previous == "eval" or _INLINE_FLAG.fullmatch(previous)):
            for piece in _PIECES.split(token):
                name = PurePosixPath(piece).name if piece else ""
                if name and (is_protected(name) or env_glob_reaches(name)):
                    return name
        previous = token

    for name, pattern in _RAW_NAMES:
        if pattern.search(command):
            return name
    return env_credential_in_text(command)


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
_WHOLE_FILES = frozenset({".env", ".env.local"})
_SECRET_KEY_PARTS = frozenset({
    "key", "keys", "token", "tokens", "secret", "secrets", "pass", "passwd", "password",
    "passwords", "pwd", "credential", "credentials", "private", "salt", "dsn",
})
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


def _key_parts(key: str) -> set[str]:
    key = key.strip()
    if key.lower().startswith("export "):
        key = key[7:].strip()
    return {part.casefold() for part in _KEY_PARTS.findall(key)}


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
    except ValueError:
        return True
    return "@" in parts.netloc or bool(parts.query) or bool(parts.fragment)


def _secret_entry(key: str, value: str) -> bool:
    """Is this value, under this key, a secret to withhold (see above)?"""
    if not value:
        return False
    parts = _key_parts(key)
    if parts & _SECRET_KEY_PARTS:
        return len(value) >= 4 and value.casefold() not in _BOOLEANS
    if parts & _SETTING_KEY_PARTS and len(value) >= 4 and not _plain(value):
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
