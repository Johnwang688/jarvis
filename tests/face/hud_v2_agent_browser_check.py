"""`hud_open_browser` in the real HUD (2026-11-13, decisions D2/D3 of
`docs/plans/2026-11-13-agent-pane-grid-decisions.md`): a chat's
`browser_open` record opens each public page in a new Remote Browser pane,
as one batch laid out as a grid, and the window reports its counts.

What it pins, against the hermetic Browser contract (`hud_v2_mock_browser`,
the production pane registry and a fake Chrome host; no page is loaded):

  - three pages from a one-pane window: a grid of four, the chat first,
    focus and the keyboard unmoved, each new pane a Browser view on the
    default profile with one Remote tab;
  - **never the owner's navigation**: each Remote tab is made by `POST
    /browser/tabs` with no URL and then `/restore` (which never opens a
    loopback port, `owner_browser_check.py` test_11c), never `/navigate`
    and never a new tab with a URL;
  - **never the owner's tab**: `/browser/front` reports each one with
    `source {kind: "unknown"}`, so the registry resolves it as unknown and
    no agent may look at or drive it;
  - the window judges each URL again: a loopback URL in the record is not
    opened (and nothing ever reaches it), and is counted as not placed in
    the report to `/agent-panes/report`.

Run: PYTHONPATH=. .venv/bin/python tests/face/hud_v2_agent_browser_check.py
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from tests.face import hud_v2_mock_subagents as mock_subagents  # noqa: E402
from tests.face.hud_v2_layout_check import _boot, _pane, _set_storage, _set_ws, _ws  # noqa: E402
from tests.face.hud_v2_mock_browser import state  # noqa: E402
from tests.face.hud_v2_remote_check import Remote  # noqa: E402

PAGES = ["https://www.instagram.com/", "https://www.instagram.com/explore/", "https://www.instagram.com/reels/"]
LOCAL = "http://127.0.0.1:3000/agent-local"


def agent_browser_checks(browser, mock, base, check, until, guard, init_script):
    print("\nhud_open_browser: agent-opened Remote panes")
    ctx = browser.new_context(viewport={"width": 1800, "height": 1000}, permissions=["microphone"])
    guard(ctx)
    ctx.add_init_script(init_script)
    Remote(ctx, mock)
    page = ctx.new_page()
    page.set_default_timeout(5000)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        state(mock)
        _boot(page, mock, base, until)
        _set_storage(page, zoom="100", layout="{}")
        _set_ws(page, json.dumps({"preset": "single", "focused": 1, "panes": [{"view": "chat"}]}))
        _boot(page, mock, None, until, reload=True)
        page.locator(_pane(1, '[data-testid="input"]')).click()
        reports = mock_subagents.state(mock).setdefault("reports", [])
        reports.clear()
        start = len(mock.calls)
        rid = "3" * 32
        mock.emit("browser_open", {"urls": PAGES + [LOCAL], "request_id": rid}, thread_id="t1", project_id="p1")
        until(lambda: len(_ws(page)["ws"]["order"]) == 4)
        until(lambda: len(reports) == 1)
        ws = _ws(page)["ws"]
        new = [n for n in ws["order"] if n != 1]
        specs = [ws["panes"][n - 1] for n in new]
        check("three pages from one pane: a grid of four, the chat first",
              ws["order"][0] == 1 and len(new) == 3 and all(s["view"] == "browser" for s in specs),
              str(ws["order"]))
        check("each new pane: the default profile, one Remote tab at its page, not the owner's",
              sorted(s["browser"]["tabs"][0]["url"] for s in specs) == sorted(PAGES)
              and all(len(s["browser"]["tabs"]) == 1 and s["browser"]["tabs"][0]["mode"] == "remote"
                      and s["browser"]["tabs"][0].get("source") == {"kind": "unknown"}
                      # The default profile (the window pins its id once the pane mounts), never Private;
                      # see and drive at their defaults (stored only when off).
                      and s.get("browserProfile", "p-11111111") == "p-11111111"
                      and "see" not in s and "drive" not in s for s in specs),
              json.dumps(specs)[:400])
        check("focus and the keyboard stay in the owner's pane",
              ws["focused"] == 1 and page.evaluate("!!document.activeElement.closest('[data-testid=\"pane-1\"]')"))
        until(lambda: sum(1 for m, p, _ in mock.calls[start:] if m == "POST" and p.endswith("/restore")) >= 3)
        calls = mock.calls[start:]
        opened = [b for m, p, b in calls if m == "POST" and p == "/browser/tabs"]
        restored = [b.get("url") for m, p, b in calls if m == "POST" and p.endswith("/restore")]
        check("each Remote tab is made without a URL and restored, never the owner's navigate",
              len(opened) >= 3 and all("url" not in b for b in opened) and sorted(set(restored)) == sorted(PAGES)
              and not any(p.endswith("/navigate") for _, p, _ in calls),
              f"opened={opened} restored={restored}")
        check("the loopback URL the window judged was never opened or asked for",
              not any(LOCAL in json.dumps(b) for _, _, b in calls)
              and all(LOCAL not in s["browser"]["tabs"][0]["url"] for s in specs))
        fronts = [b for m, p, b in mock.calls if m == "POST" and p == "/browser/front"]
        last = fronts[-1] if fronts else {"panes": []}
        agent = [p for p in last["panes"] if p["pane_id"] in new]
        check("/browser/front tells the daemon each tab is not the owner's (source unknown)",
              len(agent) == 3 and all(t["source"] == {"kind": "unknown"} and t["mode"] == "remote"
                                      for p in agent for t in p["tabs"])
              and all(p["profile"] == "p-11111111" for p in agent), json.dumps(agent)[:400])
        host = state(mock)
        check("the registry resolves them as unknown: no agent may look or drive",
              all(b.source.kind == "unknown" for w in mock.browser_panes._windows.values()
                  for pane in w.panes.values() if pane.pane_id in new for b in pane.tabs.values()),
              str(len(host.rows)))
        report = reports[0]
        check("the window reports 3 opened, 1 not (the loopback URL), keyed by the request id",
              report["request_id"] == rid and (report["placed"], report["not_placed"]) == (3, 1), str(report))
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
            try:
                agent_browser_checks(browser, mock, f"http://127.0.0.1:{mock.port}", v2.check, v2.until, v2.guard,
                                     v2.FAKE_RECOGNIZER)
            except Exception as exc:
                v2.check("agent Browser scenario completes", False, repr(exc)[:300])
            finally:
                browser.close()
    finally:
        mock.stop()
    print(f"{v2.CHECKS - len(v2.FAILURES)}/{v2.CHECKS} agent Browser checks passed")
    for failure in v2.FAILURES:
        print("  -", failure)
    return int(bool(v2.FAILURES))


if __name__ == "__main__":
    raise SystemExit(main())
