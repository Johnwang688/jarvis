"""Headless checks for the Browser pane's port rules and its "keep its own
origin" switch (WP-E; plan docs/plans/2026-10-09-hud-workspace-plan.md §2.5,
decisions W-4; contract in docs/hud-api.md), and for the rename that made the
Preview pane the Browser (BR-3a; docs/plans/2026-10-10-hud-browser-plan.md §7).
This suite was `hud_v2_preview_check.py`.

Free and hermetic. The section runs against a `MockDaemon(0)` of its own and a
browser context of its own. The mock is the HUD listener; its API listener,
workshop and a local dev server are listeners of their own on ephemeral ports,
all reported on `/status` — and every request to 8402/8403/8405, the owner's
live daemon, is refused and counted as a failure (`hud_v2_check.guard_live`).

The checks worth keeping, each written to bite:
  - the Browser pane refuses the API listener `/status` reports (and 8405),
    the HUD's own port, and anything not local; nothing ever reaches the
    mock's API listener;
  - the frame's `sandbox` is exactly `allow-scripts allow-forms`, and exactly
    `allow-scripts allow-forms allow-same-origin` with the switch on — never
    top navigation, popups or downloads;
  - the switch is offered only for a local dev server: never the workshop,
    the HUD, the API, a non-local URL, or under a daemon whose `/status`
    lacks `frame_hardened`;
  - on, the dev page really keeps its origin (its storage works), and the
    choice survives a reload, judged again; another port, host or scheme
    turns it off, and so does nothing else;
  - a stored grant that is no longer allowed (the daemon now reports that
    port as its own, or no longer says it is frame-hardened) is dropped;
  - under an authorization card the switch is disabled and inert;
  - a keep-origin page that sends its own frame to the workshop lands
    opaque — the workshop sandboxes every document it serves (PR #31
    review: it used to run with the real workshop origin);
  - the HUD under test is served under the daemon's own frame headers (the
    mock sends `hud_api.frame_headers()`, never a copy);
  - the rename (BR-3a): a layout stored before it, its Preview pane in a slot
    whose default is chat, opens as the Browser with its URL and its grant,
    and is saved back under the new names only; an old grant for another
    origin is not carried over; a stored `"file"` pane still opens as the
    editor with its tabs; the strip says Editor and Browser.
"""
from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.face.hud_v2_layout_check import (  # noqa: E402
    _active_testid, _approval, _boot, _choose, _pane, _set_storage, _set_ws, _visible, _ws,
)

OFF = "allow-scripts allow-forms"
ON = "allow-scripts allow-forms allow-same-origin"
# Every console message the page writes, (type, text): the frame guard's
# sections read it for React errors and for its own one line.
CONSOLE: list[tuple[str, str]] = []
FRAME = _pane(2, '[data-testid="browser-frame"]')
KEEP = _pane(2, '[data-testid="browser-keep-origin"]')
REFUSED = _pane(2, '[data-testid="browser-refused"]')


def browser_checks(browser, mock, base, check, until, guard, init_script):
    print("\nbrowser: the daemon's ports, keep its own origin (WP-E), the rename (BR-3a)")
    ctx = browser.new_context(viewport={"width": 1600, "height": 900}, permissions=["microphone"])
    guard(ctx)
    ctx.add_init_script(init_script)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_default_timeout(5000)
    try:
        _boot(page, mock, base, until)
        page.on("console", lambda m: CONSOLE.append((m.type, m.text)))
        for section in (_port_checks, _keep_checks, _stored_checks, _hardened_checks, _card_checks,
                        _workshop_hijack_checks, _served_under_checks, _split_checks, _rename_checks,
                        _guard_mount_checks, _guard_timer_checks, _guard_hidden_checks, _guard_card_checks,
                        _guard_frame_to_frame_checks, _guard_parked_pointer_checks, _guard_gesture_checks,
                        _guard_reload_checks):
            try:
                section(page, mock, check, until)
            except Exception as e:  # a section that cannot run is a failure; the rest still run
                check(f"{section.__name__.strip('_')} ran to the end", False, str(e).splitlines()[0][:200])
                mock.frame_hardened = True
                try:
                    page.keyboard.press("Escape")
                    _setup(page, mock, until)
                except Exception:
                    pass
        check("no page errors in the browser section", not errors, "; ".join(errors[:3]))
        check("nothing ever loaded the daemon's API listener", mock.api_hits == [], str(mock.api_hits[:3]))
    finally:
        mock.frame_hardened = True
        ctx.close()


# ---------------------------------------------------------------------------

def _setup(page, mock, until, pane2: dict | None = None):
    """Two columns: a chat, and a Browser pane (focused) with `pane2`'s stored fields."""
    raw = json.dumps({"preset": "cols2", "focused": 2,
                      "panes": [{"view": "chat"}, {"view": "browser", **(pane2 or {})},
                                {"view": "file"}, {"view": "task"}]})
    page.set_viewport_size({"width": 1600, "height": 900})
    _set_storage(page, zoom="100", layout="{}")
    _set_ws(page, raw)
    _boot(page, mock, None, until, reload=True)
    until(lambda: _visible(page, _pane(2, '[data-testid="browser-url"]')), timeout=3)


def _load(page, url: str):
    page.locator(_pane(2, '[data-testid="browser-url"]')).fill(url)
    page.locator(_pane(2, '[data-testid="browser-go"]')).click()
    page.wait_for_timeout(150)


def _count(page, sel: str) -> int:
    return page.locator(sel).count()


def _sandbox(page):
    loc = page.locator(FRAME)
    return loc.first.get_attribute("sandbox") if loc.count() else None


def _src(page):
    loc = page.locator(FRAME)
    return loc.first.get_attribute("src") if loc.count() else None


def _keep_checked(page):
    loc = page.locator(KEEP)
    return loc.count() > 0 and loc.first.is_checked()


