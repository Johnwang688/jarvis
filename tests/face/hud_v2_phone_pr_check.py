"""PR rows on the phone (PM-F of docs/plans/2026-10-28-pr-inbox-merge.md:
PM-24, PM-26, Q1, Q3, Q4; PM-D's phone routes, PM-E's fix), end to end in a
real headless Chromium at 390×844.

The built `mobile.html` is served by a real daemon's remote listener, reached
as `https://<host>` (the context forwards that origin to the loopback port with
the trusted Host, as `tailscale serve` does), and Chromium's virtual
authenticator stands in for the iPhone (the harness of
`hud_v2_phone_answer_check.py`). The §4.1 reads are a fake system.

  - **One real PR, end to end:** PM-C's real summaries over pr_facts' fake gh
    and a fake model, PM-D's real `PrActions` on the same fake gh. The row
    draws title, buttons, facts, then the AI summary; Merge asks
    `/m/api/inbox/pr/options` with the row's head and card, takes the passkey,
    and the fake gh runs `gh pr merge … --match-head-commit <head>` once; the
    row's fixed notice says "Asking for Face ID…", then the result.
  - **A card that moved under the phone:** the daemon's row changes (no event),
    Merge sends the drawn card, the options are a 409 "Changed since you read
    it.", nothing runs, and the row is drawn again from a new read.
  - **Each state** (`hud_v2_pr_rows.STATES`, crafted rows in the PR source):
    pending, failed and off summaries, notes, squash, held (no buttons), each
    fix state, the fix card; never a GitHub link, never the head SHA; hostile
    text stays text.
  - **Limited (Q1):** with one §4.1 probe failing, a flagged PR offers nothing
    and says to answer it at the desk; an unflagged one keeps its buttons.
  - **Cancel a fix** takes the `task:` passkey flow.

`--shots DIR` only draws every state and saves one screenshot per row (no
checks), which also works against an older build.

Run: PYTHONPATH=. .venv/bin/python tests/face/hud_v2_phone_pr_check.py
"""
from __future__ import annotations

import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
for extra in (REPO, REPO / "tests" / "v2"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

SHELL_HOST = "jarvis-box.tail-test.ts.net"
SUFFIX = SHELL_HOST.split(".", 1)[1]
MARKUP = "img, script, b, svg, iframe, object, embed, [onerror], [onload], a"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeSystem:
    """The four §4.1 reads, as this PC answered them (`hud_v2_phone_answer_check`)."""

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
            "windows": "127.0.0.1 wslrelay\r\nEND-OF-LISTENERS\r\n",
        }
        return self.bad.get(name, good[name])


class Shim:
    """What `pr_merge_check.Prs` asks of its test case."""

    def __init__(self):
        self.cleanups = []

    def addCleanup(self, fn, *args):  # noqa: N802 — unittest's name
        self.cleanups.append((fn, args))

    def assertEqual(self, a, b, msg=None):  # noqa: N802
        assert a == b, msg

    def close(self):
        for fn, args in reversed(self.cleanups):
            try:
                fn(*args)
            except Exception:  # noqa: BLE001
                pass


