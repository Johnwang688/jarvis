// The owner's real-machine check (AG-2): a tiny corner readout of the aurora's cost, shown only when
// `localStorage['jarvis.hud.auroraStats'] === '1'` (or 'fluid', which also lets the fluid run on a
// software renderer, for measuring it where there is no GPU). Off by default. It never takes focus
// and never takes a pointer event.

export const STATS_KEY = 'jarvis.hud.auroraStats';
const WINDOW = 120;

/** Per-frame main-thread milliseconds over the last WINDOW frames, and frames per second over the last second. */
export class FrameStats {
  private ms: number[] = [];
  private stamps: number[] = [];

  record(frameMs: number, now: number): void {
    this.ms.push(frameMs);
    if (this.ms.length > WINDOW) this.ms.shift();
    this.stamps.push(now);
    while (this.stamps.length > 1 && now - this.stamps[0]! > 1000) this.stamps.shift();
  }

  snapshot(): { fps: number; avg: number; p95: number; frames: number } {
    const n = this.ms.length;
    if (n === 0) return { fps: 0, avg: 0, p95: 0, frames: 0 };
    const sorted = [...this.ms].sort((a, b) => a - b);
    const avg = this.ms.reduce((a, b) => a + b, 0) / n;
    const p95 = sorted[Math.min(n - 1, Math.ceil(n * 0.95) - 1)]!;
    const span = this.stamps.length > 1 ? this.stamps[this.stamps.length - 1]! - this.stamps[0]! : 0;
    const fps = span > 0 ? ((this.stamps.length - 1) * 1000) / span : 0;
    return { fps, avg, p95, frames: n };
  }
}

/** What the overlay shows besides the timings. */
export interface StatsInfo {
  renderer: string;
  software: boolean;
  style: string | null;
  state: string | null;
  grid: string;
}

/** 'on' shows the overlay; 'fluid' also lets fluid run on a software renderer. */
export function statsMode(storage?: Pick<Storage, 'getItem'>): 'off' | 'on' | 'fluid' {
  try {
    const v = (storage ?? window.localStorage).getItem(STATS_KEY);
    return v === '1' ? 'on' : v === 'fluid' ? 'fluid' : 'off';
  } catch {
    return 'off';
  }
}

/** The overlay element, refreshed twice a second; `dispose()` removes it. */
export function mountStatsOverlay(
  doc: Document,
  stats: FrameStats,
  info: () => StatsInfo,
  setTimer: (cb: () => void, ms: number) => unknown = (cb, ms) => window.setInterval(cb, ms),
  clearTimer: (id: unknown) => void = (id) => window.clearInterval(id as number),
): { el: HTMLElement; dispose(): void } {
  const el = doc.createElement('div');
  el.setAttribute('data-testid', 'aurora-stats');
  el.setAttribute('aria-hidden', 'true');
  el.style.cssText =
    'position:fixed;right:6px;bottom:6px;z-index:2147483000;pointer-events:none;padding:2px 6px;' +
    'font:11px/1.3 ui-monospace,monospace;color:#cfe;background:rgba(0,0,0,.65);border-radius:3px;' +
    'white-space:pre;user-select:none';
  const draw = () => {
    const s = stats.snapshot();
    const i = info();
    el.textContent =
      `aurora ${i.style ?? 'none'} (${i.state ?? 'off'})  ${s.fps.toFixed(0)} fps\n` +
      `frame ${s.avg.toFixed(2)} ms avg, ${s.p95.toFixed(2)} ms p95\n` +
      `${i.software ? 'software' : 'GPU'}: ${i.renderer || 'unknown'}  grid ${i.grid}`;
  };
  draw();
  doc.body.appendChild(el);
  const id = setTimer(draw, 500);
  return {
    el,
    dispose() {
      clearTimer(id);
      el.remove();
    },
  };
}
