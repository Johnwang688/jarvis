import { useEffect, useRef, useState } from "react";
import { api, browserApi } from "../api";
import { cleanTitle } from "../lib/remote";
import type { Tree } from "../types";
interface Download {id:string;profile:string;name:string;size:number;held:boolean}
export function BrowserFiles(props:{tabId:string;projectId:string|null;chooser:{id:string;multiple:boolean}|null;downloads:boolean;blocked:boolean;generation:number;onCloseDownloads:()=>void}) {
  const latest=useRef(props);latest.current=props;
  const [path,setPath]=useState("");const [tree,setTree]=useState<Tree|null>(null);
  const [selected,setSelected]=useState<string[]>([]);const [rows,setRows]=useState<Download[]>([]);
  const [error,setError]=useState("");const [busy,setBusy]=useState(false);
  useEffect(()=>{
    let cancelled=false;
    if(props.chooser&&props.projectId)void api.tree(props.projectId,path,1).then(row=>{if(!cancelled)setTree(row);}).catch(()=>{if(!cancelled)setError("That project folder could not be listed.");});
    return()=>{cancelled=true;};
  },[props.chooser?.id,props.projectId,path]);
  useEffect(()=>{
    let cancelled=false;
    const load=()=>{if(!props.downloads||latest.current.blocked)return;void browserApi.get<{downloads:Download[]}>("/downloads").then(row=>{if(!cancelled)setRows(row.downloads);}).catch(()=>{if(!cancelled)setError("Downloads could not be listed.");});};
    load();const timer=setInterval(load,3000);return()=>{cancelled=true;clearInterval(timer);};
  },[props.downloads,props.tabId]);
  useEffect(()=>{setSelected([]);setPath("");setError("");},[props.chooser?.id]);
  const current=(initial:typeof props)=>latest.current.generation===initial.generation&&latest.current.tabId===initial.tabId&&!latest.current.blocked;
  const upload=async()=>{
    const initial=latest.current;if(initial.blocked||!initial.chooser||!initial.projectId)return;
    setBusy(true);setError("");
    try {await browserApi.write(`/tabs/${initial.tabId}/files`,{chooser:initial.chooser.id,files:selected.map(path=>({project:initial.projectId,path}))});}
    catch(e){if(current(initial))setError(cleanTitle(e instanceof Error?e.message:"The files could not be sent.",300));}
    finally{if(current(initial))setBusy(false);}
  };
  const save=async(row:Download)=>{
    const initial=latest.current;if(initial.blocked)return;setBusy(true);setError("");
    try {
      const blob=await browserApi.download(row.id);if(!current(initial))return;
      const url=URL.createObjectURL(blob);const anchor=document.createElement("a");anchor.href=url;anchor.download=cleanTitle(row.name,200)||"download";anchor.click();setTimeout(()=>URL.revokeObjectURL(url),0);
    }catch(e){if(current(initial))setError(cleanTitle(e instanceof Error?e.message:"The download could not be saved.",300));}
    finally{if(current(initial))setBusy(false);}
  };
  return <div className="browserfiles pad" data-testid="browser-files">
    {props.chooser?<><strong>This page asks for a file</strong>
      {!props.projectId?<p>Choose a project for this pane to select a file.</p>:<>
        <div className="row"><bdi>{path||"Project files"}</bdi>{path?<button disabled={props.blocked} onClick={()=>setPath(path.split("/").slice(0,-1).join("/"))}>Up</button>:null}</div>
        <div className="browserfilelist">{tree?.entries.filter(e=>!e.policy).map(entry=>{const file=[path,entry.name].filter(Boolean).join("/");return entry.kind==="dir"?
          <button key={file} disabled={props.blocked} onClick={()=>setPath(file)}>{entry.name}/</button>:
          <label key={file}><input type="checkbox" disabled={props.blocked} checked={selected.includes(file)} onChange={e=>setSelected(old=>e.target.checked?(props.chooser?.multiple?[...old,file]:[file]):old.filter(p=>p!==file))}/><bdi>{entry.name}</bdi></label>;})}</div>
        <button disabled={props.blocked||busy||!selected.length} onClick={()=>void upload()}>Use selected {selected.length===1?"file":"files"}</button>
      </>}</>:null}
    {props.downloads?<><div className="row"><strong>Browser downloads</strong><button disabled={props.blocked} onClick={props.onCloseDownloads}>Close</button></div>
      {rows.map(row=><div className="row" key={row.id}><bdi>{cleanTitle(row.name,200)}</bdi><span>{row.held?"Held: executable":"Ready"}</span>{!row.held?<button disabled={props.blocked||busy} onClick={()=>void save(row)}>Save a copy</button>:null}</div>)}
      {!rows.length?<p className="muted">No saved Browser downloads.</p>:null}</>:null}
    {error?<p className="err" role="status">{error}</p>:null}
  </div>;
}
