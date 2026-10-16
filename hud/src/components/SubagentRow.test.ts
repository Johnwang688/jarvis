import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
const counts = vi.hoisted(() => ({ rows: 0 }));
// jsdom cannot make a trusted event: `trust.force` stands in for the owner's
// own press, so the rest of the Stop path runs as it would (SV-4).
const trust = vi.hoisted(() => ({ force: false }));
vi.mock("../lib/subagents", async (importOriginal) => {
  const real = await importOriginal<typeof import("../lib/subagents")>();
  // statusMark runs once per row drawn: it counts the rows' renders.
  return { ...real, statusMark: (...args: Parameters<typeof real.statusMark>) => { counts.rows++; return real.statusMark(...args); },
    isOwnersPress: (e: { isTrusted: boolean; button: number }) => trust.force || real.isOwnersPress(e) };
});
import { ChatTab } from "./ChatTab";
import { subagents } from "../state/subagents";
import { InputBar } from "./InputBar";
import { MAIN_STOP_CLAUDE_HELP, STOP_HELP } from "../lib/subagents";
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

describe("the owner's Stop on a row (SV-4)", () => {
  /** The list says `rows`; a POST to a child's stop is answered `answer` (status, body), and recorded. */
  function serveStop(rows: unknown[], answer: [number, unknown] = [200, { ok: true, stop: "stopping" }]) {
    const posts: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (path: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        posts.push(path);
        return new Response(JSON.stringify(answer[1]), { status: answer[0] });
      }
      return new Response(JSON.stringify({ subagents: rows }), { status: 200 });
    }));
    return posts;
  }
  const stopOf = (host: HTMLElement, sid: string) =>
    host.querySelector<HTMLElement>(`[data-sid="${sid}"]`)!.querySelector<HTMLButtonElement>('[data-testid="subagent-stop"]');
  afterEach(() => { trust.force = false; subagents.setBlocked(false); });

  it("a live row offers Stop beside its open button, with the provider's hover help", async () => {
    serveStop([listRow("cl:toolu_a", { stoppable: true }), listRow("cx:thr", { stoppable: true, started_at: at(3) }),
      listRow("cl:toolu_b", { status: "done", stoppable: true })]);
    const { host, done } = await mount("st1", spawn(["toolu_a", "toolu_b"]));
    try {
      const claude = stopOf(host, "cl:toolu_a")!;
      expect(claude.textContent).toBe("Stop");
      expect(claude.title).toBe(STOP_HELP.claude);
      expect(claude.getAttribute("aria-label")).toBe("Stop sub-agent <b>cl:toolu_a</b>");
      expect(claude.closest(".sa-open")).toBeNull();               // a sibling, never nested
      const codex = stopOf(host, "cx:thr")!;
      expect(codex.title).toContain("A command it is already running may still finish");
      expect(codex.title).toContain("the orchestrator's model is not told");
      expect(stopOf(host, "cl:toolu_b")).toBeNull();              // ended: no Stop
    } finally { done(); }
  });

  it("a script's click sends nothing; the owner's press sends one request and shows stopping…", async () => {
    const posts = serveStop([listRow("cl:toolu_a", { stoppable: true })]);
    const opened: unknown[] = [];
    subagents.setOpener((ref) => opened.push(ref));
    const { host, done } = await mount("st2", spawn(["toolu_a"]));
    try {
      await act(async () => { stopOf(host, "cl:toolu_a")!.click(); });       // untrusted
      expect(posts).toEqual([]);
      trust.force = true;
      await act(async () => { stopOf(host, "cl:toolu_a")!.click(); await new Promise((r) => setTimeout(r, 0)); });
      expect(posts).toEqual(["/threads/st2/subagents/cl%3Atoolu_a/stop"]);
      const button = stopOf(host, "cl:toolu_a")!;
      expect(button.textContent).toBe("stopping…");
      // aria-disabled, not disabled: a keyboard press keeps its focus (review of #216).
      expect(button.getAttribute("aria-disabled")).toBe("true");
      expect(button.disabled).toBe(false);
      expect(button.dataset.state).toBe("stopping");
      await act(async () => { button.click(); });
      expect(posts.length).toBe(1);
      expect(opened).toEqual([]);                                  // Stop never opens the pane
      act(() => subagents.heard("st2", { sid: "cl:toolu_a", op: "finished", status: "stopped", depth: 1,
        background: false, started_at: at(1), entries: 1, ended_at: at(9) }));
      expect(stopOf(host, "cl:toolu_a")).toBeNull();
    } finally { done(); subagents.setOpener(null); }
  });

  it("under an approval card Stop is disabled and even the owner's press sends nothing", async () => {
    const posts = serveStop([listRow("cl:toolu_a", { stoppable: true })]);
    const { host, done } = await mount("st3", spawn(["toolu_a"]));
    try {
      act(() => subagents.setBlocked(true));
      const button = stopOf(host, "cl:toolu_a")!;
      expect(button.disabled).toBe(true);
      trust.force = true;
      button.disabled = false;                                     // even if it were pressed
      await act(async () => { button.click(); });
      expect(posts).toEqual([]);
      act(() => subagents.setBlocked(false));
      expect(stopOf(host, "cl:toolu_a")!.disabled).toBe(false);
    } finally { done(); }
  });

  it("can't stop says why; a refusal shows the daemon's sentence", async () => {
    serveStop([listRow("cl:toolu_a", { stoppable: false, stop_unavailable: "Claude Code can stop this sub-agent only by stopping the whole turn." }),
      listRow("cl:toolu_b", { stoppable: true })],
    [409, { error: "This sub-agent has already ended.", stale: true }]);
    const { host, done } = await mount("st4", spawn(["toolu_a", "toolu_b"]));
    try {
      const cant = host.querySelector<HTMLElement>('[data-sid="cl:toolu_a"] [data-testid="subagent-cant-stop"]')!;
      expect(cant.textContent).toBe("Can't stop");
      expect(cant.title).toContain("whole turn");
      // Reachable by keyboard and screen reader: focusable, described by its reason.
      expect(cant.tabIndex).toBe(0);
      expect(document.getElementById(cant.getAttribute("aria-describedby")!)!.textContent).toContain("whole turn");
      trust.force = true;
      await act(async () => { stopOf(host, "cl:toolu_b")!.click(); await new Promise((r) => setTimeout(r, 0)); });
      expect(host.querySelector('[data-sid="cl:toolu_b"] [data-testid="subagent-stop-error"]')!.textContent)
        .toBe("This sub-agent has already ended.");
    } finally { done(); }
  });
});

