// Memory-only Direct-frame budget; eviction waits for a later tab activation.
import { frameKey } from "./frameguard";
import type { PaneNo, Workspace } from "./workspace";
export const LIVE_TABS = 3;
export const LIVE_FRAMES_WINDOW = 8;
export interface BrowserFrames { active: string[]; recent: string[] }
export const EMPTY_FRAMES: BrowserFrames = { active: [], recent: [] };
const same = (a: string[], b: string[]) => a.length === b.length && a.every((v, i) => v === b[i]);

export function browserFrames(previous: BrowserFrames, ws: Workspace, drawn: readonly PaneNo[]): BrowserFrames {
  const valid = new Set<string>(), active: string[] = [], foreground: string[] = [];
  ws.panes.forEach((p, i) => {
    if (p.view !== "browser" || !p.browser) return;
    for (const t of p.browser.tabs) if (t.url) valid.add(frameKey(i + 1, t.id));
    const key = frameKey(i + 1, p.browser.active);
    if (!valid.has(key)) return;
    active.push(key);
    if (drawn.includes((i + 1) as PaneNo)) foreground.push(key);
  });
  // A stored pane never drawn has no iframe to retain. Hidden panes already
  // visited still count, but dormant specs cannot steal the live budget.
  const changed = active.filter((k) => !previous.active.includes(k)
    && (foreground.includes(k) || previous.recent.some((old) => old.split(":")[0] === k.split(":")[0])));
  const recent = [...new Set([...changed.reverse(), ...previous.recent, ...foreground])].filter((k) => valid.has(k));
  const candidates = [...recent.filter((k) => foreground.includes(k)), ...recent.filter((k) => !foreground.includes(k))];
  const kept = new Set<string>(), counts = new Map<string, number>();
  for (const key of candidates) {
    const pane = key.split(":")[0], count = counts.get(pane) ?? 0;
    if (count >= LIVE_TABS || kept.size >= LIVE_FRAMES_WINDOW) continue;
    counts.set(pane, count + 1);
    kept.add(key);
  }
  const next = recent.filter((k) => kept.has(k));
  return same(previous.active, active) && same(previous.recent, next) ? previous : { active, recent: next };
}
