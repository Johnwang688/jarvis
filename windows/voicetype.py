"""Dictation grammar and owner-hotkey SendInput glue (no import-time Win32)."""
from __future__ import annotations

import re
import time
import unicodedata
import ntpath
from typing import Literal


# Requires a complete address, so "Jarvis's shortcut" can be dictated.
JARVIS_ADDRESS = re.compile(r"^(?:(?:hey|okay|ok)[,\s]+)?jarvis(?=$|[,.:!?\s-])[,.:!?\s-]*", re.I)


POLITE_PREFIX = re.compile(r"^(?:(?:(?:could|can|would|will) you(?: please)?|I(?:[’\']d| would) like you to|please|kindly)[,\s]+)+", re.I)
WAKE_REQUEST = re.compile(r"^(?:let['’]s|I(?:['’]d| would) like to)\s+", re.I)
_WEB_FRAME = re.compile(r"go to (.+)", re.I)
_SPOKEN_DOT = re.compile(r" +dot(?: +|-)| *\. *", re.I | re.ASCII)
_DNS_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_WEB_HOST = re.compile(rf"(?:(https?)://)?((?:{_DNS_LABEL}\.)+[a-z]{{2,63}})/?",
                       re.ASCII | re.I)
_NON_WEB_SUFFIXES = frozenset("""
    arpa bat chm cmd cpl dll doc docx example exe home hta inf internal intranet invalid lan local
    localhost lnk msc msi onion pdf pif reg scr sh sys test txt vbe vbs wsf
    xls xlsx
""".split())
_BLOCKED_WEB_HOSTS = ("command.com", "localhost.direct", "localtest.me", "lvh.me",
                      "nip.io", "sslip.io", "xip.io")

OPEN_APP_FRAME = re.compile(r"(?:open(?: up)?|launch|start(?: up)?|fire up|pull up|bring up) (.+)", re.I)
_APP_SLOT = re.compile(r"[\w .'+&()–-]{1,80}", re.I)
_APP_CLAUSE = re.compile(
    r"\b(?:and|then|when|tomorrow|after|before|if|instead|because|unless|without|"
    r"in|into|on|about|source|door|doors|file|files|folder|account|accounts|"
    r"window when|settings for|is|are|was|were|will|would|should|not|don't|later|"
    r"tab|tabs|website|browser history|my|your|their|please don't)\b|\w\.\w", re.I)
# Ambiguous imperative/company names such as "Google" and "Focus" are also
# subjects in ordinary dictated statements. Require the longer explicit search
# frame for a query that itself looks like a declarative sentence.
NARRATIVE_VERB = re.compile(
    r"\b(?:was|were|is|are|has|have|had|announced|reported|acquired|says|seems|"
    r"became|went|help|helps|keeps|kept|worked|works|decided|released|bought|"
    r"needs|need|changed|took|takes)\b", re.I)

_TERMINALS = frozenset("windowsterminal cmd powershell pwsh wsl wslhost conhost openconsole "
                      "mintty bash alacritty wezterm-gui hyper putty kitty code cursor windsurf "
                      "zed idea64 pycharm64 webstorm64 clion64 goland64 rider64 studio64 devenv "
                      "sublime_text".split()) | {"code - insiders"}
# Unrecognised processes fail closed. These known document/browser apps receive
# prose cleanup; an executable name is never sent across the link.
_PROSE_APPS = frozenset("notepad notepad++ chrome msedge firefox brave opera winword outlook "
                       "onenote powerpnt excel slack discord teams ms-teams wordpad claude chatgpt "
                       "notion obsidian whatsapp telegram signal thunderbird messenger".split())


def app_category(exe_name: str | None) -> Literal["terminal", "other"]:
    if not isinstance(exe_name, str):
        return "terminal"
    name = ntpath.basename(exe_name).casefold()
    if name.endswith(".exe"):
        name = name[:-4]
    if name in _TERMINALS or name not in _PROSE_APPS:
        return "terminal"
    return "other"


def sanitize(text):
    boundaries = "\r\n\t\v\f\x1c\x1d\x1e\x85\u2028\u2029"
    text = "".join(" " if c in boundaries else c for c in text
                   if c in boundaries or unicodedata.category(c) not in {"Cc", "Cf"})
    return re.sub(" +", " ", text).strip()


def driver_handoff(driving: bool, source: str, text: str) -> bool:
    """Decide which clips the helper must send to the desktop driver."""
    return driving and (source in ("assistant", "wake")
                        or (source == "hotkey" and bool(JARVIS_ADDRESS.match(sanitize(text)))))


