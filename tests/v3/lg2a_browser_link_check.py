"""Exercise the private browser link with Linux pipes and fake processes."""

from concurrent.futures import ThreadPoolExecutor
import ctypes
import json
import os
from pathlib import Path, PureWindowsPath
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from jarvis import config
from jarvis.v3.browser.link import BrowserLink, LinkError, helper_command, windows_link
from jarvis.v3.voice import installation
from windows import v3browser_helper as helper


FAKE_BROWSER = r'''
import json
import os
from pathlib import Path
import sys

source = os.fdopen(3, "rb", buffering=0)
target = os.fdopen(4, "wb")
profile = Path(next(arg.split("=", 1)[1] for arg in sys.argv[1:]
                    if arg.startswith("--user-data-dir=")))
deferred = []

def send(message):
    target.write(json.dumps(message).encode() + b"\x00")
    target.flush()

def reply(request, result):
    send({"id": request["id"], "result": result})

buffer = b""
while chunk := source.read(65536):
    buffer += chunk
    while b"\x00" in buffer:
        frame, buffer = buffer.split(b"\x00", 1)
        (profile / "received").touch()
        request = json.loads(frame)
        method = request["method"]
        if method == "Test.exit":
            sys.exit(0)
        if method == "Browser.close":
            (profile / "close-requested").touch()
            if (profile / "save-after-close").exists():
                # Like a real browser: the pipe closes first, the profile is saved after.
                source.close()
                target.close()
                import time
                time.sleep(0.6)
                (profile / "saved").touch()
                sys.exit(0)
            if not (profile / "ignore-close").exists():
                (profile / "exited-by-request").touch()
                sys.exit(0)
        if method == "Target.createBrowserContext":
            reply(request, {"params": request.get("params")})
        if method == "Test.echo":
            reply(request, {"params": request.get("params"), "session": request.get("sessionId")})
        if method == "Test.args":
            reply(request, {"args": sys.argv[1:]})
        if method == "Test.event":
            send({"method": "Test.event", "params": {"value": "café"}})
            reply(request, {})
        if method == "Test.defer":
            deferred.append(request)
            send({"method": "Test.deferred", "params": request.get("params", {})})
        if method == "Test.release":
            for item in reversed(deferred):
                reply(item, item.get("params", {}))
            deferred.clear()
            reply(request, {})
        if method == "Test.error":
            send({"id": request["id"], "error": {"message": "fake-detail-" * 30}})
        if method == "Test.large":
            for _ in range(64):
                target.write(b"x" * (1024 * 1024))
            target.write(b"x\x00")
            target.flush()
        if method == "Test.unicode":
            target.write('{"id":1,"result":{"value":"café"}}\x00'.encode())
            target.flush()
'''


class FakeBrowserTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.profile = self.folder / "example profile"
        fake = self.folder / "fake-browser"
        fake.write_text(f"#!{sys.executable}\n" + FAKE_BROWSER, encoding="utf-8")
        fake.chmod(0o700)
        self.fake = fake

    def args(self, browser="chrome", *, window=False):
        args = ["--browser", browser, "--profile-dir", str(self.profile), "--proxy-port", "12345"]
        return [*args, "--window"] if window else args

    def command(self, args, *, missing=False, launch_failure=False, unbuffered=False):
        if launch_failure:
            body = "def launch(self, executable, args):\n        raise OSError('fake private detail')"
        else:
            body = "pass"
        candidate = self.folder / "missing" if missing else self.fake
        source = f"""from pathlib import Path
from windows.v3browser_helper import main, PosixAdapter
class FakeAdapter(PosixAdapter):
    def candidates(self, browser):
        return [Path({str(candidate)!r})]
    {body}
raise SystemExit(main(adapter=FakeAdapter()))
"""
        return [sys.executable, *(["-u"] if unbuffered else []), "-c", source, *args]

    @staticmethod
    def read(process):
        if not select.select([process.stdout], [], [], 5)[0]:
            raise AssertionError("No helper output")
        return process.stdout.readline()

    @staticmethod
    def send(process, message):
        process.stdin.write(json.dumps(message).encode() + b"\n")
        process.stdin.flush()

    def start_helper(self, args=None):
        process = subprocess.Popen(self.command(self.args() if args is None else args),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, bufsize=0)
        self.addCleanup(self.clean_process, process)
        ready = json.loads(self.read(process))
        self.assertEqual(ready["helper"], "ready")
        self.assertGreater(ready["pid"], 0)
        return process, ready

    @staticmethod
    def clean_process(process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()

    def assert_gone(self, pid):
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def start_link(self, command=None, **kwargs):
        link = BrowserLink(command if command is not None else self.command(self.args()), **kwargs)
        self.addCleanup(link.close)
        link.start()
        return link


class HelperTest(FakeBrowserTest):
    def test_unbuffered_helper_relays_requests(self):
        link = self.start_link(self.command(self.args(), unbuffered=True))
        self.assertEqual(link.call("Test.echo", {"value": "café"}, timeout=3),
                         {"params": {"value": "café"}, "session": None})

    def test_ready_round_trip_event_and_stdin_eof(self):
        process, ready = self.start_helper()
        self.assertEqual(ready, {"helper": "ready", "browser": "chrome", "pid": ready["pid"]})
        self.assertTrue(self.profile.is_dir())
        self.send(process, {"id": 7, "method": "Test.echo", "params": {"x": "café"}})
        self.assertEqual(json.loads(self.read(process)),
                         {"id": 7, "result": {"params": {"x": "café"}, "session": None}})
        self.send(process, {"id": 8, "method": "Test.event"})
        self.assertEqual(json.loads(self.read(process)),
                         {"method": "Test.event", "params": {"value": "café"}})
        self.assertEqual(json.loads(self.read(process)), {"id": 8, "result": {}})
        self.send(process, {"id": 1, "method": "Test.unicode"})
        self.assertEqual(self.read(process), '{"id":1,"result":{"value":"café"}}\n'.encode())
        process.stdin.close()
        self.assertEqual(process.wait(timeout=5), 0)
        self.assert_gone(ready["pid"])
        self.assertEqual(process.stderr.read(), b"")

    def test_invalid_requests_stop_without_forwarding(self):
        for line in (b"{bad}\n", b"[]\n", b'{"x":NaN}\n', b'{"x":"\xff"}\n',
                     b" " * (16 * 1024 * 1024 + 1) + b"\n"):
            with self.subTest(size=len(line), prefix=line[:12]):
                process, ready = self.start_helper()
                # Raw pipe writes may be short, especially with an oversized line.
                view = memoryview(line)
                while view:
                    view = view[process.stdin.write(view):]
                self.assertEqual(process.wait(timeout=5), 1)
                self.assert_gone(ready["pid"])
                self.assertFalse((self.profile / "received").exists())
                self.assertEqual(process.stdout.read(), b"")
                self.assertEqual(process.stderr.read(), b"")

    def test_browser_that_closes_its_pipe_first_is_given_time_to_save(self):
        # Measured on Windows: killing the browser the moment its pipe closed lost a new cookie.
        self.profile.mkdir(parents=True)
        (self.profile / "save-after-close").touch()
        process, ready = self.start_helper()
        self.send(process, {"id": 1, "method": "Browser.close"})
        self.assertEqual(process.wait(timeout=10), 0)
        self.assertTrue((self.profile / "saved").is_file())
        self.assert_gone(ready["pid"])

    def test_closed_stdin_ends_the_browser_without_the_saving_wait(self):
        process, ready = self.start_helper()
        started = time.monotonic()
        process.stdin.close()
        self.assertEqual(process.wait(timeout=5), 0)
        self.assertLess(time.monotonic() - started, 2)
        self.assert_gone(ready["pid"])

    def test_browser_exit_ends_helper_with_stdin_still_open(self):
        process, ready = self.start_helper()
        self.send(process, {"id": 1, "method": "Test.exit"})
        self.assertEqual(process.wait(timeout=5), 0)
        self.assert_gone(ready["pid"])
        self.assertEqual(process.stderr.read(), b"")

    def test_killing_helper_also_kills_fake_browser(self):
        # Adopt the orphan so the test can reap it and verify its exit status.
        libc = ctypes.CDLL(None)
        previous = ctypes.c_int()
        self.assertEqual(libc.prctl(37, ctypes.byref(previous), 0, 0, 0), 0)
        self.assertEqual(libc.prctl(36, 1, 0, 0, 0), 0)
        try:
            process, ready = self.start_helper()
            self.send(process, {"id": 1, "method": "Test.echo"})
            self.assertEqual(json.loads(self.read(process))["id"], 1)
            pid = ready["pid"]
            process.kill()
            self.assertEqual(process.wait(timeout=5), -signal.SIGKILL)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                reaped, status = os.waitpid(pid, os.WNOHANG)
                if reaped:
                    self.assertEqual(os.waitstatus_to_exitcode(status), -signal.SIGKILL)
                    self.assert_gone(pid)
                    return
                time.sleep(0.01)
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            self.fail("The fake browser outlived its helper")
        finally:
            self.assertEqual(libc.prctl(36, previous.value, 0, 0, 0), 0)

    def test_oversized_browser_frame_ends_helper(self):
        process, ready = self.start_helper()
        self.send(process, {"id": 1, "method": "Test.large"})
        self.assertEqual(self.read(process), b"")
        self.assertEqual(process.wait(timeout=5), 1)
        self.assert_gone(ready["pid"])
        self.assertEqual(process.stderr.read(), b"")

    def test_fixed_startup_failures(self):
        commands = [
            (self.command(self.args(), missing=True), "browser not found"),
            (self.command(self.args(), launch_failure=True), "could not start the browser"),
            (self.command([*self.args(), "--remote-debugging-port=12346"]), "bad arguments"),
            (self.command([*self.args(), "--proxy-port", "65536"]), "bad arguments"),
            (self.command([*self.args(), "--proxy-port", "0"]), "bad arguments"),
            (self.command([*self.args(), "--profile-dir", "relative"]), "bad arguments"),
            (self.command([*self.args(), "--profile-dir", str(self.profile) + '"']), "bad arguments"),
            (self.command([*self.args(), "--browser", "other"]), "bad arguments"),
            (self.command([*self.args(), "https://example.invalid"]), "bad arguments"),
            (self.command([*self.args(), "--win"]), "bad arguments"),
        ]
        for command, reason in commands:
            with self.subTest(reason=reason, argument=command[-1]):
                result = subprocess.run(command, input=b"", capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr, b"")
                self.assertEqual(result.stdout.count(b"\n"), 1)
                self.assertEqual(json.loads(result.stdout), {"helper": "error", "reason": reason})

    def test_full_flag_list_on_both_browsers(self):
        expected = [
            f"--user-data-dir={self.profile}", "--no-first-run", "--no-default-browser-check",
            "--remote-debugging-pipe", "--proxy-server=socks5://127.0.0.1:12345",
            "--proxy-bypass-list=<-loopback>", "--disable-quic",
            "--webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
            "--disable-background-networking", "--disable-component-update",
            "--disable-default-apps", "--no-pings",
            "--disable-features=MediaRouter,DialMediaRouteProvider,CastMediaRouteProvider,"
            "WebRtcHideLocalIpsWithMdns",
        ]
        for browser in ("chrome", "edge"):
            for window in (False, True):
                with self.subTest(browser=browser, window=window):
                    args = self.args(browser, window=window)
                    headless = [] if window else ["--headless=new"]
                    link = self.start_link(self.command(args))
                    self.assertEqual(link.call("Test.args")["args"],
                                     [*expected, *headless, "about:blank"])
                    link.close()
                    pipe_flags = helper.WindowsAdapter().pipe_flags(123, 456)
                    self.assertEqual(helper._browser_args(helper._arguments(args), pipe_flags),
                                     [*expected[:4], "--remote-debugging-io-pipes=123,456",
                                      *expected[4:], *headless, "about:blank"])

    def test_adapter_interface_and_fixed_windows_candidates(self):
        methods = {"candidates", "pipe_flags", "launch", "settle", "stop"}
        for adapter in (helper.WindowsAdapter, helper.PosixAdapter):
            self.assertEqual({name for name in vars(adapter)
                              if not name.startswith("_") and callable(getattr(adapter, name))},
                             methods)
        adapter = helper.WindowsAdapter()
        env = {"ProgramFiles": r"C:\Program Files", "ProgramFiles(x86)": r"C:\Program Files (x86)",
               "LocalAppData": r"C:\Users\user\AppData\Local", "PATH": r"C:\Users\user"}
        self.assertEqual(list(adapter.candidates("chrome", env)), list(map(PureWindowsPath, (
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Users\user\AppData\Local\Google\Chrome\Application\chrome.exe",
        ))))
        self.assertEqual(list(adapter.candidates("edge", env)), list(map(PureWindowsPath, (
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ))))
        self.assertEqual(list(adapter.candidates("chrome", {"ProgramFiles": "relative"})), [])


class LinkTest(FakeBrowserTest):
    def test_start_repeated_start_and_close_reap_both_processes(self):
        link = self.start_link()
        process = link._run.process
        browser_pid = link._run.startup.message["pid"]
        self.assertTrue(link.alive)
        link.start()
        self.assertIs(link._run.process, process)
        with patch.object(process, "kill", wraps=process.kill) as kill:
            link.close()
            kill.assert_not_called()
        self.assertTrue((self.profile / "exited-by-request").is_file())
        with patch.object(link, "_call") as send:
            link.close()
            send.assert_not_called()
        self.assertFalse(link.alive)
        self.assertIsNotNone(process.returncode)
        self.assert_gone(process.pid)
        self.assert_gone(browser_pid)
        with self.assertRaisesRegex(LinkError, r"The browser link is closed\."):
            link.call("Test.echo")

    def test_startup_errors_are_fixed_and_children_reaped(self):
        commands = (
            (self.command(self.args(), missing=True), "The Windows browser was not found."),
            ([sys.executable, "-c", "pass"], "The browser helper did not start."),
            ([sys.executable, "-c", "import sys; sys.stdin.read()"],
             "The browser helper did not answer in time."),
            ([sys.executable, "-c", "print('fake private detail')"],
             "The browser helper did not start."),
        )
        for command, message in commands:
            with self.subTest(message=message):
                link = BrowserLink(command, start_timeout=1)
                self.addCleanup(link.close)
                children = []
                original = subprocess.Popen

                def launch(*args, **kwargs):
                    process = original(*args, **kwargs)
                    children.append(process)
                    return process

                with patch("jarvis.v3.browser.link.subprocess.Popen", side_effect=launch), \
                        self.assertRaises(LinkError) as error:
                    link.start()
                self.assertEqual(str(error.exception), message)
                self.assertFalse(link.alive)
                self.assert_gone(children[0].pid)

    def test_concurrent_requests_receive_out_of_order_replies(self):
        link = self.start_link()
        seen = []
        complete = threading.Event()

        def on_event(message):
            seen.append(message["params"]["value"])
            if len(seen) == 5:
                complete.set()

        link.subscribe(on_event)
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(link.call, "Test.defer", {"value": value}, timeout=5)
                       for value in range(5)]
            self.assertTrue(complete.wait(4))
            self.assertCountEqual(seen, range(5))
            self.assertEqual(link.call("Test.release"), {})
            self.assertEqual([future.result(timeout=2) for future in futures],
                             [{"value": value} for value in range(5)])

    def test_protocol_error_text_is_bounded_and_separate(self):
        link = self.start_link()
        with self.assertRaises(LinkError) as error:
            link.call("Test.error")
        self.assertEqual(str(error.exception), "The browser refused the request.")
        self.assertEqual(error.exception.browser_message, ("fake-detail-" * 30)[:300])
        self.assertNotIn("fake-detail", repr(error.exception))

    def test_late_reply_is_dropped(self):
        link = self.start_link()
        seen = threading.Event()
        link.subscribe(lambda _: seen.set())
        with self.assertRaisesRegex(LinkError, r"The browser did not answer in time\."):
            link.call("Test.defer", {"value": "late"}, timeout=0.2)
        self.assertTrue(seen.wait(3))
        self.assertEqual(link.call("Test.release"), {})
        self.assertEqual(link.call("Test.echo", {"value": "new"}, session="fake-session"),
                         {"params": {"value": "new"}, "session": "fake-session"})

    def test_subscriber_exception_does_not_stop_reader(self):
        link = self.start_link()
        received = []

        def failing(message):
            raise RuntimeError("fake callback failure")

        link.subscribe(failing)
        link.subscribe(lambda message: received.append((message, threading.current_thread())))
        self.assertEqual(link.call("Test.event"), {})
        self.assertEqual(received, [({"method": "Test.event", "params": {"value": "café"}},
                                     link._run.reader)])
        self.assertEqual(link.callback_errors, 1)
        self.assertTrue(link.alive)

    def test_helper_death_releases_waiting_calls(self):
        link = self.start_link()
        seen = []
        waiting = threading.Event()

        def on_event(message):
            seen.append(message)
            if len(seen) == 3:
                waiting.set()

        link.subscribe(on_event)
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(link.call, "Test.defer") for _ in range(3)]
            self.assertTrue(waiting.wait(4))
            # The browser exits, so the real helper closes its writer and exits.
            with self.assertRaisesRegex(LinkError, r"The browser link is closed\."):
                link.call("Test.exit", timeout=3)
            for future in futures:
                with self.assertRaisesRegex(LinkError, r"The browser link is closed\."):
                    future.result(timeout=2)
        self.assertFalse(link.alive)
        with patch.object(link, "_call") as send:
            link.close()
            send.assert_not_called()

    def test_close_stops_a_browser_that_ignores_browser_close(self):
        link = self.start_link(close_request_timeout=0.2, close_exit_timeout=0.2, stop_timeout=1)
        process = link._run.process
        browser_pid = link._run.startup.message["pid"]
        (self.profile / "ignore-close").touch()
        started = time.monotonic()
        link.close()
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue((self.profile / "close-requested").is_file())
        self.assertFalse((self.profile / "exited-by-request").exists())
        self.assertEqual(process.returncode, 0)
        self.assert_gone(process.pid)
        self.assert_gone(browser_pid)

    def test_callback_can_close_its_link_gracefully(self):
        link = self.start_link()
        run = link._run
        link.subscribe(lambda _: link.close())
        try:
            link.call("Test.event", timeout=3)
        except LinkError as error:
            self.assertEqual(str(error), "The browser link is closed.")
        run.reader.join(timeout=4)
        self.assertFalse(run.reader.is_alive())
        self.assertEqual(link.callback_errors, 0)
        self.assertTrue((self.profile / "exited-by-request").is_file())
        self.assert_gone(run.process.pid)

    def test_malformed_frame_stops_and_reaps_helper(self):
        script = (
            'import sys, time\n'
            'print(\'{"helper":"ready","browser":"chrome","pid":1}\', flush=True)\n'
            'sys.stdin.readline()\n'
            'print("{malformed}", flush=True)\n'
            'sys.stdin.read()\n'
            'time.sleep(30)\n'
        )
        for method in ("Test.echo", "Browser.close"):
            with self.subTest(method=method):
                link = self.start_link([sys.executable, "-c", script],
                                       close_request_timeout=1, close_exit_timeout=1,
                                       stop_timeout=0.2)
                run = link._run
                if method == "Browser.close":
                    link.close()
                else:
                    with self.assertRaisesRegex(LinkError, r"The browser link is closed\."):
                        link.call(method, timeout=1)
                run.reader.join(timeout=3)
                self.assertFalse(run.reader.is_alive())
                self.assertFalse(link.alive)
                self.assertTrue(run.process.stdin.closed)
                self.assertEqual(run.process.returncode, -signal.SIGKILL)
                self.assert_gone(run.process.pid)

    def test_close_kills_a_helper_that_ignores_eof(self):
        script = (
            "import sys, time; "
            "print('{\"helper\":\"ready\",\"browser\":\"chrome\",\"pid\":1}', flush=True); "
            "sys.stdin.read(); time.sleep(30)"
        )
        link = self.start_link([sys.executable, "-c", script], close_request_timeout=0.2,
                               close_exit_timeout=0.2, stop_timeout=0.2)
        process = link._run.process
        link.close()
        self.assertLess(process.returncode, 0)
        self.assert_gone(process.pid)
        self.assertFalse(link.alive)

    def test_closed_link_does_not_wait_for_a_stalled_writer(self):
        script = (
            "import time; "
            "print('{\"helper\":\"ready\",\"browser\":\"chrome\",\"pid\":1}', flush=True); "
            "time.sleep(30)"
        )
        link = self.start_link([sys.executable, "-c", script])
        process = link._run.process
        try:
            with self.assertRaisesRegex(LinkError, r"The browser did not answer in time\."):
                link.call("Test.echo", {"value": "x" * (1024 * 1024)}, timeout=0.3)
            self.assertFalse(link.alive)
        finally:
            process.kill()

    def test_request_validation_before_sending(self):
        link = self.start_link()
        for method in ("test.echo", "T.echo", "Test.Echo1", "Test.echo\n", None):
            with self.subTest(method=method), self.assertRaisesRegex(LinkError, "Invalid browser method"):
                link.call(method)
        for params in ([], 1, "x"):
            with self.subTest(params=params), self.assertRaisesRegex(LinkError, "must be a JSON object"):
                link.call("Test.echo", params)
        with self.assertRaisesRegex(LinkError, "Invalid browser request data"):
            link.call("Test.echo", {"value": float("nan")})
        with self.assertRaisesRegex(LinkError, "Invalid browser request data"):
            link.call("Test.echo", {"value": object()})
        with self.assertRaisesRegex(LinkError, "The browser request is too large"):
            link.call("Test.echo", {"value": "x" * (16 * 1024 * 1024)})
        self.assertFalse((self.profile / "received").exists())
        self.assertEqual(link.call("Test.echo", {}), {"params": {}, "session": None})

    def test_proxy_overrides_are_refused_before_sending(self):
        link = self.start_link()
        for key in ("proxyServer", "proxyBypassList"):
            for method in ("Target.createBrowserContext", "Test.echo"):
                with self.subTest(key=key, method=method), self.assertRaises(LinkError) as error:
                    link.call(method, {key: None})
                self.assertEqual(str(error.exception), "The browser request is not allowed.")
        self.assertFalse((self.profile / "received").exists())
        params = {"disposeOnDetach": True}
        self.assertEqual(link.call("Target.createBrowserContext", params), {"params": params})
        nested = {"value": {"proxyServer": "example.invalid"}}
        self.assertEqual(link.call("Test.echo", nested), {"params": nested, "session": None})


