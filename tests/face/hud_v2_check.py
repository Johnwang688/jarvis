"""Headless checks for the HUD v2, against the mock of `docs/hud-api.md`.

Free — no API calls, no daemon, no providers, no network. `tests/face/
hud_v2_mock.py` serves the built `hud/dist` and answers the contract from a
world this file rewrites between assertions, with the SSE stream on a puppet
string (the v1 `hud_state_check` pattern).

The safety checks are the ones to keep, and each is written to bite:

  - a reply carrying markup renders as **text**, and a `javascript:` link
    renders as text — this window draws authorization cards, so a reply that
    could inject markup into it could draw its own AUTHORIZE button;
  - the preview pane refuses the HUD's own origin (an iframe that could reach
    `/approvals` would let an agent approve itself);
  - a hostile avatar SVG's `onload` never runs — the face is an `<img>`;
  - the approval card ignores Enter: deny is cheap, authorize is deliberate,
    and nothing on it is keyboard-defaulted;
  - an `approval_requested` from a task shows its label and the **whole**
    command, never a summary;
  - dictation REVIEW never sends on its own, and OFF uploads nothing;
  - a wake hit fires once per phrase, not once per recognizer update.

Build first:  cd hud && npm ci && npm run build
Run:          PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python \\
                  tests/face/hud_v2_check.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from playwright.sync_api import sync_playwright  # noqa: E402

from tests.face.hud_v2_mock import DIST, MockDaemon  # noqa: E402

PORT = 8479
BASE = f"http://127.0.0.1:{PORT}"

FAILURES: list[str] = []
CHECKS = 0

# A scripted SpeechRecognition, installed before the page's own script runs.
# Chrome delivers a phrase as a *growing interim transcript* — one event per
# revision — and then once more as the final result, which is precisely the
# level-triggered shape the wake de-dup exists for.
FAKE_RECOGNIZER = """
window.__wakeFires = 0;
class FakeSR {
  constructor() { this.onresult = null; this.onend = null; this.onstart = null;
                  this.onerror = null; this.continuous = false; this.interimResults = false; }
  start() { window.__sr = this; if (this.onstart) this.onstart(); }
  stop() { if (this.onend) this.onend(); }
  abort() {}
}
window.SpeechRecognition = FakeSR;
window.webkitSpeechRecognition = FakeSR;
window.__say = (segments, resultIndex) => {
  const results = segments.map((t) => ({ 0: { transcript: t }, length: 1 }));
  results.length = segments.length;
  window.__sr.onresult({ resultIndex: resultIndex || 0, results });
};
"""


def check(name: str, ok: bool, detail: str = ""):
    global CHECKS
    CHECKS += 1
    if ok:
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  FAIL {name}{(' — ' + detail) if detail else ''}")


def until(fn, timeout=6.0, step=0.05):
    """Poll until `fn()` is truthy. Returns the value, or the last falsey one."""
    deadline = time.time() + timeout
    value = None
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(step)
    return value


def boot(page, mock: MockDaemon, path: str = "/"):
    page.goto(BASE + path)
    page.wait_for_selector('[data-testid="sidebar"]')
    # The window loads projects, threads, tasks and the panels before it is
    # meaningfully alive; waiting on the sidebar's first project is the cheapest
    # signal that the first wave of calls has landed.
    until(lambda: page.locator('[data-testid="project-p1"]').count() > 0)
    _ = mock
    return page


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------

def sidebar_checks(page, mock):
    print("\nsidebar")
    check("projects listed", page.locator('[data-testid="project-p1"]').count() == 1)
    # A `/mnt/<drive>/` root is a Windows project, badged, with the 9p caution.
    badge = page.locator('[data-testid="project-p2"] .badge.win')
    check("windows project carries a badge", badge.count() == 1)
    check("the badge states the 9p caution",
          "9p" in (badge.get_attribute("title") or ""),
          badge.get_attribute("title") or "")
    check("a wsl project carries none",
          page.locator('[data-testid="project-p1"] .badge.win').count() == 0)

    page.locator('[data-testid="project-p1"]').click()
    until(lambda: page.locator('[data-testid="thread-t1"]').count() > 0)
    check("a project expands to its chat threads",
          page.locator('[data-testid="thread-t1"]').count() == 1)
    check("and to its tasks", page.locator('[data-testid="task-k1"]').count() == 1)

    # The orchestration view: a task expands to its threads with role and
    # provider, which is what makes "who is doing what" visible at all.
    page.locator('[data-testid="task-k1"]').click()
    until(lambda: page.locator('[data-testid="taskthread-kt1"]').count() > 0)
    row = page.locator('[data-testid="taskthread-kt1"]')
    check("a task expands to its threads", row.count() == 1)
    # Lower-cased because the skin renders badges in caps.
    text_kt1 = row.inner_text().lower()
    check("each names its role and provider",
          "orchestrator" in text_kt1 and "claude" in text_kt1, text_kt1)
    check("the implementer's provider is shown too",
          "codex" in page.locator('[data-testid="taskthread-kt2"]').inner_text().lower())
    check("schedules, usage and route are reachable",
          page.locator('[data-testid="open-schedules"]').count() == 1
          and page.locator('[data-testid="open-usage"]').count() == 1
          and page.locator('[data-testid="open-route"]').count() == 1)
    _ = mock


def chat_checks(page, mock):
    print("\nchat tab")
    page.locator('[data-testid="tab-chat"]').click()
    page.locator('[data-testid="thread-t1"]').click()
    until(lambda: page.locator('[data-testid="log"] .msg').count() >= 2)
    check("the saved transcript is redrawn",
          page.locator('[data-testid="msg-user"]').first.inner_text().find("what is the plan") >= 0)
    check("his reply is rendered, not printed as source",
          page.locator('[data-testid="log"] .msg.assistant strong').count() >= 1)

    # The live draft: plain text while it is being written, rendered once when
    # the turn settles.
    mock.emit("turn_started", {}, thread_id="t1")
    mock.emit("text_delta", {"text": "**Work"}, thread_id="t1")
    until(lambda: page.locator('[data-testid="draft"]').count() > 0)
    draft = page.locator('[data-testid="draft"]')
    check("a draft appears mid-turn", draft.count() == 1)
    check("the draft is plain text, not half-rendered markdown",
          draft.locator("strong").count() == 0 and "**Work" in draft.inner_text(),
          draft.inner_text())
    mock.emit("text_delta", {"text": "ing** on it."}, thread_id="t1")
    mock.emit("text", {"text": "**Working** on it."}, thread_id="t1")
    until(lambda: page.locator('[data-testid="draft"]').count() == 0)
    check("the finished reply replaces the draft",
          page.locator('[data-testid="draft"]').count() == 0)
    rendered = page.locator('[data-testid="log"] .msg.assistant').last
    check("and is rendered once",
          rendered.locator("strong").count() == 1
          and "Working on it." in rendered.inner_text()
          and "**" not in rendered.inner_text(), rendered.inner_text())

    # The tool stream: started/finished with durations (v1's OPERATIONS).
    mock.emit("tool_started", {"call_id": "c1", "name": "grep_files"}, thread_id="t1")
    until(lambda: page.locator('[data-testid="op"]').count() > 0)
    check("a running tool shows in the ticker",
          "grep_files" in page.locator('[data-testid="ops"]').inner_text())
    check("and shows as running", "running" in page.locator('[data-testid="ops"]').inner_text())
    mock.emit("tool_finished", {"call_id": "c1", "name": "grep_files", "ok": True}, thread_id="t1")
    until(lambda: "done" in page.locator('[data-testid="ops"]').inner_text())
    check("a finished tool stops being shown as running",
          "running" not in page.locator('[data-testid="ops"]').inner_text(),
          page.locator('[data-testid="ops"]').inner_text())
    mock.emit("turn_finished", {"stop": "end"}, thread_id="t1")

    # A proposal_reply is a system line with a 60s cancel that calls
    # /tasks/{id}/cancel — the fast path has answered and a task is starting.
    mock.emit("proposal_reply", {"reply": "Starting a task for that.", "task_id": "k1"},
              thread_id="t1", turn_id="turn-9")
    until(lambda: page.locator('[data-testid="proposal"]').count() > 0)
    check("a proposal shows as a system line",
          page.locator('[data-testid="proposal"]').count() == 1)
    btn = page.locator('[data-testid="proposal-cancel"]')
    check("with a countdown cancel button",
          btn.count() == 1 and "s)" in btn.inner_text().lower(),
          btn.inner_text() if btn.count() else "")
    before = len(mock.sent("POST", "/tasks/k1/cancel"))
    btn.click()
    until(lambda: len(mock.sent("POST", "/tasks/k1/cancel")) > before)
    check("cancel calls /tasks/{id}/cancel",
          len(mock.sent("POST", "/tasks/k1/cancel")) > before)


def safety_render_checks(page, mock):
    print("\nsafety: what a reply may draw in the approval surface")
    page.locator('[data-testid="tab-chat"]').click()
    page.evaluate("window.__pwned = false")
    mock.emit("text", {"text": '<img src=x onerror="window.__pwned=true"> <b>bold?</b>'},
              thread_id="t1")
    until(lambda: "onerror" in page.locator('[data-testid="log"]').inner_text())
    last = page.locator('[data-testid="log"] .msg.assistant').last
    check("a reply carrying markup renders as text",
          last.locator("img").count() == 0 and last.locator("b").count() == 0)
    check("its handler never runs", page.evaluate("window.__pwned") is False)

    mock.emit("text", {"text": "[click me](javascript:window.__pwned=true)"}, thread_id="t1")
    until(lambda: "click me" in page.locator('[data-testid="log"]').inner_text())
    last = page.locator('[data-testid="log"] .msg.assistant').last
    check("a javascript: link renders as text, not an anchor",
          last.locator("a").count() == 0, last.inner_html())

    mock.emit("text", {"text": "[docs](https://example.com/x)"}, thread_id="t1")
    until(lambda: page.locator('[data-testid="log"] .msg.assistant a').count() > 0)
    a = page.locator('[data-testid="log"] .msg.assistant a').last
    check("an http link survives, opened away from the HUD",
          a.get_attribute("target") == "_blank" and "noopener" in (a.get_attribute("rel") or ""))

    # The owner's own line goes up verbatim: typing `*foo*` means `*foo*`.
    box = page.locator('[data-testid="input"]')
    box.click()
    box.fill("*not emphasis* and <b>not bold</b>")
    box.press("Enter")
    until(lambda: "not emphasis" in page.locator('[data-testid="msg-user"]').last.inner_text())
    mine = page.locator('[data-testid="msg-user"]').last
    check("the owner's own message is verbatim",
          mine.locator("em").count() == 0 and mine.locator("b").count() == 0
          and "*not emphasis*" in mine.inner_text(), mine.inner_text())


def input_checks(page, mock):
    print("\ninput bar")
    before = len(mock.sent("POST", "/threads/t1/send"))
    box = page.locator('[data-testid="input"]')
    box.click()
    box.fill("hello there")
    box.press("Enter")
    sent = until(lambda: mock.sent("POST", "/threads/t1/send")[before:] or None)
    check("Enter sends the turn", bool(sent))
    check("with the typed text", bool(sent) and sent[-1].get("text") == "hello there",
          str(sent[-1] if sent else None))
    check("and clears the box", page.locator('[data-testid="input"]').input_value() == "")

    # Space typed in the box is a space, never push-to-talk.
    page.evaluate("window.__hud.state()")
    box.click()
    box.fill("")
    box.type("a b c")
    check("space in the box does not trigger push-to-talk",
          page.evaluate("window.__hud.capture.ptt") is None
          and box.input_value() == "a b c", box.input_value())
    box.fill("")

    # An empty Enter is inert.
    n = len(mock.sent("POST", "/threads/t1/send"))
    box.press("Enter")
    time.sleep(0.2)
    check("an empty Enter sends nothing", len(mock.sent("POST", "/threads/t1/send")) == n)


def dictation_checks(page, mock):
    print("\ndictation modes")
    # He does not answer himself: the detector is suppressed while a turn is in
    # flight, so the typed turns above have to be settled before the mic can be
    # driven at all.
    mock.emit("turn_finished", {"stop": "end"}, thread_id="t1")
    until(lambda: page.evaluate("window.__hud.state().busy") is False)
    check("a fresh window boots into REVIEW",
          page.locator('[data-testid="dictation-review"]').get_attribute("aria-pressed") == "true")

    # OFF uploads nothing — not the utterance, and not its pre-roll.
    page.locator('[data-testid="dictation-off"]').click()
    stt_before = len(mock.sent("POST", "/stt"))
    page.evaluate("""
      const m = window.__hud.mic;
      m.feedMs(2000, 0.001); m.feedMs(1400, 0.06); m.feedMs(2000, 0.0005);
    """)
    time.sleep(0.4)
    check("OFF uploads nothing", len(mock.sent("POST", "/stt")) == stt_before)
    check("OFF says the mic is muted rather than inviting speech",
          "MUTED" in page.locator('[data-testid="hint"]').inner_text(),
          page.locator('[data-testid="hint"]').inner_text())
    check("OFF stops the wake recognizer outright",
          page.evaluate("!window.__hudRecog"))
    check("and the level meter reads zero",
          float(page.locator('[data-testid="level"]').get_attribute("data-level") or 1) == 0.0)

    # REVIEW puts the transcript in the box and never sends on its own.
    page.locator('[data-testid="dictation-review"]').click()
    sends_before = len(mock.sent("POST", "/threads/t1/send"))
    stt_before = len(mock.sent("POST", "/stt"))
    page.evaluate("""
      const m = window.__hud.mic;
      m.feedMs(2000, 0.001);
      window.__hud.capture.openFollowUp();
      m.feedMs(1600, 0.06); m.feedMs(2200, 0.0005);
    """)
    until(lambda: len(mock.sent("POST", "/stt")) > stt_before)
    check("REVIEW transcribes the utterance", len(mock.sent("POST", "/stt")) > stt_before)
    until(lambda: page.locator('[data-testid="input"]').input_value() != "")
    check("REVIEW puts the transcript in the box",
          "what is the weather" in page.locator('[data-testid="input"]').input_value(),
          page.locator('[data-testid="input"]').input_value())
    time.sleep(0.3)
    check("REVIEW never sends on its own",
          len(mock.sent("POST", "/threads/t1/send")) == sends_before)
    page.locator('[data-testid="input"]').fill("")

    # AUTO sends a finished utterance: /stt then /send.
    page.locator('[data-testid="dictation-auto"]').click()
    sends_before = len(mock.sent("POST", "/threads/t1/send"))
    page.evaluate("""
      const m = window.__hud.mic;
      m.feedMs(2000, 0.001);
      window.__hud.capture.openFollowUp();
      m.feedMs(1600, 0.06); m.feedMs(2200, 0.0005);
    """)
    sent = until(lambda: mock.sent("POST", "/threads/t1/send")[sends_before:] or None)
    check("AUTO sends the finished utterance", bool(sent))
    check("carrying the transcript", bool(sent) and sent[-1].get("text") == "what is the weather",
          str(sent[-1] if sent else None))

    # The choice persists; a reload comes back to it.
    page.reload()
    page.wait_for_selector('[data-testid="dictation-auto"]')
    check("the mode persists across a reload",
          page.locator('[data-testid="dictation-auto"]').get_attribute("aria-pressed") == "true")
    page.locator('[data-testid="dictation-review"]').click()


def wake_checks(page, mock):
    print("\nwake word")
    # The real handler is what fires; count the claims the capture pipeline
    # makes rather than the events the recognizer delivers.
    #
    # Harness note worth copying: an `evaluate` whose **last expression is a
    # function** is *called* by Playwright with its argument, so a script
    # ending in `x.onWake = () => {…}` installs the hook and then runs it once —
    # which reads as the wake word firing twice. Every instrumenting evaluate
    # here therefore ends in a plain value.
    page.evaluate("""
      window.__wakeClaims = [];
      const cap = window.__hud.capture;
      const orig = cap.onWake.bind(cap);
      cap.onWake = () => { const r = orig(); window.__wakeClaims.push(r); return r; };
      true;
    """)
    # interim "jar", interim "jarvis", final "jarvis" — one phrase, three events
    page.evaluate("window.__say(['jar'], 0)")
    page.evaluate("window.__say(['jarvis'], 0)")
    page.evaluate("window.__say(['jarvis'], 0)")
    time.sleep(0.2)
    fires = page.evaluate("window.__wakeClaims.length")
    check("a wake hit fires once per phrase, not once per delivery", fires == 1, f"{fires} fires")

    # Carrying on talking in the same segment does not re-fire.
    page.evaluate("window.__say(['jarvis what is the weather today'], 0)")
    page.evaluate("window.__say(['jarvis what is the weather today please'], 0)")
    time.sleep(0.2)
    check("carrying on talking does not re-fire",
          page.evaluate("window.__wakeClaims.length") == 1)

    # Ordinary speech in a new segment stays silent.
    page.evaluate("window.__say(['jarvis', ' yahoo finance is up'], 1)")
    time.sleep(0.2)
    check("ordinary speech in a new segment stays silent",
          page.evaluate("window.__wakeClaims.length") == 1)
    _ = mock


def approval_checks(page, mock):
    print("\napprovals")
    request = {
        "req_id": "r1", "code": "AB12", "tool": "run_command",
        "args": {"command": "rm -rf /home/johnw/projects/scratch && git push --force"},
        "command": "rm -rf /home/johnw/projects/scratch && git push --force",
        "reason": "reviewer declined", "layer": "human", "thread_id": "kt2",
        "task_id": "k1", "provider": "codex", "origin": "task: add multiply",
        "asked_at": "2026-09-15T00:02:00+00:00", "allowlistable": True, "timeout_s": 120,
    }
    mock.emit("approval_requested", request)
    until(lambda: page.locator('[data-testid="approval-card"]').count() > 0)
    check("an approval_requested raises the card",
          page.locator('[data-testid="approval-card"]').count() == 1)
    check("it names the task that is asking",
          "add multiply" in page.locator('[data-testid="approval-origin"]').inner_text(),
          page.locator('[data-testid="approval-origin"]').inner_text())
    # The whole command, never a summary: approving a command is not consent to
    # what it does, so the owner has to be able to read what it does.
    cmd = page.locator('[data-testid="approval-command"]').inner_text()
    check("it shows the whole command", request["command"] in cmd, cmd)

    # Nothing is keyboard-defaulted: Enter must do nothing at all.
    resolved_before = len(mock.sent("POST", "/approvals/r1"))
    page.keyboard.press("Enter")
    time.sleep(0.3)
    check("the card ignores Enter",
          len(mock.sent("POST", "/approvals/r1")) == resolved_before
          and page.locator('[data-testid="approval-card"]').count() == 1)

    # Push-to-talk and typing are inert while a card is up.
    check("the input is disabled while a card is up",
          page.locator('[data-testid="input"]').is_disabled())
    page.keyboard.press("Space")
    check("space does not start recording while a card is up",
          page.evaluate("window.__hud.capture.ptt") is None)

    # Escape denies — the cheap action.
    page.keyboard.press("Escape")
    body = until(lambda: mock.sent("POST", "/approvals/r1") or None)
    check("Escape denies", bool(body) and body[-1].get("decision") == "deny",
          str(body[-1] if body else None))
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0)
    check("and the card goes", page.locator('[data-testid="approval-card"]').count() == 0)

    # Authorize takes a deliberate click.
    mock.emit("approval_requested", {**request, "req_id": "r2"})
    until(lambda: page.locator('[data-testid="approval-card"]').count() > 0)
    page.locator('[data-testid="approval-allow"]').click()
    body = until(lambda: mock.sent("POST", "/approvals/r2") or None)
    check("AUTHORIZE allows", bool(body) and body[-1].get("decision") == "allow",
          str(body[-1] if body else None))

    # More than one open: the queue shows in the right pane.
    mock.emit("approval_requested", {**request, "req_id": "r3", "origin": "task: one"})
    mock.emit("approval_requested", {**request, "req_id": "r4", "origin": "task: two"})
    until(lambda: page.locator('[data-testid="approval-queue"]').count() > 0)
    queue_text = page.locator('[data-testid="approval-queue"]').inner_text()
    check("a second open request shows the queue", "2" in queue_text, queue_text)
    check("each queued row names its task",
          "task: one" in queue_text and "task: two" in queue_text, queue_text)
    # An approval_resolved elsewhere (Discord) withdraws the card here.
    mock.emit("approval_resolved", {"req_id": "r3", "decision": "allow"})
    mock.emit("approval_resolved", {"req_id": "r4", "decision": "deny"})
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0)
    check("a remote resolution withdraws the card",
          page.locator('[data-testid="approval-card"]').count() == 0)

    # ALWAYS is offered only when the request allows it (§6.1: a command the
    # reviewer refused is not one to auto-approve next time).
    mock.emit("approval_requested", {**request, "req_id": "r5", "allowlistable": False})
    until(lambda: page.locator('[data-testid="approval-card"]').count() > 0)
    check("ALWAYS is absent when the request forbids it",
          page.locator('[data-testid="approval-card"] button.always').count() == 0)
    page.keyboard.press("Escape")
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0)


def task_checks(page, mock):
    print("\ntask tab")
    page.locator('[data-testid="task-k1"]').click()
    page.locator('[data-testid="tab-task"]').click()
    until(lambda: page.locator('[data-testid="task-view"]').count() > 0)
    view = page.locator('[data-testid="task-view"]').first
    # Lower-cased: the skin renders the phase label in caps.
    check("the status record drives the view",
          view.locator('[data-testid="task-phase"]').inner_text().strip().lower() == "running",
          view.locator('[data-testid="task-phase"]').inner_text())
    check("step i/n is shown",
          view.locator('[data-testid="task-step"]').inner_text() == "2/5",
          view.locator('[data-testid="task-step"]').inner_text())
    check("the elapsed clock is shown",
          "1m" in view.locator('[data-testid="task-elapsed"]').inner_text(),
          view.locator('[data-testid="task-elapsed"]').inner_text())
    check("cost is shown",
          "0.42" in view.locator('[data-testid="task-cost"]').inner_text(),
          view.locator('[data-testid="task-cost"]').inner_text())
    # §8.5: a routing decision the owner cannot see is one they will assume
    # was wrong.
    line = view.locator('[data-testid="routing-line"]').inner_text()
    check("the routing line names every role and its reason",
          "orchestrator: claude" in line and "implementer: codex" in line
          and "over threshold" in line, line)
    check("the plan marks the current step",
          "cur" in (view.locator('[data-testid="plan-step-2"]').get_attribute("class") or ""),
          view.locator('[data-testid="plan-step-2"]').get_attribute("class") or "")
    check("earlier steps are marked done",
          "done" in (view.locator('[data-testid="plan-step-1"]').get_attribute("class") or ""))
    check("the acceptance criteria are shown",
          view.locator('[data-testid="acceptance"]').count() == 2)

    # A blocking question gets an answer box; an assumable one does not park
    # the task and is not asked.
    qs = view.locator('[data-testid="blocking-questions"]')
    check("a blocking question is asked", qs.count() == 1)
    check("only the blocking one", view.locator('[data-testid="answer-1"]').count() == 0)
    view.locator('[data-testid="answer-0"]').fill("branch")
    view.locator('[data-testid="answer-send-0"]').click()
    body = until(lambda: mock.sent("POST", "/tasks/k1/answer") or None)
    check("answering calls /answer with the question and the answer",
          bool(body) and body[-1].get("answer") == "branch"
          and "commit" in (body[-1].get("question") or ""), str(body[-1] if body else None))

    view.locator('[data-testid="steer-box"]').fill("skip the docstring nit")
    view.locator('[data-testid="steer-send"]').click()
    body = until(lambda: mock.sent("POST", "/tasks/k1/steer") or None)
    check("steering calls /steer", bool(body) and "docstring" in (body[-1].get("text") or ""))

    n = len(mock.sent("POST", "/tasks/k1/cancel"))
    view.locator('[data-testid="task-cancel"]').click()
    until(lambda: len(mock.sent("POST", "/tasks/k1/cancel")) > n)
    check("cancel calls /cancel", len(mock.sent("POST", "/tasks/k1/cancel")) > n)
    # resume applies to BLOCKED only; FAILED is terminal.
    check("resume is refused on a running task",
          view.locator('[data-testid="task-resume"]').is_disabled())

    # task_status_changed drives it, not the model's prose.
    mock.world["tasks"][0]["state"] = "done"
    mock.world["tasks"][0]["status"]["phase"] = "done"
    mock.world["tasks"][0]["status"]["step"] = 5
    mock.world["tasks"][0]["report"] = {
        "done": ["multiply added", "tests written"],
        "changed": ["calc.py", "test_calc.py", "branch jarvis/add-multiply"],
        "verified": "reviewer thread kt3 ran the acceptance commands",
        "open": [], "next": "merge the branch",
        "cost": {"claude": 1.81, "codex_tokens": 40312},
    }
    mock.emit("task_status_changed", {"task_id": "k1", "state": "done"}, task_id="k1")
    until(lambda: page.locator('[data-testid="report"]').count() > 0)
    report = page.locator('[data-testid="report"]').first.inner_text()
    for heading in ("DONE", "CHANGED", "VERIFIED", "OPEN", "NEXT", "COST"):
        check(f"the report carries {heading}", heading in report)
    check("the report shows the full text, not a Discord-capped one",
          "reviewer thread kt3" in report, report)
    check("the phase followed the status record",
          page.locator('[data-testid="task-phase"]').first.inner_text().strip().lower() == "done",
          page.locator('[data-testid="task-phase"]').first.inner_text())


def file_checks(page, mock):
    print("\nfile tab")
    page.locator('[data-testid="project-p1"]').click()
    page.locator('[data-testid="tab-file"]').click()
    # Wait on a real row, not on the tree being non-empty: it renders a "…"
    # placeholder while the fetch is in flight, so "non-empty" is true before
    # anything has arrived.
    until(lambda: page.locator('[data-testid="file-calc.py"]').count() > 0)
    check("the tree lists the project root",
          page.locator('[data-testid="file-calc.py"]').count() == 1)

    page.locator('[data-testid="file-calc.py"]').click()
    until(lambda: page.locator('[data-testid="file-path"]').inner_text() == "calc.py")
    check("opening a file names it", page.locator('[data-testid="file-path"]').inner_text() == "calc.py")
    until(lambda: page.locator('[data-testid="editor"]').count() > 0
          or page.locator('[data-testid="editor-fallback"]').count() > 0)
    check("it opens in an editor",
          page.locator('[data-testid="editor"]').count() == 1
          or page.locator('[data-testid="editor-fallback"]').count() == 1)

    # The save guard: 409 means someone else wrote it, and the only safe answer
    # is to reload rather than to clobber.
    mono = page.locator('[data-testid="editor"]').count() == 1

    def edit(typed: str):
        """Type into whichever editor mounted. Monaco's text layer sits over its
        own hidden textarea, so the click has to land on the rendered lines."""
        if mono:
            page.locator('[data-testid="editor"] .view-lines').click()
            page.keyboard.press("Control+End")
            page.keyboard.type(typed)
        else:
            page.evaluate(
                "t => { const h = document.querySelector('[data-testid=\"editor-fallback\"]');"
                " h.value += t; h.dispatchEvent(new Event('input', {bubbles: true})); }",
                typed,
            )

    edit("\n# edited")
    until(lambda: not page.locator('[data-testid="file-save"]').is_disabled())
    check("save arms once the file is dirty",
          not page.locator('[data-testid="file-save"]').is_disabled())
    page.locator('[data-testid="file-save"]').click()
    body = until(lambda: mock.sent("PUT", "/projects/p1/file") or None)
    check("save sends the expected mtime",
          bool(body) and body[-1].get("expected_mtime") == 1757900002,
          str(body[-1] if body else None))
    until(lambda: page.locator('[data-testid="file-note"]').count() > 0)
    check("a successful save says so",
          "saved" in page.locator('[data-testid="file-note"]').inner_text(),
          page.locator('[data-testid="file-note"]').inner_text())

    # Now a stale write: the mock has moved the mtime on.
    mock.world["files"]["calc.py"]["mtime"] = 999999
    edit(" more")
    until(lambda: not page.locator('[data-testid="file-save"]').is_disabled())
    page.locator('[data-testid="file-save"]').click()
    until(lambda: "reload" in page.locator('[data-testid="file-note"]').inner_text())
    check("a 409 prompts a reload rather than clobbering",
          "reload" in page.locator('[data-testid="file-note"]').inner_text(),
          page.locator('[data-testid="file-note"]').inner_text())

    # A protected credential file: no content, ever.
    page.locator('[data-testid="file-.env"]').click()
    until(lambda: page.locator('[data-testid="file-withheld"]').count() > 0)
    withheld = page.locator('[data-testid="file-withheld"]')
    check("a protected file is shown as withheld", withheld.count() == 1)
    check("with no content", "withheld" in withheld.inner_text())
    check("and cannot be saved", page.locator('[data-testid="file-save"]').is_disabled())

    # Markdown opens rendered, with a toggle back to the split editor.
    page.locator('[data-testid="file-README.md"]').click()
    until(lambda: page.locator('[data-testid="md-rendered"]').count() > 0)
    check("a markdown file opens rendered for reading",
          page.locator('[data-testid="md-rendered"]').count() == 1)
    check("rendered means elements, not source",
          page.locator('[data-testid="md-rendered"] h1').count() == 1)
    page.locator('[data-testid="toggle-rendered"]').click()
    until(lambda: page.locator('[data-testid="md-split"]').count() > 0)
    check("the toggle gives the split view",
          page.locator('[data-testid="md-split"]').count() == 1
          and page.locator('[data-testid="md-preview"] h1').count() == 1)


def diff_checks(page, mock):
    print("\ndiff tab")
    page.locator('[data-testid="task-k1"]').click()
    page.locator('[data-testid="tab-diff"]').click()
    until(lambda: page.locator('[data-testid="difflist"]').inner_text() != "")
    check("the changed files are listed",
          page.locator('[data-testid="diff-calc.py"]').count() == 1
          and page.locator('[data-testid="diff-test_calc.py"]').count() == 1)
    summary = page.locator('[data-testid="diff-summary"]').inner_text()
    check("the summary counts the churn", "+27" in summary, summary)
    # Truncation is stated, never silent: a diff the owner believes is whole
    # when it is not is how a review misses the file that mattered.
    check("truncation is stated", "TRUNCATED" in summary, summary)
    page.locator('[data-testid="diff-calc.py"]').click()
    until(lambda: page.locator('[data-testid="diff-editor"]').count() > 0
          or page.locator('[data-testid="diff-fallback"]').count() > 0)
    check("a file opens in a diff editor",
          page.locator('[data-testid="diff-editor"]').count() == 1
          or page.locator('[data-testid="diff-fallback"]').count() == 1)
    check("the file's two sides were fetched", mock.saw("GET", "/tasks/k1/diff/file"))


def preview_checks(page, mock):
    print("\npreview tab")
    page.locator('[data-testid="tab-preview"]').click()
    page.wait_for_selector('[data-testid="preview-url"]')
    # The HUD is the approval surface: an iframe that could reach /approvals
    # would let an agent approve itself.
    for bad in (f"http://127.0.0.1:{8402}/", "http://localhost:8402/index.html"):
        page.locator('[data-testid="preview-url"]').fill(bad)
        page.locator('[data-testid="preview-go"]').click()
        until(lambda: page.locator('[data-testid="preview-refused"]').count() > 0)
        check(f"the preview refuses the HUD origin ({bad})",
              page.locator('[data-testid="preview-refused"]').count() == 1
              and page.locator('[data-testid="preview-frame"]').count() == 0)

    page.locator('[data-testid="preview-url"]').fill("https://example.com/")
    page.locator('[data-testid="preview-go"]').click()
    until(lambda: page.locator('[data-testid="preview-refused"]').count() > 0)
    check("and anything that is not a local server",
          page.locator('[data-testid="preview-frame"]').count() == 0)

    page.locator('[data-testid="preview-project"]').click()
    until(lambda: page.locator('[data-testid="preview-frame"]').count() > 0)
    frame = page.locator('[data-testid="preview-frame"]')
    check("the project preview loads from the workshop origin",
          "8403" in (frame.get_attribute("src") or ""), frame.get_attribute("src") or "")
    sandbox = frame.get_attribute("sandbox") or ""
    check("the frame is sandboxed", "allow-scripts" in sandbox, sandbox)
    # allow-scripts together with allow-same-origin is no sandbox at all.
    check("and never allow-same-origin beside allow-scripts",
          "allow-same-origin" not in sandbox, sandbox)
    _ = mock


def panels_checks(page, mock):
    print("\nusage, schedules, route")
    usage = page.locator('[data-testid="usage"]')
    until(lambda: usage.count() > 0 and usage.inner_text() != "")
    check("both providers' state is shown",
          "claude" in usage.inner_text() and "codex" in usage.inner_text())
    check("today's figures are shown", "412,000" in usage.inner_text()
          or "412000" in usage.inner_text(), usage.inner_text()[:200])
    check("a reported quota window is shown",
          "82%" in page.locator('[data-testid="quota-codex"]').inner_text(),
          page.locator('[data-testid="quota-codex"]').inner_text())
    # `quota` is filled only from a provider's own report; never computed.
    check("an unreported quota says so, rather than being invented",
          page.locator('[data-testid="quota-claude"]').inner_text().strip() == "not reported",
          page.locator('[data-testid="quota-claude"]').inner_text())
    check("a provider's reason is shown when it is not available",
          "weekly window at 82%" in usage.inner_text())

    sched = page.locator('[data-testid="schedules"]')
    check("schedules are listed", "morning briefing" in sched.inner_text())
    check("with their cron and last/next run",
          "0 7 * * *" in sched.inner_text() and "2026-09-16" in sched.inner_text())
    sched.locator('[data-testid="schedule-toggle-s1"]').click()
    body = until(lambda: mock.sent("PATCH", "/schedules/s1") or None)
    check("disable patches the schedule", bool(body) and body[-1].get("enabled") is False)
    n = len(mock.sent("POST", "/schedules/s1/run-now"))
    page.locator('[data-testid="schedule-run-s1"]').click()
    until(lambda: len(mock.sent("POST", "/schedules/s1/run-now")) > n)
    check("run now calls run-now", len(mock.sent("POST", "/schedules/s1/run-now")) > n)

    page.locator('[data-testid="schedule-brief"]').fill("weekly sweep")
    page.locator('[data-testid="schedule-cron"]').fill("0 9 * * 1")
    page.locator('[data-testid="schedule-create"]').click()
    body = until(lambda: mock.sent("POST", "/schedules") or None)
    check("creating a schedule sends the brief and the cron",
          bool(body) and body[-1].get("cron") == "0 9 * * 1"
          and body[-1].get("brief") == "weekly sweep", str(body[-1] if body else None))

    route = page.locator('[data-testid="route"]').first
    check("the routing table is shown", "orchestrator" in route.inner_text())
    check("ledger states are shown", "over_threshold" in route.inner_text())
    check("the last decisions carry their reasons",
          "claude over threshold" in route.inner_text(), route.inner_text()[:300])


def picker_checks(page, mock):
    print("\npickers")
    page.locator('[data-testid="open-model"]').click()
    page.wait_for_selector('[data-testid="picker"]')
    # The picker chooses the fast path's model only, and says so.
    note = page.locator('[data-testid="model-scope-note"]').inner_text()
    check("the model picker states it is the fast path only",
          "fast path" in note.lower() and "codex" in note.lower(), note)
    check("the routing table is shown read-only beside it",
          page.locator('[data-testid="routing-readonly"]').count() == 1)
    # A model name off the network, carrying markup: it renders as text.
    page.evaluate("window.__pwned = false")
    row = page.locator('[data-testid="model-evil/model"]')
    check("a model name carrying markup renders as text",
          row.locator("img").count() == 0 and "<img" in row.inner_text(), row.inner_text())
    check("and its handler never runs", page.evaluate("window.__pwned") is False)
    # Setting effort does not also switch him onto that model.
    page.locator('[data-testid="effort-openai/gpt-5.6-luna"]').select_option("high")
    body = until(lambda: mock.sent("POST", "/model") or None)
    check("choosing an effort sends the effort",
          bool(body) and body[-1].get("effort") == "high", str(body[-1] if body else None))
    page.locator('[data-testid="picker-close"]').click()
    until(lambda: page.locator('[data-testid="picker"]').count() == 0)

    page.locator('[data-testid="open-voice"]').click()
    page.wait_for_selector('[data-testid="picker"]')
    check("the voice picker groups backends",
          "POCKET" in page.locator('[data-testid="voice-pocket:alba"]').inner_text().upper(),
          page.locator('[data-testid="voice-pocket:alba"]').inner_text())
    # "" clears the override: the way back to the avatar's own voice.
    page.locator('[data-testid="voice-default"]').click()
    body = until(lambda: mock.sent("POST", "/voice") or None)
    check("AVATAR DEFAULT clears the override", bool(body) and body[-1].get("name") == "")
    page.locator('[data-testid="picker-close"]').click()
    until(lambda: page.locator('[data-testid="picker"]').count() == 0)

    page.locator('[data-testid="open-avatar"]').click()
    page.wait_for_selector('[data-testid="picker"]')
    check("the avatar picker lists them with their wake phrases",
          "hoot" in page.locator('[data-testid="avatar-hoot"]').inner_text())
    page.evaluate("window.__pwned = false")
    page.locator('[data-testid="avatar-hoot"]').click()
    until(lambda: page.locator("#avatar").count() > 0)
    check("the face is drawn as an <img>", page.locator("img#avatar").count() == 1)
    time.sleep(0.4)
    # The art is untrusted input drawn in the window that gates approvals.
    check("a hostile avatar SVG's onload never runs",
          page.evaluate("window.__pwned") is False)
    check("its <script> never runs either", page.evaluate("!window.__pwnedBySvg"))
    face = page.locator("img#avatar")
    check("the face cannot eat a click meant for the card",
          face.evaluate("el => getComputedStyle(el).pointerEvents") == "none")
    page.locator('[data-testid="picker-close"]').click()

    # An SSE avatar broadcast relabels live.
    mock.world["avatars"]["active"] = "jarvis"
    mock.emit("avatar", {"slug": "jarvis"})
    time.sleep(0.4)
    check("an SSE avatar broadcast is picked up", mock.saw("GET", "/avatars"))


def window_checks(page, mock):
    print("\nthe window itself")
    check("the title stays J.A.R.V.I.S.", page.title() == "J.A.R.V.I.S.",
          page.title())
    check("the orb is present and is the PTT control",
          page.locator('[data-testid="orb"]').count() == 1)
    # Nothing overflows horizontally at 1280px.
    page.set_viewport_size({"width": 1280, "height": 860})
    time.sleep(0.3)
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    check("nothing overflows horizontally at 1280px", overflow <= 0, f"{overflow}px over")
    _ = mock


# ---------------------------------------------------------------------------

def main():
    if not (DIST / "index.html").exists():
        print("hud/dist is not built. Run: cd hud && npm ci && npm run build")
        return 2

    mock = MockDaemon(PORT).start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"],
            )
            ctx = browser.new_context(permissions=["microphone"])
            ctx.add_init_script(FAKE_RECOGNIZER)
            page = ctx.new_page()
            page.on("pageerror", lambda e: FAILURES.append(f"page error: {e}"))
            boot(page, mock)

            window_checks(page, mock)
            sidebar_checks(page, mock)
            chat_checks(page, mock)
            safety_render_checks(page, mock)
            input_checks(page, mock)
            dictation_checks(page, mock)
            wake_checks(page, mock)
            approval_checks(page, mock)
            task_checks(page, mock)
            file_checks(page, mock)
            diff_checks(page, mock)
            preview_checks(page, mock)
            panels_checks(page, mock)
            picker_checks(page, mock)

            ctx.close()
            browser.close()
    finally:
        mock.stop()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} of {CHECKS} checks FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {CHECKS} HUD v2 checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
