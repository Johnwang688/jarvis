"""Where a permanent delete goes: a trash, never straight to unlink (decisions B2).

Two destinations, chosen by where the thing lives:

- **A Windows path** (`/mnt/<drive>/…`) goes to the Windows Recycle Bin,
  through PowerShell's `Microsoft.VisualBasic.FileIO.FileSystem` with
  `SendToRecycleBin`. The path never appears in the script as text: it is
  carried as base64 of its UTF-8 bytes and decoded inside the script, so no
  character in it — a quote, a curly quote PowerShell also reads as one, a
  `$` or a backtick — is ever parsed as code.
- **Anything else** goes to the freedesktop.org home trash,
  `$XDG_DATA_HOME/Trash/{files,info}`, with a `.trashinfo` beside each entry,
  so a file manager can show and restore it.

**Only Jarvis's own entries are ever purged.** The home trash is shared with
the owner's desktop, so the retention purge and the manual empty touch only
entries whose recorded original path is inside the data root this Trash was
built for (`owned`). Everything else in that folder belongs to someone else.

This module never deletes outside the trash: `put` moves, and the only real
deletion (`purge` / `empty`) happens inside `<trash>/files`, on names read
from `<trash>/info`, without following a symlink.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta
import errno
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Callable
from urllib.parse import quote, unquote

LOG = logging.getLogger(__name__)
DEFAULT_RETENTION_DAYS = 30.0
_WINDOWS = re.compile(r"^/mnt/([a-zA-Z])(/.*)?$")


class TrashError(Exception):
    """A move to the trash failed; nothing was deleted."""


def retention_days() -> float:
    """`JARVIS_TRASH_DAYS`, default 30. An unreadable value is the default,
    never zero: a typo must not turn retention into immediate deletion."""
    raw = os.environ.get("JARVIS_TRASH_DAYS", "")
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_RETENTION_DAYS
    return value if value > 0 else DEFAULT_RETENTION_DAYS


def home_trash() -> Path:
    """The freedesktop home trash: `$XDG_DATA_HOME/Trash`, else ~/.local/share/Trash."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "Trash"


def is_windows_path(path: str | Path) -> bool:
    return bool(_WINDOWS.match(str(path)))


def windows_path(path: str | Path) -> str:
    """`/mnt/c/Users/x` -> `C:\\Users\\x`."""
    match = _WINDOWS.match(str(path))
    if not match:
        raise TrashError(f"not a Windows path: {path}")
    rest = (match.group(2) or "").replace("/", "\\")
    return f"{match.group(1).upper()}:{rest or chr(92)}"


def recycle_script(path: Path) -> str:
    """The PowerShell that recycles `path`. The path is base64 data, decoded by
    the script itself, never a literal: PowerShell treats U+2018-U+201B as
    single quotes too, so escaping only `'` was not enough, and the only
    escaping that cannot be incomplete is not parsing the path at all."""
    data = base64.b64encode(windows_path(path).encode("utf-8")).decode("ascii")
    method = "DeleteDirectory" if path.is_dir() else "DeleteFile"
    return ("Add-Type -AssemblyName Microsoft.VisualBasic; "
            f"$p = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{data}')); "
            f"[Microsoft.VisualBasic.FileIO.FileSystem]::{method}("
            "$p, 'OnlyErrorDialogs', 'SendToRecycleBin')")


def recycle_windows(path: Path) -> None:
    """Send one file or folder to the Windows Recycle Bin. Raises on failure."""
    script = recycle_script(path)
    exe = shutil.which("powershell.exe")
    if exe is None:
        raise TrashError("powershell.exe is not reachable from here")
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        done = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                              capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise TrashError(f"Recycle Bin: {type(exc).__name__}") from exc
    if done.returncode or path.exists():
        raise TrashError("Recycle Bin refused: " + (done.stderr.strip()[:300] or "still on disk"))


