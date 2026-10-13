/** BR10 owner sound state. Desired preference and actual instance modes stay separate. */
export type SoundPhase = "ready" | "pending" | "restarting" | "failed";
export type SoundReason = "busy" | "private" | "unavailable" | "mute_unavailable" | "mute_uncertain" | "restart_failed";
export interface SoundInstance {
  profile: string; applied: boolean; generation: number; phase: SoundPhase; reason: SoundReason | null;
}
export interface SoundStatus {
  desired: boolean; generation: number; available: boolean; reason: "unavailable" | null;
  instances: readonly SoundInstance[];
}
export interface SoundState {
  status: "loading" | "ready" | "error" | "offline";
  value: SoundStatus | null; busy: boolean; error: string;
}
export const SOUND_READ_ERROR = "The sound setting could not be read. Retry to check its current state.";
export const SOUND_WRITE_ERROR = "The sound setting could not be confirmed. Retry to check its current state.";
const phases = new Set(["ready", "pending", "restarting", "failed"]);
const reasons = new Set(["busy", "private", "unavailable", "mute_unavailable", "mute_uncertain", "restart_failed"]);
const count = (v: unknown): v is number => typeof v === "number" && Number.isSafeInteger(v) && v >= 0;
const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);

export function soundStatus(raw: unknown): SoundStatus | null {
  if (!record(raw) || !record(raw.browser)) return null;
  const body = raw.browser, sound = body.sound;
  if (!record(sound) || typeof sound.desired !== "boolean" || typeof sound.available !== "boolean"
      || !count(sound.generation) || (sound.reason !== null && sound.reason !== "unavailable")
      || !Array.isArray(body.instances) || body.instances.length > 9) return null;
  const instances: SoundInstance[] = [], seen = new Set<string>();
  for (const row of body.instances) {
    if (!record(row) || typeof row.profile !== "string" || !/^(private|p-[0-9a-f]{8})$/.test(row.profile)
        || seen.has(row.profile) || !record(row.sound)) return null;
    const state = row.sound;
    if (typeof state.applied !== "boolean" || !count(state.generation) || state.generation < 1
        || typeof state.phase !== "string" || !phases.has(state.phase)
        || (state.reason !== null && (typeof state.reason !== "string" || !reasons.has(state.reason)))) return null;
    seen.add(row.profile);
    instances.push({ profile: row.profile, applied: state.applied, generation: state.generation,
      phase: state.phase as SoundPhase, reason: state.reason as SoundReason | null });
  }
  return { desired: sound.desired, generation: sound.generation, available: sound.available,
    reason: sound.reason as "unavailable" | null, instances };
}

export function soundMessage(value: SoundStatus | null): string {
  if (!value) return "Waiting for the daemon's sound setting.";
  if (value.instances.some(row => row.reason === "private")) return "Close all Private panes to apply sound.";
  if (value.instances.some(row => row.phase === "failed")) return "A Browser profile could not restart. Retry from its Remote pane.";
  if (value.desired && (!value.available || value.reason === "unavailable")) return "Sound isn't available on this machine yet.";
  if (value.instances.some(row => row.phase === "restarting")) return "Restarting Remote Browser pages to apply sound…";
  if (value.instances.some(row => row.phase === "pending" || row.applied !== value.desired))
    return "Sound will change when current Browser work finishes.";
  return value.desired ? "Remote Browser sound is on." : "Remote Browser sound is off.";
}

interface Transport { status(): Promise<unknown>; set(desired: boolean): Promise<unknown> }
export class BrowserSoundStore {
  private state: SoundState = { status: "loading", value: null, busy: false, error: "" };
  private listeners = new Set<() => void>();
  private epoch = 0;
  private started = false;
  private online = true;
  private dirty = false;
  constructor(private transport: Transport) {}
  getSnapshot = () => this.state;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  private publish(next: SoundState) { this.state = next; for (const listener of this.listeners) listener(); }
  start = () => { if (!this.started) { this.started = true; void this.refresh(); } };
  event = (event: unknown) => {
    if (!record(event)) return;
    if (event.kind === "_disconnected") {
      this.online = false; this.dirty = false; ++this.epoch;
      this.publish({ ...this.state, status: "offline", busy: false, error: "The daemon is disconnected." });
    } else if (event.kind === "_connected") {
      this.online = true;
      if (this.started) void this.refresh();
    } else if (event.kind === "browser_sound_changed" && record(event.data) && count(event.data.generation)) {
      if (this.started && this.online) {
        if (this.state.busy) this.dirty = true;
        else void this.refresh();
      }
    }
  };
  refresh = async () => {
    if (!this.online) return;
    if (this.state.busy) { this.dirty = true; return; }
    const epoch = ++this.epoch;
    this.dirty = false;
    this.publish({ ...this.state, status: "loading", error: "" });
    try {
      const value = soundStatus(await this.transport.status());
      if (epoch !== this.epoch) return;
      if (!value) throw Error(SOUND_READ_ERROR);
      this.publish({ status: "ready", value, busy: false, error: "" });
    } catch {
      if (epoch === this.epoch) this.publish({ ...this.state, status: "error", busy: false, error: SOUND_READ_ERROR });
    }
  };
  change = async (desired: boolean) => {
    if (!this.online || this.state.status !== "ready" || this.state.busy || typeof desired !== "boolean") return;
    const epoch = ++this.epoch;
    this.publish({ ...this.state, busy: true, error: "" });
    try {
      const saved = await this.transport.set(desired);
      if (epoch !== this.epoch) return;
      if (!record(saved) || typeof saved.sound !== "boolean") throw Error(SOUND_WRITE_ERROR);
      // The PATCH acknowledges persistence; only status says what is applied.
      const value = soundStatus(await this.transport.status());
      if (epoch !== this.epoch) return;
      if (!value) throw Error(SOUND_WRITE_ERROR);
      this.publish({ status: "ready", value, busy: false, error: "" });
    } catch {
      if (epoch === this.epoch) this.publish({ ...this.state, status: "error", busy: false, error: SOUND_WRITE_ERROR });
    } finally {
      if (epoch === this.epoch && this.dirty && this.state.status === "ready") {
        this.dirty = false; void this.refresh();
      }
    }
  };
}