"""The five permission layers of design §6, behind one callback.

`build_permit(ctx, asker)` returns the `PermissionCallback` every provider is
handed. The layers are evaluated in order and the first to answer wins:

1. **DENY** — v1 `rules.decide()`'s never-approvable verdicts, v1
   `secrets.protected_in_command`, and, for file tools, any target that is
   SELF_PROTECTED, the persistent allowlist, a credential bundle, a
   credential env file (ENV-1: `*.example` only), or the v2
   `allowlist.json` / `models.json` / `routing.json` trio; for read tools
   (Claude's Read, Grep, Glob, NotebookRead), a credential file or a glob onto
   one; for a sandbox grant, one that is or holds a credential env file.
   Never asked of anyone, and unreachable through the escape hatch (§6.1).
2. **ALWAYS-ASK** — `config.V2_ALWAYS_ASK_DEFAULTS` plus the additions in
   `config.V2_ALWAYS_ASK` and `Brief.always_ask` / `Project.always_ask`. A
   match goes to the owner regardless of what any reviewer thought. First,
   in code no file can edit, `merge_gate` (owner decision 2026-10-15):
   merging or closing a pull request, a push to the default branch, a
   branch deletion or a force push — asked with a plain headline ("merges a
   pull request") and never an Always, in every profile.
3. **CLI reviewer** — not here. See the ALLOW note below.
4. **Jarvis ALLOW** — the owner's persistent allowlist, or an ALLOW verdict
   from `rules`, honoured **only for a human-backed asker**. This is the v1
   invariant the 2026-08-09 round nearly shipped broken: a deny-all asker
   (strict profile, a thread nobody is watching) never auto-approves.
5. **Human** — profile ASK asks; profile AUTO returns ALLOW (layer 3 —
   the provider's own classifier still decides); profile STRICT denies.

One exception sits between layers 1 and 2: a call the provider marks as a
**sandbox widening** (`permit(..., widening="network on · …")`, Codex only)
is always asked of a human, with no Always, whatever the profile — strict
denies it. Owner Codex chats use native `user` review and classify every
command/file approval this way. Task approvals retain `auto_review`. Neither
may let the AUTO "provider reviewer decides" fallback silently grant a widening.
A **dangerous Jarvis tool** the jarvis-mcp bridge runs for a chat
(`permit(..., dangerous_tool=True)`) is handled the same way, for the same
reason: the owner, every time, no Always. A Codex chat's git tools
(`chat_git`, owner decision CG-0) are judged by the exact line the daemon
will run (`chat_git.gate_line`), so merge_gate's headline reaches the card;
a target that cannot be read is refused unasked, and the ALLOW is bound to
that line (`chat_git.grant`). A Codex chat's escalated raw network git or gh
is declined unasked with a pointer to those tools (CG-1).

Between layers 2 and 4 sits **`jarvis-tool`** (2026-10-10, plan §4.1): a
read-only Jarvis session tool (`session_tools`, `mcp__jarvis__<tool>`) in an
owner's chat is ALLOWed, never asked of anyone — the daemon's bridge judges
each call. It comes after layer 2, so an always-ask rule naming it still asks.
Exact offered tools on a Claude owner's own session server also use the
`jarvis-session-delegate` layer. Nondangerous writes remain behind always-ask;
dangerous calls delegate here and ask the owner once at bridge execution,
never as an automatic execution approval. Strict refuses both paths.

**`Decision.ALLOW` out of this callback means "not refused by Jarvis", not
"approved by the owner"** — except when layer 2 or layer 5 produced it, which
is the only case where a human actually said yes. Which layer answered is
carried on every `ApprovalRecord` written to
`config.V2_DATA_DIR/decisions.jsonl`, so the log can tell the two apart; a
surface that shows "the owner approved this" must read the layer, not the
decision. WP3's Claude hook maps an ALLOW under AUTO to `{}` (no opinion,
the classifier still runs) and under ASK to an explicit allow, which is the
same distinction seen from the provider side.

**R8 is not solved here and this file does not pretend otherwise.** Codex has
no universal pre-tool callback under `auto_review`, so layer 2 cannot be
enforced from the approval requests its reviewer chooses to escalate.
Owner chats use `user` review for escalations, but ordinary sandboxed tools
still have no universal pre-tool hook. `CodexProvider` refuses a brief with a non-empty `always_ask` rather than
claim an enforcement it lacks. The seam is `always_ask_for()` below: whatever
mechanism answers R8 (a Codex pre-execution hook, or a `jarvis-mcp` tool with
the native equivalent removed from the toolset) consumes the same list.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from fnmatch import fnmatch
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import threading
from typing import Callable, Iterable

from jarvis import config, permissions as v1_permissions, protected_state, rules
from jarvis.tools import files as v1_files, secrets as v1_secrets
from .approvals import (ApprovalRequest, DenyAll, V2_COMMAND_TOOLS, clean_line, command_text,
                        is_command_tool, is_unknown_command_tool, label,
                        unreadable_command, v1_request)
from .model import PermissionProfile, Project, Role, Task, utcnow
from .provider import Brief, Decision, PermissionCallback
from . import chat_git, merge_gate, session_tools
from .subagents import valid_sid as _valid_sid

LOG = logging.getLogger(__name__)

# Layer names, as they appear in the decision log.
L_DENY, L_ALWAYS_ASK, L_REVIEWER, L_JARVIS_ALLOW, L_HUMAN = (
    "deny", "always-ask", "reviewer", "jarvis-allow", "human")
# A provider-reported sandbox widening (see `build_permit`'s `widening`).
L_WIDENING = "sandbox-widening"
# The owner's yes to one exact call, reused when the CLI asks again about the
# same tool_use_id (Claude AUTO: the classifier's own ask after layer 2).
L_OWNER_YES = "owner-yes-reused"
OWNER_YES_TTL_S = 120.0
OWNER_YES_CAP = 256
# A read-only Jarvis session tool a Claude chat calls through jarvis-mcp
# (`session_tools`, plan 2026-10-10 §4.1): ALLOWed without asking anyone,
# because the daemon judges each call itself (the turn, the guard). Not a
# human decision, and never logged as one.
L_JARVIS_TOOL = "jarvis-tool"
L_JARVIS_DELEGATE = "jarvis-session-delegate"
L_JARVIS_GATED = "jarvis-gated"
# CG-1 (owner, 2026-10-15): a Codex chat's escalated raw network git or gh,
# declined unasked and pointed at the Jarvis git tools (`chat_git`).
L_CHAT_GIT = "chat-git"


@dataclass(frozen=True)
class PermitVerdict:
    """One permit call's answer, as `finish` logged it (`last_verdict`)."""
    decision: Decision
    layer: str
    reason: str
    asked: bool             # the call was put to the asker (a card or a DM)


_LAST_VERDICT: ContextVar[PermitVerdict | None] = ContextVar("jarvis_v2_permit_verdict",
                                                             default=None)


def last_verdict() -> PermitVerdict | None:
    """The answer of the last permit call made in this context, once
    (reading clears it). A DENY with `asked` False was refused before anyone
    was asked — layer 1, a chat git tool's `Refused` (CG-0), CG-1, the strict
    profile — and `reason` says why: what the model is told in place of "the
    user declined" (#223's review, LOW 1). It decides nothing."""
    verdict = _LAST_VERDICT.get()
    _LAST_VERDICT.set(None)
    return verdict


# Real shells, per provider (`approvals`' one list, Claude's Monitor
# included). Layers 1 and 2 judge more: `is_command_tool` also counts any
# tool whose arguments carry a `command`/`cmd`/`script` line (fail closed,
# 2026-10-15), except the CODE_TOOLS (Workflow's JavaScript). Layer 4 trusts
# only these: an unknown command-bearing tool never reaches the allowlist or
# rules' static ALLOW, and its cards offer no Always (#211 review, F1).
COMMAND_TOOLS = V2_COMMAND_TOOLS

