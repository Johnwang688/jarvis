// The phone's half of the unlock window (WS-5c, plan C §5 "PX-5"; ws5 plan §5.3, §5.3a, §5.7).
//
// One passkey assertion opens a sliding hour on the server (`remote_auth.require_unlocked`). Every
// gated action (Allow, Always, a deferral's Approve and retry, cancel, every PR action, Approve
// selected, Done and Undo) carries a server-issued op token; nothing here authorizes anything.
//
// The nine rules, as built:
//
//  1. **The pool, and freezing at the tap.** Up to 4 tokens from `GET /m/api/ops/tokens`, refilled
//     when one is used, when the oldest passes half its life, when the app becomes visible and after
//     a 410. A tap takes one (no round trip before Face ID) and freezes `{method, URL, body + op}`
//     into a `FrozenOp`: a detached, frozen copy. Nothing replaces it: not an unlock, a refill, the
//     stream or navigation. An empty pool sends nothing ("Not ready: try again in a moment").
//  2. **The mirror.** A client copy of the window (`me.unlock_s`), for the pill and the path only;
//     it never authorizes. A gated 2xx slides it; a session-only 2xx slides it only while open.
//  3. **Locked: one gesture.** unlock/options, Face ID, unlock, then the frozen op. No action POST
//     goes first.
//  4. **A 423** while the mirror said open: ONE unlock, then ONE resend of the same frozen op.
//  5. **No automatic retry** after a cancelled Face ID, an unlock 409 or 503, a second 423, an
//     ambiguous result, or a 410 ("This tap expired: nothing was done. Tap again.").
//  6. **The fallback.** Face ID refused because the tap's activation lapsed: "Unlock and send", which
//     resumes the SAME frozen op and token through rule 3.
//  7. **Outcome recovery.** After a duplicate, a lost answer or an uncertain one, `GET /m/api/ops/<n>`
//     and one total table of fixed words (`opWords`); never `prRow.resultOk`. A tap shown as
//     uncertain stays so across a later 410 on the same control: each uncertain op's nonce is kept
//     until ITS OWN status settles it (a later op's 2xx on the control never clears it).
//  8. **Always** freezes `{decision: "always", preview, op}` (app.ts and card.ts).
//  9. **A 401 is signed out** (the `me` probe), never locked.

import { OFFLINE_STATUSES, type Got, type Posted } from "./net";

type Json = Record<string, unknown>;
const isObj = (v: unknown): v is Json => !!v && typeof v === "object" && !Array.isArray(v);

export const POOL_MAX = 4;
/** `remote_auth.UNLOCK_S`: a gated 2xx slides the window this far. */
export const UNLOCK_S = 3600;
export const TOKENS_URL = "/m/api/ops/tokens";
export const UNLOCK_OPTIONS_URL = "/m/api/unlock/options";
export const UNLOCK_URL = "/m/api/unlock";
export const LOCK_URL = "/m/api/lock";
export const ME_URL = "/m/api/me";
export const opStatusUrl = (n: string): string => `/m/api/ops/${n}`;
/** `remote_auth.OP_TTL_S`, until the issue route says otherwise. */
export const DEFAULT_TTL_S = 600;
/** A token this close to its end is not handed out: Face ID and the trip could outlive it. */
export const TAKE_MARGIN_MS = 30_000;
const TOKEN_RE = /^o1\.([A-Za-z0-9_-]{1,400})\.[A-Za-z0-9_-]{43}$/;
const NONCE_RE = /^[A-Za-z0-9_-]{22}$/;
/** The longest the flow waits on one request to Jarvis (an action, an unlock step, an op status read). Past it,
 * the request's fate is unknown: an action goes to op status (never resent), a status read is "Unknown". */
export const ANSWER_WAIT_MS = 30_000;
/** A request that did not answer within `ANSWER_WAIT_MS` (or threw). */
const SLOW: unique symbol = Symbol("slow");
/** What a 502 or 504 means on a POST: the proxy answered, so the request's fate is unknown. */
const PROXY_DOWN: ReadonlySet<number> = new Set([502, 504]);

