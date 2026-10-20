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

import os
import threading
import time

from rolebench import audit, brief, cases, config, findings, flows, judge, quota, report, runlog, runners, snapshot, spend

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


# --- parsers, flags, runners ----------------------------------------------------------

BLOCK = '```json\n{"findings": [{"severity": "MED", "file": "tiny.py", "line": 2, "title": "bound excluded", "scenario": "clamp(5,0,5) is 4"}, {"severity": "WAT", "title": "x"}, {"severity": "LOW", "title": "stale doc", "scenario": "s"}], "ran_tests": true, "tried_mutant": true}\n```'


def test_parsers() -> None:
    p = findings.parse("prose\n```json\n{\"findings\": []}\n```\nmore\n" + BLOCK)
    check("the last findings block wins", len(p.findings) == 2 and p.found_block)
    check("a finding with a bad severity is dropped and reported", any("bad severity" in e for e in p.errors))
    check("self-reported flags are read", p.ran_tests is True and p.tried_mutant is True)
    check("no block is an error, not a crash", findings.parse("nothing here").found_block is False
          and findings.parse("```json\n{not json}\n```").errors == ["no findings block"])
    check("an empty findings list is a valid review", findings.parse('```json\n{"findings": []}\n```').found_block)
    calls_ok = [("Bash", '{"command": "python tests/foo_check.py"}'), ("Edit", "{}")]
    check("tests and a mutant are observed", findings.flags(calls_ok, p) == {"ran_tests": True, "tried_mutant": True, "observed": True})
    calls_no_edit = [("Bash", '{"command": "python tests/foo_check.py"}'), ("Read", "{}")]
    perl = [("shell", "/bin/bash -lc \"perl -0pi -e 's/a/b/' x.py\""), ("shell", "python tests/foo_check.py")]
    check("a perl/sed in-place edit counts as an edit", findings.flags(perl, p)["tried_mutant"] is True)
    check("a claimed mutant without any edit is not believed", findings.flags(calls_no_edit, p)["tried_mutant"] is False)
    check("no tool calls falls back to the self-report", findings.flags([], p)["ran_tests"] is True)
    check("no tests run is observed", findings.flags([("Read", "{}")], p)["ran_tests"] is False)

    stream = "\n".join(json.dumps(m) for m in [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "final " + BLOCK, "total_cost_usd": 0.0123,
         "num_turns": 7, "usage": {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 300}}])
    r = runners.parse_claude_stream(stream, "haiku")
    check("claude stream: cost, turns, tokens, tools, full text",
          r.cost_usd == 0.0123 and r.turns == 7 and r.tokens["cache_read_input_tokens"] == 300
          and r.tool_calls[0][0] == "Bash" and r.text.startswith("final"))
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
    check("claude flags: stream-json, strict MCP, project-only settings, no web, no gh",
          "stream-json" in argv and "--strict-mcp-config" in argv and "--setting-sources" in argv
          and argv[argv.index("--setting-sources") + 1] == "project,local"
          and {"WebFetch", "WebSearch", "Bash(gh:*)", "Bash(git push:*)"} <= set(argv))
    ro = runners.claude_argv("opus", 30, read_only=True)
    check("the judge's claude is read-only", "--tools" in ro and ro[ro.index("--tools") + 1] == "Read,Grep,Glob" and "bypassPermissions" not in ro)
    cx = runners.codex_argv("gpt-6-luna", "/s", Path("/t"))
    check("codex flags: workspace-write, network off, json",
          "workspace-write" in cx and "sandbox_workspace_write.network_access=false" in cx and "--json" in cx)


