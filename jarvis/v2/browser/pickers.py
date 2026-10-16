"""Owner-only fallbacks for native pickers the screencast never draws
(B-PICKERS, BR-7/M9; design `docs/plans/2026-10-16-browser-pickers.md`).

Chrome 155 opens a `<select>`, date, time, datetime-local, month, week or
colour popup in a widget the screencast does not capture, and that popup
takes the keyboard. Only the owner's ticketed socket reaches
`BrowserHost._deliver`, and only there is this module asked:

- before an owner left press is sent, the element under it is **pinned** by
  CDP hit test (`frameId`, `backendNodeId`; no page script);
- after the matching release is answered, that exact node is asked, in a
  fixed isolated-world function of its own frame, whether it is a picker
  kind and `:open`. Only then is the native popup closed with one trusted
  Escape key-down (measured: the page sees no event), the control read and
  the window told `{t:"picker"}`;
- the owner's `{t:"pick"}` comes back as queued owner input and is applied
  only to the same node in the same frame's current document, still
  connected, the same kind, with the same option list, as three separately
  awaited and re-checked steps: assignment, `input`, `change`.

Nothing here runs in the page's world, writes to the DOM, or reaches the bus
or a log line with page text. Agents never reach it.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import re
import secrets
import threading

from ..approvals import clean_line

PICK_METHOD = "Jarvis.pick"         # a queued owner input, never sent to Chrome
WORLD = "jarvis-pickers"
OBJECT_GROUP = "jarvis-pickers"
INPUT_KINDS = ("date", "time", "datetime-local", "month", "week", "color")
KINDS = ("select",) + INPUT_KINDS
OPTIONS_MAX = 500                   # options listed to the window
LABEL_MAX = 80                      # code points of one label, after cleaning
RAW_LABEL_MAX = 1024                # characters of a label kept for identity
RAW_VALUE_MAX = 2048                # an option value longer than this is not offered
READ_BUDGET = 256 * 1024            # characters of labels and values read at once
SCAN_MAX = 20_000                   # options looked at, hidden ones included
MESSAGE_MAX = 60_000                # bytes of one `picker` message (the HUD reads ≤ 64 KiB)
INDEX_MAX = 0xFFFF
VALUE_MAX = 64
PIN_WAIT_S = 1.0                    # the hit test before a press, at most
PROBE_WAIT_S = 2.0                  # the probe after a release, at most
CLOSE_TRIES = 12                    # Escapes for a native popup still loading its page
CLOSE_RETRY_S = 0.025
RELEASE_WAIT_S = 1.0                # a hung renderer never wedges the tab's input task
APPLY_WAIT_S = 2.0                  # each step of an apply, at most
ID = re.compile(r"[0-9a-f]{8}")
_VALUE_CHARS = re.compile(r"[0-9A-Za-z:.#\-]*")
GRAMMAR = {
    "date": re.compile(r"\d{4,6}-\d{2}-\d{2}"),
    "time": re.compile(r"\d{2}:\d{2}(:\d{2}(\.\d{1,3})?)?"),
    "datetime-local": re.compile(r"\d{4,6}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,3})?)?"),
    "month": re.compile(r"\d{4,6}-\d{2}"),
    "week": re.compile(r"\d{4,6}-W\d{2}"),
    "color": re.compile(r"#[0-9a-f]{6}"),
}
ESCAPE = {"type": "rawKeyDown", "modifiers": 0, "key": "Escape", "code": "Escape",
          "windowsVirtualKeyCode": 27, "nativeVirtualKeyCode": 27}
REASONS = ("applied", "cancelled", "changed", "refused", "held", "closed", "replaced")


class PickerBad(ValueError):
    """Fixed, content-free wire refusal."""


def parse_message(value) -> dict:
    """`{t:"pick", id, index}` | `{t:"pick", id, value}` | `{t:"pick_cancel", id}`."""
    if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not ID.fullmatch(value["id"]):
        raise PickerBad("a picker message has the wrong shape")
    t = value.get("t")
    if t == "pick_cancel":
        if value.keys() != {"t", "id"}:
            raise PickerBad("a picker message has the wrong shape")
        return dict(value)
    if t != "pick":
        raise PickerBad("a picker message has the wrong shape")
    if value.keys() == {"t", "id", "index"}:
        index = value["index"]
        if type(index) is not int or not 0 <= index <= INDEX_MAX:
            raise PickerBad("a picked option is out of range")
        return dict(value)
    if value.keys() == {"t", "id", "value"}:
        picked = value["value"]
        if not isinstance(picked, str) or len(picked) > VALUE_MAX or not _VALUE_CHARS.fullmatch(picked):
            raise PickerBad("a picked value is not one a control takes")
        return dict(value)
    raise PickerBad("a picker message has the wrong shape")


def valid_value(kind: str, value) -> bool:
    """The kind's own grammar; empty clears a date-like control (the page's
    `required` is judged in the isolated world)."""
    if not isinstance(value, str) or kind not in GRAMMAR:
        return False
    if value == "":
        return kind != "color"
    return GRAMMAR[kind].fullmatch(value) is not None


def label(text) -> str:
    return clean_line(text if isinstance(text, str) else "", LABEL_MAX)


# -- the fixed isolated-world functions (JSON arguments only) ---------------------------------

# `this` is the pinned element, resolved in its own frame's isolated world.
_IDENTITY = r"""
  const el = this;
  if (!el || !el.isConnected || el.ownerDocument !== document) return null;
  let kind = null;
  if (el instanceof HTMLSelectElement) {
    if (el.multiple || el.size > 1) return null;
    kind = "select";
  } else if (el instanceof HTMLInputElement && KINDS.includes(el.type)) kind = el.type;
  else return null;
