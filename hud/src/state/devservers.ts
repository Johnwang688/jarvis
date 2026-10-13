// Window-local claims for automatic Direct tabs. URLs come only from the
// owner registry or the current terminal socket; bus records carry no URLs.
// This module never imports the Remote browser manager.
import { originOf } from "../lib/browser";
import { WAIT_MAX, WAIT_MS, type DevEntry, type DevSource } from "../lib/devopen";
import { isWindowId } from "../lib/windowId";

const MAX = 32, REMEMBER = 64, RENEW_MS = 30_000;
export const devId = (v: unknown): v is string => typeof v === "string" && /^ds_[0-9a-f]{8}$/.test(v);
const obj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const boundedId = (v: unknown): v is string => typeof v === "string" && !!v && v.length <= 200;
const urlText = (v: unknown): v is string => typeof v === "string" && !!v && v.length <= 2048;
export function parseDevSource(raw: unknown): DevSource | null {
  if (!obj(raw)) return null;
  if (raw.kind === "terminal" && boundedId(raw.terminal_id)) return { kind: "terminal", id: raw.terminal_id };
  if (raw.kind === "thread" && boundedId(raw.thread_id)) {
    if (raw.task_id !== null && raw.task_id !== undefined && !boundedId(raw.task_id)) return null;
    return raw.task_id ? { kind: "task", id: raw.task_id as string } : { kind: "thread", id: raw.thread_id };
  }
  return null;
}
export type ServerState = "up" | "claimed" | "placed" | "dismissed" | "down";
interface EventRow { id: string; state: ServerState; source: DevSource }
export function parseDevEvent(raw: unknown): EventRow | null {
  if (!obj(raw) || !devId(raw.id) || !["up", "claimed", "placed", "dismissed", "down"].includes(String(raw.state))) return null;
  const source = parseDevSource(raw.source);
  return source ? { id: raw.id, state: raw.state as ServerState, source } : null;
}
interface Listed extends EventRow { url: string; mine: boolean; claimed: boolean }
function parseListing(raw: unknown): { enabled: boolean; rows: Listed[] } | null {
  if (!obj(raw) || typeof raw.auto_open !== "boolean" || !Array.isArray(raw.servers) || raw.servers.length > MAX) return null;
  const rows: Listed[] = [];
  for (const item of raw.servers) {
    const parsed = parseDevEvent(item);
    if (!parsed || !obj(item) || !urlText(item.url) || typeof item.mine !== "boolean" || typeof item.claimed !== "boolean") continue;
    if (rows.some(row => row.id === parsed.id)) continue;
    rows.push({ ...parsed, url: item.url, mine: item.mine, claimed: item.claimed });
  }
  return { enabled: raw.auto_open, rows };
}

export type DevPhase = "notice" | "claiming" | "queued" | "prompt" | "placing" | "lost";
export interface PendingDev extends DevEntry { phase: DevPhase; token: number; renewedAt: number }
export interface DevTransport {
  list(window: string): Promise<unknown>;
  claim(id: string, body: { window: string; shows: boolean; focused_at: number }): Promise<unknown>;
  act(id: string, action: "renew" | "placed" | "dismiss", window: string): Promise<unknown>;
}
export type DevAttempt = "wait" | "prompt" | "placed" | "drop";

