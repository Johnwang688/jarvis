"""SEC-4 acceptance checks: credential and private files are written 0600 from
the start, never through a symlink, and a failed write leaves nothing behind.

Writers under test, each driven at its public behaviour with fakes (no network,
no real service, a temporary config folder, `os.umask(0o022)`):

  discord    jarvis.tools.discord.connect            (bot token save)
  google     jarvis.google_auth.connect              (token bundle save)
  spotify    jarvis.spotify_auth._store              (and the rotating refresh)
  subagents  jarvis.v2.subagents.SubagentRecords._compact
  stt        jarvis.stt_settings.update

Cases per writer (each printed with its own name):

  <w>.mode_never_wider      after every os/io call the writer makes, no file
                            under the config folder has a mode wider than 0600
  <w>.final_ok              the file ends up 0600 with the expected content
  <w>.create_flags          every file creation is os.open(O_EXCL|O_NOFOLLOW,
                            mode<=0600), never a builtin open / write_text
  <w>.symlink_target        a symlink already at the target is not followed
  <w>.symlink_planted       a symlink planted at the path about to be created
                            (the temp name, or the file itself) is not followed
  <w>.fsync_fail_clean      the write reaches fsync; when it fails nothing but
                            the old target is left, and the old target is intact
  <w>.replace_fail_clean    the same for os.replace

plus `spotify.rotated_persisted_before_use` (the integration rule).

These FAIL on main by design for the writers the card names. Run:

    PYTHONPATH=$PWD .venv/bin/python tests/private_writes_check.py
"""

from __future__ import annotations

import builtins
import http.client
import io
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import types
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from jarvis import config, google_auth, spotify_auth, stt_settings
from jarvis.tools import discord
from jarvis.v2 import subagents as sub

logging.disable(logging.CRITICAL)
os.umask(0o022)

FAILS: list[str] = []
PASSES = 0

# Fake key-shaped values, built at run time.
DISCORD_TOKEN = "dc" + "-fake-bot-" + "A" * 20
GOOGLE_REFRESH = "gr" + "-fake-refresh-" + "B" * 20
SPOTIFY_REFRESH = "sp" + "-fake-refresh-" + "C" * 20
SPOTIFY_ROTATED = "sp" + "-fake-rotated-" + "D" * 20
VICTIM_TEXT = "VICTIM-UNTOUCHED"


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSES
    if cond:
        PASSES += 1
        print(f"ok   {name}")
    else:
        FAILS.append(name)
        print(f"FAIL {name}" + (f"   <- {detail}" if detail else ""))


# -- the recorder --------------------------------------------------------------


