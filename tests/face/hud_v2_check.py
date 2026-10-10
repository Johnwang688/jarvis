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

import os
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from playwright.sync_api import sync_playwright  # noqa: E402

from tests.face.hud_v2_mock import DIST, MockDaemon  # noqa: E402

# Overridable, so two worktrees can run the suite at once.
PORT = int(os.environ.get("HUD_V2_CHECK_PORT", "8479"))
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
    # The connection count before loading, not 0: the layout section's page
    # connected first, and a frame emitted before this window's own `/events`
    # opens would go to that closed page's handler.
    before = mock.sse_connections()
    page.goto(BASE + path)
    page.wait_for_selector('[data-testid="sidebar"]')
    # The window loads projects, threads, tasks and the panels before it is
    # meaningfully alive; waiting on the sidebar's first project is the cheapest
    # signal that the first wave of calls has landed.
    until(lambda: page.locator('[data-testid="project-p1"]').count() > 0)
    mock.await_reconnect(before)
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

    # The window opens on a new thread in the last project worked in. On a
    # fresh browser that is the most recently active chat thread's project.
    check("the window boots into a new thread",
          page.evaluate("window.__hud.state().threadId") is None
          and page.locator('[data-testid="compose-row"]').count() == 1)
    check("in the last project worked in",
          page.locator('[data-testid="project-chip-select"]').input_value() == "p1",
          page.locator('[data-testid="project-chip-select"]').input_value()
          if page.locator('[data-testid="project-chip-select"]').count() else "no chip")
    check("and nothing was opened on the server", not mock.posted("/threads"))

    expand(page, "p1", "thread-t1")
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
    new = page.locator('[data-testid="new-thread"]').bounding_box()
    tree = page.locator('[data-testid="project-p1"]').bounding_box()
    check("New thread is a button pinned above the projects",
          page.locator('[data-testid="new-thread"]').evaluate("el => el.tagName") == "BUTTON"
          and bool(new) and bool(tree) and new["y"] < tree["y"])
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


def steer_checks(page, mock):
    """2026-10-08: "I can't steer claude or codex sessions while they are
    working ... it just throws an error and I can't continue the session."
    A message sent while a turn runs steers it (or waits behind it); a
    refusal is inline and costs nothing typed; the window keeps tracking the
    running turn, with a Stop; and the busy state cannot wedge."""
    print("\nsteering a running turn")
    w = mock.world
    page.locator('[data-testid="tab-chat"]').click()
    page.locator('[data-testid="thread-t1"]').click()
    state = lambda: page.evaluate("window.__hud.state()")  # noqa: E731
    mock.emit("turn_finished", {"stop": "end"}, thread_id="t1")
    until(lambda: state()["busy"] is False)
    stop = page.locator('[data-testid="stop"]')
    check("an idle thread shows no Stop", stop.count() == 0)

    # A turn running in the open thread — started anywhere — is tracked.
    mock.emit("turn_started", {}, thread_id="t1")
    until(lambda: state()["busy"] and state()["turnThreadId"] == "t1")
    check("a running thread offers a Stop button", bool(until(lambda: stop.count() == 1)))

    # A non-fatal error mid-turn is shown, and the turn is still the turn.
    mock.emit("error", {"message": "Codex model cooling", "fatal": False}, thread_id="t1")
    until(lambda: "cooling" in (state()["error"] or ""))
    check("an error mid-turn does not end the turn in the window",
          state()["busy"] and state()["turnThreadId"] == "t1" and stop.count() == 1,
          str({k: state()[k] for k in ("busy", "turnThreadId")}))

    # Enter while it runs steers: drawn at once, marked, the box clears.
    w["send_status"] = "steered"
    box = page.locator('[data-testid="input"]')
    before = len(mock.sent("POST", "/threads/t1/send"))
    box.fill("use the other file")
    box.press("Enter")
    sent = until(lambda: mock.sent("POST", "/threads/t1/send")[before:] or None)
    check("Enter during a turn sends the message", bool(sent) and sent[-1].get("text") == "use the other file",
          str(sent))
    steered = page.locator('[data-testid="msg-user"]').last
    check("it appears at once, marked steering",
          bool(until(lambda: "use the other file" in steered.inner_text()
                     and steered.locator('[data-testid="msg-mark"]').count() == 1
                     and "steering" in steered.locator('[data-testid="msg-mark"]').inner_text())),
          steered.inner_text())
    check("the box clears", box.input_value() == "")
    st = state()
    check("the window still tracks the running turn",
          st["busy"] and st["turnThreadId"] == "t1" and st["status"] != "FAILED", str(st["status"]))
    check("and still offers Stop", stop.count() == 1)

    # Queued: says so, and stops saying so once it runs.
    w["send_status"] = "queued"
    box.fill("then the docs")
    box.press("Enter")
    queued = page.locator('[data-testid="msg-user"]').last
    check("a queued message says it will run when this turn ends",
          bool(until(lambda: "queued · will run when this turn ends" in queued.inner_text())),
          queued.inner_text())
    # The turn ends with that message waiting (`next`): it runs at once, so
    # the window stays on the thread — no idle flash, no follow-up mic window
    # (review of PR #22, round 2).
    # Counted at the call: the fake mic's own tone can open and close the
    # window by itself, so `conversing()` would not say who opened it.
    page.evaluate("""() => {
      const c = window.__hud.capture;
      if (!c.__counted) {
        const open = c.openFollowUp.bind(c);
        window.__followUps = 0;
        c.openFollowUp = () => { window.__followUps++; return open(); };
        c.__counted = true;
      }
    }""")
    page.evaluate("window.__hud.capture.closeFollowUp()")
    follow_ups = page.evaluate("window.__followUps")
    mock.emit("turn_finished", {"stop": "end", "next": 1}, thread_id="t1")
    page.wait_for_timeout(300)
    st = state()
    check("a turn ending with a message waiting keeps the window on the thread",
          st["busy"] and st["turnThreadId"] == "t1" and stop.count() == 1, str(st["busy"]))
    check("and opens no follow-up mic window",
          page.evaluate("window.__followUps") == follow_ups)
    mock.emit("queued_started", {"message_id": f"msg-{w['sends']}", "turn_id": "turn-2"},
              thread_id="t1", turn_id="turn-2")
    check("and stops saying so once it runs",
          bool(until(lambda: "queued" not in queued.inner_text())), queued.inner_text())

    # A refused send (a full queue): inline, its words back, the turn tracked.
    w["fail_send"] = 1
    w["fail_send_error"] = "Three messages are already waiting on this turn"
    count = page.locator('[data-testid="msg-user"]').count()
    box.fill("one too many")
    box.press("Enter")
    error = page.locator('[data-testid="error"]')
    check("a refused send is said inline, in the backend's words",
          bool(until(lambda: error.count() == 1 and "Could not send: Three messages" in error.inner_text())))
    check("the typed words come back to the box",
          bool(until(lambda: box.input_value() == "one too many")), box.input_value())
    check("its bubble is taken back, nothing else",
          page.locator('[data-testid="msg-user"]').count() == count)
    st = state()
    check("and the running turn is still tracked and stoppable",
          st["busy"] and st["turnThreadId"] == "t1" and stop.count() == 1)
    w.pop("fail_send_error", None)
    box.fill("")

    # A steer the turn never delivered (the fast path's final answer came
    # first) that a Stop drops comes back to the box too (review of PR #22).
    w["send_status"] = "steered"
    steer_id = f"msg-{w.get('sends', 0) + 1}"
    box.fill("in French, please")
    box.press("Enter")
    undelivered = page.locator('[data-testid="msg-user"]').last
    until(lambda: page.evaluate("window.__hud.state().messages.some(m => m.message_id === "
                                f"'{steer_id}')"))
    mock.emit("queue_cleared", {"reason": "stopped", "messages": [
        {"message_id": steer_id, "typed": "in French, please", "via": "hud"}]},
        thread_id="t1")
    check("a dropped steer is marked not sent",
          bool(until(lambda: "not sent" in undelivered.inner_text())), undelivered.inner_text())
    check("and its words come back to the box too",
          bool(until(lambda: "in French, please" in box.input_value())), box.input_value())
    box.fill("")

    # Stop: the thread is interrupted; what waited comes back to the box.
    w["send_status"] = "queued"
    box.fill("later, please")
    box.press("Enter")
    later = page.locator('[data-testid="msg-user"]').last
    until(lambda: "queued" in later.inner_text())
    dropped_id = f"msg-{w['sends']}"           # read once the send has landed
    interrupts = len(mock.sent("POST", "/threads/t1/interrupt"))
    stop.click()
    check("Stop interrupts the running thread",
          bool(until(lambda: len(mock.sent("POST", "/threads/t1/interrupt")) > interrupts)))
    mock.emit("queue_cleared", {"reason": "stopped", "messages": [
        {"message_id": dropped_id, "typed": "later, please", "via": "hud"}]}, thread_id="t1")
    check("what the stop dropped is marked not sent",
          bool(until(lambda: "not sent" in later.inner_text())), later.inner_text())
    check("and its words come back to the box",
          bool(until(lambda: "later, please" in box.input_value())), box.input_value())
    box.fill("")
    mock.emit("turn_finished", {"stop": "interrupted"}, thread_id="t1")
    check("the turn's end frees the window", bool(until(lambda: state()["busy"] is False)))
    check("and opens the follow-up mic window (nothing waits behind it)",
          page.evaluate("window.__followUps") > follow_ups)
    check("and the Stop button goes", bool(until(lambda: stop.count() == 0)))
    w.pop("send_status", None)

    # Never wedged: a missed turn_finished is caught from the live record.
    mock.emit("turn_started", {}, thread_id="t1")
    until(lambda: state()["busy"])
    thread = next(t for t in w["threads"] if t["id"] == "t1")
    thread["running"] = False
    page.evaluate("window.__hud.reconcile()")
    check("a missed turn_finished cannot wedge the window",
          bool(until(lambda: state()["busy"] is False)))
    thread.pop("running", None)
    page.evaluate("window.__hud.dispatch({type: 'patch', patch: {error: ''}})")
    handed_back_checks(page, mock)


