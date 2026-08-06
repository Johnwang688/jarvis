"""Onshape CAD tools: find parts, assemble, inspect, render.

VEX robot CAD is assembly from stock parts, and that is exactly what these
tools do: find a part in a configured library, insert it into an assembly in
the sandbox document, nudge it, look at the result. Modeling new geometry is
deliberately out of scope for now.

Write scope is structural, not policed: every write URL is built from the
sandbox document pinned in the key bundle (`onshape_auth.sandbox()`) and no
tool takes a document id for a write target, so none of these need
dangerous=True — there is no argument the model could supply to reach
another document. Libraries are read-only sources, resolved to their latest
version because Onshape requires cross-document inserts to reference a
version, not a live workspace.

Credentials: `onshape_auth.auth()` goes into HTTP Basic auth here and is
never part of a tool result — the model only sees API responses.

Units: the Onshape API speaks meters and 4x4 row-major transforms; these
tools speak inches and degrees, because VEX lives on a 1/2" hole grid and
the model reasons better in the parts' native units.
"""

from __future__ import annotations

import math
from typing import Annotated

import httpx

from .. import config, onshape_auth
from . import ToolResult, tool

_INCH = 0.0254  # meters


class _CadError(RuntimeError):
    """A failure the model can act on. Tools return the message as text."""


def _request(method: str, path: str, **kwargs) -> httpx.Response:
    response = httpx.request(
        method,
        config.ONSHAPE_API + path,
        auth=onshape_auth.auth(),
        timeout=60,
        **kwargs,
    )
    if response.status_code == 401:
        raise _CadError(
            "Onshape rejected the API keys (HTTP 401) — they may have been "
            "revoked. The owner needs to re-run `jarvis auth onshape --redo`."
        )
    return response


def _fail(response: httpx.Response) -> str:
    try:
        message = response.json().get("message", "")
    except ValueError:
        message = ""
    message = message or response.text[:200]
    hint = ""
    if response.status_code == 403:
        hint = " (the API key may be missing the 'write' scope)"
    return f"Error: Onshape returned {response.status_code}{hint}: {message}"


# -- transforms ---------------------------------------------------------------


def _rotation(rx_deg: float, ry_deg: float, rz_deg: float) -> list[list[float]]:
    """Rotation about the assembly X, then Y, then Z axes (fixed frame)."""
    def matmul(a, b):
        return [
            [sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)]
            for i in range(3)
        ]

    cx, sx = math.cos(math.radians(rx_deg)), math.sin(math.radians(rx_deg))
    cy, sy = math.cos(math.radians(ry_deg)), math.sin(math.radians(ry_deg))
    cz, sz = math.cos(math.radians(rz_deg)), math.sin(math.radians(rz_deg))
    rx = [[1, 0, 0], [0, cx, -sx], [0, sx, cx]]
    ry = [[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]]
    rz = [[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]]
    return matmul(rz, matmul(ry, rx))


def _matrix(
    x_in: float, y_in: float, z_in: float,
    rx_deg: float, ry_deg: float, rz_deg: float,
) -> list[float]:
    """Row-major 4x4 for the Onshape API: rotation block, translation in meters."""
    r = _rotation(rx_deg, ry_deg, rz_deg)
    return [
        r[0][0], r[0][1], r[0][2], x_in * _INCH,
        r[1][0], r[1][1], r[1][2], y_in * _INCH,
        r[2][0], r[2][1], r[2][2], z_in * _INCH,
        0.0, 0.0, 0.0, 1.0,
    ]


def _position_inches(transform: list[float]) -> tuple[float, float, float]:
    return (
        transform[3] / _INCH,
        transform[7] / _INCH,
        transform[11] / _INCH,
    )


