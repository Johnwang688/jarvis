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
