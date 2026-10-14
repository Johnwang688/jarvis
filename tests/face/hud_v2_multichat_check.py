"""Headless checks for several chat panes at once (WP-B of
docs/plans/2026-10-09-hud-workspace-plan.md, §2.2 "Several chats at once").

Called from `hud_v2_check.main()` right after the layout section, in a browser
context of its own and against **a `MockDaemon` of its own** on an ephemeral
port, as the layout section is (the mock hands SSE frames only to the newest
`/events` connection, and this section's sends, interrupts, `/seen` posts and
approval decisions must stay out of the world the main suite asserts on). It
also runs on its own: `python tests/face/hud_v2_multichat_check.py [section,…]`.

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

Eight panes (WP-G1, docs/plans/2026-10-10-hud-split-panes-plan.md §3.8):
eight chat panes on eight threads in a 4×2 grid, each drawing only its own
transcript; a turn in pane 7 draws only there; Stop in pane 8 interrupts pane
8's thread and leaves pane 7's running; a click selects pane 6 and voice lands
in pane 6's box. (With `PANE_NOS` left at four, panes 5–8 never mount or
route, and every one of these fails.)

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

The thinking line (WP-H, decisions S-4), the `_thinking_*` sections: a mini
orb, a word and the elapsed time at the foot of whichever chat pane shows a
running turn — and nothing in the others; each row of the precedence table as
its event arrives (a tool in amber, an approval for that thread with the card
still topmost, a question, an error in red and the static ended line the next
send clears); a turn started from Discord before its thread was opened
(kills a line driven only by the pane's busy flag); adoption, and a reload
timed from `running_since`; reduced motion (kills reduced motion ignored); no
canvas and no animation-frame loop of their own in a grid of lines, one clock
for them all; the log's text unchanged with the line up (kills the line inside
the log); the avatar's accent on "Thinking…" only; and the input bar's cap
measured again when the line comes.
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
    _active_testid, _approval, _attr, _choose, _edit, _hold_keys, _pane, _visible,
)

# Both chat panes; files and the task behind them.
TWO = json.dumps({"preset": "cols2", "focused": 1,
                  "panes": [{"view": "chat"}, {"view": "chat"}, {"view": "file"}, {"view": "task"}]})
# A chat, and a File pane beside it.
CHAT_FILE = json.dumps({"preset": "cols2", "focused": 1,
                        "panes": [{"view": "chat"}, {"view": "file"}, {"view": "browser"}, {"view": "task"}]})


def multichat_checks(browser, mock, base, check, until, guard, init_script):
    print("\nseveral chat panes (WP-B)")
    _seed(mock)
    ctx = browser.new_context(viewport={"width": 1600, "height": 900}, permissions=["microphone"])
    guard(ctx)                      # live ports refused, HTTP and WebSocket alike (hud_v2_check.guard_live)
    ctx.add_init_script(init_script)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_default_timeout(5000)
    # Run alone, a comma-separated list of section names runs only those.
    only = set(filter(None, (sys.argv[1] if len(sys.argv) > 1 and __name__ == "__main__" else "").split(",")))
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
                        _rr_placeholder_stop_checks, _checkpoint_send_stop_checks, _rr_chip_writeback_checks, _rr_drop_checks,
                        # the thinking line (WP-H)
                        _thinking_pane_checks, _thinking_discord_checks, _thinking_adopt_checks,
                        _thinking_motion_checks, _thinking_many_checks, _thinking_log_checks,
                        _thinking_accent_checks, _thinking_box_cap_checks,
                        # eight panes (WP-G1)
                        _eight_chats_checks,
                        # split and close (WP-G2)
                        _split_close_chat_checks, _split_close_selected_checks,
                        # review of PR #47
                        _rv47_kept_compose_checks, _rv47_close_keeps_mic_checks, _rv47_first_send_closed_checks,
                        _rv47_loose_unlisted_checks, _rv47_ask_strip_checks,
                        # review 2 of PR #47 (c3c928f)
                        _rv47b_close_other_view_checks, _rv47b_park_said_checks, _rv47b_thread_park_said_checks,
                        _rv47b_send_held_named_checks,
                        # review 3 of PR #47 (9a093f9): words parked for the next new thread
                        _rv47c_close_orphan_checks, _rv47c_archive_orphan_checks, _checkpoint_compose_checks):
            if only and section.__name__.strip("_") not in only:
                continue
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

    # Open beside, from one pane: two columns, the thread in slot 2. Since
    # WP-G2 that is a split into a free id (the stored panes 2–4 are kept):
    # pane 2, hidden by ⊞, keeps the conversation it held.
    _expand(page, until, "p2", "thread-t3")
    page.locator('[data-testid="thread-t3"]').click(modifiers=["Alt"])
    until(lambda: _attr(page, '[data-testid="workspace"]', "data-preset") == "cols2", timeout=3)
    until(lambda: len(_order(page)) == 2 and _chat(page, _order(page)[1])["threadId"] == "t3", timeout=3)
    bes = _order(page)[1]
    check("from one pane, Alt+click opens the thread beside it: two columns, a new pane in slot 2",
          _attr(page, '[data-testid="workspace"]', "data-preset") == "cols2" and bes not in (1, 2, 3, 4)
          and _attr(page, _pane(bes), "data-view") == "chat" and _chat(page, bes)["threadId"] == "t3"
          and _attr(page, _pane(bes), "data-slot") == "2"
          and _attr(page, _pane(bes), "data-focused") == "true" and _chat(page, 1)["threadId"] == "t2", str(bes))
    check("and the pane ⊞ hid keeps its conversation, still hidden",
          _chat(page, 2)["threadId"] == "t1" and not _visible(page, _pane(2)))
    until(lambda: "homework question" in _log(page, bes), timeout=3)
    check("and its transcript is drawn there", "homework question" in _log(page, bes))
    chips = [page.locator(_pane(n, '[data-testid="project-chip"]')).inner_text() for n in (1, bes)]
    check("each pane's project chip names its own conversation's project",
          "jarvis" in chips[0] and "schoolwork" in chips[1], str(chips))

    # The ⋯ menu's Open beside: from pane 1, the next pane to the right.
    _focus(page, until, 1)
    _expand(page, until, "p1", "thread-menu-t1")
    page.locator('[data-testid="thread-menu-t1"]').click()
    until(lambda: page.locator('[data-testid="tmenu-beside"]').count() > 0, timeout=2)
    check("a thread's ⋯ menu offers Open beside", page.locator('[data-testid="tmenu-beside"]').count() == 1)
    page.locator('[data-testid="tmenu-beside"]').click()
    until(lambda: _chat(page, bes)["threadId"] == "t1", timeout=3)
    check("which opens it in the next pane to the right",
          _chat(page, bes)["threadId"] == "t1" and _chat(page, 1)["threadId"] == "t2"
          and _attr(page, _pane(bes), "data-focused") == "true")


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
    until(lambda: page.locator(_pane(2, '[data-testid="tool-group"]')).count() > 0, timeout=3)
    page.locator(_pane(2, '[data-testid="tool-group"]')).last.click()
    check("tool_started: pane 2's transcript, not pane 1's",
          "grep_files" in page.locator(_pane(2, '[data-testid="tool-call"]')).last.inner_text()
          and page.locator(_pane(1, '[data-testid="tool-group"]')).count() == 0)
    check("and pane 2's own status says what it runs — on the orb, pane 2 being the selected chat",
          "RUNNING · grep_files" in _chat(page, 2)["status"]
          and "grep_files" in page.locator('[data-testid="orb-status"]').inner_text().lower()
          and page.locator(_pane(2, '[data-testid="pane-status"]')).count() == 0,
          _chat(page, 2)["status"])
    check("while pane 1's input bar still says what its own turn is doing",
          "responding" in page.locator(_pane(1, '[data-testid="pane-status"]')).inner_text().lower(),
          page.locator(_pane(1, '[data-testid="pane-status"]')).inner_text())
    mock.emit("tool_finished", {"call_id": "c2", "name": "grep_files", "ok": True}, thread_id="t2")
    until(lambda: page.locator(_pane(2, '[data-testid="tool-call"]')).last.get_attribute("data-status") == "ok", timeout=3)
    check("tool_finished: marked ok in pane 2", page.locator(_pane(2, '[data-testid="tool-call"]')).last.get_attribute("data-status") == "ok")
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

    # The orb's press interrupts the selected chat's turn, never another pane's
    # (push-to-talk, Ctrl+D since 2026-10-10, is the orb's press).
    i1, i2 = len(mock.sent("POST", "/threads/t1/interrupt")), len(mock.sent("POST", "/threads/t2/interrupt"))
    page.evaluate("document.activeElement && document.activeElement.blur()")
    _hold_keys(page, ["Control", "d"], hold=0.1)
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
    held, _ = _hold_keys(page, ["Control", "d"], hold=0.1)
    check("and push-to-talk's Ctrl+D behind the card starts nothing",
          held is None and page.locator('[data-testid="approval-card"]').count() == 1, str(held))
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
    page.locator(_pane(2, '[data-testid="tab-browser"]')).click()
    time.sleep(0.2)
    check("so is any other view", kept("browser"))

    # Open beside from pane 1 would land in pane 2.
    _focus(page, until, 1)
    _expand(page, until, "p2", "thread-t3")
    page.locator('[data-testid="thread-t3"]').click(modifiers=["Alt"])
    time.sleep(0.3)
    check("Open beside into it is refused", kept("beside") and _chat(page, 2)["threadId"] is None
          and _chat(page, 1)["threadId"] == "t1")

    # With no chat drawn, a sidebar thread, a task and New thread would all land in the focused pane.
    page.locator(_pane(1, '[data-testid="tab-browser"]')).click()
    until(lambda: _attr(page, _pane(1), "data-view") == "browser", timeout=2)
    page.locator(_pane(2, '[data-testid="file-path"]')).click()
    until(lambda: _attr(page, _pane(2), "data-focused") == "true", timeout=2)
    page.locator('[data-testid="thread-t1"]').click()
    time.sleep(0.3)
    check("a sidebar thread landing in it is refused", kept("thread")
          and _attr(page, _pane(1), "data-view") == "browser")
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
        page.set_viewport_size({"width": 760, "height": 900})
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
    page.route("**/threads/t1/transcript*", lambda r: held.append(r))
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
        page.unroute("**/threads/t1/transcript*")
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
    page.route("**/threads/t1/transcript*", lambda r: held.append(r))
    try:
        _expand(page, until, "p1", "thread-t1")
        page.locator('[data-testid="thread-t1"]').click()
        until(lambda: _pumped(page) and len(held) > 0, timeout=3)
        page.locator('[data-testid="thread-t2"]').click()
        until(lambda: "second thread question" in _log(page, 1), timeout=3)
        held[0].continue_()
    finally:
        page.unroute("**/threads/t1/transcript*")
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
    page.route("**/stt*", lambda r: held.append(r))
    try:
        page.evaluate(UTTERANCE)
        until(lambda: _pumped(page) and len(held) > 0, timeout=6)
        check("setup: an utterance spoken to pane 1 is being transcribed", len(held) > 0)
        page.locator(_pane(2, '[data-testid="log"]')).click()
        until(lambda: _attr(page, _pane(2), "data-focused") == "true", timeout=2)
        s1, s2 = len(_sent(mock, "t1")), len(_sent(mock, "t2"))
        held[0].continue_()
    finally:
        page.unroute("**/stt*")
    # V2 changed this (a different chat than at capture is never sent to
    # unseen): the words land in the chat now selected, in its box, unsent.
    until(lambda: "what is the weather" in _box(page, 2).input_value(), timeout=5)
    check("W-6 (V2): clicking pane 2 during STT puts the words in pane 2's box, unsent",
          "what is the weather" in _box(page, 2).input_value(), repr(_box(page, 2).input_value()))
    time.sleep(0.3)
    check("W-6 (V2): and nothing is sent to either chat",
          len(_sent(mock, "t1")) == s1 and len(_sent(mock, "t2")) == s2)
    _box(page, 2).fill("")
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
    """Open beside next to a dirty File pane the layout hides. Since WP-G2,
    from one pane that is a split into a free id: the hidden edit is never
    touched. Into a pane the window dropped for room (two columns drawn as
    one), the refusal is said where the owner is, not in the hidden pane."""
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
    until(lambda: len(_order(page)) == 2 and _chat(page, _order(page)[1])["threadId"] == "t3", timeout=3)
    bes = _order(page)[1]
    check("G2: Open beside from one pane splits into a free pane, leaving the hidden dirty File pane alone",
          _attr(page, '[data-testid="workspace"]', "data-preset") == "cols2" and _attr(page, _pane(bes), "data-slot") == "2"
          and not _visible(page, _pane(2)) and _attr(page, _pane(2), "data-view") == "file"
          and not save.is_disabled() and page.locator('[data-testid="pane-1-refused"]').count() == 0)
    _choose(page, until, "grid4")
    until(lambda: _visible(page, _pane(2)), timeout=2)
    save.click()
    until(lambda: save.is_disabled(), timeout=4)

    def narrow_refusal(file_pane: int, order: list):
        """Two columns drawn as one (760px: one pane fits beside the sidebar, two do not even on both
        rails): Open beside aims at the dropped File pane."""
        panes = [{"view": "chat"}, {"view": "browser"}, {"view": "file"}, {"view": "task"}, {"view": "file"}]
        if file_pane == 2:
            panes[1] = {"view": "file"}
        _fresh(page, mock, until, json.dumps({"preset": "cols2", "order": order, "focused": 1, "panes": panes}))
        _open(page, until, 1, "t1")
        page.locator(_pane(file_pane, '[data-testid="file-calc.py"]')).click()
        until(lambda: page.locator(_pane(file_pane, '[data-testid="file-path"]')).inner_text() == "calc.py", timeout=4)
        _edit(page, until, file_pane, "\n# hidden and dirty")
        dirty = page.locator(_pane(file_pane, '[data-testid="file-save"]'))
        until(lambda: not dirty.is_disabled(), timeout=4)
        _focus(page, until, 1)
        page.set_viewport_size({"width": 760, "height": 900})
        until(lambda: not _visible(page, _pane(file_pane)), timeout=3)
        _expand(page, until, "p2", "thread-t3")
        page.locator('[data-testid="thread-t3"]').click(modifiers=["Alt"])
        return dirty

    save = narrow_refusal(2, [1, 2])
    shown = page.locator('[data-testid="pane-1-refused"]')
    check("review: Open beside into a hidden dirty File pane is refused where the owner can see it",
          bool(until(lambda: shown.count() == 1 and shown.is_visible(), timeout=2))
          and "pane 2" in shown.inner_text(), shown.inner_text() if shown.count() else "")
    check("review: and nothing moved", _attr(page, '[data-testid="workspace"]', "data-preset") == "cols2"
          and _attr(page, _pane(2), "data-view") == "file" and _chat(page, 2)["threadId"] is None)
    page.set_viewport_size({"width": 1600, "height": 900})
    until(lambda: _visible(page, _pane(2)), timeout=3)
    save.click()
    until(lambda: save.is_disabled(), timeout=4)

    # The same with an id that is not its slot (review of PR #37): the File
    # pane is id 5, drawn in slot 2. The refusal names the slot.
    save = narrow_refusal(5, [1, 5])
    shown = page.locator('[data-testid="pane-1-refused"]')
    check("review of #37: the refusal names the hidden File pane by its slot (2), never its id (5)",
          bool(until(lambda: shown.count() == 1 and shown.is_visible(), timeout=2))
          and "pane 2 has an unsaved edit" in shown.inner_text() and "pane 5" not in shown.inner_text(),
          shown.inner_text() if shown.count() else "")
    page.set_viewport_size({"width": 1600, "height": 900})
    until(lambda: _visible(page, _pane(5)), timeout=3)
    save.click()
    until(lambda: save.is_disabled(), timeout=4)


# ---------------------------------------------------------------------------
# The owner's rule (decisions W-6): ambiguous input goes to the selected chat.

GRID_CHATS = json.dumps({"preset": "grid4", "focused": 1,
                         "panes": [{"view": "chat"}, {"view": "chat"}, {"view": "chat"}, {"view": "chat"}]})
THREE = json.dumps({"preset": "cols3", "focused": 1,
                    "panes": [{"view": "chat"}, {"view": "chat"}, {"view": "browser"}, {"view": "task"}]})


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
    page.locator(_pane(4, '[data-testid="tab-browser"]')).click()
    until(lambda: _attr(page, _pane(4), "data-view") == "browser", timeout=2)
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
    page.locator(_pane(3, '[data-testid="browser-url"]')).click()
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
                      "panes": [{"view": "browser"}, {"view": "chat"}, {"view": "file"}, {"view": "task"}]})
    _fresh(page, mock, until, two)
    check("W-6: the one chat pane on screen is selected though never clicked", _sel(page) == 2)

    _fresh(page, mock, until, SINGLE)
    _open(page, until, 1, "t1")
    page.locator(_pane(1, '[data-testid="tab-file"]')).click()
    until(lambda: _attr(page, _pane(1), "data-view") == "file", timeout=2)
    check("setup: no chat pane on screen", _sel(page) is None)
    stt, calls = len(mock.sent("POST", "/stt")), len(mock.calls)
    page.evaluate("document.activeElement && document.activeElement.blur()")
    _hold_keys(page, ["Control", "d"])
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
    check("review 2: and the window says where they are kept", "kept until this pane shows a new thread" in err, err)
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
        _hold_keys(page, ["Control", "d"], hold=0.1)
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



def _checkpoint_send_stop_checks(page, mock, check, until):
    """A Stop during /send waits for its exact identity, even without SSE.
    Covers the thread-created/ACK gap, known threads, stale and queued ACKs,
    and request failure. The backend separately proves a stale ID is inert.
    """
    interrupt_url = re.compile(r".*/threads/t\d+/interrupt$")
    for case in ("new", "known", "stale", "queued", "failed", "no-id", "dropped"):
        _fresh(page, mock, until, SINGLE)
        if case != "new":
            _open(page, until, 1, "t1")
        held, stops = [], []
        def stopped(route):
            stops.append(route.request.post_data_json)
            stale = case == "stale" or (case == "queued" and len(stops) == 1)
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": not stale, "stale": stale}))
        page.route(SEND_URL, lambda route: held.append(route))
        page.route(interrupt_url, stopped)
        try:
            _box(page, 1).fill("words awaiting their send reply")
            _box(page, 1).press("Enter")
            check(f"send/stop {case}: send is held after the thread exists",
                  bool(until(lambda: _pumped(page) and held, timeout=3))
                  and not str(_chat(page, 1)["turnThreadId"]).startswith("local-"))
            if case == "new":
                # A compose row offers the orb's interrupt until ACK makes it
                # an open conversation; its Send control is not Steer yet.
                page.evaluate("document.activeElement && document.activeElement.blur()")
                _hold_keys(page, ["Control", "d"], hold=0.1)
            else:
                stop = page.locator(_pane(1, '[data-testid="stop"]'))
                stop.click()
                stop.click()
            page.wait_for_timeout(100)
            check(f"send/stop {case}: Stop waits for send acknowledgement", not stops, str(stops))
            outcome = {"status": "started", "turn_id": "a" * 32}
            if case == "queued":
                outcome.update(status="queued", queued_turn_id="b" * 32, message_id="held-message")
            elif case == "no-id":
                outcome.pop("turn_id")
            elif case == "dropped":
                outcome["status"] = "dropped"
            elif case == "failed":
                outcome = {"error": "synthetic send failure"}
            held[0].fulfill(status=500 if case == "failed" else 202,
                            content_type="application/json", body=json.dumps(outcome))
            if case in ("new", "known", "stale", "queued"):
                wanted = [{"expected_turn_id": "a" * 32}]
                if case == "queued":
                    wanted.append({"expected_turn_id": "b" * 32})
                until(lambda: _pumped(page) and len(stops) >= len(wanted), timeout=3)
                page.wait_for_timeout(100)
                check(f"send/stop {case}: only the acknowledged exact turn identities are stopped",
                      stops == wanted, str(stops))
            else:
                page.wait_for_timeout(150)
                check(f"send/stop {case}: no unguarded or failed-send interrupt escapes", not stops, str(stops))
            if case == "no-id":
                err = page.locator('[data-testid="error"]')
                check("send/stop no-id: the safe refusal is visible",
                      err.count() and "no turn identity" in err.inner_text())
            if case == "failed":
                # The earlier failed request cannot consume this later Stop.
                until(lambda: not _chat(page, 1)["busy"], timeout=3)
                mock.emit("turn_started", {"turn_id": "c" * 32}, thread_id="t1")
                until(lambda: _chat(page, 1)["busy"], timeout=3)
                page.locator(_pane(1, '[data-testid="stop"]')).click()
                until(lambda: _pumped(page) and stops, timeout=3)
                check("send/stop failure: a later normal Stop is not swallowed", stops == [{}], str(stops))
        finally:
            page.unroute(SEND_URL)
            page.unroute(interrupt_url)


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
        btn.click()
        pop = '[data-testid="model-pop"][data-pane="1"]'
        page.wait_for_selector(pop)
        # The effort is a slider (PR #30): click a stop other than the one
        # the thumb is on, which commits at once.
        slider = page.locator(pop + ' [data-testid="effort-slider"]')
        at = int(slider.get_attribute("aria-valuenow") or 0)
        stops = page.locator(pop + ' [data-testid="effort-stop"]')
        box = stops.nth(0 if at != 0 else stops.count() - 1).bounding_box()
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
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
# The thinking line (WP-H, decisions S-4; plan §4): a mini orb, a word and the
# elapsed time at the foot of each chat while its turn runs. Each check that
# names a mutation was shown failing with it applied in a scratch copy.

AMBER_RGB = "rgb(251, 191, 36)"
RED_RGB = "rgb(248, 113, 113)"
HOOT_RGB = "rgb(168, 85, 247)"

LINE_JS = """(n) => {
  const p = document.querySelector(`[data-testid="pane-${n}"]`);
  const el = p && p.querySelector('[data-testid="thinking"]');
  if (!el) return null;
  const w = el.querySelector('[data-testid="thinking-word"]');
  const t = el.querySelector('[data-testid="thinking-time"]');
  const orb = el.querySelector('[data-testid="miniorb"]');
  const part = (sel) => { const x = orb && orb.querySelector(sel); return x ? getComputedStyle(x) : null; };
  const outer = part('.mo-outer'), inner = part('.mo-inner'), core = part('.mo-core');
  const cs = getComputedStyle(el);
  return {
    word: w ? w.textContent : null, time: t ? t.textContent : null, kind: el.getAttribute('data-word'),
    orb: el.getAttribute('data-orb'), stroke: outer ? outer.stroke : null, coreFill: core ? core.fill : null,
    anims: [outer, inner, core].map((c) => c ? c.animationName : null),
    still: orb ? orb.classList.contains('still') : null, inLog: !!el.closest('[data-testid="log"]'),
    afterLog: !!(el.previousElementSibling && el.previousElementSibling.matches('[data-testid="log"]')),
    aboveInput: !!(el.parentElement.nextElementSibling && el.parentElement.nextElementSibling.querySelector('[data-testid="input"]')),
    canvas: el.querySelectorAll('canvas').length, svg: el.querySelectorAll('svg').length,
    role: w ? w.getAttribute('role') : null, timeRole: t ? t.getAttribute('role') : null,
    ariaHidden: orb ? orb.getAttribute('aria-hidden') : null,
    levers: el.querySelectorAll('button, a, input, select, textarea, [tabindex], [onclick]').length,
    position: cs.position, z: cs.zIndex, wordColor: w ? getComputedStyle(w).color : null,
    visible: el.getClientRects().length > 0,
  };
}"""


def _line(page, n: int):
    return page.evaluate(LINE_JS, n)


def _secs(text) -> int:
    """'12s', '3m 07s', '1h 02m' as seconds; -1 for no time."""
    if not text:
        return -1
    m = re.fullmatch(r"(?:(\d+)h )?(?:(\d+)m )?(\d+)s|(\d+)h (\d+)m", text.strip())
    if not m:
        return -1
    if m.group(4):
        return int(m.group(4)) * 3600 + int(m.group(5)) * 60
    return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3))


def _clock(page) -> dict:
    return page.evaluate("window.__hud.thinkingClock()")


THINKING_TIDS = ("t1", "t2", "t3", "t4", "t5", "t6", "t7", "t8")


def _seed_thinking(mock):
    """Chat threads t4–t8, for a grid of lines in up to eight panes."""
    w = mock.world
    base = next(t for t in w["threads"] if t["id"] == "t1")
    for n in range(4, 9):
        tid = f"t{n}"
        if not any(t["id"] == tid for t in w["threads"]):
            w["threads"].append({**base, "id": tid, "title": f"chat {n}",
                                 "updated": f"2026-09-12T00:00:0{n}+00:00", "model": None})
            w["transcripts"][tid] = [{"role": "user", "text": f"thread {n} question",
                                      "at": "2026-09-12T00:00:00+00:00"}]


def _idle_all(page, mock, until, tids=THINKING_TIDS):
    """End every turn the section left running, here and in the mock's world
    (a start first, so an ended error is cleared too)."""
    for tid in tids:
        mock.emit("turn_started", {}, thread_id=tid)
        mock.emit("turn_finished", {"stop": "end"}, thread_id=tid)
        if mock.world["activity"]["threads"].get(tid):
            mock.activity("thread", tid, "idle")
        mock.thread_running(tid, None)
    until(lambda: not page.evaluate("Object.keys(window.__hud.state().turns).length"), timeout=3)


def _thinking_pane_checks(page, mock, check, until):
    """A turn in pane 2: the line there, nothing in pane 1, and each row of the
    precedence table as the events arrive."""
    _seed_thinking(mock)
    _fresh(page, mock, until, TWO)
    _open(page, until, 1, "t1")
    _open(page, until, 2, "t2")
    check("thinking: no line in either pane while nothing runs", _line(page, 1) is None and _line(page, 2) is None)
    check("thinking: and the one clock does not run", _clock(page)["running"] is False, str(_clock(page)))

    mock.emit("turn_started", {}, thread_id="t2")
    until(lambda: _line(page, 2) is not None, timeout=3)
    first = _line(page, 2)
    check("thinking: a turn in pane 2 shows the mini orb and 'Thinking…' there",
          first["word"] == "Thinking…" and first["svg"] == 1 and first["kind"] == "thinking", str(first))
    check("thinking: and nothing in pane 1", _line(page, 1) is None, str(_line(page, 1)))
    check("thinking: under the transcript — after the log's scroller, above the input bar, outside the log",
          first["afterLog"] and first["aboveInput"] and not first["inLog"], str(first))
    t0 = _secs(first["time"])
    time.sleep(2.2)
    t1 = _secs(_line(page, 2)["time"])
    check("thinking: with a time that ticks", 0 <= t0 < t1, f"{first['time']!r} → {_line(page, 2)['time']!r}")
    check("thinking: one clock, ticking for the one drawn line",
          _clock(page) == {"subscribers": 1, "running": True}, str(_clock(page)))
    check("thinking: the orb is aria-hidden, the word is the status and the time is not",
          first["ariaHidden"] == "true" and first["role"] == "status" and first["timeRole"] is None, str(first))
    check("thinking: in the pane's flow, nothing to press — no position, no z-index, no button or link",
          first["levers"] == 0 and first["position"] == "static" and first["z"] == "auto", str(first))

    mock.emit("tool_started", {"call_id": "th1", "name": "ls"}, thread_id="t2")
    until(lambda: (_line(page, 2) or {}).get("kind") == "tool", timeout=3)
    tool = _line(page, 2)
    check("thinking: tool_started gives 'Running ls…'", tool["word"] == "Running ls…", str(tool))
    check("thinking: with a computed stroke of rgb(251,191,36)", tool["stroke"] == AMBER_RGB, str(tool["stroke"]))
    check("thinking: and the word in amber too", tool["wordColor"] == AMBER_RGB, str(tool["wordColor"]))
    check("thinking: and the time keeps counting from the turn's start, not the tool's",
          _secs(tool["time"]) >= t1, f"{tool['time']!r}")

    mock.emit("approval_requested", {
        "req_id": "think-ap1", "code": "TH01", "tool": "run_command", "args": {"command": "ls"}, "command": "ls",
        "reason": "asks", "layer": "human", "origin": "chat: second chat", "thread_id": "t2", "allowlistable": True,
        "timeout_s": 120,
    })
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 1, timeout=3)
    until(lambda: (_line(page, 2) or {}).get("kind") == "approval", timeout=3)
    ap = _line(page, 2)
    check("thinking: an approval for that thread gives 'Waiting for approval' (ahead of the tool)",
          ap["word"] == "Waiting for approval" and ap["stroke"] == AMBER_RGB, str(ap))
    check("thinking: and pane 1, another thread, is not waiting", _line(page, 1) is None)
    top = page.evaluate("""() => Array.from(document.querySelectorAll('[data-testid="approval-card"] button')).map((b) => {
        const r = b.getBoundingClientRect();
        const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        return !!hit && (hit === b || b.contains(hit));
      })""")
    check("thinking: and the card's buttons are topmost", bool(top) and all(top), str(top))
    check("thinking: the line never says Authorize, Approve or Deny",
          not re.search(r"authori[sz]e|approve|deny", ap["word"], re.I))
    page.locator('[data-testid="approval-deny"]').click()
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=3)
    until(lambda: (_line(page, 2) or {}).get("kind") == "tool", timeout=3)
    check("thinking: the card answered, the line goes back to the tool", _line(page, 2)["word"] == "Running ls…")
    mock.emit("tool_finished", {"call_id": "th1", "name": "ls", "ok": True}, thread_id="t2")
    until(lambda: (_line(page, 2) or {}).get("kind") == "thinking", timeout=3)

    mock.activity("thread", "t2", "needs_input")
    until(lambda: (_line(page, 2) or {}).get("kind") == "answer", timeout=3)
    q = _line(page, 2)
    check("thinking: needs_input with no approval gives 'Waiting for your answer'",
          q["word"] == "Waiting for your answer" and q["stroke"] == AMBER_RGB, str(q))
    mock.activity("thread", "t2", "working")
    until(lambda: (_line(page, 2) or {}).get("kind") == "thinking", timeout=3)

    mock.emit("error", {"message": "two is cooling", "fatal": False}, thread_id="t2")
    until(lambda: (_line(page, 2) or {}).get("kind") == "error", timeout=3)
    err = _line(page, 2)
    check("thinking: error turns it red", err["word"] == "Error" and err["stroke"] == RED_RGB
          and err["wordColor"] == RED_RGB and not err["still"], str(err))
    mock.emit("turn_finished", {"stop": "error"}, thread_id="t2")
    until(lambda: _chat(page, 2)["busy"] is False, timeout=3)
    mock.activity("thread", "t2", "failed")
    time.sleep(0.3)
    ended = _line(page, 2)
    check("thinking: turn_finished{stop: 'error'} leaves the static red line",
          bool(ended) and ended["word"] == "Ended on an error" and ended["stroke"] == RED_RGB and ended["still"]
          and ended["time"] is None and ended["anims"] == ["none", "none", "none"], str(ended))
    page.evaluate("window.__hud.dispatch({type: 'patch', patch: {error: ''}})")
    box2 = _box(page, 2)
    box2.click()
    box2.fill("try again")
    box2.press("Enter")
    until(lambda: (_line(page, 2) or {}).get("kind") == "thinking", timeout=3)
    again = _line(page, 2)
    check("thinking: the next send clears it", again["word"] == "Thinking…" and again["stroke"] != RED_RGB, str(again))
    mock.emit("turn_started", {}, thread_id="t2")
    mock.emit("turn_finished", {"stop": "end"}, thread_id="t2")
    mock.activity("thread", "t2", "idle")
    until(lambda: _line(page, 2) is None, timeout=3)
    check("thinking: and a turn that ends well leaves no line", _line(page, 2) is None)
    until(lambda: _clock(page)["running"] is False, timeout=3)
    check("thinking: with no line drawn the clock stops", _clock(page)["running"] is False, str(_clock(page)))


def _thinking_discord_checks(page, mock, check, until):
    """A turn started from Discord — before any pane showed the thread, and in
    a thread a pane shows. Kills: a line driven only by the pane's busy flag."""
    _fresh(page, mock, until, TWO)
    _open(page, until, 1, "t1")
    _open(page, until, 2, "t2")
    mock.emit("user_message", {"text": "from my phone", "typed": "from my phone", "via": "discord"},
              thread_id="t3", project_id="p2")
    mock.emit("turn_started", {}, thread_id="t3")
    until(lambda: "t3" in page.evaluate("window.__hud.state().turns"), timeout=3)
    check("thinking: a Discord turn in a thread no pane shows draws nothing yet",
          _line(page, 1) is None and _line(page, 2) is None)
    time.sleep(1.3)
    _open(page, until, 2, "t3", "p2")
    until(lambda: _line(page, 2) is not None, timeout=3)
    line = _line(page, 2)
    c2 = _chat(page, 2)
    check("thinking: a Discord-started turn (user_message via discord, then turn_started) shows when its thread "
          "is opened, timed from its start — with the pane not busy",
          bool(line) and line["word"] == "Thinking…" and _secs(line["time"]) >= 1 and c2["busy"] is False,
          f"{line} busy={c2['busy']}")
    mock.emit("user_message", {"text": "and another", "typed": "and another", "via": "discord"},
              thread_id="t1", project_id="p1")
    mock.emit("turn_started", {}, thread_id="t1")
    until(lambda: _line(page, 1) is not None, timeout=3)
    check("thinking: and one in a thread pane 1 shows draws there at once", _line(page, 1)["word"] == "Thinking…")
    mock.emit("turn_finished", {"stop": "end"}, thread_id="t1")
    until(lambda: _line(page, 1) is None, timeout=3)
    check("thinking: its finish takes it away", _line(page, 1) is None and _line(page, 2) is not None)


