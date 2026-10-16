"""Private native-tool config; preparation adapted from the trading firm."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import tempfile

from jarvis import config
from jarvis.v2.model import PermissionProfile, Role
from jarvis.v2.provider import Brief, BriefRefused

_GITFILE_CAP = 4096


def _plain_path(path: Path) -> Path:
    """Absolute path with no symlink component, even before resolving '..'."""
    path = path.expanduser()
    if not path.is_absolute():
        raise ValueError("expected an absolute path")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("symlink component")
    return path.resolve()


def _small_text(path: Path) -> str:
    """One bounded UTF-8 line, from a regular, non-symlink file only."""
    path = _plain_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > _GITFILE_CAP:
            raise ValueError("not a small regular git pointer")
        raw = stream.read(_GITFILE_CAP + 1)
    if len(raw) > _GITFILE_CAP:
        raise ValueError("oversized git pointer")
    text = raw.decode("utf-8").strip()
    if not text or len(text.splitlines()) != 1 or any(ord(c) < 32 for c in text):
        raise ValueError("malformed git pointer")
    return text


def _overlaps(path: Path, other: Path) -> bool:
    return path == other or path in other.parents or other in path.parents


def _safe_root(path: Path) -> bool:
    """No writable subtree may contain or sit inside credentials/gate state.

    Reuse the gate's definitions, not denied_grant: that deliberately
    forbids .git itself. This chat-only grant is the owner's explicit
    exception for repository git metadata, never for the rest of the gate.
    """
    from jarvis.tools import files
    from jarvis.v2.permissions import _env_file_within, protected_paths

    barred = [Path(p).expanduser().resolve() for p in config.credential_dirs()]
    barred += [Path(config.V2_DATA_DIR).resolve(), owner_home(),
               Path.home() / ".claude", Path.home() / ".codex",
               Path(config.SESSIONS_DIR).resolve(), Path(config.SPILL_DIR).resolve()]
    barred += list(protected_paths()) + list(files._hud_roots())
    barred += [(Path(config.REPO_ROOT) / rel).resolve() for rel in files.SELF_PROTECTED]
    if any(_overlaps(path, item) for item in barred):
        return False
    # Names only, never env contents. A large/uncertain scan fails closed.
    env, unchecked = _env_file_within(path, path)
    return not env and not unchecked


def _head(path: Path) -> bool:
    text = _small_text(path / "HEAD")
    return bool(re.fullmatch(r"(?:ref: refs/[^\s]+|[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", text))


def _common_dir(path: Path) -> bool:
    return (_head(path) and all(_plain_path(path / name).is_dir()
                               for name in ("objects", "refs")))


def git_dirs(cwd: str) -> list[Path]:
    """Validated git metadata for a repository or linked worktree.

    Unsafe/malformed metadata grants nothing. No git command is invoked:
    discovering roots must not run repository hooks or configuration.
    """
    try:
        here = _plain_path(Path(cwd))
        for folder in (here, *here.parents):
            dot = folder / ".git"
            if not dot.exists() and not dot.is_symlink():
                continue
            dot = _plain_path(dot)
            if dot.is_dir():
                found = [dot] if _safe_root(dot) and _common_dir(dot) else []
            else:
                text = _small_text(dot)
                if not text.startswith("gitdir: "):
                    return []
                target = Path(text[len("gitdir: "):])
                gitdir = _plain_path(target if target.is_absolute() else folder / target)
                if not _safe_root(gitdir):
                    return []
                common_file = gitdir / "commondir"
                if common_file.exists() or common_file.is_symlink():
                    shared = Path(_small_text(common_file))
                    common = _plain_path(shared if shared.is_absolute() else gitdir / shared)
                    if not _safe_root(common):
                        return []
                    # Linked metadata must belong to this common directory and
                    # point back to this checkout, not merely look like a repo.
                    backlink = Path(_small_text(gitdir / "gitdir"))
                    backlink = _plain_path(backlink if backlink.is_absolute() else gitdir / backlink)
                    if (gitdir.parent != common / "worktrees" or backlink != dot
                            or not _head(gitdir) or not _common_dir(common)):
                        return []
                    found = [gitdir, common]
                else:
                    found = [gitdir] if _common_dir(gitdir) else []
            return found if all(_safe_root(path) for path in found) else []
    except (OSError, RuntimeError, ValueError):
        return []
    return []


def chat_worktrees_dir() -> Path:
    """Validate a dedicated root before creation or any permission grant."""
    try:
        path = _plain_path(Path(config.CHAT_WORKTREES_DIR))
        if not _safe_root(path) or (path.exists() and not path.is_dir()):
            raise ValueError("unsafe directory")
        if path.exists() and path.stat().st_uid != os.getuid():
            raise ValueError("not owned by the current user")
        return path
    except (OSError, RuntimeError, ValueError) as exc:
        raise BriefRefused("Unsafe Codex chat worktrees directory") from exc


def git_roots(brief: Brief) -> list[str]:
    """Chats alone may write verified git metadata and their worktree root."""
    if brief.role != Role.CHAT or brief.task_id is not None:
        return []
    root = chat_worktrees_dir()
    git = git_dirs(brief.cwd)
    # A configured worktree root must not swallow the project or its git
    # directory; in particular it must never chmod the shared checkout.
    cwd = _plain_path(Path(brief.cwd))
    if root == cwd or cwd in root.parents or any(root == p or p in root.parents for p in git):
        raise BriefRefused("Codex chat worktrees must be separate from the project")
    return [str(p) for p in git] + [str(root)]


def git_note(brief: Brief) -> str:
    """Truthful guidance: local git in the sandbox, the network through
    Jarvis's git tools (owner decision CG-0), never an escalated push or gh."""
    roots = git_roots(brief)
    if not roots:
        return ""
    where = roots[-1]
    metadata = ("Validated repository git metadata is writable for commits, branches and worktrees. "
                if len(roots) > 1 else "No writable repository git metadata was found. ")
    return ("Git in this session: " + metadata
            + f"Make new git worktrees under {where}/<unique-name>. "
            "The project checkout is shared; use a separate worktree for real edits, and "
            "commit before long pauses. "
            "The network is off inside the sandbox and this session has no GitHub login. "
            "To push, open, merge or close a pull request, call the Jarvis tools "
            "chat_git_push / chat_pr_create / chat_pr_merge / chat_pr_close; each asks the "
            "owner. For remote updates call chat_git_fetch, then `git fetch <mirror>`. "
            "Do not request escalation for git push or gh. " + NETWORK_NOTE)


