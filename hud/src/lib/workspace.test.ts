import { describe, expect, it } from "vitest";
import { DEFAULT_LAYOUT, PANE, RAIL } from "./layout";
import {
  PANEL, PANE_MIN_H, PANE_MIN_W, PANE_VIEWS, PRESETS, SHAPES, TITLEBAR_H, WORKSPACE_KEY, clampCols,
  clampPanelHeight, clampRow, columnWidths, defaultWorkspace, dropColumn, dropRow, equalSplit, fitWorkspace,
  focusPane, forgetTerminal, loadWorkspace, maxPanelHeight, moveColEdge, panesOf, parseWorkspace, pinPane,
  resetWorkspace, saveWorkspace, setKeepOrigin, setPaneTerminal, setPanel, setPreset, setPreviewUrl, setSplit,
  setView, show, unpinProject,
  type DrawnSet, type PaneNo, type Preset, type Workspace,
} from "./workspace";

const memory = (seed: Record<string, string> = {}) => {
  const m = new Map(Object.entries(seed));
  return {
    getItem: (k: string) => (m.has(k) ? m.get(k)! : null),
    setItem: (k: string, v: string) => void m.set(k, v),
    map: m,
  };
};
const throwing = {
  getItem: () => {
    throw new Error("SecurityError: storage is blocked");
  },
  setItem: () => {
    throw new Error("QuotaExceededError");
  },
};

const ws = (over: Partial<Workspace> = {}): Workspace => ({ ...defaultWorkspace(), ...over });
/** The window's room the way useLayout measures it: inner size over the zoom, the title bar off the height. */
const room = (w: number, h: number, zoom: number) => [w / (zoom / 100), h / (zoom / 100) - TITLEBAR_H] as const;
const fitAt = (w: Workspace, px: number, py: number, zoom: number, preferPanel = false) => {
  const [aw, ah] = room(px, py, zoom);
  return fitWorkspace(DEFAULT_LAYOUT, w, aw, ah, null, preferPanel);
};

describe("the preset table", () => {
  it("has the plan's six shapes with their minimums", () => {
    expect(PRESETS).toEqual(["single", "cols2", "rows2", "cols3", "main2", "grid4"]);
    const table: Record<Preset, [number, number, number]> = {
      single: [1, 480, 200], cols2: [2, 720, 200], rows2: [2, 480, 400],
      cols3: [3, 1080, 200], main2: [3, 840, 400], grid4: [4, 720, 400],
    };
    for (const p of PRESETS) {
      expect([SHAPES[p].panes, SHAPES[p].minW, SHAPES[p].minH]).toEqual(table[p]);
    }
    expect([PANE_MIN_W, PANE_MIN_H]).toEqual([360, 200]);
    expect([PANEL.min, PANEL.def]).toEqual([120, 260]);
  });

  it("names its grid slots once per pane", () => {
    for (const p of PRESETS) {
      const slots = new Set(SHAPES[p].areas.join(" ").split(" "));
      expect(slots.size).toBe(SHAPES[p].panes);
      expect(panesOf(p)).toHaveLength(SHAPES[p].panes);
    }
  });
});

