import { act, createElement, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { api } from "../api";
import { initialState, reduce, type Action, type State } from "../state/store";
import type { Thread } from "../types";
import { applyModel, type Choice, type ThreadModels } from "../lib/threadmodel";
import { useThreadModel } from "./ThreadModelControls";

const captured = vi.hoisted(() => ({ model: null as any, speed: null as any }));
vi.mock("./ModelChip", () => ({
  ModelChip: (props: any) => { captured.model = props; return null; },
  CatalogPicker: () => null, ProviderDefaults: () => null,
}));
(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
const models: ThreadModels = { providers: { codex: { label: "Codex", default: "gpt-a", default_effort: "high", models: [
  { id: "gpt-a", efforts: ["high", "low"], speeds: [["priority", "Fast", "More quota"]] },
  { id: "gpt-b", efforts: ["high", "low"], speeds: [["priority", "Fast", "More quota"]] },
] } } };
const thread = { id: "t1", project_id: "p1", role: "chat", provider: "codex", model: "gpt-a",
  effort: null, speed: null, speed_model: null } as Thread;
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((a, b) => { resolve = a; reject = b; });
  return { promise, resolve, reject };
}

describe("speed ordering with model and effort writes", () => {
  let host: HTMLDivElement;
  let root: Root;
  const select = () => host.querySelector<HTMLSelectElement>("select")!;
  const chip = () => host.querySelector<HTMLElement>('[data-testid="speed-chip"]')!;
  function Harness() {
    const [state, setState] = useState<State>({ ...initialState, threads: [thread],
      chats: { ...initialState.chats, 1: { ...initialState.chats[1], threadId: "t1", compose: null } } });
    const controls = useThreadModel(state, (action: Action) => setState((s) => reduce(s, action)));
    const chips = controls.chipFor(1);
    captured.speed = chips?.props.children[1].props.onChange;
    return chips;
  }
  beforeEach(async () => {
    vi.spyOn(api, "threadModels").mockResolvedValue(models);
    host = document.createElement("div"); document.body.append(host); root = createRoot(host);
    await act(async () => root.render(createElement(Harness)));
  });
  afterEach(() => { act(() => root.unmount()); host.remove(); vi.restoreAllMocks(); });

  it("blocks old-model speed events until the model response lands, then sends the new model precondition", async () => {
    const modelWrite = deferred<Thread>();
    const patch = vi.spyOn(api, "setThreadModel").mockReturnValueOnce(modelWrite.promise)
      .mockResolvedValueOnce({ ...thread, model: "gpt-b", speed: "priority", speed_model: "gpt-b" });
    act(() => {
      const before = captured.model.choice as Choice;
      captured.model.onChange(applyModel(before, "gpt-b", models));
      // Force an old-model event in the same batch, before disabled is drawn.
      select().value = "priority"; select().dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(patch.mock.calls).toEqual([["t1", { model: "gpt-b" }]]);
    expect(select().disabled).toBe(true);
    expect(chip().dataset.pending).toBe("true");
    expect(chip().title).toContain("pending");
    expect(host.querySelector(".err")).toBeNull();
    await act(async () => modelWrite.resolve({ ...thread, model: "gpt-b" }));
    expect(select().disabled).toBe(false);
    expect(chip().dataset.pending).toBeUndefined();
    await act(async () => {
      select().value = "priority"; select().dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(patch.mock.calls[1]).toEqual(["t1", { speed: "priority", speed_model: "gpt-b" }]);
  });

  it("keeps an old render's speed callback a speed write with the old precondition", async () => {
    const oldSpeed = captured.speed;
    const modelWrite = deferred<Thread>();
    const patch = vi.spyOn(api, "setThreadModel").mockReturnValueOnce(modelWrite.promise)
      .mockRejectedValueOnce(new Error("409: model changed"));
    act(() => { captured.model.onChange(applyModel(captured.model.choice, "gpt-b", models)); });
    await act(async () => modelWrite.resolve({ ...thread, model: "gpt-b" }));
    await act(async () => oldSpeed("priority"));
    expect(patch.mock.calls[1]).toEqual(["t1", { speed: "priority", speed_model: "gpt-a" }]);
    expect(select().value).toBe("");
    expect(select().disabled).toBe(false);
  });

  it("keeps speed pending until every effort write settles, including a refusal", async () => {
    const first = deferred<Thread>(), second = deferred<Thread>();
    const patch = vi.spyOn(api, "setThreadModel").mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    act(() => {
      captured.model.onChange({ ...captured.model.choice, effort: "low" });
      captured.model.onChange({ ...captured.model.choice, effort: "high" });
    });
    expect(patch).toHaveBeenCalledTimes(2);
    expect(select().disabled).toBe(true);
    await act(async () => second.resolve({ ...thread, effort: "high" }));
    expect(select().disabled).toBe(true);
    await act(async () => first.reject(new Error("refused")));
    expect(select().disabled).toBe(false);
    expect(chip().dataset.pending).toBeUndefined();
  });
});
