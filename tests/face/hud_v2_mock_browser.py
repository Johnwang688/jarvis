"""Hermetic Browser HTTP contract; the real pane registry, a fake Chrome host.
The stream is supplied by Playwright route_web_socket and never leaves the HUD.
No page, profile, file or network operation is performed by this fake host.
"""
from __future__ import annotations
import secrets
import threading
from types import SimpleNamespace
from jarvis.v2.browser.settings import BrowserSettings
from jarvis.v2.browser.panes import BrowserPanes, PaneRefused
from jarvis.v2.browser.policy import judge_url, URLRefused
from jarvis.v2.browser.captures import Captures, CaptureRefused, capture_direct
from jarvis.v2.browser.direct_probe import DirectProbe, ProbeRefused

class Host:
    def __init__(self, mock):
        self.mock = mock
        self.rows = {}
        self.sequence = 0
        self.profiles = [{"id": "p-11111111", "label": "Main", "created": "2026-10-13T00:00:00Z"}]
        self.tickets = {}
    def refused_ports(self):
        return {8402, 8405, self.mock.port, self.mock.api_port}
    def input_held(self):
        return bool(self.mock.world.get("approvals"))
    def pane_tab(self, tid):
        row = self.rows.get(tid)
        return dict(row) if row else None
    def open_tab(self, profile=None, url=None, ctx=None, *, owner=False):
        if len(self.rows) >= 12: raise PaneRefused(409, "the Browser already has 12 tabs open; close one first")
        profile = profile or (self.profiles[0]["id"] if self.profiles else None)
        if profile != "private" and not any(p["id"] == profile for p in self.profiles): raise PaneRefused(404, "no such profile")
        if ctx and not any(r["ctx"] == ctx for r in self.rows.values()): raise PaneRefused(404, "no such Private context")
        self.sequence += 1
        tid = f"{self.sequence:08x}"
        self.rows[tid] = {"tab_id": tid, "profile": profile, "ctx": (ctx or secrets.token_hex(4)) if profile == "private" else None,
                          "url": "about:blank", "title": "", "opener": None, "page_identity": self.sequence, "document_generation": 1}
        if url is not None: self.navigate(tid, url, owner=owner)
        return {**self.rows[tid], "loaded": True}
    def navigate(self, tid, url, *, owner=False):
        url = judge_url(url, refused=self.refused_ports())
        if tid not in self.rows: raise PaneRefused(404, "no such tab")
        if self.input_held(): raise PaneRefused(409, "an approval card is waiting")
        self.rows[tid]["url"] = url
        self.rows[tid]["document_generation"] += 1
        return {**self.rows[tid], "loaded": True}
    def close_tab(self, tid):
        if self.rows.pop(tid, None) is None: raise PaneRefused(404, "no such tab")
        return {"tab_id": tid, "closed": True}

def state(mock):
    if not hasattr(mock, "browser_host"):
        mock.browser_host = Host(mock)
        mock.browser_settings = BrowserSettings()
        mock.browser_panes = BrowserPanes(SimpleNamespace(browser=mock.browser_host, devservers=SimpleNamespace(source_snapshot=lambda _: None), browser_settings=mock.browser_settings))
        mock.browser_window = None
        mock.browser_lock = threading.RLock()
        # B-DIRECT-CAPTURE: the real probe verdict and URL-only reference.
        mock.browser_captures = Captures(values=lambda: [])
        mock.browser_direct_probe = DirectProbe()
    return mock.browser_host

def connected(mock, window):
    state(mock)
    with mock.browser_lock:
        if mock.browser_window: mock.browser_panes.window_ended(mock.browser_window)
        mock.browser_window = window
        if window: mock.browser_panes.window_started(window)

