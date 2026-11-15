// The orb's one reporting path (net.ts `Reach`, app.ts `report`): EVERY request the phone app makes, run
// through EVERY outcome, must leave the orb where the table says. A new call site that decides "offline"
// on its own (or forgets to) fails here, and the source guard below catches a raw fetch added later.
import { afterEach, describe, expect, it, vi } from "vitest";
import { PhoneApp, type StreamLike } from "./app";
import { getJson, reachOf, reachOfGot, reachOfPosted, type Got, type Posted } from "./net";
import { installUnlock } from "./unlock.fixtures";
import type { InboxRow } from "../lib/inbox";
import { mkRow } from "./threads.fixtures";
import appSource from "./app.ts?raw";
import unlockSource from "./unlock.ts?raw";

const sources = import.meta.glob("./*.ts", { query: "?raw", import: "default", eager: true }) as Record<string, string>;

class Stream implements StreamLike {
  readyState = 0;
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  open() { this.readyState = 1; this.onopen?.(new Event("open")); }
  close() { this.readyState = 2; }
}

const T1 = "aaaaaaaa", TASK = "bbbbbbbb", P1 = "99999999";
const ROW_ID = "appr:r1";
const ok = (body: unknown): Got => ({ ok: true, body });

type Outcome = "2xx" | "401" | "502" | "503" | "504" | "no answer" | "409" | "daemon 503";
const OUTCOMES: Outcome[] = ["2xx", "401", "502", "503", "504", "no answer", "409", "daemon 503"];

/** What the orb must say after each outcome (the app was Live, or Offline for "2xx", before it). */
const EXPECT: Record<Outcome, { state: string; word: RegExp; failStatus?: number | null }> = {
  "2xx": { state: "live", word: /^Live/ },
  "401": { state: "error", word: /^Signed out$/ },
  "502": { state: "error", word: /^Jarvis unreachable$/, failStatus: 502 },
  "503": { state: "error", word: /^Jarvis unreachable$/, failStatus: 503 },
  "504": { state: "error", word: /^Jarvis unreachable$/, failStatus: 504 },
  "no answer": { state: "offline", word: /^Offline/, failStatus: 0 },
  // Jarvis answered and refused: not a connection problem, the orb does not move.
  "409": { state: "live", word: /^Live$/ },
  "daemon 503": { state: "live", word: /^Live$/ },
};

function gotFor(o: Outcome, success: Got): Got {
  switch (o) {
    case "2xx": return success;
    case "401": return { ok: false, why: "signed-out", status: 401 };
    case "502": case "503": case "504": return { ok: false, why: "offline", status: Number(o) };
    case "no answer": return { ok: false, why: "offline", status: 0 };
    case "409": return { ok: false, why: "error", status: 409, body: { error: "conflict" } };
    case "daemon 503": return { ok: false, why: "error", status: 503, body: { error: "the In progress list is not running" } };
  }
}
function postedFor(o: Outcome, success: Posted): Posted {
  switch (o) {
    case "2xx": return success;
    case "401": return { status: 401, body: { error: "unauthorized" } };
    case "502": case "503": case "504": return { status: Number(o), body: {} };
    case "no answer": return { status: 0, body: {}, offline: true };
    case "409": return { status: 409, body: { error: "conflict" } };
    case "daemon 503": return { status: 503, body: { error: "passkeys unavailable" } };
  }
}

const row: InboxRow = mkRow(ROW_ID, 3, { kind: "approval", thread_id: T1 }) as InboxRow;

interface Site {
  name: string;
  kind: "GET" | "POST" | "push" | "clip";
  /** The request under test: the first one at this path (a prefix when it ends "=" or "/n"). */
  target: string;
  success: Got | Posted;
  unlockS?: number;
  setup?: (h: H) => Promise<void> | void;
  trigger: (h: H) => Promise<unknown> | unknown;
}

