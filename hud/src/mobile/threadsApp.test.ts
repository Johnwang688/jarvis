// PX-2 through the app: the working read's own state and its one retry policy (Sol, findings 3 and 5),
// the refresh triggers, task seeding (finding 4), and the keyed list under pending sends and refusals.
import { describe, expect, it, vi } from "vitest";
import { PhoneApp, WORKING_DEBOUNCE_MS, WORKING_RETRY_MS, type StreamLike } from "./app";
import type { Got, Posted } from "./net";
import type { InboxRow } from "../lib/inbox";
import { mkRow } from "./threads.fixtures";

class FakeStream implements StreamLike {
  readyState = 0;
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  constructor(public url: string) {}
  open() { this.readyState = 1; this.onopen?.(new Event("open")); }
  /** The raw SSE text, through onmessage, as the browser delivers it. */
  raw(text: string) { this.onmessage?.(new MessageEvent("message", { data: text })); }
  send(value: unknown) { this.raw(JSON.stringify(value)); }
  close() { this.readyState = 2; }
}

const ok = (body: unknown): Got => ({ ok: true, body });
const T1 = "aaaaaaaa", K1 = "11111111";

function harness() {
  const answers: Record<string, Got | (() => Got | Promise<Got>)> = {};
  const asked: string[] = [];
  const streams: FakeStream[] = [];
  const timers: { fn: () => void; ms: number }[] = [];
  const slow = new Map<number, { fn: () => void; ms: number }>();
  let slowSets = 0, slowClears = 0, slowId = 0;
  const posts: { path: string; value: unknown }[] = [];
  const postReplies: Record<string, () => Promise<Posted>> = {};
  const signedOut = vi.fn();
  const root = document.createElement("main");
  document.body.replaceChildren(root);
  const app = new PhoneApp({
    root, onSignedOut: signedOut, signOut: async () => {},
    fetchJson: async (path) => {
      asked.push(path);
      const a = answers[path.split("?")[0]];
      return typeof a === "function" ? a() : a ?? { ok: false, why: "error", status: 404, body: { error: "not found" } };
    },
    postJson: async (path, value) => { posts.push({ path, value }); const r = postReplies[path]; return r ? r() : { status: 200, body: {} }; },
    openStream: (url) => { const s = new FakeStream(url); streams.push(s); return s; },
    setTimer: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    setSlow: (fn, ms) => { slowSets += 1; slow.set(++slowId, { fn, ms }); return slowId; },
    clearSlow: (h) => { slowClears += 1; slow.delete(h as number); },
    now: () => Date.parse("2026-11-15T10:30:00Z"),
  });
  answers["/m/api/inbox"] = ok({ rows: [], more: false });
  answers["/m/api/me"] = ok({ signed_in: true });
  answers["/m/api/review"] = ok({ open: 0 });
  answers["/m/api/tasks"] = ok({ tasks: [], more: false });
  answers["/m/api/working"] = ok({ entries: [], more: false, total: 0 });
  const settle = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); };
  /** Run every timer the app armed (the debounces); the slow retry timer is fired separately. */
  const flush = async () => { await settle(); const run = timers.splice(0); for (const t of run) t.fn(); await settle(); };
  const count = (path: string) => asked.filter((p) => p.split("?")[0] === path).length;
  const q = (id: string) => root.querySelector<HTMLElement>(`[data-testid="${id}"]`);
  return { app, root, answers, asked, streams, timers, slow, posts, postReplies, signedOut, settle, flush, count, q,
    get slowSets() { return slowSets; }, get slowClears() { return slowClears; },
    fireSlow: async () => { const [id, t] = [...slow.entries()][0]; slow.delete(id); t.fn(); await settle(); } };
}
type H = ReturnType<typeof harness>;

async function started(h: H) {
  h.app.start();
  await h.settle();
  h.streams[0].open();
  await h.settle();
}

const task = (id: string, state: string, over: Record<string, unknown> = {}) => ({ id, project_id: "99999999", brief: "Build it", state,
  updated: "2026-11-15T10:00:00Z", status: { step: 1, steps: 3 }, ...over });

describe("the working read's own state (Sol, finding 3)", () => {
  it("a working 503 says In progress unavailable in the Running band and never turns the app offline", async () => {
    const h = harness();
    h.answers["/m/api/working"] = { ok: false, why: "offline", status: 503 };
    h.answers["/m/api/inbox"] = ok({ rows: [mkRow("q:1", 3, { thread_id: T1, detail: { text: "Which?", options: [] } })] });
    const flips = vi.spyOn(h.app, "setOnline");
    await started(h);
    expect(flips.mock.calls.filter((c) => c[0] === false)).toEqual([]);
    expect(h.q("m-working-unavailable")!.textContent).toBe("In progress unavailable");
    expect(h.app.online).toBe(true);
    expect(h.q("m-offline")!.hidden).toBe(true);
    expect(h.root.querySelector('.app[data-stale="true"]')).toBeNull();
    // The inbox row is still drawn: a failed working read hides nothing.
    expect(h.root.querySelectorAll('[data-testid="m-inbox-row"]').length).toBe(1);
    expect(h.q("m-conn")!.dataset.state).toBe("live");
  });

  it("ten working 503s beside inbox 200s: never offline, one retry timer, bounded request counts", async () => {
    const h = harness();
    h.answers["/m/api/working"] = { ok: false, why: "offline", status: 503 };
    const flips = vi.spyOn(h.app, "setOnline");
    await started(h);
    const working0 = h.count("/m/api/working");
    for (let i = 0; i < 10; i++) {
      h.streams[0].send({ kind: "inbox_changed", data: { ids: ["q:" + i], reason: "question" } });
      await h.flush();
      expect(h.app.online).toBe(true);
      expect(h.q("m-offline")!.hidden).toBe(true);
    }
    // The inbox was read every time; working was not (a retry is waiting): bounded.
    expect(h.count("/m/api/inbox")).toBeGreaterThanOrEqual(11);
    expect(h.count("/m/api/working")).toBe(working0);
    expect(working0).toBeLessThanOrEqual(2);
    expect(h.slow.size).toBe(1);
    expect(h.slowSets - h.slowClears).toBe(1);
    expect(flips.mock.calls.filter((c) => c[0] === false)).toEqual([]);
    // The one retry fires after 30 s, reads once, fails once, and arms one more.
    expect([...h.slow.values()][0].ms).toBe(WORKING_RETRY_MS);
    await h.fireSlow();
    expect(h.count("/m/api/working")).toBe(working0 + 1);
    expect(h.slow.size).toBe(1);
    expect(h.app.online).toBe(true);
  });

  it("a working success disarms the retry, and the entries show", async () => {
    const h = harness();
    h.answers["/m/api/working"] = { ok: false, why: "error", status: 409, body: { error: "cannot be read" } };
    await started(h);
    expect(h.slow.size).toBe(1);
    h.answers["/m/api/working"] = ok({ entries: [{ thread_id: T1, project_id: "99999999", project: "Calc", title: "Fix it",
      status: "working", last: "2026-11-15T10:00:00Z" }], more: false, total: 1 });
    await h.fireSlow();
    expect(h.slow.size).toBe(0);
    expect(h.q("m-working-unavailable")).toBeNull();
    expect(h.root.querySelector('[data-testid="m-band-running"] [data-key="thread:' + T1 + '"]')).not.toBeNull();
  });

  it("a malformed working answer is unavailable, not an empty list", async () => {
    const h = harness();
    h.answers["/m/api/working"] = ok({ nope: true });
    await started(h);
    expect(h.q("m-working-unavailable")).not.toBeNull();
  });

  it("a working 401 signs out", async () => {
    const h = harness();
    h.answers["/m/api/working"] = { ok: false, why: "signed-out", status: 401 };
    h.app.start();
    await h.settle();
    expect(h.signedOut).toHaveBeenCalled();
  });

  it("an inbox offline flip still decides offline, and coming back re-reads working at once, cancelling the timer", async () => {
    const h = harness();
    h.answers["/m/api/working"] = { ok: false, why: "offline", status: 503 };
    await started(h);
    const w = h.count("/m/api/working");
    expect(h.slow.size).toBe(1);
    h.app.setOnline(false);
    expect(h.q("m-offline")!.hidden).toBe(false);
    h.app.setOnline(true);
    await h.settle();
    expect(h.count("/m/api/working")).toBe(w + 1);
    expect(h.count("/m/api/tasks")).toBeGreaterThan(1);
    // Cancelled and re-armed: still exactly one retry waiting.
    expect(h.slow.size).toBe(1);
    expect(h.slowSets - h.slowClears).toBe(1);
  });
});

