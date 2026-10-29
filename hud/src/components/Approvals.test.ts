import { act, createElement, useLayoutEffect as useLayoutEffectProbe } from "react";
import { createRoot, type Root } from "react-dom/client";
import { ApprovalQueue, ApprovalVeil } from "./Approvals";
import { ARM_MS, STALE_PENDING_MS } from "../lib/approvalSummary";
import { useArmed } from "../lib/armed";
import { useEffect, useState } from "react";
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
  const relayed={...base,req_id:"relayed",relayed_from:{title:'x›\n · REQUESTED BY YOU 🔴 <img src=x onerror="window.__pwned=1">'+"z".repeat(100),provider:"claude",thread_id:"abcd1234",kind:"started"}};
  await act(async()=>root.render(createElement(ApprovalVeil,{requests:[relayed],onDecide})));
  const line=host.querySelector<HTMLElement>('[data-testid="approval-relayed"]')!;
  expect(line).not.toBeNull();
  expect(line.className).toBe("relayed");
  // The title is isolated text in one <bdi>; nothing else is an element.
  expect([...line.children].map(c=>c.tagName)).toEqual(["BDI"]);
  expect(line.querySelector("bdi")!.children).toHaveLength(0);
  expect(line.textContent!.startsWith("turn started by a message from chat ‹x_ _ REQUESTED BY YOU _img")).toBe(true);
  expect(line.textContent!.endsWith("› (Claude · abcd1234)")).toBe(true);
  expect(line.getAttribute("title")).toBe(line.textContent);
  expect(line.textContent!.match(/[‹›]/g)).toHaveLength(2);
  expect(line.textContent).not.toMatch(/[\n🔴<>]/);
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
  // A child the relayed turn did not start, asking while it runs: the honest wording.
  await act(async()=>root.render(createElement(ApprovalVeil,{requests:[{...base,req_id:"running",relayed_from:{title:"Frontend",provider:"codex",thread_id:"0123abcd",kind:"running"}}],onDecide})));
  const running=host.querySelector<HTMLElement>('[data-testid="approval-relayed"]')!;
  expect(running.textContent).toBe("while a turn started by a message from chat ‹Frontend› (Codex · 0123abcd) runs");
  expect(running.getAttribute("title")).toBe(running.textContent);
  expect(buttons()).toEqual(plainButtons);
  // Not an object: no line.
  for(const odd of [7,"Frontend",null]){
    await act(async()=>root.render(createElement(ApprovalVeil,{requests:[{...base,req_id:"odd"+String(odd),relayed_from:odd as never}],onDecide})));
    expect(host.querySelector('[data-testid="approval-relayed"]')).toBeNull();
  }
});

