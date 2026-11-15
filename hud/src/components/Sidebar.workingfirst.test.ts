// The rainbow Inbox row before and around the first /working read (review of PR #368): a first read that
// fails, whatever the error, leaves it a plain button that opens the Needs you page; and the row is the
// same element throughout, so focus on it survives the first read landing. Each test loads a fresh
// module graph, because the working store is a singleton that remembers its first read.

import { afterEach, describe, expect, it, vi } from "vitest";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const thread = { id: "t1", project_id: "p1", role: "chat", provider: "fast", title: "desk chat", turns: 1,
  created: "", updated: "", cwd: "/x", model: null, task_id: null } as never;
const project = { id: "p1", name: "jarvis", root: "/x", inbox: false } as never;

async function boot() {
  vi.resetModules();
  const { createElement, act } = await import("react");
  const { createRoot } = await import("react-dom/client");
  const { api, ApiError } = await import("../api");
  const { working } = await import("../state/working");
  const { Sidebar } = await import("./Sidebar");
  const calls: unknown[][] = [];
  const noop = () => {};
  const props = {
    projects: [project], platforms: {}, threads: [thread], tasks: [], taskThreads: {}, activeProjectId: null,
    open: [], composing: [], taskId: null, moveError: "", onPickThread: noop, onPickTask: noop, onNewProject: noop,
    onNewThread: noop, onNewTask: noop, onMoveThread: noop, onMoveCompose: noop, onOpen: noop,
    inbox: { count: 2, pending: false, shown: false }, onOpenInbox: (...a: unknown[]) => { calls.push(a); },
  };
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => { root.render(createElement(Sidebar, props as never)); });
  return { act, api, ApiError, working, host, root, calls };
}

let cleanup: (() => Promise<void>) | null = null;
afterEach(async () => { await cleanup?.(); cleanup = null; vi.restoreAllMocks(); localStorage.clear(); });

const q = (host: HTMLElement, sel: string) => host.querySelector(sel) as HTMLElement | null;
const okView = { entries: [{ thread_id: "t1", project_id: "p1", since: "", last: "2026-11-14T10:00:00+00:00", seq: 1,
  status: "idle", rows: [], why_not: null }], needs_you: [], count: 0, pending: false };

describe("the rainbow row around the first working read", () => {
  it("a first read that fails with a 500 is the page-opening fallback, not a fold of an empty list", async () => {
    const t = await boot();
    cleanup = async () => { await t.act(async () => { t.root.unmount(); }); t.host.remove(); };
    const ApiErr = t.ApiError as unknown as new (status: number, message: string) => Error;
    vi.spyOn(t.api, "getWorking").mockRejectedValue(new ApiErr(500, "server error"));
    await t.act(async () => { await t.working.refresh(); });
    const row = q(t.host, '[data-testid="owner-inbox"]')!;
    expect(row.hasAttribute("aria-expanded")).toBe(false);
    expect(q(t.host, '[data-testid="working-section"]')).toBeNull();
    expect(q(t.host, '[data-testid="working-stale"]')).not.toBeNull();             // the failure is still said
    await t.act(async () => { row.click(); });
    expect(t.calls).toEqual([[]]);                                                 // it opened the page
  });

  it("a network failure at first is the same fallback; a later good read makes the row fold", async () => {
    const t = await boot();
    cleanup = async () => { await t.act(async () => { t.root.unmount(); }); t.host.remove(); };
    const spy = vi.spyOn(t.api, "getWorking").mockRejectedValue(new Error("network down"));
    await t.act(async () => { await t.working.refresh(); });
    expect(q(t.host, '[data-testid="owner-inbox"]')!.hasAttribute("aria-expanded")).toBe(false);
    spy.mockResolvedValue(okView as never);
    await t.act(async () => { await t.working.refresh(); });
    expect(q(t.host, '[data-testid="owner-inbox"]')!.getAttribute("aria-expanded")).toBe("true");
    // Once a list has landed, a failed refresh keeps it (with the stale mark): the row still folds.
    spy.mockRejectedValue(new Error("network down"));
    await t.act(async () => { await t.working.refresh(); });
    expect(q(t.host, '[data-testid="owner-inbox"]')!.getAttribute("aria-expanded")).toBe("true");
    expect(q(t.host, '[data-testid="working-stale"]')).not.toBeNull();
  });

  it("focus on the row survives the first read landing (the row is never remounted)", async () => {
    const t = await boot();
    cleanup = async () => { await t.act(async () => { t.root.unmount(); }); t.host.remove(); };
    let release!: (v: unknown) => void;
    vi.spyOn(t.api, "getWorking").mockImplementation(() => new Promise((r) => { release = r; }) as never);
    const pending = t.working.refresh();
    const row = q(t.host, '[data-testid="owner-inbox"]') as HTMLButtonElement;
    row.focus();
    expect(document.activeElement).toBe(row);
    await t.act(async () => { release(okView); await pending; });
    expect(q(t.host, '[data-testid="working-section"]')).not.toBeNull();            // the list arrived
    expect(q(t.host, '[data-testid="owner-inbox"]')).toBe(row);                    // the same element
    expect(document.activeElement).toBe(row);
  });
});
