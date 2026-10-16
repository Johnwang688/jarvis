"""Push, pull requests and remote fetches for Codex chats, run by the daemon
(owner decision CG-0, 2026-10-15; `docs/plans/2026-10-15-codex-git-credentials.md`).

The GitHub login stays inside Jarvis. A Codex chat has no network in its
sandbox and no login in its environment; it commits in its own worktree and
then calls a Jarvis tool (`tools/chat_git_tools.py`). The owner is asked
every time (`permissions.build_permit`'s dangerous-tool branch, no Always;
merges and closes carry merge_gate's headline), and only then does the
daemon run git or gh with the owner's credentials:

1. **Read the target by file reads** (`read_target`): the worktree must be
   the chat's own folder or a linked worktree of the same repository under
   `config.CHAT_WORKTREES_DIR`, its git metadata validated as
   `codex_config.git_dirs` validates it (no symlinks, small pointer files,
   back-links that agree). The branch comes from HEAD; `remote.origin.url`
   from a private copy of the repository's `config`, read by `git config -f`
   with no includes, no global or system config and an empty HOME.
2. **Validate the destination** (`github_repo`, `pin`): only
   `https://github.com/O/R(.git)` or `git@github.com:O/R(.git)`, and, after
   the first push (which the owner's card names), the same `O/R` as the pin
   in `V2_DATA_DIR/chat-git/pins.json`. A changed origin is refused.
3. **A local fetch with no credentials** (`_local_fetch`): the branch is
   copied from the Codex repository into a daemon-owned bare mirror
   (`config.chat_git_mirrors_dir()/<key>`, 0700: beside V2_DATA_DIR, which a
   Codex sandbox cannot read, so the chat's own `git fetch <mirror>` can;
   phase 2, 2026-10-16). This is the only step that touches
   a repository Codex can configure, so it runs with an empty HOME, no
   global or system config, no credential variable, and gitops' hardening.
4. **The network step runs in the mirror** (`run_git`, `run_gh`): the push
   goes from the mirror to the pinned URL; gh runs with `--repo O/R` and its
   working directory in the mirror. Both run with the owner's HOME (so gh
   finds its login), gitops' hardening, the credential helper list reset to
   only `gh auth git-credential`, no proxy, and output scrubbed.

The approval is bound to the call (`grant`/`take`): the permit that put the
card to the owner records the exact line it showed, and the tool recomputes
the line from the repository as it is now and refuses if they differ (a
branch switched while the card was open is never pushed).

`raw_remote` is CG-1: an escalated raw `git push`/`fetch`/`pull`/
`ls-remote` to a remote, or any `gh`, from a Codex chat is declined without
a card and pointed at these tools — it could only fail for want of a login.

No model-facing text lives here except the refusal reasons.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from fnmatch import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import tempfile
import threading

from jarvis import config, gitops, rules

TOOLS = frozenset({"chat_git_push", "chat_pr_create", "chat_pr_merge", "chat_pr_close",
                   "chat_git_fetch"})
DANGEROUS = frozenset({"chat_git_push", "chat_pr_create", "chat_pr_merge", "chat_pr_close"})
MERGE_METHODS = ("squash", "merge", "rebase")

POINTER = ("This Codex session has no GitHub login, so an escalated git push, fetch, "
           "pull or ls-remote to a remote, or any gh command, cannot succeed and was "
           "declined without asking the owner. Call the Jarvis tools instead: "
           "chat_git_push, chat_pr_create, chat_pr_merge or chat_pr_close (each asks "
           "the owner), or chat_git_fetch and then `git fetch <mirror>`.")

CONFIG_CAP = 65_536
TITLE_CAP = 256
BODY_CAP = 60_000
OUTPUT_CAP = gitops.MAX_OUTPUT

# Test seam: "OWNER/REPO" -> a local bare repository the push goes to instead
# of github.com. Set in code only (tests), never from the environment or a
# file. The URL check (`github_repo`) is unaffected by it.
_ALLOW_TEST_REMOTE: dict[str, str] = {}

_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_HTTPS = re.compile(r"https://github\.com/([^/\s]+)/([^/\s]+?)/?")
_SSH = re.compile(r"git@github\.com:([^/\s]+)/([^/\s]+)")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
_HEX = re.compile(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?")

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_pins_lock = threading.Lock()


class Refused(ValueError):
    """A call refused before anything ran; the text says why."""


@dataclass(frozen=True)
class Target:
    worktree: str
    gitdir: Path
    common: Path
    branch: str | None          # None: detached HEAD
    origin_url: str
    repo: str                   # OWNER/REPO


@dataclass(frozen=True)
class Plan:
    tool: str
    args: tuple                 # the normalized arguments, sorted items
    line: str                   # the exact network commands, for merge_gate and the card
    headline: str
    target: Target
    brief_cwd: str
    first_pin: bool

    @property
    def where(self) -> str:
        return self.target.worktree


# --- arguments ------------------------------------------------------------------


def _text(value, name: str, cap: int, *, empty: bool = True, lines: bool = False) -> str:
    if not isinstance(value, str):
        raise Refused(f"{name} must be text")
    if not empty and not value.strip():
        raise Refused(f"{name} must not be empty")
    if len(value) > cap:
        raise Refused(f"{name} is longer than {cap} characters")
    allowed = "\n\t" if lines else ""
    if any((ord(c) < 32 and c not in allowed) or ord(c) == 127 for c in value):
        raise Refused(f"{name} holds a control character")
    return value


def normalize(tool: str, args: dict) -> dict:
    """The call's arguments with defaults filled in, checked; Refused otherwise."""
    if tool not in TOOLS:
        raise Refused("not a chat git tool")
    args = dict(args or {})
    known = {"chat_git_push": {"worktree"}, "chat_git_fetch": {"worktree"},
             "chat_pr_create": {"worktree", "title", "body", "base", "draft"},
             "chat_pr_merge": {"worktree", "number", "method"},
             "chat_pr_close": {"worktree", "number"}}[tool]
    extra = set(args) - known
    if extra:
        raise Refused("unknown argument(s): " + ", ".join(sorted(map(str, extra))))
    out = {"worktree": _text(args.get("worktree"), "worktree", 4096, empty=False)}
    if tool == "chat_pr_create":
        out["title"] = _text(args.get("title"), "title", TITLE_CAP, empty=False)
        out["body"] = _text(args.get("body", ""), "body", BODY_CAP, lines=True)
        base = args.get("base", "")
        if not isinstance(base, str) or (base and not valid_branch(base)):
            raise Refused("base must be empty or a plain branch name")
        out["base"] = base
        draft = args.get("draft", False)
        if not isinstance(draft, bool):
            raise Refused("draft must be true or false")
        out["draft"] = draft
    if tool in ("chat_pr_merge", "chat_pr_close"):
        number = args.get("number")
        if isinstance(number, bool) or not isinstance(number, int) or not 0 < number < 10 ** 9:
            raise Refused("number must be a pull request number")
        out["number"] = number
    if tool == "chat_pr_merge":
        method = args.get("method", "squash")
        if method not in MERGE_METHODS:
            raise Refused("method must be squash, merge or rebase")
        out["method"] = method
    return out


def valid_branch(name) -> bool:
    """A plain branch name: no leading '-', no '..', ':', '@{', spaces or
    control characters, no component starting with '.' or ending '.lock'."""
    if not isinstance(name, str) or not _BRANCH.fullmatch(name) or name == "HEAD":
        return False
    if ".." in name or "//" in name or name.endswith(("/", ".")):
        return False
    return not any(not part or part.startswith(".") or part.endswith(".lock")
                   for part in name.split("/"))


def github_repo(url) -> str | None:
    """`OWNER/REPO` for a github.com HTTPS or SSH URL, None for anything else
    (another host, userinfo, a port, a query, `ext::`, `file://`, a path)."""
    if not isinstance(url, str) or len(url) > 300:
        return None
    match = _HTTPS.fullmatch(url) or _SSH.fullmatch(url)
    if not match:
        return None
    owner, repo = match.group(1), match.group(2)
    if repo.endswith(".git"):
        repo = repo[:-4]
    for part in (owner, repo):
        if not _NAME.fullmatch(part) or part in (".", "..") or part.startswith("."):
            return None
    return f"{owner}/{repo}"


def push_url(repo: str) -> str:
    """Where the daemon pushes `OWNER/REPO` (the test seam aside)."""
    return _ALLOW_TEST_REMOTE.get(repo) or f"https://github.com/{repo}.git"


