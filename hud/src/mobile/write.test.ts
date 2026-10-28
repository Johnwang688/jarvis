import { describe, expect, it, vi } from "vitest";
import { PhoneApp, type StreamLike } from "./app";
import type { Got, Posted } from "./net";
import { clientIdFor, MAYBE_DONE, MAYBE_SENT, parseModels, refusal, sentNote, taskControlsView, verbsFor } from "./write";
import { parseInbox } from "../lib/inbox";
import { inboxView, type InboxAnswers } from "./views";
import type { TaskView } from "./model";

// P-5 (jarvis/v2/remote_write.py): the message box, the sheets, the task verbs.

const H = ['<img src=x onerror="window.__pwned=1">', "<script>window.__pwned=2</script>", "<b>bold</b>"].join(" ");
const MARKUP = "img, script, b, svg, iframe, [onerror], [onload]";

class Stream implements StreamLike {
  readyState = 1;
  onopen = null;
  onmessage = null;
  onerror = null;
  close() {}
}

const CHAT = "aaaaaaaa", OTHER = "dddddddd", PROJECT = "cccccccc", TASK = "bbbbbbbb";

function task(state: string, over: Record<string, unknown> = {}) {
  return { id: TASK, project_id: PROJECT, brief: "add divide", state, updated: "2026-10-28T10:00:00Z", status: {}, ...over };
}

function harness() {
  const posts: { path: string; value: any }[] = [];
  const replies: Record<string, Posted | (() => Posted)> = {};
  const gets: Record<string, Got> = {};
  const asked: string[] = [];
  let offline = false;
  const credential = vi.fn(async (_pk: Record<string, unknown>) => ({ id: "cred", response: {} }) as Record<string, unknown> | null);
  const root = document.createElement("main");
  document.body.replaceChildren(root);
  const ok = (body: unknown): Got => ({ ok: true, body });
  gets["/m/api/inbox"] = ok({ rows: [] });
  gets["/m/api/me"] = ok({ signed_in: true, secure: { ok: true } });
  gets["/m/api/projects"] = ok({ projects: [{ id: PROJECT, name: "Calc", inbox: false }, { id: "eeeeeeee", name: "Other", inbox: false }] });
  gets["/m/api/threads"] = ok({ threads: [
    { id: CHAT, project_id: PROJECT, title: "Fix it", provider: "claude", updated: "2026-10-25T10:00:00Z", running: true },
    { id: OTHER, project_id: PROJECT, task_id: TASK, title: "a task's", provider: "codex", updated: "2026-10-25T09:00:00Z" }] });
  gets[`/m/api/threads/${CHAT}/log`] = ok({ rows: [{ seq: 0, role: "owner", text: "hi", at: null, marks: [] }], next: 1,
    more: false, older: false, thread: { id: CHAT, project_id: PROJECT, role: "chat", title: "Fix it", provider: "claude" } });
  gets[`/m/api/threads/${OTHER}/log`] = ok({ rows: [], next: 0, more: false, older: false,
    thread: { id: OTHER, project_id: PROJECT, role: "orchestrator", task_id: TASK, title: "a task's", provider: "codex" } });
  gets[`/m/api/threads/${CHAT}/log?after=1`] = ok({ rows: [], next: 1, more: false, older: false, thread: null });
  gets["/m/api/thread-models"] = ok({ providers: {
    fast: { label: "Jarvis", available: true, models: [{ id: "anthropic/claude-haiku-5.5", name: "Haiku 5.5" }] },
    claude: { label: "Claude", available: true, models: [{ id: "claude-opus-5-5", name: "Opus 5.5" }] },
    codex: { label: "Codex", available: false, models: [] } } });
  gets["/m/api/tasks"] = ok({ tasks: [task("intake")] });
  gets[`/m/api/tasks/${TASK}`] = ok({ task: task("intake") });
  const app = new PhoneApp({
    root, onSignedOut: () => {}, signOut: async () => {},
    fetchJson: async (path) => {
      asked.push(path);
      if (offline) return { ok: false, why: "offline", status: 0 };
      return gets[path] ?? { ok: false, why: "error", status: 404, body: { error: "not found" } };
    },
    postJson: async (path, value) => {
      posts.push({ path, value });
      if (offline) return { status: 0, body: {}, offline: true };
      const r = replies[path];
      return typeof r === "function" ? r() : r ?? { status: 200, body: {} };
    },
    getCredential: credential,
    openStream: () => new Stream(),
    setTimer: (fn) => { queueMicrotask(fn); return 0; },
  });
  const settle = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); };
  const q = <T extends Element = HTMLElement>(id: string) => root.querySelector<T & Element>(`[data-testid="${id}"]`);
  const click = async (id: string) => { (q<HTMLButtonElement>(id) as unknown as HTMLButtonElement)!.click(); await settle(); };
  const type = (id: string, text: string) => {
    const box = q<HTMLTextAreaElement>(id) as unknown as HTMLTextAreaElement;
    box.value = text;
    box.dispatchEvent(new Event("input"));
  };
  return { app, root, posts, replies, gets, asked, credential, settle, q, click, type, ok,
    setOffline: (v: boolean) => { offline = v; } };
}