# Tools that write a file. The argument holding the target differs per
# provider, so every plausible spelling is read rather than one guessed key:
# a file tool whose target we cannot find must not slip past layer 1.
FILE_TOOLS = frozenset({
    "Write", "Edit", "MultiEdit", "NotebookEdit",    # Claude
    "apply_patch", "patch",                           # Codex
    "write_file", "edit_file",                        # fast path / jarvis-mcp
})
_PATH_KEYS = ("file_path", "notebook_path", "path", "filename", "file")
_PATHS_KEYS = ("paths", "files", "file_paths")

# Tools that read a file or search a tree (Claude's), judged at layer 1 for a
# credential env file (ENV-1: the owner's `.env*` rule — Jarvis never reads
# one). The fast path's and jarvis-mcp's `read_file`/`grep_files` are v1's
# own tools, which refuse inside; they are listed so a permit asked about
# them answers the same.
READ_TOOLS = frozenset({
    "Read", "NotebookRead", "Grep", "Glob", "LS",     # Claude
    "read_file", "grep_files",                        # fast path / jarvis-mcp
})
# The arguments that hold a glob over file names (Glob's `pattern`, Grep's
# `glob` filter): judged by the glob rule, never as a path. Grep's `pattern`
# is a regular expression over contents and is not a file name at all.
# The file-name globs of a read tool that reads what it selects (ENV-1).
# Claude's Glob is not here: it returns names, never contents (delta review
# NIT), so `**/.*` and `**/*.env*` list files and are allowed, as on main.
_GLOB_KEYS = {"Grep": ("glob",), "grep_files": ("glob",)}
# Tools whose glob and path also get the raw-line check the shell gets
# (delta review LOW-B: `glob: "*.env*"` as `--include='*.env*'`).
_RAW_CHECKED = frozenset({"Grep", "grep_files"})

_PROTECTED_BRANCHES = ("main", "master", "prod*", "production*", "release*")
_PROD_MARKER = re.compile(r"(?<![A-Za-z])(prod|production|live)(?![A-Za-z])", re.I)
_MIGRATE_PROD_PHRASES = (
    ("alembic", "upgrade"), ("rails", "db:migrate"), ("rake", "db:migrate"),
    ("flyway", "migrate"), ("dbmate", "up"),
)
_MIGRATE_ALWAYS_PHRASES = (
    ("prisma", "migrate", "deploy"), ("supabase", "db", "push"),
)
_CREDENTIAL_PHRASES = (("gh", "auth"), ("vercel", "login"), ("aws", "configure"))
_CREDENTIAL_STEMS = frozenset({"ssh-keygen", "ssh-copy-id", "ssh-add"})

_log_lock = threading.Lock()


# --------------------------------------------------------------------------
# context


@dataclass
class PermitContext:
    """Who is asking. `task` is None for a chat thread."""
    thread_id: str
    brief: Brief
    provider: str = ""
    project: Project | None = None
    task: Task | None = None
    # Factory captures exact request-time session/worker/turn, not a successor.
    delegation_cancelled: Callable | None = field(default=None, repr=False)
    # Display only (chat-to-chat C6): the clean title of the chat whose
    # message or reply started the turn a card is raised in, "" for any other
    # turn. Read by `_ask` for the card and by nothing that decides.
    relayed_from: Callable[[], str] | None = field(default=None, repr=False)

    @property
    def task_id(self) -> str | None:
        if self.task is not None:
            return self.task.id
        return self.brief.task_id if self.brief is not None else None

    def origin(self) -> str:
        """The sanitized attribution an approval surface shows (v1 `_label`)."""
        task_id = self.task_id
        if task_id is None:
            return ""
        name = self.task.brief if self.task is not None else ""
        return label(task_id, name)

    def relayed(self) -> str:
        """The card's "turn started by a message from chat" title, or "".
        Display only: a reader that fails or returns a non-string shows nothing."""
        try:
            value = self.relayed_from() if self.relayed_from is not None else ""
        except Exception:  # noqa: BLE001
            return ""
        return value if isinstance(value, str) else ""


@dataclass
class ApprovalRecord:
    tool: str
    args_digest: str
    layer: str
    decision: str
    reason: str = ""
    command: str | None = None
    thread_id: str | None = None
    task_id: str | None = None
    provider: str | None = None
    at: str = field(default_factory=utcnow)

    def to_json(self) -> dict:
        return dict(self.__dict__)


def _digest(args: dict) -> str:
    """A stable fingerprint of the arguments. A digest, not the arguments:
    the log is a durable file and a tool call can carry anything."""
    try:
        blob = json.dumps(args, sort_keys=True, default=str)
    except Exception:
        blob = repr(args)
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:16]


def log_decision(record: ApprovalRecord, path: Path | None = None) -> None:
    """Append to the v2 decision log. A log that cannot be written must never
    be a reason a decision fails, so every error here is swallowed after a
    warning — the decision itself already happened."""
    target = Path(path) if path is not None else Path(config.V2_DATA_DIR) / "decisions.jsonl"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        data = record.to_json()
        for key in ("reason", "command"):
            # A reason can quote the command (a rule's match, a reviewer's
            # words); a command a caller did not pass through `_safe_command`
            # is kept as that keeps it (idempotent when it did).
            if data.get(key):
                try:
                    data[key] = v1_secrets.redact_command(str(data[key]))[:2000]
                except Exception:
                    data[key] = f"[unscrubbable {key} withheld]"
        line = json.dumps(data, allow_nan=False) + "\n"
        with _log_lock:
            with open(target, "a", encoding="utf-8") as handle:
                handle.write(line)
    except Exception:
        LOG.warning("Cannot write the v2 decision log at %s", target, exc_info=True)


# --------------------------------------------------------------------------
# layer 1 — DENY


def protected_paths() -> set[Path]:
    """The gate-state files — v1's allowlist, `models.json`, `routing.json`,
    `provider_defaults.json`, the Discord guild file and the saved Codex
    catalog (`codex-models.json`, PR #20) — resolved.

    One set for v1 and v2 (2026-10-08): it is `jarvis.tools.files._protected_state`,
    which v1's write tools refuse, reached through `jarvis.protected_state`,
    which v1's shell gate and v2's layer 1 both ask. It used to be a second copy
    kept equal by a suite; a second copy is how two rules drift. Still derived
    from `config.ALLOWLIST_PATH`'s directory, so a test that repoints the
    allowlist repoints the set with it.
    """
    return protected_state.protected_paths()


def _resolve(path: str | Path) -> Path:
    try:
        return Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return Path(os.path.abspath(os.path.expanduser(str(path))))


def file_targets(tool: str, args: dict) -> list[str]:
    """Every path a file tool would write. Several spellings are read because
    the key differs per provider and a target we fail to find is a target
    layer 1 fails to protect."""
    found: list[str] = []
    for key in _PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            found.append(value)
    for key in _PATHS_KEYS:
        value = args.get(key)
        if isinstance(value, (list, tuple)):
            found.extend(v for v in value if isinstance(v, str) and v.strip())
    for edit in args.get("edits", []) if isinstance(args.get("edits"), list) else []:
        if isinstance(edit, dict):
            for key in _PATH_KEYS:
                value = edit.get(key)
                if isinstance(value, str) and value.strip():
                    found.append(value)
    return found


def _placed(target: str, cwd: str | None) -> Path:
    """`target` as a path, a relative one placed in the folder the tool runs
    in (the brief's), never the daemon's own cwd."""
    path = Path(target).expanduser()
    if not path.is_absolute() and cwd:
        path = Path(cwd).expanduser() / path
    return path