describe("parsing what was stored", () => {
  it("gives garbage the defaults", () => {
    for (const bad of [null, undefined, "", "{", "[]", "[1,2]", "null", "42", '"x"', 7]) {
      expect(parseWorkspace(bad)).toEqual(defaultWorkspace());
    }
  });

  it("reads an unknown preset as single", () => {
    for (const p of ["quad", "", 3, null, "SINGLE", "__proto__"]) {
      expect(parseWorkspace(JSON.stringify({ preset: p })).preset).toBe("single");
    }
    expect(parseWorkspace(JSON.stringify({ preset: "grid4" })).preset).toBe("grid4");
  });

  it("parses each pane on its own, a bad one falling back to that pane's default", () => {
    const w = parseWorkspace(JSON.stringify({
      panes: [{ view: "file", projectId: "p2" }, { view: "nonsense" }, "junk", { view: "diff", previewUrl: 5 }],
    }));
    expect(w.panes.map((p) => p.view)).toEqual(["file", "preview", "file", "diff"]);
    expect(w.panes[0].projectId).toBe("p2");
    expect(w.panes[1].projectId).toBeNull();
    expect(w.panes[3].previewUrl).toBeUndefined();
  });

  it("offers the terminal view (WP-D), with the terminal a pane shows", () => {
    const w = parseWorkspace(JSON.stringify({ panes: [{ view: "terminal", terminalId: "0a1b2c3d" }] }));
    expect(w.panes[0].view).toBe("terminal");
    expect(w.panes[0].terminalId).toBe("0a1b2c3d");
    expect(PANE_VIEWS).toContain("terminal");
  });

  it("draws one terminal in one pane: showing it here makes any other pane let it go", () => {
    let w = setView(setView(setPreset(defaultWorkspace(), "grid4"), 2, "terminal"), 3, "terminal");
    w = setPaneTerminal(w, 2, "aaaa0001");
    expect(setPaneTerminal(w, 2, "aaaa0001")).toBe(w);
    w = setPaneTerminal(w, 3, "aaaa0001");
    expect(w.panes.map((p) => p.terminalId)).toEqual([null, null, "aaaa0001", null]);
    expect(w.panes[1].view).toBe("terminal");                 // pane 2 offers the choice again
    w = setPaneTerminal(w, 2, "bbbb0002");
    expect(forgetTerminal(w, "aaaa0001").panes.map((p) => p.terminalId)).toEqual([null, "bbbb0002", null, null]);
    expect(forgetTerminal(w, "cccc0003")).toBe(w);
    expect(setPaneTerminal(w, 2, null).panes[1].terminalId).toBeNull();
  });

  it("keeps several chat panes as stored: each is its own conversation (WP-B)", () => {
    const w = parseWorkspace(JSON.stringify({
      panes: [{ view: "file" }, { view: "chat" }, { view: "chat" }, { view: "chat" }],
    }));
    expect(w.panes.map((p) => p.view)).toEqual(["file", "chat", "chat", "chat"]);
  });

  it("refuses ids and URLs that are not strings, or are too long", () => {
    const w = parseWorkspace(JSON.stringify({
      panes: [{ projectId: 7 }, { projectId: "x".repeat(500) }, { previewUrl: "u".repeat(3000) }, { terminalId: {} }],
    }));
    expect(w.panes.map((p) => p.projectId)).toEqual([null, null, null, null]);
    expect(w.panes[2].previewUrl).toBeUndefined();
    expect(w.panes[3].terminalId).toBeNull();
  });

  it("keeps a good split and replaces a bad one, preset by preset", () => {
    const w = parseWorkspace(JSON.stringify({
      splits: {
        cols2: { cols: [0.3], row: 0.7 },
        cols3: { cols: [0.6, 0.4], row: 2 },     // not increasing; row out of range
        grid4: { cols: [0.5, 0.6] },             // the wrong count
        main2: { cols: [1.2] },                  // outside (0, 1)
        rows2: "nope",
      },
    }));
    expect(w.splits.cols2).toEqual({ cols: [0.3], row: 0.7 });
    expect(w.splits.cols3).toEqual(defaultWorkspace().splits.cols3);
    expect(w.splits.grid4).toEqual(defaultWorkspace().splits.grid4);
    expect(w.splits.main2).toEqual(defaultWorkspace().splits.main2);
    expect(w.splits.rows2).toEqual(defaultWorkspace().splits.rows2);
  });

  it("opens the panel only on a literal true, and clamps its height", () => {
    for (const v of ["true", 1, "yes", null, {}]) {
      expect(parseWorkspace(JSON.stringify({ panel: { open: v } })).panel.open).toBe(false);
    }
    expect(parseWorkspace(JSON.stringify({ panel: { open: true, height: 5 } })).panel).toEqual({ open: true, height: 120 });
    expect(parseWorkspace(JSON.stringify({ panel: { height: "tall" } })).panel.height).toBe(260);
    expect(clampPanelHeight(99999)).toBe(PANEL.max);
  });

  it("focuses pane 1 when the stored focus is bad or not drawn by the preset", () => {
    expect(parseWorkspace(JSON.stringify({ preset: "grid4", focused: 3 })).focused).toBe(3);
    expect(parseWorkspace(JSON.stringify({ preset: "cols2", focused: 3 })).focused).toBe(1);
    for (const f of [0, 5, 2.5, "2", null]) {
      expect(parseWorkspace(JSON.stringify({ preset: "grid4", focused: f })).focused).toBe(1);
    }
  });

  it("round-trips through storage, cleaned", () => {
    const s = memory();
    const w = setView(setPreset(defaultWorkspace(), "cols3"), 3, "diff");
    saveWorkspace({ ...w, panel: { open: true, height: 99999 } }, s);
    const back = loadWorkspace(s);
    expect(back.preset).toBe("cols3");
    expect(back.panes[2].view).toBe("diff");
    expect(back.panel).toEqual({ open: true, height: PANEL.max });
    expect(JSON.parse(s.map.get(WORKSPACE_KEY)!).preset).toBe("cols3");
  });

  it("falls back to the defaults when storage throws, and never throws itself", () => {
    expect(loadWorkspace(throwing)).toEqual(defaultWorkspace());
    expect(() => saveWorkspace(defaultWorkspace(), throwing)).not.toThrow();
  });
});

