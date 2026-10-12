import { describe, expect, it } from "vitest";
import {
  DEV_OPEN_IDLE, OPENED_KEPT, PER_TERMINAL_MS, WAIT_MS, WINDOW_MAX, autoOpenPlan, devUrlArrived, isBrowserView,
  offerLabel, type DevOpenState, type DevWindow,
} from "./devopen";
import { defaultWorkspace, splitPane, type PaneNo } from "./workspace";

const T = 1_000_000;
const PORTS = { hud: 18402, api: 18405, workshop: 18403, self: 18402, frameHardened: true };

/** The default window: one chat pane, the terminal in the panel, room to split. */
const win = (over: Partial<DevWindow> = {}): DevWindow => ({
  enabled: true, blocked: false, ports: PORTS, panes: [{ id: 1, view: "chat" }], drawn: [1], focused: 1,
  canSplit: true, busy: [], ...over,
});

function plan(url: string, w: DevWindow = win(), s: DevOpenState = DEV_OPEN_IDLE, now = T, term = "t1") {
  return autoOpenPlan(devUrlArrived(s, url, term, now), w, now);
}

describe("where a dev server opens", () => {
  it("in a split beside the focused pane when its terminal is in the panel", () => {
    expect(plan("http://localhost:5173/").plan).toEqual({ kind: "split", source: 1, url: "http://localhost:5173/" });
  });

  it("beside the pane showing that terminal", () => {
    const w = win({
      panes: [{ id: 1, view: "chat" }, { id: 3, view: "terminal", terminalId: "t1" }], drawn: [1, 3], focused: 1,
    });
    expect(plan("http://127.0.0.1:3000", w).plan).toEqual({ kind: "split", source: 3, url: "http://127.0.0.1:3000/" });
  });

  it("splits without taking focus: the plan applied as G2 does keeps the owner where they were", () => {
    const { plan: p } = plan("http://localhost:5173/");
    expect(p.kind).toBe("split");
    if (p.kind !== "split") return;
    const ws = defaultWorkspace();
    const next = splitPane(ws, p.source, 2 as PaneNo, { view: "preview", projectId: null, terminalId: null, previewUrl: p.url },
      { focus: false });
    expect([next.preset, next.order, next.focused]).toEqual(["cols2", [1, 2], 1]);
    expect(next.panes[1].previewUrl).toBe("http://localhost:5173/");
  });

  it("with no room, in the focused pane — unless that pane is busy, the terminal itself or a page", () => {
    const tight = (over: Partial<DevWindow>) => plan("http://localhost:5173/", win({ canSplit: false, ...over })).plan;
    expect(tight({})).toEqual({ kind: "focused", pane: 1, url: "http://localhost:5173/" });
    expect(tight({ busy: [1] })).toEqual({ kind: "offer", terminalId: "t1", url: "http://localhost:5173/" });
    expect(tight({ panes: [{ id: 1, view: "terminal", terminalId: "t1" }] }).kind).toBe("offer");
    expect(tight({ panes: [{ id: 1, view: "terminal", terminalId: "t2" }] }).kind).toBe("focused");
    expect(tight({ panes: [{ id: 1, view: "preview", url: "http://localhost:8080/" }] }).kind).toBe("offer");
    expect(tight({ panes: [{ id: 1, view: "preview" }] }).kind).toBe("focused");
  });

  it("knows the Browser pane by either name", () => {
    expect(isBrowserView("preview")).toBe(true);
    expect(isBrowserView("browser")).toBe(true);
    expect(isBrowserView("chat")).toBe(false);
  });
});

