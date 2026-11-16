import { useEffect, useState, useSyncExternalStore } from "react";
import type { Capture } from "../lib/capture";
import type { VoiceDiagnostics } from "../lib/voiceDiagnostics";
import { CONTINUE_MS, SEGMENT_GAP_MS } from "../lib/vad";
import { playClip, type SttAssembler, type SttCapture } from "../lib/stt";
import { api } from "../api";

import { STAGES, stageDurations, summarize, type Marks, type Stage } from "../lib/voiceTimings";

const labels: Record<Stage | "total", string> = { endpoint: "end", transcribed: "stt", sent: "send", firstToken: "token", firstAudio: "audio", total: "total" };
const seconds = (ms: number) => (ms / 1000).toFixed(2);
function receipt(marks: Marks): string {
  const d = stageDurations(marks);
  const parts = STAGES.flatMap(stage => d[stage] === undefined ? [] : [`${labels[stage]} ${seconds(d[stage])}`]);
  return parts.length ? `${parts.join(" · ")} = ${seconds(d.total!)} s` : "";
}

const noSubscribe = () => () => {};
const emptyClips = { clips: [], pending: 0 };
const noClips = () => emptyClips;

const cleanupPasses = ["off", "rules", "raw", "model"] as const;
type CleanupCounts = Partial<Record<typeof cleanupPasses[number], { typed: number; scratched: number }>>;
function cleanupCounts(raw: unknown): CleanupCounts {
  const counts: CleanupCounts = {};
  if (!raw || typeof raw !== "object") return counts;
  for (const pass of cleanupPasses) {
    const row = (raw as Record<string, unknown>)[pass];
    if (!row || typeof row !== "object") continue;
    const { typed, scratched } = row as Record<string, unknown>;
    if (Number.isSafeInteger(typed) && Number.isSafeInteger(scratched) && (typed as number) >= 0 && (scratched as number) >= 0)
      counts[pass] = { typed: typed as number, scratched: scratched as number };
  }
  return counts;
}

