import { describe, expect, it, vi } from "vitest";
import { PhoneApp, STREAM_URL, type StreamLike } from "./app";
import type { Got } from "./net";
import { deskNote, lineKind, orderTasks, parseEvent, parseLog, parseTask, type TaskView } from "./model";
import { phoneMarkdown } from "./markdown";
import { chatsView, inboxView, logRow, taskView, tasksView, transcriptView, diffView } from "./views";
import { parseInbox, type InboxRow } from "../lib/inbox";

// Hostile strings, as a model could write them: each must arrive as text.
const HOSTILE = [
  '<img src=x onerror="window.__pwned=1">',
  "<script>window.__pwned=2</script>",
  '<b>bold</b><a href="javascript:alert(1)">x</a>',
  '<svg onload="window.__pwned=3"></svg>',
];
const H = HOSTILE.join(" ");
const MARKUP = "img, script, b, svg, iframe, object, embed, a[href^='javascript'], [onerror], [onload]";

const ANSWER_WORDS = /\b(allow|deny|always|approve|answer it|send|withdraw|reply|resume|cancel|submit)\b/i;

function row(over: Partial<InboxRow> & { id: string; kind: string; rank: number }): unknown {
  return { title: "t", blocks: "b", project_id: null, thread_id: null, task_id: null, asked_at: null,
    expires_at: null, origin: "", detail: {}, ...over };
}

const ROWS = parseInbox([
  row({ id: "appr:r1", kind: "approval", rank: 1, origin: H, thread_id: "aaaaaaaa",
    detail: { tool: H, command: H, reason: H, headline: H, args: { command: H } } }),
  row({ id: "appr:r2", kind: "approval", rank: 1, detail: { tool: "Browser", hud_only: true } }),
  row({ id: "q:aaaaaaaa:1", kind: "question", rank: 2, thread_id: "aaaaaaaa", detail: { text: H, options: [H, "two"] } }),
  row({ id: "tq:bbbbbbbb:0", kind: "task_question", rank: 2, task_id: "bbbbbbbb", detail: { text: H, options: [] } }),
  row({ id: "prop:bbbbbbbb", kind: "proposal", rank: 3, task_id: "bbbbbbbb", detail: { brief: H } }),
  row({ id: "task:bbbbbbbb", kind: "task", rank: 4, task_id: "bbbbbbbb", detail: { brief: H, state: "blocked" } }),
  row({ id: "missed:x", kind: "missed", rank: 6, title: H, detail: { text: H, n: 2 } }),
])!;

function noMarkup(node: Element) {
  expect(node.querySelectorAll(MARKUP).length).toBe(0);
  expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
}

function task(over: Partial<TaskView> = {}): TaskView {
  return { id: "bbbbbbbb", project_id: "cccccccc", brief: H, state: "running", updated: "2026-10-25T10:00:00Z",
    step: 2, steps: 5, openQuestion: "", plan: [H, "two"], report: { done: [H], changed: ["a.py"], verified: H,
      open: [], next: H }, questions: [{ text: H, options: [H], blocking: true, answer: null }], branch: "jarvis/x", ...over };
}

describe("the phone's model (P-3)", () => {
  it("orders tasks running, then blocked, then the rest, newest first", () => {
    const t = (id: string, state: string, updated: string) => task({ id, state, updated });
    const groups = orderTasks([t("00000001", "done", "2026-10-25T09:00:00Z"), t("00000002", "blocked", "2026-10-25T08:00:00Z"),
      t("00000003", "running", "2026-10-25T07:00:00Z"), t("00000004", "verifying", "2026-10-25T11:00:00Z"),
      t("00000005", "failed", "2026-10-25T12:00:00Z")]);
    expect(groups.map((g) => g.label)).toEqual(["Running", "Blocked", "Everything else"]);
    expect(groups.map((g) => g.tasks.map((x) => x.id))).toEqual([["00000004", "00000003"], ["00000002"], ["00000005", "00000001"]]);
  });

  it("reads only the stream's own kinds, ids validated", () => {
    expect(parseEvent('{"kind":"inbox_changed","data":{"ids":["appr:1"],"reason":"x"}}')).toEqual({ kind: "inbox_changed", ids: ["appr:1"] });
    expect(parseEvent('{"kind":"thread_changed","data":{"thread_id":"aaaaaaaa","project_id":"cccccccc"}}'))
      .toEqual({ kind: "thread_changed", threadId: "aaaaaaaa", projectId: "cccccccc" });
    expect(parseEvent('{"kind":"thread_changed","data":{"thread_id":"../x"}}')).toBeNull();
    expect(parseEvent('{"kind":"text","data":{"text":"hi"}}')).toBeNull();
    expect(parseEvent("not json")).toBeNull();
    expect(parseEvent('{"kind":"resync"}')).toEqual({ kind: "resync" });
  });

  it("keeps a reply's spacing (Markdown) and classifies patch lines", () => {
    const page = parseLog({ rows: [{ seq: 3, role: "Claude", text: "```\n    indented\n```", at: null, marks: [] }], next: 4 });
    expect(page!.rows[0].text).toBe("```\n    indented\n```");
    expect(["+a", "-b", "@@ -1 +1 @@", "+++ b/x", "--- a/x", " c"].map(lineKind)).toEqual(["add", "del", "hunk", "file", "file", "ctx"]);
    expect(deskNote("approval")).toBe("Answer at the desk");
    expect(deskNote("question")).toBe("Answer at the desk");
    expect(deskNote("missed")).toBeNull();
  });
});