class Recorder:
    """Wraps the file-touching calls; after each one (and before chmod/replace)
    scans the config folder and records any file wider than 0600."""

    def __init__(self, root: Path, plant: Path | None = None, fail: str | None = None):
        self.root = root
        self.plant_victim = plant        # when set, plant a symlink at each creation
        self.fail = fail                 # "fsync" | "replace" | None
        self.wide: list[str] = []
        self.creations: list[str] = []   # creations that are NOT O_EXCL|O_NOFOLLOW 0600
        self.planted: set[str] = set()
        self.fault_fired = False
        self.baseline: set[str] = set()
        self._orig: dict = {}

    # scanning -----------------------------------------------------------------
    def _scan(self, event: str) -> None:
        try:
            for dirpath, _dirs, files in os.walk(self.root):
                for f in files:
                    p = os.path.join(dirpath, f)
                    if p in self.planted:
                        continue
                    try:
                        st = os.lstat(p)
                    except OSError:
                        continue
                    if (st.st_mode & 0o177) and not os.path.islink(p) and p not in self.baseline:
                        self.wide.append(f"{event}: {os.path.relpath(p, self.root)} mode {oct(st.st_mode & 0o777)}")
        except OSError:
            pass

    def _inside(self, path) -> bool:
        try:
            p = os.path.abspath(os.fspath(path))
        except TypeError:
            return False
        return p == str(self.root) or p.startswith(str(self.root) + os.sep)

    def _maybe_plant(self, path) -> None:
        if self.plant_victim is None or not self._inside(path):
            return
        p = os.path.abspath(os.fspath(path))
        if os.path.lexists(p) or p in self.planted:
            return
        os.symlink(self.plant_victim, p)
        self.planted.add(p)

    # install ------------------------------------------------------------------
    def __enter__(self):
        rec = self
        o = self._orig
        o["os.open"], o["os.write"], o["os.fsync"] = os.open, os.write, os.fsync
        o["os.close"], o["os.replace"] = os.close, os.replace
        o["os.chmod"], o["os.fchmod"] = os.chmod, os.fchmod
        o["io.open"], o["builtins.open"] = io.open, builtins.open

        def w_open(path, flags, mode=0o777, *a, **k):
            if flags & os.O_CREAT and rec._inside(path):
                rec._maybe_plant(path)
                ok = (flags & os.O_EXCL) and (flags & os.O_NOFOLLOW) and (mode & 0o177) == 0
                if not ok:
                    rec.creations.append(f"os.open create flags={oct(flags)} mode={oct(mode)} {os.path.basename(os.fspath(path))}")
            fd = o["os.open"](path, flags, mode, *a, **k)
            rec._scan("os.open")
            return fd

        def w_write(fd, data):
            n = o["os.write"](fd, data)
            rec._scan("os.write")
            return n

        def w_fsync(fd):
            rec._scan("fsync")
            if rec.fail == "fsync":
                rec.fault_fired = True
                raise OSError(5, "injected fsync failure")
            return o["os.fsync"](fd)

        def w_close(fd):
            rec._scan("close")
            return o["os.close"](fd)

        def w_replace(src, dst, *a, **k):
            rec._scan("replace(before)")
            if rec.fail == "replace":
                rec.fault_fired = True
                raise OSError(5, "injected replace failure")
            r = o["os.replace"](src, dst, *a, **k)
            rec._scan("replace(after)")
            return r

        def w_chmod(path, mode, *a, **k):
            rec._scan("chmod(before)")
            return o["os.chmod"](path, mode, *a, **k)

        def w_fchmod(fd, mode):
            rec._scan("fchmod(before)")
            return o["os.fchmod"](fd, mode)

        def make_open(real):
            def w_io_open(file, mode="r", *a, **k):
                creating = isinstance(file, (str, bytes, os.PathLike)) and any(c in str(mode) for c in "wax+")
                if creating and rec._inside(file):
                    rec._maybe_plant(file)
                    rec.creations.append(f"builtin open({str(mode)!r}) {os.path.basename(os.fspath(file))}")
                h = real(file, mode, *a, **k)
                rec._scan("open")
                return h
            return w_io_open

        os.open, os.write, os.fsync, os.close, os.replace = w_open, w_write, w_fsync, w_close, w_replace
        os.chmod, os.fchmod = w_chmod, w_fchmod
        io.open = make_open(o["io.open"])
        builtins.open = make_open(o["builtins.open"])
        self.baseline = {
            os.path.join(d, f) for d, _x, fs in os.walk(self.root) for f in fs
        }
        return self

    def __exit__(self, *exc):
        o = self._orig
        os.open, os.write, os.fsync, os.close, os.replace = (
            o["os.open"], o["os.write"], o["os.fsync"], o["os.close"], o["os.replace"])
        os.chmod, os.fchmod = o["os.chmod"], o["os.fchmod"]
        io.open, builtins.open = o["io.open"], o["builtins.open"]
        return False

    def stray(self, keep: set[str]) -> list[str]:
        """Files (or links) under root that are neither kept nor planted by us."""
        out = []
        for d, ds, fs in os.walk(self.root):
            for f in fs + [x for x in ds if os.path.islink(os.path.join(d, x))]:
                p = os.path.join(d, f)
                if p not in keep and p not in self.planted:
                    out.append(os.path.relpath(p, self.root))
        return out


# -- the writers ------------------------------------------------------------------


