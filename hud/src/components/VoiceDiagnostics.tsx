import { useEffect, useState, useSyncExternalStore } from "react";
import type { Capture } from "../lib/capture";
import type { VoiceDiagnostics } from "../lib/voiceDiagnostics";
import { playClip, type SttAssembler, type SttCapture } from "../lib/stt";

const noSubscribe = () => () => {};
const emptyClips = { clips: [], pending: 0 };
const noClips = () => emptyClips;

export function VoiceDiagnosticsPanel({ diagnostics, capture, muted, stt }: { diagnostics: VoiceDiagnostics; capture: Capture; muted: boolean; stt?: SttAssembler<SttCapture> }) {
  const value = useSyncExternalStore(diagnostics.subscribe, diagnostics.snapshot);
  const clips = useSyncExternalStore(stt?.subscribe ?? noSubscribe, stt?.snapshot ?? noClips).clips;
  const [meter, setMeter] = useState({ rms: 0, floor: 0 });
  useEffect(() => {
    const timer = setInterval(() => setMeter({ rms: capture.vad.rms, floor: capture.vad.floor }), 250);
    return () => clearInterval(timer);
  }, [capture]);
  const setting = (v: boolean | "unknown") => v === "unknown" ? "not reported" : v ? "on" : "off";
  return <section className="voice-diagnostics" data-testid="voice-diagnostics" aria-label="Microphone diagnostics">
    <h3>Microphone diagnostics</h3>
    <p>Kept in this window only. Reload clears the last 50 utterances and any audio held for Retry.</p>
    <p data-testid="voice-mic-capability">Microphone: {value.mic === "unavailable" ? "unavailable — check the browser microphone permission" : value.mic === "unknown" ? "not checked" : "available"} · {muted ? "MIC MUTED · " : ""}Detector: energy</p>
    <p data-testid="voice-capture-path">Capture: {value.path === "worklet" ? "AudioWorklet · 16 kHz mono, 512-sample frames" : value.path === "script_processor" ? `older ScriptProcessor path — ${value.pathNote || "AudioWorklet unavailable"}` : "not started"}</p>
    <p data-testid="voice-live-level">Level {muted ? "0.0000" : Number.isFinite(meter.rms) ? meter.rms.toFixed(4) : "—"} · Noise floor {Number.isFinite(meter.floor) ? meter.floor.toFixed(4) : "—"}</p>
    <p data-testid="voice-mic-settings">Echo cancellation: {setting(value.settings.echoCancellation)} · Noise suppression: {setting(value.settings.noiseSuppression)} · Automatic gain: {setting(value.settings.autoGainControl)}</p>
    <p>Requested: echo cancellation, noise suppression and automatic gain. Values above are the browser's readback.</p>
    {!value.rows.length ? <p>No utterances yet.</p> : <ol className="voice-diagnostic-rows" aria-label="Recent utterances">
      {[...value.rows].reverse().map(row => <li key={row.id} data-testid="voice-diagnostic-row">
        #{row.id} · {row.source.replace("_", " ")} · {(row.ms / 1000).toFixed(2)} s · {row.outcome.replaceAll("_", " ")}{row.latencyMs !== undefined ? ` · ${(row.latencyMs / 1000).toFixed(2)} s to outcome` : ""}
        {row.kept ? " · audio kept" : row.outcome === "stt_failed" ? " · audio not kept" : ""}
        {clips.filter(c => c.diag === row.id).map(c => <span key={c.id}>{" "}
          <button type="button" data-testid="voice-clip-play" onClick={() => { const b = stt?.clip(c.id); if (b) void playClip(b).catch(() => {}); }}>Play</button>
          {c.state === "failed" ? <> <button type="button" data-testid="voice-clip-retry" onClick={() => stt?.retry(c.id)}>Retry</button></> : null}
        </span>)}
      </li>)}
    </ol>}
  </section>;
}