const SITES: Site[] = [
  { name: "the inbox read", kind: "GET", target: "/m/api/inbox", success: ok({ rows: [] }), trigger: (h) => h.app.loadInbox() },
  { name: "the tasks read", kind: "GET", target: "/m/api/tasks", success: ok({ tasks: [] }), trigger: (h) => h.app.loadTasks() },
  { name: "the review read", kind: "GET", target: "/m/api/review", success: ok({ open: 0 }), trigger: (h) => h.app.loadReview() },
  { name: "the In progress read", kind: "GET", target: "/m/api/working", success: ok({ entries: [], total: 0, more: false }),
    trigger: (h) => h.app.loadWorking(true) },
  { name: "me (loadMe)", kind: "GET", target: "/m/api/me", success: ok({ signed_in: true, secure: { ok: true } }), trigger: (h) => h.app.loadMe() },
  { name: "me (the stream's probe)", kind: "GET", target: "/m/api/me", success: ok({ signed_in: true, secure: { ok: true } }),
    trigger: (h) => h.app.probe() },
  { name: "the projects read", kind: "GET", target: "/m/api/projects", success: ok({ projects: [] }), trigger: (h) => h.app.loadChats() },
  { name: "a project's chats", kind: "GET", target: "/m/api/threads?project=", success: ok({ threads: [] }),
    trigger: (h) => h.app.loadProjectThreads(P1) },
  { name: "a chat's log", kind: "GET", target: `/m/api/threads/${T1}/log`, success: ok({ rows: [], next: 0, thread: null }),
    trigger: (h) => h.app.openThread(T1) },
  { name: "a task", kind: "GET", target: `/m/api/tasks/${TASK}`, success: ok({ id: TASK, state: "working", brief: "b" }),
    trigger: (h) => h.app.openTask(TASK) },
  { name: "a task's diff", kind: "GET", target: `/m/api/tasks/${TASK}/diff`, success: ok({ files: [] }),
    setup: async (h) => { await h.app.openTask(TASK); }, trigger: (h) => h.app.loadDiff() },
  { name: "a card's detail", kind: "GET", target: "/m/api/inbox/appr%3Ar1/detail", success: ok({}), trigger: (h) => h.app.openCard(row) },
  { name: "a card's ALWAYS rule", kind: "GET", target: "/m/api/inbox/appr%3Ar1/answer/rule", success: ok({}),
    setup: async (h) => { h.app.openCard(row); await h.settle(); },
    trigger: (h) => (h.app as unknown as { readRule(c: unknown, n: string): Promise<void> }).readRule(h.app.card, "") },
  { name: "the models read", kind: "GET", target: "/m/api/thread-models", success: ok({ providers: [] }), trigger: (h) => h.app.openNewChat() },
  { name: "the op-token issue", kind: "GET", target: "/m/api/ops/tokens", success: ok({ tokens: [] }), trigger: (h) => h.app.unlock.pool.refill() },
  { name: "op status (after a duplicate)", kind: "GET", target: "/m/api/ops/n", success: ok({ state: "done" }), unlockS: 1800,
    setup: (h) => { h.replies["/m/api/x"] = { status: 409, body: { duplicate: true } }; },
    trigger: (h) => h.app.unlock.run(h.app.unlock.freeze("k", "/m/api/x", {})!) },
  { name: "an answer (Deny)", kind: "POST", target: "/m/api/inbox/appr%3Ar1/answer", success: { status: 200, body: {} },
    setup: async (h) => { h.app.openCard(row); await h.settle(); }, trigger: (h) => h.app.denyCard() },
  { name: "a chat message", kind: "POST", target: `/m/api/threads/${T1}/messages`, success: { status: 202, body: {} },
    trigger: (h) => h.app.sendMessage(T1, "hi") },
  { name: "a gated action", kind: "POST", target: "/m/api/x", success: { status: 200, body: {} }, unlockS: 1800,
    trigger: (h) => h.app.unlock.run(h.app.unlock.freeze("k", "/m/api/x", {})!) },
  { name: "the unlock options", kind: "POST", target: "/m/api/unlock/options", success: { status: 200, body: { publicKey: { challenge: "AAEC" } } },
    trigger: (h) => h.app.unlock.run(h.app.unlock.freeze("k", "/m/api/x", {})!) },
  { name: "the unlock", kind: "POST", target: "/m/api/unlock", success: { status: 200, body: { unlocked: true, unlock_s: 3600 } },
    trigger: (h) => h.app.unlock.run(h.app.unlock.freeze("k", "/m/api/x", {})!) },
  { name: "Lock now", kind: "POST", target: "/m/api/lock", success: { status: 200, body: {} }, unlockS: 1800, trigger: (h) => h.app.lockNow() },
  { name: "a push route", kind: "push", target: "/m/api/push", success: { status: 200, body: {} },
    trigger: (h) => (h.app.notify as unknown as { send(m: string, p: string): Promise<unknown> }).send("GET", "/m/api/push") },
  { name: "the clip upload", kind: "clip", target: "/m/api/stt", success: { status: 200, body: { text: "x" } },
    trigger: (h) => (h.app.talk as unknown as { env: { upload(b: Blob): Promise<unknown> } }).env.upload(new Blob(["x"])) },
];