describe("what is skipped", () => {
  it("everything, with the switch off", () => {
    const r = plan("http://localhost:5173/", win({ enabled: false }));
    expect(r.plan).toEqual({ kind: "skip", why: "off" });
    expect(r.state.pending).toBeNull();
  });

  it("whatever the WP-E judge refuses: Jarvis's own ports, this window's, anything not local", () => {
    for (const url of [
      "http://localhost:18405/", "http://localhost:18402/", "http://localhost:8405/", "http://localhost:8402/",
      "http://192.168.1.4:3000/", "https://example.com/", "http://user:pw@localhost:3000/", "file:///etc/passwd",
      "javascript:alert(1)", "not a url",
    ]) {
      expect(plan(url).plan).toEqual({ kind: "skip", why: "refused" });
    }
  });

  // Review of PR #66: the WP-E judge lets 8404, 8406 and the workshop through; §D.1 refuses them.
  it("Jarvis's control ports, at their defaults and as reported, and the workshop", () => {
    for (const url of ["http://localhost:8404/", "http://127.0.0.1:8406/callback", "http://localhost:18403/p/x/"]) {
      expect(plan(url).plan).toEqual({ kind: "skip", why: "refused" });
    }
    expect(plan("http://localhost:19404/", win({ controlPorts: [19402, 19404, 19405, 19406] })).plan)
      .toEqual({ kind: "skip", why: "refused" });
    expect(plan("http://localhost:8404/", win({ controlPorts: [19404] })).plan).toEqual({ kind: "skip", why: "refused" });
    // No workshop reported: its default is refused.
    expect(plan("http://localhost:8403/", win({ ports: { hud: 18402, api: 18405 } })).plan)
      .toEqual({ kind: "skip", why: "refused" });
    expect(plan("http://localhost:19404/").plan.kind).toBe("split");
  });

  it("never carries a query or a fragment: a Jupyter token stays in the terminal", () => {
    expect(plan("http://localhost:8888/lab?token=abc123#cell").plan)
      .toEqual({ kind: "split", source: 1, url: "http://localhost:8888/lab" });
    expect(plan("http://127.0.0.1:5000/?x=1", win({ canSplit: false })).plan)
      .toEqual({ kind: "focused", pane: 1, url: "http://127.0.0.1:5000/" });
  });

  it("an origin opened before in this page, or one a pane already shows, drawn or hidden", () => {
    const first = plan("http://localhost:5173/");
    expect(first.plan.kind).toBe("split");
    // A server restart prints it again, later: no second pane.
    expect(plan("http://localhost:5173/about", win(), first.state, T + 120_000).plan).toEqual({ kind: "skip", why: "duplicate" });
    const shown = win({ panes: [{ id: 1, view: "chat" }, { id: 4, view: "preview", url: "http://localhost:4000/x" }] });
    expect(plan("http://localhost:4000/", shown).plan).toEqual({ kind: "skip", why: "duplicate" });
    // Another origin — another host spelling is another origin — opens.
    expect(plan("http://127.0.0.1:5173/", win(), first.state, T + 120_000).plan.kind).toBe("split");
  });

  it("past the rate: one per 10 s per terminal, three a minute per window", () => {
    let s = DEV_OPEN_IDLE;
    const opened: string[] = [];
    for (let i = 0; i < 3; i++) {
      const r = plan(`http://localhost:${5000 + i}/`, win(), s, T + i * 300);
      opened.push(r.plan.kind);
      s = r.state;
    }
    expect(opened).toEqual(["split", "skip", "skip"]);
    expect(plan("http://localhost:5009/", win(), s, T + PER_TERMINAL_MS - 1).plan).toEqual({ kind: "skip", why: "rate" });
    expect(plan("http://localhost:5009/", win(), s, T + PER_TERMINAL_MS).plan.kind).toBe("split");

    s = DEV_OPEN_IDLE;
    for (let i = 0; i < WINDOW_MAX; i++) s = plan(`http://localhost:${6000 + i}/`, win(), s, T + i, `t${i}`).state;
    expect(plan("http://localhost:6100/", win(), s, T + 10, "t9").plan).toEqual({ kind: "skip", why: "rate" });
    expect(plan("http://localhost:6100/", win(), s, T + 60_000, "t9").plan.kind).toBe("split");
  });

  it("nothing waiting: nothing to do", () => {
    expect(autoOpenPlan(DEV_OPEN_IDLE, win(), T)).toEqual({ state: DEV_OPEN_IDLE, plan: { kind: "skip", why: "none" } });
    expect(devUrlArrived(DEV_OPEN_IDLE, 42, "t1", T)).toBe(DEV_OPEN_IDLE);
    expect(devUrlArrived(DEV_OPEN_IDLE, "http://localhost:1/", "", T)).toBe(DEV_OPEN_IDLE);
  });
});

describe("under a card", () => {
  it("nothing changes; the newest URL waits, then opens when the card is gone", () => {
    let s = devUrlArrived(DEV_OPEN_IDLE, "http://localhost:5173/", "t1", T);
    s = devUrlArrived(s, "http://localhost:5174/", "t1", T + 1000);
    const held = autoOpenPlan(s, win({ blocked: true }), T + 2000);
    expect(held.plan).toEqual({ kind: "wait" });
    expect(held.state).toBe(s);
    expect(autoOpenPlan(held.state, win(), T + 20_000).plan)
      .toEqual({ kind: "split", source: 1, url: "http://localhost:5174/" });
  });

  it("a URL that waited past 30 s is dropped", () => {
    const s = devUrlArrived(DEV_OPEN_IDLE, "http://localhost:5173/", "t1", T);
    expect(autoOpenPlan(s, win({ blocked: true }), T + WAIT_MS).plan).toEqual({ kind: "wait" });
    const late = autoOpenPlan(s, win(), T + WAIT_MS + 1);
    expect(late.plan).toEqual({ kind: "skip", why: "expired" });
    expect(late.state.pending).toBeNull();
  });
});

describe("the page's memory", () => {
  it("stays small", () => {
    let s = DEV_OPEN_IDLE;
    for (let i = 0; i < OPENED_KEPT + 20; i++) {
      s = plan(`http://localhost:${7000 + i}/`, win(), s, T + i * 61_000).state;
    }
    expect(s.opened.length).toBe(OPENED_KEPT);
    expect(s.acts.length).toBeLessThanOrEqual(WINDOW_MAX);
  });

  it("the offer chip names the server", () => {
    expect(offerLabel("http://localhost:5173/")).toBe("Dev server at localhost:5173");
    expect(offerLabel("nope")).toBe("Dev server");
  });
});
