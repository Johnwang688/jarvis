import { describe, expect, it } from "vitest";
import { addBrowserTab, activateBrowserTab, closeBrowserTab, copySpec, defaultWorkspace, parseWorkspace,
  parseBrowserTabs, setBrowserUrl, submitBrowserUrl, setTabKeepOrigin, setView, type PaneNo } from "./workspace";
import { browserFrames, EMPTY_FRAMES } from "./browserframes";
import { browserLabel } from "./browser";

describe("Direct tab storage and grants", () => {
  it("migrates both old names, mirrors only the active tab and drops mismatched grants", () => {
    for (const old of ["browserUrl", "previewUrl"]) {
      const ws = parseWorkspace(JSON.stringify({ panes: [{ view: "browser", [old]: "http://localhost:5173/app",
        keepOrigin: "http://localhost:5173" }] }));
      const tab = ws.panes[0].browser!.tabs[0];
      expect(tab).toEqual({ id: "legacy", url: "http://localhost:5173/app", keepOrigin: "http://localhost:5173" });
      const more = addBrowserTab(ws, 1, "http://localhost:5174/");
      const saved = parseWorkspace(JSON.stringify(more)).panes[0];
      expect(saved.browser!.tabs).toHaveLength(2);
      expect(saved.browserUrl).toBe("http://localhost:5174/");
      expect(saved.keepOrigin).toBeUndefined();
      expect(saved).not.toHaveProperty("previewUrl");
    }
  });
  it("keeps an explicit empty browser empty despite stale mirrors, including final close", () => {
    const ws = addBrowserTab(defaultWorkspace(), 2, "http://localhost:5173/");
    const gone = closeBrowserTab(ws, 2, ws.panes[1].browser!.active);
    expect(gone.panes[1].browser).toEqual({ tabs: [], active: "" });
    const saved = parseWorkspace(JSON.stringify({ ...gone, panes: gone.panes.map((p, i) => i === 1
      ? { ...p, browserUrl: "http://localhost:5173/", keepOrigin: "http://localhost:5173" } : p) }));
    expect(saved.panes[1].browser).toEqual({ tabs: [], active: "" });
    expect(saved.panes[1].browserUrl).toBeUndefined();
    expect(saved.panes[1].keepOrigin).toBeUndefined();
  });
  it("drops malformed and duplicate tabs, bounds size and strips unknown fields", () => {
    const raw = { tabs: [{ id: "abcdef", url: "http://localhost:5173/", mode: "remote",
      keepOrigin: "http://localhost:5174", auto: { kind: "thread", label: "safe\u202e" + "x".repeat(80), secret: true } },
      { id: "abcdef", url: "http://localhost:5174/" }, { id: "../bad", url: "" }, { id: "bigbig", url: "x".repeat(5000) },
      ...Array.from({ length: 20 }, (_, i) => ({ id: String(i).padStart(6, "0"), url: "" }))], active: "absent" };
    const parsed = parseBrowserTabs(raw)!;
    expect(parsed.tabs).toHaveLength(12);
    expect(parsed.active).toBe("abcdef");
    expect(parsed.tabs[0].keepOrigin).toBeUndefined();
    expect(parsed.tabs[0]).not.toHaveProperty("mode");
    expect(parsed.tabs[0].auto!.label).toHaveLength(60);
    expect(parsed.tabs[0].auto!.label).not.toContain("\u202e");
    expect(parsed.tabs[0].auto).not.toHaveProperty("secret");
  });
  it("never carries grants across tab switches, URL origins or copied specs", () => {
    let ws = addBrowserTab(defaultWorkspace(), 2, "http://localhost:5173/", { auto: { kind: "thread", label: "chat" } });
    const first = ws.panes[1].browser!.active;
    ws = setTabKeepOrigin(ws, 2, first, "http://localhost:5173");
    ws = addBrowserTab(ws, 2, "http://localhost:5173/second");
    const second = ws.panes[1].browser!.active;
    expect(ws.panes[1].keepOrigin).toBeUndefined();
    ws = activateBrowserTab(ws, 2, first);
    expect(ws.panes[1].keepOrigin).toBe("http://localhost:5173");
    ws = setBrowserUrl(ws, 2, "http://localhost:5174/", second);
    expect(ws.panes[1].browserUrl).toBe("http://localhost:5173/");
    const copy = copySpec(ws.panes[1]);
    expect(copy.browser!.tabs.map((t) => t.url)).toEqual(ws.panes[1].browser!.tabs.map((t) => t.url));
    expect(copy.browser!.tabs.every((t) => !t.keepOrigin && !t.auto)).toBe(true);
    expect(copy.browser!.tabs.every((t) => !ws.panes[1].browser!.tabs.some((old) => old.id === t.id))).toBe(true);
    expect(copy.browser!.active).toBe(copy.browser!.tabs[0].id);
    expect(setBrowserUrl(ws, 2, "http://localhost:5174/").panes[1].keepOrigin).toBeUndefined();
  });
  it("caps additions at 12 and closes active/background tabs without losing siblings", () => {
    let ws = defaultWorkspace();
    for (let i = 0; i < 12; i++) ws = addBrowserTab(ws, 2, "http://localhost:5173/" + i);
    expect(addBrowserTab(ws, 2, "")).toBe(ws);
    const ids = ws.panes[1].browser!.tabs.map((t) => t.id);
    ws = closeBrowserTab(ws, 2, ids[2]);
    expect(ws.panes[1].browser!.active).toBe(ids[11]);
    ws = closeBrowserTab(ws, 2, ids[11]);
    expect(ws.panes[1].browser!.active).toBe(ids[10]);
  });
  it("renders hostile URL text as a cleaned bounded label", () => {
    const label = browserLabel("http://localhost:5173/%E2%80%AE%3Cscript%3E" + "x".repeat(5000));
    expect(Array.from(label).length).toBe(40);
    expect(label).not.toContain("\u202e");
    expect(label).toContain("<script>");
    expect(browserLabel("")).toBe("New tab");
  });
});

