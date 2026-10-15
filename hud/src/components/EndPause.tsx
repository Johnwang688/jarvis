// Settings -> Voice: how long a pause ends a message (V5, decision V-1).
import { END_PAUSE_CHOICES } from "../lib/voiceTiming";

export function EndPauseSetting(props: { ms: number; onChange: (ms: number) => void }) {
  return (
    <section className="end-pause" data-testid="end-pause" aria-label="Message pause">
      <h3>Pause that ends a message</h3>
      <label>
        Send after{" "}
        <select data-testid="end-pause-select" value={props.ms} onChange={(e) => props.onChange(Number(e.target.value))}>
          {!END_PAUSE_CHOICES.includes(props.ms) ? <option value={props.ms}>{(props.ms / 1000).toFixed(1)} s</option> : null}
          {END_PAUSE_CHOICES.map((ms) => <option key={ms} value={ms}>{(ms / 1000).toFixed(1)} s of quiet</option>)}
        </select>
      </label>
      <p>
        A longer pause lets you stop and think mid-sentence; a shorter one sends sooner. Speech that starts again
        within six seconds after a message went out continues it. Takes effect at once, even in a message already being said. The choices run from 1.5 to 4 s; the detector accepts 1 to 6 s.
      </p>
    </section>
  );
}
