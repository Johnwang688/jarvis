import { agentActive, DRIVE_IDLE, driveReducer, isTakeover, parseAgent, type DriveState } from "./drive";

/** Presentation only. A lease belongs to one ticketed WebSocket attachment. */
export interface DriveLease { readonly tabId: string; readonly key: symbol }
export interface DriveSnapshot {
  state: DriveState;
  connected: boolean;
  ready: boolean;
  pending: "stop" | "resume" | null;
}
export const DRIVE_EMPTY: DriveSnapshot = { state: DRIVE_IDLE, connected: false, ready: false, pending: null };
const MAX_TABS = 8 * 12;
interface Record { lease: DriveLease; value: DriveSnapshot; pausedSeen: boolean }

/** No persistence or bus publication: only the current retained Remote tabs. */
export class DriveRegistry {
  private records = new Map<string, Record>();
  private listeners = new Set<() => void>();
  private version = 0;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  revision = () => this.version;
  get = (tabId: string): DriveSnapshot => this.records.get(tabId)?.value ?? DRIVE_EMPTY;
  private changed() { this.version++; for (const listener of this.listeners) listener(); }
  private current(lease: DriveLease): Record | undefined {
    const row = this.records.get(lease.tabId);
    return row?.lease === lease ? row : undefined;
  }
  attach(tabId: string): DriveLease | null {
    if (!/^[0-9a-f]{8}$/.test(tabId) || (!this.records.has(tabId) && this.records.size >= MAX_TABS)) return null;
    const lease = { tabId, key: Symbol() };
    // A new connection must replay; retained text never supplies new authority.
    this.records.set(tabId, { lease, value: DRIVE_EMPTY, pausedSeen: false });
    this.changed(); return lease;
  }
  connected(lease: DriveLease) {
    const row = this.current(lease); if (!row || row.value.connected) return;
    row.value = { ...row.value, connected: true }; this.changed();
  }
  receive(lease: DriveLease, raw: unknown, now: number): boolean {
    const row = this.current(lease); if (!row?.value.connected) return false;
    const msg = parseAgent(raw, now); if (!msg) return false;
    if (msg.kind === "paused") row.pausedSeen = true;
    // The host replays optional driver, paused, then list, in socket order.
    const ready = row.value.ready || (msg.kind === "list" && row.pausedSeen);
    row.value = { ...row.value, ready, state: driveReducer(row.value.state, { type: "agent", msg, now }),
      pending: msg.kind === "paused" ? null : row.value.pending };
    this.changed(); return true;
  }
  owner(lease: DriveLease, message: { t: string; [key: string]: unknown }, now: number) {
    const row = this.current(lease);
    if (!row?.value.connected || !row.value.ready || !isTakeover(message)) return;
    const state = driveReducer(row.value.state, { type: "owner", now });
    if (state === row.value.state) return;
    row.value = { ...row.value, state }; this.changed();
  }
  /** Validate again at the gesture, then send once. A failed send cannot resume. */
  control(lease: DriveLease, action: "stop" | "resume", blocked: boolean, now: number,
    send: (message: { t: "stop" | "resume" }) => boolean): boolean {
    const row = this.current(lease);
    if (!row?.value.connected || !row.value.ready || row.value.pending || blocked) return false;
    if (action === "resume" ? !row.value.state.paused : row.value.state.paused || !agentActive(row.value.state, now)) return false;
    if (!send({ t: action }) || this.current(lease) !== row || !row.value.connected) return false;
    row.value = { ...row.value, pending: action,
      state: action === "stop" ? driveReducer(row.value.state, { type: "stop" }) : row.value.state };
    // Resume waits for the daemon's paused=false reply. No optimistic authority.
    this.changed(); return true;
  }
  detach(lease: DriveLease) {
    const row = this.current(lease); if (!row) return;
    row.value = { ...row.value, connected: false, ready: false, pending: null };
    this.changed();
  }
  retain(tabIds: readonly string[]) {
    const retained = new Set(tabIds); let changed = false;
    for (const id of this.records.keys()) if (!retained.has(id)) { this.records.delete(id); changed = true; }
    if (changed) this.changed();
  }
  active(tabId: string | undefined, now: number): boolean {
    const row = tabId ? this.records.get(tabId) : undefined;
    return !!row?.value.connected && row.value.ready && agentActive(row.value.state, now);
  }
}