class Phone:
    """A real daemon, its remote listener behind a forwarded https origin, and a
    paired phone page with a virtual authenticator."""

    def __init__(self, browser, until):
        from jarvis import config
        from jarvis.v2 import remote_api, pr_summary as S
        from jarvis.v2.daemon import Daemon
        from jarvis.v2.model import ProviderName as PN
        from jarvis.v2.provider import Decision, SessionHandle, Usage
        from jarvis.v2.stores import Stores
        self.until = until
        dist = REPO / "hud" / "dist"
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR") or None)
        root = Path(self.tmp.name)
        (root / "home").mkdir()

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

        env = {k: v for k, v in os.environ.items() if k not in ("JARVIS_REMOTE_HOST", "GH_TOKEN", "GITHUB_TOKEN")}
        env.update({"HOME": str(root / "home"), "XDG_DATA_HOME": str(root / "xdg"),
                    "JARVIS_CLAUDE_CREDENTIALS": str(root / "none.json"), "JARVIS_GH": "", "JARVIS_GIT": "",
                    "JARVIS_PR_SUMMARY_MODEL": "", "GIT_CONFIG_NOSYSTEM": "1"})
        self.patches = [patch.dict(os.environ, env, clear=True)]
        self.patches += [patch.object(config, name, value) for name, value in dict(
            V2_DATA_DIR=root / "data", ALLOWLIST_PATH=root / "allow.json", MODELS_PATH=root / "models.json",
            PROVIDER_DEFAULTS_PATH=root / "provider_defaults.json", DISCORD_GUILD_PATH=root / "guild.json",
            HUD_DEV=True, HUD_DIST=dist).items()]
        self.patches.append(patch.object(remote_api, "RETRY_MS", 300))
        self.patches.append(patch.dict(config.TIERS, {"pr_summary": S.DEFAULT_MODEL}))
        for guard in self.patches:
            guard.start()
        self.daemon = daemon = Daemon(Stores(), {name: Fake(name) for name in PN},
                                      lambda *_: lambda *_: Decision.DENY, 0, remote_host=SHELL_HOST)
        ic = _load("inbox_check", REPO / "tests" / "v2" / "inbox_check.py")
        self.runner = ic.SpyRunner(daemon)
        daemon.runner = self.runner
        daemon.start()
        self.system = FakeSystem(daemon)
        daemon.secure.run = self.system
        port = daemon.remote_port

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
                data = response.read()
                if request.method == "POST":
                    self.posts.append((url.path, json.loads(body or b"null"), response.status))
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

        self.posts: list = []
        self.ctx = browser.new_context(viewport={"width": 390, "height": 844}, ignore_https_errors=True,
                                       service_workers="block", device_scale_factor=2)
        self.ctx.route(re.compile(r".*"), lambda route: route.abort())
        self.ctx.route(re.compile(r"^https://" + re.escape(SHELL_HOST) + r"/"), forward)
        self.page = page = self.ctx.new_page()
        page.set_default_timeout(10000)
        self.errors: list[str] = []
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        cdp = self.ctx.new_cdp_session(page)
        cdp.send("WebAuthn.enable")
        cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
            "protocol": "ctap2", "transport": "internal", "hasResidentKey": True,
            "hasUserVerification": True, "isUserVerified": True, "automaticPresenceSimulation": True}})

    def screen(self):
        return self.page.evaluate("document.querySelector('[data-screen]')?.dataset.screen || ''")

    def sign_in(self):
        d = self.daemon
        d.secure.check()                        # §4.1 read once on the fake system (not limited)
        host = f"127.0.0.1:{d.face_port}"
        conn = http.client.HTTPConnection("127.0.0.1", d.face_port, timeout=5)
        conn.request("POST", "/phone/pair", b"{}", {"Host": host, "Origin": f"http://{host}",
                                                    "Content-Type": "application/json"})
        opened = json.loads(conn.getresponse().read())
        conn.close()
        self.page.goto(opened["url"])
        self.until(lambda: self.screen() == "Pair this phone", timeout=8)
        self.page.locator("button").click()
        self.until(lambda: self.screen() == "Paired", timeout=8)
        self.page.locator("button").click()
        self.until(lambda: self.screen() == "app", timeout=8)

    def reopen(self):
        """The app opened again (a launch): §4.1 read afresh. Not a reload: that is the pairing link."""
        self.page.goto(f"https://{SHELL_HOST}/m/")
        self.until(lambda: self.screen() == "app", timeout=8)

    def limit(self):
        """One §4.1 probe fails (Funnel on): the phone is limited."""
        port = self.daemon.remote_port
        self.system.bad["serve"] = json.dumps({
            "TCP": {"443": {"HTTPS": True}}, "AllowFunnel": {f"{SHELL_HOST}:443": True},
            "Web": {f"{SHELL_HOST}:443": {"Handlers": {"/": {"Proxy": f"http://127.0.0.1:{port}"}}}}})
        self.daemon.secure.check()              # §4.1's last result, which `/m/api/me` reports

    def pump(self, predicate, timeout=10.0):
        """Wait while Playwright runs the route handlers (they run only inside a Playwright call)."""
        end = time.monotonic() + timeout
        while not predicate() and time.monotonic() < end:
            self.page.wait_for_timeout(50)
        return predicate()

    def show(self, rows):
        """Crafted rows in the PR source; the phone reads the inbox again."""
        source = self.daemon.inbox.prs
        source.summaries = None                 # nothing re-builds a crafted row
        with source._lock:
            source._rows = {r["id"]: json.loads(json.dumps(r)) for r in rows}
        self.reread()

    def reread(self):
        page = self.page
        page.locator('[data-testid="m-tab-tasks"]').click()
        page.locator('[data-testid="m-tab-inbox"]').click()

    def rect(self, row_id, selector):
        """One box, read in one go (the inbox is drawn again on every read): {x, y, width, height} or None."""
        return self.page.evaluate("""([id, sel]) => {
            const r = [...document.querySelectorAll('[data-testid="m-inbox-row"]')].find(e => e.dataset.id === id);
            const e = r && r.querySelector(sel); if (!e) return null;
            const b = e.getBoundingClientRect(); return {x: b.x, y: b.y, width: b.width, height: b.height}; }""",
                                  [row_id, selector])

    def row(self, row_id):
        return self.page.locator(f'[data-testid="m-inbox-row"][data-id="{row_id}"]')

    def close(self):
        try:
            self.ctx.close()
        finally:
            self.daemon.stop()
            for guard in reversed(self.patches):
                guard.stop()
            self.tmp.cleanup()


