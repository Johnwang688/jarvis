import { describe, expect, it, vi } from "vitest";
import { PhoneApp, type StreamLike } from "./app";
import type { Got, Posted } from "./net";
import { cardView, parseDetail, type CardState } from "./card";
import { inboxView, type InboxAnswers } from "./views";
import { parseInbox, type InboxRow } from "../lib/inbox";
import { argText, browserBody, originText, reasonText } from "../lib/approvalCard";

// Hostile strings, as a model or a page could write them: each must arrive as text.
const H = ['<img src=x onerror="window.__pwned=1">', "<script>window.__pwned=2</script>",
  '<b>bold</b><a href="javascript:alert(1)">x</a>'].join(" ");
const MARKUP = "img, script, b, svg, iframe, a[href^='javascript'], [onerror], [onload]";
const LONG = "echo " + "x".repeat(9000) + " " + H;

function noMarkup(node: Element) {
  expect(node.querySelectorAll(MARKUP).length).toBe(0);
  expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
}

function row(over: Partial<InboxRow> & { id: string; kind: string; rank: number }): unknown {
  return { title: "t", blocks: "b", project_id: null, thread_id: null, task_id: null, asked_at: null,
    expires_at: null, origin: "", detail: {}, ...over };
}

const ORDINARY = {
  card: { req_id: "r1", tool: "Bash", args: { command: LONG, description: H, timeout: 5 }, command: LONG, reason: H,
    layer: "reviewer", origin: 'task 0000beef "' + H + '"', headline: "", asked_at: "2026-10-25T10:00:00+00:00",
    timeout_s: 600, allowlistable: true, provider: "claude", thread_id: "aaaaaaaa" },
  private: false, withheld: false, secure: { ok: true, failed: null, label: "", reason: "" },
  can: { deny: true, allow: true, always: true }, why: "",
};

function state(detail: unknown, over: Partial<CardState> = {}): CardState {
  return { rowId: "appr:r1", row: null, detail: parseDetail(detail), refused: null, busy: false, rule: null, notice: "",
    ...over };
}

const noop = { back() {}, deny() {}, allow() {}, always() {}, confirmAlways() {}, cancelAlways() {} };

describe("the shared card words (lib/approvalCard.ts)", () => {
  it("are the HUD card's words", () => {
    expect(argText("a b")).toBe("a b");
    expect(argText({ k: [1, 2] })).toBe('{"k":[1,2]}');
    expect(argText(5)).toBe("5");
    expect(originText("task x")).toBe("REQUESTED BY task x");
    expect(originText("")).toBe("");
    expect(reasonText("why", "reviewer")).toBe("[reviewer] why");
    expect(reasonText("why", "")).toBe("why");
    expect(reasonText("", "reviewer")).toBe("");
  });
});