def _angles(transform: list[float]) -> tuple[float, float, float]:
    """Fixed-frame X→Y→Z angles in degrees from a 16-float transform — the
    exact inverse of _rotation, so the numbers read back out of an assembly
    are the numbers cad_insert and cad_move accept going in.

    For R = Rz·Ry·Rx: R20 = -sin(ry), R21 = cy·sx, R22 = cy·cx,
    R10 = sz·cy, R00 = cz·cy. At the gimbal singularity (|ry| = 90°) rx and
    rz describe the same axis; the whole angle is reported as rx, rz as 0."""
    sy = max(-1.0, min(1.0, -transform[8]))
    ry = math.asin(sy)
    if abs(sy) < 0.999999:
        rx = math.atan2(transform[9], transform[10])
        rz = math.atan2(transform[4], transform[0])
    else:
        rx = math.atan2(-transform[6], transform[5])
        rz = 0.0
    return tuple(round(math.degrees(a), 1) + 0.0 for a in (rx, ry, rz))


def _span_text(bbox: dict) -> str:
    lo, hi = bbox["lo"], bbox["hi"]
    return (
        f"spans X {lo[0]:.2f}..{hi[0]:.2f}, Y {lo[1]:.2f}..{hi[1]:.2f}, "
        f"Z {lo[2]:.2f}..{hi[2]:.2f} in"
    )


# (did, vid, eid, partId) -> {"lo": (x,y,z), "hi": (x,y,z)} in inches.
# Process-lifetime; a part's box in its own version never changes.
_bbox_cache: dict[tuple, dict] = {}


def _part_bbox(did: str, vid: str, eid: str, part_id: str) -> dict | None:
    """A part's local-frame bounding box in inches, or None — a readback must
    degrade to the old output, never fail, when a box cannot be fetched.
    Endpoint verified live 2026-08-05: flat lowX..highZ payload, meters."""
    key = (did, vid, eid, part_id)
    if key in _bbox_cache:
        return _bbox_cache[key]
    try:
        response = _request(
            "GET", f"/parts/d/{did}/v/{vid}/e/{eid}/partid/{part_id}/boundingboxes"
        )
        if response.status_code != 200:
            return None
        data = response.json()
        box = {
            "lo": tuple(data[k] / _INCH for k in ("lowX", "lowY", "lowZ")),
            "hi": tuple(data[k] / _INCH for k in ("highX", "highY", "highZ")),
        }
    except (_CadError, httpx.HTTPError, KeyError, TypeError, ValueError):
        return None
    _bbox_cache[key] = box
    return box


def _world_aabb(local: dict, transform: list[float]) -> dict:
    """A local-frame box pushed through a 16-float row-major transform
    (meters translation) into a world-frame AABB, inches throughout."""
    corners = [
        (x, y, z)
        for x in (local["lo"][0], local["hi"][0])
        for y in (local["lo"][1], local["hi"][1])
        for z in (local["lo"][2], local["hi"][2])
    ]
    t = transform
    world = [
        (
            t[0] * x + t[1] * y + t[2] * z + t[3] / _INCH,
            t[4] * x + t[5] * y + t[6] * z + t[7] / _INCH,
            t[8] * x + t[9] * y + t[10] * z + t[11] / _INCH,
        )
        for (x, y, z) in corners
    ]
    return {
        "lo": tuple(min(p[i] for p in world) for i in range(3)),
        "hi": tuple(max(p[i] for p in world) for i in range(3)),
    }


def _is_rotated(transform: list[float]) -> bool:
    identity = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    block = (
        transform[0], transform[1], transform[2],
        transform[4], transform[5], transform[6],
        transform[8], transform[9], transform[10],
    )
    return any(abs(a - b) > 1e-6 for a, b in zip(block, identity))


# -- library resolution -------------------------------------------------------

# did -> {"vid": ..., "version_name": ..., "parts": [...]}. Process-lifetime
# cache; cad_find_part(refresh=True) busts it after the owner edits a library.
_library_cache: dict[str, dict] = {}


