// How the Diff tab labels a changed file. Pure, so the rendering rules are
// testable without a task or a worktree.

import type { DiffFile } from "../types";

export const STATUS_LABEL: Record<DiffFile["status"], string> = {
  A: "added",
  M: "modified",
  D: "deleted",
  R: "renamed",
};

export function statusLabel(status: string): string {
  return (STATUS_LABEL as Record<string, string>)[status] || status;
}

/** `+12 −3`, with a minus sign rather than a hyphen so it reads as a count. */
export function churn(f: Pick<DiffFile, "additions" | "deletions">): string {
  return `+${f.additions || 0} −${f.deletions || 0}`;
}

export function totals(files: DiffFile[]) {
  return files.reduce(
    (acc, f) => ({
      files: acc.files + 1,
      additions: acc.additions + (f.additions || 0),
      deletions: acc.deletions + (f.deletions || 0),
    }),
    { files: 0, additions: 0, deletions: 0 },
  );
}

/**
 * The summary line. Truncation is **stated**, never silent: the contract caps
 * the patch at 1 MB, and a diff the owner believes is whole when it is not is
 * how a review misses the file that mattered.
 */
export function summaryLine(files: DiffFile[], truncated?: boolean): string {
  const t = totals(files);
  const base = `${t.files} file${t.files === 1 ? "" : "s"} · +${t.additions} −${t.deletions}`;
  return truncated ? `${base} · TRUNCATED at 1 MB — not every change is shown` : base;
}

/** Deleted files have no "after" side; the editor is told so rather than 404ing. */
export function hasAfterSide(f: Pick<DiffFile, "status">): boolean {
  return f.status !== "D";
}
