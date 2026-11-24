"""Build, install and serve the v3 HUD from its own verified build store."""

import os
from pathlib import Path
import re
import shutil
import subprocess

from jarvis import config
from jarvis.v2 import hud_install


CSP = (
    "frame-ancestors 'none'; default-src 'none'; script-src 'self'; "
    "worker-src 'self'; connect-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self' data:; manifest-src 'self'; "
    "base-uri 'none'; form-action 'none'; object-src 'none'"
)

_CONTENT_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".wasm": "application/wasm",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
}

MISSING_HUD = "No intact v3 HUD is installed. Run `jarvis agent install`."


# A name Vite gave a content hash (`index-Ab3xY9_z.js`): the same name is the same bytes in any build.
# The digit or capital tells the eight-character hash from a word (`panel-settings.json`).
_HASHED = re.compile(r"assets/[A-Za-z0-9_.-]*-(?=[a-z_-]*[A-Z0-9])[A-Za-z0-9_-]{8}\.[a-z0-9]+")
_NAME_CAP = 200         # what the installer admits in one name


def root_refusal(root: Path) -> str | None:
    """Why `root` cannot hold the installed v3 HUD, or None: v2's rules, and never v2's own install root
    (or a folder around or inside it)."""
    if why := hud_install.root_refusal(root):
        return why.replace("JARVIS_HUD_DIR", "JARVIS_HUD3_DIR")
    try:
        v2_root = Path(config.HUD_DIR).resolve()
    except (OSError, RuntimeError) as exc:
        return f"Could not resolve the v2 HUD install root: {exc}"
    if root == v2_root or v2_root in root.parents or root in v2_root.parents:
        return "The v3 HUD needs an install root separate from the v2 HUD (JARVIS_HUD3_DIR)."
    return None


def _asset_from_older_build(root: Path, built: hud_install.Build, name: str) -> bytes | None:
    for older_id in hud_install.builds(root):
        # Never from a build newer than the one served: newest() passed it over as broken.
        if older_id < built.id and (older := hud_install.load(root, older_id)) is not None:
            if (data := hud_install.read(older, name)) is not None:
                return data
    return None


def _target(path: str) -> tuple[str, str] | None:
    """(the build file, its Content-Type) for a permitted, exact request path."""
    parts = path.split("/")
    if "%" in path or "\\" in path or "//" in path or any(p in (".", "..") for p in parts):
        return None
    if path == "/":
        return "index.html", "text/html; charset=utf-8"
    if path == "/manifest.webmanifest":
        return path[1:], "application/manifest+json"
    if len(parts) == 3 and parts[0] == "" and parts[1] in ("assets", "icons") and len(parts[2]) <= _NAME_CAP:
        kind = _CONTENT_TYPES.get(Path(parts[2]).suffix)
        return (path[1:], kind) if kind is not None else None
    return None


def static_file(root: Path, path: str) -> tuple[str, bytes] | None:
    """Content-Type and verified bytes for a permitted, exact request path."""
    if (target := _target(path)) is None or root_refusal(root) is not None:
        return None
    name, kind = target
    # An install can prune the build picked here before it is read: pick again, once per kept build.
    for _ in range(hud_install.KEEP + 1):
        built = hud_install.newest(root)
        if built is None:
            return None
        data = hud_install.read(built, name)
        if data is None and name not in built.files and _HASHED.fullmatch(name):
            data = _asset_from_older_build(root, built, name)
        if data is not None:
            return kind, data
        current = hud_install.newest(root)
        if current is None or current.id == built.id:
            return None
    return None


def _dependencies_stale(source: Path) -> bool:
    """Whether `npm ci` must run: no record of an install, or an older one than the lock file."""
    try:
        installed = (source / "node_modules" / ".package-lock.json").stat().st_mtime_ns
    except OSError:
        return True
    try:
        return (source / "package-lock.json").stat().st_mtime_ns > installed
    except OSError:
        return False    # no lock file: npm ci cannot run, and the build will say so


