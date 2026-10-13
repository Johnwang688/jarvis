import { noPointerFocus } from "../lib/talkkey";
import { useRef, useState } from "react";
import { browserApi } from "../api";
import { cleanTitle } from "../lib/remote";
import type { PaneNo } from "../lib/workspace";
import type { BrowserControl, BrowserProfile as Profile } from "../state/browser";
export function BrowserProfile(props:{pane:PaneNo;control:BrowserControl;selected:string|null;see:boolean;blocked:boolean;privateEnded?:boolean}) {
  const [manage,setManage]=useState(false);const [label,setLabel]=useState("");
  const [pending,setPending]=useState(false);const [error,setError]=useState("");
  const [confirm,setConfirm]=useState<"delete"|"forget"|null>(null);
  const latest=useRef(props);latest.current=props;
  const row=props.control.profiles.find(p=>p.id===props.selected);
  const run=async(job:()=>Promise<unknown>)=>{
    if(latest.current.blocked||pending)return;setPending(true);setError("");
    try {await job();}catch(e){setError(cleanTitle(e instanceof Error?e.message:"That profile could not be changed.",300));}
    finally{setPending(false);}
  };
  const disabled=props.blocked||pending||!props.control.online;
  const driveReason = props.blocked ? "Finish the approval or annotation first" : !props.see ? "Allow Jarvis to see this pane first" : props.control.globalDrive!==true ? "Agents may drive is off or unavailable" : !props.control.driveReady(props.pane) ? "Open this Private session first" : "Allow Jarvis to drive this pane when asked";
  return <div className="browserprofile" data-testid="browser-profile">
    <div className="toolbar browserpermissions">
      <label>Profile <select aria-label="Browser profile" value={props.selected??""} disabled={disabled}
        onChange={e=>{setConfirm(null);void run(()=>props.control.profile(props.pane,e.target.value));}}>
        {!props.selected?<option value="">Loading profiles…</option>:null}
        {props.selected&&props.selected!=="private"&&!row?<option value={props.selected}>Deleted profile — pick one</option>:null}
        {props.control.profiles.map(p=><option key={p.id} value={p.id}>{p.label}</option>)}
        <option value="private">Private</option>
      </select></label>
      <button disabled={disabled} onClick={()=>{setManage(v=>!v);setConfirm(null);setLabel(row?.label??"");}}>Manage</button>
      <label title="Allow Jarvis to read this pane when asked. Navigation never turns this on."><input type="checkbox" data-testid="browser-see" checked={props.see} disabled={disabled} onChange={e=>void run(()=>props.control.see(props.pane,e.target.checked))}/> Jarvis can see</label>
      <label title={driveReason}><input type="checkbox" data-testid="browser-drive" checked={props.control.drivePreference(props.pane)} disabled={disabled||!props.see||props.control.globalDrive!==true||!props.control.driveReady(props.pane)} onMouseDown={noPointerFocus} onChange={e=>{if(e.nativeEvent.isTrusted)void run(()=>props.control.drive(props.pane,e.target.checked));}}/> Jarvis can drive</label>
    </div>
    {props.selected==="private"?<p className="small muted">Private · Remote only · This pane has a separate temporary session.</p>:null}
    {props.privateEnded&&props.selected==="private"?<button disabled={disabled} onClick={()=>void run(()=>props.control.profile(props.pane,"private"))}>Start a new Private session</button>:null}
    {manage?<div className="browserprofilemanage pad" data-testid="browser-profile-manage">
      <p className="small muted">Remote uses this profile. Direct uses this window’s browser session. Chrome sync sign-in may be unavailable on this installation; passwords use Chrome’s basic store.</p>
      <label>Profile name <input value={label} maxLength={32} disabled={disabled} onChange={e=>setLabel(e.target.value)} onKeyDown={e=>e.stopPropagation()} /></label>
      <div className="row">
        <button disabled={disabled||!label.trim()||props.control.profiles.length>=8} onClick={()=>void run(async()=>{const p=await browserApi.write<Profile>("/profiles",{label:label.trim().normalize("NFC")});await props.control.refresh();if(!latest.current.blocked)await props.control.profile(props.pane,p.id);})}>New profile</button>
        <button disabled={disabled||!row||!label.trim()} onClick={()=>void run(async()=>{await browserApi.write(`/profiles/${row!.id}`,{label:label.trim().normalize("NFC")},"PATCH");await props.control.refresh();})}>Rename</button>
        <button disabled={disabled||!row} onClick={()=>void run(()=>props.control.signin(props.pane))}>Sign in to Chrome</button>
        <button disabled={disabled||!row} onClick={()=>setConfirm("forget")}>Sign out of everything</button>
        <button disabled={disabled||!row} onClick={()=>setConfirm("delete")}>Delete profile</button>
      </div>
      {confirm&&row?<div className="browserquestion"><p>{confirm==="delete"?"Delete this profile and close all its tabs?":"Clear this profile’s cookies and saved sessions, and close its tabs?"}</p>
        <button disabled={disabled} onClick={()=>void run(async()=>{if(confirm==="delete")await browserApi.remove(`/profiles/${row.id}`);else await browserApi.write(`/profiles/${row.id}/forget`);setConfirm(null);await props.control.refresh();})}>Confirm</button>
        <button disabled={disabled} onClick={()=>setConfirm(null)}>Cancel</button></div>:null}
    </div>:null}
    {pending?<p className="small muted" role="status">Applying Browser change…</p>:null}
    {error?<p className="err" role="status">{error}</p>:null}
  </div>;
}
