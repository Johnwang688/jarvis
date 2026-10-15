import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
const counts = vi.hoisted(() => ({ rows: 0 }));
vi.mock("../lib/subagents", async (importOriginal) => {
  const real = await importOriginal<typeof import("../lib/subagents")>();
  // statusMark runs once per row drawn: it counts the rows' renders.
  return { ...real, statusMark: (...args: Parameters<typeof real.statusMark>) => { counts.rows++; return real.statusMark(...args); } };
});
import { ChatTab } from "./ChatTab";
import { subagents } from "../state/subagents";
import type { ChatMessage, Thread } from "../types";
(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;

const at = (s: number) => new Date(Date.parse("2026-10-15T10:00:00Z") + s * 1000).toISOString();
const listRow = (sid: string, over: Record<string, unknown> = {}) => ({
  sid, status: "running", depth: 1, background: false, name: `<b>${sid}</b>‮`, kind: "general-purpose",
  started_at: at(1), entries: 0, tool_uses: 2, tokens: 18200, last_tool: "Bash", ...over,
});

function serve(rows: unknown[]) {
  const seen: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (path: string) => {
    seen.push(path);
    return new Response(JSON.stringify({ subagents: rows }), { status: 200 });
  }));
  return seen;
}

async function mount(threadId: string, messages: ChatMessage[]) {
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  const thread = { id: threadId, title: "t", provider: "claude" } as unknown as Thread;
  await act(async () => { root.render(createElement(ChatTab, { thread, messages, draft: "", onCancelTask: () => {} })); });
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  return { host, done: () => { act(() => root.unmount()); host.remove(); } };
}

afterEach(() => vi.unstubAllGlobals());

const spawn = (ids: string[]): ChatMessage[] => [
  { role: "user", text: "go", at: at(0) },
  { role: "tool", text: "", turn_id: "t", calls: ids.map((id) => ({ call_id: id, turn_id: "t", name: "Agent", label: id, outcome: "ok" as const, at: at(1) })) },
  { role: "assistant", text: "launched", at: at(2) },
];

it("draws a row per child right after its spawn call, as text, and opens it on a click", async () => {
  const seen = serve([listRow("cl:toolu_a"), listRow("cl:toolu_b", { status: "failed" }),
    listRow("cx:thread1", { started_at: at(3), kind: "" })]);
  const opened: unknown[] = [];
  subagents.setOpener((ref) => opened.push(ref));
  const { host, done } = await mount("th1", spawn(["toolu_a", "toolu_b"]));
  try {
    expect(seen).toEqual(["/threads/th1/subagents"]);
    const log = host.querySelector('[data-testid="log"]')!;
    const order = Array.from(log.children).map((el) => el.getAttribute("data-testid") === "subagent-rows"
      ? Array.from(el.querySelectorAll<HTMLElement>('[data-testid="subagent-row"]')).map((r) => r.dataset.sid).join("+")
      : el.getAttribute("data-testid") ?? el.className);
    // user · tool group · its two rows · assistant · the Codex row (by time, after it)
    expect(order).toEqual(["msg-user", "tool-group", "cl:toolu_a+cl:toolu_b", "msg-assistant", "cx:thread1"]);
    const rows = host.querySelectorAll<HTMLElement>('[data-testid="subagent-row"]');
    expect(Array.from(rows).map((r) => [r.dataset.sid, r.dataset.status, r.dataset.provider])).toEqual([
      ["cl:toolu_a", "running", "claude"], ["cl:toolu_b", "failed", "claude"], ["cx:thread1", "running", "codex"]]);
    expect(host.querySelectorAll("b")).toHaveLength(0);
    expect(rows[0].textContent).toContain("2 tools");
    expect(rows[0].textContent).toContain("18k tokens");
    expect(rows[0].textContent).not.toContain("‮");
    expect(rows[1].querySelector(".sa-status")!.className).toContain("failed");
    const button = rows[0].querySelector("button")!;
    expect(button.getAttribute("aria-label")).toBe("Open sub-agent <b>cl:toolu_a</b> in a pane");
    act(() => button.click());
    expect(opened).toEqual([{ threadId: "th1", sid: "cl:toolu_a" }]);
  } finally { done(); subagents.setOpener(null); }
});

it("one progress tick redraws its own row only", async () => {
  serve([listRow("cl:toolu_a"), listRow("cl:toolu_b")]);
  const { host, done } = await mount("th2", spawn(["toolu_a", "toolu_b"]));
  try {
    counts.rows = 0;
    act(() => subagents.heard("th2", { sid: "cl:toolu_a", op: "progress", status: "running", depth: 1,
      background: false, started_at: at(1), entries: 3 }));
    expect(counts.rows).toBe(1);
    act(() => subagents.heard("th2", { sid: "cl:toolu_b", op: "finished", status: "done", depth: 1,
      background: false, started_at: at(1), entries: 1, ended_at: at(9) }));
    expect(counts.rows).toBe(2);
    expect(host.querySelector<HTMLElement>('[data-sid="cl:toolu_b"]')!.dataset.status).toBe("done");
  } finally { done(); }
});

it("more than six at one spot collapse into one line; Enter and Space open it", async () => {
  const ids = Array.from({ length: 8 }, (_, i) => `toolu_${i}`);
  serve(ids.map((id, i) => listRow("cl:" + id, { status: i < 5 ? "running" : "done" })));
  const { host, done } = await mount("th3", spawn(ids));
  try {
    const toggle = host.querySelector<HTMLButtonElement>('[data-testid="subagent-block-toggle"]')!;
    expect(toggle.textContent).toContain("Sub-agents · 8 (5 running)");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(host.querySelectorAll('[data-testid="subagent-row"]')).toHaveLength(0);
    act(() => toggle.click());       // a native button: Enter and Space click it
    expect(host.querySelectorAll('[data-testid="subagent-row"]')).toHaveLength(8);
    act(() => toggle.click());
    expect(host.querySelectorAll('[data-testid="subagent-row"]')).toHaveLength(0);
  } finally { done(); }
  // Six stay rows.
  serve(ids.slice(0, 6).map((id) => listRow("cl:" + id)));
  const six = await mount("th4", spawn(ids.slice(0, 6)));
  try {
    expect(six.host.querySelectorAll('[data-testid="subagent-row"]')).toHaveLength(6);
    expect(six.host.querySelector('[data-testid="subagent-block"]')).toBeNull();
  } finally { six.done(); }
});

it("a nested child is not drawn in the parent's transcript", async () => {
  serve([listRow("cl:toolu_a"), listRow("cl:toolu_inner", { parent_sid: "cl:toolu_a", depth: 2 })]);
  const { host, done } = await mount("th5", spawn(["toolu_a"]));
  try {
    expect(Array.from(host.querySelectorAll<HTMLElement>('[data-testid="subagent-row"]')).map((r) => r.dataset.sid))
      .toEqual(["cl:toolu_a"]);
  } finally { done(); }
});