def _npm(npm: str, source: Path, *args: str, timeout: float) -> None:
    command = f"`npm {' '.join(args)}`"
    try:
        result = subprocess.run([npm, *args], cwd=source, stdin=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise hud_install.InstallRefused(
            f"{command} did not finish within {timeout:g} s. No HUD was installed."
        ) from None
    except OSError as exc:
        raise hud_install.InstallRefused(f"Could not run {command}: {exc}") from None
    if result.returncode != 0:
        raise hud_install.InstallRefused(
            f"{command} failed (exit {result.returncode}). No HUD was installed."
        )


def npm_build() -> None:
    """Refresh stale dependencies without install scripts, then build hud3/."""
    source = Path(config.REPO_ROOT) / "hud3"
    npm = hud_install.npm_path()
    if _dependencies_stale(source):
        # The flag outranks any .npmrc: packages never run code at install time (owner, 2026-10-15).
        _npm(npm, source, "ci", "--ignore-scripts", timeout=hud_install.NPM_BUILD_TIMEOUT)
    # Only what this build writes is installed: nothing a previous one left behind.
    dist = Path(config.HUD3_BUILD)
    if dist.is_symlink():
        raise hud_install.InstallRefused(f"{dist} is a symbolic link; the build is never read through one.")
    try:
        if dist.exists():
            shutil.rmtree(dist)
        _npm(npm, source, "run", "build", timeout=hud_install.NPM_BUILD_TIMEOUT)
        built = (dist / "index.html").is_file()
    except OSError as exc:
        raise hud_install.InstallRefused(f"Could not build hud3/: {exc}") from None
    if not built:
        raise hud_install.InstallRefused("`npm run build` wrote no index.html. No HUD was installed.")


def frontend_dirty() -> bool | None:
    """Whether the frontend inputs are dirty, or None when Git cannot say."""
    result = hud_install._git("status", "--porcelain", "--untracked-files=normal", "--", "hud3", "hud/src")
    if result is None or result[0] != 0:
        return None
    if result[1].strip():
        return True
    # npm also reads these files when Git ignores them.
    source = Path(config.REPO_ROOT) / "hud3"
    present = {f"hud3/{name}" for name in hud_install.NPM_FILES if os.path.lexists(source / name)}
    if not present:
        return False
    tracked = hud_install._git("ls-files", "--", *sorted(present))
    if tracked is None or tracked[0] != 0:
        return None
    return set(tracked[1].splitlines()) != present


def _require_clean(dirty: bool | None, allow_dirty: bool) -> None:
    if not allow_dirty and dirty is not False:
        why = ("hud3/ or hud/src has uncommitted changes" if dirty
               else "Git cannot say whether hud3/ and hud/src have uncommitted changes")
        raise hud_install.InstallRefused(f"{why}. Pass --allow-dirty to install anyway.")


def install_hud(*, build=npm_build, allow_dirty: bool = False) -> hud_install.Build:
    """Build the checkout and install it with its Git provenance."""
    root = Path(config.HUD3_DIR)
    if why := root_refusal(root):
        raise hud_install.InstallRefused(why)

    head, dirty = hud_install.git_head(), frontend_dirty()
    _require_clean(dirty, allow_dirty)
    build()
    after_head, after_dirty = hud_install.git_head(), frontend_dirty()
    _require_clean(after_dirty, allow_dirty)
    if after_head != head:
        if not allow_dirty:
            raise hud_install.InstallRefused("The checkout moved during the build. Run the install again.")
        after_head = None       # neither commit is what was built
    if after_dirty or dirty:
        dirty = True
    elif after_dirty is None or dirty is None:
        dirty = None
    return hud_install.install(config.HUD3_BUILD, root, note={"git_head": after_head, "dirty": dirty})


def open_window(out) -> int:
    """Open the v3 window after a successful daemon switch."""
    from jarvis.face import server

    if root_refusal(config.HUD3_DIR) is not None or hud_install.newest(config.HUD3_DIR) is None:
        out(MISSING_HUD)
    try:
        server.launch_window(f"http://localhost:{config.V3_API_PORT}/")
    except (OSError, ValueError) as exc:
        out(f"Could not open the v3 HUD window: {exc}")
        return 1
    return 0
