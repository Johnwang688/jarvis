import { describe, expect, it, vi } from "vitest";
import { PhoneApp, type StreamLike } from "./app";
import type { Got, Posted } from "./net";
import { actionPosts, installUnlock } from "./unlock.fixtures";

// PX-5 in the app: the lock pill, Lock now, the mirror's re-reads and slides, a POST 401, the card's
// "Unlock and send", and Done and Undo from an open chat (WS-5c, plan C §5).

class Stream implements StreamLike {
  readyState = 1;
  onopen: ((ev: Event) => void) | null = null;
  onmessage = null;
  onerror = null;
  close() {}
}

const T1 = "aaaaaaaa";
const NOW = Date.parse("2026-11-15T10:30:00Z");
const DETAIL = {
  card: { req_id: "r1", tool: "Bash", args: { command: "npm test" }, command: "npm test", reason: "r", layer: "human",
    origin: "", headline: "", asked_at: "2026-11-15T10:29:00+00:00", timeout_s: 600, allowlistable: true, thread_id: T1 },
  private: false, withheld: false, secure: { ok: true, failed: null, label: "", reason: "" },
  can: { deny: true, allow: true, always: true }, why: "",
};
const APPR = { id: "appr:r1", kind: "approval", rank: 1, title: "Claude asks to use Bash", blocks: "", project_id: null,
  thread_id: T1, task_id: null, asked_at: null, expires_at: null, origin: "", detail: { req_id: "r1", tool: "Bash" } };

function harness(opts: { unlockS?: number; rows?: unknown[] } = {}) {
  const posts: { path: string; value: any }[] = [];
  const replies: Record<string, Posted> = {};
  const gets: Record<string, Got> = {};
  const fetched: string[] = [];
  let clock = NOW;
  const credential = vi.fn(async (_pk: Record<string, unknown>) => ({ id: "cred", response: {} }) as Record<string, unknown> | null);
  const signedOut = vi.fn();
  const timers: { fn: () => void; ms: number }[] = [];
  const root = document.createElement("main");
  document.body.replaceChildren(root);
  gets["/m/api/inbox"] = { ok: true, body: { rows: opts.rows ?? [] } };
  gets["/m/api/me"] = { ok: true, body: { signed_in: true, secure: { ok: true, failed: null }, unlock_s: opts.unlockS ?? 0 } };
  gets["/m/api/review"] = { ok: true, body: { open: 0 } };
  gets["/m/api/tasks"] = { ok: true, body: { tasks: [], more: false } };
  gets["/m/api/working"] = { ok: true, body: { entries: [{ thread_id: T1, project_id: "p1", project: "Calc", title: "Fix it",
    status: "unread", last: "2026-11-15T10:00:00Z", seq: 7 }], more: false, total: 1 } };
  gets[`/m/api/threads/${T1}/log`] = { ok: true, body: { rows: [{ seq: 1, role: "Claude", text: "hello", at: null, marks: [] }],
    next: 2, thread: { id: T1, title: "Fix it", project_id: "p1", role: "chat", provider: "claude" } } };
  gets["/m/api/inbox/appr%3Ar1/detail"] = { ok: true, body: DETAIL };
  installUnlock(gets, replies);
  let stream: Stream | null = null;
  const app = new PhoneApp({
    root, onSignedOut: signedOut, signOut: async () => {},
    fetchJson: async (path) => { fetched.push(path); return gets[path.split("?")[0]] ?? { ok: false, why: "error", status: 404, body: { error: "not found" } }; },
    postJson: async (path, value) => { posts.push({ path, value }); return replies[path] ?? { status: 200, body: { ok: true } }; },
    getCredential: credential,
    openStream: () => { stream = new Stream(); return stream; },
    setTimer: (fn) => { queueMicrotask(fn); return 0; },
    unlockTimer: { set: (fn, ms) => { timers.push({ fn, ms }); return timers.length; }, clear: () => {} },
    now: () => clock,
  });
  const settle = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); };
  const q = (id: string) => root.querySelector<HTMLElement>(`[data-testid="${id}"]`);
  const click = async (id: string) => { q(id)!.click(); await settle(); };
  return { app, root, posts, replies, gets, fetched, credential, signedOut, timers, settle, q, click,
    stream: () => stream!, tick: (ms: number) => { clock += ms; } };
}

