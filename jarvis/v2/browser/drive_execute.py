"""Fixed CDP operations for a prepared Browser action; owner loop only.

No evaluator or coordinates are accepted from the tool. Every event is preceded
by the same live gate and a fresh value-free target check. Cancellation waits for
an already dispatched event to settle and never schedules subsequent typing.
"""
from __future__ import annotations

import asyncio
import base64
import mimetypes
import json
import secrets
import time

from jarvis import untrusted
from jarvis.v2 import terminal_guard
from . import act_rules as rules
from .drive_scan import PREPARE_JS, prepare_result
from .look import LookRefused, guarded
from .look_capture import _frame, _scan, _settle
from .look_scan import NODE_CAP, RAW_CAP, REF_CAP, SCAN_TIMEOUT_S

ACTION_TIMEOUT_S=30.0


async def _evaluate(cdp,world,script,args,*,by_value=True):
    result=await asyncio.wait_for(cdp.send("Runtime.evaluate",{
        "expression":"("+script+")("+json.dumps(args)+")","contextId":world,
        "returnByValue":by_value,"silent":True,"timeout":SCAN_TIMEOUT_S*1000}),SCAN_TIMEOUT_S+.5)
    if result.get("exceptionDetails"):raise LookRefused("the Browser action could not inspect its target safely")
    value=result.get("result") or {}
    return value.get("value") if by_value else value.get("objectId")


async def _world(host,snapshot,guard):
    guard()
    tab=host._tabs.get(snapshot.tab_id)
    if (tab is None or tab.page_identity!=snapshot.page_identity
            or tab.document_generation!=snapshot.document_generation):raise LookRefused("the Browser document changed; look again")
    if tab.dialogs or (tab.input_task is not None and not tab.input_task.done()):
        raise LookRefused("the Browser page is still handling an input or dialog")
    cdp=await host._ensure_cdp(tab)
    frame=await _frame(cdp)
    if frame[2]!=snapshot.url:raise LookRefused("the Browser document changed; look again")
    world=(await cdp.send("Page.createIsolatedWorld",{
        "frameId":frame[0],"worldName":"jarvis-look","grantUniveralAccess":False}))["executionContextId"]
    guard()
    return tab,cdp,world


async def _prepared(cdp,world,snapshot,action,serial,pin,forms,guard,*,pin_replace):
    guard()
    args={"url":snapshot.url,"serial":serial,"whole":False,"replace":False,
          "nodeCap":NODE_CAP,"rawCap":RAW_CAP,"refCap":REF_CAP,
          "action":action.action,"target":action.target,"pin":pin,"pinReplace":pin_replace}
    raw=await _evaluate(cdp,world,PREPARE_JS,args)
    result=prepare_result(raw,action.target,forms)
    guard()
    return result


async def prepare(host,snapshot,action,capability,pin,forms,guard,prepared=None):
    if host._look_lock is None:host._look_lock=asyncio.Lock()
    async with host._look_lock:
        tab,cdp,world=await _world(host,snapshot,guard)
        serial=prepared.serial if prepared is not None else (capability.serial if capability is not None else secrets.token_hex(16))
        if prepared is None and capability is None:
            raw=await _scan(cdp,world,snapshot.url,serial,False,True)
            guarded(raw,forms)
            await _evaluate(cdp,world,"()=>{globalThis.__jarvisLook?.observer?.disconnect();return true}",{})
        return await _prepared(cdp,world,snapshot,action,serial,pin,forms,guard,pin_replace=prepared is None)


# Exact isolated object, centre coverage and ordinary enabled state. The hit
# walks composed ancestors, so a shadow target or its child remains usable.
TARGET_JS=r"""(args)=>{
const s=globalThis.__jarvisLook;
if(!s||s.serial!==args.serial||!s.prepared||s.prepared.pin!==args.pin)return null;
const e=s.prepared.element;if(!e||!e.isConnected||e.disabled||e.readOnly)return null;
if(args.scroll){e.scrollIntoView({block:'center',inline:'center',behavior:'instant'});return true;}
const r=e.getBoundingClientRect(),x=args.point?args.point.x:r.left+r.width/2,y=args.point?args.point.y:r.top+r.height/2;
if(x<r.left||x>=r.right||y<r.top||y>=r.bottom)return null;
if(r.width<8||r.height<8||x<0||y<0||x>=innerWidth||y>=innerHeight)return null;
let hit=document.elementFromPoint(x,y),depth=0;
while(hit&&hit.shadowRoot){if(++depth>64)return null;const inner=hit.shadowRoot.elementFromPoint(x,y);if(!inner||inner===hit)break;hit=inner;}
for(let a=hit,n=0;a&&n++<20000;a=a.parentElement||(a.parentNode instanceof ShadowRoot?a.parentNode.host:null)){
 if(a===e)return args.object?e:{x,y};
}
return null;
}"""

