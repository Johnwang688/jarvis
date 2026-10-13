// Owner-only Browser coordination. One publisher orders discovery, permissions
// and explicit URL entry; passive location messages never reset provenance.
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { ApiError, browserApi } from "../api";
import type { LayoutControl } from "../components/Layout";
import { browserFront, BrowserPublisher, type BrowserReceipt } from "../lib/browserowner";
import { isLoopbackHost, judgeBrowserUrl, type DaemonPorts } from "../lib/browser";
import { cleanTitle } from "../lib/remote";
import { browserTabId, type PaneNo } from "../lib/workspace";
import { WINDOW_ID } from "../lib/windowId";
import { useDaemonPorts } from "./daemonPorts";

export interface BrowserProfile { id:string; label:string; created:string }
export interface RemoteRow { tab_id:string; profile:string; ctx:string|null; url?:string; title?:string; opener?:string|null; loaded?:boolean; error?:string }
export interface BrowserRuntime { busy?:boolean; error?:string; viewed?:{at:string;refused:boolean} }
const tabKey = (pane:PaneNo,id:string) => `${pane}:${id}`;
const message = (e:unknown) => cleanTitle(e instanceof Error ? e.message : "The Browser could not apply that change.", 300);
const frame = () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
export function addressTarget(raw:string, ports:DaemonPorts, privatePane:boolean, chosen?:"direct"|"remote") {
  const text=raw.trim();
  if (text.length>2048 || /[\x00-\x1f\x7f]/.test(text)) throw new Error("That address is too long or contains control characters.");
  let url:URL;
  try { url=new URL(text); } catch { throw new Error("Enter a complete http or https URL."); }
  if (!["http:","https:"].includes(url.protocol) || url.username || url.password) throw new Error("Use an http or https page without a user name or password.");
  const local=isLoopbackHost(url.hostname);
  const forbidden=[8402,8405,ports.hud,ports.api,ports.self];
  if (local && forbidden.includes(Number(url.port || (url.protocol==="https:" ? 443:80)))) throw new Error(judgeBrowserUrl(text,ports).reason || "Refused: that address is one of Jarvis's own ports.");
  const mode=privatePane ? "remote" : chosen ?? (local ? "direct":"remote");
  if (mode==="direct") { const v=judgeBrowserUrl(text,ports); if (!v.ok) throw new Error(v.reason); return {url:v.url,mode}; }
  return {url:url.href,mode};
}