/** The nonce `n` an op token carries (its status is looked up by it), or null. */
export function nonceOf(token: string): string | null {
  const m = TOKEN_RE.exec(token);
  if (!m) return null;
  try {
    let t = m[1].replace(/-/g, "+").replace(/_/g, "/");
    while (t.length % 4) t += "=";
    const payload = JSON.parse(atob(t)) as unknown;
    const n = isObj(payload) ? payload.n : null;
    return typeof n === "string" && NONCE_RE.test(n) ? n : null;
  } catch {
    return null;
  }
}

function deepFreeze<T>(v: T): T {
  if (v && typeof v === "object") {
    for (const k of Object.keys(v as object)) deepFreeze((v as Record<string, unknown>)[k]);
    Object.freeze(v);
  }
  return v;
}

// -- the pool (rule 1) --------------------------------------------------------------------------

export interface Timer { set: (fn: () => void, ms: number) => unknown; clear: (h: unknown) => void }

export const realTimer: Timer = {
  set: (fn, ms) => setTimeout(fn, ms),
  clear: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
};

export class TokenPool {
  /** Oldest first; each with when it arrived (the phone's clock). */
  private tokens: { token: string; at: number }[] = [];
  ttlMs = DEFAULT_TTL_S * 1000;
  /** How many issue reads were made (the tests count them). */
  reads = 0;
  private inflight: Promise<void> | null = null;
  private half: unknown = null;

  constructor(private readonly deps: { fetchJson: (path: string) => Promise<Got>; now: () => number; timer: Timer;
                                       signedOut: () => void }) {}

  /** The usable tokens now. */
  get size(): number {
    const now = this.deps.now();
    return this.tokens.filter((t) => now - t.at < this.ttlMs - TAKE_MARGIN_MS).length;
  }

  /** The oldest usable token, taken out of the pool, or null (empty: the tap sends nothing). Always refills. */
  take(): string | null {
    const now = this.deps.now();
    this.tokens = this.tokens.filter((t) => now - t.at < this.ttlMs - TAKE_MARGIN_MS);
    const first = this.tokens.shift() ?? null;
    void this.refill();
    return first ? first.token : null;
  }

  /** One issue read at a time; a second asker shares it. The newest 4 tokens are kept. */
  refill(): Promise<void> {
    if (this.inflight) return this.inflight;
    const run = (async () => {
      this.reads += 1;
      const got = await this.deps.fetchJson(TOKENS_URL);
      if (!got.ok) {
        if (got.why === "signed-out") this.deps.signedOut();
        return;
      }
      const body = isObj(got.body) ? got.body : {};
      const list = Array.isArray(body.tokens) ? body.tokens.filter((t): t is string => typeof t === "string" && TOKEN_RE.test(t)) : [];
      const ttl = typeof body.ttl_s === "number" && body.ttl_s > 0 && body.ttl_s <= 86_400 ? body.ttl_s : DEFAULT_TTL_S;
      if (!list.length) return;
      this.ttlMs = ttl * 1000;
      const at = this.deps.now();
      const held = new Set(this.tokens.map((t) => t.token));
      const fresh = [...new Set(list)].filter((token) => !held.has(token)).map((token) => ({ token, at }));
      this.tokens = [...this.tokens, ...fresh].slice(-POOL_MAX);
      // Refill again when these pass half their life (the oldest is now one of them).
      if (this.half !== null) this.deps.timer.clear(this.half);
      this.half = this.deps.timer.set(() => { this.half = null; void this.refill(); }, this.ttlMs / 2);
    })();
    this.inflight = run.finally(() => { this.inflight = null; });
    return this.inflight;
  }

  stop(): void {
    if (this.half !== null) this.deps.timer.clear(this.half);
    this.half = null;
  }
}

