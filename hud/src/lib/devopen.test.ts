import { describe, expect, it } from "vitest";
import {
  DEV_OPEN_IDLE, OPENED_KEPT, WAIT_MS, WINDOW_MAX, autoOpenPlan, devUrl, enqueueDev,
  offerLabel, type DevEntry, type DevOpenState, type DevWindow, type PaneShows,
} from "./devopen";
import {
  PANE_NOS, autoSplit, blankKeptPane, defaultWorkspace, fitWorkspace, freeId, orderOf, setPreset, splitPane, type PaneNo,
} from "./workspace";
import { DEFAULT_LAYOUT } from "./layout";

// FS4 (docs/plans/2026-10-19-hud-free-split-plan.md, the FS4 card): `canSplit`
// became `canSplitFrom`, the drawn panes a split could start from now (the
// window asks lib/workspace `autoSplit` per pane). The test writer's choice
// where the plan is open: the plan's source (the pane holding the source, else
// the focused drawn pane, else the first) splits only when it is in that list;
// otherwise placement falls through to a Browser tab or the prompt, exactly as
// a window that could not split did — a split never starts from another pane.

const T = 1_000_000;
const PORTS = { hud: 18402, api: 18405, workshop: 18403, self: 18402, frameHardened: true };
const pane = (id: PaneNo, over: Partial<PaneShows> = {}): PaneShows => ({ id, view: "chat", urls: [], tabCount: 0, ...over });
const win = (over: Partial<DevWindow> = {}): DevWindow => ({
  enabled: true, blocked: false, covered: false, ports: PORTS, panes: [pane(1)], drawn: [1], focused: 1,
  canSplitFrom: PANE_NOS, pointerDown: false, pointerReleasedAt: 0, ...over,
});
const entry = (over: Partial<DevEntry> = {}): DevEntry => ({
  id: "ds_12345678", url: "http://localhost:5173/", source: { kind: "terminal", id: "t1" }, at: T, ...over,
});
const plan = (e = entry(), w = win(), s: DevOpenState = DEV_OPEN_IDLE, now = T) => autoOpenPlan(e, s, w, now);