describe("Direct frame budgets", () => {
  it("keeps only three recently visited per pane and eight window-wide, active drawn first", () => {
    let ws = defaultWorkspace(), state = EMPTY_FRAMES;
    const drawn: PaneNo[] = [1, 2, 3, 4];
    for (const pane of drawn) {
      ws = setView(ws, pane, "browser");
      for (let i = 0; i < 3; i++) {
        ws = addBrowserTab(ws, pane, "http://localhost:5173/" + pane + "/" + i);
        state = browserFrames(state, ws, drawn);
      }
    }
    expect(state.recent).toHaveLength(8);
    for (const pane of drawn) {
      expect(state.recent).toContain(pane + ":" + ws.panes[pane - 1].browser!.active);
      expect(state.recent.filter((k) => k.startsWith(pane + ":")).length).toBeLessThanOrEqual(3);
    }
    const oldest = "1:" + ws.panes[0].browser!.tabs[0].id;
    expect(state.recent).not.toContain(oldest);
    // Hiding every pane never exempts those frames from the window cap.
    state = browserFrames(state, ws, []);
    expect(state.recent).toHaveLength(8);
    ws = activateBrowserTab(ws, 1, ws.panes[0].browser!.tabs[0].id);
    state = browserFrames(state, ws, drawn);
    expect(state.recent).toContain(oldest);
    expect(state.recent).toHaveLength(8);
  });
  it("does not spend the live budget on stored panes never drawn", () => {
    let ws = defaultWorkspace();
    for (const pane of [1, 2, 3, 4, 5, 6, 7, 8] as PaneNo[]) {
      ws = addBrowserTab(setView(ws, pane, "browser"), pane, "http://localhost:5173/" + pane);
    }
    const state = browserFrames(EMPTY_FRAMES, ws, [1]);
    expect(state.recent).toEqual(["1:" + ws.panes[0].browser!.active]);
  });
  it("does not reload evicted background pages when capacity becomes free", () => {
    let ws = defaultWorkspace(), state = EMPTY_FRAMES;
    for (let i = 0; i < 4; i++) {
      ws = addBrowserTab(ws, 2, "http://localhost:5173/" + i);
      state = browserFrames(state, ws, [2]);
    }
    expect(state.recent).toHaveLength(3);
    const oldest = ws.panes[1].browser!.tabs[0].id;
    ws = closeBrowserTab(ws, 2, ws.panes[1].browser!.tabs[1].id);
    state = browserFrames(state, ws, [2]);
    expect(state.recent).toHaveLength(2);
    expect(state.recent).not.toContain("2:" + oldest);
    expect(browserFrames(state, ws, [2])).toBe(state);
  });
});

