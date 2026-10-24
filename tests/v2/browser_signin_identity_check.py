"""A Remote page must not see a test harness: Google refuses sign-in with
"This browser or app may not be secure" when `navigator.webdriver` is true or
the user agent says `HeadlessChrome` (host.py `launch_args`). Real Chrome on a
temporary profile, `about:blank`, no network, no real profile or account.
"""
import asyncio
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from jarvis.v2.browser import host as H


def shape(private):
    return H.launch_args(Path("/tmp/fixture"), 1234, private=private)


class Args(unittest.TestCase):
    def setUp(self):
        H.chrome_user_agent.cache_clear()
        self.addCleanup(H.chrome_user_agent.cache_clear)

    def test_automation_flag_is_off_for_both_kinds(self):
        for private in (False, True):
            self.assertIn("--disable-blink-features=AutomationControlled", shape(private))

    def test_user_agent_drops_headless_and_keeps_the_major_version(self):
        done = type("R", (), {"stdout": "Google Chrome 155.0.8059.39 \n"})()
        with patch.object(H.subprocess, "run", return_value=done):
            args = shape(False)
        ua = [a for a in args if a.startswith("--user-agent=")]
        self.assertEqual(len(ua), 1)
        self.assertNotIn("Headless", ua[0])
        self.assertIn("Chrome/155.0.0.0", ua[0])
        self.assertEqual(shape(True).count(ua[0]), 1)

    def test_an_unreadable_version_keeps_chromes_own_string(self):
        for out in ("", "no version here"):
            H.chrome_user_agent.cache_clear()
            with patch.object(H.subprocess, "run", return_value=type("R", (), {"stdout": out})()):
                self.assertFalse([a for a in shape(False) if a.startswith("--user-agent=")])
        H.chrome_user_agent.cache_clear()
        with patch.object(H.subprocess, "run", side_effect=OSError):
            self.assertIsNone(H.chrome_user_agent())


@unittest.skipUnless(H.CHROME_PATH.exists(), "Google Chrome is not installed")
class RealChrome(unittest.TestCase):
    def test_page_sees_neither_webdriver_nor_headless(self):
        from playwright.async_api import async_playwright

        async def run():
            folder = tempfile.mkdtemp()
            args = [a for a in H.launch_args(Path(folder), 1, private=False)
                    if not a.startswith(("--proxy-server", "--host-resolver"))]
            async with async_playwright() as pw:
                ctx = await pw.chromium.launch_persistent_context(
                    folder, channel="chrome", headless=True, ignore_default_args=True, args=args)
                try:
                    return await ctx.pages[0].evaluate(
                        "() => [navigator.webdriver, navigator.userAgent]")
                finally:
                    await ctx.close()
        webdriver, ua = asyncio.run(run())
        self.assertIs(webdriver, False)
        self.assertIsNone(re.search("Headless", ua))
        self.assertRegex(ua, r"Chrome/\d+\.0\.0\.0 Safari")


if __name__ == "__main__":
    unittest.main()
