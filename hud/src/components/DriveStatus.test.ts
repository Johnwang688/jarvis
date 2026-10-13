import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { DriveStatus } from "./DriveStatus";
import { DriveRegistry } from "../lib/drivestate";
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
let host:HTMLDivElement, root:Root;
beforeEach(()=>{vi.useFakeTimers();vi.setSystemTime(2_000_000);host=document.createElement("div");document.body.appendChild(host);root=createRoot(host);});
afterEach(async()=>{await act(async()=>root.unmount());host.remove();vi.useRealTimers();});
function fixture(){const registry=new DriveRegistry(), lease=registry.attach("0123abcd")!;registry.connected(lease);
  registry.receive(lease,{t:"agent",kind:"driver",thread_id:"thread",provider:"codex"},Date.now());
  registry.receive(lease,{t:"agent",kind:"paused",paused:false},Date.now());registry.receive(lease,{t:"agent",kind:"list",actions:[]},Date.now());
  return{registry,lease};}
it("uses displayed chat slots, stays neutral and never takes focus",async()=>{
  const {registry,lease}=fixture(), outside=document.createElement("input");document.body.appendChild(outside);outside.focus();
  const control=vi.fn();await act(async()=>root.render(createElement(DriveStatus,{snapshot:registry.get(lease.tabId),blocked:false,slotOf:()=>2,onControl:control})));
  expect(host.textContent).toContain("Driven by the chat in pane 2 (Codex)");expect(document.activeElement).toBe(outside);
  expect(host.querySelector(".auth,canvas,img,svg")).toBeNull();expect(host.textContent).not.toMatch(/Authorize|Deny|Approve/i);
  await act(async()=>host.querySelector<HTMLButtonElement>("[data-testid=drive-stop]")!.click());expect(control).toHaveBeenCalledWith("stop");outside.remove();
});
it("folds cleaned action text without interpreting markup or rendering unproved outlines",async()=>{
  const {registry,lease}=fixture();registry.receive(lease,{t:"agent",kind:"action",id:"a",at:Date.now(),action:"click",outcome:"running",target:"<img onerror=bad>\u202e",text:"x".repeat(80),rect:{x:1,y:2,width:50,height:20}},Date.now());
  await act(async()=>root.render(createElement(DriveStatus,{snapshot:registry.get(lease.tabId),blocked:false,slotOf:()=>null,onControl:vi.fn()})));
  expect(host.textContent).toContain("a chat not on screen");expect(host.querySelector("ol")).toBeNull();
  await act(async()=>host.querySelector<HTMLButtonElement>("[data-testid=drive-actions-toggle]")!.click());
  expect(host.querySelector("bdi")!.textContent).toContain("<img onerror=bad>");expect(host.querySelector("bdi")!.textContent).not.toContain("\u202e");
  expect(host.querySelector("img,canvas,svg,[data-testid=drive-outline]")).toBeNull();
});
it("blocks Resume under cards, annotations, disconnect and incomplete replay",async()=>{
  const {registry,lease}=fixture();registry.receive(lease,{t:"agent",kind:"paused",paused:true,by:"owner"},Date.now());
  const control=vi.fn(), value=registry.get(lease.tabId);
  for(const [snapshot,blocked] of [[value,true],[{...value,connected:false},false],[{...value,ready:false},false],[{...value,pending:"resume" as const},false]] as const){
    await act(async()=>root.render(createElement(DriveStatus,{snapshot,blocked,slotOf:()=>null,onControl:control})));
    const resume=host.querySelector<HTMLButtonElement>("[data-testid=drive-resume]")!;expect(resume.disabled).toBe(true);await act(async()=>resume.click());
  }
  expect(control).not.toHaveBeenCalled();
});
it("expires the neutral banner and clears its timer, retaining paused status across turns",async()=>{
  const {registry,lease}=fixture();
  await act(async()=>root.render(createElement(DriveStatus,{snapshot:registry.get(lease.tabId),blocked:false,slotOf:()=>null,onControl:vi.fn()})));
  await act(async()=>vi.advanceTimersByTime(31_000));expect(host.textContent).toBe("");expect(vi.getTimerCount()).toBe(0);
  registry.receive(lease,{t:"agent",kind:"paused",paused:true,by:"owner"},Date.now());registry.receive(lease,{t:"agent",kind:"driver",thread_id:null},Date.now());
  await act(async()=>root.render(createElement(DriveStatus,{snapshot:registry.get(lease.tabId),blocked:false,slotOf:()=>null,onControl:vi.fn()})));
  expect(host.textContent).toContain("Paused");expect(host.querySelector("[data-testid=drive-resume]")).not.toBeNull();
});
