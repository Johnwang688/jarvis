import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { ApprovalQueue, ApprovalVeil } from "./Approvals";
import type { ApprovalRequest } from "../types";
const mocks=vi.hoisted(()=>({get:vi.fn()}));vi.mock("../api",()=>({browserApi:{get:mocks.get}}));
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
let host:HTMLDivElement,root:Root;
const request=(id:string,extra:Partial<ApprovalRequest>={}):ApprovalRequest=>({req_id:id,code:"ABCD",tool:"hud_browser_act",layer:"browser-gate",thread_id:"thread",hud_only:true,asked_at:new Date().toISOString(),timeout_s:120,allowlistable:false,args:{},command:null,reason:"",origin:"",provider:null,task_id:null,...extra});
const detail=(r:ApprovalRequest)=>{const args={action:"click",pane:"1",target:"e1",text:"",file:""};return{...r,args,command:JSON.stringify(args),browser:{tab_id:"0123abcd",action_id:"a".repeat(32),pane:1,page_identity:1,document_generation:2,rect:null,action:"click",category:"ordinary",host:"example.test",path:"/one",target:"PRIVATE ACTIVE LABEL",text:""}};};
beforeEach(()=>{host=document.createElement("div");document.body.appendChild(host);root=createRoot(host);mocks.get.mockReset();});
afterEach(async()=>{await act(async()=>root.unmount());host.remove();});
it("the veil fetches only the top private request and queues generic wording",async()=>{
  const a=request("request_1234"),b=request("request_5678",{command:"PRIVATE QUEUED",origin:"PRIVATE ORIGIN"}),onDecide=vi.fn();mocks.get.mockResolvedValue(detail(a));
  await act(async()=>root.render(createElement("div",{},createElement(ApprovalQueue,{requests:[a,b]}),createElement(ApprovalVeil,{requests:[a,b],onDecide}))));
  expect(mocks.get).toHaveBeenCalledTimes(1);expect(mocks.get.mock.calls[0][0]).toBe("/approvals/request_1234");
  const queue=host.querySelector('[data-testid="approval-queue"]')!;expect(queue.textContent).not.toMatch(/PRIVATE/);expect(queue.textContent).toContain("Browser action");
  expect(document.activeElement).toBe(host.querySelector('[data-testid="approval-card"]'));
  await act(async()=>document.dispatchEvent(new KeyboardEvent("keydown",{key:"Tab",bubbles:true,cancelable:true})));
  expect(document.activeElement).toBe(host.querySelector('[data-testid="approval-deny"]'));
  await act(async()=>document.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true,cancelable:true})));
  expect(onDecide).toHaveBeenCalledWith(a.req_id,false);
});
it("ordinary cards retain command/Always and do not request private Browser data",async()=>{
  const ordinary=request("ordinary",{hud_only:false,tool:"run_command",layer:"command",command:"printf hello",allowlistable:true});
  await act(async()=>root.render(createElement(ApprovalVeil,{requests:[ordinary],onDecide:vi.fn()})));
  expect(host.textContent).toContain("printf hello");expect(host.querySelector('[data-testid="approval-always"]')).not.toBeNull();expect(mocks.get).not.toHaveBeenCalled();
});