# Model guidance for the per-command network route (plan §5.2, CG-2).
NETWORK_NOTE = ("For a command that needs the internet (npm install, pip, curl), set "
                "`sandbox_permissions` to `with_additional_permissions` with "
                "`additional_permissions: {network: {enabled: true}}`; the owner approves each "
                "one, and only that command gets the network. Do not use `require_escalated` "
                "for network. Credential files (logins, keys, `.env` files) are unreadable in "
                "the sandbox, approved commands included; do not ask to read them.")

def validate(brief: Brief) -> None:
    if brief.profile != PermissionProfile.AUTO:
        raise BriefRefused("Codex WP4 supports only auto; ask/strict need a separate permission integration")
    if brief.allowed_tools is not None:
        raise BriefRefused("Codex native tools cannot enforce a Brief.allowed_tools allowlist")
    if brief.always_ask:
        raise BriefRefused("Codex app-server has no pre-tool callback to enforce Brief.always_ask")
    if not Path(brief.cwd).is_absolute() or not Path(brief.cwd).is_dir():
        raise BriefRefused("Codex cwd must be an existing absolute worktree directory")


def atomically_write(path: Path, text: str) -> None:
    """0600 (mkstemp's mode), written aside and renamed into place. The
    config may carry a jarvis-mcp caller token (plan §1.3)."""
    if path.is_symlink():
        raise BriefRefused("Codex private metadata must not be a symlink")
    fd, tmp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _value(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return str(value).lower()
    if type(value) in (int, float):
        return json.dumps(value, allow_nan=False)
    if isinstance(value, list):
        return "[" + ", ".join(_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{json.dumps(k)} = {_value(v)}" for k, v in value.items()) + " }"
    raise BriefRefused("Unsupported value in Codex MCP configuration")


def approvals_reviewer(brief: Brief) -> str:
    """Owner chats route every native approval to Jarvis; task policy stays native."""
    return "user" if brief.role == Role.CHAT and brief.task_id is None else "auto_review"


def session_mcp(brief: Brief) -> dict | None:
    """Only the daemon's owner-chat run copy carries this server."""
    if brief.role == Role.CHAT and brief.task_id is None:
        return brief.mcp_servers.get("jarvis")
    return None


def validate_mcp(brief: Brief, effective: dict) -> None:
    """Compare measured 0.161 readback without putting token values in errors."""
    expected = session_mcp(brief)
    if expected is None:
        return
    servers = effective.get("mcp_servers")
    actual = servers.get("jarvis") if isinstance(servers, dict) else None
    if (not isinstance(actual, dict)
            or any(actual.get(key) != value for key, value in expected.items())
            or actual.get("enabled", True) is not True
            or actual.get("environment_id", "local") != "local"
            or actual.get("disabled_tools") not in (None, [])):
        raise BriefRefused("Codex effective config changed the Jarvis MCP delegation")


# --- the permission profile (plan 2026-10-15 §5, CG phase 2) -----------------
#
# `sandbox_mode = "workspace-write"` confined writes and the network but not
# reads: a plain sandboxed command could `cat ~/.config/gh/hosts.yml` without
# any callback reaching Jarvis (§2.1). Codex 0.161's named permission
# profiles deny reads under bwrap, and with `exec_permission_approvals` one
# owner `accept` of a `with_additional_permissions` network request gives that
# one command the network while every deny still holds (§5.1, measured).
# Measured on 0.161 and relied on here:
# - a "write" entry under a denied folder is mounted back (writable); a
#   "read" entry under one is not (it stays hidden), so the chat-git mirrors
#   live outside V2_DATA_DIR (`config.chat_git_mirrors_dir`);
# - a deny entry naming a symlink makes bwrap fail every command, so deny
#   entries are resolved first (the owner's ~/.aws is a link into /mnt/c);
# - a deny entry for a missing path is harmless;
# - a glob deny matches regular files only, to `glob_scan_max_depth`, and is
#   expanded at each command's start: a `.env` made mid-session is covered
#   from the next command; a `.env` that is a symlink, or a folder named
#   `.env`, is not (its target is covered only inside a denied folder);
# - `$TMPDIR` stays writable under the denied private home (`:workspace`).
PROFILE = "jarvis-chat"
PROFILE_EXTENDS = ":workspace"
GLOB_DEPTH = 4
# Case-insensitive, as the gate's env-name rule is. It also hides
# `.env.example` files from commands: a glob takes only "deny".
ENV_NAME = "[.][eE][nN][vV]*"
ENV_GLOB = "**/" + ENV_NAME
# Credential files in the owner's home outside the credential folders.
HOME_SECRETS = (".gitconfig", ".git-credentials", ".netrc")
# The feature this rests on, verified at codex_cli.CODEX_VERIFIED
# (tests pin the two together: re-run tests/v2/fixtures/cg2_probe.py before
# moving either).
FEATURE = "exec_permission_approvals"
FEATURE_VERIFIED = "0.161.0"
# Turn-wide grants (§5.1): must stay off; its requests get an empty grant.
TURN_WIDE_FEATURE = "request_permissions_tool"


def sandbox_home(home: Path) -> Path:
    """HOME for the session's commands (`shell_environment_policy.set`): the
    private home is denied (it holds config.toml and its token), and npm and
    friends need a writable HOME for their caches (measured: an approved
    `npm install` failed on `mkdir ~/.npm` without one)."""
    try:
        return _plain_path(Path(home) / "sandbox-home")
    except (OSError, RuntimeError, ValueError) as exc:
        raise BriefRefused("The Codex private home must be a plain absolute path") from exc


def _resolved(path) -> Path:
    try:
        return Path(path).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise BriefRefused("A Codex sandbox deny path cannot be resolved") from exc


# ~/.codex cannot be denied whole: the Codex binary lives there
# (packages/standalone/...) and re-executes itself as its own sandbox helper,
# so a deny makes every thread start fail (measured on 0.161). Every entry in
# it but these is denied as it is found when the config is written, and the
# private ones below whether or not they exist yet. (Not by a glob: a glob
# ending in `*` reaches into sub-folders, and a match inside a denied folder
# fails every command, measured.)
CODEX_HOME_KEEP = ("packages", "skills")
CODEX_HOME_PRIVATE = ("auth.json", ".credentials.json", "config.toml", "history.jsonl",
                      "session_index.jsonl", "plugins",
                      "sessions", "archived_sessions", "log", "logs", "memories",
                      "shell_snapshots", "app-server-control", "app-server-daemon")


def _codex_homes() -> list[Path]:
    out: list[Path] = []
    for item in (owner_home(), Path.home() / ".codex"):
        path = _resolved(item)
        if path not in out:
            out.append(path)
    return out


def _codex_home_entries() -> list[Path]:
    found: list[Path] = []
    for codex_home in _codex_homes():
        found += [codex_home / name for name in CODEX_HOME_PRIVATE]
        try:
            with os.scandir(codex_home) as entries:
                found += [Path(e.path) for e in entries if e.name not in CODEX_HOME_KEEP]
        except (FileNotFoundError, NotADirectoryError):
            pass
        except OSError as exc:
            raise BriefRefused("The Codex home cannot be listed for its denies") from exc
    return found


def check_binary(binary: str, denies: list[Path] | None = None) -> None:
    """The Codex binary re-executes itself inside its own sandbox, so it must
    not lie in a denied folder (every command would fail)."""
    path = _resolved(binary)
    if any(d == path or d in path.parents for d in (deny_paths() if denies is None else denies)):
        raise BriefRefused("The Codex binary lies inside a folder its sandbox denies")


def deny_paths() -> list[Path]:
    """Every folder and file a Codex command may never read, resolved (a
    symlinked one is denied where it leads; through the link it is then
    unreadable too), with entries inside another entry dropped: bwrap fails
    every command (and Codex every thread start) on a deny nested in a denied
    folder, which it makes an empty read-only tmpfs (measured on 0.161)."""
    from jarvis.v2.permissions import protected_paths

    home = Path.home()
    found = [*config.credential_dirs(), *_codex_home_entries(), home / ".claude",
             *(home / name for name in HOME_SECRETS),
             config.V2_DATA_DIR, config.SESSIONS_DIR, config.SPILL_DIR, config.SPEAKER_DIR,
             *sorted(protected_paths())]
    resolved: list[Path] = []
    for item in found:
        path = _resolved(item)
        if path not in resolved:
            resolved.append(path)
    return [p for p in resolved if not any(other in p.parents for other in resolved)]


# Bounds the folders listed to route the `.env*` globs around denied ones.
GLOB_LIST_CAP = 4000


def env_glob_bases(brief: Brief) -> list[Path]:
    """Where `.env*` files are denied: the owner's home (other projects keep
    their own `.env` files there), the session's folder (which may lie
    outside it, e.g. under /mnt/c), Jarvis's own checkout and the chat
    worktrees."""
    out: list[Path] = []
    for item in (Path.home(), brief.cwd, config.REPO_ROOT, config.CHAT_WORKTREES_DIR):
        path = _resolved(item)
        if path not in out and path != Path("/"):
            out.append(path)
    return out


def env_globs(brief: Brief, denies: list[Path]) -> list[str]:
    """The `.env*` deny globs. A glob match inside a denied folder makes
    bwrap fail every command (measured), and Codex expands the globs as the
    owner, so no glob may reach into one: a glob reaches files at most
    GLOB_DEPTH components below its literal base (measured: 4 matched, 5 did
    not). Where a denied folder lies within reach, the base gets a glob for
    its own files and each of its sub-folders is treated the same way,
    skipping the denied ones; such a split is listed when the config is
    written, so a folder made beside a denied one later is covered from the
    next session start."""
    budget = [GLOB_LIST_CAP]
    out: list[str] = []

    def walk(base: Path) -> None:
        blocked = [d for d in denies if base in d.parents
                   and len(d.relative_to(base).parts) <= GLOB_DEPTH - 1]
        if not blocked:
            out.append(f"{base}/{ENV_GLOB}")
            return
        out.append(f"{base}/{ENV_NAME}")
        try:
            with os.scandir(base) as entries:
                children = []
                for entry in entries:
                    budget[0] -= 1
                    if budget[0] < 0:
                        raise BriefRefused("Too many folders to place the Codex .env denies around")
                    if entry.is_dir(follow_symlinks=False):
                        children.append(Path(entry.path))
        except OSError as exc:
            raise BriefRefused("A folder for the Codex .env denies cannot be listed") from exc
        for child in sorted(children):
            if child not in denies:
                walk(child)

    for base in env_glob_bases(brief):
        if any(d == base or d in base.parents for d in denies):
            continue        # denied whole already
        walk(base)
    return list(dict.fromkeys(out))


def write_paths(brief: Brief, home: Path | None) -> list[str]:
    """The profile's "write" entries: an owner chat's git metadata and
    worktree root, then (given the private home) the commands' HOME and the
    session's skills folder, the two mounted back inside the denied home."""
    roots = git_roots(brief)
    if home is not None:
        roots = roots + [str(sandbox_home(home)), str(session_skills(home))]
    return roots


def filesystem(brief: Brief, home: Path | None = None) -> dict:
    """`[permissions.jarvis-chat.filesystem]`, in the order it is written:
    the glob depth, the write roots (git metadata and worktree root for an
    owner chat; the commands' HOME), every deny, then the `.env*` globs."""
    writes = write_paths(brief, home)
    denies = deny_paths()
    cwd = _resolved(brief.cwd)
    mirrors = _resolved(config.chat_git_mirrors_dir())
    # A deny inside the session's folder is the point; one at or above it
    # would leave the session unable to read its own files.
    if any(deny == cwd or deny in cwd.parents for deny in denies):
        raise BriefRefused("The Codex session folder lies inside a folder its sandbox denies")
    own = {sandbox_home(home), session_skills(home)} if home is not None else set()
    for write in map(Path, writes):
        if _overlaps(write, mirrors):
            # The daemon pushes from the mirrors with the owner's login.
            raise BriefRefused("A Codex sandbox write root overlaps the chat git mirrors")
        # The commands' HOME and skills are the write roots inside a deny
        # (V2_DATA_DIR): mounted back on their own, nothing beside them.
        if write not in own and any(_overlaps(write, deny) for deny in denies):
            raise BriefRefused("A Codex sandbox write root overlaps a denied folder")
    table: dict = {"glob_scan_max_depth": GLOB_DEPTH}
    table.update((root, "write") for root in writes)
    table.update((str(path), "deny") for path in denies)
    table.update((glob, "deny") for glob in env_globs(brief, denies))
    return table


def config_text(brief: Brief, home: Path | None = None) -> str:
    """A session's private config. `home` is its private Codex home: given,
    the commands get `sandbox_home(home)` as HOME (prepare passes it)."""
    values = dict(model_provider="openai", forced_login_method="chatgpt",
                  cli_auth_credentials_store="file", default_permissions=PROFILE,
                  approval_policy="on-request", approvals_reviewer=approvals_reviewer(brief),
                  web_search="live", check_for_update_on_startup=False)
    if brief.model is not None:
        values["model"] = brief.model
    if brief.effort is not None:
        values["model_reasoning_effort"] = brief.effort
    lines = [f"{k} = {_value(v)}" for k, v in values.items()]
    lines += ["[features]", f"{FEATURE} = true"]
    lines += ["[shell_environment_policy]", 'inherit = "all"']
    if home is not None:
        lines.append(f"set = {_value({'HOME': str(sandbox_home(home))})}")
    lines += [f"[projects.{_value(str(Path(brief.cwd).resolve()))}]", 'trust_level = "trusted"']
    lines += [f"[permissions.{PROFILE}]", f"extends = {_value(PROFILE_EXTENDS)}",
              f"[permissions.{PROFILE}.filesystem]"]
    lines += [f"{_value(k)} = {_value(v)}" for k, v in filesystem(brief, home).items()]
    lines += [f"[permissions.{PROFILE}.network]", "enabled = false"]
    for name, server in brief.mcp_servers.items():
        lines.append(f"[mcp_servers.{_value(name)}]")
        lines.extend(f"{_value(k)} = {_value(v)}" for k, v in server.items())
    return "\n".join(lines) + "\n"


def _refuse(what: str) -> None:
    raise BriefRefused(f"Codex permission profile not applied: {what}")


def check_effective(brief: Brief, effective: dict, home: Path | None) -> None:
    """`config/read`'s word that the profile is what was written (§5.2).
    Anything else refuses the session: a Codex that dropped the feature, a
    project config that changed the profile, a legacy sandbox mode."""
    if effective.get("default_permissions") != PROFILE:
        _refuse("default_permissions")
    if effective.get("sandbox_mode") is not None or effective.get("sandbox_workspace_write") is not None:
        _refuse("a legacy sandbox mode is set")
    features = effective.get("features")
    if not isinstance(features, dict) or features.get(FEATURE) is not True:
        _refuse(f"feature {FEATURE} is not on")
    if features.get(TURN_WIDE_FEATURE) not in (None, False):
        _refuse(f"feature {TURN_WIDE_FEATURE} is on")
    profiles = effective.get("permissions")
    profile = profiles.get(PROFILE) if isinstance(profiles, dict) else None
    if not isinstance(profile, dict):
        _refuse("the profile is missing")
    if profile.get("extends") != PROFILE_EXTENDS or profile.get("workspace_roots") not in (None, []):
        _refuse("the profile's base changed")
    if profile.get("filesystem") != filesystem(brief, home):
        _refuse("the filesystem entries changed")
    network = profile.get("network")
    if (not isinstance(network, dict) or network.get("enabled") is not False
            or any(v not in (None, False) for k, v in network.items() if k != "enabled")):
        _refuse("the profile's network changed")
    policy = effective.get("shell_environment_policy")
    want = {"HOME": str(sandbox_home(home))} if home is not None else None
    if (not isinstance(policy, dict) or policy.get("inherit") != "all"
            or (policy.get("set") or None) != want):
        _refuse("the command environment changed")


def check_receipt(brief: Brief, result: dict, home: Path | None) -> None:
    """The `thread/start`/`thread/resume` receipt: the profile is active,
    and its legacy projection has the network off and only our write roots."""
    if result.get("activePermissionProfile") != {"id": PROFILE, "extends": PROFILE_EXTENDS}:
        _refuse("the thread's active permission profile")
    legacy = result.get("sandbox")
    if (not isinstance(legacy, dict) or legacy.get("type") != "workspaceWrite"
            or legacy.get("networkAccess", True) is not False
            or not isinstance(legacy.get("writableRoots"), list)
            or sorted(legacy["writableRoots"]) != sorted(write_paths(brief, home))):
        _refuse("the thread's sandbox")


def _mcp_header(line: str) -> bool:
    """`[mcp_servers]`, `[mcp_servers.x]`, `[mcp_servers."x"]` (any spacing)."""
    if not line.startswith("["):
        return False
    inner = line.lstrip("[").lstrip()
    return inner.startswith("mcp_servers") and inner[len("mcp_servers"):].lstrip()[:1] in (".", "]")


def without_mcp_servers(text: str) -> str:
    """`config_text`'s output with every `[mcp_servers.*]` table removed.

    A session's jarvis server carries its caller token in its `env` table
    (plan §1.3), so a closed session's config must not keep it. `config_text`
    writes each server as one header and inline values, last in the file, and
    no value spans a line (`_value` is JSON), so a line-wise cut is exact; the
    result is checked by parsing it, and anything that still names
    `mcp_servers` is refused rather than written."""
    import tomllib
    kept, skipping = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            skipping = _mcp_header(stripped)
        if not skipping:
            kept.append(line)
    out = "\n".join(kept) + "\n"
    if "mcp_servers" in tomllib.loads(out):
        raise ValueError("mcp_servers survived the cut")
    return out


def strip_mcp_servers(path: Path) -> bool:
    """Rewrite a private Codex `config.toml` without its MCP servers. True
    if the file changed. Atomic, refuses a symlink (`atomically_write`)."""
    import tomllib
    text = Path(path).read_text(encoding="utf-8")
    if "mcp_servers" not in tomllib.loads(text):
        return False
    atomically_write(Path(path), without_mcp_servers(text))
    return True


def metadata_config_text() -> str:
    """Minimal strict config for account/model/quota reads (no thread starts)."""
    values = dict(model_provider="openai", forced_login_method="chatgpt",
                  cli_auth_credentials_store="file", sandbox_mode="workspace-write",
                  approval_policy="on-request", approvals_reviewer="auto_review",
                  check_for_update_on_startup=False)
    return "\n".join(f"{k} = {_value(v)}" for k, v in values.items()) + "\n"


def title_config_text(model: str, effort: str | None) -> str:
    """The chat titler's one-turn config (titles.py): read-only sandbox,
    never asks, no web search, no MCP servers, nothing trusted. Each of
    these is also requested on `thread/start`/`turn/start` and checked in
    the reply (`CodexProvider.one_shot`)."""
    values = dict(model_provider="openai", forced_login_method="chatgpt",
                  cli_auth_credentials_store="file", sandbox_mode="read-only",
                  approval_policy="never", web_search="disabled",
                  check_for_update_on_startup=False, model=model)
    if effort is not None:
        values["model_reasoning_effort"] = effort
    return "\n".join(f"{k} = {_value(v)}" for k, v in values.items()) + "\n"


def owner_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).resolve()


