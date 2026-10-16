"""SV-3: a chat's native sub-agents in the HUD — rows where they were spawned,
a read-only live transcript pane, and the approval card's link to its row.

Free and hermetic: its own `MockDaemon(0)` (an ephemeral port, never
8402/8403/8405, `guard_live`), every context under the CSP watch, the mock
enforcing the owner-only rule on both sub-agent routes. What it pins:

  1. rows at the spawn point: Claude's after the group holding its Agent
     call, Codex's by its start time; no child's words in the chat's log; a
     nested child only in its parent's pane; hostile names stay text;
  2. live: a bus record moves a row's status at once, a new child appears
     and is named from the owner's list route;
  3. opening: a click (or Enter) splits a read-only pane beside the chat; a
     second click focuses it; with no room the last sub-agent pane is reused,
     and with none the window says so; the brief is collapsed, then shown;
     the pane follows the child live;
  4. keys: Tab reaches a row, Enter and Space open, Ctrl+D is still talk;
  5. a card: rows are inert under it; its "from sub-agent ‹name›" marks the
     row and, once the card is answered, focuses the pane or the row;
  6. more than six rows at a spot collapse into one line;
  7. a reconnect reads everything again (a restart's children come back
     stopped); a reload restores the pane; a gone record says so and closes.

Run: PLAYWRIGHT_BROWSERS_PATH=… PYTHONPATH=. .venv/bin/python tests/face/hud_v2_subagents_check.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.face import hud_v2_mock_subagents as mock_subagents  # noqa: E402
from tests.face.hud_v2_layout_check import TALK_PROBE, _pane  # noqa: E402
from tests.face.hud_v2_multichat_check import _box, _chat, _expand, _fresh  # noqa: E402

SHOTS = Path(os.environ.get("SV3_SHOTS", str(Path.home() / ".cache" / "jarvis-lead" / "sv3-shots")))
TWO = json.dumps({"preset": "cols2", "focused": 1,
                  "panes": [{"view": "chat"}, {"view": "chat"}, {"view": "file"}, {"view": "task"}]})
C0 = "2026-10-15T10:00:00+00:00"


def at(s: int) -> str:
    return f"2026-10-15T10:{s // 60:02d}:{s % 60:02d}+00:00"


HOSTILE = '<img src=x onerror="window.__pwned=1">‮evil'
CHILD_WORDS = "CHILD WORDS looking at tests"
BRIEF = "BRIEF-TEXT: find the flaky test under tests/ and report back"


def row(sid, **over):
    out = {"sid": sid, "status": "running", "depth": 1, "background": False, "name": sid, "kind": "general-purpose",
           "started_at": at(1), "turn_id": "turn1", "entries": 0}
    out.update(over)
    return out


def seed(mock):
    w = mock.world
    base = next(t for t in w["threads"] if t["id"] == "t1")
    for tid, provider, title in (("c1", "claude", "claude chat"), ("x1", "codex", "codex chat")):
        if not any(t["id"] == tid for t in w["threads"]):
            w["threads"].append({**base, "id": tid, "provider": provider, "title": title, "model": None,
                                 "updated": "2026-10-15T10:00:00+00:00"})
    w["transcripts"]["c1"] = [
        {"role": "user", "text": "please fan out", "at": at(0), "turn_id": "turn1"},
        {"role": "tool", "call_id": "toolu_alpha", "turn_id": "turn1", "name": "Agent", "label": "Find the flaky test",
         "outcome": "ok", "at": at(1), "finished_at": at(2)},
        {"role": "tool", "call_id": "toolu_beta", "turn_id": "turn1", "name": "Agent", "label": "hostile",
         "outcome": "ok", "at": at(1), "finished_at": at(2)},
        {"role": "assistant", "text": "Launched two.", "at": at(3), "turn_id": "turn1"},
        {"role": "user", "text": "next question", "at": at(30), "turn_id": "turn2"},
        {"role": "assistant", "text": "Second answer.", "at": at(31), "turn_id": "turn2"},
    ]
    w["transcripts"]["x1"] = [
        {"role": "user", "text": "codex fan out", "at": at(0), "turn_id": "xt1"},
        {"role": "assistant", "text": "Spawning a fixer.", "at": at(2), "turn_id": "xt1"},
        {"role": "user", "text": "codex later", "at": at(40), "turn_id": "xt2"},
    ]
    s = mock_subagents.state(mock)
    s["lists"] = {
        "c1": [row("cl:toolu_alpha", name="Find the flaky test", tool_uses=2, tokens=18200, last_tool="Bash"),
               row("cl:toolu_beta", name=HOSTILE, status="done", ended_at=at(9), entries=1),
               row("cl:toolu_inner", name="Inner probe", depth=2, parent_sid="cl:toolu_alpha", started_at=at(5))],
        "x1": [row("cx:019a-fixer", name="/root/fixer", kind="", started_at=at(5), tool_uses=1)],
    }
    s["transcripts"] = {
        "c1|cl:toolu_alpha": {
            "subagent": s["lists"]["c1"][0], "brief": BRIEF, "truncated": False,
            "messages": [
                {"role": "tool", "call_id": "toolu_c1", "name": "Bash", "label": "pytest -x tests/", "outcome": "ok",
                 "seq": 1, "at": at(2), "finished_at": at(4)},
                {"role": "tool", "call_id": "toolu_inner", "name": "Agent", "label": "Inner probe", "outcome": "running",
                 "seq": 2, "at": at(5)},
                {"role": "assistant", "text": CHILD_WORDS + " <script>window.__pwned=1</script>", "seq": 3, "at": at(6)},
            ]},
        "c1|cl:toolu_beta": {"subagent": s["lists"]["c1"][1], "truncated": False, "messages": [
            {"role": "result", "text": "beta finished", "status": "done", "seq": 1, "at": at(9)}]},
        "c1|cl:toolu_inner": {"subagent": s["lists"]["c1"][2], "truncated": False, "messages": [
            {"role": "result", "text": "inner finished", "status": "done", "seq": 1, "at": at(8)}]},
        "x1|cx:019a-fixer": {"subagent": s["lists"]["x1"][0], "truncated": False, "messages": [
            {"role": "tool", "call_id": "item_1", "name": "shell", "label": "make test", "outcome": "denied",
             "seq": 1, "at": at(6)}]},
    }
    s["fail"] = {}


def drop_events(mock):
    """End the window's event stream, as a daemon restart would: it reconnects."""
    with mock._sse_cond:
        mock._sse_gen += 1
        mock._sse_cond.notify_all()