class Trash:
    def __init__(self, owned: Path, *, root: Path | None = None,
                 retention: float | None = None,
                 recycle: Callable[[Path], None] | None = None,
                 is_windows: Callable[[Path], bool] | None = None,
                 clock: Callable[[], float] = time.time):
        self.owned = Path(owned)
        self._root = Path(root) if root is not None else None
        self.retention = retention if retention is not None else retention_days()
        self.recycle = recycle or recycle_windows
        self.is_windows = is_windows or is_windows_path
        self.clock = clock

    @property
    def root(self) -> Path:
        # Read late, so the environment a test sets is the one that counts.
        return self._root if self._root is not None else home_trash()

    def describe(self) -> dict:
        return {"location": str(self.root), "retention_days": self.retention,
                "entries": len(self.entries())}

    # -- putting things in -------------------------------------------------

    def put(self, path: Path) -> dict:
        """Move `path` to the trash. Returns where it went."""
        path = Path(path)
        if not path.is_absolute():
            raise TrashError("only absolute paths go to the trash")
        if not path.exists() and not path.is_symlink():
            raise TrashError(f"nothing at {path}")
        if self.is_windows(path):
            self.recycle(path)
            return {"where": "windows", "location": "Recycle Bin", "path": str(path)}
        files, info = self.root / "files", self.root / "info"
        files.mkdir(parents=True, exist_ok=True)
        info.mkdir(parents=True, exist_ok=True)
        name, record = self._reserve(info, path)
        target = files / name
        try:
            try:
                os.rename(path, target)
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                shutil.move(str(path), str(target))   # another filesystem: copy, then remove
        except OSError as exc:
            record.unlink(missing_ok=True)
            raise TrashError(f"could not move {path.name} to the trash: {exc.strerror or exc}") from exc
        return {"where": "linux", "location": str(self.root), "name": name, "path": str(path)}

    def _reserve(self, info: Path, path: Path) -> tuple[str, Path]:
        """Create the .trashinfo first, exclusively, which is what reserves a
        name in the spec: two writers cannot both claim it."""
        stamp = datetime.fromtimestamp(self.clock()).strftime("%Y-%m-%dT%H:%M:%S")
        body = f"[Trash Info]\nPath={quote(str(path), safe='/')}\nDeletionDate={stamp}\n".encode()
        base = path.name or "item"
        for n in range(1, 10_000):
            name = base if n == 1 else f"{base}.{n}"
            record = info / (name + ".trashinfo")
            if (self.root / "files" / name).exists():
                continue
            try:
                fd = os.open(record, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
            return name, record
        raise TrashError("no free name in the trash")

    # -- reading and purging -----------------------------------------------

    def entries(self) -> list[dict]:
        """Jarvis's entries in the Linux trash, oldest first. Others' are skipped."""
        info = self.root / "info"
        found = []
        try:
            records = sorted(info.glob("*.trashinfo"))
        except OSError:
            return []
        for record in records:
            parsed = self._parse(record)
            if parsed is None:
                continue
            original, deleted = parsed
            if not self._is_owned(original):
                continue                           # somebody else's file
            found.append({"name": record.name[:-len(".trashinfo")], "path": original,
                          "deleted": deleted.isoformat(timespec="seconds"),
                          "_when": deleted, "_record": record})
        found.sort(key=lambda e: e["_when"])
        return found

    def _is_owned(self, original: str) -> bool:
        """Is this recorded path inside the data root? Judged on the path as
        written, with no `..` allowed anywhere: `Path.relative_to` compares
        components and would take `<owned>/../elsewhere` as Jarvis's."""
        if not original.startswith("/") or "\0" in original:
            return False
        if ".." in original.split("/"):
            return False
        normal = Path(os.path.normpath(original))
        owned = Path(os.path.normpath(str(self.owned)))
        return normal == owned or owned in normal.parents

    @staticmethod
    def _parse(record: Path):
        try:
            text = record.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        fields = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
        try:
            return unquote(fields["Path"]), datetime.fromisoformat(fields["DeletionDate"])
        except (KeyError, ValueError):
            return None

    def _remove(self, entry: dict) -> None:
        name = entry["name"]
        target = self.root / "files" / name
        # `files/..` has `files` as its parent and *is* the trash itself, so a
        # parent check alone would let a planted `...trashinfo` turn an empty
        # into an rmtree of the owner's whole trash. Names are single, real
        # path components or nothing happens.
        if (not name or name in (".", "..") or "/" in name or "\0" in name
                or target.parent != self.root / "files"):
            raise TrashError("refusing a trash entry outside the trash")
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        entry["_record"].unlink(missing_ok=True)

    def _remove_each(self, entries: list[dict]) -> int:
        """Remove each entry; one that cannot be removed is logged and skipped,
        so a single bad entry never stops the rest, nor wedges every later
        purge and empty behind it."""
        removed = 0
        for entry in entries:
            try:
                self._remove(entry)
            except (TrashError, OSError) as exc:
                LOG.warning("Skipping trash entry %r: %s", entry.get("name"),
                            exc if isinstance(exc, TrashError) else type(exc).__name__)
                continue
            removed += 1
        return removed

    def purge(self, *, older_than_days: float | None = None) -> int:
        """Delete Jarvis's entries older than the retention period."""
        days = self.retention if older_than_days is None else older_than_days
        cutoff = datetime.fromtimestamp(self.clock()) - timedelta(days=days)
        return self._remove_each([e for e in self.entries() if e["_when"] <= cutoff])

    def empty(self) -> int:
        """The manual empty: every Jarvis entry, whatever its age."""
        return self._remove_each(self.entries())

    def public(self, entries: list[dict]) -> list[dict]:
        return [{k: v for k, v in e.items() if not k.startswith("_")} for e in entries]
