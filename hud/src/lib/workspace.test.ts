import { describe, expect, it } from "vitest";
import { DEFAULT_LAYOUT, PANE, RAIL } from "./layout";
import {
  LADDER, MAX_PANES, MENU_PRESETS, PANEL, PANE_MIN_H, PANE_MIN_W, PANE_NOS, PANE_VIEWS, PATH_MAX, PRESETS, SHAPES,
  TITLEBAR_H, WORKSPACE_KEY, allocate, cellsOf, clampCols, clampPanelHeight, clampRow, closePane, columnWidths, copySpec,
  defaultPane, defaultWorkspace, dropColumn, dropRow, setFilePath, splitPane, splitTitle, splitVerdict,
  equalSplit, fitWorkspace, focusPane, forgetTerminal, gridTemplate, loadWorkspace, maxPanelHeight, moveColEdge,
  orderOf, paneAt, paneSequence, parseWorkspace, pinPane, resetWorkspace, saveWorkspace, setKeepOrigin, setPaneTerminal,
  setEditors, setPanel, setPreset, setPreviewUrl, setSplit, setView, show, slotOf, slotWidths, unpinProject,
  type DrawnSet, type PaneNo, type Preset, type Workspace,
} from "./workspace";
import LEGACY_FITS from "./workspace.legacy-fits.json";

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
  it("has the plans' ten shapes with their minimums (WP-A's six, then WP-G1's four)", () => {
    expect(PRESETS).toEqual(["single", "cols2", "rows2", "cols3", "main2", "grid4", "grid5", "grid6", "grid7", "grid8"]);
    const table: Record<Preset, [number, number, number, number[] | null]> = {
      single: [1, 480, 200, [1]], cols2: [2, 720, 200, [2]], rows2: [2, 480, 400, [1, 1]],
      cols3: [3, 1080, 200, [3]], main2: [3, 840, 400, null], grid4: [4, 720, 400, [2, 2]],
      grid5: [5, 1080, 400, [3, 2]], grid6: [6, 1080, 400, [3, 3]], grid7: [7, 1440, 400, [4, 3]],
      grid8: [8, 1440, 400, [4, 4]],
    };
    for (const p of PRESETS) {
      expect([SHAPES[p].panes, SHAPES[p].minW, SHAPES[p].minH, SHAPES[p].rows]).toEqual(table[p]);
    }
    expect([PANE_MIN_W, PANE_MIN_H]).toEqual([360, 200]);
    expect([PANEL.min, PANEL.def]).toEqual([120, 260]);
    expect(PANE_NOS).toEqual([1, 2, 3, 4, 5, 6, 7, 8]);
  });

  it("offers eight tiles in ⊞ — 6 and 8 directly, 5 and 7 only by split or close (decisions S-1)", () => {
    expect(MENU_PRESETS).toEqual(["single", "cols2", "rows2", "cols3", "main2", "grid4", "grid6", "grid8"]);
    // The ladder a split climbs: three is three columns (decisions S-3).
    expect([1, 2, 3, 4, 5, 6, 7, 8].map((n) => LADDER[n]))
      .toEqual(["single", "cols2", "cols3", "grid4", "grid5", "grid6", "grid7", "grid8"]);
    for (let n = 1; n <= 8; n++) expect(SHAPES[LADDER[n]].panes).toBe(n);
  });

  it("names its grid slots once per pane, in reading order", () => {
    for (const p of PRESETS) {
      const t = gridTemplate(p, defaultWorkspace().splits[p]);
      const names = t.areas.join(" ").split(" ");
      expect(new Set(names).size, p).toBe(SHAPES[p].panes);
      // First appearances read s1, s2, … left to right, top to bottom.
      const firsts = names.filter((x, i) => names.indexOf(x) === i);
      expect(firsts, p).toEqual(Array.from({ length: SHAPES[p].panes }, (_, i) => `s${i + 1}`));
      expect(setPreset(defaultWorkspace(), p).order).toHaveLength(SHAPES[p].panes);
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
    expect(w.panes.map((p) => p.view)).toEqual(["file", "preview", "file", "diff", "chat", "chat", "chat", "chat"]);
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
    expect(w.panes.map((p) => p.terminalId).slice(0, 4)).toEqual([null, null, "aaaa0001", null]);
    expect(w.panes[1].view).toBe("terminal");                 // pane 2 offers the choice again
    w = setPaneTerminal(w, 2, "bbbb0002");
    expect(forgetTerminal(w, "aaaa0001").panes.map((p) => p.terminalId).slice(0, 4)).toEqual([null, "bbbb0002", null, null]);
    expect(forgetTerminal(w, "cccc0003")).toBe(w);
    expect(setPaneTerminal(w, 2, null).panes[1].terminalId).toBeNull();
  });

  it("keeps several chat panes as stored: each is its own conversation (WP-B)", () => {
    const w = parseWorkspace(JSON.stringify({
      panes: [{ view: "file" }, { view: "chat" }, { view: "chat" }, { view: "chat" }],
    }));
    expect(w.panes.map((p) => p.view).slice(0, 4)).toEqual(["file", "chat", "chat", "chat"]);
  });

  it("refuses ids and URLs that are not strings, or are too long", () => {
    const w = parseWorkspace(JSON.stringify({
      panes: [{ projectId: 7 }, { projectId: "x".repeat(500) }, { previewUrl: "u".repeat(3000) }, { terminalId: {} }],
    }));
    expect(w.panes.map((p) => p.projectId).slice(0, 4)).toEqual([null, null, null, null]);
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
    for (const f of [0, 5, 9, 2.5, "2", null]) {
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
    expect(w.panes.map((p) => p.view).slice(0, 4)).toEqual(["chat", "chat", "file", "task"]);
    expect(w.focused).toBe(2);
    // A hidden chat pane stays a chat pane when another pane chooses chat.
    const hidden = setView(setView(setPreset(defaultWorkspace(), "grid4"), 4, "chat"), 1, "file");
    expect(setView(setPreset(hidden, "single"), 1, "chat").panes.map((p) => p.view).slice(0, 4))
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
    expect(gone.panes.map((p) => p.projectId).slice(0, 4)).toEqual([null, null, null, "p2"]);
    expect(unpinProject(gone, "p9")).toBe(gone);
    // A pane holding an unsaved edit keeps its pin until the owner discards it.
    expect(unpinProject(w, "p1", [3]).panes.map((p) => p.projectId).slice(0, 4)).toEqual([null, null, "p1", "p2"]);
    expect(unpinProject(pinPane(defaultWorkspace(), 3, "p1"), "p1", [3]).panes[2].projectId).toBe("p1");
  });

  it("keeps a preview URL per pane", () => {
    let w = setPreviewUrl(defaultWorkspace(), 2, "http://localhost:5173/");
    w = setPreviewUrl(w, 3, "http://127.0.0.1:8403/p/p1/index.html");
    expect(w.panes.map((p) => p.previewUrl).slice(0, 4)).toEqual(
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
      for (const n of setPreset(defaultWorkspace(), p).order) {
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

// ---- WP-G1: up to eight panes (docs/plans/2026-10-10-hud-split-panes-plan.md §3.1–§3.3) ----

/** What a WP-A/WP-B window stored: no `order`, no `kept`, four pane specs. */
const LEGACY_CUSTOM: Record<string, { cols: number[]; row: number }> = {
  single: { cols: [], row: 0.5 },
  cols2: { cols: [0.3], row: 0.5 },
  rows2: { cols: [], row: 0.35 },
  cols3: { cols: [0.2, 0.7], row: 0.5 },
  main2: { cols: [0.7], row: 0.3 },
  grid4: { cols: [0.4], row: 0.6 },
};

describe("a stored WP-A or WP-B workspace: no order, no kept", () => {
  // workspace.legacy-fits.json holds what main's workspace.ts (WP-B, 8eb4903)
  // drew for every stored shape it knew: the six presets × each focused pane ×
  // the panel closed and open × default and custom edges × five windows. It was
  // generated by running that code; here the same stored values go through
  // the new parse and fit, and must draw exactly the same.
  const r6 = (x: number) => (Number.isFinite(x) ? Math.round(x * 1e6) / 1e6 : null);
  const entries = Object.entries(LEGACY_FITS as Record<string, unknown[]>);

  it("covers every WP-B shape", () => {
    expect(entries).toHaveLength(300);
    expect(new Set(entries.map(([k]) => k.split("/")[0]))).toEqual(new Set(Object.keys(LEGACY_CUSTOM)));
  });

  it("draws the same fit as before, all 300 of them", () => {
    for (const [key, want] of entries) {
      const [preset, focused, panel, custom, win] = key.split("/");
      const [w, h, z] = win.match(/^(\d+)x(\d+)@(\d+)$/)!.slice(1).map(Number);
      const stored = JSON.stringify({
        preset, focused: Number(focused), panel: { open: panel === "panel", height: 260 },
        panes: [{ view: "chat" }, { view: "preview" }, { view: "file" }, { view: "task" }],
        ...(custom === "custom" ? { splits: { [preset]: LEGACY_CUSTOM[preset] } } : {}),
      });
      const parsed = parseWorkspace(stored);
      const f = fitWorkspace(DEFAULT_LAYOUT, parsed, w / (z / 100), h / (z / 100) - TITLEBAR_H);
      const got = [
        f.drawn, f.panes.join(""), f.dropped, f.why, f.cols.map(r6), r6(f.row),
        [f.panel.open, f.panel.height, f.panel.auto],
        [f.sides.left, f.sides.right, f.sides.leftFolded, f.sides.rightFolded, f.sides.autoLeft, f.sides.autoRight],
      ];
      expect(got, key).toEqual(want);
      expect(f.bottom, key).toEqual([]);
    }
  });

  it("reads as ids 1…n, nothing kept, the other specs as stored", () => {
    for (const p of Object.keys(LEGACY_CUSTOM) as Preset[]) {
      const w = parseWorkspace(JSON.stringify({ preset: p, panes: [{ view: "diff" }, { view: "file" }] }));
      expect(w.order, p).toEqual(PANE_NOS.slice(0, SHAPES[p].panes));
      expect(w.kept, p).toEqual([]);
      expect(w.panes.map((x) => x.view), p)
        .toEqual(["diff", "file", "file", "task", "chat", "chat", "chat", "chat"]);
    }
    // ⊞ back to four from a stored one pane brings back what WP-A kept there.
    const one = parseWorkspace(JSON.stringify({
      preset: "single", panes: [{ view: "chat" }, { view: "diff" }, { view: "terminal", terminalId: "t-1" }, { view: "file" }],
    }));
    const four = setPreset(one, "grid4");
    expect(four.order).toEqual([1, 2, 3, 4]);
    expect(four.panes.slice(0, 4).map((x) => x.view)).toEqual(["chat", "diff", "terminal", "file"]);
  });
});

describe("parsing order, kept, bottom and filePath (§3.2)", () => {
  const good = {
    preset: "grid5", order: [1, 5, 2, 3, 4], kept: [6], focused: 5,
    splits: { grid5: { cols: [0.3, 0.6], bottom: [0.4], row: 0.55 }, cols2: { cols: [0.3], row: 0.5 } },
    panes: [{ view: "file", filePath: "src/app.ts" }, { view: "chat" }, { view: "diff", filePath: "README.md" }],
    panel: { open: true, height: 300 },
  };
  const base = parseWorkspace(JSON.stringify(good));

  it("reads a good value whole", () => {
    expect(base.preset).toBe("grid5");
    expect(base.order).toEqual([1, 5, 2, 3, 4]);
    expect(base.kept).toEqual([6]);
    expect(base.focused).toBe(5);
    expect(base.splits.grid5).toEqual({ cols: [0.3, 0.6], bottom: [0.4], row: 0.55 });
    expect(base.panes[0].filePath).toBe("src/app.ts");
    expect(base.panes[2].filePath).toBe("README.md");
    expect(base.panes[1].filePath).toBeUndefined();
    expect(base.panes).toHaveLength(8);
  });

  const variants: [string, Record<string, unknown>, (w: Workspace) => void][] = [
    ["a repeated id in order", { order: [1, 1, 2, 3, 4] }, (w) => expect(w.order).toEqual([1, 2, 3, 4, 5])],
    ["too few ids in order", { order: [1, 5, 2] }, (w) => expect(w.order).toEqual([1, 2, 3, 4, 5])],
    ["an id out of range in order", { order: [1, 5, 2, 3, 9] }, (w) => expect(w.order).toEqual([1, 2, 3, 4, 5])],
    ["order not a list", { order: "1,5,2,3,4" }, (w) => expect(w.order).toEqual([1, 2, 3, 4, 5])],
    ["kept not a list", { kept: 6 }, (w) => expect(w.kept).toEqual([])],
    ["kept holding junk, repeats and drawn ids", { kept: [9, 2, "7", 6, 6, 0, 7] }, (w) => expect(w.kept).toEqual([6, 7])],
    ["a bottom edge of the wrong count", { splits: { ...good.splits, grid5: { cols: [0.3, 0.6], bottom: [0.4, 0.8], row: 0.55 } } },
      (w) => expect(w.splits.grid5).toEqual({ cols: [0.3, 0.6], bottom: [0.5], row: 0.55 })],
    ["a bottom edge outside (0, 1)", { splits: { ...good.splits, grid5: { cols: [0.3, 0.6], bottom: [1.4], row: 0.55 } } },
      (w) => expect(w.splits.grid5).toEqual({ cols: [0.3, 0.6], bottom: [0.5], row: 0.55 })],
    ["filePath not a string", { panes: [{ view: "file", filePath: 7 }, { view: "chat" }, { view: "diff", filePath: "README.md" }] },
      (w) => expect(w.panes[0].filePath).toBeUndefined()],
    ["filePath too long", { panes: [{ view: "file", filePath: "a".repeat(PATH_MAX + 1) }, { view: "chat" }, { view: "diff", filePath: "README.md" }] },
      (w) => expect(w.panes[0].filePath).toBeUndefined()],
  ];

  for (const [what, bad, only] of variants) {
    it(`falls back on ${what}, and only there`, () => {
      const w = parseWorkspace(JSON.stringify({ ...good, ...bad }));
      only(w);
      // Every other field is what the good value gave.
      const field = Object.keys(bad)[0] as keyof Workspace;
      for (const k of ["preset", "order", "kept", "focused", "panel", "splits", "panes"] as (keyof Workspace)[]) {
        if (k === field) continue;
        expect(w[k], `${what}: ${k}`).toEqual(base[k]);
      }
    });
  }

  it("keeps a stored focus only if it is drawn, else the first in reading order", () => {
    expect(parseWorkspace(JSON.stringify({ ...good, focused: 6 })).focused).toBe(1);
    expect(parseWorkspace(JSON.stringify({ ...good, order: [5, 1, 2, 3, 4], focused: 8 })).focused).toBe(5);
  });

  it("round-trips order, kept and filePath through storage", () => {
    const s = memory();
    saveWorkspace(base, s);
    expect(loadWorkspace(s)).toEqual(base);
  });

  it("reads a preset this HUD does not know as single (a newer HUD's, rolled back)", () => {
    const w = parseWorkspace(JSON.stringify({ preset: "grid9", order: [1, 2, 3, 4, 5, 6, 7, 8, 9] }));
    expect([w.preset, w.order, w.focused]).toEqual(["single", [1], 1]);
  });
});

describe("a File pane's tabs (ED-2, PaneSpec.editors)", () => {
  const stored = (panes: unknown[]) => parseWorkspace(JSON.stringify({ preset: "cols2", panes }));

  it("are read field by field, and only with a pinned project", () => {
    const w = stored([{ view: "chat" }, { view: "file", projectId: "p1",
      editors: { paths: ["a.py", "src/b.ts"], active: "src/b.ts", preview: "src/b.ts" } }]);
    expect(w.panes[1].editors).toEqual({ paths: ["a.py", "src/b.ts"], active: "src/b.ts", preview: "src/b.ts" });
    const bad = stored([{ view: "chat" }, { view: "file", projectId: "p1", editors: { paths: ["a.py"], active: 9, preview: "x" } }]);
    expect(bad.panes[1].editors).toEqual({ paths: ["a.py"], active: "a.py", preview: null });
    expect(bad.panes[1].projectId).toBe("p1");
    // No project: the paths would mean nothing.
    expect(stored([{ view: "chat" }, { view: "file", editors: { paths: ["a.py"] } }]).panes[1].editors).toBeUndefined();
    expect(stored([{ view: "chat" }, { view: "file", projectId: "p1", editors: "a.py" }]).panes[1].editors).toBeUndefined();
  });

  it("take a WP-G2 filePath as one tab", () => {
    const w = stored([{ view: "chat" }, { view: "file", projectId: "p1", filePath: "calc.py" }]);
    expect(w.panes[1].editors).toEqual({ paths: ["calc.py"], active: "calc.py", preview: null });
  });

  it("round-trip through storage, and never hold text", () => {
    const s = memory();
    const w = setEditors(defaultWorkspace(), 3, "p1", { paths: ["a.py"], active: "a.py", preview: null });
    saveWorkspace(w, s);
    expect(loadWorkspace(s).panes[2].editors).toEqual({ paths: ["a.py"], active: "a.py", preview: null });
    expect(JSON.stringify(loadWorkspace(s))).not.toContain("content");
  });

  it("setEditors pins the pane to their project in the same step; null clears them, the pin kept", () => {
    let w = setEditors(defaultWorkspace(), 3, "p1", { paths: ["a.py"], active: "a.py", preview: "a.py" });
    expect(w.panes[2]).toMatchObject({ projectId: "p1", editors: { paths: ["a.py"] } });
    expect(setEditors(w, 3, "p1", { paths: ["a.py"], active: "a.py", preview: "a.py" })).toBe(w);
    w = setEditors(w, 3, "p1", null);
    expect(w.panes[2].projectId).toBe("p1");
    expect(w.panes[2].editors).toBeUndefined();
  });

  it("a pin to another project, Follow chat or a project gone drops them — except a pane kept for an unsaved edit", () => {
    const w = setEditors(defaultWorkspace(), 3, "p1", { paths: ["a.py"], active: "a.py", preview: null });
    expect(pinPane(w, 3, "p2").panes[2].editors).toBeUndefined();
    expect(pinPane(w, 3, null).panes[2].editors).toBeUndefined();
    expect(pinPane(w, 3, "p1")).toBe(w);
    expect(unpinProject(w, "p1").panes[2].editors).toBeUndefined();
    expect(unpinProject(w, "p1", [3]).panes[2].editors).toEqual({ paths: ["a.py"], active: "a.py", preview: null });
    // And another project's tabs were never this one's.
    expect(setEditors(w, 3, "p2", { paths: ["z.py"], active: "z.py", preview: null }).panes[2])
      .toMatchObject({ projectId: "p2", editors: { paths: ["z.py"] } });
  });
});

describe("⊞: fewer panes hide, more panes bring them back (plan G-2)", () => {
  const eight = () => {
    let w = setPreset(defaultWorkspace(), "grid8");
    const views = ["chat", "preview", "file", "task", "diff", "terminal", "chat", "file"] as const;
    views.forEach((v, i) => (w = setView(w, (i + 1) as PaneNo, v)));
    return pinPane(setPreviewUrl(w, 2, "http://localhost:5173/"), 8, "p2");
  };

  it("8 → 1 → 8 brings back every pane, in its place, with what it showed", () => {
    const w = eight();
    expect(w.order).toEqual([1, 2, 3, 4, 5, 6, 7, 8]);
    const one = setPreset(w, "single");
    expect([one.order, one.kept]).toEqual([[1], [2, 3, 4, 5, 6, 7, 8]]);
    const back = setPreset(one, "grid8");
    expect([back.order, back.kept]).toEqual([[1, 2, 3, 4, 5, 6, 7, 8], []]);
    expect(back.panes).toEqual(w.panes);
  });

  it("takes back the hidden panes first, most recently hidden first, then free ids", () => {
    let w = setPreset(defaultWorkspace(), "grid4");      // [1 2 3 4]
    w = setPreset(w, "cols2");                           // [1 2], 3 4 hidden
    w = setPreset(w, "single");                          // [1], 2 3 4 hidden
    expect(w.kept).toEqual([2, 3, 4]);
    expect(setPreset(w, "cols2").order).toEqual([1, 2]); // pane 2 comes back to slot 2, not a new pane
    expect(setPreset(w, "grid6").order).toEqual([1, 2, 3, 4, 5, 6]);
    // A stored order: the hidden one comes back before a free one.
    const stored = parseWorkspace(JSON.stringify({ preset: "cols3", order: [1, 5, 2] }));
    const one = setPreset(stored, "single");
    expect([one.order, one.kept]).toEqual([[1], [5, 2]]);
    expect(setPreset(one, "grid4").order).toEqual([1, 5, 2, 3]);
  });

  it("the same count changes only the shape", () => {
    const w = setPreset(parseWorkspace(JSON.stringify({ preset: "cols3", order: [3, 1, 2], kept: [7] })), "main2");
    expect([w.preset, w.order, w.kept]).toEqual(["main2", [3, 1, 2], [7]]);
  });

  it("hands focus to the first pane in reading order when it hides the focused one", () => {
    const w = focusPane(setPreset(defaultWorkspace(), "grid8"), 7);
    expect(setPreset(w, "grid6").focused).toBe(1);
    expect(setPreset(w, "grid7").focused).toBe(7);
    const stored = parseWorkspace(JSON.stringify({ preset: "cols3", order: [5, 1, 2], focused: 2 }));
    expect(setPreset(stored, "single").focused).toBe(5);
  });

  it("Reset layout keeps the first pane in reading order and hides the rest", () => {
    const stored = parseWorkspace(JSON.stringify({ preset: "cols3", order: [5, 1, 2], focused: 2 }));
    const r = resetWorkspace(stored);
    expect([r.preset, r.order, r.kept, r.focused]).toEqual(["single", [5], [1, 2], 5]);
  });
});

describe("ids are not slots (plan G-1)", () => {
  const w = parseWorkspace(JSON.stringify({ preset: "cols3", order: [1, 5, 2], kept: [7] }));

  it("numbers a pane by its place in reading order", () => {
    expect([1, 5, 2].map((n) => slotOf(w, n as PaneNo))).toEqual([1, 2, 3]);
    expect([1, 2, 3].map((s) => paneAt(w, s))).toEqual([1, 5, 2]);
    expect(paneAt(w, 4)).toBeNull();
    // A hidden pane reads as the slot a grow would bring it back in.
    expect(slotOf(w, 7)).toBe(4);
    expect(setPreset(w, "grid4").order[3]).toBe(7);
    expect(paneSequence(w)).toEqual([1, 5, 2, 7, 3, 4, 6, 8]);
  });

  it("is the identity for every WP-A and WP-B workspace", () => {
    for (const p of PRESETS) {
      const x = setPreset(defaultWorkspace(), p);
      for (const n of x.order) expect(slotOf(x, n)).toBe(n);
    }
  });

  it("reads a hand-made workspace whose order has the wrong count as a grow would", () => {
    expect(orderOf({ preset: "grid4", order: [1], kept: [3] })).toEqual([1, 3, 2, 4]);
  });
});

describe("the grid: gridTemplate and cellsOf", () => {
  const T = (p: Preset, edges = defaultWorkspace().splits[p]) => gridTemplate(p, edges);
  const close = (a: number[], b: number[]) => {
    expect(a.length).toBe(b.length);
    a.forEach((x, i) => expect(x).toBeCloseTo(b[i], 9));
  };

  it("draws all ten shapes at their default edges", () => {
    const want: Record<Preset, [number[], number[], string[]]> = {
      single: [[1], [1], ["s1"]],
      cols2: [[0.5, 0.5], [1], ["s1 s2"]],
      rows2: [[1], [0.5, 0.5], ["s1", "s2"]],
      cols3: [[1 / 3, 1 / 3, 1 / 3], [1], ["s1 s2 s3"]],
      main2: [[0.6, 0.4], [0.5, 0.5], ["s1 s2", "s1 s3"]],
      grid4: [[0.5, 0.5], [0.5, 0.5], ["s1 s2", "s3 s4"]],
      grid5: [[1 / 3, 1 / 6, 1 / 6, 1 / 3], [0.5, 0.5], ["s1 s2 s2 s3", "s4 s4 s5 s5"]],
      grid6: [[1 / 3, 1 / 3, 1 / 3], [0.5, 0.5], ["s1 s2 s3", "s4 s5 s6"]],
      grid7: [[1 / 4, 1 / 12, 1 / 6, 1 / 6, 1 / 12, 1 / 4], [0.5, 0.5], ["s1 s2 s2 s3 s3 s4", "s5 s5 s6 s6 s7 s7"]],
      grid8: [[0.25, 0.25, 0.25, 0.25], [0.5, 0.5], ["s1 s2 s3 s4", "s5 s6 s7 s8"]],
    };
    for (const p of PRESETS) {
      const t = T(p);
      close(t.columns, want[p][0]);
      close(t.rows, want[p][1]);
      expect(t.areas, p).toEqual(want[p][2]);
    }
  });

  it("gives the bottom row of 3 over 2 and 4 over 3 its own edges, never the top row's", () => {
    // ⅓ ⅔ over 0.4: units ⅓ 0.0667 0.2667 ⅓.
    const t = gridTemplate("grid5", { cols: [1 / 3, 2 / 3], row: 0.5, bottom: [0.4] });
    close(t.columns, [1 / 3, 0.4 - 1 / 3, 2 / 3 - 0.4, 1 / 3]);
    expect(t.areas).toEqual(["s1 s2 s2 s3", "s4 s4 s5 s5"]);
    const c = cellsOf("grid5", { cols: [0.3, 0.6], row: 0.55, bottom: [0.7] });
    expect(c.map((x) => [x.left, x.right, x.top, x.bottom])).toEqual([
      [0, 0.3, 0, 0.55], [0.3, 0.6, 0, 0.55], [0.6, 1, 0, 0.55], [0, 0.7, 0.55, 1], [0.7, 1, 0.55, 1],
    ]);
    const g7 = cellsOf("grid7", { cols: [0.2, 0.5, 0.8], row: 0.5, bottom: [0.45, 0.55] });
    expect(g7.slice(4).map((x) => [x.left, x.right])).toEqual([[0, 0.45], [0.45, 0.55], [0.55, 1]]);
  });

  it("merges coincident edges", () => {
    const t = gridTemplate("grid5", { cols: [1 / 3, 2 / 3], row: 0.5, bottom: [1 / 3] });
    close(t.columns, [1 / 3, 1 / 3, 1 / 3]);
    expect(t.areas).toEqual(["s1 s2 s3", "s4 s5 s5"]);
    // A hair apart is the same edge, not a sliver column.
    const near = gridTemplate("grid7", { cols: [0.25, 0.5, 0.75], row: 0.5, bottom: [0.5 + 1e-6, 0.75 - 1e-6] });
    expect(near.columns).toHaveLength(4);
    expect(near.areas).toEqual(["s1 s2 s3 s4", "s5 s5 s6 s7"]);
  });

  it("spans a full-height column for main2 and a full-width row for the stacked shapes", () => {
    expect(T("main2", { cols: [0.7], row: 0.3 }).areas).toEqual(["s1 s2", "s1 s3"]);
    close(T("main2", { cols: [0.7], row: 0.3 }).rows, [0.3, 0.7]);
  });

  it("gives each slot its own row's width", () => {
    const f = fitWorkspace(DEFAULT_LAYOUT, setSplit(setPreset(defaultWorkspace(), "grid5"), "grid5",
      { cols: [0.3, 0.6], bottom: [0.7] }), 3000, 1000);
    expect(f.bottom).toEqual([0.7]);
    const w = slotWidths(f).map((x) => Math.round(x / f.width * 100));
    expect(w).toEqual([30, 30, 40, 70, 30]);
  });

  it("moves only the bottom row's edge when the bottom row's is dragged", () => {
    const w = setSplit(setPreset(defaultWorkspace(), "grid7"), "grid7", { bottom: [0.4, 0.6] });
    expect(w.splits.grid7).toEqual({ cols: [0.25, 0.5, 0.75], bottom: [0.4, 0.6], row: 0.5 });
    expect(equalSplit(w, "grid7").splits.grid7).toEqual({ cols: [0.25, 0.5, 0.75], bottom: [1 / 3, 2 / 3], row: 0.5 });
  });
});

describe("fitting eight panes: the generic drop (§3.3)", () => {
  const g = (p: Preset, order?: PaneNo[]) => (order ?? setPreset(defaultWorkspace(), p).order);

  it("drops a column from every row, keeping the window that holds the focused pane", () => {
    expect(dropColumn("grid8", g("grid8"), 7)).toEqual({ shape: "grid6", panes: [1, 2, 3, 5, 6, 7] });
    expect(dropColumn("grid8", g("grid8"), 8)).toEqual({ shape: "grid6", panes: [2, 3, 4, 6, 7, 8] });
    expect(dropColumn("grid8", g("grid8"), 4)).toEqual({ shape: "grid6", panes: [2, 3, 4, 6, 7, 8] });
    expect(dropColumn("grid8", g("grid8"), 1)).toEqual({ shape: "grid6", panes: [1, 2, 3, 5, 6, 7] });
    expect(dropColumn("grid7", g("grid7"), 4)).toEqual({ shape: "grid6", panes: [2, 3, 4, 5, 6, 7] });
    expect(dropColumn("grid7", g("grid7"), 6)).toEqual({ shape: "grid6", panes: [1, 2, 3, 5, 6, 7] });
    expect(dropColumn("grid6", g("grid6"), 6)).toEqual({ shape: "grid4", panes: [2, 3, 5, 6] });
    expect(dropColumn("grid6", g("grid6"), 4)).toEqual({ shape: "grid4", panes: [1, 2, 4, 5] });
    expect(dropColumn("grid5", g("grid5"), 3)).toEqual({ shape: "grid4", panes: [2, 3, 4, 5] });
    expect(dropColumn("grid5", g("grid5"), 5)).toEqual({ shape: "grid4", panes: [1, 2, 4, 5] });
    expect(dropColumn("rows2", [1, 2], 2)).toBeNull();
  });

  it("drops a row, keeping the focused pane's; a four-wide row loses a column too", () => {
    expect(dropRow("grid8", g("grid8"), 6)).toEqual({ shape: "cols3", panes: [5, 6, 7] });
    expect(dropRow("grid8", g("grid8"), 8)).toEqual({ shape: "cols3", panes: [6, 7, 8] });
    expect(dropRow("grid7", g("grid7"), 2)).toEqual({ shape: "cols3", panes: [1, 2, 3] });
    expect(dropRow("grid7", g("grid7"), 7)).toEqual({ shape: "cols3", panes: [5, 6, 7] });
    expect(dropRow("grid6", g("grid6"), 2)).toEqual({ shape: "cols3", panes: [1, 2, 3] });
    expect(dropRow("grid5", g("grid5"), 5)).toEqual({ shape: "cols2", panes: [4, 5] });
    expect(dropRow("grid5", g("grid5"), 1)).toEqual({ shape: "cols3", panes: [1, 2, 3] });
  });

  it("drops by position, whatever the ids", () => {
    expect(dropColumn("cols3", [4, 8, 1], 1)).toEqual({ shape: "cols2", panes: [8, 1] });
    expect(dropColumn("grid8", [8, 7, 6, 5, 4, 3, 2, 1], 5)).toEqual({ shape: "grid6", panes: [7, 6, 5, 3, 2, 1] });
  });

  it("never drops the focused pane: every shape × every focused pane × widths 400–2000 × four heights", () => {
    const orders: PaneNo[][] = [[1, 2, 3, 4, 5, 6, 7, 8], [8, 3, 6, 1, 7, 2, 5, 4]];
    for (const p of PRESETS) {
      const n = SHAPES[p].panes;
      for (const base of orders) {
        const order = base.filter((x) => x <= n || base.indexOf(x) < n).slice(0, n);
        for (const f of order) {
          const w: Workspace = { ...defaultWorkspace(), preset: p, order, focused: f,
                                 panel: { open: true, height: 260 } };
          for (let px = 400; px <= 2000; px += 25) {
            for (const py of [300, 450, 700, 1000]) {
              const fit = fitWorkspace(DEFAULT_LAYOUT, w, px, py);
              expect(fit.panes, `${p} ${order} focused ${f} at ${px}x${py}`).toContain(f);
              expect(fit.panes.length, `${p} at ${px}x${py}`).toBe(SHAPES[fit.drawn].panes);
              expect(fit.dropped).toBe(n - fit.panes.length);
            }
          }
        }
      }
    }
  });

  it("keeps a dropped set of eight sticky while focus moves inside it", () => {
    const [aw, ah] = room(1280, 800, 100);
    const at8 = focusPane(setPreset(defaultWorkspace(), "grid8"), 8);
    const first = fitWorkspace(DEFAULT_LAYOUT, at8, aw, ah);
    expect(first).toMatchObject({ drawn: "grid6", panes: [2, 3, 4, 6, 7, 8] });
    const at2 = focusPane(at8, 2);
    const prev: DrawnSet = { preset: "grid8", drawn: first.drawn, panes: first.panes };
    expect(fitWorkspace(DEFAULT_LAYOUT, at2, aw, ah, null, false, prev).panes).toEqual([2, 3, 4, 6, 7, 8]);
    expect(fitWorkspace(DEFAULT_LAYOUT, at2, aw, ah).panes).toEqual([1, 2, 3, 5, 6, 7]);
    // Focus moved to a pane it does not draw (Ctrl+Alt+1): a set that holds it.
    expect(fitWorkspace(DEFAULT_LAYOUT, focusPane(at8, 1), aw, ah, null, false, prev).panes).toEqual([1, 2, 3, 5, 6, 7]);
    // A different order is never sticky, even with the same ids.
    const shuffled = { ...at2, order: [2, 1, 3, 4, 5, 6, 7, 8] as PaneNo[] };
    expect(fitWorkspace(DEFAULT_LAYOUT, shuffled, aw, ah, null, false, prev).panes).not.toEqual([2, 3, 4, 6, 7, 8]);
  });
});

describe("fitting eight panes: the plan's windows (§3.3)", () => {
  // The plan's table: an app window's inner height is the screen less ~80.
  const at = (p: Preset, w: number, screenH: number, zoom: number, panel = false) =>
    fitAt(setPanel(setPreset(defaultWorkspace(), p), { open: panel }), w, screenH - 80, zoom);
  const cols = (f: ReturnType<typeof fitAt>) => slotWidths(f).map((x) => Math.round(x));

  it("1920×1080: grid4 and grid6 keep the side panes; grid8 squeezes them to 480 for four 360 columns", () => {
    for (const p of ["grid4", "grid6", "grid5"] as Preset[]) {
      const f = at(p, 1920, 1080, 100);
      expect(f.drawn).toBe(p);
      expect(f.sides).toMatchObject({ left: 236, right: 316, leftFolded: false, rightFolded: false });
    }
    expect(cols(at("grid6", 1920, 1080, 100))).toEqual([456, 456, 456, 456, 456, 456]);
    for (const p of ["grid8", "grid7"] as Preset[]) {
      const f = at(p, 1920, 1080, 100);
      expect(f).toMatchObject({ drawn: p, dropped: 0 });
      expect(f.sides.leftFolded || f.sides.rightFolded).toBe(false);
      expect(f.sides.left + f.sides.right).toBe(480);
      expect(f.width).toBe(1440);
    }
    expect(cols(at("grid8", 1920, 1080, 100))).toEqual([360, 360, 360, 360, 360, 360, 360, 360]);
  });

  it("1366×768: grid4 fits; grid6 folds the status pane; grid8 draws 6 of 8", () => {
    expect(at("grid4", 1366, 768, 100)).toMatchObject({ drawn: "grid4", dropped: 0, sides: { rightFolded: false } });
    for (const p of ["grid6", "grid5"] as Preset[]) {
      expect(at(p, 1366, 768, 100)).toMatchObject({
        drawn: p, dropped: 0, sides: { rightFolded: true, autoRight: true, leftFolded: false } });
    }
    expect(at("grid8", 1366, 768, 100)).toMatchObject({
      drawn: "grid6", dropped: 2, why: "narrow", sides: { rightFolded: true, leftFolded: false } });
    expect(at("grid7", 1366, 768, 100)).toMatchObject({ drawn: "grid6", dropped: 1 });
  });

  it("1280×800: grid4 fits at 728; grid6 folds both side panes (1208); grid8 draws 6 of 8 on both rails", () => {
    const g4 = at("grid4", 1280, 800, 100);
    expect(g4).toMatchObject({ drawn: "grid4", dropped: 0, width: 728 });
    const g6 = at("grid6", 1280, 800, 100);
    expect(g6).toMatchObject({ drawn: "grid6", width: 1208, sides: { leftFolded: true, rightFolded: true } });
    const g8 = at("grid8", 1280, 800, 100);
    expect(g8).toMatchObject({ drawn: "grid6", dropped: 2, sides: { leftFolded: true, rightFolded: true } });
  });

  it("1280×800 at 150%: grid4 on both rails with the panel folded; 4 of 6; 4 of 8", () => {
    const g4 = at("grid4", 1280, 800, 150, true);
    expect(g4).toMatchObject({ drawn: "grid4", sides: { leftFolded: true, rightFolded: true },
                               panel: { open: false, auto: true } });
    expect(at("grid6", 1280, 800, 150)).toMatchObject({ drawn: "grid4", dropped: 2 });
    expect(at("grid8", 1280, 800, 150)).toMatchObject({ drawn: "grid4", dropped: 4 });
    expect(at("grid5", 1280, 800, 150)).toMatchObject({ drawn: "grid4", dropped: 1 });
    expect(at("grid7", 1280, 800, 150)).toMatchObject({ drawn: "grid4", dropped: 3 });
  });

  it("1280×800 at 80% (1600 wide): eight fit, on both rails (1528 ≥ 1440)", () => {
    for (const p of ["grid4", "grid6", "grid8"] as Preset[]) expect(at(p, 1280, 800, 80)).toMatchObject({ drawn: p, dropped: 0 });
    expect(at("grid8", 1280, 800, 80)).toMatchObject({ width: 1528, sides: { leftFolded: true, rightFolded: true } });
  });
});

// ---- WP-G2: split and close ----------------------------------------------------

describe("split and close (WP-G2, plan §3.4–§3.6)", () => {
  const chat = (): ReturnType<typeof defaultPane> => ({ view: "chat", projectId: null, terminalId: null });
  /** Split the pane in slot `slot` into the lowest free id, as the window does. */
  const splitAt = (w: Workspace, slot: number) => splitPane(w, orderOf(w)[slot - 1], allocate(w)!, chat());

  it("climbs the ladder 1 → 8 and back, one pane at a time, on exactly the ladder's shapes", () => {
    let w = defaultWorkspace();
    const up: Preset[] = [];
    for (let n = 1; n < 8; n++) {
      w = splitAt(w, n);
      up.push(w.preset);
      expect(w.order).toHaveLength(n + 1);
      expect(SHAPES[w.preset].panes).toBe(n + 1);
    }
    expect(up).toEqual(["cols2", "cols3", "grid4", "grid5", "grid6", "grid7", "grid8"]);
    expect(splitAt(w, 1)).toBe(w); // eight is the most
    const down: Preset[] = [];
    for (let i = 0; i < 8 && w.order.length > 1; i++) {
      w = closePane(w, w.order[w.order.length - 1]);
      down.push(w.preset);
    }
    expect(down).toEqual(["grid7", "grid6", "grid5", "grid4", "cols3", "cols2", "single"]);
    expect(closePane(w, w.order[0])).toBe(w); // the last pane is never closed
  });

  it("lands on the ladder's shape whatever the shape it starts from: rows2 splits to cols3, main2 closes to cols2", () => {
    const rows2 = setPreset(defaultWorkspace(), "rows2");
    expect(splitPane(rows2, 1, 3, chat()).preset).toBe("cols3");
    const main2 = setPreset(defaultWorkspace(), "main2");
    expect(closePane(main2, 3).preset).toBe("cols2");
  });

  it("puts the new pane right after its source, in reading order — never at the end", () => {
    const three = setPreset(defaultWorkspace(), "cols3");
    const w = splitPane(three, 2, 4, chat());
    expect(w.order).toEqual([1, 2, 4, 3]);
    expect(slotOf(w, 4)).toBe(3);
    expect(w.preset).toBe("grid4");
  });

  it("focuses the new pane; × hands focus to the pane now in that slot, else the slot before", () => {
    const w = splitPane(setPreset(defaultWorkspace(), "cols3"), 2, 4, chat());
    expect(w.focused).toBe(4);
    const closed = closePane(w, 4);
    expect(closed.order).toEqual([1, 2, 3]);
    expect(closed.focused).toBe(3); // slot 3 is pane 3 now
    const last = closePane(focusPane(closed, 3), 3);
    expect(last.focused).toBe(2); // no slot 3 any more: the one before
    expect(closePane(focusPane(closed, 1), 3).focused).toBe(1); // focus elsewhere stays put
    // The window names where focus goes when the selected chat closes: the chat selected before it.
    const grid = setPreset(defaultWorkspace(), "grid4");
    expect(closePane(grid, 1, 3).focused).toBe(3);
    expect(closePane(grid, 1, 1).focused).toBe(2); // not drawn after the close: the slot's pane
    expect(closePane(grid, 1, null).focused).toBe(2);
  });

  it("splits without moving focus when asked not to ({focus: false}, the browser's dev-server pane)", () => {
    const w = focusPane(setPreset(defaultWorkspace(), "cols2"), 1);
    const quiet = splitPane(w, 2, 3, { view: "preview", projectId: null, terminalId: null }, { focus: false });
    expect(quiet.order).toEqual([1, 2, 3]);
    expect(quiet.focused).toBe(1);
    expect(splitPane(w, 2, 3, chat()).focused).toBe(3);
  });

  it("refuses an id already drawn, a source not drawn, and a ninth pane", () => {
    const w = setPreset(defaultWorkspace(), "cols2");
    expect(splitPane(w, 1, 2, chat())).toBe(w);
    expect(splitPane(w, 5, 3, chat())).toBe(w);
    const eight = setPreset(defaultWorkspace(), "grid8");
    expect(eight.order).toHaveLength(MAX_PANES);
    expect(splitPane(eight, 1, 2, chat())).toBe(eight);
  });

  it("allocates the lowest free id, else the oldest hidden pane with no unsaved edit, else none", () => {
    expect(allocate(defaultWorkspace())).toBe(2);
    const gap = closePane(setPreset(defaultWorkspace(), "grid4"), 2);
    expect(allocate(gap)).toBe(2);
    // All eight used: 1 drawn, 2…8 hidden (8 hidden longest, it is last).
    const one = setPreset(setPreset(defaultWorkspace(), "grid8"), "single");
    expect(allocate(one)).toBe(8);
    expect(allocate(one, [8])).toBe(7);
    expect(allocate(one, [2, 3, 4, 5, 6, 7, 8])).toBeNull();
    // A hidden pane a split takes back leaves `kept` and shows the copy.
    const took = splitPane(one, 1, 8, chat());
    expect(took.kept).toEqual([2, 3, 4, 5, 6, 7]);
    expect(took.order).toEqual([1, 8]);
  });

  it("resets only the closed pane's spec, and leaves kept and every preset's edges alone", () => {
    let w = setPreset(defaultWorkspace(), "grid8");
    w = setPreset(w, "grid6"); // 7 and 8 hidden
    w = setSplit(w, "grid6", { cols: [0.2, 0.5], row: 0.3 });
    w = setSplit(w, "grid5", { cols: [0.25, 0.5], bottom: [0.7], row: 0.4 });
    w = setPreviewUrl(setView(w, 3, "preview"), 3, "http://localhost:5173/");
    w = setView(w, 4, "file");
    const closed = closePane(w, 3);
    expect(closed.panes[2]).toEqual(defaultPane(3));
    expect(closed.panes.filter((_, i) => i !== 2)).toEqual(w.panes.filter((_, i) => i !== 2));
    expect(closed.kept).toEqual(w.kept);
    expect(closed.splits).toEqual(w.splits);
    expect(closed.preset).toBe("grid5");
    expect(closed.order).toEqual([1, 2, 4, 5, 6]);
  });

  it("closes and splits ids, never slots: after pane 1 is closed nothing assumes it is there", () => {
    const w = closePane(setPreset(defaultWorkspace(), "cols3"), 1);
    expect(w.order).toEqual([2, 3]);
    expect(paneAt(w, 1)).toBe(2);
    expect(slotOf(w, 3)).toBe(2);
    // A free id 1 is the next a split takes, and it lands after its source.
    const back = splitPane(w, 3, allocate(w)!, chat());
    expect(back.order).toEqual([2, 3, 1]);
    expect(slotOf(back, 1)).toBe(3);
    // Stored and read back, the order survives without a pane 1 first.
    const m = memory();
    saveWorkspace(w, m);
    expect(loadWorkspace(m).order).toEqual([2, 3]);
    expect(loadWorkspace(m).focused).toBe(2);
  });

  it("with nothing drawn, the fallbacks are the first pane in reading order, never id 1 (U36/U37)", () => {
    const w = closePane(setPreset(defaultWorkspace(), "cols2"), 1); // only pane 2, which shows a preview
    expect(w.order).toEqual([2]);
    expect(show(w, "file", []).panes[1].view).toBe("file");
    expect(show(w, "file", []).panes[0].view).toBe(w.panes[0].view);
    expect(show(w, "file", []).focused).toBe(2);
  });

  it("copies what a pane shows (decisions S-2, plan §3.5)", () => {
    const base = { projectId: "p1", terminalId: "t1", previewUrl: "http://localhost:5173/x", keepOrigin: "http://localhost:5173",
                   filePath: "src/a.ts" };
    expect(copySpec({ ...base, view: "file" })).toEqual({ view: "file", projectId: "p1", terminalId: null, filePath: "src/a.ts" });
    expect(copySpec({ ...base, view: "diff" })).toEqual({ view: "diff", projectId: "p1", terminalId: null, filePath: "src/a.ts" });
    // The keep-origin grant is the owner's choice for one pane: never copied (WP-E).
    expect(copySpec({ ...base, view: "preview" })).toEqual({
      view: "preview", projectId: "p1", terminalId: null, previewUrl: "http://localhost:5173/x" });
    // A task view's copy is that task's diff (S-7).
    expect(copySpec({ ...base, view: "task" })).toEqual({ view: "diff", projectId: null, terminalId: null });
    // A chat and a terminal start empty: the window fills them (a compose row, a new terminal).
    expect(copySpec({ ...base, view: "chat" })).toEqual({ view: "chat", projectId: null, terminalId: null });
    expect(copySpec({ ...base, view: "terminal" })).toEqual({ view: "terminal", projectId: null, terminalId: null });
    // A File pane's tabs (ED-2): the copy holds the same buffers; its preview tab is pinned there.
    const tabs = { paths: ["a.py", "b.py"], active: "b.py", preview: "b.py" };
    expect(copySpec({ view: "file", projectId: "p1", terminalId: null, editors: tabs })).toEqual({
      view: "file", projectId: "p1", terminalId: null, editors: { paths: ["a.py", "b.py"], active: "b.py", preview: null } });
    // An empty pane copies empty.
    expect(copySpec({ view: "file", projectId: null, terminalId: null })).toEqual({ view: "file", projectId: null, terminalId: null });
    expect(copySpec({ view: "preview", projectId: null, terminalId: null })).toEqual({ view: "preview", projectId: null, terminalId: null });
  });

  it("decides the split button from the count and the room (G-5)", () => {
    const at = (n: number, w: number, h: number, zoom = 100) => {
      let x = defaultWorkspace();
      // Bounded, so a split that stops working fails rather than hangs.
      for (let i = 0; i < 8 && x.order.length < n; i++) x = splitAt(x, x.order.length);
      expect(x.order).toHaveLength(n);
      const [aw, ah] = room(w, h - 80, zoom);
      return splitVerdict(DEFAULT_LAYOUT, x, aw, ah);
    };
    expect(at(2, 1280, 800)).toEqual({ ok: true, why: "" });
    expect(at(6, 1280, 800)).toEqual({ ok: false, why: "room" });
    expect(at(7, 1920, 1080)).toEqual({ ok: true, why: "" });
    expect(at(8, 1920, 1080)).toEqual({ ok: false, why: "max" });
    // A window already dropping panes can still split when the next shape fits:
    // three columns drawn as two at 900 can become 2×2 (720 wide).
    const three = setPreset(defaultWorkspace(), "cols3");
    expect(fitWorkspace(DEFAULT_LAYOUT, three, 900, 700).dropped).toBe(1);
    expect(splitVerdict(DEFAULT_LAYOUT, three, 900, 700)).toEqual({ ok: true, why: "" });
    // Every free id hidden with an unsaved edit.
    const one = setPreset(setPreset(defaultWorkspace(), "grid8"), "single");
    expect(splitVerdict(DEFAULT_LAYOUT, one, 1920, 1000, null, false, [2, 3, 4, 5, 6, 7, 8]))
      .toEqual({ ok: false, why: "hidden-edit" });
    expect(splitVerdict(DEFAULT_LAYOUT, one, 1920, 1000, null, false, [2])).toEqual({ ok: true, why: "" });
  });

  it("says why on the button", () => {
    expect(splitTitle("", 2)).toBe("Split: a copy of this pane beside it");
    expect(splitTitle("max", 8)).toBe("Eight panes is the most");
    expect(splitTitle("room", 6)).toBe("No room for 7 panes in this window: widen it, zoom out (Ctrl+-) or close a pane");
    expect(splitTitle("hidden-edit", 1)).toBe("A hidden pane has an unsaved edit: show it from ⊞ first");
  });

  it("remembers the file a File or Diff pane has open, and forgets it when the pane moves project", () => {
    let w = setFilePath(pinPane(defaultWorkspace(), 3, "p1"), 3, "src/a.ts");
    expect(w.panes[2]).toMatchObject({ projectId: "p1", filePath: "src/a.ts" });
    expect(setFilePath(w, 3, "src/a.ts")).toBe(w);
    expect(setFilePath(w, 3, "x".repeat(PATH_MAX + 1)).panes[2].filePath).toBeUndefined();
    expect(pinPane(w, 3, "p2").panes[2]).toEqual({ view: "file", projectId: "p2", terminalId: null });
    expect(unpinProject(w, "p1").panes[2].filePath).toBeUndefined();
    expect(unpinProject(w, "p1", [3]).panes[2].filePath).toBe("src/a.ts"); // an unsaved edit keeps its pin and file
    w = setFilePath(w, 3, null);
    expect("filePath" in w.panes[2]).toBe(false);
  });
});

describe("fitting: the mutants WP-G1's review left alive (U4, U34, U35)", () => {
  it("U4: clamps the bottom row's own edges like the top row's", () => {
    // A stored 3 over 2 whose bottom edge sits 5% in: at 1100 wide that pane would be 55px.
    const w = setSplit(setPreset(defaultWorkspace(), "grid5"), "grid5", { bottom: [0.05] });
    const f = fitWorkspace({ ...DEFAULT_LAYOUT, leftCollapsed: true, rightCollapsed: true }, w, 1172, 900);
    expect(f.drawn).toBe("grid5");
    expect(f.bottom[0] * f.width).toBeGreaterThanOrEqual(PANE_MIN_W - 1e-6);
    expect(slotWidths(f).every((x) => x >= PANE_MIN_W - 1e-6)).toBe(true);
  });

  it("U34: a row the preferred panel dropped counts for the sticky set", () => {
    // 3×2 at 900 x 450 with the panel preferred: a column goes (2×2), then a
    // row for the panel (two columns). Focus on pane 3 draws [2, 3]; moving
    // focus to pane 2, inside that pair, keeps [2, 3] — not [1, 2].
    const w = setPanel(focusPane(setPreset(defaultWorkspace(), "grid6"), 3), { open: true });
    const f1 = fitWorkspace(DEFAULT_LAYOUT, w, 900, 450, null, true);
    expect(f1).toMatchObject({ drawn: "cols2", panes: [2, 3] });
    expect(f1.panel.open).toBe(true);
    const prev: DrawnSet = { preset: w.preset, drawn: f1.drawn, panes: f1.panes };
    const f2 = fitWorkspace(DEFAULT_LAYOUT, focusPane(w, 2), 900, 450, null, true, prev);
    expect(f2.panes).toEqual([2, 3]);
  });

  it("U35: a dropped row that needs less width gives the side panes back their room", () => {
    // 3 over 2 needs 1080 across: at 1366 that folds the status pane. Too
    // short for two rows with focus in the bottom row, it is two columns
    // (720), and the status pane comes back.
    const w = focusPane(setPreset(defaultWorkspace(), "grid5"), 5);
    expect(fitWorkspace(DEFAULT_LAYOUT, w, 1366, 900).sides.rightFolded).toBe(true);
    const f = fitWorkspace(DEFAULT_LAYOUT, w, 1366, 300);
    expect(f).toMatchObject({ drawn: "cols2", panes: [4, 5] });
    expect(f.sides).toMatchObject({ leftFolded: false, rightFolded: false });
  });
});
