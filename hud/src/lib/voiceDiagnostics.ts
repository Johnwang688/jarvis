// Window-local metadata only. No audio, transcript, device identity or error text.
export type SuppressionReason = "card" | "picker" | "settings" | "crash" | "speaking" | "no_chat";
export type DropReason = "unclaimed" | "too_short" | "muted" | "self_voice" | "not_owner" | "cancelled" | "empty_transcript" | "stt_failed" | `suppressed:${SuppressionReason}`;
export type ClaimSource = "wake" | "follow_up" | "continuation" | "ptt" | "unclaimed";
export type VoiceOutcome = DropReason | "transcribing" | "review" | "handed_to_chat" | "unplaced";
export type MicCapability = "unknown" | "available" | "unavailable";
export interface VoiceDiagnostic { id: number; at: number; ms: number; source: ClaimSource; detector: "energy"; outcome: VoiceOutcome; latencyMs?: number; /** V2: the clip is still held in memory for Play and Retry. */ kept?: boolean; /** V13: which speech-to-text backend served the clip (known values only, never daemon text). */ backend?: SttBackend; /** V13: the clip was meant for the local engine and the cloud answered it. */ fellBack?: boolean; }
export type SttBackend = "local" | "cloud";
export interface MicSettings { echoCancellation: boolean | "unknown"; noiseSuppression: boolean | "unknown"; autoGainControl: boolean | "unknown"; }
const sources: ClaimSource[] = ["wake", "follow_up", "continuation", "ptt", "unclaimed"];
const outcomes: VoiceOutcome[] = ["unclaimed", "too_short", "muted", "self_voice", "not_owner", "cancelled", "empty_transcript", "stt_failed", "transcribing", "review", "handed_to_chat", "unplaced", ...["card", "picker", "settings", "crash", "speaking", "no_chat"].map(x => `suppressed:${x}` as VoiceOutcome)];
const bounded = (n: number, max: number) => Number.isFinite(n) ? Math.max(0, Math.min(max, Math.round(n))) : 0;
export const MIC_CONSTRAINTS = { audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } };
/** Old/limited browsers may reject constraints; preserve the existing audio:true path. */
export async function openMicrophone(get: (constraints: MediaStreamConstraints) => Promise<MediaStream>): Promise<MediaStream> {
  try { return await get(MIC_CONSTRAINTS); }
  catch (error: unknown) {
    const name = error && typeof error === "object" ? (error as { name?: unknown }).name : undefined;
    if (name !== "OverconstrainedError" && name !== "NotSupportedError" && name !== "TypeError") throw error;
    return get({ audio: true });
  }
}
export function normalizeMicSettings(value: unknown): MicSettings {
  const out: MicSettings = { echoCancellation: "unknown", noiseSuppression: "unknown", autoGainControl: "unknown" };
  if (!value || typeof value !== "object" || Array.isArray(value)) return out;
  for (const key of Object.keys(out) as (keyof MicSettings)[]) {
    try { const v = (value as Record<string, unknown>)[key]; if (typeof v === "boolean") out[key] = v; } catch { /* unsupported readback */ }
  }
  return out;
}
export class VoiceDiagnostics {
  private next = 0;
  private listeners = new Set<() => void>();
  private rows: readonly VoiceDiagnostic[] = [];
  private value = { rows: this.rows, mic: "unknown" as MicCapability, settings: normalizeMicSettings(null) };
  constructor(private now: () => number = () => performance.now()) {}
  subscribe = (fn: () => void) => { this.listeners.add(fn); return () => { this.listeners.delete(fn); }; };
  snapshot = () => this.value;
  private publish() { this.value = { ...this.value, rows: this.rows }; for (const fn of this.listeners) { try { fn(); } catch { /* observations cannot affect capture */ } } }
  microphone(capability: MicCapability, settings?: unknown) {
    this.value = { ...this.value, mic: ["unknown", "available", "unavailable"].includes(capability) ? capability : "unknown", settings: normalizeMicSettings(settings) };
    this.publish();
  }
  add(ms: number, source: ClaimSource, outcome: VoiceOutcome): number {
    const id = ++this.next;
    this.rows = [...this.rows, { id, at: bounded(this.now(), Number.MAX_SAFE_INTEGER), ms: bounded(ms, 900_000), source: sources.includes(source) ? source : "unclaimed", detector: "energy" as const, outcome: outcomes.includes(outcome) ? outcome : "unclaimed" }].slice(-50);
    this.publish(); return id;
  }
  finish(id: number, outcome: VoiceOutcome, latencyMs: number) {
    if (!outcomes.includes(outcome) || outcome === "transcribing") return;
    const row = this.rows.find(x => x.id === id);
    if (!row || row.outcome !== "transcribing") return;
    this.rows = this.rows.map(x => x.id === id ? { ...x, outcome, latencyMs: bounded(latencyMs, 3_600_000) } : x);
    this.publish();
  }
  /** V13: which backend served the clip. Anything but "local"/"cloud" is ignored. */
  served(id: number, backend: unknown, fellBack = false) {
    if (backend !== "local" && backend !== "cloud") return;
    const row = this.rows.find(x => x.id === id);
    if (!row) return;
    this.rows = this.rows.map(x => x.id === id ? { ...x, backend, fellBack: fellBack === true } : x);
    this.publish();
  }
  /** An explicit Retry: the row is in flight again, so it can settle again. */
  reopen(id: number) {
    const row = this.rows.find(x => x.id === id);
    if (!row || row.outcome !== "stt_failed") return;
    this.rows = this.rows.map(x => x.id === id ? { id: x.id, at: x.at, ms: x.ms, source: x.source, detector: x.detector, outcome: "transcribing" as const, kept: x.kept } : x);
    this.publish();
  }
  /** Whether a clip's audio is still held (memory only). Never audio itself. */
  keep(id: number, kept: boolean) {
    const row = this.rows.find(x => x.id === id);
    if (!row || (row.kept ?? false) === kept) return;
    this.rows = this.rows.map(x => x.id === id ? { ...x, kept } : x);
    this.publish();
  }
}
