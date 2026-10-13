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
