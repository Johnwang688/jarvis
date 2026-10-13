import { describe, expect, it } from "vitest";
import { addBrowserTab, copySpec, defaultWorkspace, parseWorkspace, parseBrowserTabs,
  saveWorkspace, setBrowserProfile, setBrowserRemote, setBrowserSee, setBrowserUrl, setView,
  setTabKeepOrigin, workspaceStorage } from "./workspace";
import { browserFrames, EMPTY_FRAMES } from "./browserframes";
const URL = "http://localhost:23456/PRIVATE-PATH";
const PROFILE = "p-0123abcd";
const REMOTE = "0123abcd";
function workspace() {
  return addBrowserTab(setView(defaultWorkspace(), 1, "browser"), 1, URL,
    { id: "abcdef", auto: { kind: "terminal", label: "PRIVATE-LABEL" }, source: {kind:"dev_server",entryId:"ds_0123abcd"} });
}

describe("Remote pane state and Private persistence", () => {
  it("preserves validated mode/handle while dropping invalid handles and origin grants", () => {
    const parsed = parseBrowserTabs({tabs:[{id:"abcdef",mode:"remote",url:URL,remoteTabId:REMOTE,
      keepOrigin:"http://localhost:23456",title:"safe\u202etitle"}],active:"abcdef"})!;
    expect(parsed.tabs[0]).toEqual({id:"abcdef",mode:"remote",url:URL,remoteTabId:REMOTE,title:"safetitle"});
    expect(parseBrowserTabs({tabs:[{id:"abcdef",mode:"remote",remoteTabId:"../bad"}],active:"abcdef"})!.tabs[0])
      .toEqual({id:"abcdef",mode:"remote",url:""});
    expect(parseBrowserTabs({tabs:[{id:"abcdef",mode:"unknown",url:URL}],active:"abcdef"})).toBeNull();
  });
  it("Private saves omit every URL/title/grant/auto label and leave runtime state intact", () => {
    let ws=setBrowserProfile(workspace(),1,"private");
    ws=setBrowserRemote(ws,1,"abcdef",{mode:"remote",remoteTabId:REMOTE,ctx:"aaaaaaaa",url:URL,title:"PRIVATE-TITLE"});
    ws=setBrowserSee(ws,1,false);
    let stored=""; saveWorkspace(ws,{setItem:(_k,v)=>{stored=v;}});
    for (const privateValue of [URL,"PRIVATE-PATH","PRIVATE-LABEL","PRIVATE-TITLE","browserUrl","previewUrl","keepOrigin"]) expect(stored).not.toContain(privateValue);
    expect(ws.panes[0].browser!.tabs[0].url).toBe(URL);
    expect(ws.panes[0].browser!.tabs[0].title).toBe("PRIVATE-TITLE");
    expect(ws.panes[0].browserPrivateEnded).toBeUndefined();
    const restored=parseWorkspace(stored).panes[0];
    expect(restored.browserProfile).toBe("private"); expect(restored.browserCtx).toBe("aaaaaaaa");
    expect(restored.browserPrivateEnded).toBe(true); expect(restored.see).toBe(false);
    expect(restored.browser!.tabs[0]).toEqual({id:"abcdef",mode:"remote",url:"",remoteTabId:REMOTE,source:{kind:"dev_server",entryId:"ds_0123abcd"}});
  });
  it("Private legacy and malformed context never restore a Direct renderer", () => {
    const ws=parseWorkspace(JSON.stringify({panes:[{view:"browser",browserProfile:"private",browserCtx:"bad",
      previewUrl:URL,keepOrigin:"http://localhost:23456",see:false,drive:true}]}));
    expect(ws.panes[0].browser!.tabs[0].mode).toBe("remote");
    expect(ws.panes[0].keepOrigin).toBeUndefined(); expect(ws.panes[0].browserCtx).toBeUndefined();
    expect(ws.panes[0].browserPrivateEnded).toBe(true); expect(ws.panes[0].drive).toBe(false);
    expect(JSON.stringify(workspaceStorage(ws))).not.toContain("PRIVATE-PATH");
    expect(browserFrames(EMPTY_FRAMES,ws,[1]).recent).toEqual([]);
  });
  it("copy uses independent handles/context but preserves see-off and restrictive source", () => {
    let ws=setBrowserProfile(workspace(),1,"private"); ws=setBrowserSee(ws,1,false);
    ws=setBrowserRemote(ws,1,"abcdef",{mode:"remote",remoteTabId:REMOTE,ctx:"aaaaaaaa",url:URL,title:"old"});
    const copy=copySpec(ws.panes[0]);
    expect(copy.browserProfile).toBe("private"); expect(copy.see).toBe(false);
    expect(copy.browserCtx).toBeUndefined(); expect(copy.browserPrivateEnded).toBeUndefined();
    const tab=copy.browser!.tabs[0]; expect(tab.id).not.toBe("abcdef");
    expect(tab.mode).toBe("remote"); expect(tab.url).toBe(URL);
    expect(tab.remoteTabId).toBeUndefined(); expect(tab.auto).toBeUndefined(); expect(tab.title).toBeUndefined();
    expect(tab.source).toEqual({kind:"dev_server",entryId:"ds_0123abcd"});
  });
  it("profile changes and passive navigation preserve see and source and retire old handles", () => {
    let ws=setBrowserProfile(workspace(),1,"private"); ws=setBrowserSee(ws,1,false);
    ws=setBrowserRemote(ws,1,"abcdef",{mode:"remote",remoteTabId:REMOTE,ctx:"aaaaaaaa",url:URL});
    ws=setBrowserProfile(ws,1,PROFILE);
    expect(ws.panes[0].see).toBe(false); expect(ws.panes[0].browserCtx).toBeUndefined();
    expect(ws.panes[0].browser!.tabs[0].remoteTabId).toBeUndefined();
    ws=setBrowserUrl(ws,1,"https://example.test/redirect","abcdef");
    expect(ws.panes[0].browser!.tabs[0].source?.kind).toBe("dev_server");
    expect(ws.panes[0].see).toBe(false);
  });
  it("Remote metadata rejects malformed identities, Direct in Private and origin grants", () => {
    const ws=setBrowserProfile(workspace(),1,"private");
    expect(setBrowserRemote(ws,1,"abcdef",{mode:"direct"})).toBe(ws);
    expect(setBrowserRemote(ws,1,"abcdef",{mode:"remote",remoteTabId:"bad"})).toBe(ws);
    expect(setBrowserRemote(ws,1,"abcdef",{mode:"remote",ctx:"bad"})).toBe(ws);
    expect(setTabKeepOrigin(ws,1,"abcdef","http://localhost:23456")).toBe(ws);
  });
  it("Remote tabs leave Direct frame allocation immediately and don't consume retained slots", () => {
    let ws=workspace(); const frames=browserFrames(EMPTY_FRAMES,ws,[1]); expect(frames.recent).toHaveLength(1);
    ws=setBrowserRemote(ws,1,"abcdef",{mode:"remote",remoteTabId:REMOTE});
    expect(browserFrames(frames,ws,[1]).recent).toEqual([]);
  });
  it("persistent state keeps its URL and only false see is stored", () => {
    let ws=setBrowserProfile(workspace(),1,PROFILE); ws=setBrowserSee(ws,1,false);
    expect(workspaceStorage(ws).panes[0].browser!.tabs[0].url).toBe(URL);
    ws=setBrowserSee(ws,1,true); expect(workspaceStorage(ws).panes[0].see).toBeUndefined();
    const privatePane=setBrowserProfile(ws,1,"private");
    expect(addBrowserTab(privatePane,1,URL).panes[0].browser!.tabs.at(-1)!.mode).toBe("remote");
  });
});
