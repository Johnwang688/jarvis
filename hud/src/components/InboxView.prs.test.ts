// The inbox's PR rows (inbox B3, IN-4): rank 5, "Waiting on you", the PR's
// title as text, a link only to the PR's own validated GitHub URL, nothing
// posted by any click; "PR list unavailable" says which and why, and the
// inbox never reads "Nothing waiting" while it shows.

import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { parseInbox, prOf, prUnavailableOf, waitingCount } from "../lib/inbox";
import type { InboxSnapshot } from "../state/inbox";
import { InboxView, type InboxViewProps } from "./InboxView";
(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;

const HOSTILE = '<img src=x onerror="window.__pwned=1">‮evil';
const REPO = "Johnwang688/Calc";

const prRow = (over: Record<string, unknown> = {}, detail: Record<string, unknown> = {}) => ({
  id: `pr:${REPO}#7`, kind: "pr", rank: 5, title: "PR #7 is ready to merge",
  blocks: `${REPO} · branch claude/x · checks passing`, project_id: "p1",
  asked_at: "2026-10-20T10:00:00+00:00",
  detail: { repo: REPO, number: 7, title: "Add multiply " + HOSTILE, branch: "claude/x", head: "a".repeat(40),
            checks: "passing", merge_state: "CLEAN", url: `https://github.com/${REPO}/pull/7`, ...detail },
  ...over,
});
const unavailable = () => ({
  id: "prx:list", kind: "pr_unavailable", rank: 5, title: "PR list unavailable for 2 repositories",
  blocks: "open pull requests may be waiting; they cannot be read now",
  detail: { repos: [{ repo: "o/a", reason: "gh is not logged in", last_read: null },
                    { repo: "o/b", reason: "gh did not answer in time " + HOSTILE, last_read: null }] },
});
const blocked = { id: "task:b", kind: "task", rank: 4, title: "task b is blocked", blocks: "x", task_id: "b",
                  detail: { task_id: "b", state: "blocked", brief: "" } };
const decision = { id: "dec:d0123456789ab", kind: "decision", rank: 6, title: "chat t asks for your decision",
                   blocks: "x", thread_id: "t", detail: { decision_id: "d0123456789ab", question: "q",
                   options: ["a", "b"], default: null, why: "", answer: null } };

const snap = (raw: unknown[]): InboxSnapshot => ({ rows: parseInbox(raw)!, status: "ready", message: "", reads: 1 });

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
});

function draw(raw: unknown[], over: Partial<InboxViewProps> = {}) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  const props: InboxViewProps = {
    snapshot: snap(raw), approvals: [], blocked: false,
    onOpenThread: vi.fn(), onOpenTask: vi.fn(), onAnswer: vi.fn(async () => ({ ok: true })),
    onDecide: vi.fn(async () => ({ ok: true })), onReread: vi.fn(), now: () => 0,
    onMarkCheck: vi.fn(async () => ({ ok: true })), onImportChecks: vi.fn(async () => ({})),
    onMark: vi.fn(async () => ({ ok: true })), ...over,
  };
  act(() => root!.render(createElement(InboxView, props)));
  return props;
}

const rowOf = (id: string) => host!.querySelector(`[data-testid="inbox-row"][data-id="${id}"]`) as HTMLElement;

describe("prOf", () => {
  const row = (detail: Record<string, unknown>) => parseInbox([prRow({}, detail)])![0];

  it("links only this repository's own PR number on github.com", () => {
    expect(prOf(row({}))!.url).toBe(`https://github.com/${REPO}/pull/7`);
    expect(prOf(row({ url: `https://github.com/${REPO.toLowerCase()}/pull/7` }))!.url).not.toBeNull();
    for (const url of [`https://github.com/other/repo/pull/7`, `https://github.com/${REPO}/pull/8`,
                       `http://github.com/${REPO}/pull/7`, `https://github.com/${REPO}/pull/7/files`,
                       `https://github.com.evil.example/${REPO}/pull/7`, `https://x@github.com/${REPO}/pull/7`,
                       "javascript:alert(1)", null, 7]) {
      expect(prOf(row({ url }))!.url, String(url)).toBeNull();
    }
  });

  it("is null for another kind or a detail that is not a PR's", () => {
    expect(prOf(parseInbox([blocked])![0])).toBeNull();
    expect(prOf(row({ repo: "no-slash" }))).toBeNull();
    expect(prOf(row({ repo: "a/b/c" }))).toBeNull();
    expect(prOf(row({ number: "7" }))).toBeNull();
    expect(prOf(row({ number: 0 }))).toBeNull();
    // As the daemon's `github_repo`: no name that is or starts with a dot.
    for (const repo of ["./x", "../x", "o/.", "o/..", ".hidden/r", "o/.git"]) {
      expect(prOf(row({ repo, url: `https://github.com/${repo}/pull/7` })), repo).toBeNull();
    }
  });

  it("takes the title as one line with format characters out", () => {
    const t = prOf(row({ title: "a\nb c‮d" }))!.title;
    expect(t).toBe("a b cd");
  });

  it("reads an unavailable row's repositories", () => {
    expect(prUnavailableOf(parseInbox([unavailable()])![0])!.map((r) => r.repo)).toEqual(["o/a", "o/b"]);
    expect(prUnavailableOf(parseInbox([prRow()])![0])).toBeNull();
  });
});