describe("what each pane shows", () => {
  it("choosing chat in a second pane changes only that pane: two chats, no swap (WP-B)", () => {
    const w = setView(defaultWorkspace(), 2, "chat");
    expect(w.panes.map((p) => p.view)).toEqual(["chat", "chat", "file", "task"]);
    expect(w.focused).toBe(2);
    // A hidden chat pane stays a chat pane when another pane chooses chat.
    const hidden = setView(setView(setPreset(defaultWorkspace(), "grid4"), 4, "chat"), 1, "file");
    expect(setView(setPreset(hidden, "single"), 1, "chat").panes.map((p) => p.view))
      .toEqual(["chat", "preview", "file", "chat"]);
  });

  it("does not touch what was there before", () => {
    const before = defaultWorkspace();
    const copy = JSON.parse(JSON.stringify(before));
    setView(before, 2, "chat");
    setPreset(before, "grid4");
    pinPane(before, 3, "p1");
    expect(before).toEqual(copy);
  });

  it("hands focus to pane 1 when a preset stops drawing the focused pane", () => {
    const w = focusPane(setPreset(defaultWorkspace(), "grid4"), 4);
    expect(setPreset(w, "cols3").focused).toBe(1);
    expect(setPreset(w, "grid4").focused).toBe(4);
  });

  it("sends a thread click to the pane already showing the chat", () => {
    let w = setPreset(defaultWorkspace(), "cols2");
    w = focusPane(w, 2); // pane 2 shows preview, pane 1 the chat
    const after = show(w, "chat", [1, 2]);
    expect(after.panes[1].view).toBe("preview");
    expect(after.focused).toBe(1);
  });

  it("otherwise switches the focused pane to chat", () => {
    let w = setView(defaultWorkspace(), 1, "file"); // the chat went nowhere
    w = setPreset(w, "cols2");
    w = focusPane(w, 2);
    const after = show(w, "chat", [1, 2]);
    expect(after.panes.map((p) => p.view).slice(0, 2)).toEqual(["file", "chat"]);
    expect(after.focused).toBe(2);
  });

  it("in the single layout, a click is today's tab switch", () => {
    const w = setView(defaultWorkspace(), 1, "file");
    expect(show(w, "chat", [1]).panes[0].view).toBe("chat");
    expect(show(w, "task", [1]).panes[0].view).toBe("task");
    // A chat hidden in another pane is not pulled in: the drawn pane switches.
    const hidden = setView(setView(setPreset(defaultWorkspace(), "cols2"), 2, "chat"), 1, "file");
    const back = show(setPreset(hidden, "single"), "chat", [1]);
    expect(back.panes.map((p) => p.view).slice(0, 2)).toEqual(["chat", "chat"]);
  });

  it("lets a visible task or diff pane follow the selected task without moving anything", () => {
    const w = setView(setPreset(defaultWorkspace(), "cols2"), 2, "diff");
    expect(show(w, "task", [1, 2])).toBe(w);
    const t = setView(setPreset(defaultWorkspace(), "cols2"), 2, "task");
    expect(show(t, "task", [1, 2])).toBe(t);
  });

  it("switches one pane's view and keeps its own pin, URL and terminal for when it comes back", () => {
    let w = setPreviewUrl(defaultWorkspace(), 2, "http://localhost:5173/");
    w = pinPane(w, 2, "p2");
    w = { ...w, panes: w.panes.map((p, i) => (i === 1 ? { ...p, terminalId: "term-b" } : p)) as Workspace["panes"] };
    const after = setView(w, 2, "chat");
    expect(after.panes[1]).toEqual(
      { view: "chat", projectId: "p2", terminalId: "term-b", previewUrl: "http://localhost:5173/" });
    expect(after.panes[0]).toBe(w.panes[0]);
    expect(after.focused).toBe(2);
    expect(setView(after, 2, "preview").panes[1].previewUrl).toBe("http://localhost:5173/");
  });

  it("pins and unpins a pane's project", () => {
    let w = pinPane(defaultWorkspace(), 3, "p1");
    expect(w.panes[2].projectId).toBe("p1");
    expect(pinPane(w, 3, "p1")).toBe(w);
    w = pinPane(w, 2, "p1");
    w = pinPane(w, 4, "p2");
    const gone = unpinProject(w, "p1");
    expect(gone.panes.map((p) => p.projectId)).toEqual([null, null, null, "p2"]);
    expect(unpinProject(gone, "p9")).toBe(gone);
    // A pane holding an unsaved edit keeps its pin until the owner discards it.
    expect(unpinProject(w, "p1", [3]).panes.map((p) => p.projectId)).toEqual([null, null, "p1", "p2"]);
    expect(unpinProject(pinPane(defaultWorkspace(), 3, "p1"), "p1", [3]).panes[2].projectId).toBe("p1");
  });

  it("keeps a preview URL per pane", () => {
    let w = setPreviewUrl(defaultWorkspace(), 2, "http://localhost:5173/");
    w = setPreviewUrl(w, 3, "http://127.0.0.1:8403/p/p1/index.html");
    expect(w.panes.map((p) => p.previewUrl)).toEqual(
      [undefined, "http://localhost:5173/", "http://127.0.0.1:8403/p/p1/index.html", undefined]);
    expect(setPreviewUrl(w, 2, "").panes[1].previewUrl).toBeUndefined();
  });

  it("keeps a pane's keep-origin grant only for the origin it was given for (WP-E)", () => {
    let w = setPreviewUrl(defaultWorkspace(), 2, "http://localhost:5173/");
    w = setKeepOrigin(w, 2, "http://localhost:5173");
    expect(w.panes[1].keepOrigin).toBe("http://localhost:5173");
    // Another page on the same origin keeps it.
    expect(setPreviewUrl(w, 2, "http://localhost:5173/settings").panes[1].keepOrigin).toBe("http://localhost:5173");
    // Another port, host or scheme turns it off.
    for (const url of ["http://localhost:5174/", "http://127.0.0.1:5173/", "https://localhost:5173/", ""]) {
      expect(setPreviewUrl(w, 2, url).panes[1].keepOrigin, url).toBeUndefined();
    }
    // Survives a reload, judged again by the parser.
    expect(parseWorkspace(JSON.stringify(w)).panes[1].keepOrigin).toBe("http://localhost:5173");
    // Off.
    expect(setKeepOrigin(w, 2, null).panes[1].keepOrigin).toBeUndefined();
    expect(setKeepOrigin(setKeepOrigin(w, 2, null), 2, null)).toEqual(setKeepOrigin(w, 2, null));
  });

  it("never grants keep-origin to another origin than the pane's URL, nor to a daemon port", () => {
    const w = setPreviewUrl(defaultWorkspace(), 2, "http://localhost:5173/");
    expect(setKeepOrigin(w, 2, "http://localhost:5174")).toBe(w);
    expect(setKeepOrigin(w, 3, "http://localhost:5173")).toBe(w);           // pane 3 holds no URL
    const hud = setPreviewUrl(defaultWorkspace(), 2, "http://localhost:8402/");
    expect(setKeepOrigin(hud, 2, "http://localhost:8402")).toBe(hud);
    const shop = setPreviewUrl(defaultWorkspace(), 2, "http://127.0.0.1:8403/p/p1/index.html");
    expect(setKeepOrigin(shop, 2, "http://127.0.0.1:8403")).toBe(shop);
  });

  it("drops a stored keep-origin that is not allowed when it is read back", () => {
    const stored = (pane: Record<string, unknown>) =>
      parseWorkspace(JSON.stringify({ panes: [{}, { view: "preview", ...pane }] })).panes[1].keepOrigin;
    expect(stored({ previewUrl: "http://localhost:5173/", keepOrigin: "http://localhost:5173" }))
      .toBe("http://localhost:5173");
    expect(stored({ previewUrl: "http://localhost:5174/", keepOrigin: "http://localhost:5173" })).toBeUndefined();
    expect(stored({ previewUrl: "http://localhost:8402/", keepOrigin: "http://localhost:8402" })).toBeUndefined();
    expect(stored({ previewUrl: "http://localhost:8405/", keepOrigin: "http://localhost:8405" })).toBeUndefined();
    expect(stored({ previewUrl: "https://example.com/", keepOrigin: "https://example.com" })).toBeUndefined();
    expect(stored({ keepOrigin: "http://localhost:5173" })).toBeUndefined();
    expect(stored({ previewUrl: "http://localhost:5173/", keepOrigin: true })).toBeUndefined();
  });

  it("resets the layout without forgetting what each pane showed", () => {
    let w = setView(setPreset(defaultWorkspace(), "grid4"), 3, "diff");
    w = setSplit(w, "grid4", { cols: [0.3], row: 0.7 });
    w = setPanel(w, { open: true, height: 400 });
    const r = resetWorkspace(focusPane(w, 3));
    expect(r.preset).toBe("single");
    expect(r.focused).toBe(1);
    expect(r.panel).toEqual({ open: false, height: PANEL.def });
    expect(r.splits.grid4).toEqual(defaultWorkspace().splits.grid4);
    expect(r.panes[2].view).toBe("diff");
  });

  it("makes a shape's panes equal on a double-click", () => {
    const w = setSplit(defaultWorkspace(), "cols3", { cols: [0.2, 0.9] });
    expect(equalSplit(w, "cols3").splits.cols3.cols).toEqual([1 / 3, 2 / 3]);
    expect(equalSplit(w, "main2").splits.main2).toEqual({ cols: [0.5], row: 0.5 });
  });
});

