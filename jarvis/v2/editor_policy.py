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
|            | the env files), anything under `config.V2_CREDENTIAL_DIRS`   |                      |
|            | or v2's session carriers (`V2_DATA_DIR/claude`, `/codex`),   |                      |
|            | and the login files (Claude's, Codex's, git's, `.netrc`,     |                      |
|            | docker's, kube's, `.npmrc`, `.pypirc`)                       |                      |
| `safety`   | SELF_PROTECTED, git's own internals and configuration, the   | readable, read-only  |
|            | built HUD the daemon serves (`files._self_protected`)        |                      |
| `env`      | a credential env file (`secrets.is_env_credential`): the     | the owner edits it;  |
|            | owner's to edit, Jarvis's never to read — never demoted:     | Jarvis can't read it |
|            | one that is not UTF-8 stays `env`, read-only (`writable`)    |                      |
| `binary`   | not UTF-8 text, or a NUL in the first 8 KiB (`classify_bytes`)| readable, read-only |
| `text`     | everything else                                              | editable             |

`gate` comes before `withheld` because the gate's state lives inside a
credential folder and E-8 keeps it readable — only a gate path that resolves
to itself and is no credential by another rule (`Policy._readable_gate`),
and only in the editor's own routes: `hud_api.protected` has no exemption; `withheld` before `safety`
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
    # False: an editable kind the editor may still not write — an env file
    # whose bytes are not UTF-8 text keeps its kind (the lock, Add to chat
    # closed, its own language) but is read-only (`classify_bytes`).
    writable: bool = True

    @property
    def editable(self) -> bool:
        return self.kind in EDITABLE and self.writable

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
           home / ".git-credentials", home / ".netrc",
           # The common tool credential stores (review of PR #49, LOW3): a
           # registry login, a cluster's client certificate, a package
           # index's upload token. `~/.aws`, `~/.gnupg` and `~/.config/gh`
           # are credential folders already (`config.V2_CREDENTIAL_DIRS`).
           home / ".docker" / "config.json", home / ".kube" / "config",
           home / ".npmrc", home / ".pypirc"}
    claude = os.environ.get("JARVIS_CLAUDE_CREDENTIALS")
    if claude:
        out.add(Path(claude))
    codex = os.environ.get("CODEX_HOME")
    if codex:
        out.add(Path(codex) / "auth.json")
    docker = os.environ.get("DOCKER_CONFIG")
    if docker:
        out.add(Path(docker) / "config.json")
    for kube in (os.environ.get("KUBECONFIG") or "").split(os.pathsep):
        if kube:
            out.add(Path(kube))
    npmrc = os.environ.get("NPM_CONFIG_USERCONFIG")
    if npmrc:
        out.add(Path(npmrc))
    xdg = os.environ.get("XDG_CONFIG_HOME")
    out.add((Path(xdg) if xdg else home / ".config") / "git" / "credentials")
    return {_resolve(p) for p in out}


def carrier_dirs() -> list[Path]:
    """Where v2 keeps a session's credential carriers: Claude's private
    `mcp-<nonce>.json` (a jarvis-mcp caller token) and each Codex thread's
    private home (`config.toml`'s env table carries the same token)."""
    data = Path(config.V2_DATA_DIR)
    return [_resolve(data / "claude"), _resolve(data / "codex")]


# A dropped file arrives with a name and no place on this machine (a browser
# sends the base name; Windows paths are judged with `/`). These names are
# credential stores wherever they sit; a name that carries its folder is
# judged by its tail too (`.docker/config.json`, `.aws/credentials`).
CREDENTIAL_FILE_NAMES = frozenset({".npmrc", ".pypirc", ".netrc", "_netrc", ".git-credentials"})
CREDENTIAL_FILE_TAILS = (("docker", "config.json"), (".docker", "config.json"), (".kube", "config"),
                         (".aws", "credentials"), (".aws", "config"), ("gh", "hosts.yml"),
                         (".claude", ".credentials.json"), (".codex", "auth.json"), ("git", "credentials"),
                         (".ssh",), (".gnupg",))


def credential_name(name: str) -> bool:
    """A dropped file's name is a credential store's (LOW3): never attached."""
    parts = [p for p in name.replace("\\", "/").split("/") if p]
    if not parts:
        return False
    lower = [p.lower() for p in parts]
    if lower[-1] in CREDENTIAL_FILE_NAMES:
        return True
    for tail in CREDENTIAL_FILE_TAILS:
        if len(tail) == 1 and tail[0] in lower[:-1]:
            return True
        if len(tail) == 2 and lower[-2:] == list(tail):
            return True
    return False


def credential_dirs() -> list[Path]:
    return [_resolve(Path(d)) for d in config.V2_CREDENTIAL_DIRS] + carrier_dirs()


