"""The HUD under its Content-Security-Policy (editor plan §2.7, ED-4).

Free, headless, against a `MockDaemon(0)` of its own (never 8402/8403/8405),
which sends the daemon's own `hud_api.frame_headers` — the whole CSP — on
every response. Two halves, and the second is the one that matters:

1. **What the HUD needs still works, with no violation at all**: the page and
   every asset carry the one policy (and no `<meta>` policy exists); the
   `/events` stream delivers (an approval card, with its Web Audio chime);
   the terminal socket attaches (played by the terminal suite's `FakePty`)
   and a real same-origin `ws://` opens with no violation; Monaco mounts — the
   real editor, not its textarea fallback — and starts its `editor.worker`;
   the avatar `<img>` draws; the microphone runs (getUserMedia, AudioContext)
   and a dictated clip reaches `POST /stt`; a loopback Browser frame loads.
   Every `securitypolicyviolation` fails the run (`hud_v2_check.watch_csp`,
   which now watches every context of every HUD suite).

2. **What an attacker would need does not run**: an inline `<script>`, a
   script from another origin or a `data:` URL, an inline event handler, a
   `javascript:` URL, `eval`/`new Function`/a string `setTimeout`, a `blob:`
   worker, a `fetch` or an image from another origin, `<object>`, `<base>`,
   a form posted elsewhere, and a frame that is not loopback. WebAssembly
   *does* compile since ED-5 (`'wasm-unsafe-eval'`, tree-sitter's grammars)
   while `eval` and `new Function` stay refused — the one thing the policy
   gained is shown not to bring JavaScript-from-strings with it. Each is shown not to have run *and* to have
   raised a violation naming the directive that stopped it — which is also
   the proof that the watch in (1) bites. The "evil" origin is a server of
   this suite's own on another ephemeral port; it must see no request.

Run alone (after `cd hud && npm ci && npm run build`):
    PYTHONPATH=. .venv/bin/python tests/face/hud_v2_csp_check.py
It also runs inside `hud_v2_check.py`, after the browser section.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from jarvis.v2 import hud_api as H  # noqa: E402

# A minimal WebAssembly module: the magic number and version, nothing else.
WASM = "[0,97,115,109,1,0,0,0]"


class _Evil:
    """Another origin (127.0.0.1, another port) that would serve anything; the
    policy must keep the HUD from asking it for anything at all."""

    def __init__(self):
        hits = self.hits = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                hits.append(self.path)
                body = b"window.__pwnedRemote = 1;"
                self.send_response(200)
                self.send_header("Content-Type", "text/javascript")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_POST = do_GET

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def csp_checks(browser, mock, base, check, until, guard_live, init_script):
    """`guard_live(ctx)` refuses the live daemon's ports; violations are
    watched here, into a list of this section's own (part 2 raises them on
    purpose)."""
    from tests.face.hud_v2_check import watch_csp
    from tests.face.hud_v2_terminal_check import FakePty, _approval, _boot, _open_terminal, pumping

    print("\ncontent security policy (ED-4)")
    violations: list[str] = []
    want = H.content_security_policy()
    evil = _Evil()
    ctx = browser.new_context(viewport={"width": 1400, "height": 900}, permissions=["microphone"])
    guard_live(ctx)
    watch_csp(ctx, violations.append)
    ctx.add_init_script(init_script)
    fake = FakePty(ctx, mock)
    responses: list = []
    ctx.on("response", lambda r: responses.append(r) if r.url.startswith(base) else None)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    console: list[str] = []
    page.on("console", lambda m: console.append(m.text))
    workers: list = []
    page.on("worker", lambda w: workers.append(w))
    page.set_default_timeout(6000)
    until = pumping(page, until, fake)
    mock.world["avatars"]["active"] = "hoot"
    try:
        _boot(page, mock, base, until)

        # CDP asks Chrome to load the actual <link rel=manifest>, exercising
        # manifest-src (a fetch() would exercise connect-src instead).
        manifest = ctx.new_cdp_session(page).send("Page.getAppManifest")
        check("Chrome loads the linked manifest with no manifest CSP violation",
              manifest.get("url") == base + "/manifest.webmanifest"
              and bool(manifest.get("data")) and not manifest.get("errors") and not violations,
              f"{manifest.get('errors')}; {violations[:2]}")
        listed = json.loads(manifest.get("data") or "{}").get("icons", [])
        icons = page.evaluate("""async icons => Promise.all(icons.map(icon => new Promise(done => {
          const image = new Image();
          image.onload = () => done({src: icon.src, width: image.naturalWidth, height: image.naturalHeight});
          image.onerror = () => done({src: icon.src, width: 0, height: 0});
          image.src = icon.src;
        })))""", listed)
        check("all six manifest icons load as images under img-src without a violation",
              len(icons) == 6 and all(f"{i['width']}x{i['height']}" == spec["sizes"]
                                         for i, spec in zip(icons, listed)) and not violations,
              f"{icons}; {violations[:2]}")
        manifest_responses = [r for r in responses if r.url == base + "/manifest.webmanifest"]
        check("the installer receives application/manifest+json explicitly",
              bool(manifest_responses) and all(r.status == 200 and
                  r.headers.get("content-type", "").split(";")[0] == "application/manifest+json"
                  for r in manifest_responses))

        # -- 1. the policy is on everything the window loaded --------------------
        own = [r for r in responses if not r.url.split("?")[0].endswith("/avatar.svg")]
        policies = {r.url: r.headers_array() for r in own}
        bad = []
        for url, headers in policies.items():
            csp = [h["value"] for h in headers if h["name"].lower() == "content-security-policy"]
            if csp != [want]:
                bad.append(f"{url}: {csp}")
        check("the page and every asset it loaded carry the one full policy",
              own and not bad, "; ".join(bad[:3]) or "no responses seen")
        check("the policy forbids inline and remote script, eval and plugins (WebAssembly only, ED-5)",
              "script-src 'self' 'wasm-unsafe-eval';" in want
              and "'unsafe-inline'" not in want.split("script-src")[1].split(";")[0]
              and " 'unsafe-eval'" not in want and "object-src 'none'" in want and "default-src 'none'" in want)
        check("no <meta> policy: the header is the only one",
              page.evaluate("document.querySelectorAll('meta[http-equiv]').length") == 0)

        # -- SSE: an approval card arrives on the stream, with its chime ----------
        _approval(mock, "csp-card-1", "ls -la")
        until(lambda: page.locator('[data-testid="approval-card"]').count() > 0)
        check("the event stream delivers under the policy (an approval card appeared)",
              page.locator('[data-testid="approval-card"]').count() == 1)
        page.locator('[data-testid="approval-deny"]').click()
        until(lambda: page.locator('[data-testid="approval-card"]').count() == 0)

        # -- the avatar ------------------------------------------------------------
        until(lambda: page.evaluate(
            "(() => { const i = document.getElementById('avatar'); return !!i && i.complete && i.naturalWidth > 0 })()"))
        check("the avatar <img> draws from /avatar.svg",
              page.evaluate("(() => { const i = document.getElementById('avatar');"
                            " return !!i && i.naturalWidth > 0 && i.style.display !== 'none' })()"))
        svg = [r for r in responses if r.url.split("?")[0].endswith("/avatar.svg")]
        svg_csp = [h["value"] for h in svg[-1].headers_array()
                   if h["name"].lower() == "content-security-policy"] if svg else []
        check("the avatar's own response is narrowed to its own lockdown, in one header",
              svg_csp == [H.content_security_policy(getattr(H, "AVATAR_CSP", None))], str(svg_csp))

        # -- the terminal socket ------------------------------------------------------
        tid = _open_terminal(page, mock, until, fake)
        check("the terminal socket attaches under the policy", bool(tid))
        page.keyboard.press("Control+Backquote")            # the panel shut again
        ws_ok = page.evaluate("""port => new Promise(done => {
            const ws = new WebSocket(`ws://127.0.0.1:${port}/csp-probe`);
            const t = setTimeout(() => done('timeout'), 3000);
            ws.onerror = () => { clearTimeout(t); done('error'); };
            ws.onopen = () => { clearTimeout(t); ws.close(); done('open'); };
        })""", mock.port)
        check("a same-origin ws:// is allowed by connect-src 'self' (it fails only at the server)",
              ws_ok in ("error", "open") and not violations, f"{ws_ok}; {violations[:2]}")

        # -- audio: the microphone and a dictated clip to /stt ------------------------
        page.locator('[data-testid="dictation-review"]').click()
        stt_before = len(mock.sent("POST", "/stt"))
        page.evaluate("""() => { const m = window.__hud.mic;
            m.feedMs(2000, 0.001); window.__hud.capture.openFollowUp();
            m.feedMs(1600, 0.06); m.feedMs(2800, 0.0005); }""")
        until(lambda: len(mock.sent("POST", "/stt")) > stt_before)
        check("a dictated clip reaches POST /stt under the policy",
              len(mock.sent("POST", "/stt")) > stt_before)
        check("the microphone runs through Web Audio",
              page.evaluate("typeof AudioContext === 'function' && !!window.__hud.mic"))
        until(lambda: page.locator('[data-testid="input"]').input_value() != "")
        page.locator('[data-testid="input"]').fill("")

        # -- Monaco: the real editor, and its worker ----------------------------------
        page.locator('[data-testid="tab-file"]').first.click()
        until(lambda: page.locator('[data-testid="file-calc.py"]').count() > 0)
        page.locator('[data-testid="file-calc.py"]').first.click()
        until(lambda: page.locator('[data-testid="editor"] .monaco-editor').count() > 0
              or page.locator('[data-testid="editor-fallback"]').count() > 0, timeout=15)
        check("Monaco itself mounts (not its textarea fallback)",
              page.locator('[data-testid="editor"] .monaco-editor').count() == 1
              and page.locator('[data-testid="editor-fallback"]').count() == 0)
        # The worker: a diff is computed in Monaco's editor.worker. Playwright
        # reports every dedicated worker the page starts; a blocked one never
        # starts (and Monaco would say it fell back to the main thread).
        from tests.face.hud_v2_check import expand
        expand(page, "p1", "task-k1")
        page.locator('[data-testid="task-k1"]').click()
        page.locator('[data-testid="tab-diff"]').first.click()
        until(lambda: page.locator('[data-testid="diff-calc.py"]').count() > 0)
        page.locator('[data-testid="diff-calc.py"]').first.click()
        until(lambda: page.locator('[data-testid="diff-editor"] .monaco-diff-editor').count() > 0
              or page.locator('[data-testid="diff-fallback"]').count() > 0, timeout=15)
        check("the diff opens in Monaco's diff editor (not its fallback)",
              page.locator('[data-testid="diff-editor"] .monaco-diff-editor').count() == 1)
        until(lambda: any("editor.worker" in w.url for w in workers), timeout=10)
        check("Monaco's editor.worker starts from /assets under worker-src 'self'",
              any("editor.worker" in w.url and w.url.startswith(base + "/assets/") for w in workers),
              str([w.url for w in workers]))
        until(lambda: page.locator('[data-testid="diff-editor"] .line-insert, '
                                   '[data-testid="diff-editor"] .char-insert').count() > 0, timeout=10)
        check("the worker computed the diff (changed lines are marked)",
              page.locator('[data-testid="diff-editor"] .line-insert, '
                           '[data-testid="diff-editor"] .char-insert').count() > 0)
        check("Monaco never fell back to running its worker on the main thread",
              not [c for c in console if "Could not create web worker" in c],
              "; ".join(c for c in console if "worker" in c.lower())[:300])

        # -- a loopback Preview frame (frame-src) ------------------------------------
        loaded = page.evaluate("""port => new Promise(done => {
            const f = document.createElement('iframe');
            f.setAttribute('sandbox', 'allow-scripts allow-forms');
            f.style.cssText = 'position:fixed;width:10px;height:10px;left:-50px';
            f.onload = () => done(true); setTimeout(() => done(false), 4000);
            f.src = `http://localhost:${port}/`; document.body.appendChild(f);
        })""", mock.dev_port)
        check("a loopback dev server loads in a frame (frame-src)", loaded and mock.dev_hits,
              f"loaded={loaded} hits={mock.dev_hits[-2:]}")

        check("nothing the HUD needs raised a violation", not violations, "; ".join(violations[:4]))
        check("no page errors under the policy", not errors, "; ".join(errors[:3]))

        # -- 2. what an attacker would need is refused --------------------------------
        def blocked(name, script, flag, directive, arg=None):
            before = len(violations)
            result = page.evaluate(script, arg) if arg is not None else page.evaluate(script)
            page.wait_for_timeout(250)
            ran = page.evaluate(f"!!window.{flag}") if flag else False
            new = violations[before:]
            named = any(f": {directive} " in v for v in new)
            check(f"{name}: it did not run and the policy said why ({directive})",
                  not ran and named, f"ran={ran} result={result!r} new={new[:2]}")
            return result

        blocked("an inline <script> added to the page",
                "() => { const s = document.createElement('script');"
                " s.textContent = 'window.__pwnedInline = 1'; document.body.appendChild(s); }",
                "__pwnedInline", "script-src-elem")
        blocked("a script from another origin",
                "u => { const s = document.createElement('script'); s.src = u; document.body.appendChild(s); }",
                "__pwnedRemote", "script-src-elem", f"http://127.0.0.1:{evil.port}/evil.js")
        blocked("a data: script",
                "() => { const s = document.createElement('script');"
                " s.src = 'data:text/javascript,window.__pwnedData=1'; document.body.appendChild(s); }",
                "__pwnedData", "script-src-elem")
        blocked("an inline event handler",
                "() => { const d = document.createElement('div');"
                " d.setAttribute('onclick', 'window.__pwnedAttr = 1'); document.body.appendChild(d); d.click(); }",
                "__pwnedAttr", "script-src-attr")
        blocked("a javascript: URL",
                "() => { const a = document.createElement('a'); a.href = 'javascript:window.__pwnedJs=1';"
                " document.body.appendChild(a); a.click(); }",
                "__pwnedJs", "script-src-elem")
        # From a timer, not inside `page.evaluate`: code DevTools is evaluating
        # may compile strings whatever the policy says, and a page's own code
        # never runs that way.
        thrown = blocked("eval",
                         "() => new Promise(done => setTimeout(() => { try {"
                         " eval('window.__pwnedEval = 1'); done('ran') } catch (e) { done(e.name) } }, 0))",
                         "__pwnedEval", "script-src")
        check("eval throws EvalError", thrown == "EvalError", repr(thrown))
        thrown = blocked("new Function",
                         "() => new Promise(done => setTimeout(() => { try {"
                         " new Function('window.__pwnedFn = 1')(); done('ran') } catch (e) { done(e.name) } }, 0))",
                         "__pwnedFn", "script-src")
        check("new Function throws EvalError", thrown == "EvalError", repr(thrown))
        blocked("a string setTimeout", "() => { setTimeout('window.__pwnedTimer = 1', 0) }",
                "__pwnedTimer", "script-src")
        thrown = blocked("a blob: worker",
                         "() => new Promise(done => { try {"
                         " const w = new Worker(URL.createObjectURL(new Blob(['self.postMessage(1)'],"
                         " {type: 'text/javascript'})));"
                         " w.onmessage = () => done('ran'); w.onerror = () => done('error');"
                         " setTimeout(() => done('silent'), 2000) } catch (e) { done(e.name) } })",
                         "", "worker-src")
        check("a blob: worker never runs", thrown in ("error", "SecurityError"), repr(thrown))
        # WebAssembly compiles since ED-5 ('wasm-unsafe-eval'): from a timer,
        # like eval above, so it is the page's own code that compiles it.
        before = len(violations)
        wasm = page.evaluate(f"""() => new Promise(done => setTimeout(() =>
            WebAssembly.compile(new Uint8Array({WASM})).then(m => WebAssembly.instantiate(m))
              .then(() => done('compiled'), e => done(e.name)), 0))""")
        check("WebAssembly compiles and instantiates under 'wasm-unsafe-eval' (ED-5), with no violation",
              wasm == "compiled" and len(violations) == before, f"{wasm}; {violations[before:][:2]}")
        fetched = blocked("a fetch to another origin",
                          "u => fetch(u).then(() => 'fetched', e => e.name)", "", "connect-src",
                          f"http://127.0.0.1:{evil.port}/exfiltrate")
        check("the fetch rejects", fetched == "TypeError", repr(fetched))
        blocked("a WebSocket to another origin",
                "u => { try { new WebSocket(u); return 'made' } catch (e) { return e.name } }", "",
                "connect-src", f"ws://127.0.0.1:{evil.port}/")
        blocked("an image from another origin",
                "u => { const i = new Image(); i.src = u; document.body.appendChild(i); }", "",
                "img-src", f"http://127.0.0.1:{evil.port}/pixel.png")
        blocked("an <object>",
                "u => { const o = document.createElement('object'); o.data = u; document.body.appendChild(o); }",
                "", "object-src", f"http://127.0.0.1:{evil.port}/plugin")
        blocked("a <base> that would re-point relative URLs",
                "u => { const b = document.createElement('base'); b.href = u; document.head.appendChild(b); }",
                "", "base-uri", f"http://127.0.0.1:{evil.port}/")
        blocked("a frame that is not loopback",
                "() => { const f = document.createElement('iframe'); f.src = 'http://example.invalid/';"
                " document.body.appendChild(f); }", "", "frame-src")
        url = page.url
        blocked("a form posted to another origin",
                "u => { const f = document.createElement('form'); f.method = 'POST'; f.action = u;"
                " document.body.appendChild(f); f.submit(); }", "", "form-action",
                f"http://127.0.0.1:{evil.port}/collect")
        page.wait_for_timeout(300)
        check("the window stayed where it was", page.url == url, page.url)
        check("the other origin was never asked for anything", not evil.hits, str(evil.hits[:5]))
    finally:
        ctx.close()
        evil.stop()
        mock.world["avatars"]["active"] = ""


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
            csp_checks(browser, mock, f"http://127.0.0.1:{mock.port}", v2.check, v2.until,
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
    print(f"all {v2.CHECKS} CSP checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