def test_process_control(root: Path) -> None:
    t = time.time()
    rc, out, err, timed_out = runners.run_scoped(["sh", "-c", "sleep 30 & echo $! > %s; wait" % (root / "pid")],
                                                 cwd=root, env=runners.scrubbed_env(), timeout_s=1, wrap=None)
    pid = int((root / "pid").read_text())
    time.sleep(0.2)
    alive = Path(f"/proc/{pid}").exists() and "sleep" in Path(f"/proc/{pid}/cmdline").read_text().replace("\0", " ")
    check("a timeout is reported quickly and kills the whole process group", timed_out and time.time() - t < 10 and not alive)
    fake = root / "bin"
    fake.mkdir()
    (fake / "claude").write_text(
        "#!/bin/sh\nenv > %s/env.txt\ncat > %s/stdin.txt\n"
        "echo '{\"type\":\"result\",\"is_error\":false,\"result\":\"ok\",\"total_cost_usd\":0.5,\"num_turns\":1,\"usage\":{}}'\n"
        % (root, root))
    (fake / "claude").chmod(0o755)
    old = dict(os.environ)
    os.environ.update(PATH=f"{fake}:{os.environ['PATH']}", GH_TOKEN="gh-secret", GITHUB_TOKEN="g2", OPENROUTER_API_KEY="or-secret")
    try:
        res = runners.run_claude(model="haiku", prompt="PROMPT TEXT", cwd=root, timeout_s=20, max_turns=3, wrap=None)
    finally:
        os.environ.clear(); os.environ.update(old)
    env_seen = (root / "env.txt").read_text()
    check("a real subprocess never sees the scrubbed tokens",
          "gh-secret" not in env_seen and "or-secret" not in env_seen and "GITHUB_TOKEN" not in env_seen and "PATH=" in env_seen)
    check("the prompt arrives on stdin and the result is parsed", (root / "stdin.txt").read_text() == "PROMPT TEXT" and res.text == "ok" and res.cost_usd == 0.5)
    check("audit: network-shaped and out-of-snapshot tool calls are flagged",
          audit.network_flags(runners.RunResult("c", "m", tool_calls=[("Bash", "curl https://x")]))
          and audit.escape_flags(runners.RunResult("c", "m", tool_calls=[("Bash", "cat /home/johnw/projects/Jarvis/CLAUDE.md"), ("Bash", f"cat {root}/a")]), root)
          == ["Bash: /home/johnw/projects/Jarvis/CLAUDE.md"]
          and audit.escape_flags(runners.RunResult("c", "m", tool_calls=[("Bash", config.python_bin() + " x.py")]), root) == [])


# --- the flows, against fakes ---------------------------------------------------------

REVIEW_REPLY = "I ran the suite.\n" + BLOCK


def fake_runner(replies, seen=None, cost=0.01, tokens=None, edits=None):
    """A runner that returns scripted replies and records what the model could see."""
    calls = []

    def run(*, model, prompt, cwd, timeout_s, max_turns, budget_usd=None, out_dir=None):
        calls.append({"prompt": prompt, "cwd": Path(cwd), "budget": budget_usd})
        if seen is not None:
            seen.append({"hidden_present": (Path(cwd) / "tiny_hidden_check.py").read_text() if (Path(cwd) / "tiny_hidden_check.py").exists() else None})
        if edits:
            edits(len(calls), Path(cwd))
        text = replies[min(len(calls), len(replies)) - 1]
        return runners.RunResult("fake", model, text=text, cost_usd=cost, tokens=dict(tokens or {"input_tokens": 5}),
                                 turns=3, tool_calls=[("Bash", '{"command": "python tests/tiny_check.py"}'), ("Edit", "{}")],
                                 duration_s=1.0)
    run.calls = calls
    return run


