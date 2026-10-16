import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { BrowserProfile, PASSWORD_REFRESH_MS, passwordLine, type PasswordSync, type ProfileSync } from "./BrowserProfile";
const mocks=vi.hoisted(()=>({get:vi.fn(),write:vi.fn(),remove:vi.fn()}));
vi.mock("../api",()=>({browserApi:mocks}));
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
const UNLOCK="Unlock the keyring in Windows Terminal (not a Jarvis terminal): read -rsp 'Keyring password: ' KP";
const STICKY_BASIC="This profile's passwords are in the keyring; it never opens with the basic store. Remove \"password_store\": \"basic\" from browser.json to open it";
const sync=(over:Partial<PasswordSync>={}):PasswordSync=>({password_store:null,requested:"keyring",keyring:"unlocked",next_launch:"gnome-libsecret",unprotected:false,hint:null,...over});
const mine=(over:Partial<ProfileSync>={}):ProfileSync=>({store:null,sticky:false,next_launch:"gnome-libsecret",unprotected:false,hint:null,...over});
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

it("says checking, never refused, before the keyring was read (LOW 4)",()=>{
  expect(passwordLine(sync({keyring:"unknown",next_launch:"checking"}),main)).toEqual({text:"Password store: checking…",loud:false});
  expect(passwordLine(sync({keyring:"unknown",next_launch:"checking",profiles:{[main.id]:mine({next_launch:"checking"})}}),main))
    .toEqual({text:"Password store: checking…",loud:false});
});

it("uses the selected profile's own state over the global one (NIT 1)",()=>{
  // A sticky profile under browser.json's basic: the reason is browser.json, not the keyring.
  const s=sync({requested:"basic",next_launch:"basic",unprotected:true,hint:"Unprotected (basic store): browser.json asks for it",
    profiles:{[main.id]:mine({sticky:true,next_launch:"refused",hint:STICKY_BASIC})}});
  const line=passwordLine(s,{...main,password_store:"keyring"});
  expect(line).toEqual({text:STICKY_BASIC,loud:true});
  expect(line.text).toContain("browser.json");expect(line.text).not.toContain("Unlock");
  // What a running Chrome was observed to use wins over the next launch.
  expect(passwordLine(sync({password_store:"mixed",unprotected:true,profiles:{[main.id]:mine({store:"gnome-libsecret",sticky:true})}}),main))
    .toEqual({text:"Passwords are encrypted with your keyring (gnome-libsecret, checked); this profile never uses the basic store.",loud:false});
  expect(passwordLine(sync({profiles:{[main.id]:mine({store:"unverified",unprotected:true,hint:"Unprotected (basic store): install"})}}),main))
    .toEqual({text:"Unprotected (basic store): install",loud:true});
  // Another profile's state is not this one's.
  expect(passwordLine(sync({profiles:{"p-87654321":mine({next_launch:"refused",hint:UNLOCK})}}),main).loud).toBe(false);
});

describe("Manage panel",()=>{
  let root:Root;let host:HTMLDivElement;
  beforeEach(()=>{host=document.createElement("div");document.body.appendChild(host);root=createRoot(host);mocks.get.mockReset();});
  afterEach(async()=>{await act(async()=>root.unmount());host.remove();vi.useRealTimers();});
  const control:any={profiles:[main],online:true,globalDrive:true,driveReady:()=>true,drivePreference:()=>false,profile:vi.fn(),see:vi.fn(),drive:vi.fn(),signin:vi.fn(),refresh:vi.fn()};
  const manage=()=>[...host.querySelectorAll("button")].find(b=>b.textContent==="Manage")!;
  const line=()=>host.querySelector('[data-testid="browser-password-store"]');
  it("shows the unlock command from /status and never a store it was not told",async()=>{
    mocks.get.mockResolvedValue({sync:sync({keyring:"locked",next_launch:"refused",hint:UNLOCK})});
    await act(async()=>root.render(createElement(BrowserProfile,{pane:1,control,selected:main.id,see:false,blocked:false})));
    expect(line()).toBeNull();
    await act(async()=>manage().click());
    expect(mocks.get).toHaveBeenCalledWith("");
    expect(line()!.textContent).toBe(UNLOCK);expect(line()!.className).toBe("err");
    expect(host.textContent).not.toContain("passwords use Chrome’s basic store");
  });
  it("treats a malformed status as unknown",async()=>{
    mocks.get.mockResolvedValue({sync:{password_store:"basic",source:"launch_argument"}});
    await act(async()=>root.render(createElement(BrowserProfile,{pane:1,control,selected:main.id,see:false,blocked:false})));
    await act(async()=>manage().click());
    expect(line()!.textContent).toBe("Password store: checking…");
  });
  it("reads /status again while open, on an interval and on focus, and stops when closed",async()=>{
    vi.useFakeTimers();
    mocks.get.mockResolvedValue({sync:sync({keyring:"unknown",next_launch:"checking"})});
    await act(async()=>root.render(createElement(BrowserProfile,{pane:1,control,selected:main.id,see:false,blocked:false})));
    await act(async()=>manage().click());
    expect(line()!.textContent).toBe("Password store: checking…");expect(line()!.className).not.toBe("err");
    expect(mocks.get).toHaveBeenCalledTimes(1);
    // The owner unlocks the keyring in Windows Terminal; the open panel catches up.
    mocks.get.mockResolvedValue({sync:sync({profiles:{[main.id]:mine()}})});
    await act(async()=>{vi.advanceTimersByTime(PASSWORD_REFRESH_MS);});
    expect(mocks.get).toHaveBeenCalledTimes(2);
    expect(line()!.textContent).toBe("Passwords are encrypted with your keyring (gnome-libsecret).");
    mocks.get.mockResolvedValue({sync:sync({keyring:"locked",next_launch:"refused",hint:UNLOCK})});
    await act(async()=>{window.dispatchEvent(new Event("focus"));});
    expect(mocks.get).toHaveBeenCalledTimes(3);
    expect(line()!.textContent).toBe(UNLOCK);
    await act(async()=>manage().click());   // closed
    expect(line()).toBeNull();
    await act(async()=>{vi.advanceTimersByTime(PASSWORD_REFRESH_MS*3);window.dispatchEvent(new Event("focus"));});
    expect(mocks.get).toHaveBeenCalledTimes(3);
  });
});
