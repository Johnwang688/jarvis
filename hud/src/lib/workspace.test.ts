import { describe, expect, it } from "vitest";
import { DEFAULT_LAYOUT, PANE, RAIL } from "./layout";
import {
  MENU_PRESETS, PANEL, PANE_MIN_H, PANE_MIN_W, PANE_NOS, PANE_VIEWS, PATH_MAX, PRESETS, SHAPES,
  TITLEBAR_H, WORKSPACE_KEY, allocate, clampPanelHeight, closePane, copySpec,
  defaultPane, defaultWorkspace, drawnSet, setFilePath, splitPane,
  fitWorkspace, focusPane, forgetTerminal, loadWorkspace, maxPanelHeight,
  inWorkspace, orderOf, paneAt, paneSequence, parseWorkspace, pinPane, presetOfWs, resetWorkspace, saveWorkspace, setKeepOrigin, setPaneTerminal,
  setBrowserUrl, setEditors, setPanel, setPreset, setSubagent, setView, show, slotOf, subagentSpec,
  unpinProject, VIEW_LABEL,
  type DrawnSet, type PaneNo, type Preset, type Workspace,
} from "./workspace";
import { isLeaf, leaves, presetTree, shapeKey, type LayoutNode, type SplitNode } from "./splittree";
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

// FS3: the order and the preset are the tree's (orderOf / presetOfWs). A
// workspace made "with a preset" is that preset over ids 1…n, as the old
// hand-made `{ ...defaultWorkspace(), preset }` was read (orderOf grew it).
const ws = ({ preset, ...over }: Partial<Workspace> & { preset?: Preset } = {}): Workspace =>
  ({ ...setPreset(defaultWorkspace(), preset ?? "single"), ...over });
/** The tree's sizes, ids left out: what v1's `splits` was for the stored preset. */
const sizesOf = (n: LayoutNode): unknown => (isLeaf(n) ? null : { axis: n.axis, sizes: n.sizes, children: n.children.map(sizesOf) });
/** The window's room the way useLayout measures it: inner size over the zoom, the title bar off the height. */
const room = (w: number, h: number, zoom: number) => [w / (zoom / 100), h / (zoom / 100) - TITLEBAR_H] as const;
/** No measured window: nothing dropped, FS-4 skipped — a split is decided by FS-3 alone (FS4). */
const unmeasured = (w: Workspace) => fitWorkspace(DEFAULT_LAYOUT, w, NaN, NaN);
const fitAt = (w: Workspace, px: number, py: number, zoom: number, preferPanel = false) => {
  const [aw, ah] = room(px, py, zoom);
  return fitWorkspace(DEFAULT_LAYOUT, w, aw, ah, null, preferPanel);
};

describe("the preset table", () => {
  it("has the plans' ten shapes with their minimums (WP-A's six, then WP-G1's four)", () => {
    expect(PRESETS).toEqual(["single", "cols2", "rows2", "cols3", "main2", "grid4", "grid5", "grid6", "grid7", "grid8"]);
    // FS3: SHAPES keeps the pane count; the minimums and rows come from the tree
    // (workspace.tree.test.ts, "§7.1 (i): main2's minimum is 720, read from the fit").
    const table: Record<Preset, number> = {
      single: 1, cols2: 2, rows2: 2, cols3: 3, main2: 3, grid4: 4, grid5: 5, grid6: 6, grid7: 7, grid8: 8,
    };
    for (const p of PRESETS) {
      expect(SHAPES[p].panes).toEqual(table[p]);
    }
    expect([PANE_MIN_W, PANE_MIN_H]).toEqual([360, 200]);
    expect([PANEL.min, PANEL.def]).toEqual([120, 260]);
    // Twelve ids (decision FS-1), all of them drawn at once since FS4 (no MAX_DRAWN: workspace.split.test.ts).
    expect(PANE_NOS).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]);
  });

  it("offers eight tiles in ⊞ — 6 and 8 directly, 5 and 7 only by split or close (decisions S-1)", () => {
    expect(MENU_PRESETS).toEqual(["single", "cols2", "rows2", "cols3", "main2", "grid4", "grid6", "grid8"]);
    // FS4: the ladder a split climbed is gone (decisions FS-1..FS-5; workspace.split.test.ts, "the ladder is gone").
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
      expect(presetOfWs(parseWorkspace(JSON.stringify({ preset: p })))).toBe("single");
    }
    expect(presetOfWs(parseWorkspace(JSON.stringify({ preset: "grid4" })))).toBe("grid4");
  });

  it("parses each pane on its own, a bad one falling back to that pane's default", () => {
    const w = parseWorkspace(JSON.stringify({
      panes: [{ view: "file", projectId: "p2" }, { view: "nonsense" }, "junk", { view: "diff", browserUrl: 5 }],
    }));
    expect(w.panes.map((p) => p.view))
      .toEqual(["file", "browser", "file", "diff", "chat", "chat", "chat", "chat", "chat", "chat", "chat", "chat"]);
    expect(w.panes[0].projectId).toBe("p2");
    expect(w.panes[1].projectId).toBeNull();
    expect(w.panes[3].browserUrl).toBeUndefined();
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
      panes: [{ projectId: 7 }, { projectId: "x".repeat(500) }, { browserUrl: "u".repeat(3000) }, { terminalId: {} }],
    }));
    expect(w.panes.map((p) => p.projectId).slice(0, 4)).toEqual([null, null, null, null]);
    expect(w.panes[2].browserUrl).toBeUndefined();
    expect(w.panes[3].terminalId).toBeNull();
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
    expect(presetOfWs(back)).toBe("cols3");
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
      .toEqual(["chat", "browser", "file", "chat"]);
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
    w = focusPane(w, 2); // pane 2 shows the Browser, pane 1 the chat
    const after = show(w, "chat", [1, 2]);
    expect(after.panes[1].view).toBe("browser");
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
    let w = setBrowserUrl(defaultWorkspace(), 2, "http://localhost:5173/");
    w = pinPane(w, 2, "p2");
    w = { ...w, panes: w.panes.map((p, i) => (i === 1 ? { ...p, terminalId: "term-b" } : p)) as Workspace["panes"] };
    const after = setView(w, 2, "chat");
    expect(after.panes[1]).toEqual(
      { ...w.panes[1], view: "chat", projectId: "p2", terminalId: "term-b", browserUrl: "http://localhost:5173/" });
    expect(after.panes[0]).toBe(w.panes[0]);
    expect(after.focused).toBe(2);
    expect(setView(after, 2, "browser").panes[1].browserUrl).toBe("http://localhost:5173/");
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

  it("keeps a Browser URL per pane", () => {
    let w = setBrowserUrl(defaultWorkspace(), 2, "http://localhost:5173/");
    w = setBrowserUrl(w, 3, "http://127.0.0.1:8403/p/p1/index.html");
    expect(w.panes.map((p) => p.browserUrl).slice(0, 4)).toEqual(
      [undefined, "http://localhost:5173/", "http://127.0.0.1:8403/p/p1/index.html", undefined]);
    expect(setBrowserUrl(w, 2, "").panes[1].browserUrl).toBeUndefined();
  });

  it("keeps a pane's keep-origin grant only for the origin it was given for (WP-E)", () => {
    let w = setBrowserUrl(defaultWorkspace(), 2, "http://localhost:5173/");
    w = setKeepOrigin(w, 2, "http://localhost:5173");
    expect(w.panes[1].keepOrigin).toBe("http://localhost:5173");
    // Another page on the same origin keeps it.
    expect(setBrowserUrl(w, 2, "http://localhost:5173/settings").panes[1].keepOrigin).toBe("http://localhost:5173");
    // Another port, host or scheme turns it off.
    for (const url of ["http://localhost:5174/", "http://127.0.0.1:5173/", "https://localhost:5173/", ""]) {
      expect(setBrowserUrl(w, 2, url).panes[1].keepOrigin, url).toBeUndefined();
    }
    // Survives a reload, judged again by the parser.
    expect(parseWorkspace(JSON.stringify(w)).panes[1].keepOrigin).toBe("http://localhost:5173");
    // Off.
    expect(setKeepOrigin(w, 2, null).panes[1].keepOrigin).toBeUndefined();
    expect(setKeepOrigin(setKeepOrigin(w, 2, null), 2, null)).toEqual(setKeepOrigin(w, 2, null));
  });

  it("never grants keep-origin to another origin than the pane's URL, nor to a daemon port", () => {
    const w = setBrowserUrl(defaultWorkspace(), 2, "http://localhost:5173/");
    expect(setKeepOrigin(w, 2, "http://localhost:5174")).toBe(w);
    expect(setKeepOrigin(w, 3, "http://localhost:5173")).toBe(w);           // pane 3 holds no URL
    const hud = setBrowserUrl(defaultWorkspace(), 2, "http://localhost:8402/");
    expect(setKeepOrigin(hud, 2, "http://localhost:8402")).toBe(hud);
    const shop = setBrowserUrl(defaultWorkspace(), 2, "http://127.0.0.1:8403/p/p1/index.html");
    expect(setKeepOrigin(shop, 2, "http://127.0.0.1:8403")).toBe(shop);
  });

  it("drops a stored keep-origin that is not allowed when it is read back", () => {
    const stored = (pane: Record<string, unknown>) =>
      parseWorkspace(JSON.stringify({ panes: [{}, { view: "browser", ...pane }] })).panes[1].keepOrigin;
    expect(stored({ browserUrl: "http://localhost:5173/", keepOrigin: "http://localhost:5173" }))
      .toBe("http://localhost:5173");
    expect(stored({ browserUrl: "http://localhost:5174/", keepOrigin: "http://localhost:5173" })).toBeUndefined();
    expect(stored({ browserUrl: "http://localhost:8402/", keepOrigin: "http://localhost:8402" })).toBeUndefined();
    expect(stored({ browserUrl: "http://localhost:8405/", keepOrigin: "http://localhost:8405" })).toBeUndefined();
    expect(stored({ browserUrl: "https://example.com/", keepOrigin: "https://example.com" })).toBeUndefined();
    expect(stored({ keepOrigin: "http://localhost:5173" })).toBeUndefined();
    expect(stored({ browserUrl: "http://localhost:5173/", keepOrigin: true })).toBeUndefined();
  });
});

