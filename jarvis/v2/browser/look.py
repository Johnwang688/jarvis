"""The narrow, read-only Browser disclosure service (BR5).

Page data goes to the owner's model conversation only. Events and exceptions
hold fixed words and opaque identities. No input or driving capability lives
here; capture runs on BrowserHost's existing Playwright thread.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from io import BytesIO
import json
import math
import threading

from jarvis import credential_patterns, runtime, untrusted
from jarvis.tools import secrets
from .. import terminal_guard
from .look_scan import RAW_CAP

REFUSAL = "this page may contain a credential; use Send to chat to choose what to share"
CHANGED = "the Browser pane changed while it was being read; look again"
SOURCE_OFF = "this page came from a terminal whose reading is off or unavailable; ask the owner to enter its address or use Send to chat"
SOURCE_UNKNOWN = "this page's source cannot be verified; ask the owner to enter its address or use Send to chat"
NOT_AT_DESK = "hud_browser_view works only in a chat the owner is typing in at the HUD (not from Discord, a task, a schedule or a sub-agent)"
TEXT_CAP = 4000
IMAGE_EDGE = 1568
IMAGE_BYTES = 16 * 1024 * 1024
PIXEL_CAP = 16_000_000


class LookRefused(Exception):
    """Only a fixed sentence, never page or exception text."""


@dataclass(frozen=True)
class LookResult:
    text: str = ""
    source: str = ""
    notes: tuple[str, ...] = ()
    image: str | None = None
    refused: str = ""
    # Tool-local receipt; owner capture adapters never receive a ref grant.
    delivered: object = field(default=None,repr=False,compare=False)


def guarded(raw: dict, forms) -> bool:
    """Guard every raw field before presentation truncation or sanitation."""
    if not isinstance(raw, dict) or raw.get("incomplete"):
        raise LookRefused("the page is too large to scan safely; use Send to chat")
    encoded = json.dumps(raw, ensure_ascii=False)
    if len(encoded.encode("utf-8", "replace")) > RAW_CAP * 4:
        raise LookRefused("the page is too large to scan safely; use Send to chat")
    fields = raw.get("raw")
    if not isinstance(fields, list) or any(not isinstance(x, str) for x in fields):
        raise LookRefused("the page could not be read safely")
    text = "\n".join(fields)
    if len(text.encode("utf-8", "replace")) > RAW_CAP:
        raise LookRefused("the page is too large to scan safely; use Send to chat")
    if terminal_guard.holds_value(text, forms) or credential_patterns.holds_credential(text):
        raise LookRefused(REFUSAL)
    return any(terminal_guard.withhold(line) for value in fields for line in value.splitlines())


def masked_image(encoded: str, scan: dict) -> str:
    """Mask in memory, never paint on the page. Geometry uncertainty refuses."""
    from PIL import Image, ImageDraw
    if not isinstance(encoded, str) or len(encoded) > IMAGE_BYTES * 2:
        raise LookRefused("the page image could not be read safely")
    data = base64.b64decode(encoded, validate=True)
    if len(data) > IMAGE_BYTES:
        raise LookRefused("the page image could not be read safely")
    width, height = scan.get("width"), scan.get("height")
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x <= 0 for x in (width,height)):
        raise LookRefused("the page image could not be read safely")
    with Image.open(BytesIO(data)) as source:
        if source.width * source.height > PIXEL_CAP:
            raise LookRefused("the page image is too large to read safely")
        image = source.convert("RGB")
    sx, sy = image.width / width, image.height / height
    # A viewport screenshot must share one scale; do not guess at a clip.
    if abs(sx-sy) > max(sx,sy)*0.02:
        raise LookRefused("the page image geometry changed; look again")
    draw = ImageDraw.Draw(image)
    for rect in scan.get("masks", ()):
        if not isinstance(rect,list) or len(rect)!=4 or any(not isinstance(v,(int,float)) or isinstance(v,bool) or not math.isfinite(v) for v in rect):
            raise LookRefused("the page image could not be read safely")
        left,top,right,bottom=rect
        if right<=left or bottom<=top: continue
        box=(max(0,math.floor(left*sx)-3),max(0,math.floor(top*sy)-3),
             min(image.width,math.ceil(right*sx)+3),min(image.height,math.ceil(bottom*sy)+3))
        if box[2]>box[0] and box[3]>box[1]:draw.rectangle(box,fill=(24,26,29))
    image.thumbnail((IMAGE_EDGE,IMAGE_EDGE),Image.Resampling.LANCZOS)
    output=BytesIO();image.save(output,format="PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")


def presented(scan: dict, image: str | None, *, fresh: bool, vision: bool, withheld: bool) -> LookResult:
    lines=[]; count=0
    for value in scan["lines"]:
        for line in value.splitlines():
            if terminal_guard.withhold(line):count+=1
            else:
                clean=untrusted.strip_invisible(line).strip()
                if clean:lines.append(clean)
    text="\n".join(lines)
    notes=[]
    if len(text)>TEXT_CAP:notes.append("page text shortened to 4000 characters")
    text=text[:TEXT_CAP]
    refs=[]
    for row in scan["refs"]:
        label=row["label"]
        if terminal_guard.withhold(label):count+=1;continue
        refs.append(f"[{row['ref']}] <{row['tag']}> {untrusted.one_line(label,120)}")
    if refs:text+="\n\nINTERACTIVE\n"+"\n".join(refs)
    url=scan["url"];title=scan["title"]
    if terminal_guard.withhold(url):url="[address withheld]";count+=1
    if terminal_guard.withhold(title):title="[title withheld]";count+=1
    if count or withheld:notes.append("possible secret text withheld; image omitted")
    if fresh:notes.append("a fresh load, not your exact view")
    if not vision:notes.append("no image: this turn's model is not known to accept images")
    notes.append("form values and embedded frames omitted; canvas/image and inaccessible DOM text cannot be credential-scanned")
    if vision and image is None and not (count or withheld):notes.append("image omitted because the page changed during capture")
    source=f"HUD Browser page {untrusted.one_line(url,2048)} — {untrusted.one_line(title,120)}"
    return LookResult(text,source,tuple(notes),image if not(count or withheld) else None)


class BrowserLook:
    def __init__(self, daemon):
        self.daemon=daemon
        self.closed=False

    def _current(self,snapshot) -> None:
        if self.closed or not runtime.at_desk() or runtime.depth()!=0 or runtime.should_stop()():
            raise LookRefused(NOT_AT_DESK)
        if not snapshot.see:
            raise LookRefused(f"the owner has turned off 'Jarvis can see' for pane {snapshot.slot}; ask them, or use Send to chat.")
        registry=getattr(self.daemon,"browser_panes",None)
        if registry is None or not registry.still_current(snapshot):raise LookRefused(CHANGED)
        if not snapshot.drawn or snapshot.covered:raise LookRefused(CHANGED)
        source=snapshot.source
        if source.kind=="unknown":raise LookRefused(SOURCE_UNKNOWN)
        if source.kind=="terminal" and not self.daemon.terminals.readable_source(source.terminal_id):
            raise LookRefused(SOURCE_OFF)
        if source.kind not in ("owner","terminal","thread"):raise LookRefused(SOURCE_UNKNOWN)
        if self.daemon.browser.input_held():
            raise LookRefused("an approval card is waiting; look again after it is answered")

    def read(self,pane: str,whole_page: bool) -> LookResult:
        snapshot=None; refused=True
        try:
            if self.closed or not runtime.at_desk() or runtime.depth()!=0:raise LookRefused(NOT_AT_DESK)
            registry=getattr(self.daemon,"browser_panes",None)
            if registry is None:raise LookRefused("no live HUD Browser pane is available")
            try:snapshot=registry.resolve(pane)
            except Exception as exc:
                # BR3b's exact safe exception; unknown exceptions are never echoed.
                from .panes import PaneRefused
                if isinstance(exc,PaneRefused):raise LookRefused(str(exc)) from None
                raise
            self._current(snapshot)
            forms=terminal_guard.value_forms(secrets.secret_values())
            scan,image,withheld=self.daemon.browser.capture_look(
                snapshot,whole_page,runtime.desk_images(),forms,lambda:self._current(snapshot))
            self._current(snapshot)
            withheld=guarded(scan,forms) or withheld
            result=presented(scan,image,fresh=snapshot.mode=="direct",vision=runtime.desk_images(),withheld=withheld)
            self._current(snapshot)
            def delivered():
                self._current(snapshot)
                drive=getattr(self.daemon,"browser_drive",None)
                if drive is not None:drive.publish_look(snapshot,scan)
            refused=False
            return replace(result,delivered=delivered)
        except LookRefused as exc:
            return LookResult(refused=str(exc))
        except Exception:
            return LookResult(refused="the Browser page could not be read safely; try again")
        finally:
            if snapshot is not None:
                try:self.daemon.bus.publish({"kind":"browser_viewed","data":{
                    "tab_id":snapshot.view_id,"at":datetime.now(timezone.utc).isoformat(),"refused":refused}})
                except Exception:pass

    def install(self):
        global _installed
        with _installed_lock:_installed=self

    def stop(self):
        global _installed
        self.closed=True
        with _installed_lock:
            if _installed is self:_installed=None


_installed_lock=threading.Lock()
_installed: BrowserLook | None=None


def look_for_tool(pane: str,whole_page: bool=False) -> LookResult:
    """The tool's only entry point; no owner-input capability is exposed."""
    with _installed_lock:service=_installed
    if service is None:return LookResult(refused="no live HUD Browser pane is available")
    return service.read(pane,whole_page)
