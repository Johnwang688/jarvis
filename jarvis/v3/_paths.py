"""Filesystem helpers for v3 state."""

from contextlib import contextmanager
import os
from pathlib import Path


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@contextmanager
def open_directory(path: Path, *, create: bool = False):
    """Pin each component before opening the next one, without following links."""
    path = Path(os.path.abspath(path))
    fd = os.open(path.anchor, _DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)
