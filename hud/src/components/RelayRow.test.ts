// C5 (chat-to-chat plan §7 "Rendering"): one row for a message one chat sent
// another — the receiving side, the sending side and the reply. Tests first,
// by the writer; every case loads the component itself, so on today's code
// each fails alone with "Cannot find module './RelayRow'".
//
// THE INTERFACE (see also the header of src/lib/relays.test.ts)
// --------------------------------------------------------------
// `hud/src/components/RelayRow.tsx` exports
//   RelayRow(props: {
//     msg: ChatMessage;                 // role "relay", `relay` set
//     blocked?: boolean;                // an approval card is up: every button
//                                       // disabled, and a click does nothing
//     onOpen?: (threadId: string) => void;      // "Open": the peer chat beside this one
//     onWithdraw?: (messageId: string) => void; // the owner's Withdraw (queued out rows)
//   })
// and ChatTab's MessageRow draws it for every `role: "relay"` message (ChatTab
// passes the pane's card state and both callbacks; chosen: as props).
//
// DOM (test ids and classes the face check reads too):
//   root   <div class="msg relay-in|relay-out|relay-reply"
//               data-testid="msg-relay-in|msg-relay-out|msg-relay-reply"
//               data-message-id=… data-state=…>   — never class "user", and
//          nothing in it takes focus on mount (no autoFocus).
//   head   `.who`:  in    "FROM " + badge + " · " + peerLabel(title)
//                   out   "↪ SENT TO " + peerLabel(title) + the state chip
//                   reply "↩ REPLY FROM " + badge + " · " + peerLabel(title)
//          never the owner's "YOU", never "JARVIS" alone.
//   badge  <span class="badge <providerClass>"> providerWord </span>
//   chip   <span data-testid="relay-state" class="relay-state <stateTone>"> stateWords </span>
//          (out rows only)
//   body   in, out: <div class="body user-text"> plain text (never Markdown);
//          out: also class "collapsed" (3 lines) with a `relay-expand` button
//          (aria-expanded) when the text is over 3 lines or 240 characters.
//          reply: the sanitising <Markdown> (lib/markdown), class "body".
//   in     <button data-testid="relay-open">Open</button>, calls onOpen(peer
//          thread id) on a click; and once state is "answered" a muted line
//          <div data-testid="relay-answered">↩ reply sent to ‹title›</div>.
//   out    while state is "running" and progress is known, a muted
//          <div data-testid="relay-progress"> with the excerpt (plain text);
//          while state is "queued", <button data-testid="relay-withdraw">Withdraw</button>.
//   reply  a muted footer <div data-testid="relay-delivery"> deliveryWords,
//          absent when there are none.
//
// TEST -> MUTATION: [raw title] "a hostile title is cleaned text in every head".

