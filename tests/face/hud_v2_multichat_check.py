"""Headless checks for several chat panes at once (WP-B of
docs/plans/2026-10-09-hud-workspace-plan.md, §2.2 "Several chats at once").

Called from `hud_v2_check.main()` right after the layout section, in a browser
context of its own and against **a `MockDaemon` of its own** on an ephemeral
port, as the layout section is (the mock hands SSE frames only to the newest
`/events` connection, and this section's sends, interrupts, `/seen` posts and
approval decisions must stay out of the world the main suite asserts on). It
also runs on its own: `python tests/face/hud_v2_multichat_check.py`.

The checks worth keeping, each written to bite:
  - two threads side by side, each in its own pane with its own box, chips,
    Send/Steer and Stop, the sidebar marking both — the active one with the
    accent, the other dimmer — and numbering the compose rows by pane;
  - a turn running in pane A while the owner types and sends in pane B: B's
    message goes to B's thread, and A keeps running and keeps its Stop;
  - **every event kind lands in the pane showing its thread** (or tracking
    its turn), and an event with no thread in the selected chat's;
  - A's Stop interrupts A's thread and never B's;
  - voice goes to the selected chat, the chat pane most recently clicked: its header
    carries the mic mark, its status rides the orb (the dictation strip is on
    the orb, PR #29) while every other chat pane shows its own, REVIEW
    puts the transcript in its box, AUTO sends to its thread, the orb follows
    and interrupts its turn, the follow-up window opens only when its turn
    ends, and only its turn keeps the mic suppressed (a long turn in another
    pane no longer silences it);
  - a thread is never open twice: a click on a thread another pane shows
    focuses that pane; one held by a hidden pane moves into the pane it is
    opened in; Alt+click and "Open beside" open the next pane to the right;
  - `/seen` for every drawn chat pane's thread, none for a hidden one;
  - the steer and give-back cases of `hud_v2_check.steer_checks`, re-run in
    pane 2 with pane 1 holding another thread;
  - an authorization card takes focus off a focused sidebar button (Enter on
    it worked behind the card), and gives it back;
  - a File pane holding an unsaved edit refuses every way of being switched
    away — its own tabs, Open beside, a sidebar thread, a task, New thread —
    and says why.

Review of PR #27 — words or a transcript attached to the wrong thread; each
of these failed on dbd2096:
  - words handed back while a hidden pane held their thread, and a draft
    typed for a thread, follow that thread when another pane opens it, and a
    pane opening another thread does not carry its draft into it;
  - a first send finds its pane again after every await, so a trade of
    conversations or another thread opened meanwhile is never undone;
  - a late transcript lands in the pane showing its thread, or nowhere;
  - with a card up Tab and Shift+Tab never leave it, and nothing behind the
    veil can be reached (it is inert);
  - a pane running its own turn that opens a running thread trades turns
    with the pane that left it, so that thread has its Stop;
  - a capture status leaves a pane that stops being the selected chat, and an
    Open beside refusal is said where the owner can see it.

The owner's rule (decisions W-6): ambiguous input — voice, push-to-talk, an
event with no thread — goes to the selected chat, the chat pane most
recently clicked, as it is when the input is delivered; a click on a Preview
or File pane does not change it; a selected pane the window drops falls back
to the one selected before it; one chat pane is selected; with none drawn
nothing is sent; and input that belongs to a thread stays with it.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.face.hud_v2_layout_check import (  # noqa: E402
    _active_testid, _approval, _attr, _choose, _edit, _pane, _visible,
)

# Both chat panes; files and the task behind them.
TWO = json.dumps({"preset": "cols2", "focused": 1,
                  "panes": [{"view": "chat"}, {"view": "chat"}, {"view": "file"}, {"view": "task"}]})
# A chat, and a File pane beside it.
CHAT_FILE = json.dumps({"preset": "cols2", "focused": 1,
                        "panes": [{"view": "chat"}, {"view": "file"}, {"view": "preview"}, {"view": "task"}]})


def multichat_checks(browser, mock, base, check, until, guard, init_script):
    print("\nseveral chat panes (WP-B)")
    _seed(mock)
    ctx = browser.new_context(viewport={"width": 1600, "height": 900}, permissions=["microphone"])
    ctx.route(guard[0], guard[1])
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
        for section in (_side_by_side_checks, _never_twice_checks, _two_turns_checks, _moved_turn_checks,
                        _voice_checks, _seen_checks, _steer_pane2_checks, _giveback_pane2_checks,
                        _card_focus_checks, _dirty_file_checks,
                        # review of PR #27
                        _rv_giveback_hidden_swap_checks, _rv_draft_checks, _rv_compose_swap_race_checks,
                        _rv_newthread_during_send_checks, _rv_transcript_race_checks, _rv_tab_behind_card_checks,
                        _rv_handoff_trade_checks, _rv_stt_lands_selected_checks, _rv_stale_status_checks,
                        _rv_beside_refusal_checks,
                        # the owner's rule: ambiguous input goes to the selected chat (decisions W-6)
                        _w6_grid_checks, _w6_fallback_checks, _w6_one_and_none_checks, _w6_handback_held_checks,
                        # re-review of PR #27 (feb61ff)
                        _rr_compose_fail_moved_checks, _rr_two_cards_checks, _rr_tab_never_authorizes_checks,
                        _rr_placeholder_stop_checks, _rr_chip_writeback_checks, _rr_drop_checks):
            try:
                section(page, mock, check, until)
            except Exception as e:  # a section that cannot run is a failure, and the rest still run
                check(f"multichat {section.__name__.strip('_')} ran to the end", False,
                      str(e).splitlines()[0][:200])
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
        check("no page errors in the multichat section", not errors, "; ".join(errors[:3]))
    finally:
        ctx.close()


# ---------------------------------------------------------------------------

def _seed(mock):
    """Two more chat threads: t2 beside t1 in jarvis, t3 in schoolwork."""
    w = mock.world
    base = next(t for t in w["threads"] if t["id"] == "t1")
    w["threads"].append({**base, "id": "t2", "title": "second chat", "updated": "2026-09-14T00:00:00+00:00",
                         "model": None})
    w["threads"].append({**base, "id": "t3", "project_id": "p2", "title": "school chat",
                         "updated": "2026-09-13T00:00:00+00:00", "model": None, "cwd": "/mnt/c/myday/schoolwork"})
    w["transcripts"]["t2"] = [
        {"role": "user", "text": "second thread question", "at": "2026-09-14T00:00:00+00:00"},
        {"role": "assistant", "text": "second thread answer", "at": "2026-09-14T00:00:01+00:00"},
    ]
    w["transcripts"]["t3"] = [
        {"role": "user", "text": "homework question", "at": "2026-09-13T00:00:00+00:00"},
    ]


def _fresh(page, mock, until, ws: str, size=(1600, 900)):
    """Reload into a stored workspace at 100%, dictation REVIEW."""
    page.set_viewport_size({"width": size[0], "height": size[1]})
    page.evaluate("""(ws) => {
      localStorage.setItem('jarvis.hud.zoom', '100');
      localStorage.setItem('jarvis.hud.layout', '{}');
      localStorage.setItem('jarvis.hud.workspace', ws);
      localStorage.setItem('jarvis.dictation', 'review');
    }""", ws)
    before = mock.sse_connections()
    page.reload()
    page.wait_for_selector('[data-testid="sidebar"]', state="attached")
    until(lambda: page.locator('[data-testid="project-p1"]').count() > 0)
    mock.await_reconnect(before)
    until(lambda: page.locator('[data-testid="compose-row"]').count() > 0)
    time.sleep(0.3)


def _chats(page) -> dict:
    return page.evaluate("window.__hud.state().chats")


def _chat(page, n: int) -> dict:
    return _chats(page)[str(n)]


def _box(page, n: int):
    return page.locator(_pane(n, '[data-testid="input"]'))


def _log(page, n: int) -> str:
    loc = page.locator(_pane(n, '[data-testid="log"]'))
    return loc.inner_text() if loc.count() else ""


def _expand(page, until, project_id: str, child: str):
    for _ in range(4):
        if page.locator(f'[data-testid="{child}"]').count() > 0:
            return True
        page.locator(f'[data-testid="project-{project_id}"]').click()
        until(lambda: page.locator(f'[data-testid="{child}"]').count() > 0, timeout=1.0)
    return page.locator(f'[data-testid="{child}"]').count() > 0


def _focus(page, until, n: int):
    """Use pane N's chat: a click in its box focuses it (and makes it the selected chat)."""
    _box(page, n).click()
    until(lambda: _attr(page, _pane(n), "data-focused") == "true", timeout=2)


def _open(page, until, n: int, tid: str, project: str = "p1"):
    """Open thread `tid` in chat pane N: focus the pane, then click the thread."""
    _focus(page, until, n)
    _expand(page, until, project, f"thread-{tid}")
    page.locator(f'[data-testid="thread-{tid}"]').click()
    until(lambda: _chat(page, n)["threadId"] == tid, timeout=3)
    until(lambda: _log(page, n) != "", timeout=3)


def _dictation(page, mode: str):
    """Set the dictation mode on the orb's strip (PR #29): it acts on the
    selected chat, and clicking it selects no pane."""
    page.locator(f'#orbdock [data-testid="dictation-{mode}"]').click()


def _status_lines(page) -> list:
    """Which chat panes draw a status line of their own (`pane-status`)."""
    return [page.locator(_pane(n, '[data-testid="pane-status"]')).count() for n in (1, 2, 3, 4)]


def _sent(mock, tid: str) -> list:
    return mock.sent("POST", f"/threads/{tid}/send")


def _count_follow_ups(page):
    """Count the follow-up mic windows opened, at the call (the fake mic can open one itself)."""
    page.evaluate("""() => {
      const c = window.__hud.capture;
      if (!c.__counted) {
        const open = c.openFollowUp.bind(c);
        window.__followUps = 0;
        c.openFollowUp = () => { window.__followUps++; return open(); };
        c.__counted = true;
      }
      return true;
    }""")


UTTERANCE = """() => {
  const m = window.__hud.mic;
  m.feedMs(2000, 0.001);
  window.__hud.capture.openFollowUp();
  m.feedMs(1600, 0.06); m.feedMs(2200, 0.0005);
  return true;
}"""


# ---------------------------------------------------------------------------