def _thinking_adopt_checks(page, mock, check, until):
    """A running turn no pane tracks is adopted (H-3): Stop comes back. And
    after a reload the line counts from the thread record's running_since (H-b)."""
    stop2 = page.locator(_pane(2, '[data-testid="stop"]'))
    check("setup: pane 2 shows t3's Discord turn without tracking it, so it has no Stop",
          _chat(page, 2)["threadId"] == "t3" and stop2.count() == 0 and _line(page, 2) is not None)
    mock.activity("thread", "t3", "working")
    until(lambda: _chat(page, 2)["busy"], timeout=3)
    c2 = _chat(page, 2)
    check("thinking: the daemon calling it working, the pane showing it adopts the turn: busy, tracking it, Stop",
          c2["busy"] and c2["turnThreadId"] == "t3" and bool(until(lambda: stop2.count() == 1, timeout=2)), str(c2))
    i3 = len(mock.sent("POST", "/threads/t3/interrupt"))
    stop2.click()
    until(lambda: len(mock.sent("POST", "/threads/t3/interrupt")) > i3, timeout=3)
    check("thinking: and that Stop interrupts it", len(mock.sent("POST", "/threads/t3/interrupt")) == i3 + 1)

    # A reload while it runs: the activity snapshot says working, the record when it began.
    mock.thread_running("t3", time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 95)))
    _fresh(page, mock, until, TWO)
    _open(page, until, 1, "t1")
    _open(page, until, 2, "t3", "p2")
    until(lambda: _line(page, 2) is not None, timeout=3)
    line = _line(page, 2)
    check("thinking: reopening a working thread after a reload shows the line, timed from running_since (H-b)",
          line["word"] == "Thinking…" and 94 <= _secs(line["time"]) <= 110, str(line))
    check("thinking: and Stop (adoption)", bool(until(lambda: stop2.count() == 1, timeout=3))
          and _chat(page, 2)["turnThreadId"] == "t3")
    check("thinking: a pane showing another thread adopts nothing", _chat(page, 1)["busy"] is False
          and _line(page, 1) is None)
    mock.emit("turn_finished", {"stop": "interrupted"}, thread_id="t3")
    until(lambda: _chat(page, 2)["busy"] is False, timeout=3)
    time.sleep(0.3)
    check("thinking: a finish heard before the idle record is never adopted back (the entry went with it)",
          _chat(page, 2)["busy"] is False and stop2.count() == 0 and _line(page, 2) is None,
          f"{_chat(page, 2)['busy']} {_line(page, 2)}")
    _idle_all(page, mock, until)


