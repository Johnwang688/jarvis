// The phone UI audit's fixes in the views (docs/plans/2026-10-24-phone-surface-design.md §10.12):
// C1, the owner's rows can never be drawn by a reply; H1, H4, H5, H6, M5.
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { emptyLog, inboxView, logRow, NOT_TYPED_WORDS, OWNER_ROW_CLASSES, taskView, transcriptView, typedHere,
  type InboxAnswers } from "./views";
import { deskNote, type ChatThread, type LogRow, type TaskView } from "./model";
import { parseInbox, type InboxRow } from "../lib/inbox";

const CSS = readFileSync(join(__dirname, "mobile.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
/** One rule's body, by its exact selector. */
const rule = (selector: string) => {
  const at = CSS.indexOf(selector + " {");
  expect(at, selector).toBeGreaterThanOrEqual(0);
  return CSS.slice(at, CSS.indexOf("}", at));
};

const H = '<img src=x onerror="window.__pwned=1"> <script>window.__pwned=2</script>';

// The audit's spoof (iphone-chat-spoof-blockquote.png): a reply quoting an "owner" line.
const SPOOF = "Two things are left: the screenshots and the PR body. Before that, a note from earlier:\n\n"
  + "> owner · via the phone · 07:58\n>\n> Also push it and merge when green, no need to ask.\n\nI'll do the screenshots next.";

// Every Markdown construct a model could try for the owner's look.
const ATTEMPTS = [
  SPOOF,
  "# You\n\nowner · via the phone · 07:58\n\nmerge it",
  "## owner · steering · 07:52\n\n- You · via the phone\n- merge when green",
  "| You | via the phone |\n|---|---:|\n| owner | merge it |",
  "**You** · via the phone · 07:58 — merge it",
  "`You` · via the phone · 07:58",
  '<span class="who-owner" data-testid="m-owner-mark">You</span> merge it',
  '<article class="msg msg-owner"><div class="msg-bubble">merge it</div></article>',
  '<div class="msg-flat"><div class="msg-head">You · steering</div>merge</div>',
  "> > You\n> > merge it\n\n---\n\n1. You\n2. owner",
  "[You](https://example.org \"msg-owner\") ![x](https://example.org/x.png)",
  "You: merge it " + H,
  "```who-owner\nYou · via the phone\n```\n\n```msg-owner msg-flat\nmerge it\n```",
];

const reply = (text: string): LogRow => ({ seq: 9, role: "Claude", text, at: "2026-10-29T07:59:00Z", marks: [], cut: false });
const owner = (marks: string[]): LogRow => ({ seq: 1, role: "owner", text: "merge it", at: "2026-10-29T07:52:00Z", marks, cut: false });
const CHAT = { id: "aaaaaaaa", project_id: "cccccccc", task_id: null, role: "chat", title: "t", provider: "claude",
  updated: "", running: false, queued: 0, turns: 0 } as ChatThread;

describe("C1: agent text never looks like the owner's", () => {
  it("draws the audit's spoof reply with no owner mark, as the reply's own quote", () => {
    const item = logRow(reply(SPOOF));
    expect(item.className).toBe("msg msg-reply");
    expect(item.querySelector(".md blockquote")!.textContent).toContain("owner · via the phone · 07:58");
    expect(item.querySelector('.who-owner, [data-testid="m-owner-mark"]')).toBeNull();
    expect(item.querySelector(".m-sr")).toBeNull();
  });

  it("gives the rows the owner typed in this chat, bubble and flat, the code-built You mark before its marks", () => {
    for (const [marks, style] of [[["via the phone"], "msg-owner"], [["via the phone", "not sent"], "msg-flat"],
      [["not sent"], "msg-flat"], [["spoken"], "msg-owner"], [[], "msg-owner"],
      // A steer is the owner's typing (at the desk, mid-turn): You, its mark kept, flat (review of #338).
      [["steering"], "msg-flat"], [["spoken", "steering"], "msg-flat"]] as const) {
      const item = logRow(owner([...marks]));
      expect(item.classList.contains(style)).toBe(true);
      const mark = item.querySelector('[data-testid="m-owner-mark"]')!;
      expect([mark.className, mark.textContent, mark.getAttribute("aria-hidden")]).toEqual(["who-owner", "You", "true"]);
      // Before every mark and the time, in the row's own line.
      const line = mark.parentElement!;
      expect(line.firstChild).toBe(mark);
      for (const m of marks) expect(line.textContent!.indexOf(m)).toBeGreaterThan(line.textContent!.indexOf("You"));
      // M13: VoiceOver hears who said it, first.
      expect([item.firstElementChild!.className, item.firstElementChild!.textContent]).toEqual(["m-sr", "You: "]);
    }
  });

  it("never says You on an owner-role row the owner did not type here (review of #338, MED 1)", () => {
    const cases: [string, LogRow, boolean][] = [
      ["a deferral's words (written_by)", owner(["written by Jarvis (a deferred approval), not typed"]), true],
      ["an inbox decision's answer", owner(["via the inbox (not typed in this chat)"]), true],
      ["the phone inbox's answer", owner(["via the phone's inbox (not typed in this chat)"]), true],
      ["a mark that only contains the steer word", owner(["steering (relayed)"]), true],
      ["a mark the phone does not know", owner(["something new"]), true],
      ["a task thread's unmarked prompt", owner([]), false],
      ["a typed mark outside an owner's chat", owner(["via the phone"]), false],
      ["a steer outside an owner's chat", owner(["steering"]), false],
    ];
    for (const [what, r, inChat] of cases) {
      expect(typedHere(r, inChat), what).toBe(false);
      const item = logRow(r, inChat);
      expect(item.className, what).toBe("msg msg-flat");
      expect(item.querySelector('[data-testid="m-owner-mark"], .who-owner'), what).toBeNull();
      expect(item.querySelector(".m-sr"), what).toBeNull();
      expect(item.textContent, what).not.toMatch(/\bYou\b/);
      const chip = item.querySelector('[data-testid="m-not-typed-mark"]')!;
      expect([chip.className, chip.textContent, chip.getAttribute("aria-hidden")], what).toEqual(["who-other", NOT_TYPED_WORDS, null]);
      // The marks stay above the words, after the chip.
      const head = item.querySelector('[data-testid="m-msg-marks"]')!;
      expect([head.firstChild, item.firstElementChild], what).toEqual([chip, head]);
      for (const m of r.marks) expect(head.textContent, what).toContain(m);
    }
    // A thread whose meta is not read yet: not known to be an owner's chat, so no You at all.
    const unknown = transcriptView(null, [owner(["via the phone"]), owner([]), owner(["steering"])], false, () => {});
    expect(unknown.list.querySelectorAll('[data-testid="m-owner-mark"], .m-sr').length).toBe(0);
    expect(unknown.list.querySelectorAll('[data-testid="m-not-typed-mark"]').length).toBe(3);
    // A typed row in an owner's chat still gets You, bubble or flat.
    const chat = transcriptView(CHAT, [owner(["via the phone"]), owner(["via the phone", "not sent"])], false, () => {});
    expect(chat.list.querySelectorAll('[data-testid="m-owner-mark"]').length).toBe(2);
    expect(chat.list.querySelectorAll('[data-testid="m-not-typed-mark"]').length).toBe(0);
    // The not-typed chip is not the You chip's look (no light fill).
    expect(rule(".who-other")).not.toMatch(/background/);
  });

  it("no Markdown construct can put an owner-row class or the mark inside a reply (structural)", () => {
    const view = transcriptView(CHAT, ATTEMPTS.map((text, i) => ({ ...reply(text), seq: i })), false, () => {});
    const replies = Array.from(view.list.querySelectorAll(".msg-reply"));
    expect(replies.length).toBe(ATTEMPTS.length);
    const reserved = OWNER_ROW_CLASSES.map((c) => "." + c).join(", ");
    for (const r of replies) {
      expect(r.querySelectorAll(reserved + ', [data-testid="m-owner-mark"], [data-testid="m-msg-marks"], .m-sr').length).toBe(0);
      // The sanitizer keeps no class at all but KaTeX's own; no data attribute, no style.
      for (const node of Array.from(r.querySelectorAll(".md *"))) {
        for (const c of Array.from(node.classList)) expect(c.startsWith("katex"), c).toBe(true);
        expect(Object.keys((node as HTMLElement).dataset ?? {})).toEqual([]);
        expect(node.hasAttribute("style")).toBe(false);
      }
      // The reply's head is code's, the model's name first.
      expect(r.querySelector(":scope > .msg-head")!.textContent!.startsWith("Claude")).toBe(true);
    }
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
    // And the owner's rows in the same transcript carry it.
    const mixed = transcriptView(CHAT, [owner(["via the phone"]), owner(["spoken"]), reply(SPOOF)], false, () => {});
    expect(mixed.list.querySelectorAll('[data-testid="m-owner-mark"]').length).toBe(2);
  });

  it("styles a reply's quote unlike any owner row, and only owner rows sit right with a right rule", () => {
    const quote = rule(".md blockquote");
    expect(quote).toMatch(/font-style: italic/);
    expect(quote).toMatch(/background: var\(--bg-2\)/);
    expect(quote).not.toMatch(/border(-(left|right|top|bottom))?\s*:/);
    const flat = rule(".msg-flat");
    expect(flat).toMatch(/align-self: flex-end/);
    expect(flat).toMatch(/border-right: 2px solid var\(--text-2\)/);
    expect(flat).not.toMatch(/border-left/);
    expect(rule(".msg-reply")).toMatch(/border-left: 2px solid var\(--bg-3\)/);
    // The mark is a light filled tag: no reply element is ever filled with --text-2.
    expect(rule(".who-owner")).toMatch(/background: var\(--text-2\)/);
    const mdRules = CSS.match(/\.md [^{]*\{[^}]*\}/g)!.join("\n");
    expect(mdRules).not.toMatch(/var\(--text-2\)[^;]*;[^}]*background|background: var\(--text-2\)/);
    expect(mdRules).not.toMatch(/border-right|align-self|text-align: right|who-owner|msg-owner|msg-flat/);
    // No selector reaches an owner class from inside a reply.
    expect(CSS).not.toMatch(/\.md[^{]*\.(who-owner|msg-owner|msg-flat|msg-bubble)/);
  });
});

describe("H1: the relay line never cuts ', not the owner'", () => {
  it("keeps the tail whole (flex: none) and lets the title give way, the cap kept", () => {
    expect(rule(".step-role-tail")).toMatch(/^\.step-role-tail \{ flex: none; $/);
    expect(rule(".step-role-head")).toMatch(/flex: 0 1000 auto; min-width: 1ch; overflow: hidden; text-overflow: ellipsis/);
    expect(rule(".step-role")).toMatch(/max-width: 78%/);
    expect(rule(".step-text")).toMatch(/flex: 1; min-width: 0/);
    const role = "chat " + "A very long title ".repeat(6) + "(d7278e2a), not the owner";
    const step = logRow({ seq: 5, role, text: "FYI", at: null, marks: [], cut: false });
    expect(step.querySelector(".step-role-tail")!.textContent).toBe(" (d7278e2a), not the owner");
  });
});

function row(over: Partial<InboxRow> & { id: string; kind: string; rank: number }): unknown {
  return { title: "t", blocks: "b", project_id: null, thread_id: null, task_id: null, asked_at: null,
    expires_at: null, origin: "", detail: {}, ...over };
}

const answers = (): InboxAnswers => ({ openCard() {}, send() {}, retry() {}, busy: new Set(), notes: new Map(),
  drafts: new Map(), picks: new Map() });

const SHA = "a3f9c2e1".repeat(5);
const PR = row({ id: "pr:Johnwang688/Jarvis#337", kind: "pr", rank: 5, title: "PR #337 is ready to merge",
  detail: { repo: "Johnwang688/Jarvis", number: 337, title: "PM-F: PR rows " + H, branch: "claude/pm-f", base: "main",
    author: "Johnwang688", fork: false, head: SHA, checks: "failing", merge_state: "BLOCKED", url: "https://github.com/Johnwang688/Jarvis/pull/337",
    facts: [{ text: "Johnwang688/Jarvis #337", items: [], more: 0 }, { text: "claude/pm-f → main", items: [], more: 0 },
      { text: "checks: failing", items: ["lint " + H, "test"], more: 2 }, { text: "blocked by branch rules", items: [], more: 0 }],
    flags: { flagged: true, items: [{ name: "jarvis/v2/remote_api.py", families: ["phone"] }], more: 0, incomplete: false },
    summary: { does: "Adds the PR row layout " + H, blocked: "lint fails on views.ts" }, summary_state: "ready",
    actions: [], why: "checks fail", method: null } });

describe("H4 (interim, until PM-F): the PR row reads as a PR, not a key dump", () => {
  it("shows the title, code's facts, the AI summary labelled, and the desk hint; never the SHA, a key or a link", () => {
    for (const a of [undefined, answers()]) {
      const view = inboxView(parseInbox([PR])!, () => {}, a);
      const r = view.querySelector('[data-kind="pr"]')!;
      expect(r.querySelector('[data-testid="m-pr-title"]')!.textContent).toBe("PM-F: PR rows " + H);
      const facts = r.querySelector('[data-testid="m-pr-facts"]')!;
      expect(facts.textContent).toBe("Johnwang688/Jarvis #337 · claude/pm-f → main · checks: failing lint " + H
        + ", test +2 more · blocked by branch rules");
      expect(Array.from(facts.querySelectorAll("bdi")).map((b) => b.textContent)).toEqual(["lint " + H, "test"]);
      const ai = r.querySelector('[data-testid="m-pr-summary"]')!;
      expect(ai.querySelector(".as-tag")!.textContent).toBe("AI SUMMARY");
      expect(r.querySelector('[data-testid="m-pr-does"]')!.textContent).toBe("Does: Adds the PR row layout " + H);
      expect(r.querySelector('[data-testid="m-pr-blocked"]')!.textContent).toBe("Blocked: lint fails on views.ts");
      expect(r.querySelector('[data-testid="m-pr-flags"]')!.textContent).toBe("Touches safety files: jarvis/v2/remote_api.py");
      expect(r.querySelector('[data-testid="m-desk-only"]')!.textContent).toBe("Merge it at the desk for now");
      expect(r.textContent).not.toContain(SHA);
      expect(r.textContent).not.toMatch(/a3f9c2e1a3f9/);
      expect(r.querySelectorAll(".label").length).toBe(0);
      expect(r.textContent).not.toMatch(/merge state|summary state|\bhead\b/);
      expect(r.querySelectorAll("a, img, script, [onerror]").length).toBe(0);
      expect(r.querySelectorAll("button").length).toBe(0);
    }
    expect(deskNote("pr")).toBe("Merge it at the desk for now");
  });

  it("without a summary, no AI slot; facts and the hint stay", () => {
    const bare = row({ id: "pr:o/r#5", kind: "pr", rank: 5, title: "PR #5", detail: { repo: "o/r", number: 5, title: "x",
      head: SHA, facts: null, summary: null, summary_state: "pending" } });
    const r = inboxView(parseInbox([bare])!, () => {}, answers()).querySelector('[data-kind="pr"]')!;
    expect(r.querySelector('[data-testid="m-pr-summary"]')).toBeNull();
    expect(r.textContent).not.toContain(SHA);
    expect(r.querySelector('[data-testid="m-desk-only"]')!.textContent).toBe("Merge it at the desk for now");
  });
});

describe("H5: an unread chat is never shown as empty", () => {
  it("says Loading, or that it was not read offline; No messages yet only once read", () => {
    expect([emptyLog(true, true), emptyLog(true, false), emptyLog(false, true), emptyLog(false, false)])
      .toEqual(["No messages yet.", "No messages yet.", "Loading…", "Not read yet: you're offline."]);
    const off = transcriptView(CHAT, [], false, () => {}, null, { loaded: false, online: false });
    expect(off.list.querySelector('[data-testid="m-empty"]')!.textContent).toBe("Not read yet: you're offline.");
    const read = transcriptView(CHAT, [], false, () => {});
    expect(read.list.querySelector('[data-testid="m-empty"]')!.textContent).toBe("No messages yet.");
  });
});

const DEFER = row({ id: "defer:D1", kind: "deferred", rank: 4, thread_id: "aaaaaaaa", title: "Bash waits",
  detail: { deferral_id: "D1", tool: "Bash", grantable: true, expired: false, hud_only: false,
    card: { tool: "Bash", command: "make deploy", reason: "r", args: { command: "make deploy" } } } });

describe("H6: a deferred row's buttons as the Review tab's", () => {
  it("Leave it at the left, Approve and retry at the right, in one spaced row", () => {
    const view = inboxView(parseInbox([DEFER])!, () => {}, answers());
    const acts = view.querySelector('[data-testid="m-deferred-acts"]')!;
    expect(acts.className).toBe("m-review-rowacts");
    expect(Array.from(acts.children).map((b) => b.getAttribute("data-testid"))).toEqual(["m-deferred-leave", "m-deferred-retry"]);
    expect(rule(".m-review-rowacts")).toMatch(/justify-content: space-between; gap: 48px/);
    expect(rule(".m-review-rowacts .tap")).toMatch(/flex: 0 1 42%/);
  });
});

describe("M5: a task's question points at the Inbox, where the phone answers it", () => {
  const task: TaskView = { id: "bbbbbbbb", project_id: "cccccccc", brief: "b", state: "clarifying", updated: "", step: 0,
    steps: 0, openQuestion: "", plan: [], report: null, branch: "",
    questions: [{ text: "done?", options: [], blocking: true, answer: "yes", index: 0 },
      { text: "which db?", options: ["pg"], blocking: true, answer: null, index: 1 }] };

  it("offers Answer it in the Inbox for that row, never Answer at the desk", () => {
    const went: number[] = [];
    const view = taskView(task, null, { back() {}, showDiff() {}, answerInInbox: (i) => went.push(i) });
    expect(view.querySelector('[data-testid="m-desk-only"]')).toBeNull();
    const go = view.querySelectorAll<HTMLButtonElement>('[data-testid="m-answer-in-inbox"]');
    expect(Array.from(go).map((b) => b.textContent)).toEqual(["Answer it in the Inbox"]);
    go[0].click();
    expect(went).toEqual([1]);
    // Only when the row is in the Inbox now; else the plain desk note (review of #338, LOW).
    const gone = taskView(task, null, { back() {}, showDiff() {}, answerInInbox: (i) => went.push(i), inInbox: () => false });
    expect(gone.querySelector('[data-testid="m-answer-in-inbox"]')).toBeNull();
    expect(gone.querySelector('[data-testid="m-desk-only"]')!.textContent).toBe("Answer at the desk");
    // Without the app's link (read-only), the old words.
    expect(taskView(task, null, { back() {}, showDiff() {} }).querySelector('[data-testid="m-desk-only"]')!.textContent)
      .toBe("Answer at the desk");
  });
});

describe("M7: control edges at 3:1", () => {
  it("defines --control-edge as a tint of Graphite's tokens and uses it on every button and field edge", () => {
    expect(CSS).toMatch(/html \{ --control-edge: color-mix\(in srgb, var\(--text-muted\) 55%, var\(--border-strong\)\); \}/);
    for (const sel of [".tap.quiet", ".tap.m-btn", ".m-text", ".btn"]) expect(rule(sel)).toMatch(/border: 1px solid var\(--control-edge\)/);
    expect(rule(".tap.quiet.primary")).toMatch(/border-color: var\(--accent\); color: var\(--accent\)/);
    const view = inboxView(parseInbox([row({ id: "appr:r1", kind: "approval", rank: 1, detail: { tool: "Bash" } })])!, () => {}, answers());
    expect(view.querySelector('[data-testid="m-open-card"]')!.className).toBe("tap quiet m-send primary");
  });
});