// -- the mirror (rule 2) ------------------------------------------------------------------------

/** The phone's copy of its window: display and path choice only. It never authorizes. */
export class Mirror {
  /** Until when (the phone's clock, ms) it believes the window is open; 0 = locked. */
  until = 0;
  constructor(private readonly now: () => number) {}

  open(): boolean { return this.now() < this.until; }
  /** `me.unlock_s`: whole seconds left (0 = locked). Anything else changes nothing. */
  fromMe(unlockS: unknown): void {
    if (typeof unlockS !== "number" || !Number.isFinite(unlockS) || unlockS < 0) return;
    this.until = unlockS > 0 ? this.now() + unlockS * 1000 : 0;
  }
  /** The unlock answered `{unlocked: true, unlock_s}`. */
  opened(unlockS: unknown): void {
    const s = typeof unlockS === "number" && unlockS > 0 && unlockS <= UNLOCK_S ? unlockS : UNLOCK_S;
    this.until = this.now() + s * 1000;
  }
  /** A gated action's 2xx: the server's `require_unlocked` slid it (so it was open), from when the
   * request was SENT (`at`, the phone's clock), never later than that; an hour already further out stays. */
  slideGated(at: number = this.now()): void { this.until = Math.max(this.until, at + UNLOCK_S * 1000); }
  /** A session-only action's 2xx (`touch_unlock`): slides an open window, never opens one. */
  slideSession(): void { if (this.open()) this.until = this.now() + UNLOCK_S * 1000; }
  lock(): void { this.until = 0; }
}

/** When the window ends, as hours and minutes ("14:32"; with the date only when not today). */
export function untilWords(m: Mirror, now: number): string {
  const at = new Date(m.until);
  const clock = at.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return at.toDateString() === new Date(now).toDateString() ? clock
    : `${at.toLocaleDateString([], { month: "short", day: "numeric" })} ${clock}`;
}

/** The lock pill's words, short enough for the header at 375 px: "Locked", or "Unlocked · 14:32". */
export function pillWords(m: Mirror, now: number): string {
  return m.open() ? `Unlocked · ${untilWords(m, now)}` : "Locked";
}

/** What the pill says to VoiceOver: "Locked", or "Unlocked until 14:32". */
export function pillLabel(m: Mirror, now: number): string {
  return m.open() ? `Unlocked until ${untilWords(m, now)}` : "Locked";
}

// -- the frozen request -------------------------------------------------------------------------

/** One gated tap: method, URL, body and op token, frozen together at the tap. */
export interface FrozenOp {
  readonly method: "POST";
  readonly url: string;
  readonly body: Readonly<Json>;
  readonly token: string;
  /** The token's nonce: what op status is read by. */
  readonly n: string | null;
  /** The control it came from ("approval:<row>:allow"): what a 410's words and the fallback follow. */
  readonly key: string;
}

// -- op status and its words (rule 7) -----------------------------------------------------------

export interface OpStatus { state: string; outcome: string | null; code: string | null }

export function parseOpStatus(raw: unknown): OpStatus | null {
  if (!isObj(raw) || typeof raw.state !== "string") return null;
  return { state: raw.state, outcome: typeof raw.outcome === "string" ? raw.outcome : null,
    code: typeof raw.code === "string" ? raw.code : null };
}

/** Every code WS-5c records (`remote_auth.OP_CODES` plus `other`), in fixed words. Total: an unknown code is "other". */
export const CODE_WORDS: Readonly<Record<string, string>> = Object.freeze({
  merged: "merged",
  updated: "updated",
  update_requested: "update requested",
  not_updated: "not updated",
  not_read_again: "not read again",
  changed: "the pull request changed",
  queued: "queued to merge",
  head_moved: "the head moved",
  closed: "closed",
  not_mergeable: "not mergeable",
  allowed: "allowed",
  always_written: "allowed, with the rule written",
  cancelled: "cancelled",
  done: "marked done",
  undone: "undone",
  approved: "approved",
  other: "no detail",
});

