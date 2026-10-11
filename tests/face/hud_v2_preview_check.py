"""Headless checks for the Preview pane's port rules and its "keep its own
origin" switch (WP-E; plan docs/plans/2026-10-09-hud-workspace-plan.md §2.5,
decisions W-4; contract in docs/hud-api.md).

Free and hermetic. The section runs against a `MockDaemon(0)` of its own and a
browser context of its own. The mock is the HUD listener; its API listener,
workshop and a local dev server are listeners of their own on ephemeral ports,
all reported on `/status` — and every request to 8402/8403/8405, the owner's
live daemon, is refused and counted as a failure (`hud_v2_check.guard_live`).

The checks worth keeping, each written to bite:
  - the Preview pane refuses the API listener `/status` reports (and 8405),
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
  - under an authorization card the switch is disabled and inert.
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
    _approval, _boot, _pane, _set_storage, _set_ws, _visible, _ws,
)

OFF = "allow-scripts allow-forms"
ON = "allow-scripts allow-forms allow-same-origin"
FRAME = _pane(2, '[data-testid="preview-frame"]')
KEEP = _pane(2, '[data-testid="preview-keep-origin"]')
REFUSED = _pane(2, '[data-testid="preview-refused"]')


def preview_checks(browser, mock, base, check, until, guard, init_script):
    print("\npreview: the daemon's ports, keep its own origin (WP-E)")
    ctx = browser.new_context(viewport={"width": 1600, "height": 900}, permissions=["microphone"])
    guard(ctx)
    ctx.add_init_script(init_script)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_default_timeout(5000)
    try:
        _boot(page, mock, base, until)
        for section in (_port_checks, _keep_checks, _stored_checks, _hardened_checks, _card_checks):
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
        check("no page errors in the preview section", not errors, "; ".join(errors[:3]))
        check("nothing ever loaded the daemon's API listener", mock.api_hits == [], str(mock.api_hits[:3]))
    finally:
        mock.frame_hardened = True
        ctx.close()


# ---------------------------------------------------------------------------

def _setup(page, mock, until, pane2: dict | None = None):
    """Two columns: a chat, and a Preview pane (focused) with `pane2`'s stored fields."""
    raw = json.dumps({"preset": "cols2", "focused": 2,
                      "panes": [{"view": "chat"}, {"view": "preview", **(pane2 or {})},
                                {"view": "file"}, {"view": "task"}]})
    page.set_viewport_size({"width": 1600, "height": 900})
    _set_storage(page, zoom="100", layout="{}")
    _set_ws(page, raw)
    _boot(page, mock, None, until, reload=True)
    until(lambda: _visible(page, _pane(2, '[data-testid="preview-url"]')), timeout=3)


def _load(page, url: str):
    page.locator(_pane(2, '[data-testid="preview-url"]')).fill(url)
    page.locator(_pane(2, '[data-testid="preview-go"]')).click()
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
    """Text inside the preview frame (Playwright reads any origin's frame)."""
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
        check(f"Preview refuses {what}, and says why",
              word in text and _count(page, FRAME) == 0 and _count(page, KEEP) == 0, text)
    check("and nothing reached the API listener", mock.api_hits == [], str(mock.api_hits))

    page.locator(_pane(2, '[data-testid="preview-project"]')).click()
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


def _stored_checks(page, mock, check, until):
    for what, port, word in (("the API listener", mock.api_port, "API"), ("the HUD", mock.port, "HUD")):
        origin = f"http://127.0.0.1:{port}"
        _setup(page, mock, until, {"previewUrl": origin + "/", "keepOrigin": origin})
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
        _setup(page, mock, until, {"previewUrl": dev, "keepOrigin": origin})
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
    _setup(page, mock, until, {"previewUrl": dev})
    until(lambda: _count(page, KEEP) > 0, timeout=4)
    check("the same daemon saying it again offers the switch again", _visible(page, KEEP) and not _keep_checked(page))


def _card_checks(page, mock, check, until):
    _setup(page, mock, until, {"previewUrl": f"http://127.0.0.1:{mock.dev_port}/"})
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
            preview_checks(browser, mock, f"http://127.0.0.1:{mock.port}", v2.check, v2.until,
                           lambda ctx: v2.guard_live(ctx, v2.FAILURES.append), v2.FAKE_RECOGNIZER)
            browser.close()
    finally:
        mock.stop()
    print()
    if v2.FAILURES:
        print(f"{len(v2.FAILURES)} of {v2.CHECKS} checks FAILED:")
        for f in v2.FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {v2.CHECKS} preview checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
