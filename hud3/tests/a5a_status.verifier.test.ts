import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import { Status } from "../src/Status";

it("rechecks a stale Running status when a backgrounded window becomes visible", async () => {
  vi.useFakeTimers();
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  let hidden = false;
  const visibility = vi.spyOn(document, "hidden", "get").mockImplementation(() => hidden);
  const fetchStatus = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({
      service: "jarvis-agentd",
      version: "v3",
      pid: 4242,
      uptime_s: 61,
    })))
    .mockRejectedValue(new TypeError("Daemon stopped"));
  vi.stubGlobal("fetch", fetchStatus);

  const container = document.body.appendChild(document.createElement("div"));
  const root = createRoot(container);
  try {
    await act(async () => { root.render(createElement(Status)); });
    expect(container.querySelector('[role="status"]')?.textContent).toBe("Running");

    hidden = true;
    document.dispatchEvent(new Event("visibilitychange"));
    // Model a suspended tab: wall time passes without running the polling timer.
    vi.setSystemTime(Date.now() + 60_000);
    hidden = false;
    await act(async () => { document.dispatchEvent(new Event("visibilitychange")); });

    expect(container.querySelector('[role="status"]')?.textContent).toBe("Not reachable");
  } finally {
    act(() => root.unmount());
    container.remove();
    visibility.mockRestore();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  }
});