describe("edges", () => {
  it("keeps every column at least the pane minimum", () => {
    const c = clampCols([0.1, 0.95], 1200);
    const w = columnWidths(c, 1200);
    for (const x of w) expect(x).toBeGreaterThanOrEqual(PANE_MIN_W - 1e-6);
    expect(w.reduce((a, b) => a + b)).toBeCloseTo(1200);
  });

  it("leaves fitting fractions alone, and splits evenly a centre too narrow for any", () => {
    expect(clampCols([0.5], 1000)).toEqual([0.5]);
    expect(clampCols([0.2, 0.9], 700)).toEqual([1 / 3, 2 / 3]);
    expect(clampCols([], 500)).toEqual([]);
  });

  it("stops a dragged edge short of its neighbours", () => {
    const cols = [1 / 3, 2 / 3];
    const moved = moveColEdge(cols, 0, 0.01, 1200);
    expect(moved[0] * 1200).toBeCloseTo(PANE_MIN_W);
    const right = moveColEdge(cols, 0, 0.99, 1200);
    expect((right[1] - right[0]) * 1200).toBeCloseTo(PANE_MIN_W);
    expect(moveColEdge(cols, 5, 0.5, 1200)).toEqual(cols);
  });

  it("keeps the row edge off both ends", () => {
    expect(clampRow(0.05, 600)).toBeCloseTo(PANE_MIN_H / 600);
    expect(clampRow(0.99, 600)).toBeCloseTo(1 - PANE_MIN_H / 600);
    expect(clampRow(0.3, 300)).toBe(0.5);
  });
});

