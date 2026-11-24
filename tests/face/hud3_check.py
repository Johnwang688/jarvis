"""Exercise the built v3 HUD with its production CSP and an isolated fake status endpoint."""

from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
from threading import Thread
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

HUD = Path(__file__).resolve().parents[2] / "hud3"
DIST = HUD / "dist"
CSP = (
    "default-src 'none'; script-src 'self'; connect-src 'self'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "font-src 'self' data:; worker-src 'self'; manifest-src 'self'"
)
STATUS = json.dumps(
    {"service": "jarvis-agentd", "version": "v3", "pid": 4242, "uptime_s": 61}
).encode()
PANELS = ["cron", "feed", "skills", "status", "t3", "vault"]


class Handler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".webmanifest": "application/manifest+json"}

    def end_headers(self):
        self.send_header("Content-Security-Policy", CSP)
        super().end_headers()

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path != "/v3/status":
            return super().do_GET()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(STATUS)))
        self.end_headers()
        self.wfile.write(STATUS)


def check(origin: str, screenshot: Path | None):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1280, "height": 860})
        context.add_init_script(
            """
            window.cspViolations = [];
            addEventListener('securitypolicyviolation', event => {
              window.cspViolations.push([event.violatedDirective, event.blockedURI]);
            });
            """
        )
        requests: list[str] = []
        foreign: list[str] = []
        errors: list[str] = []

        def enforce_origin(route):
            url = route.request.url
            if url.startswith(origin + "/"):
                route.continue_()
            else:
                foreign.append(url)
                route.abort()

        context.route("**/*", enforce_origin)
        context.on("request", lambda request: requests.append(request.url))
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        page.set_default_timeout(6000)
        response = page.goto(origin, wait_until="load")
        assert response and response.ok and response.headers["content-security-policy"] == CSP
        manifest = context.new_cdp_session(page).send("Page.getAppManifest")
        assert not manifest.get("errors"), manifest.get("errors")
        assert json.loads(manifest["data"])["name"] == "J.A.R.V.I.S. Agent"

        expect(page.get_by_role("heading", name="J.A.R.V.I.S. Agent")).to_be_visible()
        expect(page.locator('[data-panel="status"]')).to_contain_text("Running")
        expect(page.locator('[data-panel="status"]')).to_contain_text("4242")
        assert sorted(page.locator("section[data-panel]").evaluate_all(
            "panels => panels.map(panel => panel.dataset.panel)"
        )) == PANELS
        assert page.locator("iframe, button, input, select, textarea, [tabindex]").count() == 0

        for width, height in [(1280, 860), (900, 600)]:
            page.set_viewport_size({"width": width, "height": height})
            page.wait_for_function(
                """() => {
                    const orb = document.querySelector('canvas[data-orb][aria-hidden="true"]');
                    if (!orb || orb.width !== Math.round(orb.clientWidth * Math.min(devicePixelRatio, 2))) return false;
                    const pixels = orb.getContext('2d').getImageData(0, 0, orb.width, orb.height).data;
                    return pixels.some((value, index) => index % 4 === 3 && value > 0);
                }"""
            )
            assert page.evaluate(
                """() => [document.documentElement, document.body, document.querySelector('#root'),
                          document.querySelector('main')].every(node => node.scrollWidth <= node.clientWidth)"""
            ), f"Horizontal overflow at {width}x{height}"
            assert page.evaluate("document.activeElement === document.body"), f"Focus moved at {width}x{height}"
            for panel in PANELS:
                expect(page.locator(f'[data-panel="{panel}"]')).to_be_in_viewport(ratio=1)
            print(f"PASS {width}x{height} · painted orb, six panels, no horizontal overflow, body focused")
            if screenshot and width == 1280:
                page.screenshot(path=str(screenshot))
        page.emulate_media(reduced_motion="reduce")
        assert page.locator("canvas[data-orb]").evaluate(
            """async orb => {
                const frame = () => new Promise(resolve => requestAnimationFrame(resolve));
                await frame();
                await frame();
                const still = orb.toDataURL();
                await frame();
                await frame();
                return orb.toDataURL() === still;
            }"""
        ), "Orb continued animating with reduced motion"
        assert all(url.startswith(origin + "/") for url in requests) and not foreign, foreign
        assert {urlsplit(url).path for url in requests if urlsplit(url).path.startswith("/v3/")} == {"/v3/status"}
        assert not errors, errors
        violations = page.evaluate("window.cspViolations")
        assert not violations, violations
        print("PASS reduced motion · orb stays still")
        print("PASS same-origin requests only · CSP and console clean")
        browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    if not DIST.is_dir():
        subprocess.run(["npm", "run", "build"], cwd=HUD, check=True)

    with ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=DIST)) as server:
        port = server.server_address[1]
        assert not 8402 <= port <= 8410
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            check(f"http://127.0.0.1:{port}", args.screenshot)
        finally:
            server.shutdown()
            worker.join()


if __name__ == "__main__":
    main()