def _side_by_side_checks(page, mock, check, until):
    _fresh(page, mock, until, TWO)
    rows = page.locator('[data-testid="compose-row"]')
    check("two chat panes side by side, each composing its own new thread",
          _attr(page, _pane(1), "data-view") == "chat" and _attr(page, _pane(2), "data-view") == "chat"
          and rows.count() == 2, str(rows.count()))
    labels = sorted(rows.locator(".nm").all_inner_texts())
    check("the sidebar numbers the compose rows by pane", labels == ["New thread · 1", "New thread · 2"], str(labels))
    own = all(page.locator(_pane(n, f'[data-testid="{sid}"]')).count() == 1
              for n in (1, 2) for sid in ("input", "send", "project-chip", "provider-chip-select", "log"))
    check("each pane has its own box, Send, project chip, model chip and log", own)

    _open(page, until, 1, "t1")
    _open(page, until, 2, "t2")
    check("pane 1 shows t1 and pane 2 shows t2",
          "what is the plan" in _log(page, 1) and "second thread question" in _log(page, 2),
          f"{_log(page, 1)[:60]!r} / {_log(page, 2)[:60]!r}")
    check("each transcript is drawn in its own pane only",
          "second thread" not in _log(page, 1) and "what is the plan" not in _log(page, 2))
    c = _chats(page)
    check("the store holds one conversation per pane", c["1"]["threadId"] == "t1" and c["2"]["threadId"] == "t2",
          f"{c['1']['threadId']} {c['2']['threadId']}")
    r1 = page.locator('[data-testid="thread-t1"]')
    r2 = page.locator('[data-testid="thread-t2"]')
    check("the sidebar marks both: the pane used last with the accent, the other dimmer",
          " sel" in f" {r2.get_attribute('class')}" and "panesel" in (r1.get_attribute("class") or "")
          and " sel " not in f" {r1.get_attribute('class')} ", f"{r1.get_attribute('class')} | {r2.get_attribute('class')}")
    check("each marked row says which pane it is open in",
          r1.get_attribute("data-pane") == "1" and r2.get_attribute("data-pane") == "2")
    check("no compose rows once both panes hold threads", rows.count() == 0)
    ctx = [page.locator(_pane(n, ".panectx")).inner_text() for n in (1, 2)]
    check("each pane's header names its own thread", ctx == ["desk chat", "second chat"], str(ctx))
    check("project chips read each pane's own project",
          "jarvis" in page.locator(_pane(1, '[data-testid="project-chip"]')).inner_text()
          and "jarvis" in page.locator(_pane(2, '[data-testid="project-chip"]')).inner_text())


def _never_twice_checks(page, mock, check, until):
    # Pane 2 is focused. A click on t1, which pane 1 shows, focuses pane 1.
    loads = len(mock.sent("GET", "/threads/t1/transcript"))
    page.locator('[data-testid="thread-t1"]').click()
    until(lambda: _attr(page, _pane(1), "data-focused") == "true", timeout=2)
    c = _chats(page)
    check("a click on a thread another pane shows focuses that pane",
          _attr(page, _pane(1), "data-focused") == "true" and c["1"]["threadId"] == "t1" and c["2"]["threadId"] == "t2")
    time.sleep(0.2)
    check("and opens it nowhere else, reloading nothing",
          len(mock.sent("GET", "/threads/t1/transcript")) == loads
          and sum(1 for n in "1234" if c[n]["threadId"] == "t1") == 1)
    page.locator('[data-testid="thread-t2"]').click(modifiers=["Alt"])
    until(lambda: _attr(page, _pane(2), "data-focused") == "true", timeout=2)
    check("Alt+click on a thread already on screen focuses its pane too, splitting nothing",
          _attr(page, '[data-testid="workspace"]', "data-preset") == "cols2"
          and _chat(page, 2)["threadId"] == "t2" and _chat(page, 1)["threadId"] == "t1")

    # Held by a pane the layout hides: it moves into the pane it is opened in.
    _choose(page, until, "single")
    until(lambda: not _visible(page, _pane(2)), timeout=2)
    page.locator('[data-testid="thread-t2"]').click()
    until(lambda: _chat(page, 1)["threadId"] == "t2", timeout=3)
    c = _chats(page)
    check("a thread held by a hidden pane moves into the pane it is opened in",
          c["1"]["threadId"] == "t2" and "second thread question" in _log(page, 1), str(c["1"]["threadId"]))
    check("the two panes trade conversations, so it is open in one pane only",
          c["2"]["threadId"] == "t1" and sum(1 for n in "1234" if c[n]["threadId"] == "t2") == 1,
          str(c["2"]["threadId"]))

    # Open beside, from one pane: two columns, the thread in pane 2.
    _expand(page, until, "p2", "thread-t3")
    page.locator('[data-testid="thread-t3"]').click(modifiers=["Alt"])
    until(lambda: _attr(page, '[data-testid="workspace"]', "data-preset") == "cols2", timeout=3)
    until(lambda: _chat(page, 2)["threadId"] == "t3", timeout=3)
    check("from one pane, Alt+click opens the thread beside it: two columns, pane 2",
          _attr(page, '[data-testid="workspace"]', "data-preset") == "cols2"
          and _attr(page, _pane(2), "data-view") == "chat" and _chat(page, 2)["threadId"] == "t3"
          and _attr(page, _pane(2), "data-focused") == "true" and _chat(page, 1)["threadId"] == "t2")
    until(lambda: "homework question" in _log(page, 2), timeout=3)
    check("and its transcript is drawn there", "homework question" in _log(page, 2))
    chips = [page.locator(_pane(n, '[data-testid="project-chip"]')).inner_text() for n in (1, 2)]
    check("each pane's project chip names its own conversation's project",
          "jarvis" in chips[0] and "schoolwork" in chips[1], str(chips))

    # The ⋯ menu's Open beside: from pane 1, the next pane to the right.
    _focus(page, until, 1)
    _expand(page, until, "p1", "thread-menu-t1")
    page.locator('[data-testid="thread-menu-t1"]').click()
    until(lambda: page.locator('[data-testid="tmenu-beside"]').count() > 0, timeout=2)
    check("a thread's ⋯ menu offers Open beside", page.locator('[data-testid="tmenu-beside"]').count() == 1)
    page.locator('[data-testid="tmenu-beside"]').click()
    until(lambda: _chat(page, 2)["threadId"] == "t1", timeout=3)
    check("which opens it in the next pane to the right",
          _chat(page, 2)["threadId"] == "t1" and _chat(page, 1)["threadId"] == "t2"
          and _attr(page, _pane(2), "data-focused") == "true")


def _two_turns_checks(page, mock, check, until):
    """A turn in pane 1 while the owner types and sends in pane 2; every event
    kind to its own pane; pane 1's Stop never reaches pane 2."""
    _fresh(page, mock, until, TWO)
    _open(page, until, 1, "t1")
    _open(page, until, 2, "t2")
    stop1 = page.locator(_pane(1, '[data-testid="stop"]'))
    stop2 = page.locator(_pane(2, '[data-testid="stop"]'))

    mock.emit("turn_started", {}, thread_id="t1")
    until(lambda: _chat(page, 1)["busy"], timeout=3)
    check("a turn in pane 1's thread makes pane 1 busy, with a Stop — and only pane 1",
          bool(until(lambda: stop1.count() == 1, timeout=2)) and stop2.count() == 0
          and _chat(page, 2)["busy"] is False)
    mock.emit("text_delta", {"text": "working on one"}, thread_id="t1")
    until(lambda: page.locator(_pane(1, '[data-testid="draft"]')).count() > 0, timeout=3)
    check("its deltas draw in pane 1 only",
          "working on one" in page.locator(_pane(1, '[data-testid="draft"]')).inner_text()
          and page.locator(_pane(2, '[data-testid="draft"]')).count() == 0)

    # Typing and sending in pane 2 while pane 1's turn runs.
    before1, before2 = len(_sent(mock, "t1")), len(_sent(mock, "t2"))
    box2 = _box(page, 2)
    box2.click()
    box2.fill("hello from pane two")
    box2.press("Enter")
    sent = until(lambda: _sent(mock, "t2")[before2:] or None, timeout=3)
    check("Enter in pane 2 sends to pane 2's thread while pane 1's turn runs",
          bool(sent) and sent[-1].get("text") == "hello from pane two", str(sent))
    check("nothing goes to pane 1's thread", len(_sent(mock, "t1")) == before1)
    check("pane 2 draws its message, pane 1 does not",
          "hello from pane two" in _log(page, 2) and "hello from pane two" not in _log(page, 1))
    until(lambda: _chat(page, 2)["status"] == "THINKING", timeout=3)
    c = _chats(page)
    check("each pane tracks its own turn",
          c["2"]["busy"] and c["2"]["turnThreadId"] == "t2" and c["1"]["busy"] and c["1"]["turnThreadId"] == "t1",
          f"{c['1']['turnThreadId']} {c['2']['turnThreadId']}")
    check("and pane 1 keeps its Stop and its draft", stop1.count() == 1
          and "working on one" in page.locator(_pane(1, '[data-testid="draft"]')).inner_text())

    # Every event kind for pane 2's thread lands in pane 2.
    mock.emit("turn_started", {}, thread_id="t2")
    mock.emit("tool_started", {"call_id": "c2", "name": "grep_files"}, thread_id="t2")
    until(lambda: page.locator(_pane(2, '[data-testid="op"]')).count() > 0, timeout=3)
    check("tool_started: pane 2's ticker, not pane 1's",
          "grep_files" in page.locator(_pane(2, '[data-testid="ops"]')).inner_text()
          and page.locator(_pane(1, '[data-testid="op"]')).count() == 0)
    check("and pane 2's own status says what it runs — on the orb, pane 2 being the selected chat",
          "RUNNING · grep_files" in _chat(page, 2)["status"]
          and "grep_files" in page.locator('[data-testid="orb-status"]').inner_text().lower()
          and page.locator(_pane(2, '[data-testid="pane-status"]')).count() == 0,
          _chat(page, 2)["status"])
    check("while pane 1's input bar still says what its own turn is doing",
          "responding" in page.locator(_pane(1, '[data-testid="pane-status"]')).inner_text().lower(),
          page.locator(_pane(1, '[data-testid="pane-status"]')).inner_text())
    mock.emit("tool_finished", {"call_id": "c2", "name": "grep_files", "ok": True}, thread_id="t2")
    until(lambda: "done" in page.locator(_pane(2, '[data-testid="ops"]')).inner_text(), timeout=3)
    check("tool_finished: marked done in pane 2", "running" not in page.locator(_pane(2, '[data-testid="ops"]')).inner_text())
    mock.emit("text_delta", {"text": "two is "}, thread_id="t2")
    until(lambda: page.locator(_pane(2, '[data-testid="draft"]')).count() > 0, timeout=3)
    check("text_delta: pane 2's draft, pane 1's left as it was",
          "two is" in page.locator(_pane(2, '[data-testid="draft"]')).inner_text()
          and "working on one" in page.locator(_pane(1, '[data-testid="draft"]')).inner_text()
          and "two is" not in page.locator(_pane(1, '[data-testid="draft"]')).inner_text())
    mock.emit("text", {"text": "**answer** for two"}, thread_id="t2")
    until(lambda: page.locator(_pane(2, '[data-testid="draft"]')).count() == 0, timeout=3)
    check("text: settles in pane 2 only",
          "answer for two" in _log(page, 2) and "answer for two" not in _log(page, 1)
          and page.locator(_pane(1, '[data-testid="draft"]')).count() == 1)
    mock.emit("user_message", {"text": "from my phone", "typed": "from my phone", "via": "discord"},
              thread_id="t2", project_id="p1")
    until(lambda: "from my phone" in _log(page, 2), timeout=3)
    check("user_message from Discord: pane 2, labelled, not pane 1",
          page.locator(_pane(2, '[data-testid="via-discord"]')).count() == 1 and "from my phone" not in _log(page, 1))
    mock.emit("model_set", {"text": "model → claude-haiku-4-5"}, thread_id="t2")
    until(lambda: "claude-haiku-4-5" in _log(page, 2), timeout=3)
    check("model_set: a line in pane 2 only", "claude-haiku-4-5" not in _log(page, 1))
    mock.emit("proposal_reply", {"reply": "Starting a task for two.", "task_id": "k1"}, thread_id="t2")
    until(lambda: page.locator(_pane(2, '[data-testid="proposal"]')).count() > 0, timeout=3)
    check("proposal_reply: pane 2 only", page.locator(_pane(1, '[data-testid="proposal"]')).count() == 0)
    mock.emit("error", {"message": "two is cooling", "fatal": False}, thread_id="t2")
    until(lambda: "two is cooling" in (page.evaluate("window.__hud.state().error") or ""), timeout=3)
    c = _chats(page)
    check("error: said, and neither pane's turn ends", c["2"]["busy"] and c["1"]["busy"] and stop1.count() == 1
          and stop2.count() == 1)
    page.evaluate("window.__hud.dispatch({type: 'patch', patch: {error: ''}})")
    # No thread: the selected chat's pane (pane 2, used last).
    mock.emit("text_delta", {"text": "unthreaded words"})
    until(lambda: "unthreaded" in _chat(page, 2)["draft"], timeout=3)
    check("an event with no thread goes to the selected chat's pane",
          "unthreaded" in _chat(page, 2)["draft"] and "unthreaded" not in _chat(page, 1)["draft"])
    mock.emit("text", {"text": "unthreaded words"})
    until(lambda: _chat(page, 2)["draft"] == "", timeout=3)

    # Pane 1's Stop interrupts pane 1's thread, never pane 2's.
    i1, i2 = len(mock.sent("POST", "/threads/t1/interrupt")), len(mock.sent("POST", "/threads/t2/interrupt"))
    stop1.click()
    until(lambda: len(mock.sent("POST", "/threads/t1/interrupt")) > i1, timeout=3)
    time.sleep(0.2)
    check("pane 1's Stop interrupts pane 1's thread",
          len(mock.sent("POST", "/threads/t1/interrupt")) == i1 + 1)
    check("and never pane 2's", len(mock.sent("POST", "/threads/t2/interrupt")) == i2)
    mock.emit("turn_finished", {"stop": "interrupted"}, thread_id="t1")
    until(lambda: _chat(page, 1)["busy"] is False, timeout=3)
    check("pane 1's turn ends: its Stop goes, pane 2 keeps running with its own",
          bool(until(lambda: stop1.count() == 0, timeout=2)) and _chat(page, 2)["busy"] and stop2.count() == 1)
    mock.emit("turn_finished", {"stop": "end"}, thread_id="t2")
    until(lambda: _chat(page, 2)["busy"] is False, timeout=3)
    check("and pane 2's turn ends on its own finish", stop2.count() == 0)


