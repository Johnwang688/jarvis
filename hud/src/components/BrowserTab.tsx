// The Browser pane (called Preview until BR-3a): an iframe onto the workshop
// origin (8403) or onto a localhost dev server the owner names — never onto
// the HUD's own origin, and never onto the daemon's API listener (WP-E).
//
// Two mechanical confinements, both deliberate. The URL is judged by
// `judgeBrowserUrl` before it is loaded at all, so the HUD origin is refused
// with a message rather than framed. And the frame's sandbox is
// `allow-scripts allow-forms` **without** `allow-same-origin`: the two
// together are no sandbox at all when the framed page is the HUD's origin,
// because it could then reach out of the frame and script the window that
// gates approvals.
//
// **Keep its own origin (WP-E, decisions W-4).** A dev app needs its origin
// for `localStorage`, cookies and HMR. The pane's "Keep its origin" switch
// adds `allow-same-origin` — off by default, offered only for a local dev
// server on a port that is none of the daemon's three nor this window's own,
// and only while `/status` says every HUD and API response refuses to be
// framed (`frame_hardened`). It is stored as the origin it was given for, so
// another port, host or scheme turns it off, and a stored one is judged again
// on every load and dropped when it no longer passes (lib/browser.ts). The
// frame never gets top navigation, popups or downloads, either way.
//
// **Direct tabs (AO-2)** own their URL, origin grant and stopped-page state.
// The canonical pane model lives in workspace.ts; BrowserTabStrip is shared
// chrome for later Remote tabs. A separate window-wide budget retains at
// most 3 visited frames per pane and 8 total. Dormant tabs keep their state,
// and every URL/grant still faces the serving daemon's port rules.
// Terminal links add or select tabs, with no frame-focus gesture.

// **A framed page never takes the keyboard on its own** (R-1, amendment 2
// §E.8). The iframe is FrameHost's, made imperatively, and the frame guard
// (state/frameguard.ts) removes it the moment it gains focus without the
// owner's gesture: the pane then says "This page took the keyboard · Reload".
// The owner's own load — Load, Enter in the URL box, Project root, the
// keep-origin switch, Reload — is a gesture for that frame until 2 s after
// its `load`; a stored URL restored at mount and a terminal's "Open in
// Browser" are not. Reload loads the same URL again, judged again.

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { browserHost, judgeBrowserUrl, judgeKeepOrigin, keptOrigin, sandboxFor, workshopUrl } from "../lib/browser";
import { frameKey } from "../lib/frameguard";
import { noPointerFocus } from "../lib/talkkey";
import { BROWSER_TABS_MAX, browserTabId, type BrowserTabs, type BrowserTabSpec, type PaneSpec, type PaneNo } from "../lib/workspace";
import { useDaemonPorts } from "../state/daemonPorts";
import { cancelLoad, ownLoad, selectFrame } from "../state/frameguard";
import { FrameHost } from "./FrameHost";
import { BrowserTabStrip } from "./BrowserTabStrip";
import { BrowserProfile } from "./BrowserProfile";
import { RemoteView } from "./RemoteView";
import type { BrowserControl } from "../state/browser";
import { cleanTitle } from "../lib/remote";

export const BROWSER_FULL = "This pane has 12 tabs. Close one to open another.";

export function BrowserTab(props: {
  pane: number; slot: number; drawn: boolean; projectId: string | null;
  control?: BrowserControl; spec?: PaneSpec; zoom?: number;
  browser?: BrowserTabs; liveFrames: readonly string[]; blocked?: boolean; limitMessage?: boolean;
  onLoaded: (id: string, url: string) => void;
  onSubmitted: (id: string, url: string) => void;
  onKeepOrigin: (id: string, origin: string | null) => void;
  onAdd: (id: string) => void; onClose: (id: string) => void; onActivate: (id: string) => void;
  onProjectRoot?: (projectId: string) => void;
}) {
  const [emptyId] = useState(() => browserTabId());
  const [focusUrl, setFocusUrl] = useState<string | null>(null);
  const [full, setFull] = useState(false);
  const tabs = props.browser?.tabs ?? [];
  const active = props.browser?.active ?? "";
  const activate = (id: string) => {
    if (props.blocked) return;
    selectFrame(frameKey(props.pane, id));
    props.onActivate(id);
  };
  const shown = tabs.length ? tabs : [{ id: emptyId, url: "" }];
  const profile=props.control?.selectedProfile(props.pane as PaneNo)??null;
  return <div className="tabbody browserbody" onPointerDownCapture={()=>{if(!props.blocked)props.control?.focus(props.pane as PaneNo);}}
    onKeyDownCapture={e=>{if(!props.blocked&&!e.ctrlKey&&!e.altKey&&!e.metaKey&&e.key!=="Tab")props.control?.focus(props.pane as PaneNo);}}>
    {props.control?<BrowserProfile pane={props.pane as PaneNo} control={props.control} selected={profile}
      see={props.spec?.see!==false} blocked={!!props.blocked} privateEnded={props.spec?.browserPrivateEnded} />:null}
    {props.control?.error?<div className="pad err" data-testid="browser-owner-error">{props.control.error}</div>:null}
    <BrowserTabStrip tabs={tabs} active={active} blocked={props.blocked}
      onActivate={activate} onClose={(id) => { if (!props.blocked) { setFull(false); props.onClose(id); } }}
      onNew={() => {
        if (props.blocked) return;
        if (tabs.length >= BROWSER_TABS_MAX) { setFull(true); return; }
        const id = browserTabId(tabs.map((t) => t.id));
        setFull(false);
        setFocusUrl(id);
        props.onAdd(id);
      }} />
    {(full || props.limitMessage) && tabs.length >= BROWSER_TABS_MAX
      ? <div className="pad muted" role="status" data-testid="browser-full">{BROWSER_FULL}</div> : null}
    {shown.map((tab) => tab.mode==="remote"||profile==="private" ? <RemoteTab key={tab.id} {...props} tab={tab}
      active={!tabs.length||tab.id===active} focusUrl={focusUrl===tab.id} onFocused={()=>setFocusUrl(null)} /> :
      <DirectTab key={tab.id} {...props} tab={tab}
        active={!tabs.length || tab.id === active}
        live={props.liveFrames.includes(frameKey(props.pane, tab.id))}
        focusUrl={focusUrl === tab.id} onFocused={() => setFocusUrl(null)} />)}
  </div>;
}

