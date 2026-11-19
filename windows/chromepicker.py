"""Chrome's profile picker, read and chosen without coordinates or keys.

Everything above `WindowsChromePicker` is OS-independent and tested on Linux.
The Windows half answers four questions about one window handle and acts only
through UI Automation's Invoke pattern on one profile card:

    identity(hwnd)      -> WindowIdentity | None   (Win32 only, no UIA)
    foreground()        -> hwnd | None
    process_windows(pid)-> frozenset of visible top-level hwnds
    cards(identity)     -> list[Card] | None        (None: not the trusted picker)
    invoke(card)                                     (Invoke on that element only)

What makes a window "the picker", measured on Chrome 157 (a disposable
profile directory on a hidden desktop, `tests/voice_chrome_picker_real.py`):

* a visible top-level `Chrome_WidgetWin_1` owned by `chrome.exe`, whose
  AppUserModelID is the *system profile's*: `Chrome.SystemProfile`, or
  `Chrome.<directory>.SystemProfile` for another user-data directory. A browser
  window carries the profile's own ID, so no tab can wear this one;
* a native views `ProfilePickerView` reached without entering web content, and
  under it exactly one renderer host whose document is `chrome://profile-picker`;
* profile cards: `mainView > profileCardContainer > profileCardButton`
  (Invoke) beside the card's name field (`input`, Value = the profile name).
  The button's own name must contain that label, so a rename in progress or a
  stray field never stands in for the card the owner sees.

Labels, titles and page text alone never qualify a window. Nothing here reads
profile folders or Chrome's files.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import ntpath
import queue
import threading
import unicodedata

PICKER_CLASS = "Chrome_WidgetWin_1"
PICKER_URL = "chrome://profile-picker"
BROWSER_TITLE_SUFFIX = " - google chrome"
MAX_CARDS = 24
MAX_LABEL = 80
# UIA control type ids (UIAutomationClient.h).
BUTTON, DOCUMENT, EDIT, GROUP, PANE = 50000, 50030, 50004, 50026, 50033


def normal(text):
    return " ".join("".join(c if c.isalnum() else " " for c in (text or "").casefold()).split())


def label_key(text):
    """Case and width folded, whitespace collapsed; punctuation stays significant."""
    return " ".join(unicodedata.normalize("NFKC", text or "").casefold().split())


def clean_label(text):
    """Collapse whitespace; refuse control, format and private-use characters."""
    if not isinstance(text, str):
        return ""
    text = " ".join(text.split())
    if not text or len(text) > MAX_LABEL:
        return ""
    for ch in text:
        code = ord(ch)
        if code < 0x20 or 0x7F <= code < 0xA0 or 0xE000 <= code <= 0xF8FF or 0x200B <= code <= 0x200F \
                or 0x202A <= code <= 0x202E or 0x2066 <= code <= 0x2069 or code == 0xFEFF:
            return ""
    return text


@dataclass(frozen=True)
class WindowIdentity:
    hwnd: int
    pid: int
    class_name: str
    exe: str
    aumid: str | None
    title: str
    visible: bool


@dataclass(frozen=True)
class Card:
    label: str
    button_name: str
    key: tuple
    enabled: bool
    offscreen: bool
    invokable: bool
    element: object = field(default=None, compare=False, repr=False)
    # Checked on the UIA thread immediately before Invoke; False withdraws the click.
    allowed: object = field(default=None, compare=False, repr=False)


def _aumid_parts(aumid):
    if not isinstance(aumid, str) or len(aumid) > 128:
        return None
    parts = aumid.split(".")
    if not all(part and part.isascii() and part.isalnum() for part in parts):
        return None
    return parts


def is_picker_aumid(aumid):
    parts = _aumid_parts(aumid)
    return bool(parts) and 2 <= len(parts) <= 3 and parts[0] == "Chrome" and parts[-1] == "SystemProfile"


def is_browser_aumid(aumid, picker_aumid):
    """A browser profile's ID from the same kind of install as the picker.

    Chrome shortens the middle (user-data-directory) component to fit its length
    limit, and differently for `SystemProfile` than for `Profile1` (measured:
    `Chrome.jarvispickerpro8u.SystemProfile` beside
    `Chrome.jarvispickerpron46xg8u.Profile1`), so components cannot be compared.
    The install is bound by the process instead: one browser process per
    user-data directory, and the new window must belong to the picker's.
    """
    parts, picker = _aumid_parts(aumid), _aumid_parts(picker_aumid)
    if not parts or not picker or parts[0] != "Chrome" or parts[-1] == "SystemProfile":
        return False
    # The default directory: `Chrome` or `Chrome.Profile1`; another directory: one more.
    return len(picker) - 1 <= len(parts) <= len(picker)


def _is_chrome(identity):
    return (isinstance(identity, WindowIdentity) and isinstance(identity.exe, str)
            and ntpath.basename(identity.exe).casefold() == "chrome.exe"
            and identity.class_name == PICKER_CLASS and identity.visible is True)


def is_picker_identity(identity):
    return _is_chrome(identity) and is_picker_aumid(identity.aumid)


def same_picker(now, then):
    """The window recorded at launch, still the trusted picker, still visible."""
    return (is_picker_identity(now) and is_picker_identity(then) and now.hwnd == then.hwnd
            and now.pid == then.pid and now.aumid == then.aumid)


def is_profile_window(identity, picker, before):
    """A new browser window of the picker's own Chrome process, for some profile."""
    if not _is_chrome(identity) or not is_picker_identity(picker):
        return False
    if identity.pid != picker.pid or identity.hwnd == picker.hwnd or identity.hwnd in before:
        return False
    if not isinstance(identity.title, str) or not identity.title.strip().casefold().endswith(BROWSER_TITLE_SUFFIX):
        return False
    return is_browser_aumid(identity.aumid, picker.aumid)