def _resolve_library(did: str, refresh: bool = False) -> dict:
    """Latest version + its parts. Inserts must reference a version, so a
    library that has never been versioned is unusable — say so clearly."""
    if not refresh and did in _library_cache:
        return _library_cache[did]

    versions = _request("GET", f"/documents/d/{did}/versions")
    if versions.status_code != 200:
        raise _CadError(_fail(versions))
    listing = [v for v in versions.json() if v.get("id")]
    if not listing:
        raise _CadError(
            f"library document {did} has no versions. Onshape can only insert "
            "cross-document parts from a version — the owner should open the "
            "library and click 'Create version' (right panel), then retry with "
            "refresh=true."
        )
    latest = max(listing, key=lambda v: v.get("createdAt", ""))

    parts = _request("GET", f"/parts/d/{did}/v/{latest['id']}")
    if parts.status_code != 200:
        raise _CadError(_fail(parts))

    entry = {
        "vid": latest["id"],
        "version_name": latest.get("name", "?"),
        "parts": [p for p in parts.json() if p.get("partId") and p.get("elementId")],
    }
    _library_cache[did] = entry
    return entry


# -- tools --------------------------------------------------------------------


@tool
def cad_status() -> str:
    """What CAD access looks like right now: the sandbox document (where all
    CAD writes go), its assemblies and other elements with their eids, and the
    configured parts libraries. Call this first when starting CAD work."""
    try:
        sb = onshape_auth.sandbox()
        response = _request("GET", f"/documents/d/{sb['did']}/w/{sb['wid']}/elements")
        if response.status_code != 200:
            return _fail(response)
        lines = [f"Onshape connected. Sandbox document: {sb['name']} (all CAD writes go here)."]
        elements = response.json()
        if elements:
            lines.append("Elements:")
            for element in elements:
                kind = element.get("elementType", "?").lower()
                lines.append(f"- eid {element.get('id')} · {kind} · {element.get('name')}")
        else:
            lines.append("The sandbox is empty — cad_create_assembly to start.")
        libs = onshape_auth.libraries()
        if libs:
            lines.append("Parts libraries (searched by cad_find_part):")
            lines.extend(f"- {lib['name']}" for lib in libs)
        else:
            lines.append(
                "No parts libraries configured — the owner can add them with "
                "`jarvis auth onshape --redo`."
            )
        return "\n".join(lines)
    except (onshape_auth.AuthError, _CadError) as exc:
        return f"Error: {exc}"
    except httpx.HTTPError as exc:
        return f"Error: could not reach Onshape ({exc})."


@tool
def cad_find_part(
    query: Annotated[str, "Words to match against part names/numbers, e.g. 'c-channel 25' or 'motor'"],
    max_results: Annotated[int, "How many matches, 1-25"] = 10,
    refresh: Annotated[bool, "Re-read the libraries instead of using the cache"] = False,
) -> str:
    """Search the configured parts libraries. Every whitespace-separated word
    must appear in a part's name or part number (case-insensitive). Returns a
    `ref` per match to pass to cad_insert. Parts only — library assemblies are
    not insertable yet."""
    try:
        libs = onshape_auth.libraries()
        if not libs:
            return (
                "Error: no parts libraries are configured. The owner can add "
                "library document URLs with `jarvis auth onshape --redo`."
            )
        max_results = max(1, min(int(max_results), 25))
        tokens = [t for t in query.lower().split() if t]
        matches: list[str] = []
        total = 0
        notes: list[str] = []
        for lib in libs:
            try:
                entry = _resolve_library(lib["did"], refresh=refresh)
            except _CadError as exc:
                notes.append(f"(library {lib['name']}: {exc})")
                continue
            for part in entry["parts"]:
                haystack = f"{part.get('name', '')} {part.get('partNumber') or ''}".lower()
                if all(token in haystack for token in tokens):
                    total += 1
                    if len(matches) < max_results:
                        number = f" [{part['partNumber']}]" if part.get("partNumber") else ""
                        ref = f"{lib['did']}:{entry['vid']}:{part['elementId']}:{part['partId']}"
                        # Size up front is what lets a model pick an offset
                        # instead of guessing one — the local-frame extents
                        # say which axis is long, and how long.
                        bbox = _part_bbox(
                            lib["did"], entry["vid"],
                            part["elementId"], part["partId"],
                        )
                        size = f" · {_span_text(bbox)} (local)" if bbox else ""
                        matches.append(
                            f"- {part.get('name')}{number} · ref {ref} · "
                            f"{lib['name']}{size}"
                        )
        if not matches:
            body = f"No parts match {query!r}. Try fewer or shorter words."
        else:
            shown = f" (showing {len(matches)})" if total > len(matches) else ""
            body = f"{total} part(s) match {query!r}{shown}:\n" + "\n".join(matches)
        return "\n".join([body, *notes])
    except (onshape_auth.AuthError, _CadError) as exc:
        return f"Error: {exc}"
    except httpx.HTTPError as exc:
        return f"Error: could not reach Onshape ({exc})."