describe("fitting: the four worked windows (plan §2.2)", () => {
  const preset = (p: Preset, panel = false) => ws({ preset: p, panel: { open: panel, height: PANEL.def } });

  it("1920×1080 at 100%: everything fits, side panes open", () => {
    for (const p of ["cols2", "cols3"] as Preset[]) {
      const f = fitAt(preset(p), 1920, 1080, 100);
      expect(f.drawn).toBe(p);
      expect(f.sides).toMatchObject({ leftFolded: false, rightFolded: false, left: 236, right: 316 });
    }
    const g = fitAt(preset("grid4", true), 1920, 1080, 100);
    expect(g.drawn).toBe("grid4");
    expect(g.panel).toEqual({ open: true, height: 260, auto: false });
  });

  it("1366×768 at 100%: two fit, main2 shrinks the side panes a little, three folds the status pane", () => {
    expect(fitAt(preset("cols2"), 1366, 768, 100).sides).toMatchObject({ left: 236, right: 316 });
    const m = fitAt(preset("main2"), 1366, 768, 100);
    expect(m.drawn).toBe("main2");
    expect(m.sides.leftFolded || m.sides.rightFolded).toBe(false);
    expect(m.sides.left + m.sides.right).toBeLessThan(236 + 316);
    expect(m.width).toBeGreaterThanOrEqual(SHAPES.main2.minW);
    const c = fitAt(preset("cols3"), 1366, 768, 100);
    expect(c.drawn).toBe("cols3");
    expect(c.sides).toMatchObject({ rightFolded: true, autoRight: true, leftFolded: false });
    const g = fitAt(preset("grid4", true), 1366, 768, 100);
    expect(g.drawn).toBe("grid4");
    expect(g.panel.open).toBe(true);
  });

  it("1280×800 at 150%: two columns fold both side panes; three drop to two; the panel folds for 2×2", () => {
    const c2 = fitAt(preset("cols2"), 1280, 800, 150);
    expect(c2.drawn).toBe("cols2");
    expect(c2.sides).toMatchObject({ leftFolded: true, rightFolded: true, autoLeft: true, autoRight: true });
    const c3 = fitAt(preset("cols3"), 1280, 800, 150);
    expect(c3).toMatchObject({ drawn: "cols2", dropped: 1, why: "narrow" });
    expect(c3.panes).toEqual([1, 2]);
    // 400 + 120 > 505: the panel folds for the render, the four panes stay.
    const g = fitAt(preset("grid4", true), 1280, 800, 150);
    expect(g.drawn).toBe("grid4");
    expect(g.panel).toEqual({ open: false, height: 0, auto: true });
  });

  it("1024×700 at 160%: one pane for two or three columns", () => {
    expect(fitAt(preset("cols2"), 1024, 700, 160)).toMatchObject({ drawn: "single", dropped: 1 });
    expect(fitAt(preset("cols3"), 1024, 700, 160)).toMatchObject({ drawn: "single", dropped: 2 });
    // 2×2 with the panel: the column goes first (the focused one is kept),
    // then the panel folds before any row does — the plan's stated order.
    // (The plan's table cell says "1 pane, panel at its minimum"; that is
    // what opening the panel by hand gives, below.)
    const g = fitAt(preset("grid4", true), 1024, 700, 160);
    expect(g).toMatchObject({ drawn: "rows2", panes: [1, 3] });
    expect(g.panel.auto).toBe(true);
    const opened = fitAt(preset("grid4", true), 1024, 700, 160, true);
    expect(opened.drawn).toBe("single");
    expect(opened.panel.open).toBe(true);
    expect(opened.panel.height).toBeGreaterThanOrEqual(PANEL.min);
  });
});