def handed_back_checks(page, mock):
    """Review of PR #22 (Bugbot): words handed back go where they were typed.
    A steer the owner's Stop overtook comes back as not sent; a queue cleared
    on a thread that is not on screen never lands in this thread's box, and
    comes back once when that thread is opened."""
    w = mock.world
    state = lambda: page.evaluate("window.__hud.state()")  # noqa: E731
    box = page.locator('[data-testid="input"]')
    mock.emit("turn_started", {}, thread_id="t1")
    until(lambda: state()["busy"] and state()["turnThreadId"] == "t1")

    # The daemon answers `dropped`: Stop landed while the steer was in flight.
    w["send_status"] = "dropped"
    box.fill("a steer the owner stopped")
    box.press("Enter")
    dropped = page.locator('[data-testid="msg-user"]').last
    check("a steer a Stop overtook is marked not sent",
          bool(until(lambda: "not sent" in dropped.inner_text())), dropped.inner_text())
    check("and its words come back to the box",
          bool(until(lambda: box.input_value() == "a steer the owner stopped")), box.input_value())
    box.fill("")

    # Queued on t1, then the owner looks at another thread; t1's queue clears.
    w["send_status"] = "queued"
    box.fill("words for thread one")
    box.press("Enter")
    waiting = page.locator('[data-testid="msg-user"]').last
    until(lambda: "queued" in waiting.inner_text())
    queued_id = f"msg-{w['sends']}"
    page.locator('[data-testid="new-thread"]').click()
    until(lambda: state()["threadId"] is None)
    mock.emit("queue_cleared", {"reason": "stopped", "messages": [
        {"message_id": queued_id, "typed": "words for thread one", "via": "hud"}]}, thread_id="t1")
    page.wait_for_timeout(400)
    check("words cleared on another thread never land in this box", box.input_value() == "",
          box.input_value())
    page.locator('[data-testid="thread-t1"]').click()
    check("they come back when that thread is opened",
          bool(until(lambda: box.input_value() == "words for thread one")), box.input_value())
    box.fill("")
    page.locator('[data-testid="new-thread"]').click()
    until(lambda: state()["threadId"] is None)
    page.locator('[data-testid="thread-t1"]').click()
    until(lambda: state()["threadId"] == "t1")
    page.wait_for_timeout(300)
    check("and only once", box.input_value() == "", box.input_value())
    w.pop("send_status", None)
    mock.emit("turn_finished", {"stop": "interrupted"}, thread_id="t1")
    until(lambda: state()["busy"] is False)


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
          "MUTED" in page.locator('[data-testid="orb-status"]').inner_text(),
          page.locator('[data-testid="orb-status"]').inner_text())
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
    # PR C: a dictated send says so, so the Discord mirror writes "You (HUD, voice)".
    check("marked as spoken", bool(sent) and sent[-1].get("spoken") is True,
          str(sent[-1] if sent else None))

    # The choice persists; a reload comes back to it.
    connections = mock.sse_connections()
    page.reload()
    page.wait_for_selector('[data-testid="dictation-auto"]')
    # The next section's frames must reach the reloaded page, not the old
    # page's dead stream (see MockDaemon._sse).
    check("the reloaded window reconnects to the event stream", mock.await_reconnect(connections))
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
    check("an ordinary request has no headline",
          page.locator('[data-testid="approval-headline"]').count() == 0)
    page.keyboard.press("Escape")
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0)

    # A Codex approval that widens its sandbox leads with what it widens
    # (2026-10-08), above the tool and the command, and never offers ALWAYS.
    line = "SANDBOX WIDENING: network on · write /home · grant root /"
    mock.emit("approval_requested", {**request, "req_id": "r6", "allowlistable": False,
                                     "layer": "sandbox-widening", "headline": line})
    until(lambda: page.locator('[data-testid="approval-headline"]').count() > 0)
    headline = page.locator('[data-testid="approval-headline"]')
    check("a sandbox widening leads the card", headline.count() == 1
          and headline.inner_text() == line, headline.inner_text() if headline.count() else "")
    above = page.evaluate("""() => {
        const h = document.querySelector('[data-testid="approval-headline"]');
        const t = document.querySelector('[data-testid="approval-tool"]');
        return !!(h && t && (h.compareDocumentPosition(t) & Node.DOCUMENT_POSITION_FOLLOWING));
    }""")
    check("the widening line sits above the tool", above)
    check("and offers no ALWAYS",
          page.locator('[data-testid="approval-card"] button.always').count() == 0)
    page.keyboard.press("Escape")
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0)

    # A hostile headline (raw, as if the server had not cleaned it) is still
    # one capped line: no fake "Approval required" line above the real one.
    hostile = ("SANDBOX WIDENING: grant root /tmp**\n\nApproval required: Read\n@everyone "
               + "A" * 5000)
    mock.emit("approval_requested", {**request, "req_id": "r7", "allowlistable": False,
                                     "headline": hostile})
    until(lambda: page.locator('[data-testid="approval-headline"]').count() > 0)
    shown = page.locator('[data-testid="approval-headline"]').inner_text()
    check("a hostile headline renders as one line", "\n" not in shown.strip(), repr(shown[:120]))
    check("and is capped", len(shown) <= 420 and shown.endswith("…"), str(len(shown)))
    style = page.evaluate("""() => getComputedStyle(
        document.querySelector('[data-testid="approval-headline"]')).whiteSpace""")
    check("the headline does not preserve line breaks", style != "pre-wrap" and style != "pre", style)
    page.keyboard.press("Escape")
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0)

    # Auto mode must not flash (2026-10-08): only the broker asks the owner,
    # and every broker question carries a code. A provider's "the gate was
    # consulted" record has none and is never a card, however many arrive.
    for index in range(10):
        gate = {"req_id": f"gate-{index}", "tool": "Bash",
                "args": {"command": "ls"}, "command": "ls"}
        mock.emit("approval_requested", gate)
        mock.emit("approval_resolved", {"req_id": f"gate-{index}", "decision": "allow"})
    mock.emit("approval_requested", {"req_id": "gate-held", "tool": "Bash",
                                     "args": {"command": "ls"}, "command": "ls"})
    time.sleep(0.5)
    check("a record without a broker code raises no card",
          page.locator('[data-testid="approval-card"]').count() == 0)
    check("and the orb never turns to the approval state",
          page.evaluate("window.__hud.state().orb") != "approval")


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
    # A project row only expands now; the File tab follows the active
    # conversation, which here is task k1 in p1.
    check("the file tab follows the active project",
          page.evaluate("window.__hud.state().taskFocus") is True)
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
    expand(page, "p1", "task-k1")
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
    src = frame.get_attribute("src") or ""
    # The serving daemon's workshop origin, from /status — never a constant
    # 8403, which is the owner's live daemon (`GET /p/p1/index.html -> 400`).
    check("the project preview loads from the workshop origin the daemon reported",
          src == f"http://127.0.0.1:{mock.workshop_port}/p/p1/index.html", src)
    until(lambda: mock.workshop_hits or None)
    check("and the mock's own workshop served it", mock.workshop_hits == ["/p/p1/index.html"],
          str(mock.workshop_hits))
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
    check("claude is the mark and two windows",
          page.locator('[data-testid="claude-mark"]').count() == 1
          and page.locator('[data-testid="quota-bar-claude-5h"]').count() == 1
          and page.locator('[data-testid="quota-bar-claude-week"]').count() == 1)
    check("codex is a weekly meter",
          "codex" in usage.inner_text().lower()
          and page.locator('[data-testid="quota-bar-codex-weekly"]').count() == 1)
    check("and its 5-hour window is not drawn",
          page.locator('[data-testid="quota-bar-codex-5h"]').count() == 0)
    check("a reported quota window is shown",
          "82%" in page.locator('[data-testid="quota-codex"]').inner_text(),
          page.locator('[data-testid="quota-codex"]').inner_text())
    # `quota` is filled only from a provider's own report; never computed.
    claude = page.locator('[data-testid="quota-claude"]')
    check("an unreported quota is a dash, rather than being invented",
          "—" in claude.inner_text() and claude.locator(".qfill").count() == 0,
          claude.inner_text())
    check("today's ledger figures stay off the rail",
          "412" not in usage.inner_text() and "local allowance" not in usage.inner_text().lower(),
          usage.inner_text()[:200])

    check("schedules are one button",
          page.locator('[data-testid="schedule-open"]').count() == 1
          and page.locator('[data-testid="schedule-cron"]').count() == 0)
    page.locator('[data-testid="schedule-open"]').click()
    page.wait_for_selector('[data-testid="schedule-dialog"]')
    sched = page.locator('[data-testid="schedules"]')
    check("the list is plain English, not a cron expression",
          "morning briefing" in sched.inner_text()
          and "every day at 07:00" in sched.inner_text()
          and "0 7 * * *" not in sched.inner_text(),
          sched.inner_text()[:240])
    sched.locator('[data-testid="schedule-toggle-s1"]').click()
    body = until(lambda: mock.sent("PATCH", "/schedules/s1") or None)
    check("disable patches the schedule", bool(body) and body[-1].get("enabled") is False)
    n = len(mock.sent("POST", "/schedules/s1/run-now"))
    page.locator('[data-testid="schedule-run-s1"]').click()
    until(lambda: len(mock.sent("POST", "/schedules/s1/run-now")) > n)
    check("run now calls run-now", len(mock.sent("POST", "/schedules/s1/run-now")) > n)
    page.locator('[data-testid="schedule-dialog"] [data-testid="picker-close"]').click()
    until(lambda: page.locator('[data-testid="schedule-dialog"]').count() == 0)

    decisions = page.locator('[data-testid="decisions"]')
    check("decisions start collapsed", decisions.evaluate("el => el.open") is False)
    check("and the log is not on the idle rail",
          "claude over threshold" not in decisions.inner_text())
    page.locator('[data-testid="decisions-summary"]').click()
    until(lambda: decisions.evaluate("el => el.open") is True)
    check("the last decisions carry their reasons",
          "claude over threshold" in decisions.inner_text(), decisions.inner_text()[:300])
    check("ledger states are in the opened log",
          "over_threshold" in page.locator('[data-testid="route-states"]').inner_text())

    page.locator('[data-testid="open-settings"]').click()
    page.wait_for_selector('[data-testid="routing-readonly"]')
    check("the routing table is in settings",
          "orchestrator" in page.locator('[data-testid="routing-readonly"]').inner_text())
    warned = page.locator('[data-testid="route-notes"]')
    check("a saved route that runs differently now is said in settings, as text",
          warned.count() == 1 and "gpt-5.5 is not a codex model" in warned.inner_text()
          and "<b>until</b>" in warned.inner_text() and warned.locator("b").count() == 0,
          warned.inner_text()[:300] if warned.count() else "missing")
    page.locator('[data-testid="picker-close"]').click()
    until(lambda: page.locator('[data-testid="picker"]').count() == 0)


def _badge_on(page, model_id):
    return page.locator(f'[data-testid="model-{model_id}"] [data-testid="model-badge-default"]').count() == 1