def _thinking_motion_checks(page, mock, check, until):
    """Reduced motion: nothing turns or flexes, the word and the time still
    say it. Kills: reduced motion ignored."""
    _fresh(page, mock, until, TWO)
    _open(page, until, 1, "t1")
    mock.emit("turn_started", {}, thread_id="t1")
    until(lambda: _line(page, 1) is not None, timeout=3)
    moving = _line(page, 1)
    check("setup: the orb's rings turn and its core flexes",
          moving["anims"] == ["mo-spin", "mo-spin", "mo-flex"], str(moving["anims"]))
    try:
        page.emulate_media(reduced_motion="reduce")
        time.sleep(0.2)
        still = _line(page, 1)
        check("thinking: under emulate_media(reduced_motion='reduce') the computed animation-name is none",
              still["anims"] == ["none", "none", "none"], str(still["anims"]))
        check("thinking: and the word still shows", still["word"] == "Thinking…" and still["visible"], str(still))
        a = _secs(still["time"])
        time.sleep(1.2)
        check("thinking: and the time still counts", _secs(_line(page, 1)["time"]) > a)
    finally:
        page.emulate_media(reduced_motion="no-preference")
    time.sleep(0.2)
    check("thinking: motion back when it is allowed", _line(page, 1)["anims"] == ["mo-spin", "mo-spin", "mo-flex"])
    _idle_all(page, mock, until)


