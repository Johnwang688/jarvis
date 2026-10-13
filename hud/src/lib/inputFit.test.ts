import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { requestInputFit, cancelInputFit } from "./inputFit";

describe("batched input geometry", () => {
  let frames: Map<number, FrameRequestCallback>, seq: number, trace: string[], elements: HTMLTextAreaElement[];
  beforeEach(() => {
    frames=new Map();seq=0;trace=[];elements=[];
    vi.stubGlobal("requestAnimationFrame", (fn: FrameRequestCallback) => {frames.set(++seq,fn);return seq;});
    vi.stubGlobal("cancelAnimationFrame", (id: number) => frames.delete(id));
    vi.stubGlobal("getComputedStyle", () => ({lineHeight:"20px",fontSize:"14px",borderTopWidth:"1px",borderBottomWidth:"1px",paddingTop:"3px",paddingBottom:"3px"}));
  });
  afterEach(() => {elements.forEach(cancelInputFit);document.body.replaceChildren();vi.unstubAllGlobals();});
  const tick = () => {const all=[...frames];frames.clear();all.forEach(([,fn])=>fn(0));};
  function box(name: string, paneH = 300, fixed = 150, want = 1000) {
    const pane=document.createElement("div");pane.className="wpane";
    const log=document.createElement("div");log.dataset.testid="log";
    const el=document.createElement("textarea");pane.append(log,el);document.body.append(pane);elements.push(el);
    let height="200px";
    Object.defineProperty(el.style,"height",{configurable:true,get:()=>height,set:(v:string)=>{height=v;trace.push(`${name}:write:${v}`);}});
    Object.defineProperty(pane,"clientHeight",{configurable:true,get:()=>paneH});
    Object.defineProperty(el,"clientWidth",{configurable:true,get:()=>300});
    Object.defineProperty(el,"offsetHeight",{get:()=>parseFloat(height)||28});
    Object.defineProperty(log,"offsetHeight",{get:()=>{trace.push(`${name}:fixed`);return Math.max(0,paneH-fixed-el.offsetHeight);}});
    Object.defineProperty(el,"scrollHeight",{get:()=>{trace.push(`${name}:want`);return want;}});
    return el;
  }
  it("measures fixed parts at one line even when the previous long box overflowed", () => {
    const el=box("a");requestInputFit(el,()=>{});tick();
    expect(el.style.height).toBe("54px"); // 300 - 150 fixed - 96 minimum log
    expect(el.style.overflowY).toBe("auto");
  });
  it("groups phases across requested panes and deduplicates repeated requests", () => {
    const a=box("a"),b=box("b");const done=vi.fn();
    requestInputFit(a,done);requestInputFit(b,done);requestInputFit(a,done);expect(frames.size).toBe(1);tick();
    expect(trace.slice(0,2)).toEqual(["a:write:28px","b:write:28px"]);
    expect(trace.slice(2,4)).toEqual(["a:fixed","b:fixed"]);
    expect(trace.slice(4,6)).toEqual(["a:write:auto","b:write:auto"]);
    expect(trace.slice(6,8)).toEqual(["a:want","b:want"]);
    expect(done).toHaveBeenCalledTimes(2);expect(frames.size).toBe(0);
  });
  it("does no measurement or write in seven unchanged panes", () => {
    const boxes=Array.from({length:8},(_,i)=>box(String(i)));requestInputFit(boxes[5],()=>{});tick();
    expect(trace.every(x=>x.startsWith("5:"))).toBe(true);
  });
  it("cancels one disposed pane without dropping another's pending fit", () => {
    const a=box("a"),b=box("b");requestInputFit(a,()=>{});requestInputFit(b,()=>{});cancelInputFit(a);tick();
    expect(trace.every(x=>x.startsWith("b:"))).toBe(true);
    requestInputFit(b,()=>{});cancelInputFit(b);expect(frames.size).toBe(0);
  });
  it("rechecks hidden and disconnected boxes at flush time, then fits when shown", () => {
    const a=box("a"),b=box("b");requestInputFit(a,()=>{});requestInputFit(b,()=>{});
    a.remove();Object.defineProperty(b,"clientWidth",{configurable:true,get:()=>0});tick();expect(trace).toEqual([]);
    Object.defineProperty(b,"clientWidth",{get:()=>300});requestInputFit(b,()=>{});tick();expect(trace.length).toBeGreaterThan(0);
  });
  it("preserves two-line and fifteen-line bounds and short text has no scrollbar", () => {
    const a=box("a",180,150),b=box("b",1200,80),c=box("c",500,80,46);
    for(const el of [a,b,c])requestInputFit(el,()=>{});tick();
    expect(a.style.height).toBe("48px");expect(b.style.height).toBe("308px");
    expect(c.style.height).toBe("48px");expect(c.style.overflowY).toBe("hidden");
  });
  it("keeps a request made after a batch write for the next frame", () => {
    const a=box("a"),b=box("b");requestInputFit(a,()=>requestInputFit(b,()=>{}));tick();
    expect(frames.size).toBe(1);trace=[];tick();expect(trace.every(x=>x.startsWith("b:"))).toBe(true);
  });
});
