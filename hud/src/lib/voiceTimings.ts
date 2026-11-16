/** Window-local pipeline receipts, all on performance.now()'s clock. */
export type Stage = "endpoint" | "transcribed" | "sent" | "firstToken" | "firstAudio";
export const STAGES: readonly Stage[] = ["endpoint", "transcribed", "sent", "firstToken", "firstAudio"];
export type Marks = { speechEnd?: number } & Partial<Record<Stage, number>>;
export interface StageStat { n: number; p50: number; p90: number }

/** Diagnostics are best effort: failures must never enter the observed pipeline. */
export function observeVoiceTiming<T>(observe: () => T): T | undefined {
  try { return observe(); } catch { return undefined; }
}

export function stageDurations(m: Marks): Partial<Record<Stage | "total", number>> {
  const out: Partial<Record<Stage | "total", number>> = {};
  if (m.speechEnd === undefined || !Number.isFinite(m.speechEnd)) return out;
  let previous = m.speechEnd;
  for (const stage of STAGES) {
    const at = m[stage];
    if (at === undefined || !Number.isFinite(at) || at < previous) continue;
    out[stage] = Math.round(at - previous);
    previous = at;
    out.total = Math.round(previous - m.speechEnd);
  }
  return out;
}

export function summarize(rows: readonly Marks[]): Partial<Record<Stage | "total", StageStat>> {
  const values = rows.map(stageDurations);
  const out: Partial<Record<Stage | "total", StageStat>> = {};
  for (const stage of [...STAGES, "total"] as const) {
    const sorted = values.flatMap(row => row[stage] === undefined ? [] : [row[stage]]).sort((a, b) => a - b);
    const n = sorted.length;
    if (n) out[stage] = { n, p50: sorted[Math.ceil(0.5 * n) - 1], p90: sorted[Math.ceil(0.9 * n) - 1] };
  }
  return out;
}
