"""B-LOGIN / M12: the Browser's Chrome password store (password_store.py).

Fakes only: a fake `gdbus` runner, a temporary BROWSER_DIR and settings file,
a temporary "runtime dir" with a socket nobody listens on, and a fake
Playwright whose Chrome writes the probe cookie's row into a temporary
Cookies database with the prefix a real Chrome would (`v11` with the keyring
flag, `v10` without, or whatever a test makes it fall back to). Never the
real session bus, never a real keyring, never a secret. `subprocess.run` is
patched to fail for the whole run, and the probe looks it up at each call, so
that guard is real (NIT 2 of the follow-up review). Run in a 3G/0swap systemd
scope like the other Browser suites.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from jarvis import config
from jarvis.v2.browser import host as H, password_store as S, profiles as P
from jarvis.v2.browser.audio import LaunchAudio
from jarvis.v2.browser.settings import BrowserSettings

EXISTING = Path(sys.executable)   # stands in for /usr/bin/gdbus: it only has to exist

# A probe that fails on the status refresh thread fails the run too.
THREAD_ERRORS = []


def setUpModule():
    real = threading.excepthook
    threading.excepthook = lambda args: (THREAD_ERRORS.append(repr(args.exc_value)), real(args))
    unittest.addModuleCleanup(setattr, threading, "excepthook", real)


def tearDownModule():
    if THREAD_ERRORS:
        raise AssertionError(f"errors on a background thread: {THREAD_ERRORS}")


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


def write_cookie(folder, name, encrypted, *, value="PLAINTEXT-NEVER-READ"):
    """One row of a Chrome-shaped Cookies table."""
    db = Path(folder) / "Default" / "Cookies"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS cookies (creation_utc INTEGER, host_key TEXT, "
                    "name TEXT, value TEXT, encrypted_value BLOB)")
        con.execute("INSERT INTO cookies VALUES (0, ?, ?, ?, ?)",
                    ("password-store.jarvis.invalid", name, value, encrypted))
        con.commit()
    finally:
        con.close()


class _Context:
    """A fake persistent context. `chrome` says what its Chrome encrypts the
    probe cookie with: "follow" (the flag it was given: `v11` for
    gnome-libsecret, `v10` for basic), a prefix (bytes: it fell back), None
    (the row never reaches the disk) or an exception to raise."""
    def __init__(self, folder, args, chrome="follow"):
        self.browser = SimpleNamespace(version="155.0")
        self.closed = False
        self.folder = Path(folder)
        self.args = args
        self.chrome = chrome
        self.cookie_calls = []

    def on(self, *a):
        pass

    async def add_cookies(self, cookies):
        self.cookie_calls.append(cookies)
        if isinstance(self.chrome, BaseException):
            raise self.chrome
        probe = cookies[0]
        if probe["expires"] == 1 or self.chrome is None:
            return
        if self.chrome == "follow":
            prefix = b"v11" if "--password-store=gnome-libsecret" in self.args else b"v10"
        else:
            prefix = self.chrome
        write_cookie(self.folder, probe["name"], prefix + b"\x00ciphertext-never-read")

    async def close(self):
        self.closed = True


class Observe(unittest.TestCase):
    """`observed_store`: three bytes of one row, nothing else."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="keyring-observe-"); self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)

    def test_prefixes(self):
        self.assertIsNone(S.observed_store(self.folder, "x"))            # no database yet
        write_cookie(self.folder, "other", b"v11zzz")
        self.assertIsNone(S.observed_store(self.folder, "x"))            # no row yet
        for name, enc, want in (("a", b"v11abc", S.KEYRING_STORE), ("b", b"v10abc", S.BASIC),
                                ("c", b"v12abc", S.UNVERIFIED), ("d", b"", S.UNVERIFIED),
                                ("e", b"v1", S.UNVERIFIED)):
            write_cookie(self.folder, name, enc)
            with self.subTest(name=name):
                self.assertEqual(S.observed_store(self.folder, name), want)

    def test_reads_only_three_bytes_of_one_row(self):
        write_cookie(self.folder, "probe", b"v11" + b"K" * 64, value="PLAINTEXT-NEVER-READ")
        seen = []
        real = sqlite3.connect

        def traced(*a, **kw):
            seen.append((a, kw))
            con = real(*a, **kw)
            con.set_trace_callback(lambda sql: seen.append(sql))
            return con
        with patch.object(S.sqlite3, "connect", traced):
            self.assertEqual(S.observed_store(self.folder, "probe"), S.KEYRING_STORE)
        (args, kw), statements = seen[0], [x for x in seen[1:] if isinstance(x, str)]
        self.assertTrue(args[0].startswith("file:") and args[0].endswith("?mode=ro&immutable=1"))
        self.assertTrue(kw.get("uri"))
        self.assertEqual(len(statements), 1)
        self.assertIn("substr(encrypted_value, 1, 3)", statements[0])
        self.assertNotRegex(statements[0].replace("encrypted_value", ""), r"\bvalue\b")

    def test_a_linked_or_odd_database_is_unverified(self):
        other = self.folder / "elsewhere"
        write_cookie(other, "probe", b"v11abc")
        (self.folder / "Default").mkdir()
        (self.folder / "Default" / "Cookies").symlink_to(other / "Default" / "Cookies")
        self.assertEqual(S.observed_store(self.folder, "probe"), S.UNVERIFIED)
        (self.folder / "Default" / "Cookies").unlink()
        (self.folder / "Default" / "Cookies").mkdir()
        self.assertEqual(S.observed_store(self.folder, "probe"), S.UNVERIFIED)

    def test_a_garbled_database_reads_again(self):
        (self.folder / "Default").mkdir()
        (self.folder / "Default" / "Cookies").write_bytes(b"not sqlite at all" * 100)
        self.assertIsNone(S.observed_store(self.folder, "probe"))

    def test_flush_constants(self):
        # The probe (one add), the filler's first add and a delete and an add
        # for each later set: at least 512 pending operations, Chrome's commit
        # batch, whatever was pending before.
        self.assertGreaterEqual(2 + 2 * (H.STORE_FLUSH_SETS - 1), 512)
        for url in (H.STORE_PROBE_URL, H.STORE_FILL_URL):
            self.assertTrue(url.startswith("https://") and url.rstrip("/").endswith(".invalid"))


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
        self.chrome = "follow"

        async def launch_persistent_context(folder, **kw):
            self.launches.append(kw)
            self.contexts.append(_Context(folder, kw["args"], self.chrome))
            return self.contexts[-1]
        self.host._pw = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch_persistent_context))
        self.rt = Runtime(self)
        self.stopped = []
        guard = patch.object(H, "stop_pids", side_effect=lambda folder, *a, **k: self.stopped.append(folder) or 0)
        guard.start(); self.addCleanup(guard.stop)
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

    def row_sticky(self, profile=None):
        return self.host._profiles.require(profile or self.main).get("password_store") == "keyring"

    def marker(self, profile=None):
        return P.profile_folder(profile or self.main) / P.MARKER

    def sticky(self, profile=None):
        return self.row_sticky(profile) and self.marker(profile).is_file()

    def strip_row(self, profile=None):
        path = config.BROWSER_DIR / "profiles.json"
        rows = json.loads(path.read_text())
        for row in rows:
            row.pop("password_store", None)
        path.write_text(json.dumps(rows))

    def flags(self, n=-1):
        return self.launches[n]["args"], self.launches[n]["env"]

    def publish(self, instance):
        self.host._instances[instance.profile] = instance
        self.host._publish_snapshot()

    async def refused(self, profile, message):
        with self.assertRaises(H.BrowserRefused) as caught:
            await self.host._launch(profile)
        self.assertEqual((caught.exception.status, str(caught.exception)), (409, message))

    async def test_unlocked_launch_uses_the_keyring_with_the_bus_and_turns_sticky(self):
        self.keyring(S.UNLOCKED)
        instance = await self.host._launch(self.main)
        args, env = self.flags()
        self.assertIn("--password-store=gnome-libsecret", args)
        self.assertNotIn("--password-store=basic", args)
        self.assertEqual(sum(a.startswith("--disable-features=") for a in args), 1)
        self.assertTrue(next(a for a in args if a.startswith("--disable-features=")).endswith(",HardwareMediaKeyHandling"))
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], self.rt.address)
        self.assertEqual(instance.password_store, S.KEYRING_STORE)
        self.assertTrue(self.sticky())
        self.assertEqual(stat.S_IMODE(self.marker().stat().st_mode), 0o600)
        self.assertEqual(self.marker().read_bytes(), P.MARKER_TEXT)
        self.assertEqual(len(self.run.calls), 1)          # before; after is the observation
        self.publish(instance)
        self.run.states.append(S.UNLOCKED)
        sync = self.host.password_store_status()
        self.assertEqual((sync["password_store"], sync["unprotected"], sync["hint"], sync["source"]),
                         (S.KEYRING_STORE, False, None, "observed"))
        self.assertEqual(sync["profiles"][self.main]["store"], S.KEYRING_STORE)
        self.assertTrue(sync["profiles"][self.main]["sticky"])
        self.assertFalse(sync["profiles"][self.main]["unprotected"])

    async def test_the_probe_cookie_is_fresh_flushed_and_expired_again(self):
        self.keyring(S.UNLOCKED, S.UNLOCKED)
        await self.host._launch(self.main)
        first = self.contexts[-1].cookie_calls
        self.assertEqual(len(first), 2)
        probe, fill = first[0][0], first[0][1:]
        self.assertTrue(probe["name"].startswith("jarvis-store-") and len(probe["name"]) == 25)
        self.assertEqual((probe["url"], probe["value"]), (H.STORE_PROBE_URL, "1"))
        self.assertGreater(probe["expires"], 0)
        self.assertEqual(len(fill), H.STORE_FLUSH_SETS)
        self.assertEqual({c["name"] for c in fill}, {H.STORE_FILL_NAME})
        self.assertEqual({c["url"] for c in fill}, {H.STORE_FILL_URL})
        self.assertEqual(len({c["value"] for c in fill}), H.STORE_FLUSH_SETS)   # each one a change
        expired = first[1]
        self.assertEqual([(c["name"], c["url"], c["expires"]) for c in expired],
                         [(probe["name"], H.STORE_PROBE_URL, 1), (H.STORE_FILL_NAME, H.STORE_FILL_URL, 1)])
        await self.host._launch(self.main)
        self.assertNotEqual(self.contexts[-1].cookie_calls[0][0]["name"], probe["name"])

    async def test_a_keyring_launch_that_chrome_wrote_with_basic_is_closed_and_refused(self):
        # MED 1: the keyring reads unlocked before and after, yet this Chrome
        # fell back (its own D-Bus call failed, a portal path …).
        self.keyring(S.UNLOCKED, S.UNLOCKED)
        self.chrome = b"v10"
        await self.refused(self.main, S.FELL_BACK_MESSAGE)
        self.assertEqual(len(self.launches), 1)
        self.assertTrue(self.contexts[0].closed)
        self.assertIn(P.profile_folder(self.main), self.stopped)
        self.assertTrue(self.sticky())
        self.assertNotIn(self.main, self.host._instances)

    async def test_a_keyring_that_locked_during_the_launch_says_so(self):
        self.keyring(S.UNLOCKED, S.LOCKED)
        self.chrome = b"v10"                       # measured: a locked collection gives v10
        await self.refused(self.main, S.CHANGED_MESSAGE)
        self.assertTrue(self.contexts[0].closed)
        self.assertTrue(self.sticky())

    async def test_an_unconfirmed_store_is_refused_for_a_keyring_launch(self):
        with patch.object(H, "STORE_OBSERVE_S", 0.2):
            for chrome in (None, b"v12", RuntimeError("cdp gone")):
                with self.subTest(chrome=chrome):
                    self.keyring(S.UNLOCKED, S.UNLOCKED)
                    self.chrome = chrome
                    await self.refused(self.main, S.UNVERIFIED_MESSAGE)
                    self.assertTrue(self.contexts[-1].closed)
        self.assertTrue(self.sticky())

    async def test_locked_no_keyring_and_unknown_answers_refuse_without_launching(self):
        for state in (S.LOCKED, S.NO_KEYRING, S.ERROR):
            with self.subTest(state=state):
                self.keyring(state)
                await self.refused(self.main, S.UNLOCK_MESSAGE)
                self.assertEqual(self.launches, [])
                self.assertFalse(self.row_sticky())
                self.assertFalse(self.marker().exists())

    async def test_unavailable_launches_basic_loudly_without_the_bus(self):
        self.keyring(S.UNAVAILABLE)
        with patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": self.rt.address}):
            instance = await self.host._launch(self.main)
        args, env = self.flags()
        self.assertIn("--password-store=basic", args)
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", env)
        self.assertNotIn("HardwareMediaKeyHandling", " ".join(args))
        self.assertEqual(instance.password_store, S.BASIC)
        self.assertFalse(self.row_sticky())
        self.assertFalse(self.marker().exists())
        self.publish(instance)
        sync = self.host.password_store_status()
        self.assertEqual((sync["password_store"], sync["unprotected"]), (S.BASIC, True))
        self.assertIn(S.INSTALL_COMMAND, sync["hint"])
        self.assertTrue(sync["hint"].startswith("Unprotected (basic store)"))
        self.assertTrue(sync["profiles"][self.main]["unprotected"])

    async def test_a_basic_launch_reports_what_was_observed(self):
        self.keyring(S.UNAVAILABLE)
        with patch.object(H, "STORE_OBSERVE_S", 0.2):
            self.chrome = None
            instance = await self.host._launch(self.main)
        self.assertEqual(instance.password_store, S.UNVERIFIED)
        self.publish(instance)
        sync = self.host.password_store_status()
        self.assertEqual((sync["password_store"], sync["unprotected"]), (S.UNVERIFIED, True))

    async def test_a_sticky_profile_never_opens_with_basic(self):
        self.host._profiles.mark_keyring(self.main)
        self.keyring(S.UNAVAILABLE)
        await self.refused(self.main, S.STICKY_UNAVAILABLE_MESSAGE)
        self.basic()
        self.keyring()                        # the owner's basic never probes
        await self.refused(self.main, S.STICKY_BASIC_MESSAGE)
        self.assertEqual(self.launches, [])
        self.assertEqual(self.run.calls, [])

    async def test_the_marker_alone_keeps_a_profile_sticky_and_restores_the_row(self):
        # MED 2: a rollback (or anything else) stripped the row's flag.
        self.host._profiles.mark_keyring(self.main)
        self.strip_row()
        self.assertFalse(self.row_sticky())
        self.keyring(S.UNAVAILABLE)
        await self.refused(self.main, S.STICKY_UNAVAILABLE_MESSAGE)
        self.basic()
        self.keyring()
        await self.refused(self.main, S.STICKY_BASIC_MESSAGE)
        self.settings_path.unlink()
        self.keyring(S.UNLOCKED)
        instance = await self.host._launch(self.main)
        self.assertEqual(instance.password_store, S.KEYRING_STORE)
        self.assertTrue(self.row_sticky())                     # written back

    async def test_the_row_alone_keeps_a_profile_sticky_and_restores_the_marker(self):
        self.host._profiles.mark_keyring(self.main)
        self.marker().unlink()
        self.keyring(S.UNAVAILABLE)
        await self.refused(self.main, S.STICKY_UNAVAILABLE_MESSAGE)
        self.keyring(S.UNLOCKED)
        await self.host._launch(self.main)
        self.assertTrue(self.marker().is_file())               # written back

    async def test_anything_at_the_marker_name_is_sticky(self):
        folder = H.secure_dir(P.profile_folder(self.main))
        for make in (lambda m: m.mkdir(), lambda m: m.symlink_to("/nonexistent")):
            with self.subTest(make=make):
                make(self.marker())
                self.assertTrue(self.host._profiles.sticky(self.main))
                self.keyring(S.UNAVAILABLE)
                await self.refused(self.main, S.STICKY_UNAVAILABLE_MESSAGE)
                (self.marker().rmdir if self.marker().is_dir() and not self.marker().is_symlink()
                 else self.marker().unlink)()
        self.assertTrue(folder.is_dir())

    async def test_sign_out_of_everything_keeps_the_profile_sticky(self):
        self.host._profiles.mark_keyring(self.main)
        (P.profile_folder(self.main) / "Cookies").write_text("old")
        self.host.forget(self.main)
        self.assertFalse((P.profile_folder(self.main) / "Cookies").exists())
        self.assertTrue(self.sticky())
        self.host.delete_profile(self.main)
        self.assertFalse(P.profile_folder(self.main).exists())
        self.assertNotIn(self.main, [r["id"] for r in self.host._profiles.list()])

    async def test_owner_basic_override_is_honoured_loudly_and_never_probes(self):
        self.basic()
        self.keyring()
        instance = await self.host._launch(self.main)
        args, env = self.flags()
        self.assertIn("--password-store=basic", args)
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", env)
        self.assertEqual(self.run.calls, [])
        self.assertEqual(instance.password_store, S.BASIC)
        self.publish(instance)
        self.run.states.append(S.UNLOCKED)     # /status refreshes the cached state behind
        sync = self.host.password_store_status()
        self.assertEqual((sync["requested"], sync["password_store"], sync["unprotected"]), ("basic", S.BASIC, True))
        self.assertIn("browser.json", sync["hint"])

    async def test_private_is_basic_without_the_bus_a_probe_or_a_cookie(self):
        self.keyring()
        instance = await self.host._launch(H.PRIVATE)
        args, env = self.flags()
        self.assertIn("--password-store=basic", args)
        self.assertIn("--disable-sync", args)
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", env)
        self.assertEqual(self.run.calls, [])
        self.assertEqual(self.contexts[-1].cookie_calls, [])
        self.assertEqual(instance.password_store, S.BASIC)

    async def test_a_detached_host_never_hands_chrome_the_real_bus(self):
        with patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/%d/bus" % os.getuid()}):
            await self.host._launch(self.main)
        args, env = self.flags()
        self.assertIn("--password-store=basic", args)
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", env)
        self.assertFalse(self.row_sticky())
        sync = self.host.password_store_status()
        self.assertEqual((sync["keyring"], sync["next_launch"], sync["unprotected"]), (S.UNAVAILABLE, S.BASIC, True))

    async def test_status_before_any_launch_says_what_the_next_one_does(self):
        self.keyring(S.LOCKED)
        self.host.keyring.state()
        sync = self.host.password_store_status()
        self.assertEqual((sync["password_store"], sync["next_launch"], sync["hint"]),
                         (None, "refused", S.UNLOCK_MESSAGE))
        self.assertFalse(sync["unprotected"])
        self.assertEqual(sync["profiles"][self.main]["next_launch"], "refused")

    async def test_unknown_is_checking_not_refused(self):
        # LOW 4: a cached `unknown` (nothing read yet) said "refused" and
        # showed the red unlock message even when the keyring was unlocked.
        self.keyring(S.UNLOCKED)
        with patch.object(threading.Thread, "start", lambda self: None):   # no refresh behind
            sync = self.host.password_store_status()
        self.assertEqual(sync["keyring"], S.UNKNOWN)
        self.assertEqual((sync["next_launch"], sync["hint"], sync["unprotected"]), ("checking", None, False))
        self.assertEqual(sync["profiles"][self.main]["next_launch"], "checking")
        self.assertIsNone(sync["profiles"][self.main]["hint"])
        # The owner's basic is known without the keyring: never "checking".
        self.basic()
        sync = self.host.password_store_status()
        self.assertEqual(sync["next_launch"], S.BASIC)

    async def test_per_profile_status_names_the_browser_json_override(self):
        # NIT 1: a sticky profile under browser.json's basic is refused for
        # browser.json, not the keyring; another profile is just basic.
        other = self.host._profiles.create("Other")["id"]
        self.host._profiles.mark_keyring(self.main)
        self.basic()
        self.keyring(S.UNLOCKED)
        self.host.keyring.state()
        sync = self.host.password_store_status()
        mine, theirs = sync["profiles"][self.main], sync["profiles"][other]
        self.assertEqual((mine["next_launch"], mine["hint"], mine["sticky"]), ("refused", S.STICKY_BASIC_MESSAGE, True))
        self.assertIn("browser.json", mine["hint"])
        self.assertEqual((theirs["next_launch"], theirs["sticky"], theirs["unprotected"]), (S.BASIC, False, True))
        self.assertIn("browser.json", theirs["hint"])
        self.assertNotIn("Unlock", theirs["hint"])


class Guard(unittest.TestCase):
    """NIT 2: the default runner is `subprocess.run` as it is at the call,
    so the suite's patch really guards a default `Keyring()` and `probe()`."""
    def test_default_runner_is_resolved_at_call_time(self):
        rt = Runtime(self)
        with patch.object(subprocess, "run", side_effect=AssertionError("real gdbus run")):
            k = S.Keyring(gdbus=EXISTING)
            k.attach({"DBUS_SESSION_BUS_ADDRESS": rt.address}, runtime=rt.dir)
            with self.assertRaises(AssertionError):
                k.state()
            with self.assertRaises(AssertionError):
                S.probe(rt.address, gdbus=EXISTING)
        seen = []
        with patch.object(subprocess, "run", side_effect=lambda *a, **kw: seen.append(a) or reply(S.LOCKED)):
            self.assertEqual(S.probe(rt.address, gdbus=EXISTING), S.LOCKED)
        self.assertEqual(len(seen), 1)


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
