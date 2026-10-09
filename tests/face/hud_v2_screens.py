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

SHOTS = [
    ("hud-zoom-100.png", 100, {}),
    ("hud-zoom-140.png", 140, {}),
    ("hud-panes-collapsed.png", 100, {"leftCollapsed": True, "rightCollapsed": True}),
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
            for name, zoom, layout in SHOTS:
                ctx = browser.new_context(viewport={"width": 1280, "height": 800})
                ctx.add_init_script(
                    "try { localStorage.setItem('jarvis.hud.zoom', %s);"
                    " localStorage.setItem('jarvis.hud.layout', %s); } catch (e) {}"
                    % (json.dumps(str(zoom)), json.dumps(json.dumps(layout))))
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