import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import type { ChatMessage, Thread } from "../types";
(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;

const ROW = "./RelayRow";
const load = async (): Promise<any> => import(/* @vite-ignore */ ROW);

const AT = "2026-10-19T10:00:00+00:00";
const msg = (dir: "in" | "out" | "reply", over: Record<string, unknown> = {}, text = "please check the build",
  peer: Record<string, unknown> = {}): ChatMessage => ({
  role: "relay", text, at: AT,
  relay: { dir, message_id: "m0123456789ab", state: dir === "out" ? "queued" : "running", status: null, delivery: null,
    peer: { thread_id: "peer1234", title: "Frontend", provider: "claude", ...peer }, ...over },
} as unknown as ChatMessage);

async function mount(props: Record<string, unknown>) {
  const { RelayRow } = await load();
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  await act(async () => { root.render(createElement(RelayRow, props)); });
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  return {
    host,
    rerender: async (next: Record<string, unknown>) => { await act(async () => { root.render(createElement(RelayRow, next)); }); },
    done: () => { act(() => root.unmount()); host.remove(); },
  };
}
const q = (host: HTMLElement, id: string) => host.querySelector<HTMLElement>(`[data-testid="${id}"]`);

afterEach(() => { document.body.innerHTML = ""; vi.unstubAllGlobals(); });

describe("the receiving side", () => {
  it("FROM <badge> · title, a plain body, never the owner's", async () => {
    const { host, done } = await mount({ msg: msg("in", {}, "**not bold** <b>x</b>", { provider: "fast", title: "Desk chat" }) });
    try {
      const el = q(host, "msg-relay-in")!;
      expect(el).not.toBeNull();
      expect(el.classList.contains("msg")).toBe(true);
      expect(el.classList.contains("relay-in")).toBe(true);
      expect(el.classList.contains("user")).toBe(false);
      expect(el.dataset.messageId).toBe("m0123456789ab");
      const who = el.querySelector(".who")!;
      expect(who.textContent).toBe("FROM Jarvis · Desk chat");
      expect(who.textContent).not.toContain("YOU");
      const badge = who.querySelector(".badge")!;
      expect(Array.from(badge.classList).sort()).toEqual(["badge", "fast"]);
      const body = el.querySelector(".body.user-text")!;
      expect(body.textContent).toBe("**not bold** <b>x</b>");
      expect(el.querySelector("strong, b")).toBeNull();
      expect(document.activeElement === document.body || document.activeElement === null).toBe(true);
    } finally { done(); }
  });

  it("Open calls onOpen with the peer's thread on a click, and is dead under a card", async () => {
    const opened: string[] = [];
    const props = { msg: msg("in"), onOpen: (id: string) => opened.push(id) };
    const { host, rerender, done } = await mount(props);
    try {
      const open = q(host, "relay-open") as HTMLButtonElement;
      expect(open.tagName).toBe("BUTTON");
      expect(document.activeElement).not.toBe(open);
      act(() => open.click());
      expect(opened).toEqual(["peer1234"]);
      await rerender({ ...props, blocked: true });
      const dead = q(host, "relay-open") as HTMLButtonElement;
      expect(dead.disabled).toBe(true);
      act(() => dead.click());
      expect(opened).toEqual(["peer1234"]);
    } finally { done(); }
  });

  it("after the turn, a muted '↩ reply sent to' line; none before", async () => {
    const before = await mount({ msg: msg("in") });
    try { expect(q(before.host, "relay-answered")).toBeNull(); } finally { before.done(); }
    const after = await mount({ msg: msg("in", { state: "answered", status: "answered" }, "x", { title: "Desk chat" }) });
    try { expect(q(after.host, "relay-answered")!.textContent).toBe("↩ reply sent to Desk chat"); } finally { after.done(); }
  });
});

describe("the sending side", () => {
  it("↪ SENT TO title with its state chip; the body is plain and collapsed", async () => {
    const long = Array.from({ length: 8 }, (_, i) => `line ${i} of the message`).join("\n");
    const { host, done } = await mount({ msg: msg("out", {}, long) });
    try {
      const el = q(host, "msg-relay-out")!;
      expect(el.classList.contains("relay-out")).toBe(true);
      expect(el.classList.contains("user")).toBe(false);
      expect(el.dataset.state).toBe("queued");
      const who = el.querySelector(".who")!;
      expect(who.textContent!.startsWith("↪ SENT TO Frontend")).toBe(true);
      expect(who.textContent).not.toContain("YOU");
      const chip = q(host, "relay-state")!;
      expect(chip.textContent).toBe("queued");
      expect(chip.classList.contains("relay-state")).toBe(true);
      expect(chip.classList.contains("pending")).toBe(true);
      const body = el.querySelector(".body.user-text")!;
      expect(body.textContent).toBe(long);
      expect(body.classList.contains("collapsed")).toBe(true);
      const more = q(host, "relay-expand") as HTMLButtonElement;
      expect(more.getAttribute("aria-expanded")).toBe("false");
      act(() => more.click());
      expect(el.querySelector(".body.user-text")!.classList.contains("collapsed")).toBe(false);
      expect((q(host, "relay-expand") as HTMLButtonElement).getAttribute("aria-expanded")).toBe("true");
    } finally { done(); }
  });

  it("each state's chip: words and tone", async () => {
    const cases: [Record<string, unknown>, string, string][] = [
      [{ state: "queued", position: 2 }, "queued · 2nd", "pending"],
      [{ state: "running" }, "working", "working"],
      [{ state: "answered", status: "answered" }, "answered", "done"],
      [{ state: "not delivered" }, "not delivered", "muted"],
      [{ state: "withdrawn", reason: "the owner withdrew it" }, "not delivered · the owner withdrew it", "muted"],
      [{ state: "failed", status: "failed" }, "failed", "error"],
    ];
    for (const [over, words, tone] of cases) {
      const { host, done } = await mount({ msg: msg("out", over) });
      try {
        const chip = q(host, "relay-state")!;
        expect(chip.textContent).toBe(words);
        expect(["pending", "working", "done", "muted", "error"].filter((t) => chip.classList.contains(t))).toEqual([tone]);
      } finally { done(); }
    }
  });

  it("working shows the latest excerpt, muted and plain, beneath", async () => {
    const { host, done } = await mount({ msg: msg("out", { state: "running", progress: "Looking <i>now</i>" }) });
    try {
      const p = q(host, "relay-progress")!;
      expect(p.textContent).toBe("Looking <i>now</i>");
      expect(p.querySelector("i")).toBeNull();
    } finally { done(); }
  });

  it("Withdraw only while queued; one call per click; disabled and inert under a card", async () => {
    const withdrawn: string[] = [];
    const props = { msg: msg("out"), onWithdraw: (id: string) => withdrawn.push(id) };
    const { host, rerender, done } = await mount(props);
    try {
      const button = q(host, "relay-withdraw") as HTMLButtonElement;
      expect(button.disabled).toBe(false);
      act(() => button.click());
      expect(withdrawn).toEqual(["m0123456789ab"]);
      await rerender({ ...props, blocked: true });
      const dead = q(host, "relay-withdraw") as HTMLButtonElement;
      expect(dead.disabled).toBe(true);
      act(() => dead.click());
      expect(withdrawn).toEqual(["m0123456789ab"]);
      for (const state of ["running", "answered", "withdrawn", "not delivered"]) {
        await rerender({ ...props, msg: msg("out", { state }) });
        expect(q(host, "relay-withdraw")).toBeNull();
      }
    } finally { done(); }
  });
});

describe("the reply", () => {
  it("↩ REPLY FROM <badge> · title, sanitised Markdown, a footer in words", async () => {
    const text = "**Green.** All 40 pass.\n\n<script>window.__pwned = 1</script><img src=x onerror=\"window.__pwned=1\">";
    const { host, done } = await mount({ msg: msg("reply", { state: "answered", status: "answered", delivery: "turn" }, text,
      { provider: "codex", title: "Backend" }) });
    try {
      await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
      const el = q(host, "msg-relay-reply")!;
      expect(el.classList.contains("relay-reply")).toBe(true);
      expect(el.classList.contains("user")).toBe(false);
      const who = el.querySelector(".who")!;
      expect(who.textContent).toBe("↩ REPLY FROM Codex · Backend");
      expect(Array.from(who.querySelector(".badge")!.classList).sort()).toEqual(["badge", "codex"]);
      expect(el.querySelector("strong")?.textContent).toBe("Green.");
      expect(el.querySelector("script")).toBeNull();
      expect(el.querySelector("img[onerror]")).toBeNull();
      expect((window as any).__pwned).toBeUndefined();
      expect(q(host, "relay-delivery")!.textContent).toBe("started a turn to report it");
    } finally { done(); }
  });

  it("no delivery yet, no footer", async () => {
    const { host, done } = await mount({ msg: msg("reply", { state: "answered", status: "answered" }, "ok") });
    try { expect(q(host, "relay-delivery")).toBeNull(); } finally { done(); }
  });
});

describe("titles and providers are cleaned text", () => {
  it("a hostile title is cleaned text in every head", async () => {
    const hostile = '<img src=x onerror="window.__pwned=1">\u202E\u200Bevil\nSECOND LINE ' + "z".repeat(100);
    for (const dir of ["in", "out", "reply"] as const) {
      const { host, done } = await mount({ msg: msg(dir, dir === "out" ? {} : { state: "answered", status: "answered" }, "x",
        { title: hostile, provider: "claude user" }) });
      try {
        const el = q(host, `msg-relay-${dir}`)!;
        const who = el.querySelector(".who")!;
        expect(el.querySelector("img")).toBeNull();
        expect(who.textContent).toContain('<img src=x onerror="window.__pwned=1">evil SECOND LINE');
        expect(who.textContent).not.toMatch(/[\u202E\u200B\n]/);
        expect(who.textContent).not.toContain("z".repeat(60));
        expect(who.textContent).not.toBe("YOU");
        const badge = who.querySelector(".badge");
        if (badge) expect(Array.from(badge.classList).sort()).toEqual(["badge", "unknown"]);
        expect((window as any).__pwned).toBeUndefined();
      } finally { done(); }
    }
  });
});

describe("ChatTab draws relay rows with RelayRow", () => {
  it("one row per relay message, in order, none as the owner's", async () => {
    await load();
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ subagents: [] }), { status: 200 })));
    const { ChatTab } = await import("./ChatTab");
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);
    const thread = { id: "aaaa1111", title: "t", provider: "fast" } as unknown as Thread;
    const messages = [
      { role: "user", text: "ask frontend", at: AT } as ChatMessage,
      msg("out", { state: "answered", status: "answered" }),
      msg("reply", { state: "answered", status: "answered", delivery: "wait" }, "done"),
      msg("in", { state: "answered" }),
    ];
    await act(async () => { root.render(createElement(ChatTab, { thread, messages, draft: "", onCancelTask: () => {} })); });
    try {
      const ids = Array.from(host.querySelector('[data-testid="log"]')!.children).map((e) => e.getAttribute("data-testid"));
      expect(ids).toEqual(["msg-user", "msg-relay-out", "msg-relay-reply", "msg-relay-in"]);
      expect(host.querySelectorAll('[data-testid="msg-relay"]')).toHaveLength(0);
      expect(Array.from(host.querySelectorAll(".who")).map((w) => w.textContent).filter((t) => t === "YOU")).toHaveLength(1);
    } finally { act(() => root.unmount()); host.remove(); }
  });
});
