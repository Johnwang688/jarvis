"""End-to-end check of the whiteboard with a stubbed designer. Free — no API.

Covers, with the real page JS in headless Chromium:
  1. draw -> export -> POST /design -> reply rendered, preview iframe pointed
     at the workshop origin (and the served file actually contains the design)
  2. the ATTACH SKETCH checkbox: on sends the PNG, off sends text only
  3. the /design route's mtime-diff file detection and preview URL
  4. Agent.run_turn(images=...) builds a proper multimodal user message
     (fake llm.chat — nothing leaves the machine)

Run:  .venv/bin/python tests/face/whiteboard_check.py
"""

from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from playwright.sync_api import sync_playwright

from jarvis import config, tools

FACE_PORT = 8436
WORKSHOP_PORT = 8437


def agent_image_check() -> None:
    import jarvis.agent as agent_mod

    def fake_chat(model, messages, tools=None, **kwargs):
        return types.SimpleNamespace(
            message={"content": "ok"}, tool_calls=[], text="ok", cost_usd=0.0, latency_s=0.0
        )

    real = agent_mod.llm.chat
    agent_mod.llm.chat = fake_chat
    try:
        agent = agent_mod.Agent(approve=lambda tool, args: False)
        agent.run_turn("make this", images=["QUJD"])
        user = agent.messages[1]
        assert isinstance(user["content"], list), user
        assert user["content"][0] == {"type": "text", "text": "make this"}
        assert user["content"][1]["image_url"]["url"] == "data:image/png;base64,QUJD"
        assert agent.messages[2]["role"] == "assistant"

        agent.run_turn("plain text")
        assert isinstance(agent.messages[3]["content"], str)
    finally:
        agent_mod.llm.chat = real
    print("ok  agent: images ride the user message as image_url parts")


class StubDesigner:
    """Writes a real file (so the mtime diff finds it) and echoes what it got."""

    def run_turn(self, prompt, images=None):
        target = config.DESIGNS_DIR / "stub-design"
        target.mkdir(parents=True, exist_ok=True)
        (target / "index.html").write_text(
            "<h1 id='ok'>STUB DESIGN</h1>", encoding="utf-8"
        )
        return types.SimpleNamespace(
            text=f"built it (images={len(images or [])})", steps=2, cost_usd=0.0001
        )


def prompt_anchor_check() -> None:
    """The designer prompt must anchor writes and preview URLs exactly.

    Regression guard for 2026-07-31: a relative designs/ path in the prompt
    sent output to $CWD/designs (the owner launched `jarvis face` from ~, so
    the workshop 404'd), and a line-broken preview URL led the agent to
    guess a /designs/ prefix that does not exist.
    """
    from jarvis.face import server

    assert str(config.DESIGNS_DIR) in server.DESIGNER_SYSTEM, "absolute path missing"
    url = f"http://localhost:{config.WORKSHOP_PORT}/<short-kebab-name>/"
    assert url in server.DESIGNER_SYSTEM, "preview URL broken or mis-rooted"
    print("ok  prompt: absolute designs path and intact preview URL")