describe("Browser source privacy provenance", () => {
  const source = { kind: "dev_server" as const, entryId: "ds_0123abcd" };
  const auto = { kind: "terminal" as const, label: "Build terminal" };
  const url = "http://localhost:5173/private/path";
  const active = (ws: ReturnType<typeof defaultWorkspace>) => ws.panes[1].browser!.tabs[0];
  it("normalizes legacy or malformed source to unknown without trusting display metadata", () => {
    for (const raw of [undefined, null, {}, [], "owner", { kind: "owner" },
      { kind: "dev_server", entryId: "ds_0123ABCD" }, { kind: "dev_server", entryId: "../terminal" }]) {
      const parsed = parseBrowserTabs({ tabs: [{ id: "abcdef", url, auto, source: raw }], active: "abcdef" })!;
      expect(parsed.tabs[0].source).toEqual({ kind: "unknown" });
    }
    const legacy = parseBrowserTabs({ tabs: [{ id: "abcdef", url, auto }], active: "abcdef" })!;
    expect(legacy.tabs[0].source).toEqual({ kind: "unknown" });
    const owner = parseBrowserTabs({ tabs: [{ id: "abcdef", url }], active: "abcdef" })!;
    expect(owner.tabs[0]).not.toHaveProperty("source");
    const raw = parseBrowserTabs({ tabs: [{ id: "abcdef", url,
      source: { ...source, terminal_id: "not-authority" } }], active: "abcdef" })!;
    expect(raw.tabs[0].source).toEqual(source);
  });
  it("keeps source through passive URL changes, tab selection, grants and copies without auto labels", () => {
    let ws = addBrowserTab(defaultWorkspace(), 2, url, { auto, source });
    const id = active(ws).id;
    ws = setBrowserUrl(ws, 2, "http://localhost:5174/redirected", id);
    ws = setTabKeepOrigin(ws, 2, id, "http://localhost:5174");
    ws = addBrowserTab(ws, 2, "http://localhost:5175/");
    ws = activateBrowserTab(ws, 2, id);
    expect(active(ws).source).toEqual(source);
    const copy = copySpec(ws.panes[1]);
    expect(copy.browser!.tabs[0].source).toEqual(source);
    expect(copy.browser!.tabs[0]).not.toHaveProperty("auto");
    expect(copy.browser!.tabs[0]).not.toHaveProperty("keepOrigin");
    expect(copy.browser!.tabs[0].id).not.toBe(id);
    expect(copy.browser!.tabs[0].source).not.toBe(active(ws).source);
    const saved = parseWorkspace(JSON.stringify(ws));
    expect(active(saved).source).toEqual(source);
  });
  it("keeps an unknown source on copies even when its legacy display label is discarded", () => {
    const ws = addBrowserTab(defaultWorkspace(), 2, url, { auto });
    expect(active(ws).source).toEqual({ kind: "unknown" });
    expect(copySpec(ws.panes[1]).browser!.tabs[0].source).toEqual({ kind: "unknown" });
  });
  it("only owner address submission clears source, including submitting the same URL", () => {
    const ws = addBrowserTab(defaultWorkspace(), 2, url, { auto, source });
    expect(setBrowserUrl(ws, 2, url)).toBe(ws);
    for (const nextUrl of [url, "http://localhost:5174/owner"]) {
      const submitted = submitBrowserUrl(ws, 2, nextUrl, active(ws).id);
      expect(active(submitted).url).toBe(nextUrl);
      expect(active(submitted)).not.toHaveProperty("source");
      expect(active(submitted)).not.toHaveProperty("auto");
      expect(active(parseWorkspace(JSON.stringify(submitted)))).not.toHaveProperty("source");
      expect(active(ws).source).toEqual(source);
    }
    expect(submitBrowserUrl(ws, 2, "x".repeat(5000))).toBe(ws);
    expect(submitBrowserUrl(ws, 2, url, "absent")).toBe(ws);
  });
});
