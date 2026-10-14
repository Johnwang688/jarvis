import {describe,it,expect} from "vitest";
import {defaultWorkspace,setView,addBrowserTab,setBrowserDrive,setBrowserSee,setBrowserProfile,setBrowserRemote,workspaceStorage,parseWorkspace} from "./workspace";
describe("driving preference persistence",()=>{
 it("see-off clears preference and seeing again never reenables it",()=>{
  let ws=setView(defaultWorkspace(),1,"browser");ws=setBrowserSee(ws,1,false);ws=setBrowserSee(ws,1,true);
  expect(ws.panes[0].drive).toBe(false);
  ws=setBrowserDrive(ws,1,true);expect(ws.panes[0].drive).toBeUndefined();
  ws=setBrowserDrive(ws,1,false);expect(parseWorkspace(JSON.stringify(workspaceStorage(ws))).panes[0].drive).toBe(false);
 });
 it("Private cannot persist an on grant and profile changes preserve named off",()=>{
  let ws=addBrowserTab(setView(defaultWorkspace(),1,"browser"),1,"https://example.test",{id:"abcdef"});
  ws=setBrowserDrive(ws,1,false);ws=setBrowserProfile(ws,1,"private");
  ws=setBrowserRemote(ws,1,"abcdef",{mode:"remote",ctx:"0123abcd",remoteTabId:"11111111"});
  expect(setBrowserDrive(ws,1,true)).toBe(ws);
  const stored=workspaceStorage(ws);expect(JSON.stringify(stored)).not.toContain('"drive":true');
  expect(parseWorkspace(JSON.stringify(stored)).panes[0].drive).toBe(false);
  ws=setBrowserProfile(ws,1,"p-0123abcd");expect(ws.panes[0].drive).toBe(false);
 });
});