def _thinking_many_checks(page, mock, check, until):
    """Every chat pane at once, a line in each: no canvas in any line, and no
    animation frame loop of its own (CSS on the compositor). Eight once the
    workspace has eight panes (WP-G1), each its own thread (review of the
    merge onto main: a cap at four tested four lines in an eight-pane grid)."""
    _seed_thinking(mock)
    count = len(page.evaluate("window.__hud.state().chats"))
    preset = {4: "grid4", 8: "grid8"}.get(count, "grid4")
    ws = json.dumps({"preset": preset, "focused": 1, "panes": [{"view": "chat"}] * count})
    _fresh(page, mock, until, ws, size=(1920, 1080))
    tids = list(THINKING_TIDS[:count])
    for n, tid in enumerate(tids, 1):
        _open(page, until, n, tid, "p2" if tid == "t3" else "p1")
    raf = """() => new Promise((done) => {
        const real = window.requestAnimationFrame; let calls = 0;
        window.requestAnimationFrame = (cb) => { calls++; return real.call(window, cb); };
        setTimeout(() => { window.requestAnimationFrame = real; done(calls); }, 1000);
      })"""
    canvases = page.locator("canvas").count()
    before = page.evaluate(raf)
    for tid in tids:
        mock.emit("turn_started", {}, thread_id=tid)
    mock.emit("tool_started", {"call_id": "many1", "name": "pytest"}, thread_id=tids[-1])
    until(lambda: all(_line(page, n) is not None for n in range(1, len(tids) + 1)), timeout=4)
    lines = [_line(page, n) for n in range(1, len(tids) + 1)]
    check(f"thinking: {len(lines)} lines at once, one in every chat pane",
          all(l and l["visible"] for l in lines) and lines[-1]["word"] == "Running pytest…",
          str([l and l["word"] for l in lines]))
    check("thinking: no canvas in any line", all(l["canvas"] == 0 and l["svg"] == 1 for l in lines),
          str([(l["canvas"], l["svg"]) for l in lines]))
    check("thinking: and no canvas added anywhere by them", page.locator("canvas").count() == canvases,
          f"{canvases} → {page.locator('canvas').count()}")
    during = page.evaluate(raf)
    check("thinking: and no animation-frame loop of their own (the big orb's alone)",
          during <= before * 1.25 + 5, f"{before} frames/s before, {during} with {len(lines)} lines")
    check("thinking: one clock for them all", _clock(page) == {"subscribers": len(lines), "running": True},
          str(_clock(page)))
    _choose(page, until, "single")
    until(lambda: _clock(page)["subscribers"] == 1, timeout=3)
    check("thinking: a pane the layout hides stops ticking (only drawn lines keep the clock)",
          _clock(page) == {"subscribers": 1, "running": True}, str(_clock(page)))
    _idle_all(page, mock, until)


def _thinking_log_checks(page, mock, check, until):
    """The single layout's log says exactly what it said before the line came
    up. Kills: the line inside the log."""
    _fresh(page, mock, until, SINGLE)
    _open(page, until, 1, "t1")
    before = page.locator(_pane(1, '[data-testid="log"]')).inner_text()
    mock.emit("turn_started", {}, thread_id="t1")
    until(lambda: _line(page, 1) is not None, timeout=3)
    after = page.locator(_pane(1, '[data-testid="log"]')).inner_text()
    check("thinking: the single layout's [data-testid=log] text is identical with the line up",
          before == after and "Thinking" not in after, f"{before!r} vs {after!r}")
    check("thinking: and the line is no part of the log",
          page.locator('[data-testid="log"] [data-testid="thinking"]').count() == 0)
    _idle_all(page, mock, until)


def _thinking_accent_checks(page, mock, check, until):
    """An avatar's accent recolours 'Thinking…' and never amber or red."""
    _fresh(page, mock, until, TWO)
    _open(page, until, 1, "t1")
    _open(page, until, 2, "t2")
    try:
        mock.world["avatars"]["active"] = "hoot"
        mock.emit("avatar", {})
        until(lambda: (page.evaluate("window.__hud.state().avatar") or {}).get("slug") == "hoot", timeout=3)
        mock.emit("turn_started", {}, thread_id="t1")
        mock.emit("turn_started", {}, thread_id="t2")
        mock.emit("tool_started", {"call_id": "acc1", "name": "ls"}, thread_id="t2")
        until(lambda: (_line(page, 2) or {}).get("kind") == "tool" and _line(page, 1) is not None, timeout=3)
        think, tool = _line(page, 1), _line(page, 2)
        check("thinking: the avatar accent recolours 'Thinking…'",
              think["stroke"] == HOOT_RGB and think["coreFill"] == HOOT_RGB, str(think["stroke"]))
        check("thinking: and only it: a tool line stays amber", tool["stroke"] == AMBER_RGB, str(tool["stroke"]))
        mock.emit("error", {"message": "boom", "fatal": False}, thread_id="t2")
        until(lambda: (_line(page, 2) or {}).get("kind") == "error", timeout=3)
        check("thinking: and an error stays red", _line(page, 2)["stroke"] == RED_RGB, str(_line(page, 2)["stroke"]))
        check("thinking: the word 'Thinking…' is not painted with the accent (it stays sparing)",
              think["wordColor"] != HOOT_RGB, str(think["wordColor"]))
    finally:
        mock.world["avatars"]["active"] = ""
        mock.emit("avatar", {})
    page.evaluate("window.__hud.dispatch({type: 'patch', patch: {error: ''}})")
    _idle_all(page, mock, until)

def _thinking_box_cap_checks(page, mock, check, until):
    """The line is one more fixed part above the input bar: a long draft held
    at the conversation's minimum (PR #29's cap) is measured again when it
    comes, so the line never takes the log below that minimum. Kills: the
    input bar not refitting when the line comes and goes."""
    from tests.face.hud_v2_declutter_check import (
        FORTY_LINES, MIN_LOG_PX, MIN_LOG_SHARE, SHARE, SINGLE as D_SINGLE, _box_parts, _fresh as d_fresh,
    )
    z = 1.6
    ws = json.loads(D_SINGLE)
    ws["panel"] = {"open": True, "height": 120}
    # Keep the min-log term tighter than 35% after removing the tool ticker.
    d_fresh(page, mock, until, json.dumps(ws), size=(1920, 660), zoom="160")
    _open(page, until, 1, "t1")
    box = _box(page, 1)
    box.click()
    box.fill(FORTY_LINES)
    time.sleep(0.3)

    def log_ok(where: str):
        p = _box_parts(page, 1)
        pane, log, b = p["pane"]["h"], (p["log"]["h"] if p["log"] else 0), p["box"]["h"]
        min_log = max(MIN_LOG_PX * z, MIN_LOG_SHARE * pane)
        return p, log >= min_log - 1, f"{where}: pane {pane:.0f} log {log:.0f} (min {min_log:.0f}) box {b:.0f}"

    p, ok, detail = log_ok("no line")
    check("setup: the min-log term holds the box, and the log is at its minimum",
          ok and p["box"]["h"] < SHARE * p["pane"]["h"] - 2 and p["scrolls"], detail)
    # A line that comes without the pane turning busy (a question open on the
    # thread): no Stop appears to narrow the box and refit it by the way.
    mock.activity("thread", "t1", "needs_input")
    until(lambda: _line(page, 1) is not None, timeout=3)
    time.sleep(0.3)
    p, ok, detail = log_ok("line up")
    check("setup: the line came with no Stop (nothing else changed the input bar)",
          page.locator(_pane(1, '[data-testid="stop"]')).count() == 0 and _chat(page, 1)["busy"] is False)
    check("thinking: with the line up the box is capped again, and the log keeps its minimum", ok, detail)
    mock.activity("thread", "t1", "idle")
    until(lambda: _line(page, 1) is None, timeout=3)
    box.fill("")


