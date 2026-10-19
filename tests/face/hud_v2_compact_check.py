"""WP-C of docs/plans/2026-10-19-compact-button-plan.md: the HUD's Compact chip,
with the owner's answer CB-2 (confirm first, inline, not window.confirm).

Free and hermetic: its own `MockDaemon(0)` (an ephemeral port, never
8402/8403/8405; `guard` refuses the live ports). The mock stands in for WP-A's
daemon: `POST /threads/{id}/compact` is owner-only, answers 202
`{"status":"started","turn_id"}` or 409 `{"error": sentence}`, publishes
`compact_started` + `turn_started`, and `mock.compact_finish` ends the
compaction with `turn_finished`, `compacted` `{ok, text}` and the transcript's
`system` row (see `compact_route` / `compact_finish` in hud_v2_mock.py).

What it pins. On today's code every check but the "absent" ones fails because
the chip is missing; each names the mutation it is meant to catch:
  1. the chip: `[data-testid="compact"]` in the lower row, `data-state`
     ready, the label "Compact", the aria-label "Compact the conversation",
     the provider's title (Claude / Codex / Jarvis); absent from a compose pane
     [mutation: shown for compose/tasks];
  2. a click only asks: `compact-confirm` shows, nothing is posted; Cancel,
     Escape (focus on the chip, in the box) each close it with nothing posted
     [mutation: no confirm step; confirm window.confirm];
  3. the confirm click posts `/threads/t1/compact` exactly once with an empty
     JSON body, and a double click on it still once [mutation: double post];
     the chip reads running ("Compacting…", disabled), and the box's words and
     the thread's sends are untouched;
  4. the end of the compaction: "Conversation compacted" appears once in the
     log, once more after a reload (the transcript row, no duplicate from the
     live event), and the chip is ready again; a failed one says why;
  5. the daemon's 409 sentence shows in `pane-status`;
  6. disabled while a turn runs (title "A turn is running; compact when it
     ends.", a script's click is a no-op) [mutation: enabled while running];
     a turn starting under an open confirm closes it;
  7. disabled under an approval card ("Answer the card first."), a script's
     click behind the veil is a no-op, a card arriving while the confirm is
     open dismisses it and nothing is posted, and it does not come back when
     the card is answered [mutation: enabled under card];
  8. focus: the chip never takes the composer's focus when it changes state or
     when a page loads;
  9. a reload while the thread is `compacting: true` (no `running` flag even)
     shows the chip running.

THE INTERFACE (also in hud/src/lib/compact.test.ts and
hud/src/components/InputBarCompact.test.ts, which hold the exact words)
=========================================================================
  Chip         `[data-testid="compact"]`, `data-state="ready|running|off"`.
  Confirm      `[data-testid="compact-confirm"]` with `compact-confirm-yes`
               and `compact-confirm-no`, inside the pane's input bar.
  Live events  `compact_started` (thread_id) makes the thread compacting;
               `turn_finished` / `compacted` end it. `compacted`'s
               `data.text` is a system line in the log when it arrives.
  409          the sentence is the pane's status line (`pane-status`; the
               sidebar is folded so the single selected chat draws one).
  Chosen       the chip's `title` for the disabled states is the plan's
               sentences; the thread record's `compacting` flag is what a
               reload reads.

Run alone (after `cd hud && npm run build`):
  systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 -- env PYTHONPATH=$PWD \\
    PYTHONDONTWRITEBYTECODE=1 /home/johnw/projects/Jarvis/.venv/bin/python tests/face/hud_v2_compact_check.py [section,...]
It also runs as a section of tests/face/hud_v2_check.py.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.face.hud_v2_declutter_check import _fold_left  # noqa: E402
from tests.face.hud_v2_layout_check import _pane  # noqa: E402
from tests.face.hud_v2_multichat_check import _box, _chat, _expand, _fresh  # noqa: E402

TWO = json.dumps({"preset": "cols2", "focused": 1,
                  "panes": [{"view": "chat"}, {"view": "chat"}, {"view": "file"}, {"view": "task"}]})
SINGLE = json.dumps({"preset": "single", "focused": 1,
                     "panes": [{"view": "chat"}, {"view": "file"}, {"view": "browser"}, {"view": "task"}]})

JARVIS, CLAUDE, CODEX = "t1", "tc", "tx"
CLAUDE_TITLE = ("Run Claude Code's /compact: it summarises the conversation so the model's context shrinks. "
                "Every message stays here.")
RUNNING_WORDS = "A turn is running; compact when it ends."
CARD_WORDS = "Answer the card first."


def seed(mock):
    w = mock.world
    base = next(t for t in w["threads"] if t["id"] == JARVIS)
    for tid, provider, title in ((CLAUDE, "claude", "Claude chat"), (CODEX, "codex", "Codex chat")):
        if not any(t["id"] == tid for t in w["threads"]):
            w["threads"].append({**base, "id": tid, "provider": provider, "title": title, "model": None})
        w["transcripts"][tid] = [{"role": "user", "text": f"hello {title}", "at": "2026-10-19T08:00:00+00:00"},
                                 {"role": "assistant", "text": "Hi.", "at": "2026-10-19T08:00:01+00:00"}]


# -- helpers -------------------------------------------------------------------

def _chip(page, n=1):
    return page.locator(_pane(n, '[data-testid="compact"]'))


def _ask(page, n=1):
    return page.locator(_pane(n, '[data-testid="compact-confirm"]'))


def _posts(mock, tid=JARVIS):
    return mock.sent("POST", f"/threads/{tid}/compact")


def _sends(mock, tid=JARVIS):
    return mock.sent("POST", f"/threads/{tid}/send")


def _open(page, until, n, tid):
    _box(page, n).click()
    _expand(page, until, "p1", f"thread-{tid}")
    page.locator(f'[data-testid="thread-{tid}"]').click()
    until(lambda: _chat(page, n)["threadId"] == tid, timeout=3)
    until(lambda: page.locator(_pane(n, '[data-testid="log"]')).inner_text() != "", timeout=3)


def _state(page, n=1):
    loc = _chip(page, n)
    return loc.get_attribute("data-state") if loc.count() else None


def _reset(mock, until, page=None):
    """Idle again: no compaction, no refusal scripted, no card, no running turn."""
    c = mock.compact_world()
    c["refuse"] = None
    for t in mock.world["threads"]:
        for key in ("compacting", "running", "running_since"):
            t.pop(key, None)
    mock.world["approvals"] = []
    for tid, rows in mock.world["transcripts"].items():
        mock.world["transcripts"][tid] = [r for r in rows if not r.get("compaction")]


def _open_one(page, mock, until, tid=JARVIS, ws=SINGLE):
    _reset(mock, until)
    _fresh(page, mock, until, ws)
    _open(page, until, 1, tid)


def _card(mock, req="compact-card"):
    mock.emit("approval_requested", {
        "req_id": req, "code": "CP01", "tool": "run_command", "args": {"command": "ls"}, "command": "ls",
        "reason": "reviewer declined", "layer": "human", "origin": "task: compact", "allowlistable": True,
        "timeout_s": 120})


def _active(page) -> str:
    return page.evaluate("() => document.activeElement && (document.activeElement.getAttribute('data-testid') || document.activeElement.tagName)")


# -- sections ------------------------------------------------------------------

def compact_checks(browser, mock, base, check, until, guard, init_script):
    print("\nthe Compact chip (WP-C)")
    seed(mock)
    ctx = browser.new_context(viewport={"width": 1600, "height": 900}, permissions=["microphone"])
    guard(ctx)
    ctx.add_init_script(init_script)
    page = ctx.new_page()
    errors: list[str] = []
    dialogs: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))   # a window.confirm is a failure
    page.set_default_timeout(5000)
    only = set(filter(None, (sys.argv[1] if len(sys.argv) > 1 and __name__ == "__main__" else "").split(",")))
    try:
        before = mock.sse_connections()
        page.goto(base + "/")
        page.wait_for_selector('[data-testid="sidebar"]', state="attached")
        mock.await_reconnect(before)
        for section in (_chip_checks, _confirm_checks, _post_checks, _finish_checks, _refusal_checks,
                        _running_checks, _card_checks, _focus_checks, _reload_checks):
            if only and section.__name__.strip("_") not in only:
                continue
            try:
                section(page, mock, check, until)
            except Exception as e:  # a section that cannot run is a failure, and the rest still run
                check(f"compact {section.__name__.strip('_')} ran to the end", False, str(e).splitlines()[0][:200])
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
        check("no window.confirm was raised anywhere in the compact section", not dialogs, str(dialogs))
        check("no page errors in the compact section", not errors, "; ".join(errors[:3]))
    finally:
        ctx.close()


def _chip_checks(page, mock, check, until):
    _reset(mock, until)
    _fresh(page, mock, until, TWO)
    check("a compose pane has no Compact chip [mutation: shown for compose]", _chip(page, 1).count() == 0
          and _chip(page, 2).count() == 0)
    _open(page, until, 1, JARVIS)
    chip = _chip(page, 1)
    until(lambda: chip.count() == 1, timeout=3)
    check("an idle Jarvis chat shows the chip, ready", chip.count() == 1 and _state(page) == "ready")
    if chip.count():
        text = chip.inner_text().strip()
        check("the chip says Compact and is labelled for screen readers",
              text == "Compact" and chip.get_attribute("aria-label") == "Compact the conversation", text)
        check("it is an enabled chip button in the lower row, after the model chip",
              chip.is_enabled() and chip.evaluate("e => e.tagName === 'BUTTON' && e.classList.contains('chip') "
                                                  "&& e.classList.contains('compactbtn') && !!e.closest('.row')"))
        check("a Jarvis chat's title says it summarises older messages",
              (chip.get_attribute("title") or "").startswith("Summarise older messages (your first request is kept)"),
              chip.get_attribute("title") or "")
    check("and no confirm is showing before anyone clicked", _ask(page).count() == 0)
    _open(page, until, 2, CLAUDE)
    check("a Claude chat's title is Claude Code's /compact", _chip(page, 2).get_attribute("title") == CLAUDE_TITLE
          if _chip(page, 2).count() else False)
    _open(page, until, 2, CODEX)
    check("a Codex chat's title is Codex's own compaction",
          (_chip(page, 2).get_attribute("title") or "").startswith("Run Codex's own compaction")
          if _chip(page, 2).count() else False)
    check("and nothing was posted by looking", not _posts(mock) and not _posts(mock, CLAUDE) and not _posts(mock, CODEX))


def _confirm_checks(page, mock, check, until):
    _open_one(page, mock, until)
    chip, ask = _chip(page), _ask(page)
    until(lambda: chip.count() == 1, timeout=3)
    posts, sends = len(_posts(mock)), len(_sends(mock))
    box = _box(page, 1)
    box.fill("words I was typing")

    chip.click()
    until(lambda: ask.count() == 1, timeout=2)
    check("a click only asks: the confirm shows 'Compact this conversation?' with Compact / Cancel "
          "[mutation: no confirm step]",
          ask.count() == 1 and "Compact this conversation?" in ask.inner_text()
          and page.locator(_pane(1, '[data-testid="compact-confirm-yes"]')).inner_text().strip() == "Compact"
          and page.locator(_pane(1, '[data-testid="compact-confirm-no"]')).inner_text().strip() == "Cancel",
          ask.inner_text() if ask.count() else "no confirm")
    time.sleep(0.3)
    check("and the click alone posted nothing", len(_posts(mock)) == posts)

    page.locator(_pane(1, '[data-testid="compact-confirm-no"]')).click()
    until(lambda: ask.count() == 0, timeout=2)
    check("Cancel closes it and posts nothing", ask.count() == 0 and len(_posts(mock)) == posts)

    chip.click()
    until(lambda: ask.count() == 1, timeout=2)
    page.keyboard.press("Escape")
    until(lambda: ask.count() == 0, timeout=2)
    check("Escape (focus on the chip's confirm) closes it and posts nothing",
          ask.count() == 0 and len(_posts(mock)) == posts)

    chip.click()
    until(lambda: ask.count() == 1, timeout=2)
    box.click()
    page.keyboard.press("Escape")
    until(lambda: ask.count() == 0, timeout=2)
    check("Escape typed in the box closes it too, and posts nothing",
          ask.count() == 0 and len(_posts(mock)) == posts)

    time.sleep(0.3)
    check("none of it sent a message or ate the box's words",
          len(_sends(mock)) == sends and box.input_value() == "words I was typing", box.input_value())
    box.fill("")


def _post_checks(page, mock, check, until):
    _open_one(page, mock, until)
    chip, ask = _chip(page), _ask(page)
    until(lambda: chip.count() == 1, timeout=3)
    posts, sends = len(_posts(mock)), len(_sends(mock))
    box = _box(page, 1)
    box.fill("a draft that must survive")
    chip.click()
    until(lambda: ask.count() == 1, timeout=2)
    page.locator(_pane(1, '[data-testid="compact-confirm-yes"]')).click()
    until(lambda: len(_posts(mock)) > posts, timeout=3)
    time.sleep(0.5)
    check("the confirm click posts the compact route exactly once, with an empty JSON body",
          len(_posts(mock)) == posts + 1 and _posts(mock)[-1] == {}, str(_posts(mock)[posts:]))
    until(lambda: _state(page) == "running", timeout=3)
    check("the chip reads running: Compacting…, disabled",
          _state(page) == "running" and chip.inner_text().strip() == "Compacting…" and chip.is_disabled(),
          f"{_state(page)} {chip.inner_text() if chip.count() else ''}")
    check("the confirm is gone, and nothing was sent or lost from the box",
          ask.count() == 0 and len(_sends(mock)) == sends and box.input_value() == "a draft that must survive")
    chip.evaluate("e => e.click()")
    time.sleep(0.3)
    check("a click on the running chip is nothing", ask.count() == 0 and len(_posts(mock)) == posts + 1)
    mock.compact_finish(JARVIS)
    until(lambda: _state(page) == "ready", timeout=3)
    box.fill("")

    # A double click on the confirm button is one post.
    posts = len(_posts(mock))
    chip.click()
    until(lambda: ask.count() == 1, timeout=2)
    page.locator(_pane(1, '[data-testid="compact-confirm-yes"]')).dblclick()
    time.sleep(0.6)
    check("a double click on the confirm is still one post [mutation: double post]", len(_posts(mock)) == posts + 1,
          str(len(_posts(mock)) - posts))
    mock.compact_finish(JARVIS)
    until(lambda: _state(page) == "ready", timeout=3)


def _finish_checks(page, mock, check, until):
    _open_one(page, mock, until)
    chip, ask = _chip(page), _ask(page)
    log = page.locator(_pane(1, '[data-testid="log"]'))
    until(lambda: chip.count() == 1, timeout=3)
    chip.click()
    until(lambda: ask.count() == 1, timeout=2)
    page.locator(_pane(1, '[data-testid="compact-confirm-yes"]')).click()
    until(lambda: _state(page) == "running", timeout=3)
    mock.compact_finish(JARVIS)
    until(lambda: "Conversation compacted" in log.inner_text(), timeout=3)
    check("when it ends, 'Conversation compacted' is a line in the log, once",
          log.inner_text().count("Conversation compacted") == 1, log.inner_text()[-200:])
    until(lambda: _state(page) == "ready", timeout=3)
    check("and the chip is ready (enabled) again", _state(page) == "ready" and chip.is_enabled())
    _fresh(page, mock, until, SINGLE)
    _open(page, until, 1, JARVIS)
    until(lambda: "Conversation compacted" in log.inner_text(), timeout=3)
    check("after a reload the transcript's row shows it once, and the chip is ready",
          log.inner_text().count("Conversation compacted") == 1 and _state(page) == "ready")

    # A failed one says why.
    chip.click()
    until(lambda: ask.count() == 1, timeout=2)
    page.locator(_pane(1, '[data-testid="compact-confirm-yes"]')).click()
    until(lambda: _state(page) == "running", timeout=3)
    mock.compact_finish(JARVIS, ok=False, stop="error")
    until(lambda: "Compaction failed" in log.inner_text(), timeout=3)
    freed = until(lambda: _state(page) == "ready", timeout=2)
    check("a failed compaction shows its reason in the log and frees the chip",
          "Compaction failed: the summary call timed out" in log.inner_text() and bool(freed))


def _refusal_checks(page, mock, check, until):
    _open_one(page, mock, until)
    _fold_left(page, until)         # the one selected chat then draws its own status line
    chip, ask = _chip(page), _ask(page)
    until(lambda: chip.count() == 1, timeout=3)
    mock.compact_world()["refuse"] = "Messages are waiting to run first."
    posts = len(_posts(mock))
    chip.click()
    until(lambda: ask.count() == 1, timeout=2)
    page.locator(_pane(1, '[data-testid="compact-confirm-yes"]')).click()
    until(lambda: len(_posts(mock)) > posts, timeout=3)
    line = page.locator(_pane(1, '[data-testid="pane-status"]'))
    until(lambda: line.count() == 1 and "waiting" in line.inner_text().lower(), timeout=3)
    # (the status line is drawn in capitals; the words are the daemon's)
    check("a 409 shows the daemon's sentence in pane-status",
          line.count() == 1 and "messages are waiting to run first." in line.inner_text().lower(),
          line.inner_text() if line.count() else "no pane-status")
    check("and the chip is not left running", _state(page) == "ready", str(_state(page)))
    mock.compact_world()["refuse"] = None


def _running_checks(page, mock, check, until):
    _open_one(page, mock, until)
    chip, ask = _chip(page), _ask(page)
    until(lambda: chip.count() == 1, timeout=3)
    posts = len(_posts(mock))
    mock.emit("turn_started", {}, thread_id=JARVIS, project_id="p1", turn_id="turn-run")
    until(lambda: _state(page) == "off", timeout=3)
    check("while a turn runs the chip is off and disabled, and says to wait [mutation: enabled while running]",
          _state(page) == "off" and chip.is_disabled() and chip.get_attribute("title") == RUNNING_WORDS,
          f"{_state(page)} {chip.get_attribute('title') if chip.count() else ''}")
    chip.evaluate("e => e.click()")
    time.sleep(0.3)
    check("a click on it is a no-op: no confirm, nothing posted", ask.count() == 0 and len(_posts(mock)) == posts)
    mock.emit("turn_finished", {"stop": "end"}, thread_id=JARVIS, project_id="p1", turn_id="turn-run")
    until(lambda: _state(page) == "ready", timeout=3)
    check("when the turn ends it is ready again", _state(page) == "ready" and chip.is_enabled())

    # A turn that starts (from Discord, say) under an open confirm closes it.
    chip.click()
    until(lambda: ask.count() == 1, timeout=2)
    mock.emit("turn_started", {}, thread_id=JARVIS, project_id="p1", turn_id="turn-run2")
    until(lambda: ask.count() == 0, timeout=3)
    check("a turn starting while the confirm is open closes it, and nothing is posted",
          ask.count() == 0 and len(_posts(mock)) == posts)
    mock.emit("turn_finished", {"stop": "end"}, thread_id=JARVIS, project_id="p1", turn_id="turn-run2")
    until(lambda: _state(page) == "ready", timeout=3)
    time.sleep(0.2)
    check("and it does not come back when the turn ends", ask.count() == 0 and len(_posts(mock)) == posts)


def _card_checks(page, mock, check, until):
    _open_one(page, mock, until)
    chip, ask = _chip(page), _ask(page)
    card = page.locator('[data-testid="approval-card"]')
    until(lambda: chip.count() == 1, timeout=3)
    posts = len(_posts(mock))
    _card(mock, "compact-card-1")
    until(lambda: card.count() == 1, timeout=3)
    until(lambda: _state(page) == "off", timeout=3)
    check("under an approval card the chip is off and disabled, and says to answer the card "
          "[mutation: enabled under card]",
          _state(page) == "off" and chip.is_disabled() and chip.get_attribute("title") == CARD_WORDS,
          f"{_state(page)} {chip.get_attribute('title') if chip.count() else ''}")
    chip.evaluate("e => e.click()")             # a script's click behind the veil
    time.sleep(0.3)
    check("a script's click behind the card opens nothing and posts nothing",
          ask.count() == 0 and len(_posts(mock)) == posts)
    page.locator('[data-testid="approval-deny"]').click()
    until(lambda: card.count() == 0, timeout=3)
    until(lambda: _state(page) == "ready", timeout=3)

    # A card arriving while the confirm is open dismisses it.
    chip.click()
    until(lambda: ask.count() == 1, timeout=2)
    _card(mock, "compact-card-2")
    until(lambda: card.count() == 1, timeout=3)
    until(lambda: ask.count() == 0, timeout=3)
    check("a card arriving while the confirm is open dismisses it, and nothing is posted",
          ask.count() == 0 and len(_posts(mock)) == posts)
    page.locator('[data-testid="approval-deny"]').click()
    until(lambda: card.count() == 0, timeout=3)
    until(lambda: _state(page) == "ready", timeout=3)
    time.sleep(0.3)
    check("once the card is answered the chip is ready, the confirm has not come back, and nothing was posted",
          _state(page) == "ready" and ask.count() == 0 and len(_posts(mock)) == posts)


def _focus_checks(page, mock, check, until):
    _open_one(page, mock, until)
    chip = _chip(page)
    until(lambda: chip.count() == 1, timeout=3)
    box = _box(page, 1)
    box.click()
    box.fill("typing while things change")
    check("setup: the composer is focused", _active(page) == "input", _active(page))
    mock.emit("turn_started", {}, thread_id=JARVIS, project_id="p1", turn_id="turn-f")
    until(lambda: _state(page) == "off", timeout=3)
    mock.emit("turn_finished", {"stop": "end"}, thread_id=JARVIS, project_id="p1", turn_id="turn-f")
    until(lambda: _state(page) == "ready", timeout=3)
    mock.emit("compact_started", {}, thread_id=JARVIS, project_id="p1", turn_id="turn-c", compaction=True)
    until(lambda: _state(page) == "running", timeout=3)
    check("the chip going off and running does not take the composer's focus",
          _active(page) == "input" and box.input_value() == "typing while things change", _active(page))
    mock.emit("turn_finished", {"stop": "end", "compacted": True}, thread_id=JARVIS, project_id="p1", turn_id="turn-c",
              compaction=True)
    mock.emit("compacted", {"ok": True, "text": "Conversation compacted"}, thread_id=JARVIS, project_id="p1",
              turn_id="turn-c", compaction=True)
    until(lambda: _state(page) == "ready", timeout=3)
    time.sleep(0.2)
    check("nor does it when the compaction ends", _active(page) == "input", _active(page))
    box.fill("")
    mock.world["transcripts"][JARVIS] = [r for r in mock.world["transcripts"][JARVIS] if not r.get("compaction")]
    # A page that loads with the chip in it does not put focus on it.
    _fresh(page, mock, until, SINGLE)
    _open(page, until, 1, JARVIS)
    time.sleep(0.3)
    check("a fresh page with the chip drawn does not focus it", chip.count() == 1 and _active(page) != "compact",
          _active(page))


def _reload_checks(page, mock, check, until):
    _reset(mock, until)
    t = next(t for t in mock.world["threads"] if t["id"] == JARVIS)
    t["compacting"] = True          # the daemon's live flag; no `running` on purpose
    _fresh(page, mock, until, SINGLE)
    _open(page, until, 1, JARVIS)
    chip = _chip(page)
    until(lambda: _state(page) == "running", timeout=3)
    check("a reload while the thread is compacting shows the chip running",
          _state(page) == "running" and chip.is_disabled() and chip.inner_text().strip() == "Compacting…",
          f"{_state(page)}")
    _reset(mock, until)
    mock.emit("compacted", {"ok": True, "text": "Conversation compacted"}, thread_id=JARVIS, project_id="p1",
              turn_id="turn-r", compaction=True)
    mock.emit("turn_finished", {"stop": "end", "compacted": True}, thread_id=JARVIS, project_id="p1",
              turn_id="turn-r", compaction=True)
    until(lambda: _state(page) == "ready", timeout=3)
    check("and the end of that compaction frees it", _state(page) == "ready" and chip.is_enabled())


# ---------------------------------------------------------------------------

def main():
    from playwright.sync_api import sync_playwright

    from tests.face import hud_v2_check as H
    from tests.face.hud_v2_mock import DIST, MockDaemon

    if not (DIST / "index.html").exists():
        print("hud/dist is not built. Run: cd hud && npm ci --ignore-scripts && npm run build")
        return 2
    mock = MockDaemon(0).start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--use-fake-ui-for-media-stream",
                                                              "--use-fake-device-for-media-stream"])
            compact_checks(browser, mock, f"http://127.0.0.1:{mock.port}", H.check, H.until, H.guard,
                           H.FAKE_RECOGNIZER)
            browser.close()
    finally:
        mock.stop()
    print()
    if H.FAILURES:
        print(f"{len(H.FAILURES)} of {H.CHECKS} checks FAILED:")
        for f in H.FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {H.CHECKS} compact checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
