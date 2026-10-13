"""TC-2 browser regressions. Private MockDaemon, synthetic events, live-port/CSP guards."""
from __future__ import annotations
import sys
import json
import time
from pathlib import Path
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.face.hud_v2_multichat_check import TWO, _seed, _fresh, _open, _box, _chat, _expand
from tests.face.hud_v2_layout_check import _pane, TALK_PROBE


def toolcalls_checks(browser, mock, base, check, until, guard, init_script):
    print("\ntool calls in the transcript (TC-2)")
    _seed(mock)
    ctx = browser.new_context(viewport={"width": 1600, "height": 900}, permissions=["microphone"])
    guard(ctx)
    ctx.add_init_script(init_script)
    ctx.add_init_script(TALK_PROBE)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_default_timeout(5000)
    groups = lambda n=1: page.locator(_pane(n, '[data-testid="tool-group"]'))
    calls = lambda n=1: page.locator(_pane(n, '[data-testid="tool-call"]'))
    log = lambda n=1: page.locator(_pane(n, '[data-testid="log"]'))
    serial = 0
    def emit(kind, data=None, turn="turn", tid="t1"):
        nonlocal serial
        serial += 1
        at = f"2026-10-13T00:{serial // 60:02d}:{serial % 60:02d}+00:00"
        d = data or {}
        rows = mock.world["transcripts"].setdefault(tid, [])
        if kind == "tool_started":
            rows.append(dict(role="tool", call_id=d["call_id"], turn_id=turn, name=d["name"], label=d.get("label", ""), outcome="running", at=at))
        elif kind == "tool_finished":
            for row in rows:
                if row.get("call_id") == d["call_id"] and row.get("turn_id") == turn:
                    row.update(outcome=d.get("outcome", "ok" if d.get("ok", True) else "failed"), finished_at=at)
        elif kind == "turn_finished":
            for row in rows:
                if row.get("turn_id") == turn and row.get("outcome") == "running": row.update(outcome="stopped", finished_at=at)
        elif kind == "text": rows.append(dict(role="assistant", text=d["text"], at=at, turn_id=turn))
        elif kind == "user_message": rows.append(dict(role="user", text=d["text"], at=at, turn_id=turn, message_id=d.get("message_id"), via=d.get("via"), **({"mark": "steering"} if d.get("steer") else {})))
        mock.emit(kind, d, thread_id=tid, turn_id=turn, at=at)
    try:
        page.goto(base + "/")
        page.wait_for_selector('[data-testid="sidebar"]', state="attached")
        _fresh(page, mock, until, TWO)
        _open(page, until, 1, "t1"); _open(page, until, 2, "t2")
        emit("turn_started")
        emit("text_delta", {"text": "interim words never settled"})
        for i in range(3): emit("tool_started", {"call_id": f"c{i}", "name": "Read", "label": f"src/{i}.ts", "summary": "RESULT MUST NOT SHOW"})
        until(lambda: groups().count() == 1 and "Using tools… 3" in groups().inner_text())
        check("three calls form one collapsed running group, without the old ticker", groups().count() == 1 and groups().get_attribute("aria-expanded") == "false" and page.locator('[data-testid="ops"]').count() == 0)
        check("tool start discards interim draft, and stays in its own pane", page.locator('[data-testid="draft"]').count() == 0 and groups(2).count() == 0)
        _box(page, 1).click(); groups().click()
        check("click opens calls and leaves keyboard focus in the box", calls().count() == 3 and _box(page, 1).evaluate("e => e === document.activeElement"))
        check("call rows show label/status/duration but no result text", "src/0.ts" in calls().first.inner_text() and calls().first.get_attribute("data-status") == "running" and "RESULT MUST NOT SHOW" not in log().inner_text())
        groups().focus(); groups().press("Enter")
        check("Enter closes the group", groups().get_attribute("aria-expanded") == "false")
        groups().press("Space")
        check("Space opens the group", groups().get_attribute("aria-expanded") == "true")
        page.keyboard.down("Control"); page.keyboard.down("d")
        time.sleep(.12)
        check("Ctrl+D is captured for talk and never toggles the focused group", groups().get_attribute("aria-expanded") == "true" and page.evaluate("window.__talkKeys.some(e => e.code === 'KeyD' && e.defaultPrevented) && window.__talkPast === 0"))
        page.keyboard.up("d"); page.keyboard.up("Control")
        emit("tool_finished", {"call_id": "c0", "ok": False, "outcome": "failed", "summary": "SECRET"})
        emit("tool_finished", {"call_id": "c1", "ok": False, "outcome": "denied"})
        until(lambda: "1 failed" in groups().inner_text() and "1 denied" in groups().inner_text())
        check("outcomes use words and failed has its red token", groups().locator('.tc-failed').count() == 1 and "1 denied" in groups().inner_text() and "SECRET" not in log().inner_text())
        emit("text", {"text": "A settled update"})
        emit("tool_started", {"call_id": "c3", "name": "Bash", "label": "pytest"})
        until(lambda: groups().count() == 2)
        check("settled text ends the prior group", groups().first.inner_text().startswith("▾ Used 3 tools") and "Using tools… 1" in groups().last.inner_text())
        emit("user_message", {"text": "Steer here", "via": "discord", "steer": True, "message_id": "steer1"})
        emit("tool_started", {"call_id": "c4", "name": "compact", "label": "compact"})
        until(lambda: groups().count() == 3)
        emit("turn_finished", {"stop": "interrupted", "next": 1})
        emit("turn_started", turn="queued")
        emit("tool_started", {"call_id": "c0", "name": "Read", "label": "queued.ts"}, turn="queued")
        until(lambda: groups().count() == 4)
        check("steers and queued turns split groups, and next still stops the old calls", "1 stopped" in groups().nth(2).inner_text() and "Using tools… 1" in groups().last.inner_text())
        emit("turn_finished", {"stop": "end"}, turn="queued")
        until(lambda: "Used 1 tool" in groups().last.inner_text())
        groups().first.click()
        before = log().inner_text()
        _fresh(page, mock, until, TWO); _open(page, until, 1, "t1"); _open(page, until, 2, "t2")
        check("reload folds the same transcript, with every group closed", log().inner_text() == before and all(x == "false" for x in groups().evaluate_all("es => es.map(e => e.getAttribute('aria-expanded'))")))
        check("the disclosure test hook exposes keys only", page.evaluate("Array.isArray(window.__hud.toolGroups()) && window.__hud.toolGroups().length === 0"))

        # A held snapshot overlaps live calls, a steer, completion and stop.
        _fresh(page, mock, until, TWO)
        held = []
        page.route("**/threads/t1/transcript*", lambda route: held.append(route))
        try:
            _expand(page, until, "p1", "thread-t1")
            page.locator('[data-testid="thread-t1"]').click()
            until(lambda: bool(held))
            emit("turn_started", turn="load")
            emit("tool_started", {"call_id": "load-a", "name": "Read", "label": "a.ts"}, turn="load")
            emit("user_message", {"text": "load steer", "via": "discord", "steer": True, "message_id": "load-message"}, turn="load")
            snapshot = [dict(row) for row in mock.world["transcripts"]["t1"]]
            emit("tool_started", {"call_id": "load-b", "name": "Bash", "label": "pytest"}, turn="load")
            emit("tool_finished", {"call_id": "load-a", "outcome": "ok"}, turn="load")
            emit("turn_finished", {"stop": "interrupted"}, turn="load")
            until(lambda: not _chat(page, 1)["busy"])
            held[0].fulfill(status=200, content_type="application/json", body=json.dumps({"messages": snapshot}))
            until(lambda: _chat(page, 1)["loadedThread"] == "t1")
        finally:
            page.unroute("**/threads/t1/transcript*")
        load_calls = [c for row in _chat(page, 1)["messages"] for c in row.get("calls", []) if c["turn_id"] == "load"]
        check("overlapping history counts each call once and never revives a stopped turn",
              [(c["call_id"], c["outcome"]) for c in load_calls] == [("load-a", "ok"), ("load-b", "stopped")]
              and not any(not o.get("finished") for o in _chat(page, 1)["ops"]))
        check("loading replay preserves the steer between its tool groups", log().inner_text().count("load steer") == 1)

        # Real send() acknowledgments bind an initial owner message by turn,
        # while identical old text and same-turn steers retain distinct rows.
        for mode in ("ack-first", "history-first", "snapshot-before-send", "failed-send"):
            old = dict(role="user", text="repeat this", at="old", turn_id="old")
            initial = dict(role="user", text="repeat this", at="new", turn_id="ack-turn")
            steer = dict(role="user", text="repeat this", at="steer", turn_id="ack-turn", message_id="steer-id")
            mock.world["transcripts"]["t1"] = [old]
            _fresh(page, mock, until, TWO)
            histories, sends = [], []
            page.route("**/threads/t1/transcript*", lambda route: histories.append(route))
            page.route("**/threads/t1/send", lambda route: sends.append(route))
            try:
                _expand(page, until, "p1", "thread-t1")
                page.locator('[data-testid="thread-t1"]').click()
                until(lambda: bool(histories))
                _box(page, 1).fill("repeat this"); _box(page, 1).press("Enter")
                until(lambda: bool(sends))
                snapshot = [old] if mode in ("snapshot-before-send", "failed-send") else [old, initial, steer]
                def ack():
                    sends[0].fulfill(status=409 if mode == "failed-send" else 202, content_type="application/json",
                                    body=json.dumps({"error": "synthetic failure"} if mode == "failed-send" else {"status": "started", "turn_id": "ack-turn"}))
                    until(lambda: (not any(m.get("local") for m in _chat(page, 1)["messages"])) if mode == "failed-send"
                          else any(m.get("local") and m.get("turn_id") == "ack-turn" for m in _chat(page, 1)["messages"]))
                if mode != "history-first": ack()
                histories[0].fulfill(status=200, content_type="application/json", body=json.dumps({"messages": snapshot}))
                until(lambda: _chat(page, 1)["loadedThread"] == "t1")
                if mode == "history-first": ack()
                entries = _chat(page, 1)["messages"]
                expected = 1 if mode == "failed-send" else 2 if mode == "snapshot-before-send" else 3
                check(f"initial send replay ({mode}) preserves identity and ordering", len(entries) == expected
                      and entries[0].get("turn_id") == "old"
                      and (mode == "failed-send" or entries[1].get("turn_id") == "ack-turn")
                      and (expected != 3 or entries[2].get("message_id") == "steer-id"))
            finally:
                page.unroute("**/threads/t1/transcript*"); page.unroute("**/threads/t1/send")

        # An older daemon rejects tools=1; live calls still group after legacy history lands.
        mock.world["transcript_tools_unsupported"] = True
        mock.world["transcripts"]["t1"] = [{"role": "user", "text": "legacy history", "at": "legacy"}]
        try:
            _fresh(page, mock, until, TWO); _open(page, until, 1, "t1")
            check("400 fallback retains the older daemon's text history", "legacy history" in log().inner_text())
            emit("turn_started", turn="legacy")
            emit("tool_started", {"call_id": "legacy-call", "name": "Read", "label": "legacy.ts"}, turn="legacy")
            until(lambda: groups().count() == 1)
            check("live tool groups still work with an older daemon", "Using tools… 1" in groups().inner_text())
            emit("turn_finished", {"stop": "end"}, turn="legacy")
        finally:
            mock.world.pop("transcript_tools_unsupported", None)

        # Scrolling and scale: two panes, 2000 calls/200 groups each, generated history only.
        large = []
        for g in range(200):
            large.append(dict(role="assistant", text=f"reply {g} " + "word " * 20, at=f"reply-{g}"))
            for i in range(10): large.append(dict(role="tool", call_id=f"large-{g}-{i}", turn_id="old", name="Read", label=f"src/{g}-{i}.ts", outcome="ok", at="2026-10-13T00:00:00Z", finished_at="2026-10-13T00:00:03Z"))
        mock.world["transcripts"]["t1"] = large
        mock.world["transcripts"]["t2"] = [dict(row) for row in large]
        _fresh(page, mock, until, TWO); _open(page, until, 1, "t1"); _open(page, until, 2, "t2")
        log().evaluate("e => e.scrollTop = 80")
        time.sleep(.1); held = log().evaluate("e => e.scrollTop")
        emit("turn_started", turn="scroll")
        for i in range(20): emit("tool_started", {"call_id": f"scroll-{i}", "name": "Read", "label": "label"}, turn="scroll")
        until(lambda: "Using tools… 20" in groups().last.inner_text())
        check("new calls never pull a scrolled-up reader down", log().evaluate("e => e.scrollTop") == held)
        log().evaluate("e => e.scrollTop = e.scrollHeight")
        time.sleep(.1)
        emit("text", {"text": "new reply\n" * 10}, turn="scroll")
        until(lambda: "new reply" in log().inner_text())
        check("new content follows when the reader was at the bottom", log().evaluate("e => e.scrollHeight - e.clientHeight - e.scrollTop <= 24"))
        log().evaluate("e => e.scrollTop = 0")
        time.sleep(.1); held = log().evaluate("e => e.scrollTop")
        groups().first.click()
        check("opening a group preserves scroll offset", log().evaluate("e => e.scrollTop") == held)
        groups().first.click()
        page.evaluate("""() => { window.__toolLong = []; window.__toolObserver = new PerformanceObserver(l => window.__toolLong.push(...l.getEntries().map(e => e.duration))); window.__toolObserver.observe({entryTypes:['longtask']}); }""")
        _box(page, 1).fill(""); _box(page, 1).press_sequentially("thirty characters typed in here", delay=15)
        time.sleep(.1)
        costs = page.evaluate("window.__toolObserver.disconnect(); window.__toolLong")
        check("typing with 2000-call histories in two panes has no >50ms task", not costs, str(costs))

        # One huge group: bounded DOM, End navigation, hostile labels remain text.
        hostile = '<img src=x onerror="window.__pwned=1">\u202e'
        mock.world["transcripts"]["t1"] = [dict(role="tool", call_id=f"w{i}", turn_id="window", name=hostile, label=f"{hostile} {i}", outcome="ok", at="2026-10-13T00:00:00Z") for i in range(300)]
        _fresh(page, mock, until, TWO); _open(page, until, 1, "t1")
        groups().click()
        check("an opened 300-call group has a bounded DOM", calls().count() <= 24)
        listing = log().locator('ul.tool-calls'); listing.focus(); listing.press("End")
        until(lambda: "299" in calls().last.inner_text())
        check("End reveals the final call", "299" in calls().last.inner_text() and calls().count() <= 24)
        check("hostile names/labels are plain cleaned text", log().locator('img').count() == 0 and not page.evaluate("!!window.__pwned") and "\u202e" not in log().inner_text())

        # Approval containment at the owner's maximum zoom, including keyboard focus.
        page.evaluate("localStorage.setItem('jarvis.hud.zoom', '160')")
        before_conn = mock.sse_connections(); page.reload(); page.wait_for_selector('[data-testid="sidebar"]'); mock.await_reconnect(before_conn)
        _open(page, until, 1, "t1")
        prior = groups().get_attribute("aria-expanded")
        box = groups().bounding_box()
        mock.emit("approval_requested", {"req_id": "tool-card", "code": "TC01", "tool": "run_command", "command": "pytest", "args": {}, "reason": "asks", "layer": "human", "origin": "chat", "thread_id": "t1", "timeout_s": 120})
        until(lambda: page.locator('[data-testid="approval-card"]').count() == 1)
        page.mouse.click(box["x"] + 3, box["y"] + 3)
        check("a card makes the group inert to pointer activation", groups().get_attribute("aria-expanded") == prior and groups().evaluate("e => !!e.closest('[inert]')"))
        for _ in range(6): page.keyboard.press("Tab")
        check("Tab cannot reach a tool row under a card", not page.evaluate("!!document.activeElement.closest('.tool-group')"))
        top = page.evaluate("""() => [...document.querySelectorAll('[data-testid="approval-card"] button')].every(b => {const r=b.getBoundingClientRect(); const h=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2); return h===b || b.contains(h);})""")
        check("card buttons remain topmost at 160 percent", top)
        page.locator('[data-testid="approval-deny"]').click()
        until(lambda: page.locator('[data-testid="approval-card"]').count() == 0)
        check("no page errors", not errors, "; ".join(errors[:3]))
    finally:
        ctx.close()


def main():
    from playwright.sync_api import sync_playwright
    from tests.face import hud_v2_check as v2
    from tests.face.hud_v2_mock import MockDaemon
    mock = MockDaemon(0).start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"])
            toolcalls_checks(browser, mock, f"http://127.0.0.1:{mock.port}", v2.check, v2.until, v2.guard, v2.FAKE_RECOGNIZER)
            browser.close()
    finally: mock.stop()
    print(f"{v2.CHECKS - len(v2.FAILURES)}/{v2.CHECKS} tool-call checks passed")
    return int(bool(v2.FAILURES))

if __name__ == "__main__": raise SystemExit(main())
