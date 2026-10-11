"""What the HUD's editor may do with a file: the one classifier (ED-2).

Editor plan §2.4, decisions E-7/E-8, and the owner's `.env*` rule
(`docs/plans/2026-10-11-env-files.md`). Every HUD file route — the tree, the
read, the save and the change poll — asks `Policy.classify`, and nothing else
keeps a copy of any of these rules: each kind is decided by the module that
owns the rule (`secrets.is_env_credential` for env files,
`protected_state.protected_paths` for the gate's state, `files._self_protected`
for the safety layer). Two copies of a rule drift, and the looser one decides.

The kinds, in the order they are decided (the first that matches wins):

| kind       | what                                                         | in the editor        |
|------------|--------------------------------------------------------------|----------------------|
| `gate`     | Jarvis's own permission state: `protected_paths()` (the      | readable, read-only  |
|            | allowlist, models.json, routing.json …), even inside         |                      |
|            | `~/.config/jarvis` (E-8: "gate-state files stay readable")   |                      |
| `withheld` | a credential bundle (`secrets.PROTECTED_NAMES` other than    | no content, ever     |
|            | the env files), anything under `config.V2_CREDENTIAL_DIRS`,  |                      |
|            | and the login files (Claude's, Codex's, git's, `.netrc`)     |                      |
| `safety`   | SELF_PROTECTED, git's own internals and configuration, the   | readable, read-only  |
|            | built HUD the daemon serves (`files._self_protected`)        |                      |
| `env`      | a credential env file (`secrets.is_env_credential`): the     | the owner edits it;  |
|            | owner's to edit, Jarvis's never to read                      | Jarvis can't read it |
| `binary`   | not UTF-8 text, or a NUL in the first 8 KiB (`classify_bytes`)| readable, read-only |
| `text`     | everything else                                              | editable             |

`gate` comes before `withheld` because the gate's state lives inside a
credential folder and E-8 keeps it readable; `withheld` before `safety`
because `files._git_internal` names `~/.git-credentials` (a credential the
editor must never show); `safety` before `env`, so a `.env` inside the
served HUD build stays unwritable.

**Every route that serves an `env` file's content is owner-only** (the HUD
listener and the HUD's Origin, `projects.owner_only`): the API listener every
tool's HTTP client uses never gets it. Every editor file route is owner-only
anyway (E-7). The content goes between the disk and the owner's editor and
nowhere else — never the bus, a log line, `log.jsonl`, Discord, an
attachment or a tool result.

None of this is a boundary against a program running as the owner; it is what
the HUD's own routes will and will not do.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path

from jarvis import config
from jarvis.tools import files as v1_files, secrets as v1_secrets
from jarvis.tools.secrets import is_env_credential

GATE = "gate"
WITHHELD = "withheld"
SAFETY = "safety"
ENV = "env"
BINARY = "binary"
TEXT = "text"
KINDS = (GATE, WITHHELD, SAFETY, ENV, BINARY, TEXT)
# What the editor may save. Everything else is read-only (or withheld).
EDITABLE = frozenset({TEXT, ENV})

# How much of a file the not-text test reads for a NUL (git's own rule).
NUL_PROBE = 8 * 1024

REASONS = {
    GATE: ("Jarvis's own permission state: read-only here, because changing it changes what runs "
           "without anyone being asked. Edit it by hand."),
    WITHHELD: "a credential file: its contents are never sent to this window.",
    ENV: "Jarvis can't read this file. Only you can, here.",
}


@dataclass(frozen=True)
class Verdict:
    kind: str
    # The sentence the editor shows beside a lock (read-only, withheld or env).
    reason: str | None = None

    @property
    def editable(self) -> bool:
        return self.kind in EDITABLE

    @property
    def withheld(self) -> bool:
        return self.kind == WITHHELD


def etag(data: bytes) -> str:
    """The content precondition: the first 128 bits of SHA-256, as hex."""
    return hashlib.sha256(data).hexdigest()[:32]


def is_text(data: bytes) -> bool:
    """UTF-8 throughout, and no NUL in the first 8 KiB."""
    if b"\x00" in data[:NUL_PROBE]:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def eol_of(text: str) -> str | None:
    """`LF`, `CRLF`, `CR`, `mixed`, or None for text with no line break."""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    kinds = [name for name, n in (("LF", lf), ("CRLF", crlf), ("CR", cr)) if n]
    if not kinds:
        return None
    return kinds[0] if len(kinds) == 1 else "mixed"


def _resolve(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except (OSError, RuntimeError):
        return Path(os.path.abspath(os.path.expanduser(str(path))))


def login_files() -> set[Path]:
    """Login files that are credentials but neither a protected name nor in a
    credential folder (E-8), resolved, read per call so a test's HOME is the
    one judged. Claude's and Codex's are the decided pair; git's credential
    store and `.netrc` are here because nothing else withholds them, and
    `files._git_internal` would otherwise show `~/.git-credentials` read-only,
    in full."""
    home = Path.home()
    out = {home / ".claude" / ".credentials.json", home / ".codex" / "auth.json",
           home / ".git-credentials", home / ".netrc"}
    claude = os.environ.get("JARVIS_CLAUDE_CREDENTIALS")
    if claude:
        out.add(Path(claude))
    codex = os.environ.get("CODEX_HOME")
    if codex:
        out.add(Path(codex) / "auth.json")
    xdg = os.environ.get("XDG_CONFIG_HOME")
    out.add((Path(xdg) if xdg else home / ".config") / "git" / "credentials")
    return {_resolve(p) for p in out}


def credential_dirs() -> list[Path]:
    return [_resolve(Path(d)) for d in config.V2_CREDENTIAL_DIRS]


def _under(path: Path, directory: Path) -> bool:
    return path == directory or directory in path.parents


class Policy:
    """One request's view of the rules: the resolved sets are read once, so a
    tree of a thousand entries does not resolve the gate's paths a thousand
    times. Build a new one per request — the sets follow `config` and HOME."""

    def __init__(self) -> None:
        from .permissions import protected_paths
        self.gate = protected_paths()
        self.credential_dirs = credential_dirs()
        self.login_files = login_files()

    # -- by path ---------------------------------------------------------------

    def _withheld(self, raw: Path, resolved: Path) -> bool:
        for candidate in (raw, resolved):
            # A credential bundle by name; the env files are the owner's to
            # edit, so those names are judged as `env` instead.
            if v1_secrets.is_protected(candidate) and not v1_secrets.is_env_credential_name(candidate.name):
                return True
        lexical = Path(os.path.abspath(raw))
        if resolved in self.login_files or lexical in self.login_files:
            return True
        return any(_under(p, d) for d in self.credential_dirs for p in (lexical, resolved))

    def is_withheld(self, raw: Path, resolved: Path) -> bool:
        """Withheld, and not the gate's state (which E-8 keeps readable)."""
        return resolved not in self.gate and self._withheld(raw, resolved)

    def classify(self, raw: Path, resolved: Path) -> Verdict:
        """The kind a path is, before its bytes are read. `raw` is the path as
        written (absolute), `resolved` where the filesystem takes it."""
        if resolved in self.gate:
            return Verdict(GATE, REASONS[GATE])
        if self._withheld(raw, resolved):
            return Verdict(WITHHELD, REASONS[WITHHELD])
        safety = self._safety(raw, resolved)
        if safety:
            return Verdict(SAFETY, safety)
        if is_env_credential(raw) or is_env_credential(resolved):
            return Verdict(ENV, REASONS[ENV])
        return Verdict(TEXT)

    @staticmethod
    def _safety(raw: Path, resolved: Path) -> str | None:
        try:
            hit = v1_files._self_protected(raw) or v1_files._self_protected(resolved)
        except (OSError, RuntimeError, ValueError):
            hit = True  # cannot tell: read-only
        if not hit:
            return None
        if v1_files._git_internal(resolved):
            return ("git's own configuration or internals, which git executes or obeys: "
                    "read-only here. Edit it by hand.")
        if v1_files._hud_build(resolved):
            return ("the built HUD the daemon serves, whose script can approve what you are asked: "
                    "read-only here. Change the HUD's source and rebuild.")
        return "part of Jarvis's safety layer: read-only here. Edit it by hand."

    def tree_flag(self, raw: Path, resolved: Path) -> str | None:
        """What a tree row may say before anything is read: `withheld` or
        `env`, else None (read-only kinds are said when the file opens)."""
        if self.is_withheld(raw, resolved):
            return WITHHELD
        if is_env_credential(raw) or is_env_credential(resolved):
            return ENV
        return None

    def gate_inside(self, directory: Path) -> set[Path]:
        """The gate-state files at or below a withheld directory: the only
        names listed inside one (E-8 keeps them readable)."""
        return {p for p in self.gate if _under(p, directory)}


def classify_bytes(verdict: Verdict, data: bytes) -> Verdict:
    """A readable kind, demoted to `binary` when its bytes are not text."""
    if verdict.kind in (TEXT, ENV) and not is_text(data):
        why = ("it holds NUL bytes" if b"\x00" in data[:NUL_PROBE] else "it is not UTF-8")
        return Verdict(BINARY, f"read-only here: {why}, and the editor never rewrites a file it cannot "
                               "show exactly.")
    return verdict
