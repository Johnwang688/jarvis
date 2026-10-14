"""Run the existing full driving presentation checks on an isolated fake host."""
import sys
from playwright.sync_api import sync_playwright
from tests.face import hud_v2_check as v2
from tests.face.hud_v2_mock import MockDaemon
from tests.face.hud_v2_drive_check import drive_checks
def main():
    mock=MockDaemon(0).start()
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True,args=["--use-fake-ui-for-media-stream","--use-fake-device-for-media-stream"])
            drive_checks(browser,mock,f"http://127.0.0.1:{mock.port}",v2.check,v2.until,v2.guard,v2.FAKE_RECOGNIZER)
            browser.close()
    finally:mock.stop()
    print(f"{v2.CHECKS} driving HUD checks, {len(v2.FAILURES)} failures")
    return bool(v2.FAILURES)
if __name__=="__main__":sys.exit(main())