describe("fitting: what is dropped, and what never is", () => {
  it("drops columns in order: three, two, one", () => {
    const w = ws({ preset: "cols3" });
    // 1000: 36 + 36 + 1080 does not fit, 720 does. 792 is the last width two
    // columns fit beside both rails; 780 is one pane.
    const order = [1400, 1000, 792, 780, 500].map((px) => fitAt(w, px, 900, 100).drawn);
    expect(order).toEqual(["cols3", "cols2", "cols2", "single", "single"]);
  });

  it("never drops the focused pane, whatever the preset", () => {
    for (const p of PRESETS) {
      for (const n of panesOf(p)) {
        const w = focusPane(ws({ preset: p, panel: { open: true, height: 260 } }), n);
        for (const [px, py] of [[600, 400], [900, 500], [1100, 700], [640, 437]] as [number, number][]) {
          const f = fitAt(w, px, py, 100);
          expect(f.panes, `${p} focused ${n} at ${px}x${py}`).toContain(n);
        }
      }
    }
  });

  it("keeps the focused pane's column and row", () => {
    expect(dropColumn("cols3", [1, 2, 3], 3)).toEqual({ shape: "cols2", panes: [2, 3] });
    expect(dropColumn("cols3", [1, 2, 3], 1)).toEqual({ shape: "cols2", panes: [1, 2] });
    expect(dropColumn("cols2", [2, 3], 3)).toEqual({ shape: "single", panes: [3] });
    expect(dropColumn("main2", [1, 2, 3], 3)).toEqual({ shape: "rows2", panes: [2, 3] });
    expect(dropColumn("main2", [1, 2, 3], 1)).toEqual({ shape: "single", panes: [1] });
    expect(dropColumn("grid4", [1, 2, 3, 4], 4)).toEqual({ shape: "rows2", panes: [2, 4] });
    expect(dropRow("grid4", [1, 2, 3, 4], 3)).toEqual({ shape: "cols2", panes: [3, 4] });
    expect(dropRow("main2", [1, 2, 3], 3)).toEqual({ shape: "cols2", panes: [1, 3] });
    expect(dropRow("rows2", [1, 3], 3)).toEqual({ shape: "single", panes: [3] });
    expect(dropColumn("single", [1], 1)).toBeNull();
    expect(dropRow("cols3", [1, 2, 3], 1)).toBeNull();
  });

  it("says how many panes it is not drawing", () => {
    const f = fitAt(focusPane(ws({ preset: "grid4" }), 4), 600, 300, 100);
    expect(f.panes).toContain(4 as PaneNo);
    expect(f.dropped).toBe(4 - f.panes.length);
    expect(f.dropped).toBeGreaterThan(0);
  });

  it("does not touch what is stored", () => {
    const w = ws({ preset: "cols3", panel: { open: true, height: 300 } });
    const copy = JSON.parse(JSON.stringify(w));
    fitAt(w, 600, 400, 160);
    expect(w).toEqual(copy);
  });

  it("leaves everything as chosen with no window to fit", () => {
    const f = fitWorkspace(DEFAULT_LAYOUT, ws({ preset: "grid4", panel: { open: true, height: 300 } }), NaN, NaN);
    expect(f).toMatchObject({ drawn: "grid4", dropped: 0, panel: { open: true, height: 300, auto: false } });
  });
});

