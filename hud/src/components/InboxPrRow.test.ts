// PM-F on the HUD (components/InboxPrRow.tsx in InboxView): a PR row's
// layout, its buttons and what they send, arming, 409s, hold, the fix states
// and the fix card, squash wording, the summary slot and the GitHub link.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api";
import { ARM_MS } from "../lib/approvalSummary";
import { parseInbox } from "../lib/inbox";
import type { InboxSnapshot } from "../state/inbox";
import { InboxView, type InboxViewProps } from "./InboxView";
import { FLAGGED, HOSTILE, REPO, cardOf, facts, fixRow, headOf, prRow, tqRow } from "../lib/prRow.fixtures";
(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;

const snap = (raw: unknown[]): InboxSnapshot => ({ rows: parseInbox(raw)!, status: "ready", message: "", reads: 1 });
let root: Root | null = null;
let host: HTMLElement | null = null;

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  vi.useRealTimers();
});

function props(raw: unknown[], over: Partial<InboxViewProps> = {}): InboxViewProps {
  return {
    snapshot: snap(raw), approvals: [], blocked: false,
    onOpenThread: vi.fn(), onOpenTask: vi.fn(), onAnswer: vi.fn(async () => ({ ok: true })),
    onDecide: vi.fn(async () => ({ ok: true })), onReread: vi.fn(), now: () => 0,
    onMarkCheck: vi.fn(async () => ({ ok: true })), onImportChecks: vi.fn(async () => ({})),
    onMark: vi.fn(async () => ({ ok: true })), onPr: vi.fn(async () => ({ ok: true, result: "merged" })), ...over,
  };
}

function draw(raw: unknown[], over: Partial<InboxViewProps> = {}) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  const p = props(raw, over);
  act(() => root!.render(createElement(InboxView, p)));
  return p;
}

function redraw(p: InboxViewProps) {
  act(() => root!.render(createElement(InboxView, p)));
}

const arm = () => act(() => { vi.advanceTimersByTime(ARM_MS + 10); });
const flush = async () => { await act(async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); }); };
const rowOf = (n: number) => host!.querySelector(`[data-testid="inbox-row"][data-id="pr:${REPO}#${n}"]`) as HTMLElement;
const btn = (n: number, id: string) => rowOf(n).querySelector(`[data-testid="${id}"]`) as HTMLButtonElement | null;
const actions = (n: number) => Array.from(rowOf(n).querySelectorAll<HTMLButtonElement>("[data-action]")).map((b) => b.dataset.action);
const click = (b: HTMLElement) => act(() => { b.click(); });

describe("verifier: content changes must restart the 600 ms guard", () => {
  it.each([
    ["PR title", { title: "Remove the backup before migration" }],
    ["summary caveat", { summary_note: "written without the CI logs, which could not be read" }],
    ["unavailable-summary reason", { summary_reason: "the CI logs could not be read" }],
  ])("rejects a click immediately after the %s changes", async (_name, change) => {
    const base = _name === "unavailable-summary reason"
      ? { summary: null, summary_state: "failed", summary_reason: "the model did not answer in time" }
      : {};
    const p = draw([prRow(11, base)]);
    arm();
    p.snapshot = snap([prRow(11, { ...base, ...change })]);
    redraw(p);
    // A real enabled button accepts this click; no timers have elapsed since
    // the commit that changed what the owner is being asked to read.
    click(btn(11, "inbox-pr-act-merge")!);
    await flush();
    expect(p.onPr, "content changed less than 600 ms ago").not.toHaveBeenCalled();
  });
});

describe("a hold keeps Cancel", () => {
  it("a held running PR still offers Cancel the fix (a hold never stops the owner stopping a fix)", () => {
    draw([fixRow(31, "running", {}, { hold: { note: "wait for review" } })]);
    expect(btn(31, "inbox-pr-fix-cancel")).not.toBeNull();
  });
});