async function openChat(h: ReturnType<typeof harness>, id = CHAT) {
  h.app.start();
  await h.settle();
  await h.click("m-tab-chats");
  (h.root.querySelector(`[data-testid="m-chat"][data-id="${id}"]`) as HTMLButtonElement | null)?.click();
  if (!h.root.querySelector(`[data-testid="m-chat"][data-id="${id}"]`)) await h.app.openThread(id);
  await h.settle();
}

describe("the message box (P-5)", () => {
  it("sends only on the Send tap, once, and shows the message only after its 2xx and a read of the log", async () => {
    const h = harness();
    let release!: (p: Posted) => void;
    h.replies[`/m/api/threads/${CHAT}/messages`] = () => new Promise<Posted>((r) => { release = r; }) as unknown as Posted;
    await openChat(h);
    const box = h.q<HTMLTextAreaElement>("m-compose-text") as unknown as HTMLTextAreaElement;
    expect(box).not.toBeNull();
    expect(box.getAttribute("enterkeyhint")).toBe("enter");
    h.type("m-compose-text", "  add a test  ");
    // Enter is a new line, never a send.
    box.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await h.settle();
    expect(h.posts).toEqual([]);
    await h.click("m-compose-send");
    expect(h.posts.map((p) => [p.path, p.value.text])).toEqual([[`/m/api/threads/${CHAT}/messages`, "add a test"]]);
    expect(h.posts[0].value.client_id).toMatch(/^[A-Za-z0-9-]{8,64}$/);
    // While it is on its way: no second send, and nothing shown as sent.
    expect((h.q("m-compose-send") as HTMLButtonElement).disabled).toBe(true);
    expect(h.q("m-compose-send")!.textContent).toBe("Sending…");
    await h.click("m-compose-send");
    expect(h.posts.length).toBe(1);
    expect(h.root.querySelectorAll('[data-testid="m-msg"]').length).toBe(1);
    const reads = h.asked.filter((p) => p.includes("/log?after=")).length;
    h.gets[`/m/api/threads/${CHAT}/log?after=1`] = h.ok({ rows: [{ seq: 1, role: "owner", text: "add a test", at: null,
      marks: ["via the phone"] }], next: 2, more: false, older: false, thread: null });
    release({ status: 202, body: { status: "queued", position: 1, turn_id: "t", message_id: "m" } });
    await h.settle();
    expect(h.asked.filter((p) => p.includes("/log?after=")).length).toBeGreaterThan(reads);
    expect(h.root.querySelectorAll('[data-testid="m-msg"]').length).toBe(2);
    expect(h.q("m-compose-note")!.textContent).toBe("Queued: it runs after the current turn.");
    expect((h.q("m-compose-text") as HTMLTextAreaElement).value).toBe("");
  });

  it("keeps the words and shows the refusal as text; offline it sends nothing", async () => {
    const h = harness();
    h.replies[`/m/api/threads/${CHAT}/messages`] = { status: 409, body: { error: H } };
    await openChat(h);
    h.type("m-compose-text", "hello");
    await h.click("m-compose-send");
    expect(h.q("m-compose-note")!.textContent).toBe(H[0].toUpperCase() + H.slice(1) + ".");
    expect(h.root.querySelectorAll(MARKUP).length).toBe(0);
    expect((h.q("m-compose-text") as HTMLTextAreaElement).value).toBe("hello");
    h.replies[`/m/api/threads/${CHAT}/messages`] = { status: 429, body: { error: "too many" } };
    await h.click("m-compose-send");
    expect(h.q("m-compose-note")!.textContent).toMatch(/Too many from this phone/);
    // Offline: the box says so and Send is off; nothing is queued for later.
    h.setOffline(true);
    h.app.setOnline(false);
    await h.settle();
    expect(h.q("m-compose-offline")).not.toBeNull();
    expect((h.q("m-compose-send") as HTMLButtonElement).disabled).toBe(true);
    const before = h.posts.length;
    await h.click("m-compose-send");
    await h.app.sendMessage(CHAT, "x");
    expect(h.posts.length).toBe(before);
  });

  it("an answer that never came back says it may have been sent; a retry reuses the draft's id", async () => {
    const h = harness();
    await openChat(h);
    h.type("m-compose-text", "hello");
    h.replies[`/m/api/threads/${CHAT}/messages`] = { status: 0, body: {}, offline: true };
    await h.click("m-compose-send");
    expect(h.q("m-compose-note")!.textContent).toBe(MAYBE_SENT);
    expect(h.root.textContent).not.toContain("Not sent");
    expect((h.q("m-compose-text") as HTMLTextAreaElement).value).toBe("hello");
    h.app.setOnline(true);
    await h.settle();
    h.replies[`/m/api/threads/${CHAT}/messages`] = { status: 502, body: {} };
    await h.click("m-compose-send");
    expect(h.q("m-compose-note")!.textContent).toBe(MAYBE_SENT);
    h.app.setOnline(true);
    await h.settle();
    h.replies[`/m/api/threads/${CHAT}/messages`] = { status: 202, body: { status: "started", repeat: true } };
    await h.click("m-compose-send");
    const ids = h.posts.map((p) => p.value.client_id);
    expect(new Set(ids).size).toBe(1);
    // The next message is a new id; so are edited words.
    h.type("m-compose-text", "another");
    await h.click("m-compose-send");
    expect(h.posts[3].value.client_id).not.toBe(ids[0]);
    const map = new Map<string, { id: string; text: string }>();
    const a = clientIdFor(map, "k", "one");
    expect(clientIdFor(map, "k", "one")).toBe(a);
    expect(clientIdFor(map, "k", "two")).not.toBe(a);
  });

  it("the box waits until the chat is known to be an owner's", async () => {
    const h = harness();
    let release!: (g: Got) => void;
    h.app.start();
    await h.settle();
    const pending = new Promise<Got>((r) => { release = r; });
    const got = h.gets[`/m/api/threads/${CHAT}/log`];
    h.gets[`/m/api/threads/${CHAT}/log`] = undefined as unknown as Got;
    const real = (h.app as unknown as { fetchJson: (p: string) => Promise<Got> }).fetchJson;
    (h.app as unknown as { fetchJson: (p: string) => Promise<Got> }).fetchJson = (p) =>
      p === `/m/api/threads/${CHAT}/log` ? pending : real(p);
    const opening = h.app.openThread(CHAT);
    await h.settle();
    expect(h.q("m-thread")).not.toBeNull();
    expect(h.q("m-compose")).toBeNull();
    release(got);
    await opening;
    await h.settle();
    expect(h.q("m-compose")).not.toBeNull();
  });

  it("a task's chat has no message box", async () => {
    const h = harness();
    h.app.start();
    await h.settle();
    await h.app.openThread(OTHER);
    await h.settle();
    expect(h.q("m-compose")).toBeNull();
    expect(h.root.querySelector('[data-testid="m-thread"]')!.textContent).toMatch(/read only on the phone/);
  });

  it("words what the send answered", () => {
    expect(sentNote({ status: "started" })).toBe("Sent.");
    expect(sentNote({ status: "queued", position: 2 })).toBe("Queued: it runs after the current turn (2 waiting).");
    expect(refusal(409, { error: "a turn is already running" })).toBe("A turn is already running.");
    expect(refusal(500, {})).toBe("Not sent (500).");
  });
});

