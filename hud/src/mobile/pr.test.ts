// PM-F on the phone (mobile/views.ts `prBlock`, mobile/app.ts `prAct` and
// `cancelFix`): the PR row's layout, the passkey per action with the head and
// card as drawn, 409s, Q1's limited phone, Q3's hold, each fix state, squash
// wording, the held summary slot, and never a link.
import { describe, expect, it, vi } from "vitest";
import { PhoneApp, ASKING_FACE_ID, type StreamLike } from "./app";
import type { Got, Posted } from "./net";
import { deskNote } from "./model";
import { inboxView, type InboxAnswers } from "./views";
import { parseInbox } from "../lib/inbox";
import { FLAGGED, HOSTILE, REPO, cardOf, facts, fixRow, headOf, prRow, tqRow } from "../lib/prRow.fixtures";

const MARKUP = "a, img, script, svg, iframe, [onerror], [onload]";
const CLEAN = HOSTILE.replace("‮", "");

function answers(over: Partial<NonNullable<InboxAnswers["pr"]>> = {}, rows: unknown[] = []): InboxAnswers {
  return {
    openCard() {}, send() {}, retry() {}, busy: new Set(), notes: new Map(), drafts: new Map(), picks: new Map(),
    pr: { act: vi.fn(), cancelFix: vi.fn(), question: vi.fn(), limited: false, progress: new Map(),
          rows: parseInbox(rows) ?? [], ...over },
  };
}

function draw(raw: unknown[], a: InboxAnswers | null = answers()) {
  const view = inboxView(parseInbox(raw)!, () => {}, a ?? undefined);
  document.body.replaceChildren(view);
  return view;
}

const rowEl = (view: HTMLElement, n: number) => view.querySelector(`[data-id="pr:${REPO}#${n}"]`) as HTMLElement;
const acts = (r: HTMLElement) => Array.from(r.querySelectorAll<HTMLElement>('[data-testid^="m-pr-act-"]'))
  .map((b) => b.dataset.testid!.slice(9));

