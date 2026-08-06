"""Live probe: can cad-bench delete its own assemblies? Creates one throwaway
assembly in the pinned sandbox and deletes it — the exact create/cleanup cycle
every bench run performs. Run by hand:

    .venv/bin/python tests/probe_cadbench_delete.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from jarvis import config, onshape_auth


def main() -> int:
    keys = onshape_auth.auth()
    sb = onshape_auth.sandbox()
    base = f"/assemblies/d/{sb['did']}/w/{sb['wid']}"

    r = httpx.post(config.ONSHAPE_API + base, auth=keys, timeout=30,
                   json={"name": "cadbench-probe-delete-me"})
    print(f"create -> {r.status_code}")
    if r.status_code != 200:
        print(r.text[:300])
        return 1
    eid = r.json()["id"]
    print(f"eid {eid}")

    r = httpx.delete(
        config.ONSHAPE_API + f"/elements/d/{sb['did']}/w/{sb['wid']}/e/{eid}",
        auth=keys, timeout=30,
    )
    print(f"delete /elements/.../e/{{eid}} -> {r.status_code}  {r.text[:200]}")

    # confirm it is gone
    r = httpx.get(config.ONSHAPE_API + f"/documents/d/{sb['did']}/w/{sb['wid']}/elements",
                  auth=keys, timeout=30)
    still = [e for e in r.json() if e.get("id") == eid]
    print("gone" if not still else f"STILL PRESENT: {still}")
    return 0 if not still else 1


if __name__ == "__main__":
    sys.exit(main())