describe("the New chat sheet (P-5)", () => {
  it("offers the projects and the available providers, then opens the new chat", async () => {
    const h = harness();
    h.app.start();
    await h.settle();
    await h.click("m-tab-chats");
    await h.click("m-new-chat-open");
    const providers = Array.from((h.q("m-new-provider") as HTMLSelectElement).options).map((o) => [o.value, o.disabled]);
    expect(providers).toEqual([["fast", false], ["claude", false], ["codex", true]]);
    expect(Array.from((h.q("m-new-project") as HTMLSelectElement).options).map((o) => o.value)).toEqual([PROJECT, "eeeeeeee"]);
    expect((h.q("m-new-start") as HTMLButtonElement).disabled).toBe(true);
    const provider = h.q("m-new-provider") as HTMLSelectElement;
    provider.value = "claude";
    provider.dispatchEvent(new Event("change"));
    await h.settle();
    const model = h.q("m-new-model") as HTMLSelectElement;
    expect(Array.from(model.options).map((o) => o.value)).toEqual(["", "claude-opus-5-5"]);
    model.value = "claude-opus-5-5";
    model.dispatchEvent(new Event("change"));
    h.type("m-new-text", "start here");
    const NEW = "ffffffff";
    h.replies["/m/api/threads"] = { status: 201, body: { thread: { id: NEW }, send: { status: "started" } } };
    h.gets[`/m/api/threads/${NEW}/log`] = h.ok({ rows: [{ seq: 0, role: "owner", text: "start here", at: null, marks: [] }],
      next: 1, more: false, older: false, thread: { id: NEW, project_id: PROJECT, role: "chat", title: "", provider: "claude" } });
    await h.click("m-new-start");
    expect(h.posts).toEqual([{ path: "/m/api/threads", value: { project_id: PROJECT, provider: "claude",
      model: "claude-opus-5-5", text: "start here", client_id: expect.stringMatching(/^[A-Za-z0-9-]{8,64}$/) } }]);
    expect(h.q("m-new-chat")).toBeNull();
    expect(h.q("m-thread")).not.toBeNull();
    expect(h.q("m-compose-note")!.textContent).toBe("Sent.");
  });

  it("says why when it is refused, and links a chat opened anyway", async () => {
    const h = harness();
    h.app.start();
    await h.settle();
    await h.app.openNewChat();
    await h.settle();
    h.type("m-new-text", "x");
    h.replies["/m/api/threads"] = { status: 409, body: { error: "a turn is already running", thread: { id: "ffffffff" } } };
    await h.click("m-new-start");
    expect(h.q("m-sheet-note")!.textContent).toBe("A turn is already running.");
    expect(h.q("m-sheet-open")).not.toBeNull();
    expect((h.q("m-new-text") as HTMLTextAreaElement).value).toBe("x");
  });

  it("reads only the offered providers", () => {
    expect(parseModels({ providers: { fast: { label: "J", available: true, models: [{ id: 3 }, { id: "m" }] },
      evil: { label: "x", available: true } } })).toEqual([{ name: "fast", label: "J", available: true, models: [{ id: "m", name: "m" }] }]);
  });
});