# ---------------------------------------------------------------------------
# eight panes (WP-G1)

EIGHT = json.dumps({"preset": "grid8", "focused": 1, "panes": [{"view": "chat"} for _ in range(8)]})
# Ids of their own: the mock names threads made by earlier sections t4, t5, …
EIGHT_THREADS = {1: ("t1", "p1"), 2: ("t2", "p1"), 3: ("t3", "p2"), 4: ("e4", "p1"), 5: ("e5", "p1"),
                 6: ("e6", "p1"), 7: ("e7", "p1"), 8: ("e8", "p1")}


def _seed_eight(mock):
    """Five more chat threads in jarvis, e4 … e8, each with a transcript of its own."""
    w = mock.world
    base = next(t for t in w["threads"] if t["id"] == "t1")
    for n in range(4, 9):
        tid = f"e{n}"
        if any(t["id"] == tid for t in w["threads"]):
            continue
        w["threads"].append({**base, "id": tid, "title": f"chat number {n}", "model": None,
                             "updated": f"2026-09-0{n}T00:00:00+00:00"})
        w["transcripts"][tid] = [
            {"role": "user", "text": f"question for pane {n}", "at": f"2026-09-0{n}T00:00:00+00:00"},
            {"role": "assistant", "text": f"answer from thread {n}", "at": f"2026-09-0{n}T00:00:01+00:00"},
        ]


def _eight_chats_checks(page, mock, check, until):
    _seed_eight(mock)
    _fresh(page, mock, until, EIGHT, size=(1920, 1080))
    ws = '[data-testid="workspace"]'
    check("setup: 4×2 at 1920x1080, eight chat panes drawn, each with its own box",
          _attr(page, ws, "data-drawn-preset") == "grid8"
          and all(_visible(page, _pane(n)) and _attr(page, _pane(n), "data-view") == "chat" for n in range(1, 9))
          and all(_box(page, n).count() == 1 for n in range(1, 9)))
    for n, (tid, project) in EIGHT_THREADS.items():
        _open(page, until, n, tid, project)
    c = _chats(page)
    check("eight panes on eight threads: the store holds one conversation per pane",
          [c[str(n)]["threadId"] for n in range(1, 9)] == [EIGHT_THREADS[n][0] for n in range(1, 9)],
          str([c[str(n)]["threadId"] for n in range(1, 9)]))
    logs = {n: _log(page, n) for n in range(4, 9)}
    check("each of panes 4–8 draws its own thread's transcript and no other's",
          all(f"question for pane {n}" in logs[n] and all(f"pane {m}" not in logs[n] for m in range(4, 9) if m != n)
              for n in range(4, 9)), str({n: v[:40] for n, v in logs.items()}))
    rows = page.locator('[data-testid="sidebar"] [data-pane]')
    marked = sorted(r.get_attribute("data-pane") for r in rows.all())
    check("the sidebar marks all eight open threads, each with its pane",
          marked == [str(n) for n in range(1, 9)], str(marked))

    stop = {n: page.locator(_pane(n, '[data-testid="stop"]')) for n in range(1, 9)}
    mock.emit("turn_started", {}, thread_id="e7")
    until(lambda: _chat(page, 7)["busy"], timeout=3)
    mock.emit("text_delta", {"text": "seven is working"}, thread_id="e7")
    until(lambda: page.locator(_pane(7, '[data-testid="draft"]')).count() > 0, timeout=3)
    check("a turn in pane 7's thread draws only in pane 7: its draft and its Stop",
          "seven is working" in page.locator(_pane(7, '[data-testid="draft"]')).inner_text()
          and all(page.locator(_pane(n, '[data-testid="draft"]')).count() == 0 for n in range(1, 9) if n != 7)
          and stop[7].count() == 1 and all(stop[n].count() == 0 for n in range(1, 9) if n != 7)
          and all(not _chat(page, n)["busy"] for n in range(1, 9) if n != 7))
    mock.emit("tool_started", {"call_id": "c7", "name": "pytest"}, thread_id="e7")
    until(lambda: page.locator(_pane(7, '[data-testid="tool-group"]')).count() > 0, timeout=3)
    check("and its tool call ticks in pane 7 only",
          all(page.locator(_pane(n, '[data-testid="tool-group"]')).count() == 0 for n in range(1, 9) if n != 7))

    mock.emit("turn_started", {}, thread_id="e8")
    until(lambda: _chat(page, 8)["busy"], timeout=3)
    until(lambda: stop[8].count() == 1, timeout=2)
    i7, i8 = len(mock.sent("POST", "/threads/e7/interrupt")), len(mock.sent("POST", "/threads/e8/interrupt"))
    stop[8].click()
    until(lambda: len(mock.sent("POST", "/threads/e8/interrupt")) > i8, timeout=3)
    time.sleep(0.2)
    check("Stop in pane 8 interrupts pane 8's thread", len(mock.sent("POST", "/threads/e8/interrupt")) == i8 + 1)
    check("and leaves pane 7's running, its Stop still there",
          len(mock.sent("POST", "/threads/e7/interrupt")) == i7 and _chat(page, 7)["busy"] and stop[7].count() == 1)
    mock.emit("turn_finished", {"stop": "interrupted"}, thread_id="e8")
    until(lambda: not _chat(page, 8)["busy"], timeout=3)
    check("pane 8's finish frees pane 8 and only pane 8", _chat(page, 7)["busy"] and stop[7].count() == 1)

    page.locator(_pane(6, '[data-testid="log"]')).click()
    until(lambda: _sel(page) == 6, timeout=2)
    check("a click in pane 6 makes it the selected chat: its mark, its mic",
          _sel(page) == 6 and _attr(page, _pane(6), "data-selected") == "true"
          and page.locator('[data-testid="pane-6-mic"]').count() == 1
          and page.locator('[data-testid$="-mic"][data-testid^="pane-"]').count() == 1)
    page.evaluate(UTTERANCE)
    check("voice lands in pane 6's box (REVIEW), while pane 7's turn runs",
          bool(until(lambda: "what is the weather" in _box(page, 6).input_value(), timeout=5))
          and all(_box(page, n).input_value() == "" for n in range(1, 9) if n != 6),
          _box(page, 6).input_value())
    _box(page, 6).fill("")
    mock.emit("turn_finished", {"stop": "end"}, thread_id="e7")
    until(lambda: not _chat(page, 7)["busy"], timeout=3)
    page.evaluate("window.__hud.capture.closeFollowUp()")


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
            multichat_checks(browser, mock, f"http://127.0.0.1:{mock.port}", H.check, H.until,
                             H.guard, H.FAKE_RECOGNIZER)
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


# ---------------------------------------------------------------------------
# WP-G2: split and close a chat pane (plan §3.5–§3.6)

def _order(page) -> list:
    return page.evaluate("window.__hud.workspace().ws.order")


def _split_close_chat_checks(page, mock, check, until):
    """A chat's copy is a new compose row in its project, the thread still in
    one pane. × on a chat pane mid-turn: the turn keeps running, the words in
    its box and the words a Stop handed back wait for the thread, and the pane
    that opens the thread next has its Stop. A compose row's unsent words are
    asked about, and Discard really drops them."""
    w = mock.world
    # One pane and nothing hidden (a stored WP-B value would keep panes 2–4).
    _fresh(page, mock, until, json.dumps({"preset": "single", "order": [1], "kept": [], "focused": 1,
                                          "panes": [{"view": "chat"}]}))
    _open(page, until, 1, "t3", "p2")
    page.locator('[data-testid="pane-1-split"]').click()
    until(lambda: _order(page) == [1, 2], timeout=2)
    until(lambda: (_chat(page, 2)["compose"] or {}).get("projectId") == "p2", timeout=3)
    c = _chats(page)
    check("G2: splitting a thread's chat opens a new compose row in the same project, focused beside it",
          c["2"]["compose"]["projectId"] == "p2" and c["2"]["threadId"] is None
          and _attr(page, _pane(2), "data-focused") == "true", str(c["2"]["compose"]))
    check("G2: and the thread is open in one pane only",
          sum(1 for n in c if c[n]["threadId"] == "t3") == 1 and c["1"]["threadId"] == "t3")

    # Mid-turn: a queued message, half-typed words, then × on pane 1.
    mock.emit("turn_started", {}, thread_id="t3")
    until(lambda: _chat(page, 1)["busy"], timeout=3)
    mock.activity("thread", "t3", "working")
    w["send_status"] = "queued"
    box1 = _box(page, 1)
    box1.fill("queued words")
    box1.press("Enter")
    until(lambda: "queued" in page.locator(_pane(1, '[data-testid="msg-user"]')).last.inner_text(), timeout=3)
    queued_id = f"msg-{w['sends']}"
    box1.fill("half typed")
    page.locator('[data-testid="pane-1-close"]').click()
    until(lambda: _order(page) == [2], timeout=2)
    time.sleep(0.5)
    check("G2: × on a thread's chat mid-turn closes it at once (its words can wait for the thread)",
          page.locator('[data-testid="pane-1-close-ask"]').count() == 0 and page.locator(_pane(1)).count() == 0)
    check("G2: the turn is left running for the thread, and its sidebar dot still says working",
          page.evaluate("window.__hud.state().loose").get("t3") is not None
          and page.locator('[data-testid="activity-t3"]').get_attribute("data-status") == "working",
          str(page.evaluate("window.__hud.state().loose")))
    mock.emit("text_delta", {"text": "streaming on"}, thread_id="t3")
    mock.emit("queue_cleared", {"reason": "stopped", "messages": [
        {"message_id": queued_id, "typed": "queued words", "via": "hud"}]}, thread_id="t3")
    page.wait_for_timeout(400)
    check("G2: the stream goes on with no pane drawing it, and handed-back words land in no other box",
          _box(page, 2).input_value() == "" and "streaming on" not in _chat(page, 2)["draft"], _box(page, 2).input_value())
    w.pop("send_status", None)

    _expand(page, until, "p2", "thread-t3")
    page.locator('[data-testid="thread-t3"]').click()
    until(lambda: _chat(page, 2)["threadId"] == "t3", timeout=3)
    stop2 = page.locator(_pane(2, '[data-testid="stop"]'))
    check("G2: reopening the thread in another pane brings its Stop there",
          bool(until(lambda: stop2.count() == 1, timeout=3)) and _chat(page, 2)["busy"]
          and _chat(page, 2)["turnThreadId"] == "t3")
    box2 = _box(page, 2)
    check("G2: and the words that were in the closed pane's box, and the words the Stop handed back",
          bool(until(lambda: "half typed" in box2.input_value() and "queued words" in box2.input_value(), timeout=3)),
          repr(box2.input_value()))
    calls = len(mock.sent("POST", "/threads/t3/interrupt"))
    stop2.click()
    check("G2: and that Stop interrupts the turn the closed pane started",
          bool(until(lambda: len(mock.sent("POST", "/threads/t3/interrupt")) > calls, timeout=3)))
    mock.emit("turn_finished", {"stop": "interrupted"}, thread_id="t3")
    until(lambda: _chat(page, 2)["busy"] is False, timeout=3)
    mock.activity("thread", "t3", "idle")
    box2.fill("")

    # A compose row's unsent words: × asks; Keep keeps; Discard drops them.
    page.locator('[data-testid="pane-2-split"]').click()
    until(lambda: len(_order(page)) == 2, timeout=2)
    new = _order(page)[1]
    until(lambda: _chat(page, new)["compose"] is not None, timeout=3)
    _box(page, new).fill("never sent")
    page.locator(f'[data-testid="pane-{new}-close"]').click()
    ask = page.locator(f'[data-testid="pane-{new}-close-ask"]')
    until(lambda: ask.count() > 0, timeout=2)
    check("G2: × on a compose row with unsent words asks first",
          ask.is_visible() and "unsent" in ask.inner_text() and new in _order(page))
    page.locator(f'[data-testid="pane-{new}-close-keep"]').click()
    until(lambda: ask.count() == 0, timeout=2)
    check("G2: Keep keeps the pane and its words", new in _order(page) and _box(page, new).input_value() == "never sent")
    time.sleep(0.5)
    page.locator(f'[data-testid="pane-{new}-close"]').click()
    until(lambda: ask.count() > 0, timeout=2)
    page.locator(f'[data-testid="pane-{new}-close-discard"]').click()
    until(lambda: new not in _order(page), timeout=2)
    time.sleep(0.5)
    page.locator('[data-testid="pane-2-split"]').click()
    until(lambda: len(_order(page)) == 2, timeout=2)
    again = _order(page)[1]
    until(lambda: _chat(page, again)["compose"] is not None, timeout=3)
    check("G2: Discard closes it, and its words come back nowhere — not in the next copy that reuses its id",
          again == new and _box(page, again).input_value() == ""
          and "never sent" not in json.dumps(page.evaluate("window.__hud.state().drafts")))
    _box(page, 2).fill("")


