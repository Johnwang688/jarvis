import {act,createElement,useRef} from "react";
import {createRoot,type Root} from "react-dom/client";
import {RemoteContextMenu,RemoteFind} from "./RemoteOwnerTools";
import {useRemoteOwnerTools} from "./useRemoteOwnerTools";
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
let root:Root,host:HTMLDivElement,owner:ReturnType<typeof useRemoteOwnerTools>;
let held:boolean,enabled:boolean,identity:string;
const send=vi.fn(),action=vi.fn(),copy=vi.fn(),focus=vi.fn(),release=vi.fn(),pageMouse=vi.fn();
function Harness(){
 const surface=useRef<HTMLDivElement>(null);
 owner=useRemoteOwnerTools({canInput:()=>!held,send,onAction:action,copy,focus,release,surface,enabled,identity});
 return createElement("div",{ref:surface,"data-testid":"surface",onContextMenu:owner.context,
  onMouseDown:(event:React.MouseEvent)=>{if(!owner.consumesMouse(event))pageMouse();},
  onMouseUp:(event:React.MouseEvent)=>{if(!owner.consumesMouse(event))pageMouse();}},
  owner.find?createElement(RemoteFind,{query:owner.query,status:owner.status,blocked:held,onEdit:owner.edit,onFind:owner.run,onClose:owner.closeFind}):null,
  owner.menu?createElement(RemoteContextMenu,{point:owner.menu,bounds:owner.menu.bounds,blocked:held,onAction:owner.menuAction,onClose:owner.closeMenu}):null);
}
async function render(){await act(async()=>root.render(createElement(Harness)));}
function event(type:string,extra:MouseEventInit={}){return new MouseEvent(type,{bubbles:true,cancelable:true,...extra});}
beforeEach(async()=>{
 held=false;enabled=true;identity="socket1:page1";for(const mock of [send,action,copy,focus,release,pageMouse])mock.mockReset();send.mockReturnValue(true);
 host=document.createElement("div");document.body.appendChild(host);root=createRoot(host);await render();
 const surface=host.querySelector<HTMLElement>('[data-testid=surface]')!;
 Object.defineProperty(surface,"clientWidth",{value:300});Object.defineProperty(surface,"clientHeight",{value:200});
 surface.getBoundingClientRect=()=>({left:0,top:0,width:300,height:200,right:300,bottom:200,x:0,y:0,toJSON(){}}) as DOMRect;
});
afterEach(async()=>{await act(async()=>root.unmount());host.remove();vi.useRealTimers();});
it("has no find from metadata; explicit owner Find opens and edits never search",async()=>{
 await act(async()=>owner.receive({t:"find",seq:1,available:true,matched:true}));expect(owner.find).toBe(false);expect(focus).not.toHaveBeenCalled();
 await act(async()=>owner.openFind());expect(owner.find).toBe(true);expect(document.activeElement).toBe(host.querySelector("input"));
 await act(async()=>owner.edit("query"));expect(send.mock.calls.some(([m])=>m.t==="find")).toBe(false);
 await act(async()=>owner.run(false));const one=send.mock.calls.find(([m])=>m.t==="find")![0];
 await act(async()=>owner.edit("changed"));await act(async()=>owner.receive({t:"find",seq:one.seq,available:true,matched:true}));
 expect(owner.status).toBe("idle");
});
it("holds permanently cancel find/cursor replies and cannot invoke tampered menu actions",async()=>{
 await act(async()=>owner.openFind());await act(async()=>owner.edit("query"));await act(async()=>owner.run(false));
 const one=send.mock.calls.find(([m])=>m.t==="find")![0];held=true;
 await act(async()=>owner.receive({t:"find",seq:one.seq,available:true,matched:true}));held=false;
 await act(async()=>owner.receive({t:"find",seq:one.seq,available:true,matched:true}));expect(owner.find).toBe(false);
 held=true;await act(async()=>owner.menuAction("reload"));expect(action).not.toHaveBeenCalled();
});
it("consumes right down/up and the complete first outside dismissal gesture",async()=>{
 const surface=host.querySelector<HTMLElement>('[data-testid=surface]')!;
 await act(async()=>{surface.dispatchEvent(event("mousedown",{button:2,buttons:2}));surface.dispatchEvent(event("mouseup",{button:2}));surface.dispatchEvent(event("contextmenu",{button:2,clientX:299,clientY:199}));});
 expect(pageMouse).not.toHaveBeenCalled();expect(owner.menu).not.toBeNull();
 await act(async()=>surface.dispatchEvent(event("pointerdown",{button:0,buttons:1})));
 expect(owner.menu).toBeNull();
 await act(async()=>{surface.dispatchEvent(event("mousedown",{button:0,buttons:1}));surface.dispatchEvent(event("mouseup",{button:0}));surface.dispatchEvent(event("pointerup",{button:0}));});
 expect(pageMouse).not.toHaveBeenCalled();
 await act(async()=>new Promise(resolve=>setTimeout(resolve,0)));
 await act(async()=>{surface.dispatchEvent(event("mousedown",{button:0,buttons:1}));surface.dispatchEvent(event("mouseup",{button:0}));});
 expect(pageMouse).toHaveBeenCalledTimes(2);
});
it("socket/document identity and hidden lifecycle reset memory without focus",async()=>{
 await act(async()=>owner.openFind());focus.mockClear();identity="socket1:page2";await render();
 expect(owner.find).toBe(false);expect(owner.query).toBe("");expect(focus).not.toHaveBeenCalled();
 await act(async()=>owner.openFind());enabled=false;await render();expect(owner.find).toBe(false);
});
it("coalesces movement and leaves metadata out of input takeover",async()=>{
 vi.useFakeTimers();await act(async()=>{for(let x=1;x<=100;x++)owner.move({x,y:1,inside:true});vi.advanceTimersByTime(100);});
 const request=send.mock.calls.find(([m])=>m.t==="cursor")!;expect(request[0].x).toBe(100);expect(request[1]).toBeUndefined();
 await act(async()=>owner.receive({t:"cursor",seq:request[0].seq,cursor:"pointer"}));expect(owner.cursor).toBe("pointer");
 await act(async()=>owner.clearCursor());expect(owner.cursor).toBe("default");
 await act(async()=>owner.receive({t:"cursor",seq:request[0].seq,cursor:"text"}));expect(owner.cursor).toBe("default");
});