def card_ok(card):
    if not isinstance(card, Card) or not card.invokable or not card.enabled or card.offscreen:
        return False
    label = normal(card.label)
    return bool(clean_label(card.label)) and bool(label) and f" {label} " in f" {normal(card.button_name)} "


def options(cards):
    """Labels as shown, in order. Duplicates stay so the caller sees the ambiguity."""
    return tuple(card.label for card in cards or () if card_ok(card))


def unique_card(cards, display_name):
    """The one usable card with this label. Every card with the same label, usable
    or not, makes the name ambiguous; a spoken name may end in sentence punctuation."""
    said = label_key(display_name)
    wanted = {said, said.rstrip(".!?,;: ")} - {""}
    if not wanted:
        return None
    named = [card for card in cards or () if isinstance(card, Card) and label_key(card.label) in wanted]
    return named[0] if len(named) == 1 and card_ok(named[0]) else None


# ---------------------------------------------------------------------------
# Tree reading, over a duck-typed node adapter so forged trees test on Linux.
#   tree.children(node) -> list[node]
#   tree.props(node)    -> dict(control, name, aid, cls, fw, enabled, offscreen,
#                               invoke: bool, key: tuple)
#   tree.value(node)    -> str | None   (ValuePattern)

def _find_picker_view(tree, root, *, depth=7, budget=80):
    """Native views path only: never descend into web content."""
    found, frontier, seen = [], [(root, 0)], 0
    while frontier:
        node, level = frontier.pop(0)
        for child in tree.children(node):
            seen += 1
            if seen > budget:
                return None
            p = tree.props(child)
            if p["control"] == DOCUMENT or p["cls"] == "Chrome_RenderWidgetHostHWND":
                continue
            if p["cls"] == "ProfilePickerView":
                if p["control"] == PANE and p["fw"] == "Chrome":
                    found.append(child)
                continue
            if level + 1 < depth and p["fw"] in ("Chrome", "Win32"):
                frontier.append((child, level + 1))
    return found[0] if len(found) == 1 else None


def _only(tree, nodes, **want):
    hits = [n for n in nodes if all(tree.props(n).get(k) == v for k, v in want.items())]
    return hits[0] if len(hits) == 1 else None


