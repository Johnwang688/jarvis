import {act,createElement} from "react";
import {createRoot,type Root} from "react-dom/client";
import {RemoteView,type RemoteViewProps} from "./RemoteView";
const mocks=vi.hoisted(()=>({write:vi.fn(),clipboard:vi.fn()}));
vi.mock("../api",()=>({browserApi:{write:mocks.write},api:{}}));
vi.mock("./BrowserFiles",()=>({BrowserFiles:()=>null}));
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
class Socket {
 static OPEN=1;static last:Socket;readyState=0;onopen:(()=>void)|null=null;onclose:(()=>void)|null=null;onerror:(()=>void)|null=null;onmessage:((e:{data:unknown})=>void)|null=null;binaryType="";sent:any[]=[];
 constructor(){Socket.last=this;}send(raw:string){this.sent.push(JSON.parse(raw));}close(){this.readyState=3;}
 open(){this.readyState=1;this.onopen?.();}message(message:object){this.onmessage?.({data:JSON.stringify(message)});}
}
let host:HTMLDivElement,root:Root,props:RemoteViewProps;
const socket=()=>Socket.last;
const proxy=()=>host.querySelector<HTMLTextAreaElement>('[data-testid=remote-input]')!;
const surface=()=>host.querySelector<HTMLElement>('[data-testid=remote-surface]')!;
const key=async()=>act(async()=>proxy().dispatchEvent(new KeyboardEvent("keydown",{bubbles:true,cancelable:true,key:"f",code:"KeyF",ctrlKey:true})));
const render=async(changed:Partial<RemoteViewProps>={})=>{props={...props,...changed};await act(async()=>root.render(createElement(RemoteView,props)));};
beforeEach(async()=>{
 mocks.write.mockReset().mockResolvedValue({ticket:"one"});mocks.clipboard.mockReset().mockResolvedValue(undefined);
 vi.stubGlobal("WebSocket",Socket);vi.stubGlobal("ResizeObserver",class{observe(){}disconnect(){}});
 vi.spyOn(HTMLElement.prototype,"clientWidth","get").mockReturnValue(320);vi.spyOn(HTMLElement.prototype,"clientHeight","get").mockReturnValue(200);
 Object.defineProperty(navigator,"clipboard",{configurable:true,value:{writeText:mocks.clipboard}});
 host=document.createElement("div");document.body.appendChild(host);root=createRoot(host);
 props={tabId:"1234abcd",live:true,blocked:false,zoom:100,projectId:null,onLocation:vi.fn(),onPopup:vi.fn(),onAction:vi.fn(),onOwnerFocus:vi.fn()};
 await render();await act(async()=>socket().open());socket().sent=[];
 surface().getBoundingClientRect=()=>({left:0,top:0,width:320,height:200,right:320,bottom:200,x:0,y:0,toJSON(){}}) as DOMRect;
});
afterEach(async()=>{await act(async()=>root.unmount());host.remove();vi.restoreAllMocks();vi.unstubAllGlobals();vi.useRealTimers();});
it("only the focused Remote proxy handles CtrlF and editing never searches",async()=>{
 const chat=document.createElement("textarea");document.body.appendChild(chat);chat.focus();
 await act(async()=>chat.dispatchEvent(new KeyboardEvent("keydown",{bubbles:true,cancelable:true,key:"f",ctrlKey:true})));
 expect(host.querySelector('[data-testid=remote-find]')).toBeNull();
 await act(async()=>proxy().focus());await key();
 const find=host.querySelector<HTMLInputElement>('[data-testid=remote-find-query]')!;expect(find).not.toBeNull();expect(document.activeElement).toBe(find);
 expect(socket().sent.some(m=>m.t==="key"&&m.e.key==="f")).toBe(false);expect(socket().sent.some(m=>m.t==="find")).toBe(false);
 chat.remove();
});
it("auth, hold, hide and same-document reset invalidate local find without focusing",async()=>{
 await act(async()=>proxy().focus());await key();expect(host.querySelector('[data-testid=remote-find]')).not.toBeNull();
 await act(async()=>socket().message({t:"owner_reset"}));expect(host.querySelector('[data-testid=remote-find]')).toBeNull();
 await act(async()=>proxy().focus());await key();await render({blocked:true});await render({blocked:false});expect(host.querySelector('[data-testid=remote-find]')).toBeNull();
 await act(async()=>proxy().focus());await key();
 await act(async()=>socket().message({t:"auth",id:"b".repeat(32),origin:"https://example.test",realm:"fixture",scheme:"basic",expires_in:60}));
 expect(host.querySelector('[data-testid=remote-find]')).toBeNull();socket().sent=[];
 await key();expect(socket().sent.some(m=>m.t==="find"||m.t==="cursor")).toBe(false);
});
it("consumes right-button press/release and menu actions use existing navigation/copy guards",async()=>{
 await act(async()=>{
  surface().dispatchEvent(new MouseEvent("mousedown",{bubbles:true,cancelable:true,button:2,buttons:2}));
  surface().dispatchEvent(new MouseEvent("mouseup",{bubbles:true,cancelable:true,button:2}));
  surface().dispatchEvent(new MouseEvent("contextmenu",{bubbles:true,cancelable:true,button:2,clientX:319,clientY:199}));
 });
 expect(socket().sent.some(m=>m.t==="mouse"&&m.e.button==="right")).toBe(false);
 const menu=host.querySelector('[data-testid=remote-context-menu]')!;
 expect(menu).not.toBeNull();await act(async()=>menu.querySelectorAll("button")[2].click());expect(props.onAction).toHaveBeenCalledWith("reload");
 await act(async()=>surface().dispatchEvent(new MouseEvent("contextmenu",{bubbles:true,cancelable:true,button:2,clientX:20,clientY:20})));
 await act(async()=>host.querySelector('[data-testid=remote-context-menu]')!.querySelectorAll("button")[4].click());
 expect(socket().sent).toContainEqual({t:"selection"});
 await act(async()=>socket().message({t:"selection",text:"explicit menu selection"}));expect(mocks.clipboard).toHaveBeenCalledWith("explicit menu selection");
});
it("menu copy cannot use a late document selection and stale socket replies cannot reopen tools",async()=>{
 await act(async()=>surface().dispatchEvent(new MouseEvent("contextmenu",{bubbles:true,cancelable:true,button:2,clientX:20,clientY:20})));
 await act(async()=>host.querySelector('[data-testid=remote-context-menu]')!.querySelectorAll("button")[4].click());
 await act(async()=>socket().message({t:"owner_reset"}));
 await act(async()=>socket().message({t:"selection",text:"stale"}));expect(mocks.clipboard).not.toHaveBeenCalled();
 const old=socket().onmessage!;await render({live:false});await render({live:true});await act(async()=>socket().open());
 await act(async()=>old({data:JSON.stringify({t:"find",seq:1,available:true,matched:true})}));
 expect(host.querySelector('[data-testid=remote-find]')).toBeNull();
});

it("a same-document reset cannot forward a previous key release or modifier state",async()=>{
 await act(async()=>proxy().focus());
 await act(async()=>proxy().dispatchEvent(new KeyboardEvent("keydown",{bubbles:true,cancelable:true,key:" ",code:"Space"})));
 expect(socket().sent.some(m=>m.t==="key"&&m.e.code==="Space")).toBe(true);
 await act(async()=>socket().message({t:"owner_reset"}));socket().sent=[];
 await act(async()=>proxy().dispatchEvent(new KeyboardEvent("keyup",{bubbles:true,cancelable:true,key:" ",code:"Space"})));
 expect(socket().sent.some(m=>m.t==="key")).toBe(false);
});
