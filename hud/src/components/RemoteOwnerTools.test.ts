import {act,createElement} from "react";
import {createRoot,type Root} from "react-dom/client";
import {RemoteContextMenu,RemoteFind} from "./RemoteOwnerTools";
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
let host:HTMLDivElement,root:Root;
beforeEach(()=>{host=document.createElement("div");document.body.appendChild(host);root=createRoot(host);});
afterEach(async()=>{await act(async()=>root.unmount());host.remove();document.getElementById("authveil")?.remove();});
it("only explicit mounted find focuses, Enter/ShiftEnter submit, editing alone does not search",async()=>{
 const onEdit=vi.fn(),onFind=vi.fn(),onClose=vi.fn();
 await act(async()=>root.render(createElement(RemoteFind,{query:"term",status:"idle",blocked:false,onEdit,onFind,onClose})));
 const input=host.querySelector("input")!;expect(document.activeElement).toBe(input);
 await act(async()=>input.dispatchEvent(new KeyboardEvent("keydown",{bubbles:true,key:"Enter",cancelable:true})));
 await act(async()=>input.dispatchEvent(new KeyboardEvent("keydown",{bubbles:true,key:"Enter",shiftKey:true,cancelable:true})));
 expect(onFind.mock.calls).toEqual([[false],[true]]);expect(onEdit).not.toHaveBeenCalled();
 expect(host.textContent).toContain("Top document only");
});
it("find handlers recheck disabled state and Escape leaves higher approval first",async()=>{
 const onEdit=vi.fn(),onFind=vi.fn(),onClose=vi.fn();
 await act(async()=>root.render(createElement(RemoteFind,{query:"term",status:"idle",blocked:true,onEdit,onFind,onClose})));
 const next=host.querySelectorAll("button")[1];next.disabled=false;await act(async()=>next.click());expect(onFind).not.toHaveBeenCalled();
 const veil=document.createElement("div");veil.id="authveil";document.body.appendChild(veil);let escaped=false;
 document.addEventListener("keydown",()=>escaped=true,{once:true});
 await act(async()=>host.querySelector("input")!.dispatchEvent(new KeyboardEvent("keydown",{bubbles:true,key:"Escape"})));
 expect(escaped).toBe(true);expect(onClose).not.toHaveBeenCalled();
});
it("menu has only the five bounded actions, local focus and explicit buttons",async()=>{
 const onAction=vi.fn(),onClose=vi.fn();
 await act(async()=>root.render(createElement(RemoteContextMenu,{point:{x:500,y:500},bounds:{width:300,height:200},blocked:false,onAction,onClose})));
 expect([...host.querySelectorAll("button")].map(b=>b.textContent)).toEqual(["Back","Forward","Reload","Find","Copy selection"]);
 expect(document.activeElement).toBe(host.querySelector("button"));
 await act(async()=>host.querySelectorAll("button")[4].click());expect(onAction).toHaveBeenCalledWith("copy");
});
it("tampered disabled menu buttons cannot invoke a page action",async()=>{
 const onAction=vi.fn(),onClose=vi.fn();
 await act(async()=>root.render(createElement(RemoteContextMenu,{point:{x:5,y:5},bounds:{width:300,height:200},blocked:true,onAction,onClose})));
 const first=host.querySelector("button")!;first.disabled=false;await act(async()=>first.click());expect(onAction).not.toHaveBeenCalled();
});