describe("fitting: a dropped set is sticky while focus moves inside it", () => {
  // 1280×800 at 150%: three columns draw as two.
  const [aw, ah] = room(1280, 800, 150);
  const fit = (w: Workspace, previous: DrawnSet | null) =>
    fitWorkspace(DEFAULT_LAYOUT, w, aw, ah, null, false, previous);
  const drawn = (w: Workspace, f: ReturnType<typeof fit>): DrawnSet => ({ preset: w.preset, drawn: f.drawn, panes: f.panes });

  it("keeps the drawn pair when focus moves to the other pane in it", () => {
    const at3 = focusPane(ws({ preset: "cols3" }), 3);
    const first = fit(at3, null);
    expect(first).toMatchObject({ drawn: "cols2", panes: [2, 3] });
    // The owner clicks into pane 2, the pair's other pane: nothing moves.
    const at2 = focusPane(at3, 2);
    expect(fit(at2, drawn(at3, first)).panes).toEqual([2, 3]);
    // Without the last render's set it would have redrawn [1, 2] — the jump.
    expect(fit(at2, null).panes).toEqual([1, 2]);
  });

  it("draws a set that includes a pane focused from outside it (Ctrl+Alt+N)", () => {
    const at3 = focusPane(ws({ preset: "cols3" }), 3);
    const first = fit(at3, null);
    const at1 = focusPane(at3, 1);
    expect(fit(at1, drawn(at3, first)).panes).toEqual([1, 2]);
  });

  it("does the same for one large plus two stacked, dropped to two columns", () => {
    // Too short for the stacked column: main2 draws [1, focused-of-2-or-3].
    const w3 = focusPane(ws({ preset: "main2" }), 3);
    const f3 = fitWorkspace(DEFAULT_LAYOUT, w3, 1600, 300, null, false, null);
    expect(f3).toMatchObject({ drawn: "cols2", panes: [1, 3] });
    const w1 = focusPane(w3, 1);
    expect(fitWorkspace(DEFAULT_LAYOUT, w1, 1600, 300, null, false, drawn(w3, f3)).panes).toEqual([1, 3]);
    expect(fitWorkspace(DEFAULT_LAYOUT, w1, 1600, 300, null, false, null).panes).toEqual([1, 2]);
    const w2 = focusPane(w3, 2);
    expect(fitWorkspace(DEFAULT_LAYOUT, w2, 1600, 300, null, false, drawn(w3, f3)).panes).toEqual([1, 2]);
  });

  it("is not sticky across a different preset or a different drawn shape", () => {
    const at3 = focusPane(ws({ preset: "cols3" }), 3);
    const stale: DrawnSet = { preset: "grid4", drawn: "cols2", panes: [3, 4] };
    expect(fit(at3, stale).panes).toEqual([2, 3]);
    // The window grew: three columns fit again, all three are drawn.
    const wide = fitWorkspace(DEFAULT_LAYOUT, focusPane(at3, 2), 1920, 1000, null, false,
                              { preset: "cols3", drawn: "cols2", panes: [2, 3] });
    expect(wide).toMatchObject({ drawn: "cols3", panes: [1, 2, 3] });
  });

  it("only reads what it is given", () => {
    const prev: DrawnSet = { preset: "cols3", drawn: "cols2", panes: [2, 3] };
    const copy = JSON.parse(JSON.stringify(prev));
    const w = focusPane(ws({ preset: "cols3" }), 2);
    const wcopy = JSON.parse(JSON.stringify(w));
    const f = fit(w, prev);
    f.panes.push(4 as PaneNo);
    expect(prev).toEqual(copy);
    expect(w).toEqual(wcopy);
  });
});