describe("refresh triggers", () => {
  it("working_changed, through raw SSE text, reads working once after the 1 s debounce, however many arrive", async () => {
    const h = harness();
    await started(h);
    const w = h.count("/m/api/working");
    for (let i = 0; i < 6; i++) h.streams[0].raw('{"kind":"working_changed","data":{}}');
    await h.settle();
    expect(h.timers.filter((t) => t.ms === WORKING_DEBOUNCE_MS).length).toBe(1);
    expect(h.count("/m/api/working")).toBe(w);
    await h.flush();
    expect(h.count("/m/api/working")).toBe(w + 1);
  });

  it("a turn starting or finishing reads working; a log growing does not", async () => {
    const h = harness();
    await started(h);
    const w = h.count("/m/api/working");
    h.streams[0].send({ kind: "thread_changed", data: { thread_id: T1, project_id: null, reason: "text" } });
    await h.flush();
    expect(h.count("/m/api/working")).toBe(w);
    h.streams[0].send({ kind: "thread_changed", data: { thread_id: T1, project_id: null, reason: "turn_started" } });
    await h.flush();
    expect(h.count("/m/api/working")).toBe(w + 1);
    h.streams[0].send({ kind: "thread_changed", data: { thread_id: T1, project_id: null, reason: "turn_finished" } });
    await h.flush();
    expect(h.count("/m/api/working")).toBe(w + 2);
  });

  it("the stream reopening and a resync each re-read inbox, working AND tasks", async () => {
    const h = harness();
    await started(h);
    const before = { i: h.count("/m/api/inbox"), w: h.count("/m/api/working"), t: h.count("/m/api/tasks") };
    h.streams[0].open();
    await h.settle();
    expect([h.count("/m/api/inbox"), h.count("/m/api/working"), h.count("/m/api/tasks")]).toEqual([before.i + 1, before.w + 1, before.t + 1]);
    h.streams[0].send({ kind: "resync" });
    await h.settle();
    expect([h.count("/m/api/inbox"), h.count("/m/api/working"), h.count("/m/api/tasks")]).toEqual([before.i + 2, before.w + 2, before.t + 2]);
  });

  it("task_changed and a task row's inbox change read tasks without the Tasks view open", async () => {
    const h = harness();
    await started(h);
    const t = h.count("/m/api/tasks");
    h.streams[0].send({ kind: "task_changed", data: { task_id: K1, project_id: null, reason: "task_status_changed" } });
    await h.flush();
    expect(h.count("/m/api/tasks")).toBe(t + 1);
  });
});

