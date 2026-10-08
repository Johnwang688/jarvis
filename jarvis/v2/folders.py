"""Project folders made or adopted from Discord (B2; decisions D2, O3, O4, O5).

Two functions, and the split between them is the point:

* `check_project_folder(raw)` **only reads.** It turns what the owner typed
  into one absolute path, refuses everything below, and says what is there now:
  nothing (`create`), an empty folder (`adopt`, used without asking — O5), or a
  folder with something in it (`adopt`, asked first, with its entry count and
  whether it is a git repository). It reads names and file types, never a
  file's contents.
* `make_project_folder(check, approval)` **makes at most one directory**:
  exactly one `os.mkdir(path, 0o755)`, no parents, no `exist_ok`, and nothing
  written inside. It re-runs the check first and refuses if anything moved
  since the owner said yes (`FolderChanged`, so the caller asks again).

**Who may call `make_project_folder`.** Only the `/project new` confirmation
handler (`jarvis/v2/discord/project_commands.py`). It is not a tool, not an MCP
tool and not an HTTP route; `tests/v2/folders_check.py` greps the tree to keep
it that way. A model never makes a folder: the folder is made by code, from the
check the owner approved.

*Not here yet (B2b):* `project_propose` — a Claude Sonnet or Opus chat raising
the same `project_folder` confirmation (decisions O3's model rule). It needs
the peers plan's phase 0 (`runtime.caller()` and per-session jarvis-mcp tokens)
to know which chat is asking, so it follows as its own small PR.

**What the owner may type** (plan §5):

* a bare name with no `/` → `config.PROJECT_WORK_DIR/<slug(name)>`
  (`~/jarvis-work/robotics`);
* a path starting `~/` → expanded once, against `$HOME`;
* otherwise an absolute path. No control characters, no `..`, at most 4096
  characters in all and 255 bytes per component.

**What is refused**, lexically *and* on the realpath of the nearest existing
ancestor (so a symlinked parent cannot carry the folder somewhere else):

* anything not strictly below a root in `config.PROJECT_FOLDER_ROOTS`
  (`$HOME`, `/mnt/c/Users/johnw`) — a root itself included;
* `config.PROJECT_WORK_DIR` itself (it holds projects; it is not one);
* any component starting with `.`;
* `config.V2_CREDENTIAL_DIRS`, `config.V2_DATA_DIR`, `config.REPO_ROOT` and
  `/mnt/c/Users/johnw/AppData` — inside them, or a folder that contains them;
* under `/mnt/<drive>`, a name Windows cannot hold (`CON`, `NUL`, `COM1`, …,
  `<>:"\\|?*`, a trailing dot or space);
* a path that is already another project's root;
* a file or a symlink where the folder would be;
* a missing parent (O4): one `mkdir`, never a chain of them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import unicodedata

from jarvis import config

MAX_PATH = 4096
MAX_COMPONENT = 255
APPDATA = "/mnt/c/Users/johnw/AppData"
TOOL = "project_folder"

_WINDOWS_RESERVED = re.compile(
    r"^(con|prn|aux|nul|conin\$|conout\$|com[0-9¹²³]|lpt[0-9¹²³])(\..*)?$", re.IGNORECASE)
_WINDOWS_BAD_CHARS = set('<>:"\\|?*')
_DRIVE = re.compile(r"^/mnt/[A-Za-z](/|$)")


class FolderRefused(ValueError):
    """A folder that can never be a project folder, with the sentence why."""


class FolderChanged(RuntimeError):
    """What is at the path changed after the owner was asked: ask again."""

    def __init__(self, check: "FolderCheck"):
        super().__init__(f"{check.path} changed since you were asked")
        self.check = check


@dataclass(frozen=True)
class FolderCheck:
    raw: str
    path: Path                       # absolute, lexical: what the owner sees
    state: str                       # "missing" | "empty" | "nonempty"
    entries: int = 0                 # names directly inside (nonempty only)
    git: bool = False                # a `.git` entry directly inside
    taken_roots: tuple[str, ...] = field(default=(), compare=False, repr=False)

    @property
    def action(self) -> str:
        return "create" if self.state == "missing" else "adopt"

    @property
    def needs_confirmation(self) -> bool:
        """O5: an existing empty folder is used without asking."""
        return self.state != "empty"

    @property
    def signature(self) -> tuple:
        return (str(self.path), self.state, self.entries, self.git)

    def describe(self) -> str:
        if self.state == "missing":
            return "a new, empty folder"
        if self.state == "empty":
            return "an existing empty folder"
        noun = "entry" if self.entries == 1 else "entries"
        repo = ", a git repository" if self.git else ""
        return f"an existing folder with {self.entries} {noun}{repo}"

    def approval_args(self, name: str) -> dict:
        """What the owner approves. `make_project_folder` checks it again."""
        args = {"action": self.action, "path": str(self.path), "name": name}
        if self.state == "nonempty":
            args["entries"] = self.entries
            args["git"] = self.git
        return args


@dataclass(frozen=True)
class Approved:
    """The owner's yes to one exact `project_folder` request."""
    args: dict


