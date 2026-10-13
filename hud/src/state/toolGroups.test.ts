import { toolGroups } from "./toolGroups";
it("notifies only the changed key, stays window-local, and evicts the oldest at 500", () => {
  for (const k of toolGroups.keys()) toolGroups.toggle(k);
  const a = vi.fn(), b = vi.fn();
  const unsub = toolGroups.subscribe("t|a", a), unsubB = toolGroups.subscribe("t|b", b);
  toolGroups.toggle("t|a"); expect(a).toHaveBeenCalledOnce(); expect(b).not.toHaveBeenCalled();
  for (let i = 0; i < 500; i++) toolGroups.toggle(`t|${i}`);
  expect(toolGroups.keys()).toHaveLength(500); expect(toolGroups.get("t|a")).toBe(false);
  expect(a).toHaveBeenCalledTimes(2); unsub(); unsubB();
  for (const k of toolGroups.keys()) toolGroups.toggle(k);
});

it("does not share disclosure state for a reused call id in another turn", () => {
  const a = toolGroups.key("thread", "same", "turn-a");
  const b = toolGroups.key("thread", "same", "turn-b");
  toolGroups.toggle(a); expect(toolGroups.get(a)).toBe(true); expect(toolGroups.get(b)).toBe(false);
  toolGroups.toggle(a);
});
