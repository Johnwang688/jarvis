import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { BrowserApprovalBody, useBrowserApproval } from "./BrowserApproval";
import type { BrowserApprovalPublic } from "../lib/browserapproval";
const mocks=vi.hoisted(()=>({get:vi.fn()}));vi.mock("../api",()=>({browserApi:{get:mocks.get}}));
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
let host:HTMLDivElement,root:Root,current:ReturnType<typeof useBrowserApproval>;
const issued="2026-10-13T00:00:00Z";
const request:BrowserApprovalPublic={req_id:"request_1234",code:"ABCD",tool:"hud_browser_act",layer:"browser-gate",thread_id:"thread",hud_only:true,asked_at:issued,timeout_s:120};
function details(req=request){const args={action:"type",pane:"2",target:"e1",text:"<img src=x onerror=bad>",file:""};return{...req,allowlistable:false,args,command:JSON.stringify(args),browser:{tab_id:"0123abcd",action_id:"a".repeat(32),pane:2,page_identity:1,document_generation:2,rect:null,action:"type",category:"ordinary",host:"example.test",path:"/path",target:"Untrusted label",text:args.text}};}
function Probe({req}:{req:BrowserApprovalPublic}){current=useBrowserApproval(req);return current.detail?createElement(BrowserApprovalBody,{detail:current.detail}):createElement("p",{},current.message);}
async function render(req=request){await act(async()=>root.render(createElement(Probe,{req})));}
function deferred(){let resolve!:(v:unknown)=>void;const promise=new Promise(r=>resolve=r);return{promise,resolve};}
beforeEach(()=>{vi.useFakeTimers();vi.setSystemTime(Date.parse(issued)+1000);mocks.get.mockReset().mockResolvedValue(details());host=document.createElement("div");document.body.appendChild(host);root=createRoot(host);});
afterEach(async()=>{await act(async()=>root.unmount());host.remove();vi.useRealTimers();});
it("loads only the exact owner detail route, renders inert text and never captures pixels",async()=>{
  const storage=vi.spyOn(Storage.prototype,"setItem");await render();
  expect(mocks.get).toHaveBeenCalledExactlyOnceWith("/approvals/request_1234",expect.any(AbortSignal));
  expect(host.textContent).toContain("Preview unavailable");expect(host.textContent).toContain("<img src=x onerror=bad>");
  expect(host.querySelector("img,canvas,svg")).toBeNull();expect(current.canAuthorize()).toBe(true);expect(storage).not.toHaveBeenCalled();storage.mockRestore();
});
it("aborts and discards late A details after switching to B, without exposing A",async()=>{
  const a=deferred(),b=deferred();mocks.get.mockReturnValueOnce(a.promise).mockReturnValueOnce(b.promise);await render();
  const oldAllowed=current.canAuthorize,signal=mocks.get.mock.calls[0][1] as AbortSignal;
  const second={...request,req_id:"request_5678"};await render(second);expect(signal.aborted).toBe(true);
  await act(async()=>a.resolve(details()));expect(host.textContent).not.toContain("example.test");expect(current.canAuthorize()).toBe(false);expect(oldAllowed()).toBe(false);
  await act(async()=>b.resolve(details(second)));expect(current.detail?.reqId).toBe(second.req_id);expect(current.canAuthorize()).toBe(true);
});
it("does not authorize after unmount, even when an external callback was retained",async()=>{
  await render();const oldAllowed=current.canAuthorize;await act(async()=>root.render(null));expect(oldAllowed()).toBe(false);
});
it("has a fixed deadline across delayed load and same-request rerenders",async()=>{
  const late=deferred();mocks.get.mockReturnValueOnce(late.promise);await render();await act(async()=>vi.advanceTimersByTime(118500));
  await act(async()=>late.resolve(details()));expect(current.canAuthorize()).toBe(true);await render();
  await act(async()=>vi.advanceTimersByTime(500));expect(current.canAuthorize()).toBe(false);expect(current.expired).toBe(true);expect(vi.getTimerCount()).toBe(0);
});
it("refuses malformed, wrong-identity and failed details with fixed wording only",async()=>{
  mocks.get.mockResolvedValueOnce({...details(),req_id:"request_5678"});await render();expect(current.canAuthorize()).toBe(false);expect(host.textContent).toContain("unavailable");
  mocks.get.mockRejectedValueOnce(new Error("PRIVATE RAW ERROR"));await render({...request,req_id:"request_5678"});
  expect(host.textContent).not.toContain("PRIVATE RAW ERROR");expect(host.textContent).toContain("unavailable");
});
it("never fetches invalid, expired or future-issued requests",async()=>{
  await render({...request,asked_at:"2026-10-12T00:00:00Z"});await render({...request,asked_at:"2026-10-14T00:00:00Z"});await render({...request,tool:"other"});
  expect(mocks.get).not.toHaveBeenCalled();expect(current.canAuthorize()).toBe(false);
});

it("hides already-loaded private A immediately when B replaces it",async()=>{
  await render();expect(host.textContent).toContain("example.test");const b=deferred();mocks.get.mockReturnValueOnce(b.promise);
  await render({...request,req_id:"request_5678"});expect(host.textContent).not.toContain("example.test");expect(current.canAuthorize()).toBe(false);
});
