import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import App from "../src/App";

it("shows a failed first daily brief attempt as an error", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("fetch", vi.fn(async (url: string) => Response.json(
    url === "/v3/status" ? {
      service: "jarvis-agentd", version: "v3", pid: 42, uptime_s: 30,
      jobs: {
        "morning-brief": { last_run: null, last_outcome: "error", failures: 1 },
      },
    } : { briefs: [], recent: [] },
  )));
  const host = document.body.appendChild(document.createElement("div"));
  const root = createRoot(host);
  try {
    await act(async () => { root.render(<App />); });
    expect(host.querySelector('[data-panel="status"] [role="status"]')?.textContent).toBe("Running");
    const job = host.querySelector('[data-panel="cron"] li');
    expect(job?.textContent).toMatch(/error.*1 failed/i);
  } finally {
    act(() => root.unmount());
    host.remove();
    vi.unstubAllGlobals();
  }
});
