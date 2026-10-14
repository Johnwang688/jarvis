// Speaker verification as this window knows it (voice rework V12, plan 3.4, V-2, V-14).
//
// The DAEMON decides whose voice a clip is: every chunk goes to `/stt?verify=1`
// and a voice that is not the owner's is refused there, before any cloud call.
// This module is the window's copy of the little the screen needs: whether a
// voice is enrolled, whether GUEST VOICE is on, and what Settings shows. The
// Guest switch is held on the daemon (beside the voiceprint, so it survives a
// reload and a restart) and published on the bus as `speaker_guest`, which is how
// every window shows the label at once.
//
// Nothing here touches the microphone, the VAD, noise suppression, the self-voice
// gate or an approval card. Guest voice only changes what the daemon does with a
// clip; `guestLabel` is the only thing the rest of the window reads.

export type SpeakerStateName = "not_enrolled" | "enrolling" | "enrolled" | "stale" | "damaged" | "unsafe";
/** What `/stt?verify=1` does now: nobody enrolled (off), Guest voice, verifying (on), or enrolled but unable to (blocked). */
export type SpeakerMode = "off" | "guest" | "on" | "blocked";

export interface ReadingQuality { minS: number; maxS: number; minLevelDb: number; maxLevelDb: number; maxClip: number; minSpeech: number }

export interface SpeakerStatus {
  state: SpeakerStateName;
  enrolled: boolean;
  readings: number;
  readingsMin: number;
  readingsMax: number;
  prompts: string[];
  quality: ReadingQuality;
  guest: boolean;
  mode: SpeakerMode;
  threshold: number | null;
  thresholdDefault: number;
  thresholdMin: number;
  /** A proposal is never below this; a typed threshold may be (down to `thresholdMin`) with a warning. */
  proposalFloor: number;
  /** The daemon's warning about the saved threshold ("" when none). */
  thresholdWarning: string;
  thresholdMax: number;
  shortClipS: number;
  shortRelax: number;
  installed: boolean;
  reason: string;
  loaded: boolean;
  model: string;
  fetch: string;
  error: string;
  last: { result: string; score: number | null } | null;
}

export const DEFAULT_QUALITY: ReadingQuality = { minS: 6, maxS: 30, minLevelDb: -40, maxLevelDb: -6, maxClip: 0.005, minSpeech: 0.45 };

const STATES: readonly string[] = ["not_enrolled", "enrolling", "enrolled", "stale", "damaged", "unsafe"];
const MODES: readonly string[] = ["off", "guest", "on", "blocked"];

const obj = (v: unknown): Record<string, unknown> | null => (v && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : null);
const num = (v: unknown, d: number) => (typeof v === "number" && Number.isFinite(v) ? v : d);
const str = (v: unknown, d = "", max = 400) => (typeof v === "string" ? v.slice(0, max) : d);

/** The daemon's `GET /speaker`, or null when it is not an object (an older daemon answers 404 and never gets here). */
export function parseSpeakerStatus(raw: unknown): SpeakerStatus | null {
  const r = obj(raw);
  if (!r) return null;
  const q = obj(r.quality) ?? {};
  const state = STATES.includes(r.state as string) ? r.state as SpeakerStateName : "not_enrolled";
  const mode = MODES.includes(r.mode as string) ? r.mode as SpeakerMode : "off";
  const last = obj(r.last);
  return {
    state,
    enrolled: r.enrolled === true,
    readings: Math.max(0, Math.round(num(r.readings, 0))),
    readingsMin: Math.max(1, Math.round(num(r.readings_min, 3))),
    readingsMax: Math.max(1, Math.round(num(r.readings_max, 6))),
    prompts: Array.isArray(r.prompts) ? r.prompts.filter((p): p is string => typeof p === "string").map(p => p.slice(0, 400)).slice(0, 12) : [],
    quality: {
      minS: num(q.min_s, DEFAULT_QUALITY.minS), maxS: num(q.max_s, DEFAULT_QUALITY.maxS),
      minLevelDb: num(q.min_level_db, DEFAULT_QUALITY.minLevelDb), maxLevelDb: num(q.max_level_db, DEFAULT_QUALITY.maxLevelDb),
      maxClip: num(q.max_clip, DEFAULT_QUALITY.maxClip), minSpeech: num(q.min_speech, DEFAULT_QUALITY.minSpeech),
    },
    guest: r.guest === true,
    mode,
    threshold: typeof r.threshold === "number" && Number.isFinite(r.threshold) ? r.threshold : null,
    thresholdDefault: num(r.threshold_default, 0.6),
    thresholdMin: num(r.threshold_min, 0.3),
    proposalFloor: num(r.proposal_floor, 0.5),
    thresholdWarning: str(r.threshold_warning),
    thresholdMax: num(r.threshold_max, 0.9),
    shortClipS: num(r.short_clip_s, 1.2),
    shortRelax: num(r.short_relax, 0.08),
    installed: r.installed === true,
    reason: str(r.reason),
    loaded: r.loaded === true,
    model: str(r.model, "", 80),
    fetch: str(r.fetch, "", 120),
    error: str(r.error),
    last: last ? { result: str(last.result, "", 40), score: typeof last.score === "number" && Number.isFinite(last.score) ? last.score : null } : null,
  };
}

/** What a reading's analysis (`POST /speaker {propose}`) says; the histograms are counts per `bins.width` from `bins.lo`. */
export interface Proposal {
  threshold: number;
  bins: { lo: number; width: number; n: number };
  genuine: { n: number; min: number; p10: number; median: number; hist: number[] };
  impostors: { n: number; max: number | null; median: number | null; hist: number[]; closest: { voice: string; score: number }[] };
  falseReject: number;
  falseAccept: number | null;
  overlap: boolean;
  /** The readings do not tell the owner's voice from the built-in voices clearly: record them again, or accept anyway. */
  confirm: boolean;
  warning: string;
  readings: number;
}

const hist = (v: unknown, n: number): number[] => (Array.isArray(v) ? v.slice(0, n).map(x => Math.max(0, Math.round(num(x, 0)))) : []);

export function parseProposal(raw: unknown): Proposal | null {
  const r = obj(raw);
  const g = r && obj(r.genuine);
  const i = r && obj(r.impostors);
  const b = r && obj(r.bins);
  if (!r || !g || !i || !b || typeof r.threshold !== "number" || !Number.isFinite(r.threshold)) return null;
  const n = Math.max(1, Math.min(64, Math.round(num(b.n, 24))));
  return {
    threshold: r.threshold,
    bins: { lo: num(b.lo, -0.2), width: Math.max(0.001, num(b.width, 0.05)), n },
    genuine: { n: num(g.n, 0), min: num(g.min, 0), p10: num(g.p10, 0), median: num(g.median, 0), hist: hist(g.hist, n) },
    impostors: {
      n: num(i.n, 0), max: typeof i.max === "number" ? i.max : null, median: typeof i.median === "number" ? i.median : null, hist: hist(i.hist, n),
      closest: Array.isArray(i.closest) ? i.closest.slice(0, 3).flatMap(c => {
        const o = obj(c);
        return o && typeof o.voice === "string" && typeof o.score === "number" ? [{ voice: o.voice.slice(0, 60), score: o.score }] : [];
      }) : [],
    },
    falseReject: num(r.false_reject, 0),
    falseAccept: typeof r.false_accept === "number" ? r.false_accept : null,
    overlap: r.overlap === true,
    confirm: r.confirm === true,
    warning: str(r.warning),
    readings: num(r.readings, 0),
  };
}

export interface ReadingResult { accepted: boolean; problems: string[]; readings: number }

