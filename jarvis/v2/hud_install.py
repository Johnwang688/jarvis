"""The installed HUD (decision C-1; editor plan §2.7, ED-4).

The HUD's script runs with the HUD's own Origin, the one request
`POST /approvals` accepts, so whatever the daemon serves as the HUD is a
program with the power to approve. It used to be the repo's `hud/dist`,
which any agent working on Jarvis could write. Now the daemon serves an
**installed copy**:

- `jarvis hud install` — a CLI the owner or the lead runs, never a tool —
  builds `hud/` (`npm run build`) and copies `hud/dist` into
  `config.HUD_DIR/<build-id>/`, files 0444 and folders 0555, beside a
  `manifest.json` of every file's SHA-256 and size. It is written into a
  temporary folder in the install root and renamed into place, so a crash
  never leaves half a build to serve, and the oldest builds past `KEEP` are
  pruned.
- The daemon serves the **newest** build (`newest`), and only a file its
  manifest lists, read without following a link, whose size and SHA-256
  still match. An asset the newest build lacks is looked for in the other
  kept builds (Vite names them by content hash, so the same name is the same
  bytes): a window loaded before an install still finds its lazily loaded
  chunks, so an install needs no daemon restart — a reload shows it.
- `config.HUD_DIST` is the install root, so BR-0's one write check
  (`files._hud_build`, `permissions.denied_file`/`denied_grant`) refuses every
  agent write tool and every v2 grant there, including a build planted beside
  the real ones.

Stated plainly: a *shell* command can still write the install root (the
accepted shell gap of 2026-10-08) — including a whole new build with a
manifest to match. This narrows the exposure; it does not close it. What it
buys: an agent editing Jarvis's source no longer edits the running approval
window, and a change reaches the window only when someone runs the install.

Refused, with a sentence: an install root (or any folder above it) that is a
symbolic link, one inside the repository, one that is not a folder, one
another user can write; a build output holding a link, a special file, a
name outside `[A-Za-z0-9._-]`, or more than the caps allow.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time

from jarvis import config

LOG = logging.getLogger(__name__)

MANIFEST = "manifest.json"
FORMAT = 1
KEEP = 3                                   # builds kept after an install
FILE_CAP = 64 * 1024 * 1024                # one file
TOTAL_CAP = 512 * 1024 * 1024              # one build
FILES_CAP = 5000                           # files in one build
MANIFEST_CAP = 4 * 1024 * 1024
_BUILD = re.compile(r"^\d{8}T\d{12}Z-[0-9a-f]{12}$")
_PART = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,199}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")


class InstallRefused(Exception):
    """An install that must not happen; the message is the sentence to print."""


@dataclass(frozen=True)
class Build:
    id: str
    path: Path
    files: dict           # posix relative path -> (sha256, size)


# -- the install root ---------------------------------------------------------------


def root_refusal(root) -> str | None:
    """Why `root` cannot hold the installed HUD, or None.

    Every existing folder from `/` down to `root` is looked at with lstat: a
    symbolic link anywhere on the way means the build would land (or be read)
    somewhere other than the path the owner sees, possibly a folder an agent
    can write. Inside the repository is refused because that is where agents
    work. An existing root must be a folder this user owns that no other user
    can write."""
    try:
        root = Path(root)
    except TypeError:
        return "the HUD install folder is not a path"
    if not root.is_absolute():
        return f"the HUD install folder {root} is not an absolute path"
    if ".." in root.parts:
        return f"the HUD install folder {root} has a '..' in it"
    repo = Path(config.REPO_ROOT)
    for candidate in (root, Path(os.path.abspath(root))):
        if candidate == repo or repo in candidate.parents:
            return (f"the HUD install folder {root} is inside the repository ({repo}), "
                    "which agents can write; install outside it")
    walked = Path(root.anchor)
    for part in root.parts[1:]:
        walked = walked / part
        try:
            info = os.lstat(walked)
        except FileNotFoundError:
            break                           # the rest will be created
        except OSError as exc:
            return f"cannot check {walked}: {exc.strerror or type(exc).__name__}"
        if stat.S_ISLNK(info.st_mode):
            return (f"{walked} is a symbolic link; the HUD is never installed through one "
                    "(set JARVIS_HUD_DIR to a real folder)")
        if not stat.S_ISDIR(info.st_mode):
            return f"{walked} is not a folder"
        if walked == root:
            if info.st_uid != os.getuid():
                return f"{root} belongs to another user"
            if info.st_mode & 0o022:
                return f"{root} can be written by other users (mode {stat.S_IMODE(info.st_mode):o})"
    return None


# -- reading the build output --------------------------------------------------------


def _read_regular(path: Path, cap: int) -> bytes:
    """A regular file's bytes, never through a link at the last component."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise InstallRefused(f"{path} is not a regular file")
        if info.st_size > cap:
            raise InstallRefused(f"{path} is larger than {cap} bytes")
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > cap:
                raise InstallRefused(f"{path} is larger than {cap} bytes")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def collect(source) -> list[tuple[str, bytes]]:
    """Every file of a finished build (`hud/dist`), as (relative path, bytes).

    Refuses a source that is a link or has no `index.html`, and any link,
    special file or oddly named entry inside it."""
    source = Path(source)
    try:
        info = os.lstat(source)
    except OSError:
        raise InstallRefused(f"{source} does not exist; build the HUD first") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise InstallRefused(f"{source} is not a real folder")
    out: list[tuple[str, bytes]] = []
    total = 0
    for here, dirs, files in os.walk(source, followlinks=False):
        here = Path(here)
        for name in sorted(dirs + files):
            path = here / name
            rel = path.relative_to(source).as_posix()
            if not _PART.match(name):
                raise InstallRefused(f"refusing to install {rel!r}: an unexpected name")
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode):
                raise InstallRefused(f"refusing to install {rel}: it is a symbolic link")
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                raise InstallRefused(f"refusing to install {rel}: not a regular file")
            if rel == MANIFEST:
                raise InstallRefused(f"refusing to install {rel}: the name is the manifest's")
            data = _read_regular(path, FILE_CAP)
            total += len(data)
            out.append((rel, data))
            if len(out) > FILES_CAP or total > TOTAL_CAP:
                raise InstallRefused(f"{source} is larger than one HUD build may be")
        dirs.sort()
    if not any(rel == "index.html" for rel, _ in out):
        raise InstallRefused(f"{source} has no index.html; build the HUD first")
    return sorted(out)


# -- installing ----------------------------------------------------------------------


def _build_id(entries: list[tuple[str, str]], now: float) -> str:
    digest = hashlib.sha256("\n".join(f"{rel}\0{sha}" for rel, sha in entries).encode()).hexdigest()
    micro = int(round((now % 1) * 1_000_000)) % 1_000_000
    return time.strftime("%Y%m%dT%H%M%S", time.gmtime(now)) + f"{micro:06d}Z-{digest[:12]}"


def _make_writable(path: Path) -> None:
    for here, dirs, _files in os.walk(path, followlinks=False):
        os.chmod(here, 0o755)
        for name in dirs:
            child = Path(here) / name
            if not os.path.islink(child):
                os.chmod(child, 0o755)


def _remove(path: Path) -> None:
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return                    # never ours; never served either
    _make_writable(path)
    shutil.rmtree(path)


def install(source, root, *, keep: int = KEEP, now: float | None = None,
            note: dict | None = None) -> Build:
    """Copy a finished build from `source` into a new build under `root`."""
    root = Path(root)
    why = root_refusal(root)
    if why:
        raise InstallRefused(why)
    files = collect(source)
    root.mkdir(parents=True, exist_ok=True, mode=0o755)
    why = root_refusal(root)                       # created, or raced: look again
    if why:
        raise InstallRefused(why)
    tmp = Path(tempfile.mkdtemp(prefix=".install-", dir=root))
    try:
        entries: list[tuple[str, str]] = []
        listed: dict[str, dict] = {}
        for rel, data in files:
            target = tmp / rel
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            sha = hashlib.sha256(data).hexdigest()
            entries.append((rel, sha))
            listed[rel] = {"sha256": sha, "size": len(data)}
        moment = time.time() if now is None else now
        build_id = _build_id(entries, moment)
        latest = builds(root)
        if latest and latest[0] >= build_id:
            # The newest build is the one served; one that sorted below an
            # older install would never be.
            raise InstallRefused(f"the clock is behind the newest installed build ({latest[0]})")
        manifest = {"format": FORMAT, "build": build_id,
                    "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment)),
                    "source": str(source), **(note or {}), "files": listed}
        fd = os.open(tmp / MANIFEST, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "w") as handle:
            json.dump(manifest, handle, indent=1, sort_keys=True)
        for here, dirs, names in os.walk(tmp, topdown=False):
            for name in names:
                os.chmod(Path(here) / name, 0o444)
            for name in dirs:
                os.chmod(Path(here) / name, 0o555)
        os.chmod(tmp, 0o555)
        final = root / build_id
        if os.path.lexists(final):
            raise InstallRefused(f"{final} already exists")
        os.rename(tmp, final)
    except BaseException:
        if os.path.lexists(tmp):
            _remove(tmp)
        raise
    prune(root, keep=keep, protect=build_id)
    built = load(root, build_id)
    if built is None:
        raise InstallRefused(f"the new build {build_id} does not read back; nothing is served from it")
    return built


def prune(root, *, keep: int = KEEP, protect: str | None = None) -> list[str]:
    """Remove builds past the newest `keep` (never `protect`) and stale temp folders."""
    root = Path(root)
    removed = []
    for name in builds(root)[max(keep, 1):]:
        if name != protect:
            _remove(root / name)
            removed.append(name)
    for entry in os.listdir(root):
        if entry.startswith(".install-") and entry != protect:
            path = root / entry
            try:
                if time.time() - os.lstat(path).st_mtime > 3600:
                    _remove(path)
            except OSError:
                pass
    return removed


