// The Diff tab's Quick review button and findings panel (2026-10-15,
// docs/plans/2026-10-15-diff-quick-review.md D2, D5, D6, D8): start, the
// running state with its harness and Cancel, polling to the result, plain-text
// findings that open their file:line, and the "diff changed" line.

import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const world = vi.hoisted(() => ({
  views: [] as any[],          // answers to GET, in order (the last repeats)
  started: 0,
  cancelled: 0,
  startView: null as any,
  cancelView: null as any,
  startError: null as any,
  failReads: 0,                // the next N reads throw
  reads: 0,
}));

vi.mock("../api", () => ({
  api: {
    diffReview: async () => {
      world.reads += 1;
      if (world.failReads > 0) {
        world.failReads -= 1;
        throw new Error("HTTP 502");
      }
      const v = world.views.length > 1 ? world.views.shift() : world.views[0];
      return JSON.parse(JSON.stringify(v));
    },
    startDiffReview: async () => {
      world.started += 1;
      if (world.startError) throw world.startError;
      return JSON.parse(JSON.stringify(world.startView));
    },
    cancelDiffReview: async () => {
      world.cancelled += 1;
      return JSON.parse(JSON.stringify(world.cancelView));
    },
  },
}));

import { POLL_MAX_MS, POLL_MS, QuickReviewButton, QuickReviewPanel, pollDelay, useQuickReview } from "./DiffReview";

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;

const record = (over: any = {}) => ({
  task_id: "t1", state: "done", started: "", finished: "", trying: null, harness: "claude",
  model: "claude-haiku-5-5", label: "Claude · Haiku 5.5", fallbacks: [], summary: "One off-by-one.",
  findings: [], scope: { files_total: 2, files_reviewed: 2, skipped: [], bytes: 10, cap: 150000 },
  error: null, cost: { usd: 0.002, tokens: 900 }, ...over,
});
const running = (trying = "claude", fallbacks: any[] = []) =>
  ({ review: record({ state: "running", trying, harness: null, label: null, findings: [], fallbacks }), outdated: false });

const flush = () => act(async () => {
  for (let i = 0; i < 8; i++) await Promise.resolve();
});

let host: HTMLDivElement;
let unmount: () => void = () => {};
const opened: [string, number | null][] = [];

function Harness(props: { taskId: string | null }) {
  const review = useQuickReview(props.taskId);
  return createElement("div", null,
    createElement(QuickReviewButton, { review }),
    createElement(QuickReviewPanel, { review, paths: ["app.py", "lib/x.py"],
      onOpen: (f: string, l: number | null) => opened.push([f, l]) }));
}

function mount(taskId: string | null = "t1") {
  host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  unmount = () => {
    act(() => root.unmount());
    host.remove();
  };
  act(() => root.render(createElement(Harness, { taskId })));
}

const button = () => host.querySelector('[data-testid="quick-review"]') as HTMLButtonElement;
const panel = () => host.querySelector('[data-testid="qreview"]') as HTMLElement | null;

beforeEach(() => {
  world.views = [{ review: null, outdated: false }];
  world.started = world.cancelled = 0;
  world.startView = running();
  world.cancelView = running();
  world.startError = null;
  world.failReads = 0;
  world.reads = 0;
  opened.length = 0;
});
afterEach(() => {
  unmount();
  vi.useRealTimers();
});

