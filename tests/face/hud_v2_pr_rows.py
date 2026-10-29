"""PR rows in each state (PM-F of docs/plans/2026-10-28-pr-inbox-merge.md), in
the daemon's shape after PM-C's `pr_summary`, PM-D's hold and card and PM-E's
`fix` enrichment, for the HUD's and the phone's face checks and their
screenshots. Data only: no daemon, no network.

`STATES` maps a state's name to a builder; each returns one row. The heads
and cards are fixed hex strings so a check can assert exactly what a click
sent.
"""
from __future__ import annotations

REPO = "Johnwang688/Calc"
HOSTILE = '<img src=x onerror="window.__pwned=1">‮evil'


def head(n: int) -> str:
    return f"{n:x}".rjust(2, "0")[-2:] * 20


def card(n: int) -> str:
    return f"{n:x}".rjust(2, "0")[-2:].replace("0", "c") * 32


def part(key, text, items=(), more=0):
    return {"key": key, "text": text, "items": list(items), "more": more}


def facts(n, *, branch="claude/multiply", checks="checks: passing", check_items=(), conflicts=None,
          merge_state=None, files="3 files +40 −2", method="merge commit", extra=()):
    parts = [part("pr", f"{REPO} #{n}"), part("branch", f"{branch} → main")]
    parts.append(part("checks", checks, check_items))
    if conflicts is not None:
        parts.append(part("conflicts", "conflicts:", conflicts))
    if merge_state:
        parts.append(part("merge_state", merge_state))
    parts += [part("files", files), part("head", f"head {head(n)[:7]}"), part("method", method), *extra]
    return parts


def unflagged():
    return {"flagged": False, "items": [], "more": 0, "incomplete": False}


def base(n, *, title="Add multiply to the calculator", headline=None, rank=5, checks="passing",
         merge_state="CLEAN", **detail):
    d = {"repo": REPO, "number": n, "title": title, "branch": "claude/multiply", "base": "main", "author": "johnw",
         "fork": False, "head": head(n), "checks": checks, "merge_state": merge_state,
         "url": f"https://github.com/{REPO}/pull/{n}",
         "facts": facts(n), "flags": unflagged(), "failing": [], "conflicts": None, "files": None,
         "summary": {"does": "Adds a multiply button and its test to the calculator.", "blocked": ""},
         "summary_state": "ready", "summary_note": "", "summary_reason": "", "actions": ["merge"], "why": "",
         "method": "merge", "hold": None, "card": card(n), "fix": None}
    d.update(detail)
    return {"id": f"pr:{REPO}#{n}", "kind": "pr", "rank": rank,
            "title": headline or f"PR #{n} is ready to merge",
            "blocks": f"{REPO} · by johnw · branch claude/multiply · checks {checks}",
            "project_id": None, "thread_id": None, "task_id": None, "asked_at": "2026-10-29T08:00:00+00:00",
            "expires_at": None, "origin": "", "detail": d}


def ready():
    return base(11)


def pending():
    return base(12, summary=None, summary_state="pending")


def conflicts():
    return base(13, headline="PR #13 has merge conflicts", checks="failing", merge_state="DIRTY",
                facts=facts(13, checks="checks: failing", check_items=["test (3.14)"],
                            conflicts=["calc.py", "tests/test_calc.py"]),
                failing=[{"name": "test (3.14)", "scanner": False}], conflicts=["calc.py", "tests/test_calc.py"],
                summary={"does": "Adds a multiply button.",
                         "blocked": "calc.py conflicts with main's new divide; test (3.14) fails on a missing import."},
                actions=["fix"], why="it has merge conflicts", method=None)


def scanner():
    return base(14, headline="PR #14 has failing checks", checks="failing", merge_state="UNSTABLE",
                facts=facts(14, checks="checks: failing", check_items=["GitGuardian Security Checks [secret scanner]",
                                                                       "lint [secret scanner]"]),
                failing=[{"name": "GitGuardian Security Checks", "scanner": True}, {"name": "lint", "scanner": False}],
                summary={"does": "Adds a fixture for the uploader tests.",
                         "blocked": "GitGuardian flags tests/fixtures/key.pem, which looks like a test fixture."},
                actions=["merge_as_is"], why="only a secret scanner fails; an agent never silences one")


def flagged():
    return base(15, title="Tighten the CI and agent notes",
                flags={"flagged": True, "items": [{"name": "CLAUDE.md", "families": ["agent instructions"]},
                                                  {"name": ".github/workflows/ci.yml", "families": ["CI"]}],
                       "more": 0, "incomplete": False},
                summary={"does": "Edits the agent notes and the CI workflow.", "blocked": ""})


def behind():
    return base(16, headline="PR #16 is behind its base branch", merge_state="BEHIND",
                facts=facts(16, merge_state="behind main"), actions=["update"], method=None,
                why="behind main; a fresh Merge is needed after the update")