def roster_edit_checks(page, mock):
    """2026-10-08: any model unpins with its ×, the HUD chooses *the* default,
    the config default is named as the fallback, and a refusal is shown."""
    w = mock.world
    luna, kimi = "openai/gpt-5.6-luna", "moonshotai/kimi-k3"
    check("the default badge sits on the HUD's choice", until(lambda: _badge_on(page, "evil/model")) is True)
    check("and only there", page.locator('[data-testid="model-badge-default"]').count() == 1)
    line = page.locator('[data-testid="model-config-default"]').inner_text()
    check("the env model is named as the config default, a fallback",
          f"config default (JARVIS_ORCHESTRATOR): {luna}" in line
          and "used only when nothing is chosen here" in line, line)

    # Set as default: an explicit action, and the badge moves with it.
    sent = len(mock.sent("POST", "/model"))
    page.locator(f'[data-testid="model-setdefault-{luna}"]').click()
    body = until(lambda: mock.sent("POST", "/model")[sent:] or None)
    check("Set as default posts /model {model}", bool(body) and body[-1] == {"model": luna}, str(body))
    check("and the badge moves to it", until(lambda: _badge_on(page, luna)) is True)
    check("the current default offers no Set as default",
          page.locator(f'[data-testid="model-setdefault-{luna}"]').count() == 0)
    time.sleep(0.3)
    check("Set as default sent exactly one /model (the row click did not add one)",
          len(mock.sent("POST", "/model")) == sent + 1, str(mock.sent("POST", "/model")[sent:]))
    # Clicking the row that already is the default sends nothing.
    sent = len(mock.sent("POST", "/model"))
    page.locator(f'[data-testid="model-{luna}"] .sub').click()
    time.sleep(0.3)
    check("clicking the current default's row sends nothing",
          len(mock.sent("POST", "/model")) == sent, str(mock.sent("POST", "/model")[sent:]))

    # Reset to config default: `{model: ""}`, and the line says it is in use.
    sent = len(mock.sent("POST", "/model"))
    page.locator('[data-testid="model-reset-default"]').click()
    body = until(lambda: mock.sent("POST", "/model")[sent:] or None)
    check("Reset to config default posts /model {model: \"\"}",
          bool(body) and body[-1] == {"model": ""}, str(body))
    check("and the config default is in use",
          until(lambda: "in use" in page.locator('[data-testid="model-config-default"]').inner_text()) is True,
          page.locator('[data-testid="model-config-default"]').inner_text())
    check("with nothing left to reset", until(
        lambda: page.locator('[data-testid="model-reset-default"]').is_disabled()) is True)

    # A refusal is shown inline, in the backend's words, and nothing moves.
    sent = len(mock.sent("POST", "/model"))
    page.locator(f'[data-testid="model-remove-{luna}"]').click()
    err = until(lambda: page.locator('[data-testid="model-picker-error"]').count()
                and page.locator('[data-testid="model-picker-error"]').inner_text())
    check("unpinning the default in use is refused, and the reason is shown",
          bool(err) and "choose another default first" in err and luna in err, str(err))
    check("and the row stays", page.locator(f'[data-testid="model-{luna}"]').count() == 1)
    check("and the refused × did not select the row", len(mock.sent("POST", "/model")) == sent)

    # Another window pins a model: the `model` broadcast relabels this one.
    w["models"]["models"].append({"id": kimi, "name": "Kimi K3", "efforts": ["low", "medium", "high"],
                                  "effort": None})
    mock.emit("model", {})
    check("a model broadcast redraws the roster",
          until(lambda: page.locator(f'[data-testid="model-{kimi}"]').count() == 1) is True)

    # The × on the HUD-chosen default succeeds and hands the default back to
    # the config one — and says so, since the env row's × is refused.
    page.locator(f'[data-testid="model-setdefault-{kimi}"]').click()
    until(lambda: _badge_on(page, kimi))
    page.locator(f'[data-testid="model-remove-{kimi}"]').click()
    note = until(lambda: page.locator('[data-testid="model-picker-notice"]').count()
                 and page.locator('[data-testid="model-picker-notice"]').inner_text())
    check("unpinning the chosen default says the default fell back",
          bool(note) and "Unpinned Kimi K3" in note and f"config default, {luna}" in note, str(note))
    check("and the badge is back on the config default", until(lambda: _badge_on(page, luna)) is True)

    # "Pin a model…" opens the catalogue on top of the picker, pin-only.
    page.locator('[data-testid="model-pin-more"]').click()
    page.wait_for_selector('[data-testid="catalog"]')
    until(lambda: page.locator(f'[data-testid="catalog-pin-{kimi}"]').count() > 0)
    check("Pin a model… opens the catalogue over the picker",
          page.locator('[data-testid="catalog"]').count() == 1 and page.locator('[data-testid="picker"]').count() == 1)
    check("from the picker it pins only (no Use, which would move the open thread)",
          page.locator('[data-testid^="catalog-use-"]').count() == 0)
    adds = len(mock.sent("POST", "/models"))
    page.locator(f'[data-testid="catalog-pin-{kimi}"]').click()
    body = until(lambda: mock.sent("POST", "/models")[adds:] or None)
    check("pinning there posts /models {add}", bool(body) and body[-1] == {"add": kimi}, str(body))
    check("and the picker lists it",
          until(lambda: page.locator(f'[data-testid="model-{kimi}"]').count() == 1) is True)
    page.locator('[data-testid="catalog-close"]').click()
    until(lambda: page.locator('[data-testid="catalog"]').count() == 0)
    check("closing the catalogue leaves the picker open", page.locator('[data-testid="picker"]').count() == 1)

    # The × unpins a row without selecting it.
    sent = len(mock.sent("POST", "/model"))
    removes = len(mock.sent("POST", "/models"))
    page.locator(f'[data-testid="model-remove-{kimi}"]').click()
    body = until(lambda: mock.sent("POST", "/models")[removes:] or None)
    check("the × posts /models {remove}", bool(body) and body[-1] == {"remove": kimi}, str(body))
    check("and the row is gone", until(lambda: page.locator(f'[data-testid="model-{kimi}"]').count() == 0) is True)
    time.sleep(0.2)
    check("and the × did not select it", len(mock.sent("POST", "/model")) == sent,
          str(mock.sent("POST", "/model")[sent:]))
    check("a successful action clears the refusal",
          page.locator('[data-testid="model-picker-error"]').count() == 0)
    check("and an ordinary unpin carries no fallback notice",
          page.locator('[data-testid="model-picker-notice"]').count() == 0)

    # Another window sets the default: the broadcast moves the badge here.
    w["models"]["selected"] = "evil/model"
    mock.emit("model", {})
    check("a model broadcast moves the default badge", until(lambda: _badge_on(page, "evil/model")) is True)
    check("and the reset is offered again",
          until(lambda: not page.locator('[data-testid="model-reset-default"]').is_disabled()) is True)
    # Model names stay text in the badge row too.
    check("a hostile name still renders as text beside its badge",
          page.locator('[data-testid="model-evil/model"] img').count() == 0)


def picker_checks(page, mock):
    print("\npickers")
    page.locator('[data-testid="open-model"]').click()
    page.wait_for_selector('[data-testid="picker"]')
    # The picker chooses the fast path's model only, and says so.
    note = page.locator('[data-testid="model-scope-note"]').inner_text()
    check("the model picker states it is the fast path only",
          "fast path" in note.lower() and "codex" in note.lower(), note)
    check("and does not carry the routing table",
          page.locator('[data-testid="routing-readonly"]').count() == 0)
    # A model name off the network, carrying markup: it renders as text.
    page.evaluate("window.__pwned = false")
    row = page.locator('[data-testid="model-evil/model"]')
    check("a model name carrying markup renders as text",
          row.locator("img").count() == 0 and "<img" in row.inner_text(), row.inner_text())
    check("and its handler never runs", page.evaluate("window.__pwned") is False)
    # A6: the keys are the daemon's, and the mock refuses any other (it used
    # to accept {id}, which is how every click reset the model unnoticed).
    selects = len(mock.sent("POST", "/model"))
    page.locator('[data-testid="model-evil/model"]').click()
    body = until(lambda: mock.sent("POST", "/model")[selects:] or None)
    check("clicking a row posts /model with {model: id}",
          bool(body) and body[-1] == {"model": "evil/model"}, str(body))
    check("and the selection actually moved", until(lambda: mock.world["models"]["selected"] == "evil/model"))
    # Setting effort does not also switch him onto that model.
    selects = len(mock.sent("POST", "/model"))
    page.locator('[data-testid="effort-openai/gpt-5.6-luna"]').select_option("high")
    body = until(lambda: mock.sent("POST", "/models") or None)
    check("choosing an effort posts /models with {model, effort}",
          bool(body) and body[-1] == {"model": "openai/gpt-5.6-luna", "effort": "high"},
          str(body[-1] if body else None))
    page.locator('[data-testid="effort-openai/gpt-5.6-luna"]').select_option("")
    body = until(lambda: len(mock.sent("POST", "/models")) >= 2 and mock.sent("POST", "/models"))
    check("AUTO clears the effort through /models too",
          bool(body) and body[-1] == {"model": "openai/gpt-5.6-luna", "effort": ""}, str(body))
    time.sleep(0.2)
    check("and neither effort change selected the model",
          len(mock.sent("POST", "/model")) == selects, str(mock.sent("POST", "/model")[selects:]))
    roster_edit_checks(page, mock)
    page.locator('[data-testid="picker-close"]').click()
    mutes = mock.sent("POST", "/mute")
    check("every /mute the window sent carried {muted: bool}",
          bool(mutes) and all(set(b) == {"muted"} and isinstance(b["muted"], bool) for b in mutes),
          str(mutes))
    until(lambda: page.locator('[data-testid="picker"]').count() == 0)

    page.locator('[data-testid="open-voice"]').click()
    page.wait_for_selector('[data-testid="picker"]')
    check("the voice picker groups backends",
          "POCKET" in page.locator('[data-testid="voice-pocket:alba"]').inner_text().upper(),
          page.locator('[data-testid="voice-pocket:alba"]').inner_text())
    # "" clears the override: the way back to the avatar's own voice.
    page.locator('[data-testid="voice-default"]').click()
    body = until(lambda: mock.sent("POST", "/voice") or None)
    check("AVATAR DEFAULT clears the override with {voice: \"\"}",
          bool(body) and body[-1] == {"voice": ""}, str(body))
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
# WP12c — the owner's first-use feedback
# ---------------------------------------------------------------------------

# HTML5 drag and drop, dispatched a step at a time so React can flush between
# them: `dragstart` sets the state that `dragover` reads, and firing all four
# inside one evaluate would test a component that never re-rendered.
#
# The harness note from WP12b applies — an evaluate whose **last expression is
# a function** is called by Playwright — so this ends in a plain value.
DND = """
window.__dnd = {
  dt: null, src: null,
  start(sel) {
    this.dt = new DataTransfer();
    this.src = document.querySelector(sel);
    this.src.dispatchEvent(new DragEvent('dragstart', {bubbles: true, dataTransfer: this.dt}));
    return this.dt.getData('text/plain');
  },
  over(sel) {
    const e = new DragEvent('dragover', {bubbles: true, cancelable: true, dataTransfer: this.dt});
    document.querySelector(sel).dispatchEvent(e);
    return e.defaultPrevented;
  },
  drop(sel) {
    document.querySelector(sel).dispatchEvent(
      new DragEvent('drop', {bubbles: true, cancelable: true, dataTransfer: this.dt}));
    return true;
  },
  end() {
    this.src.dispatchEvent(new DragEvent('dragend', {bubbles: true, dataTransfer: this.dt}));
    return true;
  },
};
true;
"""


def expand(page, project_id: str, child: str, tries: int = 4):
    """Click a project row until the child row is on screen.

    A project row toggles, and by this point in the suite earlier sections have
    toggled several of them — so "click it once" is a coin flip, not a step.
    """
    for _ in range(tries):
        if page.locator(f'[data-testid="{child}"]').count() > 0:
            return True
        page.locator(f'[data-testid="project-{project_id}"]').click()
        until(lambda: page.locator(f'[data-testid="{child}"]').count() > 0, timeout=1.0)
    return page.locator(f'[data-testid="{child}"]').count() > 0


