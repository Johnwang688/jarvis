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

import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from "react";
import { createPortal } from "react-dom";
import { browserHost, judgeBrowserUrl, judgeKeepOrigin, keptOrigin, sandboxFor, workshopUrl } from "../lib/browser";
import { frameKey } from "../lib/frameguard";
import { noPointerFocus } from "../lib/talkkey";
import { BROWSER_TABS_MAX, browserTabId, type BrowserTabs, type BrowserTabSpec, type PaneSpec, type PaneNo } from "../lib/workspace";
import { useDaemonPorts } from "../state/daemonPorts";
import { cancelLoad, ownLoad, selectFrame } from "../state/frameguard";
import { FrameHost } from "./FrameHost";
import { BrowserTabStrip } from "./BrowserTabStrip";
import { BrowserCapture, type CaptureDestination } from "./BrowserCapture";
import { BrowserProfile } from "./BrowserProfile";
import { BrowserSpeaker } from "./BrowserSound";
import { browserApi } from "../api";
import { browserSound } from "../state/browserSoundClient";
import { browserSettings } from "../state/browserSettings";
import { RemoteView } from "./RemoteView";
import type { BrowserControl } from "../state/browser";
import { cleanTitle } from "../lib/remote";

interface OptionsProps {
  optionsOpen: boolean; optionsBlocked: boolean; optionsTarget: HTMLElement | null;
  toggleOptions: () => void; optionsAnchor: (node: HTMLButtonElement | null) => void;
  inputHeld: () => boolean; beforeCapture: () => Promise<void>;
  onAnnotating: (active: boolean) => void;
}
function OptionsToggle(props: OptionsProps & { pane: number }) {
  return <button ref={props.optionsAnchor} type="button" data-testid="browser-options-toggle"
    aria-label="Browser options" aria-expanded={props.optionsOpen}
    aria-controls={"browser-options-" + props.pane} aria-haspopup="dialog"
    disabled={props.optionsBlocked} onClick={props.toggleOptions}>⋯</button>;
}

export const BROWSER_FULL = "This pane has 12 tabs. Close one to open another.";

