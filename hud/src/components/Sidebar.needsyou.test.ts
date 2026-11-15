// WS-3 in the sidebar: the rainbow Inbox row is the In progress list's header (a click folds it, the choice
// lasts), a "Needs you" line is always at the top of the unfolded list (at 0 too) and opens the page, a
// thread's hint names what waits on it and opens the page at that thread, and a row that waits on a thread
// is never also listed in the Needs-you rows.

import { createElement, act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { working } from "../state/working";
import { Sidebar } from "./Sidebar";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const thread = (id: string, title: string) => ({ id, project_id: "p1", role: "chat", provider: "fast", title, turns: 1,
  created: "", updated: "", cwd: "/x", model: null, task_id: null } as never);
const project = { id: "p1", name: "jarvis", root: "/x", inbox: false } as never;
const entry = (id: string, rows: unknown[] = []) => ({ thread_id: id, project_id: "p1", since: "",
  last: "2026-11-14T10:00:00+00:00", seq: 1, status: "idle", rows, why_not: null });
const row = (id: string, kind: string, rank: number, extra: Record<string, unknown> = {}) => ({
  id, kind, rank, title: `${kind} ${id}`, blocks: "", project_id: "p1", thread_id: null, task_id: null,
  asked_at: "2026-11-14T09:00:00+00:00", expires_at: null, origin: "", detail: {}, ...extra });

let root: Root | null = null;
let host: HTMLElement | null = null;
const calls: unknown[][] = [];

async function draw(view: { entries: unknown[]; needs_you: unknown[] }, inbox = { count: 2 as number | null, pending: false, shown: false }) {
  vi.spyOn(api, "getWorking").mockImplementation(async () => ({ ...view, count: 0, pending: false }));
  const noop = () => {};
  const props = {
    projects: [project], platforms: {}, threads: [thread("t1", "desk chat"), thread("t2", "second chat")], tasks: [],
    taskThreads: {}, activeProjectId: null, open: [], composing: [], taskId: null, moveError: "",
    onPickThread: noop, onPickTask: noop, onNewProject: noop, onNewThread: noop, onNewTask: noop,
    onMoveThread: noop, onMoveCompose: noop, onOpen: noop, inbox,
    onOpenInbox: (...a: unknown[]) => { calls.push(a); },
  };
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => { root!.render(createElement(Sidebar, props as never)); });
  await act(async () => { await working.refresh(); });
}
const q = (sel: string) => host!.querySelector(sel) as HTMLElement | null;
const qa = (sel: string) => Array.from(host!.querySelectorAll(sel)) as HTMLElement[];

beforeEach(() => { calls.length = 0; localStorage.clear(); });
afterEach(async () => {
  await act(async () => { root?.unmount(); });
  host?.remove();
  root = null;
  host = null;
  vi.restoreAllMocks();
});

describe("the rainbow Inbox row as the list's header", () => {
  it("folds and unfolds the list, follows with aria-expanded, and keeps the choice", async () => {
    await draw({ entries: [entry("t1")], needs_you: [] });
    const r = q('[data-testid="owner-inbox"]')!;
    expect(r.getAttribute("aria-expanded")).toBe("true");
    expect(r.getAttribute("aria-controls")).toBe(q('[data-testid="working-section"]')!.id);
    expect(r.getAttribute("aria-label")).toBe("Inbox, 2 waiting on you");
    expect(qa('[data-testid="working-row"]').length).toBe(1);
    await act(async () => { r.click(); });
    expect(r.getAttribute("aria-expanded")).toBe("false");
    expect(qa('[data-testid="working-row"]').length).toBe(0);
    expect(q('[data-testid="working-needs-line"]')).toBeNull();
    expect(localStorage.getItem("jarvis.hud.workingSection")).toBe("0");
    expect(calls.length).toBe(0);                       // the row never opens the page itself
    await act(async () => { r.click(); });
    expect(r.getAttribute("aria-expanded")).toBe("true");
    expect(qa('[data-testid="working-row"]').length).toBe(1);
    expect(localStorage.getItem("jarvis.hud.workingSection")).toBe("1");
  });

  it("starts folded when the stored choice says so", async () => {
    localStorage.setItem("jarvis.hud.workingSection", "0");
    await draw({ entries: [entry("t1")], needs_you: [] });
    expect(q('[data-testid="owner-inbox"]')!.getAttribute("aria-expanded")).toBe("false");
    expect(qa('[data-testid="working-row"]').length).toBe(0);
  });

  it("there is no separate In progress header line any more", async () => {
    await draw({ entries: [entry("t1")], needs_you: [] });
    expect(q('[data-testid="working-toggle"]')).toBeNull();
    expect(q(".workinghead")).toBeNull();
  });
});

describe("the Needs you line", () => {
  it("is shown when unfolded even with nothing waiting, reads quiet, and still opens the page", async () => {
    await draw({ entries: [], needs_you: [] }, { count: 0, pending: false, shown: false });
    const line = q('[data-testid="working-needs-line"]')!;
    expect(line).toBeTruthy();
    expect(line.textContent).toBe("Needs you · 0");
    expect(line.className).toContain("quiet0");
    await act(async () => { line.click(); });
    expect(calls).toEqual([[]]);
  });

  it("an unread count is a mark, never a zero: still clickable, and it does not announce 'nothing waiting'", async () => {
    await draw({ entries: [], needs_you: [] }, { count: null, pending: false, shown: false });
    const line = q('[data-testid="working-needs-line"]')!;
    expect(line.textContent).toBe("Needs you · –");
    expect(line.className).not.toContain("quiet0");
    expect(line.getAttribute("aria-label")).toBe("Needs you, count not known yet");
    expect(line.getAttribute("data-count")).toBe("unknown");
    await act(async () => { line.click(); });
    expect(calls).toEqual([[]]);
  });

  it("carries the waiting count, amber while something is pending, and opens the page", async () => {
    await draw({ entries: [entry("t1")], needs_you: [] }, { count: 3, pending: true, shown: false });
    const line = q('[data-testid="working-needs-line"]')!;
    expect(line.textContent).toBe("Needs you · 3");
    expect(line.className).toContain("pending");
    expect(line.getAttribute("aria-label")).toBe("Needs you, 3 waiting");
    await act(async () => { line.click(); });
    expect(calls).toEqual([[]]);
  });

  it("keeps the thread-less rows listed under it", async () => {
    await draw({ entries: [entry("t1")], needs_you: [row("pr:r1", "pr", 5)] });
    expect(qa('[data-testid="working-need"]').map((e) => e.dataset.id)).toEqual(["pr:r1"]);
    await act(async () => { q('[data-testid="working-need"]')!.click(); });
    expect(calls).toEqual([[]]);
  });
});

describe("what waits on a thread", () => {
  it("shows its hint with the kind, opens the page at that thread, and never also lists the row", async () => {
    const attached = [row("appr:a1", "approval", 1, { thread_id: "t1" })];
    await draw({ entries: [entry("t1", attached), entry("t2")], needs_you: [row("pr:r1", "pr", 5)] });
    const hint = q('[data-testid="working-hint-t1"]')!;
    expect(hint.textContent).toBe("1 approval");
    expect(q('[data-testid="working-hint-t2"]')).toBeNull();
    expect(qa('[data-testid="working-need"]').map((e) => e.dataset.id)).toEqual(["pr:r1"]);
    await act(async () => { hint.click(); });
    expect(calls).toEqual([["t1"]]);
  });

  it("drops a needs-you row that also sits on a drawn thread (it is drawn once, on the thread)", async () => {
    const dup = row("appr:a1", "approval", 1, { thread_id: "t1" });
    await draw({ entries: [entry("t1", [dup])], needs_you: [dup, row("pr:r1", "pr", 5)] });
    expect(qa('[data-testid="working-need"]').map((e) => e.dataset.id)).toEqual(["pr:r1"]);
  });

  it("keeps a row whose thread is not drawn (archived) in the Needs-you rows, urgent ranks 1-4 included", async () => {
    const rows = [row("appr:a1", "approval", 1, { thread_id: "gone" }), row("q:g:r1", "question", 2, { thread_id: "gone" }),
                  row("prop:k", "proposal", 3, { thread_id: "gone" }), row("def:d", "deferred", 4, { thread_id: "gone" }),
                  row("dec:d1", "decision", 6, { thread_id: "gone" })];
    await draw({ entries: [entry("t1")], needs_you: rows });
    expect(qa('[data-testid="working-need"]').map((e) => e.dataset.id)).toEqual(rows.map((r) => r.id));
  });
});
