"""Dictation grammar and owner-hotkey SendInput glue (no import-time Win32)."""
from __future__ import annotations

import re
import time
import unicodedata


def sanitize(text):
    boundaries = "\r\n\t\v\f\x1c\x1d\x1e\x85\u2028\u2029"
    text = "".join(" " if c in boundaries else c for c in text
                   if c in boundaries or unicodedata.category(c) not in {"Cc", "Cf"})
    return re.sub(" +", " ", text).strip()


def _words(text):
    return [(m.group().casefold(), m.start()) for m in re.finditer(r"[^\W_]+", text)]


def _command_matches(text, expected):
    if [word for word, _ in _words(text)] != expected:
        return False
    # Word extraction alone loses combining marks and symbols. Only spaces
    # and punctuation may surround or separate the exact command words.
    remainder = re.sub(r"[^\W_]+", "", text)
    return all(c.isspace() or unicodedata.category(c).startswith("P") for c in remainder)


def plan(text):
    text = sanitize(text)
    if not text:
        return []
    words = _words(text)
    if _command_matches(text, ["new", "line"]):
        return [("key", "shift+enter")]
    if _command_matches(text, ["scratch", "that"]):
        return [("scratch",)]
    if (len(words) >= 2 and _command_matches(text[words[-2][1]:], ["send", "it"])
            and (words[-2][1] == 0 or text[words[-2][1] - 1].isspace()
                 or unicodedata.category(text[words[-2][1] - 1]).startswith("P"))):
        rest = re.sub(r"[,\s]+$", "", text[:words[-2][1]])
        return ([("text", rest)] if rest else []) + [("key", "enter")]
    return [("text", text)]


def might_be_command(text):
    words = _words(sanitize(text))
    return bool(words and words[0][0] in
                {"jarvis", "open", "launch", "start", "switch", "search", "google", "read"})


class WindowsInput:
    """Small injectable backend, shared with the clipboard action."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes as w

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD),
                        ("time", w.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD),
                        ("dwFlags", w.DWORD), ("time", w.DWORD),
                        ("dwExtraInfo", ctypes.c_size_t)]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", w.DWORD), ("wParamL", w.WORD), ("wParamH", w.WORD)]

        class Payload(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("payload",)
            _fields_ = [("type", w.DWORD), ("payload", Payload)]

        self.ctypes, self.INPUT, self.KEYBDINPUT = ctypes, INPUT, KEYBDINPUT
        self.user32 = ctypes.windll.user32
        self.user32.GetForegroundWindow.restype = w.HWND
        # windll caches function objects. The clipboard helper creates a second
        # backend with its own (ABI-identical) INPUT class; a class-specific
        # pointer declaration would make the first backend's arrays fail.
        self.user32.SendInput.argtypes = [w.UINT, ctypes.c_void_p, ctypes.c_int]
        self.user32.SendInput.restype = w.UINT

    def foreground(self):
        return self.user32.GetForegroundWindow()

    def _send(self, records):
        for start in range(0, len(records), 128):
            batch = records[start:start + 128]
            events = (self.INPUT * len(batch))()
            for event, (vk, scan, flags) in zip(events, batch):
                event.type = 1  # INPUT_KEYBOARD
                event.ki = self.KEYBDINPUT(vk, scan, flags, 0, 0)
            if self.user32.SendInput(len(events), events, self.ctypes.sizeof(self.INPUT)) != len(events):
                raise OSError("Windows could not type into this window.")

    def text(self, text):
        raw = text.encode("utf-16-le", errors="surrogatepass")
        records = []
        for i in range(0, len(raw), 2):
            unit = int.from_bytes(raw[i:i + 2], "little")
            records.extend(((0, unit, 0x0004), (0, unit, 0x0004 | 0x0002)))
        self._send(records)

    def key(self, key):
        modifiers, vk = {"enter": ([], 0x0D), "shift+enter": ([0x10], 0x0D),
                         "backspace": ([], 0x08), "ctrl+c": ([0x11], 0x43)}[key]
        try:
            self._send([(m, 0, 0) for m in modifiers] + [(vk, 0, 0), (vk, 0, 2)]
                       + [(m, 0, 2) for m in reversed(modifiers)])
        except OSError:
            # A partial SendInput must not strand Shift or Ctrl in the down state.
            try:
                self._send([(vk, 0, 2)] + [(m, 0, 2) for m in reversed(modifiers)])
            except OSError:
                pass
            raise


class Typer:
    def __init__(self, backend=None, clock=time.monotonic):
        self.backend = backend if backend is not None else WindowsInput()
        self.clock = clock
        self.last = None

    def run(self, steps):
        for step in steps:
            if step[0] == "text":
                text = sanitize(step[1])
                if text:
                    hwnd = self.backend.foreground()
                    self.last = None
                    self.backend.text(text)
                    self.last = (text, hwnd, self.clock())
            elif step[0] == "key":
                self.last = None  # Enter can move to a different line or field.
                self.backend.key(step[1])
            elif step[0] == "scratch":
                last, self.last = self.last, None
                if last:
                    text, hwnd, at = last
                    if hwnd and hwnd == self.backend.foreground() and 0 <= self.clock() - at < 60:
                        for _ in text:
                            if self.backend.foreground() != hwnd:
                                break
                            self.backend.key("backspace")
            else:
                raise ValueError("Unknown typing step")