def denied_file(target: str, cwd: str | None = None, *, folder: bool = False) -> str | None:
    """The reason this path may not be written, or None. `folder`: the path
    is a folder (a grant's), so its own name is not judged as a file's."""
    path = _placed(target, cwd)
    if not folder and v1_secrets.is_env_credential(path):
        # The owner's `.env*` rule (ENV-1): `*.example` only, symlinks
        # followed both ways (`secrets.is_env_credential`).
        return (f"{path.name} is (or leads to) a credential env file; Jarvis never writes "
                "one. The owner edits it in the HUD; only names ending in .example "
                "(.env.example) are Jarvis's to write.")
    if not folder and v1_secrets.is_protected(path):
        return (f"{path.name} holds live credentials; Jarvis never writes it. "
                "Ask the owner to change it by hand.")
    resolved = _resolve(path)
    if resolved in protected_paths():
        if path.name in ("voiceprint.json", "guest.json") and resolved.parent == _resolve(config.SPEAKER_DIR):
            return (f"{path.name} holds the owner's speaker verification: whose voice is the "
                    "owner's, and whether Guest voice is on. Changing it could switch the check "
                    "that keeps other voices from the cloud off, so it is the owner's to change "
                    "in Settings > Voice.")
        if path.name == "stt.json" or resolved == _resolve(config.STT_SETTINGS_PATH):
            return (f"{path.name} holds the owner's speech-to-text switches, including whether "
                    "audio may be uploaded. Changing it could send the owner's voice off this "
                    "machine, which cannot be undone, so it is the owner's to change in "
                    "Settings > Voice.")
        return (f"{path.name} is Jarvis's own permission state. Changing it "
                "would widen what runs without anyone being asked, so it is "
                "the owner's to edit by hand.")
    try:
        if v1_files._hud_build(resolved):
            return (f"{path.name} is in the built HUD the daemon serves, whose script "
                    "can approve what the owner is asked (BR-0). Change the HUD's "
                    "source instead; the owner installs it with `jarvis hud install`.")
        if v1_files._self_protected(path):
            return (f"{path.name} is part of Jarvis's safety layer and can only "
                    "be changed by the owner by hand. Say what you wanted "
                    "changed and why instead.")
    except Exception:
        LOG.warning("SELF_PROTECTED check failed for %s", target, exc_info=True)
    return None