def _moved_turn_checks(page, mock, check, until):
    """A pane that moves to another thread mid-turn still hears that turn
    finish; the pane that then opens the thread takes its turn and its Stop."""
    mock.emit("turn_started", {}, thread_id="t1")
    until(lambda: _chat(page, 1)["busy"], timeout=3)
    _open(page, until, 1, "t3", "p2")
    c = _chat(page, 1)
    check("pane 1 moved on mid-turn still waits on t1's turn, with no Stop for t3",
          c["threadId"] == "t3" and c["busy"] and c["turnThreadId"] == "t1"
          and page.locator(_pane(1, '[data-testid="stop"]')).count() == 0, str(c["turnThreadId"]))
    _open(page, until, 2, "t1")
    c = _chats(page)
    check("opening t1 in pane 2 brings its running turn along, Stop and all",
          c["2"]["busy"] and c["2"]["turnThreadId"] == "t1"
          and bool(until(lambda: page.locator(_pane(2, '[data-testid="stop"]')).count() == 1, timeout=2)))
    check("and pane 1 is free", c["1"]["busy"] is False and c["1"]["turnThreadId"] is None)
    mock.emit("turn_finished", {"stop": "end"}, thread_id="t1")
    check("t1's finish frees pane 2", bool(until(lambda: _chat(page, 2)["busy"] is False, timeout=3)))


def _voice_checks(page, mock, check, until):
    _fresh(page, mock, until, TWO)
    _open(page, until, 1, "t1")
    _open(page, until, 2, "t2")
    _count_follow_ups(page)
    check("the chat pane used last is the selected chat: its pane says so, with a mic mark",
          page.evaluate("window.__hud.state().selectedChat") == 2 and _attr(page, _pane(2), "data-selected") == "true"
          and page.locator('[data-testid="pane-2-mic"]').count() == 1
          and _attr(page, _pane(1), "data-selected") is None and page.locator('[data-testid="pane-1-mic"]').count() == 0)
    check("no input bar draws the dictation strip (it is on the orb); the other pane shows its own status",
          all(page.locator(_pane(n, '[data-testid="dictation"]')).count() == 0 for n in (1, 2))
          and all(page.locator(_pane(n, '[data-testid="level"]')).count() == 0 for n in (1, 2))
          and page.locator('#orbdock [data-testid="dictation"]').count() == 1
          and page.locator(_pane(1, '[data-testid="pane-status"]')).count() == 1
          and page.locator(_pane(2, '[data-testid="pane-status"]')).count() == 0)

    # A long turn in pane 1: the orb does not follow it, and the mic stays open.
    mock.emit("turn_started", {}, thread_id="t1")
    mock.emit("tool_started", {"call_id": "v1", "name": "build"}, thread_id="t1")
    until(lambda: _chat(page, 1)["busy"], timeout=3)
    time.sleep(0.2)
    orb = lambda: page.locator('[data-testid="orb"]').get_attribute("data-state")  # noqa: E731
    check("the orb follows the selected chat's turn: pane 1's running tool does not light it",
          orb() not in ("thinking", "tool"), str(orb()))
    stt = len(mock.sent("POST", "/stt"))
    page.evaluate(UTTERANCE)
    until(lambda: len(mock.sent("POST", "/stt")) > stt, timeout=4)
    check("a long turn in another pane no longer silences the mic", len(mock.sent("POST", "/stt")) > stt)
    until(lambda: _box(page, 2).input_value() != "", timeout=4)
    check("REVIEW puts the transcript in the selected chat's box",
          "what is the weather" in _box(page, 2).input_value(), _box(page, 2).input_value())
    check("and not in the other pane's", _box(page, 1).input_value() == "", _box(page, 1).input_value())
    _box(page, 2).fill("")

    # AUTO sends to the selected chat's thread.
    _dictation(page, "auto")
    check("the orb's strip acts for the selected chat and selects no pane", _sel(page) == 2)
    s1, s2 = len(_sent(mock, "t1")), len(_sent(mock, "t2"))
    page.evaluate(UTTERANCE)
    sent = until(lambda: _sent(mock, "t2")[s2:] or None, timeout=5)
    check("AUTO sends the utterance to the selected chat's thread",
          bool(sent) and sent[-1].get("text") == "what is the weather" and sent[-1].get("spoken") is True, str(sent))
    check("and nothing to the other pane's", len(_sent(mock, "t1")) == s1)
    _dictation(page, "review")
    until(lambda: _chat(page, 2)["busy"], timeout=3)
    check("the orb now shows the selected chat's own turn", bool(until(lambda: orb() == "thinking", timeout=2)),
          str(orb()))
    stt = len(mock.sent("POST", "/stt"))
    page.evaluate(UTTERANCE)
    time.sleep(1.0)
    check("while the selected chat's own turn runs, the mic is suppressed", len(mock.sent("POST", "/stt")) == stt)

    # The orb's press interrupts the selected chat's turn, never another pane's.
    i1, i2 = len(mock.sent("POST", "/threads/t1/interrupt")), len(mock.sent("POST", "/threads/t2/interrupt"))
    page.evaluate("document.activeElement && document.activeElement.blur()")
    page.keyboard.down("Space")
    time.sleep(0.1)
    page.keyboard.up("Space")
    until(lambda: len(mock.sent("POST", "/threads/t2/interrupt")) > i2, timeout=3)
    time.sleep(0.2)
    check("push-to-talk interrupts the selected chat's turn", len(mock.sent("POST", "/threads/t2/interrupt")) == i2 + 1)
    check("and leaves the other pane's turn running", len(mock.sent("POST", "/threads/t1/interrupt")) == i1
          and _chat(page, 1)["busy"])
    time.sleep(1.5)  # whatever the press recorded settles before the counts below
    _box(page, 2).fill("")

    # The follow-up window opens only when the selected chat's turn ends.
    page.evaluate("window.__hud.capture.closeFollowUp()")
    ups = page.evaluate("window.__followUps")
    mock.emit("turn_finished", {"stop": "end"}, thread_id="t1")
    until(lambda: _chat(page, 1)["busy"] is False, timeout=3)
    time.sleep(0.2)
    check("another pane's turn finishing opens no follow-up mic window", page.evaluate("window.__followUps") == ups)
    mock.emit("turn_finished", {"stop": "interrupted"}, thread_id="t2")
    until(lambda: _chat(page, 2)["busy"] is False, timeout=3)
    check("the selected chat's own turn finishing does", page.evaluate("window.__followUps") > ups)

    # Using the other pane moves the selected chat, the mark and the strip with it.
    _focus(page, until, 1)
    until(lambda: page.evaluate("window.__hud.state().selectedChat") == 1, timeout=2)
    check("using pane 1 makes it the selected chat: the mark and the orb's status line move with it",
          page.locator('[data-testid="pane-1-mic"]').count() == 1 and page.locator('[data-testid="pane-2-mic"]').count() == 0
          and _status_lines(page)[:2] == [0, 1], str(_status_lines(page)))
    stt = len(mock.sent("POST", "/stt"))
    page.evaluate(UTTERANCE)
    until(lambda: _box(page, 1).input_value() != "", timeout=4)
    check("and REVIEW now fills pane 1's box", "what is the weather" in _box(page, 1).input_value(),
          f"{_box(page, 1).input_value()!r}")
    _box(page, 1).fill("")
    page.evaluate("window.__hud.capture.closeFollowUp()")


def _seen_checks(page, mock, check, until):
    seen = lambda path: len(mock.posted(path))  # noqa: E731
    _fresh(page, mock, until, TWO)
    _open(page, until, 1, "t1")
    _open(page, until, 2, "t2")
    before = seen("/threads/t1/seen")
    mock.activity("thread", "t1", "unread")
    check("a thread finishing in a drawn chat pane that is not focused is read",
          bool(until(lambda: seen("/threads/t1/seen") > before, timeout=4)))
    before = seen("/threads/t2/seen")
    mock.activity("thread", "t2", "unread")
    check("and so is the focused pane's, both at once",
          bool(until(lambda: seen("/threads/t2/seen") > before, timeout=4)))
    _choose(page, until, "single")
    until(lambda: not _visible(page, _pane(2)), timeout=2)
    check("setup: one pane drawn, pane 2 (holding t2) hidden",
          _chat(page, 2)["threadId"] == "t2" and not _visible(page, _pane(2)))
    before = seen("/threads/t2/seen")
    mock.activity("thread", "t2", "unread")
    time.sleep(0.8)
    check("a thread in a chat pane the layout hides is not read", seen("/threads/t2/seen") == before)
    mock.activity("thread", "t2", "idle")
    mock.activity("thread", "t1", "idle")


