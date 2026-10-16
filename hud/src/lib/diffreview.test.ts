// The Quick review's sentences (docs/plans/2026-10-15-diff-quick-review.md, D1/D2/D8).

import { describe, expect, it } from "vitest";
import type { DiffReview } from "../types";
import {
  QUICK_REVIEW_HELP, costLine, countLine, fallbackChain, headerLine, location, scopeLine, target,
} from "./diffreview";

const base = (over: Partial<DiffReview> = {}): DiffReview => ({
  task_id: "t1", state: "done", started: "", finished: "", trying: null, harness: "claude",
  model: "claude-haiku-5-5", label: "Claude · Haiku 5.5", fallbacks: [], summary: "", findings: [],
  scope: { files_total: 3, files_reviewed: 3, skipped: [], bytes: 10, cap: 150000 }, error: null,
  cost: { usd: 0, tokens: 0 }, ...over,
});

describe("the header names who reviewed and why any fallback happened", () => {
  it("says the harness and model alone when nothing fell back", () => {
    expect(headerLine(base())).toBe("Claude · Haiku 5.5");
  });
  it("chains every harness passed over, with its reason", () => {
    const r = base({
      harness: "codex", label: "Codex · GPT-6 Luna",
      fallbacks: [{ harness: "claude", name: "Claude", reason: "5h at 82%", tried: false }],
    });
    expect(headerLine(r)).toBe("Claude 5h at 82% → Codex · GPT-6 Luna");
    expect(fallbackChain([
      { harness: "claude", name: "Claude", reason: "5h at 82%", tried: false },
      { harness: "codex", name: "Codex", reason: "busy: another Codex turn is running", tried: true },
    ])).toBe("Claude 5h at 82% → Codex busy: another Codex turn is running");
  });
  it("says what is running, and how a failed or cancelled one ended", () => {
    expect(headerLine(base({ state: "running", trying: "codex", harness: null,
      fallbacks: [{ harness: "claude", name: "Claude", reason: "CLI missing", tried: false }] })))
      .toBe("Claude CLI missing → reviewing with Codex…");
    expect(headerLine(base({ state: "failed", error: "no harness could run the review" })))
      .toBe("no review (no harness could run the review)");
    expect(headerLine(base({ state: "cancelled" }))).toBe("cancelled");
  });
});

describe("counts, scope and cost", () => {
  it("counts by severity", () => {
    const f = (severity: "bug" | "risk" | "nit") => ({ severity, file: "a", line: 1, why: "w", in_diff: true });
    expect(countLine([])).toBe("No findings");
    expect(countLine([f("bug"), f("nit"), f("bug"), f("risk")])).toBe("2 bugs · 1 risk · 1 nit");
  });
  it("says plainly which files were not reviewed (D1)", () => {
    expect(scopeLine(base())).toBe("");
    const r = base({ scope: { files_total: 9, files_reviewed: 2, cap: 150000, bytes: 1,
      skipped: ["a", "b", "c", "d", "e", "f", "g"] } });
    expect(scopeLine(r)).toBe("Reviewed 2 of 9 files. Not reviewed (over the 150 KB cap): a, b, c, d, e, and 2 more.");
  });
  it("shows dollars (Claude's as an equivalent), else tokens", () => {
    expect(costLine(base({ cost: { usd: 0.0021, tokens: 10 } }))).toBe("≈$0.0021 equivalent");
    expect(costLine(base({ harness: "v1", cost: { usd: 0.03, tokens: 0 } }))).toBe("$0.03");
    expect(costLine(base({ harness: "codex", cost: { usd: 0, tokens: 1234 } }))).toBe("1.2k tokens");
    expect(costLine(base())).toBe("");
  });
});

describe("a finding's place", () => {
  it("opens only a file in this diff", () => {
    const f = { severity: "bug" as const, file: "src/a.py", line: 12, why: "w", in_diff: true };
    expect(location(f)).toBe("src/a.py:12");
    expect(target(f, ["src/a.py"])).toEqual({ file: "src/a.py", line: 12 });
    expect(target({ ...f, line: null }, ["src/a.py"])).toEqual({ file: "src/a.py", line: null });
    expect(target({ ...f, in_diff: false }, ["src/a.py"])).toBeNull();
    expect(target(f, ["other.py"])).toBeNull();
  });
  it("help says it is quick and read-only, not a full review", () => {
    expect(QUICK_REVIEW_HELP).toMatch(/read-only/);
    expect(QUICK_REVIEW_HELP).toMatch(/not a full review/);
  });
});
