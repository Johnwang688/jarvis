import { act, createElement, StrictMode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { initialState, reduce, StoreProvider, useStore, type Action } from "./store";
(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
let current: ReturnType<typeof useStore>;
function ReadStore() { current = useStore(); return null; }
afterEach(() => vi.restoreAllMocks());
it("replays a notice action using its issue time after the wall clock advances", () => {
  const action = { type: "notice", text: "Words are kept.", issuedAt: 1200 } as Action;
  const first = reduce(initialState, action);
  vi.spyOn(Date, "now").mockReturnValue(9000);
  expect(reduce(initialState, action)).toEqual(first);
  expect(first.errorAt).toBe(1200);
});
it("joins or replaces location notices according to the action time, and clears errors", () => {
  const start = { ...initialState, error: "Earlier failure", errorAt: 1000 };
  const draft = { ...start, chats: { ...start.chats, 1: { ...start.chats[1], compose: { id: "c1", projectId: null }, input: "draft" } } };
  const close = (issuedAt: number) => reduce(draft, { type: "release_chat", pane: 1, keep: true, issuedAt } as Action);
  vi.spyOn(Date, "now").mockReturnValue(50_000);
  expect(close(1500).error).toContain("Earlier failure");
  expect(close(3000).error).not.toContain("Earlier failure");
  const clear = reduce(start, { type: "patch", patch: { error: "" }, issuedAt: 4000 } as Action);
  expect([clear.error, clear.errorAt]).toEqual(["", 0]);
});
it("uses deterministic zero for an unstamped legacy action and preserves explicit errorAt", () => {
  vi.spyOn(Date, "now").mockImplementation(() => { throw new Error("reducer read clock"); });
  expect(reduce(initialState, { type: "patch", patch: { error: "Legacy" } }).errorAt).toBe(0);
  expect(reduce(initialState, { type: "patch", patch: { error: "Explicit", errorAt: 42 } }).errorAt).toBe(42);
});
it("stamps at the stable production dispatch boundary before React reduces it", async () => {
  const host = document.createElement("div"); document.body.appendChild(host);
  const root: Root = createRoot(host); const clock = vi.spyOn(Date, "now").mockReturnValue(1000);
  try {
    await act(async () => root.render(createElement(StrictMode, null, createElement(StoreProvider, { children: createElement(ReadStore) }))));
    const dispatch = current!.dispatch;
    await act(async () => { dispatch({ type: "patch", patch: { error: "At dispatch" } }); clock.mockReturnValue(8000); });
    expect(current!.state.errorAt).toBe(1000);
    expect(current!.dispatch).toBe(dispatch);
    await act(async () => dispatch({ type: "notice", text: "Explicit zero", issuedAt: 0 } as Action));
    expect(current!.state.errorAt).toBe(0);
  } finally { await act(async () => root.unmount()); host.remove(); }
});
