"""Synthetic checks for the vocab-bench drill page. Free — no API calls.

Verifies, with a headless browser against a local server:
  1. determinism — same seed produces the identical question sequence,
     a different seed does not
  2. a full solve — all four question types answered via real DOM
     interaction, score n/n, nothing timed out
  3. the idle detector — overlay appears, Resume recovers, event is recorded
  4. the session timer — expiry forces the results screen with timedOut=true

The solver reads window.__answerKey to know the right answers; agents never
can (they have no JS-eval tool), so this is a test backdoor only.

Run:  .venv/bin/python tests/browser/vocab_drill_check.py
"""

from __future__ import annotations

import json
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

PAGE_DIR = Path(__file__).resolve().parents[2] / "vocabbench"
PORT = 8398
BASE = f"http://localhost:{PORT}/"


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def solve_question(page) -> tuple[str, str, str]:
    """Answer the current question correctly; returns (type, prompt, key)."""
    key = page.evaluate("() => window.__answerKey")
    prompt = page.text_content(".prompt").strip()

    if page.locator('input[name="answer"]').count():
        qtype = "mc"
        page.locator(f'input[name="answer"][value="{key}"]').check()
    elif page.locator(".match-word").count():
        qtype = "match"
        for word, definition in key.items():
            page.locator(f'.match-word[data-w="{word}"]').click()
            page.locator(f'.match-def[data-d="{definition}"]').click()
    else:
        qtype = "fill/spell"
        page.fill("#answer", key)

    page.click("#next-btn")
    return qtype, prompt, json.dumps(key, sort_keys=True)


def run_drill(page, seed: int, n: int, **params) -> tuple[list, dict]:
    query = "&".join([f"seed={seed}", f"n={n}"] + [f"{k}={v}" for k, v in params.items()])
    page.goto(f"{BASE}?{query}")
    page.click("#start-btn")
    seq = [solve_question(page) for _ in range(n)]
    payload = json.loads(page.text_content("#json"))
    return seq, payload


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), partial(_Quiet, directory=str(PAGE_DIR)))
    threading.Thread(target=server.serve_forever, daemon=True).start()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # 1. determinism
        seq_a, _ = run_drill(page, seed=7, n=8)
        seq_b, _ = run_drill(page, seed=7, n=8)
        seq_c, _ = run_drill(page, seed=8, n=8)
        assert seq_a == seq_b, "same seed produced different sequences"
        assert seq_a != seq_c, "different seeds produced the same sequence"
        print("ok  determinism: seed 7 == seed 7, seed 7 != seed 8")

        # 2. full solve, every type twice
        seq, payload = run_drill(page, seed=42, n=8)
        types = [t for t, _, _ in seq]
        assert types == ["mc", "fill/spell", "match", "fill/spell"] * 2, types
        assert payload["score"] == 8 and not payload["timedOut"], payload
        assert all(a["correct"] for a in payload["answers"]), payload["answers"]
        print(f"ok  full solve: 8/8 correct, {payload['elapsedMs']}ms, seed {payload['seed']}")

        # 3. idle detector
        page.goto(f"{BASE}?seed=1&n=1&idle=1&time=60")
        page.click("#start-btn")
        page.wait_for_selector("#overlay", timeout=5_000)
        page.click("#resume-btn")
        solve_question(page)
        payload = json.loads(page.text_content("#json"))
        assert payload["idleEvents"] >= 1, payload
        print(f"ok  idle detector: overlay shown, resumed, {payload['idleEvents']} event(s) recorded")

        # 4. session timer
        page.goto(f"{BASE}?seed=1&n=8&time=5&idle=60")
        page.click("#start-btn")
        solve_question(page)  # answer one, then let the clock run out
        page.wait_for_selector("#json", timeout=8_000)
        payload = json.loads(page.text_content("#json"))
        assert payload["timedOut"] and payload["n"] == 8, payload
        assert len(payload["answers"]) == 1, payload["answers"]
        print("ok  timer: expiry forced results screen with timedOut=true")

        browser.close()

    server.shutdown()
    print("all vocab drill checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