describe("AS: the reworked ordinary card", () => {
  const card = (extra: Partial<ApprovalRequest> = {}) => request("as1", { hud_only: false, tool: "Bash", layer: "reviewer",
    command: "cd /srv/app\nrm -rf build && make deploy --prod", args: { command: "x", timeout: 5 }, reason: "deploys to production",
    allowlistable: true, where: { project: "Calc", branch: "main", folder: "~/projects/calc" }, ...extra });
  const q = (id: string) => host.querySelector<HTMLElement>(`[data-testid="${id}"]`);
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("buttons come first, right under the title", async () => {
    await act(async () => root.render(createElement(ApprovalVeil, { requests: [card({ summary_state: "pending" })], onDecide: vi.fn() })));
    const c = q("approval-card")!;
    expect(c.children[0].tagName).toBe("H3");
    expect(c.children[1]).toBe(q("approval-buttons"));
    expect(Array.from(q("approval-buttons")!.querySelectorAll("button")).map((b) => b.dataset.testid))
      .toEqual(["approval-deny", "approval-always", "approval-allow"]);
    // A headline (a widening) stays first, above the buttons.
    await act(async () => root.render(createElement(ApprovalVeil, { requests: [card({ req_id: "as2", headline: "SANDBOX WIDENING: network on" })], onDecide: vi.fn() })));
    expect(q("approval-card")!.children[1]).toBe(q("approval-headline"));
    expect(q("approval-card")!.children[2]).toBe(q("approval-buttons"));
  });

  it("with a summary pending or there the command is collapsed, one press away; the AI line is marked and text", async () => {
    const hostile = '<img src=x onerror="window.__pwned=1"> deletes build';
    await act(async () => root.render(createElement(ApprovalVeil, { requests: [card({ summary_state: "ready", summary: hostile })], onDecide: vi.fn() })));
    expect(q("approval-summary")!.dataset.state).toBe("ready");
    expect(q("approval-summary")!.textContent).toBe("AI SUMMARY" + hostile);
    expect(host.querySelector("img")).toBeNull();
    expect(q("approval-why")!.textContent).toBe("WHY ASKED [reviewer] deploys to production");
    expect(q("approval-where")!.textContent).toBe("WHERE project Calc branch main folder ~/projects/calc");
    // Each field in its own labelled box, its value isolated (review of #330, MED 1).
    for (const [key, value] of [["project", "Calc"], ["branch", "main"], ["folder", "~/projects/calc"]]) {
      const part = q("approval-where-" + key)!;
      expect(part.querySelector(".as-wk")!.textContent).toBe(key);
      expect(part.querySelector("bdi")!.textContent).toBe(value);
    }
    expect(q("approval-command")).toBeNull();
    expect(q("approval-command-preview")!.textContent).toBe("command cd /srv/app");
    const toggle = q("approval-command-toggle") as HTMLButtonElement;
    expect(toggle.tabIndex).toBe(-1);
    expect(toggle.textContent).toMatch(/^show full command · 2 lines/);
    await act(async () => toggle.click());
    expect(q("approval-command")!.textContent).toBe("command cd /srv/app\nrm -rf build && make deploy --prod");
    expect(host.textContent).toContain("timeout 5");
    // DENY stays the only Tab stop.
    await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true })));
    await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true })));
    expect(document.activeElement).toBe(q("approval-deny"));
  });

  it("with no summary (failed, off, none) the command is expanded", async () => {
    for (const [i, extra] of ([{ summary_state: "failed" }, { summary_state: "off" }, {}] as Partial<ApprovalRequest>[]).entries()) {
      await act(async () => root.render(createElement(ApprovalVeil, { requests: [card({ req_id: "n" + i, ...extra })], onDecide: vi.fn() })));
      expect(q("approval-command")!.textContent).toContain("make deploy --prod");
      expect(q("approval-command-preview")).toBeNull();
      if (extra.summary_state) expect(q("approval-summary")!.dataset.state).toBe("none");
      else expect(q("approval-summary")).toBeNull();
    }
  });

  it("a failed summary opens the command; the owner's own toggle wins", async () => {
    const r = card({ summary_state: "pending" });
    await act(async () => root.render(createElement(ApprovalVeil, { requests: [r], onDecide: vi.fn() })));
    expect(q("approval-command")).toBeNull();
    await act(async () => root.render(createElement(ApprovalVeil, { requests: [{ ...r, summary_state: "failed" }], onDecide: vi.fn() })));
    expect(q("approval-command")).not.toBeNull();
    const r2 = card({ req_id: "own", summary_state: "pending" });
    await act(async () => root.render(createElement(ApprovalVeil, { requests: [r2], onDecide: vi.fn() })));
    await act(async () => (q("approval-command-toggle") as HTMLButtonElement).click());
    await act(async () => (q("approval-command-toggle") as HTMLButtonElement).click());
    await act(async () => root.render(createElement(ApprovalVeil, { requests: [{ ...r2, summary_state: "failed" }], onDecide: vi.fn() })));
    expect(q("approval-command")).toBeNull();
  });

  it("AUTHORIZE and ALWAYS arm ARM_MS after the card, and again after the summary lands; DENY never waits", async () => {
    const onDecide = vi.fn();
    const r = card({ summary_state: "pending" });
    await act(async () => root.render(createElement(ApprovalVeil, { requests: [r], onDecide })));
    const allow = q("approval-allow") as HTMLButtonElement, always = q("approval-always") as HTMLButtonElement;
    expect(allow.disabled && always.disabled).toBe(true);
    allow.disabled = false;
    await act(async () => allow.click());
    expect(onDecide).not.toHaveBeenCalled();
    await act(async () => vi.advanceTimersByTime(700));
    expect(allow.disabled || always.disabled).toBe(false);
    await act(async () => root.render(createElement(ApprovalVeil, { requests: [{ ...r, summary_state: "ready", summary: "Deploys calc." }], onDecide })));
    expect((q("approval-allow") as HTMLButtonElement).disabled).toBe(true);
    expect((q("approval-deny") as HTMLButtonElement).disabled).toBe(false);
    await act(async () => vi.advanceTimersByTime(700));
    await act(async () => (q("approval-allow") as HTMLButtonElement).click());
    expect(onDecide).toHaveBeenCalledExactlyOnceWith("as1", true, false);
  });

  it("a forged folder stays in its own field (review of #330, MED 1)", async () => {
    await act(async () => root.render(createElement(ApprovalVeil, { requests: [card({ req_id: "forge",
      where: { project: "Calc", branch: "feature", folder: "~/tmp/x · Jarvis · main · ~/projects/Jarvis" } })], onDecide: vi.fn() })));
    expect(q("approval-where-project")!.textContent).toBe("project Calc");
    expect(q("approval-where-branch")!.textContent).toBe("branch feature");
    expect(q("approval-where-folder")!.textContent).toBe("folder ~/tmp/x ? Jarvis ? main ? ~/projects/Jarvis");
    expect(q("approval-where")!.textContent).not.toContain("·");
  });

  it("a summary still pending 10 s after the ask shows the command expanded (review of #330, LOW 3)", async () => {
    vi.setSystemTime(new Date("2026-10-28T10:00:00.000Z"));
    const r = card({ req_id: "stale", summary_state: "pending", asked_at: "2026-10-28T10:00:00.000Z" });
    await act(async () => root.render(createElement(ApprovalVeil, { requests: [r], onDecide: vi.fn() })));
    expect(q("approval-command")).toBeNull();
    expect(q("approval-summary")!.dataset.state).toBe("pending");
    await act(async () => vi.advanceTimersByTime(STALE_PENDING_MS + 100));
    expect(q("approval-summary")!.dataset.state).toBe("none");
    expect(q("approval-command")!.textContent).toContain("make deploy --prod");
  });

  it("the queue shows the summary when there is one", async () => {
    const a = card({ req_id: "q1", summary_state: "ready", summary: "Deploys calc." }), b = card({ req_id: "q2" });
    await act(async () => root.render(createElement(ApprovalQueue, { requests: [a, b] })));
    const text = q("approval-queue")!.textContent!;
    expect(text).toContain("AI: Deploys calc.");
    expect(text).toContain("make deploy --prod");
  });
});