# -- serving -------------------------------------------------------------------------


def builds(root) -> list[str]:
    """The installed build ids, newest first (real folders with a build's name)."""
    root = Path(root)
    try:
        names = os.listdir(root)
    except OSError:
        return []
    found = []
    for name in names:
        if not _BUILD.match(name):
            continue
        try:
            if stat.S_ISDIR(os.lstat(root / name).st_mode):
                found.append(name)
        except OSError:
            continue
    return sorted(found, reverse=True)


def load(root, name: str) -> Build | None:
    """One build's manifest, checked; None if it is missing or malformed."""
    path = Path(root) / name
    try:
        if not _BUILD.match(name) or not stat.S_ISDIR(os.lstat(path).st_mode):
            return None
        raw = json.loads(_read_regular(path / MANIFEST, MANIFEST_CAP))
    except (OSError, ValueError, InstallRefused):
        return None
    if not isinstance(raw, dict) or raw.get("format") != FORMAT or raw.get("build") != name:
        return None
    listed = raw.get("files")
    if not isinstance(listed, dict) or "index.html" not in listed:
        return None
    files = {}
    for rel, meta in listed.items():
        if (not isinstance(rel, str) or not all(_PART.match(p) for p in rel.split("/"))
                or rel == MANIFEST or not isinstance(meta, dict)):
            return None
        sha, size = meta.get("sha256"), meta.get("size")
        if not isinstance(sha, str) or not _SHA.match(sha) or type(size) is not int or size < 0:
            return None
        files[rel] = (sha, size)
    return Build(name, path, files)


def newest(root) -> Build | None:
    """The build the daemon serves: the newest one whose manifest reads."""
    if root_refusal(root):
        return None
    for name in builds(root):
        built = load(root, name)
        if built is not None:
            return built
    return None


_verified: dict[tuple, bool] = {}
_verified_lock = threading.Lock()


def read(built: Build, rel: str) -> bytes | None:
    """A file of `built`, only if its manifest lists it and it still matches.

    No link is followed anywhere inside the build; a file whose size or
    SHA-256 differs from the manifest is refused (and logged)."""
    meta = built.files.get(rel)
    if meta is None:
        return None
    path = built.path / rel
    if os.path.realpath(path) != str(path):
        LOG.warning("HUD build %s: %s goes through a link; refused", built.id, rel)
        return None
    try:
        data = _read_regular(path, FILE_CAP)
        info = os.stat(path, follow_symlinks=False)
    except (OSError, InstallRefused):
        return None
    sha, size = meta
    key = (str(path), info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, sha)
    with _verified_lock:
        known = _verified.get(key)
    if known is None:
        known = len(data) == size and hashlib.sha256(data).hexdigest() == sha
        with _verified_lock:
            if len(_verified) > 4096:
                _verified.clear()
            _verified[key] = known
    if not known or len(data) != size:
        LOG.warning("HUD build %s: %s does not match its manifest; refused", built.id, rel)
        return None
    return data


def find(root, rel: str) -> bytes | None:
    """An asset from the newest build, else from an older kept build that has it."""
    if root_refusal(root):
        return None
    for name in builds(root):
        built = load(root, name)
        if built is not None and rel in built.files:
            data = read(built, rel)
            if data is not None:
                return data
    return None


# -- the CLI -------------------------------------------------------------------------


def _git_head() -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(config.REPO_ROOT), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    head = out.stdout.strip()
    return head if out.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", head) else None


def npm_build() -> None:
    """`npm run build` in the repo's `hud/` (type check, then Vite)."""
    hud = Path(config.REPO_ROOT) / "hud"
    if not (hud / "node_modules").is_dir():
        raise InstallRefused(f"{hud}/node_modules is missing; run `npm ci` in {hud} first")
    npm = shutil.which("npm")
    if not npm or npm.startswith("/mnt/"):
        raise InstallRefused("npm is not on PATH (a Windows npm under /mnt/ is never used)")
    result = subprocess.run([npm, "run", "build"], cwd=hud, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise InstallRefused(f"`npm run build` failed (exit {result.returncode}); nothing was installed")


def main(*, build=npm_build, out=print) -> int:
    """`jarvis hud install`: build the HUD and install it for the daemon to serve."""
    root = Path(config.HUD_DIR)
    try:
        why = root_refusal(root)
        if why:
            raise InstallRefused(why)
        build()
        installed = install(config.HUD_BUILD, root, note={"git_head": _git_head()})
    except InstallRefused as exc:
        out(f"jarvis hud install: refused: {exc}")
        return 1
    out(f"Installed HUD build {installed.id} ({len(installed.files)} files) in {installed.path}.")
    if config.HUD_DEV:
        out("Note: JARVIS_HUD_DEV=1 is set here, so a daemon started from this shell would "
            "serve hud/dist, not this install.")
    out("The daemon serves the newest installed build: reload the HUD window to load it. "
        "Restart the daemon only if Python changed.")
    return 0
