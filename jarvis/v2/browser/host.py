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
to its persistent profile, and a context closes with its last tab. BR-1c
adds private metadata CRUD, at most eight profiles, three persistent Chrome
instances plus Private, and a five-minute idle stop for persistent Chrome.
Private's final context closes its Chrome and removes its temporary data.

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
owner-only routes pass — while at least one authorizing tab remains on that
port (`loopback_open`). The shared gate revokes existing tunnels when the
last tab leaves. A page's or an agent's navigation never opens one.

**A clean environment** (`chrome_environment`): built from nothing, not
filtered from the daemon's — `HOME` is `BROWSER_DIR/home` (inside the throwaway run folder for Private), so everything
Chrome writes outside its profile (its NSS store, the font cache) stays in
the credential folder. A private `FONTCONFIG_FILE` adds reachable Windows fonts
while retaining the distro defaults; no `JARVIS_*`, no API key, no display, no
`PULSE_SERVER`, no D-Bus. (The Playwright driver, a separate node process,
inherits the daemon's environment as v1's does; no page can reach it.)

**The landing guard** (plan §4 layer 3): on every main-frame
`framenavigated`, the URL is judged again (`policy.judge_url(landed=True)`)
and a refused one is replaced with an explanation page.

**No browsing leaves memory.** No URL, title or page text is logged or put on
the bus: refusals are fixed sentences, a failed load is reported by its
`net::ERR_…` code only, and the log carries classes and counts.

**The stream and the owner's input** (BR-1b, `stream.py`): a window attaches
to one tab over a ticketed socket and sees its screencast, latest frame
only; its keys, pointer, wheel and text go back as CDP `Input.*` events,
each checked against the approval hold on this thread just before it is
sent. Dialogs, file choosers, pop-ups and downloads are told to that
window; the files themselves are `files.py`'s.
"""

from __future__ import annotations

import asyncio
import base64
from functools import wraps
from .audio import InstanceAudio
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
from xml.sax.saxutils import escape as xml_escape

from jarvis import config

from . import files as _files
from . import policy
from .profiles import ProfileStore
from .gate import Gate
from .settings import BrowserSettings
from .stream import TICKET_TTL_S, TabStream, Tickets, serve as _serve_stream

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
PROFILE_INSTANCES_MAX = 3
PROFILE_IDLE_S = 300.0
SIGNIN_URL = "chrome://settings/syncSetup"
SIGNIN_PAGES = frozenset((SIGNIN_URL, "chrome://settings/people"))
PROFILES_CAP = 64 * 1024
TITLE_CAP = 120
URL_CAP = 2048
# BR-1b: the stream, dialogs, file choosers and the hold.
DIALOG_TIMEOUT_S = 60.0             # an unanswered dialog is dismissed
DIALOG_TEXT_CAP = 1000
CHOOSER_TTL_S = 300.0               # an unanswered file chooser is forgotten
DELIVER_WAIT_S = 5.0                # an input event's answer is waited for this long
JPEG_QUALITY = 70
DEFAULT_VIEW = (1280, 800, 1.0)     # the launch viewport, until a window says otherwise
SELECTION_MAX = 256 * 1024
DIALOG_KINDS = ("alert", "confirm", "prompt", "beforeunload")
HELD = "an approval card is waiting for you; nothing reaches the Browser until it is answered"
# What Ctrl+C copies: the selection, read in an isolated world the page's
# scripts cannot see into (plan §8.7). A password field gives nothing, as in
# Chrome; a text field gives its selected text.
SELECTION_JS = """(() => {
  const a = document.activeElement;
  if (a && (a.tagName === 'TEXTAREA' || a.tagName === 'INPUT')) {
    if (a.tagName === 'INPUT' && !/^(text|search|url|tel|email|number)$/i.test(a.type || 'text')) return '';
    try {
      const s = a.selectionStart, e = a.selectionEnd;
      return (typeof s === 'number' && e > s) ? String(a.value).slice(s, e) : '';
    } catch (_) { return ''; }
  }
  const s = getSelection();
  return s ? String(s) : '';
})()"""

TAB_ID = re.compile(r"[0-9a-f]{8}")
PROFILE_ID = re.compile(r"p-[0-9a-f]{8}")
CTX_ID = TAB_ID

EXPLANATION = (
    "<!doctype html><meta charset=utf-8><title>Not opened</title>"
    "<body style='font:15px system-ui,sans-serif;margin:3em;color:#c9c9c9;background:#141414'>"
    "<p>Jarvis's Browser does not open this kind of address.</p>"
    "<p>Only http, https and about:blank pages open here, and never Jarvis's own ports.</p>")


class BrowserRefused(Exception):
    """A request the Browser will not or cannot honour: an HTTP status and a
    fixed sentence. Never carries a URL or a page's text."""

    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status


# -- the launch -----------------------------------------------------------------------


def _tab_activity(method):
    """Admit on the actual host coroutine, before any page IO or lock wait."""
    @wraps(method)
    async def run(self, tab_or_id, *args, **kwargs):
        tab = self._tab(tab_or_id) if isinstance(tab_or_id, str) else tab_or_id
        return await self._activity_run(tab, lambda: method(self, tab_or_id, *args, **kwargs))
    return run


def _profile_activity(method):
    @wraps(method)
    async def run(self, profile, *args, **kwargs):
        instance = self._instances.get(profile)
        if instance is None:
            return await method(self, profile, *args, **kwargs)
        from .activity import ActivityRefused
        try:
            return await self._activity_work.run(profile, instance.generation,
                lambda: method(self, profile, *args, **kwargs))
        except ActivityRefused as exc:
            raise BrowserRefused(409, str(exc)) from None
        finally:
            self._activity_changed()
    return run


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



_WINDOWS_FONTS = Path("/mnt/c/Windows/Fonts")
_SYSTEM_FONTS = Path("/etc/fonts/fonts.conf")


