// PR rows in each state for the tests (PM-F), in the daemon's shape after
// PM-C's summaries, PM-D's hold and card and PM-E's fix (the same states as
// tests/face/hud_v2_pr_rows.py). Data only.

export const REPO = "Johnwang688/Calc";
export const HOSTILE = '<img src=x onerror="window.__pwned=1">‮evil';

export const headOf = (n: number) => n.toString(16).padStart(2, "0").slice(-2).repeat(20);
export const cardOf = (n: number) => n.toString(16).padStart(2, "0").slice(-2).replace(/0/g, "c").repeat(32);

const part = (key: string, text: string, items: string[] = [], more = 0) => ({ key, text, items, more });

export function facts(n: number, over: { checks?: string; checkItems?: string[]; conflicts?: string[]; mergeState?: string;
                                         method?: string } = {}) {
  const parts = [part("pr", `${REPO} #${n}`), part("branch", "claude/multiply → main"),
                 part("checks", over.checks ?? "checks: passing", over.checkItems ?? [])];
  if (over.conflicts) parts.push(part("conflicts", "conflicts:", over.conflicts));
  if (over.mergeState) parts.push(part("merge_state", over.mergeState));
  parts.push(part("files", "3 files +40 −2"), part("head", `head ${headOf(n).slice(0, 7)}`),
             part("method", over.method ?? "merge commit"));
  return parts;
}

export function prRow(n: number, detail: Record<string, unknown> = {}, over: Record<string, unknown> = {}) {
  return {
    id: `pr:${REPO}#${n}`, kind: "pr", rank: 5, title: `PR #${n} is ready to merge`,
    blocks: `${REPO} · by johnw · branch claude/multiply · checks passing`, project_id: null, thread_id: null,
    task_id: null, asked_at: "2026-10-29T08:00:00+00:00", expires_at: null, origin: "",
    detail: {
      repo: REPO, number: n, title: "Add multiply " + HOSTILE, branch: "claude/multiply", base: "main", author: "johnw",
      fork: false, head: headOf(n), checks: "passing", merge_state: "CLEAN", url: `https://github.com/${REPO}/pull/${n}`,
      facts: facts(n), flags: { flagged: false, items: [], more: 0, incomplete: false }, failing: [], conflicts: null,
      files: null, summary: { does: "Adds a multiply button " + HOSTILE, blocked: "" }, summary_state: "ready",
      summary_note: "", summary_reason: "", actions: ["merge"], why: "", method: "merge", hold: null, card: cardOf(n),
      fix: null, ...detail,
    },
    ...over,
  };
}

export const FLAGGED = { flagged: true, items: [{ name: "CLAUDE.md", families: ["agent instructions"] },
                                                { name: ".github/workflows/ci.yml", families: ["CI", "made-up"] }],
                         more: 0, incomplete: false };

export function fixRow(n: number, state: string, fix: Record<string, unknown> = {}, detail: Record<string, unknown> = {},
                       over: Record<string, unknown> = {}) {
  return prRow(n, {
    checks: "failing", merge_state: "UNSTABLE", actions: [], why: "checks fail",
    facts: facts(n, { checks: "checks: failing", checkItems: ["test (3.14)"] }),
    fix: { state, task_id: `t${String(n).padStart(7, "0")}abcdef`, from_head: "9".repeat(40), commits: null,
           ci_changed: null, note: "", summary: null, summary_state: null, count: 1, max: 3, ...fix },
    ...detail,
  }, over);
}

export const tqRow = (taskId: string) => ({
  id: `tq:${taskId}:0`, kind: "task_question", rank: 2, title: "task asks you", blocks: "x", project_id: null,
  thread_id: null, task_id: taskId, asked_at: null, expires_at: null, origin: "",
  detail: { task_id: taskId, index: 0, text: "Merge main in, or rebase?", options: [] },
});