@tool
def cad_create_assembly(
    name: Annotated[str, "Name for the new assembly, e.g. 'drivetrain'"],
) -> str:
    """Create a new, empty assembly in the sandbox document. Returns its eid,
    which the other cad_ tools take as assembly_eid."""
    try:
        sb = onshape_auth.sandbox()
        response = _request(
            "POST", f"/assemblies/d/{sb['did']}/w/{sb['wid']}", json={"name": name}
        )
        if response.status_code != 200:
            return _fail(response)
        return f"Created assembly {name!r} — eid {response.json().get('id')}."
    except (onshape_auth.AuthError, _CadError) as exc:
        return f"Error: {exc}"
    except httpx.HTTPError as exc:
        return f"Error: could not reach Onshape ({exc})."


@tool
def cad_insert(
    assembly_eid: Annotated[str, "Assembly eid in the sandbox (from cad_status or cad_create_assembly)"],
    part_ref: Annotated[str, "A ref from cad_find_part (did:vid:eid:partId)"],
    x_in: Annotated[float, "X position in inches (assembly coordinates)"] = 0,
    y_in: Annotated[float, "Y position in inches"] = 0,
    z_in: Annotated[float, "Z position in inches"] = 0,
    rx_deg: Annotated[float, "Rotation about X in degrees, applied first"] = 0,
    ry_deg: Annotated[float, "Rotation about Y in degrees, applied second"] = 0,
    rz_deg: Annotated[float, "Rotation about Z in degrees, applied third"] = 0,
    configuration: Annotated[str, "Configuration string for configurable parts, if any"] = "",
) -> str:
    """Insert a library part into a sandbox assembly at a position/orientation.
    VEX parts sit on a 0.5 inch hole grid — think in half-inch steps. After a
    few inserts, cad_assembly shows instance ids and cad_render shows the
    result; verify before building further."""
    try:
        pieces = part_ref.split(":")
        if len(pieces) != 4 or not all(pieces):
            return (
                f"Error: bad part_ref {part_ref!r} — expected the "
                "'did:vid:eid:partId' string exactly as returned by cad_find_part."
            )
        did, vid, eid, part_id = pieces
        sb = onshape_auth.sandbox()
        instance = {
            "documentId": did,
            "versionId": vid,
            "elementId": eid,
            "partId": part_id,
            "isAssembly": False,
            "isWholePartStudio": False,
        }
        if configuration:
            instance["configuration"] = configuration
        response = _request(
            "POST",
            f"/assemblies/d/{sb['did']}/w/{sb['wid']}/e/{assembly_eid}/transformedinstances",
            json={
                "transformGroups": [
                    {
                        "instances": [instance],
                        "transform": _matrix(x_in, y_in, z_in, rx_deg, ry_deg, rz_deg),
                    }
                ]
            },
        )
        if response.status_code != 200:
            return _fail(response)
        return (
            f"Inserted at ({x_in}, {y_in}, {z_in}) in, rotation "
            f"({rx_deg}, {ry_deg}, {rz_deg})°. cad_assembly lists instance ids; "
            "cad_render shows the result."
        )
    except (onshape_auth.AuthError, _CadError) as exc:
        return f"Error: {exc}"
    except httpx.HTTPError as exc:
        return f"Error: could not reach Onshape ({exc})."