def _find_edit(tree, node, depth=6):
    hits = []

    def walk(n, level):
        if level > depth or len(hits) > 1:
            return
        for child in tree.children(n):
            p = tree.props(child)
            if p["control"] == EDIT and p["aid"] == "input":
                hits.append(child)
            walk(child, level + 1)

    walk(node, 0)
    return hits[0] if len(hits) == 1 else None


def read_cards(tree, root):
    """Profile cards of a trusted picker document, or None when anything is off."""
    view = _find_picker_view(tree, root)
    if view is None:
        return None
    host = _only(tree, tree.children(view), cls="Chrome_RenderWidgetHostHWND")
    if host is None:
        return None
    doc = _only(tree, tree.children(host), control=DOCUMENT, aid="RootWebArea")
    if doc is None or (tree.value(doc) or "").partition("?")[0].partition("#")[0].rstrip("/") != PICKER_URL:
        return None
    main = _only(tree, tree.children(doc), aid="mainView")
    if main is None:
        return None
    cards = []
    for box in tree.children(main):
        if tree.props(box)["aid"] != "profileCardContainer":
            continue
        if len(cards) >= MAX_CARDS:
            return None
        kids = tree.children(box)
        button = _only(tree, kids, control=BUTTON, aid="profileCardButton")
        wrapper = _only(tree, kids, aid="profileNameInputWrapper")
        edit = _find_edit(tree, wrapper) if wrapper is not None else None
        if button is None or edit is None:
            return None  # mid-render or unfamiliar: read again, never guess
        p = tree.props(button)
        cards.append(Card(label=clean_label(tree.value(edit) or ""), button_name=p["name"] or "",
                          key=tuple(p["key"] or ()), enabled=bool(p["enabled"]),
                          offscreen=bool(p["offscreen"]), invokable=bool(p["invoke"]), element=button))
    return cards


# ---------------------------------------------------------------------------
# Windows.

class PickerUnavailable(RuntimeError):
    pass


class ClickSent(PickerUnavailable):
    """Invoke was already called and cannot be withdrawn; its outcome is unknown."""