describe("the phone's views: untrusted text stays text (P-3)", () => {
  it("draws every inbox kind with origin, detail and question text as text", () => {
    const view = inboxView(ROWS, () => {});
    document.body.replaceChildren(view);
    noMarkup(view);
    const text = view.textContent || "";
    for (const h of HOSTILE) expect(text).toContain(h);
    expect(view.querySelectorAll('[data-testid="m-inbox-row"]').length).toBe(ROWS.length);
    expect(view.querySelector('[data-testid="m-origin"]')!.textContent).toBe(ROWS[0].origin);
    expect(view.querySelector('[data-testid="m-private"]')!.textContent).toContain("shown only at the desk");
    // A kind P-3 does not know (A3's missed rows) still shows its text, as text.
    const missed = view.querySelector('[data-kind="missed"]')!;
    expect(missed.textContent).toContain(HOSTILE[0]);
  });

  it("a decision (inbox B1) shows its question, options and why, read-only, as text", () => {
    const rows = parseInbox([
      row({ id: "dec:d0123456789ab", kind: "decision", rank: 6, thread_id: "aaaaaaaa", origin: H,
        detail: { decision_id: "d0123456789ab", question: "Which?\n" + H, options: [H, "two"], default: 1,
                  why: "because " + H, answer: null, delivery: null } }),
      row({ id: "dec:d0123456789ac", kind: "decision", rank: 8, thread_id: "aaaaaaaa",
        detail: { decision_id: "d0123456789ac", question: "Done?", options: ["yes", "no"], default: null,
                  why: "w", answer: { choice: 0, text: null, at: "2026-10-25T00:00:00+00:00" }, delivery: "closed" } }),
    ])!;
    const view = inboxView(rows, () => {});
    document.body.replaceChildren(view);
    noMarkup(view);
    const open = view.querySelector('[data-id="dec:d0123456789ab"]')!;
    expect(open.querySelector('[data-testid="m-question"]')!.textContent).toBe("Which?\n" + H);
    expect(Array.from(open.querySelectorAll(".options .option-text")).map((s) => s.textContent)).toEqual([H, "two"]);
    // The marker is the phone's own element before the option's words (review of PR #309).
    const items = Array.from(open.querySelectorAll(".options li"));
    expect(items[0].querySelector('[data-testid="m-suggested"]')).toBeNull();
    const marker = items[1].querySelector('[data-testid="m-suggested"]')!;
    expect(marker.textContent).toBe("suggested");
    expect(marker.compareDocumentPosition(items[1].querySelector(".option-text")!) & Node.DOCUMENT_POSITION_FOLLOWING)
      .toBeTruthy();
    expect(open.querySelector('[data-testid="m-why"]')!.textContent).toBe("because " + H);
    expect(open.querySelector('[data-testid="m-desk-only"]')!.textContent).toBe("Answer at the desk");
    for (const b of Array.from(view.querySelectorAll("button"))) expect(b.dataset.testid).toBe("m-open");
    const done = view.querySelector('[data-id="dec:d0123456789ac"]')!;
    expect(done.querySelector('[data-testid="m-answer"]')!.textContent).toBe("yes");
    expect(done.querySelector('[data-testid="m-desk-only"]')).toBeNull();
  });

  it("offers no answer control: approvals and questions say Answer at the desk", () => {
    const view = inboxView(ROWS, () => {});
    const notes = Array.from(view.querySelectorAll('[data-testid="m-desk-only"]')).map((n) => n.textContent);
    expect(notes.filter((n) => n === "Answer at the desk").length).toBe(4);
    for (const b of Array.from(view.querySelectorAll("button"))) {
      expect(b.textContent).not.toMatch(ANSWER_WORDS);
      expect(b.dataset.testid).toBe("m-open");
    }
    expect(view.querySelectorAll("input, textarea, form, select, [contenteditable]").length).toBe(0);
  });

  it("draws thread titles and task briefs as text", () => {
    const chats = chatsView([{ id: "cccccccc", name: H, inbox: false }],
      [{ id: "aaaaaaaa", project_id: "cccccccc", task_id: null, title: H, provider: "claude", updated: "", running: false, queued: 0, turns: 1 }],
      new Set(), { open: () => {}, toggle: () => {} });
    noMarkup(chats);
    expect(chats.querySelector('[data-testid="m-chat"]')!.textContent).toContain(HOSTILE[1]);
    const tasks = tasksView([task()], () => {});
    noMarkup(tasks);
    const one = taskView(task(), null, { back: () => {}, showDiff: () => {} });
    noMarkup(one);
    expect(one.querySelector('[data-testid="m-task-brief"]')!.textContent).toBe(H);
    expect(one.querySelector('[data-testid="m-plan"]')!.textContent).toContain(HOSTILE[2]);
    expect(Array.from(one.querySelectorAll("button")).map((b) => b.textContent)).toEqual(["‹ Tasks", "Show the diff"]);
  });

  it("shows the five most recent chats per project, the rest behind Show more", () => {
    const threads = Array.from({ length: 8 }, (_, i) => ({ id: `0000000${i}`, project_id: "cccccccc", task_id: null,
      title: `chat ${i}`, provider: "claude", updated: `2026-10-25T0${i}:00:00Z`, running: false, queued: 0, turns: 1 }));
    const view = chatsView([{ id: "cccccccc", name: "P", inbox: false }], threads, new Set(), { open: () => {}, toggle: () => {} });
    const shown = Array.from(view.querySelectorAll('[data-testid="m-chat"]')).map((b) => (b as HTMLElement).dataset.id);
    expect(shown).toEqual(["00000007", "00000006", "00000005", "00000004", "00000003"]);
    expect(view.querySelector('[data-testid="m-more"]')!.textContent).toBe("Show 3 more");
  });

  it("renders an assistant's Markdown sanitized, and everyone else's words as text", () => {
    const evil = "**bold** <script>window.__pwned=4</script> <img src=x onerror=alert(1)> [x](javascript:alert(1)) "
      + "<a href=\"https://e.com\" onclick=\"x()\">a</a> $x^2$";
    const reply = logRow({ seq: 1, role: "Claude", text: evil, at: null, marks: [], cut: false });
    document.body.replaceChildren(reply);
    noMarkup(reply);
    expect(reply.querySelector("strong")!.textContent).toBe("bold");
    expect(reply.querySelectorAll("[style], .katex-html, [onclick]").length).toBe(0);
    expect(reply.textContent).toContain("<script>");
    const owner = logRow({ seq: 2, role: "owner", text: "**not bold** " + H, at: null, marks: [], cut: false });
    noMarkup(owner);
    expect(owner.querySelector("strong")).toBeNull();
    expect(owner.textContent).toContain("**not bold**");
    const fragment = phoneMarkdown("$$\\frac{a}{b}$$");
    expect(fragment.querySelectorAll("[style], .katex-html, script").length).toBe(0);
    const { view } = transcriptView({ id: "aaaaaaaa", project_id: "cccccccc", task_id: null, title: H, provider: "claude",
      updated: "", running: false, queued: 0, turns: 1 }, [], false, () => {});
    noMarkup(view);
    expect(view.querySelectorAll("input, textarea, form").length).toBe(0);
  });

  it("folds the diff file by file and says what is too large", () => {
    const d = diffView({ truncated: true, note: "Too large to show here: open it at the desk.", files: [
      { path: "a.py", status: "M", additions: 1, deletions: 1, patch: "@@ -1 +1 @@\n-" + H + "\n+new\n", tooLarge: false },
      { path: "big.txt", status: "A", additions: 9000, deletions: 0, patch: null, tooLarge: true },
      ...["c", "d", "e"].map((p) => ({ path: p, status: "A", additions: 1, deletions: 0, patch: "+x\n", tooLarge: false })),
    ] });
    noMarkup(d);
    const files = d.querySelectorAll<HTMLDetailsElement>('[data-testid="m-diff-file"]');
    expect(files.length).toBe(5);
    expect(Array.from(files).every((f) => !f.open)).toBe(true);        // more than 3: folded
    expect(files[0].querySelector(".pl.del")!.textContent).toContain(HOSTILE[0]);
    expect(files[1].textContent).toContain("open it at the desk");
    expect(d.querySelector('[data-testid="m-diff-note"]')!.textContent).toContain("open it at the desk");
  });

  it("parses a task's report and questions", () => {
    const t = parseTask({ id: "bbbbbbbb", project_id: "cccccccc", brief: "b", state: "blocked", updated: "",
      status: { step: 1, steps: 3, open_question: "q?" }, plan: ["a"], report: { done: ["d"], changed: [], verified: "v", open: [], next: "n" },
      spec: { questions: [{ text: "which?", blocking: true, options: ["x"] }] } });
    expect(t!.report!.done).toEqual(["d"]);
    expect(t!.questions[0]).toEqual({ text: "which?", options: ["x"], blocking: true, answer: null });
  });
});

