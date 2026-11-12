// The web aurora renderer (R-7a): one WebGL2 context per canvas, running the fluid simulation or
// the flow shader. Fluid falls back to flow on a software renderer, when the GPU cannot render to
// half-float targets, or when its buffers cannot be (re)built.
import type { AuroraStyle } from './keys';
import type { AuroraRenderer } from './loop';
import type { AuroraPalette } from './palette';
import { createFlow } from './flow';
import { createFluid, isSoftwareRenderer, rendererName } from './fluid';
import { CONTEXT_ATTRIBUTES, createQuad, type StyleImpl } from './gl';

export interface WebAuroraRenderer extends AuroraRenderer {
  /**
   * The style actually running (fluid falls back to flow), or null when nothing runs: neither style
   * could be built, or the context was lost (until `restore()` rebuilds).
   */
  readonly style: AuroraStyle | null;
  /** Whether the context renders in software (no GPU), read once at creation. */
  readonly software: boolean;
  /** The GPU's unmasked renderer string ('' when the browser does not say), for the stats overlay. */
  readonly rendererName: string;
  setStyle(style: AuroraStyle): void;
  setPalette(palette: AuroraPalette): void;
  /** Natural drift strength 0..2, live: the next step uses it (kept across a rebuild). */
  setDrift(drift: number): void;
  /**
   * The canvas backing size changed (the caller set `canvas.width/height`). If fluid cannot rebuild its
   * buffers at the new size it falls back to flow: read `style` afterwards.
   */
  resize(w: number, h: number): void;
  /** Stirring (fluid only; flow ignores it). `x`, `y` are 0..1 over the canvas, `y` up. */
  stir(x: number, y: number, dx: number, dy: number, gain: number, channel: number): void;
  ripple(x: number, y: number, gain: number, channel: number): void;
  /** True if it drew (never on a lost context, or with nothing built). */
  draw(): boolean;
  /** The context was lost (`webglcontextlost`): every GL object died with it; touch none until `restore()`. */
  contextLost(): void;
  /**
   * Rebuilds every GL object (after `webglcontextrestored`), whatever state creation or an earlier
   * failure left it in. False if it could not (the context was lost again, or nothing could be built).
   */
  restore(): boolean;
  dispose(): void;
}

/**
 * The renderer for `canvas`, or null only when it has no WebGL2 context at all (→ the static CSS
 * paint). Never throws. If the context exists but nothing could be built (it was lost during
 * creation, or neither style compiles), the renderer still comes back, with `style === null`, so a
 * `webglcontextrestored` can `restore()` it and `dispose()` can release the context.
 *
 * GL object ownership: every object is held by `quad` or `impl`, or was deleted by the code that made
 * it when it failed. `stale` marks the objects held as dead with a lost context: they are never used
 * or deleted again (the restored context does not know them), only dropped.
 */
export function createAuroraRenderer(
  canvas: HTMLCanvasElement,
  style: AuroraStyle,
  palette: AuroraPalette,
  opts: { allowSoftwareFluid?: boolean; drift?: number } = {},
): WebAuroraRenderer | null {
  let gl: WebGL2RenderingContext | null = null;
  try {
    gl = canvas.getContext('webgl2', CONTEXT_ATTRIBUTES);
  } catch {
    gl = null;
  }
  if (!gl) return null;
  const ctx = gl;
  const software = isSoftwareRenderer(ctx);
  const name = rendererName(ctx);

  let wanted = style;
  let colours = palette;
  let drift = opts.drift ?? 1;
  let quad: WebGLBuffer | null = null;
  let impl: StyleImpl | null = null;
  /** The context was lost since the objects above were built: they are gone; only `restore()` builds. */
  let stale = false;
  let disposed = false;

  /** Whether the context is lost (now or since the last build); notes it. */
  const isLost = (): boolean => {
    if (!stale && ctx.isContextLost()) stale = true;
    return stale;
  };

  const create = (s: AuroraStyle, w: number, h: number): StyleImpl | null => {
    try {
      return s === 'fluid' ? createFluid(ctx, colours, w, h, opts.allowSoftwareFluid === true) : createFlow(ctx, colours, w, h);
    } catch {
      return null; // createFlow deleted what it made
    }
  };

  /** Deletes the live objects (only on a context that still has them). */
  const releaseAll = () => {
    impl?.release();
    impl = null;
    if (quad) ctx.deleteBuffer(quad);
    quad = null;
  };

  /**
   * Builds the quad (if missing) and the wanted style at the canvas size, fluid falling back to flow.
   * If no style can run, the quad goes too, so a failure leaves nothing behind.
   */
  const build = () => {
    const w = Math.max(1, canvas.width);
    const h = Math.max(1, canvas.height);
    if (!quad) {
      try {
        quad = createQuad(ctx);
      } catch {
        quad = null; // createQuad deleted its buffer
      }
    }
    if (quad) {
      impl = wanted === 'fluid' ? create('fluid', w, h) : null;
      // A fluid failure from a lost context is not a reason to fall back: the restore builds fluid.
      if (!impl && !isLost()) impl = create('flow', w, h);
    }
    impl?.setDrift(drift);
    if (!impl) {
      if (quad) ctx.deleteBuffer(quad);
      quad = null;
    }
    isLost(); // lost while building: what was built is gone with the context
  };

  const live = () => !disposed && impl !== null && !isLost();

  try {
    if (!isLost()) build();
  } catch {
    // Not expected (every step above catches); never let creation throw.
    if (!isLost()) releaseAll();
  }

  return {
    software,
    rendererName: name,
    get style() {
      return disposed || stale || !impl ? null : impl.style;
    },
    setStyle(s) {
      if (disposed || s === wanted) return;
      wanted = s;
      if (isLost()) return; // the restore builds the wanted style
      impl?.release();
      impl = null;
      build();
    },
    setDrift(d) {
      drift = d;
      if (live()) impl!.setDrift(d);
    },
    setPalette(p) {
      colours = p;
      if (live()) impl!.setPalette(p);
    },
    resize(w, h) {
      if (!live()) return;
      const ok = impl!.resize(w, h);
      // Lost while rebuilding the buffers: they are gone with the context; the restore builds anew.
      if (isLost() || ok) return;
      // The fluid buffers could not be rebuilt at this size: fall back to flow, as at creation.
      impl!.release();
      impl = create('flow', Math.max(1, w), Math.max(1, h));
      impl?.setDrift(drift);
      if (!impl) {
        if (quad) ctx.deleteBuffer(quad);
        quad = null;
      }
      isLost();
    },
    stir(x, y, dx, dy, gain, channel) {
      if (live()) impl!.stir(x, y, dx, dy, gain, channel);
    },
    ripple(x, y, gain, channel) {
      if (live()) impl!.ripple(x, y, gain, channel);
    },
    step(dt) {
      if (live()) impl!.step(dt);
    },
    draw() {
      if (!live()) return false;
      impl!.draw();
      return true;
    },
    contextLost() {
      if (!disposed) stale = true;
    },
    restore() {
      if (disposed || ctx.isContextLost()) return false;
      // Objects from before a loss died with it (drop them); live ones (no loss seen) are deleted.
      if (stale) {
        impl = null;
        quad = null;
        stale = false;
      } else {
        releaseAll();
      }
      build();
      return impl !== null && !stale;
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      if (!isLost()) releaseAll();
      impl = null;
      quad = null;
      try {
        ctx.getExtension('WEBGL_lose_context')?.loseContext();
      } catch {
        // the context goes with the canvas anyway
      }
    },
  };
}