describe("PM-24 on the phone: the layout", () => {
  it("title, then the buttons, then code's facts and flags, then the AI summary; never a link or the head SHA", () => {
    const view = draw([prRow(15, { flags: FLAGGED })]);
    const r = rowEl(view, 15);
    const order = Array.from(r.querySelectorAll("[data-testid]")).map((e) => (e as HTMLElement).dataset.testid)
      .filter((t) => ["m-pr-title", "m-pr-acts", "m-pr-notice", "m-pr-facts", "m-pr-flags", "m-pr-summary"].includes(t!));
    expect(order).toEqual(["m-pr-title", "m-pr-acts", "m-pr-notice", "m-pr-facts", "m-pr-flags", "m-pr-summary"]);
    expect(r.querySelector(".row-title")!.compareDocumentPosition(r.querySelector('[data-testid="m-pr-acts"]')!)
      & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(r.querySelectorAll(MARKUP).length).toBe(0);
    expect(r.textContent).not.toContain(headOf(15));
    expect(r.textContent).not.toContain(cardOf(15));
    expect(r.textContent).not.toContain("github.com");
    expect(r.querySelector('[data-testid="m-desk-only"]')).toBeNull();
  });

  it("Merge sits at the right, Fix and Update at the left, apart; every button is a plain button", () => {
    const view = draw([prRow(11, { actions: ["merge", "merge_as_is", "fix", "update"] })]);
    const r = rowEl(view, 11);
    const [left, right] = Array.from(r.querySelectorAll('[data-testid="m-pr-acts"] > .m-pr-side'));
    expect(Array.from(left.querySelectorAll("button")).map((b) => b.textContent)).toEqual(["Fix", "Update"]);
    expect(Array.from(right.querySelectorAll("button")).map((b) => b.textContent)).toEqual(["Merge", "Merge as is"]);
    expect(Array.from(r.querySelectorAll("button")).every((b) => b.type === "button" && !b.autofocus)).toBe(true);
    expect(r.querySelectorAll("form, [autofocus]").length).toBe(0);
  });

  it("names, scanners and the model's words: each in its place, all as text", () => {
    const view = draw([prRow(14, {
      facts: facts(14, { checks: "checks: failing", checkItems: ["GitGuardian [secret scanner]", HOSTILE + " [secret scanner]"] }),
      failing: [{ name: "GitGuardian", scanner: true }], summary: { does: "Does " + HOSTILE, blocked: "Why " + HOSTILE },
    })]);
    const r = rowEl(view, 14);
    expect(r.querySelectorAll(MARKUP).length).toBe(0);
    expect(Array.from(r.querySelectorAll('[data-testid="m-pr-facts"] bdi')).map((b) => b.textContent))
      .toEqual(["GitGuardian", CLEAN + " [secret scanner]"]);
    expect(r.querySelectorAll('[data-testid="m-pr-scanner"]').length).toBe(1);
    const ai = r.querySelector('[data-testid="m-pr-summary"]')!;
    expect(ai.querySelector(".as-tag")!.textContent).toBe("AI SUMMARY");
    expect(r.querySelector('[data-testid="m-pr-does"]')!.textContent).toBe("Does: Does " + CLEAN);
    expect(r.querySelector('[data-testid="m-pr-blocked"]')!.textContent).toBe("Blocked: Why " + CLEAN);
  });

  it("holds the summary slot: pending, failed and off; code's note outside it", () => {
    const view = draw([prRow(12, { summary: null, summary_state: "pending" }),
                       prRow(18, { summary: null, summary_state: "failed", summary_reason: "the model did not answer in time" }),
                       prRow(19, { summary: null, summary_state: "off", summary_reason: "today's budget is used up" }),
                       prRow(20, { summary_note: "written without the CI logs, which could not be read" })]);
    const slot = (n: number) => rowEl(view, n).querySelector('[data-testid="m-pr-summary"]')!;
    expect(slot(12).textContent).toBe("AI SUMMARYsummarizing…");
    expect(slot(12).classList.contains("as-ai")).toBe(true);
    expect(slot(18).textContent).toContain("AI summary unavailable: the model did not answer in time");
    expect(slot(19).textContent).toContain("AI summary unavailable: today's budget is used up");
    const note = rowEl(view, 20).querySelector('[data-testid="m-pr-summary-note"]')!;
    expect(note.textContent).toBe("summary: written without the CI logs, which could not be read");
    expect(slot(20).contains(note)).toBe(false);
    expect(acts(rowEl(view, 18))).toEqual(["merge"]);
  });

  it("no buttons: code's why; squash named before the OK (Q4)", () => {
    const view = draw([prRow(17, { actions: [], why: "waits on its checks" }), prRow(21, { method: "squash" })]);
    expect(acts(rowEl(view, 17))).toEqual([]);
    expect(rowEl(view, 17).querySelector('[data-testid="m-pr-why"]')!.textContent).toBe("waits on its checks");
    expect(rowEl(view, 21).querySelector('[data-testid="m-pr-method"]')!.textContent).toContain("squash merge");
  });

  it("without the app's answers (P-3's read-only view) it says to merge at the desk", () => {
    const view = draw([prRow(11)], null);
    expect(acts(rowEl(view, 11))).toEqual([]);
    expect(rowEl(view, 11).querySelector('[data-testid="m-desk-only"]')!.textContent).toBe("Merge at the desk");
    expect(deskNote("pr")).toBe("Merge at the desk");
  });
});

describe("Q1 and Q3 on the phone", () => {
  it("limited: a flagged PR (or one whose flags are not known) offers nothing and says to answer it at the desk", () => {
    const a = answers({ limited: true });
    const view = draw([prRow(15, { flags: FLAGGED }), prRow(16, { flags: null }), prRow(11)], a);
    for (const n of [15, 16]) {
      expect(acts(rowEl(view, n)), String(n)).toEqual([]);
      expect(rowEl(view, n).querySelector('[data-testid="m-pr-why"]')!.textContent).toContain("answer it at the desk");
    }
    expect(acts(rowEl(view, 11))).toEqual(["merge"]);
    // Not limited: the flagged PR keeps its buttons (the flag is still shown).
    const full = draw([prRow(15, { flags: FLAGGED })]);
    expect(acts(rowEl(full, 15))).toEqual(["merge"]);
  });

  it("held: the note, no button anywhere, no hold control", () => {
    const view = draw([prRow(22, { hold: { note: "waits on the robot " + HOSTILE, set_at: "x" }, actions: ["merge"] }),
                       fixRow(23, "ready", { summary: "s", summary_state: "ready" }, { hold: { note: "h" }, actions: ["merge"] })]);
    for (const n of [22, 23]) {
      expect(acts(rowEl(view, n)), String(n)).toEqual([]);
      expect(rowEl(view, n).querySelectorAll('[data-testid*="hold-"]').length).toBe(0);
    }
    expect(rowEl(view, 22).querySelector('[data-testid="m-pr-hold"]')!.textContent).toBe("On hold: waits on the robot " + CLEAN);
    expect(rowEl(view, 22).querySelectorAll(MARKUP).length).toBe(0);
  });
});

describe("the fix on the phone (PM-21 to PM-23)", () => {
  it("each state in code's words; Cancel while it runs; Fix again after", () => {
    const view = draw([fixRow(31, "running"), fixRow(32, "question"),
                       fixRow(33, "stopped", { note: "fix stopped: failed" }, { actions: ["fix"] }, { rank: 4 }),
                       fixRow(34, "failing", { note: "fix pushed; checks still fail" }, { actions: ["fix"] }, { rank: 4 }),
                       fixRow(35, "pushed", { note: "fix pushed; checks running" }, {}, { rank: 4 }),
                       fixRow(36, "rewritten", {}, {}, { rank: 4 }),
                       fixRow(39, "start_failed", { note: "fix could not start: no project owns it" })]);
    const text = (n: number) => rowEl(view, n).querySelector('[data-testid="m-pr-fix-text"]')!.textContent;
    expect(text(31)).toBe("fix running (task t0000031)");
    expect(text(32)).toBe("fix waits on your answer (task t0000032)");
    expect(text(33)).toBe("fix stopped: failed");
    expect(text(34)).toBe("fix pushed; checks still fail");
    expect(text(35)).toBe("fix pushed; checks running");
    expect(text(36)).toBe("branch rewritten since your Fix");
    expect((rowEl(view, 36).querySelector('[data-testid="m-pr-fix"]') as HTMLElement).dataset.tone).toBe("amber");
    expect(text(39)).toBe("fix could not start: no project owns it");
    expect(rowEl(view, 31).querySelector('[data-testid="m-pr-fix-cancel"]')).not.toBeNull();
    expect(rowEl(view, 33).querySelector('[data-testid="m-pr-fix-cancel"]')).toBeNull();
    expect(rowEl(view, 33).querySelector('[data-testid="m-pr-act-fix"]')!.textContent).toBe("Fix again");
  });

  it("the fix card: Fixed — merge?, its own AI slot, commits, the CI amber, Merge", () => {
    const view = draw([fixRow(37, "ready", { note: "fixed: " + HOSTILE + " — merge?", summary: "merged main " + HOSTILE,
                                             summary_state: "ready", commits: 2, ci_changed: true },
                              { actions: ["merge"], flags: FLAGGED }, { rank: 4 })]);
    const r = rowEl(view, 37);
    expect(r.querySelector('[data-testid="m-pr-fix-text"]')!.textContent).toBe("Fixed — merge?");
    expect(r.querySelector('[data-testid="m-pr-fix-summary"]')!.textContent).toBe("AI SUMMARYthe fix: merged main " + CLEAN);
    expect(r.querySelector('[data-testid="m-pr-fix-commits"]')!.textContent).toBe("2 commits since your Fix");
    expect(r.querySelector('[data-testid="m-pr-fix-ci"]')!.textContent).toBe("the fix changed CI configuration");
    expect(r.querySelector('[data-testid="m-pr-flags"]')).not.toBeNull();
    expect(acts(r)).toEqual(["merge"]);
    const fix = r.querySelector('[data-testid="m-pr-fix"]')!;
    const outside = Array.from(fix.children).filter((c) => (c as HTMLElement).dataset.testid !== "m-pr-fix-summary")
      .map((c) => c.textContent).join(" ");
    expect(outside).not.toContain("evil");
    expect(r.querySelectorAll(MARKUP).length).toBe(0);
  });

  it("a fix waiting on an answer links to its question row when it is in the Inbox", () => {
    const a = answers({}, [tqRow("t0000032abcdef")]);
    const view = draw([fixRow(32, "question")], a);
    const q = rowEl(view, 32).querySelector<HTMLButtonElement>('[data-testid="m-pr-fix-question"]')!;
    expect(q.textContent).toBe("Answer its question");
    q.click();
    expect(a.pr!.question).toHaveBeenCalledWith("tq:t0000032abcdef:0", "t0000032abcdef");
    const none = draw([fixRow(32, "question")]);
    expect(rowEl(none, 32).querySelector('[data-testid="m-pr-fix-question"]')!.textContent).toBe("Open its task");
  });
});

// -- the app's flow ------------------------------------------------------------------------

class Stream implements StreamLike {
  readyState = 1;
  onopen = null;
  onmessage = null;
  onerror = null;
  close() {}
}

function harness(rows: unknown[], secure: { ok: boolean; failed: string | null; label: string } = { ok: true, failed: null, label: "" }) {
  const posts: { path: string; value: any }[] = [];
  const replies: Record<string, Posted | ((v: any) => Posted)> = {};
  const gets: Record<string, Got> = {};
  const credential = vi.fn(async (_pk: Record<string, unknown>) => ({ id: "cred", response: {} }) as Record<string, unknown> | null);
  const root = document.createElement("main");
  document.body.replaceChildren(root);
  gets["/m/api/inbox"] = { ok: true, body: { rows } };
  gets["/m/api/me"] = { ok: true, body: { signed_in: true, secure } };
  let gate: Promise<void> | null = null;
  const app = new PhoneApp({
    root, onSignedOut: () => {}, signOut: async () => {},
    fetchJson: async (path) => gets[path] ?? { ok: false, why: "error", status: 404, body: { error: "not found" } },
    postJson: async (path, value) => {
      posts.push({ path, value });
      if (gate) await gate;
      const r = replies[path];
      return (typeof r === "function" ? r(value) : r) ?? { status: 200, body: { ok: true } };
    },
    getCredential: credential,
    openStream: () => new Stream(),
    setTimer: (fn) => { queueMicrotask(fn); return 0; },
  });
  const settle = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); };
  const button = (n: number, id: string) =>
    root.querySelector<HTMLButtonElement>(`[data-id="pr:${REPO}#${n}"] [data-testid="${id}"]`);
  const notice = (n: number) => root.querySelector(`[data-id="pr:${REPO}#${n}"] [data-testid="m-pr-notice"]`)!.textContent;
  return { app, root, posts, replies, gets, credential, settle, button, notice,
           hold: () => { let open!: () => void; gate = new Promise((r) => { open = r; }); return () => { gate = null; open(); }; } };
}

