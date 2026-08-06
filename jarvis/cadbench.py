"""cad-bench: can Jarvis actually build things in Onshape?

The owner's observation — CAD works on basic tasks and degrades badly on
complex ones — was exactly that, an observation. This bench turns it into a
measurement, so the CAD work items (readback fidelity, the CAD skill, mate
connectors) can be judged by whether the numbers move.

Scoring follows agent-bench's rule: **grade the world the agent left behind,
not the prose it wrote.** Every check reads the resulting assembly back from
Onshape — instance count, positions, rotations, clearances computed from real
part bounding boxes — never the model's summary of what it claims to have
built. The agent has no way to see or set its own score.

Categories, so one task feeds several ratings:

    placement   the right part in the stated position and orientation
    clearance   parts that must not interpenetrate, don't — computed from
                bounding boxes, which the render channel can't be graded on
    revision    changing an existing assembly without collateral damage —
                the case absolute placement without mates is predicted to fail
    discipline  the insert -> readback -> render verification loop actually ran

Sandbox hygiene: every run creates its own assembly (`cadbench-<task>-<runid>`)
inside the pinned sandbox document and deletes it in a `finally:`. The write
pin is untouched — all writes still go through the same sandbox document
`tests/onshape_check.py` asserts on. A failed cleanup is printed loudly with
the assembly name so the owner can delete it by hand.

This family costs real API calls on both ends: OpenRouter for the model and
Onshape quota for the tools *and* the graders. Endpoint shapes used here were
verified live 2026-08-05 (`tests/probe_cadbench_api.py`,
`tests/probe_cadbench_delete.py`) — the generated Onshape docs have been wrong
twice before in exactly this area.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from . import agent as agent_mod
from . import config, onshape_auth
from .agentbench import Check, by_category, overall
from .bench import TaskResult
from .tools import onshape as cad

CATEGORIES = ["placement", "clearance", "revision", "discipline"]

# No cad_create_assembly: the harness creates each run's assembly itself, so
# cleanup is bounded to exactly the elements the bench made and the graders
# always know which eid to read back.
CAD_TOOLS = [
    "cad_status", "cad_find_part", "cad_insert", "cad_assembly",
    "cad_move", "cad_delete", "cad_render",
]

_INCH = 0.0254

# Position tolerance: half a hundredth of the VEX half-inch grid is generous
# for float noise and unforgiving of a wrong placement.
POS_TOL_IN = 0.05
# Rotation entries are compared on the 3x3 block; 0.02 ~= 1.1 degrees.
ROT_TOL = 0.02
# Two parts may touch (a frame is butt-joined); penetrating deeper than this
# on every axis at once counts as overlap.
PEN_TOL_IN = 0.05


# ---------------------------------------------------------------------------
# Onshape access for the harness (fixtures + graders — not the agent's tools)
# ---------------------------------------------------------------------------


class BenchSetupError(RuntimeError):
    """The bench cannot run — auth, library, or sandbox trouble. Not a model
    failure; surfaces as the task's error, never as a zero score."""


def _api(method: str, path: str, **kwargs) -> httpx.Response:
    response = httpx.request(
        method, config.ONSHAPE_API + path,
        auth=onshape_auth.auth(), timeout=60, **kwargs,
    )
    if response.status_code != 200:
        raise BenchSetupError(
            f"Onshape returned {response.status_code} for {method} {path}: "
            f"{response.text[:200]}"
        )
    return response


def _clean(name: str) -> str:
    """Part names carry invisible LRM marks (u200e) — strip them before
    matching or printing anything a prompt or grader compares against."""
    return re.sub(r"[‎‏]", "", name or "").strip()