describe("the thread's own Stop (SV-4)", () => {
  it("on a Claude chat its hover help says it also stops background sub-agents", () => {
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);
    try {
      act(() => root.render(createElement(InputBar, { pendingTranscript: "", onSend: () => {}, onTranscriptTaken: () => {},
        running: true, stoppable: true, stopHint: MAIN_STOP_CLAUDE_HELP, onStop: () => {} })));
      expect(host.querySelector<HTMLButtonElement>('[data-testid="stop"]')!.title).toBe(MAIN_STOP_CLAUDE_HELP);
      act(() => root.render(createElement(InputBar, { pendingTranscript: "", onSend: () => {}, onTranscriptTaken: () => {},
        running: true, stoppable: true, onStop: () => {} })));
      expect(host.querySelector<HTMLButtonElement>('[data-testid="stop"]')!.title)
        .toBe("Stop the running turn. Anything queued behind it comes back to this box.");
    } finally { act(() => root.unmount()); host.remove(); }
  });
});

describe("review of #216", () => {
  function serveStop(rows: unknown[], answer: [number, unknown]) {
    const posts: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (path: string, init?: RequestInit) => {
      if (init?.method === "POST") { posts.push(path); return new Response(JSON.stringify(answer[1]), { status: answer[0] }); }
      return new Response(JSON.stringify({ subagents: rows }), { status: 200 });
    }));
    return posts;
  }
  afterEach(() => { trust.force = false; });

  it("a keyboard press keeps focus on the button while it says stopping…", async () => {
    serveStop([listRow("cl:toolu_k", { stoppable: true })], [200, { ok: true, stop: "stopping" }]);
    const { host, done } = await mount("rv1", spawn(["toolu_k"]));
    try {
      const button = host.querySelector<HTMLButtonElement>('[data-sid="cl:toolu_k"] [data-testid="subagent-stop"]')!;
      button.focus();
      trust.force = true;
      await act(async () => { button.click(); await new Promise((r) => setTimeout(r, 0)); });
      const now = host.querySelector<HTMLButtonElement>('[data-sid="cl:toolu_k"] [data-testid="subagent-stop"]')!;
      expect(now.textContent).toBe("stopping…");
      expect(document.activeElement).toBe(now);
    } finally { done(); }
  });

  it("the CLI's refusal is a note, its reason reachable, and no Stop again", async () => {
    const posts = serveStop([listRow("cl:toolu_r", { stoppable: true })],
      [409, { error: "The CLI refused the stop: No task found with ID: ta.", refused: true }]);
    const { host, done } = await mount("rv2", spawn(["toolu_r"]));
    try {
      trust.force = true;
      const button = host.querySelector<HTMLButtonElement>('[data-sid="cl:toolu_r"] [data-testid="subagent-stop"]')!;
      await act(async () => { button.click(); await new Promise((r) => setTimeout(r, 0)); });
      expect(posts.length).toBe(1);
      expect(host.querySelector('[data-sid="cl:toolu_r"] [data-testid="subagent-stop"]')).toBeNull();
      const note = host.querySelector<HTMLElement>('[data-sid="cl:toolu_r"] [data-testid="subagent-stop-refused"]')!;
      expect(note.textContent).toBe("Stop refused");
      expect(note.tabIndex).toBe(0);
      expect(document.getElementById(note.getAttribute("aria-describedby")!)!.textContent).toContain("No task found");
    } finally { done(); }
  });
});
