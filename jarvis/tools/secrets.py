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

Coverage is deliberate and narrow: `.env`, `.env.local`, and the
use-but-never-see credential bundles — `google_token.json` (Gmail) and
`onshape_keys.json` (CAD). `.env.example` and friends are meant to be read.
"""

from __future__ import annotations

import json
import re
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
import shlex

from .. import config

PROTECTED_NAMES = frozenset(
    {".env", ".env.local", "google_token.json", "onshape_keys.json"}
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


def is_protected(path: str | Path) -> bool:
    """True if `path` points at a `.env` or `.env.local`, at any depth."""
    return PurePosixPath(str(path)).name in PROTECTED_NAMES


def protected_in_command(command: str) -> str | None:
    """The protected filename a shell command references, if any."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    for token in tokens:
        name = PurePosixPath(token.strip("\"'")).name
        if name in PROTECTED_NAMES:
            return name
        # A glob that could expand onto a protected file (`cat .env*`,
        # `cat google_token.*`) is refused — but only when the pattern has
        # some literal substance, so a bare `grep -R foo *` stays usable.
        if any(c in name for c in "*?["):
            literal = re.sub(r"[*?\[\]]", "", name)
            if literal and any(fnmatch(protected, name) for protected in PROTECTED_NAMES):
                return name
    return None


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

    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:_MAX_FILE_BYTES]
    except OSError:
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


def secret_values() -> list[str]:
    """Every credential-looking value in a reachable protected file."""
    values: set[str] = set()
    for directory in _search_dirs():
        for name in (".env", ".env.local"):
            values.update(_values_in(directory / name))
    values.update(_json_values(config.GOOGLE_TOKEN_PATH))
    values.update(_json_values(config.ONSHAPE_TOKEN_PATH))
    return sorted(values, key=len, reverse=True)


def _drop_attributed_lines(text: str) -> str:
    """Remove output lines a tool attributed to a protected file.

    Covers `grep -R` (`path:n:match`) and multi-file `head`/`tail`
    (`==> path <==` followed by contents). The key names leak structure even
    once the values are gone, so the whole line goes.
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
        if ":" in line:
            head = line.split(":", 1)[0].strip()
            if head and is_protected(head):
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
