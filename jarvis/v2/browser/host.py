"""The Browser's engine (BR-1a; plan §2–§4, amendment 1 §C): the owner's
daemon-owned Google Chrome, one instance per profile plus the Private one.

**One thread.** Playwright's async API runs on one daemon thread,
`jarvis-owner-browser`, with its own event loop (invariant 9: all Playwright
work stays on its own thread). Other threads submit coroutines with
`run_coroutine_threadsafe`, and every call has a timeout (v1's
`Session._submit` has none). Nothing here touches v1's `jarvis.browser`.

**Lazy.** Nothing starts with the daemon: the thread, the gate and Playwright
start at the first request that needs Chrome, and a profile's Chrome at the
first tab opened on it. `Daemon.stop` ends it all, after the terminals are
hung up and before the listeners close, and kills by exact PID anything that
outlives a clean close.

**Keyed by profile from the start.** `profiles.json` lists the persistent
profiles (`p-` plus 8 random hex digits, never a label: a label can never
become a path); the first start creates **Main**. Each persistent profile is
its own Chrome on `BROWSER_DIR/profiles/<id>/`. **Private** is one more
Chrome on a throwaway `BROWSER_DIR/private/run-<hex>/` (removed at stop,
swept at the next start), whose panes are each an off-the-record context
created with `browser.new_context()` — nothing a Private pane does is written
to disk, and a context closes with its last tab. Profile management itself
(create, rename, delete) is BR-1c's.

**The folder** (`config.BROWSER_DIR`) is a credential folder: made 0700
(every folder in it too), and the engine refuses to start when it, or one of
its folders, is a symlink, not ours, or open to anyone else.

**A curated launch** (`launch_args`): Playwright's own defaults are replaced
whole (`ignore_default_args=True`). Kept: Chrome's sandbox (the setuid
helper the deb ships), Safe Browsing and component updates, sign-in and sync
(decision BD-5: no `--disable-sync` on a persistent profile). Dropped:
`--enable-automation`, `--no-sandbox`, `--disable-background-networking`,
`--disable-component-update`, `--disable-popup-blocking` and the rest of the
test-harness list. Added: the SOCKS gate with loopback **not** bypassed,
`MAP * ~NOTFOUND` so Chrome itself never resolves a name (defense in depth),
no QUIC, WebRTC held to proxied transports
(`--webrtc-ip-handling-policy=disable_non_proxied_udp`, so no candidate, no
UDP, no STUN — review of PR #69 measured 158 datagrams and mDNS under the
first cut's non-existent `--force-…` spelling), no WebRTC mDNS responder
(`WebRtcHideLocalIpsWithMdns` off: with nothing gathered there is nothing to
hide, and the responder joins 224.0.0.251 on every interface the moment a
page makes an `RTCPeerConnection` — measured, an IGMP join on the LAN), no
extensions, no permission prompts (denied), no media router (cast and DIAL
search the LAN on their own), and `--mute-audio` (sound is off until BR-10).
DevTools only over Playwright's private pipe: never `--remote-debugging-port`.

**This machine is closed like the LAN** (the lead's decision on PR #69):
a loopback port is reachable through the gate only after the owner opened a
pane on it — `open_tab`/`navigate` with `owner=True`, which only the
owner-only routes pass — and only for this host's lifetime
(`loopback_open`). A page's or an agent's navigation never opens one.

**A clean environment** (`chrome_environment`): built from nothing, not
filtered from the daemon's — `HOME` is `BROWSER_DIR/home`, so everything
Chrome writes outside its profile (its NSS store, the font cache) stays in
the credential folder; no `JARVIS_*`, no API key, no display, no
`PULSE_SERVER`, no D-Bus. (The Playwright driver, a separate node process,
inherits the daemon's environment as v1's does; no page can reach it.)

**The landing guard** (plan §4 layer 3): on every main-frame
`framenavigated`, the URL is judged again (`policy.judge_url(landed=True)`)
and a refused one is replaced with an explanation page.

**No browsing leaves memory.** No URL, title or page text is logged or put on
the bus: refusals are fixed sentences, a failed load is reported by its
`net::ERR_…` code only, and the log carries classes and counts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pwd
import re
import secrets
import shutil
import signal
import stat
import threading
import time
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from jarvis import config

from . import policy
from .gate import Gate

LOG = logging.getLogger("jarvis.v2.browser")

PRIVATE = "private"
MAIN_LABEL = "Main"
# Where Playwright's `channel="chrome"` finds Google Chrome stable on Linux
# (B-3: installed by apt). Only checked, never launched by path.
CHROME_PATH = Path("/opt/google/chrome/chrome")

CALL_TIMEOUT_S = 30.0
LAUNCH_TIMEOUT_S = 30.0
NAV_TIMEOUT_S = 20.0
CLOSE_TIMEOUT_S = 5.0
STOP_TIMEOUT_S = 10.0
PROFILES_CAP = 64 * 1024
SETTINGS_CAP = 64 * 1024
TITLE_CAP = 120
URL_CAP = 2048

TAB_ID = re.compile(r"[0-9a-f]{8}")
PROFILE_ID = re.compile(r"p-[0-9a-f]{8}")
CTX_ID = TAB_ID

EXPLANATION = (
    "<!doctype html><meta charset=utf-8><title>Not opened</title>"
    "<body style='font:15px system-ui,sans-serif;margin:3em;color:#c9c9c9;background:#141414'>"
    "<p>Jarvis's Browser does not open this kind of address.</p>"
    "<p>Only http, https and about:blank pages open here, and never Jarvis's own ports.</p>")

# The settings `browser.json` holds and their defaults (B-5: the LAN off).
# `local_servers` is off too: on, it opens every loopback port that is not
# Jarvis's, not only the ones the owner opened in a pane — the owner's
# switch, never a default.
SETTINGS_DEFAULTS = {"lan": False, "local_servers": False}


class BrowserRefused(Exception):
    """A request the Browser will not or cannot honour: an HTTP status and a
    fixed sentence. Never carries a URL or a page's text."""

    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status