describe("automatic Direct placement", () => {
  it("splits beside the drawn source without changing focus", () => {
    for (const kind of ["thread", "task", "terminal"] as const) {
      const source = { kind, id: "source1" };
      const result = plan(entry({ source }), win({ panes: [pane(1), pane(3, { source })], drawn: [1, 3] }));
      expect(result.plan).toEqual({ kind: "split", source: 3, url: "http://localhost:5173/" });
    }
    const result = plan().plan;
    if (result.kind !== "split") throw Error("expected split");
    const one = defaultWorkspace();
    const fit = fitWorkspace(DEFAULT_LAYOUT, one, 1700, 900);
    const got = autoSplit(one, fit, result.source);
    expect(got).toEqual({ axis: "x", id: 2 });
    const ws = splitPane(one, fit, result.source, got!.axis, got!.id, { view: "browser", projectId: null, terminalId: null }, { focus: false });
    expect([orderOf(ws), ws.focused]).toEqual([[1, 2], 1]);
  });
  it("splits only from a source that can split now; otherwise a Browser tab or the prompt, never a split from another pane (FS4)", () => {
    const source = entry().source;
    const panes = [pane(1), pane(3, { source })];
    expect(plan(entry(), win({ panes, drawn: [1, 3], canSplitFrom: [3] })).plan).toMatchObject({ kind: "split", source: 3 });
    expect(plan(entry(), win({ panes, drawn: [1, 3], canSplitFrom: [1] })).plan).toEqual({ kind: "prompt", url: "http://localhost:5173/" });
    const withBrowser = [pane(1), pane(3, { source }), pane(5, { view: "browser" })];
    expect(plan(entry(), win({ panes: withBrowser, drawn: [1, 3, 5], canSplitFrom: [1, 5] })).plan)
      .toMatchObject({ kind: "tab", pane: 5 });
    // No drawn holder: the focused pane is the source, and only it.
    expect(plan(entry(), win({ panes: [pane(1), pane(2)], drawn: [1, 2], focused: 2, canSplitFrom: [2] })).plan)
      .toMatchObject({ kind: "split", source: 2 });
    expect(plan(entry(), win({ panes: [pane(1), pane(2)], drawn: [1, 2], focused: 2, canSplitFrom: [1] })).plan.kind).toBe("prompt");
    // A pane that cannot split is never a reason to wait for the pointer.
    expect(plan(entry(), win({ pointerDown: true, canSplitFrom: [2] })).plan.kind).toBe("prompt");
  });
  it("falls back to the focused drawn pane when the source is hidden or absent", () => {
    expect(plan(entry(), win({ panes: [pane(1), pane(3, { source: entry().source })] })).plan).toMatchObject({ kind: "split", source: 1 });
  });
  it("uses the nearest nonfull drawn Browser, ties after the source", () => {
    const panes = [pane(1, { view: "browser" }), pane(3, { source: entry().source }), pane(7, { view: "browser" })];
    const w = win({ panes, drawn: [1, 3, 7], focused: 3, canSplitFrom: [] });
    expect(plan(entry(), w).plan).toMatchObject({ kind: "tab", pane: 7, activate: true });
    panes[2].tabCount = 12;
    expect(plan(entry(), w).plan).toMatchObject({ kind: "tab", pane: 1 });
    panes[0].tabCount = 12;
    expect(plan(entry(), w).plan.kind).toBe("prompt");
  });
  it("uses Browser MRU without a drawn source, ignoring hidden panes", () => {
    const panes = [pane(1), pane(2, { view: "browser", usedAt: T - 20_000 }),
      pane(3, { view: "browser", usedAt: T - 30_000 }), pane(4, { view: "browser", usedAt: T })];
    expect(plan(entry(), win({ panes, drawn: [1, 2, 3], canSplitFrom: [] })).plan).toMatchObject({ kind: "tab", pane: 2, activate: true });
  });
  it("adds behind a Browser in use by workspace focus, DOM focus, or a recent owner interaction", () => {
    const panes = [pane(1), pane(2, { view: "browser" })];
    const w = win({ panes, drawn: [1, 2], canSplitFrom: [] });
    expect(plan(entry(), { ...w, focused: 2 }).plan).toMatchObject({ activate: false });
    expect(plan(entry(), { ...w, activePane: 2 }).plan).toMatchObject({ activate: false });
    panes[1].usedAt = T - 9_999;
    expect(plan(entry(), w).plan).toMatchObject({ activate: false });
    panes[1].usedAt = T - 10_000;
    expect(plan(entry(), w).plan).toMatchObject({ activate: true });
  });
  it("never places Direct content into a Private browser pane", () => {
    expect(plan(entry(), win({ canSplitFrom: [], panes: [pane(1, { view: "browser", browserProfile: "private" })] })).plan.kind).toBe("prompt");
  });
  it("never silently replaces a focused chat, editor, terminal or VEX view", () => {
    for (const view of ["chat", "file", "terminal", "vex", "task", "diff"] as const) {
      expect(plan(entry(), win({ panes: [pane(1, { view })], canSplitFrom: [] })).plan.kind).toBe("prompt");
    }
  });
});