"""
READ_FN = "function(KINDS, limits) {" + _IDENTITY + r"""
  let open = false;
  try { open = el.matches(":open"); } catch (e) { return null; }
  if (!open) return {kind, open: false};
  if (kind !== "select") {
    if (kind === "color") return {kind, open: true, value: el.value};
    return {kind, open: true, value: el.value, min: el.min, max: el.max,
            step: el.getAttribute("step") || "", required: el.required};
  }
  if (getComputedStyle(el).appearance === "base-select") return {kind, open: true, inPage: true};
  const options = el.options, count = options.length, out = [], groups = [];
  let budget = limits.budget, lastGroup = null;
  for (let i = 0; i < count && i < limits.scan && out.length < limits.max && budget > 0; i++) {
    const option = options[i];
    if (option.hidden) continue;
    const parent = option.parentElement;
    const group = parent instanceof HTMLOptGroupElement ? parent : null;
    let g = -1;
    if (group) {
      if (group !== lastGroup) groups.push([String(group.label).slice(0, limits.label), group.disabled]);
      lastGroup = group;
      g = groups.length - 1;
    } else lastGroup = null;
    const text = String(option.label), value = String(option.value);
    budget -= Math.min(text.length, limits.label) + Math.min(value.length, limits.value);
    out.push([i, text.slice(0, limits.label), text.length, value.length <= limits.value ? value : null,
              option.disabled || !!(group && group.disabled), g]);
  }
  return {kind, open: true, count, selected: el.selectedIndex, options: out, groups};
}"""
OPEN_FN = "function(KINDS) {" + _IDENTITY + r"""
  try { return el.matches(":open"); } catch (e) { return null; }
}"""
SET_FN = "function(KINDS, wanted, a) {" + _IDENTITY + r"""
  if (kind !== wanted || el.disabled) return "changed";
  if (kind === "select") {
    if (el.options.length !== a.count) return "changed";
    const option = el.options[a.index];
    if (!option || option.hidden || option.disabled) return "changed";
    const parent = option.parentElement;
    if (parent instanceof HTMLOptGroupElement && parent.disabled) return "changed";
    const text = String(option.label);
    if (option.value !== a.value || text.length !== a.labelLength || text.slice(0, a.label.length) !== a.label)
      return "changed";
    if (el.selectedIndex === a.index) return "same";
    el.selectedIndex = a.index;
    return el.selectedIndex === a.index ? "set" : "changed";
  }
  if (el.readOnly) return "changed";
  if (a.value === "") {
    if (el.required || kind === "color") return "refused";
  } else {
    const probe = document.createElement("input");
    probe.type = kind;
    for (const name of ["min", "max", "step"]) {
      const v = el.getAttribute(name);
      if (v !== null) probe.setAttribute(name, v);
    }
    probe.value = a.value;
    const v = probe.validity;
    if (probe.value !== a.value || v.badInput || v.rangeUnderflow || v.rangeOverflow || v.stepMismatch)
      return "refused";
  }
  if (el.value === a.value) return "same";
  el.value = a.value;
  return el.value === a.value ? "set" : "changed";
}"""
EVENT_FN = "function(KINDS, wanted, type, expected) {" + _IDENTITY + r"""
  if (kind !== wanted) return false;
  if (kind === "select" ? el.selectedIndex !== expected : el.value !== expected) return false;
  el.dispatchEvent(new Event(type, type === "input" ? {bubbles: true, composed: true} : {bubbles: true}));
  return true;
}"""


# -- state -----------------------------------------------------------------------------------

@dataclass(frozen=True, repr=False)
class Pin:
    """The element under an owner's press, before the press was sent."""
    frame_id: str
    node: int
    document: int
    attachment: int
    hold: int


