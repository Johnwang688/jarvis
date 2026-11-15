import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => {
  class ApiError extends Error {
    status: number; body: Record<string, unknown> | null = null;
    constructor(status: number, message: string) { super(message); this.status = status; }
  }
  return { ApiError, workingSettings: vi.fn(), setWorkingSettings: vi.fn(), getWorking: vi.fn() };
});
vi.mock("../api", () => ({ api: mocks, ApiError: mocks.ApiError }));
import { InboxSettings } from "./InboxSettings";

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
let host: HTMLDivElement;
let root: Root;
let notify: () => void;
const changes = (fn: () => void) => { notify = fn; return () => { notify = () => {}; }; };
beforeEach(() => {
  host = document.createElement("div"); document.body.appendChild(host); root = createRoot(host);
  mocks.workingSettings.mockReset(); mocks.setWorkingSettings.mockReset(); notify = () => {};
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); });

const served = (over: Record<string, unknown> = {}) => ({ idle_days: 3, min: 1, max: 30, default: 3, ...over });
const flush = () => act(async () => { await new Promise((r) => setTimeout(r, 0)); });
const show = async (props: Record<string, unknown> = {}) => {
  await act(async () => root.render(createElement(InboxSettings, { changes, ...props })));
  await flush();
};
const q = (id: string) => host.querySelector(`[data-testid="${id}"]`) as HTMLElement | null;
const input = () => q("inbox-idle-days") as HTMLInputElement;
const type = async (value: string) => {
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  await act(async () => { set.call(input(), value); input().dispatchEvent(new Event("input", { bubbles: true })); });
};
const save = async () => { await act(async () => { q("inbox-idle-save")!.click(); }); await flush(); };

