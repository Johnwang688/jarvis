"""Free checks for the role bench harness (no paid calls, no network).

    PYTHONPATH=<checkout> OPENROUTER_API_KEY=sk-dummy-not-a-key HOME=<tmp> \\
        python tests/rolebench_check.py

Needs /usr/bin/bwrap and a user systemd: the sandbox checks run real cells
(harmless `test -e` / `env` probes only; nothing is ever printed but
"present"/"absent").
"""

from __future__ import annotations

import copy
import io
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
import contextlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rolebench import (audit, brief, cases, config, findings, flows, jarvis_worker, judge, netcell, prices,  # noqa: E402
                       quota, report, runlog, runners, sandbox, snapshot, spend)

PASSED = 0
FAILED: list[str] = []
FIX = Path(__file__).parent / "fixtures" / "rolebench"
REAL_HOME = config.real_home()
NO_QUOTA = "rolebench_check: fakes only"

# Nothing here reads the live daemon's quota or OpenRouter's price list: the
# defaults every Deps and judge call resolve at call time are fakes, and the
# price fetcher refuses. Tests that need a reader or a price pass their own.
COLD = {"windows": [{"name": "5h", "used_percent": 10.0}, {"name": "weekly", "used_percent": 20.0}]}
REAL_ENDPOINT_PRICE = prices.endpoint_price


def _no_network(*args, **kwargs):
    raise AssertionError("a test reached for the network")


def FAKE_PRICE(model):
    return (1.0, 5.0, 8000)


prices._http_get = _no_network
prices.endpoint_price = FAKE_PRICE
quota.daemon_reader = lambda provider: copy.deepcopy(COLD)


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


GIT_ENV = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def sh(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True,
                          env=GIT_ENV).stdout.strip()


def make_repo(root: Path) -> dict:
    """base, head (the PR), later (the fix): returns the shas."""
    repo = root / "src"
    repo.mkdir()
    sh(repo, "init", "-q", "-b", "main")
    (repo / "tiny.py").write_text("def clamp(x, lo, hi):\n    return x\n")
    (repo / "tiny_hidden_check.py").write_text("print('old version')\n")
    (repo / "tiny_regress_check.py").write_text("import sys; sys.exit(0 if open('tiny.py').read() else 1)\n")
    (repo / "rolebench").mkdir()
    (repo / "rolebench" / "secret.json").write_text("{}")
    (repo / "hud").mkdir()
    (repo / "hud" / "app.ts").write_text("export const x = 1\n")
    (repo / "hud" / "node_modules" / "vitest").mkdir(parents=True)        # untracked, like the real one
    (repo / "hud" / "node_modules" / "vitest" / "index.js").write_text("// fake\n")
    (repo / ".gitignore").write_text("hud/node_modules\n")
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
            "regression_suites": ["tiny_regress_check.py"], "scope": ["tiny.py"], "fixtures": []}


@contextlib.contextmanager
def env(**values):
    old = dict(os.environ)
    for k, v in values.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(old)


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


# --- snapshots ------------------------------------------------------------------------


def test_snapshot(shas, root: Path) -> None:
    out = root / "snap"
    case = review_case(shas)
    snap = snapshot.build(case, "r1-claude-haiku", repo=shas["repo"], out_root=out)
    check("review snapshot is clean", snapshot.check(snap, case) == [])
    check("the snapshot's directory name is random: no case, run or model in it",
          len(snap.name) == 16 and all(w not in snap.name for w in ("tiny", "r1", "claude", "haiku")))
    check("snapshot has no remote", sh(snap, "remote") == "")
    check("snapshot has exactly two commits", sh(snap, "rev-list", "--all", "--count") == "2")
    check("diff HEAD~1 is the PR", "hi - 1" in sh(snap, "diff", "HEAD~1"))
    check("the later fix commit is not in the snapshot",
          subprocess.run(["git", "-C", str(snap), "cat-file", "-e", shas["later"]], capture_output=True).returncode != 0)
    check("the later fix is not in the tree", "hi - 1" in (snap / "tiny.py").read_text())
    check("the bench's own files are stripped", not (snap / "rolebench").exists())
    loose = sh(snap, "count-objects", "-v")
    check("objects are packed after build (git gc)", "count: 0" in loose.splitlines()[0])
    nm = snap / "hud" / "node_modules"
    check("hud/node_modules points at the bench's own copy, never the source checkout's",
          nm.is_symlink() and Path(os.readlink(nm)) == config.node_modules_dir()
          and (config.node_modules_dir() / "vitest" / "index.js").is_file()
          and not str(os.readlink(nm)).startswith(str(shas["repo"])))
    shas_b = snapshot.shas(snap)
    check("build-time shas: base is the root, change is HEAD",
          shas_b["change"] == sh(snap, "rev-parse", "HEAD") and shas_b["base"] == sh(snap, "rev-parse", "HEAD~1"))

    # a reviewer that commits a mutant, leaves a branch, a stash and an untracked file
    (snap / "tiny.py").write_text("MUTANT\n")
    sh(snap, "commit", "-qam", "reviewer commit")
    sh(snap, "branch", "scratch")
    (snap / "tiny.py").write_text("MUTANT 2\n")
    sh(snap, "stash", "-q")
    (snap / "junk.py").write_text("x")
    (snap / "__pycache__").mkdir()
    (snap / "__pycache__" / "x.pyc").write_text("x")
    dirty = snapshot.check(snap, case, shas_b)
    check("hygiene after a reviewer run sees the moved HEAD, the extra refs and commits",
          any("HEAD moved" in p for p in dirty) and any("unexpected refs" in p for p in dirty)
          and any("commit objects" in p for p in dirty))
    snapshot.reset(snap, shas_b["change"])
    check("reset returns to the build-time change: clean, two commits, no mutant, no stash",
          snapshot.check(snap, case, shas_b) == [] and "hi - 1" in (snap / "tiny.py").read_text()
          and not (snap / "junk.py").exists() and not (snap / "__pycache__").exists()
          and sh(snap, "rev-list", "--all", "--count") == "2")
    copy_dir = root / "judgecopy"
    snapshot.export(snap, shas_b["change"], copy_dir, base=shas_b["base"])
    check("the judge's copy is the change commit's tree, no .git, with the change's diff",
          not (copy_dir / ".git").exists() and "hi - 1" in (copy_dir / "tiny.py").read_text()
          and "hi - 1" in (copy_dir / snapshot.CHANGE_DIFF).read_text())

    # a planted git filter / hook / fsmonitor never runs in the harness's own git
    marker = root / "PWNED"
    hook = f"touch {marker}"
    with open(snap / ".git" / "config", "a") as fh:
        fh.write(f'[filter "evil"]\n\tclean = sh -c "{hook}; cat"\n\tsmudge = sh -c "{hook}; cat"\n'
                 f'[core]\n\tfsmonitor = sh -c "{hook}"\n\thooksPath = {root}/hooks\n')
    (root / "hooks").mkdir()
    (root / "hooks" / "pre-commit").write_text(f"#!/bin/sh\n{hook}\n")
    (root / "hooks" / "pre-commit").chmod(0o755)
    (snap / ".gitattributes").write_text("*.py filter=evil\n")
    (snap / "tiny.py").write_text("changed\n")
    snapshot.reset(snap, shas_b["change"])
    d = root / "rc"
    snapshot.review_copy(snap, shas_b["base"], d)
    check("an agent's planted git filter, fsmonitor and hooks never run in the harness's git", not marker.exists())
    sh(snap, "remote", "add", "origin", "https://example.invalid/x.git")
    check("hygiene check flags a remote", any("remote" in p for p in snapshot.check(snap, case)))

    icase = impl_case(shas)
    isnap = snapshot.build(icase, "r1", repo=shas["repo"], out_root=out)
    base = snapshot.shas(isnap)["base"]
    check("implement snapshot is one commit", sh(isnap, "rev-list", "--all", "--count") == "1")
    check("implement snapshot has no hidden tests", not (isnap / "tiny_hidden_check.py").exists())
    check("the merged commit is not in the implement snapshot",
          subprocess.run(["git", "-C", str(isnap), "cat-file", "-e", shas["later"]], capture_output=True).returncode != 0)
    # an implementer that commits, and weakens a regression suite
    (isnap / "tiny.py").write_text("def clamp(x, lo, hi):\n    return max(lo, min(x, hi))\n")
    (isnap / "tiny_regress_check.py").write_text("pass  # weakened\n")
    sh(isnap, "commit", "-qam", "implementer commit")
    folded = snapshot.fold_commits(isnap, base)
    stats = flows._diff_stats(isnap, base, icase["scope"])
    check("an implementer's commit is folded back: HEAD is the base, the change is in the tree and the diff",
          folded == 1 and sh(isnap, "rev-parse", "HEAD") == base and "tiny.py" in stats["files"]
          and snapshot.check(isnap, icase, {"change": base}) == [])
    rc = snapshot.review_copy(isnap, base, root / "rc2")
    check("the review copy is base plus the change as one commit, no implementer commit object",
          sh(rc, "rev-list", "--all", "--count") == "2" and "hi))" in sh(rc, "show", "HEAD")
          and sh(rc, "remote") == "" and sh(rc, "rev-parse", "HEAD~1") == base)
    grade = snapshot.restore_hidden(isnap, icase, repo=shas["repo"])
    check("hidden tests are restored in a copy only",
          (grade / "tiny_hidden_check.py").exists() and not (isnap / "tiny_hidden_check.py").exists())
    check("regression suites are graded from the base, not the implementer's edit",
          "weakened" not in (grade / "tiny_regress_check.py").read_text()
          and "weakened" in (isnap / "tiny_regress_check.py").read_text())
    # a symlinked directory planted where a hidden test goes is not followed
    outside = root / "outside"
    outside.mkdir()
    icase2 = dict(icase, hidden_tests=["sub/hidden_check.py"])
    (isnap / "sub").symlink_to(outside)
    sh(shas["repo"], "show", f"{shas['later']}:tiny_hidden_check.py")
    try:
        g2 = snapshot.restore_hidden(isnap, dict(icase2, hidden_tests=["sub/x.py"], merged=shas["later"]), repo=shas["repo"])
    except snapshot.SnapshotError:
        g2 = None
    check("a planted symlink on a hidden test's path is never written through", not any(outside.iterdir()))
    if g2:
        import shutil as _sh
        _sh.rmtree(g2, ignore_errors=True)
    (isnap / "sub").unlink()
    (isnap / "tiny_hidden_check.py").write_text("leak")
    check("MUTANT: a hidden test left in the snapshot is flagged",
          any("hidden test present" in p for p in snapshot.check(isnap, icase)))


# --- spend ----------------------------------------------------------------------------


def test_spend(root: Path) -> None:
    path = root / "spend.json"
    ledger = spend.Ledger(path, total_cap=1.0, per_run_cap=0.5)
    check("first reservation is the per-run cap", ledger.reserve("a", "m1") == 0.5)
    other = spend.Ledger(path, total_cap=1.0, per_run_cap=0.5)
    check("a reservation is a pending entry in the file: another ledger object sees it",
          "a" in other.pending() and other.pending()["a"]["usd"] == 0.5)
    check("second reservation fits", other.reserve("b", "m2") == 0.5)
    check("in-flight reservations count toward the total, across ledger objects",
          raises(lambda: ledger.reserve("c", "m3"), spend.CapReached))
    ledger.settle("a", "m1", 0.1)
    check("settled spend is persisted", abs(spend.Ledger(path).spent() - 0.1) < 1e-9)
    ledger.settle("b", "m2", 0.5)
    check("a run at the per-run cap aborts its model", raises(lambda: ledger.reserve("d", "m2"), spend.CapReached))
    check("another model still runs, with what is left", abs(ledger.reserve("e", "m1") - 0.4) < 1e-9)
    ledger.settle("e", "m1", 0.4)
    check("the total cap stops everything", raises(lambda: ledger.reserve("f", "m1"), spend.CapReached))
    check("no temp file is left behind (atomic replace)", not list(root.glob(".spend.json.*.tmp")))

    p2 = root / "proc.json"
    child = subprocess.run([sys.executable, "-c", (
        "import sys; sys.path.insert(0, %r); from rolebench import spend; "
        "print(spend.Ledger(%r, 1.0, 0.5).reserve('child-run', 'm'))") % (str(Path(__file__).resolve().parents[1]), str(p2))],
        capture_output=True, text=True)
    led2 = spend.Ledger(p2, 1.0, 0.5)
    check("another process's reservation is in the file", child.stdout.strip() == "0.5" and "child-run" in led2.pending(),
          child.stderr[-300:])
    led2.reserve("mine", "m")
    check("a reservation whose process died is charged in full on the next reserve",
          "child-run" not in led2.pending() and abs(led2.spent() - 0.5) < 1e-9
          and any("stale" in r.get("note", "") for r in json.loads(p2.read_text())["runs"]))
    bad = root / "bad.json"
    bad.write_text("{not json")
    check("an unreadable ledger means cap reached", raises(lambda: spend.Ledger(bad).reserve("x", "m"), spend.CapReached))
    check("every moonshotai/ id is refused now (Kimi left the matrix), case-insensitively",
          all(raises(lambda m=m: config.check_model(m), config.UnknownModel)
              for m in ("moonshotai/kimi-k3", "MoonshotAI/Kimi-K3", "moonshotai/kimi-k9")))
    check("the amended O-1 matrix runs, with no Kimi and no gpt-6-luna in the Jarvis loop",
          config.MATRIX == {"claude": ("haiku", "sonnet", "opus"), "codex": ("gpt-6.1-sol", "gpt-6-luna"),
                            "jarvis": ("anthropic/claude-haiku-5.5", "deepseek/deepseek-v4.1-flash", "z-ai/glm-5.3-flash")}
          and all(config.check_model(m) is None for m in config.MATRIX["jarvis"]))
    check("no Kimi pin or price floor is left", not hasattr(config, "price_override") and not hasattr(config, "provider_pin")
          and not hasattr(spend, "estimate") and config.KIMI_ALLOWED == ())

    # settlement: unknown spend is the full reservation
    to = runners.RunResult("jarvis", "m", timed_out=True, cost_usd=0.0)
    cr = runners.RunResult("jarvis", "m", crashed=True)
    ok = runners.RunResult("jarvis", "m", cost_usd=0.01, spend_usd=0.03)
    check("a timeout settles at the full reservation, never $0", flows.settle_amount(to, 0.5, "m")[0] == 0.5)
    check("a crash (no result) settles at the full reservation", flows.settle_amount(cr, 0.4, "m")[0] == 0.4)
    check("a finished run settles at the worker's running total when it is higher",
          flows.settle_amount(ok, 0.5, "m")[0] == 0.03)
    big = runners.RunResult("jarvis", "anthropic/claude-haiku-5.5", cost_usd=0.002, spend_usd=0.002,
                            tokens={"prompt_tokens": 1_000_000, "completion_tokens": 0})
    check("settlement is the reported usage, never a price estimate from the token count",
          flows.settle_amount(big, 0.5, "anthropic/claude-haiku-5.5")[0] == 0.002)
    check("a timeout does not abort the model (its spend is unknown, not measured over the cap)",
          flows.settle_amount(to, 0.5, "m")[1] is False)
    tm = runners.RunResult("jarvis", "m", timed_out=True, spend_usd=0.2, inflight_usd=0.05)
    cm = runners.RunResult("jarvis", "m", crashed=True, spend_usd=0.48, inflight_usd=0.1)
    zero = runners.RunResult("jarvis", "m", timed_out=True, spend_usd=0.0, inflight_usd=0.0)
    check("a timeout with a meter settles at streamed total + the in-flight estimate",
          abs(flows.settle_amount(tm, 0.5, "m")[0] - 0.25) < 1e-9 and "in-flight" in flows.settle_amount(tm, 0.5, "m")[2])
    check("that settlement is capped at the reservation, and a crash with a meter is settled the same way",
          flows.settle_amount(cm, 0.5, "m")[0] == 0.5 and flows.settle_amount(zero, 0.5, "m")[0] == 0.0)


def _price(prompt, completion, max_completion=1000, tier=None):
    return {"prompt": prompt, "completion": completion, "max_completion": max_completion, "tier": tier}


