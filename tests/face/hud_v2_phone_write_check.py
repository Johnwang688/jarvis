"""Writing from the phone (phone design §5, §6; work package P-5), end to end
in a real headless Chromium at a phone's viewport.

The built `mobile.html` is served by a real daemon's remote listener, reached
as `https://<host>` (the context forwards that origin to the loopback port
with the trusted Host, as `tailscale serve` does), and Chromium's virtual
authenticator stands in for the iPhone. Fake providers; a spy task runner.

  - a chat's message box: Enter sends nothing; Send does, once, recorded
    `via="phone"`; the message shows after the read; behind a running turn
    it queues and never steers ("Queued: …");
  - offline, the box says so and Send is off;
  - New chat: project, provider, first message; the new chat opens;
  - New task: made in INTAKE and opened; Start asks first, then starts it;
  - Cancel on a running task: asked first, then the passkey, then cancelled;
  - a proposal withdrawn from its inbox row;
  - every control at least 44 px tall; hostile text stays text; no page errors.

Skipped when `hud/dist` has no phone shell (run `npm run build` in hud/).

Run: PYTHONPATH=. .venv/bin/python tests/face/hud_v2_phone_write_check.py
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

from tests.face.hud_v2_phone_answer_check import FakeSystem, HOSTILE, MARKUP, SHELL_HOST  # noqa: E402


def run(browser, check, until):
    print("\nThe phone writes (P-5): a message, New chat, New task, Start, Cancel, Withdraw")
    from jarvis import config
    from jarvis.v2 import remote_api
    from jarvis.v2.control import ControlError
    from jarvis.v2.daemon import Daemon
    from jarvis.v2.model import ProviderName as PN, Role, TaskState, to_json
    from jarvis.v2.provider import Decision, Event, EventKind as K, SessionHandle, Usage
    from jarvis.v2.stores import Stores
    dist = REPO / "hud" / "dist"
    if not (dist / "mobile.html").is_file() or not (dist / "build-manifest.json").is_file():
        print("  skip  hud/dist has no phone shell (run npm run build in hud/)")
        return

    class Fake:
        """A provider whose turn runs until `release`, and that could steer."""

        def __init__(self, name):
            self.name = name
            self.release = threading.Event()
            self.release.set()
            self.turns: list = []
            self.steered: list = []

        def health(self):
            return True, "fake"

        def start(self, thread, brief, permit):
            return SessionHandle(thread.id, self.name, "fake:" + thread.id, SimpleNamespace(brief=brief))

        resume = start

        def usage(self, handle):
            return Usage()

        def send(self, handle, message):
            self.turns.append(message)
            yield Event(K.TURN_STARTED, handle.thread_id)
            self.release.wait(20)
            yield Event(K.TEXT, handle.thread_id, {"text": "ok"})
            yield Event(K.TURN_FINISHED, handle.thread_id, {"stop": "end"})

        def steer(self, handle, message):
            self.steered.append(message)
            return True

        def interrupt(self, handle):
            self.release.set()

        def close(self, handle):
            pass

    class Runner:
        def __init__(self, daemon):
            self.daemon, self.calls = daemon, []

        def _done(self, task_id, state, reason):
            task = self.daemon.stores.tasks.transition(task_id, state, reason=reason)
            self.daemon.bus.publish({"kind": "task_status_changed", "task_id": task.id,
                                     "project_id": task.project_id, "data": to_json(task)})
            return task

        def start(self, task_id):
            self.calls.append(("start", task_id))
            if self.daemon.stores.tasks.get(task_id).state != TaskState.INTAKE:
                raise ControlError("not in intake")
            return self._done(task_id, TaskState.CLARIFYING, "intake accepted")

        def cancel(self, task_id):
            self.calls.append(("cancel", task_id))
            if not self.daemon.router.cancel_proposal(task_id):
                return self._done(task_id, TaskState.CANCELLED, "owner cancelled")
            return self.daemon.stores.tasks.get(task_id)

        def resume(self, task_id, *, provider=None):
            self.calls.append(("resume", task_id, provider))
            raise ControlError("only a blocked task resumes")

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
    brains = {name: Fake(name) for name in PN}
    daemon = Daemon(Stores(), brains, lambda *_: lambda *_: Decision.DENY, 0, remote_host=SHELL_HOST)
    runner = Runner(daemon)
    daemon.runner = runner
    daemon.start()
    daemon.secure.run = FakeSystem(daemon)
    port = daemon.remote_port
    project = daemon.stores.projects.create("Calc " + HOSTILE, str(root))
    chat = daemon.open_thread(project.id, Role.CHAT, PN.CLAUDE, {})
    net = {"offline": False}

    def forward(route):
        request = route.request
        url = urlsplit(request.url)
        path = url.path + ("?" + url.query if url.query else "")
        sent = request.all_headers()
        if net["offline"] and url.path.startswith("/m/api/"):
            return route.fulfill(status=502, body=b"")
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

    ctx = browser.new_context(viewport={"width": 390, "height": 844}, ignore_https_errors=True)
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
    tid = lambda id_: page.locator(f'[data-testid="{id_}"]')  # noqa: E731

    def pump(predicate, timeout=10.0):
        end = time.monotonic() + timeout
        while not predicate() and time.monotonic() < end:
            page.wait_for_timeout(50)
        return predicate()

    def clean():
        return page.locator(f".app :is({MARKUP})").count() == 0 and page.evaluate("window.__pwned === undefined")

    def small_targets():
        return page.evaluate("""Array.from(document.querySelectorAll('.app button, .app select, .app textarea'))
            .filter(e => e.offsetParent !== null).map(e => [e.dataset.testid || e.textContent,
            e.getBoundingClientRect().height]).filter(([, h]) => h < 44)""")

    def users(thread_id):
        return [r["data"] for r in daemon.stores.threads.read_log(thread_id) if r.get("kind") == "user"]

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
        check("P-5 setup: paired and signed in with the virtual authenticator", screen() == "app")

        # -- a message to a chat ----------------------------------------------------------
        tid("m-tab-chats").click()
        until(lambda: page.locator(f'[data-testid="m-chat"][data-id="{chat.id}"]').count() == 1, timeout=8)
        check("P-5 Chats: New chat is offered", tid("m-new-chat-open").count() == 1)
        page.locator(f'[data-testid="m-chat"][data-id="{chat.id}"]').click()
        until(lambda: tid("m-compose-text").count() == 1, timeout=8)
        box = tid("m-compose-text")
        box.fill("first from the phone " + HOSTILE)
        box.press("Enter")
        page.wait_for_timeout(400)
        check("P-5 message box: Enter is a new line and sends nothing", users(chat.id) == [])
        brains[PN.CLAUDE].release.clear()
        tid("m-compose-send").click()
        pump(lambda: users(chat.id))
        sent = users(chat.id)
        check("P-5 Send: one message, recorded via the phone, its words as typed",
              len(sent) == 1 and sent[0]["via"] == "phone" and sent[0]["typed"].startswith("first from the phone"),
              str(sent))
        until(lambda: page.locator('[data-testid="m-msg"]').count() == 1, timeout=8)
        check("P-5 Send: the message shows after the read, as text, marked via the phone",
              HOSTILE in page.locator('[data-testid="m-msg"]').first.inner_text()
              and "via the phone" in page.locator('[data-testid="m-msg"]').first.inner_text() and clean())
        until(lambda: tid("m-compose-note").count() == 1, timeout=8)
        check("P-5 Send: the box says Sent", tid("m-compose-note").inner_text() == "Sent.")
        pump(lambda: brains[PN.CLAUDE].turns)
        tid("m-compose-text").fill("second, while it runs")
        tid("m-compose-send").click()
        pump(lambda: len(users(chat.id)) == 2)
        until(lambda: "Queued" in (tid("m-compose-note").inner_text() if tid("m-compose-note").count() else ""),
              timeout=8)
        check("P-5 queue: behind a running turn the message waits and never steers",
              users(chat.id)[1].get("queued") is True and brains[PN.CLAUDE].steered == []
              and "Queued: it runs after the current turn." == tid("m-compose-note").inner_text(),
              str(users(chat.id)))
        check("P-5 app: every control is at least 44 px tall (the message box)", not small_targets(),
              str(small_targets()))
        brains[PN.CLAUDE].release.set()

        # -- offline ------------------------------------------------------------------------
        net["offline"] = True
        tid("m-compose-text").fill("never sent")
        tid("m-compose-send").click()
        until(lambda: tid("m-compose-offline").count() == 1, timeout=8)
        check("P-5 offline: the box says so, Send is off, and nothing was sent",
              tid("m-compose-send").is_disabled() and not any(u["typed"] == "never sent" for u in users(chat.id)))
        net["offline"] = False
        tid("m-tab-chats").click()
        until(lambda: tid("m-compose-offline").count() == 0 and not page.locator('[data-testid="m-offline"]').is_visible(),
              timeout=10)

        # -- New chat ----------------------------------------------------------------------
        tid("m-tab-chats").click()
        if tid("m-new-chat-open").count() == 0:
            tid("m-tab-chats").click()
        tid("m-new-chat-open").click()
        until(lambda: tid("m-new-provider").count() == 1, timeout=8)
        tid("m-new-provider").select_option("codex")
        tid("m-new-text").fill("hello codex")
        check("P-5 New chat: every control is at least 44 px tall", not small_targets(), str(small_targets()))
        before = {t.id for t in daemon.stores.threads.list()}
        tid("m-new-start").click()
        pump(lambda: {t.id for t in daemon.stores.threads.list()} - before)
        made = [t for t in daemon.stores.threads.list() if t.id not in before]
        until(lambda: tid("m-thread").count() == 1, timeout=8)
        check("P-5 New chat: a Codex chat in the project, its first message via the phone, and it opens",
              len(made) == 1 and made[0].provider == PN.CODEX and made[0].project_id == project.id
              and pump(lambda: users(made[0].id)) and users(made[0].id)[0]["via"] == "phone"
              and tid("m-compose").count() == 1, str(made))

        # -- New task, Start ---------------------------------------------------------------
        tid("m-tab-tasks").click()
        until(lambda: tid("m-new-task-open").count() == 1, timeout=8)
        tid("m-new-task-open").click()
        tid("m-new-brief").fill("add divide " + HOSTILE)
        tid("m-new-create").click()
        until(lambda: tid("m-task-start").count() == 1, timeout=8)
        (task,) = daemon.stores.tasks.list()
        check("P-5 New task: made in INTAKE, opened, waiting for Start (nothing started)",
              task.state == TaskState.INTAKE and runner.calls == [] and clean(), str(runner.calls))
        tid("m-task-start").click()
        check("P-5 Start: asks first", tid("m-task-ask").count() == 1 and runner.calls == [])
        check("P-5 task controls: every control is at least 44 px tall", not small_targets(), str(small_targets()))
        tid("m-task-confirm").click()
        pump(lambda: runner.calls)
        until(lambda: tid("m-task-start").count() == 0, timeout=8)
        check("P-5 Start: TaskControl.start, and the task moves on",
              runner.calls == [("start", task.id)]
              and daemon.stores.tasks.get(task.id).state == TaskState.CLARIFYING, str(runner.calls))

        # -- Cancel with the passkey -------------------------------------------------------
        until(lambda: tid("m-task-cancel").count() == 1, timeout=8)
        tid("m-task-cancel").click()
        check("P-5 Cancel: asks first, and says it cannot be undone",
              "cannot be resumed" in tid("m-task-ask").inner_text() and runner.calls == [("start", task.id)])
        tid("m-task-confirm").click()
        pump(lambda: len(runner.calls) == 2)
        check("P-5 Cancel: the passkey (virtual authenticator), then TaskControl.cancel",
              runner.calls[-1] == ("cancel", task.id)
              and daemon.stores.tasks.get(task.id).state == TaskState.CANCELLED, str(runner.calls))
        journal = [(r.get("verb"), r.get("via")) for r in daemon.stores.tasks.read_journal(task.id)
                   if r.get("event") == "owner_verb"]
        check("P-5: each verb is in the task's journal as the phone's",
              journal == [("create", "phone"), ("start", "phone"), ("cancel", "phone")], str(journal))

        # -- a proposal's withdraw ---------------------------------------------------------
        proposal = daemon.stores.tasks.create(project.id, "a proposal")
        daemon.stores.tasks.save(proposal)
        daemon.stores.tasks.journal(proposal.id, "proposed_by_fastpath", brief="b", thread_id=None, turn_id=None,
                                    reply="", not_before=daemon.router.clock() + 120)
        daemon.bus.publish({"kind": "task_created", "task_id": proposal.id, "project_id": project.id,
                            "data": to_json(proposal)})
        tid("m-tab-inbox").click()
        row = page.locator(f'[data-testid="m-inbox-row"][data-id="prop:{proposal.id}"]')
        until(lambda: row.count() == 1, timeout=8)
        row.locator('[data-testid="m-withdraw"]').click()
        check("P-5 Withdraw: asks first", row.locator('[data-testid="m-withdraw-ask"]').count() == 1
              and runner.calls[-1] != ("cancel", proposal.id))
        row.locator('[data-testid="m-withdraw-send"]').click()
        pump(lambda: runner.calls[-1] == ("cancel", proposal.id))
        check("P-5 Withdraw: the session only, TaskControl.cancel",
              runner.calls[-1] == ("cancel", proposal.id)
              and daemon.stores.tasks.get(proposal.id).state == TaskState.CANCELLED, str(runner.calls))
        check("P-5: no page errors", not errors, str(errors[:3]))
    finally:
        for brain in brains.values():
            brain.release.set()
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
            v2.check("Phone write scenario completes", False, f"{type(exc).__name__}: {exc}")
        finally:
            browser.close()
    print(f"{v2.CHECKS} phone write checks, {len(v2.FAILURES)} failures")
    return bool(v2.FAILURES)


if __name__ == "__main__":
    sys.exit(main())
