// @vitest-environment jsdom
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { BrowserTab } from "./BrowserTab";
import type { BrowserControl } from "../state/browser";
import { addressTarget } from "../state/browser";
import { DEFAULT_PORTS } from "../lib/browser";
const observed=vi.hoisted(()=>({remote:null as any,capture:null as any}));
vi.mock("../state/daemonPorts",()=>({useDaemonPorts:()=>({status:"ok",ports:DEFAULT_PORTS})}));
vi.mock("./BrowserCapture",()=>({BrowserCapture:(props:any)=>{observed.capture=props;return null;}}));
vi.mock("./FrameHost",()=>({FrameHost:()=>null}));
vi.mock("./RemoteView",()=>({RemoteView:(props:any)=>{observed.remote=props;return createElement("div",{"data-testid":"remote-status"},"Page input status");}}));
vi.mock("../api",()=>({browserApi:{},api:{}}));
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
let root:Root, host:HTMLDivElement, control:BrowserControl, props:Parameters<typeof BrowserTab>[0];
async function show(change:Partial<typeof props>={}) {
  props={...props,...change};await act(async()=>root.render(createElement(BrowserTab,props)));
}
const toggle=()=>host.querySelector<HTMLButtonElement>('[data-testid="browser-options-toggle"]')!;
const panel=()=>host.querySelector<HTMLDivElement>('[data-testid="browser-options"]')!;
beforeEach(()=>{
  observed.remote=null;
  control={online:true,covered:false,profiles:[{id:"p-11111111",label:"Main",created:"now"}],
    runtime:{},error:"",cleanupError:"",selectedProfile:()=> "p-11111111",focus:vi.fn(),
    cover:vi.fn(),uncover:vi.fn().mockResolvedValue(undefined),ensure:vi.fn().mockResolvedValue(undefined),
    profile:vi.fn(),see:vi.fn(),history:vi.fn(),convert:vi.fn(),
    drive:vi.fn(),drivePreference:()=>true,driveReady:()=>true,globalDrive:true,
    submit:vi.fn().mockImplementation((_pane,_id,raw,chosen)=>Promise.resolve(addressTarget(raw,DEFAULT_PORTS,false,chosen))),
  } as unknown as BrowserControl;
  props={pane:1,slot:1,drawn:true,projectId:null,control,liveFrames:[],
    browser:{tabs:[{id:"page01",url:"https://example.test/",mode:"remote",remoteTabId:"0123abcd"}],active:"page01"},
    onLoaded:vi.fn(),onSubmitted:vi.fn(),onKeepOrigin:vi.fn(),onAdd:vi.fn(),onClose:vi.fn(),onActivate:vi.fn()};
  host=document.createElement("div");document.body.append(host);root=createRoot(host);
});
afterEach(async()=>{await act(async()=>root.unmount());host.remove();});
it("keeps navigation compact and moves profile permissions into a keyboard-accessible options panel",async()=>{
  await show();
  expect(panel().hidden).toBe(true);
  expect(host.querySelector('[aria-label="Browser mode"]')).toBeNull();
  expect(panel().querySelector('[data-testid="browser-see"]')).not.toBeNull();
  expect(host.querySelector('[data-testid="remote-status"]')?.closest('[data-testid="browser-options"]')).toBeNull();
  await act(async()=>toggle().click());
  expect(toggle().getAttribute("aria-expanded")).toBe("true");
  expect(panel().hidden).toBe(false);
  expect(document.activeElement?.getAttribute("aria-label")).toBe("Browser profile");
  expect(control.cover).toHaveBeenLastCalledWith(1,expect.any(Object),true);
  expect(observed.remote.blocked).toBe(true);
  expect(observed.remote.inputHeld()).toBe(true);
  await act(async()=>document.activeElement!.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true,cancelable:true})));
  expect(panel().hidden).toBe(true);
  expect(document.activeElement).toBe(toggle());
  expect(observed.remote.inputHeld()).toBe(false);
});
it("owner address entry from an existing Remote tab chooses localhost automatically",async()=>{
  await show({browser:{tabs:[{id:"page01",url:"http://localhost:5173/",mode:"remote",remoteTabId:"0123abcd"}],active:"page01"}});
  await act(async()=>host.querySelector<HTMLButtonElement>('[data-testid="browser-go"]')!.click());
  expect(control.submit).toHaveBeenCalledExactlyOnceWith(1,"page01","http://localhost:5173/");
  expect(await vi.mocked(control.submit).mock.results[0].value).toMatchObject({mode:"direct"});
});
it.each([{blocked:true},{drawn:false}])("cards and hidden panes close and block options: %o",async(change)=>{
  await show();await act(async()=>toggle().click());expect(panel().hidden).toBe(false);
  await show(change);expect(panel().hidden).toBe(true);expect(toggle().disabled).toBe(true);
  const old=vi.mocked(control.submit).mock.calls.length;
  await act(async()=>toggle().click());expect(panel().hidden).toBe(true);
  expect(vi.mocked(control.submit).mock.calls.length).toBe(old);
});
it("disabled local history controls retain reload and offer the same automatic address target",async()=>{
  await show({browser:{tabs:[{id:"page01",url:"http://localhost:5173/"}],active:"page01"}});
  expect(host.querySelector<HTMLButtonElement>('[aria-label="Back"]')!.disabled).toBe(true);
  expect(host.querySelector<HTMLButtonElement>('[aria-label="Forward"]')!.disabled).toBe(true);
  expect(host.querySelector<HTMLButtonElement>('[aria-label="Reload"]')!.disabled).toBe(false);
  expect(host.querySelector('[aria-label="Browser mode"]')).toBeNull();
});

it("ending an annotation releases the immediate hold and lets the next options gesture open",async()=>{
  await show({captureDestination:{target:()=>null,stage:()=>false}});
  await act(async()=>observed.capture.onAnnotating(true));
  expect(observed.remote.inputHeld()).toBe(true);expect(toggle().disabled).toBe(true);
  await act(async()=>observed.capture.onAnnotating(false));
  expect(observed.remote.inputHeld()).toBe(false);expect(toggle().disabled).toBe(false);
  await act(async()=>toggle().click());expect(panel().hidden).toBe(false);
});

it.each(["mouse","touch","pen"])("cancels the entire outside dismissal pointer gesture: %s",async(pointerType)=>{
  await show();await act(async()=>toggle().click());
  const event=new MouseEvent("pointerdown",{bubbles:true,cancelable:true});
  Object.defineProperty(event,"pointerType",{value:pointerType});
  await act(async()=>host.querySelector('[data-testid="remote-status"]')!.dispatchEvent(event));
  expect(event.defaultPrevented).toBe(true);expect(panel().hidden).toBe(true);
});