# -- the launch -----------------------------------------------------------------------

def launch_args(user_data_dir: Path, gate_port: int, *, private: bool) -> list[str]:
    """Chrome's whole command line after the binary (Playwright adds nothing:
    `ignore_default_args=True`)."""
    args = [
        "--headless",
        "--remote-debugging-pipe",
        f"--user-data-dir={user_data_dir}",
        # Plan §4 layer 2: every connection through the gate, loopback too.
        f"--proxy-server=socks5://127.0.0.1:{gate_port}",
        "--proxy-bypass-list=<-loopback>",
        # Chrome never resolves a name itself: the gate does, once. Defense
        # in depth — with a SOCKS5 proxy every name already goes to the gate
        # — kept so a path that skipped the proxy would find no DNS either.
        "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
        # No UDP leaves around the gate: no QUIC, and WebRTC may use only a
        # proxied transport — a SOCKS proxy carries no UDP, so it gathers no
        # candidate and sends no STUN or ICE check. The switch is the pref's
        # own (`webrtc.ip_handling_policy` in Chrome's command-line pref
        # table); `--force-webrtc-…`, which the first cut used, does not
        # exist, and 158 datagrams went straight out (review of PR #69).
        "--disable-quic",
        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
        # Cast and DIAL discovery search the LAN by mDNS and SSDP, around any
        # proxy; WebRTC's mDNS responder (which names host candidates — none
        # are gathered here) joins the mDNS group on every interface as soon
        # as a page makes an RTCPeerConnection. One `--disable-features`:
        # Chrome keeps only the last of a repeated switch.
        "--disable-features=MediaRouter,DialMediaRouteProvider,CastMediaRouteProvider,"
        "WebRtcHideLocalIpsWithMdns",
        "--disable-extensions",
        "--deny-permission-prompts",
        "--mute-audio",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-search-engine-choice-screen",
        "--password-store=basic",
        # A desktop pointer, so pages lay out for a mouse, not a touchscreen.
        "--blink-settings=primaryHoverType=2,availableHoverTypes=2,"
        "primaryPointerType=4,availablePointerTypes=4",
    ]
    if private:
        # Private panes are off-the-record contexts; nothing in them syncs.
        args.append("--disable-sync")
    args.append("about:blank")
    return args


_LOCALE = ("LANG", "LANGUAGE", "TZ")


def chrome_environment(home: Path, source=None) -> dict[str, str]:
    """Chrome's environment, built from nothing (module note)."""
    source = os.environ if source is None else source
    env = {"HOME": str(home), "PATH": "/usr/local/bin:/usr/bin:/bin"}
    try:
        user = pwd.getpwuid(os.getuid()).pw_name
        env["USER"] = env["LOGNAME"] = user
    except KeyError:
        pass
    for name, value in source.items():
        if name in _LOCALE or name.startswith("LC_"):
            env[name] = value
    env.setdefault("LANG", "C.UTF-8")
    return env


# -- the folder -----------------------------------------------------------------------

def secure_dir(path: Path, *, create: bool = True) -> Path:
    """`path` as a private folder of ours (0700, ours, not a symlink), made
    if it is missing and `create`; else `BrowserRefused`. Never loosened or
    repaired: a folder somebody opened up is a fact the owner should hear."""
    path = Path(path)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        if not create:
            raise BrowserRefused(409, "the Browser's folder is missing") from None
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        raise BrowserRefused(409, "the Browser's folder is a symlink; it must be a real folder")
    if not stat.S_ISDIR(st.st_mode):
        raise BrowserRefused(409, "the Browser's folder is not a folder")
    if st.st_uid != os.getuid():
        raise BrowserRefused(409, "the Browser's folder belongs to another user")
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise BrowserRefused(409, "the Browser's folder is open to other users; "
                                  "it must be private (chmod 700)")
    return path