class Writer:
    """name, how to point the config at a folder, and how to run one write."""

    name = ""
    filename = ""

    def target(self, root: Path) -> Path:
        raise NotImplementedError

    def setup(self, root: Path) -> None:
        raise NotImplementedError

    def run(self, root: Path) -> None:
        """Do one write; may raise."""
        raise NotImplementedError

    def good(self, path: Path) -> bool:
        """The written file holds what this writer was asked to save."""
        raise NotImplementedError

    def seed_old(self, path: Path) -> None:
        """An existing, valid previous file (0600) to be overwritten."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as h:
            h.write(json.dumps({"old": True}))


def _fake_resp(status: int, payload):
    return types.SimpleNamespace(status_code=status, json=lambda: payload, text=json.dumps(payload))


class DiscordW(Writer):
    name = "discord"

    def target(self, root):
        return root / "cfg" / "discord_token.json"

    def setup(self, root):
        config.DISCORD_TOKEN_PATH = self.target(root)

    def run(self, root):
        real_get, real_getpass, real_input, real_dm = httpx.get, None, builtins.input, discord.discord_dm_owner
        import getpass

        real_getpass = getpass.getpass
        urls = []

        def fake_get(url, headers=None, timeout=None):
            urls.append(url)
            if url.endswith("/users/@me"):
                return _fake_resp(200, {"username": "fakebot"})
            return _fake_resp(200, {"id": "1", "owner": {"id": "42", "username": "owner"}})

        httpx.get = fake_get
        getpass.getpass = lambda prompt="": DISCORD_TOKEN
        builtins.input = lambda prompt="": ""
        discord.discord_dm_owner = lambda *_a, **_k: "sent"
        try:
            with _quiet():
                rc = discord.connect()
            if rc != 0:
                raise RuntimeError(f"connect returned {rc}")
        finally:
            httpx.get, getpass.getpass, builtins.input, discord.discord_dm_owner = (
                real_get, real_getpass, real_input, real_dm)

    def good(self, path):
        try:
            d = json.loads(path.read_text())
        except Exception:  # noqa: BLE001
            return False
        return d.get("bot_token") == DISCORD_TOKEN and d.get("owner_id") == "42"


class GoogleW(Writer):
    name = "google"

    def target(self, root):
        return root / "cfg" / "google_token.json"

    def setup(self, root):
        config.GOOGLE_TOKEN_PATH = self.target(root)
        (root / "in").mkdir(exist_ok=True)
        (root / "in" / "client.json").write_text(json.dumps(
            {"installed": {"client_id": "cid-fake", "client_secret": "csec-" + "E" * 12}}))

    def run(self, root):
        real_post, real_browser = httpx.post, google_auth._open_browser

        def fake_browser(url):
            q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
            port = urlparse(q["redirect_uri"]).port

            def hit():
                import time
                time.sleep(0.3)
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                conn.request("GET", f"/?code=authcode&state={q['state']}")
                conn.getresponse().read()
                conn.close()

            threading.Thread(target=hit, daemon=True).start()

        httpx.post = lambda url, data=None, timeout=None, **k: _fake_resp(
            200, {"refresh_token": GOOGLE_REFRESH, "access_token": "at"})
        google_auth._open_browser = fake_browser
        try:
            with _quiet():
                rc = google_auth.connect(str(root / "in" / "client.json"))
            if rc != 0:
                raise RuntimeError(f"connect returned {rc}")
        finally:
            httpx.post, google_auth._open_browser = real_post, real_browser

    def good(self, path):
        try:
            d = json.loads(path.read_text())
        except Exception:  # noqa: BLE001
            return False
        return d.get("refresh_token") == GOOGLE_REFRESH and d.get("client_id") == "cid-fake"


class SpotifyW(Writer):
    name = "spotify"

    def target(self, root):
        return root / "cfg" / "spotify_token.json"

    def setup(self, root):
        config.SPOTIFY_TOKEN_PATH = self.target(root)

    def run(self, root):
        spotify_auth._store({"client_id": "cid", "refresh_token": SPOTIFY_REFRESH, "scopes": ["a"]})

    def good(self, path):
        try:
            return json.loads(path.read_text()).get("refresh_token") == SPOTIFY_REFRESH
        except Exception:  # noqa: BLE001
            return False


class SubagentsW(Writer):
    name = "subagents"

    def target(self, root):
        return root / "cfg" / sub.FILE

    def setup(self, root):
        pass

    def run(self, root):
        state = sub._Thread()
        child = sub._Child(sid="s1", key="k", status="done", name="n", started_at="2026-10-18T00:00:00Z")
        state.children["s1"] = child
        sub.SubagentRecords._compact(None, self.target(root), state)
        # _compact swallows its own errors (the view is not worth a failed turn);
        # report whether it landed so the checks can tell.
        if not self.good(self.target(root)):
            raise RuntimeError("compact did not land")

    def good(self, path):
        try:
            lines = path.read_text().splitlines()
            return bool(lines) and json.loads(lines[0]).get("sid") == "s1"
        except Exception:  # noqa: BLE001
            return False


class SttW(Writer):
    name = "stt"

    def target(self, root):
        return root / "cfg" / "stt.json"

    def setup(self, root):
        config.STT_SETTINGS_PATH = self.target(root)
        stt_settings._last = None

    def run(self, root):
        stt_settings.update(backend="local", cloud_fallback=False)

    def good(self, path):
        try:
            d = json.loads(path.read_text())
        except Exception:  # noqa: BLE001
            return False
        return d.get("backend") == "local" and d.get("cloud_fallback") is False


class _quiet:
    def __enter__(self):
        self._out = sys.stdout
        sys.stdout = io.StringIO()

    def __exit__(self, *exc):
        sys.stdout = self._out
        return False


WRITERS = [DiscordW(), GoogleW(), SpotifyW(), SubagentsW(), SttW()]


# -- cases -------------------------------------------------------------------------


def fresh(w: Writer):
    root = Path(tempfile.mkdtemp(prefix=f"sec4-{w.name}-", dir=os.environ.get("TMPDIR") or None)).resolve()
    (root / "cfg").mkdir()
    w.setup(root)
    return root


def attempt(w: Writer, root: Path, rec: Recorder):
    err = None
    with rec:
        try:
            w.run(root)
        except BaseException as exc:  # noqa: BLE001
            err = exc
    return err


def case_mode_and_flags(w: Writer):
    root = fresh(w)
    try:
        rec = Recorder(root / "cfg")
        err = attempt(w, root, rec)
        t = w.target(root)
        check(f"{w.name}.mode_never_wider", not rec.wide and err is None,
              "; ".join(rec.wide[:3]) or f"writer raised {err!r}")
        mode = (t.stat().st_mode & 0o777) if t.exists() else None
        check(f"{w.name}.final_ok", err is None and t.exists() and mode == 0o600 and w.good(t),
              f"mode={oct(mode) if mode is not None else None} good={t.exists() and w.good(t)} err={err!r}")
        check(f"{w.name}.create_flags", not rec.creations and err is None,
              "; ".join(rec.creations[:3]) or f"writer raised {err!r}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _victim(root: Path) -> Path:
    v = root / "victim.txt"
    fd = os.open(v, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as h:
        h.write(VICTIM_TEXT)
    return v


def case_symlink_target(w: Writer):
    root = fresh(w)
    try:
        victim = _victim(root)
        t = w.target(root)
        os.symlink(victim, t)
        rec = Recorder(root / "cfg")
        attempt(w, root, rec)           # refusing (raising) or replacing the link are both fine
        check(f"{w.name}.symlink_target", victim.read_text() == VICTIM_TEXT,
              f"victim now {victim.read_text()[:60]!r}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def case_symlink_planted(w: Writer):
    root = fresh(w)
    try:
        victim = _victim(root)
        rec = Recorder(root / "cfg", plant=victim)
        attempt(w, root, rec)
        check(f"{w.name}.symlink_planted", victim.read_text() == VICTIM_TEXT and bool(rec.planted),
              f"victim now {victim.read_text()[:60]!r}" if rec.planted else "no creation was observed to plant at")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def case_fail(w: Writer, which: str):
    root = fresh(w)
    try:
        t = w.target(root)
        w.seed_old(t)
        old = t.read_bytes()
        rec = Recorder(root / "cfg", fail=which)
        attempt(w, root, rec)
        stray = rec.stray({str(t)})
        intact = t.exists() and t.read_bytes() == old
        name = f"{w.name}.{which}_fail_clean"
        check(name, rec.fault_fired and not stray and intact,
              ("the write never reached " + which + "; ") * (not rec.fault_fired)
              + (f"stray files {stray}; " if stray else "")
              + ("old target changed" if not intact else ""))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def case_spotify_rotation():
    """The integration rule: a rotated refresh token is on disk before the access
    token is handed back."""
    w = SpotifyW()
    root = fresh(w)
    try:
        t = w.target(root)
        t.write_text(json.dumps({"client_id": "cid", "refresh_token": SPOTIFY_REFRESH, "scopes": ["a"]}))
        spotify_auth._cached_token, spotify_auth._expires_at = None, 0.0
        real_post = httpx.post
        httpx.post = lambda url, data=None, headers=None, timeout=None: _fake_resp(
            200, {"access_token": "AT", "expires_in": 3600, "refresh_token": SPOTIFY_ROTATED})
        try:
            got = spotify_auth.access_token()
        finally:
            httpx.post = real_post
            spotify_auth._cached_token, spotify_auth._expires_at = None, 0.0
        on_disk = json.loads(t.read_text()).get("refresh_token")
        mode = t.stat().st_mode & 0o777
        check("spotify.rotated_persisted_before_use", got == "AT" and on_disk == SPOTIFY_ROTATED and mode == 0o600,
              f"returned={got!r} on_disk_rotated={on_disk == SPOTIFY_ROTATED} mode={oct(mode)}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    for w in WRITERS:
        case_mode_and_flags(w)
        case_symlink_target(w)
        case_symlink_planted(w)
        case_fail(w, "fsync")
        case_fail(w, "replace")
    case_spotify_rotation()
    print(f"\nprivate_writes_check: {PASSES} passed, {len(FAILS)} failed")
    for name in FAILS:
        print(f"  FAILED {name}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