export function parseReadingResult(raw: unknown): ReadingResult | null {
  const r = obj(raw);
  if (!r || typeof r.accepted !== "boolean") return null;
  return {
    accepted: r.accepted,
    problems: Array.isArray(r.problems) ? r.problems.filter((p): p is string => typeof p === "string").map(p => p.slice(0, 200)).slice(0, 8) : [],
    readings: Math.max(0, Math.round(num(r.readings, 0))),
  };
}

export interface SpeakerApi {
  status(): Promise<SpeakerStatus | null>;
  guest(on: boolean): Promise<SpeakerStatus | null>;
}

export interface SpeakerView {
  /** Guest voice is on. */
  guest: boolean;
  enrolled: boolean;
  mode: SpeakerMode;
  /** The daemon has answered (a status or an event): until then the mode is a guess and nothing is labelled. */
  known: boolean;
  /** This window has seen a voice enrolled (kept across reloads): the voice check being OFF now is then news, not the default. */
  wasEnrolled: boolean;
  /** The daemon's whole status, once it has answered. */
  status: SpeakerStatus | null;
  /** The last switch the daemon refused, as a sentence. */
  error: string;
  /** The daemon skipped the voice check on a clip this window believed it was checking (M3); cleared when a read agrees. */
  warning: string;
}

/** The labels the orb line and the speech strip carry whenever the voice check is not judging the microphone (plan 3.4.1, M3). */
export const GUEST_LABEL = "GUEST VOICE";
export const OFF_LABEL = "VOICE CHECK OFF (not enrolled)";
export const BLOCKED_LABEL = "VOICE CHECK BLOCKED";
export const guestLabel = (guest: boolean): string => (guest ? GUEST_LABEL : "");

/**
 * What to say under the orb about speaker verification, or "". Guest voice always says so. Verification
 * being off says so once this window has known a voice enrolled (an owner who never enrolled sees the
 * quiet default; one whose voiceprint disappeared does not miss it). Enrolled but unable to verify
 * (nothing is transcribed) always says so.
 */
export function speakerLabel(v: Pick<SpeakerView, "guest" | "mode" | "wasEnrolled"> & { known?: boolean }): string {
  if (v.known === false) return "";
  if (v.guest || v.mode === "guest") return GUEST_LABEL;
  if (v.mode === "blocked") return BLOCKED_LABEL;
  if (v.mode === "off" && v.wasEnrolled) return OFF_LABEL;
  return "";
}

const SEEN_KEY = "jarvis.speaker.seenEnrolled";
const memory = {
  read(): boolean { try { return localStorage.getItem(SEEN_KEY) === "1"; } catch { return false; } },
  write(on: boolean) { try { if (on) localStorage.setItem(SEEN_KEY, "1"); else localStorage.removeItem(SEEN_KEY); } catch { /* per-window convenience only */ } },
};

export class SpeakerStore {
  private view: SpeakerView = { guest: false, enrolled: false, mode: "off", known: false, wasEnrolled: memory.read(), status: null, error: "", warning: "" };
  private listeners = new Set<() => void>();
  /** A change this window asked for that the daemon has not answered: a late reload must not undo it. */
  private pendingGuest: boolean | null = null;

  constructor(private readonly api: SpeakerApi) {}

  subscribe = (fn: () => void) => { this.listeners.add(fn); return () => { this.listeners.delete(fn); }; };
  snapshot = (): SpeakerView => this.view;

  private set(next: Partial<SpeakerView>) {
    if (next.enrolled === true && !this.view.wasEnrolled) { next = { ...next, wasEnrolled: true }; memory.write(true); }
    this.view = { ...this.view, ...next };
    for (const fn of this.listeners) { try { fn(); } catch { /* observers cannot affect speech */ } }
  }

  private take(status: SpeakerStatus) {
    this.set({ status, guest: status.guest, enrolled: status.enrolled, mode: status.mode, known: true, error: "", warning: "" });
  }