const OPTIONS = "/m/api/inbox/pr/options";
const ACT = "/m/api/inbox/pr";

describe("the phone's PR actions: one passkey each, bound to what was drawn", () => {
  it("Merge: options with the drawn head and card, the passkey, then the act with its assertion; the notice says each step", async () => {
    const h = harness([prRow(11)]);
    h.replies[OPTIONS] = { status: 200, body: { action: "merge", publicKey: { challenge: "AAEC" } } };
    h.replies[ACT] = { status: 200, body: { ok: true, result: "merged", method: "merge", authority: "hud" } };
    h.app.start();
    await h.settle();
    let seen = "";
    h.credential.mockImplementationOnce(async () => { seen = h.notice(11)!; return { id: "cred", response: {} }; });
    h.button(11, "m-pr-act-merge")!.click();
    await h.settle();
    const bound = { id: `pr:${REPO}#11`, action: "merge", head: headOf(11), card: cardOf(11) };
    expect(h.posts.map((p) => p.path)).toEqual([OPTIONS, ACT]);
    expect(h.posts[0].value).toEqual(bound);
    expect(h.posts[1].value).toEqual({ ...bound, assertion: { id: "cred", response: {} } });
    expect(h.credential).toHaveBeenCalledWith({ challenge: "AAEC" });
    expect(seen).toBe(ASKING_FACE_ID);
    expect(h.notice(11)).toBe("Merged.");
  });

  it("says Merging… while the act is on its way", async () => {
    const h = harness([prRow(11)]);
    h.replies[OPTIONS] = { status: 200, body: { publicKey: { challenge: "AAEC" } } };
    h.app.start();
    await h.settle();
    let release: (() => void) | null = null;
    h.credential.mockImplementationOnce(async () => { release = h.hold(); return { id: "cred", response: {} }; });
    h.button(11, "m-pr-act-merge")!.click();
    await h.settle();
    expect(h.notice(11)).toBe("Merging…");
    expect(h.button(11, "m-pr-act-merge")!.disabled).toBe(true);
    release!();
    await h.settle();
  });

  it("sends the row as drawn, not as read again: a read that landed under it does not change what is sent", async () => {
    // The mutant this pins: `prAct` looking the row up again in `app.inbox` at
    // click time would send the new head and card the owner never saw.
    const h = harness([prRow(11)]);
    h.replies[OPTIONS] = { status: 409, body: { error: "changed since you read it" } };
    h.app.start();
    await h.settle();
    const drawn = h.button(11, "m-pr-act-merge")!;
    h.app.inbox = parseInbox([prRow(11, { head: headOf(99), card: cardOf(99) })]);    // no redraw
    drawn.click();
    await h.settle();
    expect(h.posts[0].value).toEqual({ id: `pr:${REPO}#11`, action: "merge", head: headOf(11), card: cardOf(11) });
  });

  it("a 409 says the daemon's words in the row, reads the inbox again, never retries and asks no passkey", async () => {
    const h = harness([prRow(11)]);
    h.replies[OPTIONS] = { status: 409, body: { error: "changed since you read it" } };
    h.app.start();
    await h.settle();
    const reads = vi.spyOn(h.app, "loadInbox");
    h.button(11, "m-pr-act-merge")!.click();
    await h.settle();
    expect(h.posts.map((p) => p.path)).toEqual([OPTIONS]);
    expect(h.credential).not.toHaveBeenCalled();
    expect(h.notice(11)).toBe("Changed since you read it.");
    expect(reads).toHaveBeenCalled();
  });

  it("a 409 on the act itself (the head moved after the passkey) is said the same way, once", async () => {
    const h = harness([prRow(11)]);
    h.replies[OPTIONS] = { status: 200, body: { publicKey: { challenge: "AAEC" } } };
    h.replies[ACT] = { status: 409, body: { error: "changed since you read it: its head moved" } };
    h.app.start();
    await h.settle();
    h.button(11, "m-pr-act-merge")!.click();
    await h.settle();
    expect(h.posts.map((p) => p.path)).toEqual([OPTIONS, ACT]);
    expect(h.notice(11)).toBe("Changed since you read it: its head moved.");
  });

  it("a cancelled passkey does nothing", async () => {
    const h = harness([prRow(11)]);
    h.replies[OPTIONS] = { status: 200, body: { publicKey: { challenge: "AAEC" } } };
    h.credential.mockResolvedValueOnce(null);
    h.app.start();
    await h.settle();
    h.button(11, "m-pr-act-merge")!.click();
    await h.settle();
    expect(h.posts.map((p) => p.path)).toEqual([OPTIONS]);
    expect(h.notice(11)).toBe("Cancelled, or the passkey was not verified. Nothing was done.");
  });

  it("Fix, Merge as is and Update take the same flow with their own action word", async () => {
    const h = harness([prRow(11, { actions: ["merge_as_is", "fix", "update"] })]);
    h.replies[OPTIONS] = { status: 200, body: { publicKey: { challenge: "AAEC" } } };
    h.replies[ACT] = (v) => ({ status: 200, body: v.action === "fix" ? { ok: true, task_id: "t0000011abcdef" }
      : { ok: true, result: v.action === "update" ? "update requested" : "merged" } });
    h.app.start();
    await h.settle();
    for (const [action, words] of [["fix", "Fix started (task t0000011)."], ["merge_as_is", "Merged."],
                                   ["update", "Update requested"]] as const) {
      h.posts.length = 0;
      h.button(11, `m-pr-act-${action}`)!.click();
      await h.settle();
      expect(h.posts.map((p) => [p.path, p.value.action])).toEqual([[OPTIONS, action], [ACT, action]]);
      expect(h.notice(11)).toContain(words);
    }
  });

  it("the server's refusal of a limited phone is shown; a limited app hides Merge on a flagged PR", async () => {
    const h = harness([prRow(15, { flags: FLAGGED }), prRow(11)], { ok: false, failed: "serve", label: "tailscale serve" });
    h.replies[OPTIONS] = { status: 409, body: { error: "touches safety files: answer it at the desk" } };
    h.app.start();
    await h.settle();
    expect(h.button(15, "m-pr-act-merge")).toBeNull();
    expect(h.root.querySelector(`[data-id="pr:${REPO}#15"] [data-testid="m-pr-why"]`)!.textContent)
      .toContain("answer it at the desk");
    h.button(11, "m-pr-act-merge")!.click();
    await h.settle();
    expect(h.notice(11)).toBe("Touches safety files: answer it at the desk.");
  });

  it("Cancel a fix: the task's own cancel passkey flow", async () => {
    const h = harness([fixRow(31, "running")]);
    const path = "/m/api/tasks/t0000031abcdef/cancel";
    h.replies[path + "/options"] = { status: 200, body: { publicKey: { challenge: "AAEF" } } };
    h.replies[path] = { status: 200, body: { ok: true } };
    h.app.start();
    await h.settle();
    h.button(31, "m-pr-fix-cancel")!.click();
    await h.settle();
    expect(h.posts.map((p) => p.path)).toEqual([path + "/options", path]);
    expect(h.posts[1].value).toEqual({ assertion: { id: "cred", response: {} } });
    expect(h.notice(31)).toBe("Fix cancelled.");
  });
});
