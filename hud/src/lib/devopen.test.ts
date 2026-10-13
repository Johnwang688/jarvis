import { describe, expect, it } from "vitest";
import {
  DEV_OPEN_IDLE, OPENED_KEPT, WAIT_MS, WINDOW_MAX, autoOpenPlan, devUrl, enqueueDev,
  offerLabel, type DevEntry, type DevOpenState, type DevWindow, type PaneShows,
} from "./devopen";
import { autoSplitVerdict, blankKeptPane, defaultWorkspace, freeId, setPreset, splitPane, type PaneNo } from "./workspace";
import { DEFAULT_LAYOUT } from "./layout";

const T = 1_000_000;
const PORTS = { hud: 18402, api: 18405, workshop: 18403, self: 18402, frameHardened: true };
const pane = (id: PaneNo, over: Partial<PaneShows> = {}): PaneShows => ({ id, view: "chat", urls: [], tabCount: 0, ...over });
const win = (over: Partial<DevWindow> = {}): DevWindow => ({
  enabled: true, blocked: false, covered: false, ports: PORTS, panes: [pane(1)], drawn: [1], focused: 1,
  canSplit: true, pointerDown: false, pointerReleasedAt: 0, ...over,
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
    const ws = splitPane(defaultWorkspace(), result.source, 2, { view: "browser", projectId: null, terminalId: null }, { focus: false });
    expect([ws.order, ws.focused]).toEqual([[1, 2], 1]);
  });
  it("falls back to the focused drawn pane when the source is hidden or absent", () => {
    expect(plan(entry(), win({ panes: [pane(1), pane(3, { source: entry().source })] })).plan).toMatchObject({ kind: "split", source: 1 });
  });
  it("uses the nearest nonfull drawn Browser, ties after the source", () => {
    const panes = [pane(1, { view: "browser" }), pane(3, { source: entry().source }), pane(7, { view: "browser" })];
    const w = win({ panes, drawn: [1, 3, 7], focused: 3, canSplit: false });
    expect(plan(entry(), w).plan).toMatchObject({ kind: "tab", pane: 7, activate: true });
    panes[2].tabCount = 12;
    expect(plan(entry(), w).plan).toMatchObject({ kind: "tab", pane: 1 });
    panes[0].tabCount = 12;
    expect(plan(entry(), w).plan.kind).toBe("prompt");
  });
  it("uses Browser MRU without a drawn source, ignoring hidden panes", () => {
    const panes = [pane(1), pane(2, { view: "browser", usedAt: T - 20_000 }),
      pane(3, { view: "browser", usedAt: T - 30_000 }), pane(4, { view: "browser", usedAt: T })];
    expect(plan(entry(), win({ panes, drawn: [1, 2, 3], canSplit: false })).plan).toMatchObject({ kind: "tab", pane: 2, activate: true });
  });
  it("adds behind a Browser in use by workspace focus, DOM focus, or a recent owner interaction", () => {
    const panes = [pane(1), pane(2, { view: "browser" })];
    const w = win({ panes, drawn: [1, 2], canSplit: false });
    expect(plan(entry(), { ...w, focused: 2 }).plan).toMatchObject({ activate: false });
    expect(plan(entry(), { ...w, activePane: 2 }).plan).toMatchObject({ activate: false });
    panes[1].usedAt = T - 9_999;
    expect(plan(entry(), w).plan).toMatchObject({ activate: false });
    panes[1].usedAt = T - 10_000;
    expect(plan(entry(), w).plan).toMatchObject({ activate: true });
  });
  it("never places Direct content into a Private browser pane", () => {
    expect(plan(entry(), win({ canSplit: false, panes: [pane(1, { view: "browser", browserProfile: "private" })] })).plan.kind).toBe("prompt");
  });
  it("never silently replaces a focused chat, editor, terminal or VEX view", () => {
    for (const view of ["chat", "file", "terminal", "vex", "task", "diff"] as const) {
      expect(plan(entry(), win({ panes: [pane(1, { view })], canSplit: false })).plan.kind).toBe("prompt");
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
    expect(plan(entry({ url: "http://127.0.0.1:5000/?x=1" }), win({ canSplit: false })).plan)
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
    for (const canSplit of [false, true]) for (const flag of ["blocked", "covered"] as const) {
      const held = plan(entry(), win({ [flag]: true, canSplit }));
      expect(held.plan).toEqual({ kind: "wait", why: flag === "blocked" ? "card" : "covered" });
      expect(held.state).toBe(DEV_OPEN_IDLE);
    }
  });
  it("defers splitting until 150ms after pointer release but does not defer a fallback tab", () => {
    expect(plan(entry(), win({ pointerDown: true })).plan).toEqual({ kind: "wait", why: "pointer" });
    expect(plan(entry(), win({ pointerReleasedAt: T - 149 })).plan.kind).toBe("wait");
    expect(plan(entry(), win({ pointerReleasedAt: T - 150 })).plan.kind).toBe("split");
    expect(plan(entry(), win({ pointerDown: true, canSplit: false, panes: [pane(1, { view: "browser" })] })).plan.kind).toBe("tab");
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
    const ws = defaultWorkspace(); ws.kept = [8, 7, 6, 5, 4, 3, 2];
    expect(blankKeptPane(ws, 2)).toBe(true); expect(freeId(ws)).toBe(2);
    ws.panes[1].browser = { tabs: [], active: "" };
    ws.panes[2].filePath = "draft.ts";
    ws.panes[3].projectId = "p1";
    ws.panes[4].terminalId = "term1";
    ws.panes[5].view = "file";
    expect(freeId(ws, [7])).toBe(8);
    expect(freeId(ws, [7, 8])).toBeNull();
  });
  it("refuses new folds and dropped panes but accepts a split that fits the current chrome", () => {
    const ws = defaultWorkspace();
    expect(autoSplitVerdict(DEFAULT_LAYOUT, ws, 1700, 900)).toEqual({ ok: true, id: 2 });
    expect(autoSplitVerdict(DEFAULT_LAYOUT, ws, 1000, 900).ok).toBe(false);
    expect(autoSplitVerdict({ ...DEFAULT_LAYOUT, leftCollapsed: true, rightCollapsed: true }, ws, 500, 900).ok).toBe(false);
    expect(autoSplitVerdict(DEFAULT_LAYOUT, setPreset(ws, "grid8"), 4000, 1800).ok).toBe(false);
  });
});

it("a driven Browser receives an inactive Direct tab even when otherwise unused",()=>{
  const target=pane(2,{view:"browser",tabCount:1,driving:true});const w=win({panes:[pane(1),target],drawn:[1,2],focused:1,canSplit:false});
  expect(plan(entry(),w).plan).toEqual({kind:"tab",pane:2,url:"http://localhost:5173/",activate:false});
  target.driving=false;expect(plan(entry(),w).plan).toMatchObject({activate:true});
  target.driving=true;target.browserProfile="private";expect(plan(entry(),w).plan.kind).toBe("prompt");
});