describe("fitting: vertical precedence", () => {
  const tall = (h: number, preferPanel = false, preset: Preset = "rows2") =>
    fitWorkspace(DEFAULT_LAYOUT, ws({ preset, panel: { open: true, height: 260 } }), 1600, h, null, preferPanel);

  it("draws the panel at its stored height when there is room", () => {
    expect(tall(800).panel).toEqual({ open: true, height: 260, auto: false });
    expect(tall(800).height).toBe(800 - 260);
  });

  it("first shrinks the panel toward its minimum", () => {
    const f = tall(400 + 150);
    expect(f).toMatchObject({ drawn: "rows2", panel: { open: true, height: 150, auto: false } });
  });

  it("then folds it for the render, before any row is dropped", () => {
    const f = tall(400 + 100);
    expect(f.drawn).toBe("rows2");
    expect(f.panel).toEqual({ open: false, height: 0, auto: true });
  });

  it("and only then drops rows", () => {
    const f = tall(300);
    expect(f).toMatchObject({ drawn: "single", why: "short", panel: { auto: true } });
  });

  it("opening a panel the window folded drops rows instead, and never folds it", () => {
    const f = tall(400 + 100, true);
    expect(f.drawn).toBe("single");
    expect(f.panel).toEqual({ open: true, height: 260, auto: false });
    const tiny = tall(150, true);
    expect(tiny.panel).toEqual({ open: true, height: PANEL.min, auto: false });
  });

  it("gives back side-pane room a dropped row frees", () => {
    // main2 needs 840 across; at 1000 that folds the status pane. Too short
    // for its stacked column, it becomes two columns (720), which fit beside
    // the status pane again.
    const f = fitWorkspace(DEFAULT_LAYOUT, ws({ preset: "main2" }), 1000, 300);
    expect(f.drawn).toBe("cols2");
    expect(f.sides.rightFolded).toBe(true); // 236 + 316 + 720 > 1000 still
    const g = fitWorkspace(DEFAULT_LAYOUT, ws({ preset: "main2" }), 1300, 300);
    expect(g.drawn).toBe("cols2");
    expect(g.sides).toMatchObject({ leftFolded: false, rightFolded: false });
  });

  it("bounds the panel's drag by the drawn panes' minimum height", () => {
    const f = tall(800);
    expect(maxPanelHeight(f, 800)).toBe(800 - 400);
    expect(maxPanelHeight(f, NaN)).toBe(PANEL.max);
  });
});

describe("the side panes inside a split", () => {
  it("fold for a split centre exactly as for one pane, against the shape's minimum", () => {
    const one = fitAt(ws(), 1280, 800, 100);
    expect(one.sides).toMatchObject({ left: 236, right: 316, leftFolded: false, rightFolded: false });
    const two = fitAt(ws({ preset: "cols2" }), 1280, 800, 100);
    expect(two.sides.left + two.sides.right + SHAPES.cols2.minW).toBeLessThanOrEqual(1280);
    expect(two.sides.leftFolded || two.sides.rightFolded).toBe(false);
    expect(two.sides.left).toBeGreaterThanOrEqual(PANE.left.min);
    const three = fitAt(ws({ preset: "cols3" }), 1280, 800, 100);
    expect(three.sides).toMatchObject({ left: RAIL, right: RAIL });
  });
});
