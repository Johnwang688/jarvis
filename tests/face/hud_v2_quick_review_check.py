"""The Diff tab's Quick review (2026-10-15, docs/plans/2026-10-15-diff-quick-review.md):
the button in the tab's header, the running state, the findings panel and a
finding opening its file:line — in a real browser against the isolated mock
daemon (never a live port).

Free. Run on its own with `PYTHONPATH=. python tests/face/hud_v2_quick_review_check.py`;
set QUICK_REVIEW_SHOTS=<dir> to save screenshots of each state.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.face.hud_v2_check import expand
from tests.face.hud_v2_layout_check import _boot

BUTTON = '.toolbar [data-testid="quick-review"]'
PANEL = '[data-testid="qreview"]'

FINDINGS = [
    {"severity": "bug", "file": "calc.py", "line": 6, "why": "<img src=x onerror=alert(1)> multiply ignores b's sign",
     "in_diff": True},
    {"severity": "risk", "file": "test_calc.py", "line": 1, "why": "no test for multiply(0, x)", "in_diff": True},
    {"severity": "nit", "file": "README.md", "line": None, "why": "**docs** not updated", "in_diff": False},
]


def _shot(page, name):
    where = os.environ.get("QUICK_REVIEW_SHOTS")
    if where:
        Path(where).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(where) / f"{name}.png"))


def _done(mock, **over):
    review = mock.world["diff_review"]["review"]
    review.update(state="done", trying=None, harness="codex", model="gpt-6-luna",
                  label="Codex · GPT-6 Luna", summary="Adds multiply and a test; one sign bug.",
                  findings=FINDINGS, cost={"usd": 0.0, "tokens": 2400},
                  fallbacks=[{"harness": "claude", "name": "Claude", "reason": "5h at 82%", "tried": False}])
    review.update(over)


def quick_review_checks(browser, mock, base, check, until, guard, init_script):
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    guard(ctx)
    ctx.add_init_script(init_script)
    page = ctx.new_page()
    page.set_default_timeout(6000)
    errors, dialogs = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    try:
        _boot(page, mock, base, until)
        expand(page, "p1", "task-k1")
        page.locator('[data-testid="task-k1"]').click()
        page.locator('[data-testid="tab-diff"]').first.click()
        until(lambda: page.locator('[data-testid="diff-calc.py"]').count() > 0)
        button = page.locator(BUTTON).first
        check("one Quick review button, in the Diff tab's header", page.locator(BUTTON).count() == 1)
        check("its label", button.inner_text().strip() == "Quick review", button.inner_text())
        title = button.get_attribute("title") or ""
        check("its hover help says read-only and not a full review",
              "read-only" in title and "not a full review" in title, title)
        radius = button.evaluate("e => getComputedStyle(e).borderTopLeftRadius")
        check("a 4px rectangle (PR #210), not a pill", radius == "4px", radius)
        check("no panel before any review", page.locator(PANEL).count() == 0)
        page.locator('[data-testid="diff-calc.py"]').click()
        until(lambda: page.locator('[data-testid="diff-editor"] .monaco-diff-editor').count() > 0, timeout=15)
        _shot(page, "1-idle")

        button.click()
        until(lambda: button.get_attribute("data-state") == "running")
        check("a click starts one review (POST, an empty body)",
              any(r[0] == "POST" and r[1].endswith("/diff/review") and r[2] == {} for r in mock.calls),
              str(mock.calls[-3:]))
        check("running: a spinner, the harness and Cancel",
              button.locator(".vexspinner").count() == 1
              and page.locator('[data-testid="quick-review-harness"]').inner_text() == "Claude"
              and "Cancel" in button.inner_text(), button.inner_text())
        check("running: the panel says who is reviewing",
              "Reviewing with Claude" in page.locator('[data-testid="qr-header"]').inner_text())
        _shot(page, "2-running")

        mock.world["diff_review"]["review"].update(
            trying="codex", fallbacks=[{"harness": "claude", "name": "Claude", "reason": "5h at 82%", "tried": False}])
        until(lambda: page.locator('[data-testid="quick-review-harness"]').inner_text() == "Codex")
        _done(mock)
        until(lambda: page.locator(PANEL).get_attribute("data-state") == "done", timeout=8)
        header = page.locator('[data-testid="qr-header"]').inner_text()
        check("the header names the fallback and the harness", header == "Claude 5h at 82% → Codex · GPT-6 Luna", header)
        rows = page.locator('[data-testid="qr-finding"]')
        check("three findings, most serious first", rows.count() == 3
              and [rows.nth(i).locator(".qr-sev").inner_text().lower() for i in range(3)] == ["bug", "risk", "nit"])
        check("findings are plain text: no element from a finding's words",
              page.locator(PANEL + " img").count() == 0 and page.locator(PANEL + " strong").count() == 0
              and page.locator(PANEL + " a").count() == 0
              and "<img src=x onerror=alert(1)>" in rows.nth(0).inner_text()
              and "**docs**" in rows.nth(2).inner_text())
        check("a finding outside the diff is not clickable", rows.nth(2).evaluate("e => e.tagName") == "DIV")
        severity = rows.nth(0).locator(".qr-sev").evaluate("e => getComputedStyle(e).color")
        check("a bug reads red (Graphite's --red)", severity == "rgb(248, 113, 113)", severity)
        check("the button is idle again", button.inner_text().strip() == "Quick review")
        _shot(page, "3-findings")

        rows.nth(1).click()
        until(lambda: "test_calc.py" in page.locator(".toolbar .path").first.inner_text())
        check("a finding opens its file in the diff",
              page.locator(".toolbar .path").first.inner_text() == "test_calc.py")
        rows.nth(0).click()
        until(lambda: page.locator(".toolbar .path").first.inner_text() == "calc.py")
        active = '[data-testid="diff-editor"] .editor.modified .line-numbers.active-line-number'
        line = until(lambda: page.locator(active).count() > 0 and page.locator(active).first.inner_text(), timeout=6)
        check("and puts the new side's cursor on its line", line == "6", str(line))
        _shot(page, "4-opened")

        mock.world["diff_review"] = {"review": None, "outdated": True}
        page.locator('[data-testid="tab-file"]').first.click()
        page.locator('[data-testid="tab-diff"]').first.click()
        until(lambda: page.locator(PANEL).count() > 0)
        check("an outdated review is not shown; the panel says the diff changed",
              page.locator(PANEL).inner_text().strip() == "The diff changed since the last quick review.")

        button = page.locator(BUTTON).first
        button.click()
        until(lambda: button.get_attribute("data-state") == "running")
        button.click()
        until(lambda: button.get_attribute("data-state") == "idle")
        check("pressing again while running cancels",
              any(r[0] == "POST" and r[1].endswith("/diff/review/cancel") for r in mock.calls)
              and page.locator(PANEL).get_attribute("data-state") == "cancelled")
        check("no dialog, no page error", not dialogs and not errors, "; ".join(errors[:3] + dialogs[:3]))
    finally:
        ctx.close()


def main():
    from playwright.sync_api import sync_playwright

    from tests.face import hud_v2_check as H
    from tests.face.hud_v2_mock import DIST, MockDaemon

    if not (DIST / "index.html").exists():
        print("hud/dist is not built. Run: cd hud && npm ci --ignore-scripts && npm run build")
        return 2
    mock = MockDaemon(0).start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            quick_review_checks(browser, mock, f"http://127.0.0.1:{mock.port}", H.check, H.until,
                                H.guard, H.PLAIN_RECOGNIZER)
            browser.close()
    finally:
        mock.stop()
    print()
    if H.FAILURES:
        print(f"{len(H.FAILURES)} of {H.CHECKS} checks FAILED:")
        for f in H.FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {H.CHECKS} quick review checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