def _font_home_fd(home: Path) -> int:
    """Pin an existing home without traversing any substituted symlink."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for part in home.absolute().parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        _private_font_fd(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _private_font_fd(fd: int) -> None:
    st = os.fstat(fd)
    if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid()
            or stat.S_IMODE(st.st_mode) & 0o077):
        raise BrowserRefused(409, "the Browser's font folder must be private")


def _font_child_fd(parent: int, name: str, *, create: bool) -> int:
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        _private_font_fd(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def font_environment(home: Path, source=None) -> dict[str, str]:
    """Add reachable Windows fonts using only pinned private directories.

    The first writable cache is ours. The distro include retains its aliases,
    Linux fallbacks and rendering rules; its user includes use this private
    HOME. Private Chrome's home and font cache die with its run directory.
    The caller has created home; this function never creates a parent path.
    """
    env = chrome_environment(home, source)
    if not _WINDOWS_FONTS.is_dir():
        return env
    pinned: list[int] = []
    temporary = None
    try:
        home_fd = _font_home_fd(home); pinned.append(home_fd)
        folder_fd = _font_child_fd(home_fd, "fontconfig", create=True); pinned.append(folder_fd)
        cache_fd = _font_child_fd(folder_fd, "cache", create=True); pinned.append(cache_fd)
        folder = home / "fontconfig"
        path = folder / "fonts.conf"
        try:
            st = os.stat("fonts.conf", dir_fd=folder_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                    or stat.S_IMODE(st.st_mode) != 0o600):
                raise BrowserRefused(409, "the Browser's font configuration must be a private file")
        body = ('<?xml version="1.0"?>\n'
                '<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">\n'
                '<fontconfig>\n'
                f'  <cachedir>{xml_escape(str(folder / "cache"))}</cachedir>\n'
                f'  <include>{xml_escape(str(_SYSTEM_FONTS))}</include>\n'
                f'  <dir>{xml_escape(str(_WINDOWS_FONTS))}</dir>\n'
                '</fontconfig>\n')
        temporary = ".fonts-" + secrets.token_hex(12)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=folder_fd)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
            written = os.fstat(stream.fileno())
        os.replace(temporary, "fonts.conf", src_dir_fd=folder_fd, dst_dir_fd=folder_fd)
        temporary = None
        # Writes above never follow the pathname again. Re-open the complete
        # path before handing it to Chrome, so a swapped parent is a refusal.
        current: list[int] = []
        try:
            current.append(_font_home_fd(home))
            current.append(_font_child_fd(current[-1], "fontconfig", create=False))
            current.append(_font_child_fd(current[-1], "cache", create=False))
            same = lambda left, right: (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)
            if (not all(same(os.fstat(old), os.fstat(new)) for old, new in zip(pinned, current))
                    or not same(written, os.stat("fonts.conf", dir_fd=current[1], follow_symlinks=False))):
                raise BrowserRefused(409, "the Browser's font folder changed during preparation")
        finally:
            for current_fd in reversed(current):
                os.close(current_fd)
        env["FONTCONFIG_FILE"] = str(path)
        return env
    except OSError:
        raise BrowserRefused(409, "the Browser's font configuration could not be prepared") from None
    finally:
        if temporary is not None and len(pinned) >= 2:
            try:
                os.unlink(temporary, dir_fd=pinned[1])
            except OSError:
                pass
        for pinned_fd in reversed(pinned):
            os.close(pinned_fd)


def _remove_tree(path: Path, *, strict: bool = False) -> None:
    """Remove a folder we made, never through a symlink (`rmtree` refuses a
    symlinked root and does not follow links inside)."""
    try:
        if os.path.lexists(path):
            shutil.rmtree(path)
    except OSError as exc:
        LOG.warning("Cannot remove a browser folder: %s", type(exc).__name__)
        if strict:
            raise BrowserRefused(409, "the Private browser data could not be removed") from None


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
    idle: object = None
    closing: bool = False
    generation: int = 0
    audio: InstanceAudio = field(default_factory=lambda: InstanceAudio(False))
    audio_owner: object = None
    restoring: bool = False
    unadopted: list = field(default_factory=list)
    restore_allocation: bool = False
    refused_pages: list = field(default_factory=list)
    popup_cleanup: list = field(default_factory=list)


@dataclass
class _Tab:
    id: str
    profile: str
    ctx: str | None
    page: object
    # BR-1b: the window's stream and what the page asks of it.
    stream: TabStream | None = None
    opener: str | None = None
    cdp: object = None
    cdp_lock: object = None
    wakeup: object = None               # an asyncio.Event: input is queued
    pump: object = None                 # the input task
    casting: tuple | None = None        # the screencast's size, while it runs
    cast_lock: object = None
    security: str | None = None
    location: tuple | None = None
    dialogs: dict = field(default_factory=dict)
    timers: dict = field(default_factory=dict)
    chooser: tuple | None = None        # (id, FileChooser, loop time)
    pressed: dict = field(default_factory=dict)     # button -> hold count before dispatch
    keys_pressed: dict = field(default_factory=dict)  # code -> hold count before dispatch
    input_task: object = None           # unanswered CDP input, never bank more behind it
    loopback_port: int | None = None
    pending_port: int | None = None
    signin: bool = False
    page_identity: int = 0              # host-issued lifetime, never a Python address
    document_generation: int = 0        # includes same-URL/same-document navigation
    pane_ready: bool = False             # false until the landing guard accepts it
    retired: bool = False                # stops ticket minting before same-ID detach
    loading: bool = False                # main-frame request has not finished loading
    activity_holds: dict = field(default_factory=dict)


class BrowserHost:
    """The owner's Browser engine. Thread-safe entry points; all Playwright
    work on `jarvis-owner-browser`. `resolver` and `connector` are the gate's
    test seams (gate.py)."""

    def __init__(self, daemon=None, *, resolver=None, connector=None, settings=None):
        self._daemon = daemon
        self._resolver = resolver
        self._connector = connector
        # The owner's switches (`lan`, `local_servers`): the daemon's
        # `BrowserSettings`, the one reader of `browser.json`
        # (`settings.py`). A host made without a daemon keeps its own in
        # memory, at the defaults (both off).
        self._settings = (settings or getattr(daemon, "browser_settings", None)
                          or BrowserSettings())
        self._lock = threading.Lock()
        # Held for the whole of a start, and by `stop()`, so a stop can never
        # land between the gate and the loop thread and leave one running
        # (review of PR #69). Re-entrant: a failed start stops under it.
        self._start_lock = threading.RLock()
        # Loopback ports the owner opened in a pane (MED 2 of the review):
        # the only loopback ports the gate admits while their owner tabs remain.
        self._port_owners: dict[int, set[str]] = {}
        self._profiles = ProfileStore()
        self._started = False
        self._stopped = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._gate: Gate | None = None
        self._pw = None
        from .activity import BrowserActivity
        from .activity_work import ActivityWork
        self.activity = BrowserActivity()
        self._activity_work = ActivityWork(self.activity)
        from .attention_cleanup import AttentionCleanup
        self._attention_cleanup = AttentionCleanup(self)
        from .auth import OwnerAuth
        self._auth = OwnerAuth(self)
        self._auth.navigation_timeout = NAV_TIMEOUT_S
        from .owner_tools import OwnerTools
        self._owner_tools = OwnerTools(self)
        self._next_instance_generation = 0
        self._instances: dict[str, _Instance] = {}
        self._closing_instances: dict[str, _Instance] = {}
        self._tabs: dict[str, _Tab] = {}
        self._launching: dict[str, asyncio.Lock] = {}
        self._launch_reserved: set[str] = set()
        self._closing_contexts: set[str] = set()
        self._retiring_contexts: dict[str, asyncio.Task] = {}
        self._download_tasks: dict[asyncio.Task, _Tab] = {}
        # BR5: separate hidden-context identities count against tabs and keep
        # Private alive through capture/retirement, without entering owner tabs.
        self._look_reserved: set[str] = set()
        self._look_tasks: set[asyncio.Task] = set()
        self._capture_hold_epoch = 0
        self._look_lock = None
        self._launched: set[Path] = set()
        self._snapshot: dict = {"instances": [], "tabs": 0}
        from .sound import BrowserSound
        self._sound = BrowserSound(self)
        self._next_page_identity = 0
        self._auth_pending: dict[str, str] = {}
        self._pane_tabs: dict[str, dict] = {}  # private in-process identity cache; never status/bus
        self._guarded = 0
        self.timings: dict[str, float] = {}
        # BR-1b: the windows' sockets.
        self.tickets = Tickets()
        self._downloading = 0
        self._popups_refused = 0
        # The moment a card is raised, every tab's queued input is dropped,
        # so nothing sent before or during a card is delivered after it
        # (plan §8.3) — even input the hold check at dispatch never met,
        # because no input arrived while the card was up. Synchronous on the
        # asking thread, before the broker waits.
        bus = getattr(daemon, "bus", None)
        if bus is not None and hasattr(bus, "observe"):
            bus.observe(self._observe)

    # -- exact-instance lifecycle activity -------------------------------------------

    def _activity_changed(self) -> None:
        # The sound coordinator, installed below, coalesces a relevant state
        # change. Never called while the activity lock is held.
        sound = getattr(self, "_sound", None)
        if sound is not None:
            sound.wake()

    def begin_activity(self, tab_id: str, page_identity: int):
        """Thread-safe parent hold for a judged agent operation; no page IO."""
        from .activity import ActivityRefused
        with self._lock:
            row = self._pane_tabs.get(tab_id)
            if (self._stopped or row is None or row["page_identity"] != page_identity):
                raise BrowserRefused(409, "this Browser page is no longer current")
            profile, generation = row["profile"], row["instance_generation"]
        try:
            return self.activity.enter(profile, generation)
        except ActivityRefused as exc:
            raise BrowserRefused(409, str(exc)) from None

    def end_activity(self, token) -> None:
        self.activity.leave(token)
        self._activity_changed()

    async def _activity_run(self, tab: _Tab, operation):
        from .activity import ActivityRefused
        instance = self._instances.get(tab.profile)
        if self._tabs.get(tab.id) is not tab or instance is None or instance.closing:
            raise BrowserRefused(409, "this Browser page is no longer current")
        try:
            return await self._activity_work.run(tab.profile, instance.generation, operation)
        except ActivityRefused as exc:
            raise BrowserRefused(409, str(exc)) from None
        finally:
            self._activity_changed()

    def page_hold(self, tab: _Tab, key: str):
        """Loop-only fixed owner-attention hold (also the future auth seam).

        A page callback cannot attach to a replacement merely by tab ID.
        Release the returned token only after actual answer/timeout cleanup.
        """
        from .activity import ActivityRefused
        instance = self._instances.get(tab.profile)
        if self._tabs.get(tab.id) is not tab or instance is None or instance.closing:
            return None
        if key in tab.activity_holds:
            return tab.activity_holds[key]
        try:
            token = self.activity.observed_hold(tab.profile, instance.generation)
        except ActivityRefused:
            return None
        tab.activity_holds[key] = token
        return token

    def release_page_hold(self, tab: _Tab, key: str, token) -> None:
        if token is None or tab.activity_holds.get(key) is not token:
            return
        tab.activity_holds.pop(key, None)
        self.end_activity(token)

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
        still has an authorizing pane on it, or turned on
        `local_servers` in `browser.json`. Jarvis's ports are refused before
        this is asked, and a name that resolves to loopback never gets here."""
        with self._lock:
            if self._port_owners.get(port):
                return True
        return self._settings.value("local_servers")

    def _sync_ports(self, tab: _Tab) -> None:
        """Only an admitted owner navigation grants; every last lease revokes."""
        if self._tabs.get(tab.id) is not tab or self._stopped:
            return
        ports = {p for p in (tab.loopback_port, tab.pending_port) if p is not None}
        with self._lock:
            before = set(self._port_owners)
            for port, owners in list(self._port_owners.items()):
                owners.discard(tab.id)
                if not owners:
                    del self._port_owners[port]
            for port in ports - self.refused_ports():
                self._port_owners.setdefault(port, set()).add(tab.id)
            revoked = before - set(self._port_owners)
        if revoked and self._gate is not None:
            self._gate.revoke_closed_loopback()

    def settings_changed(self, *, sound_requested: bool = False) -> None:
        if self._gate is not None:
            self._gate.revoke_closed_loopback()
        self._sound.sync(explicit=sound_requested)

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
            gate = Gate(refused=self._gate_refused, lan=lambda: self._settings.value("lan"),
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
        # A Private download lives only as long as the Private Chrome (BR-1b).
        _remove_tree(folder.parent / "downloads" / PRIVATE)

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
            self.activity.stop()
            self._port_owners.clear()
            loop, thread = self._loop, self._thread
        # Every window first: told the Browser ended, its socket closed.
        self.tickets.clear()
        for tab in list(self._tabs.values()):
            if tab.stream is not None:
                tab.stream.close("ended")
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
        await self._sound.settle()
        await self._activity_work.settle()
        await self._auth.settle()
        looks=list(self._look_tasks)
        for task in looks:task.cancel()
        if looks:await asyncio.gather(*looks,return_exceptions=True)
        for profile in list(self._instances.keys() | self._closing_instances.keys()):
            await self._close_instance(profile)
        await self._attention_cleanup.settle()
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

    @staticmethod
    def _tab_cap() -> int:
        return max(1, config.BROWSER_TAB_CAP)

    def capture_look(self, snapshot, whole, images, forms, current, *, image_edge=1568):
        """The BR5 service's read adapter. Never admits an owner port lease."""
        from .look_capture import LOOK_TIMEOUT_S, capture, direct_url
        from .look import LookRefused
        deadline = time.monotonic() + LOOK_TIMEOUT_S
        current()
        if snapshot.mode == "direct":
            direct_url(self, snapshot.url)
            self._ensure_started()
        elif not self._running():
            raise LookRefused("no live HUD Browser pane is available")
        async def run():
            operation = lambda: capture(self, snapshot, whole, images, forms, current, deadline, image_edge=image_edge)
            if snapshot.mode == "direct":
                # Private never restarts while a hidden look exists. Its exact
                # lifetime remains owned by capture's reserved context cleanup.
                return await operation()
            return await self._activity_run(self._tab(snapshot.tab_id), operation)
        return self._call(run(), max(0.1, deadline-time.monotonic()) + CLOSE_TIMEOUT_S*3)

    def prepare_action(self, snapshot, action, capability, pin, forms, guard, *, prepared=None):
        """BR9b private adapter: read facts, never grant a network lease."""
        from .drive_execute import prepare
        guard()
        async def run():
            return await self._activity_run(self._tab(snapshot.tab_id),
                lambda: prepare(self, snapshot, action, capability, pin, forms, guard, prepared))
        return self._call(run(), 20)

    def execute_action(self, snapshot, action, prepared, pin, forms, guard, *, upload_bytes=None):
        """Wait through cancelled in-flight input cleanup before releasing a driver."""
        from .drive_execute import ACTION_TIMEOUT_S, execute
        from .drive import ActResult
        guard()
        if not self._running():
            return ActResult("stopped", "Stopped: the Browser closed.")
        done = threading.Event()
        async def run():
            try:
                return await self._activity_run(self._tab(snapshot.tab_id),
                    lambda: execute(self, snapshot, action, prepared, pin, forms, guard, upload_bytes=upload_bytes))
            finally:
                done.set()
        future = asyncio.run_coroutine_threadsafe(run(), self._loop)
        try:
            return future.result(ACTION_TIMEOUT_S + 10)
        except FuturesTimeout:
            future.cancel()
            # A cancelled concurrent Future is marked done before its coroutine
            # has settled. Wait on the coroutine's finally, not future.done().
            # Browser shutdown's exact-process kill also settles a stuck CDP.
            while not done.wait(.1):
                pass
            return ActResult("stopped", "Stopped: the Browser action timed out; look again.")

    def drive_notice(self, tab_id, thread_id, action_id, action, outcome, prepared, text):
        tab = self.require_tab(tab_id)
        target = "" if prepared.context_flagged or prepared.target is None else prepared.target.name
        rect = None
        if prepared.rect and not prepared.context_flagged:
            left, top, right, bottom = prepared.rect
            rect = {"x": left, "y": top, "width": right-left, "height": bottom-top}
        tab.stream.agent_driver(thread_id, "fast")
        tab.stream.agent_action({"id": action_id, "at": datetime.now(timezone.utc).isoformat(), "action": action,
            "target": target, "text": text if action in ("type", "select") else "",
            "outcome": outcome, "rect": rect, "page_identity": tab.page_identity,
            "document_generation": tab.document_generation})

    def drive_finished(self, tab_id, action_id, outcome):
        tab = self._tabs.get(tab_id)
        if tab is None:
            return
        with tab.stream._lock:
            row = next((dict(row) for row in tab.stream.actions if row["id"] == action_id), None)
        if row is not None:
            row["outcome"] = outcome
            tab.stream.agent_action(row)

    @staticmethod
    def sound_explanation():
        return ("<!doctype html><meta charset=utf-8><title>Browser restored</title>"
                "<body><p>This page was left blank after the sound setting changed. "
                "Enter an address or use Sign in to open it again.</p>")

    def set_sound(self, tab_id, on):
        self._refuse_if_held()
        if not self._running():
            raise BrowserRefused(404, "no such tab")
        return self._call(self._sound.change(tab_id, on))

    def retry_sound(self, tab_id):
        self._refuse_if_held()
        if not self._running():
            raise BrowserRefused(404, "no such tab")
        return self._call(self._sound.retry(tab_id))

    # -- profiles (BR-1c) ---------------------------------------------------

    def profiles(self) -> list[dict]:
        return self._profiles.list()

    def list_profiles(self) -> list[dict]:
        with self._start_lock:
            return self._profiles.list(create=True)

    def create_profile(self, label) -> dict:
        self._refuse_if_held()
        with self._start_lock:
            return self._profiles.create(label)

    def rename_profile(self, profile, label) -> dict:
        self._refuse_if_held()
        with self._start_lock:
            return self._profiles.rename(profile, label)

    def _ensure_main(self) -> None:
        self._profiles.list(create=True)

    def _profile_id(self, profile) -> str:
        """A request's profile: omitted means the first (Main), `private`, or
        an id `profiles.json` lists."""
        if profile is None:
            rows = self.profiles()
            if not rows:
                raise BrowserRefused(409, "create a Browser profile first")
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
        # Caller holds the profile lifecycle lock through adoption/navigation.
        if profile != PRIVATE:
            self._profiles.require(profile)
        found = self._instances.get(profile)
        if found is not None:
            if found.closing or self._sound_allocation_blocked(found):
                raise BrowserRefused(409, "the Browser is changing its sound setting; retry from its Remote pane")
            if found.idle is not None:
                found.idle.cancel()
                found.idle = None
            return found
        if profile in self._closing_instances:
            await self._close_instance(profile)
        count = len((set(self._instances) | set(self._closing_instances) | self._launch_reserved) - {PRIVATE})
        if profile != PRIVATE and count >= PROFILE_INSTANCES_MAX:
            raise BrowserRefused(409, "three profiles are already open; close one first")
        self._launch_reserved.add(profile)
        try:
            instance = await self._launch(profile)
            self._next_instance_generation += 1
            instance.generation = self._next_instance_generation
            if not self.activity.opened(profile, instance.generation):
                await self._quietly(instance.context.close())
                raise BrowserRefused(409, "old Browser work is still settling")
            self._instances[profile] = instance
            self._publish_snapshot()
            self._sound.wake()
            return instance
        finally:
            self._launch_reserved.discard(profile)

    def _idle_if_empty(self, instance: _Instance) -> None:
        if (instance.closing or any(t.profile == instance.profile for t in self._tabs.values())
                or (instance.profile == PRIVATE and instance.private_contexts)):
            return
        if instance.idle is not None:
            instance.idle.cancel()
        async def expire():
            try:
                await asyncio.sleep(0 if instance.profile == PRIVATE else PROFILE_IDLE_S)
                lock = self._launching.setdefault(instance.profile, asyncio.Lock())
                async with lock:
                    if (self._instances.get(instance.profile) is instance
                            and not any(t.profile == instance.profile for t in self._tabs.values())):
                        instance.idle = None
                        await self._close_instance(instance.profile)
            except asyncio.CancelledError:
                pass
        instance.idle = asyncio.create_task(expire())

    async def _launch(self, profile: str, *, restart_token=None) -> _Instance:
        root = secure_dir(config.BROWSER_DIR)
        private = profile == PRIVATE
        if private:
            folder = secure_dir(root / "private" / f"run-{secrets.token_hex(4)}")
            downloads = secure_dir(root / "downloads" / PRIVATE)
        else:
            folder = secure_dir(root / "profiles" / profile)
            downloads = secure_dir(root / "downloads" / profile)
        # Chrome writes a download under Playwright's own name here; a
        # finished one is copied out, 0600, under its cleaned name (files.py).
        incoming = secure_dir(downloads / _files.INCOMING)
        home = secure_dir((folder if private else root) / "home")
        artifacts = secure_dir((folder if private else root) / "artifacts")
        loop = asyncio.get_running_loop()
        # A Chrome a crashed daemon left on this profile would hold its lock.
        if await loop.run_in_executor(None, stop_pids, folder):
            LOG.warning("Stopped a leftover Chrome on a Browser profile")
        self._launched.add(folder)
        started = time.monotonic()
        launch_audio = self._sound.launch(launch_args(folder, self._gate.port, private=private),
                                          font_environment(home), profile=profile, restart_token=restart_token)
        try:
            context = await self._pw.chromium.launch_persistent_context(
                str(folder), channel="chrome", headless=True, ignore_default_args=True,
                args=launch_audio.args,
                env=launch_audio.env, chromium_sandbox=True,
                artifacts_dir=str(artifacts), downloads_path=str(incoming),
                accept_downloads=True, handle_sigint=False, handle_sigterm=False,
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
                             version=getattr(browser, "version", "") or "",
                             audio=InstanceAudio(launch_audio.applied))
        instance.audio.on_settled(self._sound.wake)
        context.on("page", lambda page: self._adopt(instance, page, None))
        LOG.info("Browser started a Chrome (%s)", "private" if private else "profile")
        return instance

    async def _close_instance(self, profile: str, *, preserve_activity: bool = False) -> None:
        instance = self._instances.pop(profile, None) or self._closing_instances.get(profile)
        if instance is None:
            return
        self._closing_instances[profile] = instance
        instance.closing = True
        if not preserve_activity:
            self._sound.clear(profile)
            self.activity.closed(profile, instance.generation)
        if profile == PRIVATE:
            self._closing_contexts.update(instance.private_contexts)
        if instance.idle is not None:
            instance.idle.cancel()
            instance.idle = None
        for tab_id in [t.id for t in self._tabs.values() if t.profile == profile]:
            tab = self._tabs.get(tab_id)
            if tab is not None:
                self._release(tab, "ended")
                self._tabs.pop(tab_id, None)
        try:
            await asyncio.wait_for(instance.context.close(), CLOSE_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - the PID backstop follows
            LOG.warning("A Browser Chrome did not close cleanly")
        await asyncio.get_running_loop().run_in_executor(None, stop_pids, instance.folder)
        if instance.popup_cleanup:
            await asyncio.gather(*instance.popup_cleanup, return_exceptions=True)
        await self._settle_downloads(profile)
        if profile == PRIVATE:
            _remove_tree(instance.folder, strict=True)
            # A Private download goes with the Private Chrome (BR-1b).
            _remove_tree(config.BROWSER_DIR / "downloads" / PRIVATE, strict=True)
            self._closing_contexts.difference_update(instance.private_contexts)
            instance.private_contexts.clear()
            self._look_reserved.clear()
        self._sound.retirement_finished(instance)
        self._closing_instances.pop(profile, None)
        self._publish_snapshot()

    def _sound_allocation_blocked(self, instance: _Instance) -> bool:
        # Owned stream mute cannot silence streams a new page might create.
        # Only structurally quiet launch mode permits allocation after OFF.
        return instance.audio.applied and not self._sound.preference(instance.profile)[0]

    def _adopt(self, instance: _Instance, page, ctx: str | None, *, logical_id=None, restart_token=None):
        """Register a page as a tab (a page we opened, or a pop-up), with the
        landing guard, its stream and the page's own requests (dialogs, file
        choosers, downloads). **The cap is 12 tabs in all** (decision B-13:
        "12 in all", which also bounds each profile's Chrome), and a page
        past it — always a pop-up, since a route refuses first — is closed
        at once and its opener's window told: the new page is refused,
        rather than an older tab closed, because an older tab may hold the
        owner's work (a half-filled form, a sign-in mid-way), and a page
        could otherwise use pop-ups to evict the owner's tabs one by one."""
        if self._instances.get(instance.profile) is not instance or instance.closing or self._stopped:
            # Restoration teardown owns overflow pages; never allocate a task
            # per popup after that bounded replacement has been retired.
            if not instance.restoring and not instance.closing:
                asyncio.ensure_future(self._refuse_popup(page))
            return None
        if self._sound_allocation_blocked(instance):
            if page not in instance.refused_pages:
                if len(instance.unadopted) + len(instance.refused_pages) < self._tab_cap():
                    instance.refused_pages.append(page)
                    task = asyncio.create_task(asyncio.wait_for(page.close(), CLOSE_TIMEOUT_S))
                    instance.popup_cleanup.append(task)
                    def settled(done):
                        if not done.cancelled():
                            done.exception()
                        if done in instance.popup_cleanup:
                            instance.popup_cleanup.remove(done)
                        if page in instance.refused_pages:
                            instance.refused_pages.remove(page)
                        self._sound.wake()
                    task.add_done_callback(settled)
                else:
                    instance.closing = True
                    self._sound.retire(instance)
            return None
        if instance.restoring:
            if logical_id is None:
                if page in instance.unadopted or page in instance.refused_pages:
                    return None
                pending = len(instance.unadopted) + len(instance.refused_pages)
                if pending < self._tab_cap():
                    if instance.restore_allocation:
                        instance.unadopted.append(page)
                    else:
                        instance.refused_pages.append(page)
                        instance.popup_cleanup.append(asyncio.create_task(
                            asyncio.wait_for(page.close(), CLOSE_TIMEOUT_S)))
                else:
                    # Excess unsolicited pages retire this replacement. The
                    # coordinator's context teardown owns the overflow page.
                    instance.closing = True
                return None
            if not self.activity.restart_current(restart_token, instance.profile, instance.generation):
                return None
            descriptor = self._sound.restarts.descriptors.get(logical_id)
            if descriptor is None or descriptor.profile != instance.profile or logical_id in self._tabs:
                return None
        elif logical_id is not None:
            return None
        for tab in self._tabs.values():
            if tab.page is page:
                return tab
        reserved = self._sound.restarts.reserved() - int(logical_id in self._sound.restarts.descriptors)
        if len(self._tabs) + len(self._look_reserved) + reserved >= max(1, config.BROWSER_TAB_CAP):
            asyncio.ensure_future(self._refuse_popup(page))
            return None
        tab_id = logical_id or self._new_id(set(self._tabs) | set(self._sound.restarts.descriptors))
        self._next_page_identity += 1
        tab = _Tab(id=tab_id, profile=instance.profile, ctx=ctx, page=page,
                   page_identity=self._next_page_identity,
                   stream=TabStream(tab_id, held=self.input_held, wake=lambda: self._wake(tab),
                                    blocked=lambda: bool(tab.dialogs) or self._auth.blocked(tab)),
                   cdp_lock=asyncio.Lock(), cast_lock=asyncio.Lock(), wakeup=asyncio.Event())
        self._tabs[tab.id] = tab
        page.on("framenavigated", lambda frame: self._landed(tab, frame))
        page.on("close", lambda _page: self._dropped(tab))
        page.on("request", lambda request: self._loading(tab, request))
        page.on("load", lambda _page: self._loaded(tab))
        page.on("dialog", lambda dialog: self._dialog(tab, dialog))
        page.on("filechooser", lambda chooser: self._chooser(tab, chooser))
        page.on("download", lambda download: self._start_download(tab, download))
        # A pop-up may have navigated before it was handed to us.
        self._landed(tab, page.main_frame)
        token = None if instance.restoring else self.page_hold(tab, "popup")
        async def linked():
            try:
                if token is not None:
                    await self._link_opener(tab)
            finally:
                self.release_page_hold(tab, "popup", token)
        asyncio.ensure_future(linked())
        self._publish_snapshot()
        return tab

    async def _link_opener(self, tab: _Tab) -> None:
        """A pop-up opens in its opener's pane: the opener's window is told
        the new tab's id, and adds it as a tab beside its own (B-13)."""
        try:
            opener = await asyncio.wait_for(tab.page.opener(), CLOSE_TIMEOUT_S)
        except Exception:  # noqa: BLE001
            return
        parent = next((t for t in self._tabs.values() if opener is not None and t.page is opener), None)
        if parent is None or self._tabs.get(tab.id) is not tab:
            return
        tab.opener = parent.id
        parent.stream.send({"t": "popup", "tab_id": tab.id})

    async def _refuse_popup(self, page) -> None:
        cap = max(1, config.BROWSER_TAB_CAP)
        with self._lock:
            self._popups_refused += 1
        # Playwright keeps the opener relation on the closed Page object.
        await self._quietly(page.close())
        try:
            opener = await asyncio.wait_for(page.opener(), CLOSE_TIMEOUT_S)
        except Exception:  # noqa: BLE001
            opener = None
        parent = next((t for t in self._tabs.values() if opener is not None and t.page is opener), None)
        if parent is not None:
            parent.stream.send({"t": "popup_refused",
                                "reason": f"the page opened a new tab, but the Browser already has "
                                          f"{cap} tabs open; close one first"})
        LOG.info("Browser refused a pop-up past the tab cap")

    def _release(self, tab: _Tab, reason: str) -> None:
        """A tab is gone: its window is told and let go, its timers and its
        input task stop. On the Browser's thread."""
        with self._lock:
            tab.retired = True
            # A stale old release must not revoke replacement tickets.
            if self._tabs.get(tab.id) is tab:
                self.tickets.revoke(tab.id)
        self._owner_tools.invalidate(tab, closed=True)
        self._auth.release(tab)
        tab.loopback_port = tab.pending_port = None
        self._sync_ports(tab)
        for key, token in list(tab.activity_holds.items()):
            # Actual popup/download (and future auth) cleanup owns its token.
            if key == "chooser" or key.startswith("dialog:"):
                self.release_page_hold(tab, key, token)
        if tab.stream is not None:
            tab.stream.close(reason)
        for timer in tab.timers.values():
            timer.cancel()
        tab.timers.clear()
        tab.dialogs.clear()
        tab.chooser = None
        if tab.pump is not None:
            tab.pump.cancel()
            tab.pump = None

    def _dropped(self, tab: _Tab) -> None:
        if self._tabs.get(tab.id) is not tab:
            return
        drive=getattr(self._daemon,"browser_drive",None)
        if drive is not None:drive.downloads.closed_tab(tab.id)
        self._release(tab, "closed")
        del self._tabs[tab.id]
        if not any(t.profile == tab.profile for t in self._tabs.values()):
            self._sound.clear(tab.profile)
        instance = self._instances.get(tab.profile)
        if (tab.ctx is not None and not any(t.ctx == tab.ctx for t in self._tabs.values())
                and tab.ctx not in self._closing_contexts):
            self._closing_contexts.add(tab.ctx)
            task = asyncio.create_task(self._retire_context(tab.ctx))
            self._retiring_contexts[tab.ctx] = task
            def finished(done):
                if self._retiring_contexts.get(tab.ctx) is done:
                    self._retiring_contexts.pop(tab.ctx, None)
                if not done.cancelled() and done.exception() is not None:
                    LOG.warning("A Private context did not finish closing")
            task.add_done_callback(finished)
        elif instance is not None:
            self._idle_if_empty(instance)
        self._publish_snapshot()

    async def _retire_context(self, ctx: str) -> None:
        async with self._launching.setdefault(PRIVATE, asyncio.Lock()):
            await self._retire_context_locked(ctx)

    async def _retire_context_locked(self, ctx: str) -> None:
        # The identity and closing marker survive every failure/cancellation.
        # A retry can finish the same context, even if its final engine close
        # moved that engine to _closing_instances before cleanup failed.
        instance = self._instances.get(PRIVATE) or self._closing_instances.get(PRIVATE)
        if instance is None or ctx not in instance.private_contexts:
            raise BrowserRefused(404, "no such Private context")
        self._closing_contexts.add(ctx)
        context = instance.private_contexts[ctx]
        for tab in [t for t in self._tabs.values() if t.ctx == ctx]:
            self._release(tab, "context closed")
            self._tabs.pop(tab.id, None)
        await asyncio.wait_for(context.close(), CLOSE_TIMEOUT_S)
        await self._settle_downloads(PRIVATE, ctx)
        _remove_tree(config.BROWSER_DIR / "downloads" / PRIVATE / ctx, strict=True)
        if (len(instance.private_contexts) == 1
                and not any(t.profile == PRIVATE for t in self._tabs.values())):
            await self._close_instance(PRIVATE)
        instance.private_contexts.pop(ctx, None)
        self._closing_contexts.discard(ctx)
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

    def _loading(self, tab: _Tab, request) -> None:
        if self._tabs.get(tab.id) is not tab or self._stopped:
            return
        try:
            if request.is_navigation_request() and request.frame is tab.page.main_frame:
                tab.loading = True
                self._activity_changed()
        except Exception:
            pass

    def _loaded(self, tab: _Tab) -> None:
        if self._tabs.get(tab.id) is not tab or self._stopped:
            return
        tab.loading = False
        asyncio.ensure_future(self._quietly(self._titled(tab)))
        self._activity_changed()

    def _landed(self, tab: _Tab, frame) -> None:
        if self._tabs.get(tab.id) is not tab or self._stopped:
            return
        try:
            if frame.parent_frame is not None:
                return
            url = frame.url
        except Exception:  # noqa: BLE001
            return
        # Invalidate before judging/publishing the new URL. Playwright emits
        # framenavigated for same-document changes and same-URL reloads too.
        self._owner_tools.invalidate(tab)
        if tab.stream is not None:
            # The new document cannot receive a queued old-document release,
            # and the owner gate must not retain old depressed bookkeeping.
            tab.stream.discard_document_input()
            tab.pressed.clear()
            tab.keys_pressed.clear()
            tab.stream.send({"t": "owner_reset"})
        self._auth.document_changed(tab)
        tab.document_generation += 1
        tab.pane_ready = False
        self._publish_pane_tabs()
        if not url:
            # A frame that has not navigated yet — a pop-up or a `_blank`
            # tab handed to us before its first load — has landed nowhere.
            # Its first real landing comes through `framenavigated` and is
            # judged then (review of PR #69: judging "" blanked every pop-up).
            return
        port = policy.loopback_port(url)
        auth_session = self._auth.sessions.get(tab.id)
        auth_refused = bool(auth_session and auth_session.navigation and auth_session.navigation.refused)
        if port == tab.pending_port and port is not None and not auth_refused:
            tab.loopback_port = port
        elif port != tab.loopback_port:
            tab.loopback_port = None
        tab.pending_port = None
        self._sync_ports(tab)
        internal = tab.signin and url in SIGNIN_PAGES
        if not internal:
            tab.signin = False
        try:
            if not internal:
                policy.judge_url(url, refused=self.refused_ports(), landed=True)
        except policy.URLRefused as refusal:
            with self._lock:
                self._guarded += 1
            LOG.info("Browser landing guard replaced a page (%s)", refusal.reason)
            asyncio.ensure_future(self._quietly(self._replace(tab, tab.document_generation)))
            return
        # A new page has no chooser open any more.
        if tab.chooser is not None:
            tab.chooser = None
            self.release_page_hold(tab, "chooser", tab.activity_holds.get("chooser"))
            timer = tab.timers.pop("chooser", None)
            if timer is not None:
                timer.cancel()
            if tab.stream is not None:
                tab.stream.remember("chooser", None)
        tab.pane_ready = True
        self._activity_changed()
        self._located(tab, url=url)
        self._publish_pane_tabs()  # _located may return early for a same-URL reload

    def _located(self, tab: _Tab, *, url: str | None = None, title: str | None = None,
                 force: bool = False) -> None:
        """The tab's address, title and security state, to its window only
        (`{t:"location"}`; never the bus, the log or `/status`)."""
        if self._tabs.get(tab.id) is not tab or self._stopped:
            return
        old_url, old_title = tab.location or ("", "")
        new_url = _clean(url, URL_CAP) if url is not None else old_url
        if title is not None:
            new_title = _clean(title, TITLE_CAP)
        else:
            new_title = old_title if new_url == old_url else ""
        if (new_url, new_title) == tab.location and not force:
            return
        tab.location = (new_url, new_title)
        if tab.stream is not None:
            tab.stream.remember("location", {"t": "location", "url": new_url, "title": new_title,
                                             "security": tab.security})

    @_tab_activity
    async def _titled(self, tab: _Tab) -> None:
        try:
            title = await asyncio.wait_for(tab.page.title(), 2.0)
        except Exception:  # noqa: BLE001
            return
        self._located(tab, url=tab.page.url, title=title)

    @_tab_activity
    async def _replace(self, tab: _Tab, generation: int) -> None:
        # Scheduled, never awaited by a navigation holding this lock. A failed
        # landing may queue its guard just before the owner's next navigation;
        # it must not blank the newer document when it finally gets CPU.
        async with self._launching.setdefault(tab.profile, asyncio.Lock()):
            if self._tabs.get(tab.id) is not tab or tab.document_generation != generation:
                return
            try:
                await tab.page.goto("about:blank", wait_until="commit", timeout=NAV_TIMEOUT_S * 1000)
                # Main-frame commit increments the generation once. A close or
                # another landing while goto waited invalidates the explanation.
                if (self._tabs.get(tab.id) is not tab or tab.document_generation != generation + 1
                        or tab.page.url != "about:blank"):
                    return
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
        if target is not None:
            self._refuse_if_held()
        self._ensure_started()
        profile_id = self._profile_id(profile)
        if ctx is not None and profile_id != PRIVATE:
            raise BrowserRefused(400, "only a Private tab has a ctx")
        return self._call(self._open_tab(profile_id, ctx, target, owner=owner), LAUNCH_TIMEOUT_S + NAV_TIMEOUT_S)

    @_profile_activity
    async def _open_tab(self, profile: str, ctx: str | None, url: str | None,
                        *, owner: bool = False, signin: bool = False) -> dict:
        async with self._launching.setdefault(profile, asyncio.Lock()):
            try:
                return await self._open_tab_locked(profile, ctx, url, owner=owner, signin=signin)
            finally:
                instance = self._instances.get(profile)
                if instance is not None:
                    self._idle_if_empty(instance)

    async def _open_tab_locked(self, profile: str, ctx: str | None, url: str | None,
                               *, owner: bool, signin: bool) -> dict:
        cap = max(1, config.BROWSER_TAB_CAP)
        if url is not None:
            self._refuse_if_held()
        if len(self._tabs) + len(self._look_reserved) + self._sound.restarts.reserved() >= cap:
            raise BrowserRefused(409, f"the Browser already has {cap} tabs open; close one first")
        if profile == PRIVATE and self._closing_contexts:
            if ctx is None or ctx in self._closing_contexts or PRIVATE in self._closing_instances:
                raise BrowserRefused(409, "Private cleanup is unfinished; retry closing it first")
        instance = await self._instance(profile)
        try:
            return await self._activity_work.run(profile, instance.generation,
                lambda: self._open_page_locked(instance, ctx, url, owner=owner, signin=signin))
        finally:
            self._activity_changed()

    async def _open_page_locked(self, instance: _Instance, ctx, url, *, owner, signin):
        profile = instance.profile
        cap = max(1, config.BROWSER_TAB_CAP)
        if self._sound_allocation_blocked(instance):
            raise BrowserRefused(409, "Browser sound is turning off; wait for its Remote pane to finish")
        if profile == PRIVATE:
            if ctx is None:
                ctx = self._new_id(instance.private_contexts)
                context = await instance.browser.new_context(
                    proxy={"server": f"socks5://127.0.0.1:{self._gate.port}", "bypass": "<-loopback>"},
                    accept_downloads=True, viewport={"width": 1280, "height": 800})
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
            tab.signin = signin
            row.update(await self._goto(tab, url, owner=owner))
        return row

    async def _goto(self, tab: _Tab, url: str, *, owner: bool = False, restart_token=None) -> dict:
        if restart_token is None:
            self._refuse_if_held()
        else:
            instance = self._instances.get(tab.profile)
            if (owner or instance is None or self._tabs.get(tab.id) is not tab
                    or not self.activity.restart_current(restart_token, tab.profile, instance.generation)):
                raise BrowserRefused(409, "this Browser replacement is no longer current")
        if url in SIGNIN_PAGES:
            await self._auth.cancel_navigation(tab)
            try:
                await tab.page.goto(url, wait_until="commit", timeout=NAV_TIMEOUT_S * 1000)
                return {"loaded": True}
            except Exception as exc:
                return {"loaded": False, "error": _net_error(exc)}
        return await self._auth.navigate(tab,
            lambda: tab.page.goto(url, wait_until="commit", timeout=0),
            owner=owner, port=policy.loopback_port(url), restart_token=restart_token)

    async def cancel_navigation(self, tab: _Tab) -> None:
        """Settle the exact supervised auth/navigation child before teardown."""
        await self._auth.cancel_navigation(tab)

    def auth_pending_current(self, tab_id, nav_id) -> bool:
        """Private exact live allocation receipt; no host-loop submission."""
        with self._lock:
            return bool(not self._stopped and isinstance(nav_id, str)
                        and self._auth_pending.get(tab_id) == nav_id)

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
        self._refuse_if_held()
        return self._call(self._navigate(tab_id, target, owner), NAV_TIMEOUT_S + 5)

    @_tab_activity
    async def _navigate(self, tab_id, url, owner: bool = False) -> dict:
        tab = self._tab(tab_id)
        async with self._launching.setdefault(tab.profile, asyncio.Lock()):
            tab = self._tab(tab_id)
            self._refuse_if_held()
            tab.signin = False
            return {"tab_id": tab.id, **(await self._goto(tab, url, owner=owner))}

    def history(self, tab_id, action) -> dict:
        if action not in ("back", "forward", "reload"):
            raise BrowserRefused(400, 'action must be "back", "forward" or "reload"')
        if not self._running():
            raise BrowserRefused(404, "no such tab")
        self._refuse_if_held()
        return self._call(self._history(tab_id, action), NAV_TIMEOUT_S + 5)

    @_tab_activity
    async def _history(self, tab_id, action) -> dict:
        tab = self._tab(tab_id)
        async with self._launching.setdefault(tab.profile, asyncio.Lock()):
            tab = self._tab(tab_id)
            self._refuse_if_held()
            tab.signin = False
            step = {"back": tab.page.go_back, "forward": tab.page.go_forward,
                    "reload": tab.page.reload}[action]
            return {"tab_id": tab.id, **(await self._auth.navigate(tab,
                lambda: step(wait_until="commit", timeout=0), owner=True))}

    def close_tab(self, tab_id) -> dict:
        if not self._running():
            raise BrowserRefused(404, "no such tab")
        return self._call(self._close_tab(tab_id), CLOSE_TIMEOUT_S + 25)

    async def _close_tab(self, tab_id) -> dict:
        # Cancel before waiting for the profile lock; a replacement cannot
        # resurrect an owner-closed logical tab after its next await.
        profile = self._sound.restarts.cancel_tab(tab_id)
        tab = self._tabs.get(tab_id)
        if tab is None and profile is None:
            raise BrowserRefused(404, "no such tab")
        profile = profile or tab.profile
        if (not any(t.profile == profile and t.id != tab_id for t in self._tabs.values())
                and not any(d.profile == profile for d in self._sound.restarts.descriptors.values())):
            self._sound.clear(profile)
        async with self._launching.setdefault(profile, asyncio.Lock()):
            tab = self._tabs.get(tab_id)
            if tab is None:
                return {"tab_id": tab_id, "closed": True}
            try:
                await self._auth.cancel_navigation(tab)
                await asyncio.wait_for(tab.page.close(), CLOSE_TIMEOUT_S)
            finally:
                self._dropped(tab)
            retirement = self._retiring_contexts.get(tab.ctx)
        # Last-tab closure is a completed data-removal operation for Private.
        # Its task takes the same lifecycle lock, so await it after releasing.
        if retirement is not None:
            await asyncio.shield(retirement)
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
                         "url": _clean(tab.page.url, URL_CAP), "title": _clean(title, TITLE_CAP),
                         # BR-1b: the tab that opened it (a pop-up's), and
                         # whether a window streams it now.
                         "opener": tab.opener,
                         "shown": tab.stream is not None and tab.stream.attached() is not None})
        return rows + self._sound.restarts.rows()

    def close_profile(self, profile) -> None:
        profile_id = self._profile_id(profile)
        if self._running():
            self._call(self._close_profile(profile_id), CLOSE_TIMEOUT_S + 20)

    async def _close_profile(self, profile: str) -> None:
        self._sound.clear(profile)
        self._sound.restarts.cancel_profile(profile)
        async with self._launching.setdefault(profile, asyncio.Lock()):
            await self._close_instance(profile)

    def signin(self, profile) -> dict:
        self._refuse_if_held()
        self._profiles.require(profile)  # Private is never signed in to Chrome.
        self._ensure_started()
        return self._call(self._open_tab(profile, None, SIGNIN_URL, signin=True),
                          LAUNCH_TIMEOUT_S + NAV_TIMEOUT_S)

    def close_context(self, ctx) -> dict:
        if not isinstance(ctx, str) or not CTX_ID.fullmatch(ctx):
            raise BrowserRefused(400, "ctx must be eight lowercase hex characters")
        self._refuse_if_held()
        if not self._running():
            raise BrowserRefused(404, "no such Private context")
        return self._call(self._close_context(ctx), CLOSE_TIMEOUT_S + 20)

    async def _close_context(self, ctx: str) -> dict:
        async with self._launching.setdefault(PRIVATE, asyncio.Lock()):
            self._refuse_if_held()
            await self._retire_context_locked(ctx)
        return {"ctx": ctx, "closed": True}

    def forget(self, profile) -> dict:
        self._refuse_if_held()
        if profile == PRIVATE:
            self.close_profile(PRIVATE)
            return {"profile": PRIVATE, "forgotten": True}
        return self._manage_profile(profile, delete=False)

    def delete_profile(self, profile) -> dict:
        self._refuse_if_held()
        return self._manage_profile(profile, delete=True)

    def _manage_profile(self, profile, *, delete: bool) -> dict:
        # Exclude a cold Chrome start while metadata/data are being removed.
        with self._start_lock:
            self._profiles.require(profile)
            if self._running():
                self._call(self._manage_profile_async(profile, delete), CLOSE_TIMEOUT_S + 30)
            else:
                self._wipe_profile(profile)
                if delete:
                    self._profiles.delete(profile)
                else:
                    secure_dir(config.BROWSER_DIR / "profiles" / profile)
        return {"profile": profile, "deleted" if delete else "forgotten": True}

    @_profile_activity
    async def _manage_profile_async(self, profile: str, delete: bool) -> None:
        async with self._launching.setdefault(profile, asyncio.Lock()):
            self._refuse_if_held()
            self._profiles.require(profile)
            await self._close_instance(profile)
            # A prior close may have timed out waiting for an executor copy.
            # Its task remains tracked: never wipe until the writer stopped.
            await self._settle_downloads(profile)
            # Cancellation cannot stop a filesystem worker. Keep the lifecycle
            # lock until its wipe and metadata commit finish, including when a
            # caller timed out, so a queued open cannot race the old worker.
            wiping = asyncio.get_running_loop().run_in_executor(None, self._wipe_profile, profile)
            cancelled = False
            while not wiping.done():
                try:
                    await asyncio.shield(wiping)
                except asyncio.CancelledError:
                    # More than one caller/stop timeout may cancel this task.
                    # Each cancellation is deferred; none reaches the worker.
                    cancelled = True
            wiping.result()
            if delete:
                self._profiles.delete(profile)
            else:
                secure_dir(config.BROWSER_DIR / "profiles" / profile)
            if cancelled:
                raise asyncio.CancelledError()

    def _wipe_profile(self, profile: str) -> None:
        self._profiles.require(profile)
        root = secure_dir(config.BROWSER_DIR, create=False)
        targets = []
        for name in ("profiles", "downloads"):
            parent = secure_dir(root / name, create=False)
            path = parent / profile
            if os.path.lexists(path):
                secure_dir(path, create=False)
            targets.append(path)
        stop_pids(targets[0])
        try:
            for path in targets:
                if os.path.lexists(path):
                    shutil.rmtree(path)  # symlink roots refused above and by rmtree
        except OSError:
            raise BrowserRefused(409, "the profile data could not be removed") from None

    # -- the hold (plan §8.3, amendment H3) -----------------------------------------------

    def capture_hold_epoch(self) -> int:
        """A capture spanning any approval card must be started again."""
        with self._lock:
            return self._capture_hold_epoch

    def input_held(self) -> bool:
        """Whether **any** approval is waiting on the owner: then nothing
        reaches a page. Fails closed — a broker that cannot be asked holds."""
        daemon = self._daemon
        if daemon is None:
            return False
        try:
            return bool(daemon.approvals.pending())
        except Exception:  # noqa: BLE001
            return True

    def _refuse_if_held(self) -> None:
        if self.input_held():
            raise BrowserRefused(409, HELD)

    def _observe(self, record) -> None:
        """A bus observer: a card raised holds every tab at once."""
        if isinstance(record, dict) and record.get("kind") in ("approval_resolved",):
            self._activity_changed()
        if isinstance(record, dict) and record.get("kind") == "approval_requested":
            with self._lock:
                self._capture_hold_epoch += 1
            for tab in list(self._tabs.values()):
                if tab.stream is not None:
                    tab.stream.hold_through()
                self._auth.invalidate(tab)
                self._owner_tools.invalidate(tab)
        elif isinstance(record, dict) and record.get("kind") == "approval_resolved":
            async def refresh_auth():
                for tab in list(self._tabs.values()):
                    self._auth.refresh(tab)
            self._submit(refresh_auth())

    # -- windows' sockets (BR-1b, stream.py) ------------------------------------------------

    def _submit(self, coro) -> None:
        """Run `coro` on the Browser's thread without waiting for it; an
        error is logged by its type only."""
        loop = self._loop
        if loop is None or loop.is_closed():
            coro.close()
            return
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            coro.close()
            return

        def done(f):
            if not f.cancelled() and f.exception() is not None:
                LOG.warning("Browser stream work failed: %s", type(f.exception()).__name__)
        future.add_done_callback(done)

    def _wake(self, tab: _Tab) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed() and tab.wakeup is not None:
            try:
                loop.call_soon_threadsafe(tab.wakeup.set)
            except RuntimeError:
                pass

    def require_tab(self, tab_id) -> _Tab:
        """The tab, from any thread (404 in words)."""
        if not self._running():
            raise BrowserRefused(404, "no such tab")
        return self._tab(tab_id)

    def ticket(self, tab_id) -> dict:
        tab = self.require_tab(tab_id)
        with self._lock:
            instance = self._instances.get(tab.profile)
            if (self._tabs.get(tab.id) is not tab or tab.retired or self._stopped
                    or instance is None or instance.closing or instance.restoring):
                raise BrowserRefused(409, "this Browser page is no longer current")
            ticket = self.tickets.mint(tab.id, tab.page_identity)
        return {"ticket": ticket, "expires_in": int(TICKET_TTL_S)}

    def _attached_event(self, tab_id: str) -> None:
        """`browser_attached`: the tab's id and the time, never a URL, so the
        HUD can tell an attach that was not its own (`terminal_attached`'s
        rule)."""
        daemon = self._daemon
        bus = getattr(daemon, "bus", None)
        if bus is None:
            return
        try:
            from ..model import utcnow
            bus.publish({"kind": "browser_attached", "data": {"tab_id": tab_id, "at": utcnow()}})
        except Exception as exc:  # noqa: BLE001 - a bus problem never ends a session
            LOG.warning("browser tab %s: attach not published (%s)", tab_id, type(exc).__name__)

    def serve_socket(self, tab_id: str, sock, *, expected_tab=None) -> None:
        """A window's socket for one tab, read on the calling (HTTP handler)
        thread until it closes."""
        tab = self._tabs.get(tab_id)
        instance = self._instances.get(tab.profile) if tab is not None else None
        if (tab is None or tab.stream is None or tab.retired or not self._running()
                or instance is None or instance.closing or instance.restoring
                or (expected_tab is not None and tab is not expected_tab)):
            from .. import ws
            sock.send_text(json.dumps({"t": "refused", "reason": "no such tab"}))
            sock.close(ws.POLICY, "refused")
            sock.wait_closed(ws.CLOSE_WAIT_S + 0.5)
            sock.abort()
            return
        row = {"tab_id": tab.id, "profile": tab.profile, "ctx": tab.ctx, "opener": tab.opener}

        def attached():
            self._owner_tools.invalidate(tab, attachment=True)
            LOG.info("browser tab %s attached", tab.id)
            self._attached_event(tab.id)
            self._submit(self._on_attached(tab))

        def detached():
            if tab.stream.attached() is None:
                self._owner_tools.invalidate(tab, attachment=True)
            self._submit(self._on_detached(tab))

        _serve_stream(tab.stream, sock, tab_row=row, on_attached=attached,
                      on_detached=detached,
                      handle=lambda s, msg: self._handle(tab, s, msg))

    def _handle(self, tab: _Tab, sock, msg) -> None:
        """One valid message from the tab's window (the reader thread)."""
        if self._tabs.get(tab.id) is not tab or self._stopped:
            return
        stream = tab.stream
        if not stream.holds(sock):
            return
        t = msg.t
        if t in ("find", "find_cancel", "cursor", "cursor_cancel"):
            self._owner_tools.receive(tab, sock, {"t": t, **msg.fields})
        elif t == "auth":
            if self.input_held():
                stream.hold_through()
                msg.fields.clear()
                return
            self._auth.receive(tab, sock, stream.gen, self.capture_hold_epoch(), msg.fields)
        elif msg.input:
            stream.enqueue(sock, msg)
        elif t == "dialog":
            if self.input_held():
                stream.hold_through()
                return
            stream.owner_input()
            self._submit(self._close_dialog(tab, msg.fields["id"],
                                            (msg.fields["accept"], msg.fields["text"]), "owner"))
        elif t == "ack":
            stream.ack(sock, msg.fields["id"])
        elif t == "resize":
            self._owner_tools.invalidate(tab)
            stream.viewport = (msg.fields["width"], msg.fields["height"], msg.fields["scale"])
            self._submit(self._set_viewport(tab))
        elif t == "visible":
            if stream.set_visible(sock, msg.fields["visible"]):
                self._submit(self._sync_screencast(tab))
            if not msg.fields["visible"]:
                self._owner_tools.invalidate(tab)
                self._auth.invalidate(tab)
            async def refresh_auth():
                self._auth.refresh(tab)
            self._submit(refresh_auth())
        elif t == "takeover":
            stream.answer_takeover(sock, msg)
        elif t == "resume":
            # Resume Jarvis is refused under a card, as its button is (§B.9).
            if self.input_held():
                stream.send_to(sock, {"t": "held"})
            else:
                stream.resume()
        elif t == "stop":
            stream.pause("stop")
        elif t == "selection":
            self._submit(self._selection(tab, stream.gen))
        elif t == "ping":
            stream.send_to(sock, {"t": "pong"})

    @_tab_activity
    async def _on_attached(self, tab: _Tab) -> None:
        await self._ensure_cdp(tab)
        if tab.pump is None:
            tab.pump = asyncio.ensure_future(self._pump(tab))
        if tab.location is None:
            self._located(tab, url=tab.page.url)
            await self._titled(tab)
        await self._sync_screencast(tab)
        self._auth.refresh(tab)

    @_tab_activity
    async def _on_detached(self, tab: _Tab) -> None:
        if tab.stream.attached() is None:
            self._auth.invalidate(tab)
        await self._sync_screencast(tab)
        # A press a dropped socket cut off is let go off the page, never as a
        # click (BR-2's HeldButtons rule) — unless a card is up: then nothing
        # reaches the page, and the next input carries the buttons' state.
        if tab.pressed and tab.stream.attached() is None and not self.input_held():
            await self._release_pressed(tab)

    async def _ensure_cdp(self, tab: _Tab):
        if tab.cdp is not None:
            return tab.cdp
        async with tab.cdp_lock:
            if tab.cdp is None and self._tabs.get(tab.id) is tab:
                cdp = await tab.page.context.new_cdp_session(tab.page)
                cdp.on("Page.screencastFrame", lambda params: self._frame(tab, cdp, params))
                cdp.on("Security.visibleSecurityStateChanged", lambda params: self._secured(tab, params))
                try:
                    await cdp.send("Security.enable")
                except Exception:  # noqa: BLE001 - the security state is a courtesy
                    pass
                tab.cdp = cdp
        return tab.cdp

    def _secured(self, tab: _Tab, params) -> None:
        if self._tabs.get(tab.id) is not tab or self._stopped:
            return
        state = ((params or {}).get("visibleSecurityState") or {}).get("securityState")
        if isinstance(state, str) and len(state) <= 32 and state != tab.security:
            tab.security = state
            self._located(tab, force=True)

    # -- frames ----------------------------------------------------------------------------

    def _view(self, tab: _Tab) -> tuple[int, int, float]:
        return tab.stream.viewport or DEFAULT_VIEW

    async def _sync_screencast(self, tab: _Tab) -> None:
        """Start, restart (a new size) or stop the tab's screencast to match
        whether a window streams it: a tab nobody shows sends nothing."""
        cdp = tab.cdp
        if cdp is None:
            return
        async with tab.cast_lock:
            want = tab.stream.streaming() and self._tabs.get(tab.id) is tab
            width, height, scale = self._view(tab)
            size = (round(width * scale), round(height * scale))
            try:
                if tab.casting is not None and (not want or tab.casting != size):
                    tab.casting = None
                    tab.stream.forget_picture()
                    await cdp.send("Page.stopScreencast")
                if want and tab.casting is None:
                    await cdp.send("Page.startScreencast", {
                        "format": "jpeg", "quality": JPEG_QUALITY, "everyNthFrame": 1,
                        "maxWidth": size[0], "maxHeight": size[1]})
                    tab.casting = size
            except Exception as exc:  # noqa: BLE001 - a closed page has nothing to stream
                LOG.info("browser tab %s: screencast not changed (%s)", tab.id, type(exc).__name__)

    @_tab_activity
    async def _set_viewport(self, tab: _Tab) -> None:
        """The window's canvas size at its DPR × the HUD's zoom (BR-2's
        `viewportFor`), so frames map one to one onto its pixels."""
        cdp = await self._ensure_cdp(tab)
        if cdp is None:
            return
        width, height, scale = self._view(tab)
        try:
            await cdp.send("Emulation.setDeviceMetricsOverride", {
                "width": width, "height": height, "deviceScaleFactor": scale, "mobile": False})
        except Exception as exc:  # noqa: BLE001
            LOG.info("browser tab %s: viewport not changed (%s)", tab.id, type(exc).__name__)
            return
        await self._sync_screencast(tab)

    def _frame(self, tab: _Tab, cdp, params) -> None:
        """A screencast frame: acked to Chrome at once (it keeps capturing;
        the window's own acks decide what is sent), then offered to the
        stream, which keeps only the newest waiting one."""
        if self._tabs.get(tab.id) is not tab or self._stopped:
            return
        params = params or {}
        session_id = params.get("sessionId")
        if session_id is not None:
            asyncio.ensure_future(self._quietly(cdp.send("Page.screencastFrameAck",
                                                         {"sessionId": session_id})))
        data = params.get("data")
        if not isinstance(data, str) or tab.stream is None:
            return
        try:
            jpeg = base64.b64decode(data, validate=True)
        except ValueError:
            return
        meta = params.get("metadata") or {}

        def size(name):
            value = meta.get(name)
            return round(value) if isinstance(value, (int, float)) and value == value else 0

        tab.stream.offer_frame(jpeg, size("deviceWidth"), size("deviceHeight"))

    # -- input -------------------------------------------------------------------------------

    async def _pump(self, tab: _Tab) -> None:
        """The tab's input, one CDP event at a time, in order. The hold is
        asked again immediately before each event is sent (amendment H3,
        "between every CDP event"); a hold drops what is queued too, and
        nothing dropped is ever sent later."""
        stream = tab.stream
        while True:
            await tab.wakeup.wait()
            tab.wakeup.clear()
            while True:
                item = stream.take()
                if item is None:
                    break
                seq, gen, msg = item
                if self.input_held():
                    stream.hold_through()
                    continue
                if not stream.deliverable(seq, gen):
                    continue
                try:
                    await self._deliver(tab, msg.method, msg.params)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - one event lost, never the pump
                    LOG.info("browser tab %s: an input event was not delivered (%s)", tab.id,
                             type(exc).__name__)

    @staticmethod
    def _let_go(params: dict) -> dict:
        """A pointer move with no button down: Blink's own reset for a press
        whose release it never got (it drops the press and its capture, and
        dispatches no `mouseup` and no `click`)."""
        return {"type": "mouseMoved", "x": params.get("x", -1), "y": params.get("y", -1),
                "modifiers": 0, "button": "none", "buttons": 0, "clickCount": 0,
                "pointerType": "mouse"}

    @_tab_activity
    async def _deliver(self, tab: _Tab, method: str, params: dict) -> None:
        page, page_identity, document = tab.page, tab.page_identity, tab.document_generation
        stream, attachment, held_at_entry = tab.stream, tab.stream.gen, tab.stream.hold_count
        instance = self._instances.get(tab.profile)
        def same_document():
            return (not self._stopped and self._tabs.get(tab.id) is tab
                    and tab.page is page and tab.page_identity == page_identity
                    and tab.document_generation == document and tab.stream is stream
                    and stream.gen == attachment and stream.hold_count == held_at_entry
                    and self._instances.get(tab.profile) is instance
                    and instance is not None and not instance.closing and not instance.restoring)
        cdp = await self._ensure_cdp(tab)
        if cdp is None or not same_document():
            return
        if self.input_held():
            tab.stream.hold_through()
            return
        # A dialog, or an earlier event Chrome has not answered within
        # DELIVER_WAIT_S (the pump only gets here after that wait), drops this
        # one: nothing banked behind a stuck page lands later, perhaps under a
        # card. Judged here, never on arrival, where every event of an
        # ordinary burst finds its predecessor still in flight (PR #77 delta).
        if tab.dialogs or self._auth.blocked(tab) or (tab.input_task is not None and not tab.input_task.done()):
            tab.stream.drop_blocked_input()
            return
        hold_count = tab.stream.hold_count
        key = method == "Input.dispatchKeyEvent"
        code = params.get("code", "") if key else None
        if key:
            pressed_at = tab.keys_pressed.get(code)
            if params["type"] in ("keyUp", "char") or params.get("autoRepeat", False):
                if pressed_at is not None and pressed_at != hold_count:
                    # Keep the cut-off mark: duplicate ups/chars cannot finish
                    # the old press either. A fresh down replaces it.
                    return
            if params["type"] == "keyUp":
                tab.keys_pressed.pop(code, None)
        mouse = method == "Input.dispatchMouseEvent"
        button = params.get("button") if mouse else None
        if mouse and params["type"] == "mouseReleased" and button in tab.pressed:
            # **A press a card cut off is never a click** (BR-2's HeldButtons
            # rule, made the daemon's): its release, and any release off the
            # page (BR-2's OFF_PAGE), is turned into a let-go. Chrome fires
            # `click` on the common ancestor of the press and the release —
            # `<html>` for a release at (-1, -1) — whatever `clickCount` says
            # (measured, Chrome 155), so a real release there is a click.
            if tab.pressed[button] != tab.stream.hold_count or params["x"] < 0 or params["y"] < 0:
                del tab.pressed[button]
                params = self._let_go(params)
        # Record before yielding: a card may arrive during Chrome's handler.
        if key and params["type"] in ("keyDown", "rawKeyDown"):
            tab.keys_pressed[code] = hold_count
        if mouse and button not in (None, "none"):
            if params["type"] == "mousePressed":
                tab.pressed[button] = hold_count
            elif params["type"] == "mouseReleased":
                tab.pressed.pop(button, None)
        # Timeout leaves the call alive, but no later input may follow it
        # until Chrome answers. Input arriving meanwhile is dropped.
        auth_epoch = self._auth.input_epoch(tab)
        async def send_current_auth():
            if (same_document() and not self.input_held()
                    and self._auth.input_current(tab, auth_epoch)):
                await cdp.send(method, params)
        task = asyncio.ensure_future(self._activity_run(tab, send_current_auth))
        tab.input_task = task
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        await asyncio.wait({task}, timeout=DELIVER_WAIT_S)

    async def _release_pressed(self, tab: _Tab) -> None:
        """Let go of every button the page holds, never as a click."""
        tab.pressed.clear()
        await self._deliver(tab, "Input.dispatchMouseEvent", self._let_go({}))

    @_tab_activity
    async def _selection(self, tab: _Tab, gen: int) -> None:
        """Ctrl+C: the page's selection, read in an isolated world (§8.7)."""
        cdp = await self._ensure_cdp(tab)
        text = ""
        try:
            tree = await cdp.send("Page.getFrameTree")
            world = await cdp.send("Page.createIsolatedWorld", {
                "frameId": tree["frameTree"]["frame"]["id"], "worldName": "jarvis-copy"})
            result = await cdp.send("Runtime.evaluate", {
                "expression": SELECTION_JS, "contextId": world["executionContextId"],
                "returnByValue": True})
            value = (result.get("result") or {}).get("value")
            text = value[:SELECTION_MAX] if isinstance(value, str) else ""
        except Exception as exc:  # noqa: BLE001
            LOG.info("browser tab %s: selection not read (%s)", tab.id, type(exc).__name__)
        tab.stream.send({"t": "selection", "text": text}, gen=gen)

    # -- dialogs and file choosers ---------------------------------------------------------------

    def _dialog(self, tab: _Tab, dialog) -> None:
        """alert, confirm, prompt or beforeunload: told to the tab's window
        and answered by it, or dismissed after DIALOG_TIMEOUT_S. With no
        window showing the tab, nobody could answer: dismissed at once."""
        if self._tabs.get(tab.id) is not tab or self._stopped:
            return
        dialog_id = self._new_id(tab.dialogs)
        token = self.page_hold(tab, "dialog:" + dialog_id)
        if token is None:
            self._attention_cleanup.refuse(tab, "dialog", dialog)
            return
        tab.dialogs[dialog_id] = dialog
        kind = dialog.type if dialog.type in DIALOG_KINDS else "alert"
        tab.stream.remember(f"dialog:{dialog_id}", {
            "t": "dialog", "id": dialog_id, "kind": kind,
            "message": _clean(dialog.message, DIALOG_TEXT_CAP),
            "default": _clean(dialog.default_value, DIALOG_TEXT_CAP) if kind == "prompt" else ""})
        tab.timers[dialog_id] = asyncio.get_running_loop().call_later(
            DIALOG_TIMEOUT_S if tab.stream.attached() is not None else 0,
            lambda: asyncio.ensure_future(self._quietly(self._close_dialog(tab, dialog_id, None, "timeout"))))

    @_tab_activity
    async def _close_dialog(self, tab: _Tab, dialog_id: str, answer, by: str) -> None:
        if self.input_held():
            if by == "owner":
                tab.stream.hold_through()
            elif dialog_id in tab.dialogs:
                tab.timers[dialog_id] = asyncio.get_running_loop().call_later(
                    0.1, lambda: asyncio.ensure_future(
                        self._close_dialog(tab, dialog_id, answer, by)))
            return
        token = tab.activity_holds.get("dialog:" + dialog_id)
        dialog = tab.dialogs.pop(dialog_id, None)
        if dialog is None:
            return
        timer = tab.timers.pop(dialog_id, None)
        if timer is not None:
            timer.cancel()
        tab.stream.remember(f"dialog:{dialog_id}", None)
        try:
            if answer is not None and answer[0]:
                if dialog.type == "prompt":
                    await dialog.accept(answer[1] or "")
                else:
                    await dialog.accept()
            else:
                await dialog.dismiss()
        except Exception:  # noqa: BLE001 - a page that went away has no dialog
            pass
        finally:
            self.release_page_hold(tab, "dialog:" + dialog_id, token)
        tab.stream.send({"t": "dialog_closed", "id": dialog_id, "by": by})

    def _chooser(self, tab: _Tab, chooser) -> None:
        """A page asked for files: the tab's window is told, and only the
        owner-only files route answers (`set_files`). Unanswered, nothing
        is uploaded."""
        if self._tabs.get(tab.id) is not tab or self._stopped:
            return
        if tab.stream is None or tab.stream.attached() is None:
            return
        self.release_page_hold(tab, "chooser", tab.activity_holds.get("chooser"))
        token = self.page_hold(tab, "chooser")
        if token is None:
            return
        chooser_id = secrets.token_hex(4)
        tab.chooser = (chooser_id, chooser, asyncio.get_running_loop().time())
        old = tab.timers.pop("chooser", None)
        if old is not None:
            old.cancel()
        def expire():
            if self._tabs.get(tab.id) is tab and tab.chooser is not None and tab.chooser[0] == chooser_id:
                tab.chooser = None
                tab.stream.remember("chooser", None)
                self.release_page_hold(tab, "chooser", token)
        tab.timers["chooser"] = asyncio.get_running_loop().call_later(CHOOSER_TTL_S, expire)
        try:
            multiple = bool(chooser.is_multiple())
        except Exception:  # noqa: BLE001
            multiple = False
        tab.stream.remember("chooser", {"t": "filechooser", "id": chooser_id, "multiple": multiple})

    def set_files(self, tab_id, chooser_id, payloads: list[dict]) -> dict:
        """Answer the tab's open file chooser with files the owner chose
        (`files.upload_payloads` judged them); an empty list cancels."""
        if not self._running():
            raise BrowserRefused(404, "no such tab")
        self._refuse_if_held()
        return self._call(self._set_files(tab_id, chooser_id, payloads), CALL_TIMEOUT_S)

    @_tab_activity
    async def _set_files(self, tab_id, chooser_id, payloads) -> dict:
        tab = self._tab(tab_id)
        entry = tab.chooser
        loop = asyncio.get_running_loop()
        if entry is None or entry[0] != chooser_id or loop.time() - entry[2] > CHOOSER_TTL_S:
            raise BrowserRefused(409, "that file chooser is no longer open")
        self._refuse_if_held()
        token = tab.activity_holds.get("chooser")
        tab.chooser = None
        timer = tab.timers.pop("chooser", None)
        if timer is not None:
            timer.cancel()
        tab.stream.remember("chooser", None)
        tab.stream.owner_input()
        try:
            if payloads:
                chooser = entry[1]
                if not chooser.is_multiple() and len(payloads) > 1:
                    raise BrowserRefused(400, "this file chooser takes one file")
                await chooser.set_files(payloads, timeout=CALL_TIMEOUT_S * 1000)
            tab.stream.send({"t": "filechooser_closed", "id": chooser_id, "files": len(payloads)})
            return {"tab_id": tab.id, "chooser": chooser_id, "files": len(payloads)}
        finally:
            self.release_page_hold(tab, "chooser", token)

    # -- downloads -------------------------------------------------------------------------------

    def _start_download(self, tab: _Tab, download) -> None:
        if self._tabs.get(tab.id) is not tab or self._stopped:
            return
        key = "download:" + secrets.token_hex(4)
        token = self.page_hold(tab, key)
        if token is None:
            self._attention_cleanup.refuse(tab, "download", download)
            return
        drive=getattr(self._daemon,"browser_drive",None)
        item=drive.downloads.started(tab.id,getattr(download,"url",None)) if drive is not None else None
        if item is not None:item.done=asyncio.Event()
        task = asyncio.create_task(self._download(tab, download, item))
        self._download_tasks[task] = tab
        def finished(done):
            self._download_tasks.pop(done, None)
            self.release_page_hold(tab, key, token)
        task.add_done_callback(finished)

    async def _settle_downloads(self, profile: str, ctx: str | None = None) -> None:
        tasks = [task for task, tab in self._download_tasks.items()
                 if tab.profile == profile and (ctx is None or tab.ctx == ctx)]
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=CLOSE_TIMEOUT_S + 10)
            if pending:
                raise BrowserRefused(409, "downloads are still closing; try again shortly")
            # Consume outcomes without cancelling an executor-backed writer.
            await asyncio.gather(*done, return_exceptions=True)

    def wait_action_downloads(self, token):
        async def wait():
            # Wait only for attributed saves, never another owner tab. A late
            # or unfinished save stays held and does not acquire an approval.
            events=[item.done.wait() for item in token.downloads if item.done is not None and not item.done.is_set()]
            if events:
                try:await asyncio.wait_for(asyncio.gather(*events),10)
                except asyncio.TimeoutError:pass
        self._call(wait(),12)

    async def _download(self, tab: _Tab, download, item=None) -> None:
        """A download is saved where only the owner reaches it (files.py) and
        its tab's window told; an executable one is held."""
        profile = PRIVATE if tab.profile == PRIVATE else tab.profile
        name = _files.clean_name(getattr(download, "suggested_filename", ""))
        download_id = secrets.token_hex(4)
        with self._lock:
            self._downloading += 1
        notice = {"t": "download", "id": download_id, "name": name}
        tab.stream.send({**notice, "state": "started"})
        try:
            source = await download.path()
            if self._tabs.get(tab.id) is not tab or tab.profile not in self._instances:
                return
            from functools import partial
            row = await asyncio.get_running_loop().run_in_executor(
                None, partial(_files.save, str(source), profile, name, download_id, tab.ctx,agent_held=item is not None))
            if item is not None:
                drive=getattr(self._daemon,"browser_drive",None)
                if drive is not None:drive.downloads.completed(item,row)
        except Exception as exc:  # noqa: BLE001 - a failed or cancelled download
            LOG.info("Browser download failed (%s)", type(exc).__name__)
            tab.stream.send({**notice, "state": "failed"})
            return
        finally:
            if item is not None and item.done is not None:item.done.set()
            with self._lock:
                self._downloading -= 1
            await self._quietly(download.delete())
            if profile == PRIVATE and not any(t.ctx == tab.ctx for t in self._tabs.values()):
                _remove_tree(config.BROWSER_DIR / "downloads" / PRIVATE / tab.ctx)
        LOG.info("Browser saved a download%s", " (held: executable)" if row["held"] else "")
        tab.stream.send({"t": "download", "id": row["id"], "name": row["name"], "size": row["size"],
                         "state": "held" if row["held"] else "done", "held": row["held"]})

    def download_released(self, tab_id, row):
        tab=self._tabs.get(tab_id)
        if tab is not None:
            tab.stream.send({"t":"download","id":row["id"],"name":row["name"],"size":row["size"],"state":"done","held":False})

    def downloads(self) -> dict:
        rows = _files.listing()
        drive=getattr(self._daemon,"browser_drive",None)
        if drive is not None:
            for row in rows:
                if row["held"] and drive.downloads.listed_release(row["id"]):row["held"]=False
        with self._lock:
            live = self._downloading
        return {"downloads": rows, "in_progress": live}

    # -- tabs ------------------------------------------------------------------------------------

    def activate(self, tab_id) -> dict:
        """Switch to a tab: brought to the front in its Chrome."""
        if not self._running():
            raise BrowserRefused(404, "no such tab")
        return self._call(self._activate(tab_id), CLOSE_TIMEOUT_S + 5)

    @_tab_activity
    async def _activate(self, tab_id) -> dict:
        tab = self._tab(tab_id)
        await asyncio.wait_for(tab.page.bring_to_front(), CLOSE_TIMEOUT_S)
        return {"tab_id": tab.id, "active": True}

    # -- the agent channel (amendment §B.9; BR-9b drives it) ------------------------------------

    def owner_state(self, tab_id) -> dict:
        """What BR-9b's gate reads (H3, H4): the hold, whether the owner
        paused the tab, and how long since the owner's last input to it."""
        tab = self.require_tab(tab_id)
        return {**tab.stream.owner_state(), "held": self.input_held(),
                "auth": self._auth.blocked(tab),
                "depressed": bool(tab.pressed or tab.keys_pressed),
                "input_busy": bool(tab.input_task is not None and not tab.input_task.done())}

    # -- pane identity (BR-3b/BR-5; in-process only) --------------------------------------

    def _publish_pane_tabs(self) -> None:
        """Owner-browser loop publishes primitives; readers never submit CDP.

        Lock order: build the rows on this loop, then take only _lock to swap
        the cache. No registry/DevServers callbacks and no await under _lock.
        """
        rows = {
            tab.id: {"tab_id": tab.id, "profile": tab.profile, "ctx": tab.ctx,
                     "url": (tab.location or ("", ""))[0],
                     "page_identity": tab.page_identity,
                     "instance_generation": self._instances[tab.profile].generation,
                     "document_generation": tab.document_generation}
            for tab in self._tabs.values() if tab.pane_ready and tab.page_identity > 0
            and tab.profile in self._instances and not self._instances[tab.profile].closing
            and not self._instances[tab.profile].restoring
        }
        with self._lock:
            self._pane_tabs = rows
            self._auth_pending = {
                tab_id: session.navigation.id for tab_id, session in self._auth.sessions.items()
                if self._auth.current(session) and session.navigation is not None
                and session.prompt is not None}

    def pane_tab(self, tab_id: str) -> dict | None:
        """A copied navigation identity, safe on the CDP loop or caller thread.

        Never _call/_listing/title()/CDP: BR5 revalidates from inside capture,
        and submitting back to that same event loop would deadlock. No URL or
        source is added to /status, the bus or a log by this accessor.
        """
        if not isinstance(tab_id, str) or TAB_ID.fullmatch(tab_id) is None:
            return None
        with self._lock:
            row = None if self._stopped else self._pane_tabs.get(tab_id)
            return dict(row) if row is not None else None

    # -- status -------------------------------------------------------------------------

    def _publish_snapshot(self) -> None:
        self._publish_pane_tabs()
        instances = [{"profile": p, "tabs": sum(t.profile == p for t in self._tabs.values()),
                      "contexts": len(i.private_contexts) if p == PRIVATE else None,
                      "sound": self._sound.instance_status(i)}
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
            opened = len(self._port_owners)
            downloading = self._downloading
            popups_refused = self._popups_refused
        gate = self._gate.stats() if self._gate is not None and running else None
        settings = self._settings.snapshot()
        streams = sum(1 for t in list(self._tabs.values())
                      if t.stream is not None and t.stream.attached() is not None)
        return {"available": CHROME_PATH.exists(), "running": running,
                "version": snapshot.get("version"), "instances": snapshot["instances"],
                "sound": self._sound.status(),
                "tabs": snapshot["tabs"], "tab_cap": max(1, config.BROWSER_TAB_CAP),
                "lan": settings["lan"], "local_servers": settings["local_servers"],
                "profile_cap": 8, "instance_cap": PROFILE_INSTANCES_MAX,
                "sync": {"password_store": "basic", "source": "launch_argument"},
                # How many loopback ports the owner opened — never which.
                "loopback_opened": opened, "landing_guard": guarded, "gate": gate,
                # BR-1b: counts only — windows streaming a tab, downloads under
                # way, pop-ups refused past the cap.
                "streams": streams, "downloading": downloading, "popups_refused": popups_refused}
