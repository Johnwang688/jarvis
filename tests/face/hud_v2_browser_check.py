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
    the HUD's own port, and non-HTTP schemes; Direct stays local and nothing reaches the
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
    editor with its tabs; the strip says Editor and Browser;
  - **no frame takes the keyboard on its own** (R-1; amendment 2 §E.8, §H
    AO-2): a page that focuses itself — on mount while the owner types, on a
    timer while they type in the chat and then in a `.env` buffer, from a pane
    ⊞ hid, under a card with the pointer over it, from another page the owner
    clicked into, under a pointer parked over it while the owner types, or
    after a load the owner has moved on from — is removed, the pane says so,
    every key lands where it was typed, focus comes back, and the page's
    server receives no key (`mock.dev_keys`); the owner's gestures still
    work (a click, the pointer over a page, Tab from the URL box, Enter,
    Reload, a card's give-back); a removal leaves no React error, and one
    console line names the pane slot, never the URL. `BROWSER_ONLY=guard`
    runs only these sections.
"""
from __future__ import annotations

import json
import os
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
FRAME = _pane(2, '[data-browser-page][aria-hidden="false"] [data-testid="browser-frame"]')
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
                        _tabs_checks, _tabs_ports_checks, _tabs_frames_checks, _tabs_guard_checks,
                        _guard_mount_checks, _guard_timer_checks, _guard_hidden_checks, _guard_card_checks,
                        _guard_frame_to_frame_checks, _guard_parked_pointer_checks, _guard_gesture_checks,
                        _guard_reload_checks, _guard_refocus_checks, _guard_giveback_checks, _guard_tab_checks,
                        _guard_typing_pointer_checks, _guard_survivor_checks):
            # `BROWSER_ONLY=guard` runs just the sections whose name holds it.
            if os.environ.get("BROWSER_ONLY") and os.environ["BROWSER_ONLY"] not in section.__name__:
                continue
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
    _load(page, "file:///tmp/refused")
    until(lambda: _count(page, REFUSED) > 0, timeout=3)
    check("a non-http URL is refused, with no origin switch", _count(page, FRAME) == 0 and _count(page, KEEP) == 0)


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
FRAME3 = _pane(3, '[data-browser-page][aria-hidden="false"] [data-testid="browser-frame"]')
STOLEN3 = _pane(3, '[data-testid="browser-stolen"]')


def _focus_url(mock, mode: str, **q) -> str:
    query = "&".join(f"{k}={v}" for k, v in q.items())
    return f"http://127.0.0.1:{mock.dev_port}/{mode}" + (f"?{query}" if query else "")


def _letters(n: int, start: int = 0) -> str:
    return "".join(chr(97 + (start + i) % 26) for i in range(n))


def _frame_key(page, sel):
    loc = page.locator(sel)
    return loc.first.get_attribute("data-browser-frame") if loc.count() else "missing-frame"


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
    _blur(page)
    x, y = _centre(page, FRAME)
    page.mouse.move(x - 20, y - 20)
    page.mouse.move(x, y)
    until(lambda: page.locator(SHIELD).get_attribute("data-up") == "false", timeout=2)
    check("setup: the pointer is over the page (its shield stepped aside)",
          page.locator(SHIELD).get_attribute("data-up") == "false")
    # The pointer leaves the window from inside the page (to another screen):
    # the HUD hears nothing, so "over it" still holds when the card comes.
    page.mouse.move(x, 2000)
    page.wait_for_timeout(100)
    _approval(mock, "guard1")
    until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
    page.wait_for_timeout(100)
    up = page.locator(SHIELD).get_attribute("data-up") if _count(page, SHIELD) else None
    check("setup: under the card the pointer still reads as over the page (only the card rule can refuse it)",
          up == "false" or _count(page, STOLEN) > 0, str(up))
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
    is removed, and focus goes to nothing that types (review of PR #78, H1)."""
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
    until(lambda: _active_frame(page) == _frame_key(page, FRAME), timeout=3)
    check("setup: a click put the keyboard in the page in pane 2", _active_frame(page) == _frame_key(page, FRAME), str(_active_frame(page)))
    page.keyboard.type("ab", delay=30)
    until(lambda: len(mock.dev_keys) - keys >= 2, timeout=3)
    check("setup: that page gets the owner's keys", len(mock.dev_keys) - keys == 2
          and all(k.startswith("/keys?p=%2Ffocus-none") for k in mock.dev_keys[keys:]), str(mock.dev_keys[keys:]))
    until(lambda: _count(page, _pane(3, '[data-testid="browser-stolen"]')) > 0, timeout=6)
    page.wait_for_timeout(200)
    check("R-1: the page in pane 3 grabbing the keyboard from pane 2's is removed",
          _count(page, frame3) == 0 and _count(page, _pane(3, '[data-testid="browser-stolen"]')) == 1)
    check("R-1: the page in pane 2 is untouched", _count(page, FRAME) == 1)
    check("H1: focus goes to the notice, not back to the chat box the owner left for the page",
          _active_testid(page) == "browser-stolen", str(_active_testid(page)))
    page.keyboard.type("cd", delay=30)
    page.wait_for_timeout(300)
    check("H1: the next keys land in no box", box.input_value() == "", repr(box.input_value()))
    check("R-1: and the grabbing page's server received none",
          not any("focus-timer" in k for k in mock.dev_keys[keys:]), str(mock.dev_keys[keys:]))


def _guard_parked_pointer_checks(page, mock, check, until):
    """A pointer left resting over a page stops counting at the owner's first
    key in the HUD (a decision narrower than the plan's stated limit). With
    nothing focused, so it is the key rule and not M2's text-control rule."""
    keys = len(mock.dev_keys)
    _setup(page, mock, until, {"browserUrl": _focus_url(mock, "focus-timer", after=1500)})
    until(lambda: _count(page, FRAME) > 0, timeout=4)
    _blur(page)
    x, y = _centre(page, FRAME)
    page.mouse.move(x - 20, y - 20)
    page.mouse.move(x, y)
    until(lambda: page.locator(SHIELD).get_attribute("data-up") == "false", timeout=2)
    check("setup: the pointer rests over the page (its shield stepped aside)",
          page.locator(SHIELD).get_attribute("data-up") == "false")
    page.keyboard.press("x")
    check("the owner's first key in the HUD brings the shield back",
          page.locator(SHIELD).get_attribute("data-up") == "true" if _count(page, SHIELD) else False)
    page.keyboard.type(_letters(40, start=3), delay=60)
    until(lambda: _count(page, STOLEN) > 0, timeout=4)
    page.wait_for_timeout(200)
    check("R-1: a page grabbing focus under a parked pointer while the owner types is removed",
          _count(page, FRAME) == 0 and _count(page, STOLEN) == 1)
    check("R-1: the page's server received none", mock.dev_keys[keys:] == [], str(mock.dev_keys[keys:][:5]))


def _blur(page):
    """Nothing in the HUD has the keyboard (no text control: M2's rule is off)."""
    page.evaluate("document.activeElement && document.activeElement.blur && document.activeElement.blur()")


def _three(page, mock, until, a: str, b: str):
    raw = json.dumps({"preset": "cols3", "focused": 1,
                      "panes": [{"view": "chat"}, {"view": "browser", "browserUrl": a},
                                {"view": "browser", "browserUrl": b}, {"view": "task"}]})
    page.set_viewport_size({"width": 2400, "height": 1000})
    _set_storage(page, zoom="100", layout="{}")
    _set_ws(page, raw)
    _boot(page, mock, None, until, reload=True)
    until(lambda: _count(page, FRAME) > 0 and _count(page, FRAME3) > 0, timeout=5)
    page.wait_for_timeout(300)


def _grab(page, sel: str, delay_ms: int = 0):
    """The page in `sel` takes the keyboard by itself, after `delay_ms` (never
    inside Playwright's own call: the guard may remove the frame mid-call)."""
    page.locator(sel).first.element_handle().content_frame().evaluate(f"setTimeout(grab, {delay_ms})")


def _click_into(page, until, sel: str = None) -> bool:
    x, y = _centre(page, sel or FRAME)
    page.mouse.move(x - 10, y)
    page.mouse.click(x, y)
    return bool(until(lambda: _active_frame(page) == (_frame_key(page, FRAME) if sel in (None, FRAME) else _frame_key(page, FRAME3)), timeout=3))


def _guard_refocus_checks(page, mock, check, until):
    """H1, the reviewer's exact scenario: a draft in the chat box, the owner
    clicks into page A and types a login, page B grabs. The owner's next keys
    — a password and Enter — must not land in the chat box and be sent."""
    _three(page, mock, until, _focus_url(mock, "focus-none", who="XA"), _focus_url(mock, "focus-none", who="XB"))
    _away(page)
    box = page.locator(INPUT1)
    box.click()
    box.fill("")
    page.keyboard.type("draft question", delay=20)
    check("setup: a click put the keyboard in page A", _click_into(page, until), str(_active_frame(page)))
    page.keyboard.type("username", delay=40)
    calls = len(mock.calls)
    keys = len(mock.dev_keys)
    _grab(page, FRAME3)
    until(lambda: _count(page, STOLEN3) > 0, timeout=3)
    page.wait_for_timeout(100)
    where = _active_testid(page)
    page.keyboard.type("hunter2", delay=60)
    page.keyboard.press("Enter")
    page.wait_for_timeout(600)
    posts = [(p, b) for m, p, b in mock.calls[calls:] if m == "POST"]
    check("H1: B is removed, and focus goes to its notice, not the chat box", _count(page, FRAME3) == 0
          and where == "browser-stolen", f"focus={where}")
    check("H1: nothing is sent: no POST carries the owner's next keys",
          "hunter2" not in json.dumps([b for _, b in posts]), str(posts[:3]))
    check("H1: the chat box's draft is unchanged", box.input_value() == "draft question", repr(box.input_value()))
    check("H1: and B's server received none", not any("XB" in k for k in mock.dev_keys[keys:]), str(mock.dev_keys[keys:]))


def _guard_giveback_checks(page, mock, check, until):
    """H2: a card answered gives page A the keyboard back — through its
    window, never an element focus — so B grabbing 600 ms later is seen."""
    _three(page, mock, until, _focus_url(mock, "focus-none", who="GA"), _focus_url(mock, "focus-none", who="GB"))
    _away(page)
    check("setup: a click put the keyboard in page A", _click_into(page, until), str(_active_frame(page)))
    _approval(mock, "guard3")
    until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
    page.wait_for_timeout(150)
    page.keyboard.press("Escape")
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
    page.wait_for_timeout(200)
    check("setup: the card gave page A the keyboard back", _active_frame(page) == _frame_key(page, FRAME), str(_active_frame(page)))
    keys = len(mock.dev_keys)
    _grab(page, FRAME3, 600)
    page.wait_for_timeout(900)
    page.keyboard.type("secret", delay=50)
    page.wait_for_timeout(300)
    check("H2: B grabbing 600 ms after the give-back is removed",
          _count(page, FRAME3) == 0 and _count(page, STOLEN3) == 1, f"stolen={_count(page, STOLEN3)}")
    check("H2: and gets none of the owner's keys", not any("GB" in k for k in mock.dev_keys[keys:]),
          str(mock.dev_keys[keys:]))


def _guard_tab_checks(page, mock, check, until):
    """M1: a Tab counts only when it moved focus, and only into the frame it
    moved it into."""
    # Monaco indents with Tab (the keydown is cancelled); the page grabs 120 ms later.
    url = _focus_url(mock, "focus-none", who="TM")
    raw = json.dumps({"preset": "cols3", "focused": 3,
                      "panes": [{"view": "chat"}, {"view": "browser", "browserUrl": url},
                                {"view": "file", "projectId": "p1",
                                 "editors": {"paths": [".env"], "active": ".env", "preview": None}},
                                {"view": "task"}]})
    page.set_viewport_size({"width": 2400, "height": 1000})
    _set_storage(page, zoom="100", layout="{}")
    _set_ws(page, raw)
    _boot(page, mock, None, until, reload=True)
    lines3 = _pane(3, '[data-testid="editor"] .view-lines')
    until(lambda: page.locator(_pane(3, '[data-testid="editor"][data-language]')).count() > 0, timeout=10)
    until(lambda: _count(page, FRAME) > 0, timeout=5)
    _away(page)
    page.locator(lines3).click()
    page.keyboard.press("Control+End")
    page.keyboard.type("KEY=", delay=40)
    keys = len(mock.dev_keys)
    page.keyboard.press("Tab")
    _grab(page, FRAME, 120)
    page.wait_for_timeout(250)
    page.keyboard.type("supersecretvalue", delay=40)
    page.wait_for_timeout(300)
    check("M1: Monaco's Tab indent is no gesture: the page grabbing 120 ms later is removed",
          _count(page, FRAME) == 0 and _count(page, STOLEN) == 1, f"stolen={_count(page, STOLEN)}")
    check("M1: and the value typed after it reaches the editor, not the page",
          mock.dev_keys[keys:] == [] and "supersecretvalue" in page.locator(lines3).inner_text(),
          str(mock.dev_keys[keys:][:3]))

    # Tab in the chat box moves focus to Send; the page grabs 100 ms later.
    _setup(page, mock, until, {"browserUrl": _focus_url(mock, "focus-none", who="TS")})
    until(lambda: _count(page, FRAME) > 0, timeout=4)
    _away(page)
    box = page.locator(INPUT1)
    box.click()
    box.fill("")
    page.keyboard.type("ab", delay=40)
    page.keyboard.press("Tab")
    landed = _active_testid(page)
    keys = len(mock.dev_keys)
    _grab(page, FRAME, 100)
    page.wait_for_timeout(250)
    page.keyboard.type("xyz", delay=40)
    page.wait_for_timeout(300)
    check("M1: a Tab that moved focus to Send is no gesture: the page grabbing 100 ms later is removed",
          _count(page, FRAME) == 0 and _count(page, STOLEN) == 1 and mock.dev_keys[keys:] == [],
          f"tab landed on {landed}; stolen={_count(page, STOLEN)} keys={mock.dev_keys[keys:][:3]}")

    # Tab into page A; page B grabs within 300 ms.
    _three(page, mock, until, _focus_url(mock, "focus-none", who="TA"), _focus_url(mock, "focus-none", who="TB"))
    _away(page)
    page.locator(_pane(2, '[data-testid="browser-url"]')).click()
    for _ in range(8):
        page.keyboard.press("Tab")
        page.wait_for_timeout(40)
        if _active_frame(page):
            break
    check("setup: a Tab put the keyboard in page A", _active_frame(page) == _frame_key(page, FRAME), str(_active_frame(page)))
    keys = len(mock.dev_keys)
    _grab(page, FRAME3, 30)
    page.wait_for_timeout(250)
    page.keyboard.type("secret", delay=40)
    page.wait_for_timeout(300)
    check("M1: a Tab into page A is no gesture for page B grabbing within 300 ms: B is removed",
          _count(page, FRAME3) == 0 and _count(page, STOLEN3) == 1
          and not any("TB" in k for k in mock.dev_keys[keys:]), f"stolen={_count(page, STOLEN3)}")


def _guard_typing_pointer_checks(page, mock, check, until):
    """M2: while a HUD text control has the keyboard, only a press on a page
    gives it the keyboard; a pointer crossing it never does."""
    keys = len(mock.dev_keys)
    _setup(page, mock, until, {"browserUrl": _focus_url(mock, "focus-pointer")})
    until(lambda: _count(page, FRAME) > 0, timeout=4)
    _away(page)
    box = page.locator(INPUT1)
    box.click()
    box.fill("")
    page.keyboard.type("hello ", delay=50)
    x, y = _centre(page, FRAME)
    sx, sy = _centre(page, '[data-testid="sidebar"]')
    page.mouse.move(x - 300, y)
    page.mouse.move(x + 300, y, steps=8)
    page.mouse.move(sx, sy, steps=4)
    page.keyboard.type("my secret words", delay=50)
    page.wait_for_timeout(400)
    check("M2: a pointer crossing a page mid-typing never gives it the keyboard: the box keeps every key",
          box.input_value() == "hello my secret words" and mock.dev_keys[keys:] == [],
          f"box={box.input_value()!r} keys={mock.dev_keys[keys:][:3]}")
    check("M2: the page never held the keyboard", _active_frame(page) is None, str(_active_frame(page)))

    # Click then type, from the chat box: the click puts the keyboard in the page.
    _setup(page, mock, until, {"browserUrl": _focus_url(mock, "focus-none", who="CT")})
    until(lambda: _count(page, FRAME) > 0, timeout=4)
    _away(page)
    box = page.locator(INPUT1)
    box.click()
    box.fill("")
    page.keyboard.type("ab", delay=40)
    keys = len(mock.dev_keys)
    x, y = _centre(page, FRAME)
    page.mouse.move(x - 10, y)
    page.mouse.click(x, y)
    until(lambda: _active_frame(page) == _frame_key(page, FRAME), timeout=3)
    page.keyboard.type("xyz", delay=40)
    until(lambda: len(mock.dev_keys) - keys >= 3, timeout=3)
    check("M2: from the chat box, a click puts the keyboard in the page and the keys typed after reach it",
          _active_frame(page) == _frame_key(page, FRAME) and len(mock.dev_keys) - keys == 3 and box.input_value() == "ab"
          and _count(page, STOLEN) == 0, f"{_active_frame(page)} keys={len(mock.dev_keys) - keys}")


    # Page to page: the keyboard is in page A and the pointer crosses page B,
    # which grabs on its first move (PR #78 delta review, MED).
    _three(page, mock, until, _focus_url(mock, "focus-none", who="PA"), _focus_url(mock, "focus-pointer", who="PB"))
    _away(page)
    check("setup: a click put the keyboard in page A", _click_into(page, until, FRAME))
    page.keyboard.type("ab", delay=40)
    keys = len(mock.dev_keys)
    ax, ay = _centre(page, FRAME)
    bx, by = _centre(page, FRAME3)
    page.mouse.move(bx - 200, by)
    page.mouse.move(bx + 200, by, steps=8)
    page.mouse.move(ax, ay, steps=6)
    page.keyboard.type("password", delay=50)
    page.wait_for_timeout(400)
    check("M2: crossing page B while typing in page A never gives B the keyboard",
          not any("PB" in k for k in mock.dev_keys[keys:]) and _count(page, STOLEN3) == 0,
          f"keys={mock.dev_keys[keys:][:3]} stolen={_count(page, STOLEN3)}")

    # The HUD focusing a text control raises the shields: a pointer resting
    # over a page with nothing focused stops counting once the box has the
    # keyboard, so the page grabbing then is removed (review LOW).
    _setup(page, mock, until, {"browserUrl": _focus_url(mock, "focus-none", who="RS")})
    until(lambda: _count(page, FRAME) > 0, timeout=4)
    _away(page)
    page.evaluate("document.activeElement && document.activeElement.blur && document.activeElement.blur()")
    x, y = _centre(page, FRAME)
    page.mouse.move(x - 20, y)
    page.mouse.move(x, y, steps=4)
    box = page.locator(INPUT1)
    box.focus()
    keys = len(mock.dev_keys)
    _grab(page, FRAME, 300)
    page.wait_for_timeout(450)
    page.keyboard.type("abc", delay=40)
    page.wait_for_timeout(300)
    check("M2: a text control taking focus raises the shields: a page resting under the pointer grabbing then is removed",
          _count(page, FRAME) == 0 and _count(page, STOLEN) == 1 and mock.dev_keys[keys:] == [],
          f"stolen={_count(page, STOLEN)} keys={mock.dev_keys[keys:][:3]}")

def _guard_survivor_checks(page, mock, check, until):
    """The review's two surviving mutants: a page reloading itself must not
    keep the owner's load alive; a page the owner once clicked into must not
    re-grab from the chat box later."""
    _setup(page, mock, until, {"browserUrl": _focus_url(mock, "focus-none")})
    until(lambda: _count(page, FRAME) > 0, timeout=4)
    _away(page)
    url_box = page.locator(_pane(2, '[data-testid="browser-url"]'))
    url_box.click()
    url_box.fill(_focus_url(mock, "focus-reload", grab=4000))
    url_box.press("Enter")
    page.wait_for_timeout(5500)
    check("LOW: a page reloading itself does not keep the owner's load alive: its grab at 4 s is removed",
          _count(page, FRAME) == 0 and _count(page, STOLEN) == 1, f"stolen={_count(page, STOLEN)}")

    _setup(page, mock, until, {"browserUrl": _focus_url(mock, "focus-none", who="LA")})
    until(lambda: _count(page, FRAME) > 0, timeout=4)
    _away(page)
    check("setup: a click put the keyboard in the page", _click_into(page, until), str(_active_frame(page)))
    page.keyboard.type("in", delay=40)
    _away(page)
    box = page.locator(INPUT1)
    box.click()
    box.fill("")
    keys = len(mock.dev_keys)
    _grab(page, FRAME, 600)
    page.keyboard.type(_letters(20), delay=60)
    page.wait_for_timeout(300)
    check("LOW: a page the owner once clicked into, re-grabbing after they left it, is removed",
          _count(page, STOLEN) == 1 and mock.dev_keys[keys:] == [] and box.input_value() == _letters(20),
          f"stolen={_count(page, STOLEN)} keys={mock.dev_keys[keys:][:3]} box={box.input_value()!r}")


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
    until(lambda: _active_frame(page) == _frame_key(page, FRAME), timeout=3)
    page.keyboard.type("xyz", delay=30)
    until(lambda: len(mock.dev_keys) - keys >= 3, timeout=3)
    page.wait_for_timeout(300)
    check("gesture: clicking into a page gives it the keyboard, and keys reach it",
          _active_frame(page) == _frame_key(page, FRAME) and _count(page, FRAME) == 1 and _count(page, STOLEN) == 0
          and len(mock.dev_keys) - keys == 3, f"{_active_frame(page)} keys={len(mock.dev_keys) - keys}")

    # A card comes up while the keyboard is in the page: the card takes it;
    # answered, it goes back to the page (WP-A's restore), which keeps it.
    _approval(mock, "guard2")
    until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
    page.wait_for_timeout(150)
    took = _active_testid(page)
    page.keyboard.press("Escape")
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
    page.wait_for_timeout(300)
    check("gesture: a card takes the keyboard from a page the owner clicked into, and gives it back after",
          took == "approval-card" and _active_frame(page) == _frame_key(page, FRAME) and _count(page, FRAME) == 1
          and _count(page, STOLEN) == 0, f"card={took} after={_active_frame(page)} stolen={_count(page, STOLEN)}")

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
          _active_frame(page) == _frame_key(page, FRAME) and _count(page, STOLEN) == 0 and len(mock.dev_keys) - keys == 2,
          f"{_active_frame(page)} keys={len(mock.dev_keys) - keys}")

    # The pointer over a page that grabs focus on a timer (no text control
    # has the keyboard: with one, only a click counts — M2, below).
    _setup(page, mock, until, {"browserUrl": _focus_url(mock, "focus-timer", after=800)})
    until(lambda: _count(page, FRAME) > 0, timeout=4)
    _blur(page)
    x, y = _centre(page, FRAME)
    page.mouse.move(x - 20, y - 20)
    page.mouse.move(x, y)
    until(lambda: _active_frame(page) == _frame_key(page, FRAME), timeout=4)
    page.wait_for_timeout(600)
    keys = len(mock.dev_keys)
    page.keyboard.type("pq", delay=30)
    until(lambda: len(mock.dev_keys) - keys >= 2, timeout=3)
    check("gesture: with the pointer over it, a page that focuses itself keeps the keyboard",
          _active_frame(page) == _frame_key(page, FRAME) and _count(page, FRAME) == 1 and _count(page, STOLEN) == 0
          and len(mock.dev_keys) - keys == 2, f"{_active_frame(page)} keys={len(mock.dev_keys) - keys}")

    # The owner's own load: Enter in the URL box on a page that focuses on mount.
    _away(page)
    url_box = page.locator(_pane(2, '[data-testid="browser-url"]'))
    url_box.click()
    url_box.fill(_focus_url(mock, "focus-mount"))
    url_box.press("Enter")
    until(lambda: _active_frame(page) == _frame_key(page, FRAME), timeout=4)
    page.wait_for_timeout(300)
    check("gesture: a page the owner loaded with Enter may focus itself as it loads",
          _active_frame(page) == _frame_key(page, FRAME) and _count(page, FRAME) == 1 and _count(page, STOLEN) == 0)


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
    until(lambda: _active_frame(page) == _frame_key(page, FRAME), timeout=4)
    page.wait_for_timeout(300)
    check("R-1: Reload makes a fresh frame, and the page that focuses on load keeps it this time",
          _count(page, FRAME) == 1 and _count(page, STOLEN) == 0 and _active_frame(page) == _frame_key(page, FRAME))
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



