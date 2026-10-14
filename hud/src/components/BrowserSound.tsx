import { useEffect, useRef, useSyncExternalStore } from "react";
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
      Allow Browser sound
    </label>
    <p className="muted">Remote pages stay muted until you press their speaker. Sound applies to every tab in that profile and clears when its last tab closes or Jarvis restarts. Direct pages use this window's audio after a page click.</p>
    <p className={state.error ? "err" : "muted"} role="status" data-testid="browser-sound-status">
      {state.error || soundMessage(state.value)}
    </p>
    {state.status === "error" ? <button type="button" className="quiet" disabled={blocked}
      onClick={() => void browserSound.refresh()}>Retry sound status</button> : null}
  </div>;
}

export function speakerGesture(trusted: boolean, blocked: boolean): boolean {
  return trusted && !blocked;
}

export function BrowserSpeaker({ direct, blocked, tabId, profile, profileLabel, inputHeld }: {
  direct: boolean; blocked: boolean; tabId?: string; profile?: string; profileLabel?: string; inputHeld?: () => boolean;
}) {
  const state = useSound();
  const instance = state.value?.instances.find(row => row.profile === profile);
  const pointer = useRef(false), fence = useRef("");
  const identity = [direct, blocked, !!inputHeld?.(), tabId, profile, state.status, state.busy,
    state.value?.desired, state.value?.generation, instance?.generation, instance?.phase].join("|");
  if (fence.current !== identity) { pointer.current = false; fence.current = identity; }
  useEffect(() => {
    pointer.current = false;
    if (direct) return;
    // A dismissed options gesture never reaches this button's pointerdown.
    // Reset before every target so an earlier press cannot admit its click.
    const reset = () => { pointer.current = false; };
    document.addEventListener("pointerdown", reset, true);
    // Connection/status changes revoke a press synchronously, including a
    // disconnect/reconnect that React batches into the same rendered state.
    const statusKey = () => {
      const snapshot = browserSound.getSnapshot();
      const row = snapshot.value?.instances.find(item => item.profile === profile);
      return [snapshot.status, snapshot.busy, snapshot.value?.desired, snapshot.value?.available,
        snapshot.value?.generation, row?.generation, row?.phase, row?.requested, row?.applied, row?.reason].join("|");
    };
    let previous = statusKey();
    const unsubscribe = browserSound.subscribe(() => {
      const next = statusKey();
      if (next !== previous) reset();
      previous = next;
    });
    return () => { pointer.current = false; unsubscribe(); document.removeEventListener("pointerdown", reset, true); };
  }, [direct, profile]);
  if (direct) return <span className="small muted" title="Direct audio follows this window's browser policy">Sound after a page click</span>;
  if (state.value?.desired === false) return null;
  const enabled = instance?.requested === true;
  const known = state.status === "ready" && !!instance;
  const privateOff = profile === "private" && !enabled;
  const disabled = blocked || !known || state.busy || !tabId || privateOff;
  const name = profileLabel || "this profile";
  const label = privateOff ? "Close Private panes to allow sound"
    : (enabled ? "Mute sound for " : "Turn on sound for ") + name;
  const scope = "All Remote tabs in this profile share sound. New tabs inherit it; changing sites does not mute it. Hidden panes keep playing.";
  return <button type="button" data-testid="browser-sound-toggle" data-browser-sound
    className={instance?.phase !== "ready" ? "browser-speaker warn" : "browser-speaker"} disabled={disabled}
    aria-label={label} aria-pressed={enabled} title={label + ". " + scope} onMouseDown={noPointerFocus}
    onPointerDown={e => { pointer.current = speakerGesture(e.isTrusted, !!disabled || !!inputHeld?.()); }}
    onClick={e => {
      const admitted = e.detail === 0 || pointer.current;
      pointer.current = false;
      if (admitted && speakerGesture(e.isTrusted, !!disabled || !!inputHeld?.()) && tabId) void browserSound.toggle(tabId, !enabled);
    }}>
    <svg aria-hidden="true" width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M3 7h3l4-4v14l-4-4H3z" />
      {enabled && known ? <path d="M13 6c2 2 2 6 0 8m3-11c4 4 4 10 0 14" /> : <path d="m13 7 5 6m0-6-5 6" />}
    </svg>
    <span>{enabled ? "Sound on for " + name : privateOff ? "Close Private panes to allow sound" : "Muted"}</span>
  </button>;
}