  /** Read the daemon's status (on start, on reconnect, and after Settings changes something). */
  async load(): Promise<void> {
    try {
      const status = await this.api.status();
      if (status && this.pendingGuest === null) this.take(status);
    } catch { /* an older daemon, or none yet: no label until it answers */ }
  }

  /** Another window (or this one) changed the gate, or the daemon saw it change: the bus's `speaker_guest`. */
  apply(data: unknown): void {
    const r = obj(data);
    if (!r || typeof r.guest !== "boolean") return;
    const mode = MODES.includes(r.mode as string) ? r.mode as SpeakerMode : this.view.mode;
    this.set({ guest: r.guest, enrolled: r.enrolled === true, mode, known: true, error: "", warning: "" });
    // The detail (counts, threshold) may have moved too; ask, once.
    void this.load();
  }

  /**
   * A transcript's reply said what the daemon did about the voice check. A skip this window did not expect
   * (it believed the check was on) is flagged at once and the truth is read back (M3): the label follows the
   * daemon, not the last click.
   */
  observe(kind: string): void {
    const believed = this.view.mode;
    if (kind === "skipped_guest" && !(this.view.guest || believed === "guest")) {
      this.set({ guest: true, mode: "guest", warning: "The daemon skipped the voice check (Guest voice is on) although this window thought it was checking your voice." });
    } else if (kind === "skipped_unenrolled" && (believed === "on" || believed === "blocked" || this.view.enrolled)) {
      this.set({ enrolled: false, mode: "off", warning: "The daemon skipped the voice check (no voiceprint is saved) although this window thought it was checking your voice." });
    } else return;
    void this.load();
  }

  /** The owner's switch. The daemon's answer is the truth; a refusal leaves the label as it was. */
  async setGuest(on: boolean): Promise<boolean> {
    this.pendingGuest = on;
    try {
      const status = await this.api.guest(on);
      if (status) this.take(status);
      return true;
    } catch (e) {
      this.set({ error: e instanceof Error && e.message ? e.message.slice(0, 200) : "The Guest voice switch could not be changed." });
      return false;
    } finally {
      this.pendingGuest = null;
    }
  }

  /** Settings changed something else (a reading, Accept, Delete): take the answer it was given. */
  adopt(status: SpeakerStatus | null) { if (status) this.take(status); }

  /** The owner deleted the voiceprint on purpose: the voice check being off is the default again, not news. */
  forgetEnrolled() { memory.write(false); this.set({ wasEnrolled: false }); }
}

/** The one sentence Settings shows for the daemon's status (plan 3.4: "Not enrolled: anyone's voice is accepted"). */
export function speakerSummary(s: SpeakerStatus): string {
  if (s.guest) {
    return s.enrolled ? "Guest voice is ON: anyone's voice is accepted until you turn it off."
      : "Guest voice (no voice enrolled): anyone's voice is accepted.";
  }
  switch (s.state) {
    case "not_enrolled": return "Not enrolled: anyone's voice is accepted.";
    case "enrolling": return `Enrolling: ${s.readings} of ${s.readingsMin} readings kept. Anyone's voice is accepted until you accept the voiceprint.`;
    case "enrolled":
      return s.mode === "blocked"
        ? `Enrolled, but verification cannot run (${s.reason || "the speaker model is missing"}). Nothing is transcribed until it can, or you turn on Guest voice or delete the voiceprint.`
        : `Enrolled: only your voice is accepted${s.threshold !== null ? ` (threshold ${s.threshold.toFixed(2)})` : ""}.`;
    case "stale": return "The voiceprint was made by another speaker model, so nothing is transcribed. Rebuild it from your kept readings, or turn on Guest voice.";
    case "damaged": return `The saved voiceprint cannot be used (${s.error || "damaged"}), so nothing is transcribed. Delete it and enroll again, or turn on Guest voice.`;
    case "unsafe": return `The speaker folder is not safe to use (${s.error || "unexpected"}), so nothing is transcribed. Delete it to start again, or turn on Guest voice.`;
  }
}