describe("the Quick review button", () => {
  it("is one plain button with hover help, and no panel until there is a review", async () => {
    mount();
    await flush();
    expect(button().textContent).toBe("Quick review");
    expect(button().tagName).toBe("BUTTON");
    expect(button().className).not.toMatch(/pill|chip/);
    expect(button().title).toMatch(/read-only/);
    expect(button().title).toMatch(/not a full review/);
    expect(panel()).toBeNull();
  });

  it("starts, shows a spinner and the harness, polls to the result, and draws findings as text", async () => {
    vi.useFakeTimers();
    const findings = [
      { severity: "bug", file: "app.py", line: 2, why: "<img src=x onerror=alert(1)> stops one short", in_diff: true },
      { severity: "nit", file: "elsewhere.py", line: 9, why: "**not** markdown", in_diff: false },
    ];
    world.views = [{ review: null, outdated: false }];
    mount();
    await flush();
    world.views = [
      running("codex", [{ harness: "claude", name: "Claude", reason: "5h at 82%", tried: false }]),
      { review: record({ harness: "codex", label: "Codex · GPT-6 Luna", findings,
        fallbacks: [{ harness: "claude", name: "Claude", reason: "5h at 82%", tried: false }] }), outdated: false },
    ];
    act(() => button().click());
    await flush();
    expect(world.started).toBe(1);
    expect(button().dataset.state).toBe("running");
    expect(button().querySelector(".vexspinner")).not.toBeNull();
    expect(host.querySelector('[data-testid="quick-review-harness"]')!.textContent).toBe("Claude");
    expect(button().textContent).toContain("Cancel");
    // The next read: Claude was passed over, Codex is reviewing.
    await act(async () => { vi.advanceTimersByTime(POLL_MS); });
    await flush();
    expect(host.querySelector('[data-testid="quick-review-harness"]')!.textContent).toBe("Codex");
    expect(host.querySelector('[data-testid="qr-header"]')!.textContent).toBe("Claude 5h at 82% → reviewing with Codex…");
    await act(async () => { vi.advanceTimersByTime(POLL_MS); });
    await flush();
    expect(button().textContent).toBe("Quick review");
    expect(panel()!.dataset.state).toBe("done");
    expect(host.querySelector('[data-testid="qr-header"]')!.textContent).toBe("Claude 5h at 82% → Codex · GPT-6 Luna");
    const rows = host.querySelectorAll('[data-testid="qr-finding"]');
    expect(rows.length).toBe(2);
    // Plain text: the markup is characters on screen, never an element.
    expect(host.querySelector("img")).toBeNull();
    expect(host.querySelector("strong")).toBeNull();
    expect(rows[0].textContent).toContain("<img src=x onerror=alert(1)> stops one short");
    expect(rows[1].textContent).toContain("**not** markdown");
    expect(host.querySelector("a")).toBeNull();
    // A finding in the diff opens file:line; one outside it is not a button.
    expect(rows[0].tagName).toBe("BUTTON");
    expect(rows[1].tagName).toBe("DIV");
    act(() => (rows[0] as HTMLButtonElement).click());
    expect(opened).toEqual([["app.py", 2]]);
    // Polling stops with the result.
    const before = world.views.length;
    await act(async () => { vi.advanceTimersByTime(POLL_MS * 4); });
    expect(world.views.length).toBe(before);
  });

  it("pressing again while running cancels", async () => {
    world.views = [running()];
    mount();
    await flush();
    world.cancelView = { review: record({ state: "cancelled", error: "cancelled", findings: [] }), outdated: false };
    act(() => button().click());
    await flush();
    expect(world.cancelled).toBe(1);
    expect(world.started).toBe(0);
    expect(panel()!.dataset.state).toBe("cancelled");
    expect(button().textContent).toBe("Quick review");
  });

  it("says when the diff changed since the last review, and shows a refusal", async () => {
    world.views = [{ review: null, outdated: true }];
    mount();
    await flush();
    expect(panel()!.textContent).toBe("The diff changed since the last quick review.");
    world.startError = new Error("a quick review is already running for this task");
    world.views = [running()];
    act(() => button().click());
    await flush();
    expect(panel()!.textContent).toContain("a quick review is already running for this task");
    expect(button().dataset.state).toBe("running");
  });

  it("names the files left out over the cap, and folds", async () => {
    world.views = [{ review: record({
      scope: { files_total: 3, files_reviewed: 2, skipped: ["big.lock"], bytes: 9, cap: 150000 },
      findings: [{ severity: "risk", file: "app.py", line: null, why: "w", in_diff: true }] }), outdated: false }];
    mount();
    await flush();
    expect(host.querySelector('[data-testid="qr-scope"]')!.textContent)
      .toBe("Reviewed 2 of 3 files. Not reviewed (over the 150 KB cap): big.lock.");
    act(() => (host.querySelector('[data-testid="qr-finding"]') as HTMLButtonElement).click());
    expect(opened).toEqual([["app.py", null]]);
    act(() => (host.querySelector(".qr-fold") as HTMLButtonElement).click());
    expect(host.querySelector('[data-testid="qr-finding"]')).toBeNull();
  });

  it("a failed poll backs off and keeps polling until the result", async () => {
    vi.useFakeTimers();
    world.views = [running()];
    mount();
    await flush();
    world.failReads = 2;
    world.views = [{ review: record({ findings: [] }), outdated: false }];
    await act(async () => { vi.advanceTimersByTime(POLL_MS); });
    await flush();
    expect(world.reads).toBe(2);
    expect(panel()!.textContent).toContain("HTTP 502 — retrying");
    // The second wait is twice the first; nothing is read before it.
    await act(async () => { vi.advanceTimersByTime(POLL_MS * 2 - 10); });
    expect(world.reads).toBe(2);
    await act(async () => { vi.advanceTimersByTime(10); });
    await flush();
    expect(world.reads).toBe(3);
    await act(async () => { vi.advanceTimersByTime(POLL_MS * 4); });
    await flush();
    expect(world.reads).toBe(4);
    expect(panel()!.dataset.state).toBe("done");
    expect(panel()!.textContent).not.toContain("retrying");
    expect(pollDelay(0)).toBe(POLL_MS);
    expect(pollDelay(20)).toBe(POLL_MAX_MS);
  });

  it("no task, no request", async () => {
    mount(null);
    await flush();
    act(() => button().click());
    await flush();
    expect(world.started).toBe(0);
  });
});
