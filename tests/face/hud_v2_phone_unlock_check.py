"""The phone's unlock window (WS-5c, PX-5 of plan C §5), end to end in a real headless Chromium.

The built `mobile.html` is served by a real daemon's remote listener, reached as `https://<host>` (the
context forwards that origin to the loopback port with the trusted Host, as `tailscale serve` does), and
Chromium's virtual authenticator stands in for the iPhone (user verification on), as in
`hud_v2_phone_answer_check.py`. `navigator.credentials.get` is wrapped to count its calls (the real one
still answers). Real approvals wait on the daemon's real broker:

  - **locked:** one tap on AUTHORIZE is unlock/options, the authenticator, unlock, then the answer with
    its op token; no answer POST goes first; the pill turns to "Unlocked until";
  - **unlocked:** the next AUTHORIZE asks the authenticator nothing and sends the answer alone;
  - **Lock now** from the pill's menu: `POST /m/api/lock`, the window closed at Jarvis, the pill Locked;
  - **a stale client's 423** (plan §8's two-tab test: the window is ended at Jarvis by another tab's Lock
    now while this one still believes it open): 423, then ONE unlock and ONE resend of the same frozen
    request and op token, and the approval is allowed once;
  - **"Unlock and send"**: when the tap's activation has lapsed (simulated: `userActivation.isActive` false
    and one `credentials.get` refusal, as iOS gives), the button reads "Unlock and send", and its tap sends
    the answer once, unlocking first;
  - Deny never asks the authenticator; a gated answer never carries an assertion.

Skipped when `hud/dist` has no phone shell (run `npm run build` in hud/).

Run: PYTHONPATH=. .venv/bin/python tests/face/hud_v2_phone_unlock_check.py
"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.face.hud_v2_phone_answer_check import FakeSystem, SHELL_HOST  # noqa: E402

# Counts every `credentials.get` and, while `window.__lapse` is set, plays iOS refusing it because the tap's
# activation lapsed (`userActivation.isActive` false and one NotAllowedError). The real call answers otherwise.
WRAP_WEBAUTHN = """
window.__gets = 0;
window.__lapse = false;
(() => {
  const creds = navigator.credentials;
  const real = creds.get.bind(creds);
  creds.get = async (options) => {
    window.__gets += 1;
    if (window.__lapse) throw new DOMException("The request is not allowed by the user agent", "NotAllowedError");
    return real(options);
  };
  const ua = navigator.userActivation;
  if (ua) {
    try {
      Object.defineProperty(navigator, "userActivation", { configurable: true, get: () => window.__lapse
        ? { isActive: false, hasBeenActive: true } : ua });
    } catch (e) {}
  }
})();
"""


def run(browser, check, until):
    print("\nThe phone's unlock window (WS-5c): locked, unlocked, Lock now, a stale client's 423, Unlock and send")
    from jarvis import config
    from jarvis.v2 import remote_api
    from jarvis.v2.approvals import ApprovalRequest
    from jarvis.v2.daemon import Daemon
    from jarvis.v2.model import ProviderName as PN, Role
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
    patches.append(patch.object(remote_api, "RETRY_MS", 300))
    for guard in patches:
        guard.start()
    daemon = Daemon(Stores(), {name: Fake(name) for name in PN}, lambda *_: lambda *_: Decision.DENY, 0,
                    remote_host=SHELL_HOST)
    daemon.start()
    daemon.secure.run = FakeSystem(daemon)
    port = daemon.remote_port
    project = daemon.stores.projects.create("Calc", str(root))
    thread = daemon.stores.threads.create(project.id, Role.CHAT, PN.CLAUDE, cwd=str(root))
    posted: list[tuple[str, dict, int]] = []
    hooks: dict = {"before": None}

    def raw(method, path, cookie, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", SHELL_HOST)
            conn.putheader("Origin", "https://" + SHELL_HOST)
            if cookie:
                conn.putheader("Cookie", cookie)
            data = json.dumps(body).encode() if body is not None else None
            if data is not None:
                conn.putheader("Content-Type", "application/json")
                conn.putheader("Content-Length", str(len(data)))
            conn.endheaders(data)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def forward(route):
        request = route.request
        url = urlsplit(request.url)
        path = url.path + ("?" + url.query if url.query else "")
        sent = request.all_headers()
        if url.path == "/m/api/events":
            return route.fulfill(**stream_window(path, sent))
        if hooks["before"] is not None:
            hooks["before"](request.method, url.path, sent.get("cookie", ""))
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
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
            if request.method == "POST":
                try:
                    posted.append((url.path, json.loads(body or b"{}"), response.status))
                except ValueError:
                    posted.append((url.path, {}, response.status))
            route.fulfill(status=response.status, headers={k: v for k, v in response.getheaders()}, body=data)
        finally:
            conn.close()

    def stream_window(path, sent):
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            head = f"GET {path} HTTP/1.1\r\nHost: {SHELL_HOST}\r\n"
            if sent.get("cookie"):
                head += f"Cookie: {sent['cookie']}\r\n"
            sock.sendall((head + "\r\n").encode())
            raw_, end = b"", time.monotonic() + 1.0
            sock.settimeout(0.2)
            while time.monotonic() < end:
                try:
                    chunk = sock.recv(65536)
                except (socket.timeout, TimeoutError):
                    if b"\r\n\r\n" in raw_ and not raw_.startswith(b"HTTP/1.0 200"):
                        break
                    continue
                if not chunk:
                    break
                raw_ += chunk
        finally:
            sock.close()
        top, _, data = raw_.partition(b"\r\n\r\n")
        lines = top.decode("latin-1").split("\r\n")
        status = int(lines[0].split(" ")[1]) if lines and lines[0].startswith("HTTP/") else 502
        headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
        return {"status": status, "headers": headers, "body": data}

    def ask(command):
        request = ApprovalRequest(tool="Bash", args={"command": command}, command=command, provider="claude",
                                  thread_id=thread.id)
        out: dict = {}
        threading.Thread(target=lambda: out.setdefault("decision", daemon.approvals.ask(request)), daemon=True).start()
        until(lambda: any(r.req_id == request.req_id for r in daemon.approvals.pending()), timeout=5)
        return request, out

    ctx = browser.new_context(viewport={"width": int(os.environ.get("PX5_WIDTH", "390")), "height": 844},
                              ignore_https_errors=True, service_workers="block")
    ctx.route(re.compile(r".*"), lambda route: route.abort())
    ctx.route(re.compile(r"^https://" + re.escape(SHELL_HOST) + r"/"), forward)
    ctx.add_init_script(WRAP_WEBAUTHN)
    page = ctx.new_page()
    page.set_default_timeout(10000)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    cdp = ctx.new_cdp_session(page)
    cdp.send("WebAuthn.enable")
    cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
        "protocol": "ctap2", "transport": "internal", "hasResidentKey": True,
        "hasUserVerification": True, "isUserVerified": True, "automaticPresenceSimulation": True}})
    screen = lambda: page.evaluate("document.querySelector('[data-screen]')?.dataset.screen || ''")  # noqa: E731
    card = '[data-testid="approval-card"]'
    pill = page.locator('[data-testid="m-lock-pill"]')
    gets = lambda: page.evaluate("window.__gets")  # noqa: E731

    def pump(predicate, timeout=10.0):
        end = time.monotonic() + timeout
        while not predicate() and time.monotonic() < end:
            page.wait_for_timeout(50)
        return predicate()

    def open_card(request):
        page.locator('[data-testid="m-tab-inbox"]').click()
        row = page.locator(f'[data-testid="m-inbox-row"][data-id="appr:{request.req_id}"]')
        pump(lambda: row.count() == 1, timeout=8)
        row.locator('[data-testid="m-item-review"]').click()
        pump(lambda: page.locator(card).count() == 1
             and page.locator(f"{card} .m-card-body").inner_text() not in ("", "Loading the card…"), timeout=8)

    def session():
        auth = daemon.remote_auth
        digest, row = next(iter(auth._sessions.items()))
        return {"token_sha256": digest, "id": row["id"]}

    def answer_path(request):
        return f"/m/api/inbox/appr%3A{request.req_id}/answer"

    try:
        host = f"127.0.0.1:{daemon.face_port}"
        conn = http.client.HTTPConnection("127.0.0.1", daemon.face_port, timeout=5)
        conn.request("POST", "/phone/pair", b"{}", {"Host": host, "Origin": f"http://{host}",
                                                    "Content-Type": "application/json"})
        opened = json.loads(conn.getresponse().read())
        conn.close()
        page.goto(opened["url"])
        pump(lambda: screen() == "Pair this phone", timeout=8)
        page.locator("button").click()
        pump(lambda: screen() == "Paired", timeout=8)
        before_gets = gets()
        page.locator("button").click()
        pump(lambda: screen() == "app", timeout=8)
        check("setup: paired and signed in; every §4.1 check passes", screen() == "app" and daemon.secure.check()["ok"])
        pump(lambda: pill.count() == 1)
        check("pill: Locked after sign-in (a restart or a new session starts locked)",
              pill.get_attribute("data-state") == "locked" and pill.inner_text() == "Locked", pill.inner_text())
        signin_gets = gets() - before_gets

        # -- locked: one tap, the authenticator, then the action ------------------------------------
        a, a_out = ask("npm test")
        open_card(a)
        start, g0 = len(posted), gets()
        page.locator(f'{card} [data-testid="approval-allow"]').click()
        pump(lambda: "decision" in a_out)
        trail = posted[start:]
        check("locked: one tap is unlock/options, the authenticator, unlock, then the answer (no answer POST first)",
              [p for p, _b, _s in trail] == ["/m/api/unlock/options", "/m/api/unlock", answer_path(a)]
              and gets() - g0 == 1 and a_out.get("decision") == Decision.ALLOW,
              f"{[(p, s) for p, _b, s in trail]} gets={gets() - g0} {a_out}")
        check("locked: the answer carries its op token and no assertion",
              trail and sorted(trail[-1][1]) == ["decision", "op"] and str(trail[-1][1]["op"]).startswith("o1."))
        pump(lambda: pill.get_attribute("data-state") == "unlocked")
        check("pill: Unlocked until HH:MM, from the unlock", pill.get_attribute("data-state") == "unlocked"
              and re.fullmatch(r"Unlocked until \d{1,2}:\d{2}( [AP]M)?", pill.inner_text().strip()) is not None,
              pill.inner_text())
        check("the window is open at Jarvis (unlock_left > 3500 s)", daemon.remote_auth.unlock_left(session()) > 3500)

        # -- unlocked: no credentials.get -----------------------------------------------------------
        b, b_out = ask("npm run lint")
        open_card(b)
        start, g0 = len(posted), gets()
        page.locator(f'{card} [data-testid="approval-allow"]').click()
        pump(lambda: "decision" in b_out)
        check("unlocked: AUTHORIZE asks the authenticator nothing and sends the answer alone",
              [p for p, _b, _s in posted[start:]] == [answer_path(b)] and gets() == g0
              and b_out.get("decision") == Decision.ALLOW, f"{[p for p, _b, _s in posted[start:]]} gets={gets() - g0}")

        # -- Deny never asks ------------------------------------------------------------------------
        c, c_out = ask("rm -rf build")
        open_card(c)
        g0 = gets()
        page.locator(f'{card} [data-testid="approval-deny"]').click()
        pump(lambda: "decision" in c_out)
        check("Deny: the session only, no authenticator", c_out.get("decision") == Decision.DENY and gets() == g0)
        pump(lambda: page.locator(card).count() == 0)

        # -- a stale client's 423: Lock now in another tab while this one believes it open ---------
        d, d_out = ask("make deploy")
        open_card(d)
        check("stale client: this tab still believes it is unlocked", pill.get_attribute("data-state") == "unlocked")
        locked_elsewhere: list = []

        def lock_from_tab_b(method, path, cookie):
            # As the answer arrives: the other tab's Lock now, the real route, with the same session's cookie.
            if method == "POST" and path == answer_path(d) and not locked_elsewhere:
                locked_elsewhere.append(raw("POST", "/m/api/lock", cookie, {}))
        hooks["before"] = lock_from_tab_b
        start, g0 = len(posted), gets()
        page.locator(f'{card} [data-testid="approval-allow"]').click()
        pump(lambda: "decision" in d_out)
        page.wait_for_timeout(500)
        hooks["before"] = None
        trail = posted[start:]
        acts = [(b_, s) for p, b_, s in trail if p == answer_path(d)]
        check("stale client: the other tab's Lock now landed at Jarvis (200)",
              locked_elsewhere and locked_elsewhere[0][0] == 200, str(locked_elsewhere))
        check("stale client: 423, then ONE unlock (the authenticator once), then ONE resend; allowed once",
              [p for p, _b, _s in trail] == [answer_path(d), "/m/api/unlock/options", "/m/api/unlock", answer_path(d)]
              and [s for _b, s in acts] == [423, 200] and gets() - g0 == 1 and d_out.get("decision") == Decision.ALLOW,
              f"{[(p, s) for p, _b, s in trail]} gets={gets() - g0}")
        check("stale client: the resend is the same frozen request and op token",
              len(acts) == 2 and acts[0][0] == acts[1][0], str(acts)[:300])

        # -- Lock now from the pill's menu -----------------------------------------------------------
        pump(lambda: page.locator('[data-testid="m-sheet"]').count() == 0)
        pump(lambda: pill.get_attribute("data-state") == "unlocked")
        start = len(posted)
        pill.click()
        page.locator('[data-testid="m-lock-now"]').wait_for()
        first = page.evaluate("""() => [...document.querySelectorAll('[data-testid="m-menu-panel"] > *')][0]?.dataset.testid""")
        check("Lock now: the first row of the menu the pill opens", first == "m-lock-now", str(first))
        page.locator('[data-testid="m-lock-now"]').click()
        pump(lambda: pill.get_attribute("data-state") == "locked")
        check("Lock now: POST /m/api/lock, the window closed at Jarvis, the pill Locked",
              [p for p, _b, _s in posted[start:]] == ["/m/api/lock"] and daemon.remote_auth.unlock_left(session()) == 0
              and pill.inner_text() == "Locked", f"{[p for p, _b, _s in posted[start:]]} {pill.inner_text()}")

        # -- Unlock and send: the tap's activation lapsed -----------------------------------------
        e, e_out = ask("make release")
        open_card(e)
        page.evaluate("window.__lapse = true")
        start, g0 = len(posted), gets()
        allow = page.locator(f'{card} [data-testid="approval-allow"]')
        allow.click()
        pump(lambda: allow.inner_text() == "Unlock and send")
        check("Unlock and send: Face ID refused for a lapsed tap; the button says so and nothing was answered",
              allow.inner_text() == "Unlock and send" and [p for p, _b, _s in posted[start:]] == ["/m/api/unlock/options"]
              and "decision" not in e_out
              and "Unlock and send" in page.locator('[data-testid="m-card-notice"]').inner_text(),
              f"{allow.inner_text()} {[p for p, _b, _s in posted[start:]]}")
        page.evaluate("window.__lapse = false")
        allow.click()
        pump(lambda: "decision" in e_out)
        page.wait_for_timeout(400)
        trail = posted[start:]
        check("Unlock and send: its tap unlocks first, then sends the answer once; allowed",
              [p for p, _b, _s in trail] == ["/m/api/unlock/options", "/m/api/unlock/options", "/m/api/unlock", answer_path(e)]
              and e_out.get("decision") == Decision.ALLOW, f"{[(p, s) for p, _b, s in trail]}")
        check("no page errors", not errors, str(errors[:3]))
        check("no answer ever carried an assertion; only /m/api/unlock did",
              all("assertion" not in b_ for p, b_, _s in posted if p != "/m/api/unlock")
              and all(sorted(b_) == ["assertion"] for p, b_, _s in posted if p == "/m/api/unlock"))
        check("no per-action /options route was asked (only unlock's, and sign-in's own)",
              not any(p.endswith("/options") and p not in ("/m/api/unlock/options", "/m/api/pair/options",
                                                           "/m/api/login/options") for p, _b, _s in posted),
              str(sorted({p for p, _b, _s in posted if p.endswith("/options")})))
        check("sign-in took the authenticator once (its own ceremony)", signin_gets >= 1, str(signin_gets))
    finally:
        ctx.close()
        daemon.stop()
        for guard in reversed(patches):
            guard.stop()
        tmp.cleanup()


def main():
    from playwright.sync_api import sync_playwright
    from tests.face import hud_v2_check as v2
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            run(browser, v2.check, v2.until)
        except Exception as exc:
            v2.check("Phone unlock scenario completes", False, f"{type(exc).__name__}: {exc}")
        finally:
            browser.close()
    print(f"{v2.CHECKS} phone unlock checks, {len(v2.FAILURES)} failures")
    return bool(v2.FAILURES)


if __name__ == "__main__":
    sys.exit(main())
