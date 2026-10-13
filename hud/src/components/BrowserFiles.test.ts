import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { BrowserFiles } from "./BrowserFiles";
const mocks=vi.hoisted(()=>({tree:vi.fn(),get:vi.fn(),write:vi.fn(),download:vi.fn(),click:vi.fn()}));
vi.mock("../api",()=>({api:{tree:mocks.tree},browserApi:mocks}));
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
let root:Root;let host:HTMLDivElement;let props:Parameters<typeof BrowserFiles>[0];
const render=async(change:Partial<typeof props>={})=>{props={...props,...change};await act(async()=>root.render(createElement(BrowserFiles,props)));};
const button=(text:string)=>[...host.querySelectorAll("button")].find(b=>b.textContent===text)!;
const deferred=<T,>()=>{let resolve!:(v:T)=>void;const promise=new Promise<T>(r=>resolve=r);return{resolve,promise};};
beforeEach(()=>{
  host=document.createElement("div");document.body.appendChild(host);root=createRoot(host);
  props={tabId:"0123abcd",projectId:"project-one",chooser:null,downloads:true,blocked:false,generation:1,onCloseDownloads:vi.fn()};
  mocks.get.mockReset().mockResolvedValue({downloads:[{id:"file1234",name:"output.txt",held:false},{id:"held1234",name:"app.exe",held:true}]});
  mocks.tree.mockReset().mockResolvedValue({entries:[{name:"safe.txt",kind:"file"},{name:".env",kind:"file",policy:"env"},{name:"keys",kind:"dir",policy:"withheld"}]});
  mocks.write.mockReset().mockResolvedValue({});mocks.download.mockReset().mockResolvedValue(new Blob(["file"]));mocks.click.mockReset();
  vi.spyOn(HTMLAnchorElement.prototype,"click").mockImplementation(mocks.click);
  vi.stubGlobal("URL",Object.assign(URL,{createObjectURL:vi.fn(()=>"blob:test"),revokeObjectURL:vi.fn()}));
});
afterEach(async()=>{await act(async()=>root.unmount());host.remove();vi.restoreAllMocks();vi.unstubAllGlobals();});
it("offers only safe project references to the current chooser",async()=>{
  await render({downloads:false,chooser:{id:"chooser1",multiple:false}});expect(host.textContent).not.toContain(".env");expect(host.textContent).not.toContain("keys");
  await act(async()=>host.querySelector<HTMLInputElement>("input")!.click());await act(async()=>button("Use selected file").click());
  expect(mocks.write).toHaveBeenCalledWith("/tabs/0123abcd/files",{chooser:"chooser1",files:[{project:"project-one",path:"safe.txt"}]});
  await render({chooser:{id:"chooser2",multiple:false}});expect(host.querySelector<HTMLInputElement>("input")!.checked).toBe(false);
});
it("saves an owner download but gives held executables no save gesture",async()=>{
  await render();expect([...host.querySelectorAll("button")].filter(b=>b.textContent==="Save a copy")).toHaveLength(1);
  await act(async()=>button("Save a copy").click());expect(mocks.download).toHaveBeenCalledWith("file1234");expect(mocks.click).toHaveBeenCalledOnce();
});
it("discards a pending download after a card even if the card closes before the reply",async()=>{
  const pending=deferred<Blob>();mocks.download.mockReturnValue(pending.promise);await render();await act(async()=>button("Save a copy").click());
  await render({blocked:true});await render({blocked:false});await act(async()=>pending.resolve(new Blob(["late"])));expect(mocks.click).not.toHaveBeenCalled();
});
it("discards a pending download after tab replacement or hiding its downloads panel",async()=>{
  const pending=deferred<Blob>();mocks.download.mockReturnValue(pending.promise);await render();await act(async()=>button("Save a copy").click());
  await render({downloads:false,tabId:"abcd1234"});await act(async()=>pending.resolve(new Blob(["late"])));expect(mocks.click).not.toHaveBeenCalled();
});