describe("task seeding (Sol, finding 4)", () => {
  it("entries=[], rows=[], one running task: it appears under Running", async () => {
    const h = harness();
    h.answers["/m/api/tasks"] = ok({ tasks: [task(K1, "running")], more: false });
    await started(h);
    const card = h.root.querySelector<HTMLElement>(`[data-testid="m-band-running"] [data-key="task:${K1}"]`)!;
    expect(card).not.toBeNull();
    expect(card.textContent).toContain("Build it");
    expect(h.q("m-chip-running")!.textContent).toBe("Running · 1");
  });

  it("tasks more shows its notice under Running and on the Tasks list", async () => {
    const h = harness();
    h.answers["/m/api/tasks"] = ok({ tasks: [task(K1, "running")], more: true });
    await started(h);
    expect(h.q("m-tasks-more")!.textContent).toContain("older ones, running ones included, are at the desk");
    h.q("m-chip-tasks")!.click();
    await h.settle();
    expect(h.q("m-tasks-more-list")).not.toBeNull();
  });

  it("the Tasks chip shows the task list with New task; again clears it; a task opened from the list backs to the list", async () => {
    const h = harness();
    h.answers["/m/api/tasks"] = ok({ tasks: [task(K1, "running"), task("22222222", "done")], more: false });
    h.answers[`/m/api/tasks/${K1}`] = ok({ task: task(K1, "running") });
    await started(h);
    h.q("m-chip-tasks")!.click();
    await h.settle();
    expect(h.q("m-new-task-open")).not.toBeNull();
    expect(h.root.querySelectorAll('[data-testid="m-task"]').length).toBe(2);
    expect(h.root.querySelector('[data-testid="m-tab-tasks"]')).toBeNull();
    h.root.querySelector<HTMLElement>(`[data-testid="m-task"][data-id="${K1}"]`)!.click();
    await h.settle();
    expect(h.q("m-task-view")).not.toBeNull();
    expect(h.q("m-tab-inbox")!.getAttribute("aria-selected")).toBe("true");
    h.app.show("tasks");
    await h.settle();
    expect(h.root.querySelectorAll('[data-testid="m-task"]').length).toBe(2);
    h.q("m-chip-tasks")!.click();
    await h.settle();
    expect(h.q("m-band-needs")).not.toBeNull();
  });

  it("a task opened from a Running card backs to the list, not the Tasks filter", async () => {
    const h = harness();
    h.answers["/m/api/tasks"] = ok({ tasks: [task(K1, "running")], more: false });
    h.answers[`/m/api/tasks/${K1}`] = ok({ task: task(K1, "running") });
    await started(h);
    h.root.querySelector<HTMLElement>(`[data-key="task:${K1}"] [data-testid="m-thread-open"]`)!.click();
    await h.settle();
    expect(h.q("m-task-view")).not.toBeNull();
    h.app.show("tasks");
    await h.settle();
    expect(h.q("m-band-running")).not.toBeNull();
    expect(h.q("m-chip-tasks")!.getAttribute("aria-pressed")).toBe("false");
  });
});

describe("the keyed list under a pending send and a refusal", () => {
  const rowQ = () => mkRow("q:" + T1 + ":1", 2, { thread_id: T1, detail: { text: "Which?", options: [] } });

  it("a pending send keeps its box disabled while an SSE read lands, and sends once", async () => {
    const h = harness();
    h.answers["/m/api/inbox"] = ok({ rows: [rowQ()] });
    const path = `/m/api/inbox/${encodeURIComponent("q:" + T1 + ":1")}/answer`;
    let release!: (p: Posted) => void;
    h.postReplies[path] = () => new Promise<Posted>((res) => { release = res; });
    await started(h);
    const box = h.q("m-answer-text") as unknown as HTMLTextAreaElement;
    box.value = "go on";
    box.dispatchEvent(new Event("input"));
    h.q("m-answer-send")!.click();
    await h.settle();
    h.streams[0].send({ kind: "inbox_changed", data: { ids: ["q:" + T1 + ":1"], reason: "question" } });
    await h.flush();
    expect((h.q("m-answer-send") as HTMLButtonElement).disabled).toBe(true);
    expect((h.q("m-answer-text") as HTMLTextAreaElement).readOnly).toBe(true);
    h.q("m-answer-send")!.click();
    await h.settle();
    expect(h.posts.filter((p) => p.path === path).length).toBe(1);
    release({ status: 200, body: {} });
    await h.settle();
    expect(h.posts.filter((p) => p.path === path).length).toBe(1);
  });

  it("a refusal note survives a read of an unchanged row", async () => {
    const h = harness();
    h.answers["/m/api/inbox"] = ok({ rows: [rowQ()] });
    const path = `/m/api/inbox/${encodeURIComponent("q:" + T1 + ":1")}/answer`;
    h.postReplies[path] = async () => ({ status: 409, body: { error: "already answered" } });
    await started(h);
    const box = h.q("m-answer-text") as unknown as HTMLTextAreaElement;
    box.value = "go on";
    box.dispatchEvent(new Event("input"));
    h.q("m-answer-send")!.click();
    await h.settle();
    expect(h.q("m-answer-note")!.textContent).toContain("Already answered");
    h.streams[0].send({ kind: "inbox_changed", data: { ids: ["q:" + T1 + ":1"], reason: "question" } });
    await h.flush();
    expect(h.q("m-answer-note")!.textContent).toContain("Already answered");
  });
});