def _split_close_selected_checks(page, mock, check, until):
    """× on the selected chat: the selection goes to the chat selected before
    it that is still drawn, and voice lands there. A split the owner did not
    ask for (`{focus: false}`) moves no focus and no selection."""
    _fresh(page, mock, until, GRID_CHATS)
    _focus(page, until, 2)
    _focus(page, until, 3)
    _focus(page, until, 1)
    check("setup: pane 1 selected, pane 3 before it, then pane 2", _sel(page) == 1
          and page.evaluate("window.__hud.state().selectedOrder")[:3] == [1, 3, 2])
    page.locator('[data-testid="pane-1-close"]').click()
    until(lambda: 1 not in _order(page), timeout=2)
    check("G2: closing the selected chat selects the one selected before it (pane 3)",
          bool(until(lambda: _sel(page) == 3, timeout=2)) and _attr(page, _pane(3), "data-selected") == "true"
          and 1 not in page.evaluate("window.__hud.state().selectedOrder"), str(_sel(page)))
    page.evaluate(UTTERANCE)
    check("G2: and what is said next lands in pane 3's box",
          bool(until(lambda: "what is the weather" in _box(page, 3).input_value(), timeout=5))
          and all(_box(page, n).input_value() == "" for n in (2, 4)))
    _box(page, 3).fill("")

    _box(page, 3).click()
    active = page.evaluate("document.activeElement && document.activeElement.closest('[data-testid^=\"pane-\"]')"
                           ".getAttribute('data-testid')")
    focused = page.evaluate("window.__hud.workspace().ws.focused")
    got = page.evaluate("window.__hud.split(4, {focus: false})")
    until(lambda: len(_order(page)) == 4, timeout=2)
    time.sleep(0.3)
    after = page.evaluate("document.activeElement && document.activeElement.closest('[data-testid^=\"pane-\"]')"
                          ".getAttribute('data-testid')")
    check("G2: a split with {focus: false} adds the pane after its source and moves nothing the owner is using",
          got is not None and _order(page)[-1] == got and page.evaluate("window.__hud.workspace().ws.focused") == focused
          and active == after == "pane-3" and _sel(page) == 3, f"{got} {_order(page)} {active} {after} {_sel(page)}")

    # The same from an event handler outside React (review of PR #47, L5: the
    # dev-server pane will be opened from a socket's message), with focus in
    # pane 3's box: it stays there.
    _box(page, 3).click()
    page.evaluate("""() => {
      window.__splitKey = (e) => {
        if (e.key !== 'F9') return;
        e.preventDefault();
        window.__splitGot = window.__hud.split(4, {focus: false});
      };
      // Capture phase: the chat box stops its keys from bubbling.
      window.addEventListener('keydown', window.__splitKey, true);
      return true;
    }""")
    n = len(_order(page))
    page.keyboard.press("F9")
    split = bool(until(lambda: len(_order(page)) == n + 1, timeout=2))
    time.sleep(0.4)
    after = page.evaluate("document.activeElement && document.activeElement.closest('[data-testid^=\"pane-\"]')"
                          ".getAttribute('data-testid')")
    page.evaluate("window.removeEventListener('keydown', window.__splitKey, true)")
    check("G2: a split with {focus: false} from an event handler leaves DOM focus, the focused pane and the mic where they were",
          split and page.evaluate("window.__splitGot") is not None and after == "pane-3"
          and page.evaluate("window.__hud.workspace().ws.focused") == 3 and _sel(page) == 3,
          f"split={split} {after} {_sel(page)}")


# ---------------------------------------------------------------------------
# Review of PR #47 (WP-G2). Each failed on bf09633; the docstring names the
# finding, and the mutation table in the PR body the change each one kills.

def _state(page, key: str):
    return page.evaluate(f"window.__hud.state().{key}")


def _rv47_kept_compose_checks(page, mock, check, until):
    """H1: a ⊞-hidden compose row's unsent words are never lost when a split
    or Open beside takes its pane back. An empty hidden pane goes first; when
    every one holds words, the words are parked for the next new thread."""
    _fresh(page, mock, until, EIGHT, size=(1920, 1080))
    until(lambda: len(_order(page)) == 8 and all(_chat(page, n)["compose"] for n in range(1, 9)), timeout=4)
    _focus(page, until, 8)
    _box(page, 8).fill("precious unsent words in pane 8")
    _focus(page, until, 1)
    _choose(page, until, "single")
    page.locator('[data-testid="pane-1-split"]').click()
    until(lambda: len(_order(page)) == 2, timeout=2)
    took = _order(page)[1]
    check("review #47 H1: a split takes an empty hidden pane before one holding unsent words",
          took != 8 and _chat(page, 8)["input"] == "precious unsent words in pane 8", f"took {took}")
    _choose(page, until, "grid8")
    check("review #47 H1: and the words are still in pane 8's box when ⊞ brings it back",
          _box(page, 8).input_value() == "precious unsent words in pane 8")

    # Every hidden pane holds words: the oldest is taken, and its words come
    # back in the split's own new thread.
    _fresh(page, mock, until, EIGHT, size=(1920, 1080))
    until(lambda: len(_order(page)) == 8 and all(_chat(page, n)["compose"] for n in range(1, 9)), timeout=4)
    for n in range(2, 8):
        page.evaluate("n => window.__hud.dispatch({type: 'input', pane: n, text: `words of pane ${n}`})", n)
    _focus(page, until, 8)
    _box(page, 8).fill("precious unsent words in pane 8")
    _focus(page, until, 1)
    _choose(page, until, "single")
    page.locator('[data-testid="pane-1-split"]').click()
    until(lambda: len(_order(page)) == 2, timeout=2)
    took = _order(page)[1]
    until(lambda: "precious" in _box(page, took).input_value(), timeout=3)
    check("review #47 H1: with every hidden pane holding words, the split takes the oldest and its words come back in the new pane",
          took == 8 and _box(page, took).input_value() == "precious unsent words in pane 8", f"{took} {_box(page, took).input_value()!r}")
    _choose(page, until, "grid8")
    check("review #47 H1: and every other hidden pane's words are where they were",
          all(_box(page, n).input_value() == f"words of pane {n}" for n in range(2, 8)))

    # Open beside the same way: the thread opens in the taken pane, and the
    # compose row's words wait for the next new thread.
    _fresh(page, mock, until, EIGHT, size=(1920, 1080))
    until(lambda: len(_order(page)) == 8 and all(_chat(page, n)["compose"] for n in range(1, 9)), timeout=4)
    for n in range(2, 8):
        page.evaluate("n => window.__hud.dispatch({type: 'input', pane: n, text: `words of pane ${n}`})", n)
    _focus(page, until, 8)
    _box(page, 8).fill("precious words beside")
    _focus(page, until, 1)
    _choose(page, until, "single")
    _expand(page, until, "p2", "thread-t3")
    page.locator('[data-testid="thread-t3"]').click(modifiers=["Alt"])
    until(lambda: len(_order(page)) == 2 and _chat(page, _order(page)[1])["threadId"] == "t3", timeout=3)
    check("review #47 H1: Open beside takes the oldest hidden pane, and its unsent words are kept, not dropped",
          "precious words beside" in json.dumps(_state(page, "drafts")), str(_state(page, "drafts")))
    page.locator('[data-testid="new-thread"]').click()
    pane = _sel(page)
    until(lambda: pane is not None and "precious words beside" in _box(page, pane).input_value(), timeout=3)
    check("review #47 H1: and they come back in the next new thread",
          pane is not None and _box(page, pane).input_value() == "precious words beside",
          _box(page, pane).input_value() if pane else "no pane")
    _box(page, pane).fill("")