def find_channel() -> dict:
    """The bench part: a 35-hole aluminum c-channel from the configured
    libraries, preferring the 1x2x1 profile. Deterministic — sorted by name —
    and measured: the local bounding box comes back with it, in inches."""
    libs = onshape_auth.libraries()
    if not libs:
        raise BenchSetupError(
            "no parts libraries configured — run `jarvis auth onshape --redo`."
        )
    candidates: list[tuple[str, dict]] = []
    for lib in libs:
        try:
            entry = cad._resolve_library(lib["did"])
        except cad._CadError:
            continue
        for part in entry["parts"]:
            name = _clean(part.get("name", ""))
            if "c-channel" in name.lower() and "35" in name:
                candidates.append((name, {
                    "did": lib["did"], "vid": entry["vid"],
                    "eid": part["elementId"], "pid": part["partId"],
                    "name": name,
                    "number": part.get("partNumber") or "",
                }))
    if not candidates:
        raise BenchSetupError(
            "no 35-hole c-channel found in any configured library — cad-bench "
            "expects the official VEX V5 structure library."
        )
    candidates.sort(key=lambda c: (0 if "1 x 2 x 1" in c[0] else 1, c[0]))
    part = candidates[0][1]
    bbox = _part_bbox(part["did"], part["vid"], part["eid"], part["pid"])
    part["bbox"] = bbox
    part["length_in"] = bbox["hi"][1] - bbox["lo"][1]  # long axis is local Y
    part["width_in"] = bbox["hi"][0] - bbox["lo"][0]
    part["ref"] = f"{part['did']}:{part['vid']}:{part['eid']}:{part['pid']}"
    return part


_bbox_cache: dict[tuple, dict] = {}


def _part_bbox(did: str, vid: str, eid: str, pid: str) -> dict:
    """A part's local-frame bounding box in inches: {lo: (x,y,z), hi: (x,y,z)}.
    Endpoint verified live 2026-08-05 — flat lowX..highZ payload, meters."""
    key = (did, vid, eid, pid)
    if key not in _bbox_cache:
        data = _api(
            "GET", f"/parts/d/{did}/v/{vid}/e/{eid}/partid/{pid}/boundingboxes"
        ).json()
        _bbox_cache[key] = {
            "lo": tuple(data[k] / _INCH for k in ("lowX", "lowY", "lowZ")),
            "hi": tuple(data[k] / _INCH for k in ("highX", "highY", "highZ")),
        }
    return _bbox_cache[key]


def create_assembly(name: str) -> str:
    sb = onshape_auth.sandbox()
    response = _api(
        "POST", f"/assemblies/d/{sb['did']}/w/{sb['wid']}", json={"name": name}
    )
    return response.json()["id"]


def delete_element(eid: str) -> None:
    sb = onshape_auth.sandbox()
    _api("DELETE", f"/elements/d/{sb['did']}/w/{sb['wid']}/e/{eid}")


def insert_part(asm_eid: str, part: dict, x: float, y: float, z: float,
                rx: float = 0, ry: float = 0, rz: float = 0) -> None:
    """Fixture-side insert (the `repair` task pre-builds a broken assembly)."""
    sb = onshape_auth.sandbox()
    _api(
        "POST",
        f"/assemblies/d/{sb['did']}/w/{sb['wid']}/e/{asm_eid}/transformedinstances",
        json={"transformGroups": [{
            "instances": [{
                "documentId": part["did"], "versionId": part["vid"],
                "elementId": part["eid"], "partId": part["pid"],
                "isAssembly": False, "isWholePartStudio": False,
            }],
            "transform": cad._matrix(x, y, z, rx, ry, rz),
        }]},
    )


# ---------------------------------------------------------------------------
# readback — what the graders see
# ---------------------------------------------------------------------------


@dataclass
class Instance:
    """One occurrence, with its geometry resolved into grader-friendly form."""

    id: str                       # occurrence path joined with '/'
    name: str
    pos_in: tuple[float, float, float]
    rot: tuple[float, ...]        # 3x3 rotation block, row-major
    source: tuple[str, str, str, str]  # did, documentVersion, eid, partId
    aabb: dict | None             # world-frame box in inches, or None

    def rotated_like(self, rx: float, ry: float, rz: float,
                     tol: float = ROT_TOL) -> bool:
        expect = cad._rotation(rx, ry, rz)
        flat = [expect[i][j] for i in range(3) for j in range(3)]
        return all(abs(a - b) <= tol for a, b in zip(self.rot, flat))

    def at(self, x: float, y: float, z: float, tol: float = POS_TOL_IN) -> bool:
        return all(abs(a - b) <= tol for a, b in zip(self.pos_in, (x, y, z)))


# One source of truth for the corner-transform math — the tools' readback and
# the bench's graders must never disagree about where a box is.
_world_aabb = cad._world_aabb