describe("launch and task navigation", () => {
  it("the first paint needs one round trip: the first inbox answer is applied though the stream's reopen already owes a re-read", async () => {
    const h = harness();
    let release!: (g: Got) => void;
    let calls = 0;
    h.answers["/m/api/inbox"] = () => {
      calls += 1;
      return calls === 1 ? new Promise<Got>((res) => { release = res; }) : new Promise<Got>(() => {});   // later reads never answer
    };
    h.app.start();
    await h.settle();
    h.streams[0].open();
    await h.settle();
    release(ok({ rows: [mkRow("q:1", 3, { detail: { text: "Which?", options: [] } })] }));
    await h.settle();
    expect(h.root.querySelectorAll('[data-testid="m-inbox-row"]').length).toBe(1);
  });

  it("a task_changed for the task you left (via Chats) does not pull you back onto it", async () => {
    const h = harness();
    h.answers["/m/api/tasks"] = ok({ tasks: [task(K1, "running")], more: false });
    h.answers[`/m/api/tasks/${K1}`] = ok({ task: task(K1, "running") });
    await started(h);
    await h.app.openTask(K1);
    h.q("m-tab-chats")!.click();
    await h.settle();
    h.streams[0].send({ kind: "task_changed", data: { task_id: K1, project_id: null, reason: "task_status_changed" } });
    await h.flush();
    expect(h.app.tab).toBe("chats");
  });

  it("a task_changed whose debounce fires after you left for Chats or Review does not reopen the task", async () => {
    for (const dest of ["m-tab-chats", "m-tab-review"] as const) {
      const h = harness();
      h.answers["/m/api/tasks"] = ok({ tasks: [task(K1, "running")], more: false });
      h.answers[`/m/api/tasks/${K1}`] = ok({ task: task(K1, "running") });
      h.answers["/m/api/review"] = ok({ open: 1, items: [] });
      await started(h);
      await h.app.openTask(K1);
      h.streams[0].send({ kind: "task_changed", data: { task_id: K1, project_id: null, reason: "task_status_changed" } });
      await h.settle();                                 // the debounce is armed, not fired
      if (dest === "m-tab-review") h.app.show("review"); else h.app.show("chats");
      await h.flush();                                  // now it fires
      expect(h.app.tab, dest).toBe(dest === "m-tab-review" ? "review" : "chats");
    }
  });

  it("an event for another task cannot suppress the open task's refresh (B then A inside the debounce)", async () => {
    const h = harness();
    h.answers["/m/api/tasks"] = ok({ tasks: [task(K1, "running")], more: false });
    h.answers[`/m/api/tasks/${K1}`] = ok({ task: task(K1, "running") });
    await started(h);
    await h.app.openTask(K1);
    const reads = () => h.count(`/m/api/tasks/${K1}`);
    const before = reads();
    h.streams[0].send({ kind: "task_changed", data: { task_id: "22222222", project_id: null, reason: "task_status_changed" } });
    h.streams[0].send({ kind: "task_changed", data: { task_id: K1, project_id: null, reason: "task_status_changed" } });
    await h.flush();
    expect(reads()).toBe(before + 1);
  });

  it("closing New task, opened from the Tasks filter, lands on the Tasks filter; the Inbox tab resets the way back", async () => {
    const h = harness();
    await started(h);
    h.q("m-chip-tasks")!.click();
    await h.settle();
    h.q("m-new-task-open")!.click();
    await h.settle();
    h.app.show("tasks");
    await h.settle();
    expect(h.app.filter).toBe("tasks");
    // From the plain list, New task is not reachable (it lives under the filter); the filter is where it closes.
    h.q("m-new-task-open")!.click();
    await h.settle();
    h.q("m-tab-inbox")!.click();
    await h.settle();
    expect(h.app.filter).toBe(null);
  });
});