def _stored(page):
    return _ws(page)["ws"]["panes"][1].get("keepOrigin")


def _inside(page, sel: str) -> str:
    """Text inside the Browser frame (Playwright reads any origin's frame)."""
    try:
        return page.frame_locator(FRAME).locator(sel).inner_text(timeout=1000)
    except Exception:
        return ""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_checks(page, mock, check, until):
    _setup(page, mock, until)
    for what, url, word in (
            ("the API listener /status reported", f"http://127.0.0.1:{mock.api_port}/", "API"),
            ("the same API listener as localhost", f"http://localhost:{mock.api_port}/status", "API"),
            ("the live daemon's API port (8405)", "http://127.0.0.1:8405/", "API"),
            ("the HUD's own port", f"http://127.0.0.1:{mock.port}/", "HUD")):
        _load(page, url)
        until(lambda: _count(page, REFUSED) > 0, timeout=3)
        text = page.locator(REFUSED).inner_text() if _count(page, REFUSED) else ""
        check(f"the Browser refuses {what}, and says why",
              word in text and _count(page, FRAME) == 0 and _count(page, KEEP) == 0, text)
    check("and nothing reached the API listener", mock.api_hits == [], str(mock.api_hits))

    page.locator(_pane(2, '[data-testid="browser-project"]')).click()
    until(lambda: _count(page, FRAME) > 0, timeout=4)
    check("the workshop loads, its sandbox exactly allow-scripts allow-forms", _sandbox(page) == OFF,
          str(_sandbox(page)))
    page.wait_for_timeout(200)
    check("and never offers to keep its origin (it is one of the daemon's three)", _count(page, KEEP) == 0)
    _load(page, f"http://localhost:{mock.workshop_port}/p/p1/index.html")
    until(lambda: _count(page, FRAME) > 0, timeout=4)
    page.wait_for_timeout(200)
    check("not as localhost either", _count(page, KEEP) == 0 and _sandbox(page) == OFF)
    _load(page, "https://example.com/")
    until(lambda: _count(page, REFUSED) > 0, timeout=3)
    check("a URL that is not local is refused, with no switch", _count(page, FRAME) == 0 and _count(page, KEEP) == 0)


def _keep_checks(page, mock, check, until):
    _setup(page, mock, until)
    dev = f"http://127.0.0.1:{mock.dev_port}/"
    origin = f"http://127.0.0.1:{mock.dev_port}"
    hits = len(mock.dev_hits)
    _load(page, dev)
    until(lambda: _count(page, FRAME) > 0 and _count(page, KEEP) > 0, timeout=4)
    check("a local dev server loads, its sandbox exactly allow-scripts allow-forms", _sandbox(page) == OFF,
          str(_sandbox(page)))
    check("and the pane offers 'Keep its origin', off by default",
          _visible(page, KEEP) and not _keep_checked(page) and page.locator(KEEP).get_attribute("data-origin") == origin)
    until(lambda: _inside(page, "#storage") not in ("", "?"), timeout=4)
    check("off, the page runs with an opaque origin: its storage throws",
          _inside(page, "#storage") == "blocked" and _inside(page, "#origin") == "null",
          f"{_inside(page, '#storage')} {_inside(page, '#origin')}")
    check("and the page came from the dev server", len(mock.dev_hits) > hits)

    page.locator(KEEP).check()
    until(lambda: _sandbox(page) == ON, timeout=3)
    check("on, the sandbox is exactly allow-scripts allow-forms allow-same-origin", _sandbox(page) == ON,
          str(_sandbox(page)))
    until(lambda: _inside(page, "#storage") == "ok", timeout=4)
    check("and the page keeps its own origin: its storage works",
          _inside(page, "#storage") == "ok" and _inside(page, "#origin") == origin,
          f"{_inside(page, '#storage')} {_inside(page, '#origin')}")
    check("the switch is stored with the pane, as the origin it covers", _stored(page) == origin, str(_stored(page)))

    _boot(page, mock, None, until, reload=True)
    until(lambda: _sandbox(page) == ON, timeout=4)
    check("after a reload it is judged again and still applies",
          _sandbox(page) == ON and _keep_checked(page) and _src(page) == dev, f"{_sandbox(page)} {_src(page)}")

    _load(page, dev + "other/page")
    until(lambda: _src(page) == dev + "other/page", timeout=3)
    check("another page on the same origin keeps it", _sandbox(page) == ON and _stored(page) == origin)

    for what, url in (("host (localhost for 127.0.0.1)", f"http://localhost:{mock.dev_port}/"),
                      ("port", f"http://127.0.0.1:{_free_port()}/"),
                      ("scheme", f"https://127.0.0.1:{mock.dev_port}/")):
        if not _keep_checked(page):
            _load(page, dev)
            until(lambda: _count(page, KEEP) > 0, timeout=3)
            page.locator(KEEP).check()
            until(lambda: _sandbox(page) == ON and _stored(page) == origin, timeout=3)
        _load(page, url)
        until(lambda: _src(page) == url, timeout=3)
        page.wait_for_timeout(150)
        check(f"another {what} turns it off: the frame, the switch and the store",
              _sandbox(page) == OFF and not _keep_checked(page) and _stored(page) is None,
              f"{_sandbox(page)} {_keep_checked(page)} {_stored(page)}")

    _load(page, dev)
    until(lambda: _count(page, KEEP) > 0, timeout=3)
    page.locator(KEEP).check()
    until(lambda: _sandbox(page) == ON, timeout=3)
    page.locator(KEEP).uncheck()
    until(lambda: _sandbox(page) == OFF, timeout=3)
    check("unchecked, it is off and forgotten", _sandbox(page) == OFF and _stored(page) is None)

    # Review of PR #40: Space is no longer swallowed for push-to-talk, and a
    # grant the owner just clicked must not sit under a habitual one (Space on
    # a focused checkbox flips it). Focus starts nowhere.
    page.evaluate("document.activeElement && document.activeElement.blur()")
    page.locator(KEEP).check()
    until(lambda: _sandbox(page) == ON, timeout=3)
    focused = _active_testid(page)
    page.keyboard.down("Space")
    page.wait_for_timeout(100)
    page.keyboard.up("Space")
    page.wait_for_timeout(300)
    check("review #40: a click leaves no focus on 'Keep its origin', so a Space after it flips nothing",
          focused != "browser-keep-origin" and _keep_checked(page) and _sandbox(page) == ON,
          f"focus={focused} checked={_keep_checked(page)} sandbox={_sandbox(page)}")
    page.locator(KEEP).uncheck()
    until(lambda: _sandbox(page) == OFF, timeout=3)


