import { expect, it, vi } from "vitest";
import { mkEntry } from "./threads.fixtures";
import { SlotCache, threadsView, type ListActions, type ListModel } from "./threadsView";

it("RD-3: a capped working read cannot prove that nothing is running", () => {
  // /m/api/working sorts failed before working, then caps at 100
  // (remote_api.py:600-657). Thus 100 failed chats can precede a 101st
  // working chat. The 101st entry is deliberately absent from this read.
  const model: ListModel = {
    rows: [],
    entries: Array.from({ length: 100 }, (_, i) => mkEntry(`d${i.toString(16).padStart(7, "0")}`, "failed")),
    working: "ok", workingCut: { more: true, total: 101 },
    tasks: [], tasksMore: false, inboxMore: false,
    filter: null, recentAll: false, error: null,
  };
  const actions: ListActions = {
    answers: { openCard: vi.fn(), send: vi.fn(), retry: vi.fn(), busy: new Set(), notes: new Map(),
      drafts: new Map(), picks: new Map() },
    openRow: vi.fn(), openThread: vi.fn(), openTask: vi.fn(),
    setFilter: vi.fn(), setRecentAll: vi.fn(), retry: vi.fn(),
  };
  const view = threadsView(model, actions, new SlotCache(), Date.parse("2026-11-15T10:30:00Z"), null);
  expect(view.querySelector('[data-testid="m-working-more"]')?.textContent).toContain("Showing 100 of 101");
  expect(view.querySelectorAll('[data-testid="m-empty"]')).toHaveLength(1);
  expect(view.querySelector('[data-testid="m-empty"]')?.textContent).not.toContain("Nothing is running.");
});