describe("D6: an answered row leaves at once", () => {
  const id = "q:" + T1 + ":1";
  const rowQ = () => mkRow(id, 2, { thread_id: T1, detail: { text: "Which?", options: [] } });
  const path = `/m/api/inbox/${encodeURIComponent(id)}/answer`;
  const type = (h: H) => {
    const box = h.q("m-answer-text") as unknown as HTMLTextAreaElement;
    box.value = "go on";
    box.dispatchEvent(new Event("input"));
  };

  it("a 200 removes only that row at once, updating the badge, before the re-read answers", async () => {
    const h = harness();
    h.answers["/m/api/inbox"] = ok({ rows: [rowQ(), mkRow("q:other:1", 3, { detail: { text: "Other?", options: [] } })] });
    await started(h);
    expect(h.q("m-chip-needs")!.textContent).toBe("Needs you · 2");
    let count = 0;
    h.answers["/m/api/inbox"] = () => { count += 1; return new Promise<Got>(() => {}); };   // the re-read never answers
    type(h);
    h.q("m-answer-send")!.click();
    await h.settle();
    const ids = Array.from(h.root.querySelectorAll<HTMLElement>('[data-testid="m-inbox-row"]')).map((r) => r.dataset.id);
    expect(ids).toEqual(["q:other:1"]);
    expect(h.q("m-chip-needs")!.textContent).toBe("Needs you · 1");
    expect(h.q("m-tab-inbox")!.textContent).toBe("Inbox · 1");
    expect(count).toBeGreaterThan(0);                 // the afterWrite re-read was still asked for
  });

  it("an old answer's 200 leaves a row a GET replaced under the same id (and its badge count)", async () => {
    const h = harness();
    h.answers["/m/api/inbox"] = ok({ rows: [rowQ()] });
    let release!: (p: Posted) => void;
    h.postReplies[path] = () => new Promise<Posted>((res) => { release = res; });
    await started(h);
    type(h);
    h.q("m-answer-send")!.click();                       // the POST is held
    await h.settle();
    // A GET installs changed contents under the same id (a newer question).
    h.answers["/m/api/inbox"] = ok({ rows: [mkRow(id, 2, { thread_id: T1, detail: { text: "A NEWER question?", options: [] } })] });
    h.streams[0].send({ kind: "inbox_changed", data: { ids: [id], reason: "question" } });
    await h.flush();
    h.answers["/m/api/inbox"] = () => new Promise<Got>(() => {});    // the re-read after the 200 stalls
    release({ status: 200, body: {} });
    await h.settle();
    expect(h.root.querySelectorAll('[data-testid="m-inbox-row"]').length).toBe(1);
    expect(h.root.textContent).toContain("A NEWER question?");
    expect(h.q("m-chip-needs")!.textContent).toBe("Needs you · 1");
  });

  /** Answers a question while a GET replaces it under the same id with `fresh`; whether its row is gone after the 200. */
  async function removedAfter(fresh: InboxRow): Promise<boolean> {
    const h = harness();
    h.answers["/m/api/inbox"] = ok({ rows: [rowQ()] });
    let release!: (p: Posted) => void;
    h.postReplies[path] = () => new Promise<Posted>((res) => { release = res; });
    await started(h);
    type(h);
    h.q("m-answer-send")!.click();
    await h.settle();
    h.answers["/m/api/inbox"] = ok({ rows: [fresh] });
    h.streams[0].send({ kind: "inbox_changed", data: { ids: [id], reason: "question" } });
    await h.flush();
    h.answers["/m/api/inbox"] = () => new Promise<Got>(() => {});
    release({ status: 200, body: {} });
    await h.settle();
    return h.root.querySelectorAll('[data-testid="m-inbox-row"]').length === 0;
  }

  it("the same question refreshed with reordered detail keys is removed after the 200", async () => {
    expect(await removedAfter(mkRow(id, 2, { thread_id: T1, detail: { options: [], text: "Which?" } }))).toBe(true);
  });

  it("the same question with a changed asked_at (or expiry) is removed after the 200", async () => {
    expect(await removedAfter(mkRow(id, 2, { thread_id: T1, asked_at: "2026-11-15T11:11:11Z", expires_at: "2026-11-15T12:00:00Z",
      detail: { text: "Which?", options: [], asked_at: "2026-11-15T11:11:11Z" } }))).toBe(true);
  });

  it("a changed question text, or changed options, keeps the row", async () => {
    expect(await removedAfter(mkRow(id, 2, { thread_id: T1, detail: { text: "Which one now?", options: [] } }))).toBe(false);
    expect(await removedAfter(mkRow(id, 2, { thread_id: T1, detail: { text: "Which?", options: ["a", "b"] } }))).toBe(false);
  });

  it("a failure keeps the row, with its error", async () => {
    const h = harness();
    h.answers["/m/api/inbox"] = ok({ rows: [rowQ()] });
    h.postReplies[path] = async () => ({ status: 409, body: { error: "already answered" } });
    await started(h);
    type(h);
    h.q("m-answer-send")!.click();
    await h.settle();
    expect(h.root.querySelectorAll('[data-testid="m-inbox-row"]').length).toBe(1);
    expect(h.q("m-answer-note")!.textContent).toContain("Already answered");
  });

  it("a read that was in flight before the answer cannot bring the row back; the next read restores it only if the server lists it", async () => {
    const h = harness();
    h.answers["/m/api/inbox"] = ok({ rows: [rowQ()] });
    await started(h);
    const olds: ((g: Got) => void)[] = [];
    let phase = 0;
    h.answers["/m/api/inbox"] = () => {
      phase += 1;
      return phase === 1 ? new Promise<Got>((res) => olds.push(res)) : ok({ rows: [] });
    };
    h.streams[0].send({ kind: "inbox_changed", data: { ids: [id], reason: "question" } });
    await h.flush();                                    // read 1 is now in flight, holding the row
    type(h);
    h.q("m-answer-send")!.click();
    await h.settle();
    olds[0](ok({ rows: [rowQ()] }));                    // the older read lands after the 200
    await h.settle();
    expect(h.root.querySelectorAll('[data-testid="m-inbox-row"]').length).toBe(0);
  });
});

describe("the inbox read that fails without being offline", () => {
  it("keeps the last list, names the failure and Retry reads again", async () => {
    const h = harness();
    h.answers["/m/api/inbox"] = ok({ rows: [mkRow("q:1", 3, { detail: { text: "Which?", options: [] } })] });
    await started(h);
    h.answers["/m/api/inbox"] = { ok: false, why: "error", status: 500, body: { error: "broken" } };
    h.streams[0].send({ kind: "inbox_changed", data: { ids: [], reason: "x" } });
    await h.flush();
    expect(h.q("m-list-error")!.textContent).toContain("broken");
    // Dimmed for its own error, never offline.
    expect(h.q("m-inbox")!.dataset.error).toBe("true");
    expect(h.app.online).toBe(true);
    expect(h.root.querySelectorAll('[data-testid="m-inbox-row"]').length).toBe(1);
    h.answers["/m/api/inbox"] = ok({ rows: [] });
    h.q("m-retry")!.click();
    await h.settle();
    expect(h.q("m-list-error")).toBeNull();
  });
});