describe("the rename: Preview is the Browser, File reads as the editor (BR-3a)", () => {
  it("reads a stored Preview pane as the Browser, its URL and all", () => {
    const w = parseWorkspace(JSON.stringify({
      preset: "cols2", order: [1, 2], kept: [],
      panes: [{ view: "chat" }, { view: "preview", previewUrl: "http://localhost:5173/app", projectId: "p2" }],
    }));
    expect(w.panes[1]).toEqual({
      view: "browser", projectId: "p2", terminalId: null, browserUrl: "http://localhost:5173/app",
      browser: { tabs: [{ id: "legacy", url: "http://localhost:5173/app" }], active: "legacy" } });
    expect(PANE_VIEWS as readonly string[]).not.toContain("preview");
  });

  it("migrates a WP-A value too (no order, no kept), its hidden Preview pane included", () => {
    const old = JSON.stringify({
      preset: "single",
      panes: [{ view: "chat" }, { view: "preview", previewUrl: "http://localhost:5173/" }, { view: "file" }, { view: "task" }],
    });
    const w = parseWorkspace(old);
    expect(w.kept).toEqual([2, 3, 4]);
    expect(w.panes[1].view).toBe("browser");
    expect(w.panes[1].browserUrl).toBe("http://localhost:5173/");
  });

  it("keeps an old keep-origin grant only while it matches the URL it is read with", () => {
    const pane2 = (pane: Record<string, unknown>) =>
      parseWorkspace(JSON.stringify({ panes: [{}, { view: "preview", ...pane }] })).panes[1];
    expect(pane2({ previewUrl: "http://localhost:5173/x", keepOrigin: "http://localhost:5173" }).keepOrigin)
      .toBe("http://localhost:5173");
    // Another port, host or scheme: the grant was never for this URL.
    for (const keepOrigin of ["http://localhost:5174", "http://127.0.0.1:5173", "https://localhost:5173"]) {
      expect(pane2({ previewUrl: "http://localhost:5173/x", keepOrigin }).keepOrigin, keepOrigin).toBeUndefined();
    }
    // Both names stored: the new one is the URL, and the grant is judged against it.
    const both = pane2({
      browserUrl: "http://localhost:4000/", previewUrl: "http://localhost:5173/", keepOrigin: "http://localhost:5173" });
    expect(both.browserUrl).toBe("http://localhost:4000/");
    expect(both.keepOrigin).toBeUndefined();
    // A new name that does not parse leaves the old one to be read.
    expect(pane2({ browserUrl: "u".repeat(3000), previewUrl: "http://localhost:5173/" }).browserUrl)
      .toBe("http://localhost:5173/");
  });

  it("saves only the new names", () => {
    const mem = memory({
      [WORKSPACE_KEY]: JSON.stringify({
        preset: "cols2", order: [1, 2], kept: [],
        panes: [{ view: "chat" }, { view: "preview", previewUrl: "http://localhost:5173/", keepOrigin: "http://localhost:5173" }],
      }),
    });
    saveWorkspace(loadWorkspace(mem), mem);
    const text = mem.map.get(WORKSPACE_KEY)!;
    expect(text).not.toContain("preview");
    const saved = JSON.parse(text);
    expect(saved.panes[1]).toMatchObject({
      view: "browser", browserUrl: "http://localhost:5173/", keepOrigin: "http://localhost:5173" });
    expect(saved.panes[1].previewUrl).toBeUndefined();
    expect(loadWorkspace(mem).panes[1].browserUrl).toBe("http://localhost:5173/");
  });

  it("leaves a stored File pane the editor, with its tabs (the view id stays \"file\")", () => {
    const tabs = { paths: ["src/a.py", "b.md"], active: "b.md", preview: "b.md" };
    const mem = memory({
      [WORKSPACE_KEY]: JSON.stringify({
        preset: "cols2", order: [1, 3], kept: [],
        panes: [{ view: "chat" }, {}, { view: "file", projectId: "p1", editors: tabs }],
      }),
    });
    const w = loadWorkspace(mem);
    expect(w.panes[2]).toMatchObject({ view: "file", projectId: "p1", editors: tabs });
    saveWorkspace(w, mem);
    expect(JSON.parse(mem.map.get(WORKSPACE_KEY)!).panes[2]).toMatchObject({ view: "file", editors: tabs });
    expect(VIEW_LABEL[w.panes[2].view]).toBe("editor");
  });

  it("names every view the owner can pick, and no view by its old name", () => {
    expect(Object.keys(VIEW_LABEL).sort()).toEqual([...PANE_VIEWS].sort());
    expect(VIEW_LABEL).toEqual({
      chat: "chat", task: "task", file: "editor", diff: "diff", browser: "browser", terminal: "terminal", vex: "vex",
      subagent: "sub-agent", inbox: "inbox", review: "morning review" });
    const labels = Object.values(VIEW_LABEL);
    expect(new Set(labels).size).toBe(labels.length);
    expect(labels).not.toContain("file");
    expect(labels).not.toContain("preview");
  });

  it("the inbox view (inbox A2): stored and read back, shown by `show`, and a split beside it is a chat", () => {
    const mem = memory();
    let w = setView(defaultWorkspace(), 1, "inbox");
    expect(w.panes[0].view).toBe("inbox");
    saveWorkspace(w, mem);
    expect(loadWorkspace(mem).panes[0].view).toBe("inbox");
    expect(copySpec(w.panes[0]).view).toBe("chat");
    // The sidebar's row: a drawn inbox pane is focused, else the focused pane shows it.
    w = setView(defaultWorkspace(), 1, "chat");
    expect(show(w, "inbox", [1]).panes[0].view).toBe("inbox");
    const two = setView(setPreset(defaultWorkspace(), "cols2"), 2, "inbox");
    const shown = show(focusPane(two, 1), "inbox", orderOf(two));
    expect(shown.focused).toBe(2);
    expect(shown.panes[0].view).toBe(two.panes[0].view);
  });

  it("resets the layout without forgetting what each pane showed", () => {
    let w = setView(setPreset(defaultWorkspace(), "grid4"), 3, "diff");
    w = { ...w, tree: presetTree("grid4", orderOf(w), { cols: [0.3], row: 0.7 }) };
    w = setPanel(w, { open: true, height: 400 });
    const r = resetWorkspace(focusPane(w, 3));
    expect(presetOfWs(r)).toBe("single");
    expect(r.focused).toBe(1);
    expect(r.panel).toEqual({ open: false, height: PANEL.def });
    expect(setPreset(r, "grid4").tree).toEqual(presetTree("grid4", [1, 2, 3, 4]));
    expect(r.panes[2].view).toBe("diff");
  });

});