export function useBrowser(view:LayoutControl, covered:boolean, blocked:boolean) {
  const {ports,status}=useDaemonPorts();
  const [hidden,setHidden]=useState(document.hidden);
  covered=covered||hidden;
  useEffect(()=>{const changed=()=>setHidden(document.hidden);document.addEventListener("visibilitychange",changed);return()=>document.removeEventListener("visibilitychange",changed);},[]);
  const [profiles,setProfiles]=useState<BrowserProfile[]>([]);
  const [runtime,setRuntime]=useState<Record<string,BrowserRuntime>>({});
  const [error,setError]=useState("");
  const [cleanupError,setCleanupError]=useState("");
  const retired=useRef(new Set<string>());
  const [online,setOnline]=useState(false);
  const [focus,setFocus]=useState<{pane:PaneNo|null;seq:number}>({pane:null,seq:0});
  const now=useRef({view,covered,blocked,ports,status,profiles,online,focus});
  now.current={view,covered,blocked,ports,status,profiles,online,focus};
  const mounted=useRef(true);
  const epoch=useRef(0);
  const work=useRef(new Map<string,object>());
  const transitions=useRef(new Set<string>());
  const receipts=useRef<BrowserReceipt|null>(null);
  const previous=useRef(view.ws);
  const frontCode=useRef("");
  const refreshNumber=useRef(0);
  const patch=useCallback((key:string,value:BrowserRuntime) => {
    if (mounted.current) setRuntime((old)=>({...old,[key]:{...old[key],...value}}));
  },[]);
  const [publisher]=useState(()=>new BrowserPublisher(
    (path,method,body,signal)=>browserApi.write<BrowserReceipt>(path.slice(8),body,method,signal),
    (reply,front)=>{frontCode.current=JSON.stringify(front);receipts.current=reply; if(mounted.current)setError("");},
    (text)=>{if(mounted.current)setError(text);},
  ));
  const selectedProfile=useCallback((pane:PaneNo)=>now.current.view.ws.panes[pane-1].browserProfile ?? now.current.profiles[0]?.id ?? null,[]);
  const tab=useCallback((pane:PaneNo,id:string)=>now.current.view.ws.panes[pane-1].browser?.tabs.find(t=>t.id===id),[]);
  const allowed=useCallback((pane:PaneNo)=>{
    const c=now.current;
    return mounted.current && c.online && !c.covered && !c.blocked && c.view.fit.panes.includes(pane)
      && c.view.ws.panes[pane-1].view==="browser";
  },[]);
  const publish=useCallback(()=>{
    const c=now.current;
    if (!WINDOW_ID || c.status==="pending") return;
    const front=browserFront(c.view.ws,c.view.fit.panes,c.covered,WINDOW_ID,c.profiles[0]?.id??null,c.focus.pane,c.focus.seq,c.ports);
    const encoded=JSON.stringify(front);
    if(encoded!==frontCode.current){receipts.current=null;frontCode.current=encoded;}
    publisher.update(front);
  },[publisher]);
  const refresh=useCallback(async()=>{
    const seq=++refreshNumber.current;
    try {
      const rows=await browserApi.get<BrowserProfile[]>("/profiles");
      if(mounted.current && seq===refreshNumber.current) setProfiles(rows.filter(p=>/^p-[0-9a-f]{8}$/.test(p.id)).map(p=>({...p,label:cleanTitle(p.label,32)})));
    } catch(e) {if(mounted.current && seq===refreshNumber.current)setError(message(e));}
  },[]);
  const onEvent=useCallback((event:{kind:string;data?:any})=>{
    if(event.kind==="_connected") { now.current.online=true; setOnline(true); publisher.connect(true); void refresh(); }
    if(event.kind==="_disconnected") { now.current.online=false; epoch.current++; setOnline(false); publisher.connect(false); receipts.current=null; }
    if(event.kind==="browser_viewed") {
      const data=event.data;
      if(!data || typeof data.tab_id!=="string" || typeof data.at!=="string"||!Number.isFinite(Date.parse(data.at)) || typeof data.refused!=="boolean")return;
      const front=receipts.current;
      if(!front)return;
      for(const pane of front.panes) for(const binding of pane.tabs) if(binding.view_id===data.tab_id) {
        const current=tab(pane.pane_id as PaneNo,binding.id);
        if(current)patch(tabKey(pane.pane_id as PaneNo,binding.id),{viewed:{at:data.at,refused:data.refused}});
      }
    }
    if(event.kind==="browser_profiles_changed") void refresh();
    if(event.kind==="browser_sound_changed") {
      // Passive ordered rebind preserves source/see/focus leases. Existing
      // capture identities become unavailable until the new receipt arrives.
      receipts.current=null;
      publisher.heartbeat();
    }
  },[publisher,refresh,tab,patch]);
  useLayoutEffect(()=>{
    // Pin the initial default once. Deleting it later must show a missing
    // profile, not silently adopt whichever profile moved to the first row.
    const first=profiles[0]?.id;
    if(first) for(const id of [...view.ws.order,...view.ws.kept]) {
      const spec=view.ws.panes[id-1];
      if(spec.view==="browser"&&!spec.browserProfile)view.setBrowserProfile(id,first,true);
    }
  },[profiles,view.ws,view.setBrowserProfile]);
  useLayoutEffect(()=>{ publish(); },[view.ws,view.fit.panes,covered,focus,profiles,ports,status,publish]);
  useEffect(()=>{ const timer=setInterval(()=>{if(now.current.online) {publisher.heartbeat(); void refresh();}},20000); return()=>clearInterval(timer); },[publisher,refresh]);
  useEffect(()=>{
    mounted.current=true;
    return()=>{mounted.current=false;epoch.current++;refreshNumber.current++;publisher.dispose();work.current.clear();};
  },[publisher]);
  const closeRemote=useCallback(async(path:string)=>{
    const panes=now.current.view.ws.panes;
    const retained=path.startsWith("/contexts/")?panes.some(p=>p.browserCtx===path.slice(10)):
      panes.some(p=>p.browser?.tabs.some(t=>t.remoteTabId===path.slice(6)));
    if(retained){retired.current.delete(path);if(mounted.current&&!retired.current.size)setCleanupError("");return;}
    try {await browserApi.remove(path);retired.current.delete(path);}
    catch(e){if(e instanceof ApiError&&e.status===404)retired.current.delete(path);else retired.current.add(path);}
    if(mounted.current)setCleanupError(retired.current.size?"A Browser session could not finish closing.":"");
  },[]);
  const retryCleanup=useCallback(async()=>{if(now.current.blocked||!now.current.online)return;await Promise.all([...retired.current].map(closeRemote));},[closeRemote]);
  const orphan=useCallback(async(row:RemoteRow)=>closeRemote(`/tabs/${row.tab_id}`),[closeRemote]);
  useEffect(()=>{
    const old=previous.current; previous.current=view.ws;
    const handles=new Set(view.ws.panes.flatMap(p=>p.browser?.tabs.map(t=>t.remoteTabId).filter(Boolean)??[]));
    const contexts=new Set(view.ws.panes.map(p=>p.browserCtx).filter(Boolean));
    const goneContexts=new Set(old.panes.map(p=>p.browserCtx).filter((c):c is string=>!!c&&!contexts.has(c)));
    for(const ctx of goneContexts) void closeRemote(`/contexts/${ctx}`);
    for(const p of old.panes) for(const t of p.browser?.tabs??[]) if(t.remoteTabId&&!handles.has(t.remoteTabId)&&!goneContexts.has(p.browserCtx??"")) void orphan({tab_id:t.remoteTabId,profile:p.browserProfile??"",ctx:p.browserCtx??null});
  },[view.ws,orphan,closeRemote]);

  const ensure=useCallback(async(pane:PaneNo,id:string,ownerConversion=false)=>{
    const key=tabKey(pane,id); const initial=tab(pane,id); const c=now.current;
    const profile=selectedProfile(pane); const spec=c.view.ws.panes[pane-1];
    if(!initial || !profile || !allowed(pane) || spec.browser?.active!==id || work.current.has(key) || transitions.current.has(key))return;
    const token={}; const era=epoch.current; work.current.set(key,token); patch(key,{busy:true,error:""});
    const current=()=>allowed(pane)&&epoch.current===era&&work.current.get(key)===token&&tab(pane,id)===initial&&selectedProfile(pane)===profile&&now.current.view.ws.panes[pane-1].browser?.active===id;
    let created:RemoteRow|null=null;
    try {
      const rows=await browserApi.get<RemoteRow[]>("/tabs");
      if(!current())return;
      const found=rows.find(row=>row.tab_id===initial.remoteTabId&&row.profile===profile&&row.ctx===(spec.browserCtx??null));
      if(found) {c.view.setBrowserRemote(pane,id,{mode:"remote",remoteTabId:found.tab_id,url:found.url??initial.url,title:found.title,ctx:found.ctx??undefined});return;}
      if(profile==="private"&&((spec.browserPrivateEnded&&!spec.browserCtx)||(spec.browserCtx&&!rows.some(row=>row.profile==="private"&&row.ctx===spec.browserCtx)))) throw new Error("This Private session ended. Choose Private again to start a new session.");
      created=await browserApi.write<RemoteRow>("/tabs",{profile,...(spec.browserCtx?{ctx:spec.browserCtx}:{})});
      if(!current())return;
      if(initial.url) {
        const reply=await browserApi.write<RemoteRow>(`/tabs/${created.tab_id}/${ownerConversion?"navigate":"restore"}`,{url:initial.url});
        if(!current())return;
        if(reply.loaded===false)patch(key,{error:"This page was not restored. Enter its address and choose Load to try again."});
      }
      c.view.setBrowserRemote(pane,id,{mode:"remote",remoteTabId:created.tab_id,ctx:created.ctx??undefined});
      created=null; // the state update adopts only this generation's new handle
    } catch(e) {if(current())patch(key,{error:message(e)});}
    finally {if(created)await orphan(created);if(work.current.get(key)===token){work.current.delete(key);patch(key,{busy:false});}}
  },[tab,selectedProfile,allowed,patch,orphan]);

  const submit=useCallback(async(pane:PaneNo,id:string,raw:string,mode?:"direct"|"remote")=>{
    const key=tabKey(pane,id); if(!allowed(pane))throw new Error("Wait for this Browser pane to reconnect.");
    const c=now.current; const target=addressTarget(raw,c.ports,selectedProfile(pane)==="private",mode);
    if(!tab(pane,id)) { c.view.addBrowserTab(pane,"",{id}); for(let n=0;n<6&&!tab(pane,id);n++)await frame(); }
    if(!tab(pane,id)||!allowed(pane))throw new Error("That Browser tab is no longer available.");
    publish();patch(key,{busy:true,error:""});
    try {
      const reply=await publisher.mutate(`/browser/panes/${pane}/tabs/${id}/address`,"POST",target,(reply,front)=>({...front,panes:front.panes.map(p=>p.pane_id!==pane?p:{...p,ctx:reply.ctx??p.ctx,tabs:p.tabs.map(t=>t.id!==id?t:target.mode==="direct"?{id,mode:"direct",url:reply.url??target.url,source:null}:{id,mode:"remote",tab_id:reply.tab_id??null,source:null})})}));
      if(!allowed(pane)||!tab(pane,id))return;
      now.current.view.setBrowserRemote(pane,id,{mode:target.mode,remoteTabId:reply.tab_id??undefined,url:reply.url??target.url,ctx:reply.ctx??undefined});
      now.current.view.submitBrowserUrl(pane,reply.url??target.url,id);
    } catch(e) {patch(key,{error:message(e)});throw e;}
    finally {patch(key,{busy:false});}
  },[allowed,tab,publish,publisher,patch,selectedProfile]);
  const see=useCallback(async(pane:PaneNo,value:boolean)=>{
    if(!allowed(pane))return;
    publish();
    await publisher.mutate(`/browser/panes/${pane}`,"PATCH",{see:value},(_,front)=>({...front,panes:front.panes.map(p=>p.pane_id===pane?{...p,see:value,drive:false}:p)}));
    now.current.view.setBrowserSee(pane,value);
  },[allowed,publish,publisher]);
  const profile=useCallback(async(pane:PaneNo,value:string)=>{
    if(!allowed(pane))return;
    const before=now.current.view.ws.panes[pane-1].browser?.active;
    const key=before?tabKey(pane,before):null;
    if(key)transitions.current.add(key);
    try {now.current.view.setBrowserProfile(pane,value);await frame();}
    finally {if(key)transitions.current.delete(key);}
    const active=now.current.view.ws.panes[pane-1].browser?.active;
    if(active&&allowed(pane)&&selectedProfile(pane)===value)await ensure(pane,active,true);
  },[allowed,ensure,selectedProfile]);
  const convert=useCallback(async(pane:PaneNo,id:string,mode:"direct"|"remote")=>{
    if(!allowed(pane))return;
    const t=tab(pane,id);if(!t)return;
    if(mode==="direct") {
      if(selectedProfile(pane)==="private")throw new Error("Private pages use Remote mode.");
      if(t.url)addressTarget(t.url,now.current.ports,false,"direct");
    }
    const key=tabKey(pane,id);transitions.current.add(key);
    try {now.current.view.setBrowserRemote(pane,id,{mode,url:t.url});await frame();}
    finally {transitions.current.delete(key);}
    if(mode==="remote"&&allowed(pane))await ensure(pane,id,true);
  },[allowed,tab,selectedProfile,ensure]);
  const signin=useCallback(async(pane:PaneNo)=>{
    if(!allowed(pane))return;
    const profile=selectedProfile(pane);if(!profile||profile==="private")return;
    const spec=now.current.view.ws.panes[pane-1];
    if((spec.browser?.tabs.length??0)>=12)throw new Error("Close a tab before opening Chrome sign-in.");
    const row=await browserApi.write<RemoteRow>(`/profiles/${profile}/signin`);
    if(!allowed(pane)||now.current.view.ws.panes[pane-1]!==spec){await orphan(row);return;}
    const id=browserTabId(spec.browser?.tabs.map(t=>t.id)??[]);
    now.current.view.addBrowserTab(pane,"",{id,mode:"remote"});
    now.current.view.setBrowserRemote(pane,id,{mode:"remote",remoteTabId:row.tab_id});
  },[allowed,selectedProfile,orphan]);
  const location=useCallback((pane:PaneNo,id:string,remoteId:string,url:string,title:string)=>{
    const t=tab(pane,id);if(!t||t.remoteTabId!==remoteId||t.mode!=="remote")return;
    if(t.url!==url)patch(tabKey(pane,id),{viewed:undefined});
    if(t.url!==url||t.title!==title)now.current.view.setBrowserRemote(pane,id,{mode:"remote",remoteTabId:remoteId,url,title});
  },[tab,patch]);
  const history=useCallback(async(pane:PaneNo,id:string,action:"back"|"forward"|"reload")=>{
    if(!allowed(pane))return;
    const t=tab(pane,id);if(!t?.remoteTabId)return;
    const reply=await browserApi.write<RemoteRow>(`/tabs/${t.remoteTabId}/history`,{action});
    if(reply.loaded===false)throw new Error("The page could not complete that navigation.");
  },[allowed,tab]);
  const adopt=useCallback(async(pane:PaneNo,opener:string,remoteId:string)=>{
    if(!allowed(pane)||!/^([0-9a-f]{8})$/.test(remoteId))return;
    const initial=now.current.view.ws.panes[pane-1];
    const rows=await browserApi.get<RemoteRow[]>("/tabs");
    const row=rows.find(r=>r.tab_id===remoteId&&r.opener===opener&&r.profile===selectedProfile(pane)&&r.ctx===(initial.browserCtx??null));
    if(!row)return;
    if(!allowed(pane)||now.current.view.ws.panes[pane-1]!==initial){await orphan(row);return;}
    if((initial.browser?.tabs.length??0)>=12){await orphan(row);setError("This pane has 12 tabs. Close one to open another.");return;}
    if(now.current.view.ws.panes.some(p=>p.browser?.tabs.some(t=>t.remoteTabId===remoteId)))return;
    const id=browserTabId(initial.browser?.tabs.map(t=>t.id)??[]);
    const parent=initial.browser?.tabs.find(t=>t.remoteTabId===opener);
    now.current.view.addBrowserTab(pane,row.url??"",{id,mode:"remote",...((parent?.source||parent?.auto)?{source:{kind:"unknown" as const}}:{})});
    now.current.view.setBrowserRemote(pane,id,{mode:"remote",remoteTabId:remoteId,url:row.url,title:row.title,ctx:row.ctx??undefined});
  },[allowed,selectedProfile,orphan]);
  const focusPane=useCallback((pane:PaneNo)=>{
    if(!allowed(pane))return;
    const next={pane,seq:now.current.focus.seq+1};now.current.focus=next;setFocus(next);publish();
  },[allowed,publish]);
  const captureIdentity=useCallback((pane:PaneNo,id:string)=>{
    if(!allowed(pane)||now.current.view.ws.panes[pane-1].browser?.active!==id)return null;
    const binding=receipts.current?.panes.find(p=>p.pane_id===pane)?.tabs.find(t=>t.id===id);
    return binding?{window:WINDOW_ID,pane_id:pane,local_tab_id:id,view_id:binding.view_id}:null;
  },[allowed]);
  return {profiles,runtime,error,cleanupError,retryCleanup,online,covered,captureIdentity,refresh,onEvent,ensure,submit,see,profile,convert,signin,location,history,adopt,focus:focusPane,selectedProfile};
}
export type BrowserControl=ReturnType<typeof useBrowser>;
