"""List every element in the sandbox document — hand-run hygiene check."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from jarvis import config, onshape_auth

sb = onshape_auth.sandbox()
r = httpx.get(
    config.ONSHAPE_API + f"/documents/d/{sb['did']}/w/{sb['wid']}/elements",
    auth=onshape_auth.auth(), timeout=30,
)
for e in r.json():
    print(f"{e.get('elementType', '?'):16} | {e.get('name')} | {e.get('id')}")
