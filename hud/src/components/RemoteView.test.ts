import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { RemoteView, type RemoteViewProps } from "./RemoteView";
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
  Socket.instances=[];vi.stubGlobal("WebSocket",Socket);
  vi.stubGlobal("ResizeObserver",class{observe(){}disconnect(){}});
  vi.stubGlobal("createImageBitmap",mocks.decode);
  Object.defineProperty(navigator,"clipboard",{configurable:true,value:{writeText:mocks.clipboard}});
  mocks.write.mockReset().mockResolvedValue({ticket:"one-use"});mocks.clipboard.mockReset().mockResolvedValue(undefined);
  mocks.draw.mockReset();mocks.decode.mockReset().mockResolvedValue({close:vi.fn()});
  vi.spyOn(HTMLCanvasElement.prototype,"getContext").mockReturnValue({clearRect:vi.fn(),drawImage:mocks.draw} as any);
  host=document.createElement("div");document.body.appendChild(host);root=createRoot(host);
  props={tabId:"0123abcd",live:true,blocked:false,zoom:100,projectId:null,onLocation:vi.fn(),onPopup:vi.fn(),onAction:vi.fn(),onOwnerFocus:vi.fn()};
});
afterEach(async()=>{await act(async()=>root.unmount());host.remove();vi.restoreAllMocks();vi.unstubAllGlobals();vi.useRealTimers();});
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
