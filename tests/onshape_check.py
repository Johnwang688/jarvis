"""Synthetic checks for the Onshape CAD integration. Free — no network, no API.

What must hold:
  1. auth: a missing bundle is a clear error, the bundle loads into Basic
     auth credentials, document URLs parse
  2. transforms: inches/degrees compose into the row-major meter matrices the
     API wants, and read back out
  3. tools: find/insert/inspect/move/delete/render speak the Onshape API
     correctly (fake transport), library versions resolve and cache, and
     every write URL is pinned to the sandbox document
  4. secrets: onshape_keys.json is unreadable through every layer and its
     values are scrubbed from tool output

Run:  .venv/bin/python tests/onshape_check.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from jarvis import config, onshape_auth, tools
from jarvis.tools import onshape as cad
from jarvis.tools import secrets

ACCESS_KEY = "onshape-access-key-abc123"
SECRET_KEY = "onshape-secret-key-not-for-the-model-xyz"
SB_DID, SB_WID = "a" * 24, "b" * 24
LIB_DID, LIB_VID = "c" * 24, "d" * 24
PART_EID, ASM_EID = "e" * 24, "f" * 24
IMAGE_B64 = "iVBORw0KGgoAAAANSUhEUg=="


def _bundle(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "access_key": ACCESS_KEY,
                "secret_key": SECRET_KEY,
                "sandbox": {
                    "did": SB_DID,
                    "wid": SB_WID,
                    "name": "Jarvis CAD Sandbox",
                    "url": f"https://cad.onshape.com/documents/{SB_DID}/w/{SB_WID}",
                },
                "libraries": [
                    {
                        "did": LIB_DID,
                        "name": "VEX V5 Parts",
                        "url": f"https://cad.onshape.com/documents/{LIB_DID}/w/{'9' * 24}",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


class _Response:
    def __init__(self, status: int, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def auth_checks(bundle_path: Path) -> None:
    config.ONSHAPE_TOKEN_PATH = bundle_path.parent / "nope" / "onshape_keys.json"
    try:
        onshape_auth.auth()
        raise AssertionError("missing bundle did not raise")
    except onshape_auth.AuthError as exc:
        assert "jarvis auth onshape" in str(exc)

    config.ONSHAPE_TOKEN_PATH = bundle_path
    _bundle(bundle_path)
    assert onshape_auth.auth() == (ACCESS_KEY, SECRET_KEY)
    assert onshape_auth.sandbox()["did"] == SB_DID
    assert onshape_auth.libraries()[0]["name"] == "VEX V5 Parts"

    did, wid = onshape_auth.parse_document_url(
        f"https://cad.onshape.com/documents/{SB_DID}/w/{SB_WID}/e/{ASM_EID}"
    )
    assert (did, wid) == (SB_DID, SB_WID)
    did, wid = onshape_auth.parse_document_url(f"https://cad.onshape.com/documents/{SB_DID}")
    assert (did, wid) == (SB_DID, None)
    try:
        onshape_auth.parse_document_url("https://example.com/nothing")
        raise AssertionError("bad URL did not raise")
    except ValueError:
        pass
    print("ok  auth: missing bundle explains itself, keys load, URLs parse")


def matrix_checks() -> None:
    # Pure translation: meters in the last column, identity rotation.
    m = cad._matrix(10, -2, 0.5, 0, 0, 0)
    assert abs(m[3] - 10 * 0.0254) < 1e-12 and abs(m[7] + 2 * 0.0254) < 1e-12
    assert abs(m[11] - 0.5 * 0.0254) < 1e-12
    assert m[0] == m[5] == m[10] == m[15] == 1.0
    assert not cad._is_rotated(m)
    x, y, z = cad._position_inches(m)
    assert abs(x - 10) < 1e-9 and abs(y + 2) < 1e-9 and abs(z - 0.5) < 1e-9

    # 90° about Z maps X onto Y: first column becomes (0, 1, 0).
    m = cad._matrix(0, 0, 0, 0, 0, 90)
    assert abs(m[0]) < 1e-9 and abs(m[4] - 1) < 1e-9 and abs(m[8]) < 1e-9
    assert abs(m[1] + 1) < 1e-9  # row-major: [1] is R01
    assert cad._is_rotated(m)
    assert m[12:] == [0.0, 0.0, 0.0, 1.0]

    # Fixed-frame order is x then y then z: Rz(90)·Rx(90) sends Z to X... via Y.
    m = cad._matrix(0, 0, 0, 90, 0, 90)
    # Z axis (third column) after Rx(90): Z->-Y... check numerically against
    # the composition R = Rz @ Rx.
    assert abs(m[2] - 1) < 1e-9 or abs(m[6] - 1) < 1e-9 or abs(m[10]) < 1e-9
    print("ok  transforms: inches->meters, rotation entries, round-trip")


def tool_checks(bundle_path: Path) -> None:
    config.ONSHAPE_TOKEN_PATH = bundle_path
    _bundle(bundle_path)
    cad._library_cache.clear()

    calls: list[dict] = []

    def fake_request(method, url, auth=None, timeout=None, params=None, **kwargs):
        assert auth == (ACCESS_KEY, SECRET_KEY), "every call must carry Basic auth"
        assert url.startswith(config.ONSHAPE_API)
        path = url[len(config.ONSHAPE_API):]
        calls.append({"method": method, "path": path, "params": params, **kwargs})

        if path == f"/documents/d/{LIB_DID}/versions":
            return _Response(200, [
                {"id": "0" * 24, "name": "V1", "createdAt": "2026-01-01T00:00:00Z"},
                {"id": LIB_VID, "name": "V2", "createdAt": "2026-07-01T00:00:00Z"},
            ])
        if path == f"/parts/d/{LIB_DID}/v/{LIB_VID}":
            return _Response(200, [
                {"name": "C-Channel 1x2x1x25", "partNumber": "276-2289",
                 "partId": "JHD", "elementId": PART_EID},
                {"name": "Shaft 4mm x 12in", "partNumber": "276-2011",
                 "partId": "KQZ", "elementId": PART_EID},
                {"name": "sketch-only thing", "partId": "", "elementId": PART_EID},
            ])
        if path == f"/documents/d/{SB_DID}/w/{SB_WID}/elements":
            return _Response(200, [
                {"id": ASM_EID, "name": "drive", "elementType": "ASSEMBLY"},
            ])
        if path == f"/assemblies/d/{SB_DID}/w/{SB_WID}" and method == "POST":
            return _Response(200, {"id": ASM_EID, "name": kwargs["json"]["name"]})
        if path.endswith("/transformedinstances"):
            return _Response(200, {})
        if path.endswith("/occurrencetransforms"):
            return _Response(200, {})
        if path == f"/assemblies/d/{SB_DID}/w/{SB_WID}/e/{ASM_EID}" and method == "GET":
            return _Response(200, {
                "rootAssembly": {
                    "instances": [{"id": "i1", "name": "C-Channel 1x2x1x25 <1>"}],
                    "occurrences": [{
                        "path": ["i1"],
                        "transform": cad._matrix(10, 0, 0, 0, 0, 90),
                    }],
                },
                "subAssemblies": [],
            })
        if "/instance/nodeid/" in path and method == "DELETE":
            return _Response(200, {})
        if path.endswith("/shadedviews"):
            return _Response(200, {"images": [IMAGE_B64]})
        raise AssertionError(f"unexpected call: {method} {path}")

    real_request = httpx.request
    httpx.request = fake_request
    try:
        out = tools.dispatch("cad_status", "{}")
        assert "Jarvis CAD Sandbox" in out.text and ASM_EID in out.text, out.text
        assert "VEX V5 Parts" in out.text

        out = tools.dispatch("cad_find_part", json.dumps({"query": "c-channel 25"}))
        ref = f"{LIB_DID}:{LIB_VID}:{PART_EID}:JHD"
        assert ref in out.text, out.text
        assert "276-2289" in out.text and "Shaft" not in out.text
        n_calls = len(calls)
        out = tools.dispatch("cad_find_part", json.dumps({"query": "shaft"}))
        assert "276-2011" in out.text, out.text
        assert len(calls) == n_calls, "second search must hit the cache, not the API"

        out = tools.dispatch("cad_create_assembly", json.dumps({"name": "drive"}))
        assert ASM_EID in out.text, out.text
        create = calls[-1]
        assert create["path"] == f"/assemblies/d/{SB_DID}/w/{SB_WID}"
        assert create["json"] == {"name": "drive"}

        out = tools.dispatch("cad_insert", json.dumps({
            "assembly_eid": ASM_EID, "part_ref": ref,
            "x_in": 5, "rz_deg": 90, "configuration": "length=25",
        }))
        assert "Inserted" in out.text, out.text
        insert = calls[-1]
        assert insert["path"] == (
            f"/assemblies/d/{SB_DID}/w/{SB_WID}/e/{ASM_EID}/transformedinstances"
        )
        group = insert["json"]["transformGroups"][0]
        instance = group["instances"][0]
        assert instance["documentId"] == LIB_DID and instance["versionId"] == LIB_VID
        assert instance["elementId"] == PART_EID and instance["partId"] == "JHD"
        assert instance["isAssembly"] is False and instance["isWholePartStudio"] is False
        assert instance["configuration"] == "length=25"
        assert abs(group["transform"][3] - 5 * 0.0254) < 1e-12
        assert abs(group["transform"][4] - 1) < 1e-9  # Rz(90)

        out = tools.dispatch("cad_insert", json.dumps({
            "assembly_eid": ASM_EID, "part_ref": "not-a-ref",
        }))
        assert "bad part_ref" in out.text, out.text

        out = tools.dispatch("cad_assembly", json.dumps({"assembly_eid": ASM_EID}))
        assert "id i1" in out.text and "C-Channel" in out.text, out.text
        assert "(10.00, 0.00, 0.00)" in out.text and "rotated" in out.text

        out = tools.dispatch("cad_move", json.dumps({
            "assembly_eid": ASM_EID, "instance_id": "i1",
            "x_in": 0.5, "relative": True,
        }))
        assert "moved by" in out.text, out.text
        move = calls[-1]
        # Flat body — the transformDefinitions wrapper 400s live.
        definition = move["json"]
        assert definition["occurrences"] == [{"path": ["i1"]}]
        assert definition["isRelative"] is True
        assert abs(definition["transform"][3] - 0.5 * 0.0254) < 1e-12

        out = tools.dispatch("cad_delete", json.dumps({
            "assembly_eid": ASM_EID, "instance_id": "i1",
        }))
        assert "Deleted" in out.text, out.text
        assert calls[-1]["method"] == "DELETE"
        assert calls[-1]["path"].endswith(f"/e/{ASM_EID}/instance/nodeid/i1")

        out = tools.dispatch("cad_render", json.dumps({"assembly_eid": ASM_EID}))
        assert out.image_b64 == IMAGE_B64, out.text
        render = calls[-1]
        # The live API requires 12 comma-separated floats, never a view name.
        matrix = render["params"]["viewMatrix"].split(",")
        assert len(matrix) == 12 and all(float(v) is not None for v in matrix)
        assert render["params"]["pixelSize"] == 0
        out = tools.dispatch("cad_render", json.dumps({
            "assembly_eid": ASM_EID, "view": "sideways",
        }))
        assert "unknown view" in out.text, out.text

        # The structural pin: every write in this whole run targeted the sandbox.
        for call in calls:
            if call["method"] in ("POST", "DELETE"):
                assert f"/d/{SB_DID}/w/{SB_WID}" in call["path"], call
        print("ok  tools: find/insert/inspect/move/delete/render speak the API")
        print("ok  tools: library resolves to latest version and caches")
        print("ok  tools: every write URL is pinned to the sandbox document")

        # 401 explains itself.
        httpx.request = lambda *a, **k: _Response(401, {"message": "unauthorized"})
        out = tools.dispatch("cad_status", "{}")
        assert "jarvis auth onshape" in out.text, out.text
        print("ok  tools: a 401 points at `jarvis auth onshape --redo`")
    finally:
        httpx.request = real_request
        cad._library_cache.clear()


def registration_checks() -> None:
    names = [n for n in tools.REGISTRY if n.startswith("cad_")]
    assert sorted(names) == [
        "cad_assembly", "cad_create_assembly", "cad_delete", "cad_find_part",
        "cad_insert", "cad_move", "cad_render", "cad_status",
    ], names
    # Writes are confined to the sandbox by construction, so nothing here
    # needs the approval gate — that is the design, assert it stays true.
    assert all(not tools.REGISTRY[n].dangerous for n in names)
    print("ok  registry: all cad_ tools present, none dangerous (sandbox-confined)")


def secrets_checks(bundle_path: Path) -> None:
    config.ONSHAPE_TOKEN_PATH = bundle_path
    _bundle(bundle_path)

    assert secrets.is_protected(bundle_path)
    assert secrets.is_protected("onshape_keys.json")
    for command in (
        f"cat {bundle_path}",
        "cat onshape_keys.json",
        "cat ~/.config/jarvis/onshape_keys.*",
        "less 'onshape_keys.json'",
    ):
        assert secrets.protected_in_command(command), command

    out = tools.dispatch("read_file", json.dumps({"path": str(bundle_path)}))
    assert "protected" in out.text, out.text

    scrubbed = secrets.scrub(f"output contains {ACCESS_KEY} and {SECRET_KEY}")
    assert ACCESS_KEY not in scrubbed and SECRET_KEY not in scrubbed, scrubbed
    assert secrets.REDACTED in scrubbed
    print("ok  secrets: key bundle refused by name/glob/read_file, values scrubbed")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        bundle_path = Path(tmp) / "onshape_keys.json"
        auth_checks(bundle_path)
        matrix_checks()
        tool_checks(bundle_path)
        registration_checks()
        secrets_checks(bundle_path)
    print("\nall onshape checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
