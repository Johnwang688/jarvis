"""Memory-only page text selected by an owner capture; never a file capability.

A reference is resolved only while assembling an owner HUD send. The random
fence is minted then, not stored in the browser or reused between messages.
The second credential check covers retained text; it makes no claim about the
pixels an owner previously chose to stage.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import secrets
import threading
import time
from collections import OrderedDict

from jarvis import credential_patterns, untrusted
from jarvis.tools import secrets as secret_store
from .. import terminal_guard

MIME = "application/x-jarvis-page-capture"
TTL = 24 * 60 * 60
MAX_ENTRIES = 16
MAX_BYTES = 8 * 1024 * 1024
RAW_BYTES = 256 * 1024
TEXT_CHARS = 100_000
ID = re.compile(r"[0-9a-f]{32}")
UNAVAILABLE = "[page text unavailable: the capture expired or Jarvis restarted]"
OWNER_ONLY = "[page text unavailable: only an owner HUD send can resolve this capture]"
WITHHELD = "[page text withheld: possible credential]"


class CaptureRefused(ValueError):
    """Only fixed words: page data must not become exception/log text."""


def value_forms():
    return terminal_guard.value_forms(secret_store.secret_values())


def flagged(fields: tuple[str, ...], forms) -> bool:
    text = "\n".join(fields)
    return (terminal_guard.holds_value(text, forms)
            or credential_patterns.holds_credential(text))


@dataclass(frozen=True)
class Capture:
    id: str
    url: str
    title: str
    text: str
    raw: tuple[str, ...]
    whole: bool
    at: float
    size: int
    withheld: bool = False


class Captures:
    def __init__(self, *, clock=time.monotonic, values=value_forms,
                 max_entries=MAX_ENTRIES, max_bytes=MAX_BYTES):
        self._clock, self._values = clock, values
        self._max_entries, self._max_bytes = max_entries, max_bytes
        self._lock = threading.Lock()
        self._rows: OrderedDict[str, Capture] = OrderedDict()
        self._bytes = 0

    def _expire(self):
        now = self._clock()
        for key, row in tuple(self._rows.items()):
            if now - row.at >= TTL:
                self._bytes -= row.size
                del self._rows[key]

    def add(self, scan: dict, *, whole: bool = False) -> Capture:
        """Keep one bounded scan. Raw fields are guarded before any truncation."""
        if not isinstance(whole, bool) or not isinstance(scan, dict) or scan.get("incomplete"):
            raise CaptureRefused("the page could not be captured safely")
        url, title = scan.get("url"), scan.get("title")
        lines, raw = scan.get("lines"), scan.get("raw")
        if (not isinstance(url, str) or not isinstance(title, str)
                or not isinstance(lines, list) or not isinstance(raw, list)
                or len(lines) > 20_000 or len(raw) > 40_000
                or any(not isinstance(x, str) for x in (*lines, *raw))):
            raise CaptureRefused("the page could not be captured safely")
        fields = (url, title, *raw, *lines)
        # Include presentation fields even if a malformed adapter forgot to
        # charge them. Never truncate a credential before checking it.
        if (sum(len(x.encode("utf-8", "replace")) for x in raw) > RAW_BYTES
                or sum(len(x.encode("utf-8", "replace")) for x in fields) > RAW_BYTES * 3):
            raise CaptureRefused("the page is too large to capture safely")
        hit = flagged(fields, self._values())
        if hit:
            url = title = text = ""
            fields = ()
        else:
            safe_lines = [untrusted.strip_invisible(line)
                          for value in lines for line in value.splitlines()
                          if not terminal_guard.withhold(line)]
            text = "\n".join(safe_lines)[:TEXT_CHARS]
            if terminal_guard.withhold(url): url = "[address withheld]"
            if terminal_guard.withhold(title): title = "[title withheld]"
            url = untrusted.one_line(url, 2048)
            title = untrusted.one_line(title, 120)
        size = sum(len(x.encode("utf-8", "replace")) for x in (url, title, text, *fields))
        if size > self._max_bytes or self._max_entries < 1:
            raise CaptureRefused("the capture is too large to retain")
        with self._lock:
            self._expire()
            while self._rows and (len(self._rows) >= self._max_entries
                                  or self._bytes + size > self._max_bytes):
                _, old = self._rows.popitem(last=False)
                self._bytes -= old.size
            key = secrets.token_hex(16)
            while key in self._rows: key = secrets.token_hex(16)
            row = Capture(key, url, title, text, tuple(fields), whole, self._clock(), size, hit)
            self._rows[key] = row
            self._bytes += size
            return row

    def resolve(self, key, *, owner: bool = False) -> str:
        if owner is not True:
            return OWNER_ONLY
        if not isinstance(key, str) or not ID.fullmatch(key):
            return UNAVAILABLE
        with self._lock:
            self._expire()
            row = self._rows.get(key)
        if row is None:
            return UNAVAILABLE
        if row.withheld or flagged(row.raw, self._values()):
            return WITHHELD
        # Fresh random marker per resolution; page-authored title is inside
        # the fence alongside the URL and text, never among trusted words.
        text = "URL: " + row.url + "\nTitle: " + row.title + "\n\n" + row.text
        return untrusted.fence(text, row.url)
