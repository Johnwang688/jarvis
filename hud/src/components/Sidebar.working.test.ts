// The Inbox section's refreshes (WS-2 review, item 4): the store is read by the
// section's own container, so a refresh redraws that block and never the
// Sidebar; and a refresh that finds the list unchanged redraws nothing.

import { createElement, act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { working } from "../state/working";
import { Sidebar } from "./Sidebar";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const thread = { id: "t1", project_id: "p1", role: "chat", provider: "fast", title: "desk chat", turns: 1,
  created: "", updated: "", cwd: "/x", model: null, task_id: null } as never;
const project = { id: "p1", name: "jarvis", root: "/x", inbox: false } as never;
const entry = (seq: number) => ({ thread_id: "t1", project_id: "p1", since: "", last: "2026-11-14T10:00:00+00:00",
  seq, status: "idle", rows: [], why_not: null });

afterEach(() => vi.restoreAllMocks());

describe("Sidebar and the working-set store", () => {
  it("three identical refreshes redraw nothing, and a changed list redraws the section but not the Sidebar", async () => {
    let seq = 1;
    vi.spyOn(api, "getWorking").mockImplementation(async () => ({ entries: [entry(seq)], needs_you: [], count: 0, pending: false }));
    let sidebarRenders = 0;
    const tasks: unknown[] = [];
    // Sidebar filters its tasks once per project on every render of its own, and nothing else touches them.
    (tasks as { filter: unknown }).filter = function (this: unknown[], ...a: unknown[]) {
      sidebarRenders++;
      return Array.prototype.filter.apply(this, a as [never]);
    };
    const noop = () => {};
    const props = {
      projects: [project], platforms: {}, threads: [thread], tasks: tasks as never, taskThreads: {}, activeProjectId: null,
      open: [], composing: [], taskId: null, moveError: "", onPickThread: noop, onPickTask: noop, onNewProject: noop,
      onNewThread: noop, onNewTask: noop, onMoveThread: noop, onMoveCompose: noop, onOpen: noop,
      inbox: { count: null, pending: false, shown: false }, onOpenInbox: noop,
    };
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    await act(async () => { root.render(createElement(Sidebar, props as never)); });
    await act(async () => { await working.refresh(); });
    expect(host.querySelectorAll('[data-testid="working-row"]').length).toBe(1);
    const base = sidebarRenders;
    for (let i = 0; i < 3; i++) await act(async () => { await working.refresh(); });
    expect(sidebarRenders).toBe(base);
    seq = 2;
    await act(async () => { await working.refresh(); });
    expect(sidebarRenders).toBe(base);       // the section redrew; the Sidebar did not
    expect(host.querySelectorAll('[data-testid="working-row"]').length).toBe(1);
    await act(async () => { root.unmount(); });
    host.remove();
  });
});
