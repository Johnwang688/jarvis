"""Physical Right Ctrl push-to-talk for Windows."""

from contextlib import contextmanager
from queue import Empty, SimpleQueue
import threading
import time


RIGHT_CTRL = 0xA3
HOLD_SECONDS = 120
_DOWN = (0x0100, 0x0104)
_UP = (0x0101, 0x0105)
_INJECTED = 0x02 | 0x10


def physical_event(key, flags, message):
    if flags & _INJECTED or message not in (*_DOWN, *_UP):
        return None
    return key, message in _DOWN


class Hold:
    def __init__(self, on_key):
        self._on_key = on_key
        self._pressed = set()
        self._until = None

    def end(self):
        if self._until is not None:
            self._until = None
            self._on_key(False)

    def tick(self, now):
        if self._until is not None and now >= self._until:
            self.end()

    def event(self, key, down, now):
        self.tick(now)
        if not down:
            self._pressed.discard(key)
            if key == RIGHT_CTRL:
                self.end()
        elif key not in self._pressed:
            self._pressed.add(key)
            if key != RIGHT_CTRL:
                self.end()
            elif len(self._pressed) == 1:
                self._until = now + HOLD_SECONDS
                self._on_key(True)


class WindowsHook:
    @contextmanager
    def open(self, enqueue):
        import ctypes as c
        from ctypes import wintypes as w

        class Key(c.Structure):
            _fields_ = [("key", w.DWORD), ("scan", w.DWORD), ("flags", w.DWORD),
                        ("time", w.DWORD), ("extra", c.c_size_t)]

        callback_type = c.WINFUNCTYPE(c.c_ssize_t, c.c_int, w.WPARAM, w.LPARAM)
        user = c.WinDLL("user32", use_last_error=True)
        kernel = c.WinDLL("kernel32", use_last_error=True)
        kernel.GetModuleHandleW.argtypes = (w.LPCWSTR,)
        kernel.GetModuleHandleW.restype = w.HMODULE
        user.SetWindowsHookExW.argtypes = (c.c_int, callback_type, w.HINSTANCE, w.DWORD)
        user.SetWindowsHookExW.restype = w.HANDLE
        user.UnhookWindowsHookEx.argtypes = (w.HANDLE,)
        user.CallNextHookEx.argtypes = (w.HANDLE, c.c_int, w.WPARAM, w.LPARAM)
        user.CallNextHookEx.restype = c.c_ssize_t
        user.MsgWaitForMultipleObjects.argtypes = (w.DWORD, c.c_void_p, w.BOOL, w.DWORD, w.DWORD)
        user.PeekMessageW.argtypes = (c.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT)
        user.TranslateMessage.argtypes = (c.POINTER(w.MSG),)
        user.DispatchMessageW.argtypes = (c.POINTER(w.MSG),)
        user.DispatchMessageW.restype = c.c_ssize_t

        @callback_type
        def callback(code, message, address):
            if code == 0:
                data = c.cast(address, c.POINTER(Key)).contents
                enqueue(data.key, data.flags, message)
            return user.CallNextHookEx(None, code, message, address)

        message = w.MSG()
        handle = user.SetWindowsHookExW(13, callback, kernel.GetModuleHandleW(None), 0)
        if not handle:
            raise OSError("Could not install the talk key hook.")

        def pump():
            user.MsgWaitForMultipleObjects(0, None, False, 20, 0x04FF)
            while user.PeekMessageW(c.byref(message), None, 0, 0, 1):
                user.TranslateMessage(c.byref(message))
                user.DispatchMessageW(c.byref(message))

        try:
            yield pump
        finally:
            user.UnhookWindowsHookEx(handle)


class TalkKey:
    def __init__(self, on_key, *, hook=None, on_error=None, clock=time.monotonic):
        self._hold = Hold(on_key)
        self._hook = WindowsHook() if hook is None else hook
        self._on_error, self._clock = on_error, clock
        self._events = SimpleQueue()
        self._stop = threading.Event()
        self._thread = None

    def enqueue(self, key, flags, message):
        self._events.put((key, flags, message))

    def drain(self):
        while True:
            try:
                event = physical_event(*self._events.get_nowait())
            except Empty:
                break
            if event is not None:
                self._hold.event(*event, self._clock())
        self._hold.tick(self._clock())

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def _run(self):
        try:
            with self._hook.open(self.enqueue) as pump:
                while not self._stop.is_set():
                    pump()
                    self.drain()
        except Exception:
            if self._on_error is not None:
                self._on_error("Talk key stopped after a hook error.")
        finally:
            self._hold.end()
