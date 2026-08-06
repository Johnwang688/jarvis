"""Free synthetic checks for cad-bench. No API calls, no network, no cost.

Every number cad-bench prints depends on the harness, so this is where the
harness earns trust (same contract as tests/agentbench_check.py):

  - the geometry helpers are right: world boxes from transforms, penetration
    that treats touching as zero,
  - every grader scores a hand-built correct world 100% and catches its
    specific failure,
  - every task scores ~0 on an empty run AND on a do-nothing run (repair's
    fixture pre-builds two channels — a run that never touches them must not
    get paid for the parts of the world it never touched),
  - every task survives a run cut short (0 and 1 recorded turns) — an empty
    run is fully shaped, a run killed by a provider error is *short*,
  - the bench assembly is deleted even when the run blows up,
  - part resolution strips the invisible LRM marks Onshape puts in names and
    prefers the 1x2x1 profile.

Run after touching `jarvis/cadbench.py`:

    .venv/bin/python tests/cadbench_check.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from jarvis import cadbench as cb
from jarvis import config
from jarvis.tools import onshape as cad

PASSED = 0
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name}{(' — ' + detail) if detail else ''}")


@dataclass
class FakeTurn:
    text: str = ""
    cost_usd: float = 0.0
    latency_s: float = 0.0
    stopped_early: bool = False


DID, VID, EID, PID = "1" * 24, "2" * 24, "3" * 24, "JHD"
ASM = "f" * 24

# The measured 35-hole channel: origin at one end, long axis local Y,
# X centered. Matches the live probe of 2026-08-05, in inches.
LOCAL = {"lo": (-0.5, 0.0, 0.0), "hi": (0.5, 17.5, 0.55)}

PART = {
    "did": DID, "vid": VID, "eid": EID, "pid": PID,
    "name": "1 x 2 x 1 x 35 Aluminum C-Channel (276-2289)",
    "number": "276-2289",
    "bbox": LOCAL,
    "length_in": 17.5, "width_in": 1.0,
    "ref": f"{DID}:{VID}:{EID}:{PID}",
}


def inst(x: float, y: float, z: float, rx: float = 0, ry: float = 0,
         rz: float = 0, part: bool = True, iid: str = "i1") -> cb.Instance:
    transform = cad._matrix(x, y, z, rx, ry, rz)
    return cb.Instance(
        id=iid,
        name=PART["name"],
        pos_in=cad._position_inches(transform),
        rot=(transform[0], transform[1], transform[2],
             transform[4], transform[5], transform[6],
             transform[8], transform[9], transform[10]),
        source=(DID, VID, EID, PID) if part else (DID, VID, "x" * 24, "ZZZ"),
        aabb=cb._world_aabb(LOCAL, transform),
    )


def make_run(facts: dict, instances=None, calls=None, texts=None) -> cb.CadRun:
    run = cb.CadRun(facts=facts)
    run.instances = instances or []
    run.per_turn_calls = calls if calls is not None else []
    run.turns = [FakeTurn(text=t) for t in (texts or [])]
    return run


GOOD_CALLS = [[("cad_find_part", {}), ("cad_insert", {}),
               ("cad_assembly", {}), ("cad_render", {})]]


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------


def test_geometry() -> None:
    print("\ngeometry")
    box = cb._world_aabb(LOCAL, cad._matrix(0, 0, 0, 0, 0, 0))
    check("identity transform keeps the local box",
          box["lo"] == LOCAL["lo"] and box["hi"] == LOCAL["hi"])

    box = cb._world_aabb(LOCAL, cad._matrix(10, 0, 0, 0, 0, 0))
    check("translation moves the box, in inches",
          abs(box["lo"][0] + 0.5 - 10) < 1e-9 and abs(box["hi"][0] - 10.5) < 1e-9)

    # Rz(90): the channel's local Y span becomes a -X span from the anchor.
    box = cb._world_aabb(LOCAL, cad._matrix(18.25, 0.5, 0, 0, 0, 90))
    check("rotation swings the long axis into X",
          abs(box["lo"][0] - 0.75) < 1e-6 and abs(box["hi"][0] - 18.25) < 1e-6)
    check("rotated width lands in Y",
          abs(box["lo"][1] - 0.0) < 1e-6 and abs(box["hi"][1] - 1.0) < 1e-6)

    a = {"lo": (0, 0, 0), "hi": (1, 1, 1)}
    check("separated boxes have zero penetration",
          cb.penetration_in(a, {"lo": (2, 0, 0), "hi": (3, 1, 1)}) == 0.0)
    check("touching boxes have zero penetration",
          cb.penetration_in(a, {"lo": (1, 0, 0), "hi": (2, 1, 1)}) == 0.0)
    check("overlap reports the shallowest axis",
          abs(cb.penetration_in(a, {"lo": (0.9, 0, 0), "hi": (2, 1, 1)}) - 0.1) < 1e-9)

    # The recorded c-channel failure: two rails 12" apart along a 17.5" length.
    a = cb._world_aabb(LOCAL, cad._matrix(0, 0, 0, 0, 0, 0))
    b = cb._world_aabb(LOCAL, cad._matrix(0, 12, 0, 0, 0, 0))
    check("the 12-inch c-channel overlap is detected",
          cb.penetration_in(a, b) > cb.PEN_TOL_IN)


# ---------------------------------------------------------------------------
# graders: correct worlds score 100%, specific failures are caught
# ---------------------------------------------------------------------------


def test_place() -> None:
    print("\nplace")
    facts = cb.fixture_place(ASM, PART)
    good = make_run(facts, [inst(5, 3, 0, rz=90)], GOOD_CALLS)
    check("correct world scores 100%", cb.overall(cb.grade_place(good)) == 1.0)

    wrong = make_run(facts, [inst(5, 3, 2, rz=90)], GOOD_CALLS)
    bad = [c for c in cb.grade_place(wrong) if not c.ok]
    check("wrong position caught", any("position" in c.name for c in bad))

    unrot = make_run(facts, [inst(5, 3, 0)], GOOD_CALLS)
    bad = [c for c in cb.grade_place(unrot) if not c.ok]
    check("missing rotation caught", any("rotation" in c.name for c in bad))

    other = make_run(facts, [inst(5, 3, 0, rz=90, part=False)], GOOD_CALLS)
    bad = [c for c in cb.grade_place(other) if not c.ok]
    check("wrong part caught", any("requested part" in c.name for c in bad))

    noverify = make_run(facts, [inst(5, 3, 0, rz=90)],
                        [[("cad_insert", {})]])
    bad = [c for c in cb.grade_place(noverify) if not c.ok]
    check("skipped verification caught",
          any("readback" in c.name for c in bad)
          and any("render" in c.name for c in bad))


def test_pair() -> None:
    print("\npair")
    facts = cb.fixture_pair(ASM, PART)
    good = make_run(facts, [inst(0, 0, 0), inst(0, 18.5, 0, iid="i2")],
                    GOOD_CALLS)
    check("correct world scores 100%", cb.overall(cb.grade_pair(good)) == 1.0)

    overlap = make_run(facts, [inst(0, 0, 0), inst(0, 12, 0, iid="i2")],
                       GOOD_CALLS)
    bad = [c for c in cb.grade_pair(overlap) if not c.ok]
    check("the 12-inch regression fails the gap check",
          any("gap" in c.name for c in bad))
    check("the 12-inch regression fails clearance",
          any("interpenetrat" in c.name for c in bad))

    wrong_gap = make_run(facts, [inst(0, 0, 0), inst(0, 20, 0, iid="i2")],
                         GOOD_CALLS)
    bad = [c for c in cb.grade_pair(wrong_gap) if not c.ok]
    check("a 2.5-inch gap fails the gap check while clear of overlap",
          any("gap" in c.name for c in bad)
          and not any("interpenetrat" in c.name for c in bad))


def test_frame() -> None:
    print("\nframe")
    facts = cb.fixture_frame(ASM, PART)
    check("frame spacing computed from the measured length",
          facts["d"] == 19.0 and facts["crossbars"][0][0] == 18.25)
    good = make_run(facts, [
        inst(*facts["rails"][0]),
        inst(*facts["rails"][1], iid="i2"),
        inst(*facts["crossbars"][0], rz=90, iid="i3"),
        inst(*facts["crossbars"][1], rz=90, iid="i4"),
    ], GOOD_CALLS)
    check("correct world scores 100%", cb.overall(cb.grade_frame(good)) == 1.0)

    flat = make_run(facts, [
        inst(*facts["rails"][0]),
        inst(*facts["rails"][1], iid="i2"),
        inst(*facts["crossbars"][0], iid="i3"),          # forgot the rotation
        inst(*facts["crossbars"][1], rz=90, iid="i4"),
    ], GOOD_CALLS)
    bad = [c for c in cb.grade_frame(flat) if not c.ok]
    check("an unrotated crossbar is caught", any("crossbar" in c.name for c in bad))

    three = make_run(facts, good.instances[:3], GOOD_CALLS)
    bad = [c for c in cb.grade_frame(three) if not c.ok]
    check("a missing part is caught", any("four" in c.name for c in bad))


def test_revise() -> None:
    print("\nrevise")
    facts = cb.fixture_revise(ASM, PART)
    calls = [[("cad_insert", {}), ("cad_insert", {}), ("cad_assembly", {})],
             [("cad_move", {}), ("cad_assembly", {}), ("cad_render", {})],
             []]
    good = make_run(facts, [inst(0, 0, 0), inst(14, 0, 0, iid="i2")], calls,
                    texts=["done", "moved", "They are 14 inches apart now."])
    check("correct world scores 100%", cb.overall(cb.grade_revise(good)) == 1.0)

    both_moved = make_run(facts, [inst(2, 0, 0), inst(14, 0, 0, iid="i2")],
                          calls, texts=["", "", "14"])
    bad = [c for c in cb.grade_revise(both_moved) if not c.ok]
    check("collateral damage caught — the still rail moved",
          any("never moved" in c.name for c in bad))

    forgot = make_run(facts, [inst(0, 0, 0), inst(12, 0, 0, iid="i2")],
                      calls, texts=["", "", "14"])
    bad = [c for c in cb.grade_revise(forgot) if not c.ok]
    check("an unmoved revision target caught",
          any("new position" in c.name for c in bad))

    silent = make_run(facts, [inst(0, 0, 0), inst(14, 0, 0, iid="i2")],
                      calls, texts=["", "", "they are somewhat apart"])
    bad = [c for c in cb.grade_revise(silent) if not c.ok]
    check("failing to recall the spacing caught",
          any("recalled" in c.name for c in bad))


def test_repair() -> None:
    print("\nrepair")
    facts = {"asm_eid": ASM, "part": PART, "wrong_x": 12.0}
    calls = [[("cad_assembly", {}), ("cad_move", {}), ("cad_assembly", {})]]
    good = make_run(facts, [inst(0, 0, 0), inst(12, 0, 0, iid="i2")], calls)
    check("correct world scores 100%", cb.overall(cb.grade_repair(good)) == 1.0)

    # The do-nothing world is exactly what the fixture built: one channel
    # still 90° wrong. The fixture's own work must earn the agent nothing.
    untouched = make_run(facts, [inst(0, 0, 0), inst(12, 0, 0, rx=90, iid="i2")], [])
    checks = cb.grade_repair(untouched)
    earned = sum(c.weight for c in checks if c.ok)
    check("a do-nothing run earns zero", earned == 0.0,
          f"earned {earned:g}")

    wrong_target = make_run(facts, [inst(0, 0, 0, rx=90),
                                    inst(12, 0, 0, rx=90, iid="i2")], calls)
    bad = [c for c in cb.grade_repair(wrong_target) if not c.ok]
    check("rotating the wrong channel caught",
          any("unrotated" in c.name for c in bad))

    teleported = make_run(facts, [inst(0, 0, 0), inst(3, 0, 0, iid="i2")], calls)
    bad = [c for c in cb.grade_repair(teleported) if not c.ok]
    check("fixing it by moving it caught",
          any("position kept" in c.name for c in bad))


# ---------------------------------------------------------------------------
# empty and short runs
# ---------------------------------------------------------------------------


def all_tasks_facts() -> list[tuple[cb.CadTask, dict]]:
    out = []
    for task in cb.TASKS:
        if task.name == "repair":
            facts = {"asm_eid": ASM, "part": PART, "wrong_x": 12.0}
        else:
            facts = task.fixture(ASM, PART)
        out.append((task, facts))
    return out


def test_empty_and_short_runs() -> None:
    print("\nempty and short runs")
    for task, facts in all_tasks_facts():
        empty = make_run(facts)  # nothing built, nothing called, no turns
        try:
            checks = task.grade(empty)
        except Exception as exc:
            check(f"{task.name}: empty run grades without crashing", False, str(exc))
            continue
        score = cb.overall(checks)
        check(f"{task.name}: empty run scores ~0", score <= 0.10,
              f"scored {score:.0%}")

    for task, facts in all_tasks_facts():
        short = make_run(facts, calls=[[("cad_find_part", {})]], texts=["hm"])
        try:
            task.grade(short)
            check(f"{task.name}: 1-turn run grades without crashing", True)
        except Exception as exc:
            check(f"{task.name}: 1-turn run grades without crashing", False, str(exc))

    run = cb.CadRun()
    check("said(-1) on no turns is empty", run.said(-1) == "")
    check("called(turn=5) on no turns is False", not run.called("cad_move", turn=5))
    check("verified_after_change on no calls is False",
          not run.verified_after_change())


# ---------------------------------------------------------------------------
# the harness: cleanup, and part resolution
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, status: int, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _bundle(path: Path) -> None:
    path.write_text(json.dumps({
        "access_key": "ak", "secret_key": "sk",
        "sandbox": {"did": "a" * 24, "wid": "b" * 24,
                    "name": "Jarvis CAD Sandbox", "url": ""},
        "libraries": [{"did": DID, "name": "VEX V5 - Structure", "url": ""}],
    }), encoding="utf-8")


def test_cleanup_on_crash() -> None:
    print("\ncleanup")
    deleted: list[str] = []
    saved = (cb.find_channel, cb.create_assembly, cb.delete_element,
             cb.read_assembly, cb.agent_mod.Agent)

    class ExplodingAgent:
        def __init__(self, **kwargs):
            pass

        def run_turn(self, prompt):
            raise RuntimeError("provider 400")

    try:
        cb.find_channel = lambda: dict(PART)
        cb.create_assembly = lambda name: ASM
        cb.delete_element = deleted.append
        cb.read_assembly = lambda eid: []
        cb.agent_mod.Agent = ExplodingAgent

        result = cb.run_task("fake/model", cb.TASKS[0])
        check("crashed run still deletes the bench assembly", deleted == [ASM])
        check("crash is recorded as the task error", "provider 400" in result.error)
        check("crashed run scores ~0 instead of crashing the grader",
              cb.overall(result.checks) <= 0.10)

        deleted.clear()

        def bad_fixture(asm_eid, part):
            raise cb.BenchSetupError("no such part")

        task = cb.CadTask(name="broken", tests="", fixture=bad_fixture,
                          turns=lambda f: [], grade=lambda r: [])
        result = cb.run_task("fake/model", task)
        check("failed fixture still deletes the assembly", deleted == [ASM])
        check("setup failure is an error, not a zero score",
              result.error.startswith("setup failed") and not result.checks)
    finally:
        (cb.find_channel, cb.create_assembly, cb.delete_element,
         cb.read_assembly, cb.agent_mod.Agent) = saved


def test_find_channel() -> None:
    print("\npart resolution")
    with tempfile.TemporaryDirectory() as tmp:
        bundle_path = Path(tmp) / "onshape_keys.json"
        _bundle(bundle_path)
        saved_path = config.ONSHAPE_TOKEN_PATH
        config.ONSHAPE_TOKEN_PATH = bundle_path
        cad._library_cache.clear()
        cb._bbox_cache.clear()

        def fake_request(method, url, auth=None, timeout=None, **kwargs):
            path = url[len(config.ONSHAPE_API):]
            if path == f"/documents/d/{DID}/versions":
                return _Response(200, [{"id": VID, "name": "V1",
                                        "createdAt": "2026-01-01T00:00:00Z"}])
            if path == f"/parts/d/{DID}/v/{VID}":
                return _Response(200, [
                    {"name": "1 x 5 x 1 x‎ ‎35‎ ‎Aluminum "
                             "C-Channel (276-2298)",
                     "partNumber": "276-2298", "partId": "JFH", "elementId": EID},
                    {"name": "1 x 2 x 1 x‎ ‎35‎ ‎Aluminum "
                             "C-Channel (276-2289)",
                     "partNumber": "276-2289", "partId": PID, "elementId": EID},
                    {"name": "Shaft 4mm", "partNumber": "276-2011",
                     "partId": "KQZ", "elementId": EID},
                ])
            if path.endswith("/boundingboxes"):
                return _Response(200, {
                    "lowX": -0.0127, "highX": 0.0127,
                    "lowY": 0.0, "highY": 0.4445,
                    "lowZ": 0.0, "highZ": 0.0139725,
                })
            raise AssertionError(f"unexpected call: {method} {path}")

        real = httpx.request
        httpx.request = fake_request
        try:
            part = cb.find_channel()
            check("prefers the 1x2x1 profile", part["pid"] == PID)
            check("name is cleaned of LRM marks", "‎" not in part["name"])
            check("length measured in inches", abs(part["length_in"] - 17.5) < 0.01)
            check("width measured in inches", abs(part["width_in"] - 1.0) < 0.01)
            check("ref matches the tool format",
                  part["ref"] == f"{DID}:{VID}:{EID}:{PID}")
        finally:
            httpx.request = real
            config.ONSHAPE_TOKEN_PATH = saved_path
            cad._library_cache.clear()
            cb._bbox_cache.clear()


def test_registration() -> None:
    print("\nregistration")
    from jarvis import __main__ as main_mod

    check("cad family registered", main_mod.FAMILIES.get("cad") is cb)
    check("every task has a grader and prompts",
          all(callable(t.grade) and callable(t.turns) for t in cb.TASKS))
    names = [t.name for t in cb.TASKS]
    check("the ladder is complete",
          names == ["place", "pair", "frame", "revise", "repair"])


def main() -> int:
    test_geometry()
    test_place()
    test_pair()
    test_frame()
    test_revise()
    test_repair()
    test_empty_and_short_runs()
    test_cleanup_on_crash()
    test_find_channel()
    test_registration()
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  FAILED: {name}")
        return 1
    print("all cadbench checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