export function codeWords(code: string | null): string {
  if (code === null) return "";
  return Object.prototype.hasOwnProperty.call(CODE_WORDS, code) ? CODE_WORDS[code] : CODE_WORDS.other;
}

/**
 * What the phone shows for an op's recorded state. "Done" ONLY for completed + ok and never for an
 * Update GitHub only accepted (Sol r6-1); a status that cannot be read is "Unknown: check at the desk".
 */
export function opWords(s: OpStatus | null): string {
  if (!s) return "Unknown: check at the desk";
  if (s.state === "running") return "Still running";
  if (s.state === "refused" || (s.state === "completed" && s.outcome === "failed")) {
    const w = s.code && s.code !== "other" ? codeWords(s.code) : "";
    return w ? `Not done: ${w}` : "Not done";
  }
  if (s.state === "completed" && s.outcome === "ok") {
    return s.code === "update_requested" ? "Update requested; check at the desk" : "Done";
  }
  if (s.state === "completed") return "Finished; check at the desk";
  return "Unknown: check at the desk";
}

/** A status that settles what happened (done, or definitely not done). */
export function definite(s: OpStatus | null): boolean {
  return !!s && (s.state === "refused" || (s.state === "completed" && (s.outcome === "ok" || s.outcome === "failed")));
}

// -- outcomes -----------------------------------------------------------------------------------

export type Outcome =
  /** The action answered 2xx: the caller says what it did. */
  | { kind: "done"; status: number; body: Json }
  /** A definite refusal (400, 404, 409, 429, 503 ...): the caller says the daemon's words. */
  | { kind: "refused"; status: number; body: Json }
  /** Face ID was cancelled or did not verify. `sent`: the action POST went first and was refused (a 423). */
  | { kind: "cancelled"; sent?: true }
  /** The tap's activation lapsed before Face ID: "Unlock and send" resumes the same op. */
  | { kind: "needs-tap" }
  /** The unlock was refused (409, 503, 401 with the session still on ...): the action did not run. */
  | { kind: "unlock-refused"; status: number; body: Json; sent?: true }
  /** The unlock had no answer: the action did not run (the window itself may have opened: `me` is read again). */
  | { kind: "unlock-lost"; sent?: true }
  /** 410: the token expired or predates a restart. */
  | { kind: "expired" }
  /** A second 423. */
  | { kind: "locked-again" }
  | { kind: "signed-out" }
  /** After a duplicate, a lost answer or an uncertain one: what op status says. `relocked`: the duplicate
   * answered this op's own resend after a 423 (a late 423 can refuse an op after admission, Opus L3). */
  | { kind: "recovered"; why: "duplicate" | "lost" | "uncertain"; status: OpStatus | null; relocked?: true };

export type Stage = "unlocking" | "sending";

export interface UnlockDeps {
  fetchJson: (path: string) => Promise<Got>;
  postJson: (path: string, value: unknown) => Promise<Posted>;
  /** The passkey: the request options in, the assertion's JSON out; null or a throw is a no. */
  getCredential: (publicKey: Record<string, unknown>) => Promise<Record<string, unknown> | null>;
  /** Whether the tap's user activation still holds (`navigator.userActivation.isActive`); null when unknown. */
  activation?: () => boolean | null;
  now: () => number;
  timer?: Timer;
  signedOut: () => void;
  /** A POST reached Jarvis (true) or did not (false), with its HTTP status (0 if no response). */
  reached?: (ok: boolean, status: number) => void;
  /** The mirror changed (the pill redraws). */
  changed?: () => void;
  /** Read `me` again (the app's probe): when local state cannot be trusted about the window (Opus L1).
   * Absent, `me` is read here and the mirror follows it. */
  reread?: () => void;
}