def _steer_pane2_checks(page, mock, check, until):
    """`hud_v2_check.steer_checks`, re-run in pane 2 with pane 1 on another thread."""
    w = mock.world
    _fresh(page, mock, until, TWO)
    _open(page, until, 1, "t1")
    _open(page, until, 2, "t2")
    _count_follow_ups(page)
    stop = page.locator(_pane(2, '[data-testid="stop"]'))
    box = _box(page, 2)
    box1_msgs = page.locator(_pane(1, '[data-testid="msg-user"]')).count()
    mock.emit("turn_started", {}, thread_id="t2")
    until(lambda: _chat(page, 2)["busy"] and _chat(page, 2)["turnThreadId"] == "t2", timeout=3)
    check("pane 2: a running thread offers a Stop", bool(until(lambda: stop.count() == 1, timeout=2)))
    mock.emit("error", {"message": "Codex model cooling", "fatal": False}, thread_id="t2")
    until(lambda: "cooling" in (page.evaluate("window.__hud.state().error") or ""), timeout=3)
    check("pane 2: an error mid-turn does not end the turn", _chat(page, 2)["busy"] and stop.count() == 1)

    w["send_status"] = "steered"
    before = len(_sent(mock, "t2"))
    box.fill("use the other file")
    box.press("Enter")
    sent = until(lambda: _sent(mock, "t2")[before:] or None, timeout=3)
    check("pane 2: Enter during a turn sends the message", bool(sent) and sent[-1].get("text") == "use the other file")
    steered = page.locator(_pane(2, '[data-testid="msg-user"]')).last
    check("pane 2: it appears at once, marked steering",
          bool(until(lambda: "use the other file" in steered.inner_text()
                     and "steering" in steered.inner_text(), timeout=3)), steered.inner_text())
    check("pane 2: the box clears, the turn is still tracked, with its Stop",
          box.input_value() == "" and _chat(page, 2)["busy"] and _chat(page, 2)["turnThreadId"] == "t2"
          and stop.count() == 1)
    check("pane 1 is untouched by it", page.locator(_pane(1, '[data-testid="msg-user"]')).count() == box1_msgs
          and _chat(page, 1)["busy"] is False)
    steer_id = f"msg-{w['sends']}"
    # Only once the send's answer has named the message: the mark goes by that id.
    until(lambda: any(m.get("message_id") == steer_id for m in _chat(page, 2)["messages"]), timeout=3)
    mock.emit("steer_queued", {"message_id": steer_id}, thread_id="t2")
    check("pane 2: steer_queued re-marks that steer queued, in pane 2",
          bool(until(lambda: "queued" in steered.inner_text(), timeout=3)), steered.inner_text())

    w["send_status"] = "queued"
    box.fill("then the docs")
    box.press("Enter")
    queued = page.locator(_pane(2, '[data-testid="msg-user"]')).last
    check("pane 2: a queued message says it will run when this turn ends",
          bool(until(lambda: "queued · will run when this turn ends" in queued.inner_text(), timeout=3)))
    page.evaluate("window.__hud.capture.closeFollowUp()")
    ups = page.evaluate("window.__followUps")
    mock.emit("turn_finished", {"stop": "end", "next": 1}, thread_id="t2")
    page.wait_for_timeout(300)
    check("pane 2: a turn ending with a message waiting keeps the pane on the thread, no follow-up window",
          _chat(page, 2)["busy"] and stop.count() == 1 and page.evaluate("window.__followUps") == ups)
    mock.emit("queued_started", {"message_id": f"msg-{w['sends']}", "turn_id": "turn-2"},
              thread_id="t2", turn_id="turn-2")
    check("pane 2: and stops saying queued once it runs",
          bool(until(lambda: "queued" not in queued.inner_text(), timeout=3)))

    w["fail_send"] = 1
    w["fail_send_error"] = "Three messages are already waiting on this turn"
    count = page.locator(_pane(2, '[data-testid="msg-user"]')).count()
    box.fill("one too many")
    box.press("Enter")
    check("pane 2: a refused send is said inline, in the backend's words",
          bool(until(lambda: "Could not send: Three messages" in (page.evaluate("window.__hud.state().error") or ""),
                     timeout=3)))
    check("pane 2: the typed words come back to pane 2's box", bool(until(lambda: box.input_value() == "one too many",
                                                                       timeout=3)), box.input_value())
    check("pane 2: its bubble is taken back; the turn still tracked",
          page.locator(_pane(2, '[data-testid="msg-user"]')).count() == count and _chat(page, 2)["busy"]
          and stop.count() == 1)
    check("and nothing came back to pane 1's box", _box(page, 1).input_value() == "")
    w.pop("fail_send_error", None)
    box.fill("")

    w["send_status"] = "steered"
    steer_id = f"msg-{w.get('sends', 0) + 1}"
    box.fill("in French, please")
    box.press("Enter")
    undelivered = page.locator(_pane(2, '[data-testid="msg-user"]')).last
    until(lambda: any(m.get("message_id") == steer_id for m in _chat(page, 2)["messages"]), timeout=3)
    mock.emit("queue_cleared", {"reason": "stopped", "messages": [
        {"message_id": steer_id, "typed": "in French, please", "via": "hud"}]}, thread_id="t2")
    check("pane 2: a dropped steer is marked not sent",
          bool(until(lambda: "not sent" in undelivered.inner_text(), timeout=3)))
    check("pane 2: and its words come back to pane 2's box, not pane 1's",
          bool(until(lambda: "in French, please" in box.input_value(), timeout=3))
          and _box(page, 1).input_value() == "", box.input_value())
    box.fill("")

    w["send_status"] = "queued"
    box.fill("later, please")
    box.press("Enter")
    later = page.locator(_pane(2, '[data-testid="msg-user"]')).last
    until(lambda: "queued" in later.inner_text(), timeout=3)
    dropped_id = f"msg-{w['sends']}"
    i1, i2 = len(mock.sent("POST", "/threads/t1/interrupt")), len(mock.sent("POST", "/threads/t2/interrupt"))
    stop.click()
    check("pane 2: Stop interrupts pane 2's thread",
          bool(until(lambda: len(mock.sent("POST", "/threads/t2/interrupt")) > i2, timeout=3)))
    check("and not pane 1's", len(mock.sent("POST", "/threads/t1/interrupt")) == i1)
    mock.emit("queue_cleared", {"reason": "stopped", "messages": [
        {"message_id": dropped_id, "typed": "later, please", "via": "hud"}]}, thread_id="t2")
    check("pane 2: what the stop dropped is marked not sent, and its words come back to pane 2's box",
          bool(until(lambda: "not sent" in later.inner_text() and "later, please" in box.input_value(), timeout=3)))
    box.fill("")
    mock.emit("turn_finished", {"stop": "interrupted"}, thread_id="t2")
    check("pane 2: the turn's end frees the pane", bool(until(lambda: _chat(page, 2)["busy"] is False, timeout=3)))
    check("pane 2: and, as the selected chat's, opens the follow-up mic window",
          page.evaluate("window.__followUps") > ups)
    check("pane 2: and the Stop button goes", bool(until(lambda: stop.count() == 0, timeout=2)))
    w.pop("send_status", None)

    mock.emit("turn_started", {}, thread_id="t2")
    until(lambda: _chat(page, 2)["busy"], timeout=3)
    thread = next(t for t in w["threads"] if t["id"] == "t2")
    thread["running"] = False
    page.evaluate("window.__hud.reconcile()")
    check("pane 2: a missed turn_finished cannot wedge the pane",
          bool(until(lambda: _chat(page, 2)["busy"] is False, timeout=3)))
    thread.pop("running", None)
    page.evaluate("window.__hud.dispatch({type: 'patch', patch: {error: ''}})")


def _giveback_pane2_checks(page, mock, check, until):
    """`hud_v2_check.handed_back_checks` in pane 2: words go where they were
    typed, never into the other pane's box, and wait for their thread."""
    w = mock.world
    box = _box(page, 2)
    box1 = _box(page, 1)
    mock.emit("turn_started", {}, thread_id="t2")
    until(lambda: _chat(page, 2)["busy"] and _chat(page, 2)["turnThreadId"] == "t2", timeout=3)
    w["send_status"] = "dropped"
    box.fill("a steer the owner stopped")
    box.press("Enter")
    dropped = page.locator(_pane(2, '[data-testid="msg-user"]')).last
    check("pane 2: a steer a Stop overtook is marked not sent",
          bool(until(lambda: "not sent" in dropped.inner_text(), timeout=3)))
    check("pane 2: and its words come back to pane 2's box",
          bool(until(lambda: box.input_value() == "a steer the owner stopped", timeout=3))
          and box1.input_value() == "", box.input_value())
    box.fill("")

    w["send_status"] = "queued"
    box.fill("words for thread two")
    box.press("Enter")
    waiting = page.locator(_pane(2, '[data-testid="msg-user"]')).last
    until(lambda: "queued" in waiting.inner_text(), timeout=3)
    queued_id = f"msg-{w['sends']}"
    _open(page, until, 2, "t3", "p2")        # pane 2 moves on; pane 1 still shows t1
    mock.emit("queue_cleared", {"reason": "stopped", "messages": [
        {"message_id": queued_id, "typed": "words for thread two", "via": "hud"}]}, thread_id="t2")
    page.wait_for_timeout(400)
    check("words cleared on a thread no pane shows land in no box",
          box.input_value() == "" and box1.input_value() == "", f"{box.input_value()!r} {box1.input_value()!r}")
    _focus(page, until, 2)
    page.locator('[data-testid="thread-t2"]').click()
    check("they come back when a pane opens that thread",
          bool(until(lambda: box.input_value() == "words for thread two", timeout=3))
          and box1.input_value() == "", box.input_value())
    box.fill("")
    _open(page, until, 2, "t3", "p2")
    _focus(page, until, 2)
    page.locator('[data-testid="thread-t2"]').click()
    until(lambda: _chat(page, 2)["threadId"] == "t2", timeout=3)
    page.wait_for_timeout(300)
    check("and only once", box.input_value() == "", box.input_value())
    w.pop("send_status", None)
    mock.emit("turn_finished", {"stop": "interrupted"}, thread_id="t2")
    until(lambda: _chat(page, 2)["busy"] is False, timeout=3)


def _card_focus_checks(page, mock, check, until):
    """Review of PR #26: a card took focus off a frame and the workspace only,
    so Enter on a focused sidebar button still pressed it behind the card."""
    _fresh(page, mock, until, TWO)
    _open(page, until, 1, "t1")
    page.locator('[data-testid="new-thread"]').focus()
    check("setup: the sidebar's New thread button has focus", _active_testid(page) == "new-thread")
    threads_before = _chat(page, 1)["threadId"]
    _approval(mock, "sbfocus1")
    until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
    time.sleep(0.2)
    focus = page.evaluate("[document.activeElement.tagName, document.activeElement.getAttribute('data-testid')]")
    check("a card takes focus off a focused sidebar button, onto the card and not a button",
          focus == ["DIV", "approval-card"], str(focus))
    page.keyboard.press("Enter")
    page.keyboard.press("Space")
    time.sleep(0.3)
    check("so Enter and Space behind the card press nothing",
          _chat(page, 1)["threadId"] == threads_before and page.locator('[data-testid="approval-card"]').count() == 1
          and page.evaluate("window.__hud.capture.ptt") is None)
    page.keyboard.press("Escape")
    body = until(lambda: mock.sent("POST", "/approvals/sbfocus1") or None, timeout=4)
    check("and Escape reaches the card and denies", bool(body) and body[-1].get("decision") == "deny")
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
    time.sleep(0.1)
    check("once it is answered, focus goes back to the button", _active_testid(page) == "new-thread",
          str(_active_testid(page)))

    # A pane's own tab button behind the card: the same.
    page.locator(_pane(2, '[data-testid="tab-file"]')).focus()
    _approval(mock, "sbfocus2")
    until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
    time.sleep(0.2)
    page.keyboard.press("Enter")
    time.sleep(0.2)
    check("Enter on a pane's tab that had focus does not switch it behind the card",
          _attr(page, _pane(2), "data-view") == "chat" and _active_testid(page) == "approval-card")
    page.keyboard.press("Escape")
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)


