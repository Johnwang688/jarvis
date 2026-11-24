import { afterEach, expect, it, vi } from "vitest";
import { api } from "../api";
import { approvalRegion, editorRegion, startHudSensitiveReports } from "./hudSensitive";

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function region(attributes: Record<string, string>) {
  const node = document.createElement("div");
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
  return node;
}

it("reports live cards and editors on changes and repeats a heartbeat within two seconds", async () => {
  vi.useFakeTimers();
  const reported = vi.spyOn(api, "reportHudSensitive").mockResolvedValue({ ok: true });
  const parent = document.createElement("div");
  document.body.append(parent);
  const stop = startHudSensitiveReports();
  const last = () => reported.mock.lastCall?.[0];
  const flush = async () => { await Promise.resolve(); await Promise.resolve(); };
  try {
    const client = last()?.client_id;
    expect(client).toMatch(/^[0-9a-f]{32}$/);
    expect(last()).toEqual({ client_id: client, cards: 0, editors: 0 });

    const card = region(approvalRegion);
    const editor = region(editorRegion);
    const hidden = document.createElement("div");
    hidden.style.display = "none";
    hidden.append(editor);
    parent.append(card, hidden);
    await flush();
    expect(last()).toEqual({ client_id: client, cards: 1, editors: 0 });

    hidden.style.display = "block";
    await flush();
    expect(last()).toEqual({ client_id: client, cards: 1, editors: 1 });

    card.remove();
    await flush();
    expect(last()).toEqual({ client_id: client, cards: 0, editors: 1 });

    const calls = reported.mock.calls.length;
    await vi.advanceTimersByTimeAsync(2000);
    expect(reported).toHaveBeenCalledTimes(calls + 1);
    expect(last()).toEqual({ client_id: client, cards: 0, editors: 1 });
  } finally {
    stop();
    parent.remove();
  }
});
