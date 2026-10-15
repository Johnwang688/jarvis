import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { parseRow, type TerminalRow } from "../lib/terminal";
import { TermManager } from "./Terminal";

afterEach(() => vi.restoreAllMocks());

describe("showing a newly created program terminal", () => {
  it("waits for an older listing and fetches again so the new program is shown", async () => {
    const mgr = new TermManager();
    const row = parseRow({ id: "a0000001", title: "vex clean", program: "vex-build" })!;
    let release!: (rows: TerminalRow[]) => void;
    const listing = vi.spyOn(api, "terminals")
      .mockImplementationOnce(() => new Promise((resolve) => { release = resolve; }))
      .mockResolvedValueOnce([row]);
    const open = vi.fn();
    mgr.showPanel = open;
    const older = mgr.refresh();
    const show = mgr.showProgram(row.id);
    release([]);
    await older;
    await show;
    expect(listing).toHaveBeenCalledTimes(2);
    expect(mgr.claimed.has(row.id)).toBe(true);
    expect(mgr.panelActive()).toBe(row.id);
    expect(open).toHaveBeenCalledOnce();
  });
});

describe("Settings covering terminals", () => {
  it("hides registry visibility without changing placement, selection or sessions", () => {
    const mgr = new TermManager();
    mgr.place = new Map([["pane-terminal", 1]]);
    const original = mgr.place;
    const pending = { id: "pane-terminal", continuing: true, dropHiddenHold: vi.fn() };
    mgr.sessions.set(pending.id, pending as any);
    expect(mgr.visible("pane-terminal")).toBe(true);
    mgr.setCovered(true);
    expect(mgr.visible("pane-terminal")).toBe(false);
    expect(mgr.place).toBe(original);
    expect(mgr.sessions.get(pending.id)).toBe(pending);
    expect(pending.dropHiddenHold).toHaveBeenCalledOnce();
    mgr.setCovered(false);
    expect(mgr.visible("pane-terminal")).toBe(true);
  });
});

describe("the \"Jarvis can read\" switch across windows (terminal_readable)", () => {
  const row = (readable: boolean) => parseRow({ id: "a0000002", title: "zsh", readable })!;
  const event = (readable: unknown, extra: Record<string, unknown> = {}) =>
    ({ terminal_id: "a0000002", readable, at: "2026-10-14T12:00:00+00:00", ...extra });

  it("shows another window's change in this window's switch, and says so once", () => {
    const mgr = new TermManager();
    mgr.rows = [row(true)];
    const changed = vi.spyOn(mgr, "changed");
    mgr.heardReadable(event(false));
    expect(mgr.row("a0000002")!.readable).toBe(false);
    expect(mgr.readableOf("a0000002")).toBe(false);          // a split now carries the off switch
    expect(changed).toHaveBeenCalledOnce();
    mgr.heardReadable(event(true));
    expect(mgr.row("a0000002")!.readable).toBe(true);
    changed.mockClear();
    mgr.heardReadable(event(true));                           // already shown: nothing to redraw
    expect(changed).not.toHaveBeenCalled();
  });

  it("drops a record of the wrong shape and ignores a terminal it does not list", () => {
    const mgr = new TermManager();
    mgr.rows = [row(true)];
    for (const bad of [null, {}, event("no"), event(0), event(undefined), { ...event(false), terminal_id: "../etc" }]) {
      mgr.heardReadable(bad);
      expect(mgr.row("a0000002")!.readable).toBe(true);
    }
    mgr.heardReadable({ ...event(false), terminal_id: "b0000009" });
    expect(mgr.rows.map((r) => r.id)).toEqual(["a0000002"]);   // no row is made up from an event
  });
});
