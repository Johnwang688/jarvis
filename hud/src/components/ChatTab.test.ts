import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
const counts = vi.hoisted(() => ({ message: 0, group: 0 }));
vi.mock("./Markdown", () => ({ Markdown: ({ text }: {text: string}) => { counts.message++; return text; } }));
vi.mock("../lib/toolCalls", async importOriginal => {
  const real = await importOriginal<typeof import("../lib/toolCalls")>();
  return { ...real, groupTitle: (...args: Parameters<typeof real.groupTitle>) => { counts.group++; return real.groupTitle(...args); } };
});
import { ChatTab } from "./ChatTab";
import { applyStart } from "../lib/toolCalls";
import { toolGroups } from "../state/toolGroups";
import type { ChatMessage, ToolCall } from "../types";
(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
const call = (i: number): ToolCall => ({ call_id: String(i), turn_id: "t", name: "Read", label: `<img src=x onerror=boom> ${i}`,
  at: "2026-10-13T00:00:00Z", outcome: "ok", finished_at: "2026-10-13T00:00:03Z" });
it("memoizes rows at scale and redraws only the changed group or disclosure", () => {
  const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
  let messages: ChatMessage[] = [];
  for (let group = 0; group < 200; group++) {
    messages.push({ role: "assistant", text: `reply ${group}` });
    for (let i = 0; i < 10; i++) messages = applyStart(messages, call(group * 10 + i));
  }
  const cancel = () => {};
  const render = () => act(() => root.render(createElement(ChatTab, { messages, draft: "", onCancelTask: cancel })));
  try {
    render(); counts.group = counts.message = 0;
    for (let i = 0; i < 20; i++) render();
    expect(counts).toEqual({ message: 0, group: 0 });
    messages = applyStart(messages, call(2000)); render();
    expect(counts).toEqual({ message: 0, group: 1 });
    counts.group = 0;
    act(() => host.querySelector<HTMLButtonElement>('[data-testid="tool-group"]')!.click());
    expect(counts).toEqual({ message: 0, group: 1 });
    expect(host.querySelectorAll('[data-testid="tool-call"]')).toHaveLength(10);
    expect(host.querySelectorAll('img')).toHaveLength(0);
  } finally { act(() => root.unmount()); host.remove(); for (const k of toolGroups.keys()) toolGroups.toggle(k); }
});
it("windows an opened 2000-call group and End reveals its last call", () => {
  const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
  const messages: ChatMessage[] = [{ role: "tool", text: "", turn_id: "t", calls: Array.from({ length: 2000 }, (_, i) => call(i)) }];
  try {
    act(() => root.render(createElement(ChatTab, { messages, draft: "", onCancelTask: () => {} })));
    const button = host.querySelector<HTMLButtonElement>('[data-testid="tool-group"]')!;
    expect(button.getAttribute("aria-expanded")).toBe("false");
    expect(host.querySelectorAll('[data-testid="tool-call"]')).toHaveLength(0);
    act(() => button.click());
    expect(host.querySelectorAll('[data-testid="tool-call"]').length).toBeLessThanOrEqual(24);
    const list = host.querySelector('ul')!;
    act(() => list.dispatchEvent(new KeyboardEvent("keydown", { key: "End", bubbles: true })));
    expect(host.querySelectorAll('[data-testid="tool-call"]').length).toBeLessThanOrEqual(24);
    expect(host.querySelector('[data-testid="tool-call"]:last-child')?.textContent).toContain("1999");
  } finally { act(() => root.unmount()); host.remove(); for (const k of toolGroups.keys()) toolGroups.toggle(k); }
});
