// Settings -> Voice: where speech-to-text runs (voice rework V13, answered Q6).
//
// The daemon owns the two switches (`/stt/settings`, owner-only, protected
// state); this module only parses its answer defensively and words what the
// owner is choosing. Nothing here is persisted in the window.

export type SttChoice = "cloud" | "local" | "auto";
export const STT_CHOICES: readonly SttChoice[] = ["cloud", "local", "auto"];

export interface SttLocalStatus {
  installed: boolean;
  /** Why it is not usable, in the daemon's words ("" when installed). */
  reason: string;
  loaded: boolean;
  threads: number;
  idleUnloadMin: number;
  fetch: string;
}
export interface SttSettings {
  backend: SttChoice;
  cloudFallback: boolean;
  /** Non-empty when the daemon's settings file could not be trusted and it failed closed. */
  error: string;
  local: SttLocalStatus;
}

const text = (v: unknown, max = 200) => (typeof v === "string" ? v.slice(0, max) : "");
const count = (v: unknown, fallback: number) => (typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : fallback);

/** Defensive: an odd answer reads as the safest meaning (local only, no upload), never as "upload". */
export function parseSttSettings(raw: unknown): SttSettings {
  const o = raw && typeof raw === "object" && !Array.isArray(raw) ? (raw as Record<string, unknown>) : {};
  const l = o.local && typeof o.local === "object" && !Array.isArray(o.local) ? (o.local as Record<string, unknown>) : {};
  return {
    backend: STT_CHOICES.includes(o.backend as SttChoice) ? (o.backend as SttChoice) : "local",
    cloudFallback: o.cloud_fallback === true,
    error: text(o.error),
    local: {
      installed: l.installed === true,
      reason: text(l.reason),
      loaded: l.loaded === true,
      threads: count(l.threads, 0),
      idleUnloadMin: count(l.idle_unload_min, 0),
      fetch: text(l.fetch, 100),
    },
  };
}

/** One sentence: what happens to the next clip, so the owner never has to infer it from two controls. */
export function sttSummary(s: SttSettings): string {
  const usesLocal = s.backend !== "cloud";
  if (!usesLocal) {
    return s.cloudFallback
      ? "Audio is sent to the cloud speech model (OpenRouter)."
      : "Cloud speech-to-text is off and Local is not chosen: dictation will fail until you choose Local or switch cloud fallback on.";
  }
  if (!s.local.installed) {
    return s.cloudFallback
      ? (s.backend === "auto"
        ? "Local speech-to-text is not installed, so audio goes to the cloud."
        : "Local speech-to-text is not installed, so every clip falls back to the cloud.")
      : "Local speech-to-text is not installed and cloud fallback is off: dictation will fail. Nothing is uploaded.";
  }
  return s.cloudFallback
    ? "Audio is transcribed on this computer. If that fails or is too slow, the clip is sent to the cloud."
    : "Audio is transcribed on this computer only. Nothing is uploaded; a local failure shows an error.";
}