function DirectTab(props: {
  pane: number; slot: number; drawn: boolean; projectId: string | null;
  tab: BrowserTabSpec; active: boolean; live: boolean; blocked?: boolean; control?: BrowserControl;
  focusUrl: boolean; onFocused: () => void;
  onLoaded: (id: string, url: string) => void;
  onSubmitted: (id: string, url: string) => void;
  onKeepOrigin: (id: string, origin: string | null) => void;
  onProjectRoot?: (projectId: string) => void;
}) {
  const { status, ports } = useDaemonPorts();
  const known = status !== "pending";
  const [typed, setTyped] = useState(props.tab.url);
  const [attempt, setAttempt] = useState(props.tab.url);
  const [storedUrl, setStoredUrl] = useState(props.tab.url);
  const [stolen, setStolen] = useState(false);
  const [load, setLoad] = useState(0);
  const [ownerError,setOwnerError]=useState("");
  if (storedUrl !== props.tab.url) {
    setStoredUrl(props.tab.url);
    setTyped(props.tab.url);
    setAttempt(props.tab.url);
    setStolen(false);
  }
  const input = useRef<HTMLInputElement>(null);
  const notice = useRef<HTMLDivElement>(null);
  const noticeWants = useRef(false);
  useLayoutEffect(() => {
    if (props.active && props.focusUrl && !props.blocked) {
      input.current?.focus();
      props.onFocused();
    }
  }, [props.active, props.focusUrl, props.blocked, props.onFocused]);
  useLayoutEffect(() => {
    if (!stolen || !noticeWants.current) return;
    noticeWants.current = false;
    const a = document.activeElement;
    if ((!a || a === document.body) && props.active) notice.current?.focus({ preventScroll: true });
  }, [stolen, props.active]);
  const key = frameKey(props.pane, props.tab.id);
  useEffect(() => () => cancelLoad(key), [key]);
  const verdict = known && attempt ? judgeBrowserUrl(attempt, ports) : null;
  const src = verdict?.ok ? verdict.url : "";
  const reason = verdict && !verdict.ok ? verdict.reason : "";
  const ownerGo=async(raw:string)=>{
    if(props.blocked)return;
    setOwnerError("");
    const direct=judgeBrowserUrl(raw,ports);
    if(!direct.ok)setAttempt(raw);
    try {
      await props.control!.submit(props.pane as PaneNo,props.tab.id,raw);
      ownLoad(key);setStolen(false);setLoad(n=>n+1);
    }catch(e){
      if(!direct.ok)props.onLoaded(props.tab.id,raw.length<=2048?raw:"");
      setOwnerError(cleanTitle(e instanceof Error?e.message:"The address could not be loaded.",300));
    }
  };
  const go = (raw: string, ownerAddress = false) => {
    if(ownerAddress&&props.control){void ownerGo(raw);return false;}
    if (props.blocked || !known) return false;
    const v = judgeBrowserUrl(raw, ports);
    setAttempt(raw);
    if (!v.ok) return false;
    ownLoad(key);
    setStolen(false);
    setLoad((n) => n + 1);
    setTyped(v.url);
    if (ownerAddress) props.onSubmitted(props.tab.id, v.url);
    else props.onLoaded(props.tab.id, v.url);
    return true;
  };
  // Includes dormant/evicted tabs: stale grants leave storage, not merely the DOM.
  useEffect(() => {
    if (status === "ok" && props.tab.keepOrigin && !keptOrigin(props.tab.keepOrigin, props.tab.url, ports)) {
      props.onKeepOrigin(props.tab.id, null);
    }
  }, [status, props.tab.id, props.tab.keepOrigin, props.tab.url, ports, props.onKeepOrigin]);
  const grant = status === "ok" && src ? keptOrigin(props.tab.keepOrigin, src, ports) : null;
  const offer = status === "ok" && src ? judgeKeepOrigin(src, ports) : null;
  const workshopPort = status === "ok" ? ports.workshop : null;
  return <div className="tabbody browserpage" data-browser-page={props.tab.id}
    style={props.active ? undefined : { display: "none" }} aria-hidden={!props.active}
    {...(!props.active ? { inert: "" } as Record<string, string> : {})}>
    {props.active ? <>
      <div className="toolbar">
        <input ref={input} data-testid="browser-url" value={typed} disabled={props.blocked}
          placeholder="http://localhost:5173/ or the project's workshop path"
          onChange={(e) => setTyped(e.target.value)}
          onKeyDown={(e) => { e.stopPropagation(); if (e.key === "Enter") go(typed, true); }} />
        <button type="button" data-testid="browser-go" disabled={!known || props.blocked} onClick={() => go(typed, true)}>Load</button>
        {props.control?<select aria-label="Browser mode" value="direct" disabled={props.blocked} onChange={e=>{if(e.target.value==="remote")void props.control!.convert(props.pane as PaneNo,props.tab.id,"remote").catch(e=>setOwnerError(cleanTitle(e.message,300)));}}><option value="direct">Direct</option><option value="remote">Remote</option></select>:null}
        <button type="button" data-testid="browser-project" disabled={!props.projectId || workshopPort === null || props.blocked}
          onClick={() => {
            if (!props.projectId || workshopPort === null) return;
            const u = workshopUrl(props.projectId, "index.html", workshopPort);
            if (go(u)) props.onProjectRoot?.(props.projectId);
          }}>Project root</button>
        {offer?.ok ? <label className="keeporigin" onMouseDown={noPointerFocus}
          title="Let this local page keep its storage, cookies and live reload. This grant belongs only to this tab and origin.">
          <input type="checkbox" data-testid="browser-keep-origin" data-origin={offer.origin}
            checked={!!grant} disabled={props.blocked}
            onChange={(e) => { if (!stolen) ownLoad(key); props.onKeepOrigin(props.tab.id, e.target.checked ? offer.origin : null); }}
            onKeyDown={(e) => e.stopPropagation()} onKeyUp={(e) => e.stopPropagation()} />
          Keep its origin
        </label> : null}
      </div>
      {props.tab.auto ? <div className="browserauto" data-testid="browser-auto">
        Opened for {props.tab.auto.label} · {browserHost(props.tab.url)}
      </div> : null}
      {reason && !ownerError ? <div className="pad err" data-testid="browser-refused">{reason}</div> : null}
      {ownerError?<div className="pad err" role="status" data-testid="browser-refused">{ownerError}</div>:null}
    </> : null}
    {src && stolen ? <div className="pad stolen" data-testid="browser-stolen" role="status" tabIndex={-1} ref={notice}>
      <span>This page took the keyboard</span><span aria-hidden="true">·</span>
      <button type="button" className="quiet" data-testid="browser-stolen-reload" disabled={props.blocked || !known}
        title="Load this page again" onClick={() => go(src)}>Reload</button>
    </div> : src && props.live ? <FrameHost pane={props.pane} tab={props.tab.id} slot={props.slot}
      drawn={props.drawn} active={props.active} src={src} sandbox={sandboxFor(!!grant)}
      keepOrigin={!!grant} load={load} onStolen={(focusNotice) => { noticeWants.current = focusNotice; setStolen(true); }} />
      : !src && props.active && !reason ? <div className="pad muted">Nothing loaded.</div> : null}
  </div>;
}