def web_destination(value: str) -> str:
    """Canonicalize a bare public-style hostname or an HTTP(S) origin."""
    if not isinstance(value, str) or len(value) > 262:
        return ""
    match = _WEB_HOST.fullmatch(value)
    if not match:
        return ""
    scheme, host = match.groups()
    host = host.lower()
    if (len(host) > 253 or host.startswith("127.")
            or host.rsplit(".", 1)[1] in _NON_WEB_SUFFIXES
            or any(host == name or host.endswith("." + name) for name in _BLOCKED_WEB_HOSTS)):
        return ""
    return f"{(scheme or 'https').lower()}://{host}"


def navigation_target(text: str, source: str) -> str:
    """Recognize a complete destination command directly from the transcript."""
    if (source not in ("hotkey", "wake") or not isinstance(text, str) or len(text) > 2000
            or any(unicodedata.category(char).startswith("C") for char in text)):
        return ""
    words = re.sub(" +", " ", text).strip(" ")
    address = JARVIS_ADDRESS.match(words)
    body = words[address.end():] if address else words
    if address or source == "wake":
        body = WAKE_REQUEST.sub("", body, count=1)
    body = POLITE_PREFIX.sub("", body, count=1)
    body = re.sub(r"[, ]+please[.!?]?$", "", body, flags=re.I)
    match = _WEB_FRAME.fullmatch(body)
    if not match:
        return ""
    target = re.sub(r" *[,.!?]$", "", match[1])
    return web_destination(_SPOKEN_DOT.sub(".", target))


# -- Laya routing: local literal/dictation decisions and grounding checks ------
# The helper decides explicit literal dictation and Ctrl+D typing itself, and
# rechecks every routed target against the owner's own words, so a stale or
# faulty daemon can never make it act on a site, app or query never spoken.
# These mirror jarvis/v2/laya_routing.py (the daemon's grounding adapter);
# tests/v2/voice_laya_flow_check.py holds the two in parity.

LAYA_ROUTE_WAIT_S = 40.0  # cold Laya start (20 s) plus one request, with margin
_LAYA_LITERAL = re.compile(
    r"^\s*(?:(?:please|kindly)\s+)?(?:type(?: out)?|dictate|insert)\s+"
    r"(?:(?:the\s+)?(?:words|phrase|sentence|text)|literally|verbatim)(?=[: ,\s])"
    r"(?:\s*[:,]\s*|\s+)(?=\S)", re.I)
_LAYA_LITERAL_QUOTED = re.compile(
    r"^\s*(?:(?:please|kindly)\s+)?(?:type(?: out)?|dictate|insert)"
    r"(?:\s*[:,]\s*|\s+)(?=[\"“‘])", re.I)
_LAYA_SPOKEN_DOT = re.compile(
    r"(?<=[a-z0-9])\s+dot(?:\s+|-)(?=[a-z0-9])|(?<=[a-z0-9]) +\. +(?=[a-z0-9])", re.I | re.ASCII)
_LAYA_HOST_TOKEN = re.compile(
    rf"(?<![\w.@:/\\%#?&=+~-])((?:https?://)?(?:{_DNS_LABEL}\.)+[a-z]{{2,63}}/?)"
    r"(?![\w@:/\\%#&=+~-]|\?(?=\S)|\.[a-z0-9])", re.I | re.ASCII)
_LAYA_FUSED_GREETING = re.compile(r"(\.[a-z]{2,63})(?=(?:Yo|Hey|Hi|Hello|Okay|OK)[, ]+)", re.ASCII)
_LAYA_REPEATED_HOST = re.compile(
    rf"(?<![\w.@:/\\%#?&=+~-])((?:{_DNS_LABEL}\.)+[a-z]{{2,63}}?)(?:\1)+"
    r"(?![\w@:/\\%#&=+~-]|\?(?=\S)|\.[a-z0-9])", re.I | re.ASCII)


def literal_payload(text: str) -> str:
    """The words after an explicit "type the words …" request, else ""."""
    if not isinstance(text, str):
        return ""
    body = JARVIS_ADDRESS.sub("", text.lstrip(), count=1)
    match = _LAYA_LITERAL.match(body) or _LAYA_LITERAL_QUOTED.match(body)
    return body[match.end():] if match else ""


def spoken_websites(text: str) -> tuple[str, ...]:
    """Every distinct canonical website the transcript names, in spoken order."""
    if not isinstance(text, str) or len(text) > 2000:
        return ()
    text = _LAYA_SPOKEN_DOT.sub(".", text)
    text = _LAYA_FUSED_GREETING.sub(r"\1 ", text)
    text = _LAYA_REPEATED_HOST.sub(r"\1", text)
    found = []
    for match in _LAYA_HOST_TOKEN.finditer(text):
        url = web_destination(match[1])
        if url and url not in found:
            found.append(url)
    return tuple(found)


