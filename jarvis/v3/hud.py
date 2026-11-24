"""Build, install and serve the v3 HUD from its own verified build store."""

import os
from pathlib import Path
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


def _asset_from_older_build(root: Path, built: hud_install.Build, name: str) -> bytes | None:
    names = hud_install.builds(root)
    if names and names[0] == built.id:
        return hud_install.find(root, name)
    # find() searches every build, including any newer ones with broken indexes.
    for older_id in names:
        if older_id >= built.id:
            continue
        older = hud_install.load(root, older_id)
        if older is not None and (data := hud_install.read(older, name)) is not None:
            return data
    return None


def static_file(root: Path, path: str) -> tuple[str, bytes] | None:
    """Content-Type and verified bytes for a permitted, exact request path."""
    parts = path.split("/")
    if "%" in path or "\\" in path or "//" in path or any(p in (".", "..") for p in parts):
        return None
    if path == "/":
        name, kind = "index.html", "text/html; charset=utf-8"
    elif path == "/manifest.webmanifest":
        name, kind = path[1:], "application/manifest+json"
    elif len(parts) == 3 and parts[0] == "" and parts[1] in ("assets", "icons"):
        name, kind = path[1:], _CONTENT_TYPES.get(Path(parts[2]).suffix)
        if kind is None:
            return None
    else:
        return None

    built = hud_install.newest(root)
    if built is None:
        return None
    data = hud_install.read(built, name)
    if data is None and name.startswith("assets/") and name not in built.files:
        data = _asset_from_older_build(root, built, name)
    return (kind, data) if data is not None else None


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
    _npm(npm, source, "run", "build", timeout=hud_install.NPM_BUILD_TIMEOUT)


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
    if why := hud_install.root_refusal(root):
        raise hud_install.InstallRefused(why)
    try:
        v2_root = Path(config.HUD_DIR).resolve()
    except (OSError, RuntimeError) as exc:
        raise hud_install.InstallRefused(f"Could not resolve the v2 HUD install root: {exc}") from None
    if root == v2_root or v2_root in root.parents or root in v2_root.parents:
        raise hud_install.InstallRefused("The v3 HUD needs an install root separate from the v2 HUD.")

    head, dirty = hud_install.git_head(), frontend_dirty()
    _require_clean(dirty, allow_dirty)
    build()
    after_head, after_dirty = hud_install.git_head(), frontend_dirty()
    _require_clean(after_dirty, allow_dirty)
    if after_head != head and not allow_dirty:
        raise hud_install.InstallRefused("The checkout moved during the build. Run the install again.")
    if after_dirty or dirty:
        dirty = True
    elif after_dirty is None or dirty is None:
        dirty = None
    return hud_install.install(config.HUD3_BUILD, root, note={"git_head": after_head, "dirty": dirty})


def open_window(out) -> int:
    """Open the v3 window after a successful daemon switch."""
    from jarvis.face import server

    if hud_install.newest(config.HUD3_DIR) is None:
        out(MISSING_HUD)
    try:
        server.launch_window(f"http://localhost:{config.V3_API_PORT}/")
    except (OSError, ValueError) as exc:
        out(f"Could not open the v3 HUD window: {exc}")
        return 1
    return 0
