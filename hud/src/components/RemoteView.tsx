import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { browserApi } from "../api";
import { FramePipe, FRAME_MAX_BYTES, HeldKeys, HeldButtons, routeKeyDown, routeMouse,
  pasteMessages, clientToPage, cdpWheel, viewportFor, placeFrame, decodeFrame, paintFrame,
  cleanTitle, pageToCanvas, type FrameMeta, type CdpKeyEvent, type PaneAction } from "../lib/remote";
import { RemotePicker } from "./RemotePicker";
import { parsePicker, parsePickerClosed, PickerGesture, initialHighlight, moveHighlight, edgeHighlight,
  typeahead, pickerKey, validPickerValue, PICKER_PAGE, PICKER_TYPEAHEAD_MS, type Picker } from "../lib/remotepicker";
import { RemoteFind, RemoteContextMenu } from "./RemoteOwnerTools";
import { useRemoteOwnerTools } from "./useRemoteOwnerTools";
import { ownerReset } from "../lib/remoteowner";
import { BrowserFiles } from "./BrowserFiles";
import { BrowserAuth } from "./BrowserAuth";
import { parseBrowserAuth, type BrowserAuth as AuthChallenge } from "../lib/browserauth";
import { DriveStatus } from "./DriveStatus";
import { driving, useDriving } from "../state/driving";
import type { DriveLease } from "../lib/drivestate";