// -- the app: live updates, offline, signed out ---------------------------------------

class FakeStream implements StreamLike {
  readyState = 0;
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  closed = false;
  constructor(public url: string) {}
  open() { this.readyState = 1; this.onopen?.(new Event("open")); }
  send(value: unknown) { this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(value) })); }
  fail(closed = false) { this.readyState = closed ? 2 : 0; this.onerror?.(new Event("error")); }
  close() { this.closed = true; this.readyState = 2; }
}

function harness() {
  const answers: Record<string, Got> = {};
  const asked: string[] = [];
  let offline = false;
  const streams: FakeStream[] = [];
  const signedOut = vi.fn();
  const root = document.createElement("main");
  document.body.replaceChildren(root);
  const app = new PhoneApp({
    root, onSignedOut: signedOut, signOut: async () => {},
    fetchJson: async (path) => {
      asked.push(path);
      if (offline) return { ok: false, why: "offline", status: 0 };
      const base = path.split("?")[0];
      return answers[path] ?? answers[base] ?? { ok: false, why: "error", status: 404, body: { error: "not found" } };
    },
    openStream: (url) => { const s = new FakeStream(url); streams.push(s); return s; },
    setTimer: (fn) => { queueMicrotask(fn); return 0; },
    now: () => Date.parse("2026-10-25T10:11:12"),
  });
  const ok = (body: unknown): Got => ({ ok: true, body });
  answers["/m/api/inbox"] = ok({ rows: [] });
  answers["/m/api/me"] = ok({ signed_in: true });
  answers["/m/api/projects"] = ok({ projects: [{ id: "cccccccc", name: "Calc", inbox: false }] });
  answers["/m/api/threads"] = ok({ threads: [{ id: "aaaaaaaa", project_id: "cccccccc", role: "chat", title: "Fix it", provider: "claude",
    updated: "2026-10-25T10:00:00Z", running: false }] });
  answers["/m/api/threads/aaaaaaaa/log"] = ok({ rows: [{ seq: 0, role: "owner", text: "hi", at: null, marks: [] }], next: 1,
    more: false, older: false, thread: null });
  const settle = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
  return { app, answers, asked, streams, signedOut, root, ok, settle, setOffline: (v: boolean) => { offline = v; } };
}