def test_review_flow(shas, root: Path) -> None:
    case = review_case(shas)
    deps = flows.Deps(runners={"claude": fake_runner([REVIEW_REPLY]), "jarvis": fake_runner([REVIEW_REPLY], cost=0.3)},
                      use_quota=False, repo=shas["repo"], ledger=spend.Ledger(root / "led.json", 1.0, 0.5))
    run = flows.review_once(deps, case, harness="claude", model="haiku")
    rd = config.runs_root() / run["run_id"]
    check("a review run is ok and records its findings", run["result"] == "ok" and run["n_findings"] == 2)
    check("the run directory holds prompt, review text, findings and run.json",
          all((rd / f).exists() for f in ("prompt.txt", "review.txt", "findings.json", "run.json")))
    prompt = (rd / "prompt.txt").read_text()
    check("the brief carries the case brief, focus and suites but not the ground truth",
          "Adds a clamp helper." in prompt and "off-by-one" in prompt and "tests/tiny_check.py" in prompt
          and "clamp excludes the upper bound" not in prompt and "mutant of your own" in prompt)
    check("flags come from the tool calls", run["flags"] == {"ran_tests": True, "tried_mutant": True, "observed": True})
    check("the log row has the skill's fields",
          json.loads(config.log_path().read_text().splitlines()[-1]).keys() >= {"case_id", "run_id", "model", "harness", "roles", "wall_s", "result", "rounds", "merged_ready", "task_class", "tool_calls"})
    check("the snapshot is reset to the PR commit after the run", sh(Path(run["snapshot"]), "status", "--short") == "")
    check("the claude cell had no budget imposed", deps.runners["claude"].calls[0]["budget"] is None)

    j = flows.review_once(deps, case, harness="jarvis", model="moonshotai/kimi-k3", n=2)
    check("a jarvis cell is given a budget from the ledger", deps.runners["jarvis"].calls[0]["budget"] == 0.5)
    check("a jarvis run is charged to the ledger", abs(deps.get_ledger().spent() - 0.3) < 1e-9)
    j2 = flows.review_once(deps, case, harness="jarvis", model="moonshotai/kimi-k3", n=3)
    j3 = flows.review_once(deps, case, harness="jarvis", model="other/model", n=4)
    check("the budget is whatever the total cap leaves", deps.runners["jarvis"].calls[1]["budget"] == 0.5 and j3["result"] in ("ok", "skipped"))
    over = fake_runner([REVIEW_REPLY], cost=0.6)
    deps2 = flows.Deps(runners={"jarvis": over}, use_quota=False, repo=shas["repo"], ledger=spend.Ledger(root / "led2.json", 10, 0.5))
    o1 = flows.review_once(deps2, case, harness="jarvis", model="m/over", n=1)
    o2 = flows.review_once(deps2, case, harness="jarvis", model="m/over", n=2)
    check("a run over the per-run cap is marked, and its model is aborted", o1["result"] == "over-cap" and o2["result"] == "skipped" and "per-run cap" in o2["reason"])
    deps3 = flows.Deps(runners={"jarvis": fake_runner([REVIEW_REPLY], cost=0.0, tokens={"prompt_tokens": 1_000_000, "completion_tokens": 0})},
                       use_quota=False, repo=shas["repo"], ledger=spend.Ledger(root / "led3.json", 10, 100))
    flows.review_once(deps3, case, harness="jarvis", model="moonshotai/kimi-k3", n=1)
    check("Kimi is charged at $3 per M input even when the reported cost is 0", abs(deps3.get_ledger().spent() - 3.0) < 1e-9)

    hot = flows.Deps(runners={"claude": fake_runner([REVIEW_REPLY])}, use_quota=True, repo=shas["repo"],
                     quota_reader=lambda p: {"windows": [{"name": "weekly", "used_percent": 91}]})
    s = flows.review_once(hot, case, harness="claude", model="opus")
    check("a quota-hot cell is skipped, logged, and the model never ran",
          s["result"] == "skipped" and "weekly at 91%" in s["reason"] and hot.runners["claude"].calls == []
          and json.loads(config.log_path().read_text().splitlines()[-1])["result"] == "skipped")
    unk = flows.Deps(runners={"claude": fake_runner([REVIEW_REPLY])}, use_quota=True, repo=shas["repo"], quota_reader=lambda p: None)
    u = flows.review_once(unk, case, harness="claude", model="opus")
    check("an unknown quota proceeds with a warning in the record", u["result"] == "ok" and any("unknown" in n for n in u["quota_notes"]))
    bad = flows.Deps(runners={"claude": fake_runner(["no block at all"])}, use_quota=False, repo=shas["repo"])
    check("a reply without a findings block is 'unparsed'", flows.review_once(bad, case, harness="claude", model="x")["result"] == "unparsed")

    # judging that run
    rdir = config.runs_root() / run["run_id"]
    replies = iter(["not json at all", json.dumps({"matches": [{"finding": "F1", "verdict": "match", "gt": "gt1", "note": "same"},
                                                                {"finding": "F2", "verdict": "match", "gt": "gt2"}]})])
    prompts = []

    def fake_judge(prompt, cwd):
        prompts.append((prompt, cwd))
        return next(replies), 0.05
    doc = judge.judge_run(rdir, case, judge_fn=fake_judge, use_quota=False)
    check("the judge is retried once on invalid JSON, then accepted", doc["attempts"] == 2 and doc["score"]["weighted_recall"] == 1.0)
    check("the judge prompt is blind: no model, harness or run id, but it has the key",
          all(w not in prompts[0][0] for w in ("haiku", "claude", "fake", run["run_id"])) and "clamp excludes the upper bound" in prompts[0][0])
    check("the judge reads the pristine snapshot", prompts[0][1] == Path(run["snapshot"]))
    check("the score is folded into run.json", json.loads((rdir / "run.json").read_text())["score"]["medplus_recall"] == 1.0)
    always_bad = lambda prompt, cwd: ("{}", 0.0)
    rd2 = config.runs_root() / u["run_id"]
    check("a judge that is invalid twice fails the run, not the bench", raises(lambda: judge.judge_run(rd2, case, judge_fn=always_bad, use_quota=False), judge.JudgeError)
          and json.loads((rd2 / "judge.json").read_text())["valid"] is False)
    n = judge.spotcheck([rdir], rdir / "spotcheck.md")
    sp = (rdir / "spotcheck.md").read_text()
    check("spotcheck.md samples at least one finding and names no model", n == 1 and "haiku" not in sp.lower() and "agree" in sp)
    big = []
    for i in range(10):
        d = root / f"sc{i}"
        d.mkdir()
        (d / "findings.json").write_text(json.dumps([{"severity": "LOW", "file": "a", "line": None, "title": "t", "scenario": "s"}] * 5))
        (d / "run.json").write_text(json.dumps({"case_id": "x"}))
        (d / "judge.json").write_text(json.dumps({"valid": True, "matches": [{"finding": f"F{k}", "verdict": "invalid"} for k in range(1, 6)]}))
        big.append(d)
    check("the spot-check sample is 20% of the matches", judge.spotcheck(big, root / "sc.md") == 10)

    rows = report.review_rows(report.load_runs(config.runs_root()))
    text = report.render(report.load_runs(config.runs_root()))
    check("the report has a row per (harness, model) and lists skipped cells",
          "| claude | haiku |" in text and "## Skipped cells" in text and "weekly at 91%" in text and len(rows) >= 3)