// Review of #330, LOW 2: the render that draws a new key is already
// disarmed. The old hook reset a boolean in a passive effect, so that render
// (and its commit) still said armed.
function oldUseArmed(key: string): boolean {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    setArmed(false);
    const t = setTimeout(() => setArmed(true), ARM_MS);
    return () => clearTimeout(t);
  }, [key]);
  return armed;
}

describe("useArmed: no armed frame under new content", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());
  const firstRenderWith = async (hook: (key: string) => boolean) => {
    const seen: [string, boolean][] = [];
    const Probe = ({ k }: { k: string }) => { seen.push([k, hook(k)]); return null; };
    await act(async () => root.render(createElement(Probe, { k: "A" })));
    await act(async () => vi.advanceTimersByTime(ARM_MS + 50));
    expect(seen[seen.length - 1]).toEqual(["A", true]);
    await act(async () => root.render(createElement(Probe, { k: "B" })));
    const b = seen.filter(([k]) => k === "B");
    const back = async () => {
      await act(async () => root.render(createElement(Probe, { k: "A" })));
      return seen.filter(([k]) => k === "A").slice(-1)[0][1];
    };
    return { firstB: b[0][1], backToA: await back() };
  };

  it("the shared hook: false in the very render that brings a new key, and for a key that comes back", async () => {
    const { firstB, backToA } = await firstRenderWith(useArmed);
    expect(firstB).toBe(false);
    expect(backToA).toBe(false);
  });

  it("the old hook fails the same check (mutation evidence)", async () => {
    const { firstB } = await firstRenderWith(oldUseArmed);
    expect(firstB).toBe(true);
  });

  it("the card's AUTHORIZE is disabled in the commit that draws a new summary", async () => {
    const r = request("arm", { hud_only: false, tool: "Bash", layer: "reviewer", command: "make", allowlistable: true,
      summary_state: "pending" });
    const onDecide = vi.fn();
    // Read `disabled` from the commit itself: a layout effect runs before any passive effect.
    const states: boolean[] = [];
    let recording = false;
    const Spy = () => { useLayoutEffectProbe(() => {
      const b = host.querySelector('[data-testid="approval-allow"]') as HTMLButtonElement | null;
      if (recording && b) states.push(b.disabled);
    }); return null; };
    const draw = (req: ApprovalRequest) => createElement("div", {}, createElement(ApprovalVeil, { requests: [req], onDecide }),
      createElement(Spy));
    await act(async () => root.render(draw(r)));
    await act(async () => vi.advanceTimersByTime(ARM_MS + 50));
    expect((host.querySelector('[data-testid="approval-allow"]') as HTMLButtonElement).disabled).toBe(false);
    recording = true;
    await act(async () => root.render(draw({ ...r, summary_state: "ready", summary: "Builds the app." })));
    expect(states[0]).toBe(true);
    expect(states.every((d) => d)).toBe(true);
  });
});
