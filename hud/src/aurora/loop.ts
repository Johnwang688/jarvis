// The aurora's frame loop (R-7a), independent of the renderer and the clock so it can be tested.

export type AuroraState = 'off' | 'still' | 'paused' | 'running';

export function auroraState(c: {
  supported: boolean;
  reduceMotion: boolean;
  visible: boolean;
}): AuroraState {
  if (!c.supported) return 'off';
  if (c.reduceMotion) return 'still';
  if (!c.visible) return 'paused';
  return 'running';
}

export interface AuroraRenderer {
  step(dt: number): void;
  draw(): void;
}

export interface AuroraClock {
  request(cb: (t: number) => void): number;
  cancel(id: number): void;
}

export interface AuroraLoop {
  setState(s: AuroraState): void;
  /** Under `still`, draws the still frame again (after a resize or a palette change); else nothing. */
  redraw(): void;
  destroy(): void;
  /** How many frames have been drawn. */
  frames(): number;
  /**
   * Caps running frames at `fps` per second (`null`, the default, draws on every clock tick). A tick that
   * comes sooner than one capped frame after the last drawn frame is skipped: no step, no draw.
   */
  setMaxFps(fps: number | null): void;
}

/** The step for the first frame after (re)starting and for a still frame. */
const FIRST_DT = 1 / 60;
/** A slow frame never steps the simulation further than this. */
const MAX_DT = 1 / 20;
/**
 * Display ticks jitter, so a capped frame is drawn once this share of its interval has passed. At 60 Hz a
 * 30 fps cap then draws every second tick (two ticks are 33 ms, one is 17 ms, either ±3 ms off), and at
 * 120 Hz every fourth.
 */
const CAP_TOLERANCE = 0.85;

export function createAuroraLoop(renderer: AuroraRenderer, clock: AuroraClock): AuroraLoop {
  let state: AuroraState = 'off';
  let pending: number | null = null;
  let last: number | null = null;
  let count = 0;
  let dead = false;
  /** The least time (ms) between drawn frames, or null for no cap. */
  let minInterval: number | null = null;

  const cancel = () => {
    if (pending !== null) clock.cancel(pending);
    pending = null;
  };

  const still = () => {
    renderer.step(FIRST_DT);
    if (dead || state !== 'still') return; // destroyed or switched during the step: no draw
    renderer.draw();
    count++;
  };

  const frame = (t: number) => {
    pending = null;
    if (dead || state !== 'running') return;
    if (minInterval !== null && last !== null && t - last < minInterval) {
      pending = clock.request(frame); // too soon: neither step nor draw, wait for the next tick
      return;
    }
    const dt = last === null ? FIRST_DT : Math.min(Math.max((t - last) / 1000, 0), MAX_DT);
    last = t;
    renderer.step(dt);
    if (dead || state !== 'running') return; // stopped (or unmounted) during the step: no draw
    renderer.draw();
    count++;
    // The renderer may have stopped the loop (a lost context) while drawing.
    if (!dead && state === 'running' && pending === null) pending = clock.request(frame);
  };

  return {
    setState(s) {
      if (dead || s === state) return;
      state = s;
      cancel();
      last = null;
      if (s === 'running') pending = clock.request(frame);
      else if (s === 'still') still();
    },
    redraw() {
      if (!dead && state === 'still') still();
    },
    destroy() {
      dead = true;
      cancel();
    },
    frames: () => count,
    setMaxFps(fps) {
      minInterval = fps !== null && Number.isFinite(fps) && fps > 0 ? (1000 / fps) * CAP_TOLERANCE : null;
    },
  };
}