def _remove_tree(path: Path) -> None:
    """Remove a folder we made, never through a symlink (`rmtree` refuses a
    symlinked root and does not follow links inside)."""
    try:
        if os.path.lexists(path):
            shutil.rmtree(path)
    except OSError as exc:
        LOG.warning("Cannot remove a browser folder: %s", type(exc).__name__)


def _write_private(path: Path, data: bytes) -> None:
    """`data` to `path`, atomically, 0600, never through a symlink."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_capped(path: Path, cap: int) -> bytes | None:
    """A regular file's bytes, never through a symlink and never past `cap`;
    None when it does not exist."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("not a regular file")
        data = handle.read(cap + 1)
    if len(data) > cap:
        raise ValueError("too large")
    return data


# -- browser.json -----------------------------------------------------------------------

_settings_cache: tuple[tuple, dict] | None = None
_settings_lock = threading.Lock()


def load_settings() -> dict:
    """`browser.json` with defaults; a missing, unreadable or malformed file is
    the defaults (the LAN stays off). Cached by (path, mtime, size): the gate
    asks on every connection."""
    global _settings_cache
    path = config.BROWSER_SETTINGS_PATH
    try:
        st = os.stat(path)
        key = (str(path), st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        return dict(SETTINGS_DEFAULTS)
    with _settings_lock:
        if _settings_cache is not None and _settings_cache[0] == key:
            return dict(_settings_cache[1])
    out = dict(SETTINGS_DEFAULTS)
    try:
        data = json.loads((_read_capped(path, SETTINGS_CAP) or b"{}").decode("utf-8"))
        if isinstance(data, dict):
            for name, default in SETTINGS_DEFAULTS.items():
                if isinstance(data.get(name), type(default)):
                    out[name] = data[name]
    except (OSError, ValueError, UnicodeDecodeError):
        LOG.warning("browser.json is unreadable; the Browser uses its defaults")
    with _settings_lock:
        _settings_cache = (key, out)
    return dict(out)


def save_settings(changes: dict) -> dict:
    """Merge `changes` (known keys, right types) into `browser.json`, keeping
    any key a later package added, and return the settings."""
    global _settings_cache
    for name, value in changes.items():
        if name not in SETTINGS_DEFAULTS or type(value) is not type(SETTINGS_DEFAULTS[name]):
            raise BrowserRefused(400, f"{name} must be a boolean")
    path = config.BROWSER_SETTINGS_PATH
    current: dict = {}
    try:
        raw = _read_capped(path, SETTINGS_CAP)
        if raw:
            loaded = json.loads(raw.decode("utf-8"))
            if isinstance(loaded, dict):
                current = loaded
    except (OSError, ValueError, UnicodeDecodeError):
        current = {}
    current.update(changes)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_private(path, (json.dumps(current, indent=2, sort_keys=True) + "\n").encode())
    with _settings_lock:
        _settings_cache = None
    return load_settings()


# -- Chrome processes ---------------------------------------------------------------------

def _names_dir(arg: str, folder: str) -> bool:
    """`folder` appears in `arg` as a whole path: followed by the end, a `/`,
    or a space (Chrome's helpers rewrite their command line into one
    space-separated string)."""
    start = arg.find(folder)
    while start >= 0:
        end = start + len(folder)
        if end == len(arg) or arg[end] in "/ ":
            return True
        start = arg.find(folder, start + 1)
    return False


# The programs a Browser Chrome tree runs: the browser and its helpers, and
# the crash handler beside it. Nothing else is ever signalled.
CHROME_PROGRAMS = ("chrome", "chrome_crashpad_handler")


def _runs_chrome(pid: str) -> bool:
    """`/proc/<pid>/exe` is the Chrome the host launches (or its crash
    handler), in Chrome's own install folder — measured readable for every
    process of the tree, renderers in their sandbox included. A binary
    replaced by an upgrade while running reads "… (deleted)"."""
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return False
    if exe.endswith(" (deleted)"):
        exe = exe[: -len(" (deleted)")]
    path = Path(exe)
    try:
        home = CHROME_PATH.resolve().parent
    except OSError:
        return False
    return path.parent == home and path.name in CHROME_PROGRAMS


def chrome_pids(folder: Path) -> list[int]:
    """Every Chrome process whose command line names `folder` (or a path
    inside it) as a whole path: a profile's Chrome tree — its zygotes,
    renderers and crash handler included. Matched on the folder's exact path,
    so another profile's or another daemon's Chrome is never matched, **and**
    on the program it runs (`_runs_chrome`), so a process that only names
    the folder — a shell, an editor, a `grep` — is never signalled (review of
    PR #69)."""
    text = str(folder)
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/cmdline", "rb") as handle:
                argv = handle.read().split(b"\0")
        except OSError:
            continue
        if any(_names_dir(part.decode("utf-8", "replace"), text) for part in argv) and _runs_chrome(name):
            out.append(int(name))
    return out


def stop_pids(folder: Path, timeout: float = 3.0) -> int:
    """SIGTERM, then SIGKILL, every process `chrome_pids(folder)` finds, each
    by its exact PID and re-checked before every signal (a PID can be
    reused). Returns how many there were."""
    pids = chrome_pids(folder)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not set(chrome_pids(folder)) & set(pids):
            return len(pids)
        time.sleep(0.05)
    for pid in set(chrome_pids(folder)) & set(pids):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return len(pids)


def _net_error(exc: Exception) -> str:
    """A failed load as Chrome's error code only: Playwright's message names
    the URL, which must not reach a response the log might echo."""
    found = re.search(r"net::ERR_[A-Z0-9_]+", str(exc))
    return found.group(0) if found else "navigation failed"


def _clean(text, cap: int) -> str:
    from ..approvals import clean_line
    return clean_line(text if isinstance(text, str) else "", cap)


# -- state ---------------------------------------------------------------------------------

@dataclass
class _Instance:
    profile: str
    folder: Path
    context: object
    browser: object
    version: str = ""
    private_contexts: dict = field(default_factory=dict)


@dataclass
class _Tab:
    id: str
    profile: str
    ctx: str | None
    page: object


class BrowserHost:
    """The owner's Browser engine. Thread-safe entry points; all Playwright
    work on `jarvis-owner-browser`. `resolver` and `connector` are the gate's
    test seams (gate.py)."""

    def __init__(self, daemon=None, *, resolver=None, connector=None):
        self._daemon = daemon
        self._resolver = resolver
        self._connector = connector
        self._lock = threading.Lock()
        # Held for the whole of a start, and by `stop()`, so a stop can never
        # land between the gate and the loop thread and leave one running
        # (review of PR #69). Re-entrant: a failed start stops under it.
        self._start_lock = threading.RLock()
        # Loopback ports the owner opened in a pane (MED 2 of the review):
        # the only loopback ports the gate admits, for this host's lifetime.
        self._opened_ports: set[int] = set()
        self._started = False
        self._stopped = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._gate: Gate | None = None
        self._pw = None
        self._instances: dict[str, _Instance] = {}
        self._tabs: dict[str, _Tab] = {}
        self._launching: dict[str, asyncio.Lock] = {}
        self._launched: set[Path] = set()
        self._snapshot: dict = {"instances": [], "tabs": 0}
        self._guarded = 0
        self.timings: dict[str, float] = {}

    # -- ports and settings ---------------------------------------------------------

    def refused_ports(self) -> frozenset[int]:
        """`policy.refused_ports` for this daemon's reported listeners."""
        daemon = self._daemon
        if daemon is None:
            return policy.refused_ports()
        return policy.refused_ports(hud_port=daemon.face_port, api_port=daemon.port,
                                    workshop_port=daemon.workshop_port)

    def _gate_refused(self) -> frozenset[int]:
        return self.refused_ports()

    def loopback_open(self, port: int) -> bool:
        """Whether the gate admits a loopback connection to `port`: the owner
        opened a pane on it in this host's lifetime, or turned on
        `local_servers` in `browser.json`. Jarvis's ports are refused before
        this is asked, and a name that resolves to loopback never gets here."""
        with self._lock:
            if port in self._opened_ports:
                return True
        return bool(load_settings().get("local_servers"))

    def _open_loopback(self, url: str | None) -> None:
        """The owner asked for `url` in a pane: if it is on this machine, its
        port is open from now on (never one of Jarvis's ports). Only an owner
        route reaches this; a page's or an agent's navigation never does."""
        port = policy.loopback_port(url) if url else None
        if port is None or port in self.refused_ports():
            return
        with self._lock:
            self._opened_ports.add(port)

    # -- lifecycle ------------------------------------------------------------------

    def _ensure_started(self) -> None:
        """The folder checked, the gate listening and Playwright running —
        once. A second caller waits on `_start_lock` until it is all there."""
        with self._start_lock:
            with self._lock:
                if self._stopped:
                    raise BrowserRefused(409, "the Browser is stopping")
                if self._started:
                    return
            if not CHROME_PATH.exists():
                raise BrowserRefused(409, "Google Chrome is not installed "
                                          "(sudo apt install google-chrome-stable)")
            root = secure_dir(config.BROWSER_DIR)
            for name in ("profiles", "downloads", "home", "artifacts", "private"):
                secure_dir(root / name)
            self._sweep_private(root / "private")
            self._ensure_main()
            gate = Gate(refused=self._gate_refused, lan=lambda: load_settings()["lan"],
                        loopback=self.loopback_open,
                        resolver=self._resolver, connector=self._connector)
            gate.start()
            self._gate = gate
            ready = threading.Event()

            def run():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                ready.set()
                try:
                    loop.run_forever()
                finally:
                    loop.close()

            self._thread = threading.Thread(target=run, name="jarvis-owner-browser", daemon=True)
            self._thread.start()
            ready.wait(5)
            try:
                self._call(self._start_playwright(), LAUNCH_TIMEOUT_S)
            except Exception:
                self.stop()
                raise
            with self._lock:
                if self._stopped:
                    raise BrowserRefused(409, "the Browser is stopping")
                self._started = True

    async def _start_playwright(self) -> None:
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()

    def _sweep_private(self, folder: Path) -> None:
        """Private run folders a crash left behind: their Chrome (by exact
        path) and then the folder. Only under this daemon's `BROWSER_DIR`."""
        try:
            entries = list(os.scandir(folder))
        except OSError:
            return
        for entry in entries:
            if entry.name.startswith("run-") and entry.is_dir(follow_symlinks=False):
                path = Path(entry.path)
                stop_pids(path)
                _remove_tree(path)

    def stop(self, timeout: float = STOP_TIMEOUT_S) -> None:
        """Close every Chrome, the Playwright driver and the gate. Final: the
        Browser does not start again in this daemon. A start in progress is
        waited for (`_start_lock`, bounded), so it cannot leave its gate,
        loop or driver behind."""
        locked = self._start_lock.acquire(timeout=LAUNCH_TIMEOUT_S + 5)
        try:
            self._stop(timeout)
        finally:
            if locked:
                self._start_lock.release()

    def _stop(self, timeout: float) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self._opened_ports.clear()
            loop, thread = self._loop, self._thread
        deadline = time.monotonic() + timeout
        if loop is not None and not loop.is_closed():
            future = asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
            try:
                future.result(max(0.5, deadline - time.monotonic()))
            except Exception as exc:  # noqa: BLE001 - the PID backstop follows
                LOG.warning("Browser shutdown did not finish cleanly: %s", type(exc).__name__)
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
        if thread is not None:
            thread.join(max(0.5, deadline - time.monotonic()))
        # Anything that outlived a clean close, by exact PID.
        for folder in list(self._launched):
            if stop_pids(folder, timeout=2.0):
                LOG.warning("Stopped a Chrome process that outlived the Browser")
        if self._gate is not None:
            self._gate.stop()
        private = config.BROWSER_DIR / "private"
        for folder in list(self._launched):
            if folder.parent == private:
                _remove_tree(folder)
        self._publish_snapshot()

    async def _shutdown(self) -> None:
        for profile in list(self._instances):
            await self._close_instance(profile)
        if self._pw is not None:
            try:
                await asyncio.wait_for(self._pw.stop(), CLOSE_TIMEOUT_S)
            except Exception:  # noqa: BLE001
                pass
            self._pw = None

    def _call(self, coro, timeout: float = CALL_TIMEOUT_S):
        loop = self._loop
        if loop is None or loop.is_closed():
            coro.close()
            raise BrowserRefused(409, "the Browser is not running")
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout)
        except FuturesTimeout:
            future.cancel()
            raise BrowserRefused(504, "the Browser did not answer in time") from None
        except BrowserRefused:
            raise
        except Exception as exc:
            from playwright.async_api import Error as PlaywrightError
            if isinstance(exc, PlaywrightError):
                LOG.warning("Browser call failed: %s", type(exc).__name__)
                raise BrowserRefused(409, "the Browser could not do that") from None
            raise

    # -- profiles (BR-1c grows this) ---------------------------------------------------

    def _profiles_path(self) -> Path:
        return config.BROWSER_DIR / "profiles.json"

    def profiles(self) -> list[dict]:
        """`[{id, label, created}]` from `profiles.json`; [] before the first
        start. A file that is not a list of well-formed rows is refused:
        guessing could cost the owner a profile's logins."""
        try:
            raw = _read_capped(self._profiles_path(), PROFILES_CAP)
        except (OSError, ValueError):
            raise BrowserRefused(409, "profiles.json is unreadable") from None
        if raw is None:
            return []
        try:
            rows = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise BrowserRefused(409, "profiles.json is unreadable") from None
        if not isinstance(rows, list) or not all(
                isinstance(r, dict) and isinstance(r.get("id"), str) and PROFILE_ID.fullmatch(r["id"])
                and isinstance(r.get("label"), str) for r in rows):
            raise BrowserRefused(409, "profiles.json is unreadable")
        return [{"id": r["id"], "label": _clean(r["label"], 32), "created": r.get("created")}
                for r in rows]

    def _ensure_main(self) -> None:
        if self.profiles():
            return
        row = {"id": f"p-{secrets.token_hex(4)}", "label": MAIN_LABEL,
               "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        _write_private(self._profiles_path(), (json.dumps([row], indent=2) + "\n").encode())

    def _profile_id(self, profile) -> str:
        """A request's profile: omitted means the first (Main), `private`, or
        an id `profiles.json` lists."""
        if profile is None:
            rows = self.profiles()
            if not rows:
                raise BrowserRefused(409, "the Browser has no profile yet")
            return rows[0]["id"]
        if profile == PRIVATE:
            return PRIVATE
        if not isinstance(profile, str) or not PROFILE_ID.fullmatch(profile):
            raise BrowserRefused(400, 'profile must be "private" or a profile id')
        if profile not in {r["id"] for r in self.profiles()}:
            raise BrowserRefused(404, "no such profile")
        return profile

    # -- instances ------------------------------------------------------------------

    async def _instance(self, profile: str) -> _Instance:
        found = self._instances.get(profile)
        if found is not None:
            return found
        lock = self._launching.setdefault(profile, asyncio.Lock())
        async with lock:
            found = self._instances.get(profile)
            if found is not None:
                return found
            instance = await self._launch(profile)
            self._instances[profile] = instance
            self._publish_snapshot()
            return instance

    async def _launch(self, profile: str) -> _Instance:
        root = secure_dir(config.BROWSER_DIR)
        private = profile == PRIVATE
        if private:
            folder = secure_dir(root / "private" / f"run-{secrets.token_hex(4)}")
            downloads = secure_dir(root / "downloads" / PRIVATE)
        else:
            folder = secure_dir(root / "profiles" / profile)
            downloads = secure_dir(root / "downloads" / profile)
        home = secure_dir(root / "home")
        artifacts = secure_dir(root / "artifacts")
        loop = asyncio.get_running_loop()
        # A Chrome a crashed daemon left on this profile would hold its lock.
        if await loop.run_in_executor(None, stop_pids, folder):
            LOG.warning("Stopped a leftover Chrome on a Browser profile")
        self._launched.add(folder)
        started = time.monotonic()
        try:
            context = await self._pw.chromium.launch_persistent_context(
                str(folder), channel="chrome", headless=True, ignore_default_args=True,
                args=launch_args(folder, self._gate.port, private=private),
                env=chrome_environment(home), chromium_sandbox=True,
                artifacts_dir=str(artifacts), downloads_path=str(downloads),
                accept_downloads=False, handle_sigint=False, handle_sigterm=False,
                handle_sighup=False, timeout=LAUNCH_TIMEOUT_S * 1000,
                viewport={"width": 1280, "height": 800})
        except Exception as exc:  # noqa: BLE001 - Playwright's message holds the command line
            LOG.warning("Chrome did not start for a Browser profile: %s", type(exc).__name__)
            await loop.run_in_executor(None, stop_pids, folder)
            if private:
                _remove_tree(folder)
            raise BrowserRefused(409, "Chrome could not start") from None
        self.timings["launch_s"] = round(time.monotonic() - started, 3)
        browser = context.browser
        instance = _Instance(profile=profile, folder=folder, context=context, browser=browser,
                             version=getattr(browser, "version", "") or "")
        context.on("page", lambda page: self._adopt(instance, page, None))
        LOG.info("Browser started a Chrome (%s)", "private" if private else "profile")
        return instance

    async def _close_instance(self, profile: str) -> None:
        instance = self._instances.pop(profile, None)
        if instance is None:
            return
        for tab_id in [t.id for t in self._tabs.values() if t.profile == profile]:
            self._tabs.pop(tab_id, None)
        try:
            await asyncio.wait_for(instance.context.close(), CLOSE_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - the PID backstop follows
            LOG.warning("A Browser Chrome did not close cleanly")
        await asyncio.get_running_loop().run_in_executor(None, stop_pids, instance.folder)
        if profile == PRIVATE:
            _remove_tree(instance.folder)
        self._publish_snapshot()

    def _adopt(self, instance: _Instance, page, ctx: str | None):
        """Register a page as a tab (a page we opened, or a pop-up), with the
        landing guard; a page past the tab cap is closed at once."""
        for tab in self._tabs.values():
            if tab.page is page:
                return tab
        if len(self._tabs) >= max(1, config.BROWSER_TAB_CAP):
            asyncio.ensure_future(self._quietly(page.close()))
            return None
        tab = _Tab(id=self._new_id(self._tabs), profile=instance.profile, ctx=ctx, page=page)
        self._tabs[tab.id] = tab
        page.on("framenavigated", lambda frame: self._landed(tab, frame))
        page.on("close", lambda _page: self._dropped(tab))
        # A pop-up may have navigated before it was handed to us.
        self._landed(tab, page.main_frame)
        self._publish_snapshot()
        return tab

    def _dropped(self, tab: _Tab) -> None:
        if self._tabs.get(tab.id) is tab:
            del self._tabs[tab.id]
        if tab.ctx is not None and not any(t.ctx == tab.ctx for t in self._tabs.values()):
            instance = self._instances.get(PRIVATE)
            context = instance.private_contexts.pop(tab.ctx, None) if instance else None
            if context is not None:
                asyncio.ensure_future(self._quietly(context.close()))
        self._publish_snapshot()

    @staticmethod
    async def _quietly(awaitable) -> None:
        try:
            await awaitable
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _new_id(taken) -> str:
        while True:
            value = secrets.token_hex(4)
            if value not in taken:
                return value

    # -- the landing guard ------------------------------------------------------------

    def _landed(self, tab: _Tab, frame) -> None:
        try:
            if frame.parent_frame is not None:
                return
            url = frame.url
        except Exception:  # noqa: BLE001
            return
        if not url:
            # A frame that has not navigated yet — a pop-up or a `_blank`
            # tab handed to us before its first load — has landed nowhere.
            # Its first real landing comes through `framenavigated` and is
            # judged then (review of PR #69: judging "" blanked every pop-up).
            return
        try:
            policy.judge_url(url, refused=self.refused_ports(), landed=True)
        except policy.URLRefused as refusal:
            with self._lock:
                self._guarded += 1
            LOG.info("Browser landing guard replaced a page (%s)", refusal.reason)
            asyncio.ensure_future(self._replace(tab))

    async def _replace(self, tab: _Tab) -> None:
        try:
            await tab.page.goto("about:blank", timeout=NAV_TIMEOUT_S * 1000)
            await tab.page.set_content(EXPLANATION, timeout=NAV_TIMEOUT_S * 1000)
        except Exception:  # noqa: BLE001 - a closed page has nothing to replace
            pass

    # -- tabs (thread-safe entry points) ------------------------------------------------

    def open_tab(self, profile=None, url=None, ctx=None, *, owner: bool = False) -> dict:
        """A new tab, at `url` when given (judged first). `owner=True` — only
        the owner-only route passes it — also opens the URL's loopback port,
        if it has one (`loopback_open`)."""
        target = policy.judge_url(url, refused=self.refused_ports()) if url is not None else None
        if ctx is not None and (not isinstance(ctx, str) or not CTX_ID.fullmatch(ctx)):
            raise BrowserRefused(400, "ctx must be eight lowercase hex characters")
        self._ensure_started()
        profile_id = self._profile_id(profile)
        if ctx is not None and profile_id != PRIVATE:
            raise BrowserRefused(400, "only a Private tab has a ctx")
        if owner:
            self._open_loopback(target)
        return self._call(self._open_tab(profile_id, ctx, target), LAUNCH_TIMEOUT_S + NAV_TIMEOUT_S)

    async def _open_tab(self, profile: str, ctx: str | None, url: str | None) -> dict:
        cap = max(1, config.BROWSER_TAB_CAP)
        if len(self._tabs) >= cap:
            raise BrowserRefused(409, f"the Browser already has {cap} tabs open; close one first")
        instance = await self._instance(profile)
        if profile == PRIVATE:
            if ctx is None:
                ctx = self._new_id(instance.private_contexts)
                context = await instance.browser.new_context(
                    proxy={"server": f"socks5://127.0.0.1:{self._gate.port}", "bypass": "<-loopback>"},
                    accept_downloads=False, viewport={"width": 1280, "height": 800})
                instance.private_contexts[ctx] = context
                context.on("page", lambda page, c=ctx: self._adopt(instance, page, c))
            context = instance.private_contexts.get(ctx)
            if context is None:
                raise BrowserRefused(404, "no such Private context")
            page = await context.new_page()
            tab = self._adopt(instance, page, ctx)
        else:
            spare = [p for p in instance.context.pages
                     if not any(t.page is p for t in self._tabs.values())]
            page = spare[0] if spare else await instance.context.new_page()
            tab = self._adopt(instance, page, None)
        if tab is None:
            raise BrowserRefused(409, f"the Browser already has {cap} tabs open; close one first")
        row = {"tab_id": tab.id, "profile": tab.profile, "ctx": tab.ctx, "loaded": True}
        if url is not None:
            row.update(await self._goto(tab, url))
        return row

    async def _goto(self, tab: _Tab, url: str) -> dict:
        try:
            await tab.page.goto(url, wait_until="commit", timeout=NAV_TIMEOUT_S * 1000)
            return {"loaded": True}
        except Exception as exc:  # noqa: BLE001
            return {"loaded": False, "error": _net_error(exc)}

    def _tab(self, tab_id) -> _Tab:
        if not isinstance(tab_id, str) or not TAB_ID.fullmatch(tab_id):
            raise BrowserRefused(400, "a tab id is eight lowercase hex characters")
        tab = self._tabs.get(tab_id)
        if tab is None:
            raise BrowserRefused(404, "no such tab")
        return tab

    def _running(self) -> bool:
        with self._lock:
            return self._started and not self._stopped

    def navigate(self, tab_id, url, *, owner: bool = False) -> dict:
        """Load `url` in a tab, judged first. `owner=True` (the owner-only
        route) also opens its loopback port; nothing else ever does."""
        target = policy.judge_url(url, refused=self.refused_ports())
        if not self._running():
            raise BrowserRefused(404, "no such tab")
        return self._call(self._navigate(tab_id, target, owner), NAV_TIMEOUT_S + 5)

    async def _navigate(self, tab_id, url, owner: bool = False) -> dict:
        tab = self._tab(tab_id)
        if owner:
            self._open_loopback(url)
        return {"tab_id": tab.id, **(await self._goto(tab, url))}

    def history(self, tab_id, action) -> dict:
        if action not in ("back", "forward", "reload"):
            raise BrowserRefused(400, 'action must be "back", "forward" or "reload"')
        if not self._running():
            raise BrowserRefused(404, "no such tab")
        return self._call(self._history(tab_id, action), NAV_TIMEOUT_S + 5)

    async def _history(self, tab_id, action) -> dict:
        tab = self._tab(tab_id)
        step = {"back": tab.page.go_back, "forward": tab.page.go_forward,
                "reload": tab.page.reload}[action]
        try:
            await step(wait_until="commit", timeout=NAV_TIMEOUT_S * 1000)
            return {"tab_id": tab.id, "loaded": True}
        except Exception as exc:  # noqa: BLE001
            return {"tab_id": tab.id, "loaded": False, "error": _net_error(exc)}

    def close_tab(self, tab_id) -> dict:
        if not self._running():
            raise BrowserRefused(404, "no such tab")
        return self._call(self._close_tab(tab_id), CLOSE_TIMEOUT_S + 5)

    async def _close_tab(self, tab_id) -> dict:
        tab = self._tab(tab_id)
        try:
            await asyncio.wait_for(tab.page.close(), CLOSE_TIMEOUT_S)
        finally:
            self._dropped(tab)
        return {"tab_id": tab.id, "closed": True}

    def tabs(self) -> list[dict]:
        """Every tab with its URL and cleaned title. The owner's only: these
        never reach the bus, the log or `/status`."""
        if not self._running():
            return []
        return self._call(self._listing())

    async def _listing(self) -> list[dict]:
        rows = []
        for tab in list(self._tabs.values()):
            try:
                title = await asyncio.wait_for(tab.page.title(), 2.0)
            except Exception:  # noqa: BLE001
                title = ""
            rows.append({"tab_id": tab.id, "profile": tab.profile, "ctx": tab.ctx,
                         "url": _clean(tab.page.url, URL_CAP), "title": _clean(title, TITLE_CAP)})
        return rows

    def close_profile(self, profile) -> None:
        """Stop one profile's Chrome (its tabs close with it). BR-1c's idle
        stop will call this; the suites use it to keep one Chrome at a time."""
        profile_id = self._profile_id(profile)
        if self._running():
            self._call(self._close_instance(profile_id), CLOSE_TIMEOUT_S + 10)

    def forget(self, profile) -> dict:
        """"Sign out of everything" for one profile (plan §3): its Chrome is
        stopped, its folder and downloads are removed and a fresh, empty
        folder made; the label stays. For `private`, every Private context
        closes."""
        if profile == PRIVATE:
            if self._running():
                self._call(self._close_instance(PRIVATE), CLOSE_TIMEOUT_S + 10)
            return {"profile": PRIVATE, "forgotten": True}
        if not isinstance(profile, str) or not PROFILE_ID.fullmatch(profile):
            raise BrowserRefused(400, 'profile must be "private" or a profile id')
        if profile not in {r["id"] for r in self.profiles()}:
            raise BrowserRefused(404, "no such profile")
        root = secure_dir(config.BROWSER_DIR, create=False)
        folder, downloads = root / "profiles" / profile, root / "downloads" / profile

        def wipe():
            stop_pids(folder)
            _remove_tree(folder)
            _remove_tree(downloads)
            secure_dir(folder)

        if self._running():
            # Under the profile's launch lock, so no tab can start its Chrome
            # on the folder while it is being removed.
            self._call(self._forget(profile, wipe), CLOSE_TIMEOUT_S + 20)
        else:
            wipe()
        LOG.info("Browser forgot a profile")
        return {"profile": profile, "forgotten": True}

    async def _forget(self, profile: str, wipe) -> None:
        lock = self._launching.setdefault(profile, asyncio.Lock())
        async with lock:
            await self._close_instance(profile)
            await asyncio.get_running_loop().run_in_executor(None, wipe)

    # -- status -------------------------------------------------------------------------

    def _publish_snapshot(self) -> None:
        instances = [{"profile": p, "tabs": sum(t.profile == p for t in self._tabs.values()),
                      "contexts": len(i.private_contexts) if p == PRIVATE else None}
                     for p, i in self._instances.items()]
        versions = sorted({i.version for i in self._instances.values() if i.version})
        with self._lock:
            self._snapshot = {"instances": instances, "tabs": len(self._tabs),
                              "version": versions[-1] if versions else self._snapshot.get("version")}

    def status(self) -> dict:
        """What `/status` → `browser` says: no URL, title or label."""
        with self._lock:
            snapshot = dict(self._snapshot)
            running = self._started and not self._stopped
            guarded = self._guarded
            opened = len(self._opened_ports)
        gate = self._gate.stats() if self._gate is not None and running else None
        settings = load_settings()
        return {"available": CHROME_PATH.exists(), "running": running,
                "version": snapshot.get("version"), "instances": snapshot["instances"],
                "tabs": snapshot["tabs"], "tab_cap": max(1, config.BROWSER_TAB_CAP),
                "lan": settings["lan"], "local_servers": settings["local_servers"],
                # How many loopback ports the owner opened — never which.
                "loopback_opened": opened, "landing_guard": guarded, "gate": gate}