def whiteboard_checks() -> None:
    from jarvis.face import server

    face = server.create_server(port=FACE_PORT)
    workshop = server.create_workshop(port=WORKSHOP_PORT)
    server._designer = StubDesigner()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(f"http://localhost:{FACE_PORT}/whiteboard.html")

            # -- shape tools: real pointer drags through the letterbox math --
            box = page.locator("#board").bounding_box()
            cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

            def drag(tool, dx, dy, shift=False):
                page.click(f"#t-{tool}")
                page.mouse.move(cx - dx / 2, cy - dy / 2)
                if shift:
                    page.keyboard.down("Shift")
                page.mouse.down()
                page.mouse.move(cx + dx / 2, cy + dy / 2, steps=5)
                page.mouse.up()
                if shift:
                    page.keyboard.up("Shift")
                return page.evaluate("__wb.S.ops[__wb.S.ops.length - 1]")

            op = drag("rect", 200, 120)
            assert op["t"] == "r" and abs(op["w"]) > 50 and abs(op["h"]) > 30, op
            op = drag("circ", 150, 90, shift=True)
            assert op["t"] == "e" and abs(op["rx"] - op["ry"]) < 0.01 and op["rx"] > 20, op
            op = drag("line", 180, 40, shift=True)  # 12° drag must snap flat
            assert op["t"] == "l" and abs(op["by"] - op["ay"]) < 1, op
            assert page.evaluate("__wb.S.ops.length") == 3
            page.click("#t-undo")
            assert page.evaluate("__wb.S.ops.length") == 2
            page.evaluate("() => { __wb.S.ops = []; __wb.redraw(); __wb.setTool('pen'); }")
            print("ok  shapes: rect/circle/line drags commit; shift constrains; undo pops")

            # Draw a rectangle-ish sketch and a label through the model layer.
            page.evaluate(
                """() => {
                    __wb.addStroke([[200,200],[900,200],[900,600],[200,600],[200,200]]);
                    __wb.addText(350, 420, "BIG BUTTON HERE", 40);
                }"""
            )
            assert page.evaluate("__wb.S.ops.length") == 2
            assert page.evaluate("__wb.exportPng().length > 1000")

            # Attach on (default): the sketch must reach the designer.
            page.fill("#prompt", "make it real")
            page.click("#send")
            page.wait_for_function(
                "document.getElementById('reply-panel').textContent.includes('built it')",
                timeout=15_000,
            )
            assert "images=1" in page.text_content("#reply-panel")

            iframe_src = page.get_attribute("#preview", "src")
            assert f":{WORKSHOP_PORT}/stub-design/index.html" in iframe_src, iframe_src

            # Attach off: text-only iteration.
            page.uncheck("#attach")
            page.fill("#prompt", "tweak the color")
            page.click("#send")
            page.wait_for_function(
                "document.getElementById('reply-panel').textContent.includes('images=0')",
                timeout=15_000,
            )
            browser.close()
        print("ok  whiteboard: draw -> send -> reply + preview; attach toggle honored")

        conn = http.client.HTTPConnection("localhost", WORKSHOP_PORT, timeout=5)
        conn.request("GET", "/stub-design/index.html")
        response = conn.getresponse()
        body = response.read().decode()
        assert "STUB DESIGN" in body
        assert response.getheader("Cache-Control") == "no-store"
        conn.close()
        print("ok  workshop: design served from its own origin, uncached")

        # -- whiteboard_close: board closes itself, the HUD never does -------
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            board = browser.new_page()
            board.goto(f"http://localhost:{FACE_PORT}/whiteboard.html")
            hud = browser.new_page()
            hud.goto(f"http://localhost:{FACE_PORT}/jarvis.html")
            board.wait_for_timeout(600)  # let both SSE streams attach

            out = tools.dispatch("whiteboard_close", "{}")
            assert "Close signal" in out.text, out.text
            deadline = time.time() + 5
            while time.time() < deadline and not board.is_closed():
                board.wait_for_timeout(100)
            closed = board.is_closed() or "SAFE TO DISMISS" in board.content()
            assert closed, "whiteboard neither closed nor blanked"
            assert not hud.is_closed(), "the HUD must ignore wb_close"
            hud.wait_for_timeout(300)
            assert not hud.is_closed()
            browser.close()
        print("ok  close: whiteboard shuts itself on the signal; HUD ignores it")
    finally:
        server._designer = None
        face.shutdown()
        face.server_close()
        workshop.shutdown()
        workshop.server_close()


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        config.DESIGNS_DIR = Path(tmp) / "designs"
        config.WORKSHOP_PORT = WORKSHOP_PORT
        agent_image_check()
        prompt_anchor_check()
        whiteboard_checks()
    print("\nall whiteboard checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