describe("Settings > Inbox idle limit", () => {
  it("shows the daemon's value and its bounds, with a linked label and the help line", async () => {
    mocks.workingSettings.mockResolvedValue(served({ idle_days: 5, min: 2, max: 9, default: 4 }));
    await show();
    expect(input().value).toBe("5");
    expect(host.textContent).toContain("2–9, default 4");
    expect(host.querySelector(`label[for="${input().id}"]`)?.textContent).toContain("Leave In progress after");
    expect(q("inbox-idle-help")!.textContent).toMatch(/Done/);
    expect(q("inbox-idle-help")!.textContent).toMatch(/never leaves on its own/);
    expect(input().getAttribute("aria-describedby")).toContain("inbox-idle-help");
    expect((q("inbox-idle-save") as HTMLButtonElement).disabled).toBe(true);
  });

  it("rejects 0, 31 and 2.5 before sending, using the server's bounds", async () => {
    mocks.workingSettings.mockResolvedValue(served());
    await show();
    for (const bad of ["0", "31", "2.5", "", "abc", "-1"]) {
      await type(bad); await save();
      expect(q("inbox-idle-result")!.textContent).toBe("Enter a whole number from 1 to 30.");
      expect(input().getAttribute("aria-invalid")).toBe("true");
    }
    expect(mocks.setWorkingSettings).not.toHaveBeenCalled();
  });

  it("takes its bounds from the server, not a constant", async () => {
    mocks.workingSettings.mockResolvedValue(served({ idle_days: 3, min: 2, max: 9 }));
    await show();
    await type("10"); await save();
    expect(q("inbox-idle-result")!.textContent).toBe("Enter a whole number from 2 to 9.");
    await type("1"); await save();
    expect(mocks.setWorkingSettings).not.toHaveBeenCalled();
    await type("9"); await save();
    expect(mocks.setWorkingSettings).toHaveBeenCalledWith(9);
  });

  it("saves the whole number, disables the control while saving, then says saved", async () => {
    mocks.workingSettings.mockResolvedValue(served());
    let finish!: (v: unknown) => void;
    mocks.setWorkingSettings.mockReturnValue(new Promise((r) => { finish = r; }));
    await show();
    await type(" 5 ");
    await act(async () => { q("inbox-idle-save")!.click(); });
    expect(mocks.setWorkingSettings).toHaveBeenCalledTimes(1);
    expect(mocks.setWorkingSettings).toHaveBeenCalledWith(5);
    expect(input().disabled).toBe(true);
    expect((q("inbox-idle-save") as HTMLButtonElement).disabled).toBe(true);
    await act(async () => { finish(served({ idle_days: 5 })); });
    await flush();
    expect(input().disabled).toBe(false);
    expect(input().value).toBe("5");
    expect(q("inbox-idle-result")!.textContent).toBe("Saved: 5 days.");
  });

  it("shows the server's 400 sentence inline and keeps the typed value", async () => {
    mocks.workingSettings.mockResolvedValue(served());
    mocks.setWorkingSettings.mockRejectedValue(new mocks.ApiError(400, "idle_days must be a whole number from 1 to 30"));
    await show();
    await type("7"); await save();
    expect(q("inbox-idle-result")!.textContent).toBe("idle_days must be a whole number from 1 to 30");
    expect(input().value).toBe("7");
    expect(input().disabled).toBe(false);
  });

  it("says Not saved for a 503 or a network failure", async () => {
    mocks.workingSettings.mockResolvedValue(served());
    mocks.setWorkingSettings.mockRejectedValue(new mocks.ApiError(503, "the setting could not be saved"));
    await show();
    await type("7"); await save();
    expect(q("inbox-idle-result")!.textContent).toBe("Not saved: the setting could not be saved");
    mocks.setWorkingSettings.mockRejectedValue(new TypeError("Failed to fetch"));
    await save();
    expect(q("inbox-idle-result")!.textContent).toBe("Not saved: Failed to fetch");
  });

  it("is quietly unavailable on 403 and 404, with no field and no value", async () => {
    for (const status of [403, 404]) {
      mocks.workingSettings.mockRejectedValue(new mocks.ApiError(status, "no"));
      await show();
      expect(q("inbox-settings-unavailable")).not.toBeNull();
      expect(input()).toBeNull();
      expect(host.textContent).not.toMatch(/\b3\b/);
      await act(async () => root.render(createElement("div")));
    }
  });

  it("shows an error with Retry on a network failure or an unreadable answer, never a made-up value", async () => {
    mocks.workingSettings.mockRejectedValue(new TypeError("Failed to fetch"));
    await show();
    expect(q("inbox-settings-error")!.textContent).toContain("Failed to fetch");
    expect(input()).toBeNull();
    mocks.workingSettings.mockResolvedValue({ idle_days: "3" });
    await act(async () => { q("inbox-settings-retry")!.click(); }); await flush();
    expect(q("inbox-settings-error")).not.toBeNull();
    expect(input()).toBeNull();
    mocks.workingSettings.mockResolvedValue(served({ idle_days: 6 }));
    await act(async () => { q("inbox-settings-retry")!.click(); }); await flush();
    expect(input().value).toBe("6");
  });

  it("a working_changed refresh does not clobber what is being typed, and follows when nothing is typed", async () => {
    mocks.workingSettings.mockResolvedValue(served());
    await show();
    await type("12");
    mocks.workingSettings.mockResolvedValue(served({ idle_days: 8 }));
    await act(async () => { notify(); }); await flush();
    expect(input().value).toBe("12");
  });

  it("a refresh with nothing typed shows the daemon's new value", async () => {
    mocks.workingSettings.mockResolvedValue(served());
    await show();
    mocks.workingSettings.mockResolvedValue(served({ idle_days: 8 }));
    await act(async () => { notify(); }); await flush();
    expect(input().value).toBe("8");
  });

  it("a refresh that fails shows an error with Retry, not the old value, and keeps the draft for later", async () => {
    mocks.workingSettings.mockResolvedValue(served());
    await show();
    await type("12");
    mocks.workingSettings.mockRejectedValue(new TypeError("Failed to fetch"));
    await act(async () => { notify(); }); await flush();
    expect(q("inbox-settings-error")!.textContent).toContain("Failed to fetch");
    expect(input()).toBeNull();
    mocks.workingSettings.mockResolvedValue(served({ idle_days: 8 }));
    await act(async () => { q("inbox-settings-retry")!.click(); }); await flush();
    expect(input().value).toBe("12");
    expect(host.textContent).toContain("1–30");
  });

  it("a read that began before a save cannot land over it", async () => {
    mocks.workingSettings.mockResolvedValue(served());
    await show();
    let late!: (v: unknown) => void;
    mocks.workingSettings.mockReturnValueOnce(new Promise((r) => { late = r; }));
    await act(async () => { notify(); });
    mocks.setWorkingSettings.mockResolvedValue(served({ idle_days: 5 }));
    mocks.workingSettings.mockResolvedValue(served({ idle_days: 5 }));
    await type("5"); await save();
    await act(async () => { late(served({ idle_days: 3 })); }); await flush();
    expect(input().value).toBe("5");
    expect(q("inbox-idle-result")!.textContent).toBe("Saved: 5 days.");
  });

  it("reads the setting again after a save", async () => {
    mocks.workingSettings.mockResolvedValue(served());
    await show();
    const before = mocks.workingSettings.mock.calls.length;
    mocks.setWorkingSettings.mockResolvedValue(served({ idle_days: 5 }));
    await type("5"); await save();
    expect(mocks.workingSettings.mock.calls.length).toBeGreaterThan(before);
  });

  it("a save answered 403 or 404 is unavailable: no field, no value", async () => {
    for (const status of [403, 404]) {
      mocks.workingSettings.mockResolvedValue(served());
      mocks.setWorkingSettings.mockRejectedValue(new mocks.ApiError(status, "no"));
      await show();
      await type("7"); await save();
      expect(q("inbox-settings-unavailable")).not.toBeNull();
      expect(input()).toBeNull();
      await act(async () => root.render(createElement("div")));
    }
  });

  it("a server 400 marks the field invalid, and typing clears it", async () => {
    mocks.workingSettings.mockResolvedValue(served());
    mocks.setWorkingSettings.mockRejectedValue(new mocks.ApiError(400, "refused"));
    await show();
    await type("7"); await save();
    expect(input().getAttribute("aria-invalid")).toBe("true");
    await type("8");
    expect(input().getAttribute("aria-invalid")).toBeNull();
  });

  it("is inert while blocked", async () => {
    mocks.workingSettings.mockResolvedValue(served());
    await show({ blocked: true });
    expect(input().disabled).toBe(true);
  });
});