@dataclass(repr=False)
class Picker:
    id: str
    tab: object
    page: object
    page_identity: int
    document: int
    instance: object
    instance_generation: int
    attachment: int
    hold: int
    approval: int
    frame_id: str
    node: int
    kind: str
    loader: str = ""                    # the frame's document when the picker was offered
    count: int = 0
    options: dict = field(default_factory=dict)  # index -> (value, raw label, label length)


class Pickers:
    """One open picker per tab, at most. Thread-safe: the socket's reader
    thread cancels, the bus observer and the Browser's thread invalidate,
    and only the Browser's thread probes and applies."""

    def __init__(self, host):
        self.host = host
        self._lock = threading.Lock()
        self._open: dict[str, Picker] = {}
        self.offered = 0                # counts only, never content
        self.applied = 0

    # -- the binding ------------------------------------------------------------------------

    def _bound(self, p: Picker) -> bool:
        host, tab = self.host, p.tab
        stream = tab.stream
        auth = getattr(host, "_auth", None)
        return (not host._stopped and host._tabs.get(tab.id) is tab
                and tab.page is p.page and tab.page_identity == p.page_identity
                and tab.document_generation == p.document and tab.pane_ready and not tab.retired
                and host._instances.get(tab.profile) is p.instance
                and p.instance.generation == p.instance_generation
                and not p.instance.closing and not p.instance.restoring
                and not host.input_held() and host._capture_hold_epoch == p.approval
                and not tab.dialogs and tab.chooser is None
                and not (auth is not None and auth.blocked(tab))
                and stream is not None and not stream.closed and stream.visible
                and stream.attached() is not None and stream.gen == p.attachment
                and stream.hold_count == p.hold)

    def current(self, tab) -> Picker | None:
        with self._lock:
            return self._open.get(tab.id)

    def _tell(self, p: Picker, message: dict) -> None:
        stream = p.tab.stream
        if stream is not None:
            stream.send(message, gen=p.attachment)

    def invalidate(self, tab, reason: str = "changed") -> None:
        """Synchronous, from any thread: the picker is forgotten and its
        window told. Never page IO."""
        with self._lock:
            p = self._open.get(tab.id)
            if p is None or p.tab is not tab:
                return
            del self._open[tab.id]
        self._tell(p, {"t": "picker_closed", "id": p.id, "reason": reason})

    def cancel(self, tab, sock, picker_id: str) -> None:
        """The owner's `pick_cancel` (the reader thread). No reply."""
        stream = tab.stream
        with self._lock:
            p = self._open.get(tab.id)
            if (p is None or p.tab is not tab or p.id != picker_id or stream is None
                    or not stream.holds(sock) or stream.gen != p.attachment):
                return
            del self._open[tab.id]

    # -- detection --------------------------------------------------------------------------

    async def pin(self, tab, cdp, params: dict) -> Pin | None:
        """The element under an owner's left press, by CDP hit test only:
        `getNodeForLocation` takes document coordinates (measured), so the
        layout viewport's scroll is added to the press point."""
        stream = tab.stream
        if stream is None:
            return None
        document, attachment, hold = tab.document_generation, stream.gen, stream.hold_count

        async def hit():
            metrics = await cdp.send("Page.getLayoutMetrics")
            view = (metrics or {}).get("cssLayoutViewport") or {}
            dx, dy = view.get("pageX", 0), view.get("pageY", 0)
            if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (dx, dy)):
                return None
            found = await cdp.send("DOM.getNodeForLocation", {
                "x": round(params["x"] + dx), "y": round(params["y"] + dy),
                "includeUserAgentShadowDOM": False})
            return found
        try:
            found = await asyncio.wait_for(hit(), PIN_WAIT_S)
        except Exception:  # noqa: BLE001 - no node there, or no answer: no pin
            return None
        if not isinstance(found, dict):
            return None
        node, frame_id = found.get("backendNodeId"), found.get("frameId")
        if type(node) is not int or not isinstance(frame_id, str) or not 0 < len(frame_id) <= 128:
            return None
        return Pin(frame_id, node, document, attachment, hold)

    def _attention_free(self, tab) -> bool:
        """No dialog, file chooser or auth prompt holds the tab, and its
        window shows it: nothing may be offered or applied behind them."""
        stream, auth = tab.stream, getattr(self.host, "_auth", None)
        return (not tab.dialogs and tab.chooser is None
                and not (auth is not None and auth.blocked(tab))
                and stream is not None and stream.visible and stream.attached() is not None)

    def _pin_current(self, tab, pin: Pin) -> bool:
        stream = tab.stream
        return (stream is not None and tab.document_generation == pin.document
                and stream.gen == pin.attachment and stream.hold_count == pin.hold)

    async def _call(self, cdp, object_id: str, fn: str, *args):
        reply = await cdp.send("Runtime.callFunctionOn", {
            "objectId": object_id, "functionDeclaration": fn,
            "arguments": [{"value": a} for a in (list(INPUT_KINDS), *args)],
            "returnByValue": True, "silent": True})
        if not isinstance(reply, dict) or "exceptionDetails" in reply:
            raise LookupError("the picker function failed")
        return (reply.get("result") or {}).get("value")

    async def _resolve(self, cdp, frame_id: str, node: int, used: list | None = None) -> str:
        if used is not None:
            used.append(True)           # an object group to release afterwards
        world = await cdp.send("Page.createIsolatedWorld", {"frameId": frame_id, "worldName": WORLD})
        resolved = await cdp.send("DOM.resolveNode", {
            "backendNodeId": node, "executionContextId": world["executionContextId"],
            "objectGroup": OBJECT_GROUP})
        object_id = ((resolved or {}).get("object") or {}).get("objectId")
        if not isinstance(object_id, str):
            raise LookupError("the pinned element is gone")
        return object_id

    async def _release_objects(self, cdp) -> None:
        try:
            await asyncio.wait_for(cdp.send("Runtime.releaseObjectGroup", {"objectGroup": OBJECT_GROUP}),
                                   RELEASE_WAIT_S)
        except Exception:  # noqa: BLE001 - a context that went with its document
            pass

    async def opened(self, tab, cdp, pin: Pin, current) -> None:
        """After the release of a pinned owner press: offer a picker only if
        that exact element opened one. `current()` is `_deliver`'s own check
        (same document, attachment and hold; no card; auth current)."""
        if not self._pin_current(tab, pin) or not current():
            return
        used: list = []
        try:
            await asyncio.wait_for(self._open_picker(tab, cdp, pin, current, used), PROBE_WAIT_S)
        except Exception:  # noqa: BLE001 - stale or unanswered: no overlay, never a lost pump
            pass
        finally:
            if used:
                await self._release_objects(cdp)

    async def _open_picker(self, tab, cdp, pin: Pin, current, used: list) -> None:
        def still():
            return self._pin_current(tab, pin) and self._attention_free(tab) and current()
        # A cheap first look (no script): only a select or an input can be one.
        # (`type` reflects to its attribute, so an input's attribute decides).
        node = ((await cdp.send("DOM.describeNode", {"backendNodeId": pin.node})) or {}).get("node") or {}
        name, attributes = node.get("nodeName"), node.get("attributes") or []
        if name == "INPUT":
            pairs = dict(zip(attributes[0::2], attributes[1::2])) if isinstance(attributes, list) else {}
            kind = pairs.get("type")
            if not isinstance(kind, str) or kind.strip().lower() not in INPUT_KINDS:
                return
        elif name != "SELECT":
            return
        if not still():
            return
        loader = _loader(await cdp.send("Page.getFrameTree"), pin.frame_id)
        if loader is None or not still():
            return
        object_id = await self._resolve(cdp, pin.frame_id, pin.node, used)
        if not still():
            return
        read = await self._call(cdp, object_id, READ_FN, {
            "max": OPTIONS_MAX, "label": RAW_LABEL_MAX, "value": RAW_VALUE_MAX, "budget": READ_BUDGET,
            "scan": SCAN_MAX})
        if not isinstance(read, dict) or read.get("open") is not True or read.get("kind") not in KINDS:
            return
        if read.get("inPage") is True:
            return                      # `base-select`: Chrome draws this picker in the page
        # Measured and built before anything is sent: a read that cannot be
        # offered leaves the native popup as it was.
        box = await cdp.send("DOM.getBoxModel", {"backendNodeId": pin.node})
        rect = _rect(((box or {}).get("model") or {}).get("border"))
        if rect is None or _loader(await cdp.send("Page.getFrameTree"), pin.frame_id) != loader or not still():
            return
        picker, message = self._build(tab, pin, read, rect)
        if picker is None:
            return
        picker.loader = loader
        # The popup is up and takes the keyboard: a trusted Escape key-down
        # closes it with no page event (measured, Chrome 155). While a popup
        # is open Chrome hands it every key, but a date-like popup that is
        # still loading its own page ignores one: asked again, a few times.
        for attempt in range(CLOSE_TRIES):
            if not still():
                return
            await cdp.send("Input.dispatchKeyEvent", dict(ESCAPE))
            state = await self._call(cdp, object_id, OPEN_FN)
            if state is False:
                break
            if state is not True:
                return
            await asyncio.sleep(CLOSE_RETRY_S)
        else:
            return
        old = None
        with self._lock:
            if not still():
                return
            old = self._open.get(tab.id)
            self._open[tab.id] = picker
            self.offered += 1
        if old is not None and old.tab is tab:
            self._tell(old, {"t": "picker_closed", "id": old.id, "reason": "replaced"})
        self._tell(picker, message)

    def _build(self, tab, pin: Pin, read: dict, rect: dict):
        host, stream = self.host, tab.stream
        instance = host._instances.get(tab.profile)
        if instance is None or stream is None:
            return None, None
        kind = read["kind"]
        picker = Picker(secrets.token_hex(4), tab, tab.page, tab.page_identity, pin.document,
                        instance, instance.generation, pin.attachment, pin.hold,
                        host._capture_hold_epoch, pin.frame_id, pin.node, kind)
        message = {"t": "picker", "id": picker.id, "kind": kind, "rect": rect}
        if kind != "select":
            value = read.get("value")
            message["value"] = value if valid_value(kind, value) and value else (
                "#000000" if kind == "color" else "")
            if kind != "color":
                for name in ("min", "max"):
                    bound = read.get(name)
                    message[name] = bound if valid_value(kind, bound) else ""
                step = read.get("step")
                message["step"] = step if isinstance(step, str) and len(step) <= 32 and (
                    step == "any" or re.fullmatch(r"\d+(\.\d+)?", step)) else ""
                message["required"] = read.get("required") is True
            return picker, message
        count, options, groups = read.get("count"), read.get("options"), read.get("groups")
        if type(count) is not int or not 0 <= count <= 1_000_000 or not isinstance(options, list) \
                or not isinstance(groups, list):
            return None, None
        picker.count = count
        group_labels = [label(g[0]) if isinstance(g, list) and g else "" for g in groups[:OPTIONS_MAX]]
        items, used, sent_groups, group_at = [], set(), [], {}
        # The exact encoding the socket sends (`json.dumps`, ASCII-escaped),
        # the counters at their widest; a group's label is charged when an
        # item kept first uses it, and only used groups are sent.
        size = len(json.dumps({**message, "items": [], "groups": [], "selected": INDEX_MAX,
                               "total": count, "shown": INDEX_MAX}))
        for row in options:
            if not (isinstance(row, list) and len(row) == 6 and type(row[0]) is int
                    and 0 <= row[0] < count and row[0] <= INDEX_MAX and row[0] not in used
                    and isinstance(row[1], str) and type(row[2]) is int):
                return None, None
            index, raw, length, value, disabled, g = row
            g = g if type(g) is int and 0 <= g < len(group_labels) else -1
            new_group = g >= 0 and g not in group_at
            item = {"i": index, "label": label(raw), "disabled": disabled is not False or value is None,
                    "g": group_at.get(g, len(sent_groups)) if g >= 0 else -1}
            cost = len(json.dumps(item)) + 2       # and its ", "
            if new_group:
                cost += len(json.dumps(group_labels[g])) + 2
            if size + cost > MESSAGE_MAX:
                break
            size += cost
            if new_group:
                group_at[g] = len(sent_groups)
                sent_groups.append(group_labels[g])
            used.add(index)
            items.append(item)
            if not item["disabled"]:
                picker.options[index] = (value, raw, length)
        selected = read.get("selected")
        message.update(items=items, groups=sent_groups,
                       selected=selected if type(selected) is int and selected in used else -1,
                       total=count, shown=len(items))
        if len(json.dumps(message)) > MESSAGE_MAX:
            return None, None           # never offered past the cap (the HUD would drop it)
        return picker, message

    # -- applying the owner's choice ----------------------------------------------------------

    async def apply(self, tab, cdp, params: dict, current) -> None:
        """The owner's queued `pick`, on the Browser's thread, from `_deliver`."""
        with self._lock:
            p = self._open.get(tab.id)
            if p is None or p.tab is not tab or p.id != params.get("id"):
                return
            del self._open[tab.id]      # one apply per picker
        reason = "changed"
        try:
            reason = await self._apply(p, cdp, params, current)
        except Exception:  # noqa: BLE001 - a stale node or a context gone: nothing more is sent
            reason = "changed"
        finally:
            await self._release_objects(cdp)
        if reason == "applied":
            self.applied += 1
        self._tell(p, {"t": "picker_closed", "id": p.id, "reason": reason})

    async def _apply(self, p: Picker, cdp, params: dict, current) -> str:
        def still():
            return self._bound(p) and current()
        if not still():
            return "changed"
        if p.kind == "select":
            index = params.get("index")
            if type(index) is not int or index not in p.options:
                return "refused"
            value, raw, length = p.options[index]
            argument = {"index": index, "count": p.count, "value": value, "label": raw,
                        "labelLength": length}
            expected = index
        else:
            value = params.get("value")
            if not valid_value(p.kind, value):
                return "refused"
            argument = {"value": value}
            expected = value
        # The frame's document must be the one the picker was offered for: a
        # backendNodeId from before a cross-process commit could name another
        # node in the new document.
        async def same_loader():
            tree = await asyncio.wait_for(cdp.send("Page.getFrameTree"), APPLY_WAIT_S)
            return _loader(tree, p.frame_id) == p.loader
        if not await same_loader():
            return "changed"
        object_id = await asyncio.wait_for(self._resolve(cdp, p.frame_id, p.node), APPLY_WAIT_S)
        if not still() or not await same_loader() or not still():
            return "changed"
        outcome = await asyncio.wait_for(self._call(cdp, object_id, SET_FN, p.kind, argument), APPLY_WAIT_S)
        if outcome == "same":
            return "applied"
        if outcome != "set":
            return "refused" if outcome == "refused" else "changed"
        # Each event is its own awaited call, re-checked first: a handler that
        # replaces the control or changes its value stops the later event.
        for event in ("input", "change"):
            if not still():
                return "changed"
            sent = await asyncio.wait_for(
                self._call(cdp, object_id, EVENT_FN, p.kind, event, expected), APPLY_WAIT_S)
            if sent is not True:
                return "changed"
        return "applied"



def _loader(tree, frame_id: str) -> str | None:
    """The `loaderId` of `frame_id` in a `Page.getFrameTree` answer."""
    stack = [(tree or {}).get("frameTree")]
    seen = 0
    while stack and seen < 4096:
        node = stack.pop()
        seen += 1
        if not isinstance(node, dict):
            continue
        frame = node.get("frame") or {}
        if frame.get("id") == frame_id:
            loader = frame.get("loaderId")
            return loader if isinstance(loader, str) and loader else None
        children = node.get("childFrames")
        if isinstance(children, list):
            stack.extend(children)
    return None


def _rect(quad) -> dict | None:
    if not isinstance(quad, list) or len(quad) != 8 or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and -1e6 < v < 1e6 for v in quad):
        return None
    xs, ys = quad[0::2], quad[1::2]
    x, y = min(xs), min(ys)
    width, height = max(xs) - x, max(ys) - y
    if width <= 0 or height <= 0:
        return None
    return {"x": round(x, 2), "y": round(y, 2), "width": round(width, 2), "height": round(height, 2)}
