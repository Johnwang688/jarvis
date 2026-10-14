/** Finite, memory-only owner find/cursor state. No query enters persisted state. */
export const FIND_POINTS=256, FIND_BYTES=1024, OWNER_SEQUENCE_MAX=0x7fffffff;
export const CURSOR_INTERVAL_MS=100;
export const CURSORS=["default","pointer","text","crosshair","move","grab","grabbing","help","wait","progress","not-allowed","col-resize","row-resize","n-resize","ne-resize","e-resize","se-resize","s-resize","sw-resize","w-resize","nw-resize"] as const;
export type OwnerCursor=typeof CURSORS[number];
export function cursorKeyword(value:unknown):OwnerCursor {
  return typeof value==="string"&&(CURSORS as readonly string[]).includes(value)?value as OwnerCursor:"default";
}
export function validFindQuery(query:string):boolean {
  return [...query].length>0&&[...query].length<=FIND_POINTS&&new TextEncoder().encode(query).length<=FIND_BYTES&&!/[\p{C}\u2028\u2029]/u.test(query);
}
/** Call only in the Remote proxy textarea's own key handler. */
export function findShortcut(event:Pick<KeyboardEvent,"key"|"ctrlKey"|"metaKey"|"altKey"|"shiftKey">):boolean {
  return (event.ctrlKey||event.metaKey)&&!event.altKey&&!event.shiftKey&&event.key.toLowerCase()==="f";
}
type Point={x:number;y:number};
export interface FindReply {t:"find";seq:number;available:boolean;matched:boolean}
export interface CursorReply {t:"cursor";seq:number;cursor:OwnerCursor}
function object(value:unknown):Record<string,unknown>|null {
  return value!==null&&typeof value==="object"&&!Array.isArray(value)?value as Record<string,unknown>:null;
}
function sequence(value:unknown):value is number {return typeof value==="number"&&Number.isInteger(value)&&value>0&&value<=OWNER_SEQUENCE_MAX;}
export function findReply(value:unknown):FindReply|null {
  const v=object(value);return v&&Object.keys(v).length===4&&v.t==="find"&&sequence(v.seq)&&typeof v.available==="boolean"&&typeof v.matched==="boolean"?v as unknown as FindReply:null;
}
export function cursorReply(value:unknown):CursorReply|null {
  const v=object(value);return v&&Object.keys(v).length===3&&v.t==="cursor"&&sequence(v.seq)&&typeof v.cursor==="string"&&(CURSORS as readonly string[]).includes(v.cursor)?v as unknown as CursorReply:null;
}
export class OwnerRequests {
  private seq=0;
  private pendingFind:number|null=null;
  private pendingCursor:{seq:number;version:number}|null=null;
  private point:Point|null=null;
  private version=0;
  private sampled=0;
  private cursorAt=-Infinity;
  cursor:OwnerCursor="default";
  private nextSequence():number|null {return this.seq<OWNER_SEQUENCE_MAX?++this.seq:null;}
  find(query:string,backward:boolean) {
    if(this.pendingFind!==null||!validFindQuery(query))return null;
    const seq=this.nextSequence();if(seq===null)return null;
    this.pendingFind=seq;return{t:"find" as const,seq,query,backward};
  }
  cancelFind() {
    this.pendingFind=null;const seq=this.nextSequence();
    return seq===null?null:{t:"find_cancel" as const,seq};
  }
  found(value:unknown):FindReply|null {
    const result=findReply(value);if(!result||result.seq!==this.pendingFind)return null;
    this.pendingFind=null;return result;
  }
  move(point:Point) {
    if(!Number.isFinite(point.x)||!Number.isFinite(point.y)||point.x<0||point.y<0||point.x>4096||point.y>4096)return;
    this.point=point;this.version++;
  }
  nextCursor(now:number) {
    if(this.pendingCursor||!this.point||this.sampled===this.version||now-this.cursorAt<CURSOR_INTERVAL_MS)return null;
    const seq=this.nextSequence();if(seq===null)return null;
    this.pendingCursor={seq,version:this.version};this.sampled=this.version;this.cursorAt=now;
    return{t:"cursor" as const,seq,...this.point};
  }
  cursorResult(value:unknown):OwnerCursor|null {
    const result=cursorReply(value),pending=this.pendingCursor;
    if(!result||!pending||pending.seq!==result.seq)return null;
    this.pendingCursor=null;
    if(pending.version!==this.version)return null;
    this.cursor=result.cursor;return this.cursor;
  }
  cancelCursor() {
    this.pendingCursor=null;this.point=null;this.version++;this.cursor="default";
    const seq=this.nextSequence();return seq===null?null:{t:"cursor_cancel" as const,seq};
  }
  reset() {this.pendingFind=null;this.pendingCursor=null;this.point=null;this.version++;this.cursor="default";}
}
/** CSS-space placement inside the visible Remote surface, including tiny panes. */
export function ownerMenuPosition(point:Point,size:{width:number;height:number},bounds:{width:number;height:number}) {
  const width=Math.max(0,Number.isFinite(bounds.width)?bounds.width:0),height=Math.max(0,Number.isFinite(bounds.height)?bounds.height:0);
  return{x:Math.max(0,Math.min(point.x,width-Math.min(size.width,width))),y:Math.max(0,Math.min(point.y,height-Math.min(size.height,height)))};
}