describe("task verbs (P-5)", () => {
  async function openTask(h: ReturnType<typeof harness>, state: string) {
    h.gets[`/m/api/tasks/${TASK}`] = h.ok({ task: task(state) });
    h.app.start();
    await h.settle();
    await h.app.openTask(TASK);
    await h.settle();
  }

  it("offers per state: Start in intake, Resume when blocked, Cancel until it ends", () => {
    expect(verbsFor("intake")).toEqual(["start", "cancel"]);
    expect(verbsFor("blocked")).toEqual(["resume", "cancel"]);
    expect(verbsFor("running")).toEqual(["cancel"]);
    for (const end of ["done", "failed", "cancelled"]) expect(verbsFor(end)).toEqual([]);
  });

  it("no Start on a proposal still inside its grace window", async () => {
    const h = harness();
    h.gets["/m/api/inbox"] = h.ok({ rows: [{ id: `prop:${TASK}`, kind: "proposal", rank: 3, title: "t", blocks: "b",
      project_id: null, thread_id: null, task_id: TASK, asked_at: null, expires_at: null, origin: "", detail: {} }] });
    await openTask(h, "intake");
    expect(h.q("m-task-start")).toBeNull();
    expect(h.q("m-task-cancel")).not.toBeNull();
    expect(h.q("m-task-controls")!.textContent).toMatch(/starts on its own/);
  });

  it("a verb whose answer never came back says it may not have gone through", async () => {
    const h = harness();
    await openTask(h, "intake");
    h.replies[`/m/api/tasks/${TASK}/start`] = { status: 504, body: {} };
    await h.click("m-task-start");
    await h.click("m-task-confirm");
    expect(h.q("m-task-note")!.textContent).toBe(MAYBE_DONE);
  });

  it("Start asks first, then posts the HUD's empty body", async () => {
    const h = harness();
    await openTask(h, "intake");
    await h.click("m-task-start");
    expect(h.posts).toEqual([]);
    expect(h.q("m-task-ask")!.textContent).toMatch(/^Start this task\?/);
    await h.click("m-task-back");
    expect(h.q("m-task-ask")).toBeNull();
    await h.click("m-task-start");
    h.gets[`/m/api/tasks/${TASK}`] = h.ok({ task: task("clarifying") });
    await h.click("m-task-confirm");
    expect(h.posts).toEqual([{ path: `/m/api/tasks/${TASK}/start`, value: {} }]);
    expect(h.q("m-task-note")!.textContent).toBe("Started.");
    expect(h.q("m-task-start")).toBeNull();
  });

  it("Resume asks first; a refusal is shown as text", async () => {
    const h = harness();
    await openTask(h, "blocked");
    h.replies[`/m/api/tasks/${TASK}/resume`] = { status: 409, body: { error: H } };
    await h.click("m-task-resume");
    await h.click("m-task-confirm");
    expect(h.posts).toEqual([{ path: `/m/api/tasks/${TASK}/resume`, value: {} }]);
    expect(h.root.querySelectorAll(MARKUP).length).toBe(0);
    expect(h.q("m-task-note")!.textContent).toContain(H.slice(1));
  });

  it("Cancel asks, then takes the passkey on its own challenge, then sends the assertion", async () => {
    const h = harness();
    await openTask(h, "running");
    expect(h.q("m-task-start")).toBeNull();
    expect(h.q("m-task-resume")).toBeNull();
    h.replies[`/m/api/tasks/${TASK}/cancel/options`] = { status: 200, body: { publicKey: { challenge: "AAEC" } } };
    await h.click("m-task-cancel");
    expect(h.posts).toEqual([]);
    expect(h.q("m-task-ask")!.textContent).toMatch(/cannot be resumed/);
    // The passkey refused (Face ID cancelled): nothing is cancelled.
    h.credential.mockResolvedValueOnce(null);
    await h.click("m-task-confirm");
    expect(h.posts.map((p) => p.path)).toEqual([`/m/api/tasks/${TASK}/cancel/options`]);
    expect(h.q("m-task-note")!.textContent).toMatch(/The task goes on/);
    await h.click("m-task-cancel");
    h.gets[`/m/api/tasks/${TASK}`] = h.ok({ task: task("cancelled") });
    await h.click("m-task-confirm");
    expect(h.credential).toHaveBeenLastCalledWith({ challenge: "AAEC" });
    expect(h.posts.slice(1)).toEqual([{ path: `/m/api/tasks/${TASK}/cancel/options`, value: {} },
      { path: `/m/api/tasks/${TASK}/cancel`, value: { assertion: { id: "cred", response: {} } } }]);
    expect(h.q("m-task-controls")).toBeNull();
    expect(h.root.textContent).toContain("Cancelled.");
  });

  it("nothing for a task that has ended; offline, every verb is off", async () => {
    const view = taskControlsView({ state: "running" } as TaskView, { confirming: null, busy: false, note: "", online: false,
      ask: () => {}, confirm: () => {}, back: () => {} });
    expect((view.querySelector('[data-testid="m-task-cancel"]') as HTMLButtonElement).disabled).toBe(true);
    expect(view.querySelector('[data-testid="m-task-offline"]')).not.toBeNull();
    const h = harness();
    return openTask(h, "done").then(() => { expect(h.q("m-task-controls")).toBeNull(); });
  });

  it("New task: made in the project, then opened, waiting for Start", async () => {
    const h = harness();
    h.app.start();
    await h.settle();
    await h.click("m-tab-tasks");
    await h.click("m-new-task-open");
    await h.settle();
    expect((h.q("m-new-create") as HTMLButtonElement).disabled).toBe(true);
    h.type("m-new-brief", "add divide");
    h.replies["/m/api/tasks"] = { status: 201, body: { task: task("intake") } };
    await h.click("m-new-create");
    expect(h.posts).toEqual([{ path: "/m/api/tasks", value: { project_id: PROJECT, brief: "add divide",
      client_id: expect.stringMatching(/^[A-Za-z0-9-]{8,64}$/) } }]);
    expect(h.q("m-task-view")).not.toBeNull();
    expect(h.q("m-task-start")).not.toBeNull();
    expect(h.q("m-task-note")!.textContent).toBe("Created. It waits for Start.");
  });
});

