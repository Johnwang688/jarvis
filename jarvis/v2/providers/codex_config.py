"""Private native-tool config; preparation adapted from the trading firm."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile

from jarvis import config
from jarvis.v2.model import PermissionProfile
from jarvis.v2.provider import Brief, BriefRefused


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


def owner_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).resolve()


def clean_env(home: Path, auth_home: Path | None = None) -> dict[str, str]:
    # Explicit allowlist; never forward daemon API keys, proxy variables or hooks.
    env = {"HOME": str(home), "CODEX_HOME": str(auth_home or home),
           "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
           "TZ": "America/Chicago", "TMPDIR": str(home / "tmp")}
    return env


def prepare(thread_id: str, brief: Brief, binary: str | None = None) -> tuple[list[str], dict[str, str], Path]:
    """The app-server argv, child environment and private home for a thread.

    ``binary`` is the codex the caller already version-checked; without one
    it is resolved here the same way (codex_cli.resolve), never from a bare
    PATH lookup that could land on the Windows shim.
    """
    validate(brief)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", thread_id):
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
    home = root / thread_id
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
    skills = home / "skills"
    if not skills.exists() and not skills.is_symlink() and (auth_home / "skills").is_dir():
        skills.symlink_to(auth_home / "skills", target_is_directory=True)
    atomically_write(home / "config.toml", config_text(brief))
    return [str(Path(binary).resolve()), "app-server", "--strict-config", "--stdio"], clean_env(home), home
