import { describe, expect, it } from "vitest";
import { BrowserPublisher, browserFront, type BrowserFront, type BrowserReceipt } from "./browserowner";
import { addBrowserTab, defaultWorkspace, setBrowserProfile, setBrowserSee, setView } from "./workspace";
import { DEFAULT_PORTS } from "./browser";
const WINDOW="a".repeat(32);
const front = ():BrowserFront => ({window:WINDOW,focus_seq:1,focused_slot:1,covered:false,panes:[]});
const tick=async()=>{await Promise.resolve();await Promise.resolve();await Promise.resolve();};
function fixture() {
  const calls:{path:string;body:Record<string,unknown>;signal:AbortSignal;resolve:(r:BrowserReceipt)=>void;reject:(e:Error)=>void}[]=[];
  const receipts:BrowserReceipt[]=[],errors:string[]=[]; let sequence=0;
  const publisher=new BrowserPublisher((path,_method,body,signal)=>new Promise((resolve,reject)=>{
    calls.push({path,body,signal,resolve,reject});
  }),r=>receipts.push(r),m=>errors.push(m),()=>++sequence);
  const answer=(index:number, extra={})=>calls[index].resolve({window:WINDOW,seq:calls[index].body.seq as number,panes:[],...extra});
  return {calls,receipts,errors,publisher,answer};
}

describe("one ordered Browser owner publisher",()=>{
  it("waits for live SSE, applies matching receipts and serializes permissions after front",async()=>{
    const f=fixture();f.publisher.update(front());expect(f.calls).toHaveLength(0);
    f.publisher.connect(true);expect(f.calls[0].path).toBe("/browser/front");
    const changed=f.publisher.mutate("/browser/panes/1","PATCH",{see:false},(_r,b)=>b);
    expect(f.calls).toHaveLength(1);f.answer(0);await tick();
    expect(f.calls[1].path).toBe("/browser/panes/1");expect(f.calls[1].body.seq).toBe(2);
    f.answer(1);await changed;expect(f.receipts).toHaveLength(2);
  });
  it("ignores stale report receipt and coalesces unchanged snapshots",async()=>{
    const f=fixture();f.publisher.connect(true);f.publisher.update(front());
    f.publisher.update(front());expect(f.calls).toHaveLength(1);
    f.publisher.update({...front(),covered:true,focused_slot:null});
    expect(f.calls[0].signal.aborted).toBe(true);expect(f.calls).toHaveLength(2);
    f.answer(0);await tick();expect(f.receipts).toHaveLength(0);
    f.answer(1);await tick();expect(f.receipts[0].seq).toBe(2);
  });
  it("hiding cancels a slow address immediately and sends a newer revoke before it completes",async()=>{
    const f=fixture();f.publisher.connect(true);f.publisher.update(front());f.answer(0);await tick();
    let applied=false;
    const action=f.publisher.mutate("/browser/panes/1/tabs/abcdef/address","POST",{url:"https://example.test/",mode:"remote"},(_r,b)=>{applied=true;return b;});
    const refused=expect(action).rejects.toThrow("cancelled");
    expect(f.calls).toHaveLength(2);
    f.publisher.update({...front(),covered:true,focused_slot:null});
    await refused;expect(f.calls[1].signal.aborted).toBe(true);
    expect(f.calls).toHaveLength(3);expect(f.calls[2].body.seq).toBeGreaterThan(f.calls[1].body.seq as number);
    f.answer(1,{tab_id:"0123abcd"});await tick();expect(applied).toBe(false);
    f.answer(2);await tick();expect(f.receipts.at(-1)!.seq).toBe(3);
  });
  it("disconnect fences every callback and reconnect republishes latest with a newer seq",async()=>{
    const f=fixture();f.publisher.connect(true);f.publisher.update(front());
    f.publisher.connect(false);f.answer(0);await tick();expect(f.receipts).toEqual([]);
    f.publisher.connect(true);expect(f.calls).toHaveLength(2);expect(f.calls[1].body.seq).toBe(2);
    f.answer(1);await tick();expect(f.receipts).toHaveLength(1);
    f.publisher.dispose();f.publisher.heartbeat();expect(f.calls).toHaveLength(2);
  });
  it("failed report rejects waiting mutations and never enters a rapid retry loop",async()=>{
    const f=fixture();f.publisher.connect(true);f.publisher.update(front());
    const change=f.publisher.mutate("/browser/panes/1","PATCH",{see:false},(_r,b)=>b);
    const refused=expect(change).rejects.toThrow("offline");
    f.calls[0].reject(new Error("offline"));await refused;await tick();
    expect(f.calls).toHaveLength(1);expect(f.errors).toEqual(["offline"]);
    f.publisher.heartbeat();expect(f.calls).toHaveLength(2);f.answer(1);await tick();
  });
  it("rejects mismatched receipts without acknowledging a permission",async()=>{
    const f=fixture();f.publisher.connect(true);f.publisher.update(front());
    f.answer(0,{window:"b".repeat(32)});await tick();
    expect(f.receipts).toHaveLength(0);expect(f.errors[0]).toContain("did not match");
  });
  it("heartbeat preserves owner focus and successful mutation updates next report atomically",async()=>{
    const f=fixture();f.publisher.connect(true);f.publisher.update(front());f.answer(0);await tick();
    const changed=f.publisher.mutate("/browser/panes/1","PATCH",{see:false},(_r,b)=>({...b,covered:true,focused_slot:null}));
    f.publisher.heartbeat();expect(f.calls).toHaveLength(2);
    f.answer(1);await changed;await tick();
    expect(f.calls[2].body.covered).toBe(true);expect(f.calls[2].body.focus_seq).toBe(1);
    f.answer(2);await tick();
  });
});