# Direct tabs (AO-2): actual iframe retention and the visible controls, not only storage.
def _tab(page, pane, ident):
    return page.locator(_pane(pane, f'[data-browser-tab="{ident}"]'))


def _tabframe(pane, ident):
    return _pane(pane, f'[data-browser-page="{ident}"] [data-testid="browser-frame"]')


def _tabs_checks(page, mock, check, until):
    url = _focus_url(mock, "focus-none")
    tabs = [{"id": "first1", "url": url, "keepOrigin": f"http://127.0.0.1:{mock.dev_port}",
             "auto": {"kind": "thread", "label": "Project chat"},
             "source": {"kind": "dev_server", "entryId": "ds_0123abcd"}},
            {"id": "second", "url": url + "?second=1"}]
    _setup(page, mock, until, {"browser": {"tabs": tabs, "active": "first1"}})
    until(lambda: _sandbox(page) == ON, timeout=4)
    check("AO2: auto-open source is visible chrome", page.locator(_pane(2, '[data-testid="browser-auto"]')).is_visible()
          and page.locator(_pane(2, '[data-testid="browser-auto"]')).inner_text() == f"Opened for Project chat · 127.0.0.1:{mock.dev_port}")
    check("AO2: auto-open has a mark", _tab(page, 2, "first1").locator(".browserautomark").count() == 1)
    _tab(page, 2, "second").click()
    until(lambda: _src(page) == url + "?second=1", timeout=3)
    check("AO2: switching tabs carries no origin grant", _sandbox(page) == OFF and not _keep_checked(page))
    check("AO2: the previous page stays mounted, hidden and inert",
          page.locator(_tabframe(2, "first1")).count() == 1
          and page.locator(_pane(2, '[data-browser-page="first1"]')).get_attribute("inert") is not None
          and not page.locator(_tabframe(2, "first1")).is_visible())
    _tab(page, 2, "second").press("ArrowLeft")
    check("AO2: arrows activate the adjacent tab", _tab(page, 2, "first1").get_attribute("aria-selected") == "true")
    check("AO2: returning to the first tab restores only its own grant", _sandbox(page) == ON)
    _tab(page, 2, "second").click(button="middle")
    check("AO2: middle click closes only that tab", page.locator(_pane(2, '[data-testid="browser-tab"]')).count() == 1)
    _approval(mock, "tabs-card")
    until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
    buttons = page.locator(_pane(2, '[data-testid="browser-tabs"] button'))
    check("AO2: every strip button is disabled under a card", all(buttons.nth(i).is_disabled() for i in range(buttons.count())))
    check("AO2: the strip is inert under a card", page.locator(_pane(2, '[data-testid="browser-tabs"]')).evaluate("e => !!e.closest('[inert]')"))
    page.keyboard.press("Escape")
    body = until(lambda: mock.sent("POST", "/approvals/tabs-card") or None, timeout=4)
    check("AO2: Escape denies while the strip is inert", bool(body) and body[-1].get("decision") == "deny")

    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
    first = lambda: _ws(page)["ws"]["panes"][1]["browser"]["tabs"][0]
    check("source: selection and approval leave daemon provenance intact",
          first().get("source") == {"kind": "dev_server", "entryId": "ds_0123abcd"})
    address = page.locator(_pane(2, '[data-testid="browser-url"]'))
    address.fill("http://localhost:8405/")
    page.locator(_pane(2, '[data-testid="browser-go"]')).click()
    check("source: a refused owner URL cannot clear provenance",
          first().get("source") == {"kind": "dev_server", "entryId": "ds_0123abcd"})
    address.fill(url)
    address.press("Enter")
    until(lambda: "source" not in first(), timeout=3)
    check("source: actual same-URL owner submission clears provenance and obsolete auto label",
          "source" not in first() and "auto" not in first()
          and page.locator(_pane(2, '[data-testid="browser-auto"]')).count() == 0)
    page.reload()
    until(lambda: _sandbox(page) == ON, timeout=4)
    check("source: owner submission remains owner-entered after storage reload", "source" not in first())

    # A markup/bidi URL is still only text; Direct never borrows document.title.
    hostile = f"http://localhost:{mock.dev_port}/%E2%80%AE%3Cimg%3E" + "x" * 1700
    tabs = [{"id": f"tab{i:03}", "url": hostile if i == 0 else ""} for i in range(12)]
    _setup(page, mock, until, {"browser": {"tabs": tabs, "active": "tab001"}})
    title = page.title()
    label = _tab(page, 2, "tab000").inner_text()
    check("AO2: hostile labels are clean capped React text", len(label) == 40 and "\u202e" not in label
          and "<img>" in label and _tab(page, 2, "tab000").locator("img").count() == 0 and page.title() == title, repr(label))
    page.locator(_pane(2, '[data-testid="browser-tab-new"]')).click()
    check("AO2: plus refuses a thirteenth tab with a sentence",
          page.locator(_pane(2, '[data-testid="browser-tab"]')).count() == 12
          and "12 tabs" in page.locator(_pane(2, '[data-testid="browser-full"]')).inner_text())
    for _ in range(12):
        page.locator(_pane(2, '[data-testid="browser-tab-close"]')).last.click()
    check("AO2: closing the last tab leaves Nothing loaded", page.locator(_pane(2, '[data-testid="browser-tab"]')).count() == 0
          and "Nothing loaded" in page.locator(_pane(2)).inner_text())
    _boot(page, mock, None, until, reload=True)
    check("AO2: empty tabs survive reload without mirror resurrection", page.locator(_pane(2, '[data-testid="browser-tab"]')).count() == 0
          and _ws(page)["ws"]["panes"][1]["browser"] == {"tabs": [], "active": ""})
    page.locator(_pane(2, '[data-testid="browser-tab-new"]')).click()
    until(lambda: _active_testid(page) == "browser-url", timeout=2)
    check("AO2: plus creates a blank tab and focuses its URL box",
          _active_testid(page) == "browser-url" and page.locator(_pane(2, '[data-testid="browser-tab"]')).count() == 1)


