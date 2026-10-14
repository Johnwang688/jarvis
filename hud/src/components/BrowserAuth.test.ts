import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { BrowserAuth } from "./BrowserAuth";
import type { BrowserAuth as Challenge } from "../lib/browserauth";
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
const first:Challenge={id:"a".repeat(32),origin:"https://example.test",realm:"<img src=x>",scheme:"basic",expiresIn:60};
let host:HTMLDivElement,root:Root,onAnswer:ReturnType<typeof vi.fn>,onCancel:ReturnType<typeof vi.fn>;
const input=(id:string)=>host.querySelector<HTMLInputElement>(`[data-testid="browser-auth-${id}"]`)!;
async function change(id:string,value:string){await act(async()=>{Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value")!.set!.call(input(id),value);input(id).dispatchEvent(new Event("input",{bubbles:true}));});}
async function render(challenge=first,blocked=false){await act(async()=>root.render(createElement(BrowserAuth,{challenge,blocked,onAnswer,onCancel})));}
async function submit(){await act(async()=>host.querySelector("form")!.dispatchEvent(new Event("submit",{bubbles:true,cancelable:true})));}
beforeEach(()=>{vi.useFakeTimers();vi.setSystemTime(100000);host=document.createElement("div");document.body.append(host);root=createRoot(host);onAnswer=vi.fn(()=>true);onCancel=vi.fn(()=>true);});
afterEach(async()=>{await act(async()=>root.unmount());host.remove();vi.useRealTimers();});
it("never auto-focuses, masks the secret, and renders realm as inert text",async()=>{
  const focus=document.createElement("button");document.body.append(focus);focus.focus();await render();
  expect(document.activeElement).toBe(focus);expect(input("secret").type).toBe("password");expect(input("secret").autocomplete).toBe("off");
  expect(host.textContent).toContain("<img src=x>");expect(host.querySelector("img")).toBeNull();focus.remove();
});
it("sends fields once for the exact nonce then clears both form values",async()=>{
  const storage=vi.spyOn(Storage.prototype,"setItem");await render();await change("user","owner");await change("secret","synthetic-"+String(Date.now()));
  await submit();await submit();expect(onAnswer).toHaveBeenCalledExactlyOnceWith(first.id,"owner","synthetic-100000");
  expect(input("user").value).toBe("");expect(input("secret").value).toBe("");expect(storage).not.toHaveBeenCalled();storage.mockRestore();
});
it("a higher hold clears fields and irrevocably disables the old nonce after it lifts",async()=>{
  await render();await change("secret","temporary");await render(first,true);expect(input("secret").value).toBe("");
  input("secret").disabled=false;await submit();expect(onAnswer).not.toHaveBeenCalled();await render(first,false);await submit();expect(onAnswer).not.toHaveBeenCalled();
  expect(host.textContent).toContain("fresh request");await render({...first,id:"b".repeat(32)});await change("user","new");await submit();expect(onAnswer).toHaveBeenCalledExactlyOnceWith("b".repeat(32),"new","");
});
it("same nonce rerenders cannot extend the original deadline",async()=>{
  await render();await act(async()=>vi.advanceTimersByTime(59000));await render({...first,expiresIn:60});
  await act(async()=>vi.advanceTimersByTime(1000));await submit();expect(onAnswer).not.toHaveBeenCalled();expect(host.textContent).toContain("expired");expect(vi.getTimerCount()).toBe(0);
});
it("replacement discards typed values instead of replying to a new challenge with them",async()=>{
  await render();await change("user","prior");await change("secret","prior temporary");await render({...first,id:"b".repeat(32),origin:"https://other.test"});
  expect(input("user").value).toBe("");expect(input("secret").value).toBe("");expect(onAnswer).not.toHaveBeenCalled();expect(host.textContent).toContain("https://other.test");
});
it("failed send and thrown error never show credentials or preserve a queued answer",async()=>{
  await render();await change("secret","temporary raw");onAnswer.mockImplementationOnce(()=>{throw new Error("temporary raw");});await submit();
  expect(host.textContent).not.toContain("temporary raw");expect(input("secret").value).toBe("");expect(host.textContent).toContain("could not be sent");
  await change("secret","new value");await submit();expect(onAnswer).toHaveBeenLastCalledWith(first.id,"","new value");
});
it("Escape cancels once only without a higher approval; Enter outside fields does not submit",async()=>{
  await render(first,true);const blockedEscape=new KeyboardEvent("keydown",{key:"Escape",bubbles:true,cancelable:true});
  await act(async()=>host.querySelector("form")!.dispatchEvent(blockedEscape));expect(blockedEscape.defaultPrevented).toBe(false);expect(onCancel).not.toHaveBeenCalled();
  await render({...first,id:"b".repeat(32)},false);const form=host.querySelector("form")!;const enter=new KeyboardEvent("keydown",{key:"Enter",bubbles:true,cancelable:true});
  await act(async()=>form.dispatchEvent(enter));expect(enter.defaultPrevented).toBe(true);
  await act(async()=>{input("user").dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true,cancelable:true}));input("user").dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true,cancelable:true}));});
  expect(onCancel).toHaveBeenCalledExactlyOnceWith("b".repeat(32));expect(onAnswer).not.toHaveBeenCalled();
});