describe("front snapshots",()=>{
  it("reports source/off-state and host handles, without a client Remote URL authority",()=>{
    let ws=addBrowserTab(setView(defaultWorkspace(),1,"browser"),1,"http://localhost:23456/private",{id:"abcdef",source:{kind:"unknown"}});
    ws=setBrowserSee(setBrowserProfile(ws,1,"private"),1,false);
    const body=browserFront(ws,[1],false,WINDOW,"p-0123abcd",1,3,DEFAULT_PORTS);
    expect(body.panes[0]).toMatchObject({pane_id:1,slot:1,profile:"private",see:false,drive:false});
    expect(body.panes[0].tabs[0]).toEqual({id:"abcdef",mode:"remote",tab_id:null,source:{kind:"unknown"}});
    expect(JSON.stringify(body)).not.toContain("/private");
  });
  it("coverage clears front, Direct control URLs are ineligible, and no profile invents none",()=>{
    const ws=addBrowserTab(setView(defaultWorkspace(),1,"browser"),1,"http://localhost:8402/approvals",{id:"abcdef"});
    const body=browserFront(ws,[1],true,WINDOW,"p-0123abcd",1,3,DEFAULT_PORTS);
    expect(body.focused_slot).toBeNull();expect(body.panes[0].tabs[0].url).toBe("");
    expect(browserFront(ws,[1],false,WINDOW,null,1,3,DEFAULT_PORTS).panes).toEqual([]);
  });
});

it("independent: a new owner mutation cannot bypass a failed updated front report",async()=>{
  const f=fixture();f.publisher.connect(true);f.publisher.update(front());f.answer(0);await tick();
  f.publisher.update({...front(),focus_seq:2});
  f.calls[1].reject(new Error("connection interrupted before host received new profile"));await tick();
  expect(f.calls).toHaveLength(2);
  const action=f.publisher.mutate("/browser/panes/1/tabs/abcdef/address","POST",{url:"https://example.test/",mode:"remote"},(_r,b)=>b);
  action.catch(()=>{});
  expect(f.calls[2].path).toBe("/browser/front");
  expect(f.calls[2].body.focus_seq).toBe(2);
  f.answer(2);await tick();expect(f.calls[3].path).toContain("/address");f.answer(3);await action;
});

it.each(["transport failure","mismatched receipt"])("a %s during a mutation requires a fresh report without retrying itself",async(failure)=>{
  const f=fixture();f.publisher.connect(true);f.publisher.update(front());f.answer(0);await tick();
  const first=f.publisher.mutate("/browser/panes/1","PATCH",{see:false},(_r,b)=>b);
  const refused=expect(first).rejects.toThrow();
  if(failure==="transport failure")f.calls[1].reject(new Error("lost acknowledgment"));else f.answer(1,{seq:-1});
  await refused;await tick();expect(f.calls).toHaveLength(2);
  const next=f.publisher.mutate("/browser/panes/1","PATCH",{see:true},(_r,b)=>b);
  expect(f.calls[2].path).toBe("/browser/front");
  f.answer(2);await tick();expect(f.calls[3].path).toBe("/browser/panes/1");f.answer(3);await next;
});
it("refuses a recovery mutation when its replacement report also fails and waits again",async()=>{
  const f=fixture();f.publisher.connect(true);f.publisher.update(front());
  f.calls[0].reject(new Error("offline"));await tick();
  const change=f.publisher.mutate("/browser/panes/1","PATCH",{see:false},(_r,b)=>b);
  const refused=expect(change).rejects.toThrow("still offline");
  expect(f.calls[1].path).toBe("/browser/front");f.calls[1].reject(new Error("still offline"));await refused;await tick();
  expect(f.calls).toHaveLength(2);
  f.publisher.heartbeat();expect(f.calls[2].path).toBe("/browser/front");f.answer(2);await tick();
  expect(f.calls).toHaveLength(3);
});

it("named drive opt-out persists while Private opt-in needs an exact runtime context",()=>{
  let ws=addBrowserTab(setView(defaultWorkspace(),1,"browser"),1,"https://example.test/",{id:"abcdef",mode:"remote"});
  expect(browserFront(ws,[1],false,WINDOW,"p-0123abcd",1,1,DEFAULT_PORTS).panes[0].drive).toBe(true);
  ws.panes[0].drive=false;
  expect(browserFront(ws,[1],false,WINDOW,"p-0123abcd",1,1,DEFAULT_PORTS).panes[0].drive).toBe(false);
  ws=setBrowserProfile(ws,1,"private");ws.panes[0].browserCtx="0123abcd";
  const front=(grant:ReadonlyMap<number,string>)=>browserFront(ws,[1],false,WINDOW,"p-0123abcd",1,1,DEFAULT_PORTS,grant).panes[0];
  expect(front(new Map()).drive).toBe(false);
  expect(front(new Map([[1,"11111111"]])).drive).toBe(false);
  expect(front(new Map([[2,"0123abcd"]])).drive).toBe(false);
  expect(front(new Map([[1,"0123abcd"]])).drive).toBe(true);
  ws=setBrowserSee(ws,1,false);expect(front(new Map([[1,"0123abcd"]])).drive).toBe(false);
});