def test_worker_meter(root: Path) -> None:
    luna, glm, haiku = "openai/gpt-6-luna", "z-ai/glm-5.3-flash", "anthropic/claude-haiku-5.5"
    spec = {"budget_usd": 0.1, "prices": {luna: _price(1.0, 5.0), "x/y": _price(1.0, 5.0), glm: _price(3.0, 15.0, 1_000_000)}}
    out = io.StringIO()
    m = jarvis_worker.Meter(spec, stream=out)

    class R:
        def __init__(self, cost, p=0, c=0):
            self.cost_usd, self.prompt_tokens, self.completion_tokens, self.raw = cost, p, c, {}

    def last_spend():
        return runners.spend_from_stdout(out.getvalue())
    check("the meter allows the first call, unpinned", m.before(luna) == "")
    m.after(luna, R(0.06))
    check("each call's running total is streamed on stdout", last_spend() == 0.06)
    check("the call that crosses the budget raises", raises(lambda: m.after(luna, R(0.05)), jarvis_worker.BudgetExceeded))
    check("the total covers every reply (sub-agents and compaction included) and is on the stream",
          abs(last_spend() - 0.11) < 1e-9 and m.calls == 2)
    check("no further call is made once the budget is spent", raises(lambda: m.before("x/y"), jarvis_worker.BudgetExceeded))
    P = jarvis_worker.SPEND_PREFIX
    check("the spend read from stdout is the max over every line, so a later lower line cannot undercharge",
          runners.spend_from_stdout(f'{P}{{"usd": 0.3}}\nnoise\n{P}{{"usd": 0.1}}\n') == 0.3
          and runners.spend_from_stdout("nothing") is None)
    # a call whose estimated cost at the most expensive endpoint would cross the budget is refused before it is sent
    m4 = jarvis_worker.Meter(spec, stream=io.StringIO())
    big = [{"role": "user", "content": "x" * 600_000}]        # ~150k tokens; at $3/M ~$0.45 > $0.10
    refused = raises(lambda: m4.before(glm, big, max_tokens=1), jarvis_worker.BudgetExceeded)
    small_ok = not raises(lambda: m4.before(glm, [{"role": "user", "content": "hi"}], max_tokens=1))
    out_refused = raises(lambda: m4.before(glm, [{"role": "user", "content": "hi"}], max_tokens=10_000), jarvis_worker.BudgetExceeded)
    check("a call whose estimate (prompt and max output) at the spec's price would cross the budget is refused before sending",
          refused and small_ok and out_refused and m4.calls == 0)

    # the estimate counts all of it: tool schemas, a completion allowance with no max_tokens, the long-context tier
    tools = [{"type": "function", "function": {"name": f"t{i}", "description": "d" * 4000, "parameters": {}}} for i in range(10)]
    msgs = [{"role": "user", "content": "x" * 380_000}]      # 95k tokens of messages
    hspec = {"budget_usd": 0.05, "prices": {haiku: _price(0.10, 0.50, 64_000, [100_000, 5.0])}}
    mh = jarvis_worker.Meter(hspec, stream=io.StringIO())
    e = mh.estimate(haiku, msgs, None, tools)
    naive = (len(msgs[0]["content"]) / 4) * 0.10 / 1e6       # what the old estimate saw: messages only, no output
    check("tool schemas count toward the prompt estimate (chars/4)",
          abs(e["prompt_tokens"] - (380_000 + len(json.dumps(tools))) / 4) < 1e-6 and e["prompt_tokens"] > 100_000)
    ma = jarvis_worker.Meter({"budget_usd": 1, "prices": {"a/b": _price(1, 1, 4000), "c/d": {"prompt": 1, "completion": 1, "tier": None}}},
                             stream=io.StringIO())
    check("the completion allowance: the call's max_tokens when set (bounded by the endpoint), else min(endpoint max, 16k)",
          e["completion_tokens"] == 16_000 and ma.estimate("a/b", [], None)["completion_tokens"] == 4000
          and ma.estimate("a/b", [], 2400)["completion_tokens"] == 2400
          and ma.estimate("a/b", [], 32_000)["completion_tokens"] == 4000
          and ma.estimate("c/d", [], None)["completion_tokens"] == 16_000
          and ma.estimate("c/d", [], 32_000)["completion_tokens"] == 32_000)
    # a 1M-context matrix model: its endpoint lists ~1M max completion; the first call must cost cents, not dollars
    mm = jarvis_worker.Meter({"budget_usd": 0.5, "prices": {"z-ai/glm-5.3-flash": _price(0.6, 2.2, 1_000_000)}}, stream=io.StringIO())
    first = mm.estimate("z-ai/glm-5.3-flash", [{"role": "user", "content": "x" * 80_000}], 16_000, tools)
    unset = mm.estimate("z-ai/glm-5.3-flash", [{"role": "user", "content": "x" * 80_000}], None, tools)
    check("a 1M-context model's first call estimates at cents, not dollars (with and without max_tokens)",
          first["usd"] < 0.10 and unset["usd"] < 0.10, f"${first['usd']:.4f} / ${unset['usd']:.4f}")
    check("and it is not refused before sending",
          not raises(lambda: mm.before("z-ai/glm-5.3-flash", [{"role": "user", "content": "x" * 80_000}], 16_000, tools)))
    check("Haiku 5.5 is priced at 5x for the whole request from 100k prompt tokens",
          e["multiplier"] == 5.0 and mh.estimate(haiku, [{"role": "user", "content": "x" * 1000}], 10)["multiplier"] == 1.0)
    check("one call crosses the per-run budget only once tools, the allowance and the tier are counted",
          naive < 0.05 and e["usd"] > 0.05 and raises(lambda: mh.before(haiku, msgs, None, tools), jarvis_worker.BudgetExceeded)
          and mh.calls == 0, f"naive {naive:.4f} full {e['usd']:.4f}")
    import inspect
    from jarvis import llm as jllm
    names = list(inspect.signature(jllm.chat).parameters)[2:]
    check("the worker reads tools, max_tokens and provider at llm.chat's real positions",
          all(names[i] == k for k, i in jarvis_worker.CHAT_ARGS.items()), str(names[:8]))
    m2 = jarvis_worker.Meter(spec, stream=io.StringIO())
    check("a moonshotai/ id is refused in the worker too, and a model with no price is refused, not estimated at 0",
          raises(lambda: m2.before("moonshotai/kimi-k3"), jarvis_worker.BudgetExceeded)
          and raises(lambda: m2.before("other/unpriced"), jarvis_worker.BudgetExceeded))
    r = R(0.004, 1_000_000, 0)
    m3 = jarvis_worker.Meter({**spec, "budget_usd": 100}, stream=io.StringIO())
    m3.after(glm, r)
    check("the running total is the reported cost, never floored at the estimate price", m3.total == 0.004 and r.cost_usd == 0.004)
    check("the rolebench Jarvis toolset has no delegation",
          not {"run_subagent", "run_fleet"} & set(jarvis_worker.TOOLS) and "run_command" in jarvis_worker.TOOLS)
    s = runners.jarvis_spec(model=glm, prompt="p", cwd="/w", max_turns=3, budget_usd=0.2, price_fn=lambda m: (0.5, 2.0, None))
    s2 = runners.jarvis_spec(model=haiku, prompt="p", cwd="/w", max_turns=3, budget_usd=None)
    check("the spec gives the Jarvis cell 32k output tokens per call, the 16k allowance cap and effort high",
          s["max_output_tokens"] == 32_000 == config.JARVIS_MAX_OUTPUT_TOKENS and s["allowance_cap"] == 16_000
          and s["effort"] == "high")
    mi = jarvis_worker.Meter({"budget_usd": 1, "prices": {"a/b": _price(1, 1, 4000)}}, stream=io.StringIO())
    mi.before("a/b", [{"role": "user", "content": "x" * 400}], 1000)
    mid = mi.stream.getvalue()
    mi.after("a/b", R(0.001))
    check("the meter streams the in-flight estimate before a call and clears it after",
          runners.inflight_from_stdout(mid) > 0 and runners.inflight_from_stdout(mi.stream.getvalue()) == 0.0
          and runners.inflight_from_stdout("noise") is None)
    check("the cells pin effort: claude --effort high (not the judge), codex model_reasoning_effort=high",
          (lambda a: "--effort" in a and a[a.index("--effort") + 1] == "high")(runners.claude_argv("haiku", 3)) and "--effort" not in runners.claude_argv("opus", 3, read_only=True)
          and "model_reasoning_effort=high" in runners.codex_argv("gpt-6-luna", "/w", Path("/x")))
    check("the spec prices the cell's model at its endpoint price, the 16k default allowance and its tier; no pins",
          s["prices"] == {glm: _price(0.5, 2.0, 16_000)} and "pins" not in s
          and s2["prices"] == {haiku: _price(1.0, 5.0, 8000, [100_000, 5.0])} and s2["budget_usd"] == config.PER_RUN_CAP_USD)
    check("the spec refuses a Kimi id", raises(lambda: runners.jarvis_spec(model="moonshotai/kimi-k3", prompt="p", cwd="/w",
                                                                            max_turns=1, budget_usd=0.1), config.UnknownModel))


def test_prices(root: Path) -> None:
    """The estimate's price: the most expensive endpoint, cached, never zero."""
    reply = {"data": {"id": "z-ai/glm-5.3-flash", "endpoints": [
        {"provider_name": "cheap", "pricing": {"prompt": "0.0000001", "completion": "0.0000004"}},
        {"provider_name": "dear-in", "pricing": {"prompt": "0.0000009", "completion": "0.000001"}, "max_completion_tokens": 32_000},
        {"provider_name": "dear-out", "pricing": {"prompt": "0.0000002", "completion": "0.000003"}},
        {"provider_name": "broken", "pricing": {"prompt": "n/a"}}]}}
    p, c, n, mc = prices.max_price(reply)
    check("the estimate takes the highest prompt and the highest completion price over every endpoint",
          abs(p - 0.9) < 1e-9 and abs(c - 3.0) < 1e-9 and n == 3 and mc == 32_000)
    check("a reply with no priced endpoint is an error, never a zero price",
          raises(lambda: prices.max_price({"data": {"endpoints": [{"pricing": {}}]}}), prices.PriceError)
          and raises(lambda: prices.max_price({"data": {}}), prices.PriceError))
    urls = []

    def fetch(url):
        urls.append(url)
        return reply
    with env(ROLEBENCH_HOME=str(root / "prices-home")):
        first = REAL_ENDPOINT_PRICE("z-ai/glm-5.3-flash", fetch=fetch, now=1000.0)
        again = REAL_ENDPOINT_PRICE("z-ai/glm-5.3-flash", fetch=fetch, now=1000.0 + 3600)
        check("the endpoints list is fetched once and cached",
              first == again and len(urls) == 1 and urls[0] == "https://openrouter.ai/api/v1/models/z-ai/glm-5.3-flash/endpoints")
        stale = REAL_ENDPOINT_PRICE("z-ai/glm-5.3-flash", fetch=_no_network, now=1000.0 + 2 * prices.CACHE_TTL_S)
        check("a failed refetch of a stale price falls back to the cache, raised 25%",
              abs(stale[0] - first[0] * 1.25) < 1e-9 and abs(stale[1] - first[1] * 1.25) < 1e-9 and stale[2] == first[2])
        edge = REAL_ENDPOINT_PRICE("z-ai/glm-5.3-flash", fetch=_no_network, now=1000.0 + prices.STALE_MAX_S)
        check("the stale fallback holds up to 7 days", edge == stale)
        check("a cached price older than 7 days with a failed refetch refuses the model",
              raises(lambda: REAL_ENDPOINT_PRICE("z-ai/glm-5.3-flash", fetch=_no_network, now=1001.0 + prices.STALE_MAX_S),
                     prices.PriceError))
        check("no cache and no fetch: the run is refused",
              raises(lambda: REAL_ENDPOINT_PRICE("deepseek/deepseek-v4.1-flash", fetch=_no_network), prices.PriceError))
        check("a model id that is not an OpenRouter id is refused before any fetch",
              raises(lambda: REAL_ENDPOINT_PRICE("../../etc/passwd", fetch=fetch), prices.PriceError) and len(urls) == 1)


def test_worker_env(root: Path) -> None:
    """M1: a child of the worker sees neither OPENROUTER_API_KEY nor any .env key."""
    rt = root / "runtime-m1"
    sandbox.build_runtime(rt)
    check("the worker's runtime copy has no cases and no .env",
          not (rt / "rolebench" / "cases").exists() and not (rt / ".env").exists() and (rt / "jarvis" / "config.py").is_file())
    (rt / ".env").write_text("OPENROUTER_API_KEY=sk-dummy-from-env-file\nROLEBENCH_PROBE_SECRET=probe-value\n")
    driver = (
        "import json, os, subprocess, sys\n"
        "from rolebench import jarvis_worker as w\n"
        "cfg = w.prepare({'model': 'm/cell-model', 'max_output_tokens': 32000, 'effort': 'high'}, 'sk-dummy-from-stdin')\n"
        "out = subprocess.run(['env'], capture_output=True, text=True).stdout\n"
        "from jarvis import models\n"
        "print(json.dumps({'key': 'OPENROUTER_API_KEY=' in out, 'probe': 'ROLEBENCH_PROBE_SECRET' in out,\n"
        "  'api': cfg.api_key() == 'sk-dummy-from-stdin', 'tiers': sorted(set(cfg.TIERS.values())),\n"
        "  'tier': models.tier('compaction'), 'provider': cfg.CHAT_PROVIDER, 'max_tokens': cfg.MAX_TOKENS,\n"
        "  'effort': [cfg.REASONING_EFFORT, models.effort_for('m/cell-model'), models.effort_for('openai/gpt-6-luna'),\n"
        "             models.effort_for('m/cell-model', {'m/cell-model': 'max'})]}))\n")
    home = root / "m1-home"
    home.mkdir()
    done = subprocess.run([config.python_bin(), "-c", driver], capture_output=True, text=True, cwd=str(rt),
                          env={"PATH": "/usr/bin:/bin", "HOME": str(home), "PYTHONPATH": str(rt),
                               "OPENROUTER_API_KEY": "sk-dummy-via-environment"}, timeout=120)
    try:
        doc = json.loads(done.stdout.strip().splitlines()[-1])
    except Exception:
        doc = {}
    check("a child of the worker has no OPENROUTER_API_KEY (neither the environment's nor the .env's)",
          doc.get("key") is False, done.stderr[-400:])
    check("a child of the worker has no other key the .env import added", doc.get("probe") is False)
    check("the worker still holds the key from stdin, through config.api_key only", doc.get("api") is True)
    check("the worker gives every call 32k output tokens unless the caller sets its own", doc.get("max_tokens") == 32000)
    check("the worker pins every call's reasoning effort to high (never the loop's default max, nor a roster pin)",
          doc.get("effort") == ["high", "high", "high", "high"])
    check("every tier is pinned to the cell's model, and nothing is provider-pinned globally",
          doc.get("tiers") == ["m/cell-model"] and doc.get("tier") == "m/cell-model" and doc.get("provider") == "")


# --- the sandbox ---------------------------------------------------------------------


POISON = {"CLAUDE_CODE_SSE_PORT": "1", "CLAUDE_CODE_MESSAGING_TOKEN": "x", "ANTHROPIC_BASE_URL": "http://evil",
          "ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k", "GH_TOKEN": "k", "GITHUB_TOKEN": "k",
          "SSH_AUTH_SOCK": "/tmp/agent", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
          "XDG_RUNTIME_DIR": "/run/user/1000", "JARVIS_SECRET_KEY": "k", "LC_ALL": "C.UTF-8"}