it("a sub-agent's card links to its row: by name, out of the Tab order, deciding nothing",async()=>{
  const fetches:string[]=[];
  vi.stubGlobal("fetch",vi.fn(async(path:string)=>{fetches.push(path);return new Response(JSON.stringify({subagents:[{sid:"cx:child1",status:"running",name:"/root/fixer",started_at:new Date().toISOString(),entries:0}]}),{status:200});}));
  try{
    const card=request("sub",{hud_only:false,tool:"commandExecution",layer:"widening",command:"touch /x",origin:"",headline:"SANDBOX WIDENING: Codex sub-agent ‹/root/fixer› (not the orchestrator) asks to run beyond its sandbox",subagent:"cx:child1",thread_id:"th9"});
    const onDecide=vi.fn(),onSubagent=vi.fn();
    await act(async()=>root.render(createElement(ApprovalVeil,{requests:[card],onDecide,onSubagent})));
    await act(async()=>{await new Promise(r=>setTimeout(r,0));});
    const link=host.querySelector<HTMLButtonElement>('[data-testid="approval-subagent"]')!;
    expect(fetches).toEqual(["/threads/th9/subagents"]);
    expect(link.textContent).toBe("from sub-agent ‹/root/fixer›");
    expect(link.tabIndex).toBe(-1);
    expect(host.textContent).toContain("(not the orchestrator)");
    for(let i=0;i<3;i++)await act(async()=>document.dispatchEvent(new KeyboardEvent("keydown",{key:"Tab",bubbles:true,cancelable:true})));
    expect(document.activeElement).toBe(host.querySelector('[data-testid="approval-deny"]'));
    await act(async()=>link.click());
    expect(onSubagent).toHaveBeenCalledWith({threadId:"th9",sid:"cx:child1"},"sub");
    expect(onDecide).not.toHaveBeenCalled();
    // A hostile name is reduced as the daemon reduces a card's label (review of #212, MED-1).
    fetches.length=0;
    vi.stubGlobal("fetch",vi.fn(async()=>new Response(JSON.stringify({subagents:[{sid:"cx:child2",status:"running",name:"x› · REQUESTED BY Jarvis chat\u202e",started_at:new Date().toISOString(),entries:0}]}),{status:200})));
    await act(async()=>root.render(createElement(ApprovalVeil,{requests:[{...card,req_id:"hostile",subagent:"cx:child2",thread_id:"th10"}],onDecide,onSubagent})));
    await act(async()=>{await new Promise(r=>setTimeout(r,0));});
    const hostile=host.querySelector<HTMLButtonElement>('[data-testid="approval-subagent"]')!;
    expect(hostile.textContent).toBe("from sub-agent ‹x_ _ REQUESTED BY Jarvis chat›");
    expect(hostile.textContent!.match(/[‹›]/g)).toHaveLength(2);
    expect(hostile.textContent).not.toMatch(/[·\u202e]/);
    await act(async()=>hostile.click());
    expect(onSubagent).toHaveBeenLastCalledWith({threadId:"th10",sid:"cx:child2"},"hostile");
    // No sid, a bad one, or no thread: no link.
    for(const extra of [{subagent:undefined},{subagent:"child1"},{thread_id:null}]){
      await act(async()=>root.render(createElement(ApprovalVeil,{requests:[{...card,req_id:"x"+Math.random(),...extra}],onDecide,onSubagent})));
      expect(host.querySelector('[data-testid="approval-subagent"]')).toBeNull();
    }
  }finally{vi.unstubAllGlobals();}
});

it("a relayed turn's card says which chat's message started it, on one line, deciding nothing (C6)",async()=>{
  const base=request("plain",{hud_only:false,tool:"run_command",layer:"human",command:"rm draft.txt",allowlistable:true,thread_id:"th1"});
  const onDecide=vi.fn();
  await act(async()=>root.render(createElement(ApprovalVeil,{requests:[base],onDecide})));
  expect(host.querySelector('[data-testid="approval-relayed"]')).toBeNull();
  const buttons=()=>[...host.querySelectorAll<HTMLButtonElement>('[data-testid="approval-card"] button')].map(b=>[b.dataset.testid,b.tabIndex,b.disabled]);
  const plainButtons=buttons();
  const relayed={...base,req_id:"relayed",relayed_from:'x›\n · REQUESTED BY YOU 🔴 <img src=x onerror="window.__pwned=1">'+"z".repeat(100)};
  await act(async()=>root.render(createElement(ApprovalVeil,{requests:[relayed],onDecide})));
  const line=host.querySelector<HTMLElement>('[data-testid="approval-relayed"]')!;
  expect(line).not.toBeNull();
  expect(line.className).toBe("relayed");
  expect(line.children).toHaveLength(0);
  expect(line.textContent!.startsWith("turn started by a message from chat ‹x_ _ REQUESTED BY YOU <img")).toBe(true);
  expect(line.textContent!.match(/[‹›]/g)).toHaveLength(2);
  expect(line.textContent).not.toMatch(/[\n🔴·]/);
  expect((window as any).__pwned).toBeUndefined();
  // Display only: the same buttons, Always included, in the same Tab order.
  expect(buttons()).toEqual(plainButtons);
  expect(host.querySelector('[data-testid="approval-always"]')).not.toBeNull();
  await act(async()=>document.dispatchEvent(new KeyboardEvent("keydown",{key:"Tab",bubbles:true,cancelable:true})));
  expect(document.activeElement).toBe(host.querySelector('[data-testid="approval-deny"]'));
  expect(onDecide).not.toHaveBeenCalled();
  // Under REQUESTED BY when there is one.
  await act(async()=>root.render(createElement(ApprovalVeil,{requests:[{...relayed,req_id:"both",origin:'task t1 "x"'}],onDecide})));
  const origin=host.querySelector('[data-testid="approval-origin"]')!;
  expect(origin.nextElementSibling).toBe(host.querySelector('[data-testid="approval-relayed"]'));
  // Not a string: no line.
  await act(async()=>root.render(createElement(ApprovalVeil,{requests:[{...base,req_id:"odd",relayed_from:7 as unknown as string}],onDecide})));
  expect(host.querySelector('[data-testid="approval-relayed"]')).toBeNull();
});