describe("URL and lifecycle boundaries", () => {
  it.each([
    "http://localhost:18405/", "http://localhost:18402/", "http://localhost:8405/", "http://localhost:8402/",
    "http://localhost:8404/", "http://127.0.0.1:8406/callback", "http://localhost:18403/p/x/",
    "http://192.168.1.4:3000/", "https://example.com/", "http://user:pw@localhost:3000/", "file:///etc/passwd",
    "javascript:alert(1)", "http://[::1]:3000/", "not a url",
  ])("refuses %s", url => {
    expect(plan(entry({ url })).plan).toEqual({ kind: "skip", why: "refused" });
  });
  it("combines reported and default control/workshop ports and strips query/fragment", () => {
    expect(devUrl("http://localhost:19404/", PORTS, [19404])).toBeNull();
    expect(devUrl("http://localhost:8404/", PORTS, [19404])).toBeNull();
    expect(devUrl("http://localhost:8403/", { hud: 18402, api: 18405 })).toBeNull();
    expect(devUrl("http://localhost:19404/", PORTS)).toBe("http://localhost:19404/");
    expect(plan(entry({ url: "http://localhost:8888/lab?token=abc123#cell" })).plan)
      .toEqual({ kind: "split", source: 1, url: "http://localhost:8888/lab" });
    expect(plan(entry({ url: "http://127.0.0.1:5000/?x=1" }), win({ canSplitFrom: [] })).plan)
      .toEqual({ kind: "prompt", url: "http://127.0.0.1:5000/" });
  });
  it("remembers entry IDs but allows a new lifecycle after the old tab is gone", () => {
    const first = plan();
    expect(plan(entry({ at: T + 120_000 }), win(), first.state, T + 120_000).plan).toEqual({ kind: "skip", why: "acted" });
    expect(plan(entry({ id: "ds_87654321", at: T + 120_000 }), win(), first.state, T + 120_000).plan.kind).toBe("split");
  });
  it("deduplicates origins across every retained tab, including non-Browser and hidden panes", () => {
    for (const view of ["browser", "chat"] as const) {
      expect(plan(entry(), win({ panes: [pane(1), pane(4, { view, urls: ["http://localhost:3000/", "http://localhost:5173/about"] })] })).plan)
        .toEqual({ kind: "skip", why: "duplicate" });
    }
    expect(plan(entry({ url: "http://127.0.0.1:5173/" }), win({ panes: [pane(1, { urls: ["http://localhost:5173/"] })] })).plan.kind).toBe("split");
  });
  it("applies one/10s/source and independently three/min/window", () => {
    let s = plan().state;
    expect(plan(entry({ id: "ds_00000002", at: T + 9_999 }), win(), s, T + 9_999).plan)
      .toEqual({ kind: "skip", why: "rate" });
    expect(plan(entry({ id: "ds_00000002", at: T + 10_000 }), win(), s, T + 10_000).plan.kind).toBe("split");
    s = DEV_OPEN_IDLE;
    for (let i = 0; i < 3; i++) {
      const result = plan(entry({ id: String(i), source: { kind: "task", id: String(i) } }), win(), s, T + i);
      expect(result.plan.kind).toBe("split"); s = result.state;
    }
    expect(plan(entry({ source: { kind: "thread", id: "new" } }), win(), s, T + 100).plan).toEqual({ kind: "skip", why: "rate" });
    expect(plan(entry({ at: T + 60_000 }), win(), s, T + 60_000).plan.kind).toBe("split");
  });
  it("keeps remembered IDs and rate history bounded", () => {
    let s = DEV_OPEN_IDLE;
    for (let i = 0; i < OPENED_KEPT + 20; i++) {
      const now = T + i * 61_000;
      s = plan(entry({ id: String(i), at: now }), win(), s, now).state;
    }
    expect(s.opened.length).toBe(OPENED_KEPT);
    expect(s.acts.length).toBeLessThanOrEqual(WINDOW_MAX);
  });
});

describe("deferral", () => {
  it("off consumes no rate; cards and Settings defer every placement without consuming an act", () => {
    const off = plan(entry(), win({ enabled: false }));
    expect(off.plan).toEqual({ kind: "skip", why: "off" }); expect(off.state).toBe(DEV_OPEN_IDLE);
    for (const canSplitFrom of [[], PANE_NOS]) for (const flag of ["blocked", "covered"] as const) {
      const held = plan(entry(), win({ [flag]: true, canSplitFrom }));
      expect(held.plan).toEqual({ kind: "wait", why: flag === "blocked" ? "card" : "covered" });
      expect(held.state).toBe(DEV_OPEN_IDLE);
    }
  });
  it("defers splitting until 150ms after pointer release but does not defer a fallback tab", () => {
    expect(plan(entry(), win({ pointerDown: true })).plan).toEqual({ kind: "wait", why: "pointer" });
    expect(plan(entry(), win({ pointerReleasedAt: T - 149 })).plan.kind).toBe("wait");
    expect(plan(entry(), win({ pointerReleasedAt: T - 150 })).plan.kind).toBe("split");
    expect(plan(entry(), win({ pointerDown: true, canSplitFrom: [], panes: [pane(1, { view: "browser" })] })).plan.kind).toBe("tab");
  });
  it("expires after ten minutes, including under a card", () => {
    expect(plan(entry(), win({ blocked: true }), DEV_OPEN_IDLE, T + WAIT_MS - 1).plan.kind).toBe("wait");
    expect(plan(entry(), win({ blocked: true }), DEV_OPEN_IDLE, T + WAIT_MS).plan).toEqual({ kind: "skip", why: "expired" });
    expect(plan(entry(), win(), DEV_OPEN_IDLE, T - 1).plan).toEqual({ kind: "skip", why: "expired" });
  });
  it("retains the newest four distinct waiting entries in FIFO order", () => {
    let q: { id: string }[] = [];
    for (let i = 0; i < 6; i++) q = enqueueDev(q, { id: String(i) });
    expect(q.map(e => e.id)).toEqual(["2", "3", "4", "5"]);
    expect(enqueueDev(q, { id: "3" })).toEqual(q);
  });
  it("renders a short host label", () => {
    expect(offerLabel("http://localhost:5173/")).toBe("Dev server at localhost:5173");
    expect(offerLabel("nope")).toBe("Dev server");
  });
});

