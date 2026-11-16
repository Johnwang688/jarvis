"""Private data storage; no configuration or credential file reads."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def safe_input(path: str | Path) -> Path:
    path = Path(path).expanduser().resolve()
    if any(part.startswith('.env') for part in path.parts):
        raise ValueError('refusing .env input')
    return path


def data_dir(value: str | Path | None = None) -> Path:
    path = safe_input(value or os.environ.get('VOICECLEAN_DATA_DIR')
                      or Path.home() / '.local/share/jarvis/va3-data')
    if any((p / '.git').exists() for p in (path, *path.parents)):
        raise ValueError('data directory must be outside every git work tree')
    private_dir(path)
    return path


def private_dir(path: Path) -> None:
    if not path.exists():
        if not path.parent.exists():
            private_dir(path.parent)
        path.mkdir(mode=0o700, exist_ok=True)
    path.chmod(0o700)


def create_private_file(path: Path) -> int:
    """Create a new private binary output; the caller owns the returned fd."""
    if path.is_symlink():
        raise ValueError('refusing a symlink output file')
    path = safe_input(path)
    data_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    return fd


def write_json(path: Path, value, *, lines: bool = False) -> None:
    # Validate again at each write, including dataset/ and existing symlinks.
    if path.is_symlink():
        raise ValueError('refusing a symlink output file')
    path = safe_input(path)
    data_dir(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        os.fchmod(fh.fileno(), 0o600)
        if lines:
            for row in value:
                fh.write(json.dumps(row, ensure_ascii=False) + '\n')
        else:
            json.dump(value, fh, ensure_ascii=False, indent=2)
            fh.write('\n')


def read_jsonl(path: Path):
    with safe_input(path).open(encoding='utf-8') as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with safe_input(path).open('rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def source_id(text: str) -> str:
    from .normalize import tokens
    return hashlib.sha256(' '.join(tokens(text)).encode()).hexdigest()