def test_implement_flow(shas, root: Path) -> None:
    icase = impl_case(shas)
    seen = []
    impl_replies = ["did it", "fixed"]

    def edits(n, cwd):
        if n == 1:
            (cwd / "tiny.py").write_text("def clamp(x, lo, hi):\n    return max(lo, min(x, hi - 1))\n")
            (cwd / "extra.py").write_text("x = 1\n")            # out of scope
        else:
            (cwd / "tiny.py").write_text("def clamp(x, lo, hi):\n    return max(lo, min(x, hi))\n")

    implementer = fake_runner(impl_replies, seen=seen, cost=0.2, edits=edits)
    med = '```json\n{"findings": [{"severity": "MED", "file": "tiny.py", "line": 2, "title": "bound excluded", "scenario": "s"}]}\n```'
    clean = '```json\n{"findings": [{"severity": "LOW", "file": "tiny.py", "line": 1, "title": "nit-ish", "scenario": "s"}]}\n```'
    reviewer = fake_runner([med, clean], cost=0.05)

    def suites(grade_dir, rel):
        if rel == "tiny_hidden_check.py":
            return ("hi))" in (grade_dir / "tiny.py").read_text()), "tail"
        return True, ""
    deps = flows.Deps(runners={"claude": implementer, "codex": reviewer}, use_quota=False, repo=shas["repo"],
                      suite_runner=suites, ledger=spend.Ledger(root / "led-i.json"))
    run = flows.implement_once(deps, icase, harness="claude", model="sonnet", reviewer_harness="codex", reviewer_model="gpt-6.1-sol")
    check("round 0 fails hidden tests and review; round 1 is merge-ready",
          [r["merge_ready"] for r in run["rounds"]] == [False, True] and run["merged_ready"] and run["rounds_to_merge_ready"] == 1)
    check("cost to merge-ready includes both implementer and reviewer rounds", abs(run["cost_to_merge_ready"] - (0.2 * 2 + 0.05 * 2)) < 1e-9)
    check("hidden tests were never visible to the implementer", all(s["hidden_present"] in (None, "print('old version')\n") for s in seen))
    check("hidden tests are not left in the implementer's tree", not (Path(run["snapshot"]) / "tiny_hidden_check.py").exists()
          or (Path(run["snapshot"]) / "tiny_hidden_check.py").read_text() != "print('hidden')\n")
    check("the file-scope audit names the file outside the brief", run["rounds"][0]["diff"]["out_of_scope"] == ["extra.py"])
    check("the fix prompt carries the reviewer's finding", "bound excluded" in implementer.calls[1]["prompt"] and "Review round 1" in implementer.calls[1]["prompt"])
    check("the reviewer sees the change as a commit, not the hidden tests",
          reviewer.calls[0]["cwd"].name.endswith("-review") and not reviewer.calls[0]["cwd"].exists())
    flaky_calls = []

    def flaky(grade_dir, rel):
        flaky_calls.append(rel)
        return len(flaky_calls) % 2 == 0, "flaked"          # fails first, passes on the retry
    g = flows.grade(flows.Deps(repo=shas["repo"], suite_runner=flaky), Path(run["snapshot"]), icase)
    check("a failure that does not reproduce is not counted (one retry)", g["hidden_pass"] and len(flaky_calls) == 2)
    g2 = flows.grade(flows.Deps(repo=shas["repo"], suite_runner=lambda d, r: (False, "boom")), Path(run["snapshot"]), icase)
    check("a failure that reproduces is counted, with its tail", not g2["hidden_pass"] and "boom" in g2["tails"]["tiny_hidden_check.py"])
    never = flows.Deps(runners={"claude": fake_runner(["x"], edits=lambda n, c: None), "codex": fake_runner([med])}, use_quota=False,
                       repo=shas["repo"], suite_runner=lambda g, r: (False, "fail"))
    r2 = flows.implement_once(never, icase, harness="claude", model="haiku", reviewer_harness="codex", reviewer_model="m")
    check("after two fix rounds a failing model is not merge-ready", len(r2["rounds"]) == 3 and not r2["merged_ready"] and r2["rounds_to_merge_ready"] is None)
    check("implement rows reach the report", "## Implementer bench" in report.render(report.load_runs(config.runs_root())) and "| claude | sonnet |" in report.render(report.load_runs(config.runs_root())))


