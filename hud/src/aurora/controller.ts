// Drives the HUD's aurora canvas (AG-2): the canvas inside `#aurora`, its renderer and frame loop, sizing at
// half resolution under an area cap, GPU context loss and restore, the pointer's and chat activity's stirs,
// and the data attributes the browser checks read. Nothing here touches React: the animation never
// re-renders anything.
//
// Ported from the Bible app's controller.web.ts. What changed for the HUD: the canvas fills the window and
// sits behind everything (`pointer-events: none`, `aria-hidden`, never focusable); the pointer stirs by
// hovering (a passive `pointermove` on `window`, never a drag on a screen element); pausing follows
// `document.hidden` only; and chat activity can ripple the fluid from a rect.
import type { AuroraStyle } from './keys';
import { auroraState, createAuroraLoop, type AuroraClock, type AuroraRenderer, type AuroraState } from './loop';
import { samePalette, type AuroraPalette } from './palette';
import { gridFor } from './fluid';
import { createAuroraRenderer, type WebAuroraRenderer } from './renderer';
import { FrameStats } from './stats';

export interface AuroraInputs {
  style: AuroraStyle;
  palette: AuroraPalette;
  /** One still frame, no motion: the owner's Still setting, or the system's reduced motion. */
  still: boolean;
  visible: boolean;
  /** The pointer stirs (Settings -> Appearance). */
  stir: boolean;
  /** Streaming chats ripple (Settings -> Appearance). */
  activity: boolean;
}

export interface AuroraOptions {
  /** Run the fluid on a software renderer too (the stats override, for measuring). */
  allowSoftwareFluid?: boolean;
  /** Collect per-frame main-thread time. */
  stats?: FrameStats | null;
  now?: () => number;
}

export interface AuroraController {
  update(next: AuroraInputs): void;
  /** Whether a ripple would be drawn now (setting on, fluid running). O(1), no layout reads. */
  canRipple(): boolean;
  /** A soft ripple from a rect (CSS px, viewport coordinates); false if it was not made. */
  ripple(rect: { left: number; top: number; width: number; height: number }): boolean;
  info(): { renderer: string; software: boolean; style: string | null; state: string | null; grid: string };
  destroy(): void;
}

/** The background is soft, so it renders at half the device resolution and scales up. */
export const RESOLUTION = 0.5;
/** The most backing-store pixels, whatever the window: a 4K window on integrated graphics stays affordable. */
export const MAX_BACKING_PIXELS = 1_000_000;
/** How often, at most, a running aurora writes its frame count to the canvas. */
const REPORT_MS = 250;
/** On a software renderer (no GPU) the aurora keeps moving, but at this frame rate at most. */
export const SOFTWARE_MAX_FPS = 30;
/**
 * Frame cap with a GPU: the drift is slow, and a 144 Hz display gains nothing from more. A cap a little over
 * 60 (the loop draws once 85% of the interval has passed) draws every tick at 60 Hz, every second tick at
 * 120 or 144 Hz, and so never falls to 48 fps on a 144 Hz display the way a cap of exactly 60 does.
 */
export const GPU_MAX_FPS = 70;
/** Pointer moves closer together than this (ms) are folded into the next one. */
const STIR_EVERY_MS = 16;
/** A pause in the pointer this long (ms) ends the stroke: the next move starts a new one (and a new wash). */
const STROKE_GAP_MS = 140;
/** A pointer over a Browser frame is the frame's own: it stirs nothing. */
const FRAME_SELECTOR = 'iframe, .frameshield, .framehost';

/** The canvas backing size for its CSS size, or null while it has no size. */
export function backingSize(
  cssW: number,
  cssH: number,
  dpr: number,
): { w: number; h: number } | null {
  if (!(cssW >= 1) || !(cssH >= 1)) return null;
  let scale = Math.min(dpr || 1, 2) * RESOLUTION;
  const area = cssW * scale * (cssH * scale);
  if (area > MAX_BACKING_PIXELS) scale *= Math.sqrt(MAX_BACKING_PIXELS / area);
  return { w: Math.max(2, Math.round(cssW * scale)), h: Math.max(2, Math.round(cssH * scale)) };
}