def approved(request, decision) -> Approved:
    """An `Approved` from a broker request the owner allowed, else raises."""
    from .provider import Decision
    if Decision(decision) is not Decision.ALLOW:
        raise PermissionError("the owner did not approve this folder")
    if getattr(request, "tool", None) != TOOL:
        raise PermissionError("not a project folder approval")
    return Approved(dict(request.args or {}))


# -- input -------------------------------------------------------------------------


def folder_slug(name: str) -> str:
    """A folder name from a project name: lowercase ASCII letters, digits, `-`
    and `_`; never starting with a dot; at most 100 characters."""
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9_-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-_")
    return text[:100].strip("-_")


def _control(text: str) -> bool:
    # Cc are control characters; Cf (format: bidi overrides, zero-width
    # joiners) and line/paragraph separators would let a path read one way on
    # the approval and be another on disk.
    return any(unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") for ch in text)


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def _expand(text: str) -> Path:
    return Path(os.path.normpath(os.path.expanduser(text)))


def work_dir() -> Path:
    return _expand(str(config.PROJECT_WORK_DIR))


def roots() -> list[Path]:
    return [_expand(str(r)) for r in config.PROJECT_FOLDER_ROOTS if str(r).strip()]


def _protected() -> list[Path]:
    out = [_expand(d) for d in config.V2_CREDENTIAL_DIRS]
    out += [Path(os.path.normpath(str(config.V2_DATA_DIR))),
            Path(os.path.normpath(str(config.REPO_ROOT))), Path(APPDATA)]
    return out


def resolve_input(raw) -> Path:
    """What the owner typed -> one absolute, normalised path, or FolderRefused."""
    if not isinstance(raw, str):
        raise FolderRefused("the folder must be text")
    text = raw.strip()
    if not text:
        raise FolderRefused("no folder was given")
    if _control(text):
        raise FolderRefused("the folder contains a control character")
    if len(text) > MAX_PATH:
        raise FolderRefused(f"the folder is longer than {MAX_PATH} characters")
    if "/" not in text and not text.startswith("~"):
        slug = folder_slug(text)
        if not slug:
            raise FolderRefused(f"`{text[:60]}` has no letters or digits to name a folder "
                                "after; give a full path instead")
        return work_dir() / slug
    if text.startswith("~"):
        if not text.startswith("~/"):
            raise FolderRefused("only a leading `~/` is expanded")
        text = str(_home()).rstrip("/") + "/" + text[2:]
    if not text.startswith("/"):
        raise FolderRefused("the folder must be an absolute path, start with `~/`, or be "
                            "a bare name")
    parts = [p for p in text.split("/") if p]
    if any(p in ("..", ".") for p in parts):
        raise FolderRefused("the folder may not contain `.` or `..`")
    if not parts:
        raise FolderRefused("`/` cannot be a project folder")
    for part in parts:
        if len(part.encode("utf-8")) > MAX_COMPONENT:
            raise FolderRefused(f"a folder name is longer than {MAX_COMPONENT} bytes")
    path = Path("/" + "/".join(parts))
    if len(str(path)) > MAX_PATH:
        raise FolderRefused(f"the folder is longer than {MAX_PATH} characters")
    return path


# -- checks ------------------------------------------------------------------------


def _within(path: Path, root: Path, *, casefold: bool = False) -> bool:
    """`path` is `root` or below it."""
    a, b = path.parts, root.parts
    if casefold:
        a, b = tuple(x.casefold() for x in a), tuple(x.casefold() for x in b)
    return a[:len(b)] == b


def _on_drive(path: Path) -> bool:
    return bool(_DRIVE.match(str(path)))


def _refuse_path(path: Path, how: str) -> None:
    """Every rule that judges a path by its name. `how` says which spelling
    (as typed, or as the filesystem resolves it) failed, for the message."""
    for part in path.parts[1:]:
        if part.startswith("."):
            raise FolderRefused(f"`{part}` is a hidden folder{how}; project folders may not "
                                "be or sit inside one")
    under = [r for r in roots() if r != path and _within(path, r) and len(path.parts) > len(r.parts)]
    if not under:
        if any(r == path for r in roots()):
            raise FolderRefused(f"{path} is a root{how}, not a folder inside one")
        allowed = " or ".join(f"`{r}`" for r in roots())
        raise FolderRefused(f"{path} is not inside {allowed}{how}")
    work = work_dir()
    if path == work:
        raise FolderRefused(f"{work} holds project folders; it is not one itself")
    drive = _on_drive(path)
    for protected in _protected():
        folded = drive or _on_drive(protected)
        if _within(path, protected, casefold=folded) or _within(protected, path, casefold=folded):
            raise FolderRefused(f"{path} is or holds Jarvis's own code, data or credentials "
                                f"({protected}){how}")
    if drive:
        for part in path.parts[3:]:
            if _WINDOWS_RESERVED.match(part) or _WINDOWS_RESERVED.match(part.split(".")[0]):
                raise FolderRefused(f"`{part}` is a name Windows reserves")
            if set(part) & _WINDOWS_BAD_CHARS or part.endswith((".", " ")) \
                    or any(ord(c) < 32 for c in part):
                raise FolderRefused(f"`{part}` is not a name Windows can hold")


def _nearest_existing(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if os.path.lexists(candidate):
            return candidate
    return Path("/")


def _real(path: Path) -> Path:
    """The path as the filesystem would resolve it: the realpath of its
    nearest existing ancestor, with the rest appended."""
    anchor = _nearest_existing(path)
    rest = path.parts[len(anchor.parts):]
    return Path(os.path.realpath(anchor)).joinpath(*rest)


def _same(a: str, b: Path) -> bool:
    try:
        other = Path(os.path.normpath(a))
    except (TypeError, ValueError):
        return False
    return other == b or _real(other) == _real(b)


def check_project_folder(raw, *, taken_roots=()) -> FolderCheck:
    """Read-only. -> FolderCheck, or FolderRefused with the sentence to show.

    `taken_roots` is every existing project's root; a path that is already one
    is refused."""
    path = resolve_input(raw)
    _refuse_path(path, "")
    real = _real(path)
    if real != path:
        _refuse_path(real, f" (it resolves to {real})")
    taken = tuple(str(r) for r in taken_roots if r)
    for root in taken:
        if _same(root, path):
            raise FolderRefused(f"{path} is already a project's folder")
    parent = path.parent
    if not os.path.isdir(parent):
        if parent == work_dir():
            raise FolderRefused(f"{parent} does not exist yet; make it once by hand (Jarvis "
                                "makes one folder, never its parents)")
        raise FolderRefused(f"the parent folder {parent} does not exist; Jarvis makes one "
                            "folder, never its parents")
    if os.path.islink(path):
        raise FolderRefused(f"{path} is a symlink; use the folder it points to")
    if os.path.lexists(path) and not os.path.isdir(path):
        raise FolderRefused(f"{path} is a file, not a folder")
    if not os.path.lexists(path):
        return FolderCheck(str(raw), path, "missing", taken_roots=taken)
    try:
        with os.scandir(path) as listing:
            names = [entry.name for entry in listing]
    except OSError as exc:
        raise FolderRefused(f"{path} cannot be listed ({type(exc).__name__})") from None
    if not names:
        return FolderCheck(str(raw), path, "empty", taken_roots=taken)
    return FolderCheck(str(raw), path, "nonempty", entries=len(names),
                       git=".git" in names, taken_roots=taken)


def make_project_folder(check: FolderCheck, approval: Approved | None) -> Path:
    """Make (or adopt) the folder `check` describes. -> its path.

    Re-checks first: anything different from what was approved raises
    `FolderChanged`. Asking is required unless the folder is an existing empty
    one (O5). At most one `os.mkdir`, with no parents and no `exist_ok`;
    nothing is ever written inside."""
    fresh = check_project_folder(check.raw, taken_roots=check.taken_roots)
    if fresh.signature != check.signature:
        raise FolderChanged(fresh)
    if fresh.needs_confirmation:
        if not isinstance(approval, Approved):
            raise PermissionError("this folder needs the owner's yes first")
        args = approval.args
        if args.get("path") != str(fresh.path) or args.get("action") != fresh.action \
                or fresh.approval_args(str(args.get("name", ""))) != args:
            raise FolderChanged(fresh)
    if fresh.state != "missing":
        return fresh.path
    try:
        os.mkdir(fresh.path, 0o755)
    except FileExistsError:
        raise FolderChanged(check_project_folder(check.raw, taken_roots=check.taken_roots)) \
            from None
    except FileNotFoundError:
        raise FolderRefused(f"the parent folder {fresh.path.parent} disappeared") from None
    return fresh.path


__all__ = ["FolderCheck", "FolderRefused", "FolderChanged", "Approved", "approved",
           "check_project_folder", "make_project_folder", "folder_slug", "resolve_input"]