const apps: PhoneApp[] = [];
afterEach(() => { apps.splice(0).forEach((a) => a.stop()); });

function harness(site: Site, outcome: Outcome) {
  const root = document.createElement("main");
  document.body.replaceChildren(root);
  const gets: Record<string, Got> = {
    "/m/api/inbox": ok({ rows: [row] }), "/m/api/tasks": ok({ tasks: [] }), "/m/api/review": ok({ open: 0 }),
    "/m/api/working": ok({ entries: [], total: 0, more: false }),
    "/m/api/me": ok({ signed_in: true, secure: { ok: true }, unlock_s: site.unlockS ?? 0 }),
  };
  const replies: Record<string, Posted> = {};
  installUnlock(gets, replies);
  let armed = false;
  let delivered = false;
  let release!: () => void;
  const gate = new Promise<void>((r) => { release = r; });
  // After the target answers, every later request hangs (so a follow-on read cannot repaint the orb),
  // except the `me` a write's 401 asks (WS-5c rule 9), which answers 401.
  const after = (path: string) => {
    if (outcome === "401" && path === "/m/api/me") return "me401";
    return "hang";
  };
  const onTarget = async <T>(path: string, answer: () => T, fallback: () => T | Promise<T>): Promise<T> => {
    if (delivered) {
      if (after(path) === "me401") return { ok: false, why: "signed-out", status: 401 } as T;
      return new Promise<T>(() => {});
    }
    if (armed && (site.target.endsWith("=") || site.target.endsWith("/n") ? path.startsWith(site.target) : path.split("?")[0] === site.target)) {
      await gate;
      delivered = true;
      return answer();
    }
    return fallback();
  };
  const signedOut = vi.fn();
  let stream: Stream | null = null;
  const app = new PhoneApp({
    root, onSignedOut: signedOut, signOut: async () => {},
    fetchJson: (path) => onTarget(path, () => gotFor(outcome, site.success as Got),
      () => gets[path.split("?")[0]] ?? { ok: false, why: "error", status: 404, body: { error: "not found" } }),
    postJson: (path) => onTarget(path, () => postedFor(outcome, site.success as Posted),
      () => replies[path] ?? { status: 200, body: {} }),
    pushSend: (_m, path) => onTarget(path, () => outcome === "no answer" ? null : postedFor(outcome, site.success as Posted),
      () => ({ status: 200, body: {} })),
    talkEnv: { media: () => null, context: () => null, now: () => 0, setTimer: () => 0, clearTimer: () => {},
      upload: () => onTarget("/m/api/stt", () => postedFor(outcome, site.success as Posted), () => ({ status: 200, body: {} })) },
    getCredential: async () => ({ id: "cred", response: {} }),
    openStream: () => { stream = new Stream(); return stream; },
    setTimer: () => 0,
    unlockTimer: { set: () => 0, clear: () => {} },
  });
  apps.push(app);
  const settle = async () => { for (let i = 0; i < 60; i++) await Promise.resolve(); };
  const conn = () => root.querySelector<HTMLElement>('[data-testid="m-conn"]')!;
  return { app, root, gets, replies, signedOut, settle, conn, stream: () => stream!,
    arm: () => { armed = true; }, release: () => release(), delivered: () => delivered };
}
type H = ReturnType<typeof harness>;

describe("every request reports through the one path (orb spec §4, §11)", () => {
  const cases = SITES.flatMap((site) => OUTCOMES.map((outcome) => [site.name, outcome, site] as const));
  it.each(cases)("%s answering %s", async (_name, outcome, site) => {
    const h = harness(site, outcome);
    h.app.start(); await h.settle();
    h.stream().open(); await h.settle();
    await site.setup?.(h); await h.settle();
    expect(h.conn().dataset.state).toBe("live");
    h.arm();
    const run = Promise.resolve(site.trigger(h));
    await h.settle();
    // "2xx" must bring back an app that went offline while the request was out.
    if (outcome === "2xx") { h.app.setOnline(false, 0); expect(h.conn().dataset.state).toBe("offline"); }
    h.release(); await h.settle(); await h.settle();
    expect(h.delivered()).toBe(true);
    const want = EXPECT[outcome];
    expect({ state: h.conn().dataset.state, word: h.conn().textContent }).toEqual({ state: want.state, word: expect.stringMatching(want.word) });
    if (want.failStatus !== undefined) expect(h.app.failStatus).toBe(want.failStatus);
    if (outcome === "401") expect(h.signedOut).toHaveBeenCalledOnce();
    else expect(h.signedOut).not.toHaveBeenCalled();
    void run;
  });

  it("an open event stream is reached: an offline app comes back Live", async () => {
    const h = harness(SITES[0], "2xx");
    h.app.start(); await h.settle();
    h.app.setOnline(false, 503);
    expect(h.conn().textContent).toBe("Jarvis unreachable");
    h.stream().open(); await h.settle();
    expect(h.conn().textContent).toBe("Live");
  });
});

