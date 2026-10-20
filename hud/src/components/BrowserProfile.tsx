import { noPointerFocus } from "../lib/talkkey";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { browserApi } from "../api";
import { cleanTitle } from "../lib/remote";
import type { PaneNo } from "../lib/workspace";
import type { BrowserControl, BrowserProfile as Profile } from "../state/browser";
/** `/status` → `browser.sync` (password_store.py): the store each running Chrome was observed to use, never a secret. */
export interface ProfileSync { store:string|null; sticky:boolean; next_launch:string; unprotected:boolean; hint:string|null }
export interface PasswordSync { password_store:string|null; requested:string; keyring:string; next_launch:string; unprotected:boolean; hint:string|null; profiles?:Record<string,ProfileSync> }
const text=(v:unknown)=>typeof v==="string"?v.slice(0,600):null;
const profileOf=(raw:unknown):ProfileSync|null=>{
  const p=raw as Record<string,unknown>|null;
  if(!p||typeof p!=="object"||typeof p.next_launch!=="string"||typeof p.unprotected!=="boolean"||typeof p.sticky!=="boolean")return null;
  return {store:text(p.store),sticky:p.sticky,next_launch:p.next_launch,unprotected:p.unprotected,hint:text(p.hint)};
};
const syncOf=(raw:unknown):PasswordSync|null=>{
  const s=(raw as {sync?:unknown}|null)?.sync as Record<string,unknown>|undefined;
  if(!s||typeof s!=="object"||typeof s.unprotected!=="boolean"||typeof s.next_launch!=="string")return null;
  const profiles:Record<string,ProfileSync>={};
  if(s.profiles&&typeof s.profiles==="object")for(const [id,row] of Object.entries(s.profiles as Record<string,unknown>)){const p=profileOf(row);if(p&&/^p-[0-9a-f]{8}$/.test(id))profiles[id]=p;}
  return {password_store:text(s.password_store),requested:String(s.requested??""),keyring:String(s.keyring??""),
    next_launch:s.next_launch,unprotected:s.unprotected,hint:text(s.hint),profiles};
};
const CHECKING={text:"Password store: checking…",loud:false};
const UNPROTECTED="Unprotected (basic store): saved passwords use a public key.";
/** What the Manage panel says about saved passwords. Unprotected and refused are loud.
 * The selected profile's own state when `/status` has it (what its running Chrome was
 * observed to use, else what its next launch would do); the global line otherwise. */
export function passwordLine(sync:PasswordSync|null, row:Profile|undefined):{text:string;loud:boolean} {
  if(!sync)return CHECKING;
  const mine=row?sync.profiles?.[row.id]:undefined;
  if(mine){
    const never=mine.sticky?"; this profile never uses the basic store.":".";
    if(mine.store==="gnome-libsecret")return {text:"Passwords are encrypted with your keyring (gnome-libsecret, checked)"+never,loud:false};
    if(mine.store)return {text:mine.hint??UNPROTECTED,loud:true};
    if(mine.next_launch==="checking")return CHECKING;
    if(mine.next_launch==="refused")return {text:mine.hint??"The keyring is locked.",loud:true};
    if(mine.next_launch==="basic"||mine.unprotected)return {text:mine.hint??UNPROTECTED,loud:true};
    return {text:"Passwords are encrypted with your keyring (gnome-libsecret)"+never,loud:false};
  }
  const sticky=row?.password_store==="keyring";
  if(sync.next_launch==="checking")return CHECKING;
  if(sync.next_launch==="refused")return {text:sync.hint??"The keyring is locked.",loud:true};
  if(sticky&&sync.next_launch!=="gnome-libsecret")return {text:"This profile’s passwords are in the keyring, which is not available. "+(sync.hint??""),loud:true};
  if(sync.unprotected||sync.next_launch==="basic")return {text:sync.hint??UNPROTECTED,loud:true};
  return {text:"Passwords are encrypted with your keyring (gnome-libsecret)"+(sticky?"; this profile never uses the basic store.":"."),loud:false};
}
/** How often the open Manage panel reads `/status` again (LOW 4): an unlock in another window shows within seconds. */
export const PASSWORD_REFRESH_MS=3000;
export function BrowserProfile(props:{pane:PaneNo;control:BrowserControl;selected:string|null;see:boolean;blocked:boolean;privateEnded?:boolean;statusTarget?:HTMLElement|null}) {
  const [manage,setManage]=useState(false);const [label,setLabel]=useState("");
  const [pending,setPending]=useState(false);const [error,setError]=useState("");
  const [confirm,setConfirm]=useState<"delete"|"forget"|null>(null);
  const latest=useRef(props);latest.current=props;
  const row=props.control.profiles.find(p=>p.id===props.selected);
  const [sync,setSync]=useState<PasswordSync|null>(null);
  useEffect(()=>{
    if(!manage)return;let live=true;let seq=0;
    const read=()=>{const n=++seq;void browserApi.get<unknown>("").then(raw=>{if(live&&n===seq)setSync(syncOf(raw));}).catch(()=>{if(live&&n===seq)setSync(null);});};
    read();
    // While open: again on a short interval and when the window regains focus.
    const timer=window.setInterval(read,PASSWORD_REFRESH_MS);
    window.addEventListener("focus",read);
    return()=>{live=false;window.clearInterval(timer);window.removeEventListener("focus",read);};
  },[manage,error]);
  const passwords=passwordLine(sync,row);
  const run=async(job:()=>Promise<unknown>)=>{
    if(latest.current.blocked||pending)return;setPending(true);setError("");
    try {await job();}catch(e){setError(cleanTitle(e instanceof Error?e.message:"That profile could not be changed.",500));}
    finally{setPending(false);}
  };
  const disabled=props.blocked||pending||!props.control.online;
  const driveReason = props.blocked ? "Finish the approval or annotation first" : !props.see ? "Allow Jarvis to see this pane first" : props.control.globalDrive!==true ? "Agents may drive is off or unavailable" : !props.control.driveReady(props.pane) ? "Open this Private session first" : "Allow Jarvis to drive this pane when asked";

  const status=<>
    {pending?<p className="small muted" role="status">Applying Browser change…</p>:null}
    {error?<p className="err" role="status">{error}</p>:null}
  </>;
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
      <p className="small muted">Remote uses this profile. Direct uses this window’s browser session. Chrome sync sign-in may be unavailable on this installation.</p>
      <p className={passwords.loud?"err":"small muted"} data-testid="browser-password-store" style={{whiteSpace:"pre-wrap",userSelect:"text"}}>{passwords.text}</p>
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
    {props.statusTarget ? createPortal(status, props.statusTarget) : status}
  </div>;
}
