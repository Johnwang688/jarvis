import {CURSORS,OwnerRequests,cursorKeyword,cursorReply,findReply,findShortcut,ownerMenuPosition,validFindQuery} from "./remoteowner";
describe("owner find/cursor bounds",()=>{
 it("bounds Unicode query, controls and malformed reply fields",()=>{
  expect(validFindQuery("😀".repeat(256))).toBe(true);
  for(const query of ["","x".repeat(257),"\n","\t","\u0000","\u0085","\u202e","\ud800"])expect(validFindQuery(query)).toBe(false);
  expect(findReply({t:"find",seq:1,available:true,matched:false})).not.toBeNull();
  for(const value of [{t:"find",seq:true,available:true,matched:false},{t:"find",seq:1,available:true,matched:false,text:"secret"},{t:"find",seq:1,available:1,matched:true}])expect(findReply(value)).toBeNull();
 });
 it("accepts only finite cursor keywords",()=>{
  for(const cursor of CURSORS){expect(cursorKeyword(cursor)).toBe(cursor);expect(cursorReply({t:"cursor",seq:1,cursor})).not.toBeNull();}
  for(const cursor of ["url(https://secret.test),pointer","none","auto","copy","inherit",{},null]){expect(cursorKeyword(cursor)).toBe("default");expect(cursorReply({t:"cursor",seq:1,cursor})).toBeNull();}
 });
 it("recognizes the exact local find chord only",()=>{
  const event={key:"f",ctrlKey:true,metaKey:false,altKey:false,shiftKey:false};
  expect(findShortcut(event)).toBe(true);expect(findShortcut({...event,shiftKey:true})).toBe(false);
  expect(findShortcut({...event,altKey:true})).toBe(false);expect(findShortcut({...event,ctrlKey:false})).toBe(false);
 });
 it("one outstanding find; edits, lifecycle and old results cannot revive it",()=>{
  const state=new OwnerRequests();const one=state.find("first",false)!;
  expect(state.find("second",false)).toBeNull();const cancel=state.cancelFind()!;
  expect(cancel.seq).toBeGreaterThan(one.seq);expect(state.found({t:"find",seq:one.seq,available:true,matched:true})).toBeNull();
  const two=state.find("second",true)!;expect(two.backward).toBe(true);
  expect(state.found({t:"find",seq:two.seq,available:true,matched:false})?.matched).toBe(false);
  const three=state.find("third",false)!;state.reset();expect(state.found({t:"find",seq:three.seq,available:true,matched:true})).toBeNull();
 });
 it("coalesces latest cursor point at10Hz with at most one pending request",()=>{
  const state=new OwnerRequests();state.move({x:1,y:2});const one=state.nextCursor(0)!;expect(one.x).toBe(1);
  for(let i=2;i<=100;i++)state.move({x:i,y:2});
  expect(state.nextCursor(100)).toBeNull();
  expect(state.cursorResult({t:"cursor",seq:one.seq,cursor:"pointer"})).toBeNull();
  expect(state.nextCursor(99)).toBeNull();const two=state.nextCursor(100)!;expect(two.x).toBe(100);
  expect(state.cursorResult({t:"cursor",seq:two.seq,cursor:"text"})).toBe("text");expect(state.nextCursor(200)).toBeNull();
 });
 it("leave/cancel/hold resets to default without accepting a late cursor",()=>{
  const state=new OwnerRequests();state.move({x:1,y:2});const one=state.nextCursor(0)!;
  expect(state.cancelCursor()!.seq).toBeGreaterThan(one.seq);
  expect(state.cursorResult({t:"cursor",seq:one.seq,cursor:"pointer"})).toBeNull();expect(state.cursor).toBe("default");
  state.move({x:NaN,y:2});expect(state.nextCursor(1000)).toBeNull();
  state.move({x:1,y:2});const two=state.nextCursor(1000)!;state.reset();
  expect(state.cursorResult({t:"cursor",seq:two.seq,cursor:"pointer"})).toBeNull();
 });
 it("clamps menu to normal, edge and tiny visible surface bounds",()=>{
  expect(ownerMenuPosition({x:295,y:195},{width:180,height:180},{width:300,height:200})).toEqual({x:120,y:20});
  expect(ownerMenuPosition({x:4,y:4},{width:180,height:180},{width:40,height:30})).toEqual({x:0,y:0});
  expect(ownerMenuPosition({x:-10,y:-5},{width:180,height:180},{width:300,height:200})).toEqual({x:0,y:0});
 });
});