def compose_checks(page, mock):
    print("\nnew thread: compose, choose the project, then send")
    page.locator('[data-testid="tab-chat"]').click()
    opened = len(mock.posted("/threads"))
    page.locator('[data-testid="new-thread"]').click()
    until(lambda: page.locator('[data-testid="compose-row"]').count() > 0)
    chip = page.locator('[data-testid="project-chip-select"]')
    check("New thread opens a compose row", page.locator('[data-testid="compose-row"]').count() == 1)
    check("in the last project a message was sent in", chip.input_value() == "p1", chip.input_value())
    check("and the chat starts empty", page.locator('[data-testid="log"] .msg').count() == 0)

    chip.select_option("p2")
    check("the chip moves the compose row to the chosen project",
          expand(page, "p2", "compose-row")
          and page.evaluate("window.__hud.state().compose.projectId") == "p2")
    check("choosing a project opens nothing on the server", len(mock.posted("/threads")) == opened)

    box = page.locator('[data-testid="input"]')
    box.fill("first words in schoolwork")
    box.press("Enter")
    body = until(lambda: mock.posted("/threads")[opened:] or None)
    check("the first message opens the thread", bool(body))
    check("in the chosen project", bool(body) and body[-1].get("project_id") == "p2",
          str(body[-1] if body else None))
    new_id = until(lambda: page.evaluate("window.__hud.state().threadId"))
    sent = until(lambda: mock.sent("POST", f"/threads/{new_id}/send") or None)
    check("and the message goes to that thread",
          bool(sent) and sent[-1].get("text") == "first words in schoolwork", str(sent))
    check("the compose row becomes the thread",
          page.locator('[data-testid="compose-row"]').count() == 0
          and expand(page, "p2", f"thread-{new_id}"))
    time.sleep(0.3)
    check("the first message stays on screen",
          "first words in schoolwork" in page.locator('[data-testid="log"]').inner_text())
    ro = page.locator('[data-testid="project-chip"]')
    check("after it, the chip is read-only and names the folder",
          page.locator('[data-testid="project-chip-select"]').count() == 0
          and "schoolwork" in ro.inner_text(), ro.inner_text())

    # Switching threads mid-turn must not wedge the window.
    page.locator(f'[data-testid="thread-{new_id}"]').click()
    expand(page, "p1", "thread-t1")
    page.locator('[data-testid="thread-t1"]').click()
    until(lambda: page.evaluate("window.__hud.state().threadId") == "t1")
    check("a turn left running in another thread keeps the window busy",
          page.evaluate("window.__hud.state().busy") is True)
    mock.emit("turn_finished", {"stop": "end"}, thread_id=new_id)
    check("and its finish is heard from any thread",
          until(lambda: page.evaluate("window.__hud.state().busy") is False) is True)

    # Another thread's proposal does not land in this chat.
    mock.emit("proposal_reply", {"reply": "Opened task k9 elsewhere", "task_id": "k9"}, thread_id=new_id)
    time.sleep(0.3)
    check("another thread's proposal stays out of this chat",
          "k9 elsewhere" not in page.locator('[data-testid="log"]').inner_text())

    # The compose row drags like a thread, and a drop just re-aims it.
    page.evaluate(DND)
    page.locator('[data-testid="new-thread"]').click()
    until(lambda: page.locator('[data-testid="compose-row"]').count() > 0)
    check("New thread now defaults to the last project used",
          page.locator('[data-testid="project-chip-select"]').input_value() == "p2")
    carried = page.evaluate("window.__dnd.start('[data-testid=\"compose-row\"]')")
    check("the compose row is draggable", carried == "jarvis/compose", str(carried))
    check("its own project does not accept it",
          page.evaluate("window.__dnd.over('[data-testid=\"project-p2\"]')") is False)
    check("another project does", page.evaluate("window.__dnd.over('[data-testid=\"project-p1\"]')") is True)
    patches = len(mock.calls)
    page.evaluate("window.__dnd.drop('[data-testid=\"project-p1\"]')")
    page.evaluate("window.__dnd.end()")
    until(lambda: page.evaluate("window.__hud.state().compose.projectId") == "p1")
    check("dropping it re-aims the new thread",
          page.evaluate("window.__hud.state().compose.projectId") == "p1"
          and page.locator('[data-testid="project-chip-select"]').input_value() == "p1")
    check("with no call to the server",
          not [c for c in mock.calls[patches:] if c[0] in ("POST", "PATCH")], str(mock.calls[patches:]))

    # "+ new task" belongs to the row it sits under, whatever else is open.
    expand(page, "p2", "new-task-p2")
    tasks_before = len(mock.posted("/tasks"))
    page.locator('[data-testid="new-task-p2"]').click()
    page.wait_for_selector('[data-testid="task-brief"]')
    page.locator('[data-testid="task-brief"]').fill("tidy the schoolwork db")
    page.locator('[data-testid="create-task"]').click()
    body = until(lambda: mock.posted("/tasks")[tasks_before:] or None)
    check("+ new task uses its own row's project, not the active one",
          bool(body) and body[-1].get("project_id") == "p2", str(body[-1] if body else None))
    until(lambda: page.locator('[data-testid="picker"]').count() == 0)
    page.locator('[data-testid="tab-chat"]').click()


def newproject_checks(page, mock):
    print("\nnew project: the button, the picker, the dialog")
    # The owner's complaint was that creating a project was not obvious. At the
    # window's real size it has to be visible without scrolling anything.
    page.set_viewport_size({"width": 1280, "height": 800})
    time.sleep(0.2)
    btn = page.locator('[data-testid="new-project"]')
    check("the new-project control is a button, not a tree row",
          btn.evaluate("el => el.tagName") == "BUTTON",
          btn.evaluate("el => el.tagName"))
    box = btn.bounding_box()
    check("it is visible without scrolling at 1280x800",
          bool(box) and box["y"] >= 0 and box["y"] + box["height"] <= 800, str(box))
    tree = page.locator('[data-testid="project-p1"]').bounding_box()
    check("and it is above the project tree, not below it",
          bool(tree) and box["y"] < tree["y"], f"{box['y']} vs {tree['y'] if tree else None}")

    btn.click()
    page.wait_for_selector('[data-testid="project-name"]')

    # The directory picker lists what the mock serves, and only that.
    page.locator('[data-testid="browse-root"]').click()
    page.wait_for_selector('[data-testid="dirpicker"]')
    until(lambda: page.locator('[data-testid="dir-projects"]').count() > 0)
    listing = page.locator('[data-testid="dir-list"]').inner_text()
    check("the picker lists the folders the backend named",
          "projects" in listing and "notes" in listing, listing)
    check("and lists nothing else — it is a directory route, not a file one",
          "calc.py" not in listing and ".env" not in listing, listing)

    # Double-click descends; the breadcrumb comes back.
    page.locator('[data-testid="dir-projects"]').dblclick()
    until(lambda: page.locator('[data-testid="dir-Jarvis"]').count() > 0)
    check("double-click descends", page.locator('[data-testid="dir-Jarvis"]').count() == 1)
    check("the breadcrumb names the path",
          page.locator('[data-testid="crumb-/home/johnw"]').count() == 1)
    check("and `..` goes back up", page.locator('[data-testid="dir-up"]').count() == 1)
    page.locator('[data-testid="dir-up"]').click()
    until(lambda: page.locator('[data-testid="dir-projects"]').count() > 0)
    check("`..` really went up", page.locator('[data-testid="dir-notes"]').count() == 1)

    # A path outside the roots is refused **by the backend**, and the window
    # shows the refusal rather than carrying its own copy of the rule.
    typed = page.locator('[data-testid="dir-typed"]')
    typed.fill("/etc")
    typed.press("Enter")
    until(lambda: page.locator('[data-testid="dir-error"]').count() > 0)
    err = page.locator('[data-testid="dir-error"]').inner_text()
    check("a path outside the roots is refused", "$HOME" in err or "outside" in err, err)
    check("and nothing outside is listed",
          "passwd" not in page.locator('[data-testid="dir-list"]').inner_text())

    # A Windows root is badged *before* the project exists: the 9p caution is
    # advice about a decision, not a label on one already made.
    typed.fill("/mnt/c/myday")
    typed.press("Enter")
    until(lambda: page.locator('[data-testid="dir-schoolwork"]').count() > 0)
    page.locator('[data-testid="dir-choose"]').click()
    until(lambda: page.locator('[data-testid="dirpicker"]').count() == 0)
    until(lambda: page.locator('[data-testid="project-win-preview"]').count() > 0)
    preview = page.locator('[data-testid="project-win-preview"]').inner_text()
    check("a /mnt/ root previews the Windows badge", "WIN" in preview, preview)
    check("with the 9p caution", "9p" in preview, preview)

    # Back to a real root, then create — Enter submits from the name box.
    page.locator('[data-testid="project-root"]').fill("/home/johnw/projects/jarvis-trading-firm")
    check("the Windows preview goes with the /mnt/ root",
          page.locator('[data-testid="project-win-preview"]').count() == 0)
    name = page.locator('[data-testid="project-name"]')
    name.fill("trading firm")
    name.press("Enter")
    body = until(lambda: mock.posted("/projects") or None)
    check("Enter submits the dialog", bool(body))
    check("with the name, the root and the profile",
          bool(body) and body[-1].get("name") == "trading firm"
          and body[-1].get("root") == "/home/johnw/projects/jarvis-trading-firm"
          and body[-1].get("profile") == "auto", str(body[-1] if body else None))
    until(lambda: page.locator('[data-testid="picker"]').count() == 0)
    until(lambda: page.locator('[data-testid="project-p3"]').count() > 0)
    check("the created project appears in the sidebar",
          "trading firm" in page.locator('[data-testid="project-p3"]').inner_text())
    check("and a new thread starts in it",
          page.evaluate("window.__hud.state().compose && window.__hud.state().compose.projectId") == "p3"
          and page.locator('[data-testid="project-chip-select"]').input_value() == "p3")

    # Escape cancels without creating anything.
    n = len(mock.posted("/projects"))
    page.locator('[data-testid="new-project"]').click()
    page.wait_for_selector('[data-testid="project-name"]')
    page.locator('[data-testid="project-name"]').fill("never")
    page.locator('[data-testid="project-name"]').press("Escape")
    until(lambda: page.locator('[data-testid="picker"]').count() == 0)
    time.sleep(0.2)
    check("Escape cancels and creates nothing",
          page.locator('[data-testid="picker"]').count() == 0
          and len(mock.posted("/projects")) == n)