class InteropTest(unittest.TestCase):
    def test_launch_environment(self):
        fallback = Path("/example/fallback-interop")
        current = Path("/example/current-interop")
        stale = Path("/example/stale-interop")
        command = ["/example/python.exe"]
        cases = (
            (True, current, {current, fallback}, current),
            (True, None, {fallback}, fallback),
            (True, stale, {fallback}, fallback),
            (True, stale, set(), None),
            (False, stale, set(), None),
        )
        for interop, inherited, sockets, expected in cases:
            with self.subTest(interop=interop, inherited=inherited, sockets=sockets):
                env = {"HOME": "/example/home"}
                if inherited is not None:
                    env["WSL_INTEROP"] = str(inherited)
                with (patch.dict(os.environ, env, clear=True),
                      patch.object(installation, "WSL_INTEROP_SOCKET", fallback),
                      patch.object(Path, "is_socket", autospec=True,
                                   side_effect=sockets.__contains__) as is_socket,
                      patch("jarvis.v3.browser.link.subprocess.Popen", side_effect=OSError) as popen,
                      self.assertRaises(LinkError) as error):
                    BrowserLink(command, interop=interop, cwd="/example/bridge").start()
                if interop and expected is None:
                    self.assertEqual(str(error.exception),
                                     "Windows launch is unavailable. Open WSL and retry.")
                    popen.assert_not_called()
                    continue
                self.assertEqual(str(error.exception), "The browser helper did not start.")
                expected_env = dict(env, WSL_INTEROP=str(expected)) if interop else None
                popen.assert_called_once_with(
                    command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    cwd="/example/bridge", env=expected_env, start_new_session=True)
                if not interop:
                    is_socket.assert_not_called()


