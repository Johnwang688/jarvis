"""Downloads and uploads for the owner's Browser (BR-1b; plan §3 "Downloads",
decisions B-6, amendment 1 §B.3 H8 and §B.4).

**Downloads.** Chrome saves a download into the profile's private
`downloads/<profile>/incoming/` (Playwright's own name); when it finishes it
is copied — by file descriptor, never through a link — to
`downloads/<profile>/<id>/<cleaned name>`, mode 0600, in a folder of its own
(0700), and Playwright's copy is deleted. `BROWSER_DIR` is a credential
folder, so no tool reads it. The window hears `{t:"download", …}` on the
tab's socket; `GET /browser/downloads/<id>` serves the file to the owner's
window only, as an attachment (`application/octet-stream`, `nosniff`, a
policy that runs nothing), so a downloaded page can never render on the
HUD's origin. A Private download is removed when the Private Chrome stops.

**Cleaned names** (`clean_name`): the last path component only; controls,
format and invisible characters, and every character Windows refuses
(`<>:"/\\|?*`) gone or replaced; no leading dot (no hidden file) and no
trailing dot or space; NFC; at most 120 characters, keeping the extension;
`download` when nothing is left. The page chose the name; it never names a
path here, since the file is always one level inside its own folder.

**An executable download is held** (amendment §B.4): by its name
(`act_rules.executable_download`, the one rule) or by its first bytes (`MZ`,
ELF, `#!`). It is saved like any other but never served: the card that lets
the owner release it is BR-9b's, so for now it stays held — said in the
listing and on the socket — and the owner can delete it.

**Uploads** (`upload_payloads`): only through the owner-only files route,
answering a file chooser the page opened, with files the owner chose by
project and path. Each is judged by BR-9a's H8 rule, called and not copied
(`act_rules.upload_refusal`: inside the project, no symlink out, no hidden
component, no credential or protected state, one name on disk, at most
50 MB), then read through a descriptor opened `O_NOFOLLOW` that must be the
very file judged (its realpath and its inode), and handed to Chrome as bytes
— so a swap after the judgement uploads nothing else. At most 16 files and
50 MB in all.
"""

from __future__ import annotations

import os
import re
import stat
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from jarvis import config, untrusted

NAME_CAP = 120
DOWNLOAD_ID = re.compile(r"[0-9a-f]{8}")
INCOMING = "incoming"
MAX_UPLOAD_FILES = 16
COPY_CHUNK = 1024 * 1024
_WINDOWS = set('<>:"/\\|?*')
_MAGIC = (b"MZ", b"\x7fELF", b"#!")


class FileRefused(Exception):
    """A status and a fixed sentence; never a path or a page's text."""

    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status


def clean_name(raw) -> str:
    """A page-chosen file name, safe to keep and to show (module note)."""
    text = raw if isinstance(raw, str) else ""
    text = text.replace("\\", "/").rsplit("/", 1)[-1]
    text = unicodedata.normalize("NFC", text)
    out = []
    for ch in text:
        if untrusted.unseen(ch) or ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F:
            continue
        out.append("_" if ch in _WINDOWS else ch)
    name = " ".join("".join(out).split())
    name = name.lstrip(". ").rstrip(". ")
    if len(name) > NAME_CAP:
        stem, dot, ext = name.rpartition(".")
        if dot and 0 < len(ext) <= 16 and stem:
            name = stem[: NAME_CAP - len(ext) - 1].rstrip(". ") + "." + ext
        else:
            name = name[:NAME_CAP].rstrip(". ")
    return name or "download"


def executable(name: str, head: bytes = b"") -> bool:
    """Held until the owner's card (amendment §B.4): by name or first bytes."""
    from .act_rules import executable_download
    return executable_download(name) or head.startswith(_MAGIC)


def _root() -> Path | None:
    """`BROWSER_DIR/downloads`, held to the engine's own folder rule (0700,
    ours, no symlink) before anything in it is read; None before the
    Browser ever ran (and nothing is made here)."""
    from .host import BrowserRefused, secure_dir
    downloads = config.BROWSER_DIR / "downloads"
    if not os.path.lexists(config.BROWSER_DIR) or not os.path.lexists(downloads):
        return None
    try:
        secure_dir(config.BROWSER_DIR, create=False)
        return secure_dir(downloads, create=False)
    except BrowserRefused as refusal:
        raise FileRefused(409, str(refusal)) from None


def _profile_dirs() -> list[Path]:
    """`downloads/<profile>/` folders, never through a symlink."""
    root = _root()
    if root is None:
        return []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return []
    return [Path(e.path) for e in entries if e.is_dir(follow_symlinks=False)]


def _entry(folder: Path) -> Path | None:
    """The one regular file in a download's own folder."""
    try:
        files = [e for e in os.scandir(folder) if not e.name.startswith(".")]
    except OSError:
        return None
    if len(files) != 1 or not files[0].is_file(follow_symlinks=False):
        return None
    return Path(files[0].path)


def _find(download_id) -> tuple[str, Path] | None:
    if not isinstance(download_id, str) or not DOWNLOAD_ID.fullmatch(download_id):
        raise FileRefused(400, "a download id is eight lowercase hex characters")
    for profile in _profile_dirs():
        folder = profile / download_id
        try:
            if stat.S_ISDIR(os.lstat(folder).st_mode):
                return profile.name, folder
        except OSError:
            continue
    return None