def _tabs_ports_checks(page, mock, check, until):
    good = _focus_url(mock, "focus-none")
    bad = f"http://localhost:{mock.api_port}/"
    _setup(page, mock, until, {"browser": {"tabs": [
        {"id": "good01", "url": good}, {"id": "api001", "url": bad, "keepOrigin": bad.rstrip("/")},
        {"id": "api002", "url": "http://localhost:8405/"}], "active": "good01"}})
    until(lambda: _src(page) == good, timeout=3)
    until(lambda: "keepOrigin" not in _ws(page)["ws"]["panes"][1]["browser"]["tabs"][1], timeout=3)
    check("AO2: even dormant tabs drop grants for reported control ports",
          "keepOrigin" not in _ws(page)["ws"]["panes"][1]["browser"]["tabs"][1])
    for ident in ("api001", "api002"):
        _tab(page, 2, ident).click()
        until(lambda: _count(page, REFUSED) == 1, timeout=2)
        check("AO2: stored " + ident + " is refused on load", _count(page, FRAME) == 0
              and "API" in page.locator(REFUSED).inner_text() and not mock.api_hits)
    _tab(page, 2, "good01").click()
    check("AO2: a refused sibling never stops the other tab", _src(page) == good)
    # Every save mirrors exactly the active tab for a one-release rollback.
    spec = _ws(page)["ws"]["panes"][1]
    check("AO2: canonical tabs retain an active rollback URL", spec["browserUrl"] == good
          and spec["browser"]["active"] == "good01" and not spec.get("keepOrigin"))


