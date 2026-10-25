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
against the real `webauthn` verification. Signed in, the app (P-3,
`app_checks`): the Inbox, Chats and Tasks tabs over the real read API, with
hostile text in an origin, a command, a thread title, the owner's words, a
plan, a brief and a patch drawn as text; a reply's Markdown sanitized; no
answer control anywhere; a live reply over the real event stream (each
window of it a finite answer, since a route fulfills whole bodies); the
offline banner; 44 px tap targets; the diff folded file by file; C1's CSP
unwidened. Skipped when `hud/dist` has no phone shell (run `npm run build`
in hud/).

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

        # -- §4.1 failing (P-4): a red "limited: ..." line, in the daemon's words -------
        state["remote"] = {"port": 18408, "bound": True, "host_configured": True,
                           "secure": {"ok": False, "failed": "serve", "reason": "Funnel is on",
                                      "label": "tailscale serve is not the only path, or Funnel is on"}}
        page.locator('[data-testid="settings-browser"]').click()
        page.locator('[data-testid="settings-phone"]').click()
        until(lambda: page.locator('[data-testid="phone-secure"]').count() == 1, timeout=4)
        line = page.locator('[data-testid="phone-secure"]')
        color = line.evaluate("e => getComputedStyle(e).color")
        check("P-4 Phone: a failing §4.1 check shows a red 'limited: ...' line naming it",
              line.get_attribute("data-ok") == "false"
              and "limited: tailscale serve is not the only path, or Funnel is on (Funnel is on)" in line.inner_text()
              and color == "rgb(248, 113, 113)", f"{line.inner_text()} {color}")
        state["remote"] = dict(state["remote"], secure={"ok": True, "failed": None, "reason": "", "label": ""})
        page.locator('[data-testid="settings-browser"]').click()
        page.locator('[data-testid="settings-phone"]').click()
        until(lambda: page.locator('[data-testid="phone-secure"]').get_attribute("data-ok") == "true", timeout=4)
        check("P-4 Phone: passing, it says the phone has the HUD's authority",
              "HUD's authority" in page.locator('[data-testid="phone-secure"]').inner_text())

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

# Untrusted text the app must draw as text (P-3): a model's words in a row's
# origin and detail, a question, a thread title, a task brief, a reply.
HOSTILE = '<img src=x onerror="window.__pwned=7"><b>bold</b><script>window.__pwned=8</script>'
REPLY = ("**Done.** <script>window.__pwned=9</script> <img src=x onerror=\"window.__pwned=10\"> "
         "[link](javascript:window.__pwned=11) and `code` and $x^2$")
ANSWER_WORDS = re.compile(r"\b(allow|deny|always|approve|send|withdraw|reply|resume|cancel|submit)\b", re.I)
MARKUP = "img, script, b, svg, iframe, object, embed, [onerror], [onload], a[href^='javascript']"