def move_checks(page, mock):
    print("\nmoving a chat thread between projects")
    page.evaluate(DND)
    check("the thread to move is on screen", expand(page, "p1", "thread-t1"))
    row = page.locator('[data-testid="thread-t1"]')
    check("a chat thread is draggable", row.get_attribute("draggable") == "true")

    carried = page.evaluate("window.__dnd.start('[data-testid=\"thread-t1\"]')")
    check("the drag carries the thread id", carried == "t1", str(carried))
    accepted = page.evaluate("window.__dnd.over('[data-testid=\"project-p2\"]')")
    check("another project accepts the drop", accepted is True)
    check("and says so while the drag is over it",
          "droptarget" in (page.locator('[data-testid="project-p2"]').get_attribute("class") or ""))
    # Its own project is not a drop target: a row that lit up for a no-op is a
    # promise the PATCH would then break.
    check("its own project does not accept it",
          page.evaluate("window.__dnd.over('[data-testid=\"project-p1\"]')") is False)

    before = len(mock.sent("PATCH", "/threads/t1"))
    page.evaluate("window.__dnd.drop('[data-testid=\"project-p2\"]')")
    page.evaluate("window.__dnd.end()")
    body = until(lambda: mock.sent("PATCH", "/threads/t1")[before:] or None)
    check("dropping PATCHes the thread", bool(body))
    check("with the new project", bool(body) and body[-1].get("project_id") == "p2",
          str(body[-1] if body else None))
    until(lambda: mock.world["threads"][0]["project_id"] == "p2")
    check("and the thread is re-parented",
          mock.world["threads"][0]["project_id"] == "p2"
          and page.evaluate("window.__hud.state().threads[0].project_id") == "p2",
          page.evaluate("window.__hud.state().threads[0].project_id"))
    check("under the project it was dropped on", expand(page, "p2", "thread-t1"))
    hint = page.locator('[data-testid="moved-t1"]')
    check("it says it still works in its original folder",
          hint.count() == 1 and "Jarvis" in hint.inner_text(),
          hint.inner_text() if hint.count() else "no hint")

    # A refusal reverts the row. The backend's rule is the one that decides, so
    # the world is made to refuse it — a thread the HUD still believes is a
    # chat thread, that the backend says belongs to a task.
    mock.world["threads"][0]["task_id"] = "k1"
    page.evaluate("window.__dnd.start('[data-testid=\"thread-t1\"]')")
    page.evaluate("window.__dnd.over('[data-testid=\"project-p1\"]')")
    n = len(mock.sent("PATCH", "/threads/t1"))
    page.evaluate("window.__dnd.drop('[data-testid=\"project-p1\"]')")
    page.evaluate("window.__dnd.end()")
    until(lambda: len(mock.sent("PATCH", "/threads/t1")) > n)
    until(lambda: page.locator('[data-testid="move-error"]').count() > 0)
    msg = page.locator('[data-testid="move-error"]').inner_text()
    check("a refused move says why, in the backend's words",
          "move with their task" in msg, msg)
    check("and the row goes back where it was",
          page.evaluate("window.__hud.state().threads[0].project_id") == "p2",
          page.evaluate("window.__hud.state().threads[0].project_id"))
    mock.world["threads"][0]["task_id"] = None

    # A task's thread is not draggable at all, and its menu says why rather
    # than letting the owner find out from a 409.
    expand(page, "p1", "task-k1")
    if page.locator('[data-testid="taskthread-kt1"]').count() == 0:
        page.locator('[data-testid="task-k1"]').click()
        until(lambda: page.locator('[data-testid="taskthread-kt1"]').count() > 0)
    kt = page.locator('[data-testid="taskthread-kt1"]')
    check("a task thread is not draggable", kt.get_attribute("draggable") in (None, "false"),
          str(kt.get_attribute("draggable")))
    page.locator('[data-testid="thread-menu-kt1"]').click()
    until(lambda: page.locator('[data-testid="thread-menu"]').count() > 0)
    check("its menu offers no destination",
          page.locator('[data-testid="move-to-p1"]').count() == 0)
    why = page.locator('[data-testid="move-refused"]').inner_text()
    check("and states the rule", "move with their task" in why, why)
    page.keyboard.press("Escape")
    until(lambda: page.locator('[data-testid="thread-menu"]').count() == 0)

    # The keyboard alternative: a drag is not reachable without a mouse, so
    # "the only way to do X is a gesture" would be the same problem as the
    # button nobody found.
    expand(page, "p2", "thread-menu-t1")
    page.locator('[data-testid="thread-menu-t1"]').focus()
    page.keyboard.press("Enter")
    until(lambda: page.locator('[data-testid="thread-menu"]').count() > 0)
    check("the menu opens from the keyboard",
          page.locator('[data-testid="move-to-p1"]').count() == 1)
    n = len(mock.sent("PATCH", "/threads/t1"))
    page.locator('[data-testid="move-to-p1"]').focus()
    page.keyboard.press("Enter")
    body = until(lambda: mock.sent("PATCH", "/threads/t1")[n:] or None)
    check("choosing a project moves it", bool(body) and body[-1].get("project_id") == "p1",
          str(body[-1] if body else None))
    until(lambda: mock.world["threads"][0]["project_id"] == "p1")
    check("the thread is back under its original project",
          mock.world["threads"][0]["project_id"] == "p1")


def quota_checks(page, mock):
    print("\nquota bars")
    until(lambda: page.locator('[data-testid="quota-bar-codex-weekly"]').count() > 0)
    bar = page.locator('[data-testid="quota-bar-codex-weekly"]')
    check("codex's weekly window is a bar", bar.count() == 1)
    check("at the reported 82%",
          bar.get_attribute("data-percent") == "82", bar.get_attribute("data-percent"))
    width = bar.locator(".qfill").evaluate("el => el.style.width")
    check("drawn 82% wide", width == "82%", width)
    # The fixture's reset is in the past by the time this runs — the ordinary
    # case a second before the next poll, and it must read as rolled over.
    check("the reset is a tooltip, not a second line",
          "resets" in (bar.get_attribute("title") or "").lower()
          and "resets" not in bar.inner_text().lower(),
          bar.get_attribute("title"))
    check("the 82% window is amber, not red",
          "warn" in (bar.locator(".qfill").get_attribute("class") or ""))
    check("and the 5-hour window is not drawn",
          page.locator('[data-testid="quota-bar-codex-5h"]').count() == 0)

    # Never computed: a provider that reported no quota gets dashes, not bars.
    check("an unreported quota still says so, rather than drawing an empty bar",
          "—" in page.locator('[data-testid="quota-claude"]').inner_text()
          and page.locator('[data-testid="quota-claude"] .qfill').count() == 0,
          page.locator('[data-testid="quota-claude"]').inner_text())
    check("the local allowance is not a second bar",
          page.locator('[data-testid="allowance-claude"]').count() == 0
          and page.locator('[data-testid="allowance-fast"]').count() == 0)

    # Claude's two windows, once the provider actually reports them.
    soon = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3 * 3600 + 12 * 60))
    mock.world["usage"]["providers"]["claude"]["quota"] = {"windows": [
        {"name": "5h", "used_percent": 41, "resets_at": soon},
        {"name": "week", "used_percent": 10, "resets_at": "2026-10-12T00:00:00Z"},
    ]}
    mock.emit("usage_updated", {"provider": "claude"})
    until(lambda: page.locator('[data-testid="quota-bar-claude-5h"]').get_attribute("data-percent") == "41")
    check("claude draws the 5-hour window",
          page.locator('[data-testid="quota-bar-claude-5h"]').count() == 1
          and page.locator('[data-testid="quota-bar-claude-week"]').get_attribute("data-percent") == "10")
    check("and 41% is green",
          "ok" in (page.locator('[data-testid="quota-bar-claude-5h"] .qfill').get_attribute("class") or ""))
    five = page.locator('[data-testid="quota-bar-claude-5h"]')
    until(lambda: "resets in" in (five.get_attribute("title") or "").lower())
    note = five.get_attribute("title") or ""
    check("a future reset counts down", "resets in 3h" in note.lower(), note)

    # It follows usage_updated rather than a reload.
    mock.world["usage"]["providers"]["codex"]["quota"]["windows"][1]["used_percent"] = 95
    mock.emit("usage_updated", {"provider": "codex"})
    until(lambda: page.locator('[data-testid="quota-bar-codex-weekly"]')
          .get_attribute("data-percent") == "95")
    check("usage_updated redraws the bars",
          page.locator('[data-testid="quota-bar-codex-weekly"]').get_attribute("data-percent") == "95")
    check("and 95% steps to red",
          "high" in (page.locator('[data-testid="quota-bar-codex-weekly"] .qfill')
                     .get_attribute("class") or ""))


def discord_checks(page, mock):
    """PR A: the Discord light — green ok, amber with the reason, red down —
    following `discord_status` rather than a reload, and never markup."""
    print("\ndiscord light")
    panel = page.locator('[data-testid="discord"]')
    until(lambda: panel.count() > 0 and panel.get_attribute("data-level") == "ok")
    check("the light is green when the poster is fine",
          panel.get_attribute("data-level") == "ok"
          and "ok" in (panel.locator(".discord-dot").get_attribute("class") or ""),
          panel.get_attribute("data-level") or "")
    # The light's own sentence: the panel may also hold B1's backfill button.
    words = page.locator('[data-testid="discord-text"]').inner_text().strip()
    check("and says so in words", words == "Discord ok", words)

    reporter = mock.world["discord"]["reporter"]
    reporter.update(state="degraded", reason=(
        "channel 123 is missing a bot permission (50013); attention updates go to your DM"))
    mock.emit("discord_status", {"state": "degraded"})
    until(lambda: panel.get_attribute("data-level") == "warn")
    check("discord_status turns it amber", panel.get_attribute("data-level") == "warn")
    check("amber carries the reason",
          "50013" in panel.inner_text() and "DM" in panel.inner_text(), panel.inner_text())

    reporter.update(state="down", reason="",
                    last_error={"op": "create_thread", "status": 429, "code": None, "at": 1.0})
    mock.emit("discord_status", {"state": "down"})
    until(lambda: panel.get_attribute("data-level") == "down")
    check("down is red", panel.get_attribute("data-level") == "down"
          and "down" in (panel.locator(".discord-dot").get_attribute("class") or ""))
    check("and names the operation and status",
          "create_thread" in panel.inner_text() and "429" in panel.inner_text(),
          panel.inner_text())

    reporter.update(state="degraded", reason='<img src=x onerror="window.__pwned=1">', last_error=None)
    mock.emit("discord_status", {"state": "degraded"})
    until(lambda: "<img" in panel.inner_text())
    check("a reason is rendered as text, never markup",
          panel.locator("img").count() == 0 and not page.evaluate("window.__pwned === 1"))

    # A surface that never started is red with its class, not "pending" (review 7).
    saved = dict(mock.world["discord"])
    mock.world["discord"] = {"connected": False,
                             "commands": {"state": "failed", "count": 0, "synced_at": None,
                                          "error": "RuntimeError"},
                             "reporter": None}
    mock.emit("discord_status", {"state": "down"})
    until(lambda: panel.get_attribute("data-level") == "down")
    check("a failed start is red and names the class",
          panel.get_attribute("data-level") == "down" and "RuntimeError" in panel.inner_text(),
          panel.inner_text())
    mock.world["discord"] = saved

    reporter.update(state="ok", reason="", last_error=None)
    mock.emit("discord_status", {"state": "ok"})
    until(lambda: panel.get_attribute("data-level") == "ok")
    check("and back to green", panel.get_attribute("data-level") == "ok")