def _stored_checks(page, mock, check, until):
    for what, port, word in (("the API listener", mock.api_port, "API"), ("the HUD", mock.port, "HUD")):
        origin = f"http://127.0.0.1:{port}"
        _setup(page, mock, until, {"browserUrl": origin + "/", "keepOrigin": origin})
        until(lambda: _count(page, REFUSED) > 0, timeout=4)
        text = page.locator(REFUSED).inner_text() if _count(page, REFUSED) else ""
        check(f"a stored URL on {what}'s port is refused when it is loaded again",
              word in text and _count(page, FRAME) == 0, text)
        until(lambda: _stored(page) is None, timeout=3)
        check(f"and its stored grant is dropped, not kept for later", _stored(page) is None, str(_stored(page)))
    check("and nothing reached the API listener", mock.api_hits == [], str(mock.api_hits))


def _hardened_checks(page, mock, check, until):
    dev = f"http://127.0.0.1:{mock.dev_port}/"
    origin = f"http://127.0.0.1:{mock.dev_port}"
    mock.frame_hardened = False
    try:
        _setup(page, mock, until, {"browserUrl": dev, "keepOrigin": origin})
        until(lambda: _count(page, FRAME) > 0, timeout=4)
        page.wait_for_timeout(300)
        check("a daemon whose /status lacks frame_hardened: the frame never gets allow-same-origin",
              _sandbox(page) == OFF, str(_sandbox(page)))
        check("and the switch is not offered at all", _count(page, KEEP) == 0)
        until(lambda: _stored(page) is None, timeout=3)
        check("and the stored grant is dropped", _stored(page) is None, str(_stored(page)))
        until(lambda: _inside(page, "#storage") not in ("", "?"), timeout=4)
        check("so the page's storage throws", _inside(page, "#storage") == "blocked")
    finally:
        mock.frame_hardened = True
    _setup(page, mock, until, {"browserUrl": dev})
    until(lambda: _count(page, KEEP) > 0, timeout=4)
    check("the same daemon saying it again offers the switch again", _visible(page, KEEP) and not _keep_checked(page))


def _workshop_hijack_checks(page, mock, check, until):
    """A page let keep its origin navigates its own frame to the workshop. The
    frame keeps the flags it was given (allow-same-origin among them), so
    without the workshop's own sandbox the agent-written page there ran with
    the real workshop origin: its storage, and same-origin reads of any
    project's served files (PR #31 review, shown in Chromium)."""
    dev = f"http://127.0.0.1:{mock.dev_port}"
    hits = len(mock.workshop_hits)
    _setup(page, mock, until, {"browserUrl": dev + "/to-workshop", "keepOrigin": dev})
    until(lambda: _sandbox(page) == ON, timeout=4)
    check("setup: the dev page's frame was let keep its origin", _sandbox(page) == ON, str(_sandbox(page)))
    until(lambda: "/p/p1/probe.html" in mock.workshop_hits[hits:], timeout=4)
    check("setup: the dev page sent its frame to the workshop", "/p/p1/probe.html" in mock.workshop_hits[hits:],
          str(mock.workshop_hits[hits:]))
    until(lambda: _inside(page, "#origin") not in ("", "?"), timeout=4)
    origin, storage = _inside(page, "#origin"), _inside(page, "#storage")
    check("the workshop page there runs with an opaque origin, not the workshop's",
          origin == "null", f"origin {origin}")
    check("so its storage throws", storage == "blocked", f"storage {storage}")


def _served_under_checks(page, mock, check, until):
    """The HUD under test is rendered under the daemon's own frame headers."""
    got = page.evaluate("""async () => {
      const r = await fetch('/status');
      return [r.headers.get('x-frame-options'), r.headers.get('content-security-policy')];
    }""")
    from jarvis.v2.hud_api import content_security_policy
    check("the mock HUD sends the daemon's frame headers (hud_api.frame_headers): DENY and the full CSP",
          got == ["DENY", content_security_policy()] and got[1].endswith("frame-ancestors 'none'")
          and "script-src 'self' 'wasm-unsafe-eval';" in got[1], str(got))


def _card_checks(page, mock, check, until):
    _setup(page, mock, until, {"browserUrl": f"http://127.0.0.1:{mock.dev_port}/"})
    until(lambda: _count(page, KEEP) > 0, timeout=4)
    _approval(mock, "keep1")
    until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
    page.wait_for_timeout(150)
    check("under a card the switch is disabled", page.locator(KEEP).is_disabled())
    check("and unreachable: it sits behind the inert veil",
          page.evaluate("s => !!document.querySelector(s).closest('[inert]')", KEEP))
    page.keyboard.press("Escape")
    body = until(lambda: mock.sent("POST", "/approvals/keep1") or None, timeout=4)
    check("Escape still denies the card", bool(body) and body[-1].get("decision") == "deny")
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
    page.wait_for_timeout(100)
    check("and once it goes, the switch is back, still off",
          not page.locator(KEEP).is_disabled() and not _keep_checked(page) and _sandbox(page) == OFF)