def subagents_checks(browser, mock, base, check, until, guard, init_script):
    print("\nnative sub-agents in the HUD (SV-3)")
    seed(mock)
    SHOTS.mkdir(parents=True, exist_ok=True)
    ctx = browser.new_context(viewport={"width": 1600, "height": 900}, permissions=["microphone"])
    guard(ctx)
    ctx.add_init_script(init_script)
    ctx.add_init_script(TALK_PROBE)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_default_timeout(5000)
    s = mock_subagents.state(mock)
    rows = lambda n=1: page.locator(_pane(n, '[data-testid="subagent-row"]'))
    log = lambda n=1: page.locator(_pane(n, '[data-testid="log"]'))
    sub_panes = lambda: page.locator('[data-testid^="pane-"][data-view="subagent"]')

    def emit(tid, sid, **data):
        mock.emit("subagent", {"sid": sid, "op": "progress", "status": "running", "depth": 1, "background": False,
                               "started_at": at(1), "entries": 0, **data}, thread_id=tid, project_id="p1",
                  turn_id="turn1", at=at(50))

    def open_thread(n, tid):
        _box(page, n).click()
        _expand(page, until, "p1", f"thread-{tid}")
        page.locator(f'[data-testid="thread-{tid}"]').click()
        until(lambda: _chat(page, n)["threadId"] == tid, timeout=3)
        until(lambda: log(n).inner_text() != "", timeout=3)

    try:
        page.goto(base + "/")
        page.wait_for_selector('[data-testid="sidebar"]', state="attached")
        _fresh(page, mock, until, TWO)
        open_thread(1, "c1")
        open_thread(2, "x1")
        until(lambda: rows(1).count() == 2 and rows(2).count() == 1)

        # 1. Rows at the spawn point.
        children = """e => [...e.children].map(c => c.dataset.testid === 'subagent-rows'
            ? [...c.querySelectorAll('[data-testid=subagent-row]')].map(r => r.dataset.sid).join('+')
            : c.dataset.testid || (c.classList.contains('tool-group') ? 'tool-group' : c.className))"""
        order = log(1).evaluate(children)
        check("Claude rows sit right after the group holding their Agent calls",
              order == ["msg-user", "tool-group", "cl:toolu_alpha+cl:toolu_beta", "msg-assistant", "msg-user",
                        "msg-assistant"], str(order))
        order2 = log(2).evaluate(children)
        check("a Codex row sits by its start time", order2 == ["msg-user", "msg-assistant", "cx:019a-fixer", "msg-user"],
              str(order2))
        first = rows(1).first
        check("a row reads name, provider, kind, status word, last tool, tools and tokens",
              all(x in first.inner_text() for x in ("Find the flaky test", "CLAUDE", "general-purpose", "running",
                                                    "Bash", "2 tools", "18k tokens"))
              and first.get_attribute("data-status") == "running", first.inner_text())
        check("status marks are Graphite words: running amber, done green",
              first.locator(".sa-status").evaluate("e => getComputedStyle(e).color") == "rgb(251, 191, 36)"
              and rows(1).nth(1).locator(".sa-status").evaluate("e => getComputedStyle(e).color") == "rgb(109, 208, 143)")
        check("Codex's row says Codex, with no kind", "CODEX" in rows(2).first.inner_text()
              and rows(2).first.get_attribute("data-provider") == "codex")
        check("a nested child is not in the chat's transcript",
              page.locator('[data-sid="cl:toolu_inner"]').count() == 0)
        check("hostile names are text: no element, no script, no bidi override",
              page.locator(_pane(1, '[data-testid="log"] img')).count() == 0 and not page.evaluate("!!window.__pwned")
              and "‮" not in log(1).inner_text() and "<img" in rows(1).nth(1).inner_text())
        check("no child's words reach the chat's transcript", CHILD_WORDS not in log(1).inner_text()
              and BRIEF not in log(1).inner_text())
        check("the HUD's own reads pass the owner gate; nothing was refused",
              s["refused"] == 0 and ("list", "c1") in s["reads"] and ("list", "x1") in s["reads"])
        page.screenshot(path=str(SHOTS / "sv3-rows.png"))

        # The mock's gate bites (it is the daemon's rule, not a pass-through).
        for headers in ({}, {"Origin": "http://evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
            req = urllib.request.Request(f"{base}/threads/c1/subagents", headers=headers)
            try:
                urllib.request.urlopen(req, timeout=3)
                code = 200
            except urllib.error.HTTPError as e:
                code = e.code
            check(f"the list route refuses a non-owner read {headers or '(bare)'}", code == 403)
        s["refused"] = 0

        # 2. Live: a status moves at once, a new child appears and is named.
        emit("c1", "cl:toolu_alpha", op="finished", status="failed", entries=4, ended_at=at(20))
        until(lambda: rows(1).first.get_attribute("data-status") == "failed")
        check("a bus record moves the row's status word at once",
              rows(1).first.get_attribute("data-status") == "failed" and "failed" in rows(1).first.inner_text())
        s["lists"]["c1"][0].update(status="failed", ended_at=at(20), stop_reason=None, entries=4)
        s["lists"]["c1"].append(row("cl:toolu_gamma", name="Gamma helper", started_at=at(32)))
        emit("c1", "cl:toolu_gamma", op="started", started_at=at(32))
        until(lambda: page.locator('[data-sid="cl:toolu_gamma"]').count() == 1)
        until(lambda: "Gamma helper" in page.locator('[data-sid="cl:toolu_gamma"]').inner_text())
        order = log(1).evaluate(children)
        check("a child heard on the bus appears by time and is named from the owner's list",
              order[-1] == "cl:toolu_gamma" and "Gamma helper" in page.locator('[data-sid="cl:toolu_gamma"]').inner_text(),
              str(order))

        # 3. Opening a pane.
        rows(1).first.locator("button").click()
        until(lambda: sub_panes().count() == 1)
        pane = sub_panes().first
        pane_id = pane.get_attribute("data-testid")
        check("a click splits a read-only sub-agent pane beside the chat",
              sub_panes().count() == 1 and pane.locator('[data-testid="input"]').count() == 0
              and pane.locator('[data-testid="sa-head-name"]').inner_text() == "Find the flaky test")
        offered = lambda loc: loc.locator('[data-testid="tab-subagent"], option[value="subagent"]').count()
        check("the strip offers the view only where it is shown",
              offered(page.locator(_pane(1))) == 0 and offered(page.locator(_pane(2))) == 0 and offered(pane) == 1)
        until(lambda: pane.locator('[data-testid="sa-brief-toggle"]').count() == 1)
        check("the brief is there, collapsed", pane.locator('[data-testid="sa-brief-text"]').count() == 0
              and pane.locator('[data-testid="sa-brief-toggle"]').get_attribute("aria-expanded") == "false")
        check("the pane shows the child's words and its calls by label and outcome",
              CHILD_WORDS in pane.inner_text() and not page.evaluate("!!window.__pwned")
              and pane.locator('[data-testid="tool-group"]').count() >= 1)
        pane.locator('[data-testid="tool-group"]').first.click()
        check("tool labels and outcomes, never arguments", "pytest -x tests/" in pane.inner_text()
              and pane.locator('[data-testid="tool-call"]').first.get_attribute("data-status") == "ok")
        check("its nested child is drawn in its pane, at its spawn", pane.locator('[data-sid="cl:toolu_inner"]').count() == 1)
        pane.locator('[data-testid="sa-brief-toggle"]').click()
        check("the brief opens as plain text", pane.locator('[data-testid="sa-brief-text"]').inner_text() == BRIEF)
        page.screenshot(path=str(SHOTS / "sv3-pane.png"))
        # Rows may wrap in a narrow pane, but the status word stays on the
        # name's line, right after it (review of #212, owner Q1).
        lines = page.evaluate("""() => [...document.querySelectorAll('[data-testid="subagent-row"]')].map(r => {
            const n = r.querySelector('.sa-name').getBoundingClientRect(), st = r.querySelector('.sa-status').getBoundingClientRect(),
                  b = r.querySelector('button').getBoundingClientRect();
            return [Math.abs(n.top - st.top) < 2 && st.left >= n.right, b.height > 30]; })""")
        check("a wrapped row keeps its status word on the name's line, right after it",
              lines and all(same for same, _ in lines) and any(wrapped for _, wrapped in lines), str(lines))
        before = page.evaluate("window.__hud.workspace().ws.order.length")
        rows(1).first.locator("button").click()
        time.sleep(0.2)
        check("a second click focuses the pane already showing it",
              sub_panes().count() == 1 and page.evaluate("window.__hud.workspace().ws.order.length") == before
              and page.evaluate(f"window.__hud.workspace().ws.focused") == int(pane_id.split("-")[1]))

        # Live in the pane.
        s["transcripts"]["c1|cl:toolu_alpha"]["messages"].append(
            {"role": "result", "text": "LIVE RESULT arrived", "status": "failed", "seq": 4, "at": at(20)})
        emit("c1", "cl:toolu_alpha", op="progress", status="failed", entries=5, ended_at=at(20))
        until(lambda: "LIVE RESULT arrived" in pane.inner_text())
        check("the pane follows the child live", "LIVE RESULT arrived" in pane.inner_text()
              and pane.locator('[data-testid="sa-head-status"]').inner_text().startswith("failed"))

        # A nested row opens its own pane; the Codex child shows that it has no brief.
        pane.locator('[data-sid="cl:toolu_inner"] button').click()
        until(lambda: sub_panes().count() == 2)
        check("a nested row opens its own pane", page.locator('[data-testid="sa-pane"][data-sid="cl:toolu_inner"]').count() == 1)

        # 4. Keys: Tab to the Codex row, Enter opens; Ctrl+D stays talk.
        _box(page, 2).click()
        page.keyboard.press("Shift+Tab")
        for _ in range(30):
            if page.evaluate("!!document.activeElement.closest('[data-testid=\"subagent-row\"]')"):
                break
            page.keyboard.press("Shift+Tab")
        check("Tab reaches a row", page.evaluate("!!document.activeElement.closest('[data-testid=\"subagent-row\"]')"))
        focused_sid = page.evaluate("document.activeElement.closest('[data-testid=\"subagent-row\"]').dataset.sid")
        page.keyboard.down("Control"); page.keyboard.down("d")
        time.sleep(0.12)
        page.keyboard.up("d"); page.keyboard.up("Control")
        check("Ctrl+D on a row is talk, and opens nothing",
              sub_panes().count() == 2 and page.evaluate("window.__talkKeys.some(e => e.code === 'KeyD' && e.defaultPrevented) && window.__talkPast === 0"))
        page.keyboard.press("Enter")
        until(lambda: page.locator(f'[data-testid="sa-pane"][data-sid="{focused_sid}"]').count() == 1)
        check("Enter opens the focused row's pane", page.locator(f'[data-testid="sa-pane"][data-sid="{focused_sid}"]').count() == 1)
        codex = page.locator('[data-testid="sa-pane"][data-sid="cx:019a-fixer"]')
        if focused_sid != "cx:019a-fixer":
            rows(2).first.locator("button").click()
        until(lambda: codex.count() == 1)
        until(lambda: codex.locator('[data-testid="sa-brief-none"]').count() == 1)
        check("a Codex child's pane says Codex sends no brief, and shows its denied call",
              "Codex does not send" in codex.inner_text() and "1 denied" in codex.inner_text())
        page.screenshot(path=str(SHOTS / "sv3-panes.png"))

        # With no room for another pane, the last sub-agent pane shows the next one.
        count, panes_before = sub_panes().count(), page.evaluate("window.__hud.workspace().ws.order.length")
        last = page.locator('[data-testid="sa-pane"][data-sid="cx:019a-fixer"]')
        page.set_viewport_size({"width": 1100, "height": 900})
        time.sleep(0.2)
        page.locator('[data-sid="cl:toolu_beta"] button').first.click()
        until(lambda: page.locator('[data-testid="sa-pane"][data-sid="cl:toolu_beta"]').count() == 1)
        check("past the room for a split, the last sub-agent pane is reused, never a chat",
              page.locator('[data-testid="sa-pane"][data-sid="cl:toolu_beta"]').count() == 1 and last.count() == 0
              and sub_panes().count() == count
              and page.evaluate("window.__hud.workspace().ws.order.length") == panes_before
              and _chat(page, 1)["threadId"] == "c1" and _chat(page, 2)["threadId"] == "x1")
        page.set_viewport_size({"width": 1600, "height": 900})
        # And with no sub-agent pane to reuse, the window says so.
        _fresh(page, mock, until, TWO, size=(760, 900))
        open_thread(1, "c1")
        until(lambda: rows(1).count() == 3)
        rows(1).first.locator("button").click()
        until(lambda: page.locator('[data-testid="error"]').count() == 1)
        check("with no room and no sub-agent pane, the window says so and takes nothing",
              "No room for another pane" in page.locator('[data-testid="error"]').inner_text()
              and sub_panes().count() == 0 and page.evaluate("window.__hud.workspace().ws.order.length") == 2)

        # 5. Cards: inert rows, and the card's link.
        _fresh(page, mock, until, TWO)
        open_thread(1, "c1")
        open_thread(2, "x1")
        until(lambda: rows(1).count() == 3 and rows(2).count() == 1)
        mock.emit("approval_requested", {
            "req_id": "card1", "code": "SV31", "tool": "commandExecution", "command": "touch /x", "args": {},
            "reason": "asks", "layer": "widening", "origin": "", "thread_id": "x1", "timeout_s": 120,
            "headline": "SANDBOX WIDENING: Codex sub-agent ‹/root/fixer› (not the orchestrator) asks to run beyond its sandbox",
            "subagent": "cx:019a-fixer", "allowlistable": False})
        card = page.locator('[data-testid="approval-card"]')
        until(lambda: card.count() == 1)
        link = card.locator('[data-testid="approval-subagent"]')
        until(lambda: "/root/fixer" in (link.inner_text() if link.count() else ""))
        check("a sub-agent's card names it as a link, beside '(not the orchestrator)'",
              link.inner_text() == "from sub-agent ‹/root/fixer›" and "(not the orchestrator)" in card.inner_text())
        box = rows(1).first.bounding_box()
        page.mouse.click(box["x"] + 5, box["y"] + 5)
        time.sleep(0.2)
        check("under a card a row opens nothing", sub_panes().count() == 0 and card.count() == 1)
        for _ in range(4):
            page.keyboard.press("Tab")
        check("Tab under a card never reaches a row or the link",
              not page.evaluate("!!document.activeElement.closest('[data-testid=\"subagent-row\"]')")
              and page.evaluate("document.activeElement.dataset.testid") != "approval-subagent")
        link.click()
        until(lambda: "sa-flash" in (rows(2).first.get_attribute("class") or ""))
        check("the link marks the row behind the card, and decides nothing",
              card.count() == 1 and "sa-flash" in rows(2).first.get_attribute("class") and not mock.posted("/approvals/card1"))
        page.screenshot(path=str(SHOTS / "sv3-card.png"))
        page.locator('[data-testid="approval-deny"]').click()
        until(lambda: card.count() == 0)
        until(lambda: page.evaluate("document.activeElement.closest('[data-testid=\"subagent-row\"]')?.dataset.sid || ''") == "cx:019a-fixer")
        check("once the card is answered, the row has focus",
              page.evaluate("document.activeElement.closest('[data-testid=\"subagent-row\"]')?.dataset.sid || ''") == "cx:019a-fixer"
              and sub_panes().count() == 0)
        # With its pane open, the pane takes focus instead.
        rows(1).first.locator("button").click()
        until(lambda: sub_panes().count() == 1)
        _box(page, 1).click()
        mock.emit("approval_requested", {
            "req_id": "card2", "code": "SV32", "tool": "Bash", "command": "make", "args": {}, "reason": "asks",
            "layer": "human", "origin": "Claude sub-agent ‹Find the flaky test› (not the orchestrator)",
            "thread_id": "c1", "timeout_s": 120, "subagent": "cl:toolu_alpha"})
        until(lambda: card.count() == 1)
        card.locator('[data-testid="approval-subagent"]').click()
        page.locator('[data-testid="approval-deny"]').click()
        until(lambda: card.count() == 0)
        sub_id = sub_panes().first.get_attribute("data-testid")
        until(lambda: page.evaluate(f"!!document.activeElement.closest('[data-testid=\"{sub_id}\"]')"))
        check("…and with its pane open, the pane has focus",
              page.evaluate(f"!!document.activeElement.closest('[data-testid=\"{sub_id}\"]')"))

        # The link acts only for its own card, answered here, with no card after it (review of #212, LOW-1).
        def card_for(req, sid, tid, name="x"):
            mock.emit("approval_requested", {
                "req_id": req, "code": req[-4:].upper(), "tool": "Bash", "command": "make", "args": {},
                "reason": "asks", "layer": "human", "origin": f"Claude sub-agent ‹{name}› (not the orchestrator)",
                "thread_id": tid, "timeout_s": 120, **({"subagent": sid} if sid else {})})
        _box(page, 1).click()
        focus_in_rows = "!!document.activeElement.closest('[data-testid=\"subagent-row\"], [data-view=\"subagent\"]')"
        panes_before = sub_panes().count()
        card_for("queued-a", "cx:019a-fixer", "x1")
        card_for("queued-b", None, "c1")
        until(lambda: card.count() == 1 and card.get_attribute("data-req") == "queued-a")
        card.locator('[data-testid="approval-subagent"]').click()
        page.locator('[data-testid="approval-deny"]').click()
        until(lambda: card.get_attribute("data-req") == "queued-b" if card.count() else False)
        page.locator('[data-testid="approval-deny"]').click()
        until(lambda: card.count() == 0)
        time.sleep(0.3)
        check("with another card queued behind it, the link does nothing once both are answered",
              not page.evaluate(focus_in_rows) and sub_panes().count() == panes_before)
        # …nor, with the child's pane open, move the window's focus while the next card is up.
        rows(2).first.locator("button").click()
        until(lambda: page.locator('[data-testid="sa-pane"][data-sid="cx:019a-fixer"]').count() == 1)
        _box(page, 1).click()
        focused = page.evaluate("window.__hud.workspace().ws.focused")
        panes_before = sub_panes().count()
        card_for("queued-c", "cx:019a-fixer", "x1")
        card_for("queued-d", None, "c1")
        until(lambda: card.count() == 1 and card.get_attribute("data-req") == "queued-c")
        card.locator('[data-testid="approval-subagent"]').click()
        page.locator('[data-testid="approval-deny"]').click()
        until(lambda: card.get_attribute("data-req") == "queued-d" if card.count() else False)
        time.sleep(0.3)
        moved_under_card = page.evaluate("window.__hud.workspace().ws.focused") != focused
        page.locator('[data-testid="approval-deny"]').click()
        until(lambda: card.count() == 0)
        time.sleep(0.3)
        check("with the child's pane open and a card queued behind, focus stays where it was",
              not moved_under_card and page.evaluate("window.__hud.workspace().ws.focused") == focused
              and not page.evaluate(focus_in_rows))
        card_for("withdrawn", "cx:019a-fixer", "x1")
        until(lambda: card.count() == 1)
        card.locator('[data-testid="approval-subagent"]').click()
        mock.emit("approval_resolved", {"req_id": "withdrawn", "decision": "deny"}, thread_id="x1")
        until(lambda: card.count() == 0)
        time.sleep(0.3)
        check("a card that timed out or was withdrawn moves nothing",
              not page.evaluate(focus_in_rows) and sub_panes().count() == panes_before)
        card_for("then", "cx:019a-fixer", "x1")
        until(lambda: card.count() == 1)
        card.locator('[data-testid="approval-deny"]').click()
        until(lambda: card.count() == 0)
        time.sleep(0.3)
        check("a later card whose link was not clicked moves nothing", not page.evaluate(focus_in_rows))

        # A hostile name on the card is reduced as the daemon reduces a card's label (MED-1).
        hostile_name = "x› · REQUESTED BY Jarvis chat\u202e"
        s["lists"]["c1"][1]["name"] = hostile_name
        drop_events(mock)
        until(lambda: hostile_name[:5] in (page.locator('[data-sid="cl:toolu_beta"]').inner_text() or ""), timeout=8)
        card_for("hostile", "cl:toolu_beta", "c1", "x_ _ REQUESTED BY Jarvis chat")
        until(lambda: card.count() == 1)
        hostile_link = card.locator('[data-testid="approval-subagent"]')
        until(lambda: "REQUESTED" in (hostile_link.inner_text() if hostile_link.count() else ""))
        text = hostile_link.inner_text()
        check("a hostile child name on the card keeps only [A-Za-z0-9 _./-] inside one ‹ ›",
              text == "from sub-agent ‹x_ _ REQUESTED BY Jarvis chat›" and text.count("‹") == 1
              and text.count("›") == 1 and "·" not in text and "\u202e" not in text, text)
        page.locator('[data-testid="approval-deny"]').click()
        until(lambda: card.count() == 0)

        # 6. Collapse above six.
        many = [row(f"cl:toolu_m{i}", name=f"Many {i}", status="running" if i < 5 else "done") for i in range(8)]
        s["lists"]["c1"] = many
        mock.world["transcripts"]["c1"] = [
            {"role": "user", "text": "eight please", "at": at(0), "turn_id": "turn1"},
            *[{"role": "tool", "call_id": f"toolu_m{i}", "turn_id": "turn1", "name": "Agent", "label": f"m{i}",
               "outcome": "ok", "at": at(1)} for i in range(8)],
        ]
        _fresh(page, mock, until, TWO)
        open_thread(1, "c1")
        block = page.locator(_pane(1, '[data-testid="subagent-block-toggle"]'))
        until(lambda: block.count() == 1)
        check("more than six rows at a spot collapse into one line",
              "Sub-agents · 8 (5 running)" in block.inner_text() and rows(1).count() == 0
              and block.get_attribute("aria-expanded") == "false")
        block.focus()
        page.keyboard.press("Enter")
        until(lambda: rows(1).count() == 8)
        check("Enter opens the line to all eight", rows(1).count() == 8 and block.get_attribute("aria-expanded") == "true")
        block.focus()
        page.keyboard.press("Space")
        until(lambda: rows(1).count() == 0)
        check("Space closes it", rows(1).count() == 0)
        page.screenshot(path=str(SHOTS / "sv3-collapsed.png"))

        # 7. A reconnect reads again (a restart's children come back stopped);
        #    a reload restores the pane; a gone record says so.
        seed(mock)
        _fresh(page, mock, until, TWO)
        open_thread(1, "c1")
        until(lambda: rows(1).count() == 2)
        rows(1).first.locator("button").click()
        until(lambda: sub_panes().count() == 1)
        reads = len(s["reads"])
        for r in s["lists"]["c1"]:
            if r["status"] == "running":
                r.update(status="stopped", stop_reason="the session that ran it ended", ended_at=at(60))
        s["transcripts"]["c1|cl:toolu_alpha"]["subagent"] = s["lists"]["c1"][0]
        before_conn = mock.sse_connections()
        drop_events(mock)
        mock.await_reconnect(before_conn, timeout=8)
        until(lambda: rows(1).first.get_attribute("data-status") == "stopped", timeout=8)
        new_reads = s["reads"][reads:]
        check("a reconnect reads the list and the open pane again; live children come back stopped",
              rows(1).first.get_attribute("data-status") == "stopped" and ("list", "c1") in new_reads
              and ("transcript", "c1", "cl:toolu_alpha") in new_reads, str(new_reads))
        before_conn = mock.sse_connections()
        page.reload()
        page.wait_for_selector('[data-testid="sidebar"]', state="attached")
        mock.await_reconnect(before_conn)
        until(lambda: page.locator('[data-testid="sa-pane"][data-sid="cl:toolu_alpha"]').count() == 1)
        check("a reload restores the pane and reads it again",
              "Find the flaky test" in page.locator('[data-testid="sa-pane"]').first.inner_text())
        stored = page.evaluate("JSON.parse(localStorage.getItem('jarvis.hud.workspace')).panes")
        check("the stored workspace holds the pane's ids only",
              any(p.get("subagent") == {"threadId": "c1", "sid": "cl:toolu_alpha"} for p in stored)
              and CHILD_WORDS not in json.dumps(stored) and BRIEF not in json.dumps(stored))
        s["fail"]["c1|cl:toolu_alpha"] = 404
        page.reload()
        page.wait_for_selector('[data-testid="sidebar"]', state="attached")
        gone = page.locator('[data-testid="sa-failed"][data-why="gone"]')
        until(lambda: gone.count() == 1)
        check("a gone record says so", "record is gone" in gone.inner_text())
        gone.locator('[data-testid="sa-close"]').click()
        until(lambda: sub_panes().count() == 0)
        check("…and Close closes its pane", sub_panes().count() == 0)
        s["fail"] = {}
        check("the HUD's reads were never refused by the owner gate", s["refused"] == 0)
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
            browser = pw.chromium.launch(headless=True, args=["--use-fake-ui-for-media-stream",
                                                              "--use-fake-device-for-media-stream"])
            subagents_checks(browser, mock, f"http://127.0.0.1:{mock.port}", v2.check, v2.until, v2.guard,
                             v2.FAKE_RECOGNIZER)
            browser.close()
    finally:
        mock.stop()
    print(f"{v2.CHECKS - len(v2.FAILURES)}/{v2.CHECKS} sub-agent checks passed")
    for failure in v2.FAILURES:
        print("  -", failure)
    return int(bool(v2.FAILURES))


if __name__ == "__main__":
    raise SystemExit(main())