describe("PM-24: the layout", () => {
  it("title, then the buttons, then code's facts and flags, then the AI summary, then the link", () => {
    draw([prRow(15, { flags: FLAGGED })]);
    const order = Array.from(rowOf(15).querySelectorAll("[data-testid]")).map((e) => e.getAttribute("data-testid"))
      .filter((t) => ["inbox-title", "inbox-pr-title", "inbox-pr-actions", "inbox-pr-facts", "inbox-pr-flags",
                      "inbox-pr-summary", "inbox-pr-link"].includes(t!));
    expect(order).toEqual(["inbox-title", "inbox-pr-title", "inbox-pr-actions", "inbox-pr-facts", "inbox-pr-flags",
                           "inbox-pr-summary", "inbox-pr-link"]);
  });

  it("every name is its own <bdi>; untrusted text makes no element; the model's words only in the AI slot", () => {
    draw([prRow(14, {
      facts: facts(14, { checks: "checks: failing", checkItems: ["GitGuardian [secret scanner]", HOSTILE + " [secret scanner]"],
                         conflicts: [HOSTILE] }),
      failing: [{ name: "GitGuardian", scanner: true }],
      summary: { does: "Does " + HOSTILE, blocked: "Blocked " + HOSTILE }, flags: FLAGGED,
    })]);
    const r = rowOf(14);
    expect(r.querySelectorAll("img, script, [onerror]").length).toBe(0);
    const names = Array.from(r.querySelectorAll('[data-testid="inbox-pr-facts"] bdi')).map((b) => b.textContent);
    expect(names).toEqual(["GitGuardian", HOSTILE.replace("‮", "") + " [secret scanner]", HOSTILE.replace("‮", "")]);
    // Exactly one code label: the lookalike's suffix is part of its name.
    expect(r.querySelectorAll('[data-testid="inbox-pr-scanner"]').length).toBe(1);
    const ai = r.querySelector('[data-testid="inbox-pr-summary"]')!;
    expect(ai.querySelector(".as-tag")!.textContent).toBe("AI SUMMARY");
    expect(r.querySelector('[data-testid="inbox-pr-does"]')!.textContent).toBe("Does: Does " + HOSTILE.replace("‮", ""));
    // The model's words appear nowhere outside the AI slot.
    const outside = Array.from(r.querySelectorAll(":scope [data-testid]")).filter((e) => !ai.contains(e) && !e.contains(ai))
      .map((e) => e.textContent).join(" ");
    expect(outside).not.toContain("Blocked " + HOSTILE.replace("‮", ""));
    expect(r.querySelector('[data-testid="inbox-pr-flags"]')!.textContent)
      .toBe("touches safety files: CLAUDE.md (agent instructions); .github/workflows/ci.yml (CI)");
  });

  it("holds the summary slot from the first frame: pending says summarizing…, failed and off say why", () => {
    draw([prRow(12, { summary: null, summary_state: "pending" }),
          prRow(18, { summary: null, summary_state: "failed", summary_reason: "the model did not answer in time" }),
          prRow(19, { summary: null, summary_state: "off", summary_reason: "today's budget is used up" }),
          prRow(20, { summary_note: "checks re-running" })]);
    const slot = (n: number) => rowOf(n).querySelector('[data-testid="inbox-pr-summary"]')!;
    expect(slot(12).getAttribute("data-state")).toBe("pending");
    expect(slot(12).textContent).toBe("AI SUMMARYsummarizing…");
    expect(slot(12).classList.contains("as-ai")).toBe(true);
    expect(slot(18).textContent).toContain("AI summary unavailable: the model did not answer in time");
    expect(slot(19).textContent).toContain("AI summary unavailable: today's budget is used up");
    expect(rowOf(20).querySelector('[data-testid="inbox-pr-summary-note"]')!.textContent).toBe("summary: checks re-running");
    // Merge stays offered with no summary: the facts and the binding authorize it, never the model (PM-7).
    arm();
    expect(btn(18, "inbox-pr-act-merge")!.disabled).toBe(false);
  });

  it("shows why there are no buttons, in code's words", () => {
    draw([prRow(17, { actions: [], why: "waits on its checks" })]);
    expect(actions(17)).toEqual([]);
    expect(rowOf(17).querySelector('[data-testid="inbox-pr-why"]')!.textContent).toBe("waits on its checks");
  });

  it("names a squash before the OK (Q4)", () => {
    draw([prRow(21, { method: "squash", facts: facts(21, { method: "squash merge" }) }), prRow(11)]);
    expect(rowOf(21).querySelector('[data-testid="inbox-pr-method"]')!.textContent).toContain("squash merge");
    expect(rowOf(11).querySelector('[data-testid="inbox-pr-method"]')).toBeNull();
    const m = rowOf(21).querySelector('[data-testid="inbox-pr-method"]')!;
    const b = btn(21, "inbox-pr-act-merge")!;
    expect(b.compareDocumentPosition(m) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(m.compareDocumentPosition(rowOf(21).querySelector('[data-testid="inbox-pr-facts"]')!)
      & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("links to GitHub on the HUD (PM-25), small and after the summary; never a link to another repository", () => {
    draw([prRow(11), prRow(12, { url: "https://github.com/evil/repo/pull/12" })]);
    const link = rowOf(11).querySelector('[data-testid="inbox-pr-link"]') as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe(`https://github.com/${REPO}/pull/11`);
    expect(rowOf(11).querySelector('[data-testid="inbox-pr-summary"]')!.compareDocumentPosition(link)
      & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(rowOf(12).querySelector("a")).toBeNull();
  });
});

describe("buttons and binding", () => {
  it("offers what `actions` allows, in order; no button is focused or a default", () => {
    draw([prRow(11, { actions: ["update", "fix", "merge_as_is", "merge"] })]);
    expect(actions(11)).toEqual(["merge", "merge_as_is", "fix", "update"]);
    expect(Array.from(rowOf(11).querySelectorAll("button")).every((b) => b.type === "button" && !b.autofocus)).toBe(true);
    expect(rowOf(11).contains(document.activeElement)).toBe(false);
    expect(rowOf(11).querySelectorAll("form").length).toBe(0);
  });

  it("is disarmed for 600 ms after it appears; a click then sends nothing (AS-13)", async () => {
    const p = draw([prRow(11)]);
    const b = btn(11, "inbox-pr-act-merge")!;
    expect(b.disabled).toBe(true);
    expect(rowOf(11).querySelector('[data-testid="inbox-pr-actions"]')!.getAttribute("data-armed")).toBe("false");
    // A click that lands anyway (the handler checks again).
    b.disabled = false;
    click(b);
    await flush();
    expect(p.onPr).not.toHaveBeenCalled();
    // React drops a click on a disabled button, so the handler's own check is
    // reached directly: called before arming, it sends nothing.
    const handler = Object.entries(btn(11, "inbox-pr-act-merge")!).find(([k]) => k.startsWith("__reactProps"))![1] as
      { onClick: () => void };
    act(() => handler.onClick());
    await flush();
    expect(p.onPr).not.toHaveBeenCalled();
    const armedNow = () => rowOf(11).querySelector('[data-testid="inbox-pr-actions"]')!.getAttribute("data-armed");
    act(() => { vi.advanceTimersByTime(ARM_MS - 100); });
    expect(armedNow()).toBe("false");
    act(() => { vi.advanceTimersByTime(200); });
    expect(armedNow()).toBe("true");
    expect(btn(11, "inbox-pr-act-merge")!.disabled).toBe(false);
    click(btn(11, "inbox-pr-act-merge")!);
    await flush();
    expect(p.onPr).toHaveBeenCalledTimes(1);
  });

  it("disarms again when the summary, head or card changes, and when the fix's summary lands", () => {
    const p = draw([prRow(11)]);
    arm();
    expect(btn(11, "inbox-pr-act-merge")!.disabled).toBe(false);
    for (const d of [{ summary: { does: "Something else", blocked: "" } }, { head: headOf(12) }, { card: cardOf(13) }]) {
      p.snapshot = snap([prRow(11, d)]);
      redraw(p);
      expect(btn(11, "inbox-pr-act-merge")!.disabled, JSON.stringify(d)).toBe(true);
      arm();
      expect(btn(11, "inbox-pr-act-merge")!.disabled).toBe(false);
    }
    p.snapshot = snap([fixRow(37, "ready", { summary_state: "pending" }, { actions: ["merge"] })]);
    redraw(p);
    arm();
    p.snapshot = snap([fixRow(37, "ready", { summary: "it lands", summary_state: "ready" }, { actions: ["merge"] })]);
    redraw(p);
    expect(btn(37, "inbox-pr-act-merge")!.disabled).toBe(true);
  });

  it("sends exactly the head and card the row drew, once per press, to the action's route", async () => {
    const p = draw([prRow(11, { actions: ["merge", "merge_as_is", "fix", "update"] })]);
    arm();
    const r = rowOf(11).querySelector('[data-testid="inbox-pr"]')!;
    const shown = { id: `pr:${REPO}#11`, head: r.getAttribute("data-head"), card: r.getAttribute("data-card") };
    expect(shown.head).toBe(headOf(11));
    for (const [action, route, extra] of [["merge", "merge", {}], ["merge_as_is", "merge", { action: "merge_as_is" }],
                                          ["fix", "fix", {}], ["update", "update", {}]] as const) {
      (p.onPr as any).mockClear();
      click(btn(11, `inbox-pr-act-${action}`)!);
      click(btn(11, `inbox-pr-act-${action}`)!);           // a second press while the first is on its way
      await flush();
      expect(p.onPr).toHaveBeenCalledTimes(1);
      expect(p.onPr).toHaveBeenCalledWith(route, { ...shown, ...extra });
    }
  });

  it("never reads the head or card again at click time: a row the daemon changed but this view still draws sends what it drew", async () => {
    // The mutant this pins: a click handler that looks the row up again (in the
    // snapshot, or by a fetch) would send the new head and card, and a stale
    // OK would pass PM-D's check. The store's rows object is changed under the
    // view without a render: what is sent must be what is drawn.
    const p = draw([prRow(11)]);
    arm();
    const rows = p.snapshot.rows as any[];
    rows[0].detail.head = headOf(99);
    rows[0].detail.card = cardOf(99);
    click(btn(11, "inbox-pr-act-merge")!);
    await flush();
    expect(p.onPr).toHaveBeenCalledWith("merge", { id: `pr:${REPO}#11`, head: headOf(11), card: cardOf(11) });
    expect(p.onReread).toHaveBeenCalledTimes(2);          // once on mount, once after the act: never before it
  });

  it("a 409 says the daemon's words in the row, reads the inbox again and does not retry", async () => {
    const onPr = vi.fn(async () => { throw new ApiError(409, "changed since you read it: its head moved"); });
    const p = draw([prRow(11)], { onPr });
    arm();
    click(btn(11, "inbox-pr-act-merge")!);
    await flush();
    expect(onPr).toHaveBeenCalledTimes(1);
    const notice = rowOf(11).querySelector('[data-testid="inbox-pr-notice"]')!;
    expect(notice.textContent).toBe("Changed since you read it: its head moved.");
    expect(notice.getAttribute("data-tone")).toBe("amber");
    expect(p.onReread).toHaveBeenCalledTimes(2);
    await flush();
    act(() => { vi.advanceTimersByTime(5000); });
    expect(onPr).toHaveBeenCalledTimes(1);
  });

  it("says the result in code's words: merged, or not merged and why", async () => {
    const onPr = vi.fn(async () => ({ ok: false, result: "head moved" }));
    draw([prRow(11)], { onPr });
    arm();
    click(btn(11, "inbox-pr-act-merge")!);
    await flush();
    expect(rowOf(11).querySelector('[data-testid="inbox-pr-notice"]')!.textContent).toBe("Not merged: head moved.");
  });

  it("under a card nothing acts: buttons off, and the handlers refuse", async () => {
    const p = draw([prRow(11)], { blocked: true });
    arm();
    const b = btn(11, "inbox-pr-act-merge")!;
    expect(b.disabled).toBe(true);
    b.disabled = false;
    click(b);
    await flush();
    expect(p.onPr).not.toHaveBeenCalled();
  });
});

describe("hold (Q3), the desk's own", () => {
  it("Hold… opens a note box; Set hold posts {id, note} once", async () => {
    const p = draw([prRow(11)]);
    expect(rowOf(11).querySelector('[data-testid="inbox-pr-hold-box"]')).toBeNull();
    click(btn(11, "inbox-pr-hold-open")!);
    const input = rowOf(11).querySelector('[data-testid="inbox-pr-hold-note"]') as HTMLInputElement;
    expect(document.activeElement).not.toBe(input);
    expect(btn(11, "inbox-pr-hold-set")!.disabled).toBe(true);
    act(() => {
      const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      set.call(input, "waits on the robot test");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    click(btn(11, "inbox-pr-hold-set")!);
    await flush();
    expect(p.onPr).toHaveBeenCalledWith("hold", { id: `pr:${REPO}#11`, note: "waits on the robot test" });
  });

  it("a held row shows its note, Clear hold, and no action button anywhere", async () => {
    const p = draw([prRow(22, { hold: { note: "waits on the robot " + HOSTILE, set_at: "x" }, actions: ["merge"],
                                why: "on hold: waits on the robot" }),
                    fixRow(23, "ready", { summary: "s", summary_state: "ready" },
                           { hold: { note: "h", set_at: "x" }, actions: ["merge", "fix"] })]);
    arm();
    for (const n of [22, 23]) {
      expect(actions(n), String(n)).toEqual([]);
      expect(btn(n, "inbox-pr-hold-open")).toBeNull();
    }
    expect(rowOf(22).querySelector('[data-testid="inbox-pr-hold"]')!.textContent)
      .toBe("On hold: waits on the robot " + HOSTILE.replace("‮", "") + "Clear hold");
    expect(rowOf(22).querySelectorAll("img").length).toBe(0);
    click(btn(22, "inbox-pr-hold-clear")!);
    await flush();
    expect(p.onPr).toHaveBeenCalledWith("hold", { id: `pr:${REPO}#22`, clear: true });
  });
});

describe("the fix (PM-21 to PM-23)", () => {
  const fixText = (n: number) => rowOf(n).querySelector('[data-testid="inbox-pr-fix-text"]')!.textContent;

  it("each state in code's words", () => {
    draw([fixRow(31, "running", { note: "fix running" }), fixRow(32, "question"),
          fixRow(33, "stopped", { note: "fix stopped: failed" }, { actions: ["fix"] }, { rank: 4 }),
          fixRow(34, "failing", { note: "fix pushed; checks still fail" }, { actions: ["fix"] }, { rank: 4 }),
          fixRow(35, "pushed", { note: "fix pushed; checks running" }, {}, { rank: 4 }),
          fixRow(36, "rewritten", {}, {}, { rank: 4 }), fixRow(39, "start_failed", { note: "fix could not start: x" })]);
    expect(fixText(31)).toBe("fix running (task t0000031)");
    expect(fixText(32)).toBe("fix waits on your answer (task t0000032)");
    expect(fixText(33)).toBe("fix stopped: failed");
    expect(fixText(34)).toBe("fix pushed; checks still fail");
    expect(fixText(35)).toBe("fix pushed; checks running");
    expect(fixText(36)).toBe("branch rewritten since your Fix");
    expect(rowOf(36).querySelector('[data-testid="inbox-pr-fix"]')!.getAttribute("data-tone")).toBe("amber");
    expect(fixText(39)).toBe("fix could not start: x");
    expect(btn(31, "inbox-pr-fix-cancel")).not.toBeNull();
    expect(btn(32, "inbox-pr-fix-cancel")).not.toBeNull();
    expect(btn(33, "inbox-pr-fix-cancel")).toBeNull();
    expect(btn(33, "inbox-pr-act-fix")!.textContent).toBe("Fix again");
    expect(btn(34, "inbox-pr-act-fix")!.textContent).toBe("Fix again");
  });

  it("rank 4 rows (a fix that ended) sit under 'Waiting on you'", () => {
    draw([fixRow(33, "stopped", {}, { actions: ["fix"] }, { rank: 4 }), prRow(11)]);
    expect(rowOf(33).closest('[data-testid="inbox-group-you"]')).not.toBeNull();
    const ids = Array.from(host!.querySelectorAll('[data-testid="inbox-group-you"] [data-testid="inbox-row"]'))
      .map((r) => r.getAttribute("data-id"));
    expect(ids).toEqual([`pr:${REPO}#33`, `pr:${REPO}#11`]);
  });

  it("Cancel posts {id} to fix/cancel", async () => {
    const p = draw([fixRow(31, "running")]);
    click(btn(31, "inbox-pr-fix-cancel")!);
    await flush();
    expect(p.onPr).toHaveBeenCalledWith("fix/cancel", { id: `pr:${REPO}#31` });
  });

  it("a fix waiting on an answer links to its question row, or its task when the row is not here", () => {
    const p = draw([tqRow("t0000032abcdef"), fixRow(32, "question")]);
    const q = btn(32, "inbox-pr-fix-question")!;
    expect(q.textContent).toBe("Answer its question");
    click(q);
    expect((document.activeElement as HTMLElement).getAttribute("data-id")).toBe("tq:t0000032abcdef:0");
    expect(p.onOpenTask).not.toHaveBeenCalled();
    p.snapshot = snap([fixRow(32, "question")]);
    redraw(p);
    expect(btn(32, "inbox-pr-fix-question")!.textContent).toBe("Open its task");
    click(btn(32, "inbox-pr-fix-question")!);
    expect(p.onOpenTask).toHaveBeenCalledWith("t0000032abcdef");
  });

  it("the fix card: Fixed — merge?, its own AI slot, commits, the CI amber, the flags, and Merge bound to the new head", async () => {
    const p = draw([fixRow(37, "ready", { note: "fixed: " + HOSTILE + " — merge?", summary: "merged main in " + HOSTILE,
                                          summary_state: "ready", commits: 2, ci_changed: true },
                           { actions: ["merge"], checks: "passing", merge_state: "CLEAN", flags: FLAGGED, head: headOf(77),
                             card: cardOf(77) }, { rank: 4 })]);
    const r = rowOf(37);
    expect(fixText(37)).toBe("Fixed — merge?");
    const slot = r.querySelector('[data-testid="inbox-pr-fix-summary"]')!;
    expect(slot.querySelector(".as-tag")!.textContent).toBe("AI SUMMARY");
    expect(slot.textContent).toContain("merged main in " + HOSTILE.replace("‮", ""));
    // The daemon's note (which carries the model's words) is never drawn as code's line.
    const fix = r.querySelector('[data-testid="inbox-pr-fix"]')!;
    const codeText = Array.from(fix.childNodes).filter((n) => !(n as Element).matches?.('[data-testid="inbox-pr-fix-summary"]'))
      .map((n) => n.textContent).join(" ");
    expect(codeText).not.toContain("evil");
    expect(r.querySelector('[data-testid="inbox-pr-fix-commits"]')!.textContent).toBe("2 commits since your Fix");
    expect(r.querySelector('[data-testid="inbox-pr-fix-ci"]')!.textContent).toBe("the fix changed CI configuration");
    expect(r.querySelector('[data-testid="inbox-pr-flags"]')).not.toBeNull();
    expect(r.querySelectorAll("img").length).toBe(0);
    arm();
    click(btn(37, "inbox-pr-act-merge")!);
    await flush();
    expect(p.onPr).toHaveBeenCalledWith("merge", { id: `pr:${REPO}#37`, head: headOf(77), card: cardOf(77) });
  });
});
