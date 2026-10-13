import { act, createElement, useCallback, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { useBrowser, addressTarget, type BrowserControl } from "./browser";
import { defaultWorkspace, setBrowserProfile, setBrowserRemote, setBrowserSee, addBrowserTab, closeBrowserTab, submitBrowserUrl, setView, type Workspace } from "../lib/workspace";
import type { LayoutControl } from "../components/Layout";
import { DEFAULT_PORTS } from "../lib/browser";
const mocks=vi.hoisted(()=>({get:vi.fn(),write:vi.fn(),remove:vi.fn()}));
vi.mock("../api",()=>({browserApi:mocks,ApiError:class extends Error {status=404;}}));
vi.mock("./daemonPorts",()=>({useDaemonPorts:()=>({ports:DEFAULT_PORTS,status:"ok"})}));
vi.mock("../lib/windowId",()=>({WINDOW_ID:"a".repeat(32)}));
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
const initial=()=>addBrowserTab(setView(defaultWorkspace(),1,"browser"),1,"https://example.test/",{id:"page01",mode:"remote",source:{kind:"unknown"}});
let host:HTMLDivElement;let root:Root;let browser:BrowserControl;let workspace:Workspace;let set!:(fn:(w:Workspace)=>Workspace)=>void;
let props:{covered:boolean;blocked:boolean};let source:Workspace;
function Harness({covered,blocked}:typeof props){
  const [ws,setWs]=useState(source);set=setWs;workspace=ws;
  const view={ws,fit:{panes:[1]},
    setBrowserProfile:useCallback((...a:Parameters<typeof setBrowserProfile> extends [any,...infer T]?T:never)=>setWs(w=>setBrowserProfile(w,...a)),[]),
    setBrowserRemote:useCallback((...a:Parameters<typeof setBrowserRemote> extends [any,...infer T]?T:never)=>setWs(w=>setBrowserRemote(w,...a)),[]),
    setBrowserSee:useCallback((...a:Parameters<typeof setBrowserSee> extends [any,...infer T]?T:never)=>setWs(w=>setBrowserSee(w,...a)),[]),
    addBrowserTab:useCallback((...a:Parameters<typeof addBrowserTab> extends [any,...infer T]?T:never)=>setWs(w=>addBrowserTab(w,...a)),[]),
    submitBrowserUrl:useCallback((...a:Parameters<typeof submitBrowserUrl> extends [any,...infer T]?T:never)=>setWs(w=>submitBrowserUrl(w,...a)),[]),
  } as LayoutControl;
  browser=useBrowser(view,covered,blocked);return null;
}
const render=async(change:Partial<typeof props>={})=>{props={...props,...change};await act(async()=>root.render(createElement(Harness,props)));};
const deferred=<T,>()=>{let resolve!:(v:T)=>void;const promise=new Promise<T>(r=>resolve=r);return{resolve,promise};};
let front:any;
beforeEach(()=>{
  source=initial();props={covered:false,blocked:false};front=null;
  mocks.get.mockReset().mockImplementation((path)=>Promise.resolve(path==="/profiles"?[{id:"p-11111111",label:"Main",created:"now"}]:[]));
  mocks.remove.mockReset().mockResolvedValue({});
  mocks.write.mockReset().mockImplementation((path,body)=>{
    if(path==="/front")front=structuredClone(body);
    if(path.includes("/panes/")&&!path.endsWith("/address")){const p=front.panes.find((p:any)=>path.endsWith("/"+p.pane_id));p.see=body.see;}
    if(path==="/front"||path.startsWith("/panes/"))return Promise.resolve({window:body.window,seq:body.seq,panes:front.panes.map((p:any)=>({pane_id:p.pane_id,see:p.see,drive:false,tabs:p.tabs.map((t:any)=>({id:t.id,view_id:"v_"+"a".repeat(24),generation:1}))}))});
    if(path==="/tabs")return Promise.resolve({tab_id:"0123abcd",profile:body.profile,ctx:body.profile==="private"?"1234abcd":null,loaded:true});
    return Promise.resolve({loaded:true});
  });
  host=document.createElement("div");document.body.appendChild(host);root=createRoot(host);
});
afterEach(async()=>{await act(async()=>root.unmount());host.remove();vi.restoreAllMocks();});
async function connected(){await render();await act(async()=>browser.onEvent({kind:"_connected"}));}
it("chooses Remote for public URLs, Direct for loopback, and never bypasses control or scheme refusals",()=>{
  expect(addressTarget("https://example.test/",DEFAULT_PORTS,false).mode).toBe("remote");
  expect(addressTarget("http://localhost:5173/",DEFAULT_PORTS,false).mode).toBe("direct");
  expect(addressTarget("http://localhost:5173/",DEFAULT_PORTS,true).mode).toBe("remote");
  for(const url of ["http://localhost:8402/","http://127.0.0.1:8405/","javascript:alert(1)","https://user:secret@example.test/"])expect(()=>addressTarget(url,DEFAULT_PORTS,false,"remote")).toThrow();
});
it("publishes saved see-off before discovery and pins the default profile once",async()=>{
  source=setBrowserSee(source,1,false);await connected();expect(front.panes[0].see).toBe(false);expect(front.panes[0].drive).toBe(false);
  expect(workspace.panes[0].browserProfile).toBe("p-11111111");expect(mocks.write.mock.calls.some(([p])=>p==="/tabs")).toBe(false);
  mocks.get.mockResolvedValue([{id:"p-22222222",label:"Other",created:"now"}]);await act(async()=>browser.refresh());expect(workspace.panes[0].browserProfile).toBe("p-11111111");
});
it("lazy persistent restore uses the non-owner restore route and retains provenance",async()=>{
  await connected();await act(async()=>browser.ensure(1,"page01"));
  expect(mocks.write).toHaveBeenCalledWith("/tabs",{profile:"p-11111111"});
  expect(mocks.write).toHaveBeenCalledWith("/tabs/0123abcd/restore",{url:"https://example.test/"});
  expect(workspace.panes[0].browser!.tabs[0].source).toEqual({kind:"unknown"});expect(workspace.panes[0].browser!.tabs[0].remoteTabId).toBe("0123abcd");
});
it("never starts a covered or blocked pane",async()=>{
  await connected();await render({covered:true});await act(async()=>browser.ensure(1,"page01"));expect(front.covered).toBe(true);
  await render({covered:false,blocked:true});await act(async()=>browser.ensure(1,"page01"));expect(mocks.write.mock.calls.some(([p])=>p==="/tabs")).toBe(false);
});
it("cleans a newly created orphan when coverage changes before allocation returns",async()=>{
  await connected();const pending=deferred<any>();mocks.write.mockImplementation((path)=>path==="/tabs"?pending.promise:Promise.reject(new Error("not needed")));
  let job!:Promise<void>;await act(async()=>{job=browser.ensure(1,"page01");});await render({covered:true});
  await act(async()=>{pending.resolve({tab_id:"0123abcd",profile:"p-11111111",ctx:null});await job;});
  expect(mocks.remove).toHaveBeenCalledWith("/tabs/0123abcd");expect(workspace.panes[0].browser!.tabs[0].remoteTabId).toBeUndefined();
});
it("expired Private state never recreates its stored URL",async()=>{
  source=setBrowserProfile(source,1,"private");source.panes[0].browserPrivateEnded=true;await connected();await act(async()=>browser.ensure(1,"page01"));
  expect(mocks.write.mock.calls.some(([p])=>p==="/tabs")).toBe(false);expect(browser.runtime["1:page01"].error).toContain("Private session ended");
});
it("reattaches a surviving Private tab without navigating and clears only its ended marker",async()=>{
  source=setBrowserProfile(source,1,"private");source=setBrowserRemote(source,1,"page01",{mode:"remote",remoteTabId:"0123abcd",ctx:"1234abcd"});source.panes[0].browserPrivateEnded=true;
  await connected();mocks.get.mockResolvedValue([{tab_id:"0123abcd",profile:"private",ctx:"1234abcd",url:"https://private.test/",title:"Private"}]);
  await act(async()=>browser.ensure(1,"page01"));expect(mocks.write.mock.calls.some(([p])=>p.includes("restore")||p.includes("navigate")||p==="/tabs")).toBe(false);
  expect(workspace.panes[0].browserPrivateEnded).toBeUndefined();expect(workspace.panes[0].browser!.tabs[0].source).toEqual({kind:"unknown"});
});
it("does not display a permission change before the host acknowledges it",async()=>{
  await connected();const reply=deferred<any>();const saved=mocks.write.getMockImplementation()!;mocks.write.mockImplementation((p,...args)=>p==="/panes/1"?reply.promise:saved(p,...args));
  let job!:Promise<void>;await act(async()=>{job=browser.see(1,false);});expect(workspace.panes[0].see).toBeUndefined();
  const body=mocks.write.mock.calls.find(([p])=>p==="/panes/1")![1];await act(async()=>{reply.resolve({window:body.window,seq:body.seq,panes:[]});await job;});expect(workspace.panes[0].see).toBe(false);
});
it("retains a hidden Remote handle but closes it when its tab is removed",async()=>{
  source=setBrowserRemote(source,1,"page01",{mode:"remote",remoteTabId:"0123abcd"});await connected();await render({covered:true});expect(mocks.remove).not.toHaveBeenCalled();
  await act(async()=>set(w=>closeBrowserTab(w,1,"page01")));expect(mocks.remove).toHaveBeenCalledWith("/tabs/0123abcd");
});

it("accepts only timestamped viewed events for the current host receipt",async()=>{
  await connected();await act(async()=>browser.onEvent({kind:"browser_viewed",data:{tab_id:"v_"+"a".repeat(24),at:"2026-10-13T12:30:00Z",refused:true}}));
  expect(browser.runtime["1:page01"].viewed).toEqual({at:"2026-10-13T12:30:00Z",refused:true});
  await render({covered:true});await act(async()=>browser.onEvent({kind:"browser_viewed",data:{tab_id:"not-current",at:"2026-10-13T12:31:00Z",refused:false}}));
  expect(browser.runtime["1:page01"].viewed?.refused).toBe(true);
});

it("retains the acknowledged receipt after the permission model catches up",async()=>{
  await connected();expect(browser.captureIdentity(1,"page01")).not.toBeNull();
  await act(async()=>browser.see(1,false));
  expect(browser.captureIdentity(1,"page01")).toMatchObject({local_tab_id:"page01",view_id:"v_"+"a".repeat(24)});
});
it("an explicit profile conversion owns allocation before a lazy render can restore",async()=>{
  await connected();let resume!:FrameRequestCallback;
  vi.spyOn(window,"requestAnimationFrame").mockImplementation(fn=>{resume=fn;return 1;});
  let change!:Promise<void>;await act(async()=>{change=browser.profile(1,"private");});
  await act(async()=>browser.ensure(1,"page01"));
  await act(async()=>{resume(0);await change;});
  expect(mocks.write.mock.calls.some(([p])=>p.endsWith("/restore"))).toBe(false);
  expect(mocks.write).toHaveBeenCalledWith("/tabs/0123abcd/navigate",{url:"https://example.test/"});
});

it("keeps cleanup failure visible across receipts and lets the owner retry",async()=>{
  source=setBrowserRemote(source,1,"page01",{mode:"remote",remoteTabId:"0123abcd"});await connected();
  mocks.remove.mockRejectedValueOnce(new Error("busy"));await act(async()=>set(w=>closeBrowserTab(w,1,"page01")));
  expect(browser.cleanupError).toContain("finish closing");await act(async()=>browser.focus(1));expect(browser.cleanupError).toContain("finish closing");
  mocks.remove.mockResolvedValue({});await act(async()=>browser.retryCleanup());expect(mocks.remove).toHaveBeenCalledTimes(2);expect(browser.cleanupError).toBe("");
});

it("pane options revoke drawn eligibility while preserving preferences and exact instance ownership", async () => {
  source=setBrowserSee(source,1,false); await connected();
  const oldOwner={}, newOwner={};
  await act(async()=>browser.cover(1,oldOwner,true));
  expect(front.panes[0]).toMatchObject({drawn:false,slot:1,see:false,drive:false});
  expect(workspace.panes[0].see).toBe(false);
  expect(browser.captureIdentity(1,"page01")).toBeNull();
  await act(async()=>browser.cover(1,newOwner,true));
  await act(async()=>browser.cover(1,oldOwner,false));
  expect(front.panes[0].drawn).toBe(false);
  await act(async()=>browser.uncover(1,"page01",newOwner));
  expect(front.panes[0].drawn).toBe(true);
  expect(browser.captureIdentity(1,"page01")).not.toBeNull();
});
it("uncover waits for a fresh receipt and rejects a card arriving during that wait", async () => {
  await connected(); const owner={}; await act(async()=>browser.cover(1,owner,true));
  const ready=deferred<any>(), saved=mocks.write.getMockImplementation()!;
  mocks.write.mockImplementation((p,body,...args)=>p==="/front"&&body.panes[0].drawn?ready.promise:saved(p,body,...args));
  let job!:Promise<void>, result!:Promise<unknown>;
  await act(async()=>{job=browser.uncover(1,"page01",owner);result=job.catch(e=>e);});
  expect(browser.captureIdentity(1,"page01")).toBeNull();
  await render({blocked:true});
  let answer:any; await act(async()=>{answer=await result;});
  expect(answer).toBeInstanceOf(Error);
  expect(answer.message).toContain("cancelled");
});
it("profile, context or active-tab replacement invalidates a waiting uncover gesture", async () => {
  for(const change of [
    (w:Workspace)=>setBrowserProfile(w,1,"private"),
    (w:Workspace)=>setBrowserRemote(w,1,"page01",{mode:"remote",remoteTabId:"0123abcd",ctx:"1234abcd"}),
    (w:Workspace)=>addBrowserTab(w,1,"http://localhost:5173/",{id:"other1"}),
  ]){
    await connected(); const owner={}; await act(async()=>browser.cover(1,owner,true));
    const saved=mocks.write.getMockImplementation()!, ready=deferred<any>();
    mocks.write.mockImplementation((p,body,...args)=>p==="/front"&&body.panes[0].drawn?ready.promise:saved(p,body,...args));
    let result!:Promise<unknown>; await act(async()=>{result=browser.uncover(1,"page01",owner).catch(e=>e);});
    await act(async()=>set(change));
    let answer:any; await act(async()=>{answer=await result;});
    expect(answer).toBeInstanceOf(Error);
    mocks.write.mockImplementation(saved);
  }
});
