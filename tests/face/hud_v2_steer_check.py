"""V3 voice through the real HUD: dictation steers a busy chat, push-to-talk no
longer interrupts, the continuation window, and the follow-up target.

Decisions V-1, V-10 and the owner's answers to plan Q3-Q5 (2026-10-13):
  - speech into a chat running a turn is sent as a steer, the way typing is;
  - push-to-talk on a busy chat steers like typing, and Stop interrupts;
  - a message that has gone out is continued, with no wake word, by speech
    resuming within the continuation window (6 s), and the continuation steers
    the running turn;
  - a reply right after a drawn pane spoke selects that pane and sends there,
    otherwise the selected chat; hidden panes open no follow-up window.
Kept from V2: REVIEW never sends; a different chat than at capture, a card, a
late or retried text, or a busy chat whose turn is not its own thread's all
put the words in the box. V-6: voice never answers an approval card.

Isolated fake microphone and a scripted mock `/stt` on ephemeral ports: no real
audio, external call, credential or live daemon port. Run alone:
`python tests/face/hud_v2_steer_check.py [section,section]`, with a temporary
HOME, dummy keys, the worktree PYTHONPATH and a 3G/0swap systemd scope.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
assert not (REPO / ".env").exists(), "use a credential-free worktree"
from playwright.sync_api import sync_playwright  # noqa: E402

from tests.face.hud_v2_check import FAKE_RECOGNIZER, guard_live, until, watch_csp  # noqa: E402
from tests.face.hud_v2_layout_check import _approval, _choose, _pane  # noqa: E402
from tests.face.hud_v2_mock import MockDaemon  # noqa: E402
from tests.face.hud_v2_multichat_check import (  # noqa: E402
    TWO, _box, _chat, _dictation, _focus, _fresh, _open, _seed, _sent,
)
from tests.face.hud_v2_voice_diagnostics_check import FAKE_MIC  # noqa: E402

THREE = json.dumps({"preset": "cols3", "focused": 1,
                    "panes": [{"view": "chat"}, {"view": "chat"}, {"view": "chat"}, {"view": "task"}]})

QUIET = "window.__hud.mic.feedMs(1500, .0009)"
# Push-to-talk as the capture sees it (App.press is covered by hud_v2_ptt_check).
PTT = "window.__hud.capture.press(); window.__hud.mic.feedMs(1200, .06); window.__hud.capture.release()"
# Speech after `%d` ms of quiet: the words, then the pause that ends them.
SPEECH = "{const m = window.__hud.mic; m.feedMs(%d, .0009); m.feedMs(1200, .06); m.feedMs(2800, .0005);}"
# A message said with the wake word.
WAKE = ("{const m = window.__hud.mic; m.feedMs(2000, .0009); m.feedMs(400, .06); m.wake();"
        " m.feedMs(1000, .06); m.feedMs(2800, .0005);}")


def main(argv=None):
    only = set(filter(None, ((argv if argv is not None else sys.argv[1:]) or [""])[0].split(",")))
    failures: list[str] = []
    count = 0

    def check(name, ok, detail=""):
        nonlocal count
        count += 1
        print(("ok   " if ok else "FAIL ") + name + ("" if ok or not detail else f"  [{detail}]"))
        if not ok:
            failures.append(name)

    mock = MockDaemon(0).start()
    _seed(mock)
    w = mock.world
    try:
        base = f"http://127.0.0.1:{mock.port}"
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1600, "height": 900})

            # Only requests that would leave the machine are routed: a route on
            # every request needs the sync API pumped, which the polling below
            # (time.sleep) does not do. The live daemon ports are refused by
            # guard_live; the mock's own ports are the only ones used.
            def refuse(r):
                failures.append(f"request outside isolated mock: {r.request.url}")
                r.abort()
            context.route(re.compile(r"^https?://(?!(127\.0\.0\.1|localhost)[:/])"), refuse)
            guard_live(context, failures.append)
            watch_csp(context, failures.append)
            context.add_init_script(FAKE_RECOGNIZER + FAKE_MIC)
            page = context.new_page()
            page.set_default_timeout(8000)
            page.on("pageerror", lambda e: failures.append(f"page error {e}"))
            before = mock.sse_connections()
            page.goto(base)
            page.wait_for_selector('[data-testid="sidebar"]', state="attached")
            mock.await_reconnect(before)
            page.wait_for_function("() => window.__hud && window.__hud.voiceDiagnostics().mic === 'available'")

            # ---- helpers -------------------------------------------------
            def stts():
                return mock.sent("POST", "/stt")

            def interrupts(tid):
                return len(mock.sent("POST", f"/threads/{tid}/interrupt"))

            def approvals_posted():
                return [c for c in mock.calls if c[0] == "POST" and c[1].startswith("/approvals/")]

            def rows():
                return page.evaluate("window.__hud.voiceDiagnostics().rows")

            def sel():
                return page.evaluate("window.__hud.state().selectedChat")

            def orb():
                return page.evaluate("window.__hud.state().orb")

            def reset(layout=TWO, mode="auto", select=1, threads=(("t1", "p1"), ("t2", "p1"))):
                w.pop("send_status", None)
                mock.stt_script = []
                _fresh(page, mock, until, layout)
                for n, (tid, project) in enumerate(threads, 1):
                    _open(page, until, n, tid, project)
                _focus(page, until, select)
                _dictation(page, mode)
                page.evaluate(QUIET)

            def busy(tid="t1", pane=1):
                mock.emit("turn_started", {}, thread_id=tid)
                until(lambda: _chat(page, pane)["busy"], timeout=3)

            def finish(tid="t1", pane=1):
                mock.emit("turn_finished", {"stop": "end"}, thread_id=tid)
                until(lambda: not _chat(page, pane)["busy"], timeout=3)

            def say(text, how=PTT, gate=None):
                """Script the transcript, then speak it as `how`."""
                mock.stt_script = [{"text": text, **({"wait": gate} if gate is not None else {})}]
                page.evaluate(QUIET)
                page.evaluate(how)

            def waits(pred, timeout=5.0):
                return bool(until(pred, timeout=timeout))

            def run(section):
                if only and section.__name__ not in only:
                    return
                print(f"\n{section.__name__}")
                try:
                    section()
                except Exception as e:      # a section that cannot run is a failure, the rest still run
                    check(f"{section.__name__} ran to the end", False, str(e).splitlines()[0][:200])
                    try:
                        page.keyboard.press("Escape")
                    except Exception:
                        pass

            # ---- 1. dictation into a busy chat is a steer ----------------
            def busy_steer():
                reset()
                busy()
                w["send_status"] = "steered"
                s0, i0, st0 = len(_sent(mock, "t1")), interrupts("t1"), len(stts())
                say("also run the tests")
                check("the busy chat's mic is not suppressed: the clip is uploaded", waits(lambda: len(stts()) > st0))
                sent = until(lambda: _sent(mock, "t1")[s0:] or None, timeout=5)
                check("AUTO on a busy chat: the words go to its thread as a message, not into the box",
                      bool(sent) and sent[-1].get("text") == "also run the tests", str(sent))
                check("marked as spoken", bool(sent) and sent[-1].get("spoken") is True)
                check("and the box stays empty", _box(page, 1).input_value() == "")
                msgs = _chat(page, 1)["messages"]
                check("it is drawn as a steer on the running turn",
                      bool(msgs) and msgs[-1].get("text") == "also run the tests" and msgs[-1].get("mark") == "steering",
                      str(msgs[-1:]))
                check("the running turn is untouched: still busy, still tracked, no interrupt",
                      _chat(page, 1)["busy"] and _chat(page, 1)["turnThreadId"] == "t1" and interrupts("t1") == i0)
                check("the orb and status line go back to the turn's own (not stuck on listening)",
                      waits(lambda: orb() in ("thinking", "tool")) and "LISTENING" not in (_chat(page, 1).get("status") or ""),
                      f"{orb()} / {_chat(page, 1).get('status')!r}")
                r = rows()
                check("diagnostics: handed to the chat, and no busy suppression exists any more",
                      r[-1]["outcome"] == "handed_to_chat" and not any(x["outcome"] == "suppressed:busy" for x in r),
                      str(r[-1]))
                w.pop("send_status", None)
                finish()

            # ---- 2. Stop is the interrupt ---------------------------------
            def stop_interrupts():
                reset()
                busy()
                i0 = interrupts("t1")
                page.locator(_pane(1, '[data-testid="stop"]')).click()
                check("Stop still interrupts the turn", waits(lambda: interrupts("t1") == i0 + 1))
                finish()

            # ---- 3. REVIEW keeps the words in the box --------------------
            def review_busy_box():
                reset(mode="review")
                busy()
                s0 = len(_sent(mock, "t1"))
                say("remember the box")
                check("REVIEW on a busy chat: the words wait in the box",
                      waits(lambda: "remember the box" in _box(page, 1).input_value()))
                time.sleep(0.3)
                check("and nothing is sent or steered", len(_sent(mock, "t1")) == s0)
                check("the turn's orb is not left on listening", waits(lambda: orb() in ("thinking", "tool")))
                _box(page, 1).fill("")
                finish()

            # ---- 4. a busy chat whose turn is not its own thread's -------
            def busy_other_thread():
                reset()
                busy("t1", 1)
                _open(page, until, 1, "t3", "p2")           # pane 1 moves on mid-turn
                c = _chat(page, 1)
                check("setup: pane 1 shows t3 while t1's turn is what it tracks",
                      c["threadId"] == "t3" and c["busy"] and c["turnThreadId"] == "t1")
                s1, s3 = len(_sent(mock, "t1")), len(_sent(mock, "t3"))
                say("not for this thread")
                check("AUTO puts the words in the box: a send here would start a new turn unseen",
                      waits(lambda: "not for this thread" in _box(page, 1).input_value()))
                time.sleep(0.3)
                check("and nothing is sent to t3 or to t1", len(_sent(mock, "t3")) == s3 and len(_sent(mock, "t1")) == s1)
                _box(page, 1).fill("")
                finish("t1", 1)

            # ---- 5. V2's other review conditions still hold on a busy chat
            def v2_review_conditions():
                reset()
                busy()
                s1, s2 = len(_sent(mock, "t1")), len(_sent(mock, "t2"))
                gate = threading.Event()
                say("said in the first chat", gate=gate)
                waits(lambda: not mock.stt_script)
                _focus(page, until, 2)                      # the owner moves on before the words land
                gate.set()
                check("a different chat than at capture: the words go to the box, not into a turn",
                      waits(lambda: "said in the first chat" in _box(page, 2).input_value()))
                check("and nothing is sent anywhere", len(_sent(mock, "t1")) == s1 and len(_sent(mock, "t2")) == s2)
                _box(page, 2).fill("")
                _focus(page, until, 1)
                # Retry never becomes a message, busy or not.
                mock.stt_script = [{"status": 400, "error": "bad audio"}]
                page.evaluate(QUIET)
                page.evaluate(PTT)
                page.wait_for_selector('[data-testid="stt-retry"]', timeout=15000)
                mock.stt_script = [{"text": "after retry"}]
                page.locator('[data-testid="stt-retry-button"]').click()
                check("text produced by Retry goes to the box, never steers",
                      waits(lambda: "after retry" in _box(page, 1).input_value()) and len(_sent(mock, "t1")) == s1)
                _box(page, 1).fill("")
                # The mic OFF records nothing, busy or not.
                _dictation(page, "off")
                st0 = len(stts())
                page.evaluate(QUIET)
                page.evaluate(PTT)
                time.sleep(0.5)
                check("the mic OFF uploads nothing and sends nothing", len(stts()) == st0 and len(_sent(mock, "t1")) == s1)
                _dictation(page, "auto")
                finish()

            # ---- 6. a card: no send, no approval --------------------------
            def card_blocks_voice():
                reset()
                busy()
                s1, st0, a0 = len(_sent(mock, "t1")), len(stts()), len(approvals_posted())
                _approval(mock, "v3card1")
                until(lambda: page.locator('[data-testid="approval-card"]').count() == 1, timeout=4)
                # Ctrl+D, the follow-up window and a wake hit: nothing is claimed under a card.
                mock.stt_script = [{"text": "Jarvis approve"}] * 3
                page.keyboard.down("Control")
                page.keyboard.down("d")
                page.evaluate("window.__hud.mic.feedMs(1500, .06)")
                page.keyboard.up("d")
                page.keyboard.up("Control")
                page.evaluate("window.__hud.capture.openFollowUp(); " + SPEECH % 2000)
                page.evaluate(WAKE)
                time.sleep(0.8)
                check("a card up: no upload, no send, no approval request from any way of talking",
                      len(stts()) == st0 and len(_sent(mock, "t1")) == s1 and len(approvals_posted()) == a0
                      and page.locator('[data-testid="approval-card"]').count() == 1,
                      f"stt+{len(stts()) - st0} sends+{len(_sent(mock, 't1')) - s1} approvals+{len(approvals_posted()) - a0}")
                check("the card is the reason the window gives",
                      any(x["outcome"] == "suppressed:card" for x in rows()), str([x["outcome"] for x in rows()[-4:]]))
                page.keyboard.press("Escape")               # test teardown: the owner denies it by hand
                until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)

                # A card that arrives while the words are being transcribed: the
                # words wait in the box, and "Jarvis approve" answers nothing.
                a0 = len(approvals_posted())
                gate = threading.Event()
                say("Jarvis approve", gate=gate)
                waits(lambda: not mock.stt_script)
                _approval(mock, "v3card2")
                until(lambda: page.locator('[data-testid="approval-card"]').count() == 1, timeout=4)
                gate.set()
                check("a card that arrived in flight: the words wait in the box instead of steering",
                      waits(lambda: "Jarvis approve" in _box(page, 1).input_value()))
                time.sleep(0.4)
                check("and nothing was sent and no approval was requested or answered",
                      len(_sent(mock, "t1")) == s1 and len(approvals_posted()) == a0
                      and page.locator('[data-testid="approval-card"]').count() == 1,
                      f"sends+{len(_sent(mock, 't1')) - s1} approvals+{len(approvals_posted()) - a0}")
                page.keyboard.press("Escape")
                until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
                _box(page, 1).fill("")
                finish()

            # ---- 7. the continuation window -------------------------------
            def continuation_steers():
                reset()
                s0, i0 = len(_sent(mock, "t1")), interrupts("t1")
                mock.stt_script = [{"text": "first part"}]
                page.evaluate(WAKE)
                check("setup: a message said with the wake word goes out and starts a turn",
                      waits(lambda: len(_sent(mock, "t1")) == s0 + 1) and waits(lambda: _chat(page, 1)["busy"]))
                w["send_status"] = "steered"
                mock.stt_script = [{"text": "and the second part"}]
                page.evaluate(SPEECH % 4000)
                got = until(lambda: _sent(mock, "t1")[s0 + 1:] or None, timeout=5)
                check("speech resuming 4 s later needs no wake word and steers the running turn",
                      bool(got) and got[-1].get("text") == "and the second part", str(got))
                check("diagnostics call it a continuation", rows()[-1]["source"] == "continuation", str(rows()[-1]))
                check("and the turn was not interrupted", interrupts("t1") == i0)
                st0, s1 = len(stts()), len(_sent(mock, "t1"))
                mock.stt_script = [{"text": "too late"}]
                page.evaluate(SPEECH % 7000)
                time.sleep(0.6)
                check("speech resuming 7 s after the last message ended is not claimed",
                      len(stts()) == st0 and len(_sent(mock, "t1")) == s1 and rows()[-1]["outcome"] == "unclaimed",
                      str(rows()[-1]))
                w.pop("send_status", None)
                finish()

            def continuation_review_appends():
                reset(mode="review")
                s0 = len(_sent(mock, "t1"))
                mock.stt_script = [{"text": "alpha"}]
                page.evaluate(WAKE)
                check("REVIEW: the first message goes to the box",
                      waits(lambda: _box(page, 1).input_value().strip() == "alpha"))
                mock.stt_script = [{"text": "bravo"}]
                page.evaluate(SPEECH % 3000)
                check("and a continuation is appended to it",
                      waits(lambda: _box(page, 1).input_value().strip() == "alpha bravo"), _box(page, 1).input_value())
                check("nothing was sent", len(_sent(mock, "t1")) == s0)
                _box(page, 1).fill("")

            def continuation_ends_with_the_chat():
                reset()
                mock.stt_script = [{"text": "first part"}]
                page.evaluate(WAKE)
                waits(lambda: _chat(page, 1)["busy"])
                _focus(page, until, 2)
                st0 = len(stts())
                mock.stt_script = [{"text": "wrong chat"}]
                page.evaluate(SPEECH % 2000)
                time.sleep(0.6)
                check("choosing another chat ends the window: the next words are not claimed",
                      len(stts()) == st0 and rows()[-1]["outcome"] == "unclaimed", str(rows()[-1]))
                _focus(page, until, 1)
                finish()

            def ptt_has_no_continuation():
                reset()
                st0 = len(stts())
                say("pushed")
                waits(lambda: len(stts()) > st0)
                st0 = len(stts())
                mock.stt_script = [{"text": "after the key"}]
                page.evaluate(SPEECH % 1000)
                time.sleep(0.6)
                check("letting go of push-to-talk is a full stop: the next words need the key again",
                      len(stts()) == st0, f"{len(stts()) - st0} uploads")
                finish()

            def continuation_cap():
                reset()
                s0 = len(_sent(mock, "t1"))
                mock.stt_script = [{"text": "first part"}]
                page.evaluate(WAKE)
                waits(lambda: _chat(page, 1)["busy"])
                w["send_status"] = "steered"
                # TV-like speech with short gaps: only two continuations are claimed.
                for i in range(5):
                    st0 = len(stts())
                    mock.stt_script = [{"text": f"room speech {i}"}]
                    page.evaluate(SPEECH % 2000)
                    waits(lambda: len(stts()) > st0, timeout=1.5)
                    time.sleep(0.2)
                time.sleep(0.5)
                check("room speech with 2 s gaps after a wake steers at most twice without a wake word",
                      len(_sent(mock, "t1")) - s0 == 3, f"{len(_sent(mock, 't1')) - s0} sends")
                w.pop("send_status", None)
                finish()

            def continuation_needs_a_message_that_went_out():
                reset()
                mock.stt_script = [{"status": 400, "error": "bad audio"}]
                page.evaluate(WAKE)
                page.wait_for_selector('[data-testid="stt-retry"]', timeout=15000)
                st0 = len(stts())
                mock.stt_script = [{"text": "should not be heard"}]
                page.evaluate(SPEECH % 2000)
                time.sleep(0.6)
                check("a message whose upload failed opens no continuation window", len(stts()) == st0)
                reset()
                mock.stt_script = [{"text": "   "}]
                page.evaluate(WAKE)
                waits(lambda: not mock.stt_script)
                time.sleep(0.5)
                st0 = len(stts())
                mock.stt_script = [{"text": "should not be heard"}]
                page.evaluate(SPEECH % 2000)
                time.sleep(0.6)
                check("nor does one whose transcript was empty", len(stts()) == st0)

            # ---- 8. the Ctrl+D gesture, unchanged -------------------------
            def ctrl_d_on_busy():
                reset()
                busy()
                w["send_status"] = "steered"
                s0, i0 = len(_sent(mock, "t1")), interrupts("t1")
                mock.stt_script = [{"text": "by the key"}]
                page.evaluate("document.activeElement && document.activeElement.blur()")
                page.evaluate(QUIET)
                page.keyboard.down("Control")
                page.keyboard.down("d")
                time.sleep(0.1)
                held = page.evaluate("window.__hud.capture.ptt !== null")
                page.evaluate("window.__hud.mic.feedMs(1200, .06)")
                page.keyboard.up("d")
                page.keyboard.up("Control")
                check("Ctrl+D held on a busy chat starts push-to-talk", held)
                got = until(lambda: _sent(mock, "t1")[s0:] or None, timeout=5)
                check("letting go steers the turn and does not interrupt it",
                      bool(got) and got[-1].get("text") == "by the key" and interrupts("t1") == i0, str(got))
                w.pop("send_status", None)
                finish()

            # ---- 9. the follow-up target (Q5) -----------------------------
            def followup_target():
                reset(THREE, threads=(("t1", "p1"), ("t2", "p1"), ("t3", "p2")))
                check("setup: pane 1 is the selected chat, three chat panes drawn", sel() == 1)
                # A drawn pane's reply: the window selects that pane and sends there.
                s1, s2 = len(_sent(mock, "t1")), len(_sent(mock, "t2"))
                page.evaluate("window.__hud.followUpFor(2)")
                mock.stt_script = [{"text": "to the second chat"}]
                page.evaluate(SPEECH % 2000)
                got = until(lambda: _sent(mock, "t2")[s2:] or None, timeout=5)
                check("a reply right after pane 2's reply: selected, and sent there",
                      bool(got) and got[-1].get("text") == "to the second chat" and sel() == 2
                      and len(_sent(mock, "t1")) == s1, f"{got} sel={sel()}")
                check("the mic mark moved with it", page.locator('[data-testid="pane-2-mic"]').count() == 1
                      and page.locator('[data-testid="pane-1-mic"]').count() == 0)
                finish("t2", 2)
                # Otherwise the selected chat (W-6).
                _focus(page, until, 1)
                page.evaluate("window.__hud.capture.closeFollowUp(); window.__hud.capture.openFollowUp()")
                s1 = len(_sent(mock, "t1"))
                mock.stt_script = [{"text": "to the selected chat"}]
                page.evaluate(SPEECH % 2000)
                got = until(lambda: _sent(mock, "t1")[s1:] or None, timeout=5)
                check("a follow-up with no pane behind it goes to the selected chat",
                      bool(got) and got[-1].get("text") == "to the selected chat" and sel() == 1, str(got))
                finish("t1", 1)
                # The owner chose another chat after the reply: their click is newer.
                _focus(page, until, 1)
                page.evaluate("window.__hud.followUpFor(2)")
                _focus(page, until, 3)
                s2, s3 = len(_sent(mock, "t2")), len(_sent(mock, "t3"))
                mock.stt_script = [{"text": "i clicked here"}]
                page.evaluate(SPEECH % 2000)
                got = until(lambda: _sent(mock, "t3")[s3:] or None, timeout=5)
                check("choosing pane 3 after pane 2's reply wins: the words go to pane 3",
                      bool(got) and sel() == 3 and len(_sent(mock, "t2")) == s2, f"{got} sel={sel()}")
                finish("t3", 3)
                # A pane that is not drawn opens no window.
                _focus(page, until, 1)
                _choose(page, until, "single")
                until(lambda: not page.locator(_pane(2)).is_visible(), timeout=2)
                page.evaluate("window.__hud.capture.closeFollowUp(); window.__hud.followUpFor(2)")
                check("a hidden pane opens no follow-up window", page.evaluate("window.__hud.capture.conversing()") is False)
                st0 = len(stts())
                mock.stt_script = [{"text": "nobody asked"}]
                page.evaluate(SPEECH % 2000)
                time.sleep(0.6)
                check("so speech after it is not claimed", len(stts()) == st0)
                # A pane hidden after its reply: the window stays with the selected chat.
                _choose(page, until, "cols3")
                until(lambda: page.locator(_pane(2)).is_visible(), timeout=2)
                _focus(page, until, 1)
                page.evaluate("window.__hud.followUpFor(2)")
                _choose(page, until, "single")
                until(lambda: not page.locator(_pane(2)).is_visible(), timeout=2)
                s1 = len(_sent(mock, "t1"))
                mock.stt_script = [{"text": "pane two went away"}]
                page.evaluate(SPEECH % 2000)
                got = until(lambda: _sent(mock, "t1")[s1:] or None, timeout=2)
                check("a pane that went away before the claim: the words wait in the selected chat's box, unsent",
                      waits(lambda: "pane two went away" in _box(page, 1).input_value()) and not got and sel() == 1,
                      f"{got} sel={sel()}")
                _box(page, 1).fill("")

            for section in (busy_steer, stop_interrupts, review_busy_box, busy_other_thread, v2_review_conditions,
                            card_blocks_voice, continuation_steers, continuation_review_appends,
                            continuation_ends_with_the_chat, continuation_cap, continuation_needs_a_message_that_went_out,
                            ptt_has_no_continuation, ctrl_d_on_busy,
                            followup_target):
                run(section)
            context.close()
            browser.close()
        print(f"\n{count - len(failures)}/{count} checks passed; {len(failures)} failures")
        if failures:
            print("\n".join(failures))
        return 1 if failures else 0
    finally:
        mock.stop()


if __name__ == "__main__":
    raise SystemExit(main())