/** One op's run: whether an unlock was tried for it, how often its action POST went, and whether a 423 answered it. */
interface RunState { unlockTried: boolean; sends: number; after423: boolean }

export class Unlock {
  readonly pool: TokenPool;
  readonly mirror: Mirror;
  private unlocking: Promise<true | Outcome> | null = null;
  private readonly runs = new WeakMap<FrozenOp, { state: RunState; promise: Promise<Outcome> | null }>();
  /** Rule 7: control -> the ids (nonces) of its ops whose outcome is still unknown. Each stays until op
   * status for THAT op settles it (Sol LOW 2); another op's answer on the control never clears it. */
  readonly unsure = new Map<string, Set<string>>();
  /** Bumped by a Lock now 200 and by every 423: a 2xx to a request sent before the bump does not slide (Opus L2). */
  private lockEpoch = 0;
  /** Bumped by every unlock that opened the window: a 423 to a request sent before one is stale news (Opus L1). */
  private unlocks = 0;

  private readonly timer: Timer;

  constructor(private readonly deps: UnlockDeps) {
    const timer = deps.timer ?? realTimer;
    this.timer = timer;
    this.pool = new TokenPool({ fetchJson: deps.fetchJson, now: deps.now, timer, signedOut: deps.signedOut });
    this.mirror = new Mirror(deps.now);
  }

  /** A request, bounded (Sol r1 LOW 2): its answer, or SLOW once `ANSWER_WAIT_MS` passes (or it throws). A late
   * answer after that is dropped: the flow has already moved on to recovery, and nothing is ever resent. */
  private bounded<T>(request: Promise<T>): Promise<T | typeof SLOW> {
    return new Promise((resolve) => {
      let done = false;
      const finish = (v: T | typeof SLOW) => { if (!done) { done = true; this.timer.clear(h); resolve(v); } };
      const h = this.timer.set(() => finish(SLOW), ANSWER_WAIT_MS);
      request.then((v) => finish(v), () => finish(SLOW));
    });
  }

  /** The tap: a token from the pool, frozen with the URL and a detached copy of the body. Null: the pool is empty. */
  freeze(key: string, url: string, fields: Json): FrozenOp | null {
    const token = this.pool.take();
    if (!token) return null;
    const body = JSON.parse(JSON.stringify(fields)) as Json;
    body.op = token;
    return Object.freeze({ method: "POST" as const, url, body: deepFreeze(body), token, n: nonceOf(token), key });
  }

  /** The op, once: locked → unlock first (rule 3); open → send, and on a 423 one unlock and one resend (rule 4). */
  run(op: FrozenOp, stage?: (s: Stage) => void): Promise<Outcome> {
    const held = this.entry(op);
    if (held.promise) return held.promise;
    held.promise = this.mirror.open() ? this.send(op, held.state, stage) : this.locked(op, held.state, stage);
    return held.promise;
  }

  /** "Unlock and send" (rule 6): the same op and token, through the locked path; never an action POST first. */
  resume(op: FrozenOp, stage?: (s: Stage) => void): Promise<Outcome> {
    const held = this.entry(op);
    held.promise = this.locked(op, held.state, stage);
    return held.promise;
  }

  /** How many times this op's action POST was sent (the tests read it). */
  sends(op: FrozenOp): number { return this.entry(op).state.sends; }

  private entry(op: FrozenOp) {
    let held = this.runs.get(op);
    if (!held) { held = { state: { unlockTried: false, sends: 0, after423: false }, promise: null }; this.runs.set(op, held); }
    return held;
  }

  private reached(result: Posted | typeof SLOW): void {
    const status = result === SLOW ? 0 : result.status;
    this.deps.reached?.(status !== 0 && !OFFLINE_STATUSES.has(status), status);
  }
  private changed(): void { this.deps.changed?.(); }

