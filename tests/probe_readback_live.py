"""Live read-only smoke of the upgraded cad_assembly readback (item 3).
Run by hand:  .venv/bin/python tests/probe_readback_live.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from jarvis import config, onshape_auth, tools

sb = onshape_auth.sandbox()
r = httpx.get(
    config.ONSHAPE_API + f"/documents/d/{sb['did']}/w/{sb['wid']}/elements",
    auth=onshape_auth.auth(), timeout=30,
)
asms = [e for e in r.json() if e.get("elementType", "").upper() == "ASSEMBLY"]
if not asms:
    print("no assemblies in the sandbox")
    sys.exit(1)
for asm in asms:
    print(f"== {asm['name']} ({asm['id']})")
    out = tools.dispatch("cad_assembly", json.dumps({"assembly_eid": asm["id"]}))
    print(out.text)
    print()
