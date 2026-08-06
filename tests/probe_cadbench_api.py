"""Live read-only probe for the endpoints cad-bench will depend on.

Run by hand (costs a handful of read-only API calls, writes nothing):

    .venv/bin/python tests/probe_cadbench_api.py

Verifies, against the real API:
  1. the part bounding-box endpoint shape (the generated docs have been wrong
     twice in this area — shadedviews, occurrencetransforms — so cad-bench
     does not get built on an unverified request),
  2. the assembly-definition occurrence/transform shape the graders parse,
  3. that a c-channel part the bench fixtures want actually resolves in the
     configured libraries.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from jarvis import config, onshape_auth
from jarvis.tools import onshape as cad


def main() -> int:
    keys = onshape_auth.auth()
    libs = onshape_auth.libraries()
    if not libs:
        print("no libraries configured — run `jarvis auth onshape --redo`")
        return 1

    # 1. find a c-channel the way the bench fixture will
    hit = None
    for lib in libs:
        entry = cad._resolve_library(lib["did"])
        for part in entry["parts"]:
            name = (part.get("name") or "").lower()
            if "c-channel" in name and "35" in name:
                hit = (lib, entry, part)
                break
        if hit:
            break
    if not hit:
        print("no '35 c-channel' part found in any configured library")
        return 1
    lib, entry, part = hit
    print(f"part: {part['name']!r}  [{part.get('partNumber')}]  in {lib['name']}")
    did, vid, eid, pid = lib["did"], entry["vid"], part["elementId"], part["partId"]

    # 2. bounding box for that part, several candidate paths
    candidates = [
        f"/parts/d/{did}/v/{vid}/e/{eid}/partid/{pid}/boundingboxes",
        f"/partstudios/d/{did}/v/{vid}/e/{eid}/boundingboxes",
    ]
    for path in candidates:
        r = httpx.get(config.ONSHAPE_API + path, auth=keys, timeout=30)
        print(f"\nGET {path}\n  -> {r.status_code}")
        if r.status_code == 200:
            print("  " + json.dumps(r.json(), indent=2)[:500])

    # 3. assembly definition shape for whatever assembly exists in the sandbox
    sb = onshape_auth.sandbox()
    r = httpx.get(
        config.ONSHAPE_API + f"/documents/d/{sb['did']}/w/{sb['wid']}/elements",
        auth=keys, timeout=30,
    )
    assemblies = [e for e in r.json() if e.get("elementType", "").upper() == "ASSEMBLY"]
    print(f"\nsandbox has {len(assemblies)} assemblies")
    if assemblies:
        aeid = assemblies[0]["id"]
        r = httpx.get(
            config.ONSHAPE_API + f"/assemblies/d/{sb['did']}/w/{sb['wid']}/e/{aeid}",
            auth=keys, timeout=30,
        )
        root = r.json().get("rootAssembly", {})
        occ = root.get("occurrences", [])
        inst = root.get("instances", [])
        print(f"first assembly {assemblies[0]['name']!r}: "
              f"{len(inst)} instances, {len(occ)} occurrences")
        if inst:
            keep = {k: inst[0].get(k) for k in
                    ("id", "name", "documentId", "documentVersion", "elementId",
                     "partId", "type", "isStandardContent", "configuration")}
            print("  instance[0]: " + json.dumps(keep, indent=2))
        if occ:
            print("  occurrence[0].path: " + json.dumps(occ[0].get("path")))
            t = occ[0].get("transform", [])
            print(f"  occurrence[0].transform: {len(t)} floats")
    return 0


if __name__ == "__main__":
    sys.exit(main())
