"""B-LOGIN-W (docs/plans/2026-10-24-browser-signin-window.md): the owner's
sign-in window and the gate check every named-profile launch now passes.

Fakes for the guards and the launch's shape; a real gate for the check's
record; real Chrome (temporary profile, a gate that resolves nothing, no
network) for the check passing and a gate bypass refused. The headed window
itself opens on the owner's desktop, so it runs only with
JARVIS_TEST_HEADED=1 (and WSLg). No real profile, account or live port.
"""
import asyncio
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from jarvis import config
from jarvis.v2.browser import gate as G, host as H
from tests.v2.browser_gate_check import code
from tests.v2.owner_browser_check import deny_every_name, loopback_only


def temp_config(case):
    tmp = tempfile.TemporaryDirectory(prefix="signin-window-")
    case.addCleanup(tmp.cleanup)
    root = Path(tmp.name)
    for name, value in dict(BROWSER_DIR=root / "browser",
                            BROWSER_SETTINGS_PATH=root / "browser.json").items():
        guard = patch.object(config, name, value)
        guard.start()
        case.addCleanup(guard.stop)
    return root


class Args(unittest.TestCase):
    def test_headed_is_the_same_launch_on_wayland_at_the_signin_page(self):
        base = H.launch_args(Path("/tmp/x"), 1, private=False)
        headed = H.launch_args(Path("/tmp/x"), 1, private=False, headed=True)
        self.assertNotIn("--headless", headed)
        self.assertIn("--ozone-platform=wayland", headed)
        self.assertEqual(headed[-1], H.SIGNIN_URL)
        self.assertEqual(base[-1], "about:blank")
        # Every other switch is kept: the gate, no DNS, extensions off, muted.
        self.assertEqual([a for a in base if a not in ("--headless", "about:blank")],
                         [a for a in headed if a not in ("--ozone-platform=wayland", H.SIGNIN_URL)])
        with self.assertRaises(ValueError):
            H.launch_args(Path("/tmp/x"), 1, private=True, headed=True)

    def test_display_must_be_a_socket_of_ours(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "wayland-0"
            with patch.object(H, "WSLG_WAYLAND", path):
                self.assertIsNone(H.wslg_display())
                path.write_text("")
                self.assertIsNone(H.wslg_display(), "a plain file is not a display")
                path.unlink()
                import socket
                s = socket.socket(socket.AF_UNIX)
                s.bind(str(path))
                self.addCleanup(s.close)
                self.assertEqual(H.wslg_display(), str(path))
                with patch.object(H.os, "getuid", return_value=os.getuid() + 1):
                    self.assertIsNone(H.wslg_display(), "someone else's socket")


class Guards(unittest.TestCase):
    def setUp(self):
        temp_config(self)

    def host(self, held=False):
        daemon = SimpleNamespace(approvals=SimpleNamespace(pending=lambda: [1] if held else []))
        return H.BrowserHost(daemon, resolver=deny_every_name, connector=loopback_only)

    def refused(self, call):
        with self.assertRaises(H.BrowserRefused) as caught:
            call()
        return caught.exception

    def test_held_private_unknown_and_no_display_refuse_before_starting(self):
        host = self.host()
        main = host.list_profiles()[0]["id"]
        self.assertEqual(self.refused(lambda: self.host(held=True).signin_window(main)).status, 409)
        self.refused(lambda: host.signin_window(H.PRIVATE))
        self.refused(lambda: host.signin_window("p-00000000"))
        with patch.object(H, "wslg_display", return_value=None):
            refusal = self.refused(lambda: host.signin_window(main))
        self.assertEqual((refusal.status, str(refusal)), (409, H.NO_DISPLAY))
        self.assertFalse(host.status()["running"], "a refusal started the Browser")
        self.assertEqual(host.status()["signin_windows"], [])


class HeadedLaunch(unittest.IsolatedAsyncioTestCase):
    async def test_the_window_launch_shape(self):
        root = temp_config(self)
        host = H.BrowserHost()
        host._gate = SimpleNamespace(port=19000)
        context = SimpleNamespace(browser=SimpleNamespace(version="synthetic"), on=Mock())
        launch = AsyncMock(return_value=context)
        host._pw = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
        profile = host.list_profiles()[0]["id"]
        with patch.object(H.BrowserHost, "_check_gate", AsyncMock(return_value=True)) as check, \
                patch.object(H, "stop_pids", return_value=0), \
                patch.object(H, "font_environment", return_value={"HOME": "h", "PULSE_SERVER": "ambient",
                                                                  "DISPLAY": "none"}):
            await host._launch(profile, display="/x/wayland-0")
        kwargs = launch.call_args.kwargs
        self.assertFalse(kwargs["headless"])
        self.assertFalse(kwargs["accept_downloads"])
        self.assertNotIn("downloads_path", kwargs)
        self.assertTrue(kwargs["no_viewport"])
        self.assertTrue(kwargs["chromium_sandbox"])
        self.assertIn("--mute-audio", kwargs["args"])
        env = kwargs["env"]
        self.assertEqual(env["WAYLAND_DISPLAY"], "/x/wayland-0")
        self.assertNotIn("PULSE_SERVER", env)
        run = Path(env["XDG_RUNTIME_DIR"])
        self.assertEqual(run, root / "browser" / "home" / "run")
        self.assertEqual(stat.S_IMODE(run.stat().st_mode), 0o700)
        check.assert_awaited_once()
        # No page of the window is ever adopted: only Chrome's close is heard.
        self.assertEqual([c.args[0] for c in context.on.call_args_list], ["close"])

    async def test_a_profile_in_a_window_is_refused_to_the_remote_browser_and_counted(self):
        temp_config(self)
        host = H.BrowserHost()
        profile = host.list_profiles()[0]["id"]
        host._windows[profile] = SimpleNamespace()
        with self.assertRaises(H.BrowserRefused) as caught:
            await host._instance(profile)
        self.assertEqual((caught.exception.status, str(caught.exception)), (409, H.IN_WINDOW))
        self.assertEqual(host._profiles_open(), 1)

    async def test_a_named_launch_that_bypasses_the_gate_is_closed_and_refused(self):
        temp_config(self)
        host = H.BrowserHost()
        host._gate = SimpleNamespace(port=19000)
        context = SimpleNamespace(browser=SimpleNamespace(version="synthetic"), on=Mock(),
                                  close=AsyncMock())
        host._pw = SimpleNamespace(chromium=SimpleNamespace(
            launch_persistent_context=AsyncMock(return_value=context)))
        profile = host.list_profiles()[0]["id"]
        with patch.object(H.BrowserHost, "_check_gate", AsyncMock(return_value=False)), \
                patch.object(H, "stop_pids", return_value=0) as stopped:
            with self.assertRaises(H.BrowserRefused) as caught:
                await host._launch(profile)
        self.assertEqual(str(caught.exception), H.GATE_BYPASSED)
        context.close.assert_awaited()
        self.assertTrue(stopped.called)
        context.on.assert_not_called()


class GateRecord(unittest.TestCase):
    def test_only_an_armed_name_is_recorded_and_it_never_leaves(self):
        resolved = []

        def resolve(name, port):
            resolved.append(name)
            return deny_every_name(name, port)
        gate = G.Gate(resolver=resolve)
        port = gate.start()
        self.addCleanup(gate.stop)
        armed = "gate-check-0123456789abcdef.jarvis.invalid"
        seen = gate.expect_check(armed)
        self.assertNotIn(code(port, "other.jarvis.invalid", 80), (G.REP_OK, G.REP_NOT_ALLOWED))
        self.assertFalse(seen.is_set())
        self.assertEqual(code(port, armed.upper(), 80), G.REP_NOT_ALLOWED)
        self.assertTrue(seen.is_set())
        self.assertNotIn(armed.upper(), resolved, "the armed name was looked up")
        gate.forget_check(armed)
        again = code(port, armed, 80)
        self.assertIn(armed, resolved, "a forgotten check is judged like any name")
        self.assertNotEqual(again, G.REP_OK)


class RealChrome(unittest.TestCase):
    """A named profile through the real gate: the check passes, and a Chrome
    sent around the gate (`--no-proxy-server`, as a policy could) is refused."""

    def setUp(self):
        if not H.CHROME_PATH.exists():
            self.skipTest("Google Chrome is not installed")
        temp_config(self)
        self.host = H.BrowserHost(None, resolver=deny_every_name, connector=loopback_only)
        self.addCleanup(self.host.stop)
        self.main = self.host.list_profiles()[0]["id"]

    def test_the_check_passes_through_the_gate(self):
        tab = self.host.open_tab(self.main)
        self.assertTrue(tab["tab_id"])
        self.assertEqual(len(self.host.tabs()), 1, "the check's page was adopted as a tab")
        self.assertEqual([i["profile"] for i in self.host.status()["instances"]], [self.main])

    def test_a_chrome_around_the_gate_is_refused(self):
        real = H.launch_args

        def around(*a, **k):
            return [("--no-proxy-server" if x.startswith("--proxy-server=") else x)
                    for x in real(*a, **k) if not x.startswith("--proxy-bypass-list")]
        with patch.object(H, "launch_args", around):
            with self.assertRaises(H.BrowserRefused) as caught:
                self.host.open_tab(self.main)
        self.assertEqual(str(caught.exception), H.GATE_BYPASSED)
        self.assertEqual(self.host.status()["instances"], [])
        self.assertEqual(H.chrome_pids(config.BROWSER_DIR / "profiles" / self.main), [])


@unittest.skipUnless(os.environ.get("JARVIS_TEST_HEADED") == "1" and H.wslg_display(),
                     "opens a window on the desktop: set JARVIS_TEST_HEADED=1 (WSLg)")
class RealWindow(unittest.TestCase):
    def setUp(self):
        temp_config(self)
        self.host = H.BrowserHost(None, resolver=deny_every_name, connector=loopback_only)
        self.addCleanup(self.host.stop)
        self.main = self.host.list_profiles()[0]["id"]

    def wait_closed(self):
        deadline = time.monotonic() + 15
        while self.host.status()["signin_windows"] and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(self.host.status()["signin_windows"], [])

    def test_window_holds_the_profile_until_it_closes(self):
        self.host.open_tab(self.main)
        self.assertEqual(self.host.signin_window(self.main), {"profile": self.main, "signin_window": True})
        self.assertEqual(self.host.tabs(), [], "the profile's Remote tabs did not end")
        self.assertEqual(self.host.status()["signin_windows"], [self.main])
        with self.assertRaises(H.BrowserRefused) as caught:
            self.host.open_tab(self.main)
        self.assertEqual(str(caught.exception), H.IN_WINDOW)
        with self.assertRaises(H.BrowserRefused):
            self.host.signin_window(self.main)
        self.host.close_signin_window(self.main)
        self.assertEqual(self.host.status()["signin_windows"], [])
        self.assertTrue(self.host.open_tab(self.main)["tab_id"])

    def test_closing_the_last_window_ends_it(self):
        self.host.signin_window(self.main)
        window = self.host._windows[self.main]

        async def close_pages():
            for page in list(window.context.pages):
                await page.close()
        self.host._call(close_pages())
        self.wait_closed()
        self.assertEqual(H.chrome_pids(config.BROWSER_DIR / "profiles" / self.main), [])

    def test_expiry_closes_it(self):
        with patch.object(H, "SIGNIN_WINDOW_S", 1.0):
            self.host.signin_window(self.main)
            self.wait_closed()


if __name__ == "__main__":
    unittest.main()