# The owner's git identity, read once (`git_identity`). Not a secret.
IDENTITY_CAP = 200
_IDENTITY: dict[str, str] | None = None
_IDENTITY_KEYS = (("user.name", ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME")),
                  ("user.email", ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL")))


def _global_value(key: str) -> str | None:
    """One value from the owner's global git config, or None. One printable
    line of at most IDENTITY_CAP characters; anything else is left out."""
    import subprocess
    from jarvis import gitops
    env = {"HOME": os.environ.get("HOME", ""), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
           "GIT_CONFIG_NOSYSTEM": "1"}
    if os.environ.get("XDG_CONFIG_HOME"):
        env["XDG_CONFIG_HOME"] = os.environ["XDG_CONFIG_HOME"]
    try:
        proc = subprocess.run([gitops.git_binary(), "config", "--global", "--no-includes", "--get", key],
                              cwd="/", env=env, stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=10)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        value = proc.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None
    value = value[:-1] if value.endswith("\n") else value
    if not value or len(value) > IDENTITY_CAP or not value.isprintable():
        return None
    return value


def git_identity() -> dict[str, str]:
    """GIT_AUTHOR_*/GIT_COMMITTER_* from the owner's global user.name and
    user.email (§4.6 of the 2026-10-15 git credentials plan): Codex's private
    HOME hides ~/.gitconfig, so commits would otherwise get a guessed name."""
    global _IDENTITY
    if _IDENTITY is None:
        found: dict[str, str] = {}
        for key, names in _IDENTITY_KEYS:
            value = _global_value(key)
            if value is not None:
                found.update(dict.fromkeys(names, value))
        _IDENTITY = found
    return dict(_IDENTITY)


def clean_env(home: Path, auth_home: Path | None = None) -> dict[str, str]:
    # Explicit allowlist; never forward daemon API keys, proxy variables or hooks.
    # Only the owner's git identity is added (CG-0): never PATH changes,
    # GIT_CONFIG_* or GH_* — Codex holds no login, no helper and no token path.
    env = {"HOME": str(home), "CODEX_HOME": str(auth_home or home),
           "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
           "TZ": "America/Chicago", "TMPDIR": str(home / "tmp")}
    env.update(git_identity())
    return env


def _session_skills(folder: Path, owner_skills: Path) -> None:
    """A session's `$CODEX_HOME/skills`, as a real folder: Codex lists skill
    paths under it (its bundled `.system` skills are installed there) and the
    model reads them with shell commands, but the private home is denied to
    commands and only a "write" entry is mounted back under a denied folder
    (measured), so this folder is one (`write_paths`). It holds one link per
    skill the owner installed, made fresh each start: a model that edits its
    own copy changes only its own session, and the links' targets stay as
    writable as they were (not at all, outside a write root). It used to be a
    link to the owner's whole folder; that link is replaced."""
    if folder.is_symlink():
        folder.unlink()
    folder.mkdir(mode=0o700, exist_ok=True)
    folder.chmod(0o700)
    wanted = {}
    if owner_skills.is_dir():
        with os.scandir(owner_skills) as entries:
            wanted = {e.name: Path(e.path) for e in entries
                      if not e.name.startswith(".") and e.is_dir(follow_symlinks=True)}
    with os.scandir(folder) as entries:
        for entry in entries:
            if entry.is_symlink() and wanted.get(entry.name) != Path(os.readlink(entry.path)):
                os.unlink(entry.path)
    for name, target in wanted.items():
        link = folder / name
        if not link.is_symlink() and not link.exists():
            link.symlink_to(target, target_is_directory=True)


def session_skills(home: Path) -> Path:
    try:
        return _plain_path(Path(home) / "skills")
    except (OSError, RuntimeError, ValueError) as exc:
        raise BriefRefused("The Codex private home must be a plain absolute path") from exc


def _home_of(name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise BriefRefused("Invalid Codex private thread directory id")
    return Path(config.V2_DATA_DIR) / "codex" / name


def _prepare_home(name: str, text, binary: str | None, *, skills: bool = True,
                  sandbox: bool = False) -> tuple[list[str], dict[str, str], Path]:
    """`text` is the config, or a function of the home that returns it.
    `sandbox` also makes the commands' HOME (`sandbox_home`)."""
    _home_of(name)
    auth_home = owner_home()
    auth = auth_home / "auth.json"
    if not auth.is_file():
        raise BriefRefused("Codex ChatGPT login missing; run codex login")
    if binary is None:
        from .codex_cli import resolve

        binary, reason = resolve()
        if binary is None:
            raise BriefRefused(reason)
    if sandbox:
        check_binary(binary)
    root = Path(config.V2_DATA_DIR) / "codex"
    home = root / name
    for directory in (root, home, home / "tmp", *((sandbox_home(home),) if sandbox else ())):
        if directory.is_symlink():
            raise BriefRefused("Codex private directory must not be a symlink")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
    link = home / "auth.json"
    if link.is_symlink():
        if link.resolve() != auth.resolve():
            raise BriefRefused("Codex auth link points to a different login")
    elif link.exists():
        raise BriefRefused("Codex private auth must link to the owner's login")
    else:
        link.symlink_to(auth.resolve())
    # Preserve installed skills without importing personal config or plugins.
    link_skills = skills
    skills = home / "skills"
    if sandbox:
        _session_skills(skills, auth_home / "skills")
    elif link_skills and not skills.exists() and not skills.is_symlink() and (auth_home / "skills").is_dir():
        skills.symlink_to(auth_home / "skills", target_is_directory=True)
    atomically_write(home / "config.toml", text(home) if callable(text) else text)
    return [str(Path(binary).resolve()), "app-server", "--strict-config", "--stdio"], clean_env(home), home


def prepare(thread_id: str, brief: Brief, binary: str | None = None) -> tuple[list[str], dict[str, str], Path]:
    """The app-server argv, child environment and private home for a thread.

    ``binary`` is the codex the caller already version-checked; without one
    it is resolved here the same way (codex_cli.resolve), never from a bare
    PATH lookup that could land on the Windows shim.
    """
    validate(brief)
    roots = git_roots(brief)
    if roots:
        root = Path(roots[-1])
        # Traverse with directory fds: neither mkdir nor chmod follows a
        # symlink inserted into a parent between validation and creation.
        fd = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in root.parts[1:]:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            info = os.fstat(fd)
            if (info.st_uid != os.getuid() or chat_worktrees_dir() != root
                    or (info.st_dev, info.st_ino) != (root.stat().st_dev, root.stat().st_ino)):
                raise BriefRefused("Codex chat worktrees directory changed")
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)
    return _prepare_home(thread_id, lambda home: config_text(brief, home), binary, sandbox=True)


def prepare_metadata(binary: str | None = None) -> tuple[list[str], dict[str, str], Path]:
    """An isolated app-server home for read-only account metadata RPCs."""
    return _prepare_home("_metadata", metadata_config_text(), binary)


def prepare_title(binary: str, model: str, effort: str | None) -> tuple[list[str], dict[str, str], Path]:
    """The chat titler's private home: its own config (`title_config_text`),
    the owner's login linked as every session's is, and no skills."""
    return _prepare_home("_titles", title_config_text(model, effort), binary, skills=False)