describe("unsolicited allocation cannot discard work", () => {
  it("takes the lowest unused id before any kept id", () => {
    const ws = defaultWorkspace(); ws.kept = [2, 3];
    expect(freeId(ws)).toBe(4);
    expect(freeId(ws, [4])).toBe(5);
  });
  it("only reclaims the oldest strictly default blank kept pane", () => {
    // Every id but 1 hidden (twelve ids, FS2): 2 hidden longest.
    const ws = defaultWorkspace(); ws.kept = [12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2];
    expect(blankKeptPane(ws, 2)).toBe(true); expect(freeId(ws)).toBe(2);
    ws.panes[1].browser = { tabs: [], active: "" };
    ws.panes[2].filePath = "draft.ts";
    ws.panes[3].projectId = "p1";
    ws.panes[4].terminalId = "term1";
    ws.panes[5].view = "file";
    expect(freeId(ws, [7])).toBe(8);
    expect(freeId(ws, [7, 8, 9, 10, 11, 12])).toBeNull();
  });
  it("after ⊞ 8 → 1 places an unsolicited pane in the free id 9, never a hidden one (FS2)", () => {
    const one = setPreset(setPreset(defaultWorkspace(), "grid8"), "single");
    expect(one.kept).toEqual([2, 3, 4, 5, 6, 7, 8]);
    expect(freeId(one)).toBe(9);
    expect(freeId(one, [9, 10])).toBe(11);
    expect(autoSplit(one, fitWorkspace(DEFAULT_LAYOUT, one, 1700, 900), 1)).toEqual({ axis: "x", id: 9 });
  });
  it("splits right where it fits the current chrome, else down, else not at all; never past the cap (FS4)", () => {
    const ws = defaultWorkspace();
    const at = (w: typeof ws, aw: number, ah: number, layout = DEFAULT_LAYOUT) => autoSplit(w, fitWorkspace(layout, w, aw, ah), 1);
    expect(at(ws, 1700, 900)).toEqual({ axis: "x", id: 2 });
    // 448 px of centre: no room right, and the side panes are never folded for it; 900 tall: down.
    expect(at(ws, 1000, 900)).toEqual({ axis: "y", id: 2 });
    expect(at(ws, 1000, 300)).toBeNull();
    expect(at(ws, 500, 900, { ...DEFAULT_LAYOUT, leftCollapsed: true, rightCollapsed: true })).toEqual({ axis: "y", id: 2 });
    // 4×2: four across already, so down (the ladder stopped at eight; FS-1 is twelve).
    expect(at(setPreset(ws, "grid8"), 4000, 1800)).toEqual({ axis: "y", id: 9 });
    // A window already dropping panes: nothing (P-6).
    expect(at(setPreset(ws, "cols3"), 900, 700)).toBeNull();
  });
});

it("a driven Browser receives an inactive Direct tab even when otherwise unused",()=>{
  const target=pane(2,{view:"browser",tabCount:1,driving:true});const w=win({panes:[pane(1),target],drawn:[1,2],focused:1,canSplitFrom:[]});
  expect(plan(entry(),w).plan).toEqual({kind:"tab",pane:2,url:"http://localhost:5173/",activate:false});
  target.driving=false;expect(plan(entry(),w).plan).toMatchObject({activate:true});
  target.driving=true;target.browserProfile="private";expect(plan(entry(),w).plan.kind).toBe("prompt");
});
