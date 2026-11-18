"""Focused speed-chip browser checks against an ephemeral local mock only."""
from playwright.sync_api import sync_playwright
from tests.face import hud_v2_check as H
from tests.face.hud_v2_mock import MockDaemon


def main():
    mock = MockDaemon(0).start()
    H.BASE = f"http://127.0.0.1:{mock.port}"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context()
            H.guard(context)
            H.guard_loopback(context)
            context.add_init_script(H.FAKE_RECOGNIZER)
            page = context.new_page()
            H.boot(page, mock)
            H.speed_chip_checks(page, mock)
            browser.close()
    finally:
        mock.stop()
    for failure in H.FAILURES:
        print("FAIL:", failure)
    print(f"Speed browser checks: {H.CHECKS - len(H.FAILURES)}/{H.CHECKS} passed")
    return int(bool(H.FAILURES))


if __name__ == "__main__":
    raise SystemExit(main())