def _tabs_frames_checks(page, mock, check, until):
    panes = [{"view": "browser", "browser": {"tabs": [
        {"id": f"tab{i:03}", "url": _focus_url(mock, "focus-none", budget=f"{n}-{i}")} for i in range(3)
    ], "active": "tab000"}} for n in range(1, 5)]
    page.set_viewport_size({"width": 2400, "height": 1200})
    _set_storage(page, zoom="100", layout="{}")
    _set_ws(page, json.dumps({"preset": "grid4", "focused": 1, "panes": panes}))
    _boot(page, mock, None, until, reload=True)
    # Startup creates a chat when every stored pane is non-chat. The owner
    # explicitly switches that first pane back; its retained Browser tabs stay.
    if _ws(page)["ws"]["panes"][0]["view"] != "browser":
        page.locator(_pane(1, '[data-testid="tab-browser"]')).click()
    for pane in range(1, 5):
        for i in (1, 2):
            _tab(page, pane, f"tab{i:03}").click()
    allframes = '[data-testid="browser-frame"]'
    until(lambda: page.locator(allframes).count() == 8, timeout=4)
    keys = page.locator(allframes).evaluate_all("els => els.map(e => e.dataset.browserFrame)")
    check("AO2: four panes with three visited tabs mount exactly eight frames", len(keys) == 8, str(keys))
    check("AO2: active pages win the window budget", all(f"{n}:tab002" in keys for n in range(1, 5)), str(keys))
    check("AO2: each pane keeps at most three live frames", all(sum(k.startswith(f"{n}:") for k in keys) <= 3 for n in range(1, 5)))
    check("AO2: the oldest background frame was evicted", "1:tab000" not in keys, str(keys))
    page.evaluate("""() => { window.__ao2CoveredFrames = Array.from(document.querySelectorAll('[data-testid="browser-frame"]')); }""")
    page.keyboard.press("Control+,")
    until(lambda: page.locator('[data-testid="settings-page"]').count() == 1, timeout=3)
    check("AO2: Settings retains all eight live frame identities within the window cap",
          page.locator(allframes).count() == 8 and page.evaluate("""() =>
            window.__ao2CoveredFrames.every(e => e.isConnected)"""))
    check("AO2: every covered frame is hidden while its workspace is inert",
          page.locator(".workspace-surface").evaluate("e => e.inert")
          and page.locator(allframes).evaluate_all("els => els.every(e => getComputedStyle(e).visibility === 'hidden')"))
    page.locator('[data-testid="settings-close"]').click()
    until(lambda: page.locator('[data-testid="settings-page"]').count() == 0, timeout=3)
    check("AO2: closing Settings restores the same eight frames without allocating more",
          page.locator(allframes).count() == 8 and page.evaluate("""() =>
            window.__ao2CoveredFrames.every(e => e.isConnected)"""))
    page.evaluate("delete window.__ao2CoveredFrames")
    path = "/focus-none?budget=1-0"
    before = mock.dev_hits.count(path)
    _tab(page, 1, "tab000").click()
    until(lambda: mock.dev_hits.count(path) > before, timeout=4)
    check("AO2: picking an evicted tab loads its URL again", mock.dev_hits.count(path) == before + 1
          and page.locator(_tabframe(1, "tab000")).count() == 1 and page.locator(allframes).count() == 8)
    _choose(page, until, "single")
    check("AO2: hidden panes still count toward the window budget", page.locator(allframes).count() <= 8)
    _choose(page, until, "grid4")