def handle(h, mock, method, path, body):
    if not (path == "/browser" or path.startswith("/browser/")): return False
    if path == "/browser/settings":
        state(mock)
        if method == "GET":
            out=mock.browser_settings.snapshot()
            if mock.dev_fixture: out["auto_open"]=mock.dev_fixture.enabled
            h._json(out);return True
        if method == "PATCH" and set(body) in ({"agents_may_drive"}, {"direct_capture"}):
            if body.get("direct_capture") is True and not mock.browser_direct_probe.passed():
                h._err(409,"run Test Direct capture in Settings first: it has not passed since Jarvis started");return True
            try: h._json(mock.browser_settings.update(**body))
            except ValueError: h._err(400,"invalid setting")
            return True
        return False  # AO3 owns auto_open mutations.
    host = state(mock)
    parts = path.strip("/").split("/")
    origin = h.headers.get("Origin")
    if origin is not None and origin != f"http://127.0.0.1:{mock.port}":
        h._err(403, "Browser answers only the owner window"); return True
    try:
        with mock.browser_lock:
            if path == "/browser/front" and method == "POST": out = mock.browser_panes.report(body)
            elif path == "/browser/direct-capture/probe":
                try: out = {"probe": mock.browser_direct_probe.status()} if method == "GET" else mock.browser_direct_probe.record(body)
                except ProbeRefused as exc: raise PaneRefused(400, str(exc))
            elif path == "/browser/direct-capture" and method == "POST":
                mock.direct_capture_requests = getattr(mock, "direct_capture_requests", 0) + 1
                daemon = SimpleNamespace(browser_panes=mock.browser_panes, browser=host,
                                         browser_settings=mock.browser_settings, browser_captures=mock.browser_captures)
                try: out = capture_direct(daemon, body)
                except CaptureRefused as exc: raise PaneRefused(409, str(exc))
            elif len(parts) == 3 and parts[1] == "panes" and method == "PATCH": out = mock.browser_panes.permissions(int(parts[2]), body)
            elif len(parts) == 6 and parts[1] == "panes" and parts[5] == "address": out = mock.browser_panes.address(int(parts[2]), parts[4], body)
            elif path == "/browser/profiles":
                if method == "GET": out = host.profiles
                else:
                    label = body.get("label", "")
                    if not isinstance(label,str) or not 1 <= len(label) <= 32 or label.strip()!=label: raise PaneRefused(400,"use a profile name of 1–32 characters")
                    if label.lower()=="private" or any(p["label"].lower()==label.lower() for p in host.profiles): raise PaneRefused(409,"that profile name is taken")
                    if len(host.profiles)>=8: raise PaneRefused(409,"at most eight profiles")
                    out={"id":"p-"+secrets.token_hex(4),"label":label,"created":"2026-10-13T00:00:00Z"};host.profiles.append(out)
            elif parts[:2] == ["browser","profiles"] and len(parts)>=3:
                row = next((p for p in host.profiles if p["id"]==parts[2]),None)
                if row is None: raise PaneRefused(404,"no such profile")
                if method=="PATCH": row["label"]=body["label"];out=dict(row)
                elif len(parts)==4 and parts[3]=="signin": out=host.open_tab(row["id"]);host.rows[out["tab_id"]]["url"]="chrome://settings/syncSetup"
                else:
                    host.rows={tid:tab for tid,tab in host.rows.items() if tab["profile"]!=row["id"]}
                    if method=="DELETE": host.profiles.remove(row)
                    out={"profile":row["id"],"deleted":method=="DELETE","forgotten":method!="DELETE"}
            elif path == "/browser/tabs":
                out = list(host.rows.values()) if method=="GET" else host.open_tab(body.get("profile"),body.get("url"),body.get("ctx"),owner=True)
            elif len(parts)==3 and parts[1]=="contexts" and method=="DELETE":
                host.rows={tid:tab for tid,tab in host.rows.items() if tab["ctx"]!=parts[2]};out={"ctx":parts[2],"closed":True}
            elif len(parts)>=3 and parts[1]=="tabs":
                tid=parts[2]
                if tid not in host.rows: raise PaneRefused(404,"no such tab")
                if method=="DELETE":out=host.close_tab(tid)
                elif parts[3] in ("navigate","restore"):out=host.navigate(tid,body["url"],owner=parts[3]=="navigate")
                elif parts[3]=="sound":
                    if type(body.get("on")) is not bool:raise PaneRefused(400,"on must be a boolean")
                    if host.input_held():raise PaneRefused(409,"an approval card is waiting")
                    out={"on":body["on"],"profile":host.rows[tid]["profile"]}
                elif parts[3]=="history":out={"tab_id":tid,"loaded":True}
                elif parts[3]=="ticket":
                    ticket=secrets.token_hex(16);host.tickets[ticket]=tid;out={"ticket":ticket,"expires_in":30}
                elif parts[3]=="files":out={"tab_id":tid,"chooser":body["chooser"],"files":len(body["files"])}
                elif parts[3]=="activate":out={"tab_id":tid}
                else:raise PaneRefused(404,"no such Browser route")
            elif path=="/browser/downloads":out={"downloads":mock.world.get("browser_downloads",[]),"in_progress":0}
            elif path=="/browser":out={"profiles":host.profiles,"sync":{"password_store":None,"requested":"keyring","keyring":"unavailable","next_launch":"basic","unprotected":True,"hint":"Unprotected (basic store): saved passwords use a public key.","source":"observed","profiles":{}}}
            else:raise PaneRefused(404,"no such Browser route")
        h._json(out,201 if method=="POST" and path in ("/browser/tabs","/browser/profiles") else 200)
    except (PaneRefused, URLRefused) as exc: h._err(getattr(exc,"status",400),str(exc))
    return True
