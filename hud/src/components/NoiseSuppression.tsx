// Settings -> Voice: noise suppression for the speech detector (V14, Q16). Off by default.
import { useSyncExternalStore } from "react";
import type { VoiceDiagnostics } from "../lib/voiceDiagnostics";

export function NoiseSuppressionSetting(props: { on: boolean; onChange: (on: boolean) => void; diagnostics: VoiceDiagnostics }) {
  const v = useSyncExternalStore(props.diagnostics.subscribe, props.diagnostics.snapshot);
  return (
    <section className="noise-suppression" data-testid="noise-suppression" aria-label="Noise suppression">
      <h3>Noise suppression</h3>
      <label>
        <input
          type="checkbox"
          data-testid="noise-suppression-toggle"
          checked={props.on}
          onChange={(e) => props.onChange(e.target.checked)}
        />{" "}
        Clean the microphone signal before the speech detector listens to it
      </label>
      <p data-testid="noise-suppression-state" data-state={v.noiseState}>
        {v.noiseState === "ready" ? "Running." : v.noiseState === "loading" ? "Loading…"
          : v.noiseState === "failed" ? `Not running: ${v.noiseNote}. The detector is listening to the raw signal.`
          : v.noiseState === "closed" ? "Off (mic closed): the switch is on and takes effect when the microphone opens."
          : v.noiseState === "waiting" ? "On, waiting for the Silero VAD." : "Off: nothing is loaded."}
      </p>
      <p>
        Off by default. It helps the detector ignore steady noise such as a fan, hiss or hum. It does not
        help against other voices, a TV or music. It affects only what the detector scores: what is recorded
        and sent for transcription stays exactly as the microphone captured it. It costs about 3% of one
        processor core and makes the detector&apos;s view about 30 ms (one frame) late. It works without a reload.
      </p>
    </section>
  );
}