def _shown_url(repo: str) -> str:
    return f"https://github.com/{repo}.git"


# --- the daemon's own folders ---------------------------------------------------


def root() -> Path:
    base = Path(config.V2_DATA_DIR)
    path = base / "chat-git"
    for folder in (base, path):
        if folder.is_symlink():
            raise Refused("the chat git folder must not be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _empty_home() -> Path:
    home = root() / "empty-home"
    if home.is_symlink():
        raise Refused("the chat git folder must not be a symlink")
    home.mkdir(mode=0o700, exist_ok=True)
    home.chmod(0o700)
    return home


def project_key(common: Path) -> str:
    return hashlib.sha256(str(common).encode("utf-8")).hexdigest()[:40]


def _lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def mirrors_root() -> Path:
    """The mirrors' folder (`config.chat_git_mirrors_dir`), 0700. A Codex
    chat's commands read a mirror (`git fetch <mirror>`) but must never write
    one — the daemon pushes from it with the owner's login — so it is refused
    inside a folder a chat may write (CHAT_WORKTREES_DIR) and inside
    V2_DATA_DIR, which the chat's sandbox cannot read
    (`codex_config.filesystem` refuses a write root over it too)."""
    path = Path(config.chat_git_mirrors_dir()).expanduser()
    if not path.is_absolute():
        raise Refused("the mirror folder must be an absolute path")
    for folder in (path, *path.parents):
        if folder.is_symlink():
            raise Refused("the mirror folder must not be a symlink")
    path = path.resolve()
    for other in (Path(config.CHAT_WORKTREES_DIR), Path(config.V2_DATA_DIR)):
        try:
            other = other.expanduser().resolve()
        except (OSError, RuntimeError):
            raise Refused("the mirror folder cannot be checked") from None
        if path == other or other in path.parents or path in other.parents:
            raise Refused(f"the mirror folder must be apart from {other}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def mirror_dir(common: Path) -> Path:
    """The daemon's bare mirror for the repository whose common dir is
    `common`: made on first use, 0700, refused if it is a symlink."""
    path = mirrors_root() / project_key(common)
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise Refused("the mirror must be a plain folder")
    if not (path / "HEAD").is_file():
        code, _out, err = _plain_git(["init", "--bare", "--quiet", str(path)])
        if code != 0:
            raise Refused("could not make the mirror: " + _scrub(err.strip())[:300])
    path.chmod(0o700)
    return path


def _pins_path() -> Path:
    return root() / "pins.json"


def _pins() -> dict:
    path = _pins_path()
    if path.is_symlink():
        raise Refused("the pins file must not be a symlink")
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused("the pins file cannot be read") from exc
    if not isinstance(data, dict):
        raise Refused("the pins file cannot be read")
    return data


def pinned(common: Path) -> str | None:
    with _pins_lock:
        entry = _pins().get(project_key(common))
    repo = entry.get("repo") if isinstance(entry, dict) else None
    return repo if isinstance(repo, str) else None


def pin(common: Path, repo: str) -> None:
    """Record `repo` for this repository, once. Pinning a different one is
    refused: the owner re-pins by removing the entry."""
    with _pins_lock:
        data = _pins()
        key = project_key(common)
        entry = data.get(key)
        if isinstance(entry, dict) and entry.get("repo"):
            if str(entry["repo"]).lower() != repo.lower():
                raise Refused(_changed(entry["repo"], repo))
            return
        data[key] = {"repo": repo, "common": str(common)}
        _write_pins(data)


def _write_pins(data: dict) -> None:
    path = _pins_path()
    fd, tmp = tempfile.mkstemp(prefix=".pins-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=1, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _changed(was: str, now: str) -> str:
    return (f"origin changed since it was pinned to {was} (it now names {now}); the owner "
            f"re-pins it by removing that entry from {_pins_path()}")


# --- reading the target -----------------------------------------------------------


def _private_copy(path: Path, cap: int) -> bytes:
    """A regular, non-symlink file's bytes, at most `cap`."""
    from .providers.codex_config import _plain_path
    path = _plain_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > cap:
            raise Refused("the repository config is not a small regular file")
        data = stream.read(cap + 1)
    if len(data) > cap:
        raise Refused("the repository config is too large")
    return data


def _origin_url(common: Path) -> str:
    """`remote.origin.url` from a private copy of the repository's config, read
    with no includes and no other config. Exactly one value, or Refused."""
    data = _private_copy(common / "config", CONFIG_CAP)
    fd, copy = tempfile.mkstemp(prefix=".config-", dir=_empty_home().parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        code, out, _err = _plain_git(["config", "--file", copy, "--no-includes", "--get-all",
                                      "remote.origin.url"])
    finally:
        os.unlink(copy)
    values = [v for v in out.split("\n") if v] if code == 0 else []
    if len(values) != 1:
        raise Refused("the repository needs exactly one remote.origin.url")
    return values[0]


def read_target(worktree, brief_cwd: str) -> Target:
    """What a call on `worktree` would push or act on, read from files.

    The worktree is the chat's own folder or a linked worktree of the same
    repository under `CHAT_WORKTREES_DIR`, with a `.git` of its own."""
    from .providers import codex_config
    from .provider import BriefRefused
    if not isinstance(worktree, str) or not worktree.startswith("/"):
        raise Refused("worktree must be an absolute path")
    try:
        here = codex_config._plain_path(Path(worktree))
        home = codex_config._plain_path(Path(brief_cwd))
    except (OSError, RuntimeError, ValueError) as exc:
        raise Refused("worktree must be a plain folder (no symlinks)") from exc
    if here != home:
        try:
            chats = codex_config.chat_worktrees_dir()
        except BriefRefused as exc:
            raise Refused("the chat worktrees folder is not usable") from exc
        if chats not in here.parents:
            raise Refused(f"worktree must be this chat's folder or a worktree under {chats}")
    dot = here / ".git"
    if not os.path.lexists(dot):
        raise Refused("worktree is not the top of a git checkout")
    dirs = codex_config.git_dirs(str(here))
    own = codex_config.git_dirs(str(home))
    if not dirs or not own:
        raise Refused("the git metadata could not be validated")
    gitdir, common = dirs[0], dirs[-1]
    if common != own[-1]:
        raise Refused("worktree belongs to a different repository than this chat's")
    try:
        head = codex_config._small_text(gitdir / "HEAD")
    except (OSError, ValueError) as exc:
        raise Refused("HEAD cannot be read") from exc
    if head.startswith("ref: refs/heads/"):
        branch = head[len("ref: refs/heads/"):]
    elif _HEX.fullmatch(head):
        branch = None
    else:
        raise Refused("HEAD names something other than a branch")
    try:
        url = _origin_url(common)
    except OSError as exc:
        raise Refused("the repository config cannot be read") from exc
    repo = github_repo(url)
    if repo is None:
        raise Refused("origin is not a github.com repository (https://github.com/OWNER/REPO "
                      "or git@github.com:OWNER/REPO)")
    return Target(str(here), gitdir, common, branch, url, repo)


def judge_push(target: Target, default: str | None = None) -> None:
    """Refuses unasked: a detached HEAD, a branch name git would read as
    something else, a protected branch (merge_gate's list, a remote's HEAD
    branch, the remote's default when known). The tools take no force or
    delete option; landing on main is chat_pr_merge's job."""
    from . import merge_gate
    branch = target.branch
    if branch is None:
        raise Refused("HEAD is detached; check out a branch first")
    if not valid_branch(branch):
        raise Refused(f"branch name {branch!r} is not a plain branch name")
    lowered = branch.lower()
    defaults = {d.lower() for d in merge_gate._remote_heads(target.common)}
    if default:
        defaults.add(default.lower())
    if lowered in defaults or any(fnmatch(lowered, p) for p in merge_gate.PROTECTED_BRANCHES):
        raise Refused(f"{branch} is a protected or default branch; push a topic branch and "
                      "open a pull request")


# --- the exact commands ---------------------------------------------------------------


def _commands(tool: str, args: dict, target: Target) -> list[tuple[str, list[str]]]:
    """(program, argv) for each network command, in order. `gate_line` shows
    exactly these; `_execute` runs exactly these."""
    repo, branch = target.repo, target.branch
    push = ("git", ["push", _shown_url(repo), f"refs/heads/{branch}:refs/heads/{branch}"])
    if tool == "chat_git_push":
        return [push]
    if tool == "chat_git_fetch":
        return [("git", ["fetch", "--no-tags", "--prune", _shown_url(repo),
                         "+refs/heads/*:refs/remotes/origin/*"])]
    if tool == "chat_pr_create":
        argv = ["pr", "create", "--repo", repo, "--head", branch]
        if args["base"]:
            argv += ["--base", args["base"]]
        if args["draft"]:
            argv.append("--draft")
        argv += ["--title", args["title"], "--body-file", "-"]
        return [push, ("gh", argv)]
    if tool == "chat_pr_merge":
        return [("gh", ["pr", "merge", str(args["number"]), "--repo", repo, f"--{args['method']}"])]
    return [("gh", ["pr", "close", str(args["number"]), "--repo", repo])]


def gate_line(tool: str, args: dict, target: Target) -> str:
    """The exact command(s) the daemon will run, as one shell line: what
    merge_gate reads and the owner's card shows."""
    return " && ".join(f"{prog} {shlex.join(argv)}" for prog, argv in _commands(tool, args, target))


def _headline(tool: str, args: dict, target: Target, first_pin: bool) -> str:
    if tool == "chat_git_push":
        words = f"Codex wants to push {target.branch} to {target.repo}"
    elif tool == "chat_pr_create":
        words = f"Codex wants to open a pull request from {target.branch} on {target.repo}"
    elif tool == "chat_pr_merge":
        words = f"Codex wants to merge pull request #{args['number']} on {target.repo}"
    elif tool == "chat_pr_close":
        words = f"Codex wants to close pull request #{args['number']} on {target.repo}"
    else:
        words = f"Codex fetches {target.repo} into the mirror"
    if first_pin:
        words += f" · first push: pins origin to {target.repo}"
    return words


def plan(tool: str, args: dict, brief_cwd: str) -> Plan:
    """Everything the permit needs, or Refused (the permit then DENYs)."""
    values = normalize(tool, args)
    target = read_target(values["worktree"], brief_cwd)
    current = pinned(target.common)
    if current is not None and current.lower() != target.repo.lower():
        raise Refused(_changed(current, target.repo))
    first = current is None
    if first and tool not in ("chat_git_push", "chat_pr_create"):
        raise Refused("this repository has no pinned GitHub origin yet; push a branch first "
                      "(chat_git_push), which the owner approves")
    if tool in ("chat_git_push", "chat_pr_create"):
        judge_push(target)
    return Plan(tool, tuple(sorted(values.items())), gate_line(tool, values, target),
                _headline(tool, values, target, first), target, str(brief_cwd), first)


# --- binding the owner's yes to this call ------------------------------------------

_GRANT: ContextVar[Plan | None] = ContextVar("jarvis_chat_git_grant", default=None)


def grant(approved: Plan) -> None:
    """Called by the permit when it ALLOWs this call: the plan the owner saw."""
    _GRANT.set(approved)


def take(tool: str, args: dict) -> Plan | None:
    """The plan granted for exactly this call, once; None otherwise."""
    held = _GRANT.get()
    _GRANT.set(None)
    if held is None or held.tool != tool:
        return None
    try:
        values = normalize(tool, args)
    except Refused:
        return None
    return held if held.args == tuple(sorted(values.items())) else None


# --- running git and gh ------------------------------------------------------------

_OWNER_ENV_KEYS = ("HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "USER", "LOGNAME", "TZ",
                   "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
                   "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
                   "GH_CONFIG_DIR", "SSL_CERT_FILE", "SSL_CERT_DIR")


def _owner_env() -> dict[str, str]:
    """The daemon's own HOME and PATH (gh finds the owner's login there), no
    GIT_* and nothing else from the daemon's environment, prompts off."""
    env = {k: os.environ[k] for k in _OWNER_ENV_KEYS if os.environ.get(k)}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
                "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "GH_PAGER": "cat",
                "NO_COLOR": "1", "GH_SPINNER_DISABLED": "1", "GH_EDITOR": "true",
                "GH_BROWSER": "true", "BROWSER": "true"})
    return env


def _bare_env() -> dict[str, str]:
    """No credential anywhere: an empty private HOME, no global or system
    config, a system PATH. For every step that reads a Codex-writable file."""
    home = _empty_home()
    return {"PATH": "/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0", "GIT_CEILING_DIRECTORIES": str(home.parent)}


def _credential_pins() -> list[str]:
    """`-c` pins for a credentialed git: every configured credential helper
    dropped, then only gh's; no proxy, no ssh command, no askpass."""
    pins = ["-c", "credential.helper="]
    gh = gitops.gh_binary()
    if gh:
        pins += ["-c", f"credential.https://github.com.helper=!{shlex.quote(gh)} auth git-credential"]
    pins += ["-c", "http.proxy=", "-c", "https.proxy=", "-c", "core.sshCommand=",
             "-c", "core.askPass=", "-c", "protocol.file.allow=user"]
    return pins


def _run(argv: list[str], env: dict, cwd: str, timeout: float,
         stdin: str | None = None) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(argv, cwd=cwd, env=env, input=stdin,
                              stdin=None if stdin is not None else subprocess.DEVNULL,
                              capture_output=True, text=True, errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout:.0f}s"
    except (OSError, ValueError) as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"
    return proc.returncode, proc.stdout, proc.stderr


def _plain_git(args: list[str], *, timeout: float = gitops.LOCAL_TIMEOUT_S) -> tuple[int, str, str]:
    """Hardened git with no credentials, run in the empty home."""
    env = _bare_env()
    return _run([gitops.git_binary(), *gitops._HARDENING, *args], env, env["HOME"], timeout)


def run_git(args: list[str], *, timeout: float = gitops.NETWORK_TIMEOUT_S,
            stdin: str | None = None, cwd: str | None = None) -> tuple[int, str, str]:
    """Credentialed git: gitops' hardening, then the credential pins, then
    `args` (which name the mirror with `--git-dir`)."""
    argv = [gitops.git_binary(), *gitops._HARDENING, *_credential_pins(),
            *gitops._with_submodule_flags(args)]
    return _run(argv, _owner_env(), cwd or str(root()), timeout, stdin)


def run_gh(args: list[str], cwd: str, *, stdin: str | None = None) -> tuple[int, str, str]:
    """gh with the owner's login, its git children pinned as gitops pins
    them, run in `cwd` (the mirror) with an explicit `--repo` in `args`."""
    gh = gitops.gh_binary()
    if gh is None:
        return 127, "", "gh (the GitHub CLI) is not installed"
    env = _owner_env()
    env.update(gitops._config_env())
    return _run([gh, *args], env, cwd, gitops.NETWORK_TIMEOUT_S, stdin)


def _scrub(text: str) -> str:
    from .redact import scrub_text
    text = gitops._URL_USERINFO.sub(r"\1[redacted]@", text)
    try:
        return scrub_text(text)
    except Exception:       # noqa: BLE001 — what cannot be scrubbed is withheld
        return "[output withheld: it could not be scrubbed]"


def _render(code: int, out: str, err: str) -> str:
    parts = []
    if out.strip():
        parts.append(out.rstrip())
    if err.strip():
        parts.append(f"[stderr]\n{err.strip()}")
    parts.append(f"[exit {code}]")
    text = _scrub("\n".join(parts))
    return text if len(text) <= OUTPUT_CAP else text[:OUTPUT_CAP] + "\n[truncated]"


def _local_fetch(mirror: Path, target: Target) -> tuple[int, str, str]:
    """Copy the branch from the Codex repository into the mirror. The one
    step that touches a Codex-writable repository: no credential in the
    environment, an empty HOME, no global or system config, hardened."""
    ref = f"refs/heads/{target.branch}"
    return _plain_git(["--git-dir", str(mirror), "fetch", "--no-tags", "--no-write-fetch-head",
                       "--no-recurse-submodules", str(target.common), f"+{ref}:{ref}"])


def _remote_default(mirror: Path, repo: str) -> tuple[bool, str | None]:
    """(read, branch): the remote's HEAD branch. Read but None is a remote
    whose HEAD is unborn (an empty repository): nothing to protect there
    beyond merge_gate's list. Not read refuses the push."""
    code, out, _err = run_git(["--git-dir", str(mirror), "ls-remote", "--symref",
                               push_url(repo), "HEAD"], cwd=str(mirror))
    if code != 0:
        return False, None
    match = re.search(r"^ref:\s*refs/heads/(\S+)\s+HEAD$", out, re.M)
    return True, (match.group(1) if match else None)


# --- the calls ------------------------------------------------------------------------


def execute(tool: str, args: dict) -> str:
    """Run one approved call. The tool functions call this and nothing else."""
    from jarvis import runtime
    approved = take(tool, args)
    if approved is None:
        return ("Refused: this Jarvis git tool runs only as a Codex chat's own call, after "
                "Jarvis's gate approved exactly this call.")
    if runtime.depth() != 0:
        return "Refused: a sub-agent cannot push or change pull requests."
    try:
        fresh = plan(tool, args, approved.brief_cwd)
    except Refused as exc:
        return f"Refused: {exc}"
    if fresh.line != approved.line:
        return ("Refused: the repository changed after the owner approved this call (the "
                "branch or origin is not what the card showed). Call the tool again.")
    target = fresh.target
    values = dict(fresh.args)
    with _lock(project_key(target.common)):
        try:
            mirror = mirror_dir(target.common)
            if tool == "chat_git_fetch":
                return _fetch(mirror, target)
            if tool in ("chat_git_push", "chat_pr_create"):
                pushed = _push(mirror, target)
                if tool == "chat_git_push" or not pushed.startswith("Pushed"):
                    return pushed
                _prog, argv = _commands(tool, values, target)[1]
                code, out, err = run_gh(argv, str(mirror), stdin=values["body"])
                head = "Opened the pull request." if code == 0 else "gh pr create failed."
                return f"{pushed}\n{head}\n{_render(code, out, err)}"
            _prog, argv = _commands(tool, values, target)[0]
            code, out, err = run_gh(argv, str(mirror))
            verb = "merge" if tool == "chat_pr_merge" else "close"
            head = f"gh pr {verb} succeeded." if code == 0 else f"gh pr {verb} failed."
            return f"{head}\n{_render(code, out, err)}"
        except Refused as exc:
            return f"Refused: {exc}"


def _push(mirror: Path, target: Target) -> str:
    code, out, err = _local_fetch(mirror, target)
    if code != 0:
        return "Error: copying the branch into the mirror failed.\n" + _render(code, out, err)
    read, default = _remote_default(mirror, target.repo)
    if not read:
        return ("Error: the remote's default branch could not be read, so the push was not "
                "made.")
    judge_push(target, default)
    pin(target.common, target.repo)
    _prog, argv = _commands("chat_git_push", {}, target)[0]
    argv = ["--git-dir", str(mirror), *argv]
    argv[argv.index(_shown_url(target.repo))] = push_url(target.repo)
    code, out, err = run_git(argv, cwd=str(mirror))
    if code != 0:
        return f"Error: git push failed.\n{_render(code, out, err)}"
    return f"Pushed {target.branch} to {target.repo}.\n{_render(code, out, err)}"


def _fetch(mirror: Path, target: Target) -> str:
    _prog, argv = _commands("chat_git_fetch", {}, target)[0]
    argv = ["--git-dir", str(mirror), *argv]
    argv[argv.index(_shown_url(target.repo))] = push_url(target.repo)
    code, out, err = run_git(argv, cwd=str(mirror))
    if code != 0:
        return f"Error: git fetch failed.\n{_render(code, out, err)}"
    return (f"Fetched {target.repo} into the mirror {mirror}.\nNow run, in your worktree: "
            f"git fetch {shlex.quote(str(mirror))} '+refs/remotes/origin/*:refs/remotes/origin/*'\n"
            + _render(code, out, err))


# --- CG-1: a raw network git or gh from a Codex chat ---------------------------------

_NET_GIT = frozenset({"push", "fetch", "pull", "ls-remote"})
_LOCAL = re.compile(r"(/|\./|\.\./|~|file:)")
_EXPANDS = frozenset("$`*?[{")          # what makes a remote word unsure (`_unsure`)


def raw_remote(command) -> bool:
    """True when the line surely runs `gh`, or git push/fetch/pull/ls-remote
    against a remote that is not a local path. Unsure is False: the normal
    widening ask, never a silent decline — an expansion or a glob where the
    program or remote stands (`$G push`, `git push "$R" main`), a program a
    wrapper may not run at all (`nice --bogus gh …`, where `--bogus` may take
    `gh` as its value: `prog.certain` False), a line too complex to walk."""
    if not isinstance(command, str) or not command.strip():
        return False
    from . import merge_gate
    try:
        for variant in rules._program_variants(command):
            walk = rules._Walk()
            with rules.work_scope(fresh=True):
                walk.line(variant, True, 0)
            for prog in walk.progs:
                if prog.word and prog.certain and _remote_prog(prog, merge_gate):
                    return True
    except rules.TooComplex:
        return False
    return False


def _remote_prog(prog, merge_gate) -> bool:
    stem = rules._basename(str(prog.word)).lower()
    if stem.endswith(".exe"):
        stem = stem[:-4]
    if stem == "gh":
        return True
    if stem == "git-push":
        return _remote_arg(list(prog.args))
    if stem != "git":
        return False
    args = list(prog.args)
    i = 0
    while i < len(args) and str(args[i]).startswith("-"):
        i += 2 if str(args[i]) in merge_gate._GIT_GLOBAL_VALUES else 1
    if i >= len(args) or str(args[i]) not in _NET_GIT:
        return False
    return _remote_arg(args[i + 1:])


def _remote_arg(rest: list) -> bool:
    """Whether the remote the words name is surely not a local path: none
    named (the configured remote), or a literal that is not one. A remote
    only known when the line runs (`"$R"`, `$(…)`, a glob) is unsure."""
    remote = next((a for a in rest if str(a) != "--" and not str(a).startswith("-")), None)
    if remote is None:
        return True
    if _unsure(remote):
        return False
    return not _LOCAL.match(str(remote))


def _unsure(word) -> bool:
    """A word bash will expand or glob (`rules._Word`'s active characters;
    any such character in a word read without that mask)."""
    text = str(word)
    active = getattr(word, "active", None)
    if active is None or len(active) != len(text):
        return any(c in _EXPANDS for c in text)
    return any(on and c in _EXPANDS for c, on in zip(text, active))


__all__ = ["DANGEROUS", "POINTER", "Plan", "Refused", "TOOLS", "Target", "execute", "gate_line",
           "github_repo", "grant", "judge_push", "mirror_dir", "mirrors_root", "pin", "pinned", "plan",
           "raw_remote", "read_target", "run_gh", "run_git", "take", "valid_branch"]