def penetration_in(a: dict, b: dict) -> float:
    """How deeply two world AABBs interpenetrate: the smallest per-axis
    overlap, or 0.0 if any axis separates them. Touching is 0."""
    depths = []
    for i in range(3):
        depth = min(a["hi"][i], b["hi"][i]) - max(a["lo"][i], b["lo"][i])
        if depth <= 0:
            return 0.0
        depths.append(depth)
    return min(depths)


def read_assembly(asm_eid: str) -> list[Instance]:
    """The graded world: every top-level occurrence with its transform and a
    world bbox computed from the *actual* inserted part's measured box."""
    sb = onshape_auth.sandbox()
    data = _api(
        "GET", f"/assemblies/d/{sb['did']}/w/{sb['wid']}/e/{asm_eid}"
    ).json()
    root = data.get("rootAssembly", {})
    sources: dict[str, dict] = {}
    for source in [root, *data.get("subAssemblies", [])]:
        for instance in source.get("instances", []):
            sources[instance.get("id", "")] = instance
    out: list[Instance] = []
    for occurrence in root.get("occurrences", []):
        path = occurrence.get("path", [])
        transform = occurrence.get("transform", [])
        if len(transform) != 16:
            continue
        info = sources.get(path[-1] if path else "", {})
        # The definition names the version key `documentVersion`, not
        # `versionId` (verified live 2026-08-05).
        source = (
            info.get("documentId", ""), info.get("documentVersion", ""),
            info.get("elementId", ""), info.get("partId", ""),
        )
        aabb = None
        if all(source):
            try:
                aabb = _world_aabb(_part_bbox(*source), transform)
            except BenchSetupError:
                aabb = None
        out.append(Instance(
            id="/".join(path),
            name=_clean(info.get("name", "?")),
            pos_in=cad._position_inches(transform),
            rot=(
                transform[0], transform[1], transform[2],
                transform[4], transform[5], transform[6],
                transform[8], transform[9], transform[10],
            ),
            source=source,
            aabb=aabb,
        ))
    return out


# ---------------------------------------------------------------------------
# a graded run
# ---------------------------------------------------------------------------


@dataclass
class CadRun:
    """Everything a grader may look at."""

    facts: dict[str, Any] = field(default_factory=dict)
    instances: list[Instance] = field(default_factory=list)
    turns: list[Any] = field(default_factory=list)
    per_turn_calls: list[list[tuple[str, dict]]] = field(default_factory=list)
    transcript: str = ""
    error: str = ""

    @property
    def calls(self) -> list[tuple[str, dict]]:
        return [call for turn in self.per_turn_calls for call in turn]

    def called(self, name: str, turn: int | None = None) -> bool:
        if turn is None:
            source = self.calls
        else:
            # A turn that never ran made no calls — a run cut short by a
            # provider error must score zero, not crash the grader.
            try:
                source = self.per_turn_calls[turn]
            except IndexError:
                return False
        return any(n == name for n, _ in source)

    def said(self, turn: int | None = None) -> str:
        if turn is None:
            return " ".join(t.text for t in self.turns).lower()
        try:
            return self.turns[turn].text.lower()
        except IndexError:
            return ""

    def verified_after_change(self) -> bool:
        """Did a readback (cad_assembly) come after the last mutation? The
        core of the insert -> readback -> render discipline."""
        names = [n for n, _ in self.calls]
        mutations = [i for i, n in enumerate(names)
                     if n in ("cad_insert", "cad_move", "cad_delete")]
        readbacks = [i for i, n in enumerate(names) if n == "cad_assembly"]
        if not mutations:
            return False
        return bool(readbacks) and max(readbacks) > max(mutations)

    def pair_penetration(self) -> float:
        """The deepest interpenetration between any two instances with known
        boxes. 0.0 when nothing overlaps — or when nothing was built, which is
        why every clearance check must be a conjunction with having built."""
        deepest = 0.0
        boxed = [i for i in self.instances if i.aabb]
        for i, a in enumerate(boxed):
            for b in boxed[i + 1:]:
                deepest = max(deepest, penetration_in(a.aabb, b.aabb))
        return deepest


@dataclass
class CadTask:
    name: str
    tests: str
    fixture: Callable[[str, dict], dict]   # (asm_eid, part) -> facts
    turns: Callable[[dict], list[str]]     # facts -> prompts
    grade: Callable[[CadRun], list[Check]]
    max_steps: int = 20


