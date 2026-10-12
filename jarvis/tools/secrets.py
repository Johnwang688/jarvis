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
import shlex

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
    rf"(?<![{_NAME_CHAR}])(\.env(?:\.[^\s'\"`;&|<>()/\\:,=}}]*)?)(?![A-Za-z0-9_\-])",
    re.IGNORECASE)

# What a glob is tried against, besides `PROTECTED_NAMES`: the env names
# people actually keep. A glob that is not spelled from `.env.` can still
# reach one through `find -name`, `grep --include`, `rg -g` or bash's
# `dotglob` (`*.production` finds `.env.production` there), and those are
# judged against this sample — never against "any `.env.<anything>`", which
# every `*.py` would match (`.env.py`). One spelled from `.env.` is judged by
# the rule itself (`glob_reaches`).
ENV_SAMPLES = frozenset({
    ".env.production", ".env.development", ".env.staging", ".env.test", ".env.prod",
    ".env.dev", ".env.stage", ".env.qa", ".env.ci", ".env.preview", ".env.local",
    ".env.production.local", ".env.development.local", ".env.test.local",
    ".env.staging.local", ".env.backup", ".env.bak", ".env.old", ".env.orig",
    ".env.save", ".env.sample", ".env.template", ".env.vault", ".env.1", ".env.2",
})


def env_credential_in_text(text: str) -> str | None:
    """The first credential env file named anywhere in `text` (a raw command
    line, a script, an argument), or None. The raw-line half of
    `protected_in_command`; terminal_read's guard asks it too."""
    for match in _ENV_IN_TEXT.finditer(text):
        if is_env_credential_name(match.group(1)):
            return match.group(1)
    return None


def glob_reaches(pattern: str) -> bool:
    """Could this glob (one file name's worth, glob characters in it) expand
    onto a credential file? Case-folded, because the env rule is.

    Refused only when the pattern has some literal substance, so a bare
    `grep -R foo *` stays usable; then: it matches a protected name or one of
    `ENV_SAMPLES` (`.env*`, `.e??`, `google_token.*`, `*.local`,
    `*.production`), or it is spelled from `.env.` and does not end in
    `.example` (`.env.p*`, `.env.[sS]taging`, `.env.example*` — which reaches
    `.env.example.bak`). `.env.*.example` can only name `.example` files and
    is not refused.

    Brace lists are expanded first, as bash and the glob tools do
    (`*.{ts,production}` is `*.ts` and `*.production`; one too large to
    expand is refused: a guard that cannot tell refuses)."""
    if "{" in pattern:
        from .. import protected_state

        variants = protected_state._brace_expand(pattern)
        if variants is None:
            return True
        if len(variants) > 1 or variants[0] != pattern:
            return any(is_protected(v) or glob_reaches(v) for v in variants)
    if not any(c in pattern for c in "*?["):
        return False
    if not re.sub(r"[*?\[\]]", "", pattern):
        return False
    folded = pattern.casefold()
    if any(fnmatchcase(name, folded) for name in PROTECTED_NAMES | ENV_SAMPLES):
        return True
    return folded.startswith(".env.") and not folded.endswith(".example")


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
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    for token in tokens:
        token = token.strip("\"'")
        # An option's value is judged as a word of its own too:
        # `--include=*.production` makes grep search `.env.production`.
        words = [token, token.split("=", 1)[1]] if token.startswith("-") and "=" in token else [token]
        for word in words:
            name = PurePosixPath(word).name
            if is_protected(name):
                return name
            # A glob that could expand onto a protected file (`cat .env*`,
            # `cat google_token.*`, `cat .env.p*`) is refused.
            if glob_reaches(name):
                return name

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
        data = os.read(fd, _MAX_FILE_BYTES)
    except OSError:
        return None
    finally:
        os.close(fd)
    return data.decode("utf-8", errors="replace")


def _values_in(path: Path) -> tuple[str, ...]:
    """Credential-looking values in one env file, cached until it changes."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _cache.pop(path, None)
        return ()

    cached = _cache.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    text = _read_regular(path)
    if text is None:
        return ()

    values: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        value = line.partition("=")[2].strip().strip("'\"")
        if len(value) >= MIN_SECRET_LEN:
            values.add(value)

    # Longest first, so a value that contains another is replaced whole.
    ordered = tuple(sorted(values, key=len, reverse=True))
    _cache[path] = (mtime, ordered)
    return ordered


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


_listing_cache: dict[Path, tuple[int, tuple[Path, ...]]] = {}


def env_files_in(directory: Path) -> tuple[Path, ...]:
    """Every credential env file directly in `directory`, by the one name
    rule (`.env`, `.env.local`, `.env.production`, … — never `.env.example`),
    sorted; cached until the folder's own mtime moves (adding or removing a
    file moves it). A folder that cannot be listed holds none we can see."""
    try:
        stamp = directory.stat().st_mtime_ns
    except OSError:
        _listing_cache.pop(directory, None)
        return ()
    cached = _listing_cache.get(directory)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    try:
        with os.scandir(directory) as entries:
            found = tuple(sorted(directory / entry.name for entry in entries
                                 if is_env_credential_name(entry.name)))
    except OSError:
        return ()
    _listing_cache[directory] = (stamp, found)
    return found


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