def test_sandbox_argv(root: Path) -> None:
    work = root / "w-argv"
    work.mkdir()
    base = dict(POISON, PATH="/x", HOME="/h", OPENROUTER_API_KEY="sk-dummy-not-a-key")
    e = sandbox.env_for(sandbox.Cell("claude", work), base)
    check("a cell's environment is an allowlist: no CLAUDE_CODE_* channel, ANTHROPIC_*, keys, tokens, bus or agent socket",
          not any(k.startswith(("CLAUDE_CODE_SSE", "CLAUDE_CODE_MESSAGING", "ANTHROPIC", "OPENAI", "GH_TOKEN", "GITHUB",
                                "SSH_AUTH", "DBUS", "XDG_RUNTIME", "OPENROUTER", "JARVIS_SECRET")) for k in e)
          and e["LC_ALL"] == "C.UTF-8")
    check("git and gh are configured so nothing can authenticate",
          e["GIT_CONFIG_NOSYSTEM"] == "1" and e["GIT_TERMINAL_PROMPT"] == "0" and e["GIT_ASKPASS"] == "/bin/false"
          and e["GIT_CONFIG_GLOBAL"] == str(sandbox.GITCONFIG) and e["GH_CONFIG_DIR"].startswith("/tmp/")
          and "helper =" in sandbox.GITCONFIG_TEXT and "hooksPath = /dev/null" in sandbox.GITCONFIG_TEXT)
    login = root / "login"
    (login / ".claude").mkdir(parents=True)
    (login / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-dummy", "refreshToken": "sk-ant-refresh-dummy", "expiresAt": (time.time() + 86400) * 1000,
        "scopes": ["user:inference"], "subscriptionType": "max", "rateLimitTier": "default"},
        "mcpOAuth": {"some-server": {"accessToken": "mcp-dummy"}}}))
    (login / ".codex").mkdir()
    (login / ".codex" / "auth.json").write_text(json.dumps({"auth_mode": "max", "OPENAI_API_KEY": None, "last_refresh": "x",
        "tokens": {"access_token": "cx-access-dummy", "refresh_token": "cx-refresh-dummy", "id_token": "cx-id", "account_id": "acc"}}))
    fakebin = root / "fakebin"
    (fakebin / "rel" / "bin").mkdir(parents=True)
    for name in ("claude",):
        (fakebin / name).write_text("#!/bin/sh\n")
        (fakebin / name).chmod(0o755)
    (fakebin / "rel" / "bin" / "codex").write_text("#!/bin/sh\n")
    (fakebin / "rel" / "bin" / "codex").chmod(0o755)
    (fakebin / "codex").symlink_to(fakebin / "rel" / "bin" / "codex")
    with env(ROLEBENCH_LOGIN_HOME=str(login), PATH=f"{fakebin}:{os.environ['PATH']}", ROLEBENCH_NET="none"):
        argvs = {p: sandbox.bwrap_argv(sandbox.Cell(p, work), ["true"]) for p in sandbox.PROFILES}
        # A2: the trimmed credential views, checked by key name only (no values printed)
        claude_cred = json.loads(sandbox.trimmed_claude_credentials())["claudeAiOauth"]
        codex_raw = sandbox.trimmed_codex_auth()
        codex_cred = json.loads(codex_raw)
    home = sandbox.sandbox_home()
    check("the trimmed Claude login keeps only the access token and its metadata, no refresh or MCP token",
          set(claude_cred) <= set(sandbox.CLAUDE_OAUTH_KEEP) and "accessToken" in claude_cred
          and "refreshToken" not in claude_cred and "mcpOAuth" not in json.loads(sandbox.trimmed_claude_credentials()))
    check("the trimmed Codex login replaces the refresh token with the inert placeholder (A2 amendment)",
          "access_token" in codex_cred["tokens"] and codex_cred["tokens"].get("refresh_token") == sandbox.CODEX_REFRESH_PLACEHOLDER
          and b"cx-refresh-dummy" not in codex_raw)
    check("the trimmed Codex login keeps every other field (codex's TokenData needs refresh_token as a string)",
          set(codex_cred["tokens"]) == {"access_token", "refresh_token", "id_token", "account_id"}
          and {"auth_mode", "last_refresh"} <= set(codex_cred) and isinstance(codex_cred["tokens"].get("refresh_token"), str))

    def sources(a):
        return [a[i + 1] for i, t in enumerate(a) if t in ("--bind", "--ro-bind")]

    def dests(a):
        return [a[i + 2] for i, t in enumerate(a) if t in ("--bind", "--ro-bind")]

    def data_dests(a):
        return [a[i + 2] for i, t in enumerate(a) if t == "--ro-bind-data"]
    banned = [str(Path(REAL_HOME) / "projects" / "Jarvis"), str(Path(REAL_HOME) / ".config" / "gh"),
              str(Path(REAL_HOME) / ".gitconfig"), str(Path(REAL_HOME) / ".ssh"), "/mnt/c", "/run", "/home"]
    ok = True
    for p, a in argvs.items():
        for s in sources(a):
            if any(s == b for b in banned) or s.startswith("/tmp/claude-") and not str(work).startswith(s):
                if not (s == str(work) or s.startswith(str(config.venv_dir())) or s.startswith(str(config.home()))):
                    ok = False
    check("no profile binds the checkout, ~/.config/gh, ~/.gitconfig, ~/.ssh, /mnt/c, /run or /home", ok,
          json.dumps({p: sources(a) for p, a in argvs.items()})[:600])
    check("no login file is bound from disk: credentials come only through --ro-bind-data",
          not any(d.endswith(".credentials.json") or d.endswith("auth.json") for a in argvs.values() for d in dests(a)))
    check("the venv is read-only at /opt/rolebench/venv, never at its checkout path",
          all(str(sandbox.VENV) in dests(a) and str(config.venv_dir()) not in dests(a) for a in argvs.values()))
    check("HOME and /tmp are tmpfs in every profile",
          all(a[a.index("--tmpfs", a.index("/proc")) + 1] == "/tmp" and home in a for a in argvs.values()))
    cl, cx, gr, jv, jd = (argvs[k] for k in ("claude", "codex", "grade", "jarvis", "judge"))
    creds_dest = str(Path(home) / ".claude" / ".credentials.json")
    k = cl.index(creds_dest)
    check("the claude cell gets the Claude login via --ro-bind-data at 0400, and nothing else under ~/.claude",
          creds_dest in data_dests(cl) and cl[k - 2] == "--ro-bind-data" and cl[k - 3] == "0400" and cl[k - 4] == "--perms"
          and not any(d.rstrip("/").endswith("/.claude") for d in dests(cl)))
    codex_auth = str(Path(home) / sandbox.CODEX_HOME_NAME / "auth.json")
    check("the codex cell gets an isolated CODEX_HOME holding its auth.json via --ro-bind-data, never ~/.codex",
          codex_auth in data_dests(cx) and not any(d.rstrip("/").endswith("/.codex") for d in dests(cx))
          and "CODEX_HOME" in cx and cx[cx.index("CODEX_HOME") + 1] == str(Path(home) / sandbox.CODEX_HOME_NAME))
    check("logins go only where they are needed",
          creds_dest not in data_dests(cx) + data_dests(gr) + data_dests(jv) and codex_auth not in data_dests(cl) + data_dests(gr) + data_dests(jv))
    check("the judge's work tree is read-only", jd[jd.index(str(work.resolve())) - 1] == "--ro-bind"
          if str(work.resolve()) in jd else False)
    check("every cell clears the environment and dies with its parent",
          all("--clearenv" in a and "--die-with-parent" in a and "--new-session" in a for a in argvs.values()))

    # A1: network is gated. claude/jarvis/judge get pasta (the default) or, under
    # ROLEBENCH_NET=none, no network; codex is exempt and keeps the host netns;
    # any other mode is refused, and so is a missing pasta. Never the host netns.
    check("under ROLEBENCH_NET=none, claude/jarvis/judge get no shared netns; codex does",
          all("--share-net" not in a for a in (cl, jv, jd)) and "--share-net" in cx
          and all("--unshare-all" in a for a in argvs.values()))
    with env(ROLEBENCH_LOGIN_HOME=str(login), PATH=f"{fakebin}:{os.environ['PATH']}", ROLEBENCH_NET="bogus"):
        for p in ("claude", "jarvis", "judge"):
            check(f"a {p} cell is refused under an unknown ROLEBENCH_NET mode",
                  raises(lambda p=p: sandbox.bwrap_argv(sandbox.Cell(p, work), ["true"]), sandbox.SandboxError))
        check("a codex cell is not gated and keeps the host netns",
              "--share-net" in sandbox.bwrap_argv(sandbox.Cell("codex", work), ["true"]))
    with env(ROLEBENCH_LOGIN_HOME=str(login), PATH=f"{fakebin}:{os.environ['PATH']}", ROLEBENCH_NET=None):
        check("pasta is the default network mode", sandbox.net_mode() == "pasta")
        if sandbox.pasta_available():
            check(f"pasta available ({sandbox.pasta_available()})", False)
            return
        composed = {p: sandbox.cell_command(sandbox.Cell(p, work), ["true"], cred_fds={}) for p in sandbox.PROFILES}
        saved = sandbox.PASTA
        sandbox.PASTA = str(root / "no-such-pasta")
        try:
            missing = all(raises(lambda p=p: sandbox.cell_command(sandbox.Cell(p, work), ["true"]), sandbox.SandboxError)
                          for p in sandbox.NET_GATED)
        finally:
            sandbox.PASTA = saved
    check("with pasta missing, a claude/jarvis/judge cell is refused, never given the host netns", missing)
    with env(ROLEBENCH_LOGIN_HOME=str(login), PATH=f"{fakebin}:{os.environ['PATH']}", ROLEBENCH_NET="pasta"):
        bare = all(raises(lambda p=p: sandbox.bwrap_argv(sandbox.Cell(p, work), ["true"]), sandbox.SandboxError)
                   for p in sandbox.NET_GATED)
    check("a pasta cell's bwrap argv is never built bare (it would share the host netns outside pasta)", bare)
    check("claude, jarvis and judge cells run under pasta; codex and grade never do",
          all(composed[p][1] == "pasta" and composed[p][0][0] == sandbox.PASTA for p in sandbox.NET_GATED)
          and composed["codex"][1] == "host" and composed["grade"][1] == "none"
          and all(sandbox.PASTA not in composed[p][0] for p in ("codex", "grade")))
    full = composed["claude"][0]
    dash = full.index("--")
    flags = full[1:dash]
    check("pasta's flags are exactly the pinned set (config-net, no map-gw, no host loopback or guest mapping, "
          "DNS forward, IPv4 only, no ICMP/DHCP/RA)", tuple(flags) == sandbox.PASTA_FLAGS)
    pairs = {flags[i]: flags[i + 1] for i in range(len(flags) - 1) if flags[i] in ("-t", "-u", "-T", "-U")}
    check("no port is forwarded either way: -t, -u, -T and -U are all none (each defaults to auto)",
          pairs == {"-t": "none", "-u": "none", "-T": "none", "-U": "none"}
          and not any(f.startswith(("--tcp-ports", "--udp-ports", "--tcp-ns", "--udp-ns")) for f in flags))
    check("pasta maps no address to the host: --no-map-gw, --map-host-loopback none, --map-guest-addr none",
          "--no-map-gw" in flags and flags[flags.index("--map-host-loopback") + 1] == "none"
          and flags[flags.index("--map-guest-addr") + 1] == "none")
    inner = full[dash + 1:]
    check("pasta runs netcell (isolated python) with the policy, then bwrap",
          inner[1] == "-I" and inner[2] == str(sandbox.NETCELL) and inner[4] == "--" and inner[5] == sandbox.BWRAP)
    policy = json.loads(inner[3])
    host = sandbox.host_addresses()
    check("the policy blocks every private, link-local, CGNAT and host-owned address, allows only TCP 443 and DNS",
          set(sandbox.BLOCK_V4) <= set(policy["block"]) and set(host) <= set(policy["block"])
          and {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10", "127.0.0.0/8"} <= set(policy["block"])
          and policy["allow_tcp"] == [443] and policy["dns"] == sandbox.DNS_FORWARD)
    bw = inner[5:]
    check("bwrap in a pasta cell keeps pasta's netns but takes its own user namespace, the host uid and no capabilities",
          "--share-net" in bw and "--unshare-user" in bw and "--uid" in bw and bw[bw.index("--uid") + 1] == str(os.getuid())
          and "--cap-drop" in bw and bw[bw.index("--cap-drop") + 1] == "ALL")
    check("a pasta cell's resolv.conf names only pasta's DNS forwarder, with no search list and no AAAA",
          sandbox.PASTA_RESOLV.splitlines()[0] == f"nameserver {sandbox.DNS_FORWARD}" and "search" not in sandbox.PASTA_RESOLV
          and "no-aaaa" in sandbox.PASTA_RESOLV)
    rules = netcell.rules(policy)
    check("netcell's rule order: DNS to the forwarder, the blocks, TCP 443, then prohibit everything else",
          [r["pref"] for r in rules] == sorted(r["pref"] for r in rules) and rules[-1] == {"pref": 120, "do": "prohibit"}
          and {"pref": 110, "ipproto": "tcp", "dport": 443, "do": "main"} in rules
          and sum(1 for r in rules if r["do"] == "main") == 3)
    with env(ROLEBENCH_LOGIN_HOME=str(login)):
        fds, header = sandbox.credential_header(sandbox.Cell("claude", work))
    size, _, body = header.partition("\n")
    check("a pasta cell's logins go to netcell on stdin (pasta closes inherited fds), at fds bwrap's --ro-bind-data names",
          int(size) == len(body) and set(json.loads(body)) == {str(sandbox.CRED_FD_BASE)}
          and list(fds.values()) == [sandbox.CRED_FD_BASE])
    u = sandbox.unit_argv(sandbox.unit_name("2026-x/y z"), ["echo"])
    check("each run is a named, capped transient unit", u[:4] == ["systemd-run", "--user", "--scope", "-q"]
          and "--unit=rolebench-2026-x-y-z" in u and "MemoryMax=3G" in u and "MemorySwapMax=0" in u and u[-1] == "echo")


def _probe_script(paths: dict, extra: str = "") -> str:
    lines = [f'if test -e "{p}"; then echo "{k}=present"; else echo "{k}=absent"; fi' for k, p in paths.items()]
    return "\n".join(lines) + "\n" + extra


def _parse_probe(out: str) -> dict:
    d = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            if re.fullmatch(r"[a-z0-9_]+", k):
                d[k] = v.strip()
    return d


def _throwaway_listener():
    """A socket listening on a random host port, as an inert connect target.
    Never a real daemon port. Returns (port, close)."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s.getsockname()[1], s.close


def test_sandbox_escape(root: Path) -> None:
    """Real cells: a child cannot reach the gh login, the checkout, the lead's
    session dirs, the OpenRouter key, a host port, or the host PID/IPC/UTS
    namespaces. Existence probes only; no secret is ever read or printed."""
    why = sandbox.available()
    if why:
        check(f"sandbox available ({why})", False)
        return
    work = root / "w-escape"
    work.mkdir()
    real = Path(REAL_HOME)
    port, close = _throwaway_listener()
    host_pid = os.getpid()
    probes = {"gh_hosts": real / ".config" / "gh" / "hosts.yml", "checkout": real / "projects" / "Jarvis",
              "checkout_env": real / "projects" / "Jarvis" / ".env", "ssh": real / ".ssh",
              "gitconfig": real / ".gitconfig", "mnt_c": "/mnt/c", "user_bus": f"/run/user/{os.getuid()}/bus",
              "claude_json": real / ".claude.json", "claude_projects": real / ".claude" / "projects",
              "codex_config": real / ".codex" / "config.toml", "codex_sessions": real / ".codex" / "sessions",
              "host_pid": f"/proc/{host_pid}", "host_pid_env": f"/proc/{host_pid}/environ",
              "proc1_root_home": f"/proc/1/root{real}/projects"}
    lead = [p for p in Path("/tmp").glob("claude-*/*") if not str(work).startswith(str(p))]
    for i, p in enumerate(lead[:6]):
        probes[f"lead_tmp_{i}"] = p
    host_present = {k for k, p in probes.items() if os.path.exists(p)}
    # host_pid/proc1 always "exist" from the host's view; they prove the PID ns only inside.
    pidns_probes = {"host_pid", "host_pid_env", "proc1_root_home"}
    extra = ('if env | grep -q "^OPENROUTER_API_KEY="; then echo "or_key=yes"; else echo "or_key=no"; fi\n'
             'if env | grep "^CLAUDE_CODE_\\|^ANTHROPIC_" | grep -qv "^CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="; then echo "claude_env=yes"; else echo "claude_env=no"; fi\n'
             'if systemd-run --user --scope -q true 2>/dev/null; then echo "systemd=yes"; else echo "systemd=no"; fi\n'
             'if grep -qv "lo:\\|Inter-\\|face" /proc/net/dev; then echo "netdev=yes"; else echo "netdev=no"; fi\n'
             'echo "pidcount=$(ls -d /proc/[0-9]* 2>/dev/null | wc -l)"\n'
             'if ls /etc/ssh 2>/dev/null | grep -q .; then echo "etcssh=exposed"; else echo "etcssh=masked"; fi\n'
             'echo "ipcshm=$(cat /proc/sysvipc/shm 2>/dev/null | wc -l)"\n'
             f'if test -e "{real}/.claude/.credentials.json"; then echo "claude_login=present"; else echo "claude_login=absent"; fi\n'
             f'if test -e "$CODEX_HOME/auth.json"; then echo "codex_login=present"; else echo "codex_login=absent"; fi\n'
             f'if touch "{work}/w.txt" 2>/dev/null; then echo "work_rw=yes"; else echo "work_rw=no"; fi\n'
             f"if python3 -c \"import socket;s=socket.socket();s.settimeout(2);s.connect(('127.0.0.1',{port}))\" 2>/dev/null; then echo \"hostport=reachable\"; else echo \"hostport=unreachable\"; fi\n")
    script = _probe_script(probes, extra)
    results = {}
    cells = [(p, "none") for p in ("claude", "codex", "jarvis", "grade")]
    if not sandbox.pasta_available():
        cells += [(p, "pasta") for p in ("claude", "jarvis", "judge")]
    else:
        check(f"pasta available for the escape probes ({sandbox.pasta_available()})", False)
    try:
        with env(OPENROUTER_API_KEY="sk-dummy-not-a-key", CLAUDE_CODE_SSE_PORT="1", ANTHROPIC_BASE_URL="http://x",
                 ROLEBENCH_LOGIN_HOME=None):
            for profile, net in cells:
                name = profile if net == "none" else f"{profile}+pasta"
                try:
                    with env(ROLEBENCH_NET=net):
                        rc, out, err, to = sandbox.launch(["/bin/sh", "-c", script], cell=sandbox.Cell(profile, work),
                                                          run_id=f"check-escape-{profile}-{net}-{os.getpid()}", timeout_s=60)
                except sandbox.SandboxError as exc:
                    rc, out, err, to = None, "", str(exc), False
                results[name] = _parse_probe(out)
                if rc != 0:
                    print(f"    ({name}: rc={rc} {err.strip()[:200]})")
    finally:
        close()
    print("    host has:", ", ".join(sorted(host_present - pidns_probes)) or "(none)")
    for profile, r in results.items():
        visible = sorted(k for k in probes if k not in pidns_probes and r.get(k) == "present")
        print(f"    {profile}: visible={visible or 'none'} key={r.get('or_key')} claude_env={r.get('claude_env')} "
              f"systemd={r.get('systemd')} netdev={r.get('netdev')} pidcount={r.get('pidcount')} "
              f"etcssh={r.get('etcssh')} hostport={r.get('hostport')} host_pid={r.get('host_pid')} "
              f"proc1_home={r.get('proc1_root_home')} login={r.get('claude_login')}/{r.get('codex_login')} rw={r.get('work_rw')}")
        check(f"{profile} cell: none of the gh login, checkout, ssh, gitconfig, /mnt/c, user bus, owner session "
              f"files or lead /tmp dirs is reachable", r and not visible, f"visible: {visible}")
        check(f"{profile} cell: no OPENROUTER_API_KEY, CLAUDE_CODE_* or ANTHROPIC_* in a child's environment",
              r.get("or_key") == "no" and r.get("claude_env") == "no")
        check(f"{profile} cell: cannot ask the user systemd to start anything outside", r.get("systemd") == "no")
        check(f"{profile} cell: the work tree is " + ("read-only" if profile.startswith("judge") else "writable"),
              r.get("work_rw") == ("no" if profile.startswith("judge") else "yes"))
        # A3: PID namespace — the host's own pid and its environ are invisible, /proc/1 is the cell
        check(f"{profile} cell: the host PID namespace is not visible (no host pid, no host /proc/1/root)",
              r.get("host_pid") == "absent" and r.get("host_pid_env") == "absent" and r.get("proc1_root_home") == "absent"
              and r.get("pidcount", "999").isdigit() and int(r.get("pidcount", "999")) < 50, f"pidcount={r.get('pidcount')}")
        # LOW: the /etc masks (mutant MB drops them)
        check(f"{profile} cell: /etc/ssh is masked", r.get("etcssh") == "masked")
    if "gh_hosts" not in host_present:
        print("    (this host has no gh hosts.yml: that probe proves nothing here)")
    for p in ("claude", "jarvis", "grade"):
        check(f"{p} cell (ROLEBENCH_NET=none): no network interface but lo, and a host port is unreachable",
              results[p].get("netdev") == "no" and results[p].get("hostport") == "unreachable")
    for p in [k for k in results if k.endswith("+pasta")]:
        check(f"{p} cell: a host loopback port is unreachable (pasta forwards nothing)", results[p].get("hostport") == "unreachable")
    pasta_cells = [k for k in results if k.endswith("+pasta")]
    check("only the claude cell (and the judge) holds the Claude login, in both network modes",
          results["claude"].get("claude_login") == "present"
          and all(results[p].get("claude_login") == "absent" for p in ("codex", "jarvis", "grade"))
          and all(results[p].get("claude_login") == ("absent" if p.startswith("jarvis") else "present") for p in pasta_cells))
    check("only the codex cell holds the Codex login (in its own CODEX_HOME)",
          results["codex"].get("codex_login") == "present"
          and all(results[p].get("codex_login") == "absent" for p in ("claude", "jarvis", "grade", *pasta_cells)))


NET_PROBE = """import errno, json, os, socket, subprocess, sys, time
spec = json.loads(sys.argv[1])
r = {}
def err(e):
    return errno.errorcode.get(getattr(e, "errno", None) or 0, type(e).__name__)
def conn(host, port, timeout=2.0):
    s = socket.socket(); s.settimeout(timeout)
    try:
        s.connect((host, port)); return "OPEN"
    except OSError as e:
        return err(e)
    finally:
        s.close()
def conn_retry(host, port, seconds):
    # pasta's -T auto rescans the host's listeners every second: keep trying
    deadline, last = time.monotonic() + seconds, ""
    while time.monotonic() < deadline:
        last = conn(host, port, 0.5)
        if last == "OPEN":
            return last
        time.sleep(0.2)
    return last
r["lo_tcp"] = conn_retry("127.0.0.1", spec["lo_port"], spec.get("retry", 3.0))
r["host_any"] = {a: conn(a, spec["any_port"]) for a in spec["host_addrs"]}
r["host_lo_port"] = {a: conn(a, spec["lo_port"]) for a in spec["host_addrs"]}
if spec.get("gateway"):
    r["gw_any"] = conn(spec["gateway"], spec["any_port"])
    r["gw_lo"] = conn(spec["gateway"], spec["lo_port"])
r["private"] = {a: conn(a, 443, 1.0) for a in spec.get("private", [])}
if spec.get("names"):
    for name in spec["names"]:
        try:
            infos = socket.getaddrinfo(name, 443, type=socket.SOCK_STREAM)
            r["dns_" + name] = sorted({i[0].name for i in infos})
        except OSError as e:
            r["dns_" + name] = "FAIL " + err(e)
            continue
        try:
            socket.create_connection((name, 443), timeout=10).close(); r["tcp443_" + name] = "OPEN"
        except OSError as e:
            r["tcp443_" + name] = err(e)
        v4 = [i[4][0] for i in infos if i[0] == socket.AF_INET]
        r["tcp80_" + name] = conn(v4[0], 80) if v4 else "no v4"
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        u.sendto(b"x", (spec["host_addrs"][0], 53)); r["udp_host53"] = "SENT"
    except OSError as e:
        r["udp_host53"] = err(e)
    a = socket.socket(socket.AF_UNIX)
    try:
        a.connect("\\0" + spec["abstract"]); r["abstract"] = "OPEN"
    except OSError as e:
        r["abstract"] = err(e)
    l = socket.socket(); l.bind(("127.0.0.1", 0)); l.listen(1)
    r["own_lo"] = conn("127.0.0.1", l.getsockname()[1])
    r["capeff"] = [x.split()[1] for x in open("/proc/self/status") if x.startswith("CapEff")][0]
    r["uid"] = os.getuid()
    d = subprocess.run(["ip", "-4", "rule", "del", "pref", "120"], capture_output=True, text=True)
    r["rule_del"] = d.returncode
    r["prohibits"] = subprocess.run(["ip", "-4", "rule", "show"], capture_output=True, text=True).stdout.count("prohibit")
    r["v6_addrs"] = subprocess.run(["ip", "-6", "-o", "addr", "show", "scope", "global"], capture_output=True, text=True).stdout.strip()
    r["v6_link"] = [x for x in subprocess.run(["ip", "-6", "-o", "addr", "show"], capture_output=True, text=True).stdout.splitlines()
                    if " lo " not in x]
print("NETPROBE " + json.dumps(r))
"""

NODE_PROBE = """const net = require('net');
const s = net.connect({host: 'api.anthropic.com', port: 443, timeout: 10000});
s.on('connect', () => { console.log('NODEPROBE OPEN'); s.destroy(); });
s.on('timeout', () => { console.log('NODEPROBE timeout'); s.destroy(); });
s.on('error', (e) => console.log('NODEPROBE ' + e.code));
"""


def _gateway() -> str | None:
    try:
        routes = json.loads(subprocess.run(["ip", "-j", "-4", "route", "show", "default"], capture_output=True, text=True).stdout)
        return routes[0].get("gateway")
    except Exception:
        return None


def test_pasta_network(root: Path) -> None:
    """A pasta cell against throwaway listeners (never a real port): nothing on
    the host's loopback, its own addresses (where 0.0.0.0 services listen), the
    gateway, or private/link-local/CGNAT space is reachable, nor an abstract
    unix socket; DNS resolves and TCP 443 connects (TCP only, no request).
    Positive controls show the listeners are live and the probes sensitive."""
    if sandbox.available() or sandbox.pasta_available():
        check(f"pasta cells can run ({sandbox.available() or sandbox.pasta_available()})", False)
        return
    import secrets
    import socket
    work = root / "w-pasta"
    work.mkdir()
    lo = socket.socket()
    lo.bind(("127.0.0.1", 0))
    lo.listen(64)
    anyl = socket.socket()
    anyl.bind(("0.0.0.0", 0))
    anyl.listen(64)
    abstract = "rolebench-pasta-" + secrets.token_hex(4)
    ab = socket.socket(socket.AF_UNIX)
    ab.bind("\0" + abstract)
    ab.listen(8)
    host_addrs = [a.split("/")[0] for a in sandbox.host_addresses() if not a.startswith("127.")]
    gw = _gateway()
    spec = {"lo_port": lo.getsockname()[1], "any_port": anyl.getsockname()[1], "host_addrs": host_addrs, "gateway": gw,
            "private": ["10.0.0.1", "192.168.1.1", "172.16.0.1", "100.100.100.100", "169.254.169.254"],
            "names": ["api.anthropic.com", "openrouter.ai"], "abstract": abstract}
    (work / "netprobe.py").write_text(NET_PROBE)

    def accepts():
        n = 0
        for sock in (lo, anyl, ab):
            sock.setblocking(False)
            while True:
                try:
                    c, _ = sock.accept()
                    c.close()
                    n += 1
                except BlockingIOError:
                    break
        return n
    try:
        # positive control: from the host itself every listener answers
        host = subprocess.run([sys.executable, str(work / "netprobe.py"), json.dumps({**spec, "names": [], "private": [], "retry": 0.5})],
                              capture_output=True, text=True, timeout=60)
        hr = json.loads(next((l for l in host.stdout.splitlines() if l.startswith("NETPROBE ")), "NETPROBE {}")[9:])
        accepts()
        # the pasta layer alone (no netcell policy): no forwarding, no gateway mapping
        layer = subprocess.run([sandbox.PASTA, *sandbox.PASTA_FLAGS, "--", sys.executable, "-I", str(work / "netprobe.py"),
                                json.dumps({**spec, "names": [], "private": []})], capture_output=True, text=True, timeout=90)
        lr = json.loads(next((l for l in layer.stdout.splitlines() if l.startswith("NETPROBE ")), "NETPROBE {}")[9:])
        layer_accepts = accepts()
        # the whole cell
        with env(ROLEBENCH_NET="pasta", ROLEBENCH_LOGIN_HOME=None):
            rc, out, err, _ = sandbox.launch(["python3", str(work / "netprobe.py"), json.dumps(spec)],
                                             cell=sandbox.Cell("jarvis", work), run_id=f"check-pasta-{os.getpid()}", timeout_s=120)
            node_out = node_err = None
            if os.path.exists("/usr/bin/node"):
                (work / "nodeprobe.js").write_text(NODE_PROBE)
                _, node_out, node_err, _ = sandbox.launch(["/usr/bin/node", str(work / "nodeprobe.js")], cell=sandbox.Cell("claude", work),
                                                   run_id=f"check-pasta-node-{os.getpid()}", timeout_s=60)
        cr = json.loads(next((l for l in out.splitlines() if l.startswith("NETPROBE ")), "NETPROBE {}")[9:])
        cell_accepts = accepts()
    finally:
        for sock in (lo, anyl, ab):
            sock.close()
    print(f"    host control: {hr}")
    print(f"    pasta layer only: lo={lr.get('lo_tcp')} gw_lo={lr.get('gw_lo')} host_any={lr.get('host_any')} accepts={layer_accepts}")
    print(f"    pasta cell: {json.dumps(cr)[:1500]} accepts={cell_accepts} rc={rc} {err.strip()[-300:]}")
    check("positive control: from the host the loopback and 0.0.0.0 listeners answer on every host address",
          hr.get("lo_tcp") == "OPEN" and hr.get("host_any") and all(v == "OPEN" for v in hr["host_any"].values()))
    check("pasta layer alone: host loopback is not forwarded (-T none), even after its 1 s rescan",
          lr.get("lo_tcp") not in (None, "OPEN"), layer.stderr[-300:])
    check("pasta layer alone: the gateway address is not mapped to host loopback (--no-map-gw)",
          gw is None or lr.get("gw_lo") not in (None, "OPEN"))
    check("cell: a host 127.0.0.1 listener is unreachable (retried past pasta's rescan)", cr.get("lo_tcp") not in (None, "OPEN"))
    check("cell: a 0.0.0.0 listener is unreachable on every host address (eth0 and the rest)",
          cr.get("host_any") and all(v != "OPEN" for v in cr["host_any"].values())
          and all(v != "OPEN" for v in cr.get("host_lo_port", {}).values()))
    check("cell: the gateway is unreachable (both listeners' ports)",
          gw is None or (cr.get("gw_any") not in (None, "OPEN") and cr.get("gw_lo") not in (None, "OPEN")))
    check("cell: private, CGNAT and link-local addresses are prohibited before any packet leaves",
          cr.get("private") and all(v == "EACCES" for v in cr["private"].values()))
    check("cell: no listener saw a single connection from a cell", cell_accepts == 0, str(cell_accepts))
    if any(v == "OPEN" for v in lr.get("host_any", {}).values()):
        print("    (pasta alone still reaches a host address that is not on its interface; netcell's policy is what closes it)")
    check("cell: an abstract unix socket on the host is unreachable (its own netns)", cr.get("abstract") not in (None, "OPEN"))
    check("cell: DNS resolves both API hosts, IPv4 only", all(cr.get(f"dns_{n}") == ["AF_INET"] for n in spec["names"]),
          str({n: cr.get(f"dns_{n}") for n in spec["names"]}))
    check("cell: TCP 443 connects to api.anthropic.com and openrouter.ai (connect only, no request)",
          all(cr.get(f"tcp443_{n}") == "OPEN" for n in spec["names"]))
    check("cell: any other port to the internet is prohibited (TCP 80)", all(cr.get(f"tcp80_{n}") == "EACCES" for n in spec["names"]))
    check("cell: UDP to the host's resolver directly is prohibited (DNS only through pasta's forwarder)", cr.get("udp_host53") == "EACCES")
    check("cell: its own loopback still works (suites can bind 127.0.0.1)", cr.get("own_lo") == "OPEN")
    check("cell: the agent runs as the host's uid, not as root of pasta's namespace", cr.get("uid") == os.getuid())
    check("cell: no capabilities, so the egress rules cannot be changed from inside",
          cr.get("capeff") == "0000000000000000" and cr.get("rule_del") not in (None, 0) and cr.get("prohibits", 0) >= len(sandbox.BLOCK_V4) + 3)
    check("cell: no IPv6 address but loopback", cr.get("v6_addrs") == "" and cr.get("v6_link") == [])
    if node_out is not None:
        check("cell: Node connects to api.anthropic.com:443 too (its own resolver path)", "NODEPROBE OPEN" in node_out,
              (node_out + node_err)[-300:])


def test_netcell_fail_closed(root: Path) -> None:
    """netcell runs nothing unless the policy is in place: outside a pasta
    namespace (no CAP_NET_ADMIN), with a bad policy, or with a truncated
    credential header it exits 125 and the command never starts."""
    # an `ip` that reports success but installs nothing: the read-back must refuse
    calls = []

    def silent_ip(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
    saved_run, saved_conf = netcell.subprocess.run, netcell.IPV6_CONF
    fake_conf = root / "fake-ipv6-conf"
    (fake_conf / "eth0").mkdir(parents=True)                # never the host's /proc/sys
    netcell.subprocess.run, netcell.IPV6_CONF = silent_ip, str(fake_conf)
    try:
        lied = raises(lambda: netcell.install({"dns": sandbox.DNS_FORWARD, "block": ["10.0.0.0/8"], "allow_tcp": [443]}),
                      netcell.Refused)
    finally:
        netcell.subprocess.run, netcell.IPV6_CONF = saved_run, saved_conf
    check("netcell reads its rules back and refuses when they are not in place, whatever `ip` said",
          lied and any(a[1:3] == ["-j", "-4"] for a in calls))
    pol = {"dns": sandbox.DNS_FORWARD, "block": ["10.0.0.0/8"], "allow_tcp": [443]}

    def as_json(keys):
        out = []
        for pref, dst, proto, dport, do in keys:
            r = {"priority": pref, "src": "all"}
            if dst:
                net = dst.split("/")
                r["dst"] = net[0]
                if net[1] != "32":
                    r["dstlen"] = int(net[1])
            if proto:
                r["ipproto"], r["dport"] = proto, dport
            r.update({"action": "prohibit"} if do == "prohibit" else {"table": do})
            out.append(r)
        return out
    exact = [netcell._key(r) for r in netcell.rules(pol)] + list(netcell.DEFAULTS_V4)
    for label, keys, ok in (("exact", exact, True),
                            ("an extra rule ahead of ours", [(50, "", "", 0, "main")] + exact, False),
                            ("a duplicate", exact + [exact[3]], False)):
        def fake_show(argv, keys=keys, **kw):
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(as_json(keys)), stderr="")
        netcell.subprocess.run, netcell.IPV6_CONF = fake_show, str(root / "no-ipv6-conf")
        try:
            refused = raises(lambda: netcell.verify(pol), netcell.Refused)
        finally:
            netcell.subprocess.run, netcell.IPV6_CONF = saved_run, saved_conf
        check(f"netcell's read-back is exact: {label} " + ("passes" if ok else "is refused"), refused is (not ok))
    if os.geteuid() == 0:
        print("    (netcell fail-closed test skipped as root: it would touch the host's rules)")
        return
    marker = root / "netcell-ran"
    policy = json.dumps(sandbox.pasta_policy())
    cmd = [sys.executable, "-I", str(sandbox.NETCELL)]
    host = subprocess.run(cmd + [policy, "--", "/usr/bin/touch", str(marker)], input="2\n{}", capture_output=True,
                          text=True, timeout=30)
    check("netcell outside pasta's namespace (no CAP_NET_ADMIN) refuses and starts nothing",
          host.returncode == netcell.EXIT_REFUSED and not marker.exists() and "refused" in host.stderr, host.stderr[-200:])
    if not sandbox.pasta_available():
        inner = [sandbox.PASTA, *sandbox.PASTA_FLAGS, "--", *cmd]
        bad = subprocess.run(inner + [json.dumps({**sandbox.pasta_policy(), "allow_tcp": [0]}), "--", "/usr/bin/touch", str(marker)],
                             input="2\n{}", capture_output=True, text=True, timeout=60)
        trunc = subprocess.run(inner + [policy, "--", "/usr/bin/touch", str(marker)], input="40\n{}", capture_output=True,
                               text=True, timeout=60)
        good = subprocess.run(inner + [policy, "--", "/usr/bin/touch", str(marker)], input="2\n{}", capture_output=True,
                              text=True, timeout=60)
        check("inside pasta: a bad policy or a truncated credential header refuses with 125 and runs nothing",
              bad.returncode == netcell.EXIT_REFUSED and trunc.returncode == netcell.EXIT_REFUSED, bad.stderr[-200:] + trunc.stderr[-200:])
        check("inside pasta: a good policy and header run the command (the control)", good.returncode == 0 and marker.exists(),
              good.stderr[-200:])


def test_unit_kill(root: Path) -> None:
    """M7: a timeout kills the whole unit, a setsid child included, and in a
    pasta cell pasta and netcell's bwrap too (pasta's own command line carries
    the marker, so a surviving pasta is a survivor)."""
    if sandbox.available():
        return
    cells = [("grade", "none")] + ([("jarvis", "pasta")] if not sandbox.pasta_available() else [])
    for profile, net in cells:
        marker = str(random.randint(400000, 499999))
        work = root / f"w-kill-{net}"
        work.mkdir()
        t = time.time()
        run_id = f"check-kill-{net}-{os.getpid()}"
        with env(ROLEBENCH_NET=net):
            rc, out, err, timed_out = sandbox.launch(
                ["/bin/sh", "-c", f"setsid sleep {marker} & sleep {marker}"], cell=sandbox.Cell(profile, work),
                run_id=run_id, timeout_s=2)

        def survivors():
            found = []
            for p in Path("/proc").iterdir():
                if p.name.isdigit():
                    try:
                        if marker in (p / "cmdline").read_bytes().decode(errors="replace"):
                            found.append(p.name)
                    except OSError:
                        pass
            return found
        deadline = time.monotonic() + 10
        while survivors() and time.monotonic() < deadline:
            time.sleep(0.1)
        check(f"{profile} cell ({net}): a timeout is reported quickly and the unit's setsid child dies too"
              + (", pasta included" if net == "pasta" else ""),
              timed_out and time.time() - t < 20 and survivors() == [] and
              sandbox.wait_unit_gone(sandbox.unit_name(run_id)), f"survivors {survivors()}")


def test_fake_clis(root: Path) -> None:
    """The real adapters, with fake CLIs, inside real cells: argv, env, logins."""
    if sandbox.available():
        return
    work = root / "w-cli"
    work.mkdir()
    login = root / "login2"
    (login / ".claude").mkdir(parents=True)
    (login / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-dummy", "refreshToken": "r", "expiresAt": (time.time() + 86400) * 1000,
        "scopes": ["user:inference"], "subscriptionType": "max"}}))
    import base64
    payload = base64.urlsafe_b64encode(json.dumps({"exp": time.time() + 86400}).encode()).decode().rstrip("=")
    (login / ".codex").mkdir()
    (login / ".codex" / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {
        "access_token": f"h.{payload}.s", "refresh_token": "cx-real-refresh-dummy-7f3a", "id_token": "i", "account_id": "a"}}))
    fake = root / "bin"
    (fake / "rel" / "bin").mkdir(parents=True)
    (fake / "rel" / "codex-path").mkdir()
    (fake / "claude").write_text(
        "#!/bin/sh\nenv > ./claude-env.txt\necho \"$@\" > ./claude-argv.txt\ncat > ./claude-stdin.txt\n"
        "C=\"$HOME/.claude/.credentials.json\"\n"
        "python3 -c \"import json,os,stat;d=json.load(open('$C'));d['_perm']=oct(stat.S_IMODE(os.stat('$C').st_mode))[2:];print(json.dumps(d))\" > ./claude-cred.json 2>/dev/null\n"
        "echo '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\",\"total_cost_usd\":0.5,\"num_turns\":1,\"usage\":{},"
        "\"modelUsage\":{\"claude-haiku-5-5\":{},\"claude-sonnet-5-5\":{}}}'\n")
    (fake / "claude").chmod(0o755)
    (fake / "rel" / "bin" / "codex").write_text(
        "#!/bin/sh\nenv > ./codex-env.txt\ntest -e \"$CODEX_HOME/auth.json\" && echo auth=present > ./codex-auth.txt\n"
        "python3 -c \"import json,os;t=json.load(open(os.environ['CODEX_HOME']+'/auth.json'))['tokens'];"
        "print(json.dumps({'keys':sorted(t),'placeholder':t.get('refresh_token')=='" + sandbox.CODEX_REFRESH_PLACEHOLDER + "'}))\""
        " > ./codex-refresh.json 2>/dev/null\n"
        # the needle is assembled at run time, so this script (bound under /opt/rolebench) never matches itself
        "N=cx-real-refresh-; N=\"${N}dummy-7f3a\"\n"
        "if grep -rqsF \"$N\" \"$HOME\" /tmp /opt/rolebench 2>/dev/null || env | grep -qF \"$N\";"
        " then echo leak=yes > ./codex-leak.txt; else echo leak=no > ./codex-leak.txt; fi\n"
        "cat > /dev/null\necho '{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"done\"}}'\n"
        "echo '{\"type\":\"turn.completed\",\"usage\":{\"input_tokens\":100,\"cached_input_tokens\":40,\"output_tokens\":5}}'\n")
    (fake / "rel" / "bin" / "codex").chmod(0o755)
    (fake / "codex").symlink_to(fake / "rel" / "bin" / "codex")
    with env(PATH=f"{fake}:{os.environ['PATH']}", ROLEBENCH_LOGIN_HOME=str(login), GH_TOKEN="gh-dummy",
             OPENROUTER_API_KEY="sk-dummy-not-a-key", CLAUDE_CODE_SSE_PORT="9", ROLEBENCH_NET="none"):
        res = runners.run_claude(model="haiku", prompt="PROMPT TEXT", cwd=work, timeout_s=60, max_turns=3,
                                 run_id=f"check-claude-{os.getpid()}")
        cx = runners.run_codex(model="gpt-6-luna", prompt="P", cwd=work, timeout_s=60, run_id=f"check-codex-{os.getpid()}")
        # A2: the trimmed login the claude cell actually got, read back from the fake CLI's dump
        cred_seen = (work / "claude-cred.json").read_text() if (work / "claude-cred.json").exists() else "{}"
    env_seen = (work / "claude-env.txt").read_text() if (work / "claude-env.txt").exists() else ""
    argv_seen = (work / "claude-argv.txt").read_text() if (work / "claude-argv.txt").exists() else ""
    cred = json.loads(cred_seen).get("claudeAiOauth", {})
    check("the claude cell's login is the trimmed view, 0400, no refresh or MCP token (A2)",
          "accessToken" in cred and "refreshToken" not in cred and "mcpOAuth" not in json.loads(cred_seen)
          and json.loads(cred_seen).get("_perm") in ("400", None))
    check("claude runs inside the cell: no GH_TOKEN, key or CLAUDE_CODE_SSE_PORT, and HOME is the cell's",
          env_seen and "GH_TOKEN" not in env_seen and "OPENROUTER" not in env_seen and "CLAUDE_CODE_SSE_PORT" not in env_seen
          and f"HOME={sandbox.sandbox_home()}" in env_seen and "GIT_ASKPASS=/bin/false" in env_seen, res.error)
    check("claude gets the tool allowlist, not a deny-list",
          f"--tools {runners.CLAUDE_TOOLS}" in argv_seen and "--disallowedTools" not in argv_seen
          and runners.CLAUDE_TOOLS == "Bash,Read,Edit,Write,Grep,Glob")
    check("the prompt arrives on stdin and the result is parsed",
          (work / "claude-stdin.txt").read_text() == "PROMPT TEXT" and res.text == "ok" and res.cost_usd == 0.5)
    check("claude's modelUsage is recorded and a two-model run is flagged",
          res.models_used == ["claude-haiku-5-5", "claude-sonnet-5-5"] and res.summary()["multi_model"])
    seen_refresh = json.loads((work / "codex-refresh.json").read_text()) if (work / "codex-refresh.json").exists() else {}
    check("the codex cell's auth.json has refresh_token equal to the placeholder, and all four token fields",
          seen_refresh.get("placeholder") is True
          and seen_refresh.get("keys") == ["access_token", "account_id", "id_token", "refresh_token"], str(seen_refresh))
    check("the real refresh token is nowhere in the codex cell (home, /tmp, /opt/rolebench, environment)",
          (work / "codex-leak.txt").exists() and (work / "codex-leak.txt").read_text().strip() == "leak=no")
    cenv = (work / "codex-env.txt").read_text() if (work / "codex-env.txt").exists() else ""
    check("codex runs inside the cell with its own CODEX_HOME and the login in it",
          f"CODEX_HOME={sandbox.sandbox_home()}/{sandbox.CODEX_HOME_NAME}" in cenv
          and (work / "codex-auth.txt").read_text().strip() == "auth=present" and cx.text == "done", cx.error)
    check("codex tokens split: input excludes the cached tokens it includes",
          cx.summary()["token_split"] == {"input": 60, "cached": 40, "output": 5})
    if not sandbox.pasta_available():
        for f in ("claude-cred.json", "claude-stdin.txt", "claude-env.txt"):
            (work / f).unlink(missing_ok=True)
        with env(PATH=f"{fake}:{os.environ['PATH']}", ROLEBENCH_LOGIN_HOME=str(login), ROLEBENCH_NET="pasta"):
            pres = runners.run_claude(model="haiku", prompt="PASTA PROMPT \u00e9", cwd=work, timeout_s=60, max_turns=3,
                                      run_id=f"check-claude-pasta-{os.getpid()}")
        pcred = json.loads((work / "claude-cred.json").read_text()) if (work / "claude-cred.json").exists() else {}
        check("in a pasta cell the claude adapter gets the trimmed 0400 login through netcell, and the prompt intact",
              "accessToken" in pcred.get("claudeAiOauth", {}) and "refreshToken" not in pcred.get("claudeAiOauth", {})
              and pcred.get("_perm") in ("400", None) and (work / "claude-stdin.txt").read_text() == "PASTA PROMPT \u00e9"
              and pres.text == "ok", pres.error)
    with env(ROLEBENCH_LOGIN_HOME=str(login), ROLEBENCH_NET="none"):
        (login / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "sk-ant-dummy", "expiresAt": (time.time() + 60) * 1000}}))
        soon = runners.run_claude(model="haiku", prompt="x", cwd=work, timeout_s=1800, max_turns=1)
    check("a login that would expire during the run is refused (never refreshed in a read-only cell)",
          soon.error.startswith("refused:") and soon.crashed)


CODEX_PROBE = """import socket, errno, json
r = {{}}
for key, fam, addr in (("tcp", socket.AF_INET, ("127.0.0.1", {port})), ("abstract", socket.AF_UNIX, "\\0{name}")):
    try:
        s = socket.socket(fam, socket.SOCK_STREAM); s.settimeout(3); s.connect(addr); r[key] = "connected"
    except OSError as e:
        r[key] = errno.errorcode.get(e.errno, str(e.errno))
