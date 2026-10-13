import { describe, expect, it } from "vitest";
import { CaptureDrafts, type CaptureTarget } from "./captureDraft";
import { emptyChat, emptyChats } from "./chats";
import { initialState, reduce, type State } from "../state/store";
import type { Attachment } from "../types";
const pair: Attachment[] = [{ name:"page.png", mime:"image/png", data_b64:"picture" },
  { name:"page text", mime:"application/x-jarvis-page-capture", data_b64:"a".repeat(32) }];
const fresh = (): State => ({ ...initialState, chats: { ...emptyChats(),
  2: { ...emptyChat(), compose: { id:"c2", projectId:"p1" }, input:"keep words" } }, drafts:{} });
const stage = (s:State,t:CaptureTarget) => reduce(s,{type:"stage",key:t.key,capture:t.compose,files:pair,atomic:true});
describe("capture draft identity",()=>{
  it("follows a moved compose and its opened thread",()=>{
    const drafts=new CaptureDrafts();let s=fresh();const target=drafts.pin(s,2)!;
    s={...s,chats:{...s.chats,2:emptyChat(),3:{...s.chats[2],compose:{...s.chats[2].compose!,openedId:"opened"}}}};
    s=stage(s,target);expect(s.chats[3].files).toEqual(pair);expect(s.chats[3].input).toBe("keep words");
  });
  it("the actual open-thread reply pins the created thread even after the row closes",()=>{
    const drafts=new CaptureDrafts();let s=fresh();const target=drafts.pin(s,2)!;
    drafts.opened("c2","new-thread");
    s={...s,chats:{...s.chats,2:emptyChat()}};
    s=stage(s,target);expect(s.drafts["new-thread"]?.files).toEqual(pair);
    expect(s.drafts["compose:*"]).toBeUndefined();
  });
  it("a new compose in the reused pane cannot receive the old pair",()=>{
    const drafts=new CaptureDrafts();let s=fresh();const target=drafts.pin(s,2)!;
    s={...s,chats:{...s.chats,2:{...emptyChat(),compose:{id:"new-row",projectId:"p2"}}}};
    s=stage(s,target);expect(s.chats[2].files).toEqual([]);
    expect(s.drafts["compose:*"]).toMatchObject({files:pair,projects:["p1"]});
    expect(s.error).toContain("kept for the next new thread");
    s=reduce(s,{type:"chat",pane:4,patch:{compose:{projectId:"p1"},takeOrphan:true}});
    expect(s.chats[4].files).toEqual(pair);
  });
  it("a closed target parks atomically against the orphan's current capacity",()=>{
    const drafts=new CaptureDrafts();let s=fresh();const target=drafts.pin(s,2)!;
    s={...s,chats:{...s.chats,2:emptyChat()},drafts:{"compose:*":{text:"saved",files:Array(7).fill(pair[0])}}};
    const next=stage(s,target);expect(next.drafts["compose:*"].files).toHaveLength(7);
    expect(next.drafts["compose:*"].text).toBe("saved");expect(next.error).toContain("two attachment slots");
  });
  it("release stops later open replies from altering a completed target",()=>{
    const drafts=new CaptureDrafts(),target=drafts.pin(fresh(),2)!;
    drafts.release(target);drafts.opened("c2","late");expect(target.key).toBe("compose:2");
    expect(drafts.pin(fresh(),null)).toBeNull();
  });
});