def denied_read(tool: str, args: dict, cwd: str | None = None) -> str | None:
    """Why a read tool's call may not run, or None (ENV-1): a path it reads
    that is, or leads to, a credential file — a bundle or a credential env
    file, by `secrets.is_credential` — or, for Grep (which reads what it
    selects), a file-name glob judged as the shell's `rg --hidden -g` value
    is (`secrets.pattern_reaches`, plus the raw-line check on the glob and
    the path: `**/*test*` and `*.custom` are allowed, `**/.env*`, `*.env*`
    and `*.local` are not). Claude's Glob returns names only, so its pattern
    is not judged (its `path` is). What a search over a whole folder then
    reads is beyond what a pre-tool
    hook sees: Jarvis never sees Claude's tool results, so there is no scrub
    on that side (the plan's stated limit) — the class of `grep -r foo .`."""
    for key in ("file_path", "notebook_path", "path"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            path = _placed(value, cwd)
            if v1_secrets.is_credential(path):
                return v1_secrets.refusal(path.name)
            if v1_secrets.env_glob_reaches(path.name):
                return v1_secrets.refusal(path.name)
            if tool in _RAW_CHECKED:
                named = v1_secrets.raw_names_in(value)
                if named:
                    return v1_secrets.refusal(named)
    for key in _GLOB_KEYS.get(tool, ()):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            # Judged as `rg --hidden -g VALUE` is in the shell (Claude's Grep
            # runs ripgrep over hidden files and reads what it selects):
            # the raw-line check on the whole value, then every component of
            # a pattern like `**/.env.*` or `{a,b}/.env` as a file-name
            # pattern a program expands — main's floor, the dot-led exact
            # test, or naming `env` and able to match (`*.env*`). Brace lists
            # are expanded first (bash's reading,
            # `protected_state._brace_expand`), and one too large to expand
            # is refused.
            named = v1_secrets.raw_names_in(value)
            if named:
                return v1_secrets.refusal(named)
            variants = protected_state._brace_expand(value)
            if variants is None:
                return v1_secrets.refusal(value[:80])
            for variant in variants:
                for part in variant.split("/"):
                    if part and (v1_secrets.is_protected(part)
                                 or v1_secrets.pattern_reaches(part, True)):
                        return v1_secrets.refusal(part)
    return None


# A grant over a folder is judged by what is in it (ENV-1): a credential env
# file anywhere under a grant is a file that grant opens. The walk is
# bounded, and past the bound the grant is refused — a guard that cannot
# tell refuses.
GRANT_SCAN_ENTRIES = 50_000
GRANT_SCAN_SECONDS = 2.0


def _is_folder(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _env_file_within(path: Path, raw: Path) -> tuple[str | None, str | None]:
    """(why a grant on `path` would reach a credential env file, why it could
    not be checked) — each None when it does not apply. `path` is resolved,
    `raw` as given. A file is judged by name and through its symlinks; a
    folder by what it holds at any depth (symlinked folders inside are not
    followed; a symlinked file is judged where it leads), within
    `GRANT_SCAN_ENTRIES` entries and `GRANT_SCAN_SECONDS`. A folder named
    `.env` (a virtualenv) is a folder, judged by what it holds."""
    if not _is_folder(path):
        if v1_secrets.is_env_credential(raw) or v1_secrets.is_env_credential(path):
            return f"{raw} is a credential env file", None
        return None, None
    import time as _time
    deadline = _time.monotonic() + GRANT_SCAN_SECONDS
    seen = 0
    stack = [path]
    while stack:
        folder = stack.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    seen += 1
                    if seen > GRANT_SCAN_ENTRIES or _time.monotonic() > deadline:
                        return None, (f"could not check {path} for credential env files "
                                      f"(over {GRANT_SCAN_ENTRIES} entries or "
                                      f"{GRANT_SCAN_SECONDS:g}s)")
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                        is_link = entry.is_symlink()
                    except OSError:
                        is_dir, is_link = False, False
                    if is_dir:
                        stack.append(Path(entry.path))
                    elif v1_secrets.is_env_credential_name(entry.name) or (
                            is_link and v1_secrets.is_env_credential(entry.path)):
                        return f"{path} holds {entry.path}, a credential env file", None
        except OSError:
            continue  # a folder we cannot list: nothing in it is reachable by us either
    return None, None


def denied_grant(target: str, access: str = "write",
                 unchecked: list[str] | None = None) -> str | None:
    """Why a sandbox grant on `target` may not be approved by anyone, or None.

    A grant opens a whole subtree, so the test is ancestry, not equality:
    "write /" or "write ~/.config" covers the allowlist as surely as naming
    it. Any grant touching a credential directory (on it, inside it, or
    above it) is refused, reads included — a read is how a key leaves. So is
    any grant that is or holds a credential env file (ENV-1; a bounded walk).
    A write grant that is or contains Jarvis's own permission state
    (`protected_paths()`, `files._protected_state()`), a SELF_PROTECTED file,
    or a credential bundle is refused too. Approving a grant is not consent
    to what it lets through.

    A folder too large to walk in time cannot be judged for env files. With
    `unchecked` (the permit, whose widening always reaches a human) the
    sentence is appended there and the grant goes to the owner, as on main;
    without it the grant is refused (ENV-1 review, NIT 2).
    """
    if not isinstance(target, str) or not target.strip():
        target = "/"
    path = _resolve(target)
    for directory in _credential_dirs():
        if path == directory or directory in path.parents or path in directory.parents:
            return (f"a grant on {path} would reach {directory}, which holds "
                    "credentials; no approval can open it")
    # The owner's `.env*` rule (ENV-1): a grant that is or holds a credential
    # env file opens it — to read or to write, and Jarvis may do neither.
    env, could_not = _env_file_within(path, Path(target).expanduser())
    if env:
        return (f"a grant on {path} would reach a credential env file ({env}); "
                "Jarvis never reads or writes one, so no approval can open it")
    if could_not:
        if unchecked is None:
            return f"a grant on {path}: {could_not}; refused"
        unchecked.append(could_not)
    if access == "read":
        return None
    try:
        roots = v1_files._hud_roots()
    except Exception:
        return f"a grant on {path} cannot be checked against the built HUD; refused"
    for dist in sorted(roots):
        if path == dist or dist in path.parents or path in dist.parents:
            return (f"a grant on {path} would reach {dist}, the built HUD the daemon serves, "
                    "whose script can approve what the owner is asked; no approval can open it")
    try:
        protected = set(protected_paths()) | set(v1_files._protected_state())
        protected |= {_resolve(Path(config.REPO_ROOT) / rel) for rel in v1_files.SELF_PROTECTED}
    except Exception:       # cannot tell what it covers: refuse
        LOG.warning("protected-state lookup failed for a grant on %s", target, exc_info=True)
        return f"a grant on {path} cannot be checked against Jarvis's own state; refused"
    for item in protected:
        if path == item or path in item.parents:
            return (f"a grant on {path} would cover {item}, part of Jarvis's own "
                    "permission state; only the owner edits it, by hand")
    # A folder is judged by what it holds (above), never by its own name: a
    # virtualenv named `.env` is not a credential env file (ENV-1 review,
    # NIT 1; ED-7's rule). Its git internals and the safety layer still count.
    return denied_file(str(path), folder=_is_folder(path))


def _command_of(args: dict) -> str:
    """The command line in a tool's arguments, whatever the provider calls it
    (`approvals.command_text`: one reading for the permit and the allowlist)."""
    return command_text(args)


def denied_command(command: str, cwd: str | None = None) -> str | None:
    """The reason this command may not run at all, or None.

    Two sources, both v1's and both unchanged: the never-approvable verdicts
    (sudo, dd/mkfs, `rm -rf` at a root, fork bombs, raw block writes) and the
    secrets layer, which refuses a command that *names* a credential file
    because approving a command is not consent to what it prints.
    """
    if not command.strip():
        return None
    verdict = rules.decide(command)
    if verdict.decision == rules.DENY:
        return verdict.reason or "refused by Jarvis's never-approvable rules"
    name = v1_secrets.protected_in_command(command)
    if name:
        return v1_secrets.refusal(name)
    touch = protected_state.command_touch(command, cwd)
    if touch is not None and touch.certain:
        if touch.env:
            # A write onto a credential env file, judged in the brief's folder
            # so a symlink to one counts (ENV-1).
            return protected_state.refusal(touch)
        return (f"{touch.name} is Jarvis's own permission state; a command that "
                "writes it would widen what runs without anyone being asked.")
    return None


def _command_writes_protected_state(command: str, cwd: str | None = None) -> str | None:
    """The protected file this command provably writes, or None.

    The detector is v1's (`jarvis.protected_state.command_touch`): one copy for
    both, after v1 was found to have none at all. v2 refuses every *certain*
    write at layer 1, the allowlist and its siblings alike, as it always has.
    An uncertain one — a copy into the gate's folder, an unknown program handed
    the path — is not refused here; `v1_permissions.static_verdict` turns it
    into an ASK, so layer 4 never auto-approves it.

    It is not a boundary and the code says so — a shell has more spellings than
    any matcher has patterns, which is the same caveat `rules.py`'s DENY
    section already carries. The real protection is that the file's *tool*
    path is refused above and that nothing auto-approves an unrecognised
    command.
    """
    touch = protected_state.command_touch(command, cwd)
    if touch is not None and touch.certain:
        return touch.name
    return None


# --------------------------------------------------------------------------
# layer 2 — always-ask


def _load_additions() -> list:
    """The additions file. Additions only: §6 lets a project add to this layer
    and not remove from it, and the same has to hold for a file, or a layer
    the owner cannot see could be disarmed by emptying it. An unreadable file
    costs the additions and never the shipped defaults."""
    path = Path(config.V2_ALWAYS_ASK).expanduser()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        LOG.warning("Ignoring unreadable always-ask additions at %s: %s", path, exc)
        return []
    if not isinstance(raw, list):
        LOG.warning("Ignoring always-ask additions at %s: not a JSON list", path)
        return []
    return [entry for entry in raw if isinstance(entry, (str, dict))]


def always_ask_for(brief: Brief | None = None, project: Project | None = None) -> list:
    """Every rule in force: shipped defaults + file + project + brief.

    This is the R8 seam. Codex cannot enforce layer 2 from its reviewer's
    escalations alone, so whatever answers R8 — a Codex pre-execution hook, or
    a `jarvis-mcp` tool that asks with the native equivalent removed from the
    toolset — reads its list from here rather than growing a second copy.
    """
    entries: list = list(config.V2_ALWAYS_ASK_DEFAULTS)
    entries += _load_additions()
    if project is not None:
        entries += [e for e in project.always_ask if isinstance(e, (str, dict))]
    if brief is not None:
        entries += [e for e in brief.always_ask if isinstance(e, (str, dict))]
    return entries


def _tokens_of(segment: str) -> list[str]:
    """The command a segment runs, from its program on: past the shell's
    reserved words, a `case` pattern, a function header, parens and the
    assignments (`rules.command_start`), and its wrappers (`rules.unwrap`).
    `if true; then git push --force origin main; fi` split into a segment
    whose first word was `then`, so no kind rule saw the push (third delta
    review of PR #75's audit; older than the PR)."""
    return rules.unwrap(rules.command_start(rules._tokens(segment)))


def _any_token(patterns: Iterable[str], tokens: list[str]) -> bool:
    return any(fnmatch(token, pattern) for pattern in patterns for token in tokens)


def _phrase_in(phrase: tuple[str, ...], tokens: list[str]) -> bool:
    """A contiguous run of tokens, fnmatch per token.

    `("vercel", "domains", "buy")` matches `vercel domains buy example.com`
    and `npx prisma migrate deploy` matches `("prisma","migrate","deploy")`,
    which is why this is a subsequence test and not a stem test.
    """
    width = len(phrase)
    if not width or width > len(tokens):
        return False
    return any(all(fnmatch(tokens[i + j], phrase[j]) for j in range(width))
               for i in range(len(tokens) - width + 1))


def _phrase_ordered(phrase: tuple[str, ...], tokens: list[str]) -> bool:
    """The phrase's tokens in order, with anything allowed between them.

    Contiguity is the wrong test for a CLI subcommand, and `flyway
    -url=jdbc:postgresql://prod/app migrate` is why: the flags sit *between*
    the program and its verb, so a contiguous match saw no migration at all
    and the whole prod-migration rule silently did not fire. Order still
    matters — `deploy` before `migrate` is a different command — and every
    part must be an exact token, so `--deploy-preview` is not a `deploy`.
    """
    index = 0
    for part in phrase:
        while index < len(tokens) and not fnmatch(tokens[index], part):
            index += 1
        if index >= len(tokens):
            return False
        index += 1
    return True


def _refspec_targets(tokens: list[str]) -> list[str]:
    """The branch names a `git push` names, in any spelling."""
    if "push" not in tokens:
        return []
    after = tokens[tokens.index("push") + 1:]
    targets = []
    for token in after:
        if token.startswith("-"):
            continue
        ref = token.split(":")[-1].lstrip("+")
        for prefix in ("refs/heads/", "heads/"):
            if ref.startswith(prefix):
                ref = ref[len(prefix):]
        if ref:
            targets.append(ref)
    return targets


def _git_push(tokens: list[str]) -> bool:
    return bool(tokens) and tokens[0].rsplit("/", 1)[-1] == "git" and "push" in tokens


def _kind_matches(kind: str, segment: str, tokens: list[str], stems: list[str]) -> bool:
    if kind == "git_push_protected":
        # A bare `git push` with no refspec is deliberately *not* matched: the
        # branch it would push is not in the command, and in a task worktree it
        # is `jarvis/<slug>`, never main. Asking about every push would teach
        # approve-without-reading, which is the failure this layer exists to
        # prevent. Recorded as a known gap in WP5-notes.md. Closed in front
        # of this list since 2026-10-15: `merge_gate` reads the current branch
        # where the push runs and asks only when it is a protected one.
        return _git_push(tokens) and any(
            fnmatch(ref.lower(), pattern) for ref in _refspec_targets(tokens)
            for pattern in _PROTECTED_BRANCHES)
    if kind == "git_push_force":
        return _git_push(tokens) and any(
            token == "-f" or token.startswith("--force") for token in tokens)
    if kind == "migration_prod":
        return (any(_phrase_ordered(p, tokens) for p in _MIGRATE_PROD_PHRASES)
                and bool(_PROD_MARKER.search(segment)))
    if kind == "migration_always":
        return any(_phrase_ordered(p, tokens) for p in _MIGRATE_ALWAYS_PHRASES)
    if kind == "credential_command":
        return (any(_phrase_ordered(p, tokens) for p in _CREDENTIAL_PHRASES)
                or any(stem in _CREDENTIAL_STEMS for stem in stems))
    if kind == "credential_path":
        return any(_under_credential_dir(token) for token in tokens + _path_words(segment))
    LOG.warning("Ignoring always-ask rule with unknown kind %r", kind)
    return False


def _path_words(segment: str) -> list[str]:
    """Words of the segment a path rule reads besides the unwrapped command's:
    every word as written (a wrapper's option values — `env -C DIR`, `time -o
    FILE` — are not the command's), the value of each `--name=VALUE`, and the
    files the wrappers at its front write themselves (`time -oFILE`,
    `script FILE`; delta review of PR #75, H-B)."""
    raw = rules._tokens(segment)
    words = raw + [t.split("=", 1)[1] for t in raw if t.startswith("--") and "=" in t]
    written: list[str] = []
    try:
        rules.unwrap_sure(raw, None, written)
    except Exception:     # noqa: BLE001 — a parse failure adds nothing here
        pass
    return words + written


def _credential_dirs() -> list[Path]:
    return [_resolve(d) for d in config.credential_dirs()]


def _under_credential_dir(target: str) -> bool:
    candidate = target.strip("\"'><")
    if not candidate or candidate.startswith("-"):
        return False
    if "/" not in candidate and not candidate.startswith("~"):
        return False
    resolved = _resolve(candidate)
    for directory in _credential_dirs():
        if resolved == directory or directory in resolved.parents:
            return True
    return False


def _rule_matches(rule, tool: str, segments: list[tuple[str, list[str], list[str]]],
                  targets: list[str]) -> tuple[bool, str, str]:
    """(matched, rule id, reason) for one rule against one tool call."""
    if isinstance(rule, str):
        phrase = tuple(part for part in rule.split() if part)
        if not phrase:
            return False, "", ""
        if len(phrase) == 1 and phrase[0] == tool:
            return True, rule, "named by an always-ask addition"
        for _segment, tokens, _stems in segments:
            if _phrase_in(phrase, tokens):
                return True, rule, "named by an always-ask addition"
        return False, "", ""

    if not isinstance(rule, dict):
        return False, "", ""
    rule_id = str(rule.get("id") or rule.get("kind") or "addition")
    reason = str(rule.get("reason", ""))
    tools = rule.get("tools") or []
    if isinstance(tools, (list, tuple)) and tool in tools:
        return True, rule_id, reason
    has_command_criteria = any(k in rule for k in ("kind", "stem", "all", "any", "none"))
    if not has_command_criteria:
        return False, "", ""

    kind = rule.get("kind")
    stem = rule.get("stem")
    stems_wanted = [stem] if isinstance(stem, str) else list(stem or [])
    require_all = list(rule.get("all") or [])
    require_any = list(rule.get("any") or [])
    forbid = list(rule.get("none") or [])

    for segment, tokens, stems in segments:
        if kind is not None:
            if _kind_matches(str(kind), segment, tokens, stems):
                return True, rule_id, reason
            continue
        if stems_wanted and not any(s in stems_wanted for s in stems):
            continue
        if require_all and not all(_any_token([p], tokens) for p in require_all):
            continue
        if require_any and not _any_token(require_any, tokens):
            continue
        if forbid and _any_token(forbid, tokens):
            continue
        return True, rule_id, reason

    # A path rule also applies to a file tool's targets, which have no segment.
    if kind == "credential_path" and any(_under_credential_dir(t) for t in targets):
        return True, rule_id, reason
    return False, "", ""


def always_ask_match(tool: str, args: dict, brief: Brief | None = None,
                     project: Project | None = None) -> tuple[str, str] | None:
    """(rule id, reason) if this call is on the always-ask list, else None."""
    command = _command_of(args) if is_command_tool(tool, args) else ""
    segments: list[tuple[str, list[str], list[str]]] = []
    for segment in rules.segments(command) if command.strip() else []:
        tokens = _tokens_of(segment)
        stems = [s for s, _ in rules.command_targets(segment)]
        if tokens and tokens[0].rsplit("/", 1)[-1].lower() not in stems:
            # The program past a reserved word (`then vercel --prod`), which
            # the allowlist's stems do not peel.
            stems.append(tokens[0].rsplit("/", 1)[-1].lower())
        segments.append((segment, tokens, stems))
        # What a wrapper or runner in front starts is judged too, from rules'
        # one table: `npx vercel --prod` is a production deploy, and `uv run
        # git push --force origin main` a force push (2026-10-12).
        # Past the line's work budget or a nesting cap, what it starts cannot
        # be told: asked, whatever the rules say (review of PR #75).
        try:
            started = rules.started_commands(segment)
        except rules.TooComplex:
            return "too-complex", rules.RUNNERS_TOO_COMPLEX
        for inner in started:
            segments.append((segment, inner, [inner[0].rsplit("/", 1)[-1].lower()]))
    targets = file_targets(tool, args) if tool in FILE_TOOLS else []
    for rule in always_ask_for(brief, project):
        matched, rule_id, reason = _rule_matches(rule, tool, segments, targets)
        if matched:
            return rule_id, reason
    return None


# --------------------------------------------------------------------------
# the callback


# A native sub-agent's name on an approval card (SV-1): the provider cleans
# it, and so does this, since it lands on an authorization surface. Only
# these characters survive (anything else "_"), so it can neither draw the
# card's own " · " parts nor close its ‹ ›; one space at a time; capped.
SUBAGENT_LABEL_CAP = 40
_SUBAGENT_UNSAFE = re.compile(r"[^A-Za-z0-9 _./-]+")
_SUBAGENT_PROVIDERS = {"claude": "Claude", "codex": "Codex"}


def subagent_origin(ctx: PermitContext, subagent: str) -> str:
    """Who asks, when a native sub-agent does: "Claude sub-agent ‹name› (not
    the orchestrator)", then the task's own attribution if there is one."""
    name = " ".join(_SUBAGENT_UNSAFE.sub("_", " ".join(str(subagent).split())).split()).strip(" _")
    if len(name) > SUBAGENT_LABEL_CAP:
        name = name[:SUBAGENT_LABEL_CAP - 1].rstrip() + "…"
    who = (f"{_SUBAGENT_PROVIDERS.get(ctx.provider, 'A')} sub-agent ‹{name or 'unnamed'}› "
           "(not the orchestrator)")
    task = ctx.origin()
    return f"{who} in {task}" if task else who


class _OwnerYes:
    """The owner's yes to one call, held briefly by its `tool_use_id` (review
    of #208: under Claude AUTO a layer-2 yes returns `{}`, the CLI's
    classifier still runs, and if it asks too the owner saw a second card).
    `recall` answers that re-ask only for the same tool_use_id, the same tool
    and identical arguments, once, within `OWNER_YES_TTL_S`; the classifier
    can still deny. Nothing else is ever answered from here."""

    def __init__(self, clock=None):
        import time as _time
        self._clock = clock or _time.monotonic
        self._lock = threading.Lock()
        self._held: dict[str, tuple[str, str, float]] = {}

    def remember(self, tool_use_id: str, tool: str, args: dict) -> None:
        with self._lock:
            now = self._clock()
            self._held = {k: v for k, v in self._held.items() if now - v[2] < OWNER_YES_TTL_S}
            while len(self._held) >= OWNER_YES_CAP:
                self._held.pop(next(iter(self._held)))
            self._held[tool_use_id] = (tool, _digest(args), now)

    def recall(self, tool_use_id: str, tool: str, args: dict) -> bool:
        with self._lock:
            held = self._held.pop(tool_use_id, None)
        if held is None:
            return False
        held_tool, digest, at = held
        return (held_tool == tool and digest == _digest(dict(args or {}))
                and 0 <= self._clock() - at < OWNER_YES_TTL_S)


def build_permit(ctx: PermitContext, asker, *, clock=None) -> PermissionCallback:
    """The §6 callback for one thread. `asker` is a `PendingApprovals` or a
    `DenyAll`; its `human_backed` flag is what layer 4 turns on."""

    def permit(tool: str, args: dict, brief: Brief | None = None, *,
               widening: str | None = None, grants: Iterable[tuple[str, str]] = (),
               also_commands: Iterable[str] = (), dangerous_tool: bool = False,
               subagent: str | None = None, subagent_sid: str | None = None,
               shell_cwd: str | None = None, tool_use_id: str | None = None) -> Decision:
        """`widening` is set only by a provider whose approval would widen
        its own sandbox if accepted (Codex: extra permissions, network, a
        grant root, terminal input). Such a call is asked of a human every
        time — never ALLOWed by layer 4 or 5, never Always — and a deny-all
        asker, no surface or a timeout declines it. It is a keyword the model
        cannot reach: it never rides in `args`.

        `grants` are the (access, path) pairs such an approval would open and
        `also_commands` any further command it would run (a running
        terminal's, beside the text typed into it). Layer 1 judges both: a
        grant that is or contains protected state or a credential directory,
        or a command that is never approvable, is refused unasked.

        `dangerous_tool` is set only by the daemon's jarvis-mcp bridge
        (`bridge.py`), for a Jarvis tool marked `dangerous` that a Claude or
        Codex chat called (decisions D-3: `gmail_send`). After layer 1 the
        owner is asked every time, whatever the profile, with no Always —
        never a standing yes, never the provider's reviewer — and strict
        denies. Like `widening`, the model cannot reach it.

        `subagent` is set only by a provider whose native sub-agent made the
        call (Claude, SV-1): its name, for the card's "requested by" line.
        It decides nothing — every layer below reads exactly what it reads
        without it, so a child is never answered more loosely (or more
        strictly) than its orchestrator would be.

        `subagent_sid` is the same child's sid (`cl:…`/`cx:…`, SV-3), set by
        Claude beside `subagent` and by Codex for its children's widenings
        (whose headline already names them). It rides on the owner's card
        only, so the HUD can link the card to the child's row; like the
        label it decides nothing, and a value that is not a sid is dropped.

        `shell_cwd` is set only by a provider that knows the folder its
        shell is in (Claude's hook input `cwd`): where `merge_gate` reads
        the current branch for a push with no refspec. Never from `args`,
        which the model writes; Codex's item reports its own `cwd`.

        `tool_use_id` is set only by a provider whose CLI may ask about the
        same call again (Claude): a human's yes here is then held for
        `permit.recall`, so the re-ask is answered without a second card."""
        brief = brief if brief is not None else ctx.brief
        who = subagent_origin(ctx, subagent) if subagent is not None else None
        # Exactly a sid (`subagents.SID`), else nothing.
        sid = subagent_sid if _valid_sid(subagent_sid) else ""
        args = dict(args or {})
        also_commands = tuple(also_commands)   # read twice: layer 1 and the owner-only check
        profile = brief.profile if brief is not None else PermissionProfile.AUTO
        command = _command_of(args) if is_command_tool(tool, args) else ""
        # Where the command will run: the brief's folder, never the daemon's
        # own cwd, or a relative `models.json` names the wrong file.
        cwd = (brief.cwd or None) if brief is not None else None
        # A Codex chat's git tool (CG-0, `chat_git`): the exact command the
        # daemon will run, read from the repository below, never from `args`.
        chat: chat_git.Plan | None = None
        # Whether this call was put to the asker (`last_verdict`).
        asked = [False]
        _LAST_VERDICT.set(None)

        def owner_said(decision: Decision) -> Decision:
            """A human's answer to a card: a yes is held for the re-ask."""
            asked[0] = True
            if decision is Decision.ALLOW and isinstance(tool_use_id, str) and tool_use_id:
                memo.remember(tool_use_id, tool, args)
            return decision

        def finish(decision: Decision, layer: str, reason: str) -> Decision:
            _LAST_VERDICT.set(PermitVerdict(decision, layer, reason, asked[0]))
            log_decision(ApprovalRecord(
                tool=tool, args_digest=_digest(args), layer=layer,
                decision=decision.value, reason=reason,
                command=_safe_command(command or (chat.line if chat is not None else "")),
                thread_id=ctx.thread_id, task_id=ctx.task_id, provider=ctx.provider or None))
            if (decision is Decision.ALLOW and chat is not None and not widening
                    and (dangerous_tool if tool in chat_git.DANGEROUS else True)):
                # The tool runs only the plan this ALLOW was for (`chat_git.take`).
                chat_git.grant(chat)
            return decision

        # --- layer 1: never approvable, never asked of anyone --------------
        if unreadable_command(tool, args) == "deny":
            # A shell whose command key holds no text: nothing to judge, so
            # nothing runs (#211 review, F3).
            return finish(Decision.DENY, L_DENY,
                          f"{tool} carries no command text to judge; refused")
        if command.strip():
            refusal = denied_command(command, cwd)
            if refusal:
                return finish(Decision.DENY, L_DENY, refusal)
        if tool in FILE_TOOLS:
            for target in file_targets(tool, args):
                refusal = denied_file(target, cwd)
                if refusal:
                    return finish(Decision.DENY, L_DENY, refusal)
        if tool in READ_TOOLS:
            # A credential file read or searched for by name (ENV-1).
            refusal = denied_read(tool, args, cwd)
            if refusal:
                return finish(Decision.DENY, L_DENY, refusal)
        for extra in also_commands:
            # Judged where it runs, the brief's folder (ENV-1 review, NIT 5).
            refusal = (denied_command(extra, cwd)
                       if isinstance(extra, str) and extra.strip() else None)
            if refusal:
                return finish(Decision.DENY, L_DENY, refusal)
        unchecked: list[str] = []
        for access, target in grants:
            refusal = denied_grant(target, access, unchecked)
            if refusal:
                return finish(Decision.DENY, L_DENY, refusal)
        if unchecked:
            # A grant on a folder too large to walk for env files goes to the
            # owner, saying so — never refused unasked, as on main (ENV-1
            # review, NIT 2). Only a widening is sure to reach a human.
            if not widening:
                return finish(Decision.DENY, L_DENY, "; ".join(unchecked) + "; refused")
            widening = widening + " · " + "; ".join(unchecked)

        # --- a Codex chat's git tool: read the target, or refuse ------------
        # `chat_git.plan` reads the worktree's branch and origin from files,
        # checks the pin and refuses a detached HEAD or a protected branch.
        # Whatever it cannot read is refused unasked (unknown fails closed).
        if ctx.provider == "codex" and tool in chat_git.TOOLS:
            try:
                chat = chat_git.plan(tool, args, cwd or "")
            except chat_git.Refused as exc:
                return finish(Decision.DENY, L_DENY, f"{tool}: {exc}")
            except Exception:   # noqa: BLE001 — a reader that fails has not cleared it
                LOG.exception("chat_git plan failed; refusing")
                return finish(Decision.DENY, L_DENY, f"{tool}: the repository could not be read")

        # --- owner-only repository operations (2026-10-15) -----------------
        # Merging or closing a pull request, a push to the default branch, a
        # branch deletion, a force push: asked at layer 2 below in every
        # profile, never offered an Always, never the allowlist's or the
        # reviewer's; a widening or a dangerous tool carries its headline.
        # A Codex git tool is judged by the line the daemon will run, where
        # it runs (`chat_git.gate_line`).
        owner_only = _owner_only(tool, chat.line if chat is not None else command,
                                 also_commands, args, ctx,
                                 chat.where if chat is not None else
                                 shell_cwd if isinstance(shell_cwd, str) else None, cwd)

        # --- sandbox widening: a human, every time ------------------------
        # Owner Codex chats use the native user reviewer and mark every
        # command/file approval as widening, including bare callbacks. Tasks
        # retain auto_review. Neither case may reach AUTO's reviewer fallback:
        # accepting also accepts the grant riding on it, and the reply cannot
        # strip that. So this asks whatever the profile, and an
        # ALWAYS is never offered: a standing yes to a widening is a wider box.
        if widening:
            # CG-1 (owner, 2026-10-15): a Codex chat has no GitHub login, so
            # an escalated raw push/fetch/pull/ls-remote to a remote, or any
            # gh, could only fail. Declined before any card — a merge or a
            # close spelled this way included — and pointed at the tools.
            if (ctx.provider == "codex" and brief is not None and brief.role == Role.CHAT
                    and brief.task_id is None
                    and any(chat_git.raw_remote(line) for line in (command, *also_commands))):
                return finish(Decision.DENY, L_CHAT_GIT, chat_git.POINTER)
            if profile == PermissionProfile.STRICT:
                return finish(Decision.DENY, L_WIDENING,
                              f"strict profile refuses a sandbox widening: {widening}")
            match = always_ask_match(tool, args, brief, ctx.project)
            if owner_only is not None:
                match = (owner_only.label, owner_only.reason)
            reason = widening + (f" (also always-ask rule {match[0]})" if match else "")
            headline = (f"{owner_only.headline} · {widening}" if owner_only is not None
                        else widening)
            decision = owner_said(_ask(asker, ctx, tool, args, command, reason, L_WIDENING,
                                       allowlistable=False, headline=headline, origin=who,
                                       subagent=sid))
            return finish(decision, L_WIDENING, reason)

        # --- a dangerous Jarvis tool from a chat's jarvis-mcp: the owner ----
        # The bridge asks here for a `dangerous` registry tool (D-3). Nothing
        # below may answer for the owner: not the allowlist (layer 4), not
        # the provider's reviewer (layer 5 under AUTO), so this is asked
        # every time, and an Always is not offered — it would mint an entry
        # this path never reads.
        if dangerous_tool:
            if profile == PermissionProfile.STRICT:
                return finish(Decision.DENY, L_HUMAN,
                              "strict profile refuses a dangerous Jarvis tool")
            match = always_ask_match(tool, args, brief, ctx.project)
            if match is None and ctx.provider in ("claude", "codex"):
                match = always_ask_match(session_tools.CLAUDE_PREFIX + tool, args, brief, ctx.project)
            if owner_only is not None:
                match = (owner_only.label, owner_only.reason)
            reason = ("a dangerous Jarvis tool: the owner decides every call"
                      + (f" (also always-ask rule {match[0]})" if match else ""))
            headline = " · ".join(part for part in (
                owner_only.headline if owner_only is not None else "",
                chat.headline if chat is not None else "") if part)
            decision = owner_said(_ask(asker, ctx, tool, args,
                                       chat.line if chat is not None else command, reason, L_HUMAN,
                                       allowlistable=False, origin=who, headline=headline,
                                       subagent=sid))
            return finish(decision, L_HUMAN, reason)

        # A native hook's exact offered Jarvis call delegates to this
        # session's daemon bridge. Delegation is not execution approval:
        # dangerous entries are asked once by the bridge's branch above,
        # including named always-ask rules, and can never mint Always.
        delegated = (session_tools.claude_session_tool(brief, tool)
                     if ctx.provider == "claude" else
                     session_tools.codex_session_tool(brief, tool)
                     if ctx.provider == "codex" else None)
        if delegated is not None:
            if profile == PermissionProfile.STRICT:
                return finish(Decision.DENY, L_JARVIS_DELEGATE,
                              "strict profile refuses session tool delegation")
            from jarvis import tools as registry
            if registry.REGISTRY[delegated.name].dangerous:
                return finish(Decision.ALLOW, L_JARVIS_DELEGATE,
                              "delegate to the daemon; the owner still decides execution")

        # --- layer 2: the owner is asked whatever the reviewer thought -----
        if owner_only is not None:
            reason = f"always-ask rule {owner_only.label}: {owner_only.reason}"
            decision = owner_said(_ask(asker, ctx, tool, args,
                                       chat.line if chat is not None else command, reason, L_ALWAYS_ASK,
                                       allowlistable=False, headline=owner_only.headline, origin=who,
                                       subagent=sid))
            return finish(decision, L_ALWAYS_ASK, reason)
        match = always_ask_match(tool, args, brief, ctx.project)
        if match is None and delegated is not None:
            alias = (session_tools.CLAUDE_PREFIX + delegated.name
                     if ctx.provider == "codex" else delegated.name)
            match = always_ask_match(alias, args, brief, ctx.project)
        if match is None and unreadable_command(tool, args) == "ask":
            # An unknown tool's command key holds something that is not text:
            # it cannot be judged, so the owner is asked (#211 review, F3).
            match = ("command-unreadable", f"{tool}'s command is not text and cannot be judged")
        if match is None and command.strip():
            # A line that might write the gate's own state but is not provably
            # a write (layer 1 took those): a copy into the gate's folder, a
            # path behind an unset variable — or a line too complex to judge at
            # all (past the substitution bound). Under AUTO it would otherwise
            # go to the provider's classifier; it goes to the owner instead, in
            # every profile.
            touch = protected_state.command_touch(command, cwd)
            if touch is not None:
                match = ("env-file" if touch.env else "gate-state",
                         protected_state.ask_reason(touch))
        if match is None and command.strip():
            # A line that may run VEX's device tool (the robot's download)
            # and cannot be told — a program that is only an expansion, read
            # from input, or behind an option the table does not know — or
            # one too complex to judge: never the provider's reviewer's call
            # under AUTO (VX-1, `rules.vex_uncoverable`). A line that
            # certainly names or runs it was refused at layer 1.
            unsure = rules.vex_uncoverable(command)
            if unsure:
                match = ("vex-device", unsure)
        if match is None and command.strip():
            # A kill whose pattern is too costly to judge against the v2
            # daemon — too complex to judge, so asked, never refused (that
            # is layer 1's for a provable match) and never the reviewer's
            # call under AUTO (the delta review of PR #48).
            unsure = protected_state.kill_unsure(command)
            if unsure:
                match = ("daemon-kill", unsure)
        if match is None and command.strip():
            # What the line's wrappers and runners start is too complex to
            # judge within its work budget (review of PR #75): asked, never
            # the reviewer's call under AUTO and never the allowlist's.
            unsure = rules.runners_unsure(command)
            if unsure:
                match = ("too-complex", unsure)
        if match is not None:
            rule_id, why = match
            reason = f"always-ask rule {rule_id}" + (f": {why}" if why else "")
            if delegated is not None and delegated.owner_gated:
                # Rule matching sees the original arguments, but no page facts
                # or owner text leave through the generic approval surface.
                asked[0] = True
                decision = _ask_owner_delegation(asker, ctx, delegated.name, origin=who)
                return finish(decision, L_ALWAYS_ASK, "owner rule for Browser delegation")
            decision = owner_said(_ask(asker, ctx, tool, args, command, reason, L_ALWAYS_ASK,
                                       origin=who, subagent=sid))
            return finish(decision, L_ALWAYS_ASK, reason)

        # --- jarvis-tool: a read-only Jarvis session tool --------------------
        # After layer 2, so an always-ask rule naming the tool still asks.
        # Only an owner's chat brief is offered session tools (a task's
        # threads never get a jarvis server), so only a chat brief is ALLOWed
        # here; the daemon's bridge judges the call itself.
        if delegated is not None:
            if delegated.owner_gated:
                return finish(Decision.ALLOW, L_JARVIS_GATED,
                              "delegate only; the Browser safety gate decides execution")
            if delegated.read_only:
                return finish(Decision.ALLOW, L_JARVIS_TOOL,
                              "read-only Jarvis tool; the daemon checks the turn")
            return finish(Decision.ALLOW, L_JARVIS_DELEGATE,
                          "delegate to the daemon's registered session tool")

        # --- layer 4: the owner's standing yes, human-backed only ----------
        # Layer 3 (the CLI reviewer) is not evaluated here; see the module
        # docstring for what an ALLOW out of this callback does and does not
        # mean.
        # A command-bearing tool that is not a known shell (`mcp__x__exec`)
        # is judged at layers 1 and 2 but never answered here: no allowlist
        # entry and no rules ALLOW (#211 review, F1).
        if getattr(asker, "human_backed", False) and not is_unknown_command_tool(tool, args):
            if v1_permissions.allows(*v1_request(tool, args), cwd=cwd):
                return finish(Decision.ALLOW, L_JARVIS_ALLOW,
                              "covered by the owner's persistent allowlist")
            if (command.strip() and
                    v1_permissions.static_verdict(command, cwd).decision == rules.ALLOW):
                return finish(Decision.ALLOW, L_JARVIS_ALLOW,
                              "ordinary development command (rules ALLOW)")

        # --- layer 5: profile decides -------------------------------------
        if profile == PermissionProfile.STRICT:
            return finish(Decision.DENY, L_HUMAN,
                          "strict profile: nothing runs that the owner has not "
                          "already allowlisted")
        if profile == PermissionProfile.ASK:
            reason = "ask profile: every gated call reaches the owner"
            decision = owner_said(_ask(asker, ctx, tool, args, command, reason, L_HUMAN,
                                       origin=who, subagent=sid))
            return finish(decision, L_HUMAN, reason)
        return finish(Decision.ALLOW, L_REVIEWER,
                      "no opinion; the provider's own reviewer decides")

    memo = _OwnerYes(clock)

    def recall(tool_use_id: str, tool: str, args: dict) -> bool:
        """Whether the owner already said yes to exactly this call (same
        tool_use_id, tool and arguments, once, within the TTL). Logged."""
        if not memo.recall(tool_use_id, tool, args):
            return False
        command = _command_of(dict(args or {})) if is_command_tool(tool, args) else ""
        log_decision(ApprovalRecord(
            tool=tool, args_digest=_digest(dict(args or {})), layer=L_OWNER_YES, decision="allow",
            reason="the owner's yes to this exact call, asked again by the CLI",
            command=_safe_command(command), thread_id=ctx.thread_id, task_id=ctx.task_id,
            provider=ctx.provider or None))
        return True

    permit.recall = recall
    return permit


def _owner_only(tool: str, command: str, also_commands, args: dict, ctx: PermitContext,
                shell_cwd: str | None, cwd: str | None) -> merge_gate.Hit | None:
    """`merge_gate.judge` over the command and any command riding with it,
    run where it runs: Claude's shell folder, Codex's item `cwd`, else the
    brief's. A reader that fails asks, saying so."""
    # Whatever command the permit read (`command` is empty for a tool it
    # does not read as one; #211 widens that to any command-bearing tool).
    where = shell_cwd
    if where is None and ctx.provider == "codex":
        reported = args.get("cwd")
        where = reported if isinstance(reported, str) and reported.startswith("/") else None
    where = where or cwd
    lines = [command] + [c for c in also_commands if isinstance(c, str)]
    for line in lines:
        if not line.strip():
            continue
        try:
            hit = merge_gate.judge(line, where)
        except Exception:       # noqa: BLE001 — a reader that fails has not cleared it
            LOG.exception("merge_gate failed; asking")
            hit = merge_gate.Hit(merge_gate.UNSURE, merge_gate.HEADLINES[merge_gate.UNSURE],
                                 merge_gate.REASON + ": the check failed")
        if hit is not None:
            return hit
    return None


def _ask_owner_delegation(asker, ctx: PermitContext, tool: str, *,
                          origin: str | None = None) -> Decision:
    """One metadata-only HUD consent, never a Browser action approval."""
    if tool != "hud_browser_act":
        return Decision.DENY
    request = ApprovalRequest(
        tool=tool, args={}, command=None, layer=L_ALWAYS_ASK, origin=origin or "",
        reason="Your named rule asks before Browser delegation. Browser safety checks still decide each action.",
        thread_id=ctx.thread_id, task_id=ctx.task_id, provider=ctx.provider or None,
        allowlistable=False, headline="Allow Browser safety checks for this call?",
        hud_only=True, browser={"kind": "delegation", "tool": tool}, max_wait_s=120)
    try:
        from jarvis import runtime
        native_cancelled = ctx.delegation_cancelled() if ctx.delegation_cancelled else lambda: False
        stop = runtime.should_stop()
        return Decision(asker.ask(request, cancelled=lambda: native_cancelled() or stop()))
    except Exception:
        # No exception text: a failing approval adapter may contain arguments.
        return Decision.DENY


def _ask(asker, ctx: PermitContext, tool: str, args: dict, command: str,
         reason: str, layer: str, *, allowlistable: bool = True,
         headline: str = "", origin: str | None = None, subagent: str = "") -> Decision:
    if is_unknown_command_tool(tool, args):
        allowlistable = False       # no stem and no whole-tool entry (#211 review, F1)
    request = ApprovalRequest(
        tool=tool, args=args, command=command or None, reason=reason, layer=layer,
        thread_id=ctx.thread_id, task_id=ctx.task_id,
        provider=ctx.provider or None, origin=origin if origin is not None else ctx.origin(),
        allowlistable=allowlistable, headline=clean_line(headline, 420) if headline else "",
        subagent=subagent, relayed_from=ctx.relayed())
    try:
        return Decision(asker.ask(request))
    except Exception:       # an asker that raises has not approved anything
        LOG.exception("Approval channel failed for %s; denying", tool)
        return Decision.DENY


def _safe_command(command: str) -> str | None:
    """The command as the log keeps it: scrubbed of credential values and
    capped. Layer 1 already refuses a command that names a credential file;
    this is the second half of the same rule for one that merely carries a
    value — a known one, a typed one (`MY_TOKEN=…`, `Authorization: …`,
    `curl -u user:pass`: `redact_command`) or a known shape. The log only:
    what was judged and asked about is the command itself."""
    if not command:
        return None
    try:
        scrubbed = v1_secrets.redact_command(command)
    except Exception:
        scrubbed = "[unscrubbable command withheld]"
    return scrubbed[:2000]


def deny_all(reason: str = "nobody is watching this thread") -> DenyAll:
    return DenyAll(reason)


__all__ = [
    "ApprovalRecord", "PermitContext", "PermitVerdict", "always_ask_for", "always_ask_match",
    "build_permit", "denied_command", "denied_file", "denied_grant", "denied_read", "deny_all",
    "file_targets", "last_verdict",
    "log_decision", "protected_paths", "COMMAND_TOOLS", "FILE_TOOLS", "READ_TOOLS",
]