def _dirty_file_checks(page, mock, check, until):
    """A File pane holding an unsaved edit is never switched away: not by its
    own tabs, Open beside, a sidebar thread, a task or New thread."""
    _fresh(page, mock, until, CHAT_FILE)
    _open(page, until, 1, "t1")
    page.locator(_pane(2, '[data-testid="file-calc.py"]')).click()
    until(lambda: page.locator(_pane(2, '[data-testid="file-path"]')).inner_text() == "calc.py", timeout=4)
    _edit(page, until, 2, "\n# kept across refusals")
    save = page.locator(_pane(2, '[data-testid="file-save"]'))
    until(lambda: not save.is_disabled(), timeout=4)
    check("setup: pane 2 holds an unsaved edit", not save.is_disabled())
    refused = page.locator('[data-testid="pane-2-refused"]')

    def kept(what: str):
        return (_attr(page, _pane(2), "data-view") == "file"
                and page.locator(_pane(2, '[data-testid="file-path"]')).inner_text() == "calc.py"
                and not save.is_disabled())

    page.locator(_pane(2, '[data-testid="tab-chat"]')).click()
    until(lambda: refused.count() > 0, timeout=2)
    check("its own chat tab is refused: the pane stays on the file, the edit kept", kept("tab"))
    check("and the pane says why", refused.count() == 1 and "unsaved" in refused.inner_text(), refused.inner_text()
          if refused.count() else "")
    page.locator(_pane(2, '[data-testid="tab-preview"]')).click()
    time.sleep(0.2)
    check("so is any other view", kept("preview"))

    # Open beside from pane 1 would land in pane 2.
    _focus(page, until, 1)
    _expand(page, until, "p2", "thread-t3")
    page.locator('[data-testid="thread-t3"]').click(modifiers=["Alt"])
    time.sleep(0.3)
    check("Open beside into it is refused", kept("beside") and _chat(page, 2)["threadId"] is None
          and _chat(page, 1)["threadId"] == "t1")

    # With no chat drawn, a sidebar thread, a task and New thread would all land in the focused pane.
    page.locator(_pane(1, '[data-testid="tab-preview"]')).click()
    until(lambda: _attr(page, _pane(1), "data-view") == "preview", timeout=2)
    page.locator(_pane(2, '[data-testid="file-path"]')).click()
    until(lambda: _attr(page, _pane(2), "data-focused") == "true", timeout=2)
    page.locator('[data-testid="thread-t1"]').click()
    time.sleep(0.3)
    check("a sidebar thread landing in it is refused", kept("thread")
          and _attr(page, _pane(1), "data-view") == "preview")
    page.locator('[data-testid="task-k1"]').click()
    time.sleep(0.3)
    check("so is a task", kept("task"))
    page.locator('[data-testid="new-thread"]').click()
    time.sleep(0.3)
    check("and New thread", kept("new thread"))

    save.click()
    until(lambda: save.is_disabled(), timeout=4)
    page.locator(_pane(2, '[data-testid="tab-chat"]')).click()
    until(lambda: _attr(page, _pane(2), "data-view") == "chat", timeout=2)
    check("once saved, the pane switches as asked", _attr(page, _pane(2), "data-view") == "chat")


# ---------------------------------------------------------------------------
# Review of PR #27: words or a transcript attached to the wrong thread. Each
# of these failed on dbd2096 (the reviewer's probes, made permanent).

def _pumped(page) -> bool:
    """Let Playwright run its route callbacks: the sync API only does so
    inside a call, so a plain poll would never see a request being held."""
    page.wait_for_timeout(1)
    return True


SINGLE = json.dumps({"preset": "single", "focused": 1,
                     "panes": [{"view": "chat"}, {"view": "chat"}, {"view": "file"}, {"view": "task"}]})
SEND_URL = re.compile(r".*/threads/t\d+/send$")
OPEN_URL = re.compile(r".*/threads$")


def _rv_giveback_hidden_swap_checks(page, mock, check, until):
    """Words handed back while a hidden pane held their thread: they must
    come back in that thread's box, never stay behind in a box that a trade
    of conversations then gives another thread."""
    w = mock.world
    _fresh(page, mock, until, TWO)
    _open(page, until, 1, "t1")
    _open(page, until, 2, "t2")
    mock.emit("turn_started", {}, thread_id="t2")
    until(lambda: _chat(page, 2)["busy"], timeout=3)
    w["send_status"] = "queued"
    _box(page, 2).fill("words meant for t2")
    _box(page, 2).press("Enter")
    until(lambda: any(m.get("message_id") for m in _chat(page, 2)["messages"]), timeout=3)
    qid = f"msg-{w['sends']}"
    w.pop("send_status", None)
    _focus(page, until, 1)
    _choose(page, until, "single")
    until(lambda: not _visible(page, _pane(2)), timeout=2)
    mock.emit("queue_cleared", {"reason": "stopped", "messages": [
        {"message_id": qid, "typed": "words meant for t2", "via": "hud"}]}, thread_id="t2")
    time.sleep(0.6)
    page.locator('[data-testid="thread-t2"]').click()
    until(lambda: _chat(page, 1)["threadId"] == "t2", timeout=3)
    time.sleep(0.5)
    check("review: words handed back while a hidden pane held their thread come back in its box when it opens",
          "words meant for t2" in _box(page, 1).input_value(), repr(_box(page, 1).input_value()))
    check("review: and are never left in a box that now shows another thread",
          "words meant for t2" not in _box(page, 2).input_value(), repr(_box(page, 2).input_value()))
    _choose(page, until, "cols2")
    before = len(_sent(mock, "t1"))
    _box(page, 2).press("Enter")
    time.sleep(0.4)
    check("review: so Enter in the other pane sends none of them to t1",
          not any("meant for t2" in (b.get("text") or "") for b in _sent(mock, "t1")[before:]))
    _box(page, 1).fill("")
    mock.emit("turn_finished", {"stop": "interrupted"}, thread_id="t2")


def _rv_draft_checks(page, mock, check, until):
    """An unsent draft belongs to its conversation: it follows its thread into
    another pane, and a pane that opens another thread does not keep it."""
    _fresh(page, mock, until, TWO)
    _open(page, until, 1, "t1")
    _open(page, until, 2, "t2")
    _box(page, 2).fill("half-typed for t2")
    _focus(page, until, 1)
    _choose(page, until, "single")
    page.locator('[data-testid="thread-t2"]').click()
    until(lambda: _chat(page, 1)["threadId"] == "t2", timeout=3)
    time.sleep(0.3)
    check("review: an unsent draft follows its thread into the pane that opens it",
          _box(page, 1).input_value() == "half-typed for t2", repr(_box(page, 1).input_value()))
    check("review: and stays nowhere else", "half-typed" not in _box(page, 2).input_value(),
          repr(_box(page, 2).input_value()))
    _box(page, 1).fill("")

    _fresh(page, mock, until, SINGLE)
    _open(page, until, 1, "t1")
    _box(page, 1).fill("draft for t1")
    page.locator('[data-testid="thread-t2"]').click()
    until(lambda: _chat(page, 1)["threadId"] == "t2", timeout=3)
    time.sleep(0.3)
    check("review: one pane opening another thread does not carry the draft into it",
          _box(page, 1).input_value() == "", repr(_box(page, 1).input_value()))
    page.locator('[data-testid="thread-t1"]').click()
    until(lambda: _chat(page, 1)["threadId"] == "t1", timeout=3)
    check("review: and the draft is back when its thread is",
          bool(until(lambda: _box(page, 1).input_value() == "draft for t1", timeout=2)),
          repr(_box(page, 1).input_value()))
    _box(page, 1).fill("")


def _rv_compose_swap_race_checks(page, mock, check, until):
    """A first send whose conversation is traded into another pane while its
    `/send` is in flight writes only where that conversation is."""
    _fresh(page, mock, until, TWO)
    _open(page, until, 2, "t2")
    _focus(page, until, 1)
    held = []
    page.route(SEND_URL, lambda r: held.append(r))
    try:
        _box(page, 1).fill("first words of a new thread")
        _box(page, 1).press("Enter")
        until(lambda: _pumped(page) and len(held) > 0, timeout=3)
        new = (_chat(page, 1).get("compose") or {}).get("openedId")
        check("setup: pane 1's first send has opened its thread and is in flight", bool(new) and len(held) == 1, str(new))
        _focus(page, until, 2)
        page.set_viewport_size({"width": 700, "height": 900})
        until(lambda: not _visible(page, _pane(1)), timeout=3)
        mock.emit("thread_opened", {}, thread_id=new)
        if page.locator('[data-testid="expand-left"]').count():
            page.locator('[data-testid="expand-left"]').click()
        _expand(page, until, "p1", f"thread-{new}")
        page.locator(f'[data-testid="thread-{new}"]').click()
        until(lambda: _chat(page, 2)["threadId"] == new, timeout=3)
        held[0].continue_()
    finally:
        page.unroute(SEND_URL)
    time.sleep(0.8)
    c = _chats(page)
    check("review: a first send that finishes after its thread moved panes leaves it open in one pane",
          sum(1 for k in "1234" if c[k]["threadId"] == new) == 1, str({k: c[k]["threadId"] for k in "12"}))
    check("review: and the pane it left keeps the conversation it was given (t2)", c["1"]["threadId"] == "t2",
          str(c["1"]["threadId"]))
    page.set_viewport_size({"width": 1600, "height": 900})


def _rv_newthread_during_send_checks(page, mock, check, until):
    """Opening another thread while a first send opens its thread: the pane
    stays where the owner put it (on main too, in one pane)."""
    for ws, label in ((TWO, "two panes"), (SINGLE, "one pane")):
        _fresh(page, mock, until, ws)
        if ws == TWO:
            _open(page, until, 2, "t3", "p2")
            _focus(page, until, 1)
        held = []
        page.route(OPEN_URL, lambda r: held.append(r) if r.request.method == "POST" else r.continue_())
        try:
            _box(page, 1).fill("brand new")
            _box(page, 1).press("Enter")
            until(lambda: _pumped(page) and len(held) > 0, timeout=3)
            _expand(page, until, "p1", "thread-t2")
            page.locator('[data-testid="thread-t2"]').click()
            until(lambda: _chat(page, 1)["threadId"] == "t2", timeout=3)
            held[0].continue_()
        finally:
            page.unroute(OPEN_URL)
        time.sleep(1.0)
        check(f"review ({label}): opening another thread during a first send keeps the pane on it",
              _chat(page, 1)["threadId"] == "t2", str(_chat(page, 1)["threadId"]))
        check(f"review ({label}): and the pane draws that thread's transcript",
              "second thread question" in _log(page, 1) and "brand new" not in _log(page, 1), _log(page, 1)[:80])


