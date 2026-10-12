// The status store's key decides when a watcher hears a change. Since the
// Diff pane's list is memoized (PR #67, M1), a change the key misses is never
// drawn: its summary said "on main" after `git switch -c feature` (same
// commit, same files) for as long as nothing else moved (delta review of
// PR #67, NIT). The branch and `truncated` are in the key.

import { describe, expect, it } from "vitest";
import { GitStatusStore } from "./gitStatus";
import type { GitStatus } from "../types";

const status = (o: Partial<GitStatus> = {}): GitStatus => ({
  repo: true, head: "c0ffee1", branch: "main", truncated: false,
  files: { "a.cpp": "modified" }, dirs: {}, ...o,
});

const settle = async () => {
  for (let k = 0; k < 20; k++) await Promise.resolve();
};

/** A store whose polls answer `answers` in turn (a fresh object each time), never on a timer. */
async function watched(answers: GitStatus[]) {
  let i = 0;
  const s = new GitStatusStore({
    fetch: async () => JSON.parse(JSON.stringify(answers[Math.min(i++, answers.length - 1)])),
    now: () => 0,
    visible: () => true,
  }, 1e9);
  const unsub = s.subscribe("p", () => {});
  await settle(); // the first answer, read on subscribing
  return { s, unsub };
}

describe("the git status store's key", () => {
  it("a new branch at the same commit with the same files is a change", async () => {
    const { s, unsub } = await watched([status(), status(), status({ branch: "feature" })]);
    const v0 = s.version("p");
    expect(v0).toBeGreaterThan(0);
    await s.refresh("p");
    expect(s.version("p")).toBe(v0); // the same answer: nothing heard
    await s.refresh("p");
    expect(s.version("p")).toBe(v0 + 1);
    expect(s.get("p")?.branch).toBe("feature");
    unsub();
  });

  it("a truncated flip is a change, both ways", async () => {
    const { s, unsub } = await watched([status(), status(), status({ truncated: true }), status()]);
    const v0 = s.version("p");
    await s.refresh("p");
    expect(s.version("p")).toBe(v0);
    await s.refresh("p");
    expect(s.version("p")).toBe(v0 + 1);
    await s.refresh("p");
    expect(s.version("p")).toBe(v0 + 2);
    unsub();
  });
});