describe("fitting: the four worked windows (plan §2.2)", () => {
  const preset = (p: Preset, panel = false) => ws({ preset: p, panel: { open: panel, height: PANEL.def } });

  it("1920×1080 at 100%: everything fits, side panes open", () => {
    for (const p of ["cols2", "cols3"] as Preset[]) {
      const f = fitAt(preset(p), 1920, 1080, 100);
      expect(f.drawnPreset).toBe(p);
      expect(f.sides).toMatchObject({ leftFolded: false, rightFolded: false, left: 236, right: 316 });
    }
    const g = fitAt(preset("grid4", true), 1920, 1080, 100);
    expect(g.drawnPreset).toBe("grid4");
    expect(g.panel).toEqual({ open: true, height: 260, auto: false });
  });

  it("1366×768 at 100%: two fit, main2 shrinks the side panes a little, three folds the status pane", () => {
    expect(fitAt(preset("cols2"), 1366, 768, 100).sides).toMatchObject({ left: 236, right: 316 });
    // FS3, §7.1 (i): main2's minimum is 720, so 1366 no longer squeezes the side
    // panes for it (workspace.tree.test.ts, the changed golden key
    // "main2/1/-/-/1366x768@100").
    const c = fitAt(preset("cols3"), 1366, 768, 100);
    expect(c.drawnPreset).toBe("cols3");
    expect(c.sides).toMatchObject({ rightFolded: true, autoRight: true, leftFolded: false });
    const g = fitAt(preset("grid4", true), 1366, 768, 100);
    expect(g.drawnPreset).toBe("grid4");
    expect(g.panel.open).toBe(true);
  });

  it("1280×800 at 150%: two columns fold both side panes; three drop to two; the panel folds for 2×2", () => {
    const c2 = fitAt(preset("cols2"), 1280, 800, 150);
    expect(c2.drawnPreset).toBe("cols2");
    expect(c2.sides).toMatchObject({ leftFolded: true, rightFolded: true, autoLeft: true, autoRight: true });
    const c3 = fitAt(preset("cols3"), 1280, 800, 150);
    expect(c3).toMatchObject({ drawnPreset: "cols2", dropped: 1, why: "narrow" });
    expect(c3.panes).toEqual([1, 2]);
    // 400 + 120 > 505: the panel folds for the render, the four panes stay.
    const g = fitAt(preset("grid4", true), 1280, 800, 150);
    expect(g.drawnPreset).toBe("grid4");
    expect(g.panel).toEqual({ open: false, height: 0, auto: true });
  });

  it("1024×700 at 160%: one pane for two or three columns", () => {
    expect(fitAt(preset("cols2"), 1024, 700, 160)).toMatchObject({ drawnPreset: "single", dropped: 1 });
    expect(fitAt(preset("cols3"), 1024, 700, 160)).toMatchObject({ drawnPreset: "single", dropped: 2 });
    // 2×2 with the panel: the column goes first (the focused one is kept),
    // then the panel folds before any row does — the plan's stated order.
    // (The plan's table cell says "1 pane, panel at its minimum"; that is
    // what opening the panel by hand gives, below.)
    const g = fitAt(preset("grid4", true), 1024, 700, 160);
    expect(g).toMatchObject({ drawnPreset: "rows2", panes: [1, 3] });
    expect(g.panel.auto).toBe(true);
    const opened = fitAt(preset("grid4", true), 1024, 700, 160, true);
    expect(opened.drawnPreset).toBe("single");
    expect(opened.panel.open).toBe(true);
    expect(opened.panel.height).toBeGreaterThanOrEqual(PANEL.min);
  });
});

describe("fitting: what is dropped, and what never is", () => {
  it("drops columns in order: three, two, one", () => {
    const w = ws({ preset: "cols3" });
    // 1000: 36 + 36 + 1080 does not fit, 720 does. 792 is the last width two
    // columns fit beside both rails; 780 is one pane.
    const order = [1400, 1000, 792, 780, 500].map((px) => fitAt(w, px, 900, 100).drawnPreset);
    expect(order).toEqual(["cols3", "cols2", "cols2", "single", "single"]);
  });

  it("never drops the focused pane, whatever the preset", () => {
    for (const p of PRESETS) {
      for (const n of orderOf(setPreset(defaultWorkspace(), p))) {
        const w = focusPane(ws({ preset: p, panel: { open: true, height: 260 } }), n);
        for (const [px, py] of [[600, 400], [900, 500], [1100, 700], [640, 437]] as [number, number][]) {
          const f = fitAt(w, px, py, 100);
          expect(f.panes, `${p} focused ${n} at ${px}x${py}`).toContain(n);
        }
      }
    }
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
    expect(f).toMatchObject({ drawnPreset: "grid4", dropped: 0, panel: { open: true, height: 300, auto: false } });
  });
});