/** The App supplies fresh drawn-source/focus data, never stale closure snapshots. */
export class DevServerStore {
  private entries = new Map<string, PendingDev>();
  private meta = new Map<string, EventRow>();
  private remembered: string[] = [];
  private listeners = new Set<() => void>();
  private epoch = 0;
  private token = 0;
  private revision = 0;
  private listingSeq = 0;
  private live = new Set<string>();
  private inFlight = new Set<string>();
  private timer: ReturnType<typeof setInterval> | null = null;
  private acting = false;
  connected = false;
  enabled: boolean | null = null;
  focusedAt = 0;
  shows: (source: DevSource) => boolean = () => false;
  constructor(readonly window: string, private wire: DevTransport, private now: () => number = Date.now) {}
  subscribe = (fn: () => void) => { this.listeners.add(fn); return () => { this.listeners.delete(fn); }; };
  version = () => this.revision;
  rows = (): readonly PendingDev[] => [...this.entries.values()];
  private changed() { this.revision++; for (const fn of this.listeners) fn(); }
  wake = () => this.changed();
  focus(at = this.now()) { this.focusedAt = Math.max(0, Math.floor(at)); }
  private remember(id: string) { this.remembered = [...this.remembered.filter(x => x !== id), id].slice(-REMEMBER); }
  private drop(id: string, remember = false) {
    this.entries.delete(id); this.meta.delete(id); this.live.delete(id);
    if (remember) this.remember(id);
  }
  private current(epoch: number, row: PendingDev): boolean {
    return this.connected && this.enabled === true && this.epoch === epoch && this.entries.get(row.id)?.token === row.token;
  }
  private put(row: Omit<PendingDev, "token">): PendingDev {
    const next = { ...row, token: ++this.token };
    this.entries.set(row.id, next);
    while (this.entries.size > MAX) this.drop(this.entries.keys().next().value!);
    this.cap();
    return next;
  }
  private cap() {
    const held = [...this.entries.values()].filter(e => ["queued", "prompt"].includes(e.phase));
    while (held.length > WAIT_MAX) this.drop(held.shift()!.id, true);
  }
  connect() {
    if (!isWindowId(this.window)) return;
    this.disconnect();
    this.connected = true; this.enabled = null;
    this.timer = setInterval(() => void this.tick(), 1000);
    this.changed();
    void this.refresh();
  }
  disconnect() {
    this.epoch++; this.connected = false; this.enabled = null; this.acting = false;
    this.entries.clear(); this.meta.clear(); this.live.clear(); this.inFlight.clear();
    if (this.timer) clearInterval(this.timer);
    this.timer = null; this.changed();
  }
  setting(value: boolean) {
    if (value === this.enabled) return;
    this.epoch++; this.enabled = value; this.acting = false; this.inFlight.clear();
    this.entries.clear(); this.meta.clear(); this.live.clear();
    this.changed();
    if (value && this.connected) void this.refresh();
  }
  async refresh() {
    if (!this.connected) return;
    const epoch = this.epoch, seq = ++this.listingSeq;
    let data: unknown;
    try { data = await this.wire.list(this.window); } catch { return; }
    if (!this.connected || epoch !== this.epoch || seq !== this.listingSeq) return;
    const list = parseListing(data);
    if (!list) return;
    if (!list.enabled) { this.setting(false); return; }
    // Listing is also the effective preference bootstrap: no per-event
    // /browser/settings read is ever necessary.
    this.enabled = true;
    const present = new Set(list.rows.map(e => e.id));
    for (const row of this.entries.values()) {
      if (!present.has(row.id) && row.phase !== "claiming" && !this.live.has(row.id)) this.drop(row.id);
    }
    for (const row of list.rows) {
      this.meta.set(row.id, row);
      if (this.remembered.includes(row.id)) continue;
      const old = this.entries.get(row.id);
      if (old?.phase === "claiming" || old?.phase === "placing") continue;
      if (row.state === "placed" || row.state === "dismissed" || row.state === "down") { this.drop(row.id, true); continue; }
      if (row.state === "claimed") {
        if (!row.mine) { if (old) this.drop(row.id); continue; }
        if (old && ["queued", "prompt"].includes(old.phase)) continue;
        this.put({ id: row.id, url: row.url, source: row.source, at: this.now(), phase: "queued", renewedAt: 0 });
      } else if (this.live.has(row.id)) {
        if (old && ["queued", "prompt"].includes(old.phase)) this.drop(row.id);
        void this.claim(row.id, row.source);
      } else if (!old) {
        this.put({ id: row.id, url: row.url, source: row.source, at: this.now(), phase: "notice", renewedAt: 0 });
      } else if (["queued", "prompt"].includes(old.phase)) {
        this.drop(row.id);
      }
    }
    // An up frame racing the initial list may not have appeared in its snapshot.
    for (const id of this.live) {
      const meta = this.meta.get(id);
      if (meta?.state === "up" && !this.entries.has(id) && !this.remembered.includes(id)) void this.claim(id, meta.source);
    }
    this.changed();
  }
  event(raw: unknown): boolean {
    if (!obj(raw)) return false;
    if (raw.kind === "dev_server_settings") {
      if (obj(raw.data) && typeof raw.data.auto_open === "boolean") this.setting(raw.data.auto_open);
      return true;
    }
    if (raw.kind !== "dev_server") return false;
    const row = parseDevEvent(raw.data);
    if (!row || !this.connected) return true;
    this.meta.set(row.id, row);
    while (this.meta.size > MAX) {
      const expired = this.meta.keys().next().value!;
      this.meta.delete(expired); this.live.delete(expired);
    }
    if (["down", "dismissed", "placed"].includes(row.state)) {
      const own = this.entries.get(row.id);
      if (row.state !== "placed" || own?.phase !== "placing") this.drop(row.id, row.state !== "down");
      this.changed(); return true;
    }
    if (row.state === "up") {
      this.live.add(row.id);
      if (this.enabled === true && !this.remembered.includes(row.id)) void this.claim(row.id, row.source);
    } else if (this.entries.get(row.id)?.phase !== "claiming") void this.refresh();
    return true;
  }
  async claim(id: string, source?: DevSource) {
    if (!this.connected || this.enabled !== true || this.remembered.includes(id) || this.inFlight.has(id)) return;
    const old = this.entries.get(id);
    if (old && ["claiming", "queued", "prompt", "placing"].includes(old.phase)) return;
    const src = source ?? old?.source ?? this.meta.get(id)?.source;
    if (!src || !devId(id)) return;
    const row = this.put({ id, source: src, url: old?.url ?? "", at: this.now(), phase: "claiming", renewedAt: 0 });
    const epoch = this.epoch;
    this.inFlight.add(id); this.changed();
    try {
      const reply = await this.wire.claim(id, { window: this.window, shows: this.shows(src), focused_at: this.focusedAt });
      if (!this.current(epoch, row)) return;
      if (obj(reply) && reply.won === true && urlText(reply.url)) {
        row.url = reply.url; row.phase = "queued"; row.renewedAt = this.now(); this.cap();
      } else if (old?.phase === "notice") { row.phase = "lost"; }
      else this.drop(id);
    } catch {
      if (this.current(epoch, row)) this.drop(id);
    } finally {
      if (epoch === this.epoch) this.inFlight.delete(id);
      this.changed();
    }
  }
  /** ID-bearing current-socket frames are already claimed by the daemon. */
  terminal(terminalId: string, url: string, id?: string) {
    if (!this.connected || this.enabled === false || !boundedId(terminalId) || !urlText(url)) return;
    if (!id) {
      // A legacy frame is never proof of ownership; owner listing + normal arbitration.
      void this.legacyTerminal(terminalId, url);
      return;
    }
    if (!devId(id) || this.remembered.includes(id)) return;
    const old = this.entries.get(id);
    if (old && ["queued", "prompt", "placing"].includes(old.phase)) return;
    this.put({ id, url, source: { kind: "terminal", id: terminalId }, at: this.now(), phase: "queued", renewedAt: 0 });
    this.changed();
  }
  private async legacyTerminal(terminalId: string, url: string) {
    const epoch = this.epoch;
    await this.refresh();
    if (epoch !== this.epoch || !this.connected || this.enabled !== true) return;
    const row = [...this.entries.values()].find(e => e.phase === "notice" && e.source.kind === "terminal"
      && e.source.id === terminalId && originOf(e.url) === originOf(url));
    if (row) void this.claim(row.id);
  }
  private async renew(row: PendingDev, epoch: number): Promise<boolean> {
    try {
      await this.wire.act(row.id, "renew", this.window);
      if (!this.current(epoch, row)) return false;
      row.renewedAt = this.now(); return true;
    } catch {
      if (this.current(epoch, row)) { this.drop(row.id); this.changed(); }
      return false;
    }
  }
  /** Only one fresh ownership check + synchronous workspace decision at a time. */
  async attempt(action: (row: PendingDev) => DevAttempt, selected?: string) {
    if (this.acting || !this.connected || this.enabled !== true) return;
    const row = selected ? this.entries.get(selected) : [...this.entries.values()].find(e => e.phase === "queued");
    if (!row || !["queued", "prompt"].includes(row.phase)) return;
    const epoch = this.epoch;
    this.acting = true;
    try {
      if (this.now() - row.at >= WAIT_MS) { this.drop(row.id, true); this.changed(); return; }
      if (!await this.renew(row, epoch) || !this.current(epoch, row)) return;
      // No await between this fence, the App's final eligibility check, and mutation.
      const result = action(row);
      if (!this.current(epoch, row)) return;
      if (result === "wait") return;
      if (result === "prompt") { row.phase = "prompt"; this.changed(); return; }
      if (result === "drop") { this.drop(row.id, true); this.changed(); return; }
      row.phase = "placing"; this.remember(row.id); this.changed();
      try { await this.wire.act(row.id, "placed", this.window); }
      catch { if (epoch === this.epoch) void this.refresh(); }
      if (this.current(epoch, row)) this.drop(row.id, true);
      this.changed();
    } finally { if (epoch === this.epoch) this.acting = false; }
  }
  async dismiss(id: string) {
    const row = this.entries.get(id);
    if (!row || !this.connected) return;
    const epoch = this.epoch;
    try { await this.wire.act(id, "dismiss", this.window); }
    catch { return; }
    if (epoch === this.epoch) { this.drop(id, true); this.changed(); }
  }
  private async tick() {
    if (!this.connected || this.enabled !== true) return;
    const epoch = this.epoch;
    let waiting = false;
    for (const row of [...this.entries.values()]) {
      if (!["queued", "prompt"].includes(row.phase)) continue;
      if (this.now() - row.at >= WAIT_MS) { this.drop(row.id, true); continue; }
      waiting = true;
      if (this.now() - row.renewedAt >= RENEW_MS) await this.renew(row, epoch);
      if (epoch !== this.epoch) return;
    }
    if (waiting) this.changed();
  }
}
