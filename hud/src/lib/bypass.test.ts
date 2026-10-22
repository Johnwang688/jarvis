import { describe, expect, it } from "vitest";
import { bypassFor } from "./bypass";
import type { Thread } from "../types";

const thread = (over: Partial<Thread> = {}): Thread => ({
  id: "abcd1234", project_id: "p0000001", role: "chat", provider: "claude", task_id: null,
  title: "t", state: "open", turns: 1, cost_usd: 0, tokens: 0, model: null, effort: null,
  ...over,
} as Thread);

describe("bypassFor", () => {
  it("offers the switch on an opened Claude chat, off by default", () => {
    const view = bypassFor(thread());
    expect(view?.on).toBe(false);
    expect(view?.title).toMatch(/Jarvis's own rules and cards still apply/);
  });
  it("reads the daemon's live bypass field", () => {
    expect(bypassFor(thread({ bypass: true }))?.on).toBe(true);
  });
  it("offers nothing without a classifier, for a task, or when archived", () => {
    expect(bypassFor(null)).toBeNull();
    expect(bypassFor(thread({ provider: "codex" }))).toBeNull();
    expect(bypassFor(thread({ provider: "fast" }))).toBeNull();
    expect(bypassFor(thread({ task_id: "t0000001" }))).toBeNull();
    expect(bypassFor(thread({ role: "worker" as Thread["role"] }))).toBeNull();
    expect(bypassFor(thread({ archived: "2026-10-21T00:00:00Z" }))).toBeNull();
  });
});