def _split_checks(page, mock, check, until):
    """WP-G2 (plan §3.5): a Browser pane's copy loads the same URL, judged
    again, and never inherits the keep-origin grant — that is the owner's
    choice for one pane."""
    _setup(page, mock, until)
    dev = f"http://127.0.0.1:{mock.dev_port}/"
    origin = f"http://127.0.0.1:{mock.dev_port}"
    _load(page, dev)
    until(lambda: _count(page, KEEP) > 0, timeout=4)
    page.locator(KEEP).check()
    until(lambda: _sandbox(page) == ON and _stored(page) == origin, timeout=3)
    page.locator('[data-testid="pane-2-split"]').click()
    until(lambda: len(_ws(page)["ws"]["order"]) == 3, timeout=3)
    new = _ws(page)["ws"]["order"][2]           # right after pane 2; a free id (the stored 3 and 4 are kept)
    frame3 = _pane(new, '[data-testid="browser-frame"]')
    until(lambda: _count(page, frame3) > 0, timeout=4)
    keep3 = page.locator(_pane(new, '[data-testid="browser-keep-origin"]'))
    spec3 = _ws(page)["ws"]["panes"][new - 1]
    check("G2: a Browser pane's copy loads the same URL beside it",
          page.locator(frame3).get_attribute("src") == dev and spec3.get("browserUrl") == dev, str(spec3))
    check("G2: without the source's keep-origin grant: its frame is sandboxed as any page is, the switch off",
          page.locator(frame3).get_attribute("sandbox") == OFF and "keepOrigin" not in spec3
          and keep3.count() == 1 and not keep3.is_checked(), str(page.locator(frame3).get_attribute("sandbox")))
    check("G2: and the source keeps its own", _sandbox(page) == ON and _stored(page) == origin)


def _raw_ws(page) -> str:
    return page.evaluate("localStorage.getItem('jarvis.hud.workspace') || ''")


def _rename_checks(page, mock, check, until):
    """BR-3a: Preview became the Browser in name and id, under the same
    storage key. A layout stored before the rename puts its Preview pane in
    pane 1, whose default is chat, so a dropped view migration shows a chat
    there rather than passing on pane 2's default."""
    dev = f"http://127.0.0.1:{mock.dev_port}/"
    origin = f"http://127.0.0.1:{mock.dev_port}"
    frame1 = _pane(1, '[data-testid="browser-frame"]')

    def boot_with(pane1: dict):
        raw = json.dumps({"preset": "cols2", "focused": 1,
                          "panes": [{"view": "preview", **pane1}, {"view": "chat"},
                                    {"view": "file"}, {"view": "task"}]})
        page.set_viewport_size({"width": 1600, "height": 900})
        _set_storage(page, zoom="100", layout="{}")
        _set_ws(page, raw)
        _boot(page, mock, None, until, reload=True)

    def sandbox1():
        loc = page.locator(frame1)
        return loc.first.get_attribute("sandbox") if loc.count() else None

    boot_with({"previewUrl": dev, "keepOrigin": origin})
    until(lambda: page.locator(frame1).count() > 0, timeout=4)
    until(lambda: sandbox1() == ON, timeout=4)
    spec = _ws(page)["ws"]["panes"][0]
    check("BR-3a: a stored Preview pane opens as the Browser",
          page.locator(_pane(1)).get_attribute("data-view") == "browser"
          and "on" in (page.locator(_pane(1, '[data-testid="tab-browser"]')).get_attribute("class") or ""),
          str(page.locator(_pane(1)).get_attribute("data-view")))
    check("BR-3a: with its old URL loaded, judged again",
          page.locator(frame1).first.get_attribute("src") == dev and spec.get("browserUrl") == dev, str(spec))
    check("BR-3a: and its keep-origin grant, which matches that URL", sandbox1() == ON
          and spec.get("keepOrigin") == origin, f"{sandbox1()} {spec}")
    until(lambda: '"browserUrl"' in _raw_ws(page), timeout=3)
    raw = _raw_ws(page)
    saved = json.loads(raw)["panes"][0] if raw else {}
    check("BR-3a: the window saves it under the new names only",
          "preview" not in raw and saved.get("view") == "browser" and saved.get("browserUrl") == dev, raw[:300])

    other = f"http://127.0.0.1:{_free_port()}"
    boot_with({"previewUrl": dev, "keepOrigin": other})
    until(lambda: page.locator(frame1).count() > 0, timeout=4)
    page.wait_for_timeout(300)
    spec = _ws(page)["ws"]["panes"][0]
    check("BR-3a: an old grant for another origin is not carried over",
          sandbox1() == OFF and "keepOrigin" not in spec and '"keepOrigin"' not in _raw_ws(page),
          f"{sandbox1()} {spec}")

    strip = page.locator(_pane(1, '[data-testid="tab-browser"]')).inner_text().strip()
    editor = page.locator(_pane(1, '[data-testid="tab-file"]')).inner_text().strip()
    check("the strip says Browser and Editor (the editor's id stays file)",
          strip == "Browser" and editor == "Editor", f"{strip!r} {editor!r}")
    check("and no view is called Preview or File any more",
          not any(t.strip() in ("Preview", "File")
                  for t in page.locator(_pane(1, '[data-testid="pane-1-view"] button')).all_inner_texts()),
          str(page.locator(_pane(1, '[data-testid="pane-1-view"] button')).all_inner_texts()))
    check("the pane's label says Browser", page.locator(_pane(1)).get_attribute("aria-label") == "Pane 1: browser",
          str(page.locator(_pane(1)).get_attribute("aria-label")))

    # The guard: a stored "file" pane, as every layout before this one stored
    # it, still opens as the editor with its tabs.
    tabs = {"paths": ["calc.py", "README.md"], "active": "calc.py", "preview": None}
    raw = json.dumps({"preset": "cols2", "focused": 2,
                      "panes": [{"view": "chat"}, {"view": "file", "projectId": "p1", "editors": tabs},
                                {"view": "file"}, {"view": "task"}]})
    _set_ws(page, raw)
    _boot(page, mock, None, until, reload=True)
    tab = lambda path: page.locator(_pane(2, f'[data-testid="editor-tab-{path}"]'))  # noqa: E731
    until(lambda: tab("calc.py").count() == 1 and tab("README.md").count() == 1, timeout=5)
    until(lambda: page.locator(_pane(2, '[data-testid="editor"][data-language]')).count() > 0, timeout=8)
    check("a stored \"file\" pane still opens as the editor, with its tabs",
          page.locator(_pane(2)).get_attribute("data-view") == "file"
          and tab("README.md").count() == 1 and tab("calc.py").get_attribute("data-active") == "true"
          and page.locator(_pane(2, '[data-testid="editor"][data-language]')).count() > 0,
          str(page.locator(_pane(2)).get_attribute("data-view")))
    check("and reads as the editor", page.locator(_pane(2)).get_attribute("aria-label") == "Pane 2: editor"
          and page.locator(_pane(2, '[data-testid="tab-file"]')).inner_text().strip() == "Editor",
          str(page.locator(_pane(2)).get_attribute("aria-label")))
    saved = json.loads(_raw_ws(page))["panes"][1]
    check("and is saved back as \"file\", tabs and all",
          saved.get("view") == "file" and saved.get("editors", {}).get("paths") == ["calc.py", "README.md"],
          str(saved))


# ---------------------------------------------------------------------------
# No frame takes the keyboard (R-1; amendment 2 §E.8, §H AO-2). The mock's dev
# server serves pages that focus themselves (hud_v2_mock FOCUS_PAGE) and log
# every key they receive to `/keys` (`mock.dev_keys`).

INPUT1 = _pane(1, '[data-testid="input"]')
STOLEN = _pane(2, '[data-testid="browser-stolen"]')
RELOAD = _pane(2, '[data-testid="browser-stolen-reload"]')
SHIELD = _pane(2, '[data-testid="browser-shield"]')


def _focus_url(mock, mode: str, **q) -> str:
    query = "&".join(f"{k}={v}" for k, v in q.items())
    return f"http://127.0.0.1:{mock.dev_port}/{mode}" + (f"?{query}" if query else "")


def _letters(n: int, start: int = 0) -> str:
    return "".join(chr(97 + (start + i) % 26) for i in range(n))


def _active_frame(page) -> str | None:
    """The `data-browser-frame` of the iframe holding the keyboard, if one is."""
    return page.evaluate("(() => { const a = document.activeElement;"
                         " return a && a.tagName === 'IFRAME' ? a.getAttribute('data-browser-frame') : null; })()")


def _centre(page, sel: str) -> tuple[float, float]:
    box = page.locator(sel).first.bounding_box()
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def _guard_lines(since: int) -> list[str]:
    return [text for kind, text in CONSOLE[since:] if "frame guard" in text]


def _react_errors(since: int) -> list[str]:
    return [text for kind, text in CONSOLE[since:]
            if kind == "error" and any(w in text for w in ("removeChild", "NotFoundError", "React", "Uncaught"))]


def _away(page):
    """The pointer somewhere that is no frame: over the sidebar."""
    x, y = _centre(page, '[data-testid="sidebar"]')
    page.mouse.move(x, y)


def _guard_mount_checks(page, mock, check, until):
    """A page that focuses itself as it is parsed, restored at mount with no
    gesture and arriving while the owner types in the chat box."""
    keys, con = len(mock.dev_keys), len(CONSOLE)
    url = _focus_url(mock, "focus-mount", delay=1500)
    _setup(page, mock, until, {"browserUrl": url})
    _away(page)
    box = page.locator(INPUT1)
    box.click()
    box.fill("")
    text = _letters(50)
    page.keyboard.type(text, delay=60)
    until(lambda: _count(page, STOLEN) > 0, timeout=5)
    page.wait_for_timeout(200)
    check("setup: the page that focuses on mount was served", any(p.startswith("/focus-mount") for p in mock.dev_hits))
    check("R-1: a page that focused itself on mount, restored with no gesture, is removed and the pane says so",
          _count(page, FRAME) == 0 and _count(page, STOLEN) == 1
          and "This page took the keyboard" in page.locator(STOLEN).inner_text()
          and _count(page, RELOAD) == 1, f"frames={_count(page, FRAME)} stolen={_count(page, STOLEN)}")
    check("R-1: the chat box holds all 50 characters typed, in order", box.input_value() == text,
          repr(box.input_value()))
    check("R-1: and the page's server received none of them", mock.dev_keys[keys:] == [], str(mock.dev_keys[keys:][:5]))
    check("R-1: focus is back in the chat box", _active_testid(page) == "input", str(_active_testid(page)))
    check("R-1: the tab and its URL stay (only the frame went)",
          _ws(page)["ws"]["panes"][1].get("browserUrl") == url, str(_ws(page)["ws"]["panes"][1]))
    lines = _guard_lines(con)
    check("R-1: one console line, naming the pane slot and never the URL",
          len(lines) == 1 and "pane 2" in lines[0] and "127.0.0.1" not in lines[0] and "focus-" not in lines[0]
          and str(mock.dev_port) not in lines[0], str(lines))
    check("R-1: no React error from the removal", _react_errors(con) == [], str(_react_errors(con)[:2]))