class HelperCommandTest(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.bridge = folder / ".example-bridge"
        self.python = self.bridge / "venv" / "Scripts" / "python.exe"
        self.python.parent.mkdir(parents=True)
        self.python.touch()
        self.win_bridge = r"C:\Users\user\.example-bridge"
        self.win_script = r"C:\Users\user\Jarvis\windows\v3browser_helper.py"
        self.enterContext(patch.object(config, "DESKTOP_BRIDGE_DIR", self.win_bridge))
        self.closed = self.enterContext(patch("jarvis.voice_link_setup.windows_folder_closed",
                                              return_value=True))

        def convert(flag, path):
            return {
                ("-u", self.win_bridge): str(self.bridge),
                ("-w", self.bridge): self.win_bridge,
                ("-w", config.REPO_ROOT / "windows" / "v3browser_helper.py"): self.win_script,
            }[flag, path]

        self.enterContext(patch.object(installation, "_convert", side_effect=convert))

    def test_exact_command_and_profile_location(self):
        for browser, window in (("chrome", False), ("edge", True)):
            with self.subTest(browser=browser):
                profile = str(PureWindowsPath(self.win_bridge, "browser", browser))
                command = helper_command(browser, proxy_port=65535, window=window)
                self.assertEqual(command, [str(self.python), self.win_script, "--browser", browser,
                                           "--profile-dir", profile, "--proxy-port", "65535",
                                           *(["--window"] if window else [])])
                self.assertEqual(PureWindowsPath(command[5]).parent.parent,
                                 PureWindowsPath(self.win_bridge))
        self.closed.assert_called_with(self.bridge)
        self.assertFalse((self.bridge / "browser").exists())

    def test_python_missing_bridge_missing_open_or_profile_redirected(self):
        self.python.unlink()
        with self.assertRaisesRegex(LinkError, "bridge Python is missing"):
            helper_command("chrome", proxy_port=1)
        self.python.touch()
        self.closed.return_value = False
        with self.assertRaisesRegex(LinkError, "closed Windows bridge folder is unavailable"):
            helper_command("chrome", proxy_port=1)
        self.closed.return_value = True
        (self.bridge / "browser").symlink_to(self.bridge.parent, target_is_directory=True)
        with self.assertRaisesRegex(LinkError, "profile must be inside"):
            helper_command("chrome", proxy_port=1)
        self.bridge.rename(self.bridge.with_name("moved"))
        with self.assertRaisesRegex(LinkError, "closed Windows bridge folder is unavailable"):
            helper_command("chrome", proxy_port=1)

    def test_windows_link_uses_the_validated_bridge_as_cwd(self):
        command = helper_command("edge", proxy_port=12345, window=True)
        with patch("jarvis.v3.browser.link.BrowserLink") as browser_link:
            link = windows_link("edge", proxy_port=12345, window=True, start_timeout=2)
        browser_link.assert_called_once_with(command, interop=True, cwd=self.bridge, start_timeout=2)
        self.assertIs(link, browser_link.return_value)

    def test_browser_and_port_validation(self):
        with self.assertRaisesRegex(LinkError, "Invalid Windows browser"):
            helper_command("other", proxy_port=1)
        for port in (0, 65536, "1", True):
            with self.subTest(port=port), self.assertRaisesRegex(LinkError, "Invalid browser proxy port"):
                helper_command("chrome", proxy_port=port)


if __name__ == "__main__":
    unittest.main()
