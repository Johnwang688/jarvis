import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { browserApi } from "../api";
import { FramePipe, FRAME_MAX_BYTES, HeldKeys, HeldButtons, routeKeyDown, routeMouse,
  pasteMessages, clientToPage, cdpWheel, viewportFor, backingSize, placeFrame, frameDest,
  cleanTitle, type FrameMeta, type CdpKeyEvent, type PaneAction } from "../lib/remote";
import { BrowserFiles } from "./BrowserFiles";

interface PageDialog {id:string;kind:string;message:string;default:string}
export interface RemoteViewProps {
  tabId:string; live:boolean; blocked:boolean; zoom:number; projectId:string|null;
  onLocation:(url:string,title:string)=>void; onPopup:(id:string)=>void;
  onAction:(action:PaneAction)=>void; onOwnerFocus:()=>void;
}
/** A stream owns one generation. Page messages never focus the HUD or write its clipboard. */
export function RemoteView(props:RemoteViewProps) {
  const latest=useRef(props);latest.current=props;
  const canvas=useRef<HTMLCanvasElement>(null);
  const surface=useRef<HTMLDivElement>(null);
  const input=useRef<HTMLTextAreaElement>(null);
  const socket=useRef<WebSocket|null>(null);
  const generation=useRef(0);
  const inputEpoch=useRef(0);
  const keys=useRef(new HeldKeys());const buttons=useRef(new HeldButtons());
  const geometry=useRef({css:{width:1,height:1},view:{width:1,height:1},place:{x:0,y:0,scale:1}});
  const clipboard=useRef<{gen:number;cut:CdpKeyEvent|null;cancelled:boolean;received?:boolean}|null>(null);
  const composing=useRef(false);
  const [retry,setRetry]=useState(0);
  const [connected,setConnected]=useState(false);
  const [error,setError]=useState("");
  const [security,setSecurity]=useState("unknown");
  const [dialogs,setDialogs]=useState<PageDialog[]>([]);
  const [chooser,setChooser]=useState<{id:string;multiple:boolean}|null>(null);
  const [downloads,setDownloads]=useState(false);
  const canInput=()=>latest.current.live&&!latest.current.blocked&&socket.current?.readyState===WebSocket.OPEN;
  const send=(body:object,inputMessage=false)=>{
    const ws=socket.current;
    if(!ws||ws.readyState!==WebSocket.OPEN||(inputMessage&&!canInput()))return false;
    try {ws.send(JSON.stringify(body));return true;}catch{return false;}
  };
  const release=()=>{
    const held=!canInput();
    for(const event of keys.current.releaseAll(held))send({t:"key",e:event},true);
    for(const event of buttons.current.releaseAll(held))send({t:"mouse",e:event},true);
  };
  const resize=()=>{
    const box=surface.current;
    if(!box||!latest.current.live||box.clientWidth<=0||box.clientHeight<=0)return;
    const css={width:box.clientWidth,height:box.clientHeight};
    const viewport=viewportFor(css,latest.current.zoom,devicePixelRatio);
    geometry.current.css=css;
    geometry.current.place=placeFrame(css,geometry.current.view);
    send({t:"resize",...viewport});
  };
  useLayoutEffect(()=>{
    if(props.blocked)inputEpoch.current++;
    if(props.blocked && clipboard.current)clipboard.current.cancelled=true;
    if(!props.blocked)release();
    // A composition/paste interrupted by approval is never committed later.
    if(props.blocked)composing.current=false;
  },[props.blocked]);
  useEffect(()=>{
    if(!props.live)return;
    const gen=++generation.current;let gone=false;let ws:WebSocket|null=null;
    const controller=new AbortController();
    setConnected(false);setError("");setDialogs([]);setChooser(null);setSecurity("unknown");
    keys.current=new HeldKeys();buttons.current=new HeldButtons();clipboard.current=null;
    const current=()=>!gone&&generation.current===gen&&latest.current.tabId===props.tabId;
    const pipe=new FramePipe<ImageBitmap>(
      bytes=>createImageBitmap(new Blob([bytes.slice().buffer],{type:"image/jpeg"})),
      (bitmap,meta:FrameMeta)=>{
        if(!current()||!latest.current.live||!canvas.current)return;
        const {css}=geometry.current;const size=backingSize(css,latest.current.zoom,devicePixelRatio);
        const target=canvas.current;
        if(target.width!==size.width)target.width=size.width;
        if(target.height!==size.height)target.height=size.height;
        const place=placeFrame(css,meta);geometry.current={css,view:meta,place};
        const d=frameDest(place,meta,latest.current.zoom,devicePixelRatio);
        const ctx=target.getContext("2d");if(!ctx)return;
        ctx.clearRect(0,0,target.width,target.height);ctx.drawImage(bitmap,d.x,d.y,d.width,d.height);
        target.dataset.frame="ready";
      },(_,id)=>{if(current())send({t:"ack",id});},bitmap=>bitmap.close());
    const pipeGen=pipe.next();
    const connect=async()=>{
      try {
        const row=await browserApi.write<{ticket:string}>(`/tabs/${props.tabId}/ticket`,{},"POST",controller.signal);
        if(!current())return;
        const url=new URL(`/browser/tabs/${props.tabId}/attach`,window.location.href);
        url.protocol=url.protocol==="https:"?"wss:":"ws:";url.searchParams.set("ticket",row.ticket);
        ws=new WebSocket(url);ws.binaryType="arraybuffer";socket.current=ws;
        ws.onopen=()=>{if(!current())return;setConnected(true);send({t:"visible",visible:true});resize();};
        ws.onmessage=async(event)=>{
          if(!current())return;
          if(event.data instanceof ArrayBuffer) {
            if(event.data.byteLength<9||event.data.byteLength>FRAME_MAX_BYTES+8){setError("The Browser sent an invalid frame.");return;}
            const header=new DataView(event.data);const width=header.getUint16(4),height=header.getUint16(6);
            if(!width||!height||width>4096||height>4096){setError("The Browser sent an invalid frame size.");return;}
            pipe.frame(pipeGen,{id:header.getUint32(0),meta:{width,height},data:new Uint8Array(event.data,8)});return;
          }
          if(typeof event.data!=="string"||event.data.length>65536)return;
          let msg:any;try{msg=JSON.parse(event.data);}catch{return;}
          if(!msg||typeof msg.t!=="string")return;
          if(msg.t==="location"&&typeof msg.url==="string"&&msg.url.length<=2048){
            latest.current.onLocation(msg.url,cleanTitle(msg.title));setSecurity(["secure","insecure","broken"].includes(msg.security)?msg.security:"unknown");
          } else if(msg.t==="dialog"&&/^[0-9a-f]{8}$/.test(msg.id)) {
            const d={id:msg.id,kind:["alert","confirm","prompt","beforeunload"].includes(msg.kind)?msg.kind:"alert",message:cleanTitle(msg.message,4000),default:cleanTitle(msg.default,4000)};
            setDialogs(old=>[...old.filter(x=>x.id!==d.id),d].slice(-8));
          } else if(msg.t==="dialog_closed")setDialogs(old=>old.filter(d=>d.id!==msg.id));
          else if(msg.t==="filechooser"&&/^[0-9a-f]{8}$/.test(msg.id))setChooser({id:msg.id,multiple:msg.multiple===true});
          else if(msg.t==="filechooser_closed")setChooser(old=>old?.id===msg.id?null:old);
          else if(msg.t==="popup"&&typeof msg.tab_id==="string")latest.current.onPopup(msg.tab_id);
          else if(msg.t==="selection"){
            const request=clipboard.current;
            if(!request||request.received)return;
            if(request.cancelled||request.gen!==gen||!canInput()||typeof msg.text!=="string"){clipboard.current=null;return;}
            request.received=true;
            try {
              await navigator.clipboard.writeText(msg.text);
              if(current()&&canInput()&&!request.cancelled&&request.cut){
                send({t:"key",e:request.cut},true);
                send({t:"key",e:{...request.cut,type:"keyUp",text:undefined,unmodifiedText:undefined}},true);
              }
            }catch{if(current())setError("Clipboard access was denied. Nothing was cut.");}
            finally{if(clipboard.current===request)clipboard.current=null;}
          } else if(msg.t==="ping")send({t:"pong"});
          else if(["error","input_dropped","popup_refused","closed","held","taken"].includes(msg.t))setError(cleanTitle(msg.reason??msg.error??"The Browser is unavailable.",300));
        };
        ws.onclose=()=>{if(current()){socket.current=null;setConnected(false);setError("The Browser disconnected. Choose Retry to reconnect.");if(clipboard.current)clipboard.current.cancelled=true;}};
        ws.onerror=()=>{if(current())setError("The Browser connection failed. Choose Retry.");};
      }catch(e){if(current())setError(cleanTitle(e instanceof Error?e.message:"The Browser could not connect.",300));}
    };
    void connect();
    const observer=new ResizeObserver(resize);if(surface.current)observer.observe(surface.current);
    const blur=()=>{if(clipboard.current)clipboard.current.cancelled=true;release();};
    window.addEventListener("blur",blur);
    return()=>{
      gone=true;generation.current++;controller.abort();pipe.clear();observer.disconnect();window.removeEventListener("blur",blur);
      if(clipboard.current)clipboard.current.cancelled=true;
      if(socket.current===ws)socket.current=null;
      if(ws){ws.onmessage=null;ws.onopen=null;ws.onclose=null;ws.onerror=null;ws.close();}
      keys.current=new HeldKeys();buttons.current=new HeldButtons();composing.current=false;
    };
  },[props.tabId,props.live,retry]);
  useLayoutEffect(()=>resize(),[props.zoom]);
  const point=(event:{clientX:number;clientY:number})=>clientToPage({x:event.clientX,y:event.clientY},canvas.current?.getBoundingClientRect()??{left:0,top:0},latest.current.zoom,geometry.current.place,geometry.current.view);
  const mouse=(kind:"down"|"up"|"move",event:React.MouseEvent)=>{
    if(kind==="up"&&!canInput())return;
    const route=routeMouse(kind,event,point(event),!canInput());
    if(route.kind==="forward"){
      if(kind==="down"){input.current?.focus({preventScroll:true});latest.current.onOwnerFocus();}
      event.preventDefault();if(send({t:"mouse",e:route.event},true))buttons.current.sent(route.event);
    }
  };
  const paste=async(text:string)=>{
    const gen=generation.current;const inputGen=inputEpoch.current;
    for(const msg of pasteMessages(text)) {
      if(gen!==generation.current||inputGen!==inputEpoch.current||!canInput())break;
      send(msg,true);await new Promise<void>(resolve=>setTimeout(resolve,0));
    }
  };
  return <div className="remoteview" data-testid="remote-view">
    <div className="remotestatus small muted" role="status">
      <span>{connected?`Remote · ${security==="secure"?"Encrypted connection":security==="unknown"?"Connection security unknown":"Connection not secure"}`:"Remote · Connecting"}</span>
      <button type="button" disabled={!connected||props.blocked} onClick={()=>setDownloads(v=>!v)}>Downloads</button>
    </div>
    {error?<div className="pad err" data-testid="remote-error">{error} {!connected?<button disabled={props.blocked} onClick={()=>setRetry(v=>v+1)}>Retry</button>:null}</div>:null}
    {dialogs.map(dialog=><PageQuestion key={dialog.id} dialog={dialog} blocked={props.blocked||!connected}
      answer={(accept,text)=>{if(canInput())send({t:"dialog",id:dialog.id,accept,...(dialog.kind==="prompt"?{text}: {})},true);}} />)}
    {chooser||downloads?<BrowserFiles tabId={props.tabId} projectId={props.projectId} chooser={chooser} downloads={downloads}
      blocked={props.blocked||!connected} generation={generation.current} onCloseDownloads={()=>setDownloads(false)} />:null}
    <div className="remotesurface" ref={surface} data-testid="remote-surface"
      onMouseDown={e=>mouse("down",e)} onMouseMove={e=>mouse("move",e)} onMouseUp={e=>mouse("up",e)}
      onPointerDown={e=>{if(canInput())e.currentTarget.setPointerCapture(e.pointerId);}}
      onPointerCancel={()=>release()} onLostPointerCapture={()=>release()}
      onContextMenu={e=>e.preventDefault()}
      onWheel={e=>{if(!canInput())return;const at=point(e);if(!at.inside)return;const event=cdpWheel(e,at,props.zoom,geometry.current.view.height);if(event){e.preventDefault();send({t:"mouse",e:event},true);}}}>
      <canvas ref={canvas} role="img" aria-label="Remote page image" />
      <textarea ref={input} className="remoteinput" data-remote-input data-testid="remote-input" aria-label="Type into the Remote page" spellCheck={false} autoComplete="off"
        disabled={!connected||props.blocked} onBlur={()=>{release();send({t:"blur"},true);}}
        onFocus={()=>{if(canInput())send({t:"focus"},true);}}
        onKeyDown={e=>{
          const route=routeKeyDown(e.nativeEvent,!canInput());
          if(route.kind==="hud")return;
          e.stopPropagation();if(route.kind==="paste")return;
          if(route.kind!=="drop")e.preventDefault();
          if(route.kind==="pane"){release();latest.current.onAction(route.action);}
          if(route.kind==="forward"){
            if(route.clip){
              if(clipboard.current)return;
              clipboard.current={gen:generation.current,cut:route.clip==="cut"?route.event:null,cancelled:false};
              if(route.clip==="copy"&&send({t:"key",e:route.event},true))keys.current.pressed(e.nativeEvent,route.event);
              send({t:"selection"},true);
            }else if(send({t:"key",e:route.event},true))keys.current.pressed(e.nativeEvent,route.event);
          }
        }}
        onKeyUp={e=>{const event=keys.current.released(e.nativeEvent,!canInput());if(event){e.preventDefault();send({t:"key",e:event},true);}}}
        onPaste={e=>{e.preventDefault();e.stopPropagation();if(canInput())void paste(e.clipboardData.getData("text/plain"));}}
        onCompositionStart={()=>{composing.current=!!canInput();}}
        onCompositionEnd={e=>{if(composing.current&&canInput())void paste(e.data);composing.current=false;e.currentTarget.value="";}}
        onInput={e=>{if(!composing.current)e.currentTarget.value="";}} />
    </div>
  </div>;
}
function PageQuestion({dialog,blocked,answer}:{dialog:PageDialog;blocked:boolean;answer:(accept:boolean,text:string)=>void}) {
  const [text,setText]=useState(dialog.default);
  return <div className="browserquestion pad" data-testid="browser-dialog" role="group" aria-label="This page says">
    <strong>This page says</strong><p>{dialog.message}</p>
    {dialog.kind==="prompt"?<input aria-label="Answer this page" value={text} maxLength={4000} disabled={blocked} onChange={e=>setText(e.target.value)} onKeyDown={e=>e.stopPropagation()} />:null}
    <button disabled={blocked} onClick={()=>answer(true,text)}>OK</button>
    {dialog.kind!=="alert"?<button disabled={blocked} onClick={()=>answer(false,"")}>Cancel</button>:null}
  </div>;
}