describe("fitting: a dropped set is sticky while focus moves inside it", () => {
  // 1280×800 at 150%: three columns draw as two.
  const [aw, ah] = room(1280, 800, 150);
  const fit = (w: Workspace, previous: DrawnSet | null) =>
    fitWorkspace(DEFAULT_LAYOUT, w, aw, ah, null, false, previous);
  const drawn = (w: Workspace, f: ReturnType<typeof fit>): DrawnSet => drawnSet(w, f);
  /** The DrawnSet a render of preset `p` (ids 1…n) drawn as preset `d` over `panes` recorded. */
  const set = (p: Preset, d: Preset, panes: PaneNo[]): DrawnSet => ({
    shape: shapeKey(presetTree(p, PANE_NOS.slice(0, SHAPES[p].panes))), drawn: shapeKey(presetTree(d, panes)), panes });

  it("keeps the drawn pair when focus moves to the other pane in it", () => {
    const at3 = focusPane(ws({ preset: "cols3" }), 3);
    const first = fit(at3, null);
    expect(first).toMatchObject({ drawnPreset: "cols2", panes: [2, 3] });
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
    expect(f3).toMatchObject({ drawnPreset: "cols2", panes: [1, 3] });
    const w1 = focusPane(w3, 1);
    expect(fitWorkspace(DEFAULT_LAYOUT, w1, 1600, 300, null, false, drawn(w3, f3)).panes).toEqual([1, 3]);
    expect(fitWorkspace(DEFAULT_LAYOUT, w1, 1600, 300, null, false, null).panes).toEqual([1, 2]);
    const w2 = focusPane(w3, 2);
    expect(fitWorkspace(DEFAULT_LAYOUT, w2, 1600, 300, null, false, drawn(w3, f3)).panes).toEqual([1, 2]);
  });

  it("is not sticky across a different preset or a different drawn shape", () => {
    const at3 = focusPane(ws({ preset: "cols3" }), 3);
    const stale: DrawnSet = set("grid4", "cols2", [3, 4]);
    expect(fit(at3, stale).panes).toEqual([2, 3]);
    // The window grew: three columns fit again, all three are drawn.
    const wide = fitWorkspace(DEFAULT_LAYOUT, focusPane(at3, 2), 1920, 1000, null, false,
                              set("cols3", "cols2", [2, 3]));
    expect(wide).toMatchObject({ drawnPreset: "cols3", panes: [1, 2, 3] });
  });

  it("only reads what it is given", () => {
    const prev: DrawnSet = set("cols3", "cols2", [2, 3]);
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
    expect(f).toMatchObject({ drawnPreset: "rows2", panel: { open: true, height: 150, auto: false } });
  });

  it("then folds it for the render, before any row is dropped", () => {
    const f = tall(400 + 100);
    expect(f.drawnPreset).toBe("rows2");
    expect(f.panel).toEqual({ open: false, height: 0, auto: true });
  });

  it("and only then drops rows", () => {
    const f = tall(300);
    expect(f).toMatchObject({ drawnPreset: "single", why: "short", panel: { auto: true } });
  });

  it("opening a panel the window folded drops rows instead, and never folds it", () => {
    const f = tall(400 + 100, true);
    expect(f.drawnPreset).toBe("single");
    expect(f.panel).toEqual({ open: true, height: 260, auto: false });
    const tiny = tall(150, true);
    expect(tiny.panel).toEqual({ open: true, height: PANEL.min, auto: false });
  });

  it("gives back side-pane room a dropped row frees", () => {
    // main2 needs 840 across; at 1000 that folds the status pane. Too short
    // for its stacked column, it becomes two columns (720), which fit beside
    // the status pane again.
    const f = fitWorkspace(DEFAULT_LAYOUT, ws({ preset: "main2" }), 1000, 300);
    expect(f.drawnPreset).toBe("cols2");
    expect(f.sides.rightFolded).toBe(true); // 236 + 316 + 720 > 1000 still
    const g = fitWorkspace(DEFAULT_LAYOUT, ws({ preset: "main2" }), 1300, 300);
    expect(g.drawnPreset).toBe("cols2");
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
    expect(two.sides.left + two.sides.right + two.minW).toBeLessThanOrEqual(1280);
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
  // generated by running that code. FS3: the same stored values go through the
  // tree's parse and fit in workspace.tree.test.ts ("draws every golden fit as
  // WP-B did, except the 62 keys §7.1 (i)–(ii) change").
  const entries = Object.entries(LEGACY_FITS as Record<string, unknown[]>);

  it("covers every WP-B shape", () => {
    expect(entries).toHaveLength(300);
    expect(new Set(entries.map(([k]) => k.split("/")[0]))).toEqual(new Set(Object.keys(LEGACY_CUSTOM)));
  });

  it("reads as ids 1…n, the stored panes it did not draw kept, the other specs as stored", () => {
    for (const p of Object.keys(LEGACY_CUSTOM) as Preset[]) {
      const w = parseWorkspace(JSON.stringify({ preset: p, panes: [{ view: "diff" }, { view: "file" }] }));
      expect(orderOf(w), p).toEqual(PANE_NOS.slice(0, SHAPES[p].panes));
      // Two panes stored: pane 2 is kept when the preset does not draw it (review of PR #47).
      expect(w.kept, p).toEqual(SHAPES[p].panes < 2 ? [2] : []);
      expect(w.panes.map((x) => x.view), p)
        .toEqual(["diff", "file", "file", "task", "chat", "chat", "chat", "chat", "chat", "chat", "chat", "chat"]);
    }
    // ⊞ back to four from a stored one pane brings back what WP-A kept there.
    const one = parseWorkspace(JSON.stringify({
      preset: "single", panes: [{ view: "chat" }, { view: "diff" }, { view: "terminal", terminalId: "t-1" }, { view: "file" }],
    }));
    const four = setPreset(one, "grid4");
    expect(orderOf(four)).toEqual([1, 2, 3, 4]);
    expect(four.panes.slice(0, 4).map((x) => x.view)).toEqual(["chat", "diff", "terminal", "file"]);
    // A split never writes over them (review of PR #47, L4): they are kept, so it takes a free id.
    expect(one.kept).toEqual([2, 3, 4]);
    expect(allocate(one)).toBe(5);
    const two = splitPane(one, unmeasured(one), 1, "x", allocate(one)!, { view: "chat", projectId: null, terminalId: null });
    expect(orderOf(two)).toEqual([1, 5]);
    expect(two.panes.slice(1, 4).map((x) => x.view)).toEqual(["diff", "terminal", "file"]);
    expect(two.panes[2].terminalId).toBe("t-1");
    // A value that says what it keeps is read as it says, and so is one with only a bad order.
    expect(parseWorkspace(JSON.stringify({ preset: "single", order: [1], kept: [], panes: one.panes })).kept).toEqual([]);
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
    expect(presetOfWs(base)).toBe("grid5");
    expect(orderOf(base)).toEqual([1, 5, 2, 3, 4]);
    expect(base.kept).toEqual([6]);
    expect(base.focused).toBe(5);
    // (Its edges as a tree: workspace.tree.test.ts, "places a stored order's ids in reading order, a hidden pane kept".)
    expect(base.panes[0].filePath).toBe("src/app.ts");
    expect(base.panes[2].filePath).toBe("README.md");
    expect(base.panes[1].filePath).toBeUndefined();
    expect(base.panes).toHaveLength(12);
  });

  const variants: [string, Record<string, unknown>, (w: Workspace) => void][] = [
    ["a repeated id in order", { order: [1, 1, 2, 3, 4] }, (w) => expect(orderOf(w)).toEqual([1, 2, 3, 4, 5])],
    ["too few ids in order", { order: [1, 5, 2] }, (w) => expect(orderOf(w)).toEqual([1, 2, 3, 4, 5])],
    ["an id out of range in order", { order: [1, 5, 2, 3, 13] }, (w) => expect(orderOf(w)).toEqual([1, 2, 3, 4, 5])],
    ["order not a list", { order: "1,5,2,3,4" }, (w) => expect(orderOf(w)).toEqual([1, 2, 3, 4, 5])],
    ["kept not a list", { kept: 6 }, (w) => expect(w.kept).toEqual([])],
    ["kept holding junk, repeats and drawn ids", { kept: [13, 2, "7", 6, 6, 0, 7] }, (w) => expect(w.kept).toEqual([6, 7])],
    ["a bottom edge of the wrong count", { splits: { ...good.splits, grid5: { cols: [0.3, 0.6], bottom: [0.4, 0.8], row: 0.55 } } },
      (w) => expect(((w.tree as SplitNode).children[1] as SplitNode).sizes).toEqual([0.5, 0.5])],
    ["a bottom edge outside (0, 1)", { splits: { ...good.splits, grid5: { cols: [0.3, 0.6], bottom: [1.4], row: 0.55 } } },
      (w) => expect(((w.tree as SplitNode).children[1] as SplitNode).sizes).toEqual([0.5, 0.5])],
    ["filePath not a string", { panes: [{ view: "file", filePath: 7 }, { view: "chat" }, { view: "diff", filePath: "README.md" }] },
      (w) => expect(w.panes[0].filePath).toBeUndefined()],
    ["filePath too long", { panes: [{ view: "file", filePath: "a".repeat(PATH_MAX + 1) }, { view: "chat" }, { view: "diff", filePath: "README.md" }] },
      (w) => expect(w.panes[0].filePath).toBeUndefined()],
  ];

  for (const [what, bad, only] of variants) {
    it(`falls back on ${what}, and only there`, () => {
      const w = parseWorkspace(JSON.stringify({ ...good, ...bad }));
      only(w);
      // Every other field is what the good value gave (FS3: the preset and the
      // order read from the tree, and the edges as the tree's sizes).
      const field = Object.keys(bad)[0];
      const read: Record<string, (x: Workspace) => unknown> = {
        preset: presetOfWs, order: orderOf, kept: (x) => x.kept, focused: (x) => x.focused, panel: (x) => x.panel,
        splits: (x) => sizesOf(x.tree), panes: (x) => x.panes,
      };
      for (const k of Object.keys(read)) {
        if (k === field) continue;
        expect(read[k](w), `${what}: ${k}`).toEqual(read[k](base));
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
    expect([presetOfWs(w), orderOf(w), w.focused]).toEqual(["single", [1], 1]);
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
    const views = ["chat", "browser", "file", "task", "diff", "terminal", "chat", "file"] as const;
    views.forEach((v, i) => (w = setView(w, (i + 1) as PaneNo, v)));
    return pinPane(setBrowserUrl(w, 2, "http://localhost:5173/"), 8, "p2");
  };

  it("8 → 1 → 8 brings back every pane, in its place, with what it showed", () => {
    const w = eight();
    expect(orderOf(w)).toEqual([1, 2, 3, 4, 5, 6, 7, 8]);
    const one = setPreset(w, "single");
    expect([orderOf(one), one.kept]).toEqual([[1], [2, 3, 4, 5, 6, 7, 8]]);
    const back = setPreset(one, "grid8");
    expect([orderOf(back), back.kept]).toEqual([[1, 2, 3, 4, 5, 6, 7, 8], []]);
    expect(back.panes).toEqual(w.panes);
  });

  it("takes back the hidden panes first, most recently hidden first, then free ids", () => {
    let w = setPreset(defaultWorkspace(), "grid4");      // [1 2 3 4]
    w = setPreset(w, "cols2");                           // [1 2], 3 4 hidden
    w = setPreset(w, "single");                          // [1], 2 3 4 hidden
    expect(w.kept).toEqual([2, 3, 4]);
    expect(orderOf(setPreset(w, "cols2"))).toEqual([1, 2]); // pane 2 comes back to slot 2, not a new pane
    expect(orderOf(setPreset(w, "grid6"))).toEqual([1, 2, 3, 4, 5, 6]);
    // A stored order: the hidden one comes back before a free one.
    const stored = parseWorkspace(JSON.stringify({ preset: "cols3", order: [1, 5, 2] }));
    const one = setPreset(stored, "single");
    expect([orderOf(one), one.kept]).toEqual([[1], [5, 2]]);
    expect(orderOf(setPreset(one, "grid4"))).toEqual([1, 5, 2, 3]);
  });

  it("the same count changes only the shape", () => {
    const w = setPreset(parseWorkspace(JSON.stringify({ preset: "cols3", order: [3, 1, 2], kept: [7] })), "main2");
    expect([presetOfWs(w), orderOf(w), w.kept]).toEqual(["main2", [3, 1, 2], [7]]);
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
    expect([presetOfWs(r), orderOf(r), r.kept, r.focused]).toEqual(["single", [5], [1, 2], 5]);
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
    expect(orderOf(setPreset(w, "grid4"))[3]).toBe(7);
    expect(paneSequence(w)).toEqual([1, 5, 2, 7, 3, 4, 6, 8, 9, 10, 11, 12]);
    // Drawn or hidden is in the workspace; a free id (closed, or never drawn) is not.
    expect([1, 7, 3].map((n) => inWorkspace(w, n as PaneNo))).toEqual([true, true, false]);
  });

  it("is the identity for every WP-A and WP-B workspace", () => {
    for (const p of PRESETS) {
      const x = setPreset(defaultWorkspace(), p);
      for (const n of orderOf(x)) expect(slotOf(x, n)).toBe(n);
    }
  });

});

describe("fitting eight panes: the generic drop (§3.3)", () => {
  it("never drops the focused pane: every shape × every focused pane × widths 400–2000 × four heights", () => {
    const orders: PaneNo[][] = [[1, 2, 3, 4, 5, 6, 7, 8], [8, 3, 6, 1, 7, 2, 5, 4]];
    for (const p of PRESETS) {
      const n = SHAPES[p].panes;
      for (const base of orders) {
        const order = base.filter((x) => x <= n || base.indexOf(x) < n).slice(0, n);
        for (const f of order) {
          const w: Workspace = { ...defaultWorkspace(), tree: presetTree(p, order), focused: f,
                                 panel: { open: true, height: 260 } };
          for (let px = 400; px <= 2000; px += 25) {
            for (const py of [300, 450, 700, 1000]) {
              const fit = fitWorkspace(DEFAULT_LAYOUT, w, px, py);
              expect(fit.panes, `${p} ${order} focused ${f} at ${px}x${py}`).toContain(f);
              expect(fit.panes.length, `${p} at ${px}x${py}`).toBe(leaves(fit.render).length);
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
    expect(first).toMatchObject({ drawnPreset: "grid6", panes: [2, 3, 4, 6, 7, 8] });
    const at2 = focusPane(at8, 2);
    const prev: DrawnSet = drawnSet(at8, first);
    expect(fitWorkspace(DEFAULT_LAYOUT, at2, aw, ah, null, false, prev).panes).toEqual([2, 3, 4, 6, 7, 8]);
    expect(fitWorkspace(DEFAULT_LAYOUT, at2, aw, ah).panes).toEqual([1, 2, 3, 5, 6, 7]);
    // Focus moved to a pane it does not draw (Ctrl+Alt+1): a set that holds it.
    expect(fitWorkspace(DEFAULT_LAYOUT, focusPane(at8, 1), aw, ah, null, false, prev).panes).toEqual([1, 2, 3, 5, 6, 7]);
    // A different order is never sticky, even with the same ids.
    const shuffled = { ...at2, tree: presetTree("grid8", [2, 1, 3, 4, 5, 6, 7, 8]) };
    expect(fitWorkspace(DEFAULT_LAYOUT, shuffled, aw, ah, null, false, prev).panes).not.toEqual([2, 3, 4, 6, 7, 8]);
  });
});

describe("fitting eight panes: the plan's windows (§3.3)", () => {
  // The plan's table: an app window's inner height is the screen less ~80.
  const at = (p: Preset, w: number, screenH: number, zoom: number, panel = false) =>
    fitAt(setPanel(setPreset(defaultWorkspace(), p), { open: panel }), w, screenH - 80, zoom);
  const cols = (f: ReturnType<typeof fitAt>) => f.panes.map((n) => Math.round(f.boxes[n]!.px.width));

  it("1920×1080: grid4 and grid6 keep the side panes; grid8 squeezes them to 480 for four 360 columns", () => {
    for (const p of ["grid4", "grid6", "grid5"] as Preset[]) {
      const f = at(p, 1920, 1080, 100);
      expect(f.drawnPreset).toBe(p);
      expect(f.sides).toMatchObject({ left: 236, right: 316, leftFolded: false, rightFolded: false });
    }
    expect(cols(at("grid6", 1920, 1080, 100))).toEqual([456, 456, 456, 456, 456, 456]);
    for (const p of ["grid8", "grid7"] as Preset[]) {
      const f = at(p, 1920, 1080, 100);
      expect(f).toMatchObject({ drawnPreset: p, dropped: 0 });
      expect(f.sides.leftFolded || f.sides.rightFolded).toBe(false);
      expect(f.sides.left + f.sides.right).toBe(480);
      expect(f.width).toBe(1440);
    }
    expect(cols(at("grid8", 1920, 1080, 100))).toEqual([360, 360, 360, 360, 360, 360, 360, 360]);
  });

  it("1366×768: grid4 fits; grid6 folds the status pane; grid8 draws 6 of 8", () => {
    expect(at("grid4", 1366, 768, 100)).toMatchObject({ drawnPreset: "grid4", dropped: 0, sides: { rightFolded: false } });
    for (const p of ["grid6", "grid5"] as Preset[]) {
      expect(at(p, 1366, 768, 100)).toMatchObject({
        drawnPreset: p, dropped: 0, sides: { rightFolded: true, autoRight: true, leftFolded: false } });
    }
    expect(at("grid8", 1366, 768, 100)).toMatchObject({
      drawnPreset: "grid6", dropped: 2, why: "narrow", sides: { rightFolded: true, leftFolded: false } });
    expect(at("grid7", 1366, 768, 100)).toMatchObject({ drawnPreset: "grid6", dropped: 1 });
  });

  it("1280×800: grid4 fits at 728; grid6 folds both side panes (1208); grid8 draws 6 of 8 on both rails", () => {
    const g4 = at("grid4", 1280, 800, 100);
    expect(g4).toMatchObject({ drawnPreset: "grid4", dropped: 0, width: 728 });
    const g6 = at("grid6", 1280, 800, 100);
    expect(g6).toMatchObject({ drawnPreset: "grid6", width: 1208, sides: { leftFolded: true, rightFolded: true } });
    const g8 = at("grid8", 1280, 800, 100);
    expect(g8).toMatchObject({ drawnPreset: "grid6", dropped: 2, sides: { leftFolded: true, rightFolded: true } });
  });

  it("1280×800 at 150%: grid4 on both rails with the panel folded; 4 of 6; 4 of 8", () => {
    const g4 = at("grid4", 1280, 800, 150, true);
    expect(g4).toMatchObject({ drawnPreset: "grid4", sides: { leftFolded: true, rightFolded: true },
                               panel: { open: false, auto: true } });
    expect(at("grid6", 1280, 800, 150)).toMatchObject({ drawnPreset: "grid4", dropped: 2 });
    expect(at("grid8", 1280, 800, 150)).toMatchObject({ drawnPreset: "grid4", dropped: 4 });
    expect(at("grid5", 1280, 800, 150)).toMatchObject({ drawnPreset: "grid4", dropped: 1 });
    expect(at("grid7", 1280, 800, 150)).toMatchObject({ drawnPreset: "grid4", dropped: 3 });
  });

  it("1280×800 at 80% (1600 wide): eight fit, on both rails (1528 ≥ 1440)", () => {
    for (const p of ["grid4", "grid6", "grid8"] as Preset[]) expect(at(p, 1280, 800, 80)).toMatchObject({ drawnPreset: p, dropped: 0 });
    expect(at("grid8", 1280, 800, 80)).toMatchObject({ width: 1528, sides: { leftFolded: true, rightFolded: true } });
  });
});

// ---- WP-G2: split and close (directional since FS4) ------------------------------
//
// FS4 replaced the ladder (decisions FS-1..FS-5): a split halves its pane right
// or down, and × gives the space to its neighbour. These are WP-G2's rules that
// still hold — ids, focus, allocation, copies — on the new signatures. The ladder
// tests that named shapes are gone, each replaced in workspace.split.test.ts:
//   "climbs the ladder 1 → 8 and back"       → "6. the full 4×3 …" and "close gives the space … the previous sibling takes it"
//   "lands on the ladder's shape …"           → "the split (§5.1) > halves only the pane split …"
//   "decides the split button from the count" → the "the verdict …" blocks
//   "says why on the button"                  → "the buttons' titles (§10)"

describe("split and close (WP-G2, plan §3.4–§3.6; directional since FS4)", () => {
  const chat = (): ReturnType<typeof defaultPane> => ({ view: "chat", projectId: null, terminalId: null });
  /** Split the pane in slot `slot` into the lowest free id, as the window does. */
  const splitAt = (w: Workspace, slot: number, axis: "x" | "y" = "x") =>
    splitPane(w, unmeasured(w), orderOf(w)[slot - 1], axis, allocate(w)!, chat());
  /** Every id in use: ⊞ 8 → 1 (2…8 hidden), then four splits into the free 9…12. */
  const allTwelve = () => {
    let w = setPreset(setPreset(defaultWorkspace(), "grid8"), "single");
    for (const axis of ["x", "x", "x", "y"] as const) w = splitAt(w, orderOf(w).length, axis);
    expect([orderOf(w), w.kept]).toEqual([[1, 9, 10, 11, 12], [2, 3, 4, 5, 6, 7, 8]]);
    return w;
  };

  it("puts the new pane right after its source, in reading order — never at the end", () => {
    const three = setPreset(defaultWorkspace(), "cols3");
    for (const axis of ["x", "y"] as const) {
      const w = splitPane(three, unmeasured(three), 2, axis, 4, chat());
      expect(orderOf(w), axis).toEqual([1, 2, 4, 3]);
      expect(slotOf(w, 4), axis).toBe(3);
    }
  });

  it("focuses the new pane; × hands focus to `then`, else the neighbour that takes its space", () => {
    const three = setPreset(defaultWorkspace(), "cols3");
    const w = splitPane(three, unmeasured(three), 2, "x", 4, chat());
    expect(w.focused).toBe(4);
    const closed = closePane(w, 4);
    expect(orderOf(closed)).toEqual([1, 2, 3]);
    expect(closed.focused).toBe(2); // 2 took its space (FS4; the slot rule said 3)
    const last = closePane(focusPane(closed, 3), 3);
    expect(last.focused).toBe(2);
    expect(closePane(focusPane(closed, 1), 3).focused).toBe(1); // focus elsewhere stays put
    // The window names where focus goes when the selected chat closes: the chat selected before it.
    const grid = setPreset(defaultWorkspace(), "grid4");
    expect(closePane(grid, 1, 3).focused).toBe(3);
    expect(closePane(grid, 1, 1).focused).toBe(2); // not drawn after the close: the neighbour that took its space
    expect(closePane(grid, 1, null).focused).toBe(2);
  });

  it("splits without moving focus when asked not to ({focus: false}, the browser's dev-server pane)", () => {
    const w = focusPane(setPreset(defaultWorkspace(), "cols2"), 1);
    const quiet = splitPane(w, unmeasured(w), 2, "x", 3, { view: "browser", projectId: null, terminalId: null }, { focus: false });
    expect(orderOf(quiet)).toEqual([1, 2, 3]);
    expect(quiet.focused).toBe(1);
    expect(splitPane(w, unmeasured(w), 2, "x", 3, chat()).focused).toBe(3);
  });

  it("refuses an id already drawn and a source not drawn; a ninth pane is a split like any other (FS-1)", () => {
    const w = setPreset(defaultWorkspace(), "cols2");
    expect(splitPane(w, unmeasured(w), 1, "x", 2, chat())).toBe(w);
    expect(splitPane(w, unmeasured(w), 5, "x", 3, chat())).toBe(w);
    const eight = setPreset(defaultWorkspace(), "grid8");
    expect(splitPane(eight, unmeasured(eight), 1, "y", 2, chat())).toBe(eight);
    expect(allocate(eight)).toBe(9);
    const nine = splitPane(eight, unmeasured(eight), 1, "y", 9, chat());
    expect(orderOf(nine)).toEqual([1, 9, 2, 3, 4, 5, 6, 7, 8]);
  });

  it("allocates the lowest free id, else the oldest hidden pane with no unsaved edit, else none", () => {
    expect(allocate(defaultWorkspace())).toBe(2);
    const gap = closePane(setPreset(defaultWorkspace(), "grid4"), 2);
    expect(allocate(gap)).toBe(2);
    // ⊞ 8 → 1: 2…8 hidden, and 9…12 still free, so a split takes 9 (FS2), never a hidden pane.
    expect(allocate(setPreset(setPreset(defaultWorkspace(), "grid8"), "single"))).toBe(9);
    // All twelve used: 1, 9…12 drawn, 2…8 hidden (8 hidden longest, it is last).
    const one = allTwelve();
    expect(allocate(one)).toBe(8);
    expect(allocate(one, [8])).toBe(7);
    expect(allocate(one, [2, 3, 4, 5, 6, 7, 8])).toBeNull();
    // A hidden compose row's unsent words can be parked for the next new
    // thread, an unsaved edit cannot: the empty hidden panes go first, then
    // the ones holding words (review of PR #47, H1), never a dirty one.
    expect(allocate(one, [], [8, 7])).toBe(6);
    expect(allocate(one, [6], [2, 3, 4, 5, 7, 8])).toBe(8);
    expect(allocate(one, [2, 3, 4, 5, 6], [7, 8])).toBe(8);
    // A hidden pane a split takes back leaves `kept` and shows the copy.
    const took = splitPane(one, unmeasured(one), 1, "y", 8, chat());
    expect(took.kept).toEqual([2, 3, 4, 5, 6, 7]);
    expect(orderOf(took)).toEqual([1, 8, 9, 10, 11, 12]);
  });

  it("closes and splits ids, never slots: after pane 1 is closed nothing assumes it is there", () => {
    const w = closePane(setPreset(defaultWorkspace(), "cols3"), 1);
    expect(orderOf(w)).toEqual([2, 3]);
    expect(paneAt(w, 1)).toBe(2);
    expect(slotOf(w, 3)).toBe(2);
    // A free id 1 is the next a split takes, and it lands after its source.
    const back = splitPane(w, unmeasured(w), 3, "x", allocate(w)!, chat());
    expect(orderOf(back)).toEqual([2, 3, 1]);
    expect(slotOf(back, 1)).toBe(3);
    // Stored and read back, the order survives without a pane 1 first.
    const m = memory();
    saveWorkspace(w, m);
    expect(orderOf(loadWorkspace(m))).toEqual([2, 3]);
    expect(loadWorkspace(m).focused).toBe(2);
  });

  it("with nothing drawn, the fallbacks are the first pane in reading order, never id 1 (U36/U37)", () => {
    const w = closePane(setPreset(defaultWorkspace(), "cols2"), 1); // only pane 2, which shows the Browser
    expect(orderOf(w)).toEqual([2]);
    expect(show(w, "file", []).panes[1].view).toBe("file");
    expect(show(w, "file", []).panes[0].view).toBe(w.panes[0].view);
    expect(show(w, "file", []).focused).toBe(2);
  });

  it("copies what a pane shows (decisions S-2, plan §3.5)", () => {
    const base = { projectId: "p1", terminalId: "t1", browserUrl: "http://localhost:5173/x", keepOrigin: "http://localhost:5173",
                   filePath: "src/a.ts" };
    // A File pane's files are its tabs (ED-2): a stray filePath is not what it copies.
    expect(copySpec({ ...base, view: "file" })).toEqual({ view: "file", projectId: "p1", terminalId: null });
    expect(copySpec({ ...base, view: "diff" })).toEqual({ view: "diff", projectId: "p1", terminalId: null, filePath: "src/a.ts" });
    // The keep-origin grant is the owner's choice for one pane: never copied (WP-E).
    const browserCopy = copySpec({ ...base, view: "browser" });
    expect(browserCopy).toEqual({
      view: "browser", projectId: "p1", terminalId: null, browserUrl: "http://localhost:5173/x",
      browser: { tabs: [{ id: browserCopy.browser!.active, url: "http://localhost:5173/x" }], active: browserCopy.browser!.active } });
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
    expect(copySpec({ view: "browser", projectId: null, terminalId: null })).toEqual({ view: "browser", projectId: null, terminalId: null });
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
    const w = { ...setPreset(defaultWorkspace(), "grid5"),
                tree: presetTree("grid5", [1, 2, 3, 4, 5], { cols: [1 / 3, 2 / 3], row: 0.5, bottom: [0.05] }) };
    const f = fitWorkspace({ ...DEFAULT_LAYOUT, leftCollapsed: true, rightCollapsed: true }, w, 1172, 900);
    expect(f.drawnPreset).toBe("grid5");
    expect(f.boxes[4]!.px.width).toBeGreaterThanOrEqual(PANE_MIN_W - 1e-6);
    expect(f.panes.every((n) => f.boxes[n]!.px.width >= PANE_MIN_W - 1e-6)).toBe(true);
  });

  it("U34: a row the preferred panel dropped counts for the sticky set", () => {
    // 3×2 at 900 x 450 with the panel preferred: a column goes (2×2), then a
    // row for the panel (two columns). Focus on pane 3 draws [2, 3]; moving
    // focus to pane 2, inside that pair, keeps [2, 3] — not [1, 2].
    const w = setPanel(focusPane(setPreset(defaultWorkspace(), "grid6"), 3), { open: true });
    const f1 = fitWorkspace(DEFAULT_LAYOUT, w, 900, 450, null, true);
    expect(f1).toMatchObject({ drawnPreset: "cols2", panes: [2, 3] });
    expect(f1.panel.open).toBe(true);
    const prev: DrawnSet = drawnSet(w, f1);
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
    expect(f).toMatchObject({ drawnPreset: "cols2", panes: [4, 5] });
    expect(f.sides).toMatchObject({ leftFolded: false, rightFolded: false });
  });
});


describe("the sub-agent view (SV-3)", () => {
  const ref = { threadId: "t1", sid: "cl:toolu_a" };
  it("is stored as ids only, and a bad reference shows the pane's default view", () => {
    const mem = memory();
    const ws = setSubagent(defaultWorkspace(), 1, ref);
    expect(ws.panes[0]).toEqual({ view: "subagent", projectId: null, terminalId: null, subagent: ref });
    expect(ws.focused).toBe(1);
    saveWorkspace(ws, mem);
    expect(JSON.parse(mem.map.get(WORKSPACE_KEY)!).panes[0]).toEqual({ view: "subagent", projectId: null,
      terminalId: null, subagent: ref });
    expect(loadWorkspace(mem).panes[0].subagent).toEqual(ref);
    for (const bad of [undefined, null, {}, { threadId: "t1" }, { threadId: "t1", sid: "toolu_a" },
      { threadId: "t1", sid: "cl:a/b" }, { threadId: "t 1", sid: "cl:a" }, { threadId: "t1", sid: "cl:" + "a".repeat(129) }]) {
      const raw = JSON.parse(JSON.stringify(ws));
      raw.panes[0].subagent = bad;
      const back = parseWorkspace(JSON.stringify(raw)).panes[0];
      expect(back.view).toBe("chat");
      expect(back.subagent).toBeUndefined();
    }
    // A ref on another view is dropped.
    const raw = JSON.parse(JSON.stringify(ws));
    raw.panes[0].view = "file";
    expect(parseWorkspace(JSON.stringify(raw)).panes[0].subagent).toBeUndefined();
  });

  it("is never switched to without a sub-agent, and leaving it forgets which", () => {
    const ws = defaultWorkspace();
    expect(setView(ws, 1, "subagent")).toBe(ws);
    const shown = setSubagent(ws, 1, ref);
    const left = setView(shown, 1, "chat");
    expect(left.panes[0].view).toBe("chat");
    expect(left.panes[0].subagent).toBeUndefined();
    expect(setView(shown, 1, "subagent").panes[0].subagent).toEqual(ref);
    expect(setSubagent(ws, 1, { threadId: "t1", sid: "nope" })).toBe(ws);
  });

  it("a split from it is a new chat (one sub-agent per pane), and a split opens one beside its chat", () => {
    expect(copySpec(subagentSpec(ref))).toEqual({ view: "chat", projectId: null, terminalId: null });
    const one = defaultWorkspace();
    const ws = splitPane(one, unmeasured(one), 1, "x", 2, subagentSpec(ref));
    expect(orderOf(ws)).toEqual([1, 2]);
    expect(ws.panes[1]).toEqual({ view: "subagent", projectId: null, terminalId: null, subagent: ref });
    expect(ws.focused).toBe(2);
  });
});

// ---- FS2: twelve ids, still on the ladder ---------------------------------------

describe("twelve ids (FS2, decision FS-1; all twelve drawn since FS4)", () => {
  const chat = (): ReturnType<typeof defaultPane> => ({ view: "chat", projectId: null, terminalId: null });

  it("has twelve pane ids, 9–12 showing chat by default, and a spec for each", () => {
    expect(PANE_NOS).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]);
    for (const n of [9, 10, 11, 12] as PaneNo[]) {
      expect(defaultPane(n), String(n)).toEqual({ view: "chat", projectId: null, terminalId: null });
    }
    const w = defaultWorkspace();
    expect(w.panes).toHaveLength(12);
    expect(w.panes.map((p) => p.view))
      .toEqual(["chat", "browser", "file", "task", "chat", "chat", "chat", "chat", "chat", "chat", "chat", "chat"]);
  });

  it("⊞ grid8 → single keeps 2…8, and a split then takes id 9, in slot 2, never a kept pane", () => {
    const one = setPreset(setPreset(defaultWorkspace(), "grid8"), "single");
    expect([orderOf(one), one.kept]).toEqual([[1], [2, 3, 4, 5, 6, 7, 8]]);
    expect(allocate(one)).toBe(9);
    const two = splitPane(one, unmeasured(one), 1, "x", allocate(one)!, chat());
    expect([presetOfWs(two), orderOf(two), two.kept, two.focused]).toEqual(["cols2", [1, 9], [2, 3, 4, 5, 6, 7, 8], 9]);
    expect(slotOf(two, 9)).toBe(2);
    expect(paneAt(two, 2)).toBe(9);
    // The hidden panes keep what they showed, where they were.
    expect(two.panes.slice(1, 8)).toEqual(one.panes.slice(1, 8));
    // And the next free ids follow, lowest first.
    expect(allocate(two)).toBe(10);
  });

  it("⊞ grid8 takes the drawn, then the hidden; a ninth pane is a split, which takes a hidden one back (FS4)", () => {
    let w = setPreset(setPreset(defaultWorkspace(), "grid8"), "single");
    for (const axis of ["x", "x", "x", "y"] as const) {
      w = splitPane(w, unmeasured(w), orderOf(w)[orderOf(w).length - 1], axis, allocate(w)!, chat());
    }
    expect(orderOf(w)).toEqual([1, 9, 10, 11, 12]);
    expect(paneSequence(w)).toEqual([1, 9, 10, 11, 12, 2, 3, 4, 5, 6, 7, 8]);
    const eight = setPreset(w, "grid8");
    expect([orderOf(eight), eight.kept]).toEqual([[1, 9, 10, 11, 12, 2, 3, 4], [5, 6, 7, 8]]);
    // Four across: right is refused (FS-3); down takes the hidden 5 back.
    expect(splitPane(eight, unmeasured(eight), 1, "x", 5, chat())).toBe(eight);
    const nine = splitPane(eight, unmeasured(eight), 1, "y", 5, chat());
    expect([orderOf(nine), nine.kept]).toEqual([[1, 5, 9, 10, 11, 12, 2, 3, 4], [6, 7, 8]]);
  });

  it("× on a pane above 8 resets that pane's spec to its default, and frees its id", () => {
    const one = setPreset(setPreset(defaultWorkspace(), "grid8"), "single");
    const two = splitPane(one, unmeasured(one), 1, "x", 9, { view: "file", projectId: "p1", terminalId: null });
    expect(two.panes[8]).toMatchObject({ view: "file", projectId: "p1" });
    const back = closePane(two, 9);
    expect([orderOf(back), back.kept]).toEqual([[1], [2, 3, 4, 5, 6, 7, 8]]);
    expect(back.panes[8]).toEqual(defaultPane(9));
    expect(allocate(back)).toBe(9);
  });

  it("reads and stores ids 9–12 in order, kept and focused; 13 is still out of range", () => {
    const panes = Array.from({ length: 12 }, () => ({ view: "chat" }));
    panes[9] = { view: "file" };
    panes[11] = { view: "diff" };
    const raw = { preset: "grid4", order: [1, 10, 2, 12], kept: [11, 9], focused: 10, panes };
    const w = parseWorkspace(JSON.stringify(raw));
    expect([presetOfWs(w), orderOf(w), w.kept, w.focused]).toEqual(["grid4", [1, 10, 2, 12], [11, 9], 10]);
    expect([w.panes[9].view, w.panes[11].view]).toEqual(["file", "diff"]);
    expect(slotOf(w, 10)).toBe(2);
    expect(slotOf(w, 12)).toBe(4);
    const m = memory();
    saveWorkspace(w, m);
    expect(loadWorkspace(m)).toEqual(w);
    // Out of range: 13 in order falls back to 1…n; in kept and focused it is dropped.
    expect(orderOf(parseWorkspace(JSON.stringify({ ...raw, order: [1, 10, 2, 13] })))).toEqual([1, 2, 3, 4]);
    expect(parseWorkspace(JSON.stringify({ ...raw, kept: [13, 11] })).kept).toEqual([11]);
    expect(parseWorkspace(JSON.stringify({ ...raw, focused: 13 })).focused).toBe(1);
  });
});