def schedule_dialog_checks(page, mock):
    print("\nschedule dialog")
    page.locator('[data-testid="schedule-open"]').click()
    page.wait_for_selector('[data-testid="schedule-dialog"]')
    check("the list is one quiet line when it would have been an essay",
          page.locator('[data-testid="schedules-chat-note"]').count() == 0)
    page.locator('[data-testid="schedule-new"]').click()
    page.wait_for_selector('[data-testid="sched-brief"]')
    page.locator('[data-testid="sched-brief"]').fill("weekly sweep")

    # Every preset, and what the backend reads it back as.
    page.locator('[data-testid="preset-daily"]').click()
    page.locator('[data-testid="sched-time"]').fill("08:00")
    until(lambda: "every day at 08:00" in page.locator('[data-testid="sched-describe"]').inner_text())
    check("and the reading comes from the backend, not from the window",
          "every day at 08:00" in page.locator('[data-testid="sched-describe"]').inner_text(),
          page.locator('[data-testid="sched-describe"]').inner_text())
    check("the cron expression stays hidden until Advanced",
          page.locator('[data-testid="sched-expression"]').count() == 0)
    check("with the next three fire times",
          page.locator('[data-testid="sched-next"]').count() == 3,
          str(page.locator('[data-testid="sched-next"]').count()))

    page.locator('[data-testid="preset-weekdays"]').click()
    page.locator('[data-testid="sched-time"]').fill("09:30")
    until(lambda: "weekdays at 09:30" in page.locator('[data-testid="sched-describe"]').inner_text())
    check("read back as weekdays",
          "weekdays at 09:30" in page.locator('[data-testid="sched-describe"]').inner_text())

    page.locator('[data-testid="preset-weekly"]').click()
    page.locator('[data-testid="sched-weekday"]').select_option("1")
    page.locator('[data-testid="sched-time"]').fill("10:00")
    until(lambda: "Monday" in page.locator('[data-testid="sched-describe"]').inner_text())
    check("read back by name",
          "Monday" in page.locator('[data-testid="sched-describe"]').inner_text(),
          page.locator('[data-testid="sched-describe"]').inner_text())

    page.locator('[data-testid="preset-interval"]').click()
    page.locator('[data-testid="sched-every"]').fill("15")
    until(lambda: "every 15 minutes" in page.locator('[data-testid="sched-describe"]').inner_text())
    check("Every 15 minutes is read back as an interval",
          "every 15 minutes" in page.locator('[data-testid="sched-describe"]').inner_text(),
          page.locator('[data-testid="sched-describe"]').inner_text())
    page.locator('[data-testid="sched-unit"]').select_option("hours")
    until(lambda: "every 15 hours" in page.locator('[data-testid="sched-describe"]').inner_text())
    check("and hours stay an interval",
          "every 15 hours" in page.locator('[data-testid="sched-describe"]').inner_text(),
          page.locator('[data-testid="sched-describe"]').inner_text())

    # Advanced takes a raw cron, and refuses one that is not a cron rather than
    # guessing the missing field.
    page.locator('[data-testid="preset-advanced"]').click()
    page.locator('[data-testid="sched-cron"]').fill("0 9 * *")
    until(lambda: page.locator('[data-testid="sched-error"]').count() > 0)
    check("four fields is refused, not completed",
          "5 fields" in page.locator('[data-testid="sched-error"]').inner_text(),
          page.locator('[data-testid="sched-error"]').inner_text())
    check("and cannot be saved", page.locator('[data-testid="sched-save"]').is_disabled())

    page.locator('[data-testid="sched-cron"]').fill("0 9 * * 1")
    until(lambda: page.locator('[data-testid="sched-error"]').count() == 0)
    n = len(mock.posted("/schedules"))
    page.locator('[data-testid="sched-save"]').click()
    body = until(lambda: mock.posted("/schedules")[n:] or None)
    check("saving creates the schedule", bool(body))
    check("with the brief and the cron the presets produced",
          bool(body) and body[-1].get("cron") == "0 9 * * 1"
          and body[-1].get("brief") == "weekly sweep", str(body[-1] if body else None))
    until(lambda: page.locator('[data-testid="schedule-dialog"]').count() == 0)
    check("and closes", page.locator('[data-testid="schedule-dialog"]').count() == 0)

    # The same dialog edits an existing one, opening on the preset that made it.
    page.locator('[data-testid="schedule-open"]').click()
    page.wait_for_selector('[data-testid="schedule-dialog"]')
    page.locator('[data-testid="schedule-edit-s1"]').click()
    page.wait_for_selector('[data-testid="sched-brief"]')
    check("editing opens on the preset the cron came from",
          page.locator('[data-testid="preset-daily"]').get_attribute("aria-pressed") == "true",
          page.locator('[data-testid="preset-daily"]').get_attribute("aria-pressed"))
    check("with its time",
          page.locator('[data-testid="sched-time"]').input_value() == "07:00",
          page.locator('[data-testid="sched-time"]').input_value())
    check("and its brief",
          "morning briefing" in page.locator('[data-testid="sched-brief"]').input_value())
    page.locator('[data-testid="sched-time"]').fill("07:30")
    until(lambda: "every day at 07:30" in page.locator('[data-testid="sched-describe"]').inner_text())
    n = len(mock.sent("PATCH", "/schedules/s1"))
    page.locator('[data-testid="sched-save"]').click()
    body = until(lambda: mock.sent("PATCH", "/schedules/s1")[n:] or None)
    check("saving an edit PATCHes rather than creating a second one",
          bool(body) and body[-1].get("cron") == "30 7 * * *", str(body[-1] if body else None))

    # Escape closes without saving.
    page.locator('[data-testid="schedule-open"]').click()
    page.wait_for_selector('[data-testid="schedule-dialog"]')
    page.locator('[data-testid="schedule-edit-s1"]').click()
    page.wait_for_selector('[data-testid="sched-brief"]')
    n = len(mock.sent("PATCH", "/schedules/s1"))
    page.locator('[data-testid="sched-brief"]').press("Escape")
    until(lambda: page.locator('[data-testid="schedule-dialog"]').count() == 0)
    time.sleep(0.2)
    check("Escape closes the dialog and saves nothing",
          page.locator('[data-testid="schedule-dialog"]').count() == 0
          and len(mock.sent("PATCH", "/schedules/s1")) == n)


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# the model a thread runs on (decisions 2026-10-06, part A)
# ---------------------------------------------------------------------------

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


# ---- the model chip: one button, one popover (2026-10-10) -----------------------
# provider select · [model · effort ▾] -> a popover with a Model and an Effort
# section. These drive it like the owner does; each leaves the popover as it
# found it, except pick_model, which (like the real thing) keeps it open so the
# effort can follow.

CHIP_BTN = '[data-testid="model-chip-btn"]'
POP = '[data-testid="model-pop"]'


def chip_model(page):
    return page.locator(CHIP_BTN).get_attribute("data-model")


def chip_effort(page):
    return page.locator(CHIP_BTN).get_attribute("data-effort")


def open_pop(page):
    if page.locator(POP).count() == 0:
        page.locator(CHIP_BTN).click()
        page.wait_for_selector(POP)


def close_pop(page):
    if page.locator(POP).count():
        page.locator(CHIP_BTN).click()
        until(lambda: page.locator(POP).count() == 0)


def _peek(page, fn):
    """Run `fn` with the popover open, then leave it as it was."""
    was = page.locator(POP).count() > 0
    open_pop(page)
    try:
        return fn()
    finally:
        if not was:
            close_pop(page)


def model_labels(page):
    return _peek(page, lambda: page.locator('[data-testid="model-opt"]').all_inner_texts())


def effort_labels(page):
    return _peek(page, lambda: page.locator('[data-testid="effort-opt"]').all_inner_texts())


def has_effort(page):
    return _peek(page, lambda: page.locator('[data-testid="effort-list"]').count() > 0)


def has_defaults_button(page):
    return _peek(page, lambda: page.locator('[data-testid="provider-defaults-open"]').count() > 0)


def pick_model(page, value):
    open_pop(page)
    page.locator(f'[data-testid="model-opt"][data-value="{value}"]').click()


def pick_effort(page, value):
    open_pop(page)
    page.locator(f'[data-testid="effort-opt"][data-value="{value}"]').click()
    until(lambda: page.locator(POP).count() == 0)


def open_defaults(page):
    open_pop(page)
    page.locator('[data-testid="provider-defaults-open"]').click()



def writes(mock, since):
    return [c for c in mock.calls[since:] if c[0] in ("POST", "PATCH") and c[1] not in ("/stt", "/mute")]


