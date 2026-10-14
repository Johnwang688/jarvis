import {useEffect,useLayoutEffect,useRef,useState,type RefObject} from "react";
import {OwnerRequests,findShortcut,type OwnerCursor} from "../lib/remoteowner";
import type {PaneAction} from "../lib/remote";
import type {FindState,OwnerMenuAction} from "./RemoteOwnerTools";

interface Options {
  canInput:()=>boolean;send:(message:object,input?:boolean)=>boolean;
  onAction:(action:PaneAction)=>void;copy:()=>void;focus:()=>void;release:()=>void;
  surface:RefObject<HTMLDivElement>;enabled:boolean;identity:string;
}
/** Local owner gestures only. Call reset from socket/document/hold lifecycle. */
export function useRemoteOwnerTools(options:Options) {
  const latest=useRef(options);latest.current=options;
  const requests=useRef(new OwnerRequests());
  const [find,setFind]=useState(false),findOpen=useRef(false);
  const [query,setQuery]=useState(""),[status,setStatus]=useState<FindState>("idle");
  const [cursor,setCursor]=useState<OwnerCursor>("default");
  const [menu,setMenu]=useState<{x:number;y:number;bounds:{width:number;height:number}}|null>(null);
  const menuOpen=useRef(false),ignoreGesture=useRef(false);
  const cursorTimer=useRef<ReturnType<typeof setTimeout>|null>(null),findTimer=useRef<ReturnType<typeof setTimeout>|null>(null);
  const epoch=useRef(0);
  const cancelTimers=()=>{
    if(cursorTimer.current!==null)clearTimeout(cursorTimer.current);
    if(findTimer.current!==null)clearTimeout(findTimer.current);
    cursorTimer.current=findTimer.current=null;
  };
  const reset=()=>{
    epoch.current++;cancelTimers();requests.current.reset();setCursor("default");
    findOpen.current=false;setFind(false);setQuery("");setStatus("idle");menuOpen.current=false;setMenu(null);
  };
  useLayoutEffect(()=>{if(!options.enabled)reset();},[options.enabled]);
  useLayoutEffect(()=>{reset();},[options.identity]);
  useEffect(()=>{
    const finish=()=>{setTimeout(()=>{ignoreGesture.current=false;},0);};
    document.addEventListener("pointerup",finish,true);document.addEventListener("pointercancel",finish,true);
    return()=>{cancelTimers();document.removeEventListener("pointerup",finish,true);document.removeEventListener("pointercancel",finish,true);};
  },[]);
  const closeMenu=(restore=false)=>{
    menuOpen.current=false;setMenu(null);
    if(restore&&latest.current.canInput())latest.current.focus();
  };
  const clearCursor=()=>{
    if(cursorTimer.current!==null)clearTimeout(cursorTimer.current);cursorTimer.current=null;
    const message=requests.current.cancelCursor();setCursor("default");
    if(message&&latest.current.canInput())latest.current.send(message);
  };
  const openFind=()=>{
    if(!latest.current.canInput()){reset();return;}
    latest.current.release();clearCursor();closeMenu();
    findOpen.current=true;setFind(true);
  };
  const closeFind=()=>{
    const cancel=requests.current.cancelFind();if(cancel&&latest.current.canInput())latest.current.send(cancel);
    if(findTimer.current!==null)clearTimeout(findTimer.current);findTimer.current=null;
    findOpen.current=false;setFind(false);setQuery("");setStatus("idle");
    if(latest.current.canInput())latest.current.focus();
  };
  const edit=(value:string)=>{
    const cancel=requests.current.cancelFind();if(cancel&&latest.current.canInput())latest.current.send(cancel);
    if(findTimer.current!==null)clearTimeout(findTimer.current);findTimer.current=null;
    setQuery(value);setStatus("idle");
  };
  const run=(backward:boolean)=>{
    if(!latest.current.canInput()){reset();return;}
    if(!findOpen.current)return;
    const message=requests.current.find(query,backward);if(!message)return;
    if(!latest.current.send(message,true)){requests.current.cancelFind();setStatus("unavailable");return;}
    setStatus("waiting");const at=epoch.current;
    findTimer.current=setTimeout(()=>{
      findTimer.current=null;if(at!==epoch.current)return;
      const cancel=requests.current.cancelFind();if(cancel&&latest.current.canInput())latest.current.send(cancel);
      setStatus("unavailable");
    },2500);
  };
  const scheduleCursor=()=>{
    if(cursorTimer.current!==null)return;
    const at=epoch.current;
    cursorTimer.current=setTimeout(()=>{
      cursorTimer.current=null;
      if(at!==epoch.current||!latest.current.canInput()||menuOpen.current||findOpen.current)return;
      const message=requests.current.nextCursor(performance.now());
      if(message&&!latest.current.send(message))clearCursor();
    },100);
  };
  const move=(point:{x:number;y:number;inside:boolean})=>{
    if(!point.inside||!latest.current.canInput()||menuOpen.current||findOpen.current){clearCursor();return;}
    requests.current.move(point);scheduleCursor();
  };
  const receive=(message:unknown)=>{
    const value=message as {t?:unknown};
    if(value?.t==="owner_reset"){reset();return;}
    if(!latest.current.canInput()){reset();return;}
    if(value?.t==="find"){
      const result=requests.current.found(message);if(!result)return;
      if(findTimer.current!==null)clearTimeout(findTimer.current);findTimer.current=null;
      if(findOpen.current)setStatus(!result.available?"unavailable":result.matched?"matched":"missing");
    }
    if(value?.t==="cursor"){
      const keyword=requests.current.cursorResult(message);if(keyword)setCursor(keyword);
      scheduleCursor();
    }
  };
  const context=(event:React.MouseEvent)=>{
    event.preventDefault();event.stopPropagation();
    if(!latest.current.canInput()){reset();return;}
    if(findOpen.current)return;
    const surface=latest.current.surface.current,box=surface?.getBoundingClientRect();if(!surface||!box||!box.width||!box.height)return;
    latest.current.release();clearCursor();
    const scaleX=surface.clientWidth/box.width,scaleY=surface.clientHeight/box.height;
    menuOpen.current=true;setMenu({x:(event.clientX-box.left)*scaleX,y:(event.clientY-box.top)*scaleY,bounds:{width:surface.clientWidth,height:surface.clientHeight}});
  };
  useEffect(()=>{
    if(!menu)return;
    const outside=(event:PointerEvent)=>{
      if((event.target as Element|null)?.closest?.('[data-testid="remote-context-menu"]'))return;
      event.preventDefault();event.stopPropagation();ignoreGesture.current=true;closeMenu();
    };
    const dismiss=()=>closeMenu();
    const wheel=(event:WheelEvent)=>{if(!(event.target as Element|null)?.closest?.('[data-testid="remote-context-menu"]'))closeMenu();};
    document.addEventListener("pointerdown",outside,true);
    document.addEventListener("wheel",wheel,true);window.addEventListener("resize",dismiss);window.addEventListener("blur",dismiss);
    return()=>{document.removeEventListener("pointerdown",outside,true);
      document.removeEventListener("wheel",wheel,true);window.removeEventListener("resize",dismiss);window.removeEventListener("blur",dismiss);};
  },[menu]);
  const menuAction=(action:OwnerMenuAction)=>{
    if(!latest.current.canInput()){reset();return;}closeMenu();
    if(action==="find")openFind();
    else if(action==="copy")latest.current.copy();
    else latest.current.onAction(action);
  };
  return {find,query,status,cursor,menu,openFind,closeFind,edit,run,receive,reset,move,clearCursor,context,menuAction,
    closeMenu:()=>closeMenu(true),
    shortcut:(event:KeyboardEvent)=>findShortcut(event),
    consumesMouse:(event:{button:number;buttons:number})=>event.button===2||(event.buttons&2)!==0||menuOpen.current||ignoreGesture.current};
}