  /** `me` decides the mirror (Opus L1): after a lost unlock, a lost action, or a 423 older than an unlock. */
  private reread(): void {
    if (this.deps.reread) { this.deps.reread(); return; }
    void this.deps.fetchJson(ME_URL).then((me) => { if (me.ok) this.fromMe(me.body); });
  }

  /** An unlock's failure, for THIS op: whether its action POST already went (and was refused with a 423). */
  private forOp(u: Outcome, st: RunState): Outcome {
    if (u.kind !== "cancelled" && u.kind !== "unlock-lost" && u.kind !== "unlock-refused") return u;
    const rest = { ...u };
    delete rest.sent;
    return st.sends > 0 ? { ...rest, sent: true } : rest;
  }

  private async locked(op: FrozenOp, st: RunState, stage?: (s: Stage) => void): Promise<Outcome> {
    const u = await this.unlock(st, stage);
    if (u !== true) return this.forOp(u, st);
    return this.send(op, st, stage);
  }

  private async send(op: FrozenOp, st: RunState, stage?: (s: Stage) => void): Promise<Outcome> {
    stage?.("sending");
    st.sends += 1;
    // What the window looked like when this request left: its answer is judged against that, not later state.
    const epoch = this.lockEpoch;
    const unlocks = this.unlocks;
    const sentAt = this.deps.now();
    const got = await this.bounded(this.deps.postJson(op.url, op.body));
    this.reached(got);
    if (got === SLOW || "offline" in got || PROXY_DOWN.has(got.status)) {
      this.reread();
      return this.recover(op, "lost");
    }
    const { status, body } = got;
    if (status >= 200 && status < 300) {
      // A Lock now (or a 423) since it was sent: this late answer does not slide the window back open.
      if (this.lockEpoch === epoch) {
        this.mirror.slideGated(sentAt);
        this.changed();
      }
      return { kind: "done", status, body };
    }
    if (status === 423) {
      // The mirror was wrong (Lock now elsewhere, the desk, a restart): it is locked.
      this.lockEpoch += 1;
      this.mirror.lock();
      this.changed();
      // An unlock finished while this request was out: the 423 may predate it, so `me` says what holds.
      if (this.unlocks !== unlocks) this.reread();
      st.after423 = true;
      if (st.unlockTried || st.sends > 1) return { kind: "locked-again" };
      const u = await this.unlock(st, stage);
      if (u !== true) return this.forOp(u, st);
      return this.send(op, st, stage);
    }
    if (status === 410) {
      void this.pool.refill();
      // A 410 is definite for this tap; an earlier unknown tap on the control is looked at again first.
      await this.settle(op.key);
      return { kind: "expired" };
    }
    if (status === 409 && body.duplicate === true) return this.recover(op, "duplicate", st.after423);
    if (body.uncertain === true) return this.recover(op, "uncertain");
    if (status === 401) return this.unauthorized(status, body, "refused");
    return { kind: "refused", status, body };
  }

  /** A 401 (rule 9): the `me` probe decides. Signed out, or the words of a refusal; never "locked". */
  private async unauthorized(status: number, body: Json, as: "refused" | "unlock-refused"): Promise<Outcome> {
    const me = await this.bounded(this.deps.fetchJson(ME_URL));
    if (me !== SLOW && !me.ok && me.why === "signed-out") {
      this.deps.signedOut();
      return { kind: "signed-out" };
    }
    return { kind: as, status, body };
  }

  /** One unlock at a time: a second op waits for the one under way and shares its result. */
  private unlock(st: RunState, stage?: (s: Stage) => void): Promise<true | Outcome> {
    if (this.unlocking) {
      return this.unlocking.then((r) => {
        if (r === true) st.unlockTried = true;
        return r;
      });
    }
    const p = this.doUnlock(st, stage);
    this.unlocking = p;
    return p.finally(() => { if (this.unlocking === p) this.unlocking = null; });
  }

