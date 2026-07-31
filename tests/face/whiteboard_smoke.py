"""Real end-to-end whiteboard smoke: sketch -> Luna -> served design.

Makes REAL API calls (~$0.005-0.02 per run: one vision design turn, possibly
with a self-check screenshot). The sketch is scripted so runs are comparable;
the human grades the produced design by eye — pass it an out_dir to get a
PNG of the rendered result.

Run:  JARVIS_BROWSER_HEADLESS=1 .venv/bin/python tests/face/whiteboard_smoke.py [out_dir]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from playwright.sync_api import sync_playwright

FACE_PORT = 8438

SKETCH = """() => {
    // top nav bar
    __wb.addStroke([[60,60],[1340,60]], "#1a1a1a", 4);
    __wb.addText(80, 45, "JARVIS", 28);
    __wb.addText(1050, 45, "docs   github   about", 22);
    // big centered title
    __wb.addStroke([[350,220],[1050,220],[1050,330],[350,330],[350,220]], "#2563eb", 4);
    __wb.addText(430, 290, "YOUR PERSONAL AI  (big title)", 32);
    __wb.addText(500, 400, "one-line subtitle here", 24);
    // call-to-action button
    __wb.addStroke([[580,480],[820,480],[820,560],[580,560],[580,480]], "#d33030", 6);
    __wb.addText(620, 530, "GET STARTED", 28, "#d33030");
    // annotation
    __wb.addText(950, 700, "dark theme, glowing cyan accents", 24, "#16a34a");
}"""

PROMPT = (
    "Turn this sketch into a polished landing hero for 'Jarvis', a personal "
    "AI assistant. Follow the annotations on the sketch."
)


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    from jarvis.face import server

    face = server.create_server(port=FACE_PORT)
    workshop = server.create_workshop()  # real port, real designs/
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1500, "height": 950})
            page.goto(f"http://localhost:{FACE_PORT}/whiteboard.html")
            page.evaluate(SKETCH)
            page.fill("#prompt", PROMPT)
            page.click("#send")
            page.wait_for_function(
                "document.getElementById('status').textContent.startsWith('DONE') || "
                "document.getElementById('status').textContent === 'ERROR'",
                timeout=360_000,
            )
            status = page.text_content("#status")
            reply = page.text_content("#reply-panel")
            preview = page.get_attribute("#preview", "src") or "(none)"
            print(f"status : {status}")
            print(f"reply  : {reply[:400]}")
            print(f"preview: {preview}")
            assert status.startswith("DONE"), reply
            assert preview != "(none)", "no design file was produced"

            # Render the produced design for human grading.
            shot = browser.new_page(viewport={"width": 1280, "height": 800})
            shot.goto(preview)
            shot.wait_for_timeout(800)
            shot.screenshot(path=str(out_dir / "whiteboard_smoke_design.png"))
            sketch_path = out_dir / "whiteboard_smoke_sketch.png"
            page.screenshot(path=str(sketch_path), clip={"x": 0, "y": 0, "width": 1500, "height": 950})
            print(f"saved  : {out_dir / 'whiteboard_smoke_design.png'} (+ sketch view)")
            browser.close()
    finally:
        face.shutdown()
        face.server_close()
        workshop.shutdown()
        workshop.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