class _UiaWorker:
    """One thread owns the COM apartment and the UIA client; every call is bounded.

    A Chrome that stops answering cannot hold the voice worker: a call that
    overruns is abandoned. At most MAX_THREADS apartments may be alive,
    including retired threads waiting for a blocked COM call to return.

    `close` is permanent and never waits: no new job is accepted, a queued job
    never runs, and a job already running sees `closed` at its next check. The
    `gate` lock guards only that flag, so a job can commit an irreversible step
    atomically with respect to close (`commit`); nothing blocking runs under it.
    """

    MAX_THREADS = 3

    def __init__(self, thread_setup=None):
        self.thread_setup = thread_setup
        self.queue = None
        self.started = 0
        self.threads = []
        self.closed = False
        self.lock = threading.Lock()  # queue, generation and threads; held for startup
        self.gate = threading.Lock()  # `closed` only; never held around a wait or a COM call

    def _start(self):
        self.threads = [thread for thread in self.threads if thread.is_alive()]
        if self.closed or len(self.threads) >= self.MAX_THREADS:
            raise PickerUnavailable("UI Automation stopped answering")
        self.started += 1
        self.queue = queue.Queue()
        ready, box = threading.Event(), {}
        thread = threading.Thread(target=self._run, args=(self.queue, ready, box),
                                  name="voice-chrome-picker", daemon=True)
        try:
            thread.start()
        except RuntimeError as exc:
            self.queue = None
            raise PickerUnavailable("UI Automation thread unavailable") from exc
        self.threads.append(thread)
        if not ready.wait(10) or "error" in box:
            self._retire(self.queue)
            raise PickerUnavailable("UI Automation unavailable")

    def _run(self, jobs, ready, box):
        try:
            if self.thread_setup is not None:
                self.thread_setup()  # the probe moves this thread to its hidden desktop
            import comtypes
            import comtypes.client
            try:
                comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
            except OSError:
                pass  # already initialised by the comtypes import on this thread
            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as UIA
            try:
                client = comtypes.client.CreateObject(UIA.CUIAutomation8, interface=UIA.IUIAutomation2)
                client.ConnectionTimeout, client.TransactionTimeout = 1500, 2000
            except (OSError, AttributeError, comtypes.COMError):
                client = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
            box["uia"] = (client, UIA)
        except Exception as exc:  # noqa: BLE001  (reported to the caller as unavailable)
            box["error"] = exc
            ready.set()
            return
        ready.set()
        try:
            while (job := jobs.get()) is not None:  # None retires this thread
                fn, result, done = job
                try:
                    if self.closed:  # queued before close: never runs
                        raise PickerUnavailable("the picker was closed")
                    result["value"] = fn(*box["uia"])
                except Exception as exc:  # noqa: BLE001
                    result["error"] = exc
                finally:
                    done.set()
        finally:
            box.clear()  # drop the UIA client on its own thread

    def _retire(self, jobs):
        if self.queue is jobs:
            self.queue = None
        if jobs is not None:
            jobs.put(None)

    def commit(self, step):
        """Run `step()` (quick, non-blocking) unless closed; close cannot interleave.
        Returns False, without running it, once the worker is closed."""
        with self.gate:
            if self.closed:
                return False
            step()
            return True

    def call(self, fn, timeout, *, expected_generation=None):
        """Return the executing apartment's generation and the job result."""
        with self.lock:
            if self.closed:
                raise PickerUnavailable("the picker was closed")
            if expected_generation is not None and (self.queue is None or self.started != expected_generation):
                raise PickerUnavailable("card was read by a UI Automation thread that is gone")
            if self.queue is None:
                self._start()
            jobs, generation = self.queue, self.started
            result, done = {}, threading.Event()
            if not self.commit(lambda: jobs.put((fn, result, done))):
                self._retire(jobs)  # closed while this call was starting the apartment
                raise PickerUnavailable("the picker was closed")
        if not done.wait(timeout):
            with self.lock:
                self._retire(jobs)  # abandon the stuck thread; it exits once unstuck
            raise PickerUnavailable("UI Automation call timed out")
        if "error" in result:
            raise result["error"]
        return generation, result["value"]

    def close(self):
        """Permanent, and never waits for `lock` (a startup may hold it for seconds).
        The current apartment is told to exit; `call` retires one that a concurrent
        startup creates, and threads that started earlier were retired already."""
        with self.gate:  # ordered with every job submission: none lands behind this None
            self.closed = True
            jobs = self.queue
            if jobs is not None:
                jobs.put(None)


class _ComTree:
    """`read_cards` adapter over raw IUIAutomationElement pointers."""

    def __init__(self, client, UIA):
        self.walker, self.UIA = client.RawViewWalker, UIA
        self.cache, self.visited = {}, 0

    def children(self, node):
        out, child = [], self.walker.GetFirstChildElement(node)
        while child and len(out) < 64:
            self.visited += 1
            if self.visited > 1500:
                raise PickerUnavailable("picker tree too large")
            out.append(child)
            child = self.walker.GetNextSiblingElement(child)
        return out

    def props(self, node):
        key = id(node)
        if key not in self.cache:
            invoke = node.GetCurrentPattern(10000)  # UIA_InvokePatternId
            self.cache[key] = (node, dict(
                control=node.CurrentControlType, name=node.CurrentName or "",
                aid=node.CurrentAutomationId or "", cls=node.CurrentClassName or "",
                fw=node.CurrentFrameworkId or "", enabled=bool(node.CurrentIsEnabled),
                offscreen=bool(node.CurrentIsOffscreen), invoke=bool(invoke),
                key=tuple(node.GetRuntimeId() or ())))
        return self.cache[key][1]

    def value(self, node):
        pattern = node.GetCurrentPattern(10002)  # UIA_ValuePatternId
        if not pattern:
            return None
        return pattern.QueryInterface(self.UIA.IUIAutomationValuePattern).CurrentValue


