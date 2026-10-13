import { useState, useSyncExternalStore } from "react";
import { api } from "../api";
import { BrowserSoundSetting } from "./BrowserSound";
import { devservers } from "../state/devserverClient";
import { noPointerFocus } from "../lib/talkkey";

/** Explicit owner preference action; automatic events never use this route. */
export function BrowserSettings({ blocked }: { blocked: boolean }) {
  useSyncExternalStore(devservers.subscribe, devservers.version);
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const enabled = devservers.enabled;
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
  </div><BrowserSoundSetting blocked={blocked} /></>;
}