interface PageDialog {id:string;kind:string;message:string;default:string}
export interface RemoteViewProps {
  tabId:string; instanceGeneration?:number; live:boolean; blocked:boolean; zoom:number; projectId:string|null;
  inputHeld?:()=>boolean;
  onLocation:(url:string,title:string)=>void; onPopup:(id:string)=>void;
  onAction:(action:PaneAction)=>void; onOwnerFocus:()=>void; onRetry?:()=>Promise<unknown>;
  slotOf?:(threadId:string)=>number|null;
}
/** A stream owns one generation. Page messages never focus the HUD or write its clipboard. */
export function RemoteView(props:RemoteViewProps) {
  const latest=useRef(props);latest.current=props;
  const canvas=useRef<HTMLCanvasElement>(null);
  const surface=useRef<HTMLDivElement>(null);
  const input=useRef<HTMLTextAreaElement>(null);
  const socket=useRef<WebSocket|null>(null);
  const generation=useRef(0);
  const connectedInstance=useRef<number|undefined>(undefined);
  const driveLease=useRef<DriveLease|null>(null);
  const drive=useDriving(props.tabId);
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
  const [auth,setAuth]=useState<AuthChallenge|null>(null);
  const authCurrent=useRef<AuthChallenge|null>(null);authCurrent.current=auth;
  // B-PICKERS: a native picker the screencast never draws, offered by the
  // daemon only after this window's own left press opened it.
  const [picker,setPicker]=useState<Picker|null>(null);
  const pickerRef=useRef<Picker|null>(null);
  const [pickHighlight,setPickHighlight]=useState(-1);
  const [pickValue,setPickValue]=useState("");
  const pickGesture=useRef(new PickerGesture());
  const pickDismiss=useRef(false),pickMenuSuppressed=useRef(false);
  const pickTyped=useRef({text:"",at:0});
  const pickApplied=useRef<string|null>(null);
  const canInput=()=>!authCurrent.current&&latest.current.live&&!latest.current.blocked&&!latest.current.inputHeld?.()&&connectedInstance.current===latest.current.instanceGeneration&&socket.current?.readyState===WebSocket.OPEN;
  const send=(body:object,inputMessage=false)=>{
    const ws=socket.current;
    if(!ws||ws.readyState!==WebSocket.OPEN||(inputMessage&&!canInput()))return false;
    try {
      ws.send(JSON.stringify(body));
      const lease=driveLease.current;
      if(inputMessage&&lease?.tabId===latest.current.tabId)driving.owner(lease,body as {t:string},Date.now());
      return true;
    }catch{return false;}
  };
  const answerAuth=(id:string,body:object)=>{
    if(!latest.current.live||latest.current.blocked||latest.current.inputHeld?.()
      ||connectedInstance.current!==latest.current.instanceGeneration||authCurrent.current?.id!==id)return false;
    if(!send(body))return false;
    const lease=driveLease.current;
    if(lease?.tabId===latest.current.tabId)driving.owner(lease,{t:"auth"},Date.now());
    return true;
  };
  /** Forget the open picker; `cancel` tells the daemon (no page IO), `restore`
   * returns the keyboard to the proxy after the owner's own overlay gesture. */
  const closePicker=(cancel:boolean,restore=false)=>{
    const open=pickerRef.current;
    if(!open)return;
    pickerRef.current=null;setPicker(null);setPickHighlight(-1);pickTyped.current={text:"",at:0};
    if(cancel)send({t:"pick_cancel",id:open.id});
    if(restore&&canInput())input.current?.focus({preventScroll:true});
  };
  const pickChoice=(choice:{index:number}|{value:string})=>{
    const open=pickerRef.current;
    if(!open||!canInput())return;
    if(send({t:"pick",id:open.id,...choice},true))pickApplied.current=open.id;
    closePicker(false,true);
  };
  const pickerKeyDown=(event:KeyboardEvent)=>{
    const open=pickerRef.current;if(!open)return;
    const key=pickerKey(event);
    if(key==="escape"){closePicker(true);return;}
    if(open.kind!=="select"){
      if(key==="enter"&&validPickerValue(open.kind,pickValue))pickChoice({value:pickValue});
      return;
    }
    let at=pickHighlight;
    if(key==="enter"){const item=open.items[at];if(item&&!item.disabled)pickChoice({index:item.i});return;}
    if(key==="up")at=moveHighlight(open.items,at,-1);
    else if(key==="down")at=moveHighlight(open.items,at,1);
    else if(key==="pageup")at=moveHighlight(open.items,at,-PICKER_PAGE);
    else if(key==="pagedown")at=moveHighlight(open.items,at,PICKER_PAGE);
    else if(key==="home"||key==="end")at=edgeHighlight(open.items,key==="end");
    else if(key==="char"){
      const now=performance.now(),typed=pickTyped.current;
      const text=(now-typed.at<=PICKER_TYPEAHEAD_MS?typed.text:"")+event.key;
      pickTyped.current={text,at:now};
      const found=typeahead(open.items,at,text);
      if(found>=0)at=found;
    }
    if(at>=0)setPickHighlight(at);
  };
  const release=()=>{
    const held=!canInput();
    for(const event of keys.current.releaseAll(held))send({t:"key",e:event},true);
    for(const event of buttons.current.releaseAll(held))send({t:"mouse",e:event},true);
  };
  const owner=useRemoteOwnerTools({
    canInput:()=>canInput()&&!dialogs.length&&!chooser&&!downloads,
    send,onAction:action=>latest.current.onAction(action),release,surface,
    copy:()=>{
      if(!canInput()||clipboard.current)return;
      clipboard.current={gen:generation.current,cut:null,cancelled:false};
      send({t:"selection"},true);
    },
    focus:()=>{input.current?.focus({preventScroll:true});latest.current.onOwnerFocus();},
    enabled:connected&&props.live&&!props.blocked&&!props.inputHeld?.()&&!auth&&!dialogs.length&&!chooser&&!downloads,
    identity:`${props.tabId}:${props.instanceGeneration}:${generation.current}`,
  });
  const resize=()=>{
    const box=surface.current;
    if(!box||!latest.current.live||box.clientWidth<=0||box.clientHeight<=0)return;
    const css={width:box.clientWidth,height:box.clientHeight};
    if(css.width!==geometry.current.css.width||css.height!==geometry.current.css.height)owner.clearCursor();
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
    const lease=driving.attach(props.tabId);driveLease.current=lease;
    const controller=new AbortController();
    owner.reset();setConnected(false);setError("");setDialogs([]);setAuth(null);authCurrent.current=null;setChooser(null);setSecurity("unknown");
    pickerRef.current=null;setPicker(null);pickGesture.current.clear();pickApplied.current=null;pickDismiss.current=false;
    keys.current=new HeldKeys();buttons.current=new HeldButtons();clipboard.current=null;composing.current=false;inputEpoch.current++;
    const current=()=>!gone&&generation.current===gen&&latest.current.tabId===props.tabId&&latest.current.instanceGeneration===props.instanceGeneration;
    const pipe=new FramePipe<ImageBitmap>(decodeFrame,
      (bitmap,meta:FrameMeta)=>{
        if(!current()||!latest.current.live||!canvas.current)return;
        const {css}=geometry.current;const target=canvas.current;
        const painted=paintFrame(target,bitmap,meta,css,latest.current.zoom,devicePixelRatio);
        geometry.current={css,view:meta,place:painted.place};
        if(painted.drawn)target.dataset.frame="ready";
      },(_,id)=>{if(current())send({t:"ack",id});},bitmap=>bitmap.close());
    const pipeGen=pipe.next();
    const connect=async()=>{
      try {
        const row=await browserApi.write<{ticket:string}>(`/tabs/${props.tabId}/ticket`,{},"POST",controller.signal);
        if(!current())return;
        const url=new URL(`/browser/tabs/${props.tabId}/attach`,window.location.href);
        url.protocol=url.protocol==="https:"?"wss:":"ws:";url.searchParams.set("ticket",row.ticket);
        ws=new WebSocket(url);ws.binaryType="arraybuffer";socket.current=ws;
        ws.onopen=()=>{if(!current())return;connectedInstance.current=props.instanceGeneration;if(lease)driving.connected(lease);setConnected(true);send({t:"visible",visible:true});resize();};
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
          if(msg.t==="picker"){
            const offered=parsePicker(msg);
            if(!offered)return;
            const asked=pickGesture.current.accept(gen,performance.now());
            if(!asked||!canInput()){send({t:"pick_cancel",id:offered.id});return;}
            owner.reset();
            pickerRef.current=offered;setPicker(offered);pickTyped.current={text:"",at:0};
            setPickHighlight(offered.kind==="select"?initialHighlight(offered):-1);
            setPickValue(offered.kind==="select"?"":offered.value);
            return;
          }
          if(msg.t==="picker_closed"){
            const closed=parsePickerClosed(msg);if(!closed)return;
            if(pickerRef.current?.id===closed.id){pickerRef.current=null;setPicker(null);}
            if(pickApplied.current===closed.id){
              pickApplied.current=null;
              if(closed.reason!=="applied")setError(closed.reason==="refused"?"The page's control did not take that choice; nothing was set.":"The page changed before your choice reached it; nothing was set.");
            }
            return;
          }
          if(msg.t==="find"||msg.t==="cursor"||msg.t==="owner_reset"){
            owner.receive(msg);
            if(ownerReset(msg)){
              pickerRef.current=null;setPicker(null);
              inputEpoch.current++;composing.current=false;
              keys.current=new HeldKeys();buttons.current=new HeldButtons();
              if(input.current)input.current.value="";
              if(clipboard.current)clipboard.current.cancelled=true;
            }
          }
          else if(msg.t==="agent"){if(lease)driving.receive(lease,msg,Date.now());}
          else if(msg.t==="auth"||msg.t==="auth_closed"){
            const frame=parseBrowserAuth(msg);if(!frame)return;
            if(frame.kind==="prompt"){
              owner.reset();pickerRef.current=null;setPicker(null);
              authCurrent.current=frame.auth;setAuth(frame.auth);inputEpoch.current++;
              if(clipboard.current)clipboard.current.cancelled=true;
              composing.current=false;keys.current=new HeldKeys();buttons.current=new HeldButtons();
              if(input.current)input.current.value="";
            }else{
              setAuth(old=>old?.id===frame.id?null:old);
              if(authCurrent.current?.id===frame.id)authCurrent.current=null;
              if(frame.reason!=="answered")setError(frame.reason==="expired"?"Authentication expired. Reload to retry.":"Authentication ended. Reload to retry.");
            }
          }
          else if(msg.t==="location"&&typeof msg.url==="string"&&msg.url.length<=2048){
            latest.current.onLocation(msg.url,cleanTitle(msg.title));setSecurity(["secure","insecure","broken"].includes(msg.security)?msg.security:"unknown");
          } else if(msg.t==="dialog"&&/^[0-9a-f]{8}$/.test(msg.id)) {
            const d={id:msg.id,kind:["alert","confirm","prompt","beforeunload"].includes(msg.kind)?msg.kind:"alert",message:cleanTitle(msg.message,4000),default:cleanTitle(msg.default,4000)};
            owner.reset();setDialogs(old=>[...old.filter(x=>x.id!==d.id),d].slice(-8));
          } else if(msg.t==="dialog_closed")setDialogs(old=>old.filter(d=>d.id!==msg.id));
          else if(msg.t==="filechooser"&&/^[0-9a-f]{8}$/.test(msg.id)){owner.reset();setChooser({id:msg.id,multiple:msg.multiple===true});}
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
          else if(["error","input_dropped","popup_refused","closed","held","taken"].includes(msg.t)){owner.reset();pickerRef.current=null;setPicker(null);setError(cleanTitle(msg.reason??msg.error??"The Browser is unavailable.",300));}
        };
        ws.onclose=()=>{if(current()){owner.reset();pickerRef.current=null;setPicker(null);if(lease)driving.detach(lease);socket.current=null;setConnected(false);setAuth(null);authCurrent.current=null;setError("The Browser disconnected. Choose Retry to reconnect.");if(clipboard.current)clipboard.current.cancelled=true;}};
        ws.onerror=()=>{if(current())setError("The Browser connection failed. Choose Retry.");};
      }catch(e){if(current())setError(cleanTitle(e instanceof Error?e.message:"The Browser could not connect.",300));}
    };
    void connect();
    const observer=new ResizeObserver(resize);if(surface.current)observer.observe(surface.current);
    const blur=()=>{owner.reset();if(clipboard.current)clipboard.current.cancelled=true;release();};
    window.addEventListener("blur",blur);
    return()=>{
      owner.reset();pickerRef.current=null;pickGesture.current.clear();gone=true;generation.current++;authCurrent.current=null;if(lease)driving.detach(lease);if(driveLease.current===lease)driveLease.current=null;controller.abort();pipe.clear();observer.disconnect();window.removeEventListener("blur",blur);
      if(clipboard.current)clipboard.current.cancelled=true;
      if(socket.current===ws)socket.current=null;
      if(ws){ws.onmessage=null;ws.onopen=null;ws.onclose=null;ws.onerror=null;ws.close();}
      keys.current=new HeldKeys();buttons.current=new HeldButtons();composing.current=false;
    };
  },[props.tabId,props.live,props.instanceGeneration,retry]);
  useLayoutEffect(()=>resize(),[props.zoom]);
  // Never under a card, an auth prompt, a dialog, a chooser or Downloads, and
  // never in a pane that is not live: any of them closes the picker at once.
  const pickerLive=connected&&props.live&&!props.blocked&&!props.inputHeld?.()&&!auth&&!dialogs.length&&!chooser&&!downloads;
  useLayoutEffect(()=>{if(!pickerLive){pickGesture.current.clear();closePicker(true);}},[pickerLive]);
  const point=(event:{clientX:number;clientY:number})=>clientToPage({x:event.clientX,y:event.clientY},canvas.current?.getBoundingClientRect()??{left:0,top:0},latest.current.zoom,geometry.current.place,geometry.current.view);
  const mouse=(kind:"down"|"up"|"move",event:React.MouseEvent)=>{
    // A press on the page while a picker is open closes it, and the whole
    // gesture is consumed (as the context menu's outside press is).
    if(kind==="down"&&pickerRef.current){event.preventDefault();pickDismiss.current=true;pickMenuSuppressed.current=true;closePicker(true);return;}
    if(pickDismiss.current){event.preventDefault();if(kind==="up")pickDismiss.current=false;return;}
    if(kind==="down")pickMenuSuppressed.current=false;
    if(owner.consumesMouse(event)){event.preventDefault();return;}
    if(kind==="move")owner.move(point(event));
    if(kind==="up"&&!canInput())return;
    const route=routeMouse(kind,event,point(event),!canInput());
    if(route.kind==="forward"){
      if(kind==="down"){input.current?.focus({preventScroll:true});latest.current.onOwnerFocus();}
      event.preventDefault();
      if(send({t:"mouse",e:route.event},true)){
        buttons.current.sent(route.event);
        if(route.event.button==="left"&&route.event.type!=="mouseMoved")pickGesture.current.press(generation.current,performance.now());
      }
    }
  };
  const paste=async(text:string)=>{
    const gen=generation.current;const inputGen=inputEpoch.current;
    for(const msg of pasteMessages(text)) {
      if(gen!==generation.current||inputGen!==inputEpoch.current||!canInput())break;
      send(msg,true);await new Promise<void>(resolve=>setTimeout(resolve,0));
    }
  };
  const driveControl=(action:"stop"|"resume")=>{
    const lease=driveLease.current;
    if(lease?.tabId===latest.current.tabId)driving.control(lease,action,!canInput(),Date.now(),message=>send(message,true));
  };
  return <div className="remoteview" data-testid="remote-view">
    <DriveStatus snapshot={drive} blocked={props.blocked||!props.live||!!auth} slotOf={props.slotOf??(()=>null)} onControl={driveControl} />
    <div className="remotestatus small muted" role="status">
      <span>{connected?`Remote · ${security==="secure"?"Encrypted connection":security==="unknown"?"Connection security unknown":"Connection not secure"}`:"Remote · Connecting"}</span>
      <button type="button" disabled={!connected||props.blocked} onClick={()=>setDownloads(v=>!v)}>Downloads</button>
    </div>
    {error?<div className="pad err" data-testid="remote-error">{error} {!connected?<button disabled={props.blocked} onClick={()=>{const gen=generation.current;void Promise.resolve(latest.current.onRetry?.()).then(()=>{if(generation.current===gen&&latest.current.live&&!latest.current.blocked)setRetry(v=>v+1);});}}>Retry</button>:null}</div>:null}
    {auth?<BrowserAuth key={auth.id} challenge={auth}
      blocked={props.blocked||!connected||!props.live||!!props.inputHeld?.()}
      onAnswer={(id,username,password)=>answerAuth(id,{t:"auth",id,cancel:false,username,password})}
      onCancel={id=>answerAuth(id,{t:"auth",id,cancel:true})} />:null}
    {dialogs.map(dialog=><PageQuestion key={dialog.id} dialog={dialog} blocked={props.blocked||!connected}
      answer={(accept,text)=>{if(canInput())send({t:"dialog",id:dialog.id,accept,...(dialog.kind==="prompt"?{text}: {})},true);}} />)}
    {chooser||downloads?<BrowserFiles tabId={props.tabId} projectId={props.projectId} chooser={chooser} downloads={downloads}
      blocked={props.blocked||!connected||!!auth} generation={generation.current} onOwnerInput={()=>{const lease=driveLease.current;if(lease?.tabId===latest.current.tabId&&canInput())driving.owner(lease,{t:"files"},Date.now());}} onCloseDownloads={()=>setDownloads(false)} />:null}
    {owner.find?<RemoteFind query={owner.query} status={owner.status} blocked={!canInput()} onEdit={owner.edit} onFind={owner.run} onClose={owner.closeFind}/>:null}
    <div className="remotesurface" ref={surface} data-testid="remote-surface" style={{cursor:owner.cursor}}
      onMouseLeave={()=>owner.clearCursor()}
      onMouseDown={e=>mouse("down",e)} onMouseMove={e=>mouse("move",e)} onMouseUp={e=>mouse("up",e)}
      onPointerDown={e=>{if(canInput()&&!owner.consumesMouse(e))e.currentTarget.setPointerCapture(e.pointerId);}}
      onPointerCancel={()=>release()} onLostPointerCapture={()=>release()}
      onContextMenu={e=>{if(pickerRef.current||pickMenuSuppressed.current){e.preventDefault();e.stopPropagation();return;}owner.context(e);}}
      onWheel={e=>{if(owner.menu)return;if(pickerRef.current){e.preventDefault();closePicker(true);return;}if(!canInput())return;const at=point(e);if(!at.inside)return;const event=cdpWheel(e,at,props.zoom,geometry.current.view.height,geometry.current.place.scale);if(event){e.preventDefault();send({t:"mouse",e:event},true);}}}>
      <canvas ref={canvas} role="img" aria-label="Remote page image" />
      <textarea ref={input} className="remoteinput" data-remote-input data-testid="remote-input" aria-label="Type into the Remote page" spellCheck={false} autoComplete="off"
        disabled={!connected||props.blocked||!!auth} onBlur={()=>{release();send({t:"blur"},true);}}
        onFocus={()=>{if(canInput())send({t:"focus"},true);}}
        onKeyDown={e=>{
          if(pickerRef.current){
            // The open picker takes the keys, as the native popup would; the
            // HUD's own shortcuts and an approval's Escape keep theirs.
            if(document.getElementById("authveil"))return;
            const route=routeKeyDown(e.nativeEvent,!canInput());
            if(route.kind==="hud")return;
            e.preventDefault();e.stopPropagation();
            if(route.kind==="pane"){closePicker(true);release();latest.current.onAction(route.action);return;}
            pickerKeyDown(e.nativeEvent);return;
          }
          if(document.activeElement===input.current&&owner.shortcut(e.nativeEvent)&&canInput()){
            e.preventDefault();e.stopPropagation();owner.openFind();return;
          }
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
      {picker&&pickerLive?<RemotePicker picker={picker} anchor={pageToCanvas(picker.rect,geometry.current.place)}
        bounds={{width:surface.current?.clientWidth??0,height:surface.current?.clientHeight??0}}
        highlight={pickHighlight} value={pickValue} blocked={!canInput()}
        onHighlight={setPickHighlight} onValue={setPickValue} onPick={pickChoice} onCancel={()=>closePicker(true,true)} />:null}
      {owner.menu?<RemoteContextMenu point={owner.menu} bounds={owner.menu.bounds} blocked={!canInput()} onAction={owner.menuAction} onClose={owner.closeMenu}/>:null}
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
