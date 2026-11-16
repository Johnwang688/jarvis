// The open chat's log reads in the running app (PR 384 follow-up): randomised interleavings of tail, forward
// and re-opening responses across three chats, against a model of the daemon's log pages; and the new-chat
// sheet's provider read through applyRead.
import { afterEach, describe, expect, it, vi } from "vitest";
import { PhoneApp, type StreamLike } from "./app";
import type { Got } from "./net";
import { decodeModels, readVerdict } from "./reads";
import { T1, T2, T3 } from "./threads.fixtures";

class Stream implements StreamLike {
  readyState = 1;
  onopen = null;
  onmessage = null;
  onerror = null;
  close() { this.readyState = 2; }
}
const apps: PhoneApp[] = [];
afterEach(() => { apps.splice(0).forEach((app) => app.stop()); vi.useRealTimers(); });
const settle = async (n = 160) => { for (let i = 0; i < n; i++) await Promise.resolve(); };
const ok = (body: unknown): Got => ({ ok: true, body });
const range = (a: number, b: number) => Array.from({ length: Math.max(0, b - a) }, (_, i) => a + i);

function rand(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const STATIC: Record<string, Got> = {
  "/m/api/me": ok({ signed_in: true, secure: { ok: true } }),
  "/m/api/inbox": ok({ rows: [] }),
  "/m/api/working": ok({ entries: [], total: 0 }),
  "/m/api/tasks": ok({ tasks: [] }),
  "/m/api/review": ok({ v: 1, overnight: [], before: [], open: 0 }),
  "/m/api/projects": ok({ projects: [] }),
  "/m/api/threads": ok({ threads: [] }),
};

// A model of jarvis/v2/remote_api.py `log`: records the phone is not shown leave sparse seqs; small pages.
const TAIL = 5, PAGE = 3;
const CHATS = [T1, T2, T3];
type Mode = "clean" | "faults" | "hang";
interface Pending {
  chat: string; kind: "tail" | "forward"; opening: unknown; resolve: (got: Got) => void; body: unknown; hung: boolean; done: boolean;
}

async function run(seed: number, mode: Mode) {
  vi.useFakeTimers();
  const r = rand(seed * 7 + (mode === "clean" ? 1 : mode === "faults" ? 2 : 3));
  const pick = <T,>(list: T[]) => list[Math.floor(r() * list.length)];
  const records: Record<string, boolean[]> = Object.fromEntries(CHATS.map((c) => [c, []]));
  // Now and then a burst longer than a tail, so a re-opening can land past the window.
  const grow = (chat: string) => {
    for (let n = r() < 0.25 ? 6 + Math.floor(r() * 10) : 1 + Math.floor(r() * 4); n > 0; n--) records[chat].push(r() < 0.7);
  };
  CHATS.forEach(grow);
  const serve = (chat: string, after: number | null) => {
    let idx = records[chat].flatMap((s, i) => (s ? [i] : []));
    let older = false;
    if (after === null) { older = idx.length > TAIL; idx = idx.slice(-TAIL); } else idx = idx.filter((i) => i >= after);
    const out = idx.slice(0, PAGE);
    const more = idx.length > PAGE;
    const next = more ? out[out.length - 1] + 1 : Math.max(records[chat].length, after ?? 0);
    return { rows: out.map((seq) => ({ seq, role: "Claude", text: `${chat} line ${seq}` })), next, more, older };
  };
  const pending: Pending[] = [];
  const overlap: string[] = [];
  let app!: PhoneApp;
  const fetchJson = async (path: string): Promise<Got> => {
    const m = /^\/m\/api\/threads\/([a-z]+)\/log(?:\?after=(\d+))?$/.exec(path);
    if (!m) return STATIC[path] ?? ok({});
    const chat = m[1], after = m[2] === undefined ? null : Number(m[2]);
    const kind = after === null ? "tail" : "forward";
    const opening = app.thread;
    if (pending.some((p) => !p.done && p.opening === opening && p.kind === kind)) overlap.push(path);
    let body: unknown = serve(chat, after);
    if (mode === "faults" && r() < 0.2) {
      const rows = (body as { rows: unknown[] }).rows;
      body = r() < 0.5 || !rows.length ? {} : { ...(body as object), rows: [...rows.slice(0, 1), null, ...rows.slice(1)] };
    }
    const hung = mode === "hang" && chat === T1 && r() < 0.5;
    return new Promise<Got>((resolve) => pending.push({ chat, kind, opening, resolve, body, hung, done: false }));
  };
  const root = document.createElement("main");
  document.body.replaceChildren(root);
  app = new PhoneApp({ root, fetchJson, onSignedOut: vi.fn(), signOut: async () => {}, openStream: () => new Stream() });
  apps.push(app);
  app.start(); await settle();

  // What each opening was last asked to catch up to: the log's length when a read was asked for.
  const asked = new WeakMap<object, number>();
  const cursor = new WeakMap<object, number>();
  const trigger = () => { if (app.thread) asked.set(app.thread, records[app.thread.id].length); };
  const answer = (p: Pending) => { p.done = true; p.resolve(ok(p.body)); };
  const check = (where: string) => {
    const open = app.thread;
    if (!open) return;
    expect(open.next, `${where}: the cursor never moves back`).toBeGreaterThanOrEqual(cursor.get(open) ?? 0);
    cursor.set(open, open.next);
    const shown = open.rows.map((row) => row.seq);
    const truth = open.base === null ? [] : range(open.base, open.next).filter((i) => records[open.id][i]);
    expect(shown, `${where}: one row per seq, nothing missing in [base, next)`).toEqual(truth);
    expect(open.rows.every((row) => row.text === `${open.id} line ${row.seq}`), `${where}: only this chat's rows`).toBe(true);
  };
  const drawn = () => Array.from(root.querySelectorAll<HTMLElement>('[data-testid="m-msg"]')).map((n) => Number(n.dataset.seq));

  for (let step = 0; step < 50; step++) {
    const x = r();
    if (x < 0.2) grow(pick(CHATS));
    else if (x < 0.4) { trigger(); void app.moreLog(); }
    else if (x < 0.52) { void app.openThread(pick(CHATS)); trigger(); }
    else if (x < 0.56) { if (app.thread) app.goBack(); }
    else {
      const live = pending.filter((p) => !p.done && !p.hung);
      if (live.length) answer(pick(live));
    }
    await settle(Math.floor(r() * 30));
    check(`seed ${seed} step ${step}`);
  }

  // The owner is in a chat, and one more event arrives for it.
  if (!app.thread) { void app.openThread(pick(CHATS)); trigger(); await settle(); }
  if (r() < 0.5) grow(app.thread!.id);
  trigger(); void app.moreLog();
  for (let i = 0; i < 400; i++) {
    const live = pending.filter((p) => !p.done && !p.hung);
    if (!live.length) { await settle(); if (!pending.some((p) => !p.done && !p.hung)) break; continue; }
    answer(pick(live)); await settle();
  }
  const open = app.thread!;
  check(`seed ${seed} settled`);
  expect(overlap, "one request per opening per kind at most").toEqual([]);
  expect(drawn(), "the transcript drawn is the window").toEqual(open.rows.map((row) => row.seq));
  const blocked = pending.some((p) => !p.done && p.opening === open);
  if (mode !== "faults" && !blocked) {
    // No queued re-read was lost, and no other chat's unanswered request held this one back.
    expect(open.next, `seed ${seed}: caught up to the last read asked for`).toBeGreaterThanOrEqual(asked.get(open) ?? 0);
  }
  // The requests that never answered do, late: still one window, no hole, no repeat.
  pending.filter((p) => !p.done).forEach(answer);
  await settle();
  check(`seed ${seed} late`);
  expect(drawn()).toEqual(app.thread!.rows.map((row) => row.seq));
  return { hungAny: pending.some((p) => p.hung), blocked };
}

describe("chat log reads under random interleavings (property)", () => {
  it.each(range(0, 120))("clean, seed %i", async (seed) => { await run(seed, "clean"); });
  it.each(range(0, 80))("malformed and partial pages, seed %i", async (seed) => { await run(seed, "faults"); });
  it.each(range(0, 120))("a chat whose requests never answer, seed %i", async (seed) => { await run(seed, "hang"); });
});

describe("chat log reads: the cases the property rarely draws", () => {
  const line = (seq: number) => ({ seq, role: "Claude", text: `message ${seq}` });
  const page = (start: number, end: number, extra: object = {}) =>
    ok({ rows: range(start, end + 1).map(line), next: end + 1, ...extra });
  function deferred() {
    let resolve!: (got: Got) => void;
    const promise = new Promise<Got>((res) => { resolve = res; });
    return { promise, resolve };
  }
  async function harness() {
    vi.useFakeTimers();
    const root = document.createElement("main");
    document.body.replaceChildren(root);
    const replies: Record<string, Got | Promise<Got>> = { ...STATIC, [`/m/api/threads/${T1}/log`]: page(0, 4) };
    const fetchJson = vi.fn(async (path: string): Promise<Got> => replies[path] ?? replies[path.split("?")[0]] ?? ok({}));
    const app = new PhoneApp({ root, fetchJson, onSignedOut: vi.fn(), signOut: async () => {}, openStream: () => new Stream() });
    apps.push(app);
    app.start(); await settle();
    return { app, replies, fetchJson, path: `/m/api/threads/${T1}/log` };
  }
  const seqs = (app: PhoneApp) => app.thread?.rows.map((row) => row.seq);

  it("a page dropped behind a newer tail does not end the read asked for after that tail", async () => {
    const h = await harness();
    await h.app.openThread(T1);
    // Opened again while 20 rows came in: its tail is out. An event then asks for the rows after the cursor.
    const tail = deferred(); h.replies[h.path] = tail.promise;
    void h.app.openThread(T1); await settle();
    const forward = deferred(); h.replies[`${h.path}?after=5`] = forward.promise;
    const reading = h.app.moreLog(); await settle();
    tail.resolve(page(20, 24, { older: true })); await settle();
    expect(seqs(h.app)).toEqual(range(20, 25));
    // The forward page (asked when the log was 27 long) lands behind the new window: dropped, and read on.
    h.replies[`${h.path}?after=25`] = page(25, 26);
    forward.resolve(page(5, 7, { more: true })); await reading; await settle();
    expect(h.fetchJson.mock.calls.map((call) => call[0])).toContain(`${h.path}?after=25`);
    expect(seqs(h.app)).toEqual(range(20, 27));
  });

  it("a follow asked before any window reads the tail, not the whole log, and the owed follow runs after", async () => {
    const h = await harness();
    const tail = deferred(); h.replies[h.path] = tail.promise;
    const opening = h.app.openThread(T1); await settle();
    await h.app.moreLog();
    expect(h.fetchJson.mock.calls.map((call) => call[0]).filter((p) => p.startsWith(h.path))).toEqual([h.path]);
    // The owed read is the tail again (still no cursor when it was asked for), and it runs once.
    h.replies[h.path] = page(0, 5);
    tail.resolve(page(0, 4)); await opening; await settle();
    expect(seqs(h.app)).toEqual(range(0, 6));
    expect(h.fetchJson.mock.calls.map((call) => call[0]).filter((p) => p.startsWith(h.path))).toEqual([h.path, h.path]);
  });

  it("a tail cut short by its size reads on from its cursor", async () => {
    const h = await harness();
    h.replies[h.path] = page(10, 12, { older: true, more: true });
    h.replies[`${h.path}?after=13`] = page(13, 14);
    await h.app.openThread(T1); await settle();
    expect(seqs(h.app)).toEqual(range(10, 15));
    expect(h.app.thread?.next).toBe(15);
  });
});

describe("the new-chat sheet's providers are a resource read (PR 384 MED 4)", () => {
  const fast = { label: "Fast", available: true, models: [{ id: "m1", name: "Model one" }] };

  it("decodes with the one rule: no provider named is malformed, a dropped record is partial", () => {
    for (const body of [null, {}, { providers: [] }, { providers: null }, { providers: {} }, { providers: { other: fast } }]) {
      expect(decodeModels(body)).toBeNull();
    }
    expect(readVerdict(decodeModels({ providers: { fast } })!.lists)).toBe("valid");
    expect(readVerdict(decodeModels({ providers: { fast, claude: null } })!.lists)).toBe("partial");
    expect(readVerdict(decodeModels({ providers: { fast: { ...fast, models: [{ id: 3 }, { id: "m" }] } } })!.lists)).toBe("partial");
    expect(readVerdict(decodeModels({ providers: { fast: { ...fast, models: [{ id: 3 }] } } })!.lists)).toBe("malformed");
    expect(readVerdict(decodeModels({ providers: { claude: null } })!.lists)).toBe("malformed");
  });

  it("keeps the last providers under a warning, says not read yet before any, and clears on a valid read", async () => {
    vi.useFakeTimers();
    const root = document.createElement("main");
    document.body.replaceChildren(root);
    const replies: Record<string, Got> = { ...STATIC, "/m/api/projects": ok({ projects: [{ id: T2, name: "Project" }] }),
      "/m/api/thread-models": ok({ providers: [] }) };
    const app = new PhoneApp({ root, fetchJson: async (path: string) => replies[path] ?? ok({}), onSignedOut: vi.fn(),
      signOut: async () => {}, openStream: () => new Stream() });
    apps.push(app);
    app.start(); await settle();
    const warning = () => root.querySelector<HTMLElement>('.m-resource-warning[data-resource="thread-models"]');
    await app.openNewChat();
    expect(app.models).toBeNull();
    expect(warning()?.textContent).toBe("Providers couldn't refresh: not read yet.");
    app.goBack(); await settle();
    replies["/m/api/thread-models"] = ok({ providers: { fast } });
    await app.openNewChat();
    expect(warning()).toBeNull();
    const picker = root.querySelector('[data-testid="m-new-provider"]')!.textContent;
    expect(picker).toContain("Fast");
    for (const bad of [ok({ providers: [] }), ok({ providers: {} }), ok({}), { ok: false, why: "error", status: 503, body: {} } as Got]) {
      app.goBack(); await settle();
      replies["/m/api/thread-models"] = bad;
      await app.openNewChat();
      expect(root.querySelector('[data-testid="m-new-provider"]')!.textContent).toBe(picker);
      expect(warning()?.textContent).toBe("Providers couldn't refresh: showing the last read.");
    }
    app.goBack(); await settle();
    replies["/m/api/thread-models"] = ok({ providers: { fast } });
    await app.openNewChat();
    expect(warning()).toBeNull();
  });
});