  private async doUnlock(st: RunState, stage?: (s: Stage) => void): Promise<true | Outcome> {
    stage?.("unlocking");
    const opts = await this.bounded(this.deps.postJson(UNLOCK_OPTIONS_URL, {}));
    this.reached(opts);
    if (opts === SLOW || "offline" in opts || PROXY_DOWN.has(opts.status)) { this.reread(); return { kind: "unlock-lost" }; }
    if (opts.status === 401) return this.unauthorized(401, opts.body, "unlock-refused");
    const publicKey = opts.body.publicKey;
    if (opts.status !== 200 || !isObj(publicKey)) return { kind: "unlock-refused", status: opts.status, body: opts.body };
    const active = this.deps.activation ? this.deps.activation() : null;
    let assertion: Record<string, unknown> | null = null;
    try {
      assertion = await this.deps.getCredential(publicKey);
    } catch {
      assertion = null;
    }
    if (!assertion) {
      // iOS refuses `credentials.get` once the tap's activation has lapsed: a fresh tap resumes (rule 6).
      if (active === false) return { kind: "needs-tap" };
      st.unlockTried = true;
      return { kind: "cancelled" };
    }
    st.unlockTried = true;
    const got = await this.bounded(this.deps.postJson(UNLOCK_URL, { assertion }));
    this.reached(got);
    // The assertion may have verified and opened the window with only its answer lost: `me` decides the pill.
    if (got === SLOW || "offline" in got || PROXY_DOWN.has(got.status)) { this.reread(); return { kind: "unlock-lost" }; }
    if (got.status === 200 && got.body.unlocked === true) {
      this.unlocks += 1;
      this.mirror.opened(got.body.unlock_s);
      this.changed();
      return true;
    }
    if (got.status === 401) return this.unauthorized(401, got.body, "unlock-refused");
    return { kind: "unlock-refused", status: got.status, body: got.body };
  }

  /** Rule 7: what op status says, never success by default and never a new token. */
  private async recover(op: FrozenOp, why: "duplicate" | "lost" | "uncertain", relocked = false): Promise<Outcome> {
    let status: OpStatus | null = null;
    if (op.n) {
      // A status read that never answers is "Unknown: check at the desk" after the bound, never a wait forever.
      const got = await this.bounded(this.deps.fetchJson(opStatusUrl(op.n)));
      if (got !== SLOW && !got.ok && got.why === "signed-out") { this.deps.signedOut(); return { kind: "signed-out" }; }
      status = got !== SLOW && got.ok ? parseOpStatus(got.body) : null;
    }
    const id = op.n ?? op.token;
    if (definite(status)) this.forget(op.key, id); else this.remember(op.key, id);
    return relocked ? { kind: "recovered", why, status, relocked: true } : { kind: "recovered", why, status };
  }

  /** Whether a control has an op whose outcome is still unknown. */
  isUnsure(key: string): boolean { return !!this.unsure.get(key)?.size; }

  private remember(key: string, id: string): void {
    const ids = this.unsure.get(key) ?? new Set<string>();
    ids.add(id);
    this.unsure.set(key, ids);
  }

  private forget(key: string, id: string): void {
    const ids = this.unsure.get(key);
    if (!ids) return;
    ids.delete(id);
    if (!ids.size) this.unsure.delete(key);
  }

  /** Rule 7: each still-unknown op on the control, read again; only its own definite status settles it. */
  private async settle(key: string): Promise<void> {
    const ids = this.unsure.get(key);
    if (!ids) return;
    for (const id of [...ids]) {
      if (!NONCE_RE.test(id)) continue;
      const got = await this.bounded(this.deps.fetchJson(opStatusUrl(id)));
      if (got === SLOW) continue;
      if (!got.ok && got.why === "signed-out") { this.deps.signedOut(); return; }
      if (got.ok && definite(parseOpStatus(got.body))) this.forget(key, id);
    }
  }