describe("the lock pill and Lock now", () => {
  it("shows Locked, then Unlocked until HH:MM from me's unlock_s; it opens the menu, whose first row is Lock now", async () => {
    const h = harness();
    h.app.start();
    await h.settle();
    const pill = h.q("m-lock-pill")!;
    expect(pill.textContent).toBe("Locked");
    expect(pill.dataset.state).toBe("locked");
    const h2 = harness({ unlockS: 1800 });
    h2.app.start();
    await h2.settle();
    expect(h2.q("m-lock-pill")!.textContent).toMatch(/^Unlocked · \d{1,2}:\d{2}/);
    expect(h2.q("m-lock-pill")!.dataset.state).toBe("unlocked");
    await h2.click("m-lock-pill");
    const rows = Array.from(h2.root.querySelectorAll('[data-testid="m-menu-panel"] > *')).map((n) => (n as HTMLElement).dataset.testid);
    expect(rows[0]).toBe("m-lock-now");
    await h2.click("m-lock-now");
    expect(h2.posts).toEqual([{ path: "/m/api/lock", value: {} }]);
    expect(h2.q("m-lock-pill")!.textContent).toBe("Locked");
    expect(h2.q("m-flash")!.textContent).toMatch(/^Locked\./);
  });

  it("a Lock now that did not reach Jarvis says the phone may still be unlocked, and the pill does not claim Locked", async () => {
    const h = harness({ unlockS: 1800 });
    h.replies["/m/api/lock"] = { status: 0, body: {}, offline: true };
    h.app.start();
    await h.settle();
    await h.click("m-lock-pill");
    await h.click("m-lock-now");
    expect(h.q("m-lock-pill")!.dataset.state).toBe("unlocked");
    expect(h.q("m-flash")!.textContent).toContain("may still be unlocked");
  });

  it("after Lock now the next Allow takes the locked path (Face ID first, no doomed POST)", async () => {
    const h = harness({ unlockS: 1800, rows: [APPR] });
    h.app.start();
    await h.settle();
    await h.app.lockNow();
    h.posts.length = 0;
    await h.click("m-item-review");
    await h.click("approval-allow");
    expect(h.posts.map((p) => p.path)).toEqual(["/m/api/unlock/options", "/m/api/unlock", "/m/api/inbox/appr%3Ar1/answer"]);
  });
});

describe("the mirror (rule 2)", () => {
  it("is read from me again on the stream's reopen and on visibility, which also refills the pool", async () => {
    const h = harness();
    h.app.start();
    await h.settle();
    expect(h.app.unlock.mirror.open()).toBe(false);
    h.gets["/m/api/me"] = { ok: true, body: { signed_in: true, secure: { ok: true }, unlock_s: 600 } };
    h.stream().onopen!(new Event("open"));
    await h.settle();
    expect(h.app.unlock.mirror.open()).toBe(true);
    h.gets["/m/api/me"] = { ok: true, body: { signed_in: true, secure: { ok: true }, unlock_s: 0 } };
    const reads = h.app.unlock.pool.reads;
    h.app.visible();
    await h.settle();
    expect(h.app.unlock.mirror.open()).toBe(false);
    expect(h.app.unlock.pool.reads).toBe(reads + 1);
    expect(h.q("m-lock-pill")!.textContent).toBe("Locked");
  });

  it("a 202 message slides it only while open, and never opens it", async () => {
    const h = harness();
    h.replies[`/m/api/threads/${T1}/messages`] = { status: 202, body: { status: "sent" } };
    h.app.start();
    await h.settle();
    await h.app.openThread(T1);
    await h.app.sendMessage(T1, "hi");
    expect(h.app.unlock.mirror.open()).toBe(false);
    h.app.unlock.mirror.fromMe(60);
    h.tick(30_000);
    await h.app.sendMessage(T1, "again");
    expect(h.app.unlock.mirror.until).toBe(NOW + 30_000 + 3_600_000);
  });

  it("Deny and Leave never ask for Face ID nor unlock", async () => {
    const h = harness({ rows: [APPR] });
    h.app.start();
    await h.settle();
    await h.click("m-item-review");
    await h.click("approval-deny");
    expect(h.posts.map((p) => p.path)).toEqual(["/m/api/inbox/appr%3Ar1/answer"]);
    expect(h.credential).not.toHaveBeenCalled();
  });
});

