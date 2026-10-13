import { useEffect, useState, useSyncExternalStore } from "react";
import type { Capture } from "../lib/capture";
import type { VoiceDiagnostics } from "../lib/voiceDiagnostics";

export function VoiceDiagnosticsPanel({ diagnostics, capture, muted }: { diagnostics: VoiceDiagnostics; capture: Capture; muted: boolean }) {
  const value = useSyncExternalStore(diagnostics.subscribe, diagnostics.snapshot);
  const [meter, setMeter] = useState({ rms: 0, floor: 0 });
  useEffect(() => {
    const timer = setInterval(() => setMeter({ rms: capture.vad.rms, floor: capture.vad.floor }), 250);
    return () => clearInterval(timer);
  }, [capture]);
  const setting = (v: boolean | "unknown") => v === "unknown" ? "not reported" : v ? "on" : "off";
  return <section className="voice-diagnostics" data-testid="voice-diagnostics" aria-label="Microphone diagnostics">
    <h3>Microphone diagnostics</h3>
    <p>Kept in this window only. Reload clears the last 50 utterances.</p>
    <p data-testid="voice-mic-capability">Microphone: {value.mic === "unavailable" ? "unavailable — check the browser microphone permission" : value.mic === "unknown" ? "not checked" : "available"} · {muted ? "MIC MUTED · " : ""}Detector: energy</p>
    <p data-testid="voice-live-level">Level {muted ? "0.0000" : Number.isFinite(meter.rms) ? meter.rms.toFixed(4) : "—"} · Noise floor {Number.isFinite(meter.floor) ? meter.floor.toFixed(4) : "—"}</p>
    <p data-testid="voice-mic-settings">Echo cancellation: {setting(value.settings.echoCancellation)} · Noise suppression: {setting(value.settings.noiseSuppression)} · Automatic gain: {setting(value.settings.autoGainControl)}</p>
    <p>Requested: echo cancellation, noise suppression and automatic gain. Values above are the browser's readback.</p>
    {!value.rows.length ? <p>No utterances yet.</p> : <ol className="voice-diagnostic-rows" aria-label="Recent utterances">
      {[...value.rows].reverse().map(row => <li key={row.id} data-testid="voice-diagnostic-row">
        #{row.id} · {row.source.replace("_", " ")} · {(row.ms / 1000).toFixed(2)} s · {row.outcome.replaceAll("_", " ")}{row.latencyMs !== undefined ? ` · ${(row.latencyMs / 1000).toFixed(2)} s to outcome` : ""}
      </li>)}
    </ol>}
  </section>;
}