def _guard_timer_checks(page, mock, check, until):
    """A page that grabs focus on a timer, drawn beside a chat and an editor
    holding a fake .env buffer: every key lands where it was typed."""
    keys = len(mock.dev_keys)
    url = _focus_url(mock, "focus-timer", after=800)
    raw = json.dumps({"preset": "cols3", "focused": 1,
                      "panes": [{"view": "chat"}, {"view": "task", "browserUrl": url},
                                {"view": "file", "projectId": "p1",
                                 "editors": {"paths": [".env"], "active": ".env", "preview": None}},
                                {"view": "task"}]})
    page.set_viewport_size({"width": 2400, "height": 1000})
    _set_storage(page, zoom="100", layout="{}")
    _set_ws(page, raw)
    _boot(page, mock, None, until, reload=True)
    lines3 = _pane(3, '[data-testid="editor"] .view-lines')
    until(lambda: page.locator(_pane(3, '[data-testid="editor"][data-language]')).count() > 0, timeout=10)
    # Pane 2 becomes the Browser now, restoring its stored URL (no gesture).
    page.locator(_pane(2, '[data-testid="tab-browser"]')).click()
    until(lambda: _count(page, FRAME) > 0, timeout=4)
    _away(page)
    box = page.locator(INPUT1)
    box.click()
    box.fill("")
    chat = _letters(100)
    page.keyboard.type(chat, delay=25)
    until(lambda: _count(page, STOLEN) > 0, timeout=4)
    check("R-1: a page grabbing focus on a timer is removed on its first grab",
          _count(page, FRAME) == 0 and _count(page, STOLEN) == 1)
    check("R-1: the chat box holds all 100 characters, in order", box.input_value() == chat,
          repr(box.input_value()))

    # Again while the owner types in the editor, on the fake .env buffer. The
    # Reload is the owner's own load, but the page grabs after it has lapsed.
    page.locator(_pane(2, '[data-testid="browser-stolen-reload"]')).click()
    until(lambda: _count(page, FRAME) > 0, timeout=4)
    stolen_before = _count(page, STOLEN)
    page.locator(lines3).click()
    page.keyboard.press("Control+End")
    ed = _letters(100, start=7)
    page.keyboard.type(ed, delay=40)
    until(lambda: _count(page, STOLEN) > 0, timeout=4)
    page.wait_for_timeout(200)
    text3 = page.locator(lines3).inner_text().replace(" ", " ")
    check("R-1: reloaded, it grabs again while the owner types in the .env buffer, and is removed",
          stolen_before == 0 and _count(page, FRAME) == 0 and _count(page, STOLEN) == 1)
    check("R-1: the editor holds all 100 characters, in order", ed in text3, repr(text3[-140:]))
    check("R-1: focus is back in the editor", page.evaluate("!!document.activeElement.closest('.monaco-editor')"))
    check("R-1: the page's server received none of the 200 keys", mock.dev_keys[keys:] == [],
          str(mock.dev_keys[keys:][:5]))


def _guard_hidden_checks(page, mock, check, until):
    """A Browser frame ⊞ hid (mounted, `display: none`) that grabs focus is
    removed; the drawn Browser pane is untouched."""
    keys = len(mock.dev_keys)
    plain = _focus_url(mock, "focus-none")
    url = _focus_url(mock, "focus-timer", after=3000)
    raw = json.dumps({"preset": "cols3", "focused": 1,
                      "panes": [{"view": "chat"}, {"view": "browser", "browserUrl": plain},
                                {"view": "browser", "browserUrl": url}, {"view": "task"}]})
    page.set_viewport_size({"width": 2400, "height": 1000})
    _set_storage(page, zoom="100", layout="{}")
    _set_ws(page, raw)
    _boot(page, mock, None, until, reload=True)
    frame3 = _pane(3, '[data-testid="browser-frame"]')
    until(lambda: _count(page, FRAME) > 0 and _count(page, frame3) > 0, timeout=4)
    _choose(page, until, "cols2")
    check("setup: pane 3 is hidden by ⊞, its frame still mounted",
          not _visible(page, _pane(3)) and _count(page, frame3) == 1)
    _away(page)
    box = page.locator(INPUT1)
    box.click()
    box.fill("")
    text = _letters(70)
    page.keyboard.type(text, delay=50)
    until(lambda: _count(page, _pane(3, '[data-testid="browser-stolen"]')) > 0, timeout=4)
    page.wait_for_timeout(200)
    check("R-1: the hidden frame that grabbed focus is removed",
          _count(page, frame3) == 0 and _count(page, _pane(3, '[data-testid="browser-stolen"]')) == 1)
    check("R-1: the drawn Browser pane is untouched", _count(page, FRAME) == 1 and _src(page) == plain, str(_src(page)))
    check("R-1: the chat box holds every key", box.input_value() == text, repr(box.input_value()))
    check("R-1: and the hidden page's server received none", mock.dev_keys[keys:] == [], str(mock.dev_keys[keys:][:5]))


def _guard_card_checks(page, mock, check, until):
    """Under a card, a frame grabbing focus with the owner's pointer over it is
    removed: the card keeps focus and Escape still denies."""
    keys = len(mock.dev_keys)
    _setup(page, mock, until, {"browserUrl": _focus_url(mock, "focus-timer", after=2500)})
    until(lambda: _count(page, FRAME) > 0, timeout=4)
    x, y = _centre(page, FRAME)
    page.mouse.move(x - 20, y - 20)
    page.mouse.move(x, y)
    until(lambda: page.locator(SHIELD).get_attribute("data-up") == "false", timeout=2)
    check("setup: the pointer is over the page (its shield stepped aside)",
          page.locator(SHIELD).get_attribute("data-up") == "false")
    _approval(mock, "guard1")
    until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
    until(lambda: _count(page, STOLEN) > 0, timeout=5)
    page.wait_for_timeout(200)
    check("R-1: under a card, a frame that grabs focus is removed, pointer over it or not",
          _count(page, FRAME) == 0 and _count(page, STOLEN) == 1)
    check("R-1: the card keeps focus", _active_testid(page) == "approval-card", str(_active_testid(page)))
    page.keyboard.type("zz")
    page.wait_for_timeout(200)
    check("R-1: and the page's server received no key", mock.dev_keys[keys:] == [], str(mock.dev_keys[keys:][:5]))
    check("R-1: the notice's Reload is disabled and inert under the card",
          page.locator(RELOAD).is_disabled() and page.evaluate("s => !!document.querySelector(s).closest('[inert]')", RELOAD))
    page.keyboard.press("Escape")
    body = until(lambda: mock.sent("POST", "/approvals/guard1") or None, timeout=4)
    check("R-1: Escape still denies the card", bool(body) and body[-1].get("decision") == "deny")
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
    page.wait_for_timeout(100)
    check("R-1: once the card goes, Reload is offered again", not page.locator(RELOAD).is_disabled())