  /** Lock now: the window ends on the server; the mirror follows only a 200, and only if no unlock finished
   * while it was out ("overtaken": `me` is read and followed instead, Sol r1 LOW 1). */
  async lockNow(): Promise<"locked" | "overtaken" | "unreached" | "refused" | "signed-out"> {
    const unlocks = this.unlocks;
    const got = await this.bounded(this.deps.postJson(LOCK_URL, {}));
    this.reached(got);
    if (got === SLOW) { this.reread(); return "unreached"; }
    if ("offline" in got || PROXY_DOWN.has(got.status)) { return "unreached"; }
    if (got.status === 200) {
      // A delayed acknowledgement: an unlock completed after this was sent, so the server may be open again
      // (a 423, a fresh unlock, an action that slid it). Its 200 locks nothing here: `me` says what holds.
      if (this.unlocks !== unlocks) {
        this.reread();
        return "overtaken";
      }
      // An action in flight that answers 2xx after this does not slide the pill back open (Opus L2).
      this.lockEpoch += 1;
      this.mirror.lock();
      this.changed();
      return "locked";
    }
    if (got.status === 401) {
      const r = await this.unauthorized(401, got.body, "refused");
      return r.kind === "signed-out" ? "signed-out" : "refused";
    }
    return "refused";
  }

  /** `me` read: its `unlock_s`, for the mirror. */
  fromMe(body: unknown): void {
    if (!isObj(body)) return;
    const before = this.mirror.until;
    this.mirror.fromMe(body.unlock_s);
    if (this.mirror.until !== before) this.changed();
  }

  /** The words for an outcome that is not the action's own answer ("" for done, refused and signed out). */
  words(outcome: Outcome, key?: string): string {
    switch (outcome.kind) {
      case "done":
      case "refused":
      case "signed-out":
        return "";
      case "cancelled":
        return `Cancelled, or Face ID did not verify: ${nothing(outcome.sent)}.`;
      case "needs-tap":
        return "Face ID needs a fresh tap: press Unlock and send.";
      case "unlock-lost":
        return `No answer to the unlock: ${nothing(outcome.sent)}. Tap again.`;
      case "unlock-refused": {
        const n = nothing(outcome.sent);
        if (outcome.status === 409) return `The unlock expired or was replaced: ${n}. Tap again.`;
        if (outcome.status === 503) return `Passkeys are unavailable at Jarvis: ${n}.`;
        if (outcome.status === 401) return `Not accepted: Face ID was not verified. ${n[0].toUpperCase() + n.slice(1)}.`;
        if (outcome.status === 429) return `Too many requests from this phone: ${n}. Wait a minute.`;
        return `Not unlocked (${outcome.status}): ${n}.`;
      }
      case "expired":
        // A 410 means the request reached Jarvis and was refused unrun.
        return key && this.isUnsure(key)
          ? "This tap expired and was refused unrun. The earlier tap is still unknown: check at the desk."
          : "This tap expired: nothing was done. Tap again.";
      case "locked-again":
        return "Locked again before it could be sent: nothing was done. Tap again.";
      case "recovered": {
        // Our own resend after a 423 met its token already used: the 423 refused it after admission.
        if (outcome.why === "duplicate" && outcome.relocked && outcome.status?.state === "refused") {
          return "Locked before it ran: nothing was done. Tap again.";
        }
        const lead = outcome.why === "duplicate" ? "This tap was already sent."
          : outcome.why === "lost" ? "No answer came back." : "Jarvis could not say how it ended.";
        return `${lead} ${opWords(outcome.status)}.`;
      }
    }
  }

  stop(): void { this.pool.stop(); }
}

/** What did not happen: on the 423 path the action POST went and was refused, so "nothing was done". */
function nothing(sent: true | undefined): string {
  return sent ? "nothing was done" : "nothing was sent";
}

/** An empty pool's words (rule 1). */
export const NOT_READY = "Not ready: try again in a moment.";
/** The fallback button's label (rule 6). */
export const UNLOCK_AND_SEND = "Unlock and send";