def _tabs_guard_checks(page, mock, check, until):
    tabs = [{"id": "pageaa", "url": _focus_url(mock, "focus-none", who="TAB-A")},
            {"id": "pagebb", "url": _focus_url(mock, "focus-none", who="TAB-B")}]
    _setup(page, mock, until, {"browser": {"tabs": tabs, "active": "pageaa"}})
    until(lambda: _count(page, _tabframe(2, "pageaa")) == 1, timeout=3)
    _tab(page, 2, "pagebb").click()
    until(lambda: _count(page, _tabframe(2, "pagebb")) == 1, timeout=3)
    _tab(page, 2, "pageaa").click()
    _grab(page, _tabframe(2, "pageaa"))
    until(lambda: _active_frame(page) == "2:pageaa", timeout=3)
    check("AO2: picking an existing tab grants only its exact frame", _active_frame(page) == "2:pageaa"
          and _count(page, _tabframe(2, "pageaa")) == 1)
    box = page.locator(INPUT1)
    box.click()
    box.fill("kept")
    keys = len(mock.dev_keys)
    _grab(page, _tabframe(2, "pagebb"))
    until(lambda: _count(page, _tabframe(2, "pagebb")) == 0, timeout=3)
    page.keyboard.type("-typing", delay=30)
    check("AO2: a background tab taking focus is removed and the chat keeps its keys",
          _count(page, _tabframe(2, "pagebb")) == 0 and box.input_value() == "kept-typing"
          and not mock.dev_keys[keys:])
    _tab(page, 2, "pagebb").click()
    check("AO2: a stolen tab stays stopped after selecting it", _count(page, FRAME) == 0
          and page.locator(STOLEN).is_visible())
    page.locator(RELOAD).click()
    until(lambda: _count(page, FRAME) == 1, timeout=3)
    check("AO2: only explicit Reload restores that stolen tab", _count(page, FRAME) == 1
          and _frame_key(page, FRAME) == "2:pagebb")

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