def _guard_frame_to_frame_checks(page, mock, check, until):
    """The keyboard already in a page the owner clicked into (so the HUD
    window is already blurred and no `blur` fires): another page grabbing it
    is removed, and focus returns to the HUD element the owner last used."""
    keys = len(mock.dev_keys)
    plain = _focus_url(mock, "focus-none")
    url = _focus_url(mock, "focus-timer", after=3500)
    raw = json.dumps({"preset": "cols3", "focused": 1,
                      "panes": [{"view": "chat"}, {"view": "browser", "browserUrl": plain},
                                {"view": "browser", "browserUrl": url}, {"view": "task"}]})
    page.set_viewport_size({"width": 2400, "height": 1000})
    _set_storage(page, zoom="100", layout="{}")
    _set_ws(page, raw)
    _boot(page, mock, None, until, reload=True)
    frame3 = _pane(3, '[data-testid="browser-frame"]')
    until(lambda: _count(page, FRAME) > 0 and _count(page, frame3) > 0, timeout=4)
    _away(page)
    box = page.locator(INPUT1)
    box.click()
    box.fill("")
    x, y = _centre(page, FRAME)
    page.mouse.click(x, y)
    until(lambda: _active_frame(page) == "2:0", timeout=3)
    check("setup: a click put the keyboard in the page in pane 2", _active_frame(page) == "2:0", str(_active_frame(page)))
    page.keyboard.type("ab", delay=30)
    until(lambda: len(mock.dev_keys) - keys >= 2, timeout=3)
    check("setup: that page gets the owner's keys", len(mock.dev_keys) - keys == 2
          and all(k.startswith("/keys?p=%2Ffocus-none") for k in mock.dev_keys[keys:]), str(mock.dev_keys[keys:]))
    until(lambda: _count(page, _pane(3, '[data-testid="browser-stolen"]')) > 0, timeout=6)
    page.wait_for_timeout(200)
    check("R-1: the page in pane 3 grabbing the keyboard from pane 2's is removed",
          _count(page, frame3) == 0 and _count(page, _pane(3, '[data-testid="browser-stolen"]')) == 1)
    check("R-1: the page in pane 2 is untouched", _count(page, FRAME) == 1)
    check("R-1: focus returns to the HUD element the owner last used (the chat box)",
          _active_testid(page) == "input", str(_active_testid(page)))
    page.keyboard.type("cd", delay=30)
    page.wait_for_timeout(300)
    check("R-1: the next keys land there", box.input_value() == "cd", repr(box.input_value()))
    check("R-1: and the grabbing page's server received none",
          not any("focus-timer" in k for k in mock.dev_keys[keys:]), str(mock.dev_keys[keys:]))


def _guard_parked_pointer_checks(page, mock, check, until):
    """A pointer left resting over a page stops counting at the owner's first
    key in the HUD (a decision narrower than the plan's stated limit)."""
    keys = len(mock.dev_keys)
    _setup(page, mock, until, {"browserUrl": _focus_url(mock, "focus-timer", after=1500)})
    until(lambda: _count(page, FRAME) > 0, timeout=4)
    _away(page)
    box = page.locator(INPUT1)
    box.click()
    box.fill("")
    x, y = _centre(page, FRAME)
    page.mouse.move(x - 20, y - 20)
    page.mouse.move(x, y)
    until(lambda: page.locator(SHIELD).get_attribute("data-up") == "false", timeout=2)
    check("setup: the pointer rests over the page (its shield stepped aside)",
          page.locator(SHIELD).get_attribute("data-up") == "false")
    text = _letters(50, start=3)
    page.keyboard.type(text[:1])
    check("the owner's first key in the HUD brings the shield back",
          page.locator(SHIELD).get_attribute("data-up") == "true" if _count(page, SHIELD) else False)
    page.keyboard.type(text[1:], delay=60)
    until(lambda: _count(page, STOLEN) > 0, timeout=4)
    page.wait_for_timeout(200)
    check("R-1: a page grabbing focus under a parked pointer while the owner types is removed",
          _count(page, FRAME) == 0 and _count(page, STOLEN) == 1)
    check("R-1: the chat box holds every key", box.input_value() == text, repr(box.input_value()))
    check("R-1: the page's server received none", mock.dev_keys[keys:] == [], str(mock.dev_keys[keys:][:5]))