def _rv47_close_keeps_mic_checks(page, mock, check, until):
    """M1: closing a pane that is not the selected chat leaves the mic — and
    files dropped outside a pane — with the chat the owner selected, never the
    chat that slides into the closed slot."""
    ws = json.dumps({"preset": "cols3", "order": [1, 2, 3], "kept": [], "focused": 1,
                     "panes": [{"view": "chat"}, {"view": "file"}, {"view": "chat"}]})
    _fresh(page, mock, until, ws, size=(1920, 1080))
    _focus(page, until, 3)
    _focus(page, until, 1)
    page.locator('[data-testid="pane-2-close"]').click()
    until(lambda: _order(page) == [1, 3], timeout=2)
    time.sleep(0.5)
    check("review #47 M1: × on a File pane between two chats leaves pane 1 the selected chat, focused",
          _sel(page) == 1 and page.evaluate("window.__hud.workspace().ws.focused") == 1, str(_sel(page)))
    # Nothing here clicks a box: that would select its pane and hide the bug.
    _drop_on(page, '[data-testid="sidebar"]', "dropped.txt")
    until(lambda: any(f["name"] == "dropped.txt" for f in _chat(page, 1)["files"] + _chat(page, 3)["files"]), timeout=3)
    check("review #47 M1: a file dropped outside a pane goes to pane 1",
          any(f["name"] == "dropped.txt" for f in _chat(page, 1)["files"]) and not _chat(page, 3)["files"],
          str([f["name"] for f in _chat(page, 3)["files"]]))
    page.evaluate(UTTERANCE)
    until(lambda: "what is the weather" in _box(page, 1).input_value() + _box(page, 3).input_value(), timeout=5)
    check("review #47 M1: and what is said next lands in pane 1, not the chat that slid into the slot",
          "what is the weather" in _box(page, 1).input_value() and _box(page, 3).input_value() == "",
          _box(page, 3).input_value())
    _box(page, 1).fill("")
    _box(page, 3).fill("")
    _unstage(page, 1)
    _unstage(page, 3)

    ws = json.dumps({"preset": "cols3", "order": [1, 2, 3], "kept": [], "focused": 1,
                     "panes": [{"view": "chat"}, {"view": "chat"}, {"view": "file"}]})
    _fresh(page, mock, until, ws, size=(1920, 1080))
    _focus(page, until, 2)
    _focus(page, until, 1)
    page.locator('[data-testid="pane-3-close"]').click()
    until(lambda: _order(page) == [1, 2], timeout=2)
    time.sleep(0.5)
    check("review #47 M1: × on the last-slot File pane leaves pane 1 selected, not pane 2", _sel(page) == 1,
          str(_sel(page)))


def _rv47_first_send_closed_checks(page, mock, check, until):
    """M2: a compose pane closed while its first send opens the thread leaves
    the new thread's turn loose, so the pane that opens the thread has Stop.
    And a first send that fails after its pane closed puts its words in the
    next new thread wherever it opens, and says so."""
    _fresh(page, mock, until, TWO)
    _open(page, until, 1, "t1")
    _focus(page, until, 2)
    held = []
    page.route(OPEN_URL, lambda r: held.append(r) if r.request.method == "POST" else r.continue_())
    try:
        _box(page, 2).fill("first words of a new thread")
        _box(page, 2).press("Enter")
        until(lambda: _pumped(page) and len(held) > 0, timeout=3)
        page.locator('[data-testid="pane-2-close"]').click()
        until(lambda: _order(page) == [1], timeout=2)
        held[0].continue_()
    finally:
        page.unroute(OPEN_URL)
    until(lambda: _state(page, "loose"), timeout=4)
    loose = _state(page, "loose")
    new = next(iter(loose), None)
    check("review #47 M2: the new thread's turn is left loose when its pane closed mid first send",
          new is not None and new not in ("t1", "t2", "t3") and len(mock.sent("POST", f"/threads/{new}/send")) == 1,
          str(loose))
    if new:
        mock.emit("turn_started", {}, thread_id=new)
        _expand(page, until, "p1", f"thread-{new}")
        page.locator(f'[data-testid="thread-{new}"]').click()
        until(lambda: _chat(page, 1)["threadId"] == new, timeout=3)
        stop = page.locator(_pane(1, '[data-testid="stop"]'))
        check("review #47 M2: opening that thread shows its Stop", bool(until(lambda: stop.count() == 1, timeout=3))
              and _state(page, "loose") == {})
        mock.emit("turn_finished", {"stop": "done"}, thread_id=new)
        until(lambda: _chat(page, 1)["busy"] is False, timeout=3)

    # The same send failing after the close.
    _fresh(page, mock, until, TWO)
    _open(page, until, 1, "t1")
    _focus(page, until, 2)
    held = []
    page.route(OPEN_URL, lambda r: held.append(r) if r.request.method == "POST" else r.continue_())
    try:
        _box(page, 2).fill("words whose send will fail")
        _box(page, 2).press("Enter")
        until(lambda: _pumped(page) and len(held) > 0, timeout=3)
        page.locator('[data-testid="pane-2-close"]').click()
        until(lambda: _order(page) == [1], timeout=2)
        held[0].fulfill(status=500, content_type="application/json", body=json.dumps({"error": "daemon hiccup"}))
    finally:
        page.unroute(OPEN_URL)
    until(lambda: "next new thread" in (_state(page, "error") or ""), timeout=3)
    check("review #47 M2: a first send failing after its pane closed says its words wait for the next new thread",
          "next new thread you start" in _state(page, "error") and "in this pane" not in _state(page, "error"),
          _state(page, "error"))
    page.locator('[data-testid="new-thread"]').click()
    until(lambda: "words whose send will fail" in _box(page, 1).input_value(), timeout=3)
    check("review #47 M2: and the next new thread, in the pane still open, gets them back",
          _box(page, 1).input_value() == "words whose send will fail", _box(page, 1).input_value())
    _box(page, 1).fill("")


def _rv47_loose_unlisted_checks(page, mock, check, until):
    """L1: a loose turn whose thread is no longer listed (archived, deleted)
    is cleared by the reconcile, so the 15 s poll does not run for good."""
    _fresh(page, mock, until, TWO)
    _open(page, until, 2, "t2")
    mock.emit("turn_started", {}, thread_id="t2")
    until(lambda: _chat(page, 2)["busy"], timeout=3)
    page.locator('[data-testid="pane-2-close"]').click()
    until(lambda: _order(page) == [1], timeout=2)
    check("setup: × on pane 2 mid-turn leaves t2's turn loose", _state(page, "loose").get("t2") is not None,
          str(_state(page, "loose")))
    w = mock.world
    gone = next(t for t in w["threads"] if t["id"] == "t2")
    w["threads"].remove(gone)                     # archived elsewhere: no longer listed
    try:
        page.evaluate("window.__hud.reconcile()")
        check("review #47 L1: the reconcile clears a loose turn whose thread is no longer listed",
              bool(until(lambda: _state(page, "loose") == {}, timeout=3)), str(_state(page, "loose")))
    finally:
        w["threads"].append(gone)
    mock.emit("turn_finished", {"stop": "done"}, thread_id="t2")


def _rv47_ask_strip_checks(page, mock, check, until):
    """L3: the close question is dropped when what it asked about changes, so
    Discard never answers a question nobody asked; and after a card over it,
    focus is back on Keep."""
    _fresh(page, mock, until, TWO)
    _open(page, until, 1, "t1")
    _focus(page, until, 2)
    _box(page, 2).fill("unsent compose words")
    page.locator('[data-testid="pane-2-close"]').click()
    ask = page.locator('[data-testid="pane-2-close-ask"]')
    until(lambda: ask.count() > 0, timeout=2)
    _approval(mock, "rv47ask")
    until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
    time.sleep(0.2)
    check("review #47 L3: under a card Discard is disabled and the card holds the focus",
          page.locator('[data-testid="pane-2-close-discard"]').is_disabled() and _active_testid(page) == "approval-card")
    page.keyboard.press("Escape")
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
    check("review #47 L3: once the card is answered, focus is back on Keep",
          bool(until(lambda: _active_testid(page) == "pane-2-close-keep", timeout=2)), str(_active_testid(page)))
    # The words are gone now: the question about them goes. (A switch to
    # another view no longer drops it — the words are still the pane's, and ×
    # still drops them; review 2 of PR #47, MED-1.)
    _box(page, 2).fill("")
    check("review #47 L3: the question is dropped when the words it asked about are gone",
          bool(until(lambda: ask.count() == 0, timeout=2)))


# ---------------------------------------------------------------------------
# Review 2 of PR #47 (the delta review of c3c928f). Each failed on c3c928f.

def _rv47b_close_other_view_checks(page, mock, check, until):
    """MED-1: × judges the pane's conversation whatever view it shows. A
    compose row's unsent words in a pane switched to File: × asks, and the
    strip that asked stays when the pane switches view."""
    _fresh(page, mock, until, TWO)
    _open(page, until, 1, "t1")
    _focus(page, until, 2)
    _box(page, 2).fill("words typed before a switch to File")
    page.locator(_pane(2, '[data-testid="tab-file"]')).click()
    until(lambda: _attr(page, _pane(2), "data-view") == "file", timeout=2)
    page.locator('[data-testid="pane-2-close"]').click()
    ask = page.locator('[data-testid="pane-2-close-ask"]')
    until(lambda: ask.count() > 0, timeout=2)
    time.sleep(0.3)
    check("review 2 #47 MED-1: × on a pane showing File asks about its compose row's unsent words",
          ask.count() == 1 and "unsent" in ask.inner_text() and 2 in _order(page), str(_order(page)))
    page.locator('[data-testid="pane-2-close-keep"]').click()
    until(lambda: ask.count() == 0, timeout=2)
    page.locator(_pane(2, '[data-testid="tab-chat"]')).click()
    until(lambda: _attr(page, _pane(2), "data-view") == "chat", timeout=2)
    check("review 2 #47 MED-1: Keep keeps them", _box(page, 2).input_value() == "words typed before a switch to File",
          _box(page, 2).input_value())

    # Asked in chat, then switched to File: the question still holds, so the
    # strip stays, and × there asks again rather than closing.
    time.sleep(0.5)
    page.locator('[data-testid="pane-2-close"]').click()
    until(lambda: ask.count() > 0, timeout=2)
    page.locator(_pane(2, '[data-testid="tab-file"]')).click()
    until(lambda: _attr(page, _pane(2), "data-view") == "file", timeout=2)
    time.sleep(0.4)
    check("review 2 #47 MED-1: the question stays when the pane switches to File (the words are still there)",
          ask.count() == 1 and 2 in _order(page))
    time.sleep(0.5)
    page.locator('[data-testid="pane-2-close"]').click()
    time.sleep(0.5)
    check("review 2 #47 MED-1: and × there still asks rather than closing",
          2 in _order(page) and ask.count() == 1, str(_order(page)))
    page.locator('[data-testid="pane-2-close-discard"]').click()
    until(lambda: 2 not in _order(page), timeout=2)
    check("review 2 #47 MED-1: Discard closes it, the words dropped as asked",
          "words typed before" not in json.dumps(_state(page, "drafts")), str(_state(page, "drafts")))


def _files(n: int, tag: str) -> list:
    return [{"name": f"{tag}{i}.txt", "mime": "text/plain", "data_b64": "YQ=="} for i in range(n)]


