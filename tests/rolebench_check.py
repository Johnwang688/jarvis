"""Free checks for the role bench harness (no paid calls, no network).

    PYTHONPATH=<checkout> python tests/rolebench_check.py
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rolebench import cases, config, judge, quota, report, runners, snapshot, spend

PASSED = 0
FAILED: list[str] = []
FIX = Path(__file__).parent / "fixtures" / "rolebench"


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name}" + (f" - {detail}" if detail else ""))


def raises(fn, exc=Exception) -> bool:
    try:
        fn()
    except exc:
        return True
    return False


def sh(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True,
                          env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": "/dev/null",
                               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}).stdout.strip()


def make_repo(root: Path) -> dict:
    """base, head (the PR), later (the fix): returns the shas."""
    repo = root / "src"
    repo.mkdir()
    sh(repo, "init", "-q", "-b", "main")
    (repo / "tiny.py").write_text("def clamp(x, lo, hi):\n    return x\n")
    (repo / "tiny_hidden_check.py").write_text("print('old version')\n")
    (repo / "rolebench").mkdir()
    (repo / "rolebench" / "secret.json").write_text("{}")
    sh(repo, "add", "-A"); sh(repo, "commit", "-qm", "base"); base = sh(repo, "rev-parse", "HEAD")
    (repo / "tiny.py").write_text("def clamp(x, lo, hi):\n    return max(lo, min(x, hi - 1))\n")
    sh(repo, "commit", "-qam", "pr"); head = sh(repo, "rev-parse", "HEAD")
    (repo / "tiny.py").write_text("def clamp(x, lo, hi):\n    return max(lo, min(x, hi))\n")
    (repo / "tiny_hidden_check.py").write_text("print('hidden')\n")
    sh(repo, "add", "-A"); sh(repo, "commit", "-qm", "fix"); later = sh(repo, "rev-parse", "HEAD")
    return {"repo": repo, "base": base, "head": head, "later": later}


def review_case(shas) -> dict:
    d = json.loads((FIX / "tiny-review.json").read_text())
    d["base"], d["head"] = shas["base"][:12], shas["head"][:12]
    return d


def impl_case(shas) -> dict:
    return {"id": "tiny-impl", "kind": "implement", "pr": None, "size": "S", "base": shas["base"],
            "merged": shas["later"], "brief": "Make clamp work.", "hidden_tests": ["tiny_hidden_check.py"],
            "regression_suites": [], "scope": ["tiny.py"], "fixtures": []}


def test_schema(shas) -> None:
    good = review_case(shas)
    check("a good review case validates", cases.validate(copy.deepcopy(good)) is not None)
    mutations = {
        "missing brief": lambda d: d.pop("brief"),
        "bad kind": lambda d: d.update(kind="triage"),
        "unknown key": lambda d: d.update(groundtruth=[]),
        "bad severity": lambda d: d["ground_truth"][0].update(severity="CRITICAL"),
        "duplicate gt id": lambda d: d["ground_truth"][1].update(id="gt1"),
        "path escape in suites": lambda d: d.update(suites=["../x.py"]),
        "absolute suite": lambda d: d.update(suites=["/etc/passwd"]),
        "non-bool control": lambda d: d.update(control="yes"),
        "no scored ground truth": lambda d: d.update(ground_truth=[]),
        "control with scored ground truth": lambda d: d.update(control=True),
        "seeded without patch": lambda d: d.update(seeded={"description": "x"}),
        "credential in text": lambda d: d.update(brief="key ghp_" + "a" * 30),
        "non-hex sha": lambda d: d.update(base="main"),
    }
    for name, mutate in mutations.items():
        bad = copy.deepcopy(good)
        mutate(bad)
        check(f"rejects: {name}", raises(lambda: cases.validate(bad), cases.CaseError))
    imp = impl_case(shas)
    check("a good implement case validates", cases.validate(copy.deepcopy(imp)) is not None)
    imp2 = copy.deepcopy(imp); imp2["hidden_tests"] = []
    check("rejects: implement with no hidden tests", raises(lambda: cases.validate(imp2), cases.CaseError))


def test_snapshot(shas, root: Path) -> None:
    out = root / "snap"
    case = review_case(shas)
    snap = snapshot.build(case, "r1", repo=shas["repo"], out_root=out)
    check("review snapshot is clean", snapshot.check(snap, case) == [])
    check("snapshot has no remote", sh(snap, "remote") == "")
    check("snapshot has exactly two commits", sh(snap, "rev-list", "--all", "--count") == "2")
    check("diff HEAD~1 is the PR", "hi - 1" in sh(snap, "diff", "HEAD~1"))
    check("the later fix commit is not in the snapshot",
          subprocess.run(["git", "-C", str(snap), "cat-file", "-e", shas["later"]], capture_output=True).returncode != 0)
    check("the later fix is not in the tree", "hi - 1" in (snap / "tiny.py").read_text())
    check("the bench's own files are stripped", not (snap / "rolebench").exists())
    # a snapshot that gained a remote or a later commit is reported
    sh(snap, "remote", "add", "origin", "https://example.invalid/x.git")
    check("hygiene check flags a remote", any("remote" in p for p in snapshot.check(snap, case)))

    icase = impl_case(shas)
    isnap = snapshot.build(icase, "r1", repo=shas["repo"], out_root=out)
    check("implement snapshot is one commit", sh(isnap, "rev-list", "--all", "--count") == "1")
    check("implement snapshot has no hidden tests", not (isnap / "tiny_hidden_check.py").exists())
    check("the merged commit is not in the implement snapshot",
          subprocess.run(["git", "-C", str(isnap), "cat-file", "-e", shas["later"]], capture_output=True).returncode != 0)
    grade = snapshot.restore_hidden(isnap, icase, repo=shas["repo"])
    check("hidden tests are restored in a copy only",
          (grade / "tiny_hidden_check.py").exists() and not (isnap / "tiny_hidden_check.py").exists())
    # spot-check mutant: leave the hidden test in the snapshot -> check must object
    (isnap / "tiny_hidden_check.py").write_text("leak")
    check("MUTANT: a hidden test left in the snapshot is flagged",
          any("hidden test present" in p for p in snapshot.check(isnap, icase)))


def test_spend(root: Path) -> None:
    ledger = spend.Ledger(root / "spend.json", total_cap=1.0, per_run_cap=0.5)
    check("first reservation is the per-run cap", ledger.reserve("a", "m1") == 0.5)
    check("second reservation fits", ledger.reserve("b", "m2") == 0.5)
    check("in-flight reservations count toward the total", raises(lambda: ledger.reserve("c", "m3"), spend.CapReached))
    ledger.settle("a", "m1", 0.1)
    check("settled spend is persisted", abs(spend.Ledger(root / "spend.json").spent() - 0.1) < 1e-9)
    ledger.settle("b", "m2", 0.5)
    check("a run at the per-run cap aborts its model", raises(lambda: ledger.reserve("d", "m2"), spend.CapReached))
    check("another model still runs, with what is left", abs(ledger.reserve("e", "m1") - 0.4) < 1e-9)
    ledger.settle("e", "m1", 0.4)
    check("the total cap stops everything", raises(lambda: ledger.reserve("f", "m1"), spend.CapReached))
    est = spend.estimate("moonshotai/kimi-k3", 1_000_000, 1_000_000)
    check("Kimi K3 is budgeted at $3/$15 per M", est == 18.0 and spend.estimate("openai/gpt-6-luna", 1, 1) is None)


def test_env_and_quota() -> None:
    env = {"PATH": "/bin", "HOME": "/h", "GH_TOKEN": "x", "GITHUB_TOKEN": "y", "OPENROUTER_API_KEY": "z"}
    clean = runners.scrubbed_env(env)
    check("tokens are scrubbed from the environment",
          not {"GH_TOKEN", "GITHUB_TOKEN", "OPENROUTER_API_KEY"} & set(clean) and clean["PATH"] == "/bin")
    argv = runners.wrap_scope(["echo"])
    check("runs are wrapped in a 3G no-swap scope", "MemoryMax=3G" in argv and "MemorySwapMax=0" in argv and argv[-1] == "echo")

    hot = {"windows": [{"name": "5h", "used_percent": 80, "resets_at": 0}]}
    cold = {"windows": [{"name": "5h", "used_percent": 40}]}
    stale = {"windows": [{"name": "5h", "used_percent": 99, "resets_at": 1}]}
    check("a window at 80% skips the claude cell", quota.guard("claude", lambda p: hot)[0] is True)
    check("exactly 75% skips", quota.guard("codex", lambda p: {"windows": [{"used_percent": 75}]})[0] is True)
    check("a cold quota proceeds", quota.guard("claude", lambda p: cold) == (False, ""))
    check("a window past its reset is ignored", quota.guard("claude", lambda p: stale)[0] is False)
    skip, note = quota.guard("claude", lambda p: None)
    check("unknown proceeds with a warning", skip is False and "unknown" in note)
    check("a reader that raises is unknown", quota.guard("codex", lambda p: 1 / 0)[0] is False)
    check("the jarvis harness has no quota guard", quota.guard("jarvis", lambda p: hot) == (False, ""))
    try:
        from jarvis.v2 import diff_review
        check("threshold matches the Diff quick review", diff_review.THRESHOLD == config.QUOTA_THRESHOLD)
    except Exception:
        pass


def test_judge_and_report() -> None:
    gt = [{"id": "gt1", "severity": "MED"}, {"id": "gt2", "severity": "LOW"}, {"id": "gt3", "severity": "HIGH"}]
    ok = {"matches": [{"finding": "F1", "verdict": "match", "gt": "gt1"},
                      {"finding": "F2", "verdict": "invalid"},
                      {"finding": "F3", "verdict": "valid-new", "severity": "LOW"},
                      {"finding": "F4", "verdict": "nit"}]}
    ids, gids = ["F1", "F2", "F3", "F4"], ["gt1", "gt2", "gt3"]
    matches = judge.validate(ok, ids, gids)
    check("a valid judge answer passes", len(matches) == 4)
    bad = {
        "missing a finding": {"matches": ok["matches"][:3]},
        "unknown gt": {"matches": [dict(ok["matches"][0], gt="gt9")] + ok["matches"][1:]},
        "gt on a non-match": {"matches": [ok["matches"][0], dict(ok["matches"][1], gt="gt1")] + ok["matches"][2:]},
        "valid-new without severity": {"matches": ok["matches"][:2] + [{"finding": "F3", "verdict": "valid-new"}, ok["matches"][3]]},
        "bad verdict": {"matches": [dict(ok["matches"][0], verdict="maybe")] + ok["matches"][1:]},
        "repeated finding": {"matches": ok["matches"] + [ok["matches"][0]]},
        "extra field": {"matches": [dict(ok["matches"][0], extra=1)] + ok["matches"][1:]},
        "not an object": [],
    }
    for name, doc in bad.items():
        check(f"judge rejects: {name}", raises(lambda: judge.validate(doc, ids, gids), judge.JudgeError))
    s = judge.score(matches, gt)
    check("weighted recall is 3/9", abs(s["weighted_recall"] - 3 / 9) < 1e-9)
    check("MED+ recall is 1/2", s["medplus_recall"] == 0.5)
    check("false positives, valid-new and nits are counted", (s["false_positives"], s["valid_new"], s["nits"]) == (1, 1, 1))
    ctl = judge.score([{"finding": "F1", "verdict": "invalid"}], [])
    check("a control case has no recall, only false positives",
          ctl["weighted_recall"] is None and ctl["medplus_recall"] is None and ctl["false_positives"] == 1)
    rows = [{"harness": "claude", "model": "haiku", "cost": 0.1, "wall": 10}, {"harness": "claude", "model": "haiku", "cost": 0.3, "wall": None},
            {"harness": "jarvis", "model": "haiku", "cost": 0.2, "wall": 5}]
    agg = report.by_model(rows, ["cost", "wall"])
    c = agg[("claude", "haiku")]["cost"]
    check("report ranges are per (harness, model)", (c["lo"], c["hi"], c["n"]) == (0.1, 0.3, 2) and abs(c["mean"] - 0.2) < 1e-9)
    check("None is skipped, not zero", agg[("claude", "haiku")]["wall"]["n"] == 1)
    check("a measure never taken is None", report.span([None]) is None)


def main() -> int:
    print("role bench checks (free, no API)")
    with tempfile.TemporaryDirectory(prefix="rolebench-test-") as tmp:
        root = Path(tmp)
        shas = make_repo(root)
        test_schema(shas)
        test_snapshot(shas, root)
        test_spend(root)
    test_env_and_quota()
    test_judge_and_report()
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  - {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