def _guard_gesture_checks(page, mock, check, until):
    """The owner's gestures still work: a click, the pointer over a page, Tab
    from the URL box, and their own load."""
    plain = _focus_url(mock, "focus-none")
    _setup(page, mock, until, {"browserUrl": plain})
    until(lambda: _count(page, FRAME) > 0, timeout=4)
    _away(page)
    keys = len(mock.dev_keys)
    x, y = _centre(page, FRAME)
    page.mouse.click(x, y)
    until(lambda: _active_frame(page) == "2:0", timeout=3)
    page.keyboard.type("xyz", delay=30)
    until(lambda: len(mock.dev_keys) - keys >= 3, timeout=3)
    page.wait_for_timeout(300)
    check("gesture: clicking into a page gives it the keyboard, and keys reach it",
          _active_frame(page) == "2:0" and _count(page, FRAME) == 1 and _count(page, STOLEN) == 0
          and len(mock.dev_keys) - keys == 3, f"{_active_frame(page)} keys={len(mock.dev_keys) - keys}")

    # (Playwright's own `locator(frame).click()` checks the hit target before
    # it moves the mouse, so the shield reads as covering the frame: suites
    # click a frame with `page.mouse`, which moves first, as a hand does.)

    # Tab from the URL box, as many times as the toolbar needs.
    _away(page)
    page.locator(_pane(2, '[data-testid="browser-url"]')).click()
    for _ in range(6):
        page.keyboard.press("Tab")
        page.wait_for_timeout(80)
        if _active_frame(page):
            break
    keys = len(mock.dev_keys)
    page.keyboard.type("tt", delay=30)
    until(lambda: len(mock.dev_keys) - keys >= 2, timeout=3)
    check("gesture: Tab from the URL box into the page keeps it, and keys reach it",
          _active_frame(page) == "2:0" and _count(page, STOLEN) == 0 and len(mock.dev_keys) - keys == 2,
          f"{_active_frame(page)} keys={len(mock.dev_keys) - keys}")

    # The pointer over a page that grabs focus on a timer.
    _setup(page, mock, until, {"browserUrl": _focus_url(mock, "focus-timer", after=800)})
    until(lambda: _count(page, FRAME) > 0, timeout=4)
    x, y = _centre(page, FRAME)
    page.mouse.move(x - 20, y - 20)
    page.mouse.move(x, y)
    until(lambda: _active_frame(page) == "2:0", timeout=4)
    page.wait_for_timeout(600)
    keys = len(mock.dev_keys)
    page.keyboard.type("pq", delay=30)
    until(lambda: len(mock.dev_keys) - keys >= 2, timeout=3)
    check("gesture: with the pointer over it, a page that focuses itself keeps the keyboard",
          _active_frame(page) == "2:0" and _count(page, FRAME) == 1 and _count(page, STOLEN) == 0
          and len(mock.dev_keys) - keys == 2, f"{_active_frame(page)} keys={len(mock.dev_keys) - keys}")

    # The owner's own load: Enter in the URL box on a page that focuses on mount.
    _away(page)
    url_box = page.locator(_pane(2, '[data-testid="browser-url"]'))
    url_box.click()
    url_box.fill(_focus_url(mock, "focus-mount"))
    url_box.press("Enter")
    until(lambda: _active_frame(page) == "2:0", timeout=4)
    page.wait_for_timeout(300)
    check("gesture: a page the owner loaded with Enter may focus itself as it loads",
          _active_frame(page) == "2:0" and _count(page, FRAME) == 1 and _count(page, STOLEN) == 0)


def _guard_reload_checks(page, mock, check, until):
    """Reload on the notice is the owner's own load; after a removal there is
    no React error, and a fresh frame comes. A load the owner then moves on
    from no longer covers the page."""
    con = len(CONSOLE)
    _setup(page, mock, until, {"browserUrl": _focus_url(mock, "focus-load")})
    until(lambda: _count(page, STOLEN) > 0, timeout=5)
    check("R-1: a page that focuses on its load, restored with no gesture, is removed",
          _count(page, FRAME) == 0 and _count(page, STOLEN) == 1)
    check("R-1: no React error from the removal", _react_errors(con) == [], str(_react_errors(con)[:2]))
    keys = len(mock.dev_keys)
    page.locator(RELOAD).click()
    until(lambda: _active_frame(page) == "2:0", timeout=4)
    page.wait_for_timeout(300)
    check("R-1: Reload makes a fresh frame, and the page that focuses on load keeps it this time",
          _count(page, FRAME) == 1 and _count(page, STOLEN) == 0 and _active_frame(page) == "2:0")
    page.keyboard.type("rr", delay=30)
    until(lambda: len(mock.dev_keys) - keys >= 2, timeout=3)
    check("R-1: and the owner's keys reach it", len(mock.dev_keys) - keys == 2, str(mock.dev_keys[keys:]))

    # The owner loads a page, then goes back to the chat before it grabs.
    keys = len(mock.dev_keys)
    _away(page)
    url_box = page.locator(_pane(2, '[data-testid="browser-url"]'))
    url_box.click()
    url_box.fill(_focus_url(mock, "focus-timer", after=900))
    url_box.press("Enter")
    box = page.locator(INPUT1)
    box.click()
    box.fill("")
    text = _letters(40, start=11)
    page.keyboard.type(text, delay=50)
    until(lambda: _count(page, STOLEN) > 0, timeout=4)
    page.wait_for_timeout(200)
    check("R-1: the owner's load stops covering a page once they move on: it grabs, and is removed",
          _count(page, FRAME) == 0 and _count(page, STOLEN) == 1)
    check("R-1: the chat box holds every key", box.input_value() == text, repr(box.input_value()))
    check("R-1: the page's server received none", mock.dev_keys[keys:] == [], str(mock.dev_keys[keys:][:5]))


def main():
    from playwright.sync_api import sync_playwright
    from tests.face import hud_v2_check as v2
    from tests.face.hud_v2_mock import DIST, MockDaemon
    if not (DIST / "index.html").exists():
        print("hud/dist is not built. Run: cd hud && npm ci && npm run build")
        return 2
    mock = MockDaemon(0).start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"])
            browser_checks(browser, mock, f"http://127.0.0.1:{mock.port}", v2.check, v2.until,
                           v2.guard, v2.FAKE_RECOGNIZER)
            browser.close()
    finally:
        mock.stop()
    print()
    if v2.FAILURES:
        print(f"{len(v2.FAILURES)} of {v2.CHECKS} checks FAILED:")
        for f in v2.FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {v2.CHECKS} browser checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

