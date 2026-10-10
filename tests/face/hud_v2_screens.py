"""Headless screenshots of the HUD v2 at a few zooms and pane states.

Free, against the mock (`hud_v2_mock.py`), like `hud_v2_check.py`. A helper
for eyes, not a check: it asserts nothing. Writes PNGs to the directory given
(default `docs/screenshots/`).

Run:  PYTHONPATH=. .venv/bin/python tests/face/hud_v2_screens.py [outdir]
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from playwright.sync_api import sync_playwright  # noqa: E402

from tests.face.hud_v2_mock import DIST, MockDaemon  # noqa: E402

PORT = int(os.environ.get("HUD_V2_CHECK_PORT", "8481"))
BASE = f"http://127.0.0.1:{PORT}"

# The workspace (2026-10-09): a preset and what each pane shows.
COLS2 = {"preset": "cols2", "panes": [{"view": "chat"}, {"view": "file"}, {"view": "file"}, {"view": "task"}]}
GRID4 = {"preset": "grid4", "panes": [{"view": "chat"}, {"view": "file"}, {"view": "preview"}, {"view": "task"}],
         "panel": {"open": True}}

SHOTS = [
    ("hud-zoom-100.png", 100, {}, (1280, 800), {}),
    ("hud-zoom-140.png", 140, {}, (1280, 800), {}),
    ("hud-panes-collapsed.png", 100, {"leftCollapsed": True, "rightCollapsed": True}, (1280, 800), {}),
    # A small window at a high zoom: the window folds both panes for the render.
    ("hud-1024x700-zoom-160.png", 160, {}, (1024, 700), {}),
    ("hud-cols2-100.png", 100, {}, (1600, 900), COLS2),
    ("hud-cols2-160.png", 160, {}, (1600, 900), COLS2),
    ("hud-grid4-100.png", 100, {}, (1600, 900), GRID4),
    ("hud-grid4-160.png", 160, {}, (1600, 900), GRID4),
]


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "docs" / "screenshots"
    out.mkdir(parents=True, exist_ok=True)
    if not (DIST / "index.html").exists():
        print("hud/dist is not built. Run: cd hud && npm run build")
        return 2
    mock = MockDaemon(PORT).start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            for name, zoom, layout, (w, h), workspace in SHOTS:
                ctx = browser.new_context(viewport={"width": w, "height": h})
                ctx.add_init_script(
                    "try { localStorage.setItem('jarvis.hud.zoom', %s);"
                    " localStorage.setItem('jarvis.hud.layout', %s);"
                    " localStorage.setItem('jarvis.hud.workspace', %s); } catch (e) {}"
                    % (json.dumps(str(zoom)), json.dumps(json.dumps(layout)), json.dumps(json.dumps(workspace))))
                page = ctx.new_page()
                page.goto(BASE + "/")
                page.wait_for_selector('[data-testid="sidebar"]', state="attached")
                time.sleep(1.2)
                page.screenshot(path=str(out / name))
                print(f"wrote {out / name}")
                ctx.close()
            browser.close()
    finally:
        mock.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
