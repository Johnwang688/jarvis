import { useState, useSyncExternalStore, useEffect } from "react";
import { browserSettings } from "../state/browserSettings";
import { api } from "../api";
import { BrowserSoundSetting } from "./BrowserSound";
import { devservers } from "../state/devserverClient";
import { noPointerFocus } from "../lib/talkkey";

/** Explicit owner preference action; automatic events never use this route. */
export function BrowserSettings({ blocked }: { blocked: boolean }) {
  useSyncExternalStore(devservers.subscribe, devservers.version);
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const enabled = devservers.enabled;
  const drive = useSyncExternalStore(browserSettings.subscribe,browserSettings.drive);
  useEffect(()=>{void browserSettings.refresh();},[]);
  return <><div className="block">
    <label className="browser-auto-setting">
      <input type="checkbox" data-testid="browser-auto-open" checked={enabled === true}
        disabled={blocked || busy || enabled === null}
        onMouseDown={noPointerFocus}
        onChange={async e => {
          if (blocked || busy || enabled === null) return;
          const value = e.target.checked;
          setBusy(true); setError("");
          try {
            const saved = await api.setBrowserAutoOpen(value);
            if (typeof saved.auto_open !== "boolean") throw Error("The setting could not be read back.");
            devservers.setting(saved.auto_open);
          } catch (e) { setError(e instanceof Error ? e.message : "The setting could not be saved."); }
          finally { setBusy(false); }
        }} />
      Open dev servers automatically
    </label>
    <p className="muted">Local servers can open in a Browser pane while your work stays in place.</p>
    {enabled === null ? <p className="muted">Waiting for the daemon's setting.</p> : null}
    {error ? <p className="err" role="status">{error}</p> : null}
    <label className="browser-auto-setting"><input type="checkbox" data-testid="browser-agents-drive" checked={drive===true} disabled={blocked||busy||drive===null} onMouseDown={noPointerFocus}
      onChange={async e=>{if(!e.nativeEvent.isTrusted||blocked||busy||drive===null)return;const value=e.target.checked;setBusy(true);setError("");
        try{await browserSettings.setDrive(value);}catch(e){setError(e instanceof Error?e.message:"The setting could not be saved.");}finally{setBusy(false);}}} />Agents may drive</label>
    <p className="muted">Allows requested actions in visible Remote panes with their own driving permission.</p>
  </div><BrowserSoundSetting blocked={blocked} /></>;
}