@tool
def cad_assembly(
    assembly_eid: Annotated[str, "Assembly eid in the sandbox"],
) -> str:
    """List a sandbox assembly's instances: id, part name, position in inches
    (assembly coordinates), rotation in the same X→Y→Z degrees cad_insert and
    cad_move accept, and each part's world-frame extents — so clearance
    between parts can be computed from the text, not eyeballed. The ids are
    what cad_move and cad_delete take."""
    try:
        sb = onshape_auth.sandbox()
        response = _request(
            "GET", f"/assemblies/d/{sb['did']}/w/{sb['wid']}/e/{assembly_eid}"
        )
        if response.status_code != 200:
            return _fail(response)
        data = response.json()
        root = data.get("rootAssembly", {})
        infos: dict[str, dict] = {}
        for source in [root, *data.get("subAssemblies", [])]:
            for instance in source.get("instances", []):
                infos[instance.get("id", "")] = instance
        occurrences = root.get("occurrences", [])
        if not occurrences:
            return "The assembly is empty. cad_insert adds parts."
        lines = [f"{len(occurrences)} instance(s), positions in inches:"]
        for occurrence in occurrences:
            path = occurrence.get("path", [])
            transform = occurrence.get("transform", [])
            label = "/".join(path)
            info = infos.get(path[-1] if path else "", {})
            name = info.get("name", "?")
            if len(transform) == 16:
                x, y, z = _position_inches(transform)
                where = f"at ({x:.2f}, {y:.2f}, {z:.2f})"
                if _is_rotated(transform):
                    rx, ry, rz = _angles(transform)
                    where += f", rotated ({rx:g}, {ry:g}, {rz:g})°"
                # World extents from the actual inserted part's measured box.
                # The definition names the version key `documentVersion`
                # (verified live 2026-08-05). Missing pieces degrade to the
                # bare position — never an error.
                source_ids = (
                    info.get("documentId"), info.get("documentVersion"),
                    info.get("elementId"), info.get("partId"),
                )
                if all(source_ids):
                    local = _part_bbox(*source_ids)
                    if local:
                        where += f" · {_span_text(_world_aabb(local, transform))}"
            else:
                where = "(no transform)"
            lines.append(f"- id {label} · {name} · {where}")
        return "\n".join(lines)
    except (onshape_auth.AuthError, _CadError) as exc:
        return f"Error: {exc}"
    except httpx.HTTPError as exc:
        return f"Error: could not reach Onshape ({exc})."


@tool
def cad_move(
    assembly_eid: Annotated[str, "Assembly eid in the sandbox"],
    instance_id: Annotated[str, "Instance id from cad_assembly (nested: ids joined with '/')"],
    x_in: Annotated[float, "X in inches — target position, or offset when relative"] = 0,
    y_in: Annotated[float, "Y in inches"] = 0,
    z_in: Annotated[float, "Z in inches"] = 0,
    rx_deg: Annotated[float, "Rotation about X in degrees, applied first"] = 0,
    ry_deg: Annotated[float, "Rotation about Y in degrees, applied second"] = 0,
    rz_deg: Annotated[float, "Rotation about Z in degrees, applied third"] = 0,
    relative: Annotated[bool, "True: apply as an offset to where it is. False: set the absolute placement"] = False,
) -> str:
    """Move/rotate one instance in a sandbox assembly. Absolute mode replaces
    the whole placement (position and rotation); relative mode nudges from the
    current one."""
    try:
        sb = onshape_auth.sandbox()
        # Flat body, no transformDefinitions wrapper — that shape belongs to
        # the /modify endpoint and this one 400s on it (verified live).
        response = _request(
            "POST",
            f"/assemblies/d/{sb['did']}/w/{sb['wid']}/e/{assembly_eid}/occurrencetransforms",
            json={
                "occurrences": [{"path": instance_id.split("/")}],
                "transform": _matrix(x_in, y_in, z_in, rx_deg, ry_deg, rz_deg),
                "isRelative": bool(relative),
            },
        )
        if response.status_code != 200:
            return _fail(response)
        mode = "moved by" if relative else "placed at"
        return f"Instance {instance_id} {mode} ({x_in}, {y_in}, {z_in}) in."
    except (onshape_auth.AuthError, _CadError) as exc:
        return f"Error: {exc}"
    except httpx.HTTPError as exc:
        return f"Error: could not reach Onshape ({exc})."


