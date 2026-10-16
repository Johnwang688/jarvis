import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { BrowserProfile, passwordLine, type PasswordSync } from "./BrowserProfile";
const mocks=vi.hoisted(()=>({get:vi.fn(),write:vi.fn(),remove:vi.fn()}));
vi.mock("../api",()=>({browserApi:mocks}));
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
const UNLOCK="Unlock the keyring in Windows Terminal (not a Jarvis terminal): read -rsp 'Keyring password: ' KP";
const sync=(over:Partial<PasswordSync>={}):PasswordSync=>({password_store:null,requested:"keyring",keyring:"unlocked",next_launch:"gnome-libsecret",unprotected:false,hint:null,...over});
const main={id:"p-12345678",label:"Main",created:"2026-10-15T00:00:00+00:00"};

it("says the keyring quietly and every other store loudly",()=>{
  expect(passwordLine(sync(),main)).toEqual({text:"Passwords are encrypted with your keyring (gnome-libsecret).",loud:false});
  expect(passwordLine(sync(),{...main,password_store:"keyring"}).text).toContain("never uses the basic store");
  const basic=passwordLine(sync({keyring:"unavailable",next_launch:"basic",unprotected:true,hint:"Unprotected (basic store): install"}),main);
  expect(basic).toEqual({text:"Unprotected (basic store): install",loud:true});
  expect(passwordLine(sync({keyring:"locked",next_launch:"refused",hint:UNLOCK}),main)).toEqual({text:UNLOCK,loud:true});
  expect(passwordLine(sync({keyring:"unavailable",next_launch:"basic",unprotected:true,hint:"x"}),{...main,password_store:"keyring"}).loud).toBe(true);
  expect(passwordLine(null,main)).toEqual({text:"Password store: checking…",loud:false});
});

describe("Manage panel",()=>{
  let root:Root;let host:HTMLDivElement;
  beforeEach(()=>{host=document.createElement("div");document.body.appendChild(host);root=createRoot(host);mocks.get.mockReset();});
  afterEach(async()=>{await act(async()=>root.unmount());host.remove();});
  const control:any={profiles:[main],online:true,globalDrive:true,driveReady:()=>true,drivePreference:()=>false,profile:vi.fn(),see:vi.fn(),drive:vi.fn(),signin:vi.fn(),refresh:vi.fn()};
  it("shows the unlock command from /status and never a store it was not told",async()=>{
    mocks.get.mockResolvedValue({sync:sync({keyring:"locked",next_launch:"refused",hint:UNLOCK})});
    await act(async()=>root.render(createElement(BrowserProfile,{pane:1,control,selected:main.id,see:false,blocked:false})));
    expect(host.querySelector('[data-testid="browser-password-store"]')).toBeNull();
    await act(async()=>[...host.querySelectorAll("button")].find(b=>b.textContent==="Manage")!.click());
    expect(mocks.get).toHaveBeenCalledWith("");
    const line=host.querySelector('[data-testid="browser-password-store"]')!;
    expect(line.textContent).toBe(UNLOCK);expect(line.className).toBe("err");
    expect(host.textContent).not.toContain("passwords use Chrome’s basic store");
  });
  it("treats a malformed status as unknown",async()=>{
    mocks.get.mockResolvedValue({sync:{password_store:"basic",source:"launch_argument"}});
    await act(async()=>root.render(createElement(BrowserProfile,{pane:1,control,selected:main.id,see:false,blocked:false})));
    await act(async()=>[...host.querySelectorAll("button")].find(b=>b.textContent==="Manage")!.click());
    expect(host.querySelector('[data-testid="browser-password-store"]')!.textContent).toBe("Password store: checking…");
  });
});