class WindowsChromePicker:
    def __init__(self, *, foreground=None, thread_setup=None, read_timeout=4.0, invoke_timeout=5.0):
        import ctypes
        from ctypes import wintypes as w
        self.ctypes, self.w = ctypes, w
        self.user32, self.kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        u, k = self.user32, self.kernel32
        u.GetForegroundWindow.restype = w.HWND
        for name in ("IsWindow", "IsWindowVisible"):
            getattr(u, name).argtypes = [w.HWND]
        u.GetAncestor.argtypes, u.GetAncestor.restype = [w.HWND, w.UINT], w.HWND
        u.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
        u.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
        u.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
        u.GetWindowThreadProcessId.restype = w.DWORD
        k.OpenProcess.argtypes, k.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
        k.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
        k.CloseHandle.argtypes = [w.HANDLE]
        self.shell32, self.ole32 = ctypes.windll.shell32, ctypes.oledll.ole32
        self.shell32.SHGetPropertyStoreForWindow.argtypes = [w.HWND, ctypes.c_void_p, ctypes.c_void_p]
        self._foreground = foreground
        self.worker = _UiaWorker(thread_setup)
        self.read_timeout, self.invoke_timeout = read_timeout, invoke_timeout

    # -- Win32 ----------------------------------------------------------------
    def _aumid(self, hwnd):
        c, w = self.ctypes, self.w

        class GUID(c.Structure):
            _fields_ = [("a", w.DWORD), ("b", w.WORD), ("c", w.WORD), ("d", c.c_ubyte * 8)]

        class PROPERTYKEY(c.Structure):
            _fields_ = [("fmtid", GUID), ("pid", w.DWORD)]

        class PROPVARIANT(c.Structure):
            _fields_ = [("vt", w.USHORT), ("r1", w.USHORT), ("r2", w.USHORT), ("r3", w.USHORT),
                        ("p", c.c_void_p), ("p2", c.c_void_p)]

        def guid(text):
            g = GUID()
            self.ole32.CLSIDFromString(text, c.byref(g))
            return g

        store = c.c_void_p()
        iid = guid("{886d8eeb-8cf2-4446-8d02-cdba1dbdcf99}")  # IPropertyStore
        if self.shell32.SHGetPropertyStoreForWindow(hwnd, c.byref(iid), c.byref(store)) or not store:
            return None
        table = c.cast(c.cast(store, c.POINTER(c.c_void_p))[0], c.POINTER(c.c_void_p))
        get_value = c.WINFUNCTYPE(c.c_long, c.c_void_p, c.POINTER(PROPERTYKEY), c.POINTER(PROPVARIANT))(table[5])
        release = c.WINFUNCTYPE(c.c_ulong, c.c_void_p)(table[2])
        key = PROPERTYKEY(guid("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"), 5)  # PKEY_AppUserModel_ID
        value = PROPVARIANT()
        try:
            if get_value(store, c.byref(key), c.byref(value)) or value.vt != 31 or not value.p:  # VT_LPWSTR
                return None
            return c.wstring_at(value.p)[:129]
        finally:
            try:
                self.ole32.PropVariantClear(c.byref(value))
            except OSError:
                pass
            release(store)

    def _aumid_with_com(self, hwnd):
        # The property store is a COM object: initialise this thread for the
        # call if nobody has (an MTA thread answers RPC_E_CHANGED_MODE and works).
        com = self.ctypes.windll.ole32
        com.CoInitializeEx.argtypes = [self.ctypes.c_void_p, self.w.DWORD]
        com.CoInitializeEx.restype = self.ctypes.c_long
        started = com.CoInitializeEx(None, 0x2) in (0, 1)  # COINIT_APARTMENTTHREADED: S_OK, S_FALSE
        try:
            return self._aumid(hwnd)
        finally:
            if started:
                com.CoUninitialize()

    def identity(self, hwnd):
        c, w, u, k = self.ctypes, self.w, self.user32, self.kernel32
        if not hwnd or not u.IsWindow(hwnd) or u.GetAncestor(hwnd, 2) != hwnd:  # GA_ROOT
            return None
        name = c.create_unicode_buffer(64)
        u.GetClassNameW(hwnd, name, len(name))
        title = c.create_unicode_buffer(512)
        u.GetWindowTextW(hwnd, title, len(title))
        pid = w.DWORD()
        u.GetWindowThreadProcessId(hwnd, c.byref(pid))
        exe = ""
        process = k.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if process:
            try:
                path, length = c.create_unicode_buffer(32768), w.DWORD(32768)
                if k.QueryFullProcessImageNameW(process, 0, path, c.byref(length)):
                    exe = path.value
            finally:
                k.CloseHandle(process)
        try:
            aumid = self._aumid_with_com(hwnd)
        except OSError:
            aumid = None
        return WindowIdentity(int(hwnd), int(pid.value), name.value, exe, aumid, title.value,
                              bool(u.IsWindowVisible(hwnd)))

    def foreground(self):
        if self._foreground is not None:
            return self._foreground()
        hwnd = self.user32.GetForegroundWindow()
        return int(hwnd) if hwnd else None

    def process_windows(self, pid):
        c, w, u = self.ctypes, self.w, self.user32
        found = set()
        callback_type = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

        @callback_type
        def visit(hwnd, unused):
            owner = w.DWORD()
            u.GetWindowThreadProcessId(hwnd, c.byref(owner))
            if owner.value == pid and u.IsWindowVisible(hwnd):
                found.add(int(hwnd))
            return len(found) < 256

        u.EnumWindows.argtypes = [callback_type, w.LPARAM]
        u.EnumWindows(visit, 0)
        return frozenset(found)

    # -- UI Automation ----------------------------------------------------------
    def cards(self, identity):
        hwnd = identity.hwnd

        def read(client, UIA):
            tree = _ComTree(client, UIA)
            try:
                root = client.ElementFromHandle(hwnd)
                return read_cards(tree, root) if root else None
            finally:
                tree.cache.clear()  # release every visited element on this thread

        generation, cards = self.worker.call(read, self.read_timeout)
        return None if cards is None else [replace(c, element=(generation, c.element)) for c in cards]

    def invoke(self, card):
        """Invoke one card at most once. Raises PickerUnavailable when no click was
        sent (withdrawn, timed out first, unreadable) and ClickSent when Invoke was
        called but did not return cleanly in time.

        A call that times out is abandoned under a lock; the job re-checks that,
        `card.allowed` and the worker's close after every blocking COM call, so a
        job that resumes late, or after the picker was closed, never starts a click.
        The move to "sent" is committed under the worker's close gate: a close that
        lands first withdraws the click, one that lands after cannot unsend it."""
        generation, element = card.element
        allowed, lock, state = card.allowed, threading.Lock(), ["pending"]
        worker = self.worker

        def still(final=False):
            try:
                ok = allowed is None or allowed() is True  # owner callbacks: outside every lock
            except Exception:  # noqa: BLE001
                ok = False
            answer = []

            def decide():  # under the close gate, then the per-click lock: never the reverse
                with lock:
                    if state[0] == "pending" and ok and final:
                        state[0] = "sent"
                    answer.append(state[0] != "abandoned" and ok)

            return worker.commit(decide) and answer[0]

        def press(client, UIA):
            if not still():
                return "withdrawn"
            pattern = element.GetCurrentPattern(10000)
            if not still():
                return "withdrawn"
            if not pattern:
                raise PickerUnavailable("card cannot be invoked")
            action = pattern.QueryInterface(UIA.IUIAutomationInvokePattern)
            if not still(final=True):
                return "withdrawn"
            action.Invoke()
            return "sent"

        try:
            _, outcome = self.worker.call(press, self.invoke_timeout, expected_generation=generation)
        except BaseException:
            with lock:
                sent = state[0] == "sent"
                if not sent:
                    state[0] = "abandoned"
            if sent:
                raise ClickSent("the click was sent but UI Automation did not confirm it") from None
            raise
        if outcome != "sent":
            raise PickerUnavailable("the click was withdrawn before it was sent")

    def close(self):
        self.worker.close()
