"""Pinned-download helper shared by the V0 probes (stdlib only).

`ensure(name)` returns the directory holding that model, downloading and
verifying it first when missing. Downloads are untrusted data: each goes into
its own empty directory, is hashed before it is touched, and archives are
extracted with the `data` tar filter (no absolute paths, no links out).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "models.json"
ROOT = Path(os.environ.get("JARVIS_VOICE_MODELS", Path.home() / ".cache" / "jarvis-voice-models"))


def manifest() -> dict:
    return json.loads(MANIFEST.read_text())["models"]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "jarvis-voice-v0/1"})
    with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as out:
        shutil.copyfileobj(resp, out, 1 << 20)


def fetch(name: str, record: bool = False) -> Path:
    entry = manifest()[name]
    target = ROOT / name
    marker = target / ".verified"
    if marker.exists() and entry.get("sha256") and marker.read_text().strip() == entry["sha256"]:
        return target
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    filename = entry["url"].rsplit("/", 1)[-1]
    archive = target / filename
    print(f"downloading {name} ...", file=sys.stderr)
    _download(entry["url"], archive)
    digest = sha256_file(archive)
    pinned = entry.get("sha256", "")
    if not pinned:
        if not record:
            raise SystemExit(f"{name}: no pinned sha256 in models.json (run fetch_models.py --record)")
        print(f'  "{name}": sha256 = {digest}', file=sys.stderr)
    elif digest != pinned:
        shutil.rmtree(target)
        raise SystemExit(f"{name}: sha256 mismatch: got {digest}, pinned {pinned}")
    sri = entry.get("npm_integrity")
    if sri:  # the registry's own digest, independent of our pinned sha256
        import base64

        got = "sha512-" + base64.b64encode(hashlib.sha512(archive.read_bytes()).digest()).decode()
        if got != sri:
            shutil.rmtree(target)
            raise SystemExit(f"{name}: npm integrity mismatch")
    if entry.get("extract"):
        out = target / "x"
        out.mkdir()
        with tarfile.open(archive) as tar:
            tar.extractall(out, filter="data")
        archive.unlink()
    marker.write_text(digest)
    return target


def ensure(name: str) -> Path:
    """Directory for `name`; raises SystemExit with a clear message when offline/unpinned."""
    return fetch(name)


def model_file(name: str) -> Path:
    """The single file of a non-archive entry."""
    d = ensure(name)
    return d / manifest()[name]["url"].rsplit("/", 1)[-1]


def extracted(name: str) -> Path:
    """Content root of an archive entry (the single top-level dir, if any)."""
    x = ensure(name) / "x"
    kids = [p for p in x.iterdir()]
    return kids[0] if len(kids) == 1 and kids[0].is_dir() else x
