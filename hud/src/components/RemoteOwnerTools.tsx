import {useLayoutEffect,useRef,useState} from "react";
import {ownerMenuPosition,validFindQuery} from "../lib/remoteowner";
import type {PaneAction} from "../lib/remote";
import "./RemoteOwnerTools.css";

export type FindState="idle"|"waiting"|"matched"|"missing"|"unavailable";
export function RemoteFind({query,status,blocked,onEdit,onFind,onClose}:{query:string;status:FindState;blocked:boolean;onEdit:(query:string)=>void;onFind:(backward:boolean)=>void;onClose:()=>void}) {
  const input=useRef<HTMLInputElement>(null);
  // This component is mounted only by an explicit owner Find gesture.
  useLayoutEffect(()=>{input.current?.focus({preventScroll:true});input.current?.select();},[]);
  const run=(backward:boolean)=>{if(!blocked&&status!=="waiting"&&validFindQuery(query))onFind(backward);};
  return <div className="remotefind" data-testid="remote-find" role="search" aria-label="Find in Remote page">
    <input ref={input} aria-label="Find text in top document" data-testid="remote-find-query" value={query} maxLength={1024} disabled={blocked}
      spellCheck={false} autoComplete="off" onChange={event=>onEdit(event.target.value)}
      onKeyDown={event=>{
        if(event.key==="Escape"&&document.getElementById("authveil"))return;
        event.stopPropagation();
        if(event.key==="Enter"){event.preventDefault();run(event.shiftKey);}
        if(event.key==="Escape"){event.preventDefault();onClose();}
      }} />
    <button type="button" disabled={blocked||status==="waiting"||!validFindQuery(query)} onClick={()=>run(true)}>Previous</button>
    <button type="button" disabled={blocked||status==="waiting"||!validFindQuery(query)} onClick={()=>run(false)}>Next</button>
    <button type="button" disabled={blocked} onClick={()=>{if(!blocked)onClose();}}>Close</button>
    <span className="small muted" role="status">{query&&!validFindQuery(query)?"Use up to 256 characters without controls.":status==="waiting"?"Finding…":status==="missing"?"No match in top document.":status==="unavailable"?"Find is unavailable on this page.":status==="matched"?"Match selected in top document.":"Top document only · native page selection"}</span>
  </div>;
}
export type OwnerMenuAction=Extract<PaneAction,"back"|"forward"|"reload">|"find"|"copy";
const MENU:[OwnerMenuAction,string][]=[["back","Back"],["forward","Forward"],["reload","Reload"],["find","Find"],["copy","Copy selection"]];
export function RemoteContextMenu({point,bounds,blocked,onAction,onClose}:{point:{x:number;y:number};bounds:{width:number;height:number};blocked:boolean;onAction:(action:OwnerMenuAction)=>void;onClose:()=>void}) {
  const menu=useRef<HTMLDivElement>(null),[size,setSize]=useState({width:180,height:180});
  const at=ownerMenuPosition(point,size,bounds);
  useLayoutEffect(()=>{const node=menu.current;if(!node)return;setSize({width:node.offsetWidth,height:node.offsetHeight});node.querySelector<HTMLElement>("button:not(:disabled)")?.focus({preventScroll:true});},[]);
  return <div ref={menu} role="menu" aria-label="Remote page actions" data-testid="remote-context-menu" className="remotecontext"
    style={{left:at.x,top:at.y,maxWidth:bounds.width,maxHeight:bounds.height}}
    onContextMenu={event=>{event.preventDefault();event.stopPropagation();}}
    onMouseDown={event=>event.stopPropagation()} onMouseUp={event=>event.stopPropagation()}
    onMouseMove={event=>event.stopPropagation()} onWheel={event=>event.stopPropagation()} onPointerDown={event=>event.stopPropagation()}
    onKeyDown={event=>{
      if(event.key==="Escape"&&document.getElementById("authveil"))return;
      event.stopPropagation();
      if(event.key==="Escape"){event.preventDefault();onClose();}
      if(event.key==="ArrowDown"||event.key==="ArrowUp"){
        event.preventDefault();const buttons=Array.from(menu.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)")??[]);
        if(buttons.length){const index=buttons.indexOf(document.activeElement as HTMLButtonElement);buttons[(index+(event.key==="ArrowDown"?1:-1)+buttons.length)%buttons.length].focus();}
      }
    }}>
    {MENU.map(([action,label])=><button key={action} type="button" role="menuitem" disabled={blocked} onClick={event=>{event.stopPropagation();if(!blocked)onAction(action);}}>{label}</button>)}
  </div>;
}