print("PROBE " + json.dumps(r))
"""


def test_codex_loopback(root: Path) -> None:
    """Codex keeps the host netns, so its own sandbox is what keeps agent commands
    off loopback. `codex sandbox` (no model call) in our codex cell: the
    workspace profile gets EPERM on a throwaway TCP listener and a throwaway
    abstract unix socket, with zero accepts; danger-full-access (the positive
    control) connects to both, so the probe is sensitive."""
    if sandbox.available():
        return
    if not sandbox.codex_release() or not Path(config.real_home(), ".codex", "auth.json").is_file():
        print("    (codex loopback test skipped: no codex binary or login on this host)")
        return
    import secrets
    import socket
    work = root / "w-codexnet"
    work.mkdir()
    tcp = socket.socket()
    tcp.bind(("127.0.0.1", 0))
    tcp.listen(8)
    name = "rolebench-probe-" + secrets.token_hex(4)
    ab = socket.socket(socket.AF_UNIX)
    ab.bind("\0" + name)
    ab.listen(8)
    (work / "probe.py").write_text(CODEX_PROBE.format(port=tcp.getsockname()[1], name=name))
    seen = {}
    try:
        with env(ROLEBENCH_LOGIN_HOME=None):
            for prof in (":workspace", ":danger-full-access"):
                cmd = [str(sandbox.CODEX / "bin" / "codex"), "sandbox", "-P", prof, "-C", str(work), "--",
                       "python3", str(work / "probe.py")]
                rc, out, err, to = sandbox.launch(cmd, cell=sandbox.Cell("codex", work),
                                                  run_id=f"check-cxnet-{prof.strip(':')}-{os.getpid()}", timeout_s=90)
                line = next((l for l in out.splitlines() if l.startswith("PROBE ")), "PROBE {}")
                accepts = {}
                for key, sock in (("tcp", tcp), ("abstract", ab)):
                    sock.setblocking(False)
                    accepts[key] = 0
                    while True:
                        try:
                            c, _ = sock.accept()
                            c.close()
                            accepts[key] += 1
                        except BlockingIOError:
                            break
                seen[prof] = (json.loads(line[6:]), accepts, err.strip()[-200:])
    finally:
        tcp.close()
        ab.close()
    ws, ws_acc, ws_err = seen.get(":workspace", ({}, {}, ""))
    dfa, dfa_acc, _ = seen.get(":danger-full-access", ({}, {}, ""))
    print(f"    codex sandbox :workspace -> {ws} accepts {ws_acc}; :danger-full-access -> {dfa} accepts {dfa_acc}")
    check("codex's workspace sandbox refuses loopback TCP and abstract unix sockets (EPERM, zero accepts)",
          ws.get("tcp") == "EPERM" and ws.get("abstract") == "EPERM" and ws_acc == {"tcp": 0, "abstract": 0}, ws_err)
    check("positive control: danger-full-access connects to both, so the probe is sensitive",
          dfa.get("tcp") == "connected" and dfa.get("abstract") == "connected"
          and dfa_acc.get("tcp", 0) >= 1 and dfa_acc.get("abstract", 0) >= 1)


def test_jarvis_in_cell(root: Path) -> None:
    """The real jarvis adapter and worker in a cell with no network: a zero
    budget means the first chat call is refused, so nothing leaves the machine."""
    if sandbox.available():
        return
    work = root / "w-jarvis"
    work.mkdir()
    seen = {}

    def offline(command, *, cell, run_id, timeout_s, stdin=None):
        cell.network = False
        seen["argv"] = sandbox.bwrap_argv(cell, command)
        seen["stdin"] = stdin
        return sandbox.launch(command, cell=cell, run_id=run_id, timeout_s=timeout_s, stdin=stdin)
    with env(OPENROUTER_API_KEY="sk-dummy-not-a-key"):
        res = runners.run_jarvis(model="openai/gpt-6-luna", prompt="hi", cwd=work, timeout_s=180, max_turns=2,
                                 budget_usd=0.0, run_id=f"check-jarvis-{os.getpid()}", launch=offline)
    argv = seen.get("argv", [])
    check("the key goes to the worker on stdin, never into the cell's environment",
          seen.get("stdin") == "sk-dummy-not-a-key\n" and not any("OPENROUTER" in a for a in argv) and argv)
    check("the jarvis worker ran in the cell, wrote its spend and stopped at the budget before any call",
          "budget" in res.error.lower() and res.spend_usd == 0.0 and not res.crashed, f"{res.error[:300]}")


# --- quota, judge and report ------------------------------------------------------------


def test_env_and_quota() -> None:
    env_ = {"PATH": "/bin", "HOME": "/h", "GH_TOKEN": "x", "GITHUB_TOKEN": "y", "OPENROUTER_API_KEY": "z"}
    clean = runners.scrubbed_env(env_)
    check("tokens are scrubbed from the harness's own git environment",
          not {"GH_TOKEN", "GITHUB_TOKEN", "OPENROUTER_API_KEY"} & set(clean) and clean["PATH"] == "/bin")
    hot = {"windows": [{"name": "5h", "used_percent": 80, "resets_at": 0}]}
    cold = {"windows": [{"name": "5h", "used_percent": 40}]}
    stale = {"windows": [{"name": "5h", "used_percent": 99, "resets_at": 1}]}
    check("a window at 80% skips the claude cell", quota.guard("claude", lambda p: hot)[0] is True)
    check("exactly 75% skips", quota.guard("codex", lambda p: {"windows": [{"used_percent": 75}]})[0] is True)
    check("a cold quota proceeds", quota.guard("claude", lambda p: cold) == (False, ""))
    check("a window past its reset is ignored", quota.guard("claude", lambda p: stale)[0] is False)
    skip, note = quota.guard("claude", lambda p: None)
    check("unknown quota fails closed", skip is True and "unknown" in note)
    check("a reader that raises is unknown, so skipped", quota.guard("codex", lambda p: 1 / 0)[0] is True)
    check("a quota with no measured window is unknown", quota.guard("claude", lambda p: {"windows": [{"used_percent": None}]})[0] is True)
    check("the jarvis harness has no quota guard", quota.guard("jarvis", lambda p: hot) == (False, ""))
    at = lambda pct: (lambda p: {"windows": [{"name": "weekly", "used_percent": pct}]})
    check("the override lifts the 75% guard", quota.guard("claude", at(90), override="ok") == (False, ""))
    check("the override never lifts the 97% ceiling (claude and codex)",
          quota.guard("claude", at(97), override="ok")[0] is True and quota.guard("codex", at(99), override="ok")[0] is True
          and "ceiling" in quota.guard("claude", at(97), override="ok")[1])
    check("the ceiling defaults to 97", config.QUOTA_CEILING == 97.0 and quota.ceiling() == 97.0)
    check("the override does not run blind: unknown quota still skips",
          quota.guard("claude", lambda p: None, override="ok")[0] is True)
    with env(ROLEBENCH_QUOTA_CEILING="80"):
        check("ROLEBENCH_QUOTA_CEILING sets the ceiling", quota.guard("claude", at(85), override="ok")[0] is True
              and quota.guard("claude", at(79), override="ok")[0] is False)
    with env(ROLEBENCH_QUOTA_CEILING="98"):
        up = quota.guard("claude", at(1), override="ok")
    check("ROLEBENCH_QUOTA_CEILING can only lower the ceiling: 98 is refused with a clear message",
          up[0] is True and "can only lower it" in up[1])
    unk = quota.guard("claude", lambda p: None, override="ok")[1]
    check("under the override an unknown quota says the ceiling needs a reading from the daemon's /usage",
          "ceiling needs a reading" in unk and "/usage" in unk)
    for bad in ("abc", "0", "-5", "101", "nan", "inf"):
        with env(ROLEBENCH_QUOTA_CEILING=bad):
            check(f"ROLEBENCH_QUOTA_CEILING={bad} fails closed", quota.guard("claude", at(1), override="ok")[0] is True)
    check("a stale window does not count toward the ceiling either",
          quota.guard("claude", lambda p: {"windows": [{"used_percent": 99, "resets_at": 1}, {"used_percent": 5}]}, override="ok") == (False, ""))
    check("readings are per provider and window, None when unknown",
          quota.readings(["claude", "codex"], lambda p: cold if p == "claude" else None) == {"claude": {"5h": 40.0}, "codex": None})
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
    check("token splits per harness", runners.token_split("claude", {"input_tokens": 10, "cache_creation_input_tokens": 5,
                                                                      "cache_read_input_tokens": 300, "output_tokens": 7})
          == {"input": 15, "cached": 300, "output": 7}
          and runners.token_split("jarvis", {"prompt_tokens": 100, "cached_tokens": 30, "completion_tokens": 4})
          == {"input": 70, "cached": 30, "output": 4})


# --- parsers, flags, audits -------------------------------------------------------------

BLOCK = '```json\n{"findings": [{"severity": "MED", "file": "tiny.py", "line": 2, "title": "bound excluded", "scenario": "clamp(5,0,5) is 4"}, {"severity": "WAT", "title": "x"}, {"severity": "LOW", "title": "stale doc", "scenario": "s"}], "ran_tests": true, "tried_mutant": true}\n```'


def test_parsers(root: Path) -> None:
    p = findings.parse("prose\n```json\n{\"findings\": []}\n```\nmore\n" + BLOCK)
    check("the last findings block wins", len(p.findings) == 2 and p.found_block)
    check("a finding with a bad severity is dropped and reported", any("bad severity" in e for e in p.errors))
    check("self-reported flags are read", p.ran_tests is True and p.tried_mutant is True)
    check("no block is an error, not a crash", findings.parse("nothing here").found_block is False
          and findings.parse("```json\n{not json}\n```").errors == ["no findings block"])
    check("an empty findings list is a valid review", findings.parse('```json\n{"findings": []}\n```').found_block)
    calls_ok = [("Bash", '{"command": "python tests/foo_check.py"}'), ("Edit", "{}")]
    check("tests and a mutant are observed", findings.flags(calls_ok, p) == {"ran_tests": True, "tried_mutant": True, "observed": True})
    cases_ = {
        "codex bash -lc wrapper runs a suite": ([("shell", "/bin/bash -lc 'cd x && env PYTHONPATH=. python tests/a_check.py'")], True, None),
        "a Read of a _check.py file is not a test run": ([("Read", '{"file_path": "tests/a_check.py"}')], False, None),
        "grep for _check.py is not a test run": ([("Bash", '{"command": "grep -rn foo tests/a_check.py"}')], False, None),
        "cat of a suite is not a test run": ([("Bash", '{"command": "cat tests/a_check.py"}')], False, None),
        "npx vitest is a test run": ([("Bash", '{"command": "cd hud && npx vitest run src/x.test.ts"}')], True, None),
    }
    for name, (calls, tests, _) in cases_.items():
        check(f"ran_tests: {name}", findings.flags(calls, p)["ran_tests"] is tests)
    edits = {
        "sed -E -i (flag not first)": ("Bash", '{"command": "sed -E -i \'s/a/b/\' tiny.py"}', True),
        "sed --in-place": ("Bash", '{"command": "sed --in-place=.bak s/a/b/ tiny.py"}', True),
        "perl -p -i": ("shell", "perl -p -i -e 's/a/b/' x.py", True),
        "perl -0pi": ("shell", "/bin/bash -lc \"perl -0pi -e 's/a/b/' x.py\"", True),
        "cp over a source file": ("Bash", '{"command": "cp /tmp/x.py jarvis/a.py"}', True),
        "heredoc write": ("Bash", '{"command": "cat > tiny.py <<EOF\\nx = 1\\nEOF"}', True),
        "python heredoc write_text": ("Bash", '{"command": "python - <<EOF\\nfrom pathlib import Path\\nPath(\\"a.py\\").write_text(\\"\\")\\nEOF"}', True),
        "redirect to a source file": ("shell", "echo 'x = 1' >> tiny.py", True),
        "grep write_text is not an edit": ("Bash", '{"command": "grep -rn write_text jarvis/"}', False),
        "a Grep tool hit is not an edit": ("Grep", '{"pattern": "write_text|sed -i"}', False),
        "perl -Ilib is not in place": ("shell", "perl -Ilib x.pl", False),
        "a log redirect is not an edit": ("shell", "python tests/a_check.py > out.txt 2>&1", False),
    }
    for name, (tool, args, want) in edits.items():
        got = findings.flags([(tool, args)], p)["tried_mutant"]
        check(f"tried_mutant: {name}", got is want)
    calls_no_edit = [("Bash", '{"command": "python tests/foo_check.py"}'), ("Read", "{}")]
    check("a claimed mutant without any edit is not believed", findings.flags(calls_no_edit, p)["tried_mutant"] is False)
    check("no tool calls falls back to the self-report", findings.flags([], p)["ran_tests"] is True)
    check("no tests run is observed", findings.flags([("Read", "{}")], p)["ran_tests"] is False)

    stream = "\n".join(json.dumps(m) for m in [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "final " + BLOCK, "total_cost_usd": 0.0123,
         "num_turns": 7, "usage": {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 300},
         "modelUsage": {"claude-haiku-5-5": {"inputTokens": 1}}}])
    r = runners.parse_claude_stream(stream, "haiku")
    check("claude stream: cost, turns, tokens, tools, full text, one model",
          r.cost_usd == 0.0123 and r.turns == 7 and r.tokens["cache_read_input_tokens"] == 300
          and r.tool_calls[0][0] == "Bash" and r.text.startswith("final") and r.models_used == ["claude-haiku-5-5"]
          and not r.summary()["multi_model"])
    events = "\n".join(json.dumps(e) for e in [
        {"type": "item.completed", "item": {"type": "command_execution", "command": "python x.py"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done " + BLOCK}},
        {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 5, "cached_input_tokens": 40}},
        {"type": "turn.completed", "usage": {"input_tokens": 50, "output_tokens": 5}}])
    c = runners.parse_codex_events(events, "gpt-6-luna")
    check("codex events: tokens summed over turns, tools, text",
          c.tokens == {"input_tokens": 150, "output_tokens": 10, "cached_input_tokens": 40} and c.turns == 2
          and c.tool_calls == [("shell", "python x.py")] and c.text.startswith("done"))
    argv = runners.claude_argv("haiku", 50)
    check("claude flags: stream-json, strict MCP, project-only settings, bypass with an allowlist",
          "stream-json" in argv and "--strict-mcp-config" in argv and "--setting-sources" in argv
          and argv[argv.index("--setting-sources") + 1] == "project,local"
          and argv[argv.index("--tools") + 1] == "Bash,Read,Edit,Write,Grep,Glob" and "--disallowedTools" not in argv)
    ro = runners.claude_argv("opus", 30, read_only=True)
    check("the judge's claude is read-only", "--tools" in ro and ro[ro.index("--tools") + 1] == "Read,Grep,Glob" and "bypassPermissions" not in ro)
    cx = runners.codex_argv("gpt-6-luna", "/s", Path("/t"))
    check("codex flags: workspace-write, network off, json, the sandbox's binary",
          "workspace-write" in cx and "sandbox_workspace_write.network_access=false" in cx and "--json" in cx
          and cx[0] == str(sandbox.CODEX / "bin" / "codex"))
    check("codex's approval policy is pinned to never (no escalated, unsandboxed re-run)",
          'approval_policy="never"' in cx and cx[cx.index('approval_policy="never"') - 1] == "-c")

    snap = Path("/home/rolebench-audit/cache/snap/0123456789abcdef")    # outside /tmp, which a cell may use
    R = runners.RunResult
    check("audit: gh and api.github are network markers",
          audit.network_flags(R("c", "m", tool_calls=[("Bash", '{"command": "gh pr view 1"}')]))
          and audit.network_flags(R("c", "m", tool_calls=[("Bash", "python -c 'x=\"api.github.com\"'")])))
    esc = lambda cmd, tool="Bash": audit.escape_flags(R("c", "m", tool_calls=[(tool, json.dumps({"command": cmd}))]), snap)
    check("audit: an absolute path outside the snapshot is flagged", esc("cat /home/johnw/projects/Jarvis/CLAUDE.md") != [])
    check("audit: a path inside the snapshot is not", esc(f"cat {snap}/tiny.py") == [])
    check("audit: `..` inside an absolute snapshot path is normalized before the test",
          esc(f"cat {snap}/../../../etc/passwd") != [])
    check("audit: a sibling with the snapshot's name as a prefix is outside",
          esc(f"cat {snap}X/secret") != [])
    check("audit: $HOME, ${HOME} and ~ are flagged", esc("cat $HOME/.ssh/id_rsa") != [] and esc("ls ${HOME}") != []
          and esc("cat ~/.config/gh/hosts.yml") != [])
    check("audit: a relative `..` that climbs out of the snapshot is flagged", esc("cat ../../other/x") != [])
    check("audit: a relative `..` that stays inside is not", esc("cat tests/../tiny.py") == [])
    check("audit: the sandbox's python and /usr are allowed", esc(f"{sandbox.PYTHON} tests/x_check.py; /usr/bin/git diff HEAD~1") == [])
    check("audit: a file body with ../ imports is not an escape",
          audit.escape_flags(R("c", "m", tool_calls=[("Edit", json.dumps({"file_path": f"{snap}/a.ts",
                                                                            "new_string": "import x from '../../lib'"}))]), snap) == [])
    check("audit: the lead's /tmp/claude-* is flagged", esc("ls /tmp/claude-1000/") != [])


# --- the flows, against fakes ---------------------------------------------------------

REVIEW_REPLY = "I ran the suite.\n" + BLOCK


def fake_runner(replies, seen=None, cost=0.01, tokens=None, edits=None, tool_calls=None, timed_out=False, spend_usd=None):
    """A runner that returns scripted replies and records what the model could see."""
    calls = []

    def run(*, model, prompt, cwd, timeout_s, max_turns, budget_usd=None, out_dir=None, run_id=None):
        calls.append({"prompt": prompt, "cwd": Path(cwd), "budget": budget_usd, "run_id": run_id})
        if seen is not None:
            seen.append({"hidden_present": (Path(cwd) / "tiny_hidden_check.py").read_text() if (Path(cwd) / "tiny_hidden_check.py").exists() else None})
        if edits:
            edits(len(calls), Path(cwd))
        text = replies[min(len(calls), len(replies)) - 1]
        return runners.RunResult("fake", model, text=text, cost_usd=cost, tokens=dict(tokens or {"input_tokens": 5}),
                                 turns=3, tool_calls=list(tool_calls or [("Bash", '{"command": "python tests/tiny_check.py"}'), ("Edit", "{}")]),
                                 duration_s=1.0, timed_out=timed_out, spend_usd=spend_usd)
    run.calls = calls
    return run


def test_review_flow(shas, root: Path) -> None:
    case = review_case(shas)
    deps = flows.Deps(runners={"claude": fake_runner([REVIEW_REPLY]), "jarvis": fake_runner([REVIEW_REPLY], cost=0.3)},
                      quota_override=NO_QUOTA, repo=shas["repo"], ledger=spend.Ledger(root / "led.json", 1.0, 0.5))
    run = flows.review_once(deps, case, harness="claude", model="haiku")
    rd = config.runs_root() / run["run_id"]
    check("a review run is ok and records its findings", run["result"] == "ok" and run["n_findings"] == 2)
    check("the run directory holds prompt, review text, findings and run.json",
          all((rd / f).exists() for f in ("prompt.txt", "review.txt", "findings.json", "run.json")))
    prompt = (rd / "prompt.txt").read_text()
    check("the brief carries the case brief, focus and suites but not the ground truth",
          "Adds a clamp helper." in prompt and "off-by-one" in prompt and "tests/tiny_check.py" in prompt
          and "clamp excludes the upper bound" not in prompt and "mutant of your own" in prompt)
    check("the brief names the sandbox's python and no systemd-run (there is no user bus in a cell)",
          sandbox.PYTHON in prompt and "systemd-run" not in prompt)
    check("the brief's network sentence is true per harness",
          "no network access for your commands" in brief.review_prompt(case, "/s", harness="codex")
          and "Do not use the network" in brief.review_prompt(case, "/s", harness="claude")
          and "no network" not in brief.review_prompt(case, "/s", harness="claude")
          and "no network" not in brief.review_prompt(case, "/s", harness="jarvis"))
    check("flags come from the tool calls", run["flags"] == {"ran_tests": True, "tried_mutant": True, "observed": True})
    row = json.loads(config.log_path().read_text().splitlines()[-1])
    check("the log row has the skill's fields and the quota state",
          row.keys() >= {"case_id", "run_id", "model", "harness", "roles", "wall_s", "result", "rounds", "merged_ready",
                         "task_class", "tool_calls", "quota_guard"} and row["quota_guard"] == f"off: {NO_QUOTA}")
    check("the snapshot is reset to the PR commit after the run", sh(Path(run["snapshot"]), "status", "--short") == "")
    check("the run records its reasoning effort in run.json and the log row", run["effort"] == "high" and row["effort"] == "high")
    snap_p = Path(run["snapshot"])
    benign = runners.RunResult("codex", "m", tool_calls=[
        ("shell", "/bin/bash -lc \"rg --files -g 'CLAUDE.md' -g '!**/.git/**'\""), ("shell", "cat .git/config"),
        ("shell", "git -C . status"), ("shell", f"ls {snap_p}/.git/")])
    escape = runners.RunResult("codex", "m", tool_calls=[("shell", "cat /home/johnw/.git/config")])
    check("the escape audit ignores a glob's /.git/ and paths inside the snapshot, and still flags a real escape",
          audit.escape_flags(benign, snap_p) == [] and len(audit.escape_flags(escape, snap_p)) == 1,
          str(audit.escape_flags(benign, snap_p)))
    check("the claude cell had no budget imposed", deps.runners["claude"].calls[0]["budget"] is None)
    check("run.json keeps the random snapshot name and the build-time shas",
          run["snapshot"].endswith(Path(run["snapshot"]).name) and run["shas"]["change"] and run["hygiene"]["after_reset"] == [])

    glm, ds, luna = "z-ai/glm-5.3-flash", "deepseek/deepseek-v4.1-flash", "anthropic/claude-haiku-5.5"  # all on the matrix
    j = flows.review_once(deps, case, harness="jarvis", model=glm, n=2)
    check("a jarvis cell is given a budget from the ledger", deps.runners["jarvis"].calls[0]["budget"] == 0.5)
    check("a jarvis run is charged to the ledger", abs(deps.get_ledger().spent() - 0.3) < 1e-9)
    j2 = flows.review_once(deps, case, harness="jarvis", model=glm, n=3)
    j3 = flows.review_once(deps, case, harness="jarvis", model=ds, n=4)
    check("the budget is whatever the total cap leaves", deps.runners["jarvis"].calls[1]["budget"] == 0.5 and j3["result"] in ("ok", "skipped"))
    check("a jarvis-only run reads no quota", j["quota_before"] == {} and "quota_after" not in j)
    over = fake_runner([REVIEW_REPLY], cost=0.6)
    deps2 = flows.Deps(runners={"jarvis": over}, quota_override=NO_QUOTA, repo=shas["repo"], ledger=spend.Ledger(root / "led2.json", 10, 0.5))
    o1 = flows.review_once(deps2, case, harness="jarvis", model="z-ai/glm-5.3-flash", n=1)
    o2 = flows.review_once(deps2, case, harness="jarvis", model="z-ai/glm-5.3-flash", n=2)
    check("a run over the per-run cap is marked, and its model is aborted", o1["result"] == "over-cap" and o2["result"] == "skipped" and "per-run cap" in o2["reason"])
    deps3 = flows.Deps(runners={"jarvis": fake_runner([REVIEW_REPLY], cost=0.001, tokens={"prompt_tokens": 1_000_000, "completion_tokens": 0})},
                       quota_override=NO_QUOTA, repo=shas["repo"], ledger=spend.Ledger(root / "led3.json", 10, 100))
    flows.review_once(deps3, case, harness="jarvis", model=ds, n=1)
    check("the ledger settles on reported usage, not on the estimate price", abs(deps3.get_ledger().spent() - 0.001) < 1e-9)
    deps4 = flows.Deps(runners={"jarvis": fake_runner([REVIEW_REPLY], cost=0.0, timed_out=True)}, quota_override=NO_QUOTA,
                       repo=shas["repo"], ledger=spend.Ledger(root / "led4.json", 10, 0.5))
    flows.review_once(deps4, case, harness="jarvis", model=luna, n=1)
    check("a jarvis run that timed out is charged its whole reservation", abs(deps4.get_ledger().spent() - 0.5) < 1e-9
          and deps4.get_ledger().pending() == {})
    deps5 = flows.Deps(runners={"jarvis": fake_runner([REVIEW_REPLY], cost=0.01, spend_usd=0.2)}, quota_override=NO_QUOTA,
                       repo=shas["repo"], ledger=spend.Ledger(root / "led5.json", 10, 0.5))
    flows.review_once(deps5, case, harness="jarvis", model=luna, n=1)
    check("the worker's running total (sub-agents included) wins over a lower reported cost",
          abs(deps5.get_ledger().spent() - 0.2) < 1e-9)
    deps6 = flows.Deps(runners={"jarvis": fake_runner([REVIEW_REPLY])}, quota_override=NO_QUOTA, repo=shas["repo"],
                       ledger=spend.Ledger(root / "led6.json", 10, 0.5), off_matrix=True)    # past the matrix, the Kimi guard still holds
    k3 = flows.review_once(deps6, case, harness="jarvis", model="moonshotai/kimi-k3", n=1)
    check("a Kimi id (even K3, now off the matrix) is skipped before any spend",
          k3["result"] == "skipped" and "refused" in k3["reason"] and deps6.runners["jarvis"].calls == []
          and deps6.get_ledger().pending() == {} and deps6.get_ledger().spent() == 0)

    mreads = []
    dm = flows.Deps(runners={"jarvis": fake_runner([REVIEW_REPLY]), "claude": fake_runner([REVIEW_REPLY])}, repo=shas["repo"],
                    ledger=spend.Ledger(root / "led-matrix.json", 10, 0.5), quota_reader=lambda p: mreads.append(p) or COLD)
    off1 = flows.review_once(dm, case, harness="jarvis", model="openai/gpt-6-luna")
    off2 = flows.review_once(dm, case, harness="claude", model="claude-opus-9")
    off3 = flows.implement_once(dm, impl_case(shas), harness="claude", model="haiku", reviewer_harness="jarvis",
                                reviewer_model="moonshotai/kimi-k3")
    check("a model off the O-1 matrix for its harness is refused up front (gpt-6-luna left the Jarvis loop)",
          all(r["result"] == "skipped" and "not in the O-1 matrix" in r["reason"] and "--off-matrix" in r["reason"]
              for r in (off1, off2, off3))
          and dm.runners["jarvis"].calls == [] and dm.runners["claude"].calls == [] and mreads == []
          and not (root / "led-matrix.json").exists())
    dm.off_matrix = True
    on1 = flows.review_once(dm, case, harness="jarvis", model="openai/gpt-6-luna")
    row = json.loads(config.log_path().read_text().splitlines()[-1])
    check("--off-matrix runs it, and the run row and run.json record off_matrix",
          on1["result"] == "ok" and on1["off_matrix"] is True and row["off_matrix"] is True
          and json.loads(config.log_path().read_text().splitlines()[0]).get("off_matrix") is False)

    def no_price(model):
        raise prices.PriceError(f"no endpoint price for {model}")
    deps7 = flows.Deps(runners={"jarvis": fake_runner([REVIEW_REPLY])}, quota_override=NO_QUOTA, repo=shas["repo"],
                       ledger=spend.Ledger(root / "led7.json", 10, 0.5), price_fn=no_price)
    np_ = flows.review_once(deps7, case, harness="jarvis", model=glm, n=1)
    check("a jarvis model with no endpoint price is skipped before any reservation or call",
          np_["result"] == "skipped" and "no endpoint price" in np_["reason"] and deps7.runners["jarvis"].calls == []
          and not (root / "led7.json").exists())

    hot = flows.Deps(runners={"claude": fake_runner([REVIEW_REPLY])}, repo=shas["repo"],
                     quota_reader=lambda p: {"windows": [{"name": "weekly", "used_percent": 91}]})
    s = flows.review_once(hot, case, harness="claude", model="opus")
    check("a quota-hot cell is skipped, logged, and the model never ran",
          s["result"] == "skipped" and "weekly at 91%" in s["reason"] and hot.runners["claude"].calls == []
          and json.loads(config.log_path().read_text().splitlines()[-1])["result"] == "skipped")
    unk = flows.Deps(runners={"claude": fake_runner([REVIEW_REPLY])}, repo=shas["repo"], quota_reader=lambda p: None)
    u = flows.review_once(unk, case, harness="claude", model="opus")
    check("an unknown quota skips the cell (fail closed)", u["result"] == "skipped" and "unknown" in u["reason"]
          and unk.runners["claude"].calls == [])
    ovr = flows.Deps(runners={"claude": fake_runner([REVIEW_REPLY])}, repo=shas["repo"],
                     quota_reader=lambda p: {"windows": [{"name": "weekly", "used_percent": 91}]}, quota_override="owner said so")
    uo = flows.review_once(ovr, case, harness="claude", model="opus")
    check("the owner's override runs a cell over the 75% guard, recorded in run.json",
          uo["result"] == "ok" and uo["quota_guard"] == "off: owner said so")
    check("quota_before and quota_after are recorded per window, in run.json and the log row",
          uo["quota_before"] == {"claude": {"weekly": 91.0}} and uo["quota_after"] == {"claude": {"weekly": 91.0}}
          and json.loads(config.log_path().read_text().splitlines()[-1])["quota_before"] == {"claude": {"weekly": 91.0}})
    blind = flows.Deps(runners={"claude": fake_runner([REVIEW_REPLY])}, repo=shas["repo"], quota_reader=lambda p: None,
                       quota_override="owner said so")
    ub = flows.review_once(blind, case, harness="claude", model="opus")
    check("under the override an unknown quota is still refused (the ceiling cannot be checked)",
          ub["result"] == "skipped" and "unknown" in ub["reason"] and blind.runners["claude"].calls == [])
    for used, runs in ((96.9, True), (97, False), (99.5, False)):
        cd = flows.Deps(runners={"claude": fake_runner([REVIEW_REPLY])}, repo=shas["repo"], quota_override="owner said so",
                        quota_reader=lambda p, used=used: {"windows": [{"name": "5h", "used_percent": 10},
                                                                       {"name": "weekly", "used_percent": used}]})
        cr = flows.review_once(cd, case, harness="claude", model="opus")
        check(f"under the override, weekly at {used}%: " + ("runs (below the 97% ceiling)" if runs else "refused at the ceiling"),
              (cr["result"] == "ok") if runs else (cr["result"] == "skipped" and "ceiling" in cr["reason"]
                                                   and cd.runners["claude"].calls == []), cr.get("reason", ""))
    with env(ROLEBENCH_QUOTA_CEILING="90"):
        c90 = flows.review_once(ovr, case, harness="claude", model="opus")
    with env(ROLEBENCH_QUOTA_CEILING="250"):
        cbad = flows.review_once(ovr, case, harness="claude", model="opus")
    check("ROLEBENCH_QUOTA_CEILING lowers the ceiling; a value outside (0, 100] fails closed",
          c90["result"] == "skipped" and "90% ceiling" in c90["reason"] and cbad["result"] == "skipped"
          and "fail closed" in cbad["reason"])
    # the ceiling is checked before every model step: a window that crosses it between the
    # up-front check and the step refuses the step
    climbing = iter([50, 50, 98, 98, 98, 98])
    cl_deps = flows.Deps(runners={"claude": fake_runner([REVIEW_REPLY])}, repo=shas["repo"], quota_override="owner said so",
                         quota_reader=lambda p: {"windows": [{"name": "weekly", "used_percent": next(climbing, 98)}]})
    clr = flows.review_once(cl_deps, case, harness="claude", model="opus")
    check("a window that reaches the ceiling before the model step refuses that step, under the override",
          clr["result"] == "skipped" and "ceiling" in clr["reason"] and cl_deps.runners["claude"].calls == [])
    # The up-front refusal keys on "is this the real runner". Stand-ins take the
    # real runners' places in REGISTRY, so the identity check sees "real" while
    # a regression (refusal removed) can only ever reach a harmless fake.
    reads, ran = [], []

    def standin(name):
        def run(**kw):
            ran.append(name)
            return runners.RunResult(name, kw.get("model", ""), text=REVIEW_REPLY)
        return run
    saved = dict(runners.REGISTRY)
    saved_claude = runners.run_claude
    saved_pasta = sandbox.PASTA
    try:
        for k in ("claude", "codex", "jarvis"):
            runners.REGISTRY[k] = standin(k)
        runners.run_claude = standin("judge")
        for net, why in (("none", "no network, so no API access"), ("bogus", "is not a mode"),
                         ("pasta-missing", "not found (install the passt package)")):
            if net == "pasta-missing":
                sandbox.PASTA = str(root / "no-such-pasta")
            with env(ROLEBENCH_NET="pasta" if net == "pasta-missing" else net):
                led = spend.Ledger(root / f"led-net-{net}.json", 10, 0.5)
                real = flows.Deps(runners=dict(runners.REGISTRY), repo=shas["repo"], ledger=led,
                                  quota_reader=lambda p: reads.append(p) or {"windows": [{"used_percent": 1}]})
                rc = flows.review_once(real, case, harness="claude", model="haiku")
                rj = flows.review_once(real, case, harness="jarvis", model="deepseek/deepseek-v4.1-flash")
                ri = flows.implement_once(real, impl_case(shas), harness="codex", model="gpt-6-luna", reviewer_harness="claude",
                                          reviewer_model="opus")
            sandbox.PASTA = saved_pasta
            check(f"ROLEBENCH_NET={net}: real claude/jarvis runs (and a claude reviewer) are refused up front",
                  all(r["result"] == "skipped" and why in r["reason"] for r in (rc, rj, ri)), rc.get("reason", ""))
            check(f"ROLEBENCH_NET={net}: refused before any quota read, ledger reservation or runner call",
                  reads == [] and ran == [] and not (root / f"led-net-{net}.json").exists())
        with env(ROLEBENCH_NET="none"):
            jr = raises(lambda: judge.judge_run(config.runs_root() / run["run_id"], case, force=True,
                                                quota_reader=lambda p: reads.append(p)), judge.JudgeError)
        check("the real judge is refused up front too, before its quota read", jr and reads == [] and ran == [])
        if not sandbox.pasta_available():
            for net in ("pasta", None):
                with env(ROLEBENCH_NET=net):
                    real = flows.Deps(runners=dict(runners.REGISTRY), repo=shas["repo"],
                                      ledger=spend.Ledger(root / f"led-net-ok-{net}.json", 10, 0.5),
                                      quota_reader=lambda p: {"windows": [{"used_percent": 1}]})
                    ok_c = flows.review_once(real, case, harness="claude", model="haiku")
                    ok_j = flows.review_once(real, case, harness="jarvis", model="deepseek/deepseek-v4.1-flash")
                check(f"ROLEBENCH_NET={net or 'unset'} (pasta): real claude and jarvis runs are no longer refused up front",
                      ok_c["result"] == "ok" and ok_j["result"] == "ok", ok_c.get("reason", ""))
    finally:
        runners.REGISTRY.clear()
        runners.REGISTRY.update(saved)
        runners.run_claude = saved_claude
        sandbox.PASTA = saved_pasta
    def cut_off(**kw):
        return runners.RunResult("jarvis", kw["model"], text="half a review\n\n" + jarvis_worker.CUT_OFF_MARK,
                                 cost_usd=0.01, turns=7, truncated=True)
    tr = flows.review_once(flows.Deps(runners={"jarvis": cut_off}, quota_override=NO_QUOTA, repo=shas["repo"],
                                      ledger=spend.Ledger(root / "led-trunc.json", 10, 0.5)),
                           case, harness="jarvis", model="anthropic/claude-haiku-5.5")
    trow = json.loads(config.log_path().read_text().splitlines()[-1])
    check("a review cut off at the output limit is 'truncated' in run.json and the log row, not scored as no findings",
          tr["result"] == "truncated" and tr["truncated"] is True and trow["truncated"] is True and trow["result"] == "truncated")
    check("the worker's cut-off mark is the one jarvis.agent appends",
          f'"\\n\\n{jarvis_worker.CUT_OFF_MARK}"' in (Path(config.REPO_ROOT) / "jarvis" / "agent.py").read_text())
    check("the worker marks a final answer that still ends at the limit, and only that",
          jarvis_worker.final_truncated("x\n\n[cut off at the token limit]") and not jarvis_worker.final_truncated("done"))
    bad = flows.Deps(runners={"claude": fake_runner(["no block at all"])}, quota_override=NO_QUOTA, repo=shas["repo"])
    check("a reply without a findings block is 'unparsed'", flows.review_once(bad, case, harness="claude", model="sonnet")["result"] == "unparsed")

    # a reviewer that commits a mutant: recorded, undone, never judged
    def commit_mutant(n, cwd):
        (cwd / "tiny.py").write_text("REVIEWER MUTANT\n")
        sh(cwd, "commit", "-qam", "mutant")
    cm = flows.Deps(runners={"claude": fake_runner([REVIEW_REPLY], edits=commit_mutant)}, quota_override=NO_QUOTA, repo=shas["repo"])
    rm = flows.review_once(cm, case, harness="claude", model="haiku")
    check("a reviewer's commit is recorded and undone: HEAD back at the change, mutant gone",
          any("HEAD moved" in p for p in rm["hygiene"]["after_run"]) and rm["hygiene"]["after_reset"] == []
          and "hi - 1" in (Path(rm["snapshot"]) / "tiny.py").read_text()
          and sh(Path(rm["snapshot"]), "rev-parse", "HEAD") == rm["shas"]["change"])

    # judging that run
    rdir = config.runs_root() / run["run_id"]
    items = json.loads((rdir / "findings.json").read_text())
    prompts = []
    copies = []

    def fake_judge(prompt, cwd):
        prompts.append(prompt)
        copies.append({"cwd": Path(cwd), "tiny": (Path(cwd) / "tiny.py").read_text(), "git": (Path(cwd) / ".git").exists()})
        if len(prompts) == 1:
            return "not json at all", 0.05
        # match whichever F-id carries the MED finding
        med = [f for f in re.findall(r"(F\d) \[(\w+)\]", prompt)]
        return json.dumps({"matches": [{"finding": fid, "verdict": "match", "gt": "gt1" if sev == "MED" else "gt2"}
                                       for fid, sev in med]}), 0.05
    doc = judge.judge_run(rdir, case, judge_fn=fake_judge, quota_override=NO_QUOTA, rng=random.Random(3))
    check("the judge is retried once on invalid JSON, then accepted", doc["attempts"] == 2 and doc["score"]["weighted_recall"] == 1.0)
    check("the judge prompt is blind: no model, harness or run id, but it has the key",
          all(w not in prompts[0] for w in ("haiku", "claude", "fake", run["run_id"])) and "clamp excludes the upper bound" in prompts[0])
    check("the judge reads a copy of the change in a neutral random directory, no .git, then it is removed",
          copies[0]["cwd"] != Path(run["snapshot"]) and len(copies[0]["cwd"].name) == 16
          and all(w not in str(copies[0]["cwd"]) for w in ("haiku", "claude", run["run_id"], "runs"))
          and "hi - 1" in copies[0]["tiny"] and not copies[0]["git"] and not copies[0]["cwd"].exists())
    ids = doc["finding_ids"]
    check("findings are shuffled for the judge and mapped back exactly",
          sorted(ids.values()) == list(range(len(items))) and ids != {f"F{i + 1}": i for i in range(len(items))}
          and all(m["gt"] == ("gt1" if items[ids[m["finding"]]]["severity"] == "MED" else "gt2") for m in doc["matches"]))
    check("the score is folded into run.json", json.loads((rdir / "run.json").read_text())["score"]["medplus_recall"] == 1.0)
    again = judge.judge_run(rdir, case, judge_fn=lambda p, c: (_ for _ in ()).throw(AssertionError("called")), quota_override=NO_QUOTA)
    check("re-judging a judged run is idempotent (no judge call, same verdicts)", again.get("reused") and again["matches"] == doc["matches"])
    vn_case = copy.deepcopy(case)
    vn_reply = lambda p, c: (json.dumps({"matches": [{"finding": fid, "verdict": "valid-new", "severity": "LOW"}
                                                     for fid in re.findall(r"(F\d) \[", p)]}), 0.0)
    judge.judge_run(rdir, vn_case, judge_fn=vn_reply, quota_override=NO_QUOTA, force=True)
    judge.judge_run(rdir, vn_case, judge_fn=vn_reply, quota_override=NO_QUOTA, force=True)
    vn = [json.loads(line) for line in (config.home() / "valid_new.jsonl").read_text().splitlines()]
    check("forced re-judging rewrites the run's valid-new rows instead of appending duplicates",
          sum(1 for r in vn if r["run"] == run["run_id"]) == len(items))
    judge.judge_run(rdir, case, judge_fn=lambda p, c: (json.dumps({"matches": [
        {"finding": fid, "verdict": "match", "gt": "gt1" if sev == "MED" else "gt2"}
        for fid, sev in re.findall(r"(F\d) \[(\w+)\]", p)]}), 0.0), quota_override=NO_QUOTA, force=True)
    hot_judge = raises(lambda: judge.judge_run(config.runs_root() / rm["run_id"], case, judge_fn=fake_judge,
                                               quota_reader=lambda p: None), judge.JudgeError)
    check("the judge fails closed on unknown quota", hot_judge)
    jcalls = []

    def counting_judge(prompt, cwd):
        jcalls.append(1)
        return "not json", 0.0
    at_ceiling = raises(lambda: judge.judge_run(config.runs_root() / rm["run_id"], case, judge_fn=counting_judge, force=True,
                                                quota_override="owner said so",
                                                quota_reader=lambda p: {"windows": [{"name": "weekly", "used_percent": 97}]}),
                        judge.JudgeError)
    check("under the override, a judge call at the ceiling is refused before it is made", at_ceiling and jcalls == [])
    rising = iter([50, 50, 98])
    retry_refused = raises(lambda: judge.judge_run(config.runs_root() / rm["run_id"], case, judge_fn=counting_judge, force=True,
                                                   quota_override="owner said so",
                                                   quota_reader=lambda p: {"windows": [{"name": "weekly",
                                                                                        "used_percent": next(rising, 98)}]}),
                           judge.JudgeError)
    check("the ceiling is checked again before the judge's retry", retry_refused and jcalls == [1])
    jdoc = json.loads((rdir / "judge.json").read_text())
    check("judge.json records the quota before and after", jdoc["quota_before"] == {"claude": {"5h": 10.0, "weekly": 20.0}}
          and jdoc["quota_after"] == {"claude": {"5h": 10.0, "weekly": 20.0}})
    always_bad = lambda prompt, cwd: ("{}", 0.0)
    rd2 = config.runs_root() / uo["run_id"]
    check("a judge that is invalid twice fails the run, not the bench", raises(lambda: judge.judge_run(rd2, case, judge_fn=always_bad, quota_override=NO_QUOTA), judge.JudgeError)
          and json.loads((rd2 / "judge.json").read_text())["valid"] is False)
    n, sp_path = judge.spotcheck([rdir])
    sp = sp_path.read_text()
    key_files = list((config.home() / "spotcheck-keys").glob("*.json"))
    check("spotcheck.md samples at least one finding and names no model", n == 1 and "haiku" not in sp.lower() and "agree" in sp)
    check("the spot-check sample and its key live under neutral paths, apart, never in a run directory",
          key_files and all("runs" not in str(p) and "haiku" not in str(p) for p in key_files + [sp_path])
          and key_files[0].parent != sp_path.parent)
    big = []
    for i in range(10):
        d = root / f"sc{i}"
        d.mkdir()
        (d / "findings.json").write_text(json.dumps([{"severity": "LOW", "file": "a", "line": None, "title": "t", "scenario": "s"}] * 5))
        (d / "run.json").write_text(json.dumps({"case_id": "x"}))
        (d / "judge.json").write_text(json.dumps({"valid": True, "matches": [{"finding": f"F{k}", "verdict": "invalid"} for k in range(1, 6)]}))
        big.append(d)
    check("the spot-check sample is 20% of the matches", judge.spotcheck(big, root / "sc.md", root / "sck.json")[0] == 10)

    rows = report.review_rows(report.load_runs(config.runs_root()))
    text = report.render(report.load_runs(config.runs_root()))
    check("the report has a row per (harness, model), splits tokens and lists skipped cells",
          "| claude | haiku |" in text and "## Skipped cells" in text and "weekly at 91%" in text and len(rows) >= 3
          and "cached tok" in text)
    check("the report lists cells run with the quota guard off", "quota guard OFF" in text and "owner said so" in text)
    check("the report lists snapshots a reviewer changed", "Snapshots an agent changed" in text)


def test_implement_flow(shas, root: Path) -> None:
    icase = impl_case(shas)
    seen = []
    impl_replies = ["did it", "fixed"]

    def edits(n, cwd):
        if n == 1:
            (cwd / "tiny.py").write_text("def clamp(x, lo, hi):\n    return max(lo, min(x, hi - 1))\n")
            (cwd / "extra.py").write_text("x = 1\n")            # out of scope
            (cwd / "tiny_regress_check.py").write_text("pass\n")     # a weakened suite
            sh(cwd, "add", "-A")
            sh(cwd, "commit", "-qm", "implementer commits its change")
        else:
            (cwd / "tiny.py").write_text("def clamp(x, lo, hi):\n    return max(lo, min(x, hi))\n")
            (cwd / "extra.py").unlink()
            (cwd / "tests").mkdir(exist_ok=True)
            (cwd / "tests" / "tiny_new_check.py").write_text("pass\n")      # a test file: allowed

    calls_impl = [("Bash", '{"command": "curl https://example.invalid/x"}'), ("Bash", '{"command": "cat ~/.ssh/id_rsa"}')]
    implementer = fake_runner(impl_replies, seen=seen, cost=0.2, edits=edits, tool_calls=calls_impl)
    med = '```json\n{"findings": [{"severity": "MED", "file": "tiny.py", "line": 2, "title": "bound excluded", "scenario": "s"}]}\n```'
    clean = '```json\n{"findings": [{"severity": "LOW", "file": "tiny.py", "line": 1, "title": "nit-ish", "scenario": "s"}]}\n```'
    reviewer = fake_runner([med, clean], cost=0.05)
    graded_regress = []

    def suites(grade_dir, rel):
        if rel == "tiny_hidden_check.py":
            return ("hi))" in (grade_dir / "tiny.py").read_text()), "tail"
        graded_regress.append((grade_dir / rel).read_text())
        return True, ""
    deps = flows.Deps(runners={"claude": implementer, "codex": reviewer}, quota_override=NO_QUOTA, repo=shas["repo"],
                      suite_runner=suites, ledger=spend.Ledger(root / "led-i.json"))
    run = flows.implement_once(deps, icase, harness="claude", model="sonnet", reviewer_harness="codex", reviewer_model="gpt-6.1-sol")
    check("round 0 fails hidden tests and review; round 1 is merge-ready",
          [r["merge_ready"] for r in run["rounds"]] == [False, True] and run["merged_ready"] and run["rounds_to_merge_ready"] == 1)
    check("cost to merge-ready includes both implementer and reviewer rounds", abs(run["cost_to_merge_ready"] - (0.2 * 2 + 0.05 * 2)) < 1e-9)
    check("hidden tests were never visible to the implementer", all(s["hidden_present"] in (None, "print('old version')\n") for s in seen))
    check("hidden tests are not left in the implementer's tree", not (Path(run["snapshot"]) / "tiny_hidden_check.py").exists()
          or (Path(run["snapshot"]) / "tiny_hidden_check.py").read_text() != "print('hidden')\n")
    r0 = run["rounds"][0]
    check("an implementer's commit is folded and its diff still seen against the base",
          r0["commits_folded"] == 1 and "tiny.py" in r0["diff"]["files"] and r0["hygiene"] == [])
    check("the file-scope audit names the file outside the brief", r0["diff"]["out_of_scope"] == ["extra.py", "tiny_regress_check.py"]
          or r0["diff"]["out_of_scope"] == ["extra.py"], json.dumps(r0["diff"]))
    check("a weakened regression suite is graded from the base", graded_regress and all("pass\n" != t for t in graded_regress))
    check("a new test file is allowed and listed, not out of scope",
          run["rounds"][1]["diff"]["out_of_scope"] == [] and "tests/tiny_new_check.py" in run["rounds"][1]["diff"]["test_files"])
    check("the implementer's tool calls are audited in every round (network and escape)",
          all(r["implementer"]["network_flags"] and r["implementer"]["escape_flags"] for r in run["rounds"])
          and any(f.startswith("implementer r1:") for f in run["network_flags"])
          and any(f.startswith("implementer r0:") for f in run["escape_flags"]))
    check("the fix prompt carries the reviewer's finding", "bound excluded" in implementer.calls[1]["prompt"] and "Review round 1" in implementer.calls[1]["prompt"])
    check("the reviewer sees the change as a clean commit in a random-named copy, then the copy is gone",
          len(reviewer.calls[0]["cwd"].name) == 16 and not reviewer.calls[0]["cwd"].exists())
    row = runlog.row(run)
    check("the implement log row is the whole run's cost, not the last round's", abs(row["cost_usd"] - run["total_cost_usd"]) < 1e-9
          and abs(row["cost_usd"] - 0.5) < 1e-9 and row["tool_calls"] == 8)

    # out-of-scope blocks merge-ready even when everything else passes
    def oos(n, cwd):
        (cwd / "tiny.py").write_text("def clamp(x, lo, hi):\n    return max(lo, min(x, hi))\n")
        (cwd / "jarvis_extra.py").write_text("x = 1\n")
    o = flows.implement_once(flows.Deps(runners={"claude": fake_runner(["x"], edits=oos), "codex": fake_runner([clean])},
                                        quota_override=NO_QUOTA, repo=shas["repo"], suite_runner=lambda g, r: (True, "")),
                             icase, harness="claude", model="haiku", reviewer_harness="codex", reviewer_model="gpt-6-luna", max_rounds=0)
    check("merge-ready respects out-of-scope edits", not o["merged_ready"] and o["rounds"][0]["diff"]["out_of_scope"] == ["jarvis_extra.py"])

    # the quota is checked again before every round
    reads = []

    def warming(p):
        reads.append(p)
        # two reads for quota_before (claude, codex), then the up-front guards and round 0's implementer
        return {"windows": [{"name": "5h", "used_percent": 10 if len(reads) <= 5 else 90}]}
    q = flows.implement_once(flows.Deps(runners={"claude": fake_runner(["x"], edits=lambda n, c: None), "codex": fake_runner([med])},
                                        repo=shas["repo"], quota_reader=warming, suite_runner=lambda g, r: (False, "f")),
                             icase, harness="claude", model="haiku", reviewer_harness="codex", reviewer_model="gpt-6-luna")
    check("the quota is re-checked per implement round: a window that heats up stops the run",
          q["result"] == "partial" and "quota" in q["reason"] and len(q["rounds"]) < 3)
    check("an implement run records quota before and after for both roles' providers",
          q["quota_before"] == {"claude": {"5h": 10.0}, "codex": {"5h": 10.0}}
          and q["quota_after"] == {"claude": {"5h": 90.0}, "codex": {"5h": 90.0}})
    ceil_reads = []

    def to_ceiling(p):
        ceil_reads.append(p)
        return {"windows": [{"name": "weekly", "used_percent": 80 if len(ceil_reads) <= 5 else 97}]}
    qc = flows.implement_once(flows.Deps(runners={"claude": fake_runner(["x"], edits=lambda n, c: None), "codex": fake_runner([med])},
                                         repo=shas["repo"], quota_reader=to_ceiling, quota_override="owner said so",
                                         suite_runner=lambda g, r: (False, "f")),
                              icase, harness="claude", model="haiku", reviewer_harness="codex", reviewer_model="gpt-6-luna")
    check("under the override, a window reaching the ceiling mid-run refuses the next model step",
          qc["result"] == "partial" and "ceiling" in qc["reason"] and len(qc["rounds"]) == 1
          and qc["rounds"][0]["reviewer"] is None)

    flaky_calls = []

    def flaky(grade_dir, rel):
        flaky_calls.append(rel)
        return len(flaky_calls) % 2 == 0, "flaked"          # fails first, passes on the retry
    g = flows.grade(flows.Deps(repo=shas["repo"], suite_runner=flaky), Path(run["snapshot"]), icase)
    check("a failure that does not reproduce is not counted (one retry)", g["hidden_pass"] and len(flaky_calls) == 4)
    g2 = flows.grade(flows.Deps(repo=shas["repo"], suite_runner=lambda d, r: (False, "boom")), Path(run["snapshot"]), icase)
    check("a failure that reproduces is counted, with its tail", not g2["hidden_pass"] and "boom" in g2["tails"]["tiny_hidden_check.py"])
    never = flows.Deps(runners={"claude": fake_runner(["x"], edits=lambda n, c: None), "codex": fake_runner([med])}, quota_override=NO_QUOTA,
                       repo=shas["repo"], suite_runner=lambda g, r: (False, "fail"))
    r2 = flows.implement_once(never, icase, harness="claude", model="haiku", reviewer_harness="codex", reviewer_model="gpt-6-luna")
    check("after two fix rounds a failing model is not merge-ready", len(r2["rounds"]) == 3 and not r2["merged_ready"] and r2["rounds_to_merge_ready"] is None)
    check("implement rows reach the report", "## Implementer bench" in report.render(report.load_runs(config.runs_root())) and "| claude | sonnet |" in report.render(report.load_runs(config.runs_root())))

    if not sandbox.available():
        ok, tail = flows.default_suite_runner(Path(run["snapshot"]), "tiny_regress_check.py")
        check("a suite runs in a real no-network grade cell", ok, tail[-300:])
        bad_dir = root / "w-grade-net"
        bad_dir.mkdir()
        (bad_dir / "net_check.py").write_text(
            "import socket, sys\ntry:\n    socket.create_connection(('1.1.1.1', 53), timeout=2)\n    sys.exit(0)\nexcept OSError:\n    sys.exit(1)\n")
        ok2, _ = flows.default_suite_runner(bad_dir, "net_check.py")
        check("graded code cannot open a network connection", ok2 is False)


def test_cli_and_cells(shas, root: Path) -> None:
    live, peak, lock = [0], [0], threading.Lock()

    def cell():
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.05)
        with lock:
            live[0] -= 1
        return {}
    flows.run_cells([cell] * 9, workers=50)
    check("never more than 3 runs at once", peak[0] == 3, f"peak {peak[0]}")
    check("the shuffle is seeded", flows.shuffled(list(range(20)), 1) == flows.shuffled(list(range(20)), 1) != list(range(20)))

    cdir = root / "cases"
    (cdir / "fixtures").mkdir(parents=True)
    (cdir / "fixtures" / "timeline.json").write_text('{"synthetic": true}')
    with env(ROLEBENCH_CASES=str(cdir)):
        c = impl_case(shas)
        c["fixtures"] = ["rolebench/cases/fixtures/timeline.json"]
        snap = snapshot.build(c, "fx", repo=shas["repo"])
        check("a fixture is copied into the snapshot at its own path, and the snapshot is still clean",
              (snap / "rolebench/cases/fixtures/timeline.json").read_text() == '{"synthetic": true}' and snapshot.check(snap, c) == [])
        (snap / "rolebench" / "cases" / "answers.json").write_text("{}")
        check("only the named fixtures may sit under rolebench/", snapshot.check(snap, c) != [])
        c2 = impl_case(shas)
        c2["fixtures"] = ["rolebench/cases/fixtures/missing.json"]
        check("a missing fixture is an error", raises(lambda: snapshot.build(c2, "fx2", repo=shas["repo"]), snapshot.SnapshotError))
    p = brief.fix_prompt(impl_case(shas), "/s", 1, "[MED] a:1 - t\n   s")
    texts = [brief.review_prompt(review_case(shas), "/s"), brief.implement_prompt(impl_case(shas), "/s"), p]
    check("brief templates fill every placeholder",
          not any("{" + k + "}" in t for t in texts for k in ("snap", "python", "brief", "focus", "suites", "scope", "fixtures",
                                                               "implement", "round", "findings", "environment")))
    main = __import__("rolebench.__main__", fromlist=["main"]).main
    with contextlib.redirect_stdout(io.StringIO()):
        check("the CLI parses", main(["report", "--out", str(root / "r.md")]) == 0)
    cli_run = root / "cli-run"
    cli_run.mkdir()
    (cli_run / "run.json").write_text(json.dumps({"kind": "implement", "result": "ok"}))
    with env(ROLEBENCH_QUOTA_OVERRIDE=None):
        refused = raises(lambda: main(["--no-quota-guard", "judge", "--run", str(root)]), SystemExit)
    check("--no-quota-guard without ROLEBENCH_QUOTA_OVERRIDE is refused", refused)
    err = io.StringIO()
    with env(ROLEBENCH_QUOTA_OVERRIDE="owner OK in test"), contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        main(["--no-quota-guard", "judge", "--run", str(cli_run)])
    check("--no-quota-guard with a reason prints a loud warning", "quota guard is OFF" in err.getvalue() and "owner OK in test" in err.getvalue())
    # --off-matrix through the CLI itself: refused without it, run and logged with it.
    # Stand-in runners take the real ones' places (never a real CLI, network or model).
    ccase = review_case(shas)
    mdir = root / "cases-matrix"
    mdir.mkdir()
    (mdir / f"{ccase['id']}.json").write_text(json.dumps(ccase))
    saved_reg = dict(runners.REGISTRY)
    seen_models = []

    seen_caps = []

    def standin(**kw):
        seen_models.append(kw.get("model"))
        seen_caps.append((kw.get("timeout_s"), kw.get("max_turns")))
        return runners.RunResult("jarvis", kw.get("model", ""), text=REVIEW_REPLY, cost_usd=0.001)
    off_argv = ["review", "--harness", "jarvis", "--model", "openai/gpt-6-luna", "--case", ccase["id"]]
    merr = io.StringIO()
    try:
        runners.REGISTRY["jarvis"] = standin
        with env(ROLEBENCH_CASES=str(mdir), ROLEBENCH_REPO=str(shas["repo"]), ROLEBENCH_NET="pasta"), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(merr):
            main(off_argv)
            refused_row = json.loads(config.log_path().read_text().splitlines()[-1])
            calls_without = len(seen_models)
            main(["--off-matrix", *off_argv, "--timeout", "77", "--max-turns", "9"])
            allowed_row = json.loads(config.log_path().read_text().splitlines()[-1])
    finally:
        runners.REGISTRY.clear()
        runners.REGISTRY.update(saved_reg)
    check("CLI: an off-matrix model is refused without --off-matrix (no runner call, off_matrix false in the row)",
          refused_row["model"] == "openai/gpt-6-luna" and refused_row["result"] == "skipped"
          and "not in the O-1 matrix" in refused_row["reason"] and refused_row["off_matrix"] is False
          and calls_without == 0)
    check("CLI: --off-matrix parses, warns, runs it, and the run row records off_matrix: true",
          allowed_row["model"] == "openai/gpt-6-luna" and allowed_row["result"] == "ok" and allowed_row["off_matrix"] is True
          and seen_models == ["openai/gpt-6-luna"] and "--off-matrix" in merr.getvalue())
    check("CLI: --timeout and --max-turns reach the runner (smoke caps)", seen_caps == [(77, 9)])
    help_text = __import__("rolebench.__main__", fromlist=["main"]).__doc__
    check("the docs say an agent never passes the override without the owner's OK", "never passes it" in help_text)

    # clean --judged removes judged snapshots only
    runs = report.load_runs(config.runs_root())
    judged = [r for r in runs if r.get("judged") and r.get("snapshot")]
    unjudged = [r for r in runs if r.get("kind") == "review" and not r.get("judged") and r.get("snapshot")]
    with contextlib.redirect_stdout(io.StringIO()):
        main(["clean", "--judged"])
    check("clean --judged removes the snapshots of judged runs, and keeps the rest",
          judged and all(not Path(r["snapshot"]).exists() for r in judged)
          and all(Path(r["snapshot"]).exists() for r in unjudged))


def main() -> int:
    print("role bench checks (free, no API)")
    with tempfile.TemporaryDirectory(prefix="rolebench-test-") as tmp:
        root = Path(tmp)
        os.environ["ROLEBENCH_HOME"] = str(root / "home")
        shas = make_repo(root)
        test_schema(shas)
        test_snapshot(shas, root)
        test_spend(root)
        test_worker_meter(root)
        test_prices(root)
        test_worker_env(root)
        test_parsers(root)
        test_sandbox_argv(root)
        test_sandbox_escape(root)
        test_pasta_network(root)
        test_netcell_fail_closed(root)
        test_unit_kill(root)
        test_fake_clis(root)
        test_codex_loopback(root)
        test_jarvis_in_cell(root)
        test_review_flow(shas, root)
        test_implement_flow(shas, root)
        test_cli_and_cells(shas, root)
    test_env_and_quota()
    test_judge_and_report()
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  - {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