# ---------------------------------------------------------------------------
# fixtures — resolve the part, pre-build what the task needs, pin the numbers
# ---------------------------------------------------------------------------


def fixture_place(asm_eid: str, part: dict) -> dict:
    return {"asm_eid": asm_eid, "part": part,
            "x": 5.0, "y": 3.0, "z": 0.0, "rz": 90.0}


def fixture_pair(asm_eid: str, part: dict) -> dict:
    return {"asm_eid": asm_eid, "part": part, "gap": 1.0}


def fixture_frame(asm_eid: str, part: dict) -> dict:
    length = part["length_in"]
    width = part["width_in"]
    # Rails run along local Y at x=0 and x=D. Crossbars are the same channel
    # rotated 90° about Z, centered between the rails; a rotated channel spans
    # [anchor - length, anchor] in X, so centering puts its anchor at
    # (D + length) / 2. D leaves >= 0.25" clear of each rail's inner face,
    # rounded up to the VEX half-inch grid.
    clearance = 0.25
    d = length + width + 2 * clearance
    d = round((d * 2 + 0.999) // 1 / 2, 2)  # ceil to the 0.5" grid
    cross_x = (d + length) / 2
    y_far = length - width / 2  # crossbar centerline inside the rails' span
    y_near = width / 2
    return {
        "asm_eid": asm_eid, "part": part, "d": d,
        "rails": [(0.0, 0.0, 0.0), (d, 0.0, 0.0)],
        "crossbars": [(cross_x, y_near, 0.0), (cross_x, y_far, 0.0)],
    }


def fixture_revise(asm_eid: str, part: dict) -> dict:
    return {"asm_eid": asm_eid, "part": part, "x0": 12.0, "x1": 14.0}


def fixture_repair(asm_eid: str, part: dict) -> dict:
    # Two rails that should both lie flat, long axis along Y — one pre-built
    # 90° wrong about X. The break the task hands over is real, not described.
    insert_part(asm_eid, part, 0, 0, 0)
    insert_part(asm_eid, part, 12, 0, 0, rx=90)
    return {"asm_eid": asm_eid, "part": part, "wrong_x": 12.0}


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------


def _part_line(facts: dict) -> str:
    part = facts["part"]
    number = f" (part number {part['number']})" if part["number"] else ""
    return f"the {part['name']}{number}"


def turns_place(facts: dict) -> list[str]:
    return [
        f"In the assembly with eid {facts['asm_eid']} in the CAD sandbox, "
        f"insert one of {_part_line(facts)} at position "
        f"({facts['x']}, {facts['y']}, {facts['z']}) inches, rotated "
        f"{facts['rz']} degrees about the Z axis. Verify the result before "
        "you finish."
    ]


def turns_pair(facts: dict) -> list[str]:
    return [
        f"In the assembly with eid {facts['asm_eid']}, place two of "
        f"{_part_line(facts)} end-to-end along the assembly Y axis: the first "
        f"at the origin, unrotated, and the second past it in +Y with exactly "
        f"a {facts['gap']} inch gap between them. They must not overlap. "
        "Verify the result before you finish."
    ]


def turns_frame(facts: dict) -> list[str]:
    (r1, r2), (c1, c2) = facts["rails"], facts["crossbars"]
    return [
        f"In the assembly with eid {facts['asm_eid']}, build a rectangular "
        f"frame out of four of {_part_line(facts)}:\n"
        f"- rail A at ({r1[0]}, {r1[1]}, {r1[2]}) inches, unrotated\n"
        f"- rail B at ({r2[0]}, {r2[1]}, {r2[2]}) inches, unrotated\n"
        f"- crossbar C at ({c1[0]}, {c1[1]}, {c1[2]}) inches, rotated 90 "
        "degrees about Z\n"
        f"- crossbar D at ({c2[0]}, {c2[1]}, {c2[2]}) inches, rotated 90 "
        "degrees about Z\n"
        "Nothing may interpenetrate. Verify the result before you finish."
    ]


def turns_revise(facts: dict) -> list[str]:
    return [
        f"In the assembly with eid {facts['asm_eid']}, insert two of "
        f"{_part_line(facts)} as parallel rails: one at the origin and one at "
        f"({facts['x0']}, 0, 0) inches, both unrotated. Verify before you "
        "finish.",
        f"Move the rail at x={facts['x0']} outward to x={facts['x1']}. Leave "
        "the other rail exactly where it is.",
        "Without re-checking the assembly, what is the distance in inches "
        "between the two rails' anchor points now?",
    ]


def turns_repair(facts: dict) -> list[str]:
    return [
        f"The assembly with eid {facts['asm_eid']} holds two c-channels that "
        "should both be lying flat with their long axis along the assembly Y "
        "axis, unrotated. One of them was inserted with the wrong "
        "orientation. Find which one, and fix its orientation in place — keep "
        "its X/Y/Z position, and do not touch the other channel. Verify the "
        "result before you finish."
    ]


# ---------------------------------------------------------------------------
# graders
# ---------------------------------------------------------------------------


def _right_part(run: CadRun, instance: Instance) -> bool:
    part = run.facts["part"]
    return instance.source[2] == part["eid"] and instance.source[3] == part["pid"]


def grade_place(run: CadRun) -> list[Check]:
    one = run.instances[0] if len(run.instances) == 1 else None
    f = run.facts
    return [
        Check("placement", "exactly one instance", one is not None, weight=2),
        Check("placement", "it is the requested part",
              one is not None and _right_part(run, one)),
        Check("placement", "position as stated",
              one is not None and one.at(f["x"], f["y"], f["z"]), weight=2),
        Check("placement", "rotation as stated",
              one is not None and one.rotated_like(0, 0, f["rz"]), weight=2),
        Check("discipline", "readback after the last change",
              run.verified_after_change()),
        Check("discipline", "rendered the result", run.called("cad_render")),
    ]


def grade_pair(run: CadRun) -> list[Check]:
    built = (len(run.instances) == 2
             and all(_right_part(run, i) for i in run.instances))
    gap_ok = origin_ok = False
    if built and all(i.aabb for i in run.instances):
        a, b = sorted(run.instances, key=lambda i: i.aabb["lo"][1])
        gap = b.aabb["lo"][1] - a.aabb["hi"][1]
        gap_ok = abs(gap - run.facts["gap"]) <= 0.1
        origin_ok = a.at(0, 0, 0)
    return [
        Check("placement", "both instances of the right part built",
              built, weight=2),
        Check("placement", "first sits at the origin", built and origin_ok),
        # The c-channel regression: a 12" offset on a 17.5" part. The gap is
        # computed from the parts' real boxes, so a guessed offset fails here
        # even when the prose sounds confident.
        Check("placement", "stated gap between the parts",
              built and gap_ok, weight=3),
        Check("clearance", "built both and nothing interpenetrates",
              built and run.pair_penetration() <= PEN_TOL_IN, weight=3),
        Check("discipline", "readback after the last change",
              run.verified_after_change()),
        Check("discipline", "rendered the result", run.called("cad_render")),
    ]


def grade_frame(run: CadRun) -> list[Check]:
    f = run.facts
    built = (len(run.instances) == 4
             and all(_right_part(run, i) for i in run.instances))
    rails = [i for i in run.instances if i.rotated_like(0, 0, 0)]
    crossbars = [i for i in run.instances if i.rotated_like(0, 0, 90)]
    rails_ok = (
        len(rails) == 2
        and any(i.at(*f["rails"][0]) for i in rails)
        and any(i.at(*f["rails"][1]) for i in rails)
    )
    cross_ok = (
        len(crossbars) == 2
        and any(i.at(*f["crossbars"][0]) for i in crossbars)
        and any(i.at(*f["crossbars"][1]) for i in crossbars)
    )
    return [
        Check("placement", "four instances of the right part", built, weight=2),
        Check("placement", "both rails placed as stated",
              built and rails_ok, weight=2),
        Check("placement", "both crossbars rotated and placed as stated",
              built and cross_ok, weight=3),
        Check("clearance", "built the frame and nothing interpenetrates",
              built and run.pair_penetration() <= PEN_TOL_IN, weight=3),
        Check("discipline", "readback after the last change",
              run.verified_after_change()),
        Check("discipline", "rendered the result", run.called("cad_render")),
    ]


def grade_revise(run: CadRun) -> list[Check]:
    f = run.facts
    built = (len(run.instances) == 2
             and all(_right_part(run, i) for i in run.instances))
    stayed = moved = None
    if built:
        stayed = next((i for i in run.instances if i.at(0, 0, 0)), None)
        moved = next((i for i in run.instances if i.at(f["x1"], 0, 0)), None)
    distance = str(f["x1"]).rstrip("0").rstrip(".")
    return [
        Check("placement", "both rails built", built),
        Check("revision", "revised rail ended at the new position",
              built and moved is not None, weight=3),
        Check("revision", "the other rail never moved",
              built and stayed is not None, weight=2),
        Check("revision", "rotations survived the move",
              built and all(i.rotated_like(0, 0, 0) for i in run.instances)),
        Check("revision", "recalled the new spacing unaided",
              distance in run.said(-1).replace(",", ""), weight=2),
        Check("discipline", "readback after the last change",
              run.verified_after_change()),
        Check("discipline", "rendered the result", run.called("cad_render")),
    ]


def grade_repair(run: CadRun) -> list[Check]:
    f = run.facts
    two = len(run.instances) == 2
    fixed = anchored = untouched = False
    if two:
        wrong = next((i for i in run.instances
                      if abs(i.pos_in[0] - f["wrong_x"]) <= POS_TOL_IN), None)
        right = next((i for i in run.instances if i.at(0, 0, 0)), None)
        fixed = wrong is not None and wrong.rotated_like(0, 0, 0)
        anchored = wrong is not None and wrong.at(f["wrong_x"], 0, 0)
        untouched = right is not None and right.rotated_like(0, 0, 0)
    # Every passive property here (two channels, one untouched) was true
    # before the agent did anything — the fixture built it that way. Credit
    # only lands together with the fix, and the names say so, or a do-nothing
    # run would score on the parts of the world it never touched.
    return [
        Check("revision", "the wrong channel is now unrotated",
              fixed, weight=3),
        Check("revision", "fixed it in place — position kept",
              fixed and anchored, weight=2),
        Check("revision", "fixed it and left the correct channel alone",
              fixed and untouched, weight=2),
        Check("revision", "fixed it with still exactly two channels",
              fixed and two),
        Check("clearance", "fixed it and nothing interpenetrates",
              fixed and run.pair_penetration() <= PEN_TOL_IN),
        Check("discipline", "read the assembly before changing it",
              run.called("cad_assembly")
              and bool(run.calls) and run.calls[0][0] != "cad_move"),
        Check("discipline", "readback after the last change",
              run.verified_after_change()),
    ]


# ---------------------------------------------------------------------------
# running one task
# ---------------------------------------------------------------------------


def run_task(model: str, task: CadTask) -> TaskResult:
    result = TaskResult(task=task.name, passed=False)
    run = CadRun()

    runid = time.strftime("%m%d-%H%M%S")
    asm_name = f"cadbench-{task.name}-{runid}"
    asm_eid = ""
    try:
        part = find_channel()
        asm_eid = create_assembly(asm_name)
        run.facts = task.fixture(asm_eid, part)
    except (BenchSetupError, onshape_auth.AuthError, httpx.HTTPError) as exc:
        result.error = f"setup failed: {exc}"
        if asm_eid:
            _cleanup(asm_eid, asm_name)
        return result

    transcript: list[str] = []
    current: list[tuple[str, dict]] = []

    def on_event(kind: str, data) -> None:
        if kind == "tool_start":
            name, raw = data
            try:
                args = json.loads(raw or "{}")
            except ValueError:
                args = {}
            current.append((name, args if isinstance(args, dict) else {}))
        elif kind == "tool_end":
            transcript.append(str(data[1]))
        elif kind in ("text", "interim_text"):
            transcript.append(str(data))

    def deny(tool, args) -> bool:  # no cad_ tool is dangerous; asks mean drift
        return False

    jarvis = agent_mod.Agent(
        model=model,
        system=config.SYSTEM_PROMPT,
        tool_names=CAD_TOOLS,
        max_steps=task.max_steps,
        approve=deny,
        on_event=on_event,
    )

    try:
        for index, prompt in enumerate(task.turns(run.facts)):
            current = []
            turn = jarvis.run_turn(prompt)
            run.turns.append(turn)
            run.per_turn_calls.append(list(current))
            result.cost_usd += turn.cost_usd
            result.latency_s += turn.latency_s
            if turn.stopped_early:
                result.error = f"turn {index + 1} hit the step cap"
    except Exception as exc:
        run.error = result.error = f"{type(exc).__name__}: {exc}"

    run.transcript = "\n".join(transcript)
    try:
        run.instances = read_assembly(asm_eid)
    except (BenchSetupError, httpx.HTTPError) as exc:
        run.error = run.error or f"readback failed: {exc}"
    finally:
        _cleanup(asm_eid, asm_name)

    result.text = run.turns[-1].text if run.turns else ""
    result.calls = list(run.calls)

    # A failed check is only worth acting on once you have seen what the
    # agent actually did. JARVIS_CADBENCH_TRACE=1 keeps the whole exchange.
    if os.environ.get("JARVIS_CADBENCH_TRACE") == "1":
        trace = Path(tempfile.gettempdir()) / f"cadbench-{task.name}-{runid}.log"
        lines = []
        for i, turn_calls in enumerate(run.per_turn_calls):
            lines.append(f"== turn {i + 1} calls ==")
            lines += [f"  {name} {json.dumps(args)}" for name, args in turn_calls]
        lines.append("== transcript ==")
        lines.append(run.transcript)
        trace.write_text("\n".join(lines), encoding="utf-8")
        print(f"[cad-bench] trace kept at {trace}")

    try:
        checks = task.grade(run)
    except Exception as exc:  # a grader bug must not read as a model failure
        checks = [Check("placement", f"grader crashed: {exc}", False)]

    result.checks = checks
    earned = sum(c.weight for c in checks if c.ok)
    possible = sum(c.weight for c in checks) or 1.0
    result.passed = earned == possible
    result.detail = f"{earned:g}/{possible:g} checks"
    return result


def _cleanup(asm_eid: str, asm_name: str) -> None:
    """Delete the bench's assembly. Never raises — but a leftover element in
    the owner's sandbox is called out loudly, by name."""
    try:
        delete_element(asm_eid)
    except Exception as exc:
        print(
            f"[cad-bench] CLEANUP FAILED — assembly {asm_name!r} "
            f"(eid {asm_eid}) is still in the sandbox document: {exc}"
        )


# ---------------------------------------------------------------------------
# the tasks
# ---------------------------------------------------------------------------

TASKS: list[CadTask] = [
    CadTask(
        name="place",
        tests="one part at a stated position and rotation",
        fixture=fixture_place, turns=turns_place, grade=grade_place,
        max_steps=14,
    ),
    CadTask(
        name="pair",
        tests="a stated gap that requires knowing the part's length",
        fixture=fixture_pair, turns=turns_pair, grade=grade_pair,
        max_steps=20,
    ),
    CadTask(
        name="frame",
        tests="four placements, two rotated, nothing interpenetrating",
        fixture=fixture_frame, turns=turns_frame, grade=grade_frame,
        max_steps=30,
    ),
    CadTask(
        name="revise",
        tests="move one rail without collateral damage, recall the spacing",
        fixture=fixture_revise, turns=turns_revise, grade=grade_revise,
        max_steps=22,
    ),
    CadTask(
        name="repair",
        tests="find the mis-rotated part and fix it in place",
        fixture=fixture_repair, turns=turns_repair, grade=grade_repair,
        max_steps=18,
    ),
]

DEFAULT_ROSTER = ["openai/gpt-5.6-luna"]


def score_report(console, results: dict[str, list[TaskResult]]) -> None:
    """Category ratings per model, printed after the standard bench tables."""
    from rich.table import Table

    table = Table(title="cad-bench ratings", title_style="bold", header_style="dim")
    table.add_column("model")
    for category in CATEGORIES:
        table.add_column(category, justify="right")
    table.add_column("OVERALL", justify="right", style="bold")

    for model, task_results in results.items():
        checks = [c for r in task_results for c in r.checks]
        if not checks:
            continue
        totals = by_category(checks)
        row = [model]
        for category in CATEGORIES:
            earned, possible = totals.get(category, (0.0, 0.0))
            if not possible:
                row.append("[dim]—[/dim]")
                continue
            rating = earned / possible
            color = "green" if rating >= 0.85 else "yellow" if rating >= 0.6 else "red"
            row.append(f"[{color}]{rating:.0%}[/{color}]")
        total = overall(checks)
        color = "green" if total >= 0.85 else "yellow" if total >= 0.6 else "red"
        row.append(f"[{color}]{total:.0%}[/{color}]")
        table.add_row(*row)

    console.print(table)