def jarvis_dirs() -> list[Path]:
    """Jarvis's own config and data folders: nothing in them is a page.

    The preview origin refuses every path under one (review of PR #49,
    LOW3): it answers any loopback client, `fetch_page` included, and the
    Inbox is rooted at `~`."""
    home = Path.home()
    dirs = {home / ".config" / "jarvis", home / ".local" / "share" / "jarvis",
            Path(config.V2_DATA_DIR), Path(config.SESSIONS_DIR), Path(config.SPILL_DIR),
            Path(config.GOALS_DIR), Path(config.VOICES_DIR)}
    return [_resolve(d) for d in dirs]


def _under(path: Path, directory: Path) -> bool:
    return path == directory or directory in path.parents


def hidden_component(path: Path, root: Path) -> bool:
    """Does `path` have a hidden component below `root` — a name starting
    with `.` (`.git` and `.GIT`, `.docker/config.json`, `.npmrc`, a tool's
    dotfolder), `.well-known` excepted? `path` must lie in `root`
    (`ValueError` otherwise). What the preview origin (`hud_api._not_a_page`)
    and the Browser's uploads (`act_rules.upload_refusal`) both refuse."""
    rel = Path(path).relative_to(root)
    return any(part.startswith(".") and part != ".well-known" for part in rel.parts)


class Policy:
    """One request's view of the rules: the resolved sets are read once, so a
    tree of a thousand entries does not resolve the gate's paths a thousand
    times. Build a new one per request — the sets follow `config` and HOME."""

    def __init__(self) -> None:
        self.credential_dirs = credential_dirs()
        self.login_files = login_files()
        self.gate = self._readable_gate()

    def _readable_gate(self) -> set[Path]:
        """The gate's state files E-8 keeps readable in the editor.

        A gate path counts only when it **resolves to itself** — no symlink
        anywhere on its way — and is not a credential by any other rule: a
        protected or env name, a login file, or a credential folder other than
        the gate's own (`~/.config/jarvis`, where E-8 keeps it readable). A
        `models.json -> ~/.ssh/id_x` symlink used to put the key itself on the
        gate's list, and the gate's readable exemption then let the preview
        origin serve it and an `@path` attach it (review of PR #49, LOW2).

        The exemption is the editor's own read path only: `hud_api.protected`
        asks `withheld_anywhere`, which has none."""
        from .permissions import protected_paths
        from jarvis.tools.files import _protected_state_paths
        own = _resolve(Path("~/.config/jarvis"))
        configured = {Path(os.path.abspath(p.expanduser())) for p in _protected_state_paths()}
        ok = set()
        for path in configured:
            real = _resolve(path)
            if real != path:
                continue  # a symlink, here or on the way: judged where it goes, as any file
            if v1_secrets.is_protected(real) or is_env_credential(real) or real in self.login_files:
                continue
            if any(_under(real, d) for d in self.credential_dirs if d != own):
                continue
            ok.add(real)
        # Never wider than the set the write tools refuse.
        return ok & protected_paths()

    # -- by path ---------------------------------------------------------------

    def withheld_anywhere(self, raw: Path, resolved: Path) -> bool:
        """Withheld, with no gate exemption: what an `@path`, the preview
        origin and a task diff refuse (`hud_api.protected`)."""
        return self._withheld(raw, resolved)

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

    def tree_flag(self, raw: Path, resolved: Path, *, is_dir: bool = False) -> str | None:
        """What a tree row may say before anything is read: `withheld` or
        `env`, else None (read-only kinds are said when the file opens). Only
        a file is an env file: a virtualenv folder named `.env` is a folder."""
        if self.is_withheld(raw, resolved):
            return WITHHELD
        if not is_dir and (is_env_credential(raw) or is_env_credential(resolved)):
            return ENV
        return None

    def gate_inside(self, directory: Path) -> set[Path]:
        """The gate-state files at or below a withheld directory: the only
        names listed inside one (E-8 keeps them readable)."""
        return {p for p in self.gate if _under(p, directory)}


def classify_bytes(verdict: Verdict, data: bytes) -> Verdict:
    """A readable kind, made read-only when its bytes are not text.

    Text becomes `binary`. **An env file stays `env`** — it is never demoted:
    a UTF-16 `.env.production` (PowerShell 5.1 writes those, and the VEX
    projects live on `/mnt/c`) or a Latin-1 one is still a credential, so it
    keeps the lock, Add to chat stays closed and its language stays
    `dotenv`; it is only no longer writable (review of PR #49, MED1)."""
    if verdict.kind in (TEXT, ENV) and not is_text(data):
        why = ("it holds NUL bytes" if b"\x00" in data[:NUL_PROBE] else "it is not UTF-8")
        tail = f"read-only here: {why}, and the editor never rewrites a file it cannot show exactly."
        if verdict.kind == ENV:
            return Verdict(ENV, f"{REASONS[ENV]} And {tail}", writable=False)
        return Verdict(BINARY, tail)
    return verdict
