import { useEffect, useSyncExternalStore } from "react";
import { browserSound } from "../state/browserSoundClient";
import { soundMessage } from "../state/browserSound";
import { noPointerFocus } from "../lib/talkkey";

function useSound() {
  useEffect(browserSound.start, []);
  return useSyncExternalStore(browserSound.subscribe, browserSound.getSnapshot);
}

export function BrowserSoundSetting({ blocked }: { blocked: boolean }) {
  const state = useSound();
  return <div className="block">
    <label className="browser-auto-setting">
      <input type="checkbox" data-testid="browser-sound-setting" checked={state.value?.desired === true}
        disabled={blocked || state.busy || state.status !== "ready"} onMouseDown={noPointerFocus}
        onChange={e => { if (!blocked) void browserSound.change(e.target.checked); }} />
      Sound in Remote Browser pages
    </label>
    <p className="muted">Direct pages use this window's audio. Remote pages apply changes when Browser work is idle.</p>
    <p className={state.error ? "err" : "muted"} role="status" data-testid="browser-sound-status">
      {state.error || soundMessage(state.value)}
    </p>
    {state.status === "error" ? <button type="button" className="quiet" disabled={blocked}
      onClick={() => void browserSound.refresh()}>Retry sound status</button> : null}
  </div>;
}

export function BrowserSpeaker({ direct, blocked, onSettings }: {
  direct: boolean; blocked: boolean; onSettings: () => void;
}) {
  const state = useSound();
  const enabled = state.value?.desired === true;
  const known = state.status === "ready";
  const pending = state.value?.instances.some(row => row.phase !== "ready" || row.applied !== enabled);
  const label = direct ? "Remote sound setting; Direct audio is unchanged. Open Browser settings."
    : `Remote sound ${known ? enabled ? "on" : "off" : "status unavailable"}. Open Browser settings.`;
  return <button type="button" data-testid="browser-sound-open" data-browser-settings
    className={pending ? "browser-speaker warn" : "browser-speaker"} disabled={blocked}
    aria-label={label} title={`${label} ${known ? soundMessage(state.value) : state.error || "Waiting for the daemon."}`} onMouseDown={noPointerFocus}
    onClick={() => { if (!blocked) onSettings(); }}>
    <svg aria-hidden="true" width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M3 7h3l4-4v14l-4-4H3z" />
      {enabled && known ? <path d="M13 6c2 2 2 6 0 8m3-11c4 4 4 10 0 14" /> : <path d="m13 7 5 6m0-6-5 6" />}
    </svg>
  </button>;
}