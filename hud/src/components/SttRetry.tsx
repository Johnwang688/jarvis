import { useSyncExternalStore } from "react";
import type { SttAssembler, SttCapture } from "../lib/stt";

/**
 * V2: a clip that could not be transcribed is kept, in memory, until its text
 * is placed. Retry sends that same clip once more; Discard releases the audio.
 * Neither is ever automatic -- the one automatic retry already happened.
 */
export function SttRetry({ stt, disabled }: { stt: SttAssembler<SttCapture>; disabled: boolean }) {
  const snap = useSyncExternalStore(stt.subscribe, stt.snapshot);
  const failed = snap.clips.filter(c => c.state === "failed");
  if (!failed.length) return null;
  return <div className="block err stt-retry" data-testid="stt-retry" role="alert">
    {failed.map(c => <div key={c.id} className="stt-retry-row" data-testid="stt-retry-row">
      <span>Couldn't transcribe {(c.ms / 1000).toFixed(1)} s of speech{c.of > 1 ? ` (part ${c.part} of ${c.of} of a longer message)` : ""}{c.error ? ` (${c.error})` : ""}. Your audio is kept in this window.</span>
      <span className="row">
        <button type="button" data-testid="stt-retry-button" disabled={disabled} onClick={() => stt.retry(c.id)}>Retry</button>
        <button type="button" data-testid="stt-discard-button" onClick={() => stt.discard(c.id)}>Discard</button>
      </span>
    </div>)}
  </div>;
}
