import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { RemoteView, type RemoteViewProps } from "./RemoteView";
import { driving } from "../state/driving";
const mocks=vi.hoisted(()=>({write:vi.fn(),clipboard:vi.fn(),draw:vi.fn(),decode:vi.fn()}));
vi.mock("../api",()=>({browserApi:{write:mocks.write},api:{}}));
vi.mock("./BrowserFiles",()=>({BrowserFiles:()=>null}));
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
class Socket {
  static OPEN=1;static instances:Socket[]=[];readyState=0;binaryType="";
  onopen:(()=>void)|null=null;onclose:(()=>void)|null=null;onerror:(()=>void)|null=null;
  onmessage:((event:{data:unknown})=>void)|null=null;
  sent:any[]=[];url:string;closed=false;
  constructor(url:string){this.url=url;Socket.instances.push(this);}
  send(text:string){this.sent.push(JSON.parse(text));}
  close(){this.closed=true;this.readyState=3;}
  open(){this.readyState=1;this.onopen?.();}
  message(value:unknown){this.onmessage?.({data:value instanceof ArrayBuffer?value:JSON.stringify(value)});}
}
function binary(id:number){const b=new ArrayBuffer(10);const h=new DataView(b);h.setUint32(0,id);h.setUint16(4,320);h.setUint16(6,200);return b;}
function deferred<T>(){let resolve!:(value:T)=>void;const promise=new Promise<T>(r=>resolve=r);return{promise,resolve};}
let host:HTMLDivElement;let root:Root;let props:RemoteViewProps;
const render=async(change:Partial<RemoteViewProps>={})=>{props={...props,...change};await act(async()=>root.render(createElement(RemoteView,props)));};
const socket=()=>Socket.instances.at(-1)!;
const input=()=>host.querySelector<HTMLTextAreaElement>("textarea")!;
const key=async(key:string,extra:KeyboardEventInit={})=>{await act(async()=>{input().dispatchEvent(new KeyboardEvent("keydown",{bubbles:true,cancelable:true,key,code:key.length===1?`Key${key.toUpperCase()}`:key,...extra}));});};
const frame=async(value:unknown)=>{await act(async()=>socket().message(value));};
beforeEach(()=>{
  driving.retain([]);Socket.instances=[];vi.stubGlobal("WebSocket",Socket);
  vi.stubGlobal("ResizeObserver",class{observe(){}disconnect(){}});
  vi.stubGlobal("createImageBitmap",mocks.decode);
  Object.defineProperty(navigator,"clipboard",{configurable:true,value:{writeText:mocks.clipboard}});
  mocks.write.mockReset().mockResolvedValue({ticket:"one-use"});mocks.clipboard.mockReset().mockResolvedValue(undefined);
  mocks.draw.mockReset();mocks.decode.mockReset().mockResolvedValue({close:vi.fn()});
  vi.spyOn(HTMLCanvasElement.prototype,"getContext").mockReturnValue({clearRect:vi.fn(),drawImage:mocks.draw} as any);
  host=document.createElement("div");document.body.appendChild(host);root=createRoot(host);
  props={tabId:"0123abcd",live:true,blocked:false,zoom:100,projectId:null,onLocation:vi.fn(),onPopup:vi.fn(),onAction:vi.fn(),onOwnerFocus:vi.fn()};
});
afterEach(async()=>{await act(async()=>root.unmount());host.remove();driving.retain([]);vi.restoreAllMocks();vi.unstubAllGlobals();vi.useRealTimers();});
async function ready(){await render();await act(async()=>socket().open());socket().sent=[];}
it("never opens a hidden stream and discards a late ticket",async()=>{
  const ticket=deferred<{ticket:string}>();mocks.write.mockReturnValue(ticket.promise);
  await render({live:false});expect(mocks.write).not.toHaveBeenCalled();
  await render({live:true});expect(mocks.write).toHaveBeenCalledTimes(1);
  await render({live:false});await act(async()=>ticket.resolve({ticket:"late"}));expect(Socket.instances).toHaveLength(0);
});
it("draws and ACKs only the current socket generation, disposing stale bitmaps",async()=>{
  await ready();const decode=deferred<any>();const close=vi.fn();mocks.decode.mockReturnValueOnce(decode.promise);
  await frame(binary(1));const first=socket();await render({live:false});expect(first.closed).toBe(true);
  await render({live:true});await act(async()=>socket().open());await act(async()=>decode.resolve({close}));
  expect(mocks.draw).not.toHaveBeenCalled();expect(close).toHaveBeenCalledOnce();expect(first.sent.some(x=>x.t==="ack")).toBe(false);
  await frame(binary(2));expect(mocks.draw).toHaveBeenCalledOnce();expect(socket().sent).toContainEqual({t:"ack",id:2});
});
it("forwards ordinary typing but holds keys, IME, paste and dialog answers under a card",async()=>{
  await ready();await key("a");expect(socket().sent).toContainEqual(expect.objectContaining({t:"key",e:expect.objectContaining({key:"a"})}));
  await frame({t:"dialog",id:"1234abcd",kind:"prompt",message:"question",default:""});
  await render({blocked:true});socket().sent=[];
  await key("b");await act(async()=>{input().dispatchEvent(new CompositionEvent("compositionend",{bubbles:true,data:"blocked text"}));
    const event=new Event("paste",{bubbles:true,cancelable:true});Object.defineProperty(event,"clipboardData",{value:{getData:()=>"blocked paste"}});input().dispatchEvent(event);
    host.querySelector<HTMLButtonElement>('[data-testid="browser-dialog"] button')!.click();});
  expect(socket().sent).toEqual([]);expect(host.textContent).toContain("This page says");
});
it("keeps HUD chords out of the page and routes pane navigation explicitly",async()=>{
  await ready();await key("d",{ctrlKey:true});await key("+",{ctrlKey:true});expect(socket().sent).toEqual([]);
  await key("p",{ctrlKey:true});expect(socket().sent.at(-1).e.key).toBe("p");
  await key("l",{ctrlKey:true});expect(props.onAction).toHaveBeenCalledWith("address");
});
it("ignores unsolicited selection and performs cut only after clipboard success",async()=>{
  await ready();await frame({t:"selection",text:"unsolicited"});expect(mocks.clipboard).not.toHaveBeenCalled();
  const saved=deferred<void>();mocks.clipboard.mockReturnValue(saved.promise);
  await key("x",{ctrlKey:true});expect(socket().sent).toEqual([{t:"selection"}]);
  await frame({t:"selection",text:"owner selection"});expect(mocks.clipboard).toHaveBeenCalledWith("owner selection");expect(socket().sent).toEqual([{t:"selection"}]);
  await act(async()=>saved.resolve());expect(socket().sent.filter(x=>x.t==="key")).toHaveLength(2);
});
it("cancels an in-flight clipboard cut permanently when a card appears",async()=>{
  await ready();const saved=deferred<void>();mocks.clipboard.mockReturnValue(saved.promise);
  await key("x",{ctrlKey:true});await frame({t:"selection",text:"keep"});await render({blocked:true});await render({blocked:false});
  socket().sent=[];await act(async()=>saved.resolve());expect(socket().sent).toEqual([]);
});
it("does not delete when clipboard writing rejects",async()=>{
  await ready();mocks.clipboard.mockRejectedValue(new Error("denied"));await key("x",{ctrlKey:true});await frame({t:"selection",text:"keep"});
  expect(socket().sent).toEqual([{t:"selection"}]);expect(host.textContent).toContain("Nothing was cut");
});
it("never adopts a cancelled selection reply as a later owner request",async()=>{
  await ready();await key("x",{ctrlKey:true});await render({blocked:true});await render({blocked:false});
  await key("c",{ctrlKey:true});expect(socket().sent.filter(x=>x.t==="selection")).toHaveLength(1);
  await frame({t:"selection",text:"late"});expect(mocks.clipboard).not.toHaveBeenCalled();
  await key("c",{ctrlKey:true});expect(socket().sent.filter(x=>x.t==="selection")).toHaveLength(2);
});
it("page dialogs and metadata are plain text and never steal focus",async()=>{
  await ready();const outside=document.createElement("button");document.body.appendChild(outside);outside.focus();
  await frame({t:"location",url:"https://example.test/",title:"<img src=x onerror=alert(1)>"});
  await frame({t:"dialog",id:"1234abcd",kind:"alert",message:"<img src=x onerror=alert(1)>",default:""});
  expect(host.querySelector("img")).toBeNull();expect(host.textContent).toContain("<img");expect(document.activeElement).toBe(outside);expect(props.onLocation).toHaveBeenCalledWith("https://example.test/",expect.stringContaining("<img"));outside.remove();
});
it("closes hidden streams and uses a fresh one-use ticket on return",async()=>{
  await ready();const first=socket();await render({live:false});expect(first.closed).toBe(true);await render({live:true});expect(mocks.write).toHaveBeenCalledTimes(2);expect(Socket.instances).toHaveLength(2);
});