describe("the answering card (P-4)", () => {
  it("shows the whole card as text: every argument, the whole command, no cut", () => {
    const view = cardView(state(ORDINARY), noop);
    document.body.replaceChildren(view);
    noMarkup(view);
    const command = view.querySelector('[data-testid="approval-command"]')!;
    expect(command.textContent).toBe("command " + LONG);
    const args = Array.from(view.querySelectorAll('[data-testid="approval-arg"]')).map((n) => n.textContent);
    expect(args).toEqual(["command " + LONG, "description " + H, "timeout 5"]);
    expect(view.querySelector('[data-testid="approval-origin"]')!.textContent).toBe(originText(ORDINARY.card.origin));
    expect(view.querySelector(".reason")!.textContent).toBe("[reviewer] " + H);
    expect(view.querySelector('[data-testid="approval-tool"]')!.textContent).toBe("Bash");
  });

  it("puts Deny first and alone; AUTHORIZE and ALWAYS far below, out of the Tab order; nothing is a default", () => {
    const view = cardView(state(ORDINARY), noop);
    document.body.replaceChildren(view);
    const buttons = Array.from(view.querySelectorAll<HTMLButtonElement>(".m-card button"));
    expect(buttons.map((b) => b.dataset.testid)).toEqual(["approval-deny", "approval-always", "approval-allow"]);
    expect(buttons.every((b) => b.type === "button")).toBe(true);
    const deny = buttons[0], always = buttons[1], allow = buttons[2];
    expect(deny.tabIndex).toBe(0);
    expect(always.tabIndex).toBe(-1);
    expect(allow.tabIndex).toBe(-1);
    expect(view.querySelector(".m-gap")).not.toBeNull();
    expect(deny.compareDocumentPosition(view.querySelector(".m-gap")!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(view.querySelectorAll("form, input, textarea, [autofocus]").length).toBe(0);
    // A tap never leaves focus on AUTHORIZE or ALWAYS.
    const down = new MouseEvent("mousedown", { bubbles: true, cancelable: true });
    allow.dispatchEvent(down);
    expect(down.defaultPrevented).toBe(true);
    // Enter and Space on the card do nothing (Deny itself excepted).
    const card = view.querySelector<HTMLElement>('[data-testid="approval-card"]')!;
    for (const key of ["Enter", " "]) {
      const e = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
      allow.dispatchEvent(e);
      expect(e.defaultPrevented).toBe(true);
      const onCard = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
      card.dispatchEvent(onCard);
      expect(onCard.defaultPrevented).toBe(true);
      const onDeny = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
      deny.dispatchEvent(onDeny);
      expect(onDeny.defaultPrevented).toBe(false);
    }
  });

  it("a withheld card offers Deny only, and says why", () => {
    const view = cardView(state({ ...ORDINARY, withheld: true, can: { deny: true, allow: false, always: false },
      why: "this card has withheld text: answer it at the desk" }), noop);
    expect(view.querySelector<HTMLButtonElement>('[data-testid="approval-allow"]')!.disabled).toBe(true);
    expect(view.querySelector<HTMLButtonElement>('[data-testid="approval-always"]')!.disabled).toBe(true);
    expect(view.querySelector<HTMLButtonElement>('[data-testid="approval-deny"]')!.disabled).toBe(false);
    expect(view.querySelector('[data-testid="m-card-why"]')!.textContent).toBe("this card has withheld text: answer it at the desk");
  });

  it("limited: a red line, no Always", () => {
    const view = cardView(state({ ...ORDINARY, secure: { ok: false, failed: "serve", label: "tailscale serve is not the only path, or Funnel is on" },
      can: { deny: true, allow: true, always: false }, why: "limited: x" }), noop);
    expect(view.querySelector('[data-testid="m-limited"]')!.textContent).toBe("limited: tailscale serve is not the only path, or Funnel is on");
    expect(view.querySelector<HTMLButtonElement>('[data-testid="approval-always"]')!.disabled).toBe(true);
    expect(view.querySelector<HTMLButtonElement>('[data-testid="approval-allow"]')!.disabled).toBe(false);
  });

  it("a HUD-only card while limited: answer at the desk, Deny only", () => {
    const r = parseInbox([row({ id: "appr:p1", kind: "approval", rank: 1, detail: { tool: "hud_browser_act", hud_only: true } })])![0];
    const view = cardView({ rowId: r.id, row: r, detail: null, refused: "answer at the desk", busy: false, rule: null, notice: "" }, noop);
    expect(view.querySelector('[data-testid="m-card-refused"]')!.textContent).toBe("A private card: answer at the desk.");
    expect(view.querySelector<HTMLButtonElement>('[data-testid="approval-allow"]')!.disabled).toBe(true);
    expect(view.querySelector('[data-testid="approval-always"]')).toBeNull();
    expect(view.querySelector<HTMLButtonElement>('[data-testid="approval-deny"]')!.disabled).toBe(false);
  });

  it("a HUD-only card under §4.1 shows the HUD's Browser words, page text in <bdi>", () => {
    const browser = { tab_id: "0a1b2c3d", action_id: "f".repeat(32), pane: 2, page_identity: 1, document_generation: 1,
      action: "click", category: "ordinary", host: "example.com", path: "/" + H, target: H, text: "", rect: null };
    const args = { action: "click", pane: "2", target: H, text: "", file: "" };
    const asked = new Date(Date.now() - 5000).toISOString();
    const card = { req_id: "AbCdEfGhIjKl", tool: "hud_browser_act", layer: "browser-gate", thread_id: null, asked_at: asked,
      allowlistable: false, timeout_s: 120, hud_only: true, args, command: JSON.stringify(args), reason: "", headline: "",
      browser };
    const r = parseInbox([row({ id: "appr:AbCdEfGhIjKl", kind: "approval", rank: 1,
      detail: { req_id: "AbCdEfGhIjKl", tool: "hud_browser_act", hud_only: true } })])![0];
    const view = cardView({ rowId: r.id, row: r, refused: null, busy: false, rule: null, notice: "",
      detail: parseDetail({ card, private: true, withheld: false, secure: { ok: true }, can: { allow: true }, why: "" }) }, noop);
    document.body.replaceChildren(view);
    noMarkup(view);
    const body = view.querySelector('[data-testid="browser-approval-details"]')!;
    expect(body.querySelector(".headline")!.textContent).toBe("Authorize this one Browser action?");
    const words = browserBody({ kind: "action", reqId: card.req_id, command: card.command,
      browser: browser as never });
    expect(Array.from(body.querySelectorAll("p")).map((p) => p.textContent))
      .toEqual(words.paras.map((p) => p.segs.map((s) => s.text).join("")));
    expect(body.querySelectorAll("bdi").length).toBeGreaterThanOrEqual(4);
    expect(view.querySelector('[data-testid="approval-command"]')!.textContent).toBe(card.command);
    expect(view.querySelector<HTMLButtonElement>('[data-testid="approval-allow"]')!.disabled).toBe(false);
    expect(view.querySelector('[data-testid="approval-always"]')).toBeNull();
  });

  it("Always shows its rule, then asks again before the passkey", () => {
    const confirm = vi.fn();
    const view = cardView(state(ORDINARY, { rule: { words: "From now on, allow run_command for any command whose stem is 'git'",
      entry: { tool: "run_command", prefix: "git" }, publicKey: {} } }), { ...noop, confirmAlways: confirm });
    expect(view.querySelector('[data-testid="m-rule-words"]')!.textContent).toContain("'git'");
    expect(view.querySelector('[data-testid="m-rule-entry"]')!.textContent).toBe('{"tool":"run_command","prefix":"git"}');
    expect(view.querySelector('[data-testid="approval-allow"]')).toBeNull();
    const button = view.querySelector<HTMLButtonElement>('[data-testid="m-rule-confirm"]')!;
    expect(button.tabIndex).toBe(-1);
    button.click();
    expect(confirm).toHaveBeenCalledTimes(1);
  });
});

// -- the app's answering flow ---------------------------------------------------------

class Stream implements StreamLike {
  readyState = 1;
  onopen = null;
  onmessage = null;
  onerror = null;
  close() {}
}

function harness(rows: unknown[]) {
  const posts: { path: string; value: any }[] = [];
  const replies: Record<string, Posted> = {};
  const gets: Record<string, Got> = {};
  const credential = vi.fn(async (_pk: Record<string, unknown>) => ({ id: "cred", response: {} }) as Record<string, unknown> | null);
  const root = document.createElement("main");
  document.body.replaceChildren(root);
  gets["/m/api/inbox"] = { ok: true, body: { rows } };
  gets["/m/api/me"] = { ok: true, body: { signed_in: true, secure: { ok: true, failed: null } } };
  const app = new PhoneApp({
    root, onSignedOut: () => {}, signOut: async () => {},
    fetchJson: async (path) => gets[path] ?? { ok: false, why: "error", status: 404, body: { error: "not found" } },
    postJson: async (path, value) => { posts.push({ path, value }); return replies[path] ?? { status: 200, body: { ok: true } }; },
    getCredential: credential,
    openStream: () => new Stream(),
    setTimer: (fn) => { queueMicrotask(fn); return 0; },
  });
  const settle = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };
  const click = async (id: string) => { root.querySelector<HTMLButtonElement>(`[data-testid="${id}"]`)!.click(); await settle(); };
  return { app, root, posts, replies, gets, credential, settle, click };
}

const APPR = row({ id: "appr:r1", kind: "approval", rank: 1, title: "Claude asks to use Bash", detail: { req_id: "r1", tool: "Bash" } });

describe("the phone app answers (P-4)", () => {
  it("AUTHORIZE: a challenge for this card and answer, the passkey, then the answer with its assertion", async () => {
    const h = harness([APPR]);
    h.gets["/m/api/inbox/appr%3Ar1/detail"] = { ok: true, body: ORDINARY };
    h.replies["/m/api/inbox/appr%3Ar1/answer/options"] = { status: 200, body: { decision: "allow", publicKey: { challenge: "AAEC" } } };
    h.app.start();
    await h.settle();
    await h.click("m-open-card");
    expect(h.root.querySelector('[data-testid="approval-card"]')).not.toBeNull();
    expect(h.credential).not.toHaveBeenCalled();
    await h.click("approval-allow");
    expect(h.posts.map((p) => [p.path, p.value.decision])).toEqual([
      ["/m/api/inbox/appr%3Ar1/answer/options", "allow"], ["/m/api/inbox/appr%3Ar1/answer", "allow"]]);
    expect(h.credential).toHaveBeenCalledWith({ challenge: "AAEC" });
    expect(h.posts[1].value.assertion).toEqual({ id: "cred", response: {} });
    expect(h.root.querySelector('[data-testid="approval-card"]')).toBeNull();
    expect(h.root.querySelector('[data-testid="m-flash"]')!.textContent).toBe("Authorized.");
  });

  it("ALWAYS: the rule first, the passkey only after Confirm", async () => {
    const h = harness([APPR]);
    h.gets["/m/api/inbox/appr%3Ar1/detail"] = { ok: true, body: ORDINARY };
    h.replies["/m/api/inbox/appr%3Ar1/answer/options"] = { status: 200, body: { decision: "always",
      rule: { tool: "run_command", prefix: "git" }, rule_words: "the rule", publicKey: { challenge: "AAED" } } };
    h.app.start();
    await h.settle();
    await h.click("m-open-card");
    await h.click("approval-always");
    expect(h.root.querySelector('[data-testid="m-rule-words"]')!.textContent).toBe("the rule");
    expect(h.credential).not.toHaveBeenCalled();
    expect(h.posts.length).toBe(1);
    await h.click("m-rule-confirm");
    expect(h.credential).toHaveBeenCalledWith({ challenge: "AAED" });
    expect(h.posts[1]).toEqual({ path: "/m/api/inbox/appr%3Ar1/answer", value: { decision: "always", assertion: { id: "cred", response: {} } } });
  });

  it("DENY: one tap, the session only, no passkey", async () => {
    const h = harness([APPR]);
    h.gets["/m/api/inbox/appr%3Ar1/detail"] = { ok: true, body: ORDINARY };
    h.app.start();
    await h.settle();
    await h.click("m-open-card");
    await h.click("approval-deny");
    expect(h.posts).toEqual([{ path: "/m/api/inbox/appr%3Ar1/answer", value: { decision: "deny" } }]);
    expect(h.credential).not.toHaveBeenCalled();
  });

  it("a cancelled passkey answers nothing; a refusal shows the daemon's words", async () => {
    const h = harness([APPR]);
    h.gets["/m/api/inbox/appr%3Ar1/detail"] = { ok: true, body: ORDINARY };
    h.replies["/m/api/inbox/appr%3Ar1/answer/options"] = { status: 200, body: { publicKey: { challenge: "AAEC" } } };
    h.credential.mockResolvedValueOnce(null);
    h.app.start();
    await h.settle();
    await h.click("m-open-card");
    await h.click("approval-allow");
    expect(h.posts.map((p) => p.path)).toEqual(["/m/api/inbox/appr%3Ar1/answer/options"]);
    expect(h.root.querySelector('[data-testid="m-card-notice"]')!.textContent).toContain("Nothing was answered");
    h.replies["/m/api/inbox/appr%3Ar1/answer/options"] = { status: 409, body: { error: "answer at the desk" } };
    await h.click("approval-allow");
    expect(h.root.querySelector('[data-testid="m-card-notice"]')!.textContent).toBe("Answer at the desk.");
  });

  it("questions and decisions answer from their rows, only with the button", async () => {
    const rows = [
      row({ id: "q:aaaaaaaa:q-1", kind: "question", rank: 2, thread_id: "aaaaaaaa", detail: { text: H, options: ["npm"] } }),
      row({ id: "dec:d0123456789ab", kind: "decision", rank: 6, thread_id: "aaaaaaaa",
        detail: { decision_id: "d0123456789ab", question: "Which?", options: ["sqlite", H], default: 0, why: "w",
          answer: null, delivery: null } }),
    ];
    const h = harness(rows);
    h.replies["/m/api/inbox/decisions/d0123456789ab"] = { status: 200, body: { ok: true, note: "Sent to the asking chat." } };
    h.app.start();
    await h.settle();
    noMarkup(h.root);
    const text = h.root.querySelector<HTMLTextAreaElement>('[data-testid="m-answer-text"]')!;
    const send = h.root.querySelector<HTMLButtonElement>('[data-testid="m-answer-send"]')!;
    expect(send.disabled).toBe(true);
    text.value = "pnpm";
    text.dispatchEvent(new Event("input"));
    text.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await h.settle();
    expect(h.posts).toEqual([]);
    await h.click("m-answer-send");
    expect(h.posts).toEqual([{ path: "/m/api/inbox/q%3Aaaaaaaaa%3Aq-1/answer", value: { text: "pnpm" } }]);
    // The suggestion's tag is the phone's own element before the option's words (as the HUD's).
    const choices = Array.from(h.root.querySelectorAll(".m-choice"));
    const tag = choices[0].querySelector('[data-testid="m-suggested"]')!;
    expect(tag.textContent).toBe("suggested");
    expect(tag.compareDocumentPosition(choices[0].querySelector(".option-text")!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(choices[1].querySelector('[data-testid="m-suggested"]')).toBeNull();
    expect(choices[0].querySelector<HTMLInputElement>("input")!.checked).toBe(true);
    const radio = h.root.querySelectorAll<HTMLInputElement>('[data-testid="m-decision-option"]')[1];
    radio.checked = true;
    radio.dispatchEvent(new Event("change"));
    await h.click("m-decision-send");
    expect(h.posts[1]).toEqual({ path: "/m/api/inbox/decisions/d0123456789ab", value: { choice: 1 } });
    expect(h.root.querySelector('[data-testid="m-flash"]')!.textContent).toBe("Sent to the asking chat.");
  });

  it("the inbox says limited when §4.1 does not hold", async () => {
    const h = harness([APPR]);
    h.gets["/m/api/me"] = { ok: true, body: { secure: { ok: false, failed: "listeners", label: "something else listens on the phone's port" } } };
    h.app.start();
    await h.settle();
    expect(h.root.querySelector('[data-testid="m-limited"]')!.textContent).toBe("limited: something else listens on the phone's port");
  });

  it("read-only rows keep saying Answer at the desk without answers (the view's P-3 mode)", () => {
    const view = inboxView(parseInbox([APPR])!, () => {});
    expect(view.querySelector('[data-testid="m-desk-only"]')!.textContent).toBe("Answer at the desk");
    const answers: InboxAnswers = { openCard: () => {}, send: () => {}, busy: new Set(), notes: new Map(), drafts: new Map(), picks: new Map() };
    const live = inboxView(parseInbox([APPR, row({ id: "prop:bbbbbbbb", kind: "proposal", rank: 3, task_id: "bbbbbbbb" })])!, () => {}, answers);
    expect(live.querySelector('[data-testid="m-open-card"]')).not.toBeNull();
    expect(live.querySelector('[data-testid="m-desk-only"]')!.textContent).toBe("Withdraw at the desk");
  });
});