export function VoiceDiagnosticsPanel({ diagnostics, capture, muted, stt, onRetryDetector, onRetryNoise }: { diagnostics: VoiceDiagnostics; capture: Capture; muted: boolean; stt?: SttAssembler<SttCapture>; onRetryDetector?: () => void; onRetryNoise?: () => void }) {
  const value = useSyncExternalStore(diagnostics.subscribe, diagnostics.snapshot);
  const stats = summarize(value.rows.map(row => row.marks ?? {}));
  const clips = useSyncExternalStore(stt?.subscribe ?? noSubscribe, stt?.snapshot ?? noClips).clips;
  const [meter, setMeter] = useState({ rms: 0, floor: 0, pause: capture.endPauseMs });
  const [cleanup, setCleanup] = useState<CleanupCounts>({});
  useEffect(() => {
    let live = true;
    const refresh = () => { void api.dictationCleanupCounts().then(raw => {
      if (live) setCleanup(cleanupCounts(raw));
    }).catch(() => {}); };
    refresh();
    const timer = setInterval(refresh, 5000);
    return () => { live = false; clearInterval(timer); };
  }, []);
  useEffect(() => {
    const timer = setInterval(() => setMeter({ rms: capture.vad.rms, floor: capture.vad.floor, pause: capture.endPauseMs }), 250);
    return () => clearInterval(timer);
  }, [capture]);
  const setting = (v: boolean | "unknown") => v === "unknown" ? "not reported" : v ? "on" : "off";
  return <section className="voice-diagnostics" data-testid="voice-diagnostics" aria-label="Microphone diagnostics">
    <h3>Microphone diagnostics</h3>
    <div data-testid="dictation-cleanup-counts" aria-label="Dictation cleanup counts">
      <h3>Dictation cleanup</h3>
      <p>Counts since the daemon started.</p>
      {cleanupPasses.map(pass => {
        const row = cleanup[pass];
        return row ? <p key={pass}>{pass} · typed {row.typed} · scratched {row.scratched} · scratch rate {row.typed ? (100 * row.scratched / row.typed).toFixed(1) : "0.0"}%</p> : null;
      })}
    </div>
    <p>Kept in this window only. Reload clears the last 50 utterances and any audio held for Retry.</p>
    <p data-testid="voice-mic-capability">Microphone: {value.mic === "unavailable" ? "unavailable — check the browser microphone permission" : value.mic === "unknown" ? "not checked" : "available"} · {muted ? "MIC MUTED" : "mic live"}</p>
    <p data-testid="voice-detector" data-detector={value.detector} data-phase={value.detectorPhase}>
      Speech detector: {value.detector === "silero"
        ? `Silero VAD (ML) · ${value.detectorMs ? value.detectorMs.toFixed(1) : "0.0"} ms per 32 ms frame`
        : `energy fallback — ${value.detectorNote || "not started"}`}
      {value.detectorPhase === "failed" && onRetryDetector ? <> <button type="button" data-testid="voice-detector-retry" onClick={onRetryDetector}>Try the Silero VAD again</button></> : null}
    </p>
    <p data-testid="voice-noise-suppression" data-state={value.noiseState} data-wanted={value.noiseWanted ? "1" : "0"}>
      Noise suppression: {value.noiseState === "ready"
        ? `on (RNNoise) · ${value.noiseMs ? value.noiseMs.toFixed(1) : "0.0"} ms per 32 ms frame · the detector hears the denoised signal, recordings stay raw`
        : value.noiseState === "loading" ? "on, loading"
        : value.noiseState === "closed" ? "off (mic closed)"
        : value.noiseState === "waiting" ? "on, waiting for the Silero VAD (the energy detector hears the raw signal)"
        : value.noiseState === "failed" ? `off, fell back to the raw signal — ${value.noiseNote}`
        : "off"}
      {value.noiseState === "failed" && onRetryNoise ? <> <button type="button" data-testid="voice-noise-retry" onClick={onRetryNoise}>Try noise suppression again</button></> : null}
    </p>
    {value.detectorLog.length > 0 ? <details data-testid="voice-detector-log">
      <summary>Detector changes ({value.detectorLog.length})</summary>
      <ol>{value.detectorLog.map((c, i) => <li key={i}>{(c.at / 1000).toFixed(1)} s after the window opened · {c.detector === "silero" ? "Silero" : "energy"} · {c.phase} · {c.note || "—"}</li>)}</ol>
    </details> : null}
    <p data-testid="voice-end-pause">Message ends after {(meter.pause / 1000).toFixed(1)} s of quiet · segments are cut at {(SEGMENT_GAP_MS / 1000).toFixed(1)} s gaps · speech within {(CONTINUE_MS / 1000).toFixed(0)} s of a message continues it</p>
    <p data-testid="voice-capture-path">Capture: {value.path === "worklet" ? "AudioWorklet · 16 kHz mono, 512-sample frames" : value.path === "script_processor" ? `older ScriptProcessor path — ${value.pathNote || "AudioWorklet unavailable"}` : "not started"}</p>
    <p data-testid="voice-live-level">Level {muted ? "0.0000" : Number.isFinite(meter.rms) ? meter.rms.toFixed(4) : "—"} · Noise floor {Number.isFinite(meter.floor) ? meter.floor.toFixed(4) : "—"}</p>
    <p data-testid="voice-mic-settings">Echo cancellation: {setting(value.settings.echoCancellation)} · Noise suppression: {setting(value.settings.noiseSuppression)} · Automatic gain: {setting(value.settings.autoGainControl)}</p>
    <p>Requested: echo cancellation, noise suppression and automatic gain. Values above are the browser's readback.</p>
    {Object.keys(stats).length ? <div data-testid="voice-timing-summary" aria-label="Voice latency summary">
      <p>Voice latency · seconds · last 50 utterances</p>
      {([...STAGES, "total"] as const).map(stage => {
        const stat = stats[stage];
        return stat ? <p key={stage}>{labels[stage]} · p50 {seconds(stat.p50)} s · p90 {seconds(stat.p90)} s · n={stat.n}</p> : null;
      })}
    </div> : null}
    {!value.rows.length ? <p>No utterances yet.</p> : <ol className="voice-diagnostic-rows" aria-label="Recent utterances">
      {[...value.rows].reverse().map(row => <li key={row.id} data-testid="voice-diagnostic-row">
        #{row.id} · {row.source.replace("_", " ")} · {(row.ms / 1000).toFixed(2)} s · {row.outcome.replaceAll("_", " ")}{row.latencyMs !== undefined ? ` · ${(row.latencyMs / 1000).toFixed(2)} s to outcome` : ""}
        {row.marks && receipt(row.marks) ? <span data-testid="voice-timing-receipt"> · {receipt(row.marks)}</span> : null}
        {row.backend ? ` · ${row.backend}${row.fellBack ? " (local failed)" : ""}` : ""}
        {row.kept ? " · audio kept" : row.outcome === "stt_failed" ? " · audio not kept" : ""}
        {row.verify === "skipped_guest" ? " · verification skipped (guest voice)" : row.verify === "skipped_unenrolled" ? " · verification skipped (no voice enrolled)" : row.verify === "matched" ? " · voice matched" : row.verify === "no_speech" ? " · too little speech to check" : ""}
        {row.rejected ? ` · ${row.rejected} part${row.rejected === 1 ? "" : "s"} not your voice` : ""}
        {clips.filter(c => c.diag === row.id).map(c => <span key={c.id}>{" "}
          <button type="button" data-testid="voice-clip-play" onClick={() => { const b = stt?.clip(c.id); if (b) void playClip(b).catch(() => {}); }}>Play</button>
          {c.state === "failed" ? <> <button type="button" data-testid="voice-clip-retry" onClick={() => stt?.retry(c.id)}>Retry</button></> : null}
        </span>)}
      </li>)}
    </ol>}
  </section>;
}