def _shot(page, selector, path):
    """A row's screenshot by its box: the phone redraws its inbox on every read (each
    stream window), so the element itself never holds still for Playwright."""
    rect = page.evaluate("""s => { const e = document.querySelector(s); if (!e) return null;
        e.scrollIntoView({block: "start"}); const r = e.getBoundingClientRect();
        return {x: r.x, y: r.y, width: r.width, height: Math.min(r.height, innerHeight - r.y)}; }""", selector)
    if rect and rect["width"] and rect["height"] > 0:
        page.screenshot(path=path, clip=rect)


def shots(browser, until, out: Path, prefix: str):
    """Every state, one screenshot per row (no checks): works against an older build too."""
    from tests.face import hud_v2_pr_rows as R
    out.mkdir(parents=True, exist_ok=True)
    p = Phone(browser, until)
    try:
        p.sign_in()
        rows = R.all_rows()
        p.show(rows)
        until(lambda: p.page.locator('[data-testid="m-inbox-row"]').count() >= len(rows), timeout=8)
        for name, build in R.STATES.items():
            _shot(p.page, f'[data-testid="m-inbox-row"][data-id="{build()["id"]}"]', str(out / f"{prefix}-phone-{name}.png"))
        p.page.evaluate("document.querySelector('.main').scrollTop = 0")
        p.page.screenshot(path=str(out / f"{prefix}-phone-inbox-top.png"))
        # Limited: a §4.1 probe fails.
        p.limit()
        p.reopen()
        until(lambda: p.screen() == "app", timeout=8)
        until(lambda: p.row(R.flagged()["id"]).count() == 1, timeout=8)
        for name in ("flagged", "ready"):
            _shot(p.page, f'[data-testid="m-inbox-row"][data-id="{R.STATES[name]()["id"]}"]',
                  str(out / f"{prefix}-phone-limited-{name}.png"))
    finally:
        p.close()


