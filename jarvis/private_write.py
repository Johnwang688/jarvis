"""One way to write a file that must never be readable by anyone else.

Credential and private-state writers (Discord, Google and Spotify tokens, the
STT settings, the sub-agent record, the Browser host's marker files) all used
to spell this recipe their own way, and three of them created the file at the
umask mode and narrowed it afterwards. `write_private` is the recipe, once:

  * the bytes go to a temporary file beside the target, created with
    `O_CREAT | O_EXCL | O_NOFOLLOW` at 0600, so it is never wider than 0600
    for an instant and a symlink planted at its name is refused, not followed;
  * the name is unique per write, so two writers never share one and a
    leftover from a crash is never reused;
  * the data is fsynced, then `os.replace` swaps it in (a reader sees the old
    file or the new one, never half of either). The rename replaces a symlink
    at the target itself rather than writing through it;
  * on any failure the temporary file is unlinked and the error propagates.

This module imports nothing from the rest of Jarvis, so v1 and v2 can both use
it. It does not create the parent folder or change its mode: that stays with
the caller, who knows what the folder is for.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

__all__ = ["write_private"]


def write_private(path: Path | str, data: bytes) -> None:
    """`data` to `path`, atomically, 0600, never through a symlink."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