describe("a proposal's withdraw (P-5)", () => {
  const PROP = { id: `prop:${TASK}`, kind: "proposal", rank: 3, title: "t", blocks: "b", project_id: null, thread_id: null,
    task_id: TASK, asked_at: null, expires_at: null, origin: "", detail: { brief: H, task_id: TASK } };

  it("asks once, then sends {withdraw: true} on the session", async () => {
    const h = harness();
    h.gets["/m/api/inbox"] = h.ok({ rows: [PROP] });
    h.app.start();
    await h.settle();
    expect(h.q("m-desk-only")).toBeNull();
    await h.click("m-withdraw");
    expect(h.posts).toEqual([]);
    expect(h.q("m-withdraw-ask")!.textContent).toBe("Withdraw this proposal? The task will not start.");
    await h.click("m-withdraw-back");
    expect(h.q("m-withdraw-ask")).toBeNull();
    await h.click("m-withdraw");
    await h.click("m-withdraw-send");
    expect(h.posts).toEqual([{ path: `/m/api/inbox/${encodeURIComponent(PROP.id)}/answer`, value: { withdraw: true } }]);
    expect(h.q("m-flash")!.textContent).toBe("Withdrawn: the task will not start.");
    expect(h.root.querySelectorAll(MARKUP).length).toBe(0);
  });

  it("without the app's withdraw (the P-3/P-4 view), it still says Withdraw at the desk", () => {
    const answers: InboxAnswers = { openCard: () => {}, send: () => {}, busy: new Set(), notes: new Map(), drafts: new Map(), picks: new Map() };
    const view = inboxView(parseInbox([PROP])!, () => {}, answers);
    expect(view.querySelector('[data-testid="m-desk-only"]')!.textContent).toBe("Withdraw at the desk");
    expect(view.querySelector('[data-testid="m-withdraw"]')).toBeNull();
  });
});
