import { act, createElement, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
const seen = vi.hoisted(() => ({ positions: [] as any[], focus: 0, model: null as any }));
vi.mock("./Editor", async () => {
  const { useLayoutEffect } = await import("react");
  return { PaneEditor: (p: any) => {
    useLayoutEffect(() => {
      seen.model = p.model;
      p.editorRef({ getModel: () => p.model,
        setPosition: (pos: any) => seen.positions.push(pos),
        revealLineInCenter: () => {}, focus: () => { seen.focus++; } });
    }, [p.model]);
    return null;
  }, CompareEditor: () => null };
});
vi.mock("./EditorStatus", () => ({ EditorStatus: () => null }));
vi.mock("./GitMarks", () => ({ useGitMarks: () => ({ file: () => undefined, folder: () => undefined }) }));
vi.mock("../api", () => ({ api: { tree: async () => ({ entries: [], path: "" }) } }));
import { FileTab, type FileAsk } from "./FileTab";
import { buffers, PlainModel } from "../state/buffers";
import { TABS_MAX, type Tabs } from "../lib/buffers";
(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
let host: HTMLDivElement, root: Root, change: (x: Partial<{ ask: FileAsk|null; blocked: boolean; tabs: Tabs|null }>) => void;
let state: { ask: FileAsk|null; blocked: boolean; tabs: Tabs|null };
let deferred = new Map<string, { resolve: (r: any) => void; reject: (e: any) => void }>();
const row = (path: string) => ({ path, abs: `/w/${path}`, content: "one\ntwo\nthree\nfour\nfive", kind: "text", protected: false, editable: true, etag: "abc" });
const flush = () => act(async () => { for(let i=0;i<12;i++) await Promise.resolve(); });
async function ask(path: string, nonce: number, line = 4, col = 2) {
  act(() => change({ ask: {kind: "open", path, nonce, line, col} })); await flush();
}
beforeEach(async () => {
  seen.positions=[]; seen.focus=0; seen.model=null; deferred=new Map(); buffers.reset();
  buffers.configure({ read: (_pid,path) => path.startsWith("late") ? new Promise((resolve,reject) => deferred.set(path,{resolve,reject})) : Promise.resolve(row(path)),
    write: async () => ({etag:"def"}), stat:async()=>({files:{}}), now:()=>0,
    makeModel: async text => Object.assign(new PlainModel(text), {getLineCount:()=>5,getLineMaxColumn:()=>8}) });
  host=document.createElement("div");document.body.append(host);root=createRoot(host);
  function Window() {
    const [s,set]=useState({ask:null as FileAsk|null,blocked:false,tabs:null as Tabs|null}); state=s;
    change=x=>set(s=>({...s,...x}));
    return createElement(FileTab,{pane:2,projectId:"p1",root:"/w",...s,onTabs:(_pid,tabs)=>set(s=>({...s,tabs}))});
  }
  act(()=>root.render(createElement(Window)));await flush();
});
afterEach(()=>{ act(()=>root.unmount());host.remove();buffers.reset(); });
describe("one editor navigation request",()=>{
  it("opens pinned and reveals exactly once, with a new nonce for a repeated location",async()=>{
    await ask("main.cpp",1,99,-2);
    expect(state.tabs?.preview).toBeNull();expect(state.tabs?.paths).toEqual(["main.cpp"]);
    expect(seen.positions).toEqual([{lineNumber:5,column:1}]);
    act(()=>change({ask:{kind:"open",path:"main.cpp",nonce:1,line:2,col:3}}));await flush();
    expect(seen.positions).toHaveLength(1);
    await ask("main.cpp",2,3,4);expect(seen.positions.at(-1)).toEqual({lineNumber:3,column:4});
  });
  it("a pending request waits for its model and a newer open supersedes it",async()=>{
    await ask("late.cpp",1);expect(seen.positions).toEqual([]);
    await ask("new.cpp",2,2,1);expect(seen.positions).toEqual([{lineNumber:2,column:1}]);
    await act(async()=>deferred.get("late.cpp")!.resolve(row("late.cpp")));await flush();
    expect(state.tabs?.active).toBe("new.cpp");expect(seen.positions).toHaveLength(1);
  });
  it("an ask behind a card is consumed and never replayed",async()=>{
    act(()=>change({blocked:true}));await ask("main.cpp",1);
    expect(state.tabs).toBeNull();expect(seen.positions).toEqual([]);
    act(()=>change({blocked:false}));await flush();expect(state.tabs).toBeNull();
  });
  it("a card during load cancels the pending cursor move and focus",async()=>{
    await ask("late.cpp",1);act(()=>change({blocked:true}));await flush();
    await act(async()=>deferred.get("late.cpp")!.resolve(row("late.cpp")));await flush();
    expect(seen.positions).toEqual([]);expect(seen.focus).toBe(0);
    act(()=>change({blocked:false}));await flush();expect(seen.positions).toEqual([]);
  });
  it("an open without coordinates supersedes a pending reveal",async()=>{
    await ask("late.cpp",1);
    act(()=>change({ask:{kind:"open",path:"new.cpp",nonce:2}}));await flush();
    await act(async()=>deferred.get("late.cpp")!.resolve(row("late.cpp")));await flush();
    act(()=>change({ask:null,tabs:{paths:["late.cpp","new.cpp"],active:"late.cpp",preview:null}}));await flush();
    expect(seen.positions).toEqual([]);
  });
  it("clamps a column against the actual target model",async()=>{
    await ask("main.cpp",1,2,999);
    expect(seen.positions).toEqual([{lineNumber:2,column:8}]);
  });
  it("a failed open cannot move the cursor when that file is later opened normally",async()=>{
    await ask("late.cpp",1);
    await act(async()=>deferred.get("late.cpp")!.reject({status:404}));await flush();
    expect(state.tabs).toBeNull();
    act(()=>change({ask:null,tabs:{paths:["late.cpp"],active:"late.cpp",preview:null}}));await flush();
    await act(async()=>deferred.get("late.cpp")!.resolve(row("late.cpp")));await flush();
    expect(seen.positions).toEqual([]);
  });
  it("closing a loading target drops its pending cursor position",async()=>{
    await ask("late.cpp",1);
    act(()=>(host.querySelector('[data-testid="editor-tab-close-late.cpp"]') as HTMLElement).click());await flush();
    await act(async()=>deferred.get("late.cpp")!.resolve(row("late.cpp")));await flush();
    act(()=>change({ask:null,tabs:{paths:["late.cpp"],active:"late.cpp",preview:null}}));await flush();
    await act(async()=>deferred.get("late.cpp")!.resolve(row("late.cpp")));await flush();
    expect(seen.positions).toEqual([]);
  });
  it("capacity keeps dirty models and refuses all-dirty without a stale reveal",async()=>{
    const paths=Array.from({length:TABS_MAX},(_,i)=>`file${i}.cpp`);
    act(()=>change({tabs:{paths,active:paths[0],preview:null}}));await flush();
    act(()=>{for(const p of paths)(buffers.model(`/w/${p}`) as PlainModel).setValue("unsaved");});
    await ask("extra.cpp",1);expect(state.tabs?.paths).toEqual(paths);
    expect(host.textContent).toContain("save or close one first");expect(seen.positions).toEqual([]);
    act(()=>change({ask:null,tabs:{paths:["extra.cpp"],active:"extra.cpp",preview:null}}));await flush();
    expect(seen.positions).toEqual([]);
    expect(buffers.text(`/w/${paths[0]}`)).toBe("unsaved");
  });
  it("at capacity only a clean tab closes and the requested tab stays pinned",async()=>{
    const paths=Array.from({length:TABS_MAX},(_,i)=>`file${i}.cpp`);
    act(()=>change({tabs:{paths,active:paths[0],preview:null}}));await flush();
    act(()=>(buffers.model(`/w/${paths[0]}`) as PlainModel).setValue("unsaved"));
    await ask("extra.cpp",1);expect(state.tabs?.paths).toHaveLength(TABS_MAX);
    expect(state.tabs?.paths).toContain(paths[0]);expect(state.tabs?.paths).not.toContain(paths[1]);
    expect(state.tabs?.preview).toBeNull();expect(seen.positions).toHaveLength(1);
  });
});
