"""Finite owner-only Remote find/cursor work; no public evaluator or page text.

The socket reader retains one find and one latest cursor point per exact tab.
One loop-owned worker serializes CDP. A short response deadline never releases
the actual ActivityWork child before its coroutine has settled.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
import json
import math
import threading
import time
import unicodedata

QUERY_POINTS = 256
QUERY_BYTES = 1024
SEQUENCE_MAX = 0x7FFFFFFF
V8_TIMEOUT_MS = 500
RESPONSE_WAIT_S = 2.0
CURSOR_INTERVAL_S = .1
CURSORS = frozenset({
    "default", "pointer", "text", "crosshair", "move", "grab", "grabbing",
    "help", "wait", "progress", "not-allowed", "col-resize", "row-resize",
    "n-resize", "ne-resize", "e-resize", "se-resize", "s-resize",
    "sw-resize", "w-resize", "nw-resize",
})


class OwnerBad(ValueError):
    """Fixed, content-free wire refusal."""


def _sequence(value):
    return type(value) is int and 1 <= value <= SEQUENCE_MAX


def valid_query(value):
    if not isinstance(value, str) or not 1 <= len(value) <= QUERY_POINTS:
        return False
    try:
        if len(value.encode("utf-8")) > QUERY_BYTES:
            return False
    except UnicodeError:
        return False
    return not any(unicodedata.category(c).startswith("C") or c in "\u2028\u2029"
                   for c in value)


def parse_message(value):
    """Strict finite subset; called only by the existing owner socket parser."""
    if not isinstance(value, dict):
        raise OwnerBad("an owner page request has the wrong shape")
    t = value.get("t")
    fields = {
        "find": {"t", "seq", "query", "backward"},
        "find_cancel": {"t", "seq"},
        "cursor": {"t", "seq", "x", "y"},
        "cursor_cancel": {"t", "seq"},
    }.get(t)
    if fields is None or value.keys() != fields or not _sequence(value.get("seq")):
        raise OwnerBad("an owner page request has the wrong shape")
    if t == "find" and (not valid_query(value["query"]) or type(value["backward"]) is not bool):
        raise OwnerBad("a find request has invalid text or direction")
    if t == "cursor":
        for name in ("x", "y"):
            point = value[name]
            if type(point) not in (int, float) or not math.isfinite(point) or not 0 <= point <= 4096:
                raise OwnerBad("a cursor point is outside the page")
    return dict(value)


def cursor_keyword(value):
    return value if isinstance(value, str) and value in CURSORS else "default"


# These are fixed operations, with JSON-encoded data only. No page-global
# function, selector, script or CSS supplied by the caller is executed.
_FIND_PREFIX = """(() => { const [query, backward] = """
_FIND_SUFFIX = """; return typeof window.find === "function"
  ? window.find(query, false, backward, true, false, false, false) : null; })()"""
_CURSOR_PREFIX = """(() => { const [x, y] = """
_CURSOR_SUFFIX = """; let node = document.elementFromPoint(x, y);
  for (let depth = 0; depth < 8; depth++) {
    if (!node || node.tagName === "IFRAME" || node.tagName === "FRAME") return "default";
    const root = node.shadowRoot;
    if (!root) return getComputedStyle(node).cursor;
    const next = root.elementFromPoint(x, y);
    if (!next || next === node) return getComputedStyle(node).cursor;
    node = next;
  }
  return "default";
})()"""


@dataclass(frozen=True, repr=False)
class Binding:
    tab: object
    page: object
    instance: object
    page_identity: int
    document: int
    instance_generation: int
    sock: object
    attachment: int
    hold: int
    approval: int


@dataclass(frozen=True, repr=False)
class Request:
    binding: Binding
    fields: dict = field(repr=False)
    epoch: int = 0


@dataclass(repr=False)
class Slot:
    tab: object
    epoch: int = 0
    find_seq: int = 0
    cursor_seq: int = 0
    find: Request | None = None
    cursor: Request | None = None
    scheduled: bool = False
    closed: bool = False
    worker: object = None
    operation: object = None
    cursor_at: float = -math.inf


class OwnerTools:
    def __init__(self, host, *, clock=time.monotonic):
        self.host = host
        self.clock = clock
        self._lock = threading.RLock()
        self._slots = {}

    def _binding(self, tab, sock):
        host, stream = self.host, tab.stream
        instance = host._instances.get(tab.profile)
        if instance is None or stream is None:
            return None
        with stream._lock:
            result = Binding(tab, tab.page, instance, tab.page_identity,
                tab.document_generation, instance.generation, sock, stream.gen,
                stream.hold_count, host._capture_hold_epoch)
            return result if self._bound(result) else None

    def _bound(self, b):
        host, tab = self.host, b.tab
        stream = tab.stream
        auth = getattr(host, "_auth", None)
        return (not host._stopped and host._tabs.get(tab.id) is tab
            and tab.page is b.page and tab.page_identity == b.page_identity
            and tab.document_generation == b.document and tab.pane_ready and not tab.retired
            and host._instances.get(tab.profile) is b.instance
            and b.instance.generation == b.instance_generation
            and not b.instance.closing and not b.instance.restoring
            and not host.input_held() and host._capture_hold_epoch == b.approval
            and not tab.dialogs and tab.chooser is None
            and not (auth is not None and auth.blocked(tab))
            and stream is not None and not stream.closed and stream.visible
            and stream.attached() is b.sock and stream.gen == b.attachment
            and stream.hold_count == b.hold)

    def _current(self, slot, request):
        fields = request.fields
        seq = slot.find_seq if fields["t"] == "find" else slot.cursor_seq
        return (self._slots.get(slot.tab.id) is slot and not slot.closed and slot.epoch == request.epoch
                and fields["seq"] == seq and self._bound(request.binding))

    def receive(self, tab, sock, fields):
        """Reader-thread admission; one scheduled callback, never one per point."""
        binding = self._binding(tab, sock)
        if binding is None:
            return
        kind = fields["t"]
        with self._lock:
            slot = self._slots.get(tab.id)
            if slot is None or slot.tab is not tab:
                slot = self._slots[tab.id] = Slot(tab)
            if slot.closed:
                return
            attribute = "find_seq" if kind.startswith("find") else "cursor_seq"
            if fields["seq"] <= getattr(slot, attribute) or not self._bound(binding):
                return
            setattr(slot, attribute, fields["seq"])
            if kind.endswith("_cancel"):
                setattr(slot, "find" if kind.startswith("find") else "cursor", None)
                return
            if kind == "cursor":
                viewport = tab.stream.page_viewport()
                if viewport is None or fields["x"] >= viewport[0] or fields["y"] >= viewport[1]:
                    return
            request = Request(binding, fields, slot.epoch)
            setattr(slot, kind, request)
            if kind == "find":
                tab.stream.owner_input()
            if slot.scheduled or slot.worker is not None:
                return
            loop = self.host._loop
            if loop is None or loop.is_closed():
                setattr(slot, kind, None)
                return
            slot.scheduled = True
            try:
                loop.call_soon_threadsafe(self._start, slot)
            except RuntimeError:
                slot.scheduled = False
                setattr(slot, kind, None)

    def invalidate(self, tab, *, attachment=False, closed=False):
        """Synchronous lifecycle revocation. Actual child work is never erased."""
        with self._lock:
            slot = self._slots.get(tab.id)
            if slot is None or slot.tab is not tab:
                return
            slot.epoch += 1
            slot.find = slot.cursor = None
            slot.closed = slot.closed or closed
            if slot.closed and slot.worker is None and not slot.scheduled:
                self._slots.pop(tab.id, None)
            if attachment:
                slot.find_seq = slot.cursor_seq = 0

    def _start(self, slot):
        with self._lock:
            slot.scheduled = False
            if slot.closed and slot.worker is None:
                if self._slots.get(slot.tab.id) is slot:
                    self._slots.pop(slot.tab.id, None)
                return
            if self._slots.get(slot.tab.id) is not slot or slot.worker is not None:
                return
            slot.worker = asyncio.create_task(self._worker(slot))
            slot.worker.add_done_callback(lambda task: task.cancelled() or task.exception())

    async def _worker(self, slot):
        try:
            while True:
                with self._lock:
                    request = slot.find or slot.cursor
                    if request is None:
                        return
                    kind = request.fields["t"]
                    if kind == "cursor":
                        delay = max(0, slot.cursor_at + CURSOR_INTERVAL_S - self.clock())
                    else:
                        delay = 0
                    if not delay:
                        setattr(slot, kind, None)
                        if not self._current(slot, request):
                            continue
                        if kind == "cursor":
                            slot.cursor_at = self.clock()
                if delay:
                    await asyncio.sleep(delay)
                    continue
                await self._perform(slot, request)
        finally:
            with self._lock:
                slot.worker = None
                if self._slots.get(slot.tab.id) is slot and not self.host._stopped:
                    if slot.closed:
                        self._slots.pop(slot.tab.id, None)
                    elif slot.find is not None or slot.cursor is not None:
                        self._start(slot)
                    elif self.host._tabs.get(slot.tab.id) is not slot.tab:
                        self._slots.pop(slot.tab.id, None)

    async def _evaluate(self, slot, request):
        def check():
            with self._lock:
                if not self._current(slot, request):
                    raise OwnerBad("this owner page request is no longer current")
        check()
        cdp = await self.host._ensure_cdp(slot.tab)
        check()
        if cdp is None:
            raise OwnerBad("this owner page operation is unavailable")
        tree = await cdp.send("Page.getFrameTree")
        check()
        world = await cdp.send("Page.createIsolatedWorld", {
            "frameId": tree["frameTree"]["frame"]["id"], "worldName": "jarvis-owner-tools"})
        check()
        fields = request.fields
        if fields["t"] == "find":
            expression = _FIND_PREFIX + json.dumps([fields["query"], fields["backward"]]) + _FIND_SUFFIX
        else:
            expression = _CURSOR_PREFIX + json.dumps([fields["x"], fields["y"]]) + _CURSOR_SUFFIX
        result = await cdp.send("Runtime.evaluate", {"expression": expression,
            "contextId": world["executionContextId"], "returnByValue": True,
            "timeout": V8_TIMEOUT_MS})
        check()
        if "exceptionDetails" in result:
            raise OwnerBad("this owner page operation is unavailable")
        return (result.get("result") or {}).get("value")

    def _reply(self, slot, request, value, *, available):
        with self._lock:
            if not self._current(slot, request):
                return
            fields = request.fields
            if fields["t"] == "find":
                message = {"t": "find", "seq": fields["seq"],
                           "available": available and type(value) is bool,
                           "matched": value is True}
            else:
                message = {"t": "cursor", "seq": fields["seq"],
                           "cursor": cursor_keyword(value) if available else "default"}
            stream = slot.tab.stream
            with stream._lock:
                if self._current(slot, request):
                    stream.send_to(request.binding.sock, message)

    async def _actual(self, slot, request):
        # Keep expected stale/CDP failures as data even if a shielded wait was
        # canceled. Python 3.14 otherwise logs shielded-child exceptions.
        try:
            value = await self.host._activity_run(slot.tab,
                lambda: self._evaluate(slot, request))
            return True, value
        except Exception:
            return False, None

    async def _perform(self, slot, request):
        task = asyncio.create_task(self._actual(slot, request))
        slot.operation = task
        try:
            done, _ = await asyncio.wait({task}, timeout=RESPONSE_WAIT_S)
            if done:
                try:
                    available, value = task.result()
                except Exception:
                    self._reply(slot, request, None, available=False)
                else:
                    self._reply(slot, request, value, available=available)
            else:
                self._reply(slot, request, None, available=False)
                # A response deadline is not a native-operation termination.
                # The worker and ActivityWork remain busy until actual finally.
                try:
                    await asyncio.shield(task)
                except Exception:
                    pass
        except asyncio.CancelledError:
            # Cancellation of a waiter cannot free the serialization slot.
            # Shutdown may cancel the actual child; its finally still settles.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            raise
        finally:
            slot.operation = None