describe("the classification table", () => {
  it.each([
    [200, undefined, "reached"], [204, {}, "reached"], [0, undefined, "offline"], [401, {}, "signed-out"],
    [502, undefined, "unreachable"], [503, {}, "unreachable"], [504, "<html>", "unreachable"], [503, { error: "  " }, "unreachable"],
    [503, { error: "the In progress list is not running" }, "refused"], [502, { error: "speech-to-text unavailable" }, "refused"],
    [409, { error: "x" }, "refused"], [404, {}, "refused"], [500, {}, "refused"], [423, {}, "refused"],
  ] as const)("status %s with %j is %s", (status, body, reach) => {
    expect(reachOf(status, body)).toBe(reach);
  });
  it("reads and writes classify alike", () => {
    expect(reachOfGot({ ok: false, why: "offline", status: 0 })).toBe("offline");
    expect(reachOfGot({ ok: false, why: "offline", status: 503 })).toBe("unreachable");
    expect(reachOfGot({ ok: false, why: "error", status: 503, body: { error: "w" } })).toBe("refused");
    expect(reachOfGot({ ok: false, why: "signed-out", status: 401 })).toBe("signed-out");
    expect(reachOfPosted({ status: 0, body: {}, offline: true })).toBe("offline");
    expect(reachOfPosted(null)).toBe("offline");
    expect(reachOfPosted({ status: 504, body: {} })).toBe("unreachable");
  });
  it("getJson tells the proxy's 503 from the daemon's own", async () => {
    const real = globalThis.fetch;
    try {
      globalThis.fetch = (async () => new Response("Bad Gateway", { status: 503 })) as typeof fetch;
      expect(await getJson("/m/api/working")).toEqual({ ok: false, why: "offline", status: 503 });
      globalThis.fetch = (async () => new Response(JSON.stringify({ error: "the In progress list is not running" }), { status: 503 })) as typeof fetch;
      expect(await getJson("/m/api/working")).toEqual({ ok: false, why: "error", status: 503, body: { error: "the In progress list is not running" } });
      globalThis.fetch = (async () => { throw new TypeError("network"); }) as typeof fetch;
      expect(await getJson("/m/api/working")).toEqual({ ok: false, why: "offline", status: 0 });
    } finally { globalThis.fetch = real; }
  });
});

describe("no request bypasses the path (source guard)", () => {
  it("app.ts calls the raw helpers only inside the reporting wrappers", () => {
    const code = appSource.replace(/\/\/.*$/gm, "");
    // The raw functions are named once each, where the wrappers take them.
    expect(code.match(/deps\.fetchJson/g)).toHaveLength(1);
    expect(code.match(/deps\.postJson/g)).toHaveLength(1);
    expect(code.match(/deps\.pushSend/g)).toHaveLength(1);
    expect(code.match(/deps\.talkEnv/g)).toHaveLength(1);
    expect(code.match(/\bgetJson\b/g)).toHaveLength(2); // the import and the wrapper
    expect(code.match(/[^.\w]postJson\(/g) ?? []).toHaveLength(0);
    expect(code.match(/\bbrowserSend\b/g)).toHaveLength(2);
    expect(code.match(/\bbrowserTalkEnv\b/g)).toHaveLength(2);
    expect(code).not.toMatch(/\bfetch\(/);
    expect(code).not.toMatch(/new EventSource\([^)]*\)(?![^;]*StreamLike)/);
  });
  it("Unlock reaches Jarvis only through the functions the app hands it", () => {
    expect(unlockSource.replace(/\/\/.*$/gm, "")).not.toMatch(/\bfetch\(/);
  });
  it("only net.ts, the push and clip helpers (wrapped by the app) and the sign-in shell call fetch", () => {
    const callers = Object.entries(sources).filter(([name, code]) => !name.includes(".test.") && /\bfetch\(/.test(code.replace(/\/\/.*$/gm, "")))
      .map(([name]) => name).sort();
    expect(callers).toEqual(["./main.ts", "./net.ts", "./notify.ts", "./reader.ts", "./talk.ts"]);
  });
});
