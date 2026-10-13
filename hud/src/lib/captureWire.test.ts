import { afterEach, expect, it, vi } from "vitest";
import { api } from "../api";
afterEach(() => vi.unstubAllGlobals());
it("capture GET carries only exact opaque binding and explicit preselected text scope", async () => {
  const fetch = vi.fn(async (_path: string, _init?: RequestInit) => new Response("{}", { status: 200 }));
  vi.stubGlobal("fetch", fetch);
  const identity = { window: "a".repeat(32), pane_id: 5, local_tab_id: "abcdef", view_id: "deadbeef" };
  await api.captureBrowser("deadbeef", identity, false);
  expect(fetch).toHaveBeenCalledExactlyOnceWith(
    "/browser/tabs/deadbeef/capture?window=" + "a".repeat(32) + "&pane=5&local_tab=abcdef&view=deadbeef&whole=0", undefined);
  await api.captureBrowser("deadbeef", identity, true);
  expect(fetch.mock.calls[1][0]).toContain("whole=1");
});