def thread_model_checks(page, mock):
    print("\nthe model a thread runs on: provider, model, effort")
    w = mock.world
    w["models"]["selected"] = "openai/gpt-5.6-luna"
    mock.emit("model", {})
    page.locator('[data-testid="tab-chat"]').click()
    page.locator('[data-testid="new-thread"]').click()
    until(lambda: page.locator(CHIP_BTN).count() > 0)
    until(lambda: "gpt-5.6-luna" in model_labels(page)[0])
    check("a new thread starts on the default, named",
          chip_model(page) == "" and model_labels(page)[0] == "default · gpt-5.6-luna",
          model_labels(page)[0])
    check("the chip button reads model and effort",
          page.locator(CHIP_BTN).inner_text().strip().startswith("default · gpt-5.6-luna · high"),
          page.locator(CHIP_BTN).inner_text())
    open_pop(page)
    check("the popover has a Model section and an Effort section",
          [t.strip() for t in page.locator(POP + " .msec").all_inner_texts()] == ["MODEL", "EFFORT"]
          or [t.strip().lower() for t in page.locator(POP + " .msec").all_inner_texts()] == ["model", "effort"],
          str(page.locator(POP + " .msec").all_inner_texts()))
    page.keyboard.press("Escape")
    check("Escape closes it", until(lambda: page.locator(POP).count() == 0) is True)
    open_pop(page)
    page.mouse.click(5, 5)
    check("and so does a click outside", until(lambda: page.locator(POP).count() == 0) is True)
    check("the provider chip offers OpenRouter, Claude and Codex",
          page.locator('[data-testid="provider-chip-select"] option').all_inner_texts()
          == ["OpenRouter", "Claude", "Codex"])
    check("the catalogue search is the last model entry",
          model_labels(page)[-1] == "search catalogue…")
    check("the effort defaults to high",
          effort_labels(page)[0] == "default · high")

    since = len(mock.calls)
    page.locator('[data-testid="provider-chip-select"]').select_option("claude")
    until(lambda: "claude-opus-5-5" in model_labels(page)[0])
    check("Claude lists its own models, defaulting to Opus 5.5",
          model_labels(page)[0] == "default · claude-opus-5-5"
          and "claude-haiku-4-5" in model_labels(page))
    pick_model(page, "claude-haiku-4-5")
    check("a model with no effort control gets no effort section",
          until(lambda: not has_effort(page)) is True)
    close_pop(page)
    page.locator('[data-testid="provider-chip-select"]').select_option("fast")
    check("changing provider starts from that provider's defaults",
          until(lambda: chip_model(page) == "") is True)
    page.evaluate("window.__pwned = false")
    pick_model(page, "evil/model")
    check("the chip and its popover draw no markup from a network-supplied model",
          page.locator('[data-testid="model-chip"] img').count() == 0
          and page.locator(POP + " img").count() == 0
          and page.evaluate("window.__pwned") is False)
    pick_model(page, "openai/gpt-5.6-luna")
    pick_effort(page, "low")
    check("choosing while composing sends nothing", not writes(mock, since), str(writes(mock, since)))

    # A provider the project's profile cannot run is greyed out, with why.
    project_select = page.locator('[data-testid="project-chip-select"]')
    started_in = project_select.input_value()
    project_select.select_option("p2")              # the ask project
    codex = page.locator('[data-testid="provider-chip-select"] option[value="codex"]')
    until(lambda: codex.is_disabled())
    check("Codex is greyed out in an ask project",
          codex.is_disabled() and "auto" in (codex.get_attribute("title") or ""),
          codex.get_attribute("title"))
    check("and the providers it can run stay enabled",
          not page.locator('[data-testid="provider-chip-select"] option[value="claude"]').is_disabled()
          and not page.locator('[data-testid="provider-chip-select"] option[value="fast"]').is_disabled())
    project_select.select_option("p1")              # an auto project
    check("and enabled again in an auto project", until(lambda: not codex.is_disabled()) is True,
          f"started in {started_in}")
    project_select.select_option(started_in)
    pick_model(page, "openai/gpt-5.6-luna")
    pick_effort(page, "low")

    # Space on a focused chip never starts push-to-talk.
    page.locator(CHIP_BTN).focus()
    page.keyboard.press("Space")
    page.keyboard.press("Escape")
    until(lambda: page.locator(POP).count() == 0)
    check("space on a chip does not start push-to-talk", page.evaluate("window.__hud.capture.ptt") is None)

    opened = len(mock.posted("/threads"))
    box = page.locator('[data-testid="input"]')
    box.fill("model test")
    w["fail_send"] = 1                                # the thread opens; its first send fails
    box.press("Enter")
    body = until(lambda: mock.posted("/threads")[opened:] or None)
    check("the first message carries the choice",
          bool(body) and body[-1].get("provider") == "fast"
          and body[-1].get("brief") == {"model": "openai/gpt-5.6-luna", "effort": "low"},
          str(body[-1] if body else None))
    kept = until(lambda: page.evaluate("(() => { const c = window.__hud.state().compose;"
                                       " return c && c.openedId && !window.__hud.state().busy ? c : null })()"))
    check("a failed first send keeps the compose choice beside the opened thread",
          bool(kept) and (kept.get("provider"), kept.get("model"), kept.get("effort"))
          == ("fast", "openai/gpt-5.6-luna", "low"), str(kept))
    # Bugbot 2026-10-08: the chips used to vanish here, until the retry.
    chips = until(lambda: page.locator(CHIP_BTN).count() > 0)
    check("and the chips stay on screen, on that choice, for the retry",
          bool(chips)
          and chip_model(page) == "openai/gpt-5.6-luna"
          and chip_effort(page) == "low",
          chip_model(page) if page.locator(CHIP_BTN).count() else "no model chip")
    box.fill("model test")
    box.press("Enter")
    tid = until(lambda: page.evaluate("window.__hud.state().threadId"))
    mock.emit("turn_finished", {"stop": "end"}, thread_id=tid)
    until(lambda: page.locator('[data-testid="provider-chip"]').count() > 0)
    check("after it the provider is fixed",
          page.locator('[data-testid="provider-chip-select"]').count() == 0
          and page.locator('[data-testid="provider-chip"]').inner_text() == "OpenRouter")
    pid = page.evaluate("window.__hud.state().threads.find(t => t.id === window.__hud.state().threadId)?.project_id")
    expand(page, pid or "p1", f"thread-{tid}")
    check("the sidebar row badges the pinned model",
          until(lambda: page.locator(f'[data-testid="pin-{tid}"]').count() == 1) is True
          and page.locator(f'[data-testid="pin-{tid}"]').inner_text() == "gpt-5.6-luna")
    tip = page.locator(f'[data-testid="thread-{tid}"]').get_attribute("title") or ""
    check("and its tooltip names the provider and the model",
          "OpenRouter" in tip and "openai/gpt-5.6-luna" in tip and "pinned" in tip, tip)

    since = len(mock.calls)
    pick_model(page, "")
    close_pop(page)
    sent = until(lambda: mock.sent("PATCH", f"/threads/{tid}") or None)
    check("a change after the first message is a PATCH", bool(sent) and sent[-1] == {"model": None}, str(sent))
    check("and nothing else is written", [c[1] for c in writes(mock, since)] == [f"/threads/{tid}"],
          str(writes(mock, since)))
    check("the change is a line in the chat",
          until(lambda: "model →" in page.locator('[data-testid="log"]').inner_text()) is True)
    check("the pin badge goes when the thread follows the default",
          until(lambda: page.locator(f'[data-testid="pin-{tid}"]').count() == 0) is True)

    # An effort alone keeps the thread on the default model (A4 amendment).
    n = len(mock.sent("PATCH", f"/threads/{tid}"))
    pick_effort(page, "low")
    sent = until(lambda: mock.sent("PATCH", f"/threads/{tid}")[n:] or None)
    check("an effort on a default thread is a PATCH of the effort alone",
          bool(sent) and sent[-1] == {"effort": "low"}, str(sent))
    check("and the line names the default model it still follows",
          until(lambda: "effort → low (default model: openai/gpt-5.6-luna)"
                in page.locator('[data-testid="log"]').inner_text()) is True)
    check("the chip still shows the default",
          until(lambda: chip_effort(page) == "low") is True
          and chip_model(page) == "" and model_labels(page)[0] == "default · gpt-5.6-luna",
          chip_model(page))
    check("and no pin badge appears", page.locator(f'[data-testid="pin-{tid}"]').count() == 0)
    tip = page.locator(f'[data-testid="thread-{tid}"]').get_attribute("title") or ""
    check("its tooltip says it follows the default, at that effort",
          until(lambda: "follows the default · effort low" in
                (page.locator(f'[data-testid="thread-{tid}"]').get_attribute("title") or "")) is True, tip)

    # A refused change leaves the chip on what the server holds, and says why.
    w["refuse_choice"] = "evil/model cannot call tools here"
    pick_model(page, "evil/model")
    close_pop(page)
    err = until(lambda: page.locator('[data-testid="model-error"]').count() and
                page.locator('[data-testid="model-error"]').inner_text())
    check("a refused change is shown with the server's reason",
          bool(err) and "Could not change model" in err and "cannot call tools" in err, str(err))
    check("and the chip reverts", chip_model(page) == "")
    w["refuse_choice"] = None

    # The catalogue: search, use (pins to the roster), and the text-only note.
    pick_model(page, "__search__")
    page.wait_for_selector('[data-testid="catalog"]')
    page.evaluate("window.__pwned = false")
    evil = page.locator('[data-testid="catalog-row-evil/model"]')
    until(lambda: evil.count() > 0)
    check("a model name carrying markup renders as text in the catalogue",
          evil.count() == 1 and evil.locator("img").count() == 0 and "<img" in evil.inner_text(),
          evil.inner_text() if evil.count() else "no row")
    check("and its handler never runs", page.evaluate("window.__pwned") is False)
    page.locator('[data-testid="catalog-search"]').fill("deepseek")
    check("the catalogue search narrows the list",
          page.locator('[data-testid^="catalog-row-"]').count() == 1)
    page.locator('[data-testid="catalog-use-deepseek/deepseek-v4-flash-0731"]').click()
    # Wait for *this* add: earlier sections already POSTed /models (the global
    # picker's {model, effort}), so "any POST" raced the click.
    added = until(lambda: [b for b in mock.sent("POST", "/models") if b.get("add", "").startswith("deepseek")] or None)
    check("using a catalogue model pins it to the roster",
          bool(added) and added[-1] == {"add": "deepseek/deepseek-v4-flash-0731"}, str(added))
    sent = until(lambda: [b for b in mock.sent("PATCH", f"/threads/{tid}") if (b.get("model") or "").startswith("deepseek")] or None)
    check("and puts this thread on it", bool(sent), str(mock.sent("PATCH", f"/threads/{tid}")))
    until(lambda: chip_model(page).startswith("deepseek"))
    page.locator('[data-testid="filepicker"]').set_input_files(
        files=[{"name": "shot.png", "mimeType": "image/png", "buffer": PNG_1PX}])
    note = until(lambda: page.locator('[data-testid="image-note"]').count() and
                 page.locator('[data-testid="image-note"]').inner_text())
    check("a text-only model says it cannot see an attached image",
          bool(note) and "text-only" in note, str(note))

    # The catalogue unpins what is pinned (2026-10-08): "on roster" was text.
    pick_model(page, "__search__")
    page.wait_for_selector('[data-testid="catalog"]')
    luna = "openai/gpt-5.6-luna"
    until(lambda: page.locator(f'[data-testid="catalog-unpin-{luna}"]').count() > 0)
    check("a pinned model offers Unpin in the catalogue",
          page.locator(f'[data-testid="catalog-unpin-{luna}"]').count() == 1
          and page.locator(f'[data-testid="catalog-pin-{luna}"]').count() == 0)
    # Luna is the default in use: refused, with the reason, and still pinned.
    page.locator(f'[data-testid="catalog-unpin-{luna}"]').click()
    err = until(lambda: page.locator('[data-testid="catalog-error"]').count()
                and page.locator('[data-testid="catalog-error"]').inner_text())
    check("a refused Unpin is shown in the catalogue",
          bool(err) and "choose another default first" in err, str(err))
    check("and the model stays pinned", page.locator(f'[data-testid="catalog-unpin-{luna}"]').count() == 1)
    deep = "deepseek/deepseek-v4-flash-0731"
    page.locator(f'[data-testid="catalog-unpin-{deep}"]').click()
    removed = until(lambda: [b for b in mock.sent("POST", "/models") if b.get("remove") == deep] or None)
    check("Unpin posts /models {remove}", bool(removed), str(mock.sent("POST", "/models")[-3:]))
    check("and the row offers Pin again",
          until(lambda: page.locator(f'[data-testid="catalog-pin-{deep}"]').count() == 1) is True)
    check("and the catalogue error cleared", page.locator('[data-testid="catalog-error"]').count() == 0)
    page.locator('[data-testid="catalog-close"]').click()
    until(lambda: page.locator('[data-testid="catalog"]').count() == 0)

    # A task's thread is read-only.
    w["threads"].append({
        "id": "kt9", "project_id": "p1", "role": "implementer", "provider": "codex",
        "provider_session_id": None, "task_id": "k1", "title": "", "created": "", "updated": "",
        "turns": 1, "cost_usd": 0, "tokens": 0, "model": "gpt-5.6-sol", "effort": "high", "cwd": None})
    mock.emit("thread_opened", {}, thread_id="kt9")
    until(lambda: any(t["id"] == "kt9" for t in page.evaluate("window.__hud.state().threads")))
    page.evaluate("window.__hud.dispatch({type: 'patch', patch: {threadId: 'kt9', compose: null}})")
    ro = until(lambda: page.locator('[data-testid="model-chip-ro"]').count() and
               page.locator('[data-testid="model-chip-ro"]').inner_text())
    check("a task's thread shows its model read-only",
          bool(ro) and "gpt-5.6-sol" in ro and page.locator(CHIP_BTN).count() == 0,
          str(ro))