def _head(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return b""
    try:
        return os.read(fd, 8)
    finally:
        os.close(fd)


def listing() -> list[dict]:
    """Every saved download, newest first: `{id, profile, name, size, held, at}`."""
    rows = []
    for profile in _profile_dirs():
        try:
            entries = list(os.scandir(profile))
        except OSError:
            continue
        for entry in entries:
            if not DOWNLOAD_ID.fullmatch(entry.name) or not entry.is_dir(follow_symlinks=False):
                continue
            path = _entry(Path(entry.path))
            if path is None:
                continue
            try:
                info = os.lstat(path)
            except OSError:
                continue
            rows.append({"id": entry.name, "profile": profile.name, "name": path.name,
                         "size": info.st_size, "held": executable(path.name, _head(path)),
                         "at": datetime.fromtimestamp(info.st_mtime, timezone.utc)
                         .isoformat(timespec="seconds")})
    rows.sort(key=lambda r: r["at"], reverse=True)
    return rows


def open_download(download_id) -> tuple[int, str, int]:
    """(fd, name, size) of a saved download to serve; `FileRefused` otherwise."""
    found = _find(download_id)
    path = _entry(found[1]) if found else None
    if path is None:
        raise FileRefused(404, "no such download")
    if executable(path.name, _head(path)):
        raise FileRefused(409, "this download is an executable and is held until you approve it "
                               "(the card for that is not built yet); delete it instead")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(fd)
        raise FileRefused(404, "no such download")
    return fd, path.name, info.st_size


def delete(download_id) -> dict:
    found = _find(download_id)
    if found is None:
        raise FileRefused(404, "no such download")
    from .host import _remove_tree
    _remove_tree(found[1])
    return {"id": download_id, "deleted": True}


def save(source: str, profile: str, name: str, download_id: str) -> dict:
    """Copy a finished download into its own folder (`download_id`, made
    here and never one that exists), 0600, under its cleaned name. Returns
    `{id, name, size, held}`."""
    from .host import secure_dir
    if not DOWNLOAD_ID.fullmatch(download_id):
        raise FileRefused(400, "a download id is eight lowercase hex characters")
    folder_root = secure_dir(config.BROWSER_DIR / "downloads" / profile)
    folder = folder_root / download_id
    os.mkdir(folder, 0o700)                     # FileExistsError: never into another's folder
    secure_dir(folder, create=False)
    clean = clean_name(name)
    src = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(src).st_mode):
            raise FileRefused(409, "the download is not a file")
        dst = os.open(folder / clean, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        size, head = 0, b""
        try:
            while True:
                chunk = os.read(src, COPY_CHUNK)
                if not chunk:
                    break
                if not head:
                    head = chunk[:8]
                view = memoryview(chunk)
                while view:
                    view = view[os.write(dst, view):]
                size += len(chunk)
            os.fsync(dst)
        finally:
            os.close(dst)
    finally:
        os.close(src)
    return {"id": download_id, "name": clean, "size": size, "held": executable(clean, head)}


# -- uploads -------------------------------------------------------------------------------

def upload_payloads(daemon, files) -> list[dict]:
    """Playwright file payloads for `[{project, path}, …]` the owner chose
    (module note). `FileRefused` with H8's own sentence otherwise."""
    from .act_rules import MAX_UPLOAD_BYTES, upload_refusal
    if not isinstance(files, list) or len(files) > MAX_UPLOAD_FILES:
        raise FileRefused(400, f"files must be a list of at most {MAX_UPLOAD_FILES}")
    out, total = [], 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"project", "path"} \
                or not isinstance(item["project"], str) or not isinstance(item["path"], str):
            raise FileRefused(400, 'each file is {"project": id, "path": "relative/path"}')
        project = (daemon.stores.projects.get(item["project"])
                   if DOWNLOAD_ID.fullmatch(item["project"]) else None)   # a store id: 8 hex
        if project is None or getattr(project, "archived", False):
            raise FileRefused(404, "no such project")
        if os.path.isabs(item["path"]) or item["path"].startswith("~"):
            raise FileRefused(400, "a file's path is relative to its project")
        why, resolved = upload_refusal(item["path"], project.root)
        if why is not None:
            raise FileRefused(403, why)
        data = _read_judged(resolved, MAX_UPLOAD_BYTES)
        total += len(data)
        if total > MAX_UPLOAD_BYTES:
            raise FileRefused(413, "the files to upload are larger than 50 MB in all")
        import mimetypes
        mime = mimetypes.guess_type(resolved)[0] or "application/octet-stream"
        out.append({"name": os.path.basename(resolved), "mimeType": mime, "buffer": data})
    return out


def _read_judged(resolved: str, cap: int) -> bytes:
    """The bytes of exactly the file judged: opened `O_NOFOLLOW`, a regular
    file with one name, no larger than `cap`, and still the path judged
    (its realpath through the descriptor)."""
    refused = FileRefused(409, "the file changed after it was checked; choose it again")
    try:
        fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise refused from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > cap:
            raise refused
        if os.path.realpath(f"/proc/self/fd/{fd}") != resolved:
            raise refused
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, COPY_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > cap:
                raise refused
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)