function RemoteTab(props:{pane:number;slot:number;drawn:boolean;projectId:string|null;tab:BrowserTabSpec;
  active:boolean;blocked?:boolean;control?:BrowserControl;spec?:PaneSpec;zoom?:number;focusUrl:boolean;onFocused:()=>void}) {
  const input=useRef<HTMLInputElement>(null);
  const [typed,setTyped]=useState(props.tab.url);const [error,setError]=useState("");
  const control=props.control;const pane=props.pane as PaneNo;
  const live=props.active&&props.drawn&&!!control?.online&&!control.covered;
  const runtime=control?.runtime[`${pane}:${props.tab.id}`];
  const profile=control?.selectedProfile(pane);
  useEffect(()=>setTyped(props.tab.url),[props.tab.url]);
  useEffect(()=>{
    if(live&&!props.blocked&&control)void control.ensure(pane,props.tab.id);
  },[live,props.blocked,props.tab.id,props.tab.remoteTabId,profile,props.spec?.browserCtx,control?.ensure,pane]);
  useLayoutEffect(()=>{if(props.active&&props.focusUrl&&!props.blocked){input.current?.focus();props.onFocused();}},[props.active,props.focusUrl,props.blocked,props.onFocused]);
  const action=async(job:()=>Promise<unknown>)=>{if(props.blocked)return;setError("");try{await job();}catch(e){setError(cleanTitle(e instanceof Error?e.message:"The Browser action failed.",300));}};
  return <div className="tabbody browserpage remotepage" data-browser-page={props.tab.id}
    style={props.active?undefined:{display:"none"}} aria-hidden={!props.active} {...(!props.active?{inert:""} as Record<string,string>:{})}>
    <div className="toolbar">
      <button aria-label="Back" disabled={props.blocked||!props.tab.remoteTabId} onClick={()=>void action(()=>control!.history(pane,props.tab.id,"back"))}>←</button>
      <button aria-label="Forward" disabled={props.blocked||!props.tab.remoteTabId} onClick={()=>void action(()=>control!.history(pane,props.tab.id,"forward"))}>→</button>
      <button aria-label="Reload" title="Reload page (ordinary reload)" disabled={props.blocked||!props.tab.remoteTabId} onClick={()=>void action(()=>control!.history(pane,props.tab.id,"reload"))}>↻</button>
      <input ref={input} data-testid="browser-url" aria-label="Browser address" value={typed} disabled={props.blocked} placeholder="https://example.com/"
        onChange={e=>setTyped(e.target.value)} onKeyDown={e=>{e.stopPropagation();if(e.key==="Enter")void action(()=>control!.submit(pane,props.tab.id,typed,"remote"));}} />
      <button data-testid="browser-go" disabled={props.blocked||runtime?.busy} onClick={()=>void action(()=>control!.submit(pane,props.tab.id,typed,"remote"))}>Load</button>
      <select aria-label="Browser mode" value="remote" disabled={props.blocked||profile==="private"} onChange={()=>void action(()=>control!.convert(pane,props.tab.id,"direct"))}><option value="direct">Direct</option><option value="remote">Remote</option></select>
    </div>
    {props.tab.auto?<div className="browserauto">Opened for {props.tab.auto.label}</div>:null}
    {runtime?.viewed?<div className="small muted" data-testid="browser-viewed">{runtime.viewed.refused?"Jarvis look refused":"Jarvis looked"} · {new Date(runtime.viewed.at*1000).toLocaleTimeString()}</div>:null}
    {error||runtime?.error?<div className="pad err" role="status">{error||runtime?.error}</div>:null}
    {runtime?.busy?<div className="pad muted" role="status">Opening Remote page…</div>:null}
    {props.tab.remoteTabId&&control?<RemoteView key={props.tab.remoteTabId} tabId={props.tab.remoteTabId} live={live} blocked={!!props.blocked} zoom={props.zoom??100} projectId={props.projectId}
      onOwnerFocus={()=>control.focus(pane)} onLocation={(url,title)=>control.location(pane,props.tab.id,props.tab.remoteTabId!,url,title)}
      onPopup={id=>void control.adopt(pane,props.tab.remoteTabId!,id)}
      onAction={what=>{if(what==="address")input.current?.focus();else void action(()=>control.history(pane,props.tab.id,what==="hardReload"?"reload":what));}} />:
      !runtime?.busy?<div className="pad muted">{props.spec?.browserPrivateEnded?"This Private session ended. Choose Private again to start a new session.":"Enter an address to open a Remote page."}</div>:null}
  </div>;
}