@tool
def cad_delete(
    assembly_eid: Annotated[str, "Assembly eid in the sandbox"],
    instance_id: Annotated[str, "Top-level instance id from cad_assembly"],
) -> str:
    """Remove one top-level instance from a sandbox assembly."""
    try:
        sb = onshape_auth.sandbox()
        response = _request(
            "DELETE",
            f"/assemblies/d/{sb['did']}/w/{sb['wid']}/e/{assembly_eid}"
            f"/instance/nodeid/{instance_id}",
        )
        if response.status_code != 200:
            return _fail(response)
        return f"Deleted instance {instance_id}."
    except (onshape_auth.AuthError, _CadError) as exc:
        return f"Error: {exc}"
    except httpx.HTTPError as exc:
        return f"Error: could not reach Onshape ({exc})."


# The live API refuses named views ("A 12-element view matrix is required"),
# so the names are translated here: world→camera rotation rows, zero
# translation per row. iso is Onshape's standard isometric.
_VIEWS: dict[str, list[float]] = {
    "top": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0],
    "bottom": [1, 0, 0, 0, 0, -1, 0, 0, 0, 0, -1, 0],
    "front": [1, 0, 0, 0, 0, 0, 1, 0, 0, -1, 0, 0],
    "back": [-1, 0, 0, 0, 0, 0, 1, 0, 0, 1, 0, 0],
    "right": [0, 1, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0],
    "left": [0, -1, 0, 0, 0, 0, 1, 0, -1, 0, 0, 0],
    "iso": [0.707, 0.707, 0, 0, -0.408, 0.408, 0.816, 0, 0.577, -0.577, 0.577, 0],
}


@tool
def cad_render(
    assembly_eid: Annotated[str, "Assembly eid in the sandbox"],
    view: Annotated[str, "front, back, top, bottom, left, right, or iso"] = "iso",
) -> ToolResult:
    """Render a shaded image of a sandbox assembly, zoomed to fit. This is how
    you see your work — render after every few changes and check the image
    against what you intended before continuing."""
    try:
        matrix = _VIEWS.get(view.lower().strip())
        if matrix is None:
            return ToolResult(
                f"Error: unknown view {view!r} — use one of {', '.join(_VIEWS)}."
            )
        sb = onshape_auth.sandbox()
        response = _request(
            "GET",
            f"/assemblies/d/{sb['did']}/w/{sb['wid']}/e/{assembly_eid}/shadedviews",
            params={
                "viewMatrix": ",".join(str(v) for v in matrix),
                "outputWidth": 960,
                "outputHeight": 720,
                "pixelSize": 0,  # 0 = zoom to fit the output size
                "edges": "show",
            },
        )
        if response.status_code != 200:
            return ToolResult(_fail(response))
        images = response.json().get("images") or []
        image = images[0] if images else None
        if isinstance(image, list):  # some client docs type this [[str]]
            image = image[0] if image else None
        if not image:
            return ToolResult("Error: Onshape returned no image for that view.")
        return ToolResult(
            f"Rendered {view} view of assembly {assembly_eid} (960x720).",
            image_b64=image,
        )
    except (onshape_auth.AuthError, _CadError) as exc:
        return ToolResult(f"Error: {exc}")
    except httpx.HTTPError as exc:
        return ToolResult(f"Error: could not reach Onshape ({exc}).")