export function BrowserTab(props: {
  pane: number; slot: number; drawn: boolean; projectId: string | null;
  control?: BrowserControl; spec?: PaneSpec; zoom?: number; captureDestination?: CaptureDestination;
  slotOf?:(threadId:string)=>number|null;
  unseen?: readonly string[]; newTabNote?: string;
  browser?: BrowserTabs; liveFrames: readonly string[]; blocked?: boolean; limitMessage?: boolean;
  onLoaded: (id: string, url: string) => void;
  onSubmitted: (id: string, url: string) => void;
  onKeepOrigin: (id: string, origin: string | null) => void;
  onAdd: (id: string) => void; onClose: (id: string) => void; onActivate: (id: string) => void;
  onProjectRoot?: (projectId: string) => void; onSoundSettings?: () => void;
}) {
  const sound = useSyncExternalStore(browserSound.subscribe, browserSound.getSnapshot);
  useEffect(browserSound.start, []);
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
  const [optionsOpen, setOptionsOpen] = useState(false);
  const [optionsTarget, setOptionsTarget] = useState<HTMLDivElement | null>(null);
  const [statusTarget, setStatusTarget] = useState<HTMLDivElement | null>(null);
  const [annotating, setAnnotating] = useState(false);
  const [overlayOwner] = useState(() => ({}));
  const held = useRef(false), anchor = useRef<HTMLButtonElement | null>(null);
  const pane = props.pane as PaneNo, cover = props.control?.cover;
  const optionsBlocked = !!props.blocked || !props.drawn || !!props.control?.covered || annotating;
  const closeOptions = () => {
    held.current = annotating; setOptionsOpen(false);
    cover?.(pane, overlayOwner, annotating);
  };
  const toggleOptions = () => {
    if (optionsBlocked) return;
    const next = !held.current;
    // The current event must close local input before the visibility report.
    held.current = next; setOptionsOpen(next); cover?.(pane, overlayOwner, next);
  };
  useLayoutEffect(() => {
    if (optionsBlocked) closeOptions();
  }, [optionsBlocked]);
  useLayoutEffect(() => { closeOptions(); }, [active, profile]);
  useLayoutEffect(() => { held.current = optionsOpen || annotating; cover?.(pane, overlayOwner, held.current); },
    [cover, pane, overlayOwner, optionsOpen, annotating]);
  useLayoutEffect(() => () => cover?.(pane, overlayOwner, false), [cover, pane, overlayOwner]);
  useLayoutEffect(() => {
    if (optionsOpen && !optionsBlocked) optionsTarget?.parentElement?.querySelector<HTMLElement>("select:not(:disabled),button:not(:disabled),input:not(:disabled)")?.focus();
  }, [optionsOpen, optionsBlocked, optionsTarget]);
  useEffect(() => {
    if (!optionsOpen) return;
    const outside = (event: PointerEvent) => {
      if (!(event.target instanceof Node) || optionsTarget?.parentElement?.contains(event.target)
          || anchor.current?.contains(event.target)) return;
      // Cancelling pointerdown suppresses this gesture's compatibility mouse
      // press/release even if closing the panel commits before those events.
      event.preventDefault(); event.stopPropagation();
      closeOptions();
    };
    document.addEventListener("pointerdown", outside, true);
    return () => document.removeEventListener("pointerdown", outside, true);
  }, [optionsOpen, optionsTarget, annotating, cover, pane, overlayOwner]);
  const options: OptionsProps = {
    optionsOpen, optionsBlocked, optionsTarget, toggleOptions,
    optionsAnchor: node => { anchor.current = node; },
    inputHeld: () => held.current,
    beforeCapture: async () => {
      closeOptions();
      await props.control?.uncover(pane, tabs.length ? active : emptyId, overlayOwner);
    },
    onAnnotating: setAnnotating,
  };
  return <div className="tabbody browserbody" data-options-open={optionsOpen ? "true" : undefined} onPointerDownCapture={e=>{if(!(e.target as Element).closest("[data-browser-capture],[data-browser-options],[data-browser-sound]")&&!props.blocked)props.control?.focus(props.pane as PaneNo);}}
    onKeyDownCapture={e=>{
      if ((e.target as Element).closest("[data-browser-options]") && !props.blocked) {
        if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeOptions(); anchor.current?.focus(); }
        return;
      }
      if(!(e.target as Element).closest("[data-browser-capture],[data-browser-sound]")&&!props.blocked&&!e.ctrlKey&&!e.altKey&&!e.metaKey&&e.key!=="Tab")props.control?.focus(props.pane as PaneNo);
    }} onKeyDown={e=>{if((e.target as Element).closest("[data-browser-options]")&&!props.blocked)e.stopPropagation();}}
    onKeyUp={e=>{if((e.target as Element).closest("[data-browser-options]")&&!props.blocked)e.stopPropagation();}}
    onPointerDown={e=>{if((e.target as Element).closest("[data-browser-options]"))e.stopPropagation();}}
    onPointerUp={e=>{if((e.target as Element).closest("[data-browser-options]"))e.stopPropagation();}}>
    <div ref={setStatusTarget} className="browser-option-status" />
    <div id={"browser-options-" + props.pane} className="browser-options" data-browser-options=""
      data-testid="browser-options" role="dialog" aria-label="Browser options" hidden={!optionsOpen}
      {...(optionsBlocked ? { inert: "" } as Record<string, string> : {})}
      onPointerDown={e=>e.stopPropagation()} onPointerUp={e=>e.stopPropagation()}
      onKeyDown={e=>{
        if(props.blocked)return;
        e.stopPropagation();
        if(e.key==="Escape"){e.preventDefault();closeOptions();anchor.current?.focus();}
      }} onKeyUp={e=>e.stopPropagation()}>
      <div className="browser-options-heading">Browser options</div>
      {props.control?<BrowserProfile pane={pane} control={props.control} selected={profile}
        see={props.spec?.see!==false} blocked={optionsBlocked} statusTarget={statusTarget}
        privateEnded={props.spec?.browserPrivateEnded} />:null}
      <div ref={setOptionsTarget} />
    </div>
    {props.control?.error?<div className="pad err" data-testid="browser-owner-error">{props.control.error}</div>:null}
    {props.control?.cleanupError?<div className="pad err" role="status">{props.control.cleanupError} <button disabled={props.blocked||!props.control.online} onClick={()=>void props.control!.retryCleanup()}>Retry closing Browser sessions</button></div>:null}
    <BrowserTabStrip tabs={tabs} active={active} blocked={optionsBlocked} unseen={props.unseen}
      soundOn={sound.value?.instances.some(i=>i.profile===profile&&i.applied)}
      onActivate={activate} onClose={(id) => { if (!props.blocked) { setFull(false); props.onClose(id); } }}
      onNew={() => {
        if (props.blocked) return;
        if (tabs.length >= BROWSER_TABS_MAX) { setFull(true); return; }
        const id = browserTabId(tabs.map((t) => t.id));
        setFull(false);
        setFocusUrl(id);
        props.onAdd(id);
      }} />
    {props.newTabNote ? <div className="browser-newtab-note" data-testid="browser-new-tab-note">New tab · {props.newTabNote}</div> : null}
    {(full || props.limitMessage) && tabs.length >= BROWSER_TABS_MAX
      ? <div className="pad muted" role="status" data-testid="browser-full">{BROWSER_FULL}</div> : null}
    {shown.map((tab) => tab.mode==="remote"||profile==="private" ? <RemoteTab key={tab.id} {...props} {...options} tab={tab}
      active={!tabs.length||tab.id===active} focusUrl={focusUrl===tab.id} onFocused={()=>setFocusUrl(null)} /> :
      <DirectTab key={tab.id} {...props} {...options} tab={tab}
        active={!tabs.length || tab.id === active}
        live={props.liveFrames.includes(frameKey(props.pane, tab.id))}
        focusUrl={focusUrl === tab.id} onFocused={() => setFocusUrl(null)} />)}
  </div>;
}