def spoken_span(text: str, value: str) -> bool:
    """Whether ``value`` occurs in the transcript as whole words (any case)."""
    if not isinstance(text, str) or not isinstance(value, str) or not value.strip():
        return False
    phrase = r"\s+".join(map(re.escape, value.split()))
    return re.search(rf"(?<![\w+]){phrase}(?![\w+])", text, re.I) is not None


def laya_grounded(intent, text, apps=(), windows=()):
    """Recheck one routed Laya decision against the owner's own words.

    ``apps`` and ``windows`` are exactly the names this helper offered for the
    request. Only an exact offered name the transcript says may launch or
    focus; only the one website it names may navigate; a search query must be
    the owner's own contiguous words. A clarification may only offer
    websites the owner named.
    """
    kind, target = intent.get("kind"), intent.get("target")
    if not isinstance(target, str):
        return False
    if kind == "navigate":
        return bool(target) and spoken_websites(text) == (target,)
    if kind == "clarify":
        choices = intent.get("choices", ())
        if target == "":
            return choices == ()
        spoken = spoken_websites(text)
        return (target == "website" and isinstance(choices, tuple) and len(choices) == 2
                and len(set(choices)) == 2 and all(choice in spoken for choice in choices))
    if kind in ("open_app", "switch_app"):
        offered = apps if kind == "open_app" else windows
        return bool(target) and target in offered and spoken_span(text, target)
    if kind == "search":
        return 0 < len(target) <= 500 and spoken_span(text, target)
    if kind == "ask":
        return target in ("", "conversation")
    return kind == "read_selection" and target == ""


_LITERAL_DICTATION = re.compile(
    r"^(?:type(?: out)?|dictate|insert)(?:\s+(?:the )?(?:words?|phrase|sentence|text)"
    r"(?=[:,\s]))?(?:\s+(?:literally|verbatim))?(?:\s*[:,]\s*|\s+)(.+)$", re.I)
_DICTATION_QUOTES = {'"': '"', "'": "'", '“': '”', '‘': '’'}
_REPORTED = re.compile(r"\b(?:said|says|say the words|phrase|literally|verbatim|"
                       r"the words|quotation|quote|quoted|reads|wrote|write the words|heard|told)\b", re.I)
_SEND_IN_SENTENCE = re.compile(
    r"\b(?:I|you|we|she|he|they|to|not|never|cannot|can|could|would|will|"
    r"should|may|might|must|shall|did|do|does|usually|already|always|"
    r"\w+['’](?:t|ll|d))\s+send it[.!?]*$", re.I)
_EDIT_WORDS = re.compile(r"\b(?:scratch that|new line|send it)\b", re.I)
_TYPE_NOUN = re.compile(r"^type (?:[0-9]+|one|two|three|i|ii|iii|safety|checking|"
                        r"system|inference|error|theory|of)\b", re.I)


def _unquote(payload):
    for candidate in (payload, payload.rstrip(".!?")):
        if len(candidate) > 1 and _DICTATION_QUOTES.get(candidate[0]) == candidate[-1]:
            return candidate[1:-1].strip()
    return payload


def dictation_choice(text: str) -> tuple[str, str]:
    """How Ctrl+D types a transcript, decided locally with no intent model.

    ``("literal", words)`` inserts exactly those words; ``("edit", words)``
    goes through the editing grammar (scratch that, new line, send it). This
    keeps the held shortcut's established escapes: "type the words …" and a
    quoted or reported command phrase are typed, never pressed.
    """
    words = sanitize(text)
    body = JARVIS_ADDRESS.sub("", words, count=1)
    body = POLITE_PREFIX.sub("", body, count=1).strip()
    literal = _LITERAL_DICTATION.fullmatch(body)
    if literal and not _TYPE_NOUN.match(body):
        return "literal", _unquote(literal[1].strip())
    if _SEND_IN_SENTENCE.search(body) or (body.endswith("?") and _EDIT_WORDS.search(body)):
        return "literal", words
    if (_unquote(body) != body or (_EDIT_WORDS.search(body) and
            (_REPORTED.search(body) or re.search(r"""["“”‘’]|(?:^|\s)'""", body)))):
        return "literal", words
    return "edit", words


def app_name(target):
    """Extract one bounded spoken app name from a complete imperative."""
    target = re.sub(r"^the ", "", target.strip(), flags=re.I)
    target = re.sub(r" (?:app|application|window)$", "", target, flags=re.I)
    if (not _APP_SLOT.fullmatch(target) or _APP_CLAUSE.search(target)
            or NARRATIVE_VERB.search(target) or len(target.split()) > 5
            or target.startswith("'") or target.endswith("'")):
        return ""
    return target