def _rv_transcript_race_checks(page, mock, check, until):
    """A transcript lands in the pane showing its thread when it arrives, or
    nowhere: never in the pane number it was asked for."""
    _fresh(page, mock, until, TWO)
    _open(page, until, 1, "t3", "p2")
    _open(page, until, 2, "t2")
    held = []
    page.route("**/threads/t1/transcript", lambda r: held.append(r))
    try:
        _focus(page, until, 1)
        _choose(page, until, "single")
        until(lambda: not _visible(page, _pane(2)), timeout=2)
        _expand(page, until, "p1", "thread-t1")
        page.locator('[data-testid="thread-t1"]').click()
        until(lambda: _pumped(page) and len(held) > 0, timeout=3)
        page.locator('[data-testid="thread-t2"]').click()
        until(lambda: _chat(page, 1)["threadId"] == "t2", timeout=3)
        time.sleep(0.2)
        held[0].continue_()
    finally:
        page.unroute("**/threads/t1/transcript")
    time.sleep(0.8)
    log1 = _log(page, 1)
    check("review: a late transcript never lands in a pane that moved on (swap)",
          _chat(page, 1)["threadId"] == "t2" and "second thread question" in log1 and "what is the plan" not in log1,
          log1[:80])
    c2 = _chat(page, 2)
    texts = " ".join(m.get("text", "") for m in c2["messages"])
    check("review: it lands in the pane showing its thread now (the hidden one that took t1)",
          c2["threadId"] == "t1" and "what is the plan" in texts and "homework" not in texts, texts[:80])

    _fresh(page, mock, until, SINGLE)
    held = []
    page.route("**/threads/t1/transcript", lambda r: held.append(r))
    try:
        _expand(page, until, "p1", "thread-t1")
        page.locator('[data-testid="thread-t1"]').click()
        until(lambda: _pumped(page) and len(held) > 0, timeout=3)
        page.locator('[data-testid="thread-t2"]').click()
        until(lambda: "second thread question" in _log(page, 1), timeout=3)
        held[0].continue_()
    finally:
        page.unroute("**/threads/t1/transcript")
    time.sleep(0.6)
    check("review: one pane — a late transcript for the thread it left does not overwrite the one it shows",
          "what is the plan" not in _log(page, 1) and "second thread question" in _log(page, 1), _log(page, 1)[:80])


def _rv_tab_behind_card_checks(page, mock, check, until):
    """With a card up, Tab and Shift+Tab never leave it: Shift+Tab used to
    walk onto a Stop behind the veil, and Enter interrupted the turn."""
    _fresh(page, mock, until, TWO)
    _open(page, until, 1, "t1")
    _open(page, until, 2, "t2")
    mock.emit("turn_started", {}, thread_id="t1")
    mock.emit("turn_started", {}, thread_id="t2")
    until(lambda: page.locator('[data-testid="stop"]').count() == 2, timeout=3)
    page.evaluate("document.activeElement && document.activeElement.blur()")
    calls = len(mock.calls)
    _approval(mock, "tabtrap1")
    until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
    time.sleep(0.3)
    outside = []
    for key in ["Shift+Tab"] * 16 + ["Tab"] * 16:
        page.keyboard.press(key)
        where = page.evaluate("""() => { const a = document.activeElement;
            return a ? [a.getAttribute('data-testid') || a.tagName, !!a.closest('#authveil')] : null; }""")
        if not where or not where[1]:
            outside.append(where)
    check("review: with a card up, Tab and Shift+Tab never leave the card", not outside, str(outside[:4]))
    stops_live = page.evaluate("""() => Array.from(document.querySelectorAll('[data-testid="stop"]'))
                                   .filter(b => !b.closest('[inert]')).length""")
    check("review: and nothing behind it can be reached: everything outside the veil is inert",
          stops_live == 0 and page.evaluate("""() => Array.from(document.getElementById('root').children)
              .filter(el => el.id !== 'authveil').every(el => el.hasAttribute('inert'))"""), str(stops_live))
    sent = [c for c in mock.calls[calls:] if c[0] == "POST" and c[1] != "/stt"]
    check("review: and no request went out", not sent, str(sent[:3]))
    page.keyboard.press("Escape")
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
    check("review: once it is answered nothing stays inert",
          bool(until(lambda: page.evaluate("!document.querySelector('#root > [inert]')"), timeout=2)))
    mock.emit("turn_finished", {"stop": "end"}, thread_id="t1")
    mock.emit("turn_finished", {"stop": "end"}, thread_id="t2")


def _rv_handoff_trade_checks(page, mock, check, until):
    """Pane 1 left t1 running; pane 2, running t2, opens t1: they trade, so
    t1 on screen has its Stop and t2's turn is still heard."""
    _fresh(page, mock, until, TWO)
    _open(page, until, 1, "t1")
    mock.emit("turn_started", {}, thread_id="t1")
    until(lambda: _chat(page, 1)["busy"], timeout=3)
    _open(page, until, 1, "t3", "p2")
    _open(page, until, 2, "t2")
    mock.emit("turn_started", {}, thread_id="t2")
    until(lambda: _chat(page, 2)["busy"], timeout=3)
    _focus(page, until, 2)
    page.locator('[data-testid="thread-t1"]').click()
    until(lambda: _chat(page, 2)["threadId"] == "t1", timeout=3)
    check("review: a pane running its own turn that opens a running thread has that thread's Stop",
          bool(until(lambda: page.locator(_pane(2, '[data-testid="stop"]')).count() == 1, timeout=2)),
          str(page.locator('[data-testid="stop"]').count()))
    c = _chats(page)
    check("review: and the other pane takes its turn off screen, so its finish is still heard",
          c["1"]["busy"] and c["1"]["turnThreadId"] == "t2", str((c["1"]["busy"], c["1"]["turnThreadId"])))
    mock.emit("turn_finished", {"stop": "end"}, thread_id="t2")
    check("review: t2's finish frees pane 1", bool(until(lambda: _chat(page, 1)["busy"] is False, timeout=3)))
    i1 = len(mock.sent("POST", "/threads/t1/interrupt"))
    page.locator(_pane(2, '[data-testid="stop"]')).click()
    check("review: and pane 2's Stop stops t1",
          bool(until(lambda: len(mock.sent("POST", "/threads/t1/interrupt")) > i1, timeout=3)))
    mock.emit("turn_finished", {"stop": "interrupted"}, thread_id="t1")


def _rv_stt_lands_selected_checks(page, mock, check, until):
    """Decisions W-6: voice goes to the selected chat as it is when the
    transcript lands — clicking pane 2 during STT sends the words there."""
    _fresh(page, mock, until, TWO)
    _open(page, until, 1, "t1")
    _open(page, until, 2, "t2")
    _focus(page, until, 1)
    _dictation(page, "auto")
    held = []
    page.route("**/stt", lambda r: held.append(r))
    try:
        page.evaluate(UTTERANCE)
        until(lambda: _pumped(page) and len(held) > 0, timeout=6)
        check("setup: an utterance spoken to pane 1 is being transcribed", len(held) > 0)
        page.locator(_pane(2, '[data-testid="log"]')).click()
        until(lambda: _attr(page, _pane(2), "data-focused") == "true", timeout=2)
        s1, s2 = len(_sent(mock, "t1")), len(_sent(mock, "t2"))
        held[0].continue_()
    finally:
        page.unroute("**/stt")
    sent = until(lambda: _sent(mock, "t2")[s2:] or None, timeout=5)
    check("W-6: clicking pane 2 during STT sends the words to pane 2's chat",
          bool(sent) and sent[-1].get("text") == "what is the weather", str(sent))
    check("W-6: and none to the pane they were spoken in", len(_sent(mock, "t1")) == s1)
    _dictation(page, "review")
    mock.emit("turn_finished", {"stop": "end"}, thread_id="t2")


def _rv_stale_status_checks(page, mock, check, until):
    """What the microphone says moves with the selected chat: a pane that is
    no longer selected never keeps LISTENING · SPEAK NOW."""
    _fresh(page, mock, until, TWO)
    _open(page, until, 1, "t1")
    _open(page, until, 2, "t2")
    page.evaluate("""() => { const m = window.__hud.mic; m.feedMs(2000, 0.001);
        window.__hud.capture.openFollowUp(); m.feedMs(800, 0.06); return true; }""")
    until(lambda: _chat(page, 2)["status"].startswith("LISTENING"), timeout=3)
    check("setup: the selected chat (pane 2) says it is listening", _chat(page, 2)["status"].startswith("LISTENING"))
    page.locator(_pane(1, '[data-testid="log"]')).click()
    time.sleep(0.2)
    st = page.locator(_pane(2, '[data-testid="pane-status"]'))
    txt = st.inner_text() if st.count() else ""
    check("review: a pane that stops being the selected chat does not go on saying LISTENING",
          "LISTENING" not in txt.upper(), txt)
    page.evaluate("() => { const m = window.__hud.mic; m.feedMs(800, 0.06); m.feedMs(2200, 0.0005); return true; }")
    check("W-6: the utterance's transcript lands in the chat selected when it lands (pane 1)",
          bool(until(lambda: "what is the weather" in _box(page, 1).input_value(), timeout=5))
          and "what is the weather" not in _box(page, 2).input_value())
    _box(page, 1).fill("")
    page.evaluate("window.__hud.capture.closeFollowUp()")


def _rv_beside_refusal_checks(page, mock, check, until):
    """Open beside into a dirty File pane the layout hides: the refusal is
    said where the owner is, not in the hidden pane."""
    _fresh(page, mock, until, CHAT_FILE)
    _open(page, until, 1, "t1")
    page.locator(_pane(2, '[data-testid="file-calc.py"]')).click()
    until(lambda: page.locator(_pane(2, '[data-testid="file-path"]')).inner_text() == "calc.py", timeout=4)
    _edit(page, until, 2, "\n# hidden and dirty")
    save = page.locator(_pane(2, '[data-testid="file-save"]'))
    until(lambda: not save.is_disabled(), timeout=4)
    _focus(page, until, 1)
    _choose(page, until, "single")
    until(lambda: not _visible(page, _pane(2)), timeout=2)
    _expand(page, until, "p2", "thread-t3")
    page.locator('[data-testid="thread-t3"]').click(modifiers=["Alt"])
    shown = page.locator('[data-testid="pane-1-refused"]')
    check("review: Open beside into a hidden dirty File pane is refused where the owner can see it",
          bool(until(lambda: shown.count() == 1 and shown.is_visible(), timeout=2))
          and "pane 2" in shown.inner_text(), shown.inner_text() if shown.count() else "")
    check("review: and nothing moved", _attr(page, '[data-testid="workspace"]', "data-preset") == "single"
          and _chat(page, 2)["threadId"] is None)
    _choose(page, until, "cols2")
    save.click()
    until(lambda: save.is_disabled(), timeout=4)


# ---------------------------------------------------------------------------
# The owner's rule (decisions W-6): ambiguous input goes to the selected chat.

GRID_CHATS = json.dumps({"preset": "grid4", "focused": 1,
                         "panes": [{"view": "chat"}, {"view": "chat"}, {"view": "chat"}, {"view": "chat"}]})
THREE = json.dumps({"preset": "cols3", "focused": 1,
                    "panes": [{"view": "chat"}, {"view": "chat"}, {"view": "preview"}, {"view": "task"}]})


def _sel(page):
    return page.evaluate("window.__hud.state().selectedChat")


