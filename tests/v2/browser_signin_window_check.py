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
        self.assertNotIn("DISPLAY", env, "the window must not reach X11")
        run = Path(env["XDG_RUNTIME_DIR"])
        self.assertEqual(run, root / "browser" / "home" / "run" / profile)
        for folder in (run, run.parent):
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
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

    async def test_a_cancelled_launch_closes_its_chrome(self):
        """The caller's timeout cancels `_launch` mid-check: the Chrome it
        started must not be left running untracked (review MED 3)."""
        temp_config(self)
        host = H.BrowserHost()
        host._gate = SimpleNamespace(port=19000)
        context = SimpleNamespace(browser=SimpleNamespace(version="synthetic"), on=Mock(),
                                  close=AsyncMock())
        host._pw = SimpleNamespace(chromium=SimpleNamespace(
            launch_persistent_context=AsyncMock(return_value=context)))
        profile = host.list_profiles()[0]["id"]
        started = asyncio.Event()

        async def hang(self_, _context):
            started.set()
            await asyncio.sleep(60)
        with patch.object(H.BrowserHost, "_check_gate", hang), \
                patch.object(H, "stop_pids", return_value=0) as stopped:
            task = asyncio.ensure_future(host._launch(profile, display="/x/wayland-0"))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        context.close.assert_awaited()
        self.assertTrue(stopped.called)


def fake_window(profile):
    return H._Instance(profile=profile, folder=Path("/nonexistent/" + profile),
                       context=SimpleNamespace(close=AsyncMock()), browser=None)


class WindowLifecycle(unittest.IsolatedAsyncioTestCase):
    """Every way a window ends, with a fake Chrome (review LOW 8)."""

    def setUp(self):
        temp_config(self)
        self.host = H.BrowserHost()
        self.profile = self.host.list_profiles()[0]["id"]
        guard = patch.object(H, "stop_pids", return_value=0)
        self.stopped = guard.start()
        self.addCleanup(guard.stop)

    def put(self):
        window = fake_window(self.profile)
        self.host._windows[self.profile] = window
        return window

    def assert_ended(self, window):
        self.assertNotIn(self.profile, self.host._windows)
        window.context.close.assert_awaited()
        self.assertTrue(self.stopped.called)
        self.assertEqual(self.host.status()["signin_windows"], [])

    async def test_delete_ends_it(self):
        window = self.put()
        window.idle = asyncio.ensure_future(asyncio.sleep(60))
        await self.host._close_window_locked(self.profile)
        self.assert_ended(window)
        await asyncio.sleep(0)
        self.assertTrue(window.idle is None)

    async def test_a_stale_close_event_ends_nothing(self):
        window = self.put()
        self.host._window_gone(self.profile, fake_window(self.profile))
        await asyncio.sleep(0.05)
        self.assertIs(self.host._windows[self.profile], window)
        self.host._window_gone(self.profile, window)
        await asyncio.sleep(0.05)
        self.assert_ended(window)

    async def test_expiry_ends_it_without_cancelling_itself(self):
        window = self.put()
        with patch.object(H, "SIGNIN_WINDOW_S", 0.01):
            window.idle = asyncio.ensure_future(self.host._window_expiry(self.profile, window))
            await window.idle
        self.assert_ended(window)

    async def test_forget_and_delete_end_it_before_the_wipe(self):
        for delete in (False, True):
            with self.subTest(delete=delete):
                if delete:
                    self.profile = self.host.list_profiles()[0]["id"]
                window = self.put()
                order = []
                window.context.close = AsyncMock(side_effect=lambda: order.append("closed"))
                with patch.object(H.BrowserHost, "_wipe_profile", lambda s, p: order.append("wiped")):
                    await self.host._manage_profile_async(self.profile, delete)
                self.assertEqual(order, ["closed", "wiped"])
                self.assertNotIn(self.profile, self.host._windows)

    async def test_shutdown_ends_it(self):
        window = self.put()
        await self.host._shutdown()
        self.assert_ended(window)

    async def test_the_cap_refuses_before_any_tab_ends(self):
        for n in range(3):
            self.host._instances[f"p-0000000{n}"] = SimpleNamespace()
        with patch.object(H.BrowserHost, "_close_instance", AsyncMock()) as closed:
            with self.assertRaises(H.BrowserRefused):
                await self.host._open_window(self.profile, "/x/wayland-0")
        closed.assert_not_called()

    async def test_a_window_closed_before_it_was_ready_is_refused(self):
        window = fake_window(self.profile)
        window.ended = True
        with patch.object(H.BrowserHost, "_launch", AsyncMock(return_value=window)):
            with self.assertRaises(H.BrowserRefused) as caught:
                await self.host._open_window(self.profile, "/x/wayland-0")
        self.assertEqual(str(caught.exception), H.WINDOW_ENDED)
        self.assert_ended(window)


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
