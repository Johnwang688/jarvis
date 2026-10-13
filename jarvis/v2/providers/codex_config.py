"""Private native-tool config; preparation adapted from the trading firm."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile

from jarvis import config
from jarvis.v2.model import PermissionProfile, Role
from jarvis.v2.provider import Brief, BriefRefused

_GITFILE_CAP = 4096


def _small_text(path: Path) -> str:
    """A `.git` or `commondir` file's first line; a regular file only, capped."""
    with open(path, "rb") as stream:
        return stream.read(_GITFILE_CAP).decode("utf-8", "replace").strip()


def git_dirs(cwd: str) -> list[Path]:
    """Where the repository at or above `cwd` keeps its history: its git dir
    and, for a linked worktree, the common one. [] outside a repository, when
    one cannot be read, or when it sits in a credential folder or Jarvis's own
    data."""
    here = Path(cwd).resolve()
    found: list[Path] = []
    try:
        for folder in (here, *here.parents):
            dot = folder / ".git"
            if dot.is_symlink():
                return []
            if dot.is_dir():
                found = [dot.resolve()]
                break
            if dot.is_file():
                text = _small_text(dot)
                if not text.startswith("gitdir:"):
                    return []
                gitdir = Path(text[len("gitdir:"):].strip())
                gitdir = (gitdir if gitdir.is_absolute() else folder / gitdir).resolve()
                found = [gitdir]
                common = gitdir / "commondir"
                if common.is_file():
                    shared = Path(_small_text(common))
                    found.append((shared if shared.is_absolute() else gitdir / shared).resolve())
                break
    except OSError:
        return []
    barred = [Path(p).expanduser().resolve() for p in config.credential_dirs()]
    barred.append(Path(config.V2_DATA_DIR).resolve())
    out = []
    for path in found:
        if not path.is_dir() or any(path == b or b in path.parents for b in barred):
            return []
        if path not in out:
            out.append(path)
    return out


def git_roots(brief: Brief) -> list[str]:
    """What a Codex chat's sandbox may write outside its project: the
    repository's git dirs (commits, branches, worktrees) and
    `config.CHAT_WORKTREES_DIR`. Task workers get none; their worktrees are
    Jarvis's. The network stays off: a push or `gh` has to ask to leave the
    sandbox, so Jarvis's gate still judges it (a push to main or a force push
    asks the owner)."""
    if brief.role != Role.CHAT:
        return []
    return [str(p) for p in git_dirs(brief.cwd)] + [str(Path(config.CHAT_WORKTREES_DIR).expanduser())]


def git_note(brief: Brief) -> str:
    """The sentence a Codex chat is told about git, or "" for anything else."""
    roots = git_roots(brief)
    if not roots:
        return ""
    where = roots[-1]
    return (f"Git in this session: the sandbox lets you write this repository's .git (commit, branch, "
            f"worktree) and {where}. Make new git worktrees under {where}/<name>; other folders outside "
            "the project are read-only. The network is off inside the sandbox, so run `git push` and `gh` "
            "with escalated permissions; Jarvis decides those, and a push to main or a force push asks the owner.")


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


def config_text(brief: Brief) -> str:
    values = dict(model_provider="openai", forced_login_method="chatgpt",
                  cli_auth_credentials_store="file", sandbox_mode="workspace-write",
                  approval_policy="on-request", approvals_reviewer="auto_review",
                  web_search="live", check_for_update_on_startup=False)
    if brief.model is not None:
        values["model"] = brief.model
    if brief.effort is not None:
        values["model_reasoning_effort"] = brief.effort
    lines = [f"{k} = {_value(v)}" for k, v in values.items()]
    lines += ["[shell_environment_policy]", 'inherit = "all"',
              f"[projects.{_value(str(Path(brief.cwd).resolve()))}]", 'trust_level = "trusted"']
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
    return _prepare_home(thread_id, config_text(brief), binary)


def prepare_metadata(binary: str | None = None) -> tuple[list[str], dict[str, str], Path]:
    """An isolated app-server home for read-only account metadata RPCs."""
    return _prepare_home("_metadata", metadata_config_text(), binary)


def prepare_title(binary: str, model: str, effort: str | None) -> tuple[list[str], dict[str, str], Path]:
    """The chat titler's private home: its own config (`title_config_text`),
    the owner's login linked as every session's is, and no skills."""
    return _prepare_home("_titles", title_config_text(model, effort), binary, skills=False)