it("sound instance replacement drops same-ID late frames, location and clipboard cut",async()=>{
  await render({instanceGeneration:1});await act(async()=>socket().open());socket().sent=[];
  const decode=deferred<any>(),saved=deferred<void>(),close=vi.fn();
  mocks.decode.mockReturnValueOnce(decode.promise);mocks.clipboard.mockReturnValue(saved.promise);
  await frame(binary(1));await key("x",{ctrlKey:true});await frame({t:"selection",text:"old"});
  const old=socket(),late=old.onmessage!;await render({instanceGeneration:2});
  expect(old.closed).toBe(true);expect(mocks.write).toHaveBeenCalledTimes(2);
  await act(async()=>{socket().open();late({data:JSON.stringify({t:"location",url:"https://stale.test/",title:"stale"})});decode.resolve({close});saved.resolve();});
  expect(mocks.draw).not.toHaveBeenCalled();expect(close).toHaveBeenCalledOnce();expect(props.onLocation).not.toHaveBeenCalled();
  expect(old.sent.filter(x=>x.t==="key")).toHaveLength(0);expect(socket().sent.some(x=>x.t==="key")).toBe(false);
});
it("hidden sound generation changes request no ticket until drawn and never focus a page",async()=>{
  await render({live:false,instanceGeneration:1});await render({instanceGeneration:3});expect(mocks.write).not.toHaveBeenCalled();
  await render({live:true});await act(async()=>socket().open());expect(mocks.write).toHaveBeenCalledOnce();
  expect(socket().sent.some(x=>x.t==="focus"||x.t==="key")).toBe(false);expect(props.onOwnerFocus).not.toHaveBeenCalled();
});

async function replay(paused=false){await frame({t:"agent",kind:"driver",thread_id:"driver-thread",provider:"fast"});await frame({t:"agent",kind:"paused",paused,by:paused?"owner":null});await frame({t:"agent",kind:"list",actions:[]});}
const driveButton=(kind:string)=>host.querySelector<HTMLButtonElement>(`[data-testid="drive-${kind}"]`);
it("agent replay uses displayed slots; only successful owner input mirrors takeover",async()=>{
  await ready();await render({slotOf:id=>id==="driver-thread"?2:null});await replay();
  expect(host.textContent).toContain("chat in pane 2");await key("d",{ctrlKey:true});expect(driveButton("stop")).not.toBeNull();
  const realSend=socket().send;socket().send=()=>{throw new Error("closed");};await key("a");expect(driveButton("stop")).not.toBeNull();
  socket().send=realSend;await key("b");expect(host.textContent).toContain("you took over");expect(driveButton("resume")?.disabled).toBe(false);
});
it("Stop and Resume use the current socket, wait for confirmation and stay inert under a card",async()=>{
  await ready();await replay();await act(async()=>driveButton("stop")!.click());expect(socket().sent).toContainEqual({t:"stop"});expect(driveButton("resume")?.disabled).toBe(true);
  await frame({t:"agent",kind:"paused",paused:true,by:"stop"});await render({blocked:true});socket().sent=[];
  const resume=driveButton("resume")!;resume.disabled=false;await act(async()=>resume.click());expect(socket().sent).toEqual([]);
  await render({blocked:false});await act(async()=>driveButton("resume")!.click());expect(socket().sent).toEqual([{t:"resume"}]);expect(host.textContent).toContain("Paused");
  await frame({t:"agent",kind:"paused",paused:false});expect(host.textContent).toContain("Driven by");
});
it("old socket agent replies cannot revive Resume after hide and reattach",async()=>{
  await ready();await replay(true);const first=socket(),stale=first.onmessage!;
  await render({live:false});expect(driveButton("resume")?.disabled).toBe(true);
  await render({live:true});await act(async()=>socket().open());expect(driveButton("resume")).toBeNull();
  await act(async()=>stale({data:JSON.stringify({t:"agent",kind:"paused",paused:true,by:"owner"})}));expect(driveButton("resume")).toBeNull();
  await frame({t:"agent",kind:"paused",paused:true,by:"owner"});expect(driveButton("resume")?.disabled).toBe(true);
  await frame({t:"agent",kind:"list",actions:[]});expect(driveButton("resume")?.disabled).toBe(false);
  await act(async()=>socket().onclose?.());expect(driveButton("resume")?.disabled).toBe(true);
});
it("annotation/card blocking leaves frames flowing without target pixels or active controls",async()=>{
  await ready();await replay();await frame({t:"agent",kind:"action",id:"one",at:Date.now(),action:"click",outcome:"asking",target:"target",text:"",rect:{x:1,y:2,width:10,height:20},page_identity:1,document_generation:2});
  await render({blocked:true});await frame(binary(1));expect(mocks.draw).toHaveBeenCalledOnce();expect(driveButton("stop")?.disabled).toBe(true);
  expect(host.querySelector('[data-testid="drive-outline"]')).toBeNull();expect(host.querySelectorAll("canvas")).toHaveLength(1);
});

it("an options hold blocks input synchronously before its blocked render while frames keep flowing",async()=>{
  let held=false;await render({inputHeld:()=>held});await act(async()=>socket().open());socket().sent=[];
  await key("a");expect(socket().sent.some(x=>x.t==="key")).toBe(true);socket().sent=[];
  held=true;
  await key("b");
  await act(async()=>{
    input().dispatchEvent(new CompositionEvent("compositionend",{bubbles:true,data:"menu text"}));
    const paste=new Event("paste",{bubbles:true,cancelable:true});
    Object.defineProperty(paste,"clipboardData",{value:{getData:()=>"menu paste"}});
    input().dispatchEvent(paste);
    host.querySelector('[data-testid="remote-surface"]')!.dispatchEvent(new MouseEvent("mousedown",{bubbles:true,button:0,clientX:1,clientY:1}));
  });
  expect(socket().sent).toEqual([]);
  await frame(binary(1));expect(mocks.draw).toHaveBeenCalledOnce();
  expect(socket().sent).toContainEqual({t:"ack",id:1});
});