SELECT_JS=r"""(args)=>{
const s=globalThis.__jarvisLook;
if(!s||s.serial!==args.serial||!s.prepared||s.prepared.pin!==args.pin)return false;
const e=s.prepared.element;
if(!e||!e.isConnected||e.tagName!=='SELECT'||e.disabled||!Number.isInteger(args.index))return false;
const option=e.options[args.index];
if(!option||option.disabled||option.closest('optgroup[disabled]')||
   (option.getAttribute('label')||option.textContent||'')!==args.label)return false;
if(args.phase==='assign'){e.selectedIndex=args.index;return true;}
if(e.selectedIndex!==args.index)return false;
if(args.phase==='input'){e.dispatchEvent(new Event('input',{bubbles:true,composed:true}));return true;}
if(args.phase==='change'){e.dispatchEvent(new Event('change',{bubbles:true}));return true;}
return false;
}"""

# A bounded buffer in the isolated world becomes a byte-backed File. Chrome
# never receives a source path or a lazy filesystem reference. At most 1 MiB
# of base64 is serialized in one CDP message; decoding creates no JS number
# array proportional to a 50 MiB upload. Every phase pins the same DOM object.
UPLOAD_CHUNK_BYTES=192*1024
UPLOAD_JS=r"""(args)=>{
const s=globalThis.__jarvisLook;
if(!s||s.serial!==args.serial||!s.prepared||s.prepared.pin!==args.pin)return false;
const e=s.prepared.element;
if(!e||!e.isConnected||e.tagName!=='INPUT'||e.type!=='file'||e.disabled||e.readOnly||
   e.webkitdirectory||e.hasAttribute('webkitdirectory')||e.hasAttribute('directory'))return false;
if(args.phase==='begin'){
 if(!Number.isInteger(args.size)||args.size<0||args.size>52428800)return false;
 s.prepared.upload={parts:[],size:args.size,offset:0};return true;
}
const u=s.prepared.upload;if(!u)return false;
if(args.phase==='chunk'){
 if(u.file||args.offset!==u.offset||typeof args.data!=='string'||args.data.length>1048576)return false;
 if(typeof Uint8Array.fromBase64!=='function')return false;
 const bytes=Uint8Array.fromBase64(args.data,{lastChunkHandling:'strict'});
 if(bytes.length>786432||u.offset+bytes.length>u.size)return false;
 if(typeof bytes.buffer.transfer!=='function')return false;
 u.parts.push(new Blob([bytes]));u.offset+=bytes.length;bytes.buffer.transfer(0);return true;
}
if(args.phase==='assign'){
 if(u.file||u.offset!==u.size||typeof args.name!=='string'||args.name.length>255||
    /[\\/\x00-\x1f]/.test(args.name))return false;
 const file=new File(u.parts,args.name,{type:args.mime,lastModified:0});
 const list=new DataTransfer();list.items.add(file);e.files=list.files;
 u.parts=null;u.file=e.files[0];return !!u.file;
}
if(!u.file||e.files.length!==1||e.files[0]!==u.file)return false;
if(args.phase==='input'){e.dispatchEvent(new Event('input',{bubbles:true,composed:true}));return true;}
if(args.phase==='change'){e.dispatchEvent(new Event('change',{bubbles:true}));return true;}
return false;
}"""

