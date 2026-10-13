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
    """Truthful guidance; network escalation still goes through Jarvis."""
    roots = git_roots(brief)
    if not roots:
        return ""
    where = roots[-1]
    metadata = ("Validated repository git metadata is writable for commits, branches and worktrees. "
                if len(roots) > 1 else "No writable repository git metadata was found. ")
    return ("Git in this session: " + metadata
            + f"Make new git worktrees under {where}/<unique-name>. "
            "The project checkout is shared; use a separate worktree for real edits. "
            "The network is off inside the sandbox. Request escalated permissions for "
            "git push and gh; Jarvis judges the request, and sandbox widening "
            "requires the owner's approval.")

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


def config_text(brief: Brief) -> str:
    values = dict(model_provider="openai", forced_login_method="chatgpt",
                  cli_auth_credentials_store="file", sandbox_mode="workspace-write",
                  approval_policy="on-request", approvals_reviewer=approvals_reviewer(brief),
                  web_search="live", check_for_update_on_startup=False)
    if brief.model is not None:
        values["model"] = brief.model
    if brief.effort is not None:
        values["model_reasoning_effort"] = brief.effort
    lines = [f"{k} = {_value(v)}" for k, v in values.items()]
    lines += ["[shell_environment_policy]", 'inherit = "all"',
              f"[projects.{_value(str(Path(brief.cwd).resolve()))}]", 'trust_level = "trusted"']
    roots = git_roots(brief)
    if roots:
        lines += ["[sandbox_workspace_write]", f"writable_roots = {_value(roots)}",
                  "network_access = false"]
    for name, server in brief.mcp_servers.items():
        lines.append(f"[mcp_servers.{_value(name)}]")
        lines.extend(f"{_value(k)} = {_value(v)}" for k, v in server.items())
    return "\n".join(lines) + "\n"


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


def clean_env(home: Path, auth_home: Path | None = None) -> dict[str, str]:
    # Explicit allowlist; never forward daemon API keys, proxy variables or hooks.
    env = {"HOME": str(home), "CODEX_HOME": str(auth_home or home),
           "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
           "TZ": "America/Chicago", "TMPDIR": str(home / "tmp")}
    return env


def _prepare_home(name: str, text: str, binary: str | None, *,
                  skills: bool = True) -> tuple[list[str], dict[str, str], Path]:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise BriefRefused("Invalid Codex private thread directory id")
    auth_home = owner_home()
    auth = auth_home / "auth.json"
    if not auth.is_file():
        raise BriefRefused("Codex ChatGPT login missing; run codex login")
    if binary is None:
        from .codex_cli import resolve

        binary, reason = resolve()
        if binary is None:
            raise BriefRefused(reason)
    root = Path(config.V2_DATA_DIR) / "codex"
    home = root / name
    for directory in (root, home, home / "tmp"):
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
    if link_skills and not skills.exists() and not skills.is_symlink() and (auth_home / "skills").is_dir():
        skills.symlink_to(auth_home / "skills", target_is_directory=True)
    atomically_write(home / "config.toml", text)
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
    return _prepare_home(thread_id, config_text(brief), binary)


def prepare_metadata(binary: str | None = None) -> tuple[list[str], dict[str, str], Path]:
    """An isolated app-server home for read-only account metadata RPCs."""
    return _prepare_home("_metadata", metadata_config_text(), binary)


def prepare_title(binary: str, model: str, effort: str | None) -> tuple[list[str], dict[str, str], Path]:
    """The chat titler's private home: its own config (`title_config_text`),
    the owner's login linked as every session's is, and no skills."""
    return _prepare_home("_titles", title_config_text(model, effort), binary, skills=False)