def run(browser, check, until):
    print("\nPR rows on the phone (PM-F): one real PR merged with the passkey, a 409, each state, limited")
    from jarvis.v2 import pr_merge
    from tests.face import hud_v2_pr_rows as R
    dist = REPO / "hud" / "dist"
    if not (dist / "mobile.html").is_file():
        print("  skip  hud/dist has no phone shell (run npm run build in hud/)")
        return
    pmc = _load("pr_merge_check", REPO / "tests" / "v2" / "pr_merge_check.py")
    p = Phone(browser, until)
    shim = Shim()
    page = p.page
    try:
        p.sign_in()
        check("PM-F phone setup: paired, signed in, every §4.1 check passing",
              p.screen() == "app" and p.daemon.secure.check()["ok"])

        # -- one real PR, end to end ---------------------------------------------------
        prs = pmc.Prs(shim, p.daemon)
        d = prs.poll()
        p.reread()
        row = p.row(pmc.ROW)
        until(lambda: row.count() == 1 and row.locator('[data-testid="m-pr-act-merge"]').count() == 1, timeout=8)
        order = row.evaluate("""r => [...r.querySelectorAll('[data-testid]')].map(e => e.dataset.testid)
            .filter(t => ['m-pr-acts', 'm-pr-facts', 'm-pr-summary'].includes(t))""")
        title_top = (p.rect(pmc.ROW, ".row-title") or {"y": 1e9})["y"]
        acts_top = (p.rect(pmc.ROW, '[data-testid="m-pr-acts"]') or {"y": -1})["y"]
        check("PM-24 phone: title, then the buttons, then code's facts, then the AI summary [mutation: summary first]",
              order == ["m-pr-acts", "m-pr-facts", "m-pr-summary"] and title_top < acts_top, str(order))
        check("PM-25 phone: no link and no full head SHA on the row [mutation: url kept]",
              row.locator("a").count() == 0 and d["head"] not in row.inner_text())
        check("PM-24 phone: the AI summary is labelled and holds the model's Does line",
              "AI SUMMARY" in row.locator('[data-testid="m-pr-summary"]').inner_text()
              and "retry" in row.locator('[data-testid="m-pr-does"]').inner_text())
        merge = row.locator('[data-testid="m-pr-act-merge"]')
        box = p.rect(pmc.ROW, '[data-testid="m-pr-act-merge"]') or {"height": 0, "x": 0}
        check("phone: Merge is at least 44 px tall and sits at the right half [mutation: Merge at the left]",
              box["height"] >= 44 and box["x"] > 390 / 2 - 10, str(box))
        page.evaluate("document.activeElement && document.activeElement.blur()")
        page.keyboard.press("Enter")
        page.keyboard.press(" ")
        page.wait_for_timeout(300)
        check("phone: Enter and Space on the page act on nothing (no keyboard default)", prs.run.acts() == [])
        before = len(p.posts)
        merge.click()
        p.pump(lambda: prs.run.acts() != [] or len(p.posts) > before + 1, timeout=10)
        p.pump(lambda: "Merged" in row.locator('[data-testid="m-pr-notice"]').inner_text(), timeout=8)
        sent = [x for x in p.posts[before:] if x[0].startswith("/m/api/inbox/pr")]
        acts = prs.run.acts()
        check("phone Merge: options then the act, each with the row's head and card exactly as drawn",
              [x[0] for x in sent] == [pr_merge.PHONE_OPTIONS, pr_merge.PHONE_ACT]
              and sent[0][1] == {"id": pmc.ROW, "action": "merge", "head": d["head"], "card": d["card"]}
              and {k: sent[1][1][k] for k in ("id", "action", "head", "card")} == sent[0][1]
              and isinstance(sent[1][1].get("assertion"), dict), str([(x[0], x[2]) for x in sent]))
        check("phone Merge: the passkey, then gh pr merge … --match-head-commit <head>, once [mutation: no passkey]",
              len(acts) == 1 and acts[0]["argv"][-2:] == ["--match-head-commit", d["head"]]
              and "--merge" in acts[0]["argv"], str([a["argv"] for a in acts]))
        check("phone Merge: the row's fixed notice says what happened",
              "Merged" in row.locator('[data-testid="m-pr-notice"]').inner_text())

        # -- a card that moved under the phone: 409, nothing runs, the row drawn again ---
        prs.run.merged = False
        prs.poll()
        p.reread()
        until(lambda: p.row(pmc.ROW).locator('[data-testid="m-pr-act-merge"]').count() == 1, timeout=8)
        source = p.daemon.inbox.prs
        with source._lock:
            source._rows[pmc.ROW]["detail"]["card"] = "f" * 64          # no event: the phone still draws the old
        acts_before = len(prs.run.acts())
        before = len(p.posts)
        p.row(pmc.ROW).locator('[data-testid="m-pr-act-merge"]').click()
        p.pump(lambda: "changed since you read it" in p.row(pmc.ROW).inner_text().lower(), timeout=8)
        sent = [x for x in p.posts[before:] if x[0].startswith("/m/api/inbox/pr")]
        check("phone 409: the drawn card is sent, the options refuse it, nothing runs, no retry [mutation: retry]",
              len(sent) == 1 and sent[0][2] == 409 and sent[0][1]["card"] == d["card"]
              and len(prs.run.acts()) == acts_before, str([(x[0], x[2]) for x in sent]))
        check("phone 409: the server's words in the row's notice",
              "Changed since you read it" in p.row(pmc.ROW).locator('[data-testid="m-pr-notice"]').inner_text())
        shim.close()

        # -- each state ----------------------------------------------------------------
        # The running fix's task is a real one, so its Cancel takes the real `task:` passkey flow.
        project = p.daemon.stores.projects.create("Calc", str(Path(p.tmp.name)))
        fix_task = p.daemon.stores.tasks.create(project.id, "fix PR #31")
        rows = R.all_rows()
        running_row = next(r for r in rows if r["id"] == R.fix_running()["id"])
        running_row["detail"]["fix"]["task_id"] = fix_task.id
        p.show(rows)
        until(lambda: page.locator('[data-testid="m-inbox-row"][data-kind="pr"]').count() == len(R.STATES), timeout=8)
        acts_of = lambda r: r.evaluate(  # noqa: E731
            "e => [...e.querySelectorAll('[data-testid^=\"m-pr-act-\"]')].map(b => b.dataset.testid.slice(9))")
        ready = p.row(R.ready()["id"])
        check("states: a ready row offers Merge", acts_of(ready) == ["merge"], str(acts_of(ready)))
        pend = p.row(R.pending()["id"])
        slot_h = lambda rid: page.evaluate(  # noqa: E731 — one read: the inbox is redrawn on every read
            """id => [...document.querySelectorAll('[data-testid="m-inbox-row"]')].find(r => r.dataset.id === id)
                ?.querySelector('[data-testid="m-pr-summary"]')?.getBoundingClientRect().height || 0""", rid)
        check("states: a pending summary says summarizing…, in its slot held two lines high (AS-14)",
              "summarizing…" in pend.locator('[data-testid="m-pr-summary"]').inner_text()
              and slot_h(R.pending()["id"]) >= 40, str(slot_h(R.pending()["id"])))
        for name, words in (("failed", "AI summary unavailable: the model did not answer in time"),
                            ("off", "AI summary unavailable: today's summary budget is used up")):
            check(f"states: a {name} summary says why", words in p.row(R.STATES[name]()["id"]).inner_text())
        check("states: summary notes are code's lines outside the AI slot",
              "checks re-running" in p.row(R.waiting()["id"]).locator('[data-testid="m-pr-summary-note"]').inner_text()
              and "without the CI logs" in p.row(R.no_logs()["id"]).locator('[data-testid="m-pr-summary-note"]').inner_text())
        wait = p.row(R.waiting()["id"])
        check("states: no buttons, and code's why ('waits on its checks')",
              acts_of(wait) == [] and "waits on its checks" in wait.locator('[data-testid="m-pr-why"]').inner_text())
        check("states: Q4, squash named before the OK",
              "squash merge" in p.row(R.squash()["id"]).locator('[data-testid="m-pr-method"]').inner_text())
        held = p.row(R.held()["id"])
        check("Q3 phone: a held row shows its note and no button, and no hold control [mutation: hold on phone]",
              acts_of(held) == [] and "On hold: waits on the robot test"
              in (held.locator('[data-testid="m-pr-hold"]').text_content() or "")
              and held.locator('[data-testid="m-pr-hold-set"]').count() == 0)
        sc = p.row(R.scanner()["id"])
        check("states: a scanner is labelled by code, beside its name; a lookalike name stays a name",
              sc.locator('[data-testid="m-pr-scanner"]').count() == 1
              and "lint [secret scanner]" in sc.locator('[data-testid="m-pr-facts"]').inner_text()
              and acts_of(sc) == ["merge_as_is"])
        fl = p.row(R.flagged()["id"])
        check("states: the safety-file flag is its own amber line",
              "touches safety files" in fl.locator('[data-testid="m-pr-flags"]').inner_text().lower())
        expect = {"fix_running": f"fix running (task {fix_task.id[:8]}", "fix_question": "fix waits on your answer",
                  "fix_stopped": "fix stopped: failed", "fix_failing": "fix pushed; checks still fail",
                  "fix_pushed": "fix pushed; checks running", "fix_rewritten": "branch rewritten since your Fix",
                  "fix_start_failed": "fix could not start"}
        for name, words in expect.items():
            fx = p.row(R.STATES[name]()["id"]).locator('[data-testid="m-pr-fix"]')
            check(f"fix states: {name} reads '{words}'", fx.count() == 1 and words in fx.inner_text(),
                  fx.inner_text() if fx.count() else "none")
        check("fix states: running and question offer Cancel; stopped offers Fix again",
              p.row(R.fix_running()["id"]).locator('[data-testid="m-pr-fix-cancel"]').count() == 1
              and p.row(R.fix_question()["id"]).locator('[data-testid="m-pr-fix-cancel"]').count() == 1
              and p.row(R.fix_stopped()["id"]).locator('[data-testid="m-pr-act-fix"]').inner_text() == "Fix again")
        check("fix states: rewritten is amber",
              p.row(R.fix_rewritten()["id"]).locator('[data-testid="m-pr-fix"]').get_attribute("data-tone") == "amber")
        card = p.row(R.fix_ready()["id"])
        check("fix card: Fixed — merge?, N commits, the CI amber, its AI slot and Merge",
              "Fixed — merge?" in card.locator('[data-testid="m-pr-fix"]').inner_text()
              and "2 commits since your Fix" in card.inner_text()
              and "the fix changed CI configuration" in card.locator('[data-testid="m-pr-fix-ci"]').inner_text()
              and "AI SUMMARY" in card.locator('[data-testid="m-pr-fix-summary"]').inner_text()
              and acts_of(card) == ["merge"])
        check("fix card: the model's words sit only in the AI slot, as text [mutation: the daemon's note drawn]",
              card.locator(f":is({MARKUP})").count() == 0 and page.evaluate("window.__pwned === undefined")
              and "evil" not in card.locator('[data-testid="m-pr-fix"] > :not([data-testid="m-pr-fix-summary"])')
              .all_inner_texts().__str__())
        q = p.row(R.fix_question()["id"])
        q.locator('[data-testid="m-pr-fix-question"]').click()
        until(lambda: page.evaluate("document.activeElement?.dataset?.id || ''").startswith("tq:"), timeout=4)
        check("fix question: its link goes to the task's question row", True)
        groups = page.evaluate("""() => [...document.querySelectorAll('[data-testid="m-inbox"] h2.group')]
            .map(h => h.textContent)""")
        check("rank 4 rows (ended fixes) sit under 'Waiting on you'", "Waiting on you" in groups, str(groups))
        check("no link anywhere in the inbox, no markup made from daemon text",
              page.locator('[data-testid="m-inbox"] :is(' + MARKUP + ')').count() == 0
              and page.evaluate("window.__pwned === undefined"))

        # -- Cancel a fix: the task: passkey ---------------------------------------------
        before = len(p.posts)
        p.row(R.fix_running()["id"]).locator('[data-testid="m-pr-fix-cancel"]').click()
        p.pump(lambda: ("cancel", fix_task.id) in p.runner.calls, timeout=8)
        sent = [x[0] for x in p.posts[before:]]
        check("fix Cancel: the task's cancel options, the passkey, then TaskControl.cancel [mutation: no passkey]",
              sent[:2] == [f"/m/api/tasks/{fix_task.id}/cancel/options", f"/m/api/tasks/{fix_task.id}/cancel"]
              and isinstance(p.posts[before + 1][1].get("assertion"), dict)
              and ("cancel", fix_task.id) in p.runner.calls, str(sent))

        # -- limited (Q1) ------------------------------------------------------------------
        p.limit()
        p.reopen()
        until(lambda: p.screen() == "app", timeout=8)
        p.pump(lambda: p.row(R.flagged()["id"]).count() == 1
               and page.locator('[data-testid="m-limited"]').count() == 1, timeout=15)
        fl = p.row(R.flagged()["id"])
        limited_line = page.locator('[data-testid="m-limited"]')
        check("Q1 limited: a flagged PR offers nothing and says to answer it at the desk [mutation: no pre-hide]",
              limited_line.count() == 1 and acts_of(fl) == []
              and "answer it at the desk" in fl.locator('[data-testid="m-pr-why"]').inner_text(),
              f"{limited_line.count()} {acts_of(fl)} {p.daemon.secure.check()}")
        check("Q1 limited: an unflagged PR keeps Merge", acts_of(p.row(R.ready()["id"])) == ["merge"])
        check("no page errors", not p.errors, "; ".join(p.errors[:3]))
    finally:
        shim.close()
        p.close()


def main():
    from playwright.sync_api import sync_playwright
    from tests.face import hud_v2_check as H
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            if "--shots" in sys.argv:
                out = Path(sys.argv[sys.argv.index("--shots") + 1])
                prefix = sys.argv[sys.argv.index("--prefix") + 1] if "--prefix" in sys.argv else "after"
                shots(browser, H.until, out, prefix)
                return 0
            run(browser, H.check, H.until)
        finally:
            browser.close()
    print()
    if H.FAILURES:
        print(f"{len(H.FAILURES)} of {H.CHECKS} checks FAILED:")
        for f in H.FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {H.CHECKS} phone PR checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
