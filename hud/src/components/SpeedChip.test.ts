import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { SpeedChip } from "./SpeedChip";
import { applyModel, patchBody, threadBody, type Choice, type ThreadModels } from "../lib/threadmodel";

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
const description = "Faster output, billed as extra usage beyond your subscription";
const models: ThreadModels = { providers: { claude: { label: "Claude", default: "claude-a", default_effort: "high", models: [
  { id: "claude-a", speeds: [["fast", "Fast", description], ["future", "Future", "More usage"]] },
  { id: "claude-b", speeds: [] }, { id: "claude-c", speeds: [["fast", "Fast", description]] },
] } } };
const choice: Choice = { provider: "claude", model: "claude-a", effort: null, speed: null, speed_model: "claude-a" };

describe("per-chat speed chip", () => {
  let host: HTMLDivElement;
  let root: Root;
  let changes: (string | null)[];
  beforeEach(() => {
    host = document.createElement("div"); document.body.append(host);
    root = createRoot(host); changes = [];
  });
  afterEach(() => { act(() => root.unmount()); host.remove(); });
  const draw = (c = choice, disabled = false) => act(() => root.render(createElement(SpeedChip,
    { models, choice: c, disabled, onChange: (speed) => changes.push(speed) })));
  const chip = () => host.querySelector<HTMLElement>('[data-testid="speed-chip"]')!;
  const select = () => host.querySelector("select")!;

  it("offers Standard and catalog names with descriptions, including future tiers", () => {
    draw();
    expect(Array.from(select().options).map((o) => [o.value, o.text, o.title])).toEqual([
      ["", "Standard", ""], ["fast", "Fast", description], ["future", "Future", "More usage"],
    ]);
    expect(chip().dataset.on).toBe("false");
    expect(select().disabled).toBe(false);
    expect(changes).toEqual([]);
  });
  it("lights the badge and names the extra billing cost", () => {
    draw({ ...choice, speed: "fast" });
    expect(chip().dataset.on).toBe("true");
    expect(chip().title).toBe(description);
    expect(select().selectedOptions[0].text).toBe("Fast");
  });
  it("describes quota cost when the catalog description is empty", () => {
    const empty = { providers: { claude: { ...models.providers.claude!, models: [
      { id: "claude-a", speeds: [["fast", "Fast", ""]] as [string, string, string][] },
    ] } } };
    act(() => root.render(createElement(SpeedChip, { models: empty, choice: { ...choice, speed: "fast" }, onChange: () => {} })));
    const cost = "Faster responses; uses more of your plan's quota";
    expect(chip().title).toBe(cost);
    expect(select().selectedOptions[0].title).toBe(cost);
  });
  it("shows a pending change with a disabled chip", () => {
    act(() => root.render(createElement(SpeedChip, { models, choice, pending: true, onChange: () => {} })));
    expect(select().disabled).toBe(true);
    expect(chip().dataset.pending).toBe("true");
    expect(chip().getAttribute("aria-busy")).toBe("true");
    expect(chip().title).toContain("pending");
    expect(chip().classList.contains("err")).toBe(false);
  });
  it("sets and resets speed directly, with no confirmation or auto-off", () => {
    draw();
    act(() => { select().value = "fast"; select().dispatchEvent(new Event("change", { bubbles: true })); });
    expect(changes).toEqual(["fast"]);
    draw({ ...choice, speed: "fast" });
    act(() => { select().value = ""; select().dispatchEvent(new Event("change", { bubbles: true })); });
    expect(changes).toEqual(["fast", null]);
  });
  it("disables and greys models without speed tiers", () => {
    draw({ ...choice, model: "claude-b" });
    expect(select().disabled).toBe(true);
    expect(chip().dataset.unavailable).toBe("true");
    expect(chip().title).toBe("This model has no fast mode");
    expect(Array.from(select().options).map((o) => o.text)).toEqual(["Standard"]);
  });
  it("uses the default model's tiers, and disables under an authorization card", () => {
    draw({ ...choice, model: null }, true);
    expect(select().options.length).toBe(3);
    expect(select().disabled).toBe(true);
  });
  it("keeps keyboard events local and is natively keyboard reachable", () => {
    draw();
    const listener = vi.fn(); document.addEventListener("keydown", listener);
    act(() => { select().focus(); select().dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true })); });
    expect(document.activeElement).toBe(select());
    expect(select().tabIndex).toBe(0);
    expect(listener).not.toHaveBeenCalled();
    document.removeEventListener("keydown", listener);
  });
  it("retains an unavailable saved choice visibly without changing it", () => {
    draw({ ...choice, speed: "old" });
    expect(chip().dataset.on).toBe("true");
    expect(chip().title).toContain("next message uses Standard");
    expect(select().value).toBe("__saved_speed");
    expect(changes).toEqual([]);
  });
  it("allows reselecting a dormant saved tier even when the current model offers the same id", () => {
    draw({ ...choice, speed: "fast", speed_model: "claude-c" });
    expect(chip().title).toContain("next message uses Standard");
    expect(select().value).toBe("__saved_speed");
    act(() => { select().value = "fast"; select().dispatchEvent(new Event("change", { bubbles: true })); });
    expect(changes).toEqual(["fast"]);
  });
  it("keeps a dormant saved speed enabled on a tierless model so Standard can clear it", () => {
    draw({ ...choice, model: "claude-b", speed: "fast", speed_model: "claude-a" });
    expect(select().disabled).toBe(false);
    expect(select().selectedOptions[0].text).toBe("fast (not active on this model)");
    expect(chip().title).toContain("next message uses Standard");
    act(() => { select().value = ""; select().dispatchEvent(new Event("change", { bubbles: true })); });
    expect(changes).toEqual([null]);
  });
  it("shows a saved default-following thread's speed as dormant even at the bound default", () => {
    draw({ ...choice, model: null, speed: "fast", speed_model: "claude-a" });
    expect(select().value).toBe("__saved_speed");
    expect(chip().title).toContain("next message uses Standard");
  });
  it("includes the default model precondition for speed and Standard", () => {
    const before = { ...choice, model: null };
    expect(patchBody(before, { ...before, speed: "fast" }, models)).toEqual({ speed: "fast", speed_model: "claude-a" });
    expect(patchBody({ ...before, speed: "fast" }, before, models)).toEqual({ speed: null, speed_model: "claude-a" });
  });
  it("clears an incompatible speed in the same model write and retains compatible tiers", () => {
    const before = { ...choice, speed: "fast" };
    const after = applyModel(before, "claude-b", models);
    expect(after.speed).toBeNull();
    expect(patchBody(before, after)).toEqual({ model: "claude-b" });
    expect(applyModel(before, "claude-c", models).speed).toBe("fast");
    expect(patchBody(before, { ...before, speed: null })).toEqual({ speed: null, speed_model: "claude-a" });
    expect(threadBody(before)).toEqual({ provider: "claude", brief: { model: "claude-a", speed: "fast", speed_model: "claude-a" } });
  });
});