def _populate(daemon, root):
    """A project with chats, a running and a blocked task (one with a git
    worktree and a change), and the inbox's sources, all holding hostile text."""
    import subprocess
    from jarvis.v2 import worktrees
    from jarvis.v2.approvals import ApprovalRequest
    from jarvis.v2.model import ProviderName as PN, Role, TaskState
    repo = root / "project"
    repo.mkdir()
    (repo / "app.py").write_text("def f(n):\n    return n\n")
    for args in (["init", "-q"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "T"],
                 ["add", "."], ["commit", "-qm", "base"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    stores = daemon.stores
    project = stores.projects.create("Calc", str(repo))
    chats = []
    for i in range(7):
        t = stores.threads.create(project.id, Role.CHAT, PN.CLAUDE, cwd=str(repo))
        t.updated = f"2026-10-25T0{i}:00:00+00:00"
        stores.threads.save(t)
        stores.threads.set_title(t.id, HOSTILE if i == 6 else f"chat {i}", "owner")
        chats.append(stores.threads.get(t.id))
    chat = chats[-1]
    for record in ({"kind": "user", "data": {"typed": "owner words " + HOSTILE, "via": "hud"}},
                   {"kind": "tool_started", "data": {"name": "Bash", "args": {"command": "TOOL-ARGS-NEVER"}}},
                   {"kind": "text", "data": {"text": REPLY}}):
        stores.threads._append(chat.id, "log.jsonl", {"at": "2026-10-25T10:00:00+00:00", "thread_id": chat.id, **record})
    running = stores.tasks.create(project.id, "Running task " + HOSTILE)
    running = worktrees.ensure(running, project, stores)
    (Path(running.worktree) / "app.py").write_text("def f(n):\n    return n + 1\n")
    (Path(running.worktree) / "notes.txt").write_text("a note " + HOSTILE + "\n")
    live = stores.tasks.get(running.id)
    live.plan = ["read the code", "change it " + HOSTILE]
    stores.tasks.save(live)
    for state in (TaskState.CLARIFYING, TaskState.PLANNED, TaskState.RUNNING):
        stores.tasks.transition(running.id, state)
    blocked = stores.tasks.create(project.id, "Blocked task")
    stores.tasks.save(blocked)
    for state in (TaskState.CLARIFYING, TaskState.BLOCKED):
        stores.tasks.transition(blocked.id, state)
    public = ApprovalRequest(tool="Bash", args={"command": "rm -rf build"}, command="rm -rf build " + HOSTILE,
                             reason=HOSTILE, provider="claude", thread_id=chat.id,
                             origin='task 0000beef "' + HOSTILE + '"')
    public.req_id, public.code, public.asked_at, public.timeout_s = "req-pub", "abcd", "2026-10-25T10:00:00+00:00", 600.0
    private = ApprovalRequest(tool="hud_browser_act", args={"url": "PRIVATE-NEVER"}, command="PRIVATE-NEVER",
                              hud_only=True, browser={"url": "PRIVATE-NEVER"})
    private.req_id, private.code, private.asked_at, private.timeout_s = "req-priv", "efgh", "2026-10-25T10:00:00+00:00", 600.0
    daemon.approvals.pending = lambda: [public, private]
    return {"project": project, "chat": chat, "chats": chats, "running": running, "blocked": blocked}


def shell_checks(browser, check, until):
    print("\nThe phone shell: pair, sign in, the app (P-3), sign out, with a virtual authenticator, end to end")
    import http.client
    import os
    import socket
    import tempfile
    import threading
    from unittest.mock import patch
    from urllib.parse import urlsplit
    from types import SimpleNamespace
    from jarvis import config
    from jarvis.v2 import remote_api
    from jarvis.v2.daemon import Daemon
    from jarvis.v2.model import ProviderName as PN
    from jarvis.v2.provider import Decision, SessionHandle, Usage
    from jarvis.v2.remote_listener import REMOTE_POLICY
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
    # The stream reconnects quickly here, because each window of it is a
    # finite response below (a route can only fulfill a whole body).
    patches.append(patch.object(remote_api, "RETRY_MS", 300))
    for guard in patches:
        guard.start()
    daemon = Daemon(Stores(), {name: Fake(name) for name in PN}, lambda *_: lambda *_: Decision.DENY, 0,
                    remote_host=SHELL_HOST)
    daemon.start()
    port = daemon.remote_port
    world = _populate(daemon, root)
    forwarded: list[str] = []
    streamed: list[bytes] = []
    shell_headers: dict = {}
    net = {"offline": False}

    def forward(route):
        request = route.request
        url = urlsplit(request.url)
        path = url.path + ("?" + url.query if url.query else "")
        forwarded.append(request.method + " " + url.path)
        if net["offline"] and url.path.startswith("/m/api/"):
            return route.abort("internetdisconnected")
        sent = request.all_headers()
        if url.path == "/m/api/events":
            return route.fulfill(**stream_window(path, sent))
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
            headers = {k: v for k, v in response.getheaders()}
            data = response.read()
            if url.path == "/m/":
                shell_headers.update(headers)
            route.fulfill(status=response.status, headers=headers, body=data)
        finally:
            conn.close()

    def stream_window(path, sent):
        """One window of the live stream as a finite answer: what the remote
        listener sends in about a second (or until it ends the stream). The
        page's EventSource reads it, then reconnects after RETRY_MS."""
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            head = f"GET {path} HTTP/1.1\r\nHost: {SHELL_HOST}\r\n"
            if sent.get("cookie"):
                head += f"Cookie: {sent['cookie']}\r\n"
            sock.sendall((head + "\r\n").encode())
            raw, end = b"", time.monotonic() + 1.0
            sock.settimeout(0.2)
            while time.monotonic() < end:
                try:
                    chunk = sock.recv(65536)
                except (socket.timeout, TimeoutError):
                    if b"\r\n\r\n" in raw and not raw.startswith(b"HTTP/1.0 200"):
                        break
                    continue
                if not chunk:
                    break
                raw += chunk
        finally:
            sock.close()
        top, _, data = raw.partition(b"\r\n\r\n")
        lines = top.decode("latin-1").split("\r\n")
        status = int(lines[0].split(" ")[1]) if lines and lines[0].startswith("HTTP/") else 502
        headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
        if status == 200:
            streamed.append(data)
        return {"status": status, "headers": headers, "body": data}

    ctx = browser.new_context(viewport={"width": 390, "height": 844}, ignore_https_errors=True)
    ctx.route(re.compile(r".*"), lambda route: route.abort())
    ctx.route(re.compile(r"^https://" + re.escape(SHELL_HOST) + r"/"), forward)
    ctx.add_init_script("window.__csp = []; document.addEventListener('securitypolicyviolation', "
                        "e => window.__csp.push(e.violatedDirective + ' ' + (e.blockedURI || '')));")
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
    main = '[data-testid="m-main"]'
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
        until(lambda: screen() in ("app", "Jarvis"), timeout=8)
        check("P-2 shell: then it signs in with the passkey, and the app opens", screen() == "app",
              page.locator("main").first.inner_text())
        cookie = [c for c in ctx.cookies() if c["name"] == "__Host-jarvis_m"]
        check("P-2 shell: the browser holds __Host-jarvis_m: HttpOnly, Secure, SameSite=Strict, Path=/",
              len(cookie) == 1 and cookie[0]["httpOnly"] and cookie[0]["secure"]
              and cookie[0]["sameSite"] == "Strict" and cookie[0]["path"] == "/"
              and cookie[0]["domain"] == SHELL_HOST, str(cookie))
        check("P-2 shell: page script cannot read the session cookie",
              "jarvis_m" not in page.evaluate("document.cookie"))
        page.goto(f"https://{SHELL_HOST}/m/")
        until(lambda: screen() == "app", timeout=8)
        check("P-2 shell: /m/ opens signed in", screen() == "app")
        check("P-3 CSP: the shell's policy is C1's, unwidened (style-src 'self', no 'unsafe-inline')",
              shell_headers.get("Content-Security-Policy") == REMOTE_POLICY
              and "'unsafe-inline'" not in REMOTE_POLICY, str(shell_headers.get("Content-Security-Policy")))
        app_checks(page, daemon, world, check, until, streamed, net, main)
        check("P-3 app: no page errors", not errors, str(errors[:3]))
        violations = page.evaluate("window.__csp")
        print(f"  note  {len(violations)} CSP refusal(s) in the app: {sorted(set(violations))[:3]}")
        check("P-3 app: nothing the app draws needs more than C1's policy (inline styles, if any, are refused "
              "and dropped)", all(v.startswith("style-src") for v in violations), str(violations[:5]))
        page.locator('[data-testid="m-signout"]').click()
        until(lambda: screen() == "Jarvis", timeout=8)
        check("P-2 shell: Sign out ends the session",
              page.evaluate("fetch('/m/api/me').then(r => r.status)") == 401)
        cdp.send("WebAuthn.setUserVerified", {"authenticatorId": authenticator, "isUserVerified": False})
        page.locator("button").click()
        until(lambda: page.locator("button").is_enabled() if screen() == "Jarvis" else False, timeout=10)
        check("P-2 shell: without user verification the sign-in is refused",
              screen() == "Jarvis" and page.evaluate("fetch('/m/api/me').then(r => r.status)") == 401,
              page.locator("main").first.inner_text())
        cdp.send("WebAuthn.setUserVerified", {"authenticatorId": authenticator, "isUserVerified": True})
        page.locator("button").click()
        until(lambda: screen() == "app", timeout=8)
        check("P-2 shell: with it back, the same passkey signs in again", screen() == "app")
        key = daemon.remote_auth.status()["passkeys"][0]["key"]
        daemon.remote_auth.revoke(key)
        check("P-2 shell: a revoke at the desk ends the phone's session at once",
              page.evaluate("fetch('/m/api/me').then(r => r.status)") == 401)
        until(lambda: screen() == "Jarvis", timeout=10)
        check("P-3 app: after a revoke the app stops and asks for a sign-in", screen() == "Jarvis",
              page.locator("main").first.inner_text())
        check("P-2 shell: no page errors", not errors, str(errors[:3]))
        check("P-2 shell: only the ts.net origin was reached", all(f.split(" ")[1].startswith("/m/") for f in forwarded),
              str(forwarded[:5]))
    finally:
        ctx.close()
        daemon.stop()
        for guard in reversed(patches):
            guard.stop()
        tmp.cleanup()


def app_checks(page, daemon, world, check, until, streamed, net, main):
    """The signed-in app (P-3): the three tabs, untrusted text as text, the
    sanitized Markdown, no answer control, live updates over the real stream,
    the offline banner, touch targets, the diff folded file by file."""
    import threading
    chat, running = world["chat"], world["running"]

    def text(selector=main):
        return page.locator(selector).first.inner_text()

    def clean():
        return (page.locator(f"{main} :is({MARKUP})").count() == 0
                and page.evaluate("window.__pwned === undefined"))

    def answer_controls():
        names = page.evaluate("Array.from(document.querySelectorAll('button')).map(b => b.textContent)")
        fields = page.locator("input, textarea, select, form, [contenteditable]").count()
        return [n for n in names if ANSWER_WORDS.search(n or "")], fields

    def small_targets():
        return page.evaluate("""Array.from(document.querySelectorAll('.app button, .app summary'))
            .filter(e => e.offsetParent !== null).map(e => [e.textContent, e.getBoundingClientRect().height])
            .filter(([, h]) => h < 44)""")

    # -- Inbox ---------------------------------------------------------------------------
    until(lambda: page.locator('[data-testid="m-inbox-row"]').count() >= 3, timeout=8)
    rows = page.locator('[data-testid="m-inbox-row"]')
    check("P-3 Inbox: the rows render (approvals, the private card, the blocked task)",
          rows.count() >= 3 and page.locator('[data-testid="m-inbox-row"][data-kind="approval"]').count() == 2,
          text())
    check("P-3 Inbox: origin, command and reason are text, never markup",
          HOSTILE in page.locator('[data-testid="m-origin"]').first.inner_text()
          and HOSTILE in page.locator('[data-testid="m-command"]').first.inner_text() and clean())
    check("P-3 Inbox: a private card shows its private projection only",
          page.locator('[data-testid="m-private"]').count() == 1 and "PRIVATE-NEVER" not in text())
    # P-4: an approval row opens its answering card (hud_v2_phone_answer_check.py);
    # nothing on the list itself allows, denies or always-allows.
    check("P-4 Inbox: approvals offer 'Open the card to answer'",
          page.locator('[data-testid="m-open-card"]', has_text="Open the card to answer").count() == 2)
    bad, fields = answer_controls()
    check("P-3 Inbox: no answer control on the list itself (no Allow, Deny, Always, no input)",
          not bad and fields == 0, f"{bad} {fields}")
    check("P-3 Inbox: the tab counts what waits", "Inbox · " in page.locator('[data-testid="m-tab-inbox"]').inner_text())
    check("P-3 app: every tap target is at least 44 px tall (Inbox)", not small_targets(), str(small_targets()))

    # -- Chats ---------------------------------------------------------------------------
    page.locator('[data-testid="m-tab-chats"]').click()
    until(lambda: page.locator('[data-testid="m-chat"]').count() >= 1, timeout=8)
    check("P-3 Chats: the five most recent chats of the project, the rest behind Show more",
          page.locator('[data-testid="m-chat"]').count() == 5
          and page.locator('[data-testid="m-more"]').inner_text() == "Show 2 more")
    check("P-3 Chats: a thread title is text", HOSTILE in page.locator('[data-testid="m-chat"]').first.inner_text()
          and clean())
    page.locator(f'[data-testid="m-chat"][data-id="{chat.id}"]').click()
    until(lambda: page.locator('[data-testid="m-msg"]').count() >= 2, timeout=8)
    msgs = page.locator('[data-testid="m-msg"]')
    reply = page.locator('[data-testid="m-msg"][data-role="Claude"]').first
    check("P-3 Chats: the transcript is the projection (owner, reply; no tool row)",
          msgs.count() == 2 and "TOOL-ARGS-NEVER" not in text(), text())
    check("P-3 Chats: the owner's words are text", HOSTILE in msgs.first.inner_text() and clean())
    check("P-3 Chats: a reply's Markdown is rendered and sanitized (no script, no image, no javascript: link)",
          reply.locator("strong").inner_text() == "Done." and reply.locator("code").count() == 1
          and reply.locator("script, img, a").count() == 0 and clean(), reply.inner_html()[:300])
    check("P-3 Chats: math is drawn as MathML, KaTeX's styled HTML layer and every style attribute dropped "
          "(C1's style-src kept)", reply.locator("math").count() == 1
          and reply.locator(".katex-html, [style]").count() == 0, reply.inner_html()[:300])
    bad, fields = answer_controls()
    check("P-3 Chats: no input box yet, no send", not bad and fields == 0, f"{bad} {fields}")
    check("P-3 app: every tap target is at least 44 px tall (Chats)", not small_targets(), str(small_targets()))

    # -- live: a reply lands while the transcript is open --------------------------------
    daemon.stores.threads._append(chat.id, "log.jsonl", {"at": "2026-10-25T10:05:00+00:00", "thread_id": chat.id,
                                                         "kind": "text", "data": {"text": "a **live** reply"}})
    stop = threading.Event()

    def publish():
        while not stop.wait(0.15):
            daemon.bus.publish({"kind": "text", "thread_id": chat.id, "project_id": chat.project_id,
                                "data": {"text": "STREAM-TEXT-NEVER"}})

    worker = threading.Thread(target=publish, daemon=True)
    before = len(streamed)
    worker.start()
    try:
        until(lambda: page.locator('[data-testid="m-msg"]').count() == 3, timeout=10)
    finally:
        stop.set()
        worker.join(2)
    check("P-3 live: a new reply appears in the open transcript", page.locator('[data-testid="m-msg"]').count() == 3
          and "live" in page.locator('[data-testid="m-msg"]').last.inner_text())
    windows = b"".join(streamed[before:])
    check("P-3 live: the stream carried thread_changed for it, ids only",
          b'"kind":"thread_changed"' in windows and chat.id.encode() in windows
          and b"STREAM-TEXT-NEVER" not in windows, windows[:300].decode("utf-8", "replace"))

    # -- Tasks ---------------------------------------------------------------------------
    page.locator('[data-testid="m-tab-tasks"]').click()
    until(lambda: page.locator('[data-testid="m-task"]').count() == 2, timeout=8)
    groups = page.locator('[data-testid="m-task-group"]').all_inner_texts()
    check("P-3 Tasks: running first, then blocked", [g.lower() for g in groups] == ["running", "blocked"], str(groups))
    check("P-3 Tasks: a brief is text", HOSTILE in page.locator('[data-testid="m-task"]').first.inner_text() and clean())
    page.locator(f'[data-testid="m-task"][data-id="{running.id}"]').click()
    until(lambda: page.locator('[data-testid="m-task-brief"]').count() == 1, timeout=8)
    check("P-3 Tasks: the task's state and plan, as text",
          page.locator('[data-testid="m-state"]').first.inner_text() == "running"
          and HOSTILE in page.locator('[data-testid="m-plan"]').inner_text() and clean())
    page.locator('[data-testid="m-show-diff"]').click()
    until(lambda: page.locator('[data-testid="m-diff-file"]').count() >= 2, timeout=8)
    files = page.locator('[data-testid="m-diff-file"]')
    paths = [files.nth(i).get_attribute("data-path") for i in range(files.count())]
    check("P-3 Tasks: the diff, file by file", sorted(paths) == ["app.py", "notes.txt"], str(paths))
    first = files.first
    first.locator("summary").click()
    folded = page.evaluate("Array.from(document.querySelectorAll('[data-testid=m-diff-file]')).map(d => d.open)")
    check("P-3 Tasks: each file folds", folded.count(False) == 1, str(folded))
    check("P-3 Tasks: a patch's text is text", clean())
    check("P-3 app: every tap target is at least 44 px tall (Tasks)", not small_targets(), str(small_targets()))
    bad, fields = answer_controls()
    check("P-3 Tasks: no resume, cancel or answer control", not bad and fields == 0, f"{bad} {fields}")

    # -- offline: nothing shown as current -----------------------------------------------
    net["offline"] = True
    page.locator('[data-testid="m-tab-inbox"]').click()
    until(lambda: page.locator('[data-testid="m-offline"]').is_visible(), timeout=8)
    banner = page.locator('[data-testid="m-offline"]')
    check("P-3 offline: a banner says so, and says nothing is current",
          banner.is_visible() and "Offline" in banner.inner_text() and "Nothing here is current" in banner.inner_text(),
          banner.inner_text() if banner.count() else "")
    check("P-3 offline: the view is marked stale",
          page.evaluate("document.querySelector('[data-screen]').dataset.stale") == "true"
          and page.locator('[data-testid="m-conn"]').inner_text() == "offline")
    net["offline"] = False
    page.locator('[data-testid="m-tab-inbox"]').click()
    until(lambda: not page.locator('[data-testid="m-offline"]').is_visible(), timeout=10)
    check("P-3 offline: back online, the banner goes and the view is current again",
          not banner.is_visible() and page.evaluate("document.querySelector('[data-screen]').dataset.stale") == "false")


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