def _w6_grid_checks(page, mock, check, until):
    _fresh(page, mock, until, GRID_CHATS)
    _open(page, until, 3, "t3", "p2")
    _focus(page, until, 1)
    check("setup: four chat panes in a 2×2 grid, pane 1 selected",
          all(_attr(page, _pane(n), "data-view") == "chat" for n in (1, 2, 3, 4)) and _sel(page) == 1)
    page.locator(_pane(3, '[data-testid="log"]')).click()
    until(lambda: _sel(page) == 3, timeout=2)
    check("W-6: a click in pane 3's transcript selects pane 3",
          _sel(page) == 3 and _attr(page, _pane(3), "data-selected") == "true"
          and page.locator('[data-testid="pane-3-mic"]').count() == 1
          and "selected" in (_attr(page, _pane(3), "class") or ""))
    check("W-6: only the selected chat's status rides the orb; every other chat pane draws its own line",
          _status_lines(page) == [1, 1, 0, 1]
          and [page.locator(_pane(n, '[data-testid="dictation"]')).count() for n in (1, 2, 3, 4)] == [0, 0, 0, 0],
          str(_status_lines(page)))
    page.locator(_pane(4, '[data-testid="tab-preview"]')).click()
    until(lambda: _attr(page, _pane(4), "data-view") == "preview", timeout=2)
    page.locator(_pane(4, '[data-testid="tab-file"]')).click()
    until(lambda: _attr(page, _pane(4), "data-view") == "file", timeout=2)
    page.locator(_pane(4)).click(position={"x": 40, "y": 80})
    time.sleep(0.2)
    check("W-6: clicking Preview or File in pane 4 leaves pane 3 selected",
          _sel(page) == 3 and _attr(page, _pane(4), "data-focused") == "true")
    page.evaluate(UTTERANCE)
    check("W-6: REVIEW puts what was said in the selected chat's box (pane 3)",
          bool(until(lambda: "what is the weather" in _box(page, 3).input_value(), timeout=5))
          and all(_box(page, n).input_value() == "" for n in (1, 2)))
    _box(page, 3).fill("")
    _dictation(page, "auto")
    s3 = len(_sent(mock, "t3"))
    page.evaluate(UTTERANCE)
    sent = until(lambda: _sent(mock, "t3")[s3:] or None, timeout=5)
    check("W-6: AUTO sends it to the selected chat's thread (pane 3)",
          bool(sent) and sent[-1].get("spoken") is True, str(sent))
    _dictation(page, "review")
    mock.emit("turn_finished", {"stop": "end"}, thread_id="t3")
    until(lambda: _chat(page, 3)["busy"] is False, timeout=3)
    mock.emit("text_delta", {"text": "no thread of its own"})
    check("W-6: an event with no thread lands in the selected chat (pane 3)",
          bool(until(lambda: "no thread of its own" in _chat(page, 3)["draft"], timeout=3))
          and all("no thread" not in _chat(page, n)["draft"] for n in (1, 2)))
    mock.emit("text", {"text": "no thread of its own"})


def _w6_fallback_checks(page, mock, check, until):
    _fresh(page, mock, until, THREE)
    _focus(page, until, 2)
    _focus(page, until, 1)
    # Three columns are narrow: pane 3's views are a menu, so click its URL box.
    page.locator(_pane(3, '[data-testid="preview-url"]')).click()
    until(lambda: _attr(page, _pane(3), "data-focused") == "true", timeout=2)
    check("setup: pane 1 selected last, pane 2 before it; pane 3 (Preview) focused", _sel(page) == 1)
    page.set_viewport_size({"width": 1100, "height": 900})
    until(lambda: not _visible(page, _pane(1)), timeout=3)
    check("W-6: the selected pane dropped by a narrow window falls back to the one selected before it, still drawn",
          bool(until(lambda: _sel(page) == 2, timeout=2))
          and _attr(page, _pane(2), "data-selected") == "true", str(_sel(page)))
    page.set_viewport_size({"width": 1600, "height": 900})
    check("W-6: and the selection is pane 1 again once it is drawn again",
          bool(until(lambda: _sel(page) == 1 and _visible(page, _pane(1)), timeout=3)), str(_sel(page)))


def _w6_one_and_none_checks(page, mock, check, until):
    _fresh(page, mock, until, SINGLE)
    check("W-6: with one chat open, that chat is selected",
          _sel(page) == 1 and _attr(page, _pane(1), "data-selected") == "true")
    two = json.dumps({"preset": "cols2", "focused": 1,
                      "panes": [{"view": "preview"}, {"view": "chat"}, {"view": "file"}, {"view": "task"}]})
    _fresh(page, mock, until, two)
    check("W-6: the one chat pane on screen is selected though never clicked", _sel(page) == 2)

    _fresh(page, mock, until, SINGLE)
    _open(page, until, 1, "t1")
    page.locator(_pane(1, '[data-testid="tab-file"]')).click()
    until(lambda: _attr(page, _pane(1), "data-view") == "file", timeout=2)
    check("setup: no chat pane on screen", _sel(page) is None)
    stt, calls = len(mock.sent("POST", "/stt")), len(mock.calls)
    page.evaluate("document.activeElement && document.activeElement.blur()")
    page.keyboard.down("Space")
    time.sleep(0.15)
    page.keyboard.up("Space")
    err = page.evaluate("window.__hud.state().error") or ""
    check("W-6: with no chat on screen, push-to-talk is refused and says why",
          "No chat is open" in err and page.evaluate("window.__hud.capture.ptt") is None, err)
    page.evaluate(UTTERANCE)
    time.sleep(1.0)
    sends = [c for c in mock.calls[calls:] if c[0] == "POST" and (c[1].endswith("/send") or c[1] == "/threads")]
    check("W-6: and nothing is transcribed or sent", len(mock.sent("POST", "/stt")) == stt and not sends, str(sends))
    mock.emit("text_delta", {"text": "for nobody"})
    time.sleep(0.4)
    check("W-6: an event with no thread lands in no pane",
          all("for nobody" not in _chat(page, n)["draft"] for n in (1, 2, 3, 4)))
    page.evaluate("window.__hud.dispatch({type: 'patch', patch: {error: ''}})")
    page.evaluate("window.__hud.capture.closeFollowUp()")


def _w6_handback_held_checks(page, mock, check, until):
    w = mock.world
    _fresh(page, mock, until, TWO)
    _open(page, until, 1, "t1")
    _open(page, until, 2, "t2")
    mock.emit("turn_started", {}, thread_id="t2")
    until(lambda: _chat(page, 2)["busy"], timeout=3)
    w["send_status"] = "queued"
    _box(page, 2).fill("words for t2 only")
    _box(page, 2).press("Enter")
    until(lambda: any(m.get("message_id") for m in _chat(page, 2)["messages"]), timeout=3)
    qid = f"msg-{w['sends']}"
    w.pop("send_status", None)
    page.locator(_pane(2, '[data-testid="tab-file"]')).click()
    until(lambda: _attr(page, _pane(2), "data-view") == "file", timeout=2)
    _focus(page, until, 1)
    mock.emit("queue_cleared", {"reason": "stopped", "messages": [
        {"message_id": qid, "typed": "words for t2 only", "via": "hud"}]}, thread_id="t2")
    time.sleep(0.6)
    check("W-6: a hand-back for a thread off screen is held, never put in the selected chat",
          _sel(page) == 1 and _box(page, 1).input_value() == "", repr(_box(page, 1).input_value()))
    page.locator(_pane(2, '[data-testid="tab-chat"]')).click()
    check("W-6: and comes back in its own thread's box when that is on screen",
          bool(until(lambda: _box(page, 2).input_value() == "words for t2 only", timeout=3)),
          repr(_box(page, 2).input_value()))
    _box(page, 2).fill("")
    mock.emit("turn_finished", {"stop": "interrupted"}, thread_id="t2")


# ---------------------------------------------------------------------------
# Re-review of PR #27 (feb61ff). Each failed there and passes now; the
# docstring names the mutation each one kills.

def _drop_on(page, selector: str, name: str = "note.txt"):
    """Drop a file on `selector` the way a browser does: dragover, then drop."""
    page.evaluate("""([sel, name]) => {
      const dt = new DataTransfer();
      dt.items.add(new File(["hello"], name, {type: "text/plain"}));
      const el = document.querySelector(sel);
      el.dispatchEvent(new DragEvent("dragover", {dataTransfer: dt, bubbles: true, cancelable: true}));
      el.dispatchEvent(new DragEvent("drop", {dataTransfer: dt, bubbles: true, cancelable: true}));
      return true; }""", [selector, name])


def _unstage(page, n: int):
    """Take every staged file out of pane N's box (each chip's x)."""
    for _ in range(10):
        x = page.locator(_pane(n, '[data-testid="chips"] .chip b'))
        if not x.count():
            return
        x.first.click()


def _rr_compose_fail_moved_checks(page, mock, check, until):
    """A first send that fails after the owner opened another thread in its
    pane: its words never go into a box showing another thread (W-6), and the
    next new thread in that pane gets them back. Kills: a hand-back with no
    thread falling back to the selected chat (giveBackPane's old
    `prefer ?? target`)."""
    _fresh(page, mock, until, TWO)
    _open(page, until, 2, "t3", "p2")
    _focus(page, until, 1)
    held = []
    page.route(OPEN_URL, lambda r: held.append(r) if r.request.method == "POST" else r.continue_())
    try:
        _box(page, 1).fill("words for a brand new thread")
        _box(page, 1).press("Enter")
        until(lambda: _pumped(page) and len(held) > 0, timeout=3)
        check("setup: pane 1's first send is opening its thread", len(held) == 1)
        _expand(page, until, "p1", "thread-t2")
        page.locator('[data-testid="thread-t2"]').click()
        until(lambda: _chat(page, 1)["threadId"] == "t2", timeout=3)
        held[0].abort()
    finally:
        page.unroute(OPEN_URL)
    time.sleep(0.8)
    b1, b2 = _box(page, 1).input_value(), _box(page, 2).input_value()
    check("review 2: a failed new-thread send's words never land in a box showing another thread",
          "brand new" not in b1 and "brand new" not in b2, f"pane 1 (t2) {b1!r}, pane 2 (t3) {b2!r}")
    err = page.evaluate("window.__hud.state().error") or ""
    check("review 2: and the window says where they are kept", "kept for the next new thread" in err, err)
    _focus(page, until, 1)
    page.locator('[data-testid="new-thread"]').click()
    until(lambda: _chat(page, 1)["threadId"] is None and _chat(page, 1)["compose"] is not None, timeout=3)
    check("review 2: the next new thread in that pane gets them back",
          bool(until(lambda: "brand new" in _box(page, 1).input_value(), timeout=2)),
          repr(_box(page, 1).input_value()))
    _box(page, 1).fill("")