def waiting():
    return base(17, headline="PR #17 waits on its checks", checks="pending", merge_state="BLOCKED",
                facts=facts(17, checks="checks: pending (2)"), actions=[], method=None, why="waits on its checks",
                summary_note="checks re-running")


def failed():
    return base(18, summary=None, summary_state="failed", summary_reason="the model did not answer in time")


def off():
    return base(19, summary=None, summary_state="off", summary_reason="today's summary budget is used up")


def no_logs():
    return base(20, summary_note="written without the CI logs, which could not be read")


def squash():
    return base(21, facts=facts(21, method="squash merge"), method="squash")


def held():
    return base(22, title="Firmware brake override", actions=[], method=None, why="on hold: waits on the robot test",
                hold={"note": "waits on the robot test", "set_at": "2026-10-29T07:00:00+00:00"})


def fix(state, n, *, rank=5, actions=(), why="", note="", **more):
    rec = {"state": state, "task_id": f"t{n:07d}abcdef", "from_head": "9" * 40, "commits": None,
           "ci_changed": None, "note": note, "summary": None, "summary_state": None, "count": 1, "max": 3}
    rec.update(more)
    return base(n, headline=f"PR #{n} has failing checks", checks="failing", merge_state="UNSTABLE", rank=rank,
                facts=facts(n, checks="checks: failing", check_items=["test (3.14)"]),
                failing=[{"name": "test (3.14)", "scanner": False}], actions=list(actions), why=why, fix=rec,
                summary={"does": "Adds a multiply button.", "blocked": "test (3.14) fails on a missing import."})


def fix_running():
    return fix("running", 31, note="fix running", why="checks fail; a fix is running", actions=["merge_as_is"])


def fix_question():
    return fix("question", 32, note="fix waits on your answer", why="checks fail; a fix is running")


def fix_stopped():
    return fix("stopped", 33, rank=4, note="fix stopped: failed", actions=["fix"], why="checks fail")


def fix_failing():
    return fix("failing", 34, rank=4, note="fix pushed; checks still fail", actions=["fix"], why="checks fail",
               commits=1, ci_changed=False)


def fix_pushed():
    return fix("pushed", 35, rank=4, note="fix pushed; checks running", commits=2, ci_changed=False)


def fix_rewritten():
    return fix("rewritten", 36, rank=4, note="branch rewritten since your Fix", actions=["fix"], why="checks fail")


def fix_ready():
    row = fix("ready", 37, rank=4, note="fixed: " + HOSTILE + " — merge?", actions=["merge"], commits=2,
              ci_changed=True, summary="Merged main in and added the missing import " + HOSTILE,
              summary_state="ready")
    d = row["detail"]
    d.update(checks="passing", merge_state="CLEAN", facts=facts(37), failing=[], why="",
             flags={"flagged": True, "items": [{"name": ".github/workflows/ci.yml", "families": ["CI"]}],
                    "more": 0, "incomplete": False})
    row["title"] = "PR #37 is ready to merge"
    return row


def fix_ready_pending():
    row = fix_ready()
    row["id"] = f"pr:{REPO}#38"
    d = row["detail"]
    d.update(number=38, head=head(38), card=card(38), facts=facts(38), summary=None, summary_state="pending")
    d["fix"].update(summary=None, summary_state="pending", commits=1, ci_changed=False)
    row["title"] = "PR #38 is ready to merge"
    return row


def fix_start_failed():
    return fix("start_failed", 39, note="fix could not start: no project owns this repository", actions=["fix"],
               why="checks fail")


def tq_row(fix_row):
    """The fix task's question (PM-23): its own rank-2 row."""
    task = fix_row["detail"]["fix"]["task_id"]
    return {"id": f"tq:{task}:0", "kind": "task_question", "rank": 2, "title": f"task {task[:8]} asks you",
            "blocks": "blocks its work", "project_id": None, "thread_id": None, "task_id": task,
            "asked_at": "2026-10-29T08:30:00+00:00", "expires_at": None, "origin": "",
            "detail": {"task_id": task, "index": 0, "text": "Merge main in, or rebase?", "options": ["merge", "rebase"]}}


STATES = {
    "ready": ready, "pending": pending, "conflicts": conflicts, "scanner": scanner, "flagged": flagged,
    "behind": behind, "waiting": waiting, "failed": failed, "off": off, "no_logs": no_logs, "squash": squash,
    "held": held, "fix_running": fix_running, "fix_question": fix_question, "fix_stopped": fix_stopped,
    "fix_failing": fix_failing, "fix_pushed": fix_pushed, "fix_rewritten": fix_rewritten, "fix_ready": fix_ready,
    "fix_ready_pending": fix_ready_pending, "fix_start_failed": fix_start_failed,
}


def all_rows():
    rows = [build() for build in STATES.values()]
    q = next(r for r in rows if r["detail"].get("fix") and r["detail"]["fix"]["state"] == "question")
    return [tq_row(q), *rows]
