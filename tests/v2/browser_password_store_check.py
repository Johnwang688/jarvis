"""B-LOGIN / M12: the Browser's Chrome password store (password_store.py).

Fakes only: a fake `gdbus` runner, a temporary BROWSER_DIR and settings file,
a temporary "runtime dir" with a socket nobody listens on, and a fake
Playwright. Never the real session bus, never a real keyring, never a secret.
Run in a 3G/0swap systemd scope like the other Browser suites.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from jarvis import config
from jarvis.v2.browser import host as H, password_store as S, profiles as P
from jarvis.v2.browser.audio import LaunchAudio
from jarvis.v2.browser.settings import BrowserSettings

EXISTING = Path(sys.executable)   # stands in for /usr/bin/gdbus: it only has to exist


def reply(state):
    """A fake `gdbus call` result for one state."""
    if state == S.UNLOCKED:
        return subprocess.CompletedProcess([], 0, "(<false>,)\n", "")
    if state == S.LOCKED:
        return subprocess.CompletedProcess([], 0, "(<true>,)\n", "")
    if state == S.UNAVAILABLE:
        return subprocess.CompletedProcess([], 1, "", "Error: GDBus.Error:org.freedesktop.DBus.Error."
                                           "ServiceUnknown: The name org.freedesktop.secrets was not provided\n")
    if state == S.NO_KEYRING:
        return subprocess.CompletedProcess([], 1, "", "Error: GDBus.Error:org.freedesktop.Secret.Error."
                                           "NoSuchObject: The collection does not exist\n")
    return subprocess.CompletedProcess([], 1, "", "Error: GDBus.Error:org.freedesktop.DBus.Error.NoReply: x\n")


class Runner:
    """Answers each probe from a script of states; records every call."""
    def __init__(self, *states):
        self.states = list(states)
        self.calls = []

    def __call__(self, command, **kw):
        self.calls.append((command, kw))
        if not self.states:
            raise AssertionError("an unexpected keyring probe")
        state = self.states.pop(0)
        if isinstance(state, BaseException):
            raise state
        return reply(state)


class Runtime:
    """A private folder with a bound unix socket: the shape of /run/user/<uid>."""
    def __init__(self, case):
        tmp = tempfile.TemporaryDirectory(prefix="keyring-rt-")
        case.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "rt"
        self.dir.mkdir(mode=0o700)
        self.sock = socket.socket(socket.AF_UNIX)
        case.addCleanup(self.sock.close)
        self.sock.bind(str(self.dir / "bus"))
        self.address = f"unix:path={self.dir}/bus"


class Classify(unittest.TestCase):
    def test_states(self):
        for state in (S.UNLOCKED, S.LOCKED, S.UNAVAILABLE, S.NO_KEYRING):
            r = reply(state)
            self.assertEqual(S.classify(r.returncode, r.stdout, r.stderr), state)
        self.assertEqual(S.classify(0, "(<'yes'>,)", ""), S.ERROR)
        self.assertEqual(S.classify(0, "", ""), S.ERROR)
        self.assertEqual(S.classify(1, "", "garbage"), S.ERROR)
        self.assertEqual(S.classify(1, "", "Error: GDBus.Error:org.freedesktop.DBus.Error.Spawn.ChildExited: x"),
                         S.ERROR)
        # Unlocked text on a failing exit is not unlocked.
        self.assertEqual(S.classify(1, "(<false>,)", ""), S.ERROR)


class Probe(unittest.TestCase):
    def test_reads_only_the_locked_property_with_a_minimal_environment(self):
        run = Runner(S.UNLOCKED)
        self.assertEqual(S.probe("unix:path=/x/bus", run=run, gdbus=EXISTING), S.UNLOCKED)
        command, kw = run.calls[0]
        self.assertEqual(command[1:3], ["call", "--session"])
        self.assertEqual(command[-3:], ["org.freedesktop.DBus.Properties.Get",
                                        "org.freedesktop.Secret.Collection", "Locked"])
        self.assertIn("/org/freedesktop/secrets/aliases/default", command)
        text = " ".join(command)
        for never in ("GetSecret", "SearchItems", "Unlock", "OpenSession", "Prompt", "CreateItem"):
            self.assertNotIn(never, text)
        self.assertEqual(kw["env"]["DBUS_SESSION_BUS_ADDRESS"], "unix:path=/x/bus")
        self.assertEqual(set(kw["env"]), {"DBUS_SESSION_BUS_ADDRESS", "PATH", "LANG", "LC_ALL"})
        self.assertIs(kw["stdin"], subprocess.DEVNULL)
        self.assertTrue(kw["timeout"])

    def test_no_bus_no_tool_timeout(self):
        run = Runner()
        self.assertEqual(S.probe(None, run=run, gdbus=EXISTING), S.UNAVAILABLE)
        self.assertEqual(S.probe("unix:path=/x/bus", run=run, gdbus=Path("/nonexistent/gdbus")), S.UNAVAILABLE)
        self.assertEqual(run.calls, [])
        run = Runner(subprocess.TimeoutExpired("gdbus", 5), OSError("boom"))
        self.assertEqual(S.probe("unix:path=/x/bus", run=run, gdbus=EXISTING), S.ERROR)
        self.assertEqual(S.probe("unix:path=/x/bus", run=run, gdbus=EXISTING), S.ERROR)


class Address(unittest.TestCase):
    def test_valid_socket_in_private_runtime(self):
        rt = Runtime(self)
        self.assertEqual(S.bus_address({"DBUS_SESSION_BUS_ADDRESS": rt.address}, runtime=rt.dir), rt.address)
        # Unset: the standard <runtime>/bus.
        self.assertEqual(S.bus_address({}, runtime=rt.dir), rt.address)

    def test_refused_shapes(self):
        rt = Runtime(self)
        other = Path(tempfile.mkdtemp(prefix="keyring-other-")); self.addCleanup(lambda: __import__("shutil").rmtree(other))
        (rt.dir / "file").write_text("")
        (rt.dir / "link").symlink_to(rt.dir / "bus")
        for raw in (rt.address + ",guid=abc", "unix:abstract=/tmp/dbus-x", "tcp:host=127.0.0.1,port=1",
                    f"unix:path={rt.dir}/../rt/bus", f"unix:path={rt.dir}/file", f"unix:path={rt.dir}/link",
                    f"unix:path={rt.dir}/missing", f"unix:path={other}/bus", "unix:path=relative/bus",
                    f"unix:path={rt.dir}/bus;unix:path=/tmp/x", " " + rt.address):
            with self.subTest(raw=raw):
                self.assertIsNone(S.bus_address({"DBUS_SESSION_BUS_ADDRESS": raw}, runtime=rt.dir))

    def test_open_or_foreign_runtime_folder_is_refused(self):
        rt = Runtime(self)
        os.chmod(rt.dir, 0o755)
        self.assertIsNone(S.bus_address({"DBUS_SESSION_BUS_ADDRESS": rt.address}, runtime=rt.dir))
        os.chmod(rt.dir, 0o700)
        self.assertIsNone(S.bus_address({"DBUS_SESSION_BUS_ADDRESS": rt.address}, uid=os.getuid() + 1,
                                        runtime=rt.dir))


class Choose(unittest.TestCase):
    def test_matrix(self):
        bus = "unix:path=/run/user/1/bus"
        for state in (*S.STATES, S.UNKNOWN):
            for requested in (S.REQUEST_KEYRING, S.REQUEST_BASIC):
                for sticky in (False, True):
                    with self.subTest(state=state, requested=requested, sticky=sticky):
                        # Private: always basic, never the bus, never refused.
                        c = S.choose(private=True, requested=requested, sticky=sticky, state=state, bus=bus)
                        self.assertEqual((c.store, c.bus, c.refused), (S.BASIC, None, None))
                        c = S.choose(private=False, requested=requested, sticky=sticky, state=state, bus=bus)
                        if c.refused is None:
                            # Every basic launch is unprotected; only keyring gets the bus.
                            self.assertEqual(c.bus is not None, c.store == S.KEYRING_STORE)
                            self.assertEqual(c.unprotected, c.store == S.BASIC)
                            if sticky:
                                self.assertEqual(c.store, S.KEYRING_STORE, "sticky downgraded")
                        if requested == S.REQUEST_BASIC:
                            self.assertEqual(c.refused, S.STICKY_BASIC_MESSAGE if sticky else None)
                        elif state == S.UNLOCKED:
                            self.assertEqual((c.store, c.bus), (S.KEYRING_STORE, bus))
                        elif state == S.UNAVAILABLE:
                            self.assertEqual(c.refused, S.STICKY_UNAVAILABLE_MESSAGE if sticky else None)
                        else:   # locked, no_keyring, error, unknown: never a silent downgrade
                            self.assertEqual(c.refused, S.UNLOCK_MESSAGE)
        # Unlocked but no bus to hand Chrome: the keyring is not usable.
        c = S.choose(private=False, requested=S.REQUEST_KEYRING, sticky=False, state=S.UNLOCKED, bus=None)
        self.assertEqual((c.store, c.refused), (S.BASIC, None))
        c = S.choose(private=False, requested=S.REQUEST_KEYRING, sticky=True, state=S.UNLOCKED, bus=None)
        self.assertIsNotNone(c.refused)

    def test_messages_name_the_owner_terminal_and_commands(self):
        self.assertIn("Windows Terminal (not a Jarvis terminal)", S.UNLOCK_MESSAGE)
        self.assertIn("gnome-keyring-daemon --unlock", S.UNLOCK_MESSAGE)
        self.assertIn("read -rsp", S.UNLOCK_COMMAND)          # no echo, not in argv
        self.assertIn(S.INSTALL_COMMAND, S.UNPROTECTED_HINT)
        self.assertLessEqual(len(S.UNLOCK_MESSAGE), 300)        # the HUD shows 300 characters


class Detached(unittest.TestCase):
    def test_a_new_keyring_never_reads_the_bus(self):
        run = Runner()
        k = S.Keyring(run=run, gdbus=EXISTING)
        with patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/%d/bus" % os.getuid()}):
            self.assertEqual(k.state(), S.UNAVAILABLE)
            self.assertEqual(k.cached_state(), S.UNAVAILABLE)
            self.assertIsNone(k.bus())
        self.assertEqual(run.calls, [])
        self.assertIsInstance(H.BrowserHost().keyring, S.Keyring)
        self.assertFalse(H.BrowserHost().keyring.attached)

    def test_attached_status_read_is_cached_and_never_blocks(self):
        rt = Runtime(self)
        now = [100.0]
        run = Runner(S.LOCKED, S.UNLOCKED)
        k = S.Keyring(run=run, gdbus=EXISTING, clock=lambda: now[0])
        k.attach({"DBUS_SESSION_BUS_ADDRESS": rt.address}, runtime=rt.dir)
        self.assertEqual(k.bus(), rt.address)
        self.assertEqual(k.state(), S.LOCKED)
        self.assertEqual(k.cached_state(), S.LOCKED)
        self.assertEqual(len(run.calls), 1)
        now[0] += S.STATUS_TTL_S
        self.assertEqual(k.cached_state(), S.LOCKED)       # stale value now, refresh behind
        for _ in range(200):
            if len(run.calls) == 2 and not k._refreshing:
                break
            asyncio.run(asyncio.sleep(0.01))
        self.assertEqual(k.cached_state(), S.UNLOCKED)


class Settings(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="keyring-settings-"); self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "browser.json"

    def test_only_the_exact_word_basic_asks_for_basic(self):
        self.assertEqual(BrowserSettings().password_store(), "keyring")
        for raw, want in (({"password_store": "basic"}, "basic"), ({"password_store": "BASIC"}, "keyring"),
                          ({"password_store": True}, "keyring"), ({}, "keyring"),
                          ({"password_store": "gnome-libsecret"}, "keyring")):
            self.path.write_text(json.dumps(raw))
            with self.subTest(raw=raw):
                self.assertEqual(BrowserSettings(self.path).password_store(), want)
        self.path.write_text("not json")
        self.assertEqual(BrowserSettings(self.path).password_store(), "keyring")

    def test_a_switch_write_keeps_the_owner_choice(self):
        self.path.write_text(json.dumps({"password_store": "basic"}))
        s = BrowserSettings(self.path)
        s.update(lan=False)
        self.assertEqual(json.loads(self.path.read_text())["password_store"], "basic")
        self.assertEqual(s.password_store(), "basic")


class Sticky(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="keyring-profiles-"); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "browser"
        guard = patch.object(config, "BROWSER_DIR", self.root); guard.start(); self.addCleanup(guard.stop)
        self.store = P.ProfileStore()

    def test_mark_persists_survives_rename_and_validates(self):
        main = self.store.list(create=True)[0]
        self.assertNotIn("password_store", main)
        self.store.mark_keyring(main["id"])
        self.store.rename(main["id"], "Renamed")
        self.assertEqual(self.store.require(main["id"])["password_store"], "keyring")
        path = self.root / "profiles.json"
        rows = json.loads(path.read_text())
        for bad in ("basic", True, None, ""):
            path.write_text(json.dumps([{**rows[0], "password_store": bad}]))
            with self.subTest(bad=bad), self.assertRaises(H.BrowserRefused):
                self.store.list()
        path.write_text(json.dumps([{**rows[0], "other": 1}]))
        with self.assertRaises(H.BrowserRefused):
            self.store.list()
        with self.assertRaises(H.BrowserRefused):
            self.store.mark_keyring("p-00000000")


class _Context:
    def __init__(self):
        self.browser = SimpleNamespace(version="155.0")
        self.closed = False

    def on(self, *a):
        pass

    async def close(self):
        self.closed = True


class Launch(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="keyring-launch-"); self.addCleanup(tmp.cleanup)
        guard = patch.object(config, "BROWSER_DIR", Path(tmp.name) / "browser"); guard.start(); self.addCleanup(guard.stop)
        self.settings_path = Path(tmp.name) / "browser.json"
        self.host = H.BrowserHost(settings=BrowserSettings(self.settings_path))
        self.main = self.host.list_profiles()[0]["id"]
        self.host._gate = SimpleNamespace(port=1)
        self.host._sound = SimpleNamespace(launch=lambda args, env, **kw: LaunchAudio(list(args), dict(env), False, None),
                                           wake=lambda *a: None, instance_status=lambda i: None)
        self.launches = []
        self.contexts = []

        async def launch_persistent_context(folder, **kw):
            self.launches.append(kw)
            self.contexts.append(_Context())
            return self.contexts[-1]
        self.host._pw = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch_persistent_context))
        self.rt = Runtime(self)
        # Any probe that reaches the real subprocess.run fails the test.
        guard = patch.object(subprocess, "run", side_effect=AssertionError("real gdbus run"))
        guard.start(); self.addCleanup(guard.stop)

    def keyring(self, *states):
        self.run = Runner(*states)
        k = S.Keyring(run=self.run, gdbus=EXISTING)
        k.attach({"DBUS_SESSION_BUS_ADDRESS": self.rt.address}, runtime=self.rt.dir)
        self.host.keyring = k
        return k

    def basic(self, value="basic"):
        self.settings_path.write_text(json.dumps({"password_store": value}))

    def sticky(self):
        return self.host._profiles.require(self.main).get("password_store") == "keyring"

    def flags(self, n=-1):
        return self.launches[n]["args"], self.launches[n]["env"]

    async def refused(self, profile, message):
        with self.assertRaises(H.BrowserRefused) as caught:
            await self.host._launch(profile)
        self.assertEqual((caught.exception.status, str(caught.exception)), (409, message))

    async def test_unlocked_launch_uses_the_keyring_with_the_bus_and_turns_sticky(self):
        self.keyring(S.UNLOCKED, S.UNLOCKED)
        instance = await self.host._launch(self.main)
        args, env = self.flags()
        self.assertIn("--password-store=gnome-libsecret", args)
        self.assertNotIn("--password-store=basic", args)
        self.assertEqual(sum(a.startswith("--disable-features=") for a in args), 1)
        self.assertTrue(next(a for a in args if a.startswith("--disable-features=")).endswith(",HardwareMediaKeyHandling"))
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], self.rt.address)
        self.assertEqual(instance.password_store, S.KEYRING_STORE)
        self.assertTrue(self.sticky())
        self.assertEqual(len(self.run.calls), 2)          # before and after
        self.host._instances[self.main] = instance
        self.host._publish_snapshot()
        self.run.states.append(S.UNLOCKED)
        sync = self.host.password_store_status()
        self.assertEqual((sync["password_store"], sync["unprotected"], sync["hint"]), (S.KEYRING_STORE, False, None))

    async def test_locked_no_keyring_and_unknown_answers_refuse_without_launching(self):
        for state in (S.LOCKED, S.NO_KEYRING, S.ERROR):
            with self.subTest(state=state):
                self.keyring(state)
                await self.refused(self.main, S.UNLOCK_MESSAGE)
                self.assertEqual(self.launches, [])
                self.assertFalse(self.sticky())

    async def test_unavailable_launches_basic_loudly_without_the_bus(self):
        self.keyring(S.UNAVAILABLE, S.UNAVAILABLE)
        with patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": self.rt.address}):
            instance = await self.host._launch(self.main)
        args, env = self.flags()
        self.assertIn("--password-store=basic", args)
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", env)
        self.assertNotIn("HardwareMediaKeyHandling", " ".join(args))
        self.assertEqual(instance.password_store, S.BASIC)
        self.assertFalse(self.sticky())
        self.host._instances[self.main] = instance
        self.host._publish_snapshot()
        sync = self.host.password_store_status()
        self.assertEqual((sync["password_store"], sync["unprotected"]), (S.BASIC, True))
        self.assertIn(S.INSTALL_COMMAND, sync["hint"])
        self.assertTrue(sync["hint"].startswith("Unprotected (basic store)"))

    async def test_a_sticky_profile_never_opens_with_basic(self):
        self.host._profiles.mark_keyring(self.main)
        self.keyring(S.UNAVAILABLE)
        await self.refused(self.main, S.STICKY_UNAVAILABLE_MESSAGE)
        self.basic()
        self.keyring()                        # the owner's basic never probes
        await self.refused(self.main, S.STICKY_BASIC_MESSAGE)
        self.assertEqual(self.launches, [])
        self.assertEqual(self.run.calls, [])

    async def test_owner_basic_override_is_honoured_loudly_and_never_probes(self):
        self.basic()
        self.keyring()
        instance = await self.host._launch(self.main)
        args, env = self.flags()
        self.assertIn("--password-store=basic", args)
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", env)
        self.assertEqual(self.run.calls, [])
        self.host._instances[self.main] = instance
        self.host._publish_snapshot()
        self.run.states.append(S.UNLOCKED)
        sync = self.host.password_store_status()
        self.assertEqual((sync["requested"], sync["password_store"], sync["unprotected"]), ("basic", S.BASIC, True))
        self.assertIn("browser.json", sync["hint"])

    async def test_keyring_locked_during_launch_closes_chrome_and_stays_sticky(self):
        self.keyring(S.UNLOCKED, S.LOCKED)
        await self.refused(self.main, S.CHANGED_MESSAGE)
        self.assertEqual(len(self.launches), 1)
        self.assertTrue(self.contexts[0].closed)
        self.assertTrue(self.sticky())

    async def test_private_is_basic_without_the_bus_or_a_probe(self):
        self.keyring()
        instance = await self.host._launch(H.PRIVATE)
        args, env = self.flags()
        self.assertIn("--password-store=basic", args)
        self.assertIn("--disable-sync", args)
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", env)
        self.assertEqual(self.run.calls, [])
        self.assertEqual(instance.password_store, S.BASIC)

    async def test_a_detached_host_never_hands_chrome_the_real_bus(self):
        with patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/%d/bus" % os.getuid()}):
            await self.host._launch(self.main)
        args, env = self.flags()
        self.assertIn("--password-store=basic", args)
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", env)
        self.assertFalse(self.sticky())
        sync = self.host.password_store_status()
        self.assertEqual((sync["keyring"], sync["next_launch"], sync["unprotected"]), (S.UNAVAILABLE, S.BASIC, True))

    async def test_status_before_any_launch_says_what_the_next_one_does(self):
        self.keyring(S.LOCKED)
        self.host.keyring.state()
        sync = self.host.password_store_status()
        self.assertEqual((sync["password_store"], sync["next_launch"], sync["hint"]),
                         (None, "refused", S.UNLOCK_MESSAGE))
        self.assertFalse(sync["unprotected"])


class LaunchArgs(unittest.TestCase):
    def test_flag_shapes(self):
        base = H.launch_args(Path("/tmp/x"), 1, private=False)
        self.assertIn("--password-store=basic", base)
        ring = H.launch_args(Path("/tmp/x"), 1, private=False, password_store=S.KEYRING_STORE)
        self.assertEqual([a for a in ring if a not in base],
                         ["--disable-features=MediaRouter,DialMediaRouteProvider,CastMediaRouteProvider,"
                          "WebRtcHideLocalIpsWithMdns,HardwareMediaKeyHandling", "--password-store=gnome-libsecret"])
        self.assertEqual(H.launch_args(Path("/tmp/x"), 1, private=True, password_store=S.KEYRING_STORE),
                         H.launch_args(Path("/tmp/x"), 1, private=True))
        with self.assertRaises(ValueError):
            H.launch_args(Path("/tmp/x"), 1, private=False, password_store="kwallet")
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS",
                         H.chrome_environment(Path("/tmp/h"), {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/x", "LANG": "C"}))


if __name__ == "__main__":
    unittest.main()