def _rr_two_cards_checks(page, mock, check, until):
    """Two queued cards: the second is a card of its own — live buttons and
    the focus — and after the last one focus is back in the box it was taken
    from. Kills: the card not keyed by its request (card 2 inherited card 1's
    busy state, so its buttons stayed disabled; main had this too); no
    re-focus when a new card comes to the top; the box `disabled` under a
    card (focus fell to the page before the card recorded it)."""
    _fresh(page, mock, until, TWO)
    _open(page, until, 1, "t1")
    _box(page, 1).fill("typing here")
    _box(page, 1).focus()
    _approval(mock, "twocard01")
    _approval(mock, "twocard02")
    until(lambda: page.evaluate("window.__hud.state().approvals.length") == 2, timeout=4)
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 1, timeout=4)
    time.sleep(0.3)
    first = page.locator('[data-testid="approval-card"]').get_attribute("data-req")
    page.locator('[data-testid="approval-card"] button.deny').click()
    second = until(lambda: (lambda r: r if r and r != first else None)(
        page.locator('[data-testid="approval-card"]').get_attribute("data-req")), timeout=4)
    check("setup: the second card comes up when the first is denied", bool(second), str(second))
    time.sleep(0.3)
    dis = page.evaluate("""() => Array.from(document.querySelectorAll('[data-testid="approval-card"] button'))
                             .map(b => b.disabled)""")
    check("review 2: after the first card is denied by a click, the second card's buttons are live",
          bool(dis) and not any(dis), str(dis))
    focused = page.evaluate("""() => document.activeElement?.closest('[data-testid="approval-card"]')
                                 ?.getAttribute('data-req') || null""")
    check("review 2: and the second card has the focus", focused == second, str(focused))
    page.locator('[data-testid="approval-allow"]').click()
    body = until(lambda: mock.sent("POST", f"/approvals/{second}") or None, timeout=3)
    check("review 2: and its AUTHORIZE is clickable: it authorizes that request",
          bool(body) and body[-1].get("decision") == "allow", str(body[-1] if body else None))
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
    act = until(lambda: page.evaluate("""() => { const a = document.activeElement;
        return a && a.getAttribute('data-testid') === 'input' && a.closest('[data-testid="pane-1"]') ? 'pane-1 input'
             : (a && (a.getAttribute('data-testid') || a.tagName)); }""") == "pane-1 input", timeout=2)
    check("review 2: with the last card answered, focus is back in the box it was taken from", bool(act),
          str(page.evaluate("document.activeElement && (document.activeElement.getAttribute('data-testid') || document.activeElement.tagName)")))
    page.keyboard.type(" more")
    check("review 2: and typing goes on where it left off", _box(page, 1).input_value() == "typing here more",
          repr(_box(page, 1).input_value()))
    _box(page, 1).fill("")


def _rr_tab_never_authorizes_checks(page, mock, check, until):
    """No Tab or Shift+Tab sequence from the card's first focus puts AUTHORIZE
    or ALWAYS under an Enter: authorizing takes a click (CLAUDE.md, the
    approval card). Kills: AUTHORIZE or ALWAYS back in the card's Tab round
    (no tabIndex -1, or the round not skipping it)."""
    import itertools
    _fresh(page, mock, until, SINGLE)
    _approval(mock, "tabauth01")
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 1, timeout=4)
    time.sleep(0.3)
    first_focus = page.evaluate("document.activeElement?.getAttribute('data-testid')")
    check("setup: the card has the focus when it comes up", first_focus == "approval-card", str(first_focus))
    bad = []
    for seq in itertools.product(["Tab", "Shift+Tab"], repeat=4):
        page.evaluate("document.querySelector('[data-testid=\"approval-card\"]').focus()")
        for i, key in enumerate(seq):
            page.keyboard.press(key)
            t = page.evaluate("document.activeElement?.getAttribute('data-testid')")
            if t in ("approval-allow", "approval-always"):
                bad.append((seq[: i + 1], t))
    check("review 2: no Tab or Shift+Tab sequence from the card's first focus reaches AUTHORIZE or ALWAYS",
          not bad, str(bad[:3]))
    for key in ("Shift+Tab", "Tab"):
        page.evaluate("document.querySelector('[data-testid=\"approval-card\"]')?.focus()")
        page.keyboard.press(key)
        page.keyboard.press("Enter")
        time.sleep(0.3)
    decisions = [b.get("decision") for b in mock.sent("POST", "/approvals/tabauth01") if b]
    check("review 2: and Enter after either key never authorizes", "allow" not in decisions, str(decisions))
    if page.locator('[data-testid="approval-card"]').count():
        page.keyboard.press("Escape")
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)


def _rr_placeholder_stop_checks(page, mock, check, until):
    """The orb or Stop pressed while a first send opens its thread: no
    interrupt names the send's placeholder id (`local-N`, no thread the
    daemon knows), and the stop reaches the thread once it exists. Kills: the
    orb and Stop interrupting whatever id the pane tracks."""
    _fresh(page, mock, until, SINGLE)
    held = []
    page.route(OPEN_URL, lambda r: held.append(r) if r.request.method == "POST" else r.continue_())
    try:
        _box(page, 1).fill("stop me before I start")
        _box(page, 1).press("Enter")
        until(lambda: _pumped(page) and len(held) > 0, timeout=3)
        tracked = _chat(page, 1)["turnThreadId"]
        n0 = len(mock.calls)
        page.evaluate("document.activeElement && document.activeElement.blur()")
        page.keyboard.down("Space")
        time.sleep(0.1)
        page.keyboard.up("Space")
        stop = page.locator(_pane(1, '[data-testid="stop"]'))
        if stop.count():
            stop.click()
        time.sleep(0.3)
        odd = [c[1] for c in mock.calls[n0:] if "interrupt" in c[1]]
        check("review 2: the orb and Stop send no interrupt for a first send's placeholder id",
              str(tracked).startswith("local-") and not odd, f"tracked {tracked}, sent {odd}")
        held[0].continue_()
    finally:
        page.unroute(OPEN_URL)
    new = until(lambda: _chat(page, 1)["threadId"], timeout=3)
    check("review 2: and the stop asked for meanwhile reaches the thread once it exists",
          bool(new) and bool(until(lambda: mock.saw("POST", f"/threads/{new}/interrupt"), timeout=3)), str(new))
    if new:
        mock.emit("turn_finished", {"stop": "interrupted"}, thread_id=new)


def _rr_chip_writeback_checks(page, mock, check, until):
    """A model change on an opened-but-unsent thread whose answer lands after
    New thread: the fresh compose row stays fresh, and the next message opens
    a new thread. Kills: the answer written back into the pane captured
    before the request (ThreadModelControls' `change`)."""
    _fresh(page, mock, until, SINGLE)
    mock.world["fail_send"] = 1
    _box(page, 1).fill("first try")
    _box(page, 1).press("Enter")
    opened = until(lambda: (_chat(page, 1)["compose"] or {}).get("openedId")
                   if not _chat(page, 1)["busy"] else None, timeout=4)
    check("setup: a failed first send left its thread opened", bool(opened), str(opened))
    url = re.compile(rf".*/threads/{re.escape(str(opened))}$")
    held = []
    page.route(url, lambda r: held.append(r) if r.request.method == "PATCH" else r.continue_())
    try:
        btn = page.locator(_pane(1, '[data-testid="model-chip-btn"]'))
        until(lambda: btn.count() > 0, timeout=3)
        cur = btn.get_attribute("data-effort") or ""
        btn.click()
        pop = '[data-testid="model-pop"][data-pane="1"]'
        page.wait_for_selector(pop)
        opts = page.locator(pop + ' [data-testid="effort-opt"]').evaluate_all(
            "els => els.map(e => e.getAttribute('data-value'))")
        page.locator(pop + f' [data-testid="effort-opt"][data-value="{next(o for o in opts if o != cur)}"]').click()
        until(lambda: _pumped(page) and len(held) > 0, timeout=3)
        check("setup: the model change is on its way", len(held) == 1)
        _box(page, 1).fill("")
        page.locator('[data-testid="new-thread"]').click()
        until(lambda: (_chat(page, 1)["compose"] or {}).get("openedId") is None, timeout=3)
        held[0].continue_()
    finally:
        page.unroute(url)
    time.sleep(0.6)
    c = _chat(page, 1)["compose"] or {}
    check("review 2: a late model answer does not put the old thread back on a fresh compose row",
          c.get("openedId") is None, str(c))
    n_open = len(mock.posted("/threads"))
    _box(page, 1).fill("meant for a new thread")
    _box(page, 1).press("Enter")
    until(lambda: len(mock.posted("/threads")) > n_open, timeout=3)
    went_old = any((b or {}).get("text") == "meant for a new thread" for b in _sent(mock, str(opened)))
    check("review 2: and the next message opens a new thread rather than going to the old one",
          not went_old and len(mock.posted("/threads")) > n_open, f"to the old thread: {went_old}")
    tid = until(lambda: _chat(page, 1)["threadId"], timeout=3)
    if tid:
        mock.emit("turn_finished", {"stop": "end"}, thread_id=tid)


def _rr_drop_checks(page, mock, check, until):
    """Files dropped outside a box go to the selected chat (W-6) — a sidebar
    project row included — and stay with the conversation they were dropped
    for when its pane moves on while they are read in. Kills: the project
    row's drop handler swallowing a file drop (preventDefault); the window's
    drop handler writing the files it read before the await into whatever the
    pane shows after it."""
    _fresh(page, mock, until, CHAT_FILE)
    _open(page, until, 1, "t1")
    _drop_on(page, '[data-testid="project-p1"]')
    check("review 2: a file dropped on a sidebar project row is staged in the selected chat",
          bool(until(lambda: len(_chat(page, 1)["files"]) == 1, timeout=3)),
          str([f["name"] for f in _chat(page, 1)["files"]]))
    _unstage(page, 1)
    until(lambda: not _chat(page, 1)["files"], timeout=2)
    # Hold the read, move the pane on, then let the read finish.
    page.evaluate("""() => { window.__origRead = File.prototype.arrayBuffer; window.__reads = [];
      File.prototype.arrayBuffer = function () {
        const f = this; return new Promise((res) => window.__reads.push(() => res(window.__origRead.call(f))));
      }; return true; }""")
    try:
        _drop_on(page, _pane(2), "for-t1.txt")
        until(lambda: page.evaluate("window.__reads.length") > 0, timeout=2)
        _expand(page, until, "p1", "thread-t2")
        page.locator('[data-testid="thread-t2"]').click()
        until(lambda: _chat(page, 1)["threadId"] == "t2", timeout=3)
        page.evaluate("() => { window.__reads.forEach((go) => go()); window.__reads = []; return true; }")
    finally:
        page.evaluate("() => { if (window.__origRead) File.prototype.arrayBuffer = window.__origRead; return true; }")
    time.sleep(0.5)
    check("review 2: a file read in while its pane moved on is not staged in the thread it moved to",
          not _chat(page, 1)["files"], str([f["name"] for f in _chat(page, 1)["files"]]))
    page.locator('[data-testid="thread-t1"]').click()
    until(lambda: _chat(page, 1)["threadId"] == "t1", timeout=3)
    check("review 2: it is with the conversation it was dropped for",
          bool(until(lambda: [f["name"] for f in _chat(page, 1)["files"]] == ["for-t1.txt"], timeout=2)),
          str([f["name"] for f in _chat(page, 1)["files"]]))
    _unstage(page, 1)

# ---------------------------------------------------------------------------

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
            live = re.compile(r"^https?://(127\.0\.0\.1|localhost|\[::1\]):(8402|8403|8405)/")

            def refuse_live(route):
                H.FAILURES.append(f"the HUD under test reached a live daemon port: {route.request.url}")
                route.abort()

            multichat_checks(browser, mock, f"http://127.0.0.1:{mock.port}", H.check, H.until,
                             (live, refuse_live), H.FAKE_RECOGNIZER)
            browser.close()
    finally:
        mock.stop()
    print()
    if H.FAILURES:
        print(f"{len(H.FAILURES)} of {H.CHECKS} checks FAILED:")
        for f in H.FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {H.CHECKS} multichat checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