function DirectTab(props: {
  pane: number; slot: number; drawn: boolean; projectId: string | null;
  tab: BrowserTabSpec; active: boolean; live: boolean; blocked?: boolean; control?: BrowserControl;
  captureDestination?: CaptureDestination; zoom?: number;
  focusUrl: boolean; onFocused: () => void;
  onLoaded: (id: string, url: string) => void;
  onSubmitted: (id: string, url: string) => void;
  onKeepOrigin: (id: string, origin: string | null) => void;
  onProjectRoot?: (projectId: string) => void;
} & OptionsProps) {
  const { status, ports } = useDaemonPorts();
  const known = status !== "pending";
  const [typed, setTyped] = useState(props.tab.url);
  const [attempt, setAttempt] = useState(props.tab.url);
  const [storedUrl, setStoredUrl] = useState(props.tab.url);
  const [stolen, setStolen] = useState(false);
  const [load, setLoad] = useState(0);
  const [ownerError,setOwnerError]=useState("");
  // An address this Direct tab cannot frame, on its way to Remote: its
  // refusal is not shown while the owner's submit runs (the tab turns Remote).
  const [opening,setOpening]=useState("");
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
  const refused = verdict && !verdict.ok ? verdict.reason : "";
  const reason = refused && !(opening && opening === attempt) ? refused : "";
  const ownerGo=async(raw:string)=>{
    if(props.optionsBlocked)return;
    setOwnerError("");
    const direct=judgeBrowserUrl(raw,ports);
    if(!direct.ok){setAttempt(raw);setOpening(raw);}
    try {
      await props.control!.submit(props.pane as PaneNo,props.tab.id,raw);
      // Normally this tab is Remote by now and gone; one still Direct says why.
      setOpening(o=>o===raw?"":o);
      ownLoad(key);setStolen(false);setLoad(n=>n+1);
    }catch(e){
      setOpening(o=>o===raw?"":o);
      if(!direct.ok)props.onLoaded(props.tab.id,raw.length<=2048?raw:"");
      setOwnerError(cleanTitle(e instanceof Error?e.message:"The address could not be loaded.",300));
    }
  };
  const go = (raw: string, ownerAddress = false) => {
    if(ownerAddress&&props.control){void ownerGo(raw);return false;}
    if (props.optionsBlocked || !known) return false;
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
  const viewed=props.control?.runtime[`${props.pane}:${props.tab.id}`]?.viewed;
  const page = useRef<HTMLDivElement>(null);
  // B-DIRECT-CAPTURE: the owner's switch and this tab's own live frame, read at the gesture.
  const direct = useRef({
    enabled: () => browserSettings.directCapture() === true,
    frame: () => page.current?.querySelector<HTMLIFrameElement>(":scope > .framehost > iframe[data-browser-frame]") ?? null,
  });
  return <div ref={page} className="tabbody browserpage" data-browser-page={props.tab.id}
    style={props.active ? undefined : { display: "none" }} aria-hidden={!props.active}
    {...(!props.active ? { inert: "" } as Record<string, string> : {})}>
    {props.active ? <>
      <div className="toolbar browser-navigation">
        <button aria-label="Back" disabled title="History is unavailable for this local preview">←</button>
        <button aria-label="Forward" disabled title="History is unavailable for this local preview">→</button>
        <button aria-label="Reload" disabled={props.optionsBlocked || !src || !known} onClick={() => go(src)}>↻</button>
        <input ref={input} data-testid="browser-url" value={typed} disabled={props.optionsBlocked}
          placeholder="http://localhost:5173/ or the project's workshop path"
          onChange={(e) => setTyped(e.target.value)}
          onKeyDown={(e) => { e.stopPropagation(); if (e.key === "Enter") go(typed, true); }} />
        <button type="button" data-testid="browser-go" disabled={!known || props.optionsBlocked} onClick={() => go(typed, true)}>Load</button>
        <OptionsToggle {...props} />
      </div>
      {props.optionsTarget ? createPortal(<div className="browser-project-options">
        <BrowserSpeaker direct blocked={props.optionsBlocked} />
        <button type="button" data-testid="browser-project" disabled={!props.projectId || workshopPort === null || props.optionsBlocked}
          onClick={() => {
            if (!props.projectId || workshopPort === null) return;
            const u = workshopUrl(props.projectId, "index.html", workshopPort);
            if (go(u)) props.onProjectRoot?.(props.projectId);
          }}>Project root</button>
        {offer?.ok ? <label className="keeporigin" onMouseDown={noPointerFocus}
          title="Let this local page keep its storage, cookies and live reload. This grant belongs only to this tab and origin.">
          <input type="checkbox" data-testid="browser-keep-origin" data-origin={offer.origin}
            checked={!!grant} disabled={props.optionsBlocked}
            onChange={(e) => { if (!stolen) ownLoad(key); props.onKeepOrigin(props.tab.id, e.target.checked ? offer.origin : null); }}
            onKeyDown={(e) => e.stopPropagation()} onKeyUp={(e) => e.stopPropagation()} />
          Keep its origin
        </label> : null}
      </div>, props.optionsTarget) : null}
      {props.tab.auto ? <div className="browserauto" data-testid="browser-auto">
        Opened for {props.tab.auto.label} · {browserHost(props.tab.url)}
      </div> : null}
      {viewed?<div className="small muted" data-testid="browser-viewed">{viewed.refused?"Jarvis look refused":"Jarvis looked"} · {new Date(viewed.at).toLocaleTimeString()}</div>:null}
      {reason && !ownerError ? <div className="pad err" data-testid="browser-refused">{reason}</div> : null}
      {refused && !reason && !ownerError ? <div className="pad muted" role="status" data-testid="browser-opening">Opening…</div> : null}
      {ownerError?<div className="pad err" role="status" data-testid="browser-refused">{ownerError}</div>:null}
    </> : null}
    {props.captureDestination && props.control ? <BrowserCapture remoteTabId={null}
      active={props.active} drawn={props.drawn} blocked={!!props.blocked} zoom={props.zoom ?? 100}
      identity={() => props.control!.captureIdentity(props.pane as PaneNo, props.tab.id)}
      destination={props.captureDestination}
      controlsTarget={props.optionsTarget} beforeCapture={props.beforeCapture}
      onAnnotating={props.onAnnotating} direct={direct.current}
      onRemote={() => void props.control!.convert(props.pane as PaneNo, props.tab.id, "remote")
        .catch(e => setOwnerError(cleanTitle(e.message, 300)))} /> : null}
    {src && stolen ? <div className="pad stolen" data-testid="browser-stolen" role="status" tabIndex={-1} ref={notice}>
      <span>This page took the keyboard</span><span aria-hidden="true">·</span>
      <button type="button" className="quiet" data-testid="browser-stolen-reload" disabled={props.blocked || !known}
        title="Load this page again" onClick={() => go(src)}>Reload</button>
    </div> : src && props.live ? <FrameHost pane={props.pane} tab={props.tab.id} slot={props.slot}
      drawn={props.drawn} active={props.active} src={src} sandbox={sandboxFor(!!grant)}
      keepOrigin={!!grant} load={load} onStolen={(focusNotice) => { noticeWants.current = focusNotice; setStolen(true); }} />
      : !src && props.active && !refused ? <div className="pad muted">Nothing loaded.</div> : null}
  </div>;
}

function RemoteTab(props:{pane:number;slot:number;drawn:boolean;projectId:string|null;tab:BrowserTabSpec;
  active:boolean;blocked?:boolean;control?:BrowserControl;spec?:PaneSpec;zoom?:number;captureDestination?:CaptureDestination;slotOf?:(threadId:string)=>number|null;focusUrl:boolean;onFocused:()=>void} & OptionsProps) {
  const input=useRef<HTMLInputElement>(null);
  const soundState = useSyncExternalStore(browserSound.subscribe, browserSound.getSnapshot);
  useEffect(browserSound.start, []);
  const soundInstance = soundState.value?.instances.find(row=>row.profile===props.control?.selectedProfile(props.pane as PaneNo));
  const soundUnavailable = soundInstance?.phase === "restarting" || soundInstance?.phase === "failed";
  const pageBlocked = props.optionsBlocked || soundUnavailable;
  const [typed,setTyped]=useState(props.tab.url);const [error,setError]=useState("");
  const control=props.control;const pane=props.pane as PaneNo;
  const live=props.active&&props.drawn&&!!control?.online&&!control.covered&&!soundUnavailable;
  const runtime=control?.runtime[`${pane}:${props.tab.id}`];
  const profile=control?.selectedProfile(pane);
  useEffect(()=>setTyped(props.tab.url),[props.tab.url]);
  useEffect(()=>{
    if(live&&!props.blocked&&control)void control.ensure(pane,props.tab.id);
  },[live,props.blocked,props.tab.id,props.tab.remoteTabId,profile,props.spec?.browserCtx,control?.ensure,pane]);
  useLayoutEffect(()=>{if(props.active&&props.focusUrl&&!props.blocked){input.current?.focus();props.onFocused();}},[props.active,props.focusUrl,props.blocked,props.onFocused]);
  const action=async(job:()=>Promise<unknown>)=>{if(pageBlocked)return;setError("");try{await job();}catch(e){setError(cleanTitle(e instanceof Error?e.message:"The Browser action failed.",300));}};
  return <div className="tabbody browserpage remotepage" data-browser-page={props.tab.id}
    style={props.active?undefined:{display:"none"}} aria-hidden={!props.active} {...(!props.active?{inert:""} as Record<string,string>:{})}>
    <div className="toolbar browser-navigation">
      <button aria-label="Back" disabled={pageBlocked||!props.tab.remoteTabId} onClick={()=>void action(()=>control!.history(pane,props.tab.id,"back"))}>←</button>
      <button aria-label="Forward" disabled={pageBlocked||!props.tab.remoteTabId} onClick={()=>void action(()=>control!.history(pane,props.tab.id,"forward"))}>→</button>
      <button aria-label="Reload" title="Reload page (ordinary reload)" disabled={pageBlocked||!props.tab.remoteTabId} onClick={()=>void action(()=>control!.history(pane,props.tab.id,"reload"))}>↻</button>
      <input ref={input} data-testid="browser-url" aria-label="Browser address" value={typed} disabled={pageBlocked} placeholder="https://example.com/"
        onChange={e=>setTyped(e.target.value)} onKeyDown={e=>{e.stopPropagation();if(e.key==="Enter")void action(()=>control!.submit(pane,props.tab.id,typed));}} />
      <button data-testid="browser-go" disabled={pageBlocked||runtime?.busy} onClick={()=>void action(()=>control!.submit(pane,props.tab.id,typed))}>Load</button>
      {props.active ? <OptionsToggle {...props} /> : null}
    </div>
    <div className="browser-sound-controls"><BrowserSpeaker direct={false} blocked={pageBlocked || !live || props.optionsOpen} inputHeld={props.inputHeld}
      tabId={props.tab.remoteTabId} profile={profile ?? undefined}
      profileLabel={profile==="private"?"Private":control?.profiles.find(p=>p.id===profile)?.label} /></div>
    {soundInstance?.applied ? <div className="small muted" role="status">Sound on for {control?.profiles.find(p=>p.id===profile)?.label ?? "this profile"} · all Remote tabs, including new tabs. Changing sites does not mute sound.</div> : null}
    {props.tab.auto?<div className="browserauto">Opened for {props.tab.auto.label}</div>:null}
    {runtime?.viewed?<div className="small muted" data-testid="browser-viewed">{runtime.viewed.refused?"Jarvis look refused":"Jarvis looked"} · {new Date(runtime.viewed.at).toLocaleTimeString()}</div>:null}
    {error||runtime?.error?<div className="pad err" role="status">{error||runtime?.error}</div>:null}
    {runtime?.busy?<div className="pad muted" role="status">Opening Remote page…</div>:null}
    {soundInstance?.phase === "restarting" ? <div className="pad muted" role="status">Restarting this Remote profile to apply sound…</div> : null}
    {soundInstance?.phase === "failed" ? <div className="pad err" role="status">This Remote profile could not restart.
      <button disabled={props.optionsBlocked || props.optionsOpen || !props.active || !props.drawn || !control?.online || control.covered || !props.tab.remoteTabId} onClick={()=>{
        const id=props.tab.remoteTabId;if(!id)return;
        void browserApi.write(`/tabs/${id}/sound-retry`,{}).then(()=>browserSound.refresh()).catch(()=>setError("The sound restart could not be requested. Try again."));
      }}>Retry sound restart</button></div> : null}
    {props.captureDestination && control ? <BrowserCapture key={`${props.tab.remoteTabId}:${soundInstance?.generation ?? 0}`} remoteTabId={props.tab.remoteTabId ?? null}
      active={props.active} drawn={props.drawn} blocked={!!props.blocked || soundUnavailable || !control.online || control.covered || !props.tab.remoteTabId}
      zoom={props.zoom ?? 100} identity={() => control.captureIdentity(pane, props.tab.id)}
      destination={props.captureDestination} onAnnotating={props.onAnnotating}
      controlsTarget={props.optionsTarget} beforeCapture={props.beforeCapture}
      onRemote={() => void action(() => control.ensure(pane, props.tab.id))} /> : null}
    {props.tab.remoteTabId&&control?<RemoteView key={props.tab.remoteTabId} tabId={props.tab.remoteTabId} instanceGeneration={soundInstance?.generation} live={live} blocked={pageBlocked || props.optionsOpen} inputHeld={props.inputHeld} zoom={props.zoom??100} projectId={props.projectId}
      slotOf={props.slotOf} onRetry={()=>control.ensure(pane,props.tab.id)} onOwnerFocus={()=>control.focus(pane)} onLocation={(url,title)=>control.location(pane,props.tab.id,props.tab.remoteTabId!,url,title)}
      onPopup={id=>void control.adopt(pane,props.tab.remoteTabId!,id)}
      onAction={what=>{if(what==="address")input.current?.focus();else void action(()=>control.history(pane,props.tab.id,what==="hardReload"?"reload":what));}} />:
      !runtime?.busy?<div className="pad muted">{props.spec?.browserPrivateEnded?"This Private session ended. Choose Private again to start a new session.":"Enter an address to open a Remote page."}</div>:null}
  </div>;
}
