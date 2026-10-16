// The Diff tab with its Quick review (2026-10-15): the button sits in the
// tab's header, the panel above the diff, and a finding opens its file in the
// diff at its line (`DiffEditor`'s `reveal`).

import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

const seen = vi.hoisted(() => ({ editors: [] as any[], files: [] as string[] }));

vi.mock("./Editor", () => ({
  DiffEditor: (props: any) => {
    seen.editors.push({ path: props.path, reveal: props.reveal ?? null });
    return null;
  },
}));
vi.mock("../api", () => ({
  api: {
    diff: async () => ({ base: "b0", head: "h1", patch: "", files: [
      { path: "app.py", status: "M", additions: 1, deletions: 1 },
      { path: "lib/x.py", status: "A", additions: 3, deletions: 0 }] }),
    diffFile: async (_t: string, path: string) => {
      seen.files.push(path);
      return { before: "a\n", after: "b\n" };
    },
    diffReview: async () => ({ outdated: false, review: {
      task_id: "t1", state: "done", started: "", finished: "", trying: null, harness: "claude",
      model: "claude-haiku-5-5", label: "Claude · Haiku 5.5", fallbacks: [], summary: "",
      scope: { files_total: 2, files_reviewed: 2, skipped: [], bytes: 1, cap: 150000 }, error: null,
      cost: { usd: 0, tokens: 0 },
      findings: [{ severity: "bug", file: "lib/x.py", line: 3, why: "w", in_diff: true }] } }),
    startDiffReview: async () => ({ review: null, outdated: false }),
    cancelDiffReview: async () => ({ review: null, outdated: false }),
  },
}));

import { DiffTab } from "./DiffTab";
import { revealIn } from "../lib/diffreview";

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
const flush = () => act(async () => {
  for (let i = 0; i < 10; i++) await Promise.resolve();
});

let unmount: () => void = () => {};
afterEach(() => unmount());

describe("the Diff tab's Quick review", () => {
  it("puts the button in the header and opens a finding's file at its line", async () => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    unmount = () => {
      act(() => root.unmount());
      host.remove();
    };
    const picked: string[] = [];
    act(() => root.render(createElement(DiffTab, { taskId: "t1", onSelect: (p: string) => picked.push(p) })));
    await flush();
    expect(host.querySelector('.toolbar [data-testid="quick-review"]')).not.toBeNull();
    const finding = host.querySelector('[data-testid="qr-finding"]') as HTMLButtonElement;
    act(() => finding.click());
    await flush();
    expect(seen.files).toEqual(["lib/x.py"]);
    expect(picked).toEqual(["lib/x.py"]);
    expect(seen.editors.at(-1)).toEqual({ path: "lib/x.py", reveal: { file: "lib/x.py", line: 3, n: 1 } });
    // Again: the same line is asked for again (`n` moves).
    act(() => finding.click());
    await flush();
    expect(seen.editors.at(-1).reveal).toEqual({ file: "lib/x.py", line: 3, n: 2 });
    // Picking a file from the list reveals nothing.
    act(() => (host.querySelector('[data-testid="diff-app.py"]') as HTMLElement).click());
    await flush();
    expect(seen.editors.at(-1)).toEqual({ path: "app.py", reveal: null });
  });

  it("reveals on the new side, clamped to the file", () => {
    const calls: any[] = [];
    const side = {
      getModel: () => ({ getLineCount: () => 10 }),
      revealLineInCenter: (n: number) => calls.push(["reveal", n]),
      setPosition: (p: any) => calls.push(["at", p.lineNumber]),
    };
    revealIn({ getModifiedEditor: () => side }, 4);
    revealIn({ getModifiedEditor: () => side }, 99);
    revealIn({ getModifiedEditor: () => side }, undefined);
    revealIn(null, 3);
    expect(calls).toEqual([["reveal", 4], ["at", 4], ["reveal", 10], ["at", 10]]);
  });
});
