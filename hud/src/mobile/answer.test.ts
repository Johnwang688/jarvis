import { describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { PhoneApp, type StreamLike } from "./app";
import type { Got, Posted } from "./net";
import { cardView, parseDetail, type CardState } from "./card";
import { inboxView, type InboxAnswers } from "./views";
import { parseInbox, type InboxRow } from "../lib/inbox";
import { argText, browserBody, originText, reasonText } from "../lib/approvalCard";
import { STALE_PENDING_MS } from "../lib/approvalSummary";
import { actionPosts, installUnlock } from "./unlock.fixtures";

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
    expect(view.querySelector(".reason")!.textContent).toBe("WHY ASKED [reviewer] " + H);
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

  it("limited: a red line, said once with what AUTHORIZE then means, no Always (M3 of the phone audit)", () => {
    const view = cardView(state({ ...ORDINARY, secure: { ok: false, failed: "serve", label: "tailscale serve is not the only path, or Funnel is on" },
      can: { deny: true, allow: true, always: false }, why: "limited: x" }), noop);
    expect(view.querySelector('[data-testid="m-limited"]')!.textContent).toBe("limited: tailscale serve is not the only path, "
      + "or Funnel is on. AUTHORIZE answers with Discord's authority; ALWAYS is off.");
    // The daemon's own "limited: …" is not said a second time.
    expect(view.querySelector('[data-testid="m-card-why"]')).toBeNull();
    expect(view.querySelectorAll(".m-limited").length).toBe(1);
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

  it("Always shows its rule, then takes one confirm tap (WS-5c: no Face ID inside the window)", () => {
    const confirm = vi.fn();
    const view = cardView(state(ORDINARY, { rule: { words: "From now on, allow run_command for any command whose stem is 'git'",
      entry: { tool: "run_command", prefix: "git" }, resolution: "r", preview: "a".repeat(64) } }), { ...noop, confirmAlways: confirm });
    expect(view.querySelector('[data-testid="m-rule-words"]')!.textContent).toContain("'git'");
    expect(view.querySelector('[data-testid="m-rule-entry"]')!.textContent).toBe('{"tool":"run_command","prefix":"git"}');
    expect(view.querySelector('[data-testid="approval-allow"]')).toBeNull();
    const button = view.querySelector<HTMLButtonElement>('[data-testid="m-rule-confirm"]')!;
    expect(button.textContent).toBe("Confirm ALWAYS");
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
  const gate: { wait: Promise<void> | null } = { wait: null };
  const credential = vi.fn(async (_pk: Record<string, unknown>) => ({ id: "cred", response: {} }) as Record<string, unknown> | null);
  const root = document.createElement("main");
  document.body.replaceChildren(root);
  gets["/m/api/inbox"] = { ok: true, body: { rows } };
  gets["/m/api/me"] = { ok: true, body: { signed_in: true, secure: { ok: true, failed: null } } };
  installUnlock(gets, replies);
  const app = new PhoneApp({
    root, onSignedOut: () => {}, signOut: async () => {},
    fetchJson: async (path) => gets[path] ?? { ok: false, why: "error", status: 404, body: { error: "not found" } },
    postJson: async (path, value) => { posts.push({ path, value }); if (gate.wait) await gate.wait; return replies[path] ?? { status: 200, body: { ok: true } }; },
    getCredential: credential,
    openStream: () => new Stream(),
    setTimer: (fn) => { queueMicrotask(fn); return 0; },
  });
  const settle = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };
  const click = async (id: string) => { root.querySelector<HTMLButtonElement>(`[data-testid="${id}"]`)!.click(); await settle(); };
  return { app, root, posts, replies, gets, credential, settle, click, gate };
}

const APPR = row({ id: "appr:r1", kind: "approval", rank: 1, title: "Claude asks to use Bash", detail: { req_id: "r1", tool: "Bash" } });

describe("the phone app answers (P-4)", () => {
  it("AUTHORIZE while locked: Face ID once (unlock/options, the passkey, unlock), then the answer with its op token", async () => {
    const h = harness([APPR]);
    h.gets["/m/api/inbox/appr%3Ar1/detail"] = { ok: true, body: ORDINARY };
    h.app.start();
    await h.settle();
    await h.click("m-item-review");
    expect(h.root.querySelector('[data-testid="approval-card"]')).not.toBeNull();
    expect(h.credential).not.toHaveBeenCalled();
    await h.click("approval-allow");
    expect(h.posts.map((p) => p.path)).toEqual(["/m/api/unlock/options", "/m/api/unlock", "/m/api/inbox/appr%3Ar1/answer"]);
    expect(h.credential).toHaveBeenCalledWith({ challenge: "AAEC" });
    expect(h.posts[1].value).toEqual({ assertion: { id: "cred", response: {} } });
    expect(Object.keys(h.posts[2].value).sort()).toEqual(["decision", "op"]);
    expect(h.posts[2].value.decision).toBe("allow");
    expect(h.posts[2].value.op).toMatch(/^o1\./);
    expect(h.root.querySelector('[data-testid="approval-card"]')).toBeNull();
    expect(h.root.querySelector('[data-testid="m-flash"]')!.textContent).toBe("Authorized.");
    // Unlocked now: the next AUTHORIZE asks nothing.
    expect(h.app.unlock.mirror.open()).toBe(true);
  });

  it("AUTHORIZE while unlocked: no Face ID, the answer alone", async () => {
    const h = harness([APPR]);
    h.gets["/m/api/inbox/appr%3Ar1/detail"] = { ok: true, body: ORDINARY };
    h.gets["/m/api/me"] = { ok: true, body: { signed_in: true, secure: { ok: true, failed: null }, unlock_s: 1800 } };
    h.app.start();
    await h.settle();
    await h.click("m-item-review");
    await h.click("approval-allow");
    expect(h.posts.map((p) => p.path)).toEqual(["/m/api/inbox/appr%3Ar1/answer"]);
    expect(h.credential).not.toHaveBeenCalled();
  });

  it("ALWAYS: the rule from the challenge-free rule read, one confirm tap, then {decision, preview, op}", async () => {
    const h = harness([APPR]);
    h.gets["/m/api/inbox/appr%3Ar1/detail"] = { ok: true, body: ORDINARY };
    h.gets["/m/api/inbox/appr%3Ar1/answer/rule"] = { ok: true, body: { rule: { tool: "run_command", prefix: "git" },
      rule_words: "the rule", resolution: "run_command", preview: "b".repeat(64) } };
    h.app.start();
    await h.settle();
    await h.click("m-item-review");
    await h.click("approval-always");
    expect(h.root.querySelector('[data-testid="m-rule-words"]')!.textContent).toBe("the rule");
    expect(h.root.querySelector('[data-testid="m-rule-confirm"]')!.textContent).toBe("Confirm ALWAYS");
    expect(h.credential).not.toHaveBeenCalled();
    expect(h.posts.length).toBe(0);
    await h.click("m-rule-confirm");
    const acts = actionPosts(h.posts);
    expect(acts.length).toBe(1);
    expect(acts[0].path).toBe("/m/api/inbox/appr%3Ar1/answer");
    expect(Object.keys(acts[0].value).sort()).toEqual(["decision", "op", "preview"]);
    expect(acts[0].value.decision).toBe("always");
    expect(acts[0].value.preview).toBe("b".repeat(64));
  });

  it("ALWAYS: a changed rule (409) is read again and shown; nothing is sent until a new confirm tap", async () => {
    const h = harness([APPR]);
    h.gets["/m/api/inbox/appr%3Ar1/detail"] = { ok: true, body: ORDINARY };
    h.gets["/m/api/me"] = { ok: true, body: { signed_in: true, secure: { ok: true }, unlock_s: 1800 } };
    h.gets["/m/api/inbox/appr%3Ar1/answer/rule"] = { ok: true, body: { rule: { tool: "run_command", prefix: "git" },
      rule_words: "the old rule", resolution: "r", preview: "b".repeat(64) } };
    h.app.start();
    await h.settle();
    await h.click("m-item-review");
    await h.click("approval-always");
    h.replies["/m/api/inbox/appr%3Ar1/answer"] = { status: 409, body: { error: "the rule changed: read it again" } };
    h.gets["/m/api/inbox/appr%3Ar1/answer/rule"] = { ok: true, body: { rule: { tool: "run_command", prefix: "git push" },
      rule_words: "the new rule", resolution: "r", preview: "c".repeat(64) } };
    await h.click("m-rule-confirm");
    expect(actionPosts(h.posts).length).toBe(1);
    expect(h.root.querySelector('[data-testid="m-rule-words"]')!.textContent).toBe("the new rule");
    expect(h.root.querySelector('[data-testid="m-card-notice"]')!.textContent).toBe("The rule changed: read it again, then confirm.");
    await h.settle();
    expect(actionPosts(h.posts).length).toBe(1);
    h.replies["/m/api/inbox/appr%3Ar1/answer"] = { status: 200, body: { ok: true } };
    await h.click("m-rule-confirm");
    const acts = actionPosts(h.posts);
    expect(acts.length).toBe(2);
    expect(acts[1].value.preview).toBe("c".repeat(64));
    expect(acts[1].value.op).not.toBe(acts[0].value.op);
  });

  it("DENY: one tap, the session only, no passkey and no unlock", async () => {
    const h = harness([APPR]);
    h.gets["/m/api/inbox/appr%3Ar1/detail"] = { ok: true, body: ORDINARY };
    h.app.start();
    await h.settle();
    await h.click("m-item-review");
    await h.click("approval-deny");
    expect(h.posts).toEqual([{ path: "/m/api/inbox/appr%3Ar1/answer", value: { decision: "deny" } }]);
    expect(h.credential).not.toHaveBeenCalled();
  });

  it("a cancelled Face ID answers nothing; a refusal shows the daemon's words", async () => {
    const h = harness([APPR]);
    h.gets["/m/api/inbox/appr%3Ar1/detail"] = { ok: true, body: ORDINARY };
    h.credential.mockResolvedValueOnce(null);
    h.app.start();
    await h.settle();
    await h.click("m-item-review");
    await h.click("approval-allow");
    expect(h.posts.map((p) => p.path)).toEqual(["/m/api/unlock/options"]);
    expect(h.root.querySelector('[data-testid="m-card-notice"]')!.textContent).toBe("Cancelled, or Face ID did not verify: nothing was sent.");
    h.replies["/m/api/inbox/appr%3Ar1/answer"] = { status: 409, body: { error: "answer at the desk" } };
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
    // PX-3a: the decision's options are chips; the suggestion's tag is the phone's own element
    // before the option's words (as the HUD's), and nothing is chosen until the owner taps one.
    const chips = Array.from(h.root.querySelectorAll<HTMLButtonElement>('[data-kind="decision"] [data-testid="m-option"]'));
    expect(chips.length).toBe(2);
    const tag = chips[0].querySelector('[data-testid="m-suggested"]')!;
    expect(tag.textContent).toBe("suggested");
    expect(tag.compareDocumentPosition(chips[0].querySelector(".option-text")!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(chips[1].querySelector('[data-testid="m-suggested"]')).toBeNull();
    expect(h.root.querySelectorAll('[data-kind="decision"] ul.options, [data-kind="decision"] input[type=radio]').length).toBe(0);
    expect(h.posts.length).toBe(1);
    chips[1].click();
    await h.settle();
    expect(h.posts[1]).toEqual({ path: "/m/api/inbox/decisions/d0123456789ab", value: { choice: 1 } });
    // The receipt is the phone's own copy of the option, not the daemon's note.
    expect(h.root.querySelector('[data-testid="m-flash"]')!.textContent).toBe("Sent: " + H);
    noMarkup(h.root);
  });

  // QH: an approval deferred during quiet hours.
  const DEFER = (detail: Record<string, unknown> = {}, over: Record<string, unknown> = {}) => row({
    id: "defer:D1", kind: "deferred", rank: 4, thread_id: "aaaaaaaa", title: "Bash is waiting for your morning approval",
    detail: { deferral_id: "D1", tool: "Bash", grantable: true, expired: false, hud_only: false,
      card: { tool: "Bash", command: "make deploy " + H, reason: H, args: { command: "make deploy" } }, ...detail },
    ...over } as never);

  it("Approve and retry on a deferred row: a gated action (Face ID while locked), bound to the deferral in its URL", async () => {
    const h = harness([DEFER()]);
    h.replies["/m/api/inbox/defer%3AD1/answer"] = { status: 200, body: { ok: true, delivery: "queued", note: "Approved once for 30 minutes." } };
    h.app.start();
    await h.settle();
    noMarkup(h.root);
    expect(h.root.querySelector('[data-testid="m-command"]')!.textContent).toBe("make deploy " + H);
    expect(h.credential).not.toHaveBeenCalled();
    await h.click("m-deferred-retry");
    expect(h.posts.map((p) => p.path)).toEqual(["/m/api/unlock/options", "/m/api/unlock", "/m/api/inbox/defer%3AD1/answer"]);
    expect(Object.keys(h.posts[2].value).sort()).toEqual(["action", "op"]);
    expect(h.posts[2].value.action).toBe("retry");
    expect(h.credential).toHaveBeenCalledWith({ challenge: "AAEC" });
    expect(h.root.querySelector('[data-testid="m-flash"]')!.textContent).toBe("Approved once for 30 minutes.");
  });

  it("a cancelled Face ID approves nothing; Leave it needs no passkey", async () => {
    const h = harness([DEFER()]);
    h.credential.mockResolvedValueOnce(null);
    h.app.start();
    await h.settle();
    await h.click("m-deferred-retry");
    expect(h.posts.map((p) => p.path)).toEqual(["/m/api/unlock/options"]);
    expect(h.root.querySelector('[data-testid="m-answer-note"]')!.textContent).toContain("nothing was sent");
    h.credential.mockClear();
    await h.click("m-deferred-leave");
    expect(h.posts[1]).toEqual({ path: "/m/api/inbox/defer%3AD1/answer", value: { action: "leave" } });
    expect(h.credential).not.toHaveBeenCalled();
  });

  it("a Browser card or a day-old deferred row offers Leave it only", async () => {
    for (const r of [DEFER({ hud_only: true }), DEFER({ grantable: false }), DEFER({}, { rank: 8 }),
                     DEFER({ why_not: "codex" }), DEFER({ more: 4, deferral_id: null })]) {
      const h = harness([r]);
      h.app.start();
      await h.settle();
      expect(h.root.querySelector('[data-testid="m-deferred-retry"]')).toBeNull();
      expect(h.root.querySelector('[data-testid="m-deferred-leave"]')).not.toBeNull();
    }
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
    const answers: InboxAnswers = { openCard: () => {}, send: () => {}, retry: () => {}, busy: new Set(), notes: new Map(), drafts: new Map(), picks: new Map() };
    const live = inboxView(parseInbox([APPR, row({ id: "prop:bbbbbbbb", kind: "proposal", rank: 3, task_id: "bbbbbbbb" })])!, () => {}, answers);
    expect(live.querySelector('[data-testid="m-item-review"]')).not.toBeNull();
    expect(live.querySelector('[data-testid="m-desk-only"]')!.textContent).toBe("Withdraw at the desk");
  });
});

describe("AS: the phone's card and rows", () => {
  const withSummary = (extra: Record<string, unknown>) => ({ ...ORDINARY, card: { ...ORDINARY.card,
    where: { project: "Calc", branch: "main", folder: "~/c" }, ...extra } });

  it("buttons before the body; collapsed with a summary; the toggle opens it; the AI line is text", () => {
    const toggles: boolean[] = [];
    const acts = { ...noop, toggleCommand: (open: boolean) => toggles.push(open) };
    const view = cardView(state(withSummary({ summary_state: "ready", summary: H })), acts);
    document.body.replaceChildren(view);
    noMarkup(view);
    const card = view.querySelector('[data-testid="approval-card"]')!;
    const deny = view.querySelector('[data-testid="approval-deny"]')!, body = card.querySelector(".m-card-body")!;
    expect(deny.compareDocumentPosition(body) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const allow = view.querySelector('[data-testid="approval-allow"]')!;
    expect(allow.compareDocumentPosition(body) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(view.querySelector('[data-testid="approval-summary"]')!.textContent).toContain("AI SUMMARY");
    expect(view.querySelector('[data-testid="approval-where"]')!.textContent).toBe("WHERE project Calc branch main folder ~/c");
    expect(view.querySelector('[data-testid="approval-where-folder"] bdi')!.textContent).toBe("~/c");
    expect(view.querySelector('[data-testid="approval-command"]')).toBeNull();
    expect(view.querySelector('[data-testid="approval-command-preview"]')).not.toBeNull();
    const toggle = view.querySelector<HTMLButtonElement>('[data-testid="approval-command-toggle"]')!;
    expect(toggle.tabIndex).toBe(-1);
    toggle.click();
    expect(toggles).toEqual([true]);
    const open = cardView(state(withSummary({ summary_state: "ready", summary: "x" }), { expanded: true }), acts);
    expect(open.querySelector('[data-testid="approval-command"]')!.textContent).toBe("command " + LONG);
  });

  it("Where: each field in its own box; a forged folder stays in it (review of #330, MED 1)", () => {
    const view = cardView(state(withSummary({ where: { project: "Calc", branch: "feature",
      folder: "~/tmp/x · Jarvis · main · ~/projects/Jarvis" } })), { ...noop, toggleCommand: () => {} });
    noMarkup(view);
    expect(view.querySelector('[data-testid="approval-where-project"]')!.textContent).toBe("project Calc");
    expect(view.querySelector('[data-testid="approval-where-branch"]')!.textContent).toBe("branch feature");
    expect(view.querySelector('[data-testid="approval-where-folder"]')!.textContent).toBe("folder ~/tmp/x ? Jarvis ? main ? ~/projects/Jarvis");
    // The old one-line string is not drawn.
    const old = cardView(state(withSummary({ where: "Calc · main · ~/c" })), { ...noop, toggleCommand: () => {} });
    expect(old.querySelector('[data-testid="approval-where"]')).toBeNull();
  });

  it("a summary still pending 10 s after the ask shows the command expanded (review of #330, LOW 3)", () => {
    const asked = new Date(Date.now() - STALE_PENDING_MS - 1000).toISOString();
    const stale = cardView(state(withSummary({ summary_state: "pending", asked_at: asked })), { ...noop, toggleCommand: () => {} });
    expect(stale.querySelector('[data-testid="approval-summary"]')!.getAttribute("data-state")).toBe("none");
    expect(stale.querySelector('[data-testid="approval-command"]')!.textContent).toBe("command " + LONG);
    const fresh = cardView(state(withSummary({ summary_state: "pending", asked_at: new Date().toISOString() })),
      { ...noop, toggleCommand: () => {} });
    expect(fresh.querySelector('[data-testid="approval-command"]')).toBeNull();
  });

  it("no summary: expanded; a summary the daemon said failed: expanded", () => {
    for (const extra of [{}, { summary_state: "failed" }, { summary_state: "off" }]) {
      const view = cardView(state(withSummary(extra)), { ...noop, toggleCommand: () => {} });
      expect(view.querySelector('[data-testid="approval-command"]')!.textContent).toBe("command " + LONG);
    }
  });

  it("an approval row's buttons come first; its summary and code lines show as text", () => {
    const raw = row({ id: "appr:r1", kind: "approval", rank: 1, detail: { ...withSummary({ summary_state: "ready", summary: H }).card } });
    const parsed = parseInbox([raw])!;
    const answers: InboxAnswers = { openCard() {}, send() {}, retry() {}, busy: new Set(), notes: new Map(), drafts: new Map(),
      picks: new Map(), expanded: new Map(), toggle() {} };
    const view = inboxView(parsed, () => {}, answers);
    noMarkup(view);
    const open = view.querySelector('[data-testid="m-item-review"]')!, detail = view.querySelector('[data-testid="m-detail"]')!;
    expect(open.compareDocumentPosition(detail) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(view.querySelector('[data-testid="m-row-summary"]')!.textContent).toContain("AI SUMMARY");
    expect(view.querySelector('[data-testid="m-row-why"]')!.textContent).toContain("WHY ASKED");
    expect(view.querySelector('[data-testid="m-command"]')).toBeNull();
    expect(view.querySelector('[data-testid="m-command-preview"]')).not.toBeNull();
  });
});


// PX-3a: one-tap answer chips.
describe("one-tap answer chips (PX-3a)", () => {
  const Q = row({ id: "q:aaaaaaaa:q-1", kind: "question", rank: 2, thread_id: "aaaaaaaa",
    detail: { text: "Which scope?", options: ["Only user-visible", "Everything"] } });
  const TQ = row({ id: "tq:0000beef:0", kind: "task_question", rank: 2, task_id: "0000beef",
    detail: { text: "Which db?", options: ["pg", H] } });
  const DEC = row({ id: "dec:d0123456789ab", kind: "decision", rank: 6, thread_id: "aaaaaaaa",
    detail: { decision_id: "d0123456789ab", question: "Which?", options: ["sqlite", "pg"], default: 1, why: "w",
      answer: null, delivery: null } });
  const chipsOf = (h: ReturnType<typeof harness>, kind: string) =>
    Array.from(h.root.querySelectorAll<HTMLButtonElement>(`[data-kind="${kind}"] [data-testid="m-option"]`));
  const start = async (rows: unknown[]) => { const h = harness(rows); h.app.start(); await h.settle(); return h; };

  it("render for a question, a task question and an open decision, as real buttons at least 48 px tall by rule", async () => {
    const h = await start([Q, TQ, DEC, APPR]);
    expect(chipsOf(h, "question").map((c) => c.querySelector(".option-text")!.textContent)).toEqual(["Only user-visible", "Everything"]);
    expect(chipsOf(h, "task_question").map((c) => c.querySelector(".option-text")!.textContent)).toEqual(["pg", H]);
    expect(chipsOf(h, "decision").length).toBe(2);
    expect(chipsOf(h, "approval").length).toBe(0);
    for (const c of chipsOf(h, "question")) {
      expect(c.tagName).toBe("BUTTON");
      expect(c.type).toBe("button");
      expect(c.getAttribute("aria-label")).toBe(c.textContent!.trim());
      expect(c.disabled).toBe(false);
    }
    expect(h.root.querySelector('[data-kind="question"] [data-testid="m-chips"]')!.getAttribute("role")).toBe("group");
    // The approval keeps its card button and gains no Write your own.
    expect(h.root.querySelector('[data-kind="approval"] [data-testid="m-item-review"]')).not.toBeNull();
    expect(h.root.querySelector('[data-kind="approval"] [data-testid="m-answer-write"]')).toBeNull();
    // The options are not listed twice, and Write your own is still there.
    expect(h.root.querySelectorAll('[data-kind="question"] ul.options').length).toBe(0);
    expect(h.root.querySelector('[data-kind="question"] [data-testid="m-answer-write"] [data-testid="m-answer-text"]')).not.toBeNull();
    noMarkup(h.root);
    const css = readFileSync(join(__dirname, "items.css"), "utf8");
    expect(css).toMatch(/\.m-chips > \.tap\.m-chip\s*\{[^}]*min-height:\s*3rem/);
    expect(css).toMatch(/\.m-chips\s*\{[^}]*flex-wrap:\s*wrap/);
    expect(css).not.toMatch(/url\s*\(/);
  });

  it("a question with no options keeps just its box", async () => {
    const h = await start([row({ id: "q:aaaaaaaa:q-2", kind: "question", rank: 2, thread_id: "aaaaaaaa", detail: { text: "Why?", options: [] } })]);
    expect(chipsOf(h, "question").length).toBe(0);
    expect(h.root.querySelector('[data-testid="m-answer-text"]')).not.toBeNull();
  });

  it("one tap sends the exact option once, and the receipt is the local copy, not the server's text", async () => {
    const h = await start([Q]);
    h.replies["/m/api/inbox/q%3Aaaaaaaaa%3Aq-1/answer"] = { status: 200, body: { ok: true, note: "Server says: Everything" } };
    chipsOf(h, "question")[0].click();
    await h.settle();
    expect(h.posts).toEqual([{ path: "/m/api/inbox/q%3Aaaaaaaaa%3Aq-1/answer", value: { text: "Only user-visible" } }]);
    expect(h.root.querySelector('[data-testid="m-flash"]')!.textContent).toBe("Sent: Only user-visible");
  });

  it("a long option is sent whole, never cut at the display's 200 characters, and the receipt says it whole", async () => {
    const LONGOPT = "Rewrite the history of the repo and " + "x".repeat(190) + " but NEVER delete originals";
    expect(Array.from(LONGOPT).length).toBeGreaterThan(240);
    const h = await start([row({ id: "q:aaaaaaaa:q-9", kind: "question", rank: 2, thread_id: "aaaaaaaa",
      detail: { text: "How?", options: [LONGOPT, "no"] } })]);
    const chips = chipsOf(h, "question");
    expect(chips[0].querySelector(".option-text")!.textContent).toBe(LONGOPT);
    chips[0].click();
    await h.settle();
    expect(h.posts[0].value).toEqual({ text: LONGOPT });
    expect(h.posts[0].value.text).not.toContain("…");
    expect(h.root.querySelector('[data-testid="m-flash"]')!.textContent).toBe("Sent: " + LONGOPT);
  });

  it("an option too long to have whole gets no chip: it is shown, and the others keep theirs", async () => {
    const HUGE = "y".repeat(2500);
    const h = await start([row({ id: "q:aaaaaaaa:q-8", kind: "question", rank: 2, thread_id: "aaaaaaaa",
      detail: { text: "How?", options: [HUGE, "ok"] } })]);
    expect(chipsOf(h, "question").map((c) => c.querySelector(".option-text")!.textContent)).toEqual(["ok"]);
    expect(h.root.querySelector('[data-testid="m-option-long"]')!.textContent).toContain("write it below");
    expect(h.root.querySelector('[data-testid="m-answer-text"]')).not.toBeNull();
  });

  it("the chip rules are scoped to the answer chips, not Review's chat-jump buttons", () => {
    const css = readFileSync(join(__dirname, "items.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
    const selectors = css.match(/[^{}]+(?=\{)/g)!.map((x) => x.trim());
    for (const sel of selectors) if (sel.includes("m-chip") && !sel.includes("m-chips")) throw new Error(sel);
    expect(selectors.filter((x) => x.includes("m-chip")).every((x) => x.startsWith(".m-chips"))).toBe(true);
  });

  it("a task question's chip sends its option text", async () => {
    const h = await start([TQ]);
    chipsOf(h, "task_question")[0].click();
    await h.settle();
    expect(h.posts).toEqual([{ path: "/m/api/inbox/tq%3A0000beef%3A0/answer", value: { text: "pg" } }]);
  });

  it("a double tap, or another chip while it is on its way, sends once; the chips are off meanwhile", async () => {
    const h = await start([Q]);
    let release = () => {};
    h.gate.wait = new Promise<void>((r) => { release = r; });
    const [first, second] = chipsOf(h, "question");
    first.click();
    first.click();
    second.click();
    await h.settle();
    expect(h.posts.length).toBe(1);
    const during = chipsOf(h, "question");
    expect(during.length).toBe(2);
    expect(during.every((c) => c.disabled)).toBe(true);
    during[1].click();
    during[0].click();
    await h.settle();
    expect(h.posts.length).toBe(1);
    h.gate.wait = null;
    release();
    await h.settle();
    expect(h.posts.length).toBe(1);
    expect(h.root.querySelector('[data-testid="m-flash"]')!.textContent).toBe("Sent: Only user-visible");
  });

  it("a failure re-enables the chips and shows the error; a second try sends again", async () => {
    const h = await start([Q]);
    const path = "/m/api/inbox/q%3Aaaaaaaaa%3Aq-1/answer";
    h.replies[path] = { status: 500, body: { error: "the daemon failed" } };
    chipsOf(h, "question")[1].click();
    await h.settle();
    expect(h.root.querySelector('[data-testid="m-answer-note"]')!.textContent).toBe("The daemon failed.");
    expect(h.root.querySelector('[data-testid="m-flash"]')).toBeNull();
    expect(chipsOf(h, "question").every((c) => !c.disabled)).toBe(true);
    h.replies[path] = { status: 200, body: { ok: true } };
    chipsOf(h, "question")[1].click();
    await h.settle();
    expect(h.posts.map((p) => p.value)).toEqual([{ text: "Everything" }, { text: "Everything" }]);
  });

  it("a 409 says it was already answered or gone, as today", async () => {
    const h = await start([Q]);
    h.replies["/m/api/inbox/q%3Aaaaaaaaa%3Aq-1/answer"] = { status: 409, body: { error: "already answered or gone" } };
    chipsOf(h, "question")[0].click();
    await h.settle();
    expect(h.root.querySelector('[data-testid="m-answer-note"]')!.textContent).toBe("Already answered or gone.");
  });

  it("a decision's chip sends its index", async () => {
    const h = await start([DEC]);
    h.replies["/m/api/inbox/decisions/d0123456789ab"] = { status: 200, body: { ok: true, note: "Sent to the asking chat." } };
    expect(chipsOf(h, "decision")[1].querySelector('[data-testid="m-suggested"]')).not.toBeNull();
    chipsOf(h, "decision")[0].click();
    await h.settle();
    expect(h.posts).toEqual([{ path: "/m/api/inbox/decisions/d0123456789ab", value: { choice: 0 } }]);
    expect(h.root.querySelector('[data-testid="m-flash"]')!.textContent).toBe("Sent: sqlite");
  });

  it("Write your own still sends only from its button, and keeps the server's note", async () => {
    const h = await start([Q]);
    const text = h.root.querySelector<HTMLTextAreaElement>('[data-testid="m-answer-text"]')!;
    const send = h.root.querySelector<HTMLButtonElement>('[data-testid="m-answer-send"]')!;
    expect(send.disabled).toBe(true);
    text.value = "Only the footer";
    text.dispatchEvent(new Event("input"));
    text.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await h.settle();
    expect(h.posts).toEqual([]);
    await h.click("m-answer-send");
    expect(h.posts).toEqual([{ path: "/m/api/inbox/q%3Aaaaaaaaa%3Aq-1/answer", value: { text: "Only the footer" } }]);
  });
});