describe("the phone app: live, offline and signed out (P-3)", () => {
  it("opens the stream, and reads the inbox again on inbox_changed", async () => {
    const h = harness();
    h.app.start();
    await h.settle();
    expect(h.streams.map((s) => s.url)).toEqual([STREAM_URL]);
    expect(h.root.querySelector('[data-testid="m-empty"]')!.textContent).toBe("Nothing is waiting on you.");
    h.answers["/m/api/inbox"] = h.ok({ rows: [row({ id: "appr:r9", kind: "approval", rank: 1, title: "Claude asks to use Bash",
      detail: { tool: "Bash", command: "ls" } })] });
    h.streams[0].open();
    h.streams[0].send({ kind: "inbox_changed", data: { ids: ["appr:r9"], reason: "approval_requested" } });
    await h.settle();
    expect(h.root.querySelectorAll('[data-testid="m-inbox-row"]').length).toBe(1);
    expect(h.root.querySelector('[data-testid="m-tab-inbox"]')!.textContent).toBe("Inbox · 1");
    // P-4: an approval row opens its answering card; nothing answers from the row.
    expect(h.root.querySelector('[data-testid="m-open-card"]')!.textContent).toBe("Open the card to answer");
    expect(h.root.querySelector('[data-testid="m-desk-only"]')).toBeNull();
  });

  it("appends a thread's new rows on thread_changed, reading after the cursor", async () => {
    const h = harness();
    h.app.start();
    await h.settle();
    (h.root.querySelector('[data-testid="m-tab-chats"]') as HTMLButtonElement).click();
    await h.settle();
    (h.root.querySelector('[data-testid="m-chat"]') as HTMLButtonElement).click();
    await h.settle();
    expect(h.root.querySelectorAll('[data-testid="m-msg"]').length).toBe(1);
    h.answers["/m/api/threads/aaaaaaaa/log?after=1"] = h.ok({ rows: [{ seq: 3, role: "Claude", text: "**done**", at: null, marks: [] }],
      next: 4, more: false, older: false, thread: null });
    h.streams[0].send({ kind: "thread_changed", data: { thread_id: "aaaaaaaa", project_id: "cccccccc", reason: "text" } });
    await h.settle();
    expect(h.asked).toContain("/m/api/threads/aaaaaaaa/log?after=1");
    const msgs = h.root.querySelectorAll('[data-testid="m-msg"]');
    expect(msgs.length).toBe(2);
    expect(msgs[1].querySelector("strong")!.textContent).toBe("done");
    // The same event again finds nothing new and appends nothing twice.
    h.answers["/m/api/threads/aaaaaaaa/log?after=4"] = h.ok({ rows: [], next: 4, more: false, older: false, thread: null });
    h.streams[0].send({ kind: "thread_changed", data: { thread_id: "aaaaaaaa", project_id: "cccccccc", reason: "text" } });
    await h.settle();
    expect(h.root.querySelectorAll('[data-testid="m-msg"]').length).toBe(2);
    // P-5: the chat's one message box, and nothing else to type in.
    expect(h.root.querySelectorAll("input, form").length).toBe(0);
    expect(h.root.querySelectorAll('textarea[data-testid="m-compose-text"]').length).toBe(1);
  });

  it("goes offline with a banner and stale data, and back once a read lands", async () => {
    const h = harness();
    h.app.start();
    await h.settle();
    const banner = h.root.querySelector<HTMLElement>('[data-testid="m-offline"]')!;
    expect(banner.hidden).toBe(true);
    h.setOffline(true);
    h.streams[0].fail();
    await h.settle();
    expect(banner.hidden).toBe(false);
    expect(banner.textContent).toContain("Offline");
    expect(banner.textContent).toContain("Nothing here is current");
    expect(banner.textContent).toContain("10:11:12");
    expect(h.root.querySelector<HTMLElement>("[data-screen]")!.dataset.stale).toBe("true");
    expect(h.root.querySelector('[data-testid="m-conn"]')!.textContent).toBe("offline");
    h.setOffline(false);
    h.streams[0].open();
    await h.settle();
    expect(banner.hidden).toBe(true);
    expect(h.root.querySelector<HTMLElement>("[data-screen]")!.dataset.stale).toBe("false");
    expect(h.asked.filter((p) => p === "/m/api/inbox").length).toBeGreaterThanOrEqual(2);   // read again
  });

  it("a stream that drops for good is checked and reopened", async () => {
    const h = harness();
    h.app.start();
    await h.settle();
    h.streams[0].fail(true);
    await h.settle();
    expect(h.asked).toContain("/m/api/me");
    expect(h.streams.length).toBe(2);
  });

  it("a 401 (logout, revoke, expiry) stops the app and asks for a sign-in", async () => {
    const h = harness();
    h.app.start();
    await h.settle();
    h.answers["/m/api/me"] = { ok: false, why: "signed-out", status: 401 };
    h.streams[0].fail(true);
    await h.settle();
    expect(h.signedOut).toHaveBeenCalledTimes(1);
    expect(h.streams[0].closed).toBe(true);
    expect(h.app.stopped).toBe(true);
  });
});