def _rv47b_park_said_checks(page, mock, check, until):
    """MED-2: Open beside reusing a hidden pane says where its unsent words
    went, and they come back in a New thread pressed in a pane that is already
    composing, which says so and names the project they were written in."""
    _fresh(page, mock, until, EIGHT, size=(1920, 1080))
    until(lambda: len(_order(page)) == 8 and all(_chat(page, n)["compose"] for n in range(1, 9)), timeout=4)
    for n in range(2, 9):
        page.evaluate("""n => {
          const c = window.__hud.state().chats[n].compose;
          window.__hud.dispatch({type: 'chat', pane: n, patch: {compose: {...c, projectId: 'p2'}}});
          window.__hud.dispatch({type: 'input', pane: n, text: `school words of pane ${n}`});
          return true;
        }""", n)
    _focus(page, until, 1)
    _box(page, 1).fill("my own words in pane 1")
    _choose(page, until, "single")
    _expand(page, until, "p2", "thread-t3")
    page.locator('[data-testid="thread-t3"]').click(modifiers=["Alt"])
    until(lambda: len(_order(page)) == 2 and _chat(page, _order(page)[1])["threadId"] == "t3", timeout=3)
    took = _order(page)[1]
    err = page.locator('[data-testid="error"]')
    check("review 2 #47 MED-2: Open beside reusing a hidden pane says its unsent words are kept, and for which project",
          bool(until(lambda: err.count() > 0 and "next new thread" in err.inner_text(), timeout=2))
          and "schoolwork" in err.inner_text(), err.inner_text() if err.count() else "no notice")
    _focus(page, until, 1)
    page.evaluate("localStorage.setItem('jarvis.hud.lastProject', 'p1')")   # New thread opens in jarvis
    page.locator('[data-testid="new-thread"]').click()
    until(lambda: f"school words of pane {took}" in _box(page, 1).input_value(), timeout=3)
    check("review 2 #47 MED-2: New thread in a pane already composing takes them back, after its own words",
          _box(page, 1).input_value() == f"my own words in pane 1\nschool words of pane {took}"
          and "compose:*" not in _state(page, "drafts"), repr(_box(page, 1).input_value()))
    check("review 2 #47 MED-2: and says so, naming the project they were written in",
          bool(until(lambda: "are back" in err.inner_text(), timeout=2))
          and "schoolwork" in err.inner_text() and "jarvis" in err.inner_text(), err.inner_text())
    _box(page, 1).fill("")


def _rv47b_thread_park_said_checks(page, mock, check, until):
    """MED-2 and NIT-3: × on a thread's pane — its words wait with the thread,
    and that is said, with the files past eight that did not fit."""
    err = page.locator('[data-testid="error"]')
    _fresh(page, mock, until, TWO)
    _open(page, until, 1, "t1")
    _open(page, until, 2, "t2")
    page.evaluate("""f => {
      window.__hud.dispatch({type: 'park', key: 't2', text: '', files: f.parked});
      window.__hud.dispatch({type: 'input', pane: 2, text: 'words for the second chat', files: f.box});
      return true;
    }""", {"parked": _files(3, "p"), "box": _files(8, "b")})
    page.locator('[data-testid="pane-2-close"]').click()
    until(lambda: _order(page) == [1], timeout=2)
    check("review 2 #47 MED-2/NIT-3: × on a thread's pane says its words wait with that thread, and 3 files were left out",
          bool(until(lambda: err.count() > 0 and "second chat" in err.inner_text(), timeout=2))
          and "3 files were left out" in err.inner_text()
          and len(_state(page, "drafts")["t2"]["files"]) == 8, err.inner_text() if err.count() else "no notice")
    _open(page, until, 1, "t2")
    until(lambda: "words for the second chat" in _box(page, 1).input_value(), timeout=3)
    check("review 2 #47 MED-2: and they are back when the thread opens",
          _box(page, 1).input_value() == "words for the second chat")
    page.evaluate("window.__hud.dispatch({type: 'input', pane: 1, text: '', files: []})")


def _rv47b_send_held_named_checks(page, mock, check, until):
    """LOW-2: a first send that fails at `/send` after its pane closed says
    its words wait with the thread it opened, naming it; and they are back in
    the box when that thread is opened."""
    _fresh(page, mock, until, TWO)
    _open(page, until, 1, "t1")
    _focus(page, until, 2)
    held = []
    page.route(SEND_URL, lambda r: held.append(r) if r.request.method == "POST" else r.continue_())
    try:
        _box(page, 2).fill("words whose send fails at /send")
        _box(page, 2).press("Enter")
        until(lambda: _pumped(page) and len(held) > 0, timeout=3)
        tid = held[0].request.url.rsplit("/", 2)[-2]
        page.locator('[data-testid="pane-2-close"]').click()
        until(lambda: _order(page) == [1], timeout=2)
        held[0].fulfill(status=500, content_type="application/json", body=json.dumps({"error": "daemon hiccup"}))
    finally:
        page.unroute(SEND_URL)
    err = page.locator('[data-testid="error"]')
    until(lambda: err.count() > 0 and "Could not send" in err.inner_text(), timeout=3)
    check("review 2 #47 LOW-2: a first send failing at /send after its pane closed says where its words wait, naming the thread",
          "waits with “new thread”" in err.inner_text(), err.inner_text())
    _expand(page, until, "p1", f"thread-{tid}")
    page.locator(f'[data-testid="thread-{tid}"]').click()
    until(lambda: _chat(page, 1)["threadId"] == tid, timeout=3)
    check("review 2 #47 LOW-2: and they are back in the box when that thread is opened",
          bool(until(lambda: _box(page, 1).input_value() == "words whose send fails at /send", timeout=3)),
          repr(_box(page, 1).input_value()))
    _box(page, 1).fill("")


# ---------------------------------------------------------------------------
# Review 3 of PR #47 (9a093f9), MED: words parked under `compose:*` were taken
# up by a pane nobody could see, and the window said they were "back, in this
# new thread's box". Each failed on 1181a1c.

# A chat and a File pane drawn; panes 5–8 hidden by ⊞, each holding a compose row.
CHAT_FILE_KEPT = json.dumps({"preset": "cols2", "order": [1, 2], "kept": [5, 6, 7, 8], "focused": 1,
                             "panes": [{"view": "chat"}, {"view": "file"}, {"view": "browser"}, {"view": "task"},
                                       {"view": "chat"}, {"view": "chat"}, {"view": "chat"}, {"view": "chat"}]})

PARK = "w => window.__hud.dispatch({type: 'park', key: 'compose:*', text: w, projectId: 'p1'})"


def _rv47c_close_orphan_checks(page, mock, check, until):
    """(a) × on a pane gives the closed pane no compose row, so it takes up no
    parked words; the next New thread, on screen, does, and says so."""
    _fresh(page, mock, until, TWO)
    _open(page, until, 2, "t1")
    page.evaluate(PARK, "orphan words A")
    page.locator('[data-testid="pane-1-close"]').click()
    until(lambda: _order(page) == [2], timeout=2)
    time.sleep(0.5)                               # the effect that gave the closed pane a row runs after the render
    drafts, closed = _state(page, "drafts"), _chat(page, 1)
    check("review 3 #47 MED (a): × on pane 1 leaves the parked words parked, and the closed pane holds nothing",
          drafts.get("compose:*", {}).get("text") == "orphan words A" and closed["input"] == ""
          and closed["compose"] is None, f"drafts {drafts}, closed pane {closed['input']!r}")
    check("review 3 #47 MED (a): and the window never says they are back",
          "are back" not in _state(page, "error"), _state(page, "error"))
    page.locator('[data-testid="new-thread"]').click()
    until(lambda: "orphan words A" in _box(page, 2).input_value(), timeout=3)
    check("review 3 #47 MED (a): New thread in the pane on screen takes them up, and says so",
          _box(page, 2).input_value() == "orphan words A" and "compose:*" not in _state(page, "drafts")
          and "are back, in this new thread's box" in _state(page, "error"), _state(page, "error"))
    _box(page, 2).fill("")


def _rv47c_archive_orphan_checks(page, mock, check, until):
    """(b) A project archived re-aims every compose row in it, hidden ones
    included, with a new id: no re-aim takes up parked words."""
    page.evaluate("localStorage.setItem('jarvis.hud.lastProject', 'p1')")
    _fresh(page, mock, until, CHAT_FILE_KEPT)
    until(lambda: all((_chat(page, n)["compose"] or {}).get("projectId") == "p1" for n in (5, 6, 7, 8)), timeout=4)
    _open(page, until, 1, "t3", "p2")
    check("setup: pane 1 on t3 (schoolwork), panes 5–8 hidden with compose rows in jarvis",
          _order(page) == [1, 2] and _chat(page, 1)["threadId"] == "t3")
    page.evaluate(PARK, "orphan words B")
    try:
        mock.emit("project_archived", {"project_id": "p1"}, project_id="p1")
        until(lambda: all((_chat(page, n)["compose"] or {}).get("projectId") != "p1" for n in (5, 6, 7, 8)),
              timeout=4)
        time.sleep(0.3)
        boxes = {n: _chat(page, n)["input"] for n in (5, 6, 7, 8)}
        check("review 3 #47 MED (b): the re-aimed hidden panes take up nothing; the words stay parked",
              not any(boxes.values()) and _state(page, "drafts").get("compose:*", {}).get("text") == "orphan words B",
              f"{boxes} {_state(page, 'drafts')}")
        check("review 3 #47 MED (b): and the window never says they are back",
              "are back" not in _state(page, "error"), _state(page, "error"))
        page.locator('[data-testid="new-thread"]').click()
        until(lambda: "orphan words B" in _box(page, 1).input_value(), timeout=3)
        check("review 3 #47 MED (b): the next New thread, in pane 1, takes them up and says so",
              _box(page, 1).input_value() == "orphan words B"
              and "are back, in this new thread's box" in _state(page, "error"), _state(page, "error"))
        _box(page, 1).fill("")
    finally:
        mock.emit("project_restored", {"project_id": "p1"}, project_id="p1")
        page.evaluate("localStorage.setItem('jarvis.hud.lastProject', 'p1')")


def _checkpoint_compose_checks(page, mock, check, until):
    """A failed first send keeps its hidden compose row recoverable."""
    _fresh(page, mock, until, TWO)
    held = []
    page.route(OPEN_URL, lambda r: held.append(r) if r.request.method == "POST" else r.continue_())
    try:
        _box(page, 2).fill("hidden first-send words")
        _box(page, 2).press("Enter")
        assert until(lambda: _pumped(page) and held, timeout=3)
        _focus(page, until, 1)
        _choose(page, until, "single")
        assert until(lambda: not _visible(page, _pane(2)), timeout=2)
        held[0].fulfill(status=500, content_type="application/json", body=json.dumps({"error": "fixture refusal"}))
        held.clear()
        parked = until(lambda: _state(page, "drafts").get("compose:2"), timeout=3)
        check("checkpoint: a hidden compose failure parks its words", parked and parked.get("text") == "hidden first-send words", str(parked))
        check("checkpoint: hidden words never go to the selected chat", not _box(page, 1).input_value())
        check("checkpoint: the hidden row does not silently take up its words", _chat(page, 2)["input"] == "")
        _choose(page, until, "cols2")
        check("checkpoint: showing that compose row restores its words once", bool(until(lambda: _box(page, 2).input_value() == "hidden first-send words", timeout=3)))
        check("checkpoint: successful takeup clears only its parked draft", "compose:2" not in _state(page, "drafts"))
        _box(page, 2).fill("")
    finally:
        for route in held: route.abort()
        page.unroute(OPEN_URL)

    _fresh(page, mock, until, TWO)
    _open(page, until, 2, "t2")
    page.evaluate("""() => window.__hud.dispatch({type:'park',key:'compose:2',text:'closed-slot words',projectId:'p1'})""")
    page.locator('[data-testid="pane-2-close"]').click()
    check("checkpoint: closing a different thread preserves that slot's parked compose words",
          bool(until(lambda: _state(page,"drafts").get("compose:*",{}).get("text") == "closed-slot words", timeout=3)))
    page.locator('[data-testid="new-thread"]').click()
    check("checkpoint: the next owner-started thread retrieves closed-slot words", bool(until(lambda: _box(page,1).input_value() == "closed-slot words", timeout=3)))
    _box(page,1).fill("")


if __name__ == "__main__":
    sys.exit(main())
