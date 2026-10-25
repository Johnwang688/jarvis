"""Settings > Phone (phone design PH-6, P-2) in a real headless Chromium against
the mock daemon; the `/phone` routes and `/status`'s `remote` are faked per
test with `ctx.route`, so nothing here pairs a real passkey.

What it pins: the QR code, its URL and a countdown that runs; Cancel; a
window that expires; a registration seen by the poll; the listener's state
from `/status`; the 3-passkey limit; Revoke (with its confirmation) and End
reaching their routes; untrusted phone text (the user agent, Tailscale's
advisory login) shown as text, never markup; an approval card disabling
the section; no page errors.

And the phone's side, end to end (`shell_checks`): the built `mobile.html`
served by a real daemon's remote listener, reached as `https://<host>` (the
context forwards that origin to the loopback port with the trusted Host, as
`tailscale serve` does), Chromium's virtual authenticator standing in for
the iPhone (user verification on). It pairs through `/m/pair#<code>`, signs
in, holds the `__Host-` cookie with its attributes, signs out, and is refused
once user verification is off: the browser's own WebAuthn serialization
against the real `webauthn` verification. Skipped when `hud/dist` has no
phone shell (run `npm run build` in hud/).

Run: PYTHONPATH=. .venv/bin/python tests/face/hud_v2_phone_check.py
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.face.hud_v2_layout_check import _approval, _boot  # noqa: E402

URL = "https://jarvis-box.tail-test.ts.net/m/pair#" + "Q" * 43
HOSTILE_UA = '<img src=x onerror="window.__pwned=1">Mozilla/5.0 (iPhone) <b>bold</b>'
HOSTILE_LOGIN = '<script>window.__pwned=2</script>owner@example.com'


def phone_checks(browser, mock, base, check, until, guard, init_script):
    print("\nSettings > Phone: pairing QR, countdown, passkeys, revoke, untrusted text")
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    guard(ctx)
    ctx.add_init_script(init_script)
    state = {
        "remote": {"port": 18408, "bound": True, "host_configured": True},
        "open": False, "expires_in": 0,
        "passkeys": [
            {"key": "k-aaaaaaaaaaaa", "created": time.time() - 86400 * 3, "last_used": time.time() - 600,
             "backed_up": True, "sessions": [
                 {"id": "s-1111111111111111", "ua": HOSTILE_UA, "created": time.time() - 3600,
                  "last_seen": time.time() - 120, "advisory": {"login": HOSTILE_LOGIN, "name": ""}},
                 {"id": "s-2222222222222222", "ua": "Mozilla/5.0 (iPad)", "created": time.time() - 7200,
                  "last_seen": time.time() - 7200, "advisory": {"login": "", "name": ""}}]},
        ],
        "window_s": 300,
    }
    posts: list[str] = []

    def view():
        unsaved = state.get("unsaved", [])
        return {"state": "unsaved" if unsaved else "ok", "unsaved": unsaved,
                "host_configured": True, "max_passkeys": 3,
                "pairing": {"open": state["open"], "expires_in": state["expires_in"]},
                "passkeys": state["passkeys"], "remote": state["remote"]}

    def status(route):
        response = route.fetch()
        value = response.json()
        value["remote"] = state["remote"]
        route.fulfill(response=response, json=value)

    def phone(route):
        request = route.request
        path = request.url.split(base, 1)[1].split("?", 1)[0]
        if request.method == "GET" and path == "/phone":
            return route.fulfill(json=view())
        if request.method != "POST":
            return route.fulfill(status=404, json={"error": "route not found"})
        posts.append(path)
        if path == "/phone/pair":
            state["open"], state["expires_in"] = True, state["window_s"]
            return route.fulfill(json={"url": URL, "expires_in": state["window_s"]})
        if path == "/phone/pair/cancel":
            state["open"] = False
            return route.fulfill(json={"open": False})
        m = re.fullmatch(r"/phone/passkeys/(k-[0-9a-f]{12})/revoke", path)
        if m:
            if state.get("fail_revoke"):
                gone = next(p for p in state["passkeys"] if p["key"] == m.group(1)) if any(
                    p["key"] == m.group(1) for p in state["passkeys"]) else None
                if gone is not None:
                    state["passkeys"].remove(gone)
                    state.setdefault("unsaved", []).append({"key": gone["key"], "revoked": time.time()})
                return route.fulfill(status=503, json={"error": "phone state unavailable"})
            state["passkeys"] = [p for p in state["passkeys"] if p["key"] != m.group(1)]
            state["unsaved"] = [u for u in state.get("unsaved", []) if u["key"] != m.group(1)]
            return route.fulfill(json={"revoked": True})
        m = re.fullmatch(r"/phone/sessions/(s-[0-9a-f]{16})/end", path)
        if m:
            for p in state["passkeys"]:
                p["sessions"] = [s for s in p["sessions"] if s["id"] != m.group(1)]
            return route.fulfill(json={"ended": True})
        return route.fulfill(status=404, json={"error": "route not found"})

    ctx.route(base + "/status", status)
    ctx.route(base + "/phone", phone)
    ctx.route(base + "/phone/**", phone)
    page = ctx.new_page()
    page.set_default_timeout(6000)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    settings = '[data-testid="phone-settings"]'
    try:
        _boot(page, mock, base, until)
        page.keyboard.press("Control+,")
        page.wait_for_selector('[data-testid="settings-page"]')
        page.locator('[data-testid="settings-phone"]').click()
        page.wait_for_selector(settings)
        until(lambda: page.locator('[data-testid="phone-passkey"]').count() == 1, timeout=4)
        listener = page.locator('[data-testid="phone-listener"]')
        check("P-2 Phone: the listener's state comes from /status",
              listener.get_attribute("data-bound") == "true" and "127.0.0.1:18408" in listener.inner_text())

        # -- untrusted text -------------------------------------------------------
        ua = page.locator('[data-testid="phone-ua"]').first
        check("P-2 Phone: a session's user agent is shown as text", ua.inner_text() == HOSTILE_UA)
        advisory = page.locator('[data-testid="phone-advisory"]').first
        check("P-2 Phone: Tailscale's login is shown as text and marked unverified",
              HOSTILE_LOGIN in advisory.inner_text() and "unverified" in advisory.inner_text())
        check("P-2 Phone: no markup from the phone reaches the page",
              page.locator(settings + " img, " + settings + " b, " + settings + " script").count() == 0
              and page.evaluate("window.__pwned === undefined"))

        # -- pairing: QR, URL, countdown, cancel ------------------------------------
        pair = page.locator('[data-testid="phone-pair"]')
        check("P-2 Phone: Pair a phone is offered", pair.is_enabled())
        pair.click()
        page.wait_for_selector('[data-testid="phone-qr"]')
        qr = page.locator('[data-testid="phone-qr"]')
        d = qr.locator("path").get_attribute("d") or ""
        check("P-2 Phone: the QR code is drawn (white quiet zone, black modules)",
              len(re.findall(r"h1v1h-1z", d)) > 300 and qr.locator("rect").get_attribute("fill") == "#ffffff")
        size = qr.bounding_box()
        check("P-2 Phone: the QR code is large enough to scan", size is not None and size["width"] >= 200)
        check("P-2 Phone: the URL is shown, as text", page.locator('[data-testid="phone-url"]').inner_text() == URL)
        countdown = page.locator('[data-testid="phone-countdown"]')
        first = countdown.inner_text()
        check("P-2 Phone: a countdown from 5:00", re.fullmatch(r"(5:00|4:5\d) left", first) is not None, first)
        page.wait_for_timeout(1300)
        second = countdown.inner_text()
        check("P-2 Phone: the countdown runs", second != first and re.fullmatch(r"4:5\d left", second) is not None,
              f"{first} -> {second}")
        page.locator('[data-testid="phone-cancel"]').click()
        until(lambda: page.locator('[data-testid="phone-qr"]').count() == 0, timeout=4)
        check("P-2 Phone: Cancel closes the window at the daemon", posts[-1] == "/phone/pair/cancel")

        # -- a window that expires ------------------------------------------------------
        state["window_s"] = 2
        pair.click()
        page.wait_for_selector('[data-testid="phone-qr"]')
        state["open"] = True
        until(lambda: page.locator('[data-testid="phone-qr"]').count() == 0, timeout=5)
        check("P-2 Phone: at zero the QR code goes and says the window closed",
              "closed" in page.locator('[data-testid="phone-notice"]').inner_text())
        state["open"] = False

        # -- a registration, seen by the poll ----------------------------------------
        state["window_s"] = 300
        pair.click()
        page.wait_for_selector('[data-testid="phone-qr"]')
        state["passkeys"].append({"key": "k-bbbbbbbbbbbb", "created": time.time(), "last_used": None,
                                  "backed_up": True, "sessions": []})
        state["open"] = False
        until(lambda: page.locator('[data-testid="phone-qr"]').count() == 0, timeout=6)
        check("P-2 Phone: a registration closes the QR code and says so",
              "Paired" in page.locator('[data-testid="phone-notice"]').inner_text()
              and page.locator('[data-testid="phone-passkey"]').count() == 2)

        # -- End one session -----------------------------------------------------------
        before = len(posts)
        page.locator('[data-testid="phone-session"][data-id="s-2222222222222222"] [data-testid="phone-end"]').click()
        until(lambda: page.locator('[data-testid="phone-session"]').count() == 1, timeout=4)
        check("P-2 Phone: End ends that session", posts[before:] == ["/phone/sessions/s-2222222222222222/end"])

        # -- Revoke, with a confirmation ---------------------------------------------
        row = page.locator('[data-testid="phone-passkey"][data-key="k-aaaaaaaaaaaa"]')
        row.locator('[data-testid="phone-revoke"]').click()
        check("P-2 Phone: Revoke asks first, naming the sessions it ends",
              "end 1 session" in row.inner_text() and posts[-1] != "/phone/passkeys/k-aaaaaaaaaaaa/revoke")
        row.locator('[data-testid="phone-revoke-keep"]').click()
        check("P-2 Phone: Keep revokes nothing", row.locator('[data-testid="phone-revoke"]').count() == 1
              and "/phone/passkeys/k-aaaaaaaaaaaa/revoke" not in posts)
        row.locator('[data-testid="phone-revoke"]').click()
        row.locator('[data-testid="phone-revoke-confirm"]').click()
        until(lambda: page.locator('[data-testid="phone-passkey"]').count() == 1, timeout=4)
        check("P-2 Phone: Revoke reaches the daemon and the passkey leaves the list",
              posts[-1] == "/phone/passkeys/k-aaaaaaaaaaaa/revoke"
              and page.locator('[data-testid="phone-session"]').count() == 0)

        # -- three passkeys: no fourth --------------------------------------------------
        state["passkeys"] += [{"key": f"k-{c * 12}", "created": time.time(), "last_used": None,
                               "backed_up": False, "sessions": []} for c in "cd"]
        page.locator('[data-testid="settings-browser"]').click()
        page.locator('[data-testid="settings-phone"]').click()
        until(lambda: page.locator('[data-testid="phone-passkey"]').count() == 3, timeout=4)
        check("P-2 Phone: with 3 passkeys paired, Pair a phone is off",
              page.locator('[data-testid="phone-pair"]').is_disabled())
        state["passkeys"].pop()

        # -- an approval card disables the section ------------------------------------
        page.locator('[data-testid="settings-browser"]').click()
        page.locator('[data-testid="settings-phone"]').click()
        until(lambda: page.locator('[data-testid="phone-pair"]').is_enabled(), timeout=4)
        _approval(mock, "phone-card")
        until(lambda: page.locator('[data-testid="approval-card"]').count() == 1, timeout=4)
        until(lambda: page.locator('[data-testid="phone-pair"]').is_disabled(), timeout=4)
        check("P-2 Phone: an approval card disables pairing and revoking",
              page.locator('[data-testid="phone-pair"]').is_disabled()
              and page.locator('[data-testid="phone-revoke"]').first.is_disabled())
        page.keyboard.press("Escape")
        until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)

        # -- a revoke whose write failed: "revoked, not saved", and Save now -----------
        state["fail_revoke"] = True
        row = page.locator('[data-testid="phone-passkey"]').first
        target = row.get_attribute("data-key")
        row.locator('[data-testid="phone-revoke"]').click()
        row.locator('[data-testid="phone-revoke-confirm"]').click()
        until(lambda: page.locator('[data-testid="phone-unsaved-row"]').count() == 1, timeout=4)
        unsaved = page.locator('[data-testid="phone-unsaved-row"]')
        check("P-2 Phone: a revoke that could not be written shows 'revoked, not saved'",
              "revoked, not saved" in unsaved.inner_text() and unsaved.get_attribute("data-key") == target
              and page.locator(f'[data-testid="phone-passkey"][data-key="{target}"]').count() == 0)
        check("P-2 Phone: and the error is shown", "phone state unavailable" in
              page.locator('[data-testid="phone-error"]').inner_text())
        state["fail_revoke"] = False
        before = len(posts)
        unsaved.locator('[data-testid="phone-unsaved-retry"]').click()
        until(lambda: page.locator('[data-testid="phone-unsaved-row"]').count() == 0, timeout=4)
        check("P-2 Phone: Save now retries the revoke and the row goes once it lands",
              posts[before:] == [f"/phone/passkeys/{target}/revoke"])

        # -- the listener down ---------------------------------------------------------
        state["remote"] = {"port": 8408, "bound": False, "host_configured": False}
        page.locator('[data-testid="settings-browser"]').click()
        page.locator('[data-testid="settings-phone"]').click()
        until(lambda: page.locator('[data-testid="phone-listener"]').get_attribute("data-bound") == "false",
              timeout=4)
        check("P-2 Phone: no listener, no pairing, and it says why",
              page.locator('[data-testid="phone-pair"]').is_disabled()
              and "JARVIS_REMOTE_HOST" in page.locator('[data-testid="phone-listener"]').inner_text())
        check("P-2 Phone: no page errors and no control-listener requests", not errors and not mock.api_hits,
              str(errors[:3]))
    finally:
        ctx.close()


SHELL_HOST = "jarvis-box.tail-test.ts.net"


def shell_checks(browser, check, until):
    print("\nThe phone shell: pair, sign in and out with a virtual authenticator, end to end")
    import http.client
    import os
    import tempfile
    from unittest.mock import patch
    from urllib.parse import urlsplit
    from types import SimpleNamespace
    from jarvis import config
    from jarvis.v2.daemon import Daemon
    from jarvis.v2.model import ProviderName as PN
    from jarvis.v2.provider import Decision, SessionHandle, Usage
    from jarvis.v2.stores import Stores
    dist = REPO / "hud" / "dist"
    if not (dist / "mobile.html").is_file() or not (dist / "build-manifest.json").is_file():
        print("  skip  hud/dist has no phone shell (run npm run build in hud/)")
        return

    class Fake:
        def __init__(self, name):
            self.name = name

        def health(self):
            return True, "fake"

        def start(self, thread, brief, permit):
            return SessionHandle(thread.id, self.name, "fake:" + thread.id, SimpleNamespace(brief=brief))

        resume = start

        def usage(self, handle):
            return Usage()

        def send(self, handle, message):
            return iter(())

        def interrupt(self, handle):
            pass

        def close(self, handle):
            pass

    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "home").mkdir()
    env = {k: v for k, v in os.environ.items() if k != "JARVIS_REMOTE_HOST"}
    env.update({"HOME": str(root / "home"), "XDG_DATA_HOME": str(root / "xdg"),
                "JARVIS_CLAUDE_CREDENTIALS": str(root / "none.json")})
    patches = [patch.dict(os.environ, env, clear=True)]
    patches += [patch.object(config, name, value) for name, value in dict(
        V2_DATA_DIR=root / "data", ALLOWLIST_PATH=root / "allow.json", MODELS_PATH=root / "models.json",
        PROVIDER_DEFAULTS_PATH=root / "provider_defaults.json", DISCORD_GUILD_PATH=root / "guild.json",
        HUD_DEV=True, HUD_DIST=dist).items()]
    for guard in patches:
        guard.start()
    daemon = Daemon(Stores(), {name: Fake(name) for name in PN}, lambda *_: lambda *_: Decision.DENY, 0,
                    remote_host=SHELL_HOST)
    daemon.start()
    port = daemon.remote_port
    forwarded: list[str] = []

    def forward(route):
        request = route.request
        url = urlsplit(request.url)
        path = url.path + ("?" + url.query if url.query else "")
        forwarded.append(request.method + " " + url.path)
        sent = request.all_headers()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.putrequest(request.method, path, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", SHELL_HOST)
            for name in ("origin", "cookie", "content-type", "user-agent"):
                if sent.get(name):
                    conn.putheader(name, sent[name])
            body = request.post_data_buffer
            if body is not None:
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            response = conn.getresponse()
            data = response.read()
            headers = {k: v for k, v in response.getheaders()}
            route.fulfill(status=response.status, headers=headers, body=data)
        finally:
            conn.close()

    ctx = browser.new_context(viewport={"width": 390, "height": 844}, ignore_https_errors=True)
    ctx.route(re.compile(r".*"), lambda route: route.abort())
    ctx.route(re.compile(r"^https://" + re.escape(SHELL_HOST) + r"/"), forward)
    page = ctx.new_page()
    page.set_default_timeout(8000)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    cdp = ctx.new_cdp_session(page)
    cdp.send("WebAuthn.enable")
    authenticator = cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
        "protocol": "ctap2", "transport": "internal", "hasResidentKey": True,
        "hasUserVerification": True, "isUserVerified": True, "automaticPresenceSimulation": True}})["authenticatorId"]
    screen = lambda: page.evaluate("document.querySelector('[data-screen]')?.dataset.screen || ''")  # noqa: E731
    try:
        host = f"127.0.0.1:{daemon.face_port}"
        conn = http.client.HTTPConnection("127.0.0.1", daemon.face_port, timeout=5)
        conn.request("POST", "/phone/pair", b"{}", {"Host": host, "Origin": f"http://{host}",
                                                    "Content-Type": "application/json"})
        opened = json.loads(conn.getresponse().read())
        conn.close()
        page.goto(opened["url"])
        until(lambda: screen() == "Pair this phone", timeout=8)
        check("P-2 shell: /m/pair#code shows the pairing card, the code gone from the address",
              page.evaluate("location.hash") == "" and page.evaluate("location.pathname") == "/m/pair")
        page.locator("button").click()
        until(lambda: screen() in ("Paired", "Not paired"), timeout=8)
        check("P-2 shell: a passkey registers with the window's code", screen() == "Paired",
              page.locator("main").inner_text())
        credentials = cdp.send("WebAuthn.getCredentials", {"authenticatorId": authenticator})["credentials"]
        check("P-2 shell: the passkey is resident, for the ts.net rpId",
              len(credentials) == 1 and credentials[0]["isResidentCredential"] and credentials[0]["rpId"] == SHELL_HOST)
        check("P-2 shell: pairing alone signs nothing in",
              not [c for c in ctx.cookies() if c["name"] == "__Host-jarvis_m"])
        page.locator("button").click()
        until(lambda: screen() in ("Signed in · paired", "Jarvis"), timeout=8)
        check("P-2 shell: then it signs in with the passkey", screen() == "Signed in · paired",
              page.locator("main").inner_text())
        cookie = [c for c in ctx.cookies() if c["name"] == "__Host-jarvis_m"]
        check("P-2 shell: the browser holds __Host-jarvis_m: HttpOnly, Secure, SameSite=Strict, Path=/",
              len(cookie) == 1 and cookie[0]["httpOnly"] and cookie[0]["secure"]
              and cookie[0]["sameSite"] == "Strict" and cookie[0]["path"] == "/"
              and cookie[0]["domain"] == SHELL_HOST, str(cookie))
        check("P-2 shell: page script cannot read the session cookie",
              "jarvis_m" not in page.evaluate("document.cookie"))
        page.goto(f"https://{SHELL_HOST}/m/")
        until(lambda: screen() == "Signed in · paired", timeout=8)
        check("P-2 shell: /m/ opens signed in", screen() == "Signed in · paired")
        status = page.evaluate("fetch('/m/api/inbox').then(r => r.status)")
        check("P-2 shell: the rest of /m/api is 404 to a signed-in phone until P-3", status == 404, str(status))
        page.locator("button").click()
        until(lambda: screen() == "Jarvis", timeout=8)
        check("P-2 shell: Sign out ends the session",
              page.evaluate("fetch('/m/api/me').then(r => r.status)") == 401)
        cdp.send("WebAuthn.setUserVerified", {"authenticatorId": authenticator, "isUserVerified": False})
        page.locator("button").click()
        until(lambda: page.locator("button").is_enabled() if screen() == "Jarvis" else False, timeout=10)
        check("P-2 shell: without user verification the sign-in is refused",
              screen() == "Jarvis" and page.evaluate("fetch('/m/api/me').then(r => r.status)") == 401,
              page.locator("main").inner_text())
        cdp.send("WebAuthn.setUserVerified", {"authenticatorId": authenticator, "isUserVerified": True})
        page.locator("button").click()
        until(lambda: screen() == "Signed in · paired", timeout=8)
        check("P-2 shell: with it back, the same passkey signs in again", screen() == "Signed in · paired")
        key = daemon.remote_auth.status()["passkeys"][0]["key"]
        daemon.remote_auth.revoke(key)
        check("P-2 shell: a revoke at the desk ends the phone's session at once",
              page.evaluate("fetch('/m/api/me').then(r => r.status)") == 401)
        check("P-2 shell: no page errors", not errors, str(errors[:3]))
        check("P-2 shell: only the ts.net origin was reached", all(f.split(" ")[1].startswith("/m/") for f in forwarded),
              str(forwarded[:5]))
    finally:
        ctx.close()
        daemon.stop()
        for guard in reversed(patches):
            guard.stop()
        tmp.cleanup()


def main():
    from playwright.sync_api import sync_playwright
    from tests.face import hud_v2_check as v2
    from tests.face.hud_v2_mock import MockDaemon
    mock = MockDaemon(0).start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                phone_checks(browser, mock, f"http://127.0.0.1:{mock.port}", v2.check, v2.until, v2.guard,
                             v2.FAKE_RECOGNIZER)
            except Exception as exc:
                v2.check("Phone scenario completes", False, f"{type(exc).__name__}: {exc}")
            try:
                shell_checks(browser, v2.check, v2.until)
            except Exception as exc:
                v2.check("Phone shell scenario completes", False, f"{type(exc).__name__}: {exc}")
            finally:
                browser.close()
    finally:
        mock.stop()
    print(f"{v2.CHECKS} phone checks, {len(v2.FAILURES)} failures")
    return bool(v2.FAILURES)


if __name__ == "__main__":
    sys.exit(main())