describe("the view", () => {
  it("puts a PR under 'Waiting on you' between a blocked task and a decision", () => {
    draw([decision, prRow(), blocked]);
    const ids = Array.from(host!.querySelectorAll('[data-testid="inbox-row"]')).map((r) => r.getAttribute("data-id"));
    expect(ids).toEqual(["task:b", `pr:${REPO}#7`, "dec:d0123456789ab"]);
    expect(rowOf(`pr:${REPO}#7`).closest('[data-testid="inbox-group-you"]')).not.toBeNull();
  });

  it("shows the PR's title as text and links only to its PR; a click posts nothing", () => {
    const props = draw([prRow()]);
    const r = rowOf(`pr:${REPO}#7`);
    expect(r.querySelector('[data-testid="inbox-pr-title"]')!.textContent).toBe('Add multiply <img src=x onerror="window.__pwned=1">evil');
    expect(r.querySelector("img")).toBeNull();
    const link = r.querySelector('[data-testid="inbox-pr-link"]') as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe(`https://github.com/${REPO}/pull/7`);
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
    expect(link.getAttribute("target")).toBe("_blank");
    act(() => { link.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true })); });
    act(() => { r.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
    expect(props.onAnswer).not.toHaveBeenCalled();
    expect(props.onDecide).not.toHaveBeenCalled();
    expect(props.onOpenThread).not.toHaveBeenCalled();
    expect(props.onOpenTask).not.toHaveBeenCalled();
    // No answer box, button or chat link on a PR row.
    expect(r.querySelectorAll("input, button").length).toBe(0);
  });

  it("does not link a URL for another repository", () => {
    draw([prRow({}, { url: "https://github.com/someone/else/pull/7" })]);
    expect(rowOf(`pr:${REPO}#7`).querySelector('[data-testid="inbox-pr-link"]')).toBeNull();
  });

  it("follows no link under a card: click, middle click or Enter", () => {
    draw([prRow()], { blocked: true });
    const link = rowOf(`pr:${REPO}#7`).querySelector('[data-testid="inbox-pr-link"]') as HTMLAnchorElement;
    const events = [new MouseEvent("click", { bubbles: true, cancelable: true }),
                    new MouseEvent("auxclick", { bubbles: true, cancelable: true, button: 1 }),
                    new MouseEvent("mousedown", { bubbles: true, cancelable: true, button: 1 }),
                    new KeyboardEvent("keydown", { bubbles: true, cancelable: true, key: "Enter" })];
    for (const ev of events) {
      act(() => { link.dispatchEvent(ev); });
      expect(ev.defaultPrevented, ev.type).toBe(true);
    }
  });

  it("follows its link when no card is up", () => {
    draw([prRow()]);
    const link = rowOf(`pr:${REPO}#7`).querySelector('[data-testid="inbox-pr-link"]') as HTMLAnchorElement;
    const aux = new MouseEvent("auxclick", { bubbles: true, cancelable: true, button: 1 });
    const key = new KeyboardEvent("keydown", { bubbles: true, cancelable: true, key: "Enter" });
    act(() => { link.dispatchEvent(aux); link.dispatchEvent(key); });
    expect(aux.defaultPrevented || key.defaultPrevented).toBe(false);
  });

  it("says the PR list is unavailable, and never 'Nothing waiting'", () => {
    draw([unavailable()]);
    const lines = Array.from(host!.querySelectorAll('[data-testid="inbox-pr-unavailable"]')).map((e) => e.textContent);
    expect(lines).toEqual(["o/a: gh is not logged in", 'o/b: gh did not answer in time <img src=x onerror="window.__pwned=1">evil']);
    expect(host!.querySelector('[data-testid="inbox-empty"]')).toBeNull();
    expect(host!.querySelector('[data-testid="inbox-summary"]')!.textContent).toBe("1 waiting on you");
    expect(waitingCount(parseInbox([unavailable(), prRow()])!)).toBe(2);
  });
});

describe("PR rows and marks (inbox B4 with B3)", () => {
  it("a PR row offers Snooze and Dismiss beside its link; 'PR list unavailable' offers none, even if served markable", async () => {
    const p = draw([prRow({ markable: true }), { ...unavailable(), markable: true }]);
    const marks = (id: string) => Array.from(rowOf(id).querySelectorAll('[data-testid="inbox-marks"] button'))
      .map((b) => b.textContent);
    expect(marks(`pr:${REPO}#7`)).toEqual(["Snooze 1 h", "Until tomorrow", "1 week", "Dismiss"]);
    expect(marks("prx:list")).toEqual([]);
    expect(rowOf(`pr:${REPO}#7`).querySelector("a")).not.toBeNull();
    act(() => (rowOf(`pr:${REPO}#7`).querySelector('[data-testid="inbox-dismiss"]') as HTMLButtonElement).click());
    await act(async () => { await Promise.resolve(); });
    expect(p.onMark).toHaveBeenCalledWith(`pr:${REPO}#7`, { dismiss: true });
  });

  it("a dismissed PR leaves the count; a 'hidden' served on 'PR list unavailable' is ignored", () => {
    const gone = { snoozed_until: null, dismissed_at: "2026-10-28T09:00:00Z" };
    const rows = parseInbox([prRow({ markable: true, hidden: gone }), { ...unavailable(), hidden: gone }])!;
    expect(waitingCount(rows)).toBe(1);
    draw([prRow({ markable: true, hidden: gone }), { ...unavailable(), hidden: gone }]);
    expect(rowOf("prx:list")).not.toBeNull();
    expect(rowOf(`pr:${REPO}#7`)).toBeNull();
  });
});