def test_cells_and_fixtures(shas, root: Path) -> None:
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
    os.environ["ROLEBENCH_CASES"] = str(cdir)
    try:
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
    finally:
        os.environ.pop("ROLEBENCH_CASES")
    p = brief.fix_prompt(impl_case(shas), "/s", 1, "[MED] a:1 - t\n   s")
    texts = [brief.review_prompt(review_case(shas), "/s"), brief.implement_prompt(impl_case(shas), "/s"), p]
    check("brief templates fill every placeholder",
          not any("{" + k + "}" in t for t in texts for k in ("snap", "python", "brief", "focus", "suites", "scope", "fixtures", "implement", "round", "findings")))
    check("the CLI parses", __import__("rolebench.__main__", fromlist=["main"]).main(["--no-quota-guard", "report", "--out", str(root / "r.md")]) == 0)


def main() -> int:
    print("role bench checks (free, no API)")
    with tempfile.TemporaryDirectory(prefix="rolebench-test-") as tmp:
        root = Path(tmp)
        shas = make_repo(root)
        test_schema(shas)
        test_snapshot(shas, root)
        test_spend(root)
        os.environ["ROLEBENCH_HOME"] = str(root / "home")
        test_parsers()
        test_process_control(root)
        test_review_flow(shas, root)
        test_implement_flow(shas, root)
        test_cells_and_fixtures(shas, root)
    test_env_and_quota()
    test_judge_and_report()
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  - {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