describe("rule 9: a POST 401 is signed out, never locked", () => {
  it("an Allow's 401 probes me; signed out there, the app signs out and no unlock is tried", async () => {
    const h = harness({ unlockS: 1800, rows: [APPR] });
    h.app.start();
    await h.settle();
    await h.click("m-item-review");
    h.replies["/m/api/inbox/appr%3Ar1/answer"] = { status: 401, body: { error: "unauthorized" } };
    h.gets["/m/api/me"] = { ok: false, why: "signed-out", status: 401 };
    await h.click("approval-allow");
    expect(h.signedOut).toHaveBeenCalledTimes(1);
    expect(h.posts.map((p) => p.path)).toEqual(["/m/api/inbox/appr%3Ar1/answer"]);
    expect(h.credential).not.toHaveBeenCalled();
  });
});

describe("rule 6 on the card: Unlock and send", () => {
  it("AUTHORIZE becomes Unlock and send, resumes the same op, and is dropped when the sheet closes", async () => {
    const h = harness({ rows: [APPR] });
    let active = false;
    (h.app.unlock as any).deps.activation = () => active;
    h.credential.mockRejectedValueOnce(new Error("NotAllowedError"));
    h.app.start();
    await h.settle();
    await h.click("m-item-review");
    await h.click("approval-allow");
    expect(h.q("approval-allow")!.textContent).toBe("Unlock and send");
    expect(h.q("m-card-notice")!.textContent).toBe("Face ID needs a fresh tap: press Unlock and send.");
    expect(actionPosts(h.posts)).toEqual([]);
    // Closing the sheet drops it: opening again shows AUTHORIZE and a new tap takes a new token.
    await h.click("m-back");
    await h.click("m-item-review");
    expect(h.q("approval-allow")!.textContent).toBe("AUTHORIZE");
    expect(h.app.tapAgain.size).toBe(0);
    h.credential.mockRejectedValueOnce(new Error("NotAllowedError"));
    await h.click("approval-allow");
    expect(h.q("approval-allow")!.textContent).toBe("Unlock and send");
    active = true;
    await h.click("approval-allow");
    const acts = actionPosts(h.posts);
    expect(acts.length).toBe(1);
    expect(acts[0].value.decision).toBe("allow");
    expect(h.q("m-flash")!.textContent).toBe("Authorized.");
  });
});

describe("Done and Undo from an open chat (WS-5c §5.6)", () => {
  it("Done sends the chat's seq with an op token; Undo within its window sends Done's token with a fresh op", async () => {
    const h = harness({ unlockS: 1800 });
    h.replies[`/m/api/working/${T1}/done`] = { status: 200, body: { thread_id: T1, done: true, token: "u".repeat(32), undo_s: 10 } };
    h.replies[`/m/api/working/${T1}/undo`] = { status: 200, body: { thread_id: T1, done: false } };
    h.app.start();
    await h.settle();
    await h.app.openThread(T1);
    await h.settle();
    await h.click("m-thread-more");
    await h.click("m-thread-done");
    expect(h.posts[0].path).toBe(`/m/api/working/${T1}/done`);
    expect(Object.keys(h.posts[0].value).sort()).toEqual(["op", "seq"]);
    expect(h.posts[0].value.seq).toBe(7);
    expect(h.q("m-thread-done-note")!.textContent).toContain("Marked done.");
    await h.click("m-thread-undo");
    expect(h.posts[1].path).toBe(`/m/api/working/${T1}/undo`);
    expect(h.posts[1].value.token).toBe("u".repeat(32));
    expect(h.posts[1].value.op).not.toBe(h.posts[0].value.op);
    expect(h.q("m-thread-done-note")!.textContent).toContain("Undone");
  });

  it("Undo is gone once its window ends; a refused Done says why", async () => {
    const h = harness({ unlockS: 1800 });
    h.replies[`/m/api/working/${T1}/done`] = { status: 200, body: { thread_id: T1, done: true, token: "u".repeat(32), undo_s: 10 } };
    h.app.start();
    await h.settle();
    await h.app.openThread(T1);
    await h.settle();
    await h.app.doneThread(T1);
    expect(h.q("m-thread-undo")).not.toBeNull();
    h.tick(10_001);
    h.timers.at(-1)!.fn();
    expect(h.q("m-thread-undo")).toBeNull();
    await h.app.undoThread(T1);
    expect(h.posts.filter((p) => p.path.endsWith("/undo"))).toEqual([]);
    h.replies[`/m/api/working/${T1}/done`] = { status: 409, body: { error: "it still needs you", why_not: "an approval is waiting" } };
    await h.app.doneThread(T1);
    expect(h.q("m-thread-done-note")!.textContent).toBe("Not done. It still needs you. An approval is waiting.");
  });
});