/** Creates the canvas inside `host` (the `#aurora` layer) and starts it. */
export function createAuroraController(host: HTMLElement, first: AuroraInputs, opts: AuroraOptions = {}): AuroraController {
  let inputs = first;
  let state: AuroraState | null = null;
  let lost = false;
  let shown = false;
  let destroyed = false;
  let lastReport = -Infinity;
  const now = opts.now ?? (() => performance.now());
  const stats = opts.stats ?? null;
  let stirs = 0;
  let ripples = 0;
  let channel = 0;

  // A fresh canvas per controller: a canvas whose context was released cannot get another one.
  const canvas = document.createElement('canvas');
  canvas.setAttribute('data-testid', 'aurora-canvas');
  canvas.setAttribute('aria-hidden', 'true');
  canvas.style.pointerEvents = 'none';
  canvas.style.visibility = 'hidden';
  host.appendChild(canvas);
  const measure = () => {
    const r = host.getBoundingClientRect();
    return backingSize(r.width, r.height, window.devicePixelRatio || 1);
  };
  const initial = measure();
  if (initial) {
    canvas.width = initial.w;
    canvas.height = initial.h;
  }

  // Null only without WebGL2. A renderer whose context was lost while it was being built comes back
  // with no style (state off) and rebuilds on `webglcontextrestored`.
  let renderer: WebAuroraRenderer | null = createAuroraRenderer(canvas, inputs.style, inputs.palette, {
    allowSoftwareFluid: opts.allowSoftwareFluid,
  });

  const show = (on: boolean) => {
    if (shown === on) return;
    shown = on;
    canvas.style.visibility = on ? 'visible' : 'hidden';
  };

  const delegate: AuroraRenderer = {
    step: (dt) => renderer?.step(dt),
    draw: () => {
      // Shown only once a frame is really on it (the renderer never draws on a lost context).
      if (renderer && !lost && renderer.draw()) show(true);
    },
  };

  // requestAnimationFrame, with the frame count reported after each frame and the frame's main-thread time
  // recorded. One frame is pending at a time, so one wrapper (made once) is enough: no allocation per frame.
  let inner: ((t: number) => void) | null = null;
  const outer = (t: number) => {
    const cb = inner;
    inner = null;
    const t0 = stats ? now() : 0;
    const drawn0 = loop.frames();
    cb?.(t);
    // Only a tick that drew a frame counts: a tick the cap skipped did no work (and is not a frame).
    if (stats && loop.frames() !== drawn0) stats.record(now() - t0, t);
    if (!destroyed && state === 'running' && t - lastReport >= REPORT_MS) {
      lastReport = t;
      reportFrames();
    }
  };
  const clock: AuroraClock = {
    request(cb) {
      inner = cb;
      return requestAnimationFrame(outer);
    },
    cancel(id) {
      inner = null;
      cancelAnimationFrame(id);
    },
  };
  const loop = createAuroraLoop(delegate, clock);
  const capFps = () => loop.setMaxFps(renderer?.software ? SOFTWARE_MAX_FPS : GPU_MAX_FPS);
  capFps();

  function reportFrames() {
    canvas.setAttribute('data-aurora-frames', String(loop.frames()));
  }
  const reportStirs = () => {
    canvas.setAttribute('data-aurora-stirs', String(stirs));
    canvas.setAttribute('data-aurora-ripples', String(ripples));
  };

  const reportStyle = () => {
    const s = renderer?.style ?? null;
    if (s === null) canvas.removeAttribute('data-aurora-style');
    else canvas.setAttribute('data-aurora-style', s);
  };

  const apply = () => {
    if (destroyed) return;
    const next = auroraState({
      supported: renderer !== null && renderer.style !== null && !lost,
      reduceMotion: inputs.still,
      visible: inputs.visible,
    });
    if (next !== state) {
      state = next;
      if (next === 'off') show(false);
      loop.setState(next);
      canvas.setAttribute('data-aurora-state', next);
      reportFrames();
    }
    if (!canStir()) lastPoint = null;
  };

  /** After the canvas content was lost (resize, restore, new style): put a frame back if it is still. */
  const refresh = () => {
    if (state === 'still') {
      loop.redraw();
      reportFrames();
    } else if (state === 'running') {
      // Draw now (the loop draws again next frame), so a resize never shows an empty canvas.
      delegate.draw();
    } else {
      // Paused or off: show the still CSS paint until the next frame.
      show(false);
    }
  };

  // ---- sizing ----
  const resize = () => {
    const size = measure();
    if (!size || (size.w === canvas.width && size.h === canvas.height)) return;
    canvas.width = size.w;
    canvas.height = size.h;
    if (!renderer || lost) return;
    const before = renderer.style;
    renderer.resize(size.w, size.h);
    if (renderer.style !== before) {
      // Fluid could not rebuild its buffers at this size and fell back to flow (or nothing could run).
      reportStyle();
      const prev = state;
      apply();
      if (prev !== state) return; // the new state drew (still) or stopped as it should
    }
    refresh();
  };
  const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(resize) : null;
  observer?.observe(host);

  // ---- context loss ----
  const onLost = (e: Event) => {
    e.preventDefault(); // without this the browser never restores the context
    if (destroyed) return;
    lost = true;
    renderer?.contextLost();
    apply();
  };
  // Rebuild whatever state creation (or an earlier restore) left: with no renderer, create one.
  const onRestored = () => {
    if (destroyed) return;
    lastPoint = null;
    if (renderer) lost = !renderer.restore();
    else {
      renderer = createAuroraRenderer(canvas, inputs.style, inputs.palette, { allowSoftwareFluid: opts.allowSoftwareFluid });
      lost = renderer?.style == null;
      capFps();
    }
    reportStyle();
    if (lost) {
      apply(); // could not rebuild: nothing runs (and nothing stirs)
      return;
    }
    show(false);
    const prev = state;
    apply();
    if (prev === state) refresh();
  };
  canvas.addEventListener('webglcontextlost', onLost);
  canvas.addEventListener('webglcontextrestored', onRestored);

  // ---- stirring (fluid only) ----
  const canStir = () => !!renderer && !lost && state === 'running' && renderer.style === 'fluid';
  /** The last stirred point (unit coordinates, y up) and its time, or null before a stroke. */
  let lastPoint: { x: number; y: number; t: number } | null = null;
  let lastMove = -Infinity;
  const onMove = (ev: PointerEvent) => {
    // Mouse and pen only: a finger's move is a scroll or a drag, not a hover trail.
    if (!inputs.stir || ev.pointerType === 'touch' || !canStir()) return;
    // Throttled before any work: a 1000 Hz mouse costs one stir per ~16 ms.
    if (ev.timeStamp - lastMove < STIR_EVERY_MS) return;
    const target = ev.target;
    // A Browser frame's own events are its own (and a pointer over a shield is over a frame).
    if (target instanceof Element && target.closest(FRAME_SELECTOR)) {
      lastPoint = null;
      return;
    }
    const r = canvas.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return;
    lastMove = ev.timeStamp;
    const x = (ev.clientX - r.left) / r.width;
    const y = 1 - (ev.clientY - r.top) / r.height;
    const p = lastPoint;
    lastPoint = { x, y, t: ev.timeStamp };
    if (!p || ev.timeStamp - p.t > STROKE_GAP_MS) {
      channel = (channel + 1) % 3; // a new stroke lays down the next wash colour
      return;
    }
    const dx = x - p.x;
    const dy = y - p.y;
    const dist = Math.hypot(dx, dy);
    if (dist < 0.0005) return;
    renderer!.stir(x, y, dx, dy, Math.min(1, dist * 30), channel);
    stirs++;
    reportStirs();
  };
  // Passive and on the window: it can never call preventDefault, never takes focus, and never sits in
  // the way of the HUD's own handlers (it is only a listener).
  const passive: AddEventListenerOptions = { passive: true };
  window.addEventListener('pointermove', onMove, passive);

  reportStyle();
  apply();
  reportStirs();

  return {
    update(next) {
      if (destroyed) return;
      const prev = inputs;
      inputs = next;
      if (renderer && next.style !== prev.style) {
        renderer.setStyle(next.style);
        reportStyle();
        if (renderer.style !== null) refresh();
      }
      if (renderer && !samePalette(next.palette, prev.palette)) {
        renderer.setPalette(next.palette);
        // A palette change while still only updates the uniforms; a still frame is drawn again.
        if (state === 'still') {
          loop.redraw();
          reportFrames();
        }
      }
      apply();
    },
    canRipple() {
      return !destroyed && inputs.activity && canStir();
    },
    ripple(rect) {
      if (destroyed || !inputs.activity || !canStir()) return false;
      const r = canvas.getBoundingClientRect();
      if (r.width < 1 || r.height < 1 || rect.width < 1 || rect.height < 1) return false;
      const cx = rect.left + rect.width * (0.25 + 0.5 * Math.random());
      const cy = rect.top + rect.height * (0.35 + 0.5 * Math.random());
      const x = (cx - r.left) / r.width;
      const y = 1 - (cy - r.top) / r.height;
      if (x < -0.1 || x > 1.1 || y < -0.1 || y > 1.1) return false;
      channel = (channel + 1) % 3;
      renderer!.ripple(x, y, 0.7, channel);
      ripples++;
      reportStirs();
      return true;
    },
    info() {
      const gridText = () => {
        const g = gridFor(canvas.width, canvas.height);
        const fluid = renderer?.style === 'fluid' ? ` sim ${g.sim.w}x${g.sim.h} dye ${g.dye.w}x${g.dye.h}` : '';
        return `canvas ${canvas.width}x${canvas.height}${fluid}`;
      };
      return {
        renderer: renderer?.rendererName ?? '',
        software: renderer?.software ?? false,
        style: renderer?.style ?? null,
        state,
        grid: gridText(),
      };
    },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      loop.destroy();
      observer?.disconnect();
      canvas.removeEventListener('webglcontextlost', onLost);
      canvas.removeEventListener('webglcontextrestored', onRestored);
      window.removeEventListener('pointermove', onMove, passive);
      renderer?.dispose();
      renderer = null;
      canvas.remove();
    },
  };
}
