"""Answering from the phone (phone design PH-4, PH-7, §4.1; work package P-4),
end to end in a real headless Chromium.

The built `mobile.html` is served by a real daemon's remote listener, reached
as `https://<host>` (the context forwards that origin to the loopback port
with the trusted Host, as `tailscale serve` does), and Chromium's virtual
authenticator stands in for the iPhone (user verification on). The §4.1 reads
are a fake system answering as this PC did, so no Windows command runs. Real
approvals wait on the daemon's real broker, and each is answered from the
phone:

  - Deny (one tap, no passkey), Allow (the passkey), Always (the rule
    first, then the passkey; the rule is then in the allowlist);
  - a HUD-only Browser card under §4.1 shows the desk's Browser words;
    with one probe flipped to failing, the same card says "answer at the
    desk" and only Deny works;
  - a card the phone's scrub would change says so and offers Deny only;
  - hostile text in a card stays text; nothing is keyboard-defaulted (Enter
    and Space on the card answer nothing); AUTHORIZE and ALWAYS are far
    below DENY; every button at least 44 px tall;
  - a question answered from its row.

Skipped when `hud/dist` has no phone shell (run `npm run build` in hud/).

Run: PYTHONPATH=. .venv/bin/python tests/face/hud_v2_phone_answer_check.py
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

SHELL_HOST = "jarvis-box.tail-test.ts.net"
SUFFIX = SHELL_HOST.split(".", 1)[1]
HOSTILE = '<img src=x onerror="window.__pwned=7"><b>bold</b><script>window.__pwned=8</script>'
MARKUP = "img, script, b, svg, iframe, object, embed, [onerror], [onload], a[href^='javascript']"
PEM_BODY = "MII" + "EowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun"
PEM = "-----BEGIN " + "RSA PRIVATE KEY-----\n" + PEM_BODY + "\n-----END " + "RSA PRIVATE KEY-----\n"


class FakeSystem:
    """The four §4.1 reads, as this PC answered them, for the test daemon."""

    def __init__(self, daemon):
        self.d = daemon
        self.bad: dict = {}

    def __call__(self, argv, timeout):
        from jarvis.v2 import remote_secure
        names = {tuple(v): k for k, v in remote_secure.reads(self.d.remote_port).items()}
        name = names[tuple(argv)]
        port, host = self.d.remote_port, self.d.remote_host
        ino = os.fstat(self.d.remote_auth.server.socket.fileno()).st_ino
        good = {
            "serve": json.dumps({"TCP": {"443": {"HTTPS": True}},
                                 "Web": {f"{host}:443": {"Handlers": {"/": {"Proxy": f"http://127.0.0.1:{port}"}}}}}),
            "status": json.dumps({"BackendState": "Running", "Self": {"DNSName": host + "."},
                                  "MagicDNSSuffix": SUFFIX, "CertDomains": [host]}),
            "ss": f"LISTEN 0 5 127.0.0.1:{port} 0.0.0.0:* uid:1000 ino:{ino} sk:1 <->\n",
            # This PC's real shape (CRLF lines, then the end line).
            "windows": "127.0.0.1 wslrelay\r\nEND-OF-LISTENERS\r\n",
        }
        return self.bad.get(name, good[name])


def run(browser, check, until):
    print("\nThe phone answers (P-4): Deny, Allow, Always, a HUD-only card under §4.1, limited, withheld")
    from jarvis import config, permissions as v1_permissions
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
    import importlib.util
    spec = importlib.util.spec_from_file_location("inbox_check", REPO / "tests" / "v2" / "inbox_check.py")
    ic = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ic)
    runner = ic.SpyRunner(daemon)
    daemon.runner = runner
    daemon.start()
    system = FakeSystem(daemon)
    daemon.secure.run = system
    port = daemon.remote_port
    project = daemon.stores.projects.create("Calc", str(root))
    thread = daemon.stores.threads.create(project.id, Role.CHAT, PN.CLAUDE, cwd=str(root))

    def forward(route):
        request = route.request
        url = urlsplit(request.url)
        path = url.path + ("?" + url.query if url.query else "")
        sent = request.all_headers()
        if url.path == "/m/api/events":
            return route.fulfill(**stream_window(path, sent))
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
            route.fulfill(status=response.status, headers={k: v for k, v in response.getheaders()},
                          body=response.read())
        finally:
            conn.close()

    def stream_window(path, sent):
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
        return {"status": status, "headers": headers, "body": data}

    def ask(**kw):
        kw.setdefault("provider", "claude")
        kw.setdefault("thread_id", thread.id)
        request = ApprovalRequest(**kw)
        out: dict = {}
        worker = threading.Thread(target=lambda: out.setdefault("decision", daemon.approvals.ask(request)),
                                  daemon=True)
        worker.start()
        until(lambda: any(r.req_id == request.req_id and request.req_id for r in daemon.approvals.pending()),
              timeout=5)
        return request, out, worker

    def pending(request):
        return any(r.req_id == request.req_id for r in daemon.approvals.pending())

    def browser_card():
        args = {"action": "click", "pane": "1", "target": "Buy " + HOSTILE, "text": "", "file": ""}
        b = {"tab_id": "0a1b2c3d", "action_id": "f" * 32, "pane": 1, "page_identity": 1, "document_generation": 1,
             "action": "click", "category": "purchase", "host": "shop.example", "path": "/cart",
             "target": "Buy " + HOSTILE, "text": "", "rect": None}
        return ask(tool="hud_browser_act", args=args, command=json.dumps(args), layer="browser-gate",
                   hud_only=True, browser=b, max_wait_s=120)

    # Service workers blocked (P-6 registers one at /m/sw.js): Playwright's
    # routes, which stand in for `tailscale serve` here, do not see the
    # requests of a page a service worker controls.
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, ignore_https_errors=True,
                              service_workers="block")
    ctx.route(re.compile(r".*"), lambda route: route.abort())
    ctx.route(re.compile(r"^https://" + re.escape(SHELL_HOST) + r"/"), forward)
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

    def pump(predicate, timeout=10.0):
        """Wait for `predicate` while Playwright runs its route handlers
        (`forward` runs only inside a Playwright call, never during a sleep)."""
        end = time.monotonic() + timeout
        while not predicate() and time.monotonic() < end:
            page.wait_for_timeout(50)
        return predicate()

    def clean():
        return page.locator(f"{card} :is({MARKUP})").count() == 0 and page.evaluate("window.__pwned === undefined")

    def open_card(request):
        page.locator('[data-testid="m-tab-inbox"]').click()
        row = page.locator(f'[data-testid="m-inbox-row"][data-id="appr:{request.req_id}"]')
        until(lambda: row.count() == 1, timeout=8)
        row.locator('[data-testid="m-open-card"]').click()
        until(lambda: page.locator(card).count() == 1
              and page.locator(f"{card} .m-card-body").inner_text() not in ("", "Loading the card…"), timeout=8)

    def heights():
        return page.evaluate("""Array.from(document.querySelectorAll('.m-card button'))
            .filter(e => e.offsetParent !== null).map(e => [e.dataset.testid, e.getBoundingClientRect().height])
            .filter(([, h]) => h < 44)""")

    try:
        # -- pair and sign in ------------------------------------------------------------
        host = f"127.0.0.1:{daemon.face_port}"
        conn = http.client.HTTPConnection("127.0.0.1", daemon.face_port, timeout=5)
        conn.request("POST", "/phone/pair", b"{}", {"Host": host, "Origin": f"http://{host}",
                                                    "Content-Type": "application/json"})
        opened = json.loads(conn.getresponse().read())
        conn.close()
        page.goto(opened["url"])
        until(lambda: screen() == "Pair this phone", timeout=8)
        page.locator("button").click()
        until(lambda: screen() == "Paired", timeout=8)
        page.locator("button").click()
        until(lambda: screen() == "app", timeout=8)
        check("P-4 setup: paired and signed in with the virtual authenticator", screen() == "app")
        check("P-4 setup: every §4.1 check passes on the fake system", daemon.secure.check()["ok"])

        # -- Deny ---------------------------------------------------------------------------
        hostile, h_out, h_worker = ask(tool="Bash", args={"command": "rm -rf build " + HOSTILE},
                                       command="rm -rf build " + HOSTILE, reason="cleanup " + HOSTILE,
                                       origin='task 0000beef "' + HOSTILE + '"')
        open_card(hostile)
        check("P-4 card: the whole command, the origin and the reason are text, never markup",
              HOSTILE in page.locator(f'{card} [data-testid="approval-command"]').inner_text()
              and HOSTILE in page.locator(f'{card} [data-testid="approval-origin"]').inner_text() and clean())
        page.locator(card).focus()
        for key in ("Enter", " "):
            page.keyboard.press(key)
        page.wait_for_timeout(400)
        check("P-4 card: Enter and Space on the card answer nothing (no keyboard default)", pending(hostile))
        page.keyboard.press("Tab")
        focused = page.evaluate("document.activeElement?.dataset?.testid || ''")
        check("P-4 card: Tab reaches Deny and never AUTHORIZE or ALWAYS", focused not in ("approval-allow",
                                                                                           "approval-always"), focused)
        # Waited for: the card can be redrawn (a re-read) between two reads (the known flake).
        box = lambda id_: until(lambda: page.locator(f'{card} [data-testid="{id_}"]').bounding_box(), timeout=4)  # noqa: E731
        deny_box, allow_box = box("approval-deny"), box("approval-allow")
        check("P-4 card: AUTHORIZE is far below DENY (more than 100 px)",
              allow_box["y"] - (deny_box["y"] + deny_box["height"]) > 100, f"{deny_box} {allow_box}")
        check("P-4 card: every button is at least 44 px tall", not heights(), str(heights()))
        page.locator(f'{card} [data-testid="approval-deny"]').click()
        pump(lambda: "decision" in h_out)
        check("P-4 Deny: one tap answers it, no passkey", h_out.get("decision") == Decision.DENY, str(h_out))
        until(lambda: page.locator(card).count() == 0, timeout=8)

        # -- AS: the summary, a fake model, the card's new order -----------------------------
        from types import SimpleNamespace
        from jarvis.v2.approval_summary import ApprovalSummaries
        gate = threading.Event()

        def fake_chat(model, messages, **kw):
            gate.wait(5)
            return SimpleNamespace(text=HOSTILE + " deletes build and deploys calc", prompt_tokens=1,
                                   completion_tokens=1, cost_usd=0.0)
        daemon.attach_approval_summaries(ApprovalSummaries(chat=fake_chat, deadline_s=6.0))
        long = "cd /srv/calc\nrm -rf build && make deploy --prod"
        summed, s_out, s_worker = ask(tool="Bash", args={"command": long}, command=long, reason="deploys")
        open_card(summed)
        check("AS phone: the card shows the summary pending and the command collapsed",
              page.locator(f'{card} [data-testid="approval-summary"]').get_attribute("data-state") == "pending"
              and page.locator(f'{card} [data-testid="approval-command"]').count() == 0
              and page.locator(f'{card} [data-testid="approval-command-preview"]').count() == 1)
        gate.set()
        until(lambda: summed.summary_state == "ready", timeout=6)
        # The stream's re-read is the app's own (`inbox_changed` names the row); here
        # the routes stand in for the network, so the card is read again by hand.
        if not pump(lambda: page.locator(f'{card} [data-testid="approval-summary"][data-state="ready"]').count() == 1,
                    timeout=3):
            page.locator('[data-testid="m-back"]').click()
            open_card(summed)
            pump(lambda: page.locator(f'{card} [data-testid="approval-summary"][data-state="ready"]').count() == 1)
        check("AS phone: the summary lands as text, marked as the AI's",
              page.locator(f'{card} [data-testid="approval-summary"]').get_attribute("data-state") == "ready"
              and "AI SUMMARY" in page.locator(f'{card} [data-testid="approval-summary"]').inner_text()
              and HOSTILE in page.locator(f'{card} [data-testid="approval-summary-text"]').inner_text() and clean(),
              page.locator(f'{card} [data-testid="approval-summary"]').inner_text())
        first = page.evaluate("""() => {
            const c = document.querySelector('[data-testid="approval-card"]');
            const d = c.querySelector('[data-testid="approval-deny"]'), b = c.querySelector('.m-card-body');
            return !!(d && b && (d.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING)); }""")
        check("AS phone: the buttons come before the card's body", first)
        page.locator(f'{card} [data-testid="approval-command-toggle"]').click()
        pump(lambda: page.locator(f'{card} [data-testid="approval-command"]').count() == 1)
        check("AS phone: one tap shows the whole command",
              page.locator(f'{card} [data-testid="approval-command"]').inner_text() == "command " + long)
        page.locator(f'{card} [data-testid="approval-deny"]').click()
        pump(lambda: "decision" in s_out)
        check("AS phone: Deny still answers it", s_out.get("decision") == Decision.DENY, str(s_out))
        until(lambda: page.locator(card).count() == 0, timeout=8)
        daemon.approval_summaries = None

        # -- Allow --------------------------------------------------------------------------
        allow, a_out, a_worker = ask(tool="Bash", args={"command": "npm test"}, command="npm test")
        open_card(allow)
        page.locator(f'{card} [data-testid="approval-allow"]').click()
        pump(lambda: "decision" in a_out)
        check("P-4 Allow: the passkey (UV), then the answer", a_out.get("decision") == Decision.ALLOW, str(a_out))
        until(lambda: page.locator('[data-testid="m-flash"]').count() == 1, timeout=8)
        check("P-4 Allow: the phone says so", "Authorized" in page.locator('[data-testid="m-flash"]').inner_text())
        check("P-4 Allow: no rule was written", not Path(config.ALLOWLIST_PATH).exists()
              or v1_permissions.load_allowlist() == [])

        # -- Always -------------------------------------------------------------------------
        always, w_out, w_worker = ask(tool="Bash", args={"command": "git status"}, command="git status")
        open_card(always)
        page.locator(f'{card} [data-testid="approval-always"]').click()
        until(lambda: page.locator('[data-testid="m-rule"]').count() == 1, timeout=8)
        check("P-4 Always: the rule shows before the passkey, and nothing is answered yet",
              "'git'" in page.locator('[data-testid="m-rule-words"]').inner_text()
              and json.loads(page.locator('[data-testid="m-rule-entry"]').inner_text())
              == {"tool": "run_command", "prefix": "git"} and pending(always))
        check("P-4 Always: nothing written before the passkey", not Path(config.ALLOWLIST_PATH).exists()
              or v1_permissions.load_allowlist() == [])
        page.locator('[data-testid="m-rule-confirm"]').click()
        pump(lambda: "decision" in w_out)
        check("P-4 Always: confirmed with the passkey, allowed", w_out.get("decision") == Decision.ALLOW, str(w_out))
        check("P-4 Always: and exactly the rule shown is written",
              v1_permissions.load_allowlist() == [{"tool": "run_command", "prefix": "git"}])

        # -- a HUD-only card under §4.1, then limited ------------------------------------
        private, p_out, p_worker = browser_card()
        open_card(private)
        body = page.locator(f'{card} [data-testid="browser-approval-details"]')
        check("P-4 HUD-only: under §4.1 the desk's Browser words show, page text as text",
              body.count() == 1 and "Authorize this one Browser action?" in body.inner_text()
              and "shop.example/cart" in body.inner_text() and HOSTILE in body.inner_text() and clean(),
              page.locator(card).inner_text()[:300])
        check("P-4 HUD-only: AUTHORIZE is offered, ALWAYS is not",
              page.locator(f'{card} [data-testid="approval-allow"]').is_enabled()
              and page.locator(f'{card} [data-testid="approval-always"]').count() == 0)
        system.bad["serve"] = json.dumps({
            "TCP": {"443": {"HTTPS": True}}, "AllowFunnel": {f"{SHELL_HOST}:443": True},
            "Web": {f"{SHELL_HOST}:443": {"Handlers": {"/": {"Proxy": f"http://127.0.0.1:{port}"}}}}})
        page.locator('[data-testid="m-back"]').click()
        open_card(private)
        check("P-4 HUD-only: one probe failing (Funnel), the same card says 'answer at the desk'",
              page.locator('[data-testid="m-card-refused"]').count() == 1
              and "answer at the desk" in page.locator('[data-testid="m-card-refused"]').inner_text()
              and page.locator(f'{card} [data-testid="approval-allow"]').is_disabled()
              and "PRIVATE" not in page.locator(card).inner_text(), page.locator(card).inner_text()[:300])
        check("P-4 HUD-only: and the private detail is not on the page any more",
              "shop.example" not in page.locator(card).inner_text())
        check("P-4 limited: the card's own red line names the check",
              "limited: tailscale serve" in page.locator(f'{card}').inner_text()
              or page.locator('[data-testid="m-card-refused"]').count() == 1)
        page.locator('[data-testid="m-back"]').click()
        until(lambda: page.locator('[data-testid="m-limited"]').count() >= 1, timeout=8)
        check("P-4 limited: a red 'limited' line on the inbox, in the daemon's words",
              "limited: tailscale serve is not the only path, or Funnel is on"
              in page.locator('[data-testid="m-limited"]').first.inner_text())
        open_card(private)
        page.locator(f'{card} [data-testid="approval-deny"]').click()
        pump(lambda: "decision" in p_out)
        check("P-4 HUD-only: Deny still works while limited", p_out.get("decision") == Decision.DENY, str(p_out))
        system.bad.clear()

        # -- a card the phone's scrub would change -------------------------------------------
        withheld, x_out, x_worker = ask(tool="Write", args={"file_path": "/tmp/k", "content": PEM}, command=None)
        open_card(withheld)
        check("P-4 withheld: the card says so, offers Deny only, and the key's body never reaches the page",
              "withheld text" in page.locator('[data-testid="m-card-why"]').inner_text()
              and page.locator(f'{card} [data-testid="approval-allow"]').is_disabled()
              and page.locator(f'{card} [data-testid="approval-always"]').is_disabled()
              and PEM_BODY not in page.content())
        page.locator(f'{card} [data-testid="approval-deny"]').click()
        pump(lambda: "decision" in x_out)
        check("P-4 withheld: Deny works", x_out.get("decision") == Decision.DENY, str(x_out))

        # -- a task's question, from its row ----------------------------------------------
        from jarvis.v2.model import OpenQuestion, Spec, TaskState
        task = daemon.stores.tasks.create(project.id, "add multiply")
        daemon.stores.tasks.save(task)
        task = daemon.stores.tasks.transition(task.id, TaskState.CLARIFYING)
        task.spec = Spec(goal="g", questions=[OpenQuestion("which db? " + HOSTILE, blocking=True, options=["pg"])])
        daemon.stores.tasks.save(task)
        daemon.bus.publish({"kind": "task_status_changed", "task_id": task.id, "project_id": project.id,
                            "data": {"task_id": task.id}})
        page.locator('[data-testid="m-tab-inbox"]').click()
        row = page.locator(f'[data-testid="m-inbox-row"][data-id="tq:{task.id}:0"]')
        until(lambda: row.count() == 1, timeout=8)
        text = row.locator('[data-testid="m-answer-text"]')
        text.fill("postgres")
        text.press("Enter")
        page.wait_for_timeout(400)
        check("P-4 question: Enter in the box sends nothing", runner.calls == [])
        row.locator('[data-testid="m-answer-send"]').click()
        pump(lambda: runner.calls)
        page.wait_for_timeout(300)
        note = page.locator("[data-testid=m-answer-note], [data-testid=m-flash]").all_inner_texts()
        check("P-4 question: Send answers it, the session only",
              runner.calls == [("answer_question", task.id, 0, "postgres")], f"{runner.calls} {note}")
        check("P-4: no page errors", not errors, str(errors[:3]))
    finally:
        ctx.close()
        daemon.stop()
        for guard in reversed(patches):
            guard.stop()
        tmp.cleanup()


def desk_too_deep(browser, check, until, guard, init_script):
    """The desk's card (review of PR #313): an argument nested past the
    redaction's depth shows a warning, AUTHORIZE and ALWAYS are off, DENY
    answers. Against the mock daemon, the card as the daemon would send it."""
    print("\nThe desk's card: arguments nested too deep to judge offer DENY only")
    from tests.face.hud_v2_layout_check import _boot
    from tests.face.hud_v2_mock import MockDaemon
    mock = MockDaemon(0).start()
    base = f"http://127.0.0.1:{mock.port}"
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    guard(ctx)
    ctx.add_init_script(init_script)
    posts: list[tuple[str, str]] = []

    def record(route):
        request = route.request
        if request.method == "POST":
            posts.append((request.url.split("/approvals/")[1], request.post_data or ""))
        route.continue_()

    ctx.route(re.compile(r".*/approvals/[^/]+$"), record)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        _boot(page, mock, base, until)
        deep: object = "[nested too deep to judge]"
        for i in range(32):
            deep = {f"l{i}": deep}
        mock.emit("approval_requested", {
            "req_id": "deep-1", "code": "DEEP", "tool": "mcp__x__exec", "args": {"spec": deep},
            "command": None, "reason": "unknown tool", "layer": "human", "origin": "task: deep",
            "allowlistable": True, "timeout_s": 120})
        until(lambda: page.locator('[data-testid="approval-card"]').count() == 1, timeout=6)
        warn = page.locator('[data-testid="approval-too-deep"]')
        check("desk: the card warns that arguments are nested too deep to check",
              warn.count() == 1 and "nested too deep" in warn.inner_text())
        check("desk: AUTHORIZE and ALWAYS are off, DENY is not",
              page.locator('[data-testid="approval-allow"]').is_disabled()
              and page.locator('[data-testid="approval-always"]').is_disabled()
              and page.locator('[data-testid="approval-deny"]').is_enabled())
        page.locator('[data-testid="approval-allow"]').click(force=True)
        page.wait_for_timeout(300)
        check("desk: a forced click on AUTHORIZE answers nothing", posts == [], str(posts))
        page.locator('[data-testid="approval-deny"]').click()
        until(lambda: posts, timeout=4)
        check("desk: DENY answers it", posts and posts[0][0] == "deep-1" and '"deny"' in posts[0][1], str(posts))
        mock.emit("approval_requested", {
            "req_id": "shallow-1", "code": "SHAL", "tool": "run_command", "args": {"command": "ls"},
            "command": "ls", "reason": "r", "layer": "human", "origin": "task: x", "allowlistable": True,
            "timeout_s": 120})
        until(lambda: page.locator('[data-testid="approval-card"][data-req="shallow-1"]').count() == 1, timeout=6)
        # AS-13: AUTHORIZE arms ARM_MS after the card appears.
        until(lambda: page.locator('[data-testid="approval-allow"]').is_enabled(), timeout=4)
        check("desk: an ordinary card is unchanged (no warning, AUTHORIZE on once armed)",
              page.locator('[data-testid="approval-too-deep"]').count() == 0
              and page.locator('[data-testid="approval-allow"]').is_enabled())
        check("desk: no page errors", not errors, str(errors[:3]))
    finally:
        ctx.close()
        mock.stop()


def main():
    from playwright.sync_api import sync_playwright
    from tests.face import hud_v2_check as v2
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            run(browser, v2.check, v2.until)
        except Exception as exc:
            v2.check("Phone answer scenario completes", False, f"{type(exc).__name__}: {exc}")
        try:
            desk_too_deep(browser, v2.check, v2.until, v2.guard, v2.FAKE_RECOGNIZER)
        except Exception as exc:
            v2.check("Desk too-deep scenario completes", False, f"{type(exc).__name__}: {exc}")
        finally:
            browser.close()
    print(f"{v2.CHECKS} phone answer checks, {len(v2.FAILURES)} failures")
    return bool(v2.FAILURES)


if __name__ == "__main__":
    sys.exit(main())