def provider_default_checks(page, mock):
    """Claude's and Codex's default, set from the model chip (2026-10-08)."""
    print("\nClaude and Codex defaults, from the model chip")
    from tests.face import hud_v2_mock
    w = mock.world
    w["cli_defaults"] = {}
    evil = {"id": "claude-evil", "name": '<img src=x onerror="window.__pwned=true">',
            "vision": True, "efforts": []}
    hud_v2_mock.CLI_MODELS["claude"].append(evil)
    mock.emit("model", {})
    # A default Claude thread, and one pinned to Opus.
    for tid, model in (("cd1", None), ("cp1", "claude-opus-5-5")):
        w["threads"].append({
            "id": tid, "project_id": "p1", "role": "chat", "provider": "claude",
            "provider_session_id": None, "task_id": None, "title": f"claude {tid}", "created": "",
            "updated": "", "turns": 1, "cost_usd": 0, "tokens": 0, "model": model, "effort": None,
            "cwd": None})
        mock.emit("thread_opened", {}, thread_id=tid)
    until(lambda: all(any(t["id"] == tid for t in page.evaluate("window.__hud.state().threads"))
                      for tid in ("cd1", "cp1")))

    def show(tid):
        page.evaluate(f"window.__hud.dispatch({{type: 'patch', patch: {{threadId: '{tid}', compose: null}}}})")
        until(lambda: page.locator(CHIP_BTN).count() > 0)

    def first_option():
        # Read off the chip's own text: a dialog's veil is often up, and the
        # popover cannot be opened under it.
        t = page.locator(CHIP_BTN).inner_text().strip()
        m = re.match(r"(default · \S+)", t)
        return m.group(1) if m else t

    show("cd1")
    check("a default Claude thread is labelled with the built-in default",
          until(lambda: first_option() == "default · claude-opus-5-5") is True, first_option())
    check("the Claude chip offers its default menu", has_defaults_button(page))
    page.locator('[data-testid="tab-chat"]').click()
    page.locator('[data-testid="new-thread"]').click()
    until(lambda: page.locator('[data-testid="provider-chip-select"]').count() > 0)
    page.locator('[data-testid="provider-chip-select"]').select_option("fast")
    check("the OpenRouter chip does not (its default is the Model picker's)",
          until(lambda: not has_defaults_button(page)) is True)

    # The chip's dialogs are open pickers: no open mic under them (review).
    page.locator('[data-testid="dictation-review"]').click()

    def mic_uploads():
        before = len(mock.sent("POST", "/stt"))
        page.evaluate("""
          const m = window.__hud.mic;
          m.feedMs(2000, 0.001);
          window.__hud.capture.openFollowUp();
          m.feedMs(1600, 0.06); m.feedMs(2200, 0.0005);
        """)
        return until(lambda: len(mock.sent("POST", "/stt")) > before, timeout=1.0) is True

    pick_model(page, "__search__")
    page.wait_for_selector('[data-testid="catalog"]')
    check("speech under the catalogue is not taken", not mic_uploads())
    page.locator('[data-testid="catalog-close"]').click()
    until(lambda: page.locator('[data-testid="catalog"]').count() == 0)
    page.locator('[data-testid="provider-chip-select"]').select_option("claude")
    open_defaults(page)
    page.wait_for_selector('[data-testid="provider-defaults"]')
    check("nor under the provider-default dialog", not mic_uploads())
    z = page.evaluate("getComputedStyle(document.querySelector('[data-testid=\"provider-defaults\"]')).zIndex")
    check("and the dialog stays under the approval veil (z-index 100)", int(z) < 100, z)
    page.locator('[data-testid="pd-close"]').click()
    until(lambda: page.locator('[data-testid="provider-defaults"]').count() == 0)
    check("with both closed, the same speech is taken", mic_uploads())
    until(lambda: page.locator('[data-testid="input"]').input_value() != "", timeout=2.0)
    page.locator('[data-testid="input"]').fill("")

    show("cd1")
    page.evaluate("window.__pwned = false")
    open_defaults(page)
    page.wait_for_selector('[data-testid="provider-defaults"]')
    badge = lambda mid: page.locator(f'[data-testid="pd-badge-{mid}"]').count() == 1  # noqa: E731
    check("the current default wears the badge", badge("claude-opus-5-5") and not badge("claude-haiku-4-5"))
    check("and offers no Set as default", page.locator('[data-testid="pd-set-claude-opus-5-5"]').count() == 0)
    check("Reset is disabled while nothing is chosen here",
          page.locator('[data-testid="pd-reset"]').is_disabled())
    row = page.locator('[data-testid="pd-row-claude-evil"]')
    check("a model name carrying markup renders as text",
          row.count() == 1 and row.locator("img").count() == 0 and "<img" in row.inner_text()
          and page.evaluate("window.__pwned") is False, row.inner_text() if row.count() else "no row")

    since = len(mock.calls)
    page.locator('[data-testid="pd-set-claude-haiku-4-5"]').click()
    body = until(lambda: mock.sent("POST", "/thread-models")[-1:] or None)
    check("Set as default posts exactly {provider, model}",
          bool(body) and body[-1] == {"provider": "claude", "model": "claude-haiku-4-5"}, str(body))
    check("the badge moves", until(lambda: badge("claude-haiku-4-5") and not badge("claude-opus-5-5")) is True)
    check("and Reset is enabled", until(lambda: not page.locator('[data-testid="pd-reset"]').is_disabled()) is True)
    check("the open default thread relabels at once",
          until(lambda: first_option() == "default · claude-haiku-4-5") is True, first_option())
    check("and nothing but the default was written",
          [c[1] for c in writes(mock, since)] == ["/thread-models"], str(writes(mock, since)))
    page.locator('[data-testid="pd-close"]').click()
    show("cp1")
    check("a pinned thread keeps its own model",
          until(lambda: chip_model(page) == "claude-opus-5-5") is True
          and not mock.sent("PATCH", "/threads/cp1"))

    # The default's effort: the model's own ladder, stored with it.
    show("cd1")
    open_defaults(page)
    page.locator('[data-testid="pd-set-claude-opus-5-5"]').click()
    until(lambda: badge("claude-opus-5-5"))
    page.locator('[data-testid="pd-effort"]').select_option("low")
    body = until(lambda: [b for b in mock.sent("POST", "/thread-models") if b.get("effort")] or None)
    check("an effort on the default posts {provider, model, effort}",
          bool(body) and body[-1] == {"provider": "claude", "model": "claude-opus-5-5", "effort": "low"}, str(body))
    check("and the default thread's effort chip follows it",
          until(lambda: "· low" in page.locator(CHIP_BTN).inner_text()) is True)

    # A refusal is shown inline, in the server's words, and changes nothing.
    w["refuse_default"] = "claude-haiku-4-5 is not allowed here for a reason"
    page.locator('[data-testid="pd-set-claude-haiku-4-5"]').click()
    err = until(lambda: page.locator('[data-testid="pd-error"]').count()
                and page.locator('[data-testid="pd-error"]').inner_text())
    check("a refused Set as default is shown inline",
          bool(err) and "Could not set claude-haiku-4-5 as the Claude default" in err
          and "for a reason" in err, str(err))
    check("and the badge stays", badge("claude-opus-5-5") and not badge("claude-haiku-4-5"))
    w["refuse_default"] = None

    # Reset to built-in default.
    n = len(mock.sent("POST", "/thread-models"))
    page.locator('[data-testid="pd-reset"]').click()
    body = until(lambda: mock.sent("POST", "/thread-models")[n:] or None)
    check("Reset posts {provider, model: ''}", bool(body) and body[-1] == {"provider": "claude", "model": ""},
          str(body))
    check("and the built-in default is back, Reset disabled again",
          until(lambda: page.locator('[data-testid="pd-reset"]').is_disabled()) is True
          and badge("claude-opus-5-5") and page.locator('[data-testid="pd-error"]').count() == 0)
    page.locator('[data-testid="pd-close"]').click()
    check("the dialog closes", until(lambda: page.locator('[data-testid="provider-defaults"]').count() == 0) is True)

    # Another window's change arrives as a `model` event and relabels here.
    w["cli_defaults"]["claude"] = {"model": "claude-haiku-4-5", "effort": None}
    mock.emit("model", {"provider": "claude", "default": "claude-haiku-4-5"})
    check("a change from elsewhere relabels default threads",
          until(lambda: first_option() == "default · claude-haiku-4-5") is True, first_option())
    w["cli_defaults"] = {}
    mock.emit("model", {})

    # Codex: its default wins over routing for chat threads; Reset is routing.
    page.locator('[data-testid="tab-chat"]').click()
    page.locator('[data-testid="new-thread"]').click()
    until(lambda: page.locator('[data-testid="provider-chip-select"]').count() > 0)
    page.locator('[data-testid="project-chip-select"]').select_option("p1")     # an auto project
    page.locator('[data-testid="provider-chip-select"]').select_option("codex")
    until(lambda: first_option() == "default · gpt-6-astra")
    open_defaults(page)
    page.wait_for_selector('[data-testid="provider-defaults"]')
    check("Codex's menu says routing is untouched",
          "Routing for tasks is unchanged" in page.locator('[data-testid="pd-now"]').inner_text()
          and "from routing" in page.locator('[data-testid="pd-now"]').inner_text())
    check("and labels routing's model's effort as routing's",
          page.locator('[data-testid="pd-effort"] option').first.inner_text() == "routing default · xhigh",
          page.locator('[data-testid="pd-effort"] option').first.inner_text())
    page.locator('[data-testid="pd-set-gpt-5.6-sol"]').click()
    until(lambda: badge("gpt-5.6-sol"))
    page.locator('[data-testid="pd-set-gpt-6-astra"]').click()
    check("Set as default on routing's own model keeps routing's effort",
          until(lambda: badge("gpt-6-astra") and "· xhigh (set here)"
                in page.locator('[data-testid="pd-now"]').inner_text()) is True,
          page.locator('[data-testid="pd-now"]').inner_text())
    page.locator('[data-testid="pd-set-gpt-5.6-sol"]').click()
    check("Set as default on Codex relabels the compose row",
          until(lambda: first_option() == "default · gpt-5.6-sol") is True, first_option())
    page.locator('[data-testid="pd-reset"]').click()
    check("and Reset returns it to routing's default",
          until(lambda: first_option() == "default · gpt-6-astra") is True, first_option())
    page.locator('[data-testid="pd-close"]').click()
    hud_v2_mock.CLI_MODELS["claude"].remove(evil)
    mock.emit("model", {})


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
            # The owner's live daemon listens on these. Nothing this suite
            # loads may reach it: a hard-coded preview port once did, and its
            # log filled with `GET /p/p1/index.html -> 400`.
            live = re.compile(r"^https?://(127\.0\.0\.1|localhost|\[::1\]):(8402|8403|8405)/")

            def refuse_live(route):
                FAILURES.append(f"the HUD under test reached a live daemon port: {route.request.url}")
                route.abort()

            # Zoom, folding panes, resizing and the workspace (2026-10-08/09)
            # first, in a context of its own **and against a mock of its own**
            # on an ephemeral port: its localStorage, its reloads, its saves,
            # `/seen` posts, approval decisions and preview hits all stay out of
            # the world the rest of the suite asserts on.
            from tests.face.hud_v2_layout_check import layout_checks
            layout_mock = MockDaemon(0).start()
            try:
                layout_checks(browser, layout_mock, f"http://127.0.0.1:{layout_mock.port}", check, until,
                              (live, refuse_live), FAKE_RECOGNIZER)
            finally:
                layout_mock.stop()

            ctx = browser.new_context(permissions=["microphone"])
            ctx.route(live, refuse_live)
            ctx.add_init_script(FAKE_RECOGNIZER)
            page = ctx.new_page()
            page.on("pageerror", lambda e: FAILURES.append(f"page error: {e}"))
            boot(page, mock)

            window_checks(page, mock)
            sidebar_checks(page, mock)
            from tests.face.hud_v2_activity_check import activity_checks
            activity_checks(page, mock, check, until, expand, boot)
            chat_checks(page, mock)
            # PR C: the chat's place on Discord, and messages typed there.
            from tests.face.hud_v2_mirror_check import mirror_checks
            mirror_checks(page, mock, check, until, mock.await_reconnect)
            safety_render_checks(page, mock)
            input_checks(page, mock)
            steer_checks(page, mock)
            dictation_checks(page, mock)
            wake_checks(page, mock)
            approval_checks(page, mock)
            task_checks(page, mock)
            file_checks(page, mock)
            diff_checks(page, mock)
            preview_checks(page, mock)
            panels_checks(page, mock)
            picker_checks(page, mock)
            # WP12c last: these add a project and re-parent a thread, so they
            # rearrange the very tree the earlier sections navigate.
            quota_checks(page, mock)
            discord_checks(page, mock)
            schedule_dialog_checks(page, mock)
            compose_checks(page, mock)
            newproject_checks(page, mock)
            move_checks(page, mock)
            thread_model_checks(page, mock)
            provider_default_checks(page, mock)
            # B1: project channels, before part B archives what they link.
            from tests.face.hud_v2_discord_check import discord_link_checks
            discord_link_checks(page, mock, check, until, expand)
            # Decisions part B last of all: it renames, archives and deletes.
            from tests.face.hud_v2_projects_check import projects_checks
            projects_checks(page, mock, check, until, expand)

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
