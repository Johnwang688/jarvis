import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, browserApi } from "../api";
import { cleanTitle } from "../lib/remote";
import type { Tree } from "../types";
interface Download {id:string;profile:string;name:string;size:number;held:boolean}
export function BrowserFiles(props:{tabId:string;projectId:string|null;chooser:{id:string;multiple:boolean}|null;downloads:boolean;blocked:boolean;generation:number;onOwnerInput?:()=>void;onCloseDownloads:()=>void}) {
  const latest=useRef(props);latest.current=props;
  // A listing and its selections belong to one chooser/project binding. Do not
  // reinterpret retained names under a new folder or a replacement chooser.
  const scope=JSON.stringify([props.projectId,props.chooser?.id,props.tabId,props.generation]);
  const [location,setLocation]=useState({scope,path:""});
  const path=location.scope===scope?location.path:"";
  const [listing,setListing]=useState<{key:string;tree:Tree}|null>(null);
  const listingKey=JSON.stringify([scope,path]);
  const tree=listing?.key===listingKey?listing.tree:null;
  const [selection,setSelection]=useState<{scope:string;paths:string[]}>({scope,paths:[]});
  const selected=selection.scope===scope?selection.paths:[];
  const setPath=(path:string)=>{setListing(null);setError("");setLocation({scope,path});};
  const [rows,setRows]=useState<Download[]>([]);
  const [error,setError]=useState("");const [busy,setBusy]=useState(false);
  const revision=useRef(0);
  useLayoutEffect(()=>{revision.current++;setBusy(false);return()=>{revision.current++;};},
    [props.blocked,props.generation,props.tabId,props.chooser?.id,props.projectId,props.downloads]);
  useEffect(()=>{
    let cancelled=false;
    if(props.chooser&&props.projectId)void api.tree(props.projectId,path,1).then(row=>{if(!cancelled)setListing({key:listingKey,tree:row});}).catch(()=>{if(!cancelled)setError("That project folder could not be listed.");});
    return()=>{cancelled=true;};
  },[scope,listingKey,props.chooser?.id,props.projectId,path]);
  useEffect(()=>{
    let cancelled=false;
    const load=()=>{if(!props.downloads||latest.current.blocked)return;void browserApi.get<{downloads:Download[]}>("/downloads").then(row=>{if(!cancelled)setRows(row.downloads);}).catch(()=>{if(!cancelled)setError("Downloads could not be listed.");});};
    load();const timer=setInterval(load,3000);return()=>{cancelled=true;clearInterval(timer);};
  },[props.downloads,props.tabId]);
  useEffect(()=>{setListing(null);setSelection({scope,paths:[]});setLocation({scope,path:""});setError("");},[scope]);
  const current=(initial:typeof props,started:number)=>revision.current===started&&latest.current.generation===initial.generation&&latest.current.tabId===initial.tabId&&!latest.current.blocked;
  const upload=async()=>{
    const initial=latest.current;const started=revision.current;if(initial.blocked||!initial.chooser||!initial.projectId)return;
    setBusy(true);setError("");
    try {await browserApi.write(`/tabs/${initial.tabId}/files`,{chooser:initial.chooser.id,files:selected.map(path=>({project:initial.projectId,path}))});if(current(initial,started))latest.current.onOwnerInput?.();}
    catch(e){if(current(initial,started))setError(cleanTitle(e instanceof Error?e.message:"The files could not be sent.",300));}
    finally{if(current(initial,started))setBusy(false);}
  };
  const save=async(row:Download)=>{
    const initial=latest.current;const started=revision.current;if(initial.blocked||row.held)return;setBusy(true);setError("");
    try {
      const blob=await browserApi.download(row.id);if(!current(initial,started))return;
      const url=URL.createObjectURL(blob);const anchor=document.createElement("a");anchor.href=url;anchor.download=cleanTitle(row.name,200)||"download";anchor.click();setTimeout(()=>URL.revokeObjectURL(url),0);
    }catch(e){if(current(initial,started))setError(cleanTitle(e instanceof Error?e.message:"The download could not be saved.",300));}
    finally{if(current(initial,started))setBusy(false);}
  };
  const remove=async(row:Download)=>{
    const initial=latest.current,started=revision.current;if(initial.blocked||busy)return;
    setBusy(true);setError("");
    try{await browserApi.remove(`/downloads/${encodeURIComponent(row.id)}`);if(current(initial,started))setRows(old=>old.filter(item=>item.id!==row.id));}
    catch{if(current(initial,started))setError("The download could not be deleted.");}
    finally{if(current(initial,started))setBusy(false);}
  };
  return <div className="browserfiles pad" data-testid="browser-files">
    {props.chooser?<><strong>This page asks for a file</strong>
      {!props.projectId?<p>Choose a project for this pane to select a file.</p>:<>
        <div className="row"><bdi>{path||"Project files"}</bdi>{path?<button disabled={props.blocked} onClick={()=>setPath(path.split("/").slice(0,-1).join("/"))}>Up</button>:null}</div>
        <div className="browserfilelist">{tree?.entries.filter(e=>!e.policy).map(entry=>{const file=[path,entry.name].filter(Boolean).join("/");return entry.kind==="dir"?
          <button key={file} disabled={props.blocked} onClick={()=>setPath(file)}>{entry.name}/</button>:
          <label key={file}><input type="checkbox" disabled={props.blocked} checked={selected.includes(file)} onChange={e=>{const checked=e.target.checked;setSelection(previous=>{const old=previous.scope===scope?previous.paths:[];return{scope,paths:checked?(props.chooser?.multiple?[...old,file]:[file]):old.filter(p=>p!==file)};});}}/><bdi>{entry.name}</bdi></label>;})}</div>
        <button disabled={props.blocked||busy||!selected.length} onClick={()=>void upload()}>Use selected {selected.length===1?"file":"files"}</button>
      </>}</>:null}
    {props.downloads?<><div className="row"><strong>Browser downloads</strong><button disabled={props.blocked} onClick={props.onCloseDownloads}>Close</button></div>
      {rows.map(row=><div className="row" key={row.id}><bdi>{cleanTitle(row.name,200)}</bdi><span>{row.held?"Held for safety":"Ready"}</span>{!row.held?<button disabled={props.blocked||busy} onClick={()=>void save(row)}>Save a copy</button>:null}<button disabled={props.blocked||busy} onClick={()=>void remove(row)}>Delete</button></div>)}
      {rows.some(row=>row.held)?<p className="muted">A held download cannot be saved here. Delete it and reopen the page in a fresh tab before trying again.</p>:null}
      {!rows.length?<p className="muted">No saved Browser downloads.</p>:null}</>:null}
    {error?<p className="err" role="status">{error}</p>:null}
  </div>;
}