_KEYS={"Enter":("Enter",13),"Space":("Space",32),"Tab":("Tab",9),"Escape":("Escape",27),
       "ArrowUp":("ArrowUp",38),"ArrowDown":("ArrowDown",40),"ArrowLeft":("ArrowLeft",37),
       "ArrowRight":("ArrowRight",39),"PageUp":("PageUp",33),"PageDown":("PageDown",34),
       "Home":("Home",36),"End":("End",35),"Backspace":("Backspace",8),"Delete":("Delete",46)}


async def execute(host,snapshot,action,prepared,pin,forms,guard,*,upload_bytes=None):
    from .drive import ActResult,CHANGED_ACTION
    task=asyncio.current_task();host._look_tasks.add(task)
    inserted=0;mouse_down=False;assigned=False;keys={};tab=cdp=world=None;response_handler=None
    if host._look_lock is None:host._look_lock=asyncio.Lock()
    try:
        async with host._look_lock:
            async with asyncio.timeout(ACTION_TIMEOUT_S):
                tab,cdp,world=await _world(host,snapshot,guard)
                if guard.download is not None:
                    response_handler=lambda params:guard.service.downloads.response(guard.download,params)
                    cdp.on("Network.responseReceived",response_handler)
                    await cdp.send("Network.enable",{"maxTotalBufferSize":0,"maxResourceBufferSize":0})
                identity={"serial":prepared.serial,"pin":pin}
                async def checked(*,focused=False,tab_release=False):
                    guard()
                    if tab.dialogs or (tab.input_task is not None and not tab.input_task.done()):
                        raise LookRefused("the Browser page is still handling an input or dialog")
                    now=await _prepared(cdp,world,snapshot,action,prepared.serial,pin,forms,guard,pin_replace=tab_release)
                    if tab_release:
                        # Tab intentionally changes focus. Its final keyUp may
                        # reach only another ordinary, independently ALLOW-tier
                        # focus on the unchanged document/context. A sensitive
                        # or unknown next field stops without a second card.
                        _, owner=guard()
                        facts=guard.service._facts(action,now,guard,owner)
                        if (now.context!=prepared.context or now.title!=prepared.title
                                or rules.pre_judge(facts).kind!=rules.ALLOW):
                            raise LookRefused("Browser focus moved to a field that needs review; stopped")
                    elif now.fingerprint!=prepared.fingerprint:raise LookRefused(CHANGED_ACTION)
                    if focused and not now.focused_target:raise LookRefused("the Browser focus changed; typing stopped")
                    return now
                async def send(method,params,*,focused=False,tab_release=False,ack=None):
                    nonlocal inserted,mouse_down
                    await checked(focused=focused,tab_release=tab_release)
                    if method=="Input.dispatchMouseEvent" and action.target:
                        # Revalidate the original dispatch point before BOTH
                        # press and release. Never move a pending click to a
                        # new centre or release into a newly inserted overlay.
                        point_now=await _evaluate(cdp,world,TARGET_JS,{**identity,
                            "point":{"x":params["x"],"y":params["y"]}})
                        if not point_now:raise LookRefused("the Browser target moved or became covered; stopped")
                        guard()
                    # Mark before yielding so owner input observes a busy page;
                    # owner input still stamps takeover before it is queued.
                    if method=="Input.dispatchMouseEvent" and params.get("type")=="mousePressed":mouse_down=True
                    pending=asyncio.create_task(cdp.send(method,params));tab.input_task=pending
                    async def wait_pending():return await pending
                    try:return await _settle(wait_pending())
                    finally:
                        if pending.done() and not pending.cancelled() and pending.exception() is None:
                            if ack is not None:ack(pending.result())
                            if method=="Input.insertText":inserted+=len(params["text"])
                            if method=="Input.dispatchMouseEvent" and params.get("type")=="mouseReleased":mouse_down=False
                        if pending.done() and tab.input_task is pending:tab.input_task=None
                async def apply(script,args,*,assignment=False):
                    nonlocal assigned
                    def acknowledged(reply):
                        nonlocal assigned
                        if assignment and not reply.get("exceptionDetails") and (reply.get("result") or {}).get("value") is True:
                            assigned=True
                    result=await send("Runtime.callFunctionOn",{
                        "functionDeclaration":"function(args){return ("+script+")(args)}",
                        "arguments":[{"value":args}],"executionContextId":world,
                        "returnByValue":True,"silent":True},ack=acknowledged)
                    if result.get("exceptionDetails") or (result.get("result") or {}).get("value") is not True:
                        raise LookRefused("the Browser control changed; look again")
                async def key(name,down,*,modifiers=0,focused=False):
                    code,vk=_KEYS.get(name,(name,0))
                    params={"type":"rawKeyDown" if down else "keyUp","key":" " if name=="Space" else name,
                            "code":code,"windowsVirtualKeyCode":vk,"modifiers":modifiers}
                    if down:keys[code]=params
                    await send("Input.dispatchKeyEvent",params,focused=focused,tab_release=not down and name=="Tab")
                    if not down:keys.pop(code,None)
                if action.target:
                    await checked()
                    if not await _evaluate(cdp,world,TARGET_JS,{**identity,"scroll":True}):
                        raise LookRefused("the Browser target is disabled or no longer available")
                    await checked()
                    point=await _evaluate(cdp,world,TARGET_JS,identity)
                    if not point:raise LookRefused("the Browser target is covered or off screen")
                else:
                    width,height,_scale=host._view(tab)
                    point={"x":width/2,"y":height/2}
                if action.action=="click":
                    await send("Input.dispatchMouseEvent",{"type":"mousePressed",**point,"button":"left","buttons":1,"clickCount":1})
                    await send("Input.dispatchMouseEvent",{"type":"mouseReleased",**point,"button":"left","buttons":0,"clickCount":1})
                    mouse_down=False
                elif action.action=="type":
                    if not rules._text_entry(prepared.target):raise LookRefused("that Browser target is not an ordinary text field")
                    obj=await _evaluate(cdp,world,TARGET_JS,{**identity,"object":True},by_value=False)
                    if not obj:raise LookRefused("the Browser target is no longer available")
                    await send("DOM.focus",{"objectId":obj})
                    await checked(focused=True)
                    # Modifiers are supplied on each event; no separate held
                    # Control key is introduced into the owner's input stream.
                    keys["KeyA"]={"key":"a","code":"KeyA","windowsVirtualKeyCode":65,"modifiers":2}
                    await send("Input.dispatchKeyEvent",{"type":"rawKeyDown",**keys["KeyA"]},focused=True)
                    await send("Input.dispatchKeyEvent",{"type":"keyUp",**keys["KeyA"]},focused=True)
                    keys.pop("KeyA",None)
                    # Empty text still replaces the selection with nothing.
                    for start in range(0,max(1,len(action.text)),32):
                        chunk=action.text[start:start+32]
                        await send("Input.insertText",{"text":chunk},focused=True)
                elif action.action=="press":
                    name=rules._key_name(action.text)
                    if name=="Shift+Tab":await key("Tab",True,modifiers=8);await key("Tab",False,modifiers=8)
                    else:await key(name,True);await key(name,False)
                elif action.action=="scroll":
                    direction=action.text.strip().lower() or "down"
                    delta={"up":-600,"down":600,"top":-1_000_000,"bottom":1_000_000}[direction]
                    await send("Input.dispatchMouseEvent",{"type":"mouseWheel",**point,"deltaX":0,"deltaY":delta,"modifiers":0})
                elif action.action=="select":
                    matches=[index for index,label,disabled in prepared.options if label==action.text and not disabled]
                    if len(matches)!=1:raise LookRefused("choose one unique enabled option label from this Browser control")
                    args={**identity,"index":matches[0],"label":action.text}
                    await apply(SELECT_JS,{**args,"phase":"assign"},assignment=True)
                    await apply(SELECT_JS,{**args,"phase":"input"})
                    await apply(SELECT_JS,{**args,"phase":"change"})
                elif action.action=="upload":
                    from .drive_files import UploadBytes
                    if (not isinstance(upload_bytes,UploadBytes) or not isinstance(upload_bytes.data,bytes)
                            or len(upload_bytes.data)>rules.MAX_UPLOAD_BYTES or not prepared.target
                            or prepared.target.tag!="input" or prepared.target.type.lower()!="file"):
                        raise LookRefused("the Browser upload target or approved file is no longer available")
                    await apply(UPLOAD_JS,{**identity,"phase":"begin","size":len(upload_bytes.data)})
                    for offset in range(0,len(upload_bytes.data),UPLOAD_CHUNK_BYTES):
                        chunk=base64.b64encode(upload_bytes.data[offset:offset+UPLOAD_CHUNK_BYTES]).decode("ascii")
                        await apply(UPLOAD_JS,{**identity,"phase":"chunk","offset":offset,"data":chunk})
                    await apply(UPLOAD_JS,{**identity,"phase":"assign","name":upload_bytes.name,
                        "mime":mimetypes.guess_type(upload_bytes.name)[0] or "application/octet-stream"},assignment=True)
                    await apply(UPLOAD_JS,{**identity,"phase":"input"})
                    await apply(UPLOAD_JS,{**identity,"phase":"change"})
                elif action.action=="navigate":
                    await checked();guard.service._url(action.text)
                    await send("Page.navigate",{"url":action.text})
                elif action.action in ("back","forward"):
                    await checked();history=await cdp.send("Page.getNavigationHistory")
                    index=history["currentIndex"]+(-1 if action.action=="back" else 1)
                    if not 0<=index<len(history["entries"]):raise LookRefused("there is no Browser history entry in that direction")
                    entry=history["entries"][index];guard.service._url(entry["url"])
                    await send("Page.navigateToHistoryEntry",{"entryId":entry["id"]})
                # Input is complete. Only this phase may accept an increased
                # document generation on the same exact visible page binding.
                end=time.monotonic()+2
                scan=None;fresh=None
                while time.monotonic()<end:
                    fresh,_=guard(navigation=True)
                    cdp=await host._ensure_cdp(tab);frame=await _frame(cdp)
                    if frame[2]!=fresh.url:await asyncio.sleep(.05);continue
                    world=(await cdp.send("Page.createIsolatedWorld",{
                        "frameId":frame[0],"worldName":"jarvis-look","grantUniveralAccess":False}))["executionContextId"]
                    serial=secrets.token_hex(16)
                    scan=await _scan(cdp,world,fresh.url,serial,False,True)
                    guarded(scan,forms)
                    await asyncio.sleep(.3)
                    after=await _scan(cdp,world,fresh.url,serial,False,False)
                    guarded(after,forms)
                    again,_=guard(navigation=True)
                    if scan==after and again==fresh:break
                    scan=None
                if scan is None:raise LookRefused("the Browser changed after the action; look again")
                guard(navigation=True)
                labels=[]
                for row in scan["refs"]:
                    label="[withheld]" if terminal_guard.withhold(row["label"]) else untrusted.one_line(row["label"],120)
                    labels.append(row["ref"]+" ("+row["tag"]+"): "+label)
                return ActResult("acted","Acted in Browser pane "+str(fresh.slot)+"."+
                    ("\nFresh interactive refs (page text):\n"+"\n".join(labels) if labels else "\nNo visible interactive refs."),fresh,scan)
    except (Exception,asyncio.CancelledError):
        if assigned:
            text=("Stopped after selecting the approved file" if action.action=="upload" else "Stopped after updating the Browser control")
            return ActResult("stopped",text+"; page events may be incomplete. Look again.")
        return ActResult("stopped","Stopped after sending "+str(inserted)+" characters; look again." if action.action=="type" else "Stopped; look again before another Browser action.")
    finally:
        async def cleanup():
            if cdp is not None:
                if response_handler is not None:
                    try:cdp.remove_listener("Network.responseReceived",response_handler)
                    except Exception:pass
                    try:await asyncio.wait_for(cdp.send("Network.disable"),1)
                    except Exception:pass
                if mouse_down and tab is not None and not tab.pressed:
                    # Blink's neutral reset, never mouseReleased/click.
                    try:await asyncio.wait_for(cdp.send("Input.dispatchMouseEvent",host._let_go({})),1)
                    except Exception:pass
                # A keyUp can execute page handlers. Do not manufacture one
                # after a hold or takeover; future inputs specify modifiers.
                keys.clear()
                if world is not None:
                    try:await _evaluate(cdp,world,"()=>{globalThis.__jarvisLook?.observer?.disconnect();delete globalThis.__jarvisLook?.prepared;return true}",{})
                    except Exception:pass
        try:await _settle(cleanup())
        finally:host._look_tasks.discard(task)
