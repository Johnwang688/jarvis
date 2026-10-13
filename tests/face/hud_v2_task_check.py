"""Headless checks for answering a task's question from the HUD (2026-10-11).

Answering from the task view had never worked. The HUD sent `{question,
answer}` to `POST /tasks/{id}/answer`, the daemon takes exactly `{index, text}`
(`daemon.task_verb_body`) and answered 400, and the HUD swallowed the error,
so the owner pressed Answer and nothing happened. The mock answered `{"ok":
true}` to any body, which is why every check stayed green; it now calls the
daemon's own body check and the runner's own rule (`control.answer_refusal`).

Called from `hud_v2_check.main()` after the push-to-talk section, in a browser
context of its own and against **a `MockDaemon` of its own** on an ephemeral
port, as the other sections are (it rewrites task k1). It also runs on its own:
`python tests/face/hud_v2_task_check.py`.

The checks worth keeping, each shown to fail on origin/main (9c94aaf):
  - the body the daemon receives is exactly `{index, text}`, `index` being the
    question's place in `spec.questions` (an assumable question sits before the
    open ones, so a place among the open ones would be wrong), and the question
    it answered clears while the other stays;
  - a refused answer (the task moved on, unheard by this window) shows the
    daemon's sentence under the box it was typed in, keeps the words, and a
    second try once it is taken clears both;
  - no box is offered for a task that is not CLARIFYING;
  - the mock refuses the old body 400, as the daemon does.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

ASSUMABLE = "python version?"
BRANCH = "commit on main or a branch?"
RUNNER = "which test runner?"


def _clarifying(mock) -> dict:
    """Task k1 as the runner leaves it while it waits on the owner: CLARIFYING,
    an assumable question first, then two blocking ones (indices 1 and 2)."""
    k1 = next(t for t in mock.world["tasks"] if t["id"] == "k1")
    k1["state"] = "clarifying"
    k1["status"].update({"phase": "clarifying", "step": 0, "steps": 0, "open_question": BRANCH})
    k1["plan"] = []
    k1["report"] = None
    k1["spec"]["questions"] = [
        {"text": ASSUMABLE, "blocking": False, "options": [], "answer": None, "assumed": "3.14"},
        {"text": BRANCH, "blocking": True, "options": ["main", "branch"], "answer": None, "assumed": None},
        {"text": RUNNER, "blocking": True, "options": [], "answer": None, "assumed": None},
    ]
    return k1


def _row(view, text):
    """The question's row, found by its words, so the check reads the same on
    a HUD that numbers its boxes differently (origin/main numbered them by
    their place among the open questions)."""
    return view.locator('[data-testid="blocking-questions"] > div').filter(has_text=text)


def task_answer_checks(browser, mock, base, check, until, guard, init_script):
    print("\nanswering a task's question (2026-10-11)")
    from tests.face.hud_v2_check import expand

    k1 = _clarifying(mock)
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, permissions=["microphone"])
    guard(ctx)                      # live ports refused, HTTP and WebSocket alike
    ctx.add_init_script(init_script)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_default_timeout(5000)
    try:
        before = mock.sse_connections()
        page.goto(base + "/")
        page.wait_for_selector('[data-testid="sidebar"]', state="attached")
        mock.await_reconnect(before)
        expand(page, "p1", "task-k1")
        page.locator('[data-testid="task-k1"]').click()
        page.locator('[data-testid="tab-task"]').click()
        until(lambda: page.locator('[data-testid="task-view"]').count() > 0)
        view = page.locator('[data-testid="task-view"]').first
        for section in (_answer_checks, _refusal_checks, _not_clarifying_checks):
            try:
                section(page, view, mock, k1, check, until)
            except Exception as e:  # a section that cannot run is a failure, and the rest still run
                check(f"task answer {section.__name__.strip('_')} ran to the end", False,
                      str(e).splitlines()[0][:200])
        _mock_contract_checks(mock, check)
        check("no page errors in the task answer section", not errors, "; ".join(errors[:3]))
    finally:
        ctx.close()


def _answers(mock) -> list[dict]:
    return mock.sent("POST", "/tasks/k1/answer")


def _answer_checks(page, view, mock, k1, check, until):
    until(lambda: _row(view, RUNNER).count() > 0)
    check("both blocking questions are asked", _row(view, BRANCH).count() == 1 and _row(view, RUNNER).count() == 1)
    check("the assumable one is not", _row(view, ASSUMABLE).count() == 0)
    check("each box is named by its question's index in spec.questions",
          view.locator('[data-testid="answer-0"]').count() == 0
          and view.locator('[data-testid="answer-1"]').count() == 1
          and view.locator('[data-testid="answer-2"]').count() == 1)

    # The second open question first: its index is 2, its place among the
    # open ones 1 — a HUD that counts the wrong list answers the wrong one.
    n = len(_answers(mock))
    row = _row(view, RUNNER)
    row.locator("input").fill("unittest")
    row.locator("button", has_text="Answer").click()
    until(lambda: len(_answers(mock)) > n)
    sent = _answers(mock)[n:]
    check("the daemon receives exactly {index, text}, index the place in spec.questions",
          sent == [{"index": 2, "text": "unittest"}], json.dumps(sent))
    # The mock records a request before it answers it: wait on the world.
    check("the daemon took it", until(lambda: k1["spec"]["questions"][2]["answer"] == "unittest") is True,
          json.dumps(k1["spec"]["questions"][2]))
    check("the question it answered clears", until(lambda: _row(view, RUNNER).count() == 0) is True)
    check("the other one is still asked", _row(view, BRANCH).count() == 1)
    check("nothing was answered wrongly", k1["spec"]["questions"][1]["answer"] is None)


def _refusal_checks(page, view, mock, k1, check, until):
    # The task moved on without this window hearing it (cancelled from
    # Discord, say): the box is still drawn, and the runner refuses.
    k1["state"] = "cancelled"
    n = len(_answers(mock))
    row = _row(view, BRANCH)
    row.locator("input").fill("a branch")
    row.locator("button", has_text="Answer").click()
    until(lambda: len(_answers(mock)) > n)
    check("the refused answer still went out as {index, text}",
          _answers(mock)[n:] == [{"index": 1, "text": "a branch"}], json.dumps(_answers(mock)[n:]))
    err = row.locator('[data-testid="answer-error-1"]')
    shown = until(lambda: err.count() > 0 and err.inner_text().strip())
    check("a refusal shows the daemon's sentence where the owner answered",
          bool(shown) and "Not answered" in shown and "not waiting on a question" in shown, str(shown))
    check("the typed answer is kept", row.locator("input").input_value() == "a branch",
          row.locator("input").input_value())
    check("nothing was answered", k1["spec"]["questions"][1]["answer"] is None
          and row.locator("button", has_text="Answer").is_enabled())

    # It is waiting again: a second try is taken, and the sentence goes with it.
    k1["state"] = "clarifying"
    n = len(_answers(mock))
    row.locator("button", has_text="Answer").click()
    until(lambda: len(_answers(mock)) > n)
    check("the second try is taken", until(lambda: k1["spec"]["questions"][1]["answer"] == "a branch") is True,
          json.dumps(k1["spec"]["questions"][1]))
    check("no question is left to ask",
          until(lambda: view.locator('[data-testid="blocking-questions"]').count() == 0) is True)
    check("and no sentence is left either", view.locator('[data-testid="answer-error-1"]').count() == 0)


def _not_clarifying_checks(page, view, mock, k1, check, until):
    # A running task with a blocking question unanswered (the shape the main
    # suite's world has): the runner would refuse an answer, so none is asked.
    k1["spec"]["questions"][1]["answer"] = None
    k1["state"] = "running"
    k1["status"]["phase"] = "running"
    mock.emit("task_status_changed", {"task_id": "k1", "state": "running"}, task_id="k1")
    until(lambda: view.locator('[data-testid="task-phase"]').inner_text().strip().lower() == "running")
    check("a task that is not clarifying offers no answer box",
          view.locator('[data-testid="blocking-questions"]').count() == 0)


def _mock_contract_checks(mock, check):
    """The mock refuses what the daemon refuses: it calls the daemon's own
    `task_verb_body`, so the old body is a 400 here too."""
    def post(body) -> tuple[int, dict]:
        req = urllib.request.Request(f"http://127.0.0.1:{mock.port}/tasks/k1/answer",
                                     data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as res:
                return res.status, json.loads(res.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    status, payload = post({"question": BRANCH, "answer": "main"})
    check("the mock refuses the old {question, answer} body 400, as the daemon does",
          status == 400 and "unknown fields" in payload.get("error", ""), f"{status} {payload}")
    status, payload = post({"index": True, "text": "main"})
    check("and a boolean index", status == 400 and "index" in payload.get("error", ""), f"{status} {payload}")


def main():
    from playwright.sync_api import sync_playwright

    from tests.face import hud_v2_check as H
    from tests.face.hud_v2_mock import DIST, MockDaemon

    if not (DIST / "index.html").exists():
        print("hud/dist is not built. Run: cd hud && npm ci && npm run build")
        return 2
    mock = MockDaemon(0).start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--use-fake-ui-for-media-stream",
                                                              "--use-fake-device-for-media-stream"])
            task_answer_checks(browser, mock, f"http://127.0.0.1:{mock.port}", H.check, H.until,
                               lambda ctx: H.guard_live(ctx, H.FAILURES.append), H.FAKE_RECOGNIZER)
            browser.close()
    finally:
        mock.stop()
    print()
    if H.FAILURES:
        print(f"{len(H.FAILURES)} of {H.CHECKS} checks FAILED:")
        for f in H.FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {H.CHECKS} task answer checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
