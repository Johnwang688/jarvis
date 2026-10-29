"""Push to talk from the phone (phone design §6; work package P-7), end to
end in a real headless Chromium at a phone's viewport.

The built `mobile.html` is served by a real daemon's remote listener, reached
as `https://<host>` (the context forwards that origin to the loopback port
with the trusted Host, as `tailscale serve` does); Chromium's virtual
authenticator stands in for the iPhone and its **fake audio device** for the
microphone (`--use-fake-device-for-media-stream`: a generated tone, never a
real mic). The daemon's speech-to-text is set to local only, with a fake
local engine and a spy in place of the cloud call.

  - the microphone is not asked for until the button is held;
  - hold, release: the clip is a 16 kHz mono WAV through the desk's local
    path, the words land in the box, and nothing is sent; the cloud is never
    called; every track has ended afterwards (the mic indicator goes out);
  - sliding off the button cancels: nothing is uploaded;
  - the Inbox has no mic; the button is at least 44 px tall;
  - Send, tapped, sends the words, via the phone; no page errors.

Skipped when `hud/dist` has no phone shell (run `npm run build` in hud/).

Run: PYTHONPATH=. .venv/bin/python tests/face/hud_v2_phone_talk_check.py
"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from unittest.mock import patch
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.face.hud_v2_phone_answer_check import FakeSystem, SHELL_HOST  # noqa: E402

WORDS = "remember the milk"

# Every stream the page opens, so the check can see each track end.
TRACKS = """
(() => {
  window.__streams = [];
  const md = navigator.mediaDevices;
  if (!md || !md.getUserMedia) return;
  const real = md.getUserMedia.bind(md);
  md.getUserMedia = async (c) => { const s = await real(c); window.__streams.push(s); return s; };
})();
"""


def run(browser, check, until):
    print("\nPush to talk from the phone (P-7): hold, release, the words in the box, unsent")
    from jarvis import config, llm, stt_local
    from jarvis.v2 import remote_api
    from jarvis.v2.daemon import Daemon
    from jarvis.v2.model import ProviderName as PN, Role
    from jarvis.v2.provider import Decision
    from jarvis.v2.stores import Stores
    dist = REPO / "hud" / "dist"
    if not (dist / "mobile.html").is_file() or not (dist / "build-manifest.json").is_file():
        print("  skip  hud/dist has no phone shell (run npm run build in hud/)")
        return

    class Local:
        def __init__(self):
            self.calls = []

        def decode(self, samples, rate):
            self.calls.append((len(samples), rate))
            return WORDS

        def close(self):
            pass

    cloud_calls = []
    local = Local()
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "home").mkdir()
    (root / "stt.json").write_text(json.dumps({"backend": "local", "cloud_fallback": False}))
    env = {k: v for k, v in os.environ.items() if k != "JARVIS_REMOTE_HOST"}
    env.update({"HOME": str(root / "home"), "XDG_DATA_HOME": str(root / "xdg"),
                "JARVIS_CLAUDE_CREDENTIALS": str(root / "none.json")})
    patches = [patch.dict(os.environ, env, clear=True)]
    patches += [patch.object(config, name, value) for name, value in dict(
        V2_DATA_DIR=root / "data", ALLOWLIST_PATH=root / "allow.json", MODELS_PATH=root / "models.json",
        PROVIDER_DEFAULTS_PATH=root / "provider_defaults.json", DISCORD_GUILD_PATH=root / "guild.json",
        STT_SETTINGS_PATH=root / "stt.json", SPEAKER_DIR=root / "speaker",
        HUD_DEV=True, HUD_DIST=dist).items()]
    patches.append(patch.object(remote_api, "RETRY_MS", 300))
    patches.append(patch.object(llm, "transcribe", lambda *a, **kw: cloud_calls.append(kw) or "cloud"))
    for guard in patches:
        guard.start()
    previous = stt_local.set_engine(stt_local.Engine(lambda threads: local, schedule=False))
    brains = {name: write_check_fake(name) for name in PN}
    daemon = Daemon(Stores(), brains, lambda *_: lambda *_: Decision.DENY, 0, remote_host=SHELL_HOST)
    daemon.start()
    daemon.secure.run = FakeSystem(daemon)
    port = daemon.remote_port
    project = daemon.stores.projects.create("Calc", str(root))
    chat = daemon.open_thread(project.id, Role.CHAT, PN.CLAUDE, {})
    uploads = []

    def forward(route):
        request = route.request
        url = urlsplit(request.url)
        path = url.path + ("?" + url.query if url.query else "")
        sent = request.all_headers()
        if url.path == "/m/api/events":
            return route.fulfill(**write_check_stream(port, path, sent))
        if url.path == "/m/api/stt":
            uploads.append((sent.get("content-type"), len(request.post_data_buffer or b"")))
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
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

    ctx = browser.new_context(viewport={"width": 390, "height": 844}, ignore_https_errors=True, has_touch=False)
    ctx.grant_permissions(["microphone"], origin=f"https://{SHELL_HOST}")
    ctx.add_init_script(TRACKS)
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
    streams = lambda: page.evaluate("window.__streams.length")  # noqa: E731
    live_tracks = lambda: page.evaluate(  # noqa: E731
        "window.__streams.flatMap(s => s.getTracks()).filter(t => t.readyState !== 'ended').length")

    def users(thread_id):
        return [r["data"] for r in daemon.stores.threads.read_log(thread_id) if r.get("kind") == "user"]

    def hold(ms, slide=False):
        box = tid("m-mic").bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.move(x, y)
        page.mouse.down()
        page.wait_for_timeout(ms)
        if slide:
            page.mouse.move(x, y - 300, steps=5)
        page.mouse.up()

    try:
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
        check("P-7 setup: paired and signed in with the virtual authenticator", screen() == "app")

        check("P-7 Inbox: no mic on the Inbox (answer surfaces never take speech)", tid("m-mic").count() == 0)

        tid("m-tab-chats").click()
        until(lambda: page.locator(f'[data-testid="m-chat"][data-id="{chat.id}"]').count() == 1, timeout=8)
        page.locator(f'[data-testid="m-chat"][data-id="{chat.id}"]').click()
        until(lambda: tid("m-mic").count() == 1, timeout=8)
        check("P-7 the chat's box has Hold to talk, and the mic was not asked for yet",
              tid("m-mic").inner_text() == "Hold to talk" and streams() == 0, str(streams()))
        height = tid("m-mic").bounding_box()["height"]
        check("P-7 the button is at least 44 px tall", height >= 44, str(height))
        mic, text = tid("m-mic").bounding_box(), tid("m-compose-text").bounding_box()
        check("P-7 the button is a round mic icon in the message bar, beside the box (2026-10-28)",
              mic["width"] <= 56 and mic["x"] >= text["x"] + text["width"]
              and abs((mic["y"] + mic["height"]) - (text["y"] + text["height"])) <= 2
              and page.locator('[data-testid="m-compose"] .m-bar [data-testid="m-mic"] .m-mic-icon').count() == 1,
              f"{mic} {text}")
        tid("m-compose-text").fill("please")

        hold(1500)
        until(lambda: "please " + WORDS == tid("m-compose-text").input_value(), timeout=15)
        check("P-7 hold and release: the words land in the box after what was typed",
              tid("m-compose-text").input_value() == "please " + WORDS, tid("m-compose-text").input_value())
        check("P-7 the clip went as audio/wav, through the desk's local path (16 kHz), never the cloud",
              uploads and uploads[0][0] == "audio/wav" and local.calls and local.calls[0][1] == 16000
              and 0.5 * 16000 <= local.calls[0][0] <= 5 * 16000 and cloud_calls == [],
              f"{uploads} {local.calls} {cloud_calls}")
        check("P-7 nothing was sent: the chat has no message", users(chat.id) == [], str(users(chat.id)))
        check("P-7 the microphone was released: every track ended", streams() == 1 and live_tracks() == 0,
              f"{streams()} {live_tracks()}")
        check("P-7 the line says the words are in the box, unsent",
              "unsent" in tid("m-talk-line").inner_text(), tid("m-talk-line").inner_text())

        hold(1200, slide=True)
        page.wait_for_timeout(800)
        check("P-7 sliding off cancels: nothing uploaded, the box unchanged, the mic released",
              len(uploads) == 1 and tid("m-compose-text").input_value() == "please " + WORDS
              and live_tracks() == 0 and "Cancelled" in tid("m-talk-line").inner_text(),
              f"{uploads} {live_tracks()} {tid('m-talk-line').inner_text()}")

        before = (streams(), len(uploads), tid("m-talk-line").inner_text())
        hold(40)
        page.wait_for_timeout(500)
        check("P-7 a brush shorter than the hold delay does nothing: no mic, no upload, no new line (review of #331)",
              (streams(), len(uploads), tid("m-talk-line").inner_text()) == before
              and tid("m-mic").get_attribute("data-phase") == "idle", str(before))

        tid("m-compose-send").click()
        deadline = time.monotonic() + 10
        while not users(chat.id) and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        sent = users(chat.id)
        check("P-7 only the Send tap sends, via the phone, with the dictated words",
              len(sent) == 1 and sent[0]["via"] == "phone" and sent[0]["typed"] == "please " + WORDS, str(sent))
        check("P-7: no page errors", not errors, str(errors[:3]))
    finally:
        for brain in brains.values():
            brain.release.set()
        ctx.close()
        daemon.stop()
        stt_local.set_engine(previous)
        for guard in reversed(patches):
            guard.stop()
        tmp.cleanup()


def write_check_fake(name):
    """The P-5 check's fake provider (a turn that ends at once)."""
    import types
    from jarvis.v2.provider import Event, EventKind as K, SessionHandle, Usage
    import threading

    class Fake:
        def __init__(self):
            self.name = name
            self.release = threading.Event()
            self.release.set()

        def health(self):
            return True, "fake"

        def start(self, thread, brief, permit):
            return SessionHandle(thread.id, self.name, "fake:" + thread.id, types.SimpleNamespace(brief=brief))

        resume = start

        def usage(self, handle):
            return Usage()

        def send(self, handle, message):
            yield Event(K.TURN_STARTED, handle.thread_id)
            yield Event(K.TEXT, handle.thread_id, {"text": "ok"})
            yield Event(K.TURN_FINISHED, handle.thread_id, {"stop": "end"})

        def steer(self, handle, message):
            return False

        def interrupt(self, handle):
            pass

        def close(self, handle):
            pass
    return Fake()


def write_check_stream(port, path, sent):
    """One window of the SSE stream, as the P-5 check reads it."""
    import socket
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


def main():
    from playwright.sync_api import sync_playwright
    from tests.face import hud_v2_check as v2
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--use-fake-device-for-media-stream",
                                                          "--use-fake-ui-for-media-stream"])
        try:
            run(browser, v2.check, v2.until)
        except Exception as exc:
            v2.check("Phone talk scenario completes", False, f"{type(exc).__name__}: {exc}")
        finally:
            browser.close()
    print(f"{v2.CHECKS} phone talk checks, {len(v2.FAILURES)} failures")
    return bool(v2.FAILURES)


if __name__ == "__main__":
    sys.exit(main())
