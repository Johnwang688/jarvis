import { afterEach, expect, it, vi } from "vitest";
import { PhoneApp } from "./app";
import type { ChatThread, WorkingItem } from "./model";
import type { Got } from "./net";

// Independent RD-5 counterexamples. No network, real session, or writes.
const project = { id: "cccccccc", name: "Project", inbox: false };
const chat: ChatThread = { id: "aaaaaaaa", project_id: project.id, task_id: null,
  role: "chat", title: "Chat", provider: "claude", updated: "2026-11-15T10:00:00Z",
  running: false, queued: 0, turns: 0 };
const apps: PhoneApp[] = [];
afterEach(() => { apps.forEach(a => a.stop()); apps.length = 0; document.body.replaceChildren(); localStorage.clear(); });

function setup() {
  const root = document.createElement("main");
  document.body.append(root);
  // Startup reads stay pending; each explicit refresh below receives its own response.
  const pending = new Map<string, (got: Got) => void>();
  const fetchJson = vi.fn<(path: string) => Promise<Got>>(path => new Promise(resolve => pending.set(path, resolve)));
  const app = new PhoneApp({ root, fetchJson, onSignedOut: vi.fn(), signOut: async () => {},
    postJson: () => new Promise(() => {}),
    openStream: () => ({ readyState: 0, onopen: null, onmessage: null, onerror: null, close: vi.fn() }) });
  apps.push(app);
  app.start();
  app.tab = "chats";
  app.projects = [project];
  app.threads = [chat];
  app.inbox = [];
  app.entries = [];
  app.render();
  const head = () => root.querySelector<HTMLButtonElement>('[data-testid="m-project-head"]')!;
  const finish = (path: string, body: unknown) => pending.get(path)!({ ok: true, body });
  return { app, fetchJson, head, finish };
}

it("RD-5 keeps keyboard focus on the project head after a thread refresh", async () => {
  const { app, fetchJson, head } = setup();
  head().focus();
  head().click();
  expect(document.activeElement).toBe(head()); // toggle itself works
  fetchJson.mockResolvedValue({ ok: true, body: { threads: [{ ...chat, title: "Refreshed" }] } });
  // This is the read/render path used by thread_changed SSE.
  await app.loadProjectThreads(project.id);
  expect(app.threads[0].title).toBe("Refreshed");
  expect(head().getAttribute("aria-expanded")).toBe("true");
  expect(document.activeElement).toBe(head());
});

it.each(["working", "needs_input"] as const)("RD-5 updates a visible project's %s count and activity default after working refresh", async status => {
  const { app, head, finish } = setup();
  expect(head().getAttribute("aria-expanded")).toBe("false");
  const entry: WorkingItem = { thread_id: chat.id, project_id: project.id, project: project.name,
    title: chat.title, status, last: chat.updated, seq: null };
  finish("/m/api/working", { entries: [entry], more: false, total: 1 });
  await vi.waitFor(() => expect(app.entries?.[0]?.status).toBe(status));
  expect(head().textContent).toContain(status === "working" ? "1 running" : "1 need you");
  expect(head().getAttribute("aria-expanded")).toBe("true");
});

it("RD-5 updates needs-you counts when the inbox read completes while Chats is visible", async () => {
  const { app, head, finish } = setup();
  finish("/m/api/inbox", { rows: [{ id: "q:1", kind: "question", rank: 2,
    thread_id: chat.id, project_id: project.id, title: "Which?", detail: {} }] });
  await vi.waitFor(() => expect(app.inbox).toHaveLength(1));
  expect(head().textContent).toContain("1 need you");
  expect(head().getAttribute("aria-expanded")).toBe("true");
});
