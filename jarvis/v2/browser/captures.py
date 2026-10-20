"""Memory-only page text selected by an owner capture; never a file capability.

A reference is resolved only while assembling an owner HUD send. The random
fence is minted then, not stored in the browser or reused between messages.
The second credential check covers retained text; it makes no claim about the
pixels an owner previously chose to stage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import secrets
import threading
import time
from collections import OrderedDict

from jarvis import credential_patterns, untrusted
from jarvis.tools import secrets as secret_store
from .. import terminal_guard
from .panes import PANE_SLOTS

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
    url: str = field(repr=False)
    title: str = field(repr=False)
    text: str = field(repr=False)
    raw: tuple[str, ...] = field(repr=False)
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

CAPTURE_CHANGED = "this Browser view changed; capture it again"
CAPTURE_HELD = "answer the approval card before capturing a page"
CAPTURE_WITHHELD = "page image and text withheld: this page may contain a credential"
CAPTURE_NOTES = (
    "Form values and embedded frames are omitted. Canvas/image and inaccessible DOM text cannot be credential-scanned.",
    "Page text is checked again when you send. That later check does not inspect the staged picture.",
)


def capture_image(encoded: str) -> dict:
    """Bound the already-masked owner still; no paths or metadata survive."""
    import base64
    from io import BytesIO
    from PIL import Image
    from .look import IMAGE_BYTES, PIXEL_CAP
    if not isinstance(encoded, str) or len(encoded) > IMAGE_BYTES * 2:
        raise CaptureRefused("the capture image could not be read safely")
    try:
        data = base64.b64decode(encoded, validate=True)
        if len(data) > IMAGE_BYTES:
            raise ValueError()
        with Image.open(BytesIO(data)) as image:
            if (image.width * image.height > PIXEL_CAP
                    or min(image.size) < 1 or max(image.size) > 1920):
                raise ValueError()
            width, height = image.size
            output = BytesIO()
            clean = image.convert("RGB")
            clean.save(output, format="PNG")
            mime = "image/png"
            if output.tell() > 3.5 * 1024 * 1024:
                output = BytesIO()
                clean.save(output, format="JPEG", quality=90)
                mime = "image/jpeg"
            data = output.getvalue()
        if len(data) > 4 * 1024 * 1024:
            raise ValueError()
    except Exception:
        raise CaptureRefused("the capture image could not be read safely") from None
    return dict(mime=mime, data_b64=base64.b64encode(data).decode("ascii"), width=width, height=height)


def capture_page(daemon, tab_id, query) -> dict:
    """Called only after the Browser route's owner/listener/Host checks."""
    from .look import LookRefused, REFUSAL, guarded
    registry, host = daemon.browser_panes, daemon.browser
    if (not {"window", "pane", "local_tab", "view"} <= set(query)
            or set(query) - {"window", "pane", "local_tab", "view", "whole"}
            or query["pane"] not in PANE_SLOTS
            or query.get("whole", "0") not in ("0", "1")):
        raise CaptureRefused("invalid page capture request")
    snapshot = registry.resolve_owner(query["window"], int(query["pane"]), query["local_tab"], query["view"])
    if snapshot.mode != "remote" or snapshot.tab_id != tab_id:
        raise CaptureRefused("open this in Remote to annotate")
    held_epoch = host.capture_hold_epoch()
    def current():
        if not registry.owner_current(snapshot):
            raise CaptureRefused(CAPTURE_CHANGED)
        if host.input_held() or host.capture_hold_epoch() != held_epoch:
            raise CaptureRefused(CAPTURE_HELD)
    current()
    try:
        forms = value_forms()
        scan, encoded, withheld = host.capture_look(
            snapshot, query.get("whole", "0") == "1", True, forms, current, image_edge=1920)
        current()
        if guarded(scan, forms) or withheld:
            raise CaptureRefused(CAPTURE_WITHHELD)
        if encoded is None:
            raise CaptureRefused(CAPTURE_CHANGED)
        image = capture_image(encoded)
        current()
        row = daemon.browser_captures.add(scan, whole=query.get("whole", "0") == "1")
        if row.withheld:
            raise CaptureRefused(CAPTURE_WITHHELD)
        current()
    except LookRefused as exc:
        raise CaptureRefused(CAPTURE_WITHHELD if str(exc) == REFUSAL
                             else "this page could not be captured safely; try again") from None
    except CaptureRefused:
        raise
    except Exception:
        raise CaptureRefused("this page could not be captured safely; try again") from None
    return dict(capture_id=row.id, image=image, url=row.url, title=row.title,
                text_chars=len(row.text), whole=row.whole, notes=list(CAPTURE_NOTES))


DIRECT_OFF = "Direct capture is off: turn it on in Settings, Browser"
DIRECT_NOTES = (
    "Direct capture: only the address is attached. Jarvis cannot read this page's text, so the picture had no credential check.",
)


def capture_direct(daemon, body) -> dict:
    """A URL-only reference for an owner's Direct still (B-DIRECT-CAPTURE).

    Called only after the Browser route's owner/listener/Host checks. The
    pixels never come here: the HUD took them from Chrome's tab share on the
    owner's press. The URL is the registry's own record of that exact visible
    binding, never the HUD's word, and passes the same guard as any capture.
    """
    registry, host = daemon.browser_panes, daemon.browser
    if not daemon.browser_settings.value("direct_capture"):
        raise CaptureRefused(DIRECT_OFF)
    if (not isinstance(body, dict) or set(body) != {"window", "pane", "local_tab", "view"}
            or body["pane"] not in PANE_SLOTS):
        raise CaptureRefused("invalid page capture request")
    snapshot = registry.resolve_owner(body["window"], int(body["pane"]), body["local_tab"], body["view"])
    if snapshot.mode != "direct":
        raise CaptureRefused(CAPTURE_CHANGED)
    if not registry.owner_current(snapshot):
        raise CaptureRefused(CAPTURE_CHANGED)
    if host.input_held():
        raise CaptureRefused(CAPTURE_HELD)
    try:
        row = daemon.browser_captures.add({"url": snapshot.url, "title": "", "lines": [], "raw": []})
    except CaptureRefused:
        raise
    except Exception:
        raise CaptureRefused("this page could not be captured safely; try again") from None
    if row.withheld:
        raise CaptureRefused(CAPTURE_WITHHELD)
    if not registry.owner_current(snapshot) or host.input_held():
        raise CaptureRefused(CAPTURE_CHANGED)
    return dict(capture_id=row.id, url=row.url, title="", text_chars=0, whole=False, notes=list(DIRECT_NOTES))
