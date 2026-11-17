import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { api } from "../api";
import { initialState } from "../state/store";
import { useThreadModel } from "./ThreadModelControls";
import type { ThreadModels } from "../lib/threadmodel";

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;

it("ignores older reload responses even when the newest request fails", async () => {
  const pending: { resolve: (value: ThreadModels) => void; reject: (reason: Error) => void }[] = [];
  const mock = vi.spyOn(api, "threadModels").mockImplementation(() => new Promise((resolve, reject) => {
    pending.push({ resolve, reject });
  }));
  let model!: ReturnType<typeof useThreadModel>;
  function Harness() {
    model = useThreadModel(initialState, () => {});
    return null;
  }
  const host = document.createElement("div");
  const root = createRoot(host);
  const old = { version: "old" } as unknown as ThreadModels;
  const fresh = { version: "fresh" } as unknown as ThreadModels;
  try {
    await act(async () => root.render(createElement(Harness)));
    let second!: Promise<void>;
    await act(async () => { second = model.reload(); });
    await act(async () => { pending[1].resolve(fresh); await second; });
    expect(model.models).toBe(fresh);
    await act(async () => { pending[0].resolve(old); });
    expect(model.models).toBe(fresh);
    await act(async () => { void model.reload(); void model.reload(); });
    await act(async () => { pending[3].reject(new Error("offline")); });
    await act(async () => { pending[2].resolve(old); });
    expect(model.models).toBe(fresh);
  } finally {
    await act(async () => root.unmount());
    mock.mockRestore();
  }
});