def shortcut_open_target(text):
    """An unaddressed held shortcut may open one exactly named installed app."""
    words = sanitize(text)
    if len(words) > 2000 or JARVIS_ADDRESS.match(words):
        return ""
    polite = POLITE_PREFIX.match(words)
    body = words[polite.end():] if polite else words
    if body.endswith("?") and not polite and not re.search(r"\b(?:for me|please)\?$", body, re.I):
        return ""
    # Strip courtesy words only at the end, so reported or compound clauses
    # cannot be truncated to a name the app catalog would accept.
    body = re.sub(r"(?:[,\s]+(?:please|for me)){0,2}[.!?]*$", "", body, count=1, flags=re.I)
    match = OPEN_APP_FRAME.fullmatch(body.strip())
    if not match or re.search(r"(?:^|\s)-+\w|\b(?:search for|look up|tell me)\b", match[1], re.I):
        return ""
    return app_name(match[1])


def _words(text):
    return [(m.group().casefold(), m.start()) for m in re.finditer(r"[^\W_]+", text)]


COMMAND_PHRASES = (("new", "line"), ("scratch", "that"), ("send", "it"))


def mentions_command(text):
    """True when the text holds a command phrase anywhere ("please new line
    now"). Cleanup skips such text: it may never alter command words, even
    where plan() left them as words (Sol review of #399)."""
    words = [word for word, _ in _words(text)]
    return any(tuple(words[i:i + 2]) == phrase for phrase in COMMAND_PHRASES
               for i in range(len(words) - 1))


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

    def foreground_exe(self):
        from ctypes import wintypes as w
        handle = None
        result = None
        try:
            if not hasattr(self, "kernel32"):
                c = self.ctypes
                self.user32.GetWindowThreadProcessId.argtypes = [w.HWND, c.POINTER(w.DWORD)]
                self.user32.GetWindowThreadProcessId.restype = w.DWORD
                kernel32 = c.windll.kernel32
                kernel32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
                kernel32.OpenProcess.restype = w.HANDLE
                kernel32.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)]
                kernel32.QueryFullProcessImageNameW.restype = w.BOOL
                kernel32.CloseHandle.argtypes = [w.HANDLE]
                kernel32.CloseHandle.restype = w.BOOL
                self.kernel32 = kernel32
            pid = w.DWORD()
            hwnd = self.foreground()
            if not hwnd or not self.user32.GetWindowThreadProcessId(hwnd, self.ctypes.byref(pid)) or not pid.value:
                return None
            handle = self.kernel32.OpenProcess(0x1000, False, pid.value)
            if not handle:
                return None
            size = w.DWORD(32768)
            path = self.ctypes.create_unicode_buffer(size.value)
            if not self.kernel32.QueryFullProcessImageNameW(handle, 0, path, self.ctypes.byref(size)):
                return None
            result = ntpath.basename(path.value) or None
        except Exception:
            pass
        finally:
            if handle:
                try:
                    if not self.kernel32.CloseHandle(handle):
                        result = None
                except Exception:
                    result = None
        return result

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
    def __init__(self, backend=None, clock=time.monotonic, on_scratch=None):
        self.backend = backend if backend is not None else WindowsInput()
        self.clock = clock
        self.last = None
        self.on_scratch = on_scratch
        self.on_insert = None

    def category(self):
        try:
            return app_category(self.backend.foreground_exe())
        except Exception:
            return "terminal"

    def run(self, steps):
        for step in steps:
            if step[0] == "text":
                text = sanitize(step[1])
                if text:
                    hwnd = self.backend.foreground()
                    self.last = None
                    self.backend.text(text)
                    if self.on_insert:
                        callback, self.on_insert = self.on_insert, None
                        callback(self.clock())
                    self.last = (text, hwnd, self.clock(), step[2] if len(step) > 2 else "raw")
            elif step[0] == "key":
                self.last = None  # Enter can move to a different line or field.
                self.backend.key(step[1])
            elif step[0] == "scratch":
                last, self.last = self.last, None
                if last:
                    text, hwnd, at, which = last
                    if hwnd and hwnd == self.backend.foreground() and 0 <= self.clock() - at < 60:
                        deleted = False
                        try:
                            for _ in text:
                                if self.backend.foreground() != hwnd:
                                    break
                                self.backend.key("backspace")
                                deleted = True
                        finally:
                            if deleted and self.on_scratch:
                                try:
                                    self.on_scratch(which)
                                except Exception:
                                    pass
            else:
                raise ValueError("Unknown typing step")
