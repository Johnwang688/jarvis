// The stable-fluids simulation (R-7a, from the owner's Bible app), tuned for the HUD (AG-2, owner D-10/D-11):
// splat, curl, vorticity, divergence, pressure (Jacobi), gradient subtraction, then advection of velocity
// and dye, on half-float targets, with three looping emitters (one per wash colour) and a seeded first frame.
//
// HUD motion model. "Slow drift at rest; when stirred it moves more, then settles back to the slow drift."
//   - At rest: the emitters are gentle (AMBIENT_FORCE), vorticity is low (CURL_REST) and velocity dies out
//     in a few seconds (DISSIPATION_REST), so the dye drifts like slow clouds and never swirls.
//   - A stir (the pointer, a chat's ripple) pushes the fluid and raises `energy` (0..1). Energy raises
//     vorticity and lets velocity live longer, then decays with ENERGY_TAU seconds, and the fluid settles
//     back to the drift on its own.
//   - The grid is capped by area (SIM_MAX_AREA, DYE_MAX_AREA), so a 4K window costs what a laptop's does.
// What is displayed is bounded by gl.ts's PALETTE (see palette.ts): the fluid never shows more light than
// the brightest wash.
import {
  applyPalette,
  createProgram,
  fillPalette,
  PALETTE,
  paletteUniforms,
  type Program,
  type StyleImpl,
} from './gl';
import type { AuroraPalette } from './palette';

const SHADERS = {
  splat:
    'uniform sampler2D uTarget;uniform float uAspect;uniform vec3 uColor;uniform vec2 uPoint;uniform float uRadius;\n' +
    'void main(){vec2 p=vUv-uPoint;p.x*=uAspect;vec3 s=exp(-dot(p,p)/uRadius)*uColor;o=vec4(texture(uTarget,vUv).xyz+s,1.0);}',
  // The three emitters in one pass: the same sum as three splats in a row.
  splat3:
    'uniform sampler2D uTarget;uniform float uAspect;uniform vec3 uColors[3];uniform vec2 uPoints[3];uniform float uRadius;\n' +
    'void main(){vec3 s=vec3(0.0);for(int i=0;i<3;i++){vec2 p=vUv-uPoints[i];p.x*=uAspect;s+=exp(-dot(p,p)/uRadius)*uColors[i];}\n' +
    ' o=vec4(texture(uTarget,vUv).xyz+s,1.0);}',
  advect:
    'uniform sampler2D uVelocity;uniform sampler2D uSource;uniform vec2 uSimTexel;uniform float uDt;uniform float uDiss;\n' +
    'void main(){vec2 c=vUv-uDt*texture(uVelocity,vUv).xy*uSimTexel;o=texture(uSource,c)/(1.0+uDiss*uDt);}',
  curl:
    'uniform sampler2D uVelocity;\n' +
    'void main(){float L=texture(uVelocity,vL).y,R=texture(uVelocity,vR).y,T=texture(uVelocity,vT).x,B=texture(uVelocity,vB).x;o=vec4(0.5*(R-L-T+B),0.0,0.0,1.0);}',
  vort:
    'uniform sampler2D uVelocity;uniform sampler2D uCurl;uniform float uCurlK;uniform float uDt;\n' +
    'void main(){float L=texture(uCurl,vL).x,R=texture(uCurl,vR).x,T=texture(uCurl,vT).x,B=texture(uCurl,vB).x,C=texture(uCurl,vUv).x;\n' +
    ' vec2 f=0.5*vec2(abs(T)-abs(B),abs(R)-abs(L));f/=length(f)+0.0001;f*=uCurlK*C;f.y*=-1.0;\n' +
    ' vec2 v=texture(uVelocity,vUv).xy+f*uDt;o=vec4(clamp(v,-1000.0,1000.0),0.0,1.0);}',
  div:
    'uniform sampler2D uVelocity;\n' +
    'void main(){float L=texture(uVelocity,vL).x,R=texture(uVelocity,vR).x,T=texture(uVelocity,vT).y,B=texture(uVelocity,vB).y;vec2 C=texture(uVelocity,vUv).xy;\n' +
    ' if(vL.x<0.0)L=-C.x;if(vR.x>1.0)R=-C.x;if(vT.y>1.0)T=-C.y;if(vB.y<0.0)B=-C.y;o=vec4(0.5*(R-L+T-B),0.0,0.0,1.0);}',
  clear: 'uniform sampler2D uTex;uniform float uValue;void main(){o=uValue*texture(uTex,vUv);}',
  press:
    'uniform sampler2D uPressure;uniform sampler2D uDivergence;\n' +
    'void main(){float L=texture(uPressure,vL).x,R=texture(uPressure,vR).x,T=texture(uPressure,vT).x,B=texture(uPressure,vB).x;\n' +
    ' o=vec4((L+R+B+T-texture(uDivergence,vUv).x)*0.25,0.0,0.0,1.0);}',
  grad:
    'uniform sampler2D uPressure;uniform sampler2D uVelocity;\n' +
    'void main(){float L=texture(uPressure,vL).x,R=texture(uPressure,vR).x,T=texture(uPressure,vT).x,B=texture(uPressure,vB).x;\n' +
    ' vec2 v=texture(uVelocity,vUv).xy-vec2(R-L,T-B);o=vec4(v,0.0,1.0);}',
  show:
    PALETTE +
    'uniform sampler2D uDye;void main(){vec3 w=max(texture(uDye,vUv).rgb,0.0);w/=max(1.0,w.x+w.y+w.z);o=vec4(palette(w*0.95),1.0);}',
} as const;

/** The fragment shader that puts the dye on the screen (pinned whole by palette.test.ts). */
export const FLUID_SHOW_FS = SHADERS.show;

type ProgramName = keyof typeof SHADERS;

/** Short-side resolutions, then the area caps (texels) that win on a wide window. */
const SIM_RES = 96;
const DYE_RES = 224;
export const SIM_MAX_AREA = 20000;
export const DYE_MAX_AREA = 90000;
const PRESSURE_ITERATIONS = 16;
const PRESSURE_DECAY = 0.8;
const DYE_DISSIPATION = 0.35;
const TAU = 6.283;

/** At rest: low vorticity, velocity dying out in a few seconds, gentle emitters. */
const CURL_REST = 3;
const DISSIPATION_REST = 0.4;
const AMBIENT_FORCE = 0.7;
/** Stirred (energy 1): more vorticity, and velocity that lasts longer. */
const CURL_STIRRED = 11;
const DISSIPATION_STIRRED = 0.2;
/** Seconds for stirred energy to fall to 1/e: the fluid is back at its drift in a few of these. */
export const ENERGY_TAU = 1.6;
/** A stir's weight on the fluid: weaker than the Bible app's wallpaper, so it never distracts. */
const STIR_FORCE = 2600;
const STIR_DYE = 0.32;
const STIR_RADIUS = 0.3;
const RIPPLE_FORCE = 55;
const RIPPLE_DYE = 0.22;
const RIPPLE_RADIUS = 0.5;
/** How much each unit of stir adds to the energy, and the most a stir can raise it. */
const ENERGY_PER_STIR = 0.35;
const ENERGY_MAX = 0.7;

/** Three slow currents, one per palette colour, each on its own looping path. */
const EMITTERS = [
  { ax: 0.32, ay: 0.3, fx: 0.11, fy: 0.07, px: 0.0, py: 1.3, ch: 0 },
  { ax: 0.3, ay: 0.34, fx: 0.08, fy: 0.12, px: 2.1, py: 0.4, ch: 1 },
  { ax: 0.34, ay: 0.28, fx: 0.13, fy: 0.09, px: 4.0, py: 2.6, ch: 2 },
] as const;

/** The first frame starts full: colour 1 top left, 2 right, 3 bottom, as the still paint draws them. */
const SEEDS: readonly (readonly [number, number, number, number, number])[] = [
  [0.15, 0.85, 0.9, 0, 0],
  [0.85, 0.55, 0, 0.9, 0],
  [0.25, 0.15, 0, 0, 0.9],
  [0.75, 0.2, 0.5, 0, 0],
  [0.4, 0.5, 0, 0.4, 0],
];

interface Target {
  tex: WebGLTexture;
  fb: WebGLFramebuffer;
  w: number;
  h: number;
}

interface DoubleTarget {
  read: Target;
  write: Target;
  w: number;
  h: number;
  tx: number;
  ty: number;
}

/** A buffer size with `short` on the short side, scaled down so its area is at most `maxArea`. */
export function size(short: number, aspect: number, maxArea: number): { w: number; h: number } {
  const a = aspect < 1 ? 1 / aspect : aspect;
  let mn = short;
  let mx = short * a;
  if (mn * mx > maxArea) {
    const k = Math.sqrt(maxArea / (mn * mx));
    mn *= k;
    mx *= k;
  }
  mn = Math.max(8, Math.round(mn));
  mx = Math.max(8, Math.round(mx));
  return aspect < 1 ? { w: mn, h: mx } : { w: mx, h: mn };
}

/** The simulation and dye grids for a canvas of this size (for the stats overlay). */
export function gridFor(w: number, h: number): { sim: { w: number; h: number }; dye: { w: number; h: number } } {
  const aspect = w / Math.max(1, h);
  return { sim: size(SIM_RES, aspect, SIM_MAX_AREA), dye: size(DYE_RES, aspect, DYE_MAX_AREA) };
}

/** Renderer names of GPUs emulated on the CPU, where the simulation would cost the main thread dearly. */
const SOFTWARE = /swiftshader|llvmpipe|softpipe|software|microsoft basic render/i;

/**
 * Whether this context renders in software (SwiftShader, llvmpipe, ...). Without the debug renderer
 * extension (or if reading it fails) it is assumed to be a real GPU.
 */
export function isSoftwareRenderer(gl: WebGL2RenderingContext): boolean {
  return SOFTWARE.test(rendererName(gl));
}

/** The unmasked renderer string, or '' when the browser does not say (read for the stats overlay). */
export function rendererName(gl: WebGL2RenderingContext): string {
  try {
    const ext = gl.getExtension('WEBGL_debug_renderer_info');
    if (!ext) return '';
    const name: unknown = gl.getParameter(ext.UNMASKED_RENDERER_WEBGL);
    return typeof name === 'string' ? name : '';
  } catch {
    return '';
  }
}

/**
 * Whether this context can run the simulation: a hardware renderer that can render to half-float
 * colour targets (enables the extension if so).
 */
export function supportsFluid(gl: WebGL2RenderingContext, allowSoftware = false): boolean {
  if (!allowSoftware && isSoftwareRenderer(gl)) return false;
  try {
    return gl.getExtension('EXT_color_buffer_float') !== null || gl.getExtension('EXT_color_buffer_half_float') !== null;
  } catch {
    return false;
  }
}

/**
 * The fluid style, or null when this GPU cannot run it (a software renderer, no half-float targets,
 * incomplete framebuffers, a lost context). Never throws; when it returns null, every object it made
 * has been deleted.
 */
export function createFluid(
  gl: WebGL2RenderingContext,
  palette: AuroraPalette,
  w: number,
  h: number,
  allowSoftware = false,
): StyleImpl | null {
  if (!supportsFluid(gl, allowSoftware)) return null;

  const P = {} as Record<ProgramName, Program>;
  const programs: WebGLProgram[] = [];

  const pal = paletteUniforms();
  const scratch = new Float32Array(3);
  const points = new Float32Array(6);
  const velColours = new Float32Array(9);
  const dyeColours = new Float32Array(9);
  const scratch2 = new Float32Array(3);
  let energy = 0;
  let width = w;
  let height = h;
  let t = 0;
  let targets: Target[] = [];
  let vel: DoubleTarget;
  let dye: DoubleTarget;
  let press: DoubleTarget;
  let div: Target;
  let curl: Target;

  /**
   * One render target, tracked in `targets` (and so released with them) as soon as both of its
   * objects exist. If only one of them could be made (a lost context), that one is deleted here.
   */
  const target = (tw: number, th: number): Target | null => {
    const tex = gl.createTexture();
    if (!tex) return null;
    let fb: WebGLFramebuffer | null = null;
    try {
      fb = gl.createFramebuffer();
    } finally {
      if (!fb) gl.deleteTexture(tex); // on a null or a throw: the texture is not left behind
    }
    if (!fb) return null;
    const t0: Target = { tex, fb, w: tw, h: th };
    targets.push(t0);
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA16F, tw, th, 0, gl.RGBA, gl.HALF_FLOAT, null);
    gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
    if (gl.checkFramebufferStatus(gl.FRAMEBUFFER) !== gl.FRAMEBUFFER_COMPLETE) return null;
    gl.viewport(0, 0, tw, th);
    gl.clearColor(0, 0, 0, 1);
    gl.clear(gl.COLOR_BUFFER_BIT);
    return t0;
  };

  const double = (tw: number, th: number): DoubleTarget | null => {
    const a = target(tw, th);
    const b = a && target(tw, th);
    return a && b ? { read: a, write: b, w: tw, h: th, tx: 1 / tw, ty: 1 / th } : null;
  };

  const swap = (d: DoubleTarget) => {
    const x = d.read;
    d.read = d.write;
    d.write = x;
  };

  const releaseTargets = () => {
    for (const x of targets) {
      gl.deleteFramebuffer(x.fb);
      gl.deleteTexture(x.tex);
    }
    targets = [];
  };

  const bindTex = (unit: number, tex: WebGLTexture) => {
    gl.activeTexture(gl.TEXTURE0 + unit);
    gl.bindTexture(gl.TEXTURE_2D, tex);
    return unit;
  };

  const blit = (to: Target | null) => {
    if (to) {
      gl.bindFramebuffer(gl.FRAMEBUFFER, to.fb);
      gl.viewport(0, 0, to.w, to.h);
    } else {
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      gl.viewport(0, 0, width, height);
    }
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  };

  const bind = (pr: Program, tx: number, ty: number) => {
    gl.useProgram(pr.program);
    const texel = pr.uniforms.texel;
    if (texel) gl.uniform2f(texel, tx, ty);
    return pr.uniforms;
  };

  const splat = (d: DoubleTarget, x: number, y: number, color: Float32Array, radius: number) => {
    const aspect = width / height;
    const u = bind(P.splat, d.tx, d.ty);
    gl.uniform1i(u.uTarget ?? null, bindTex(0, d.read.tex));
    gl.uniform1f(u.uAspect ?? null, aspect);
    gl.uniform2f(u.uPoint ?? null, x, y);
    gl.uniform3fv(u.uColor ?? null, color);
    gl.uniform1f(u.uRadius ?? null, (radius / 100) * (aspect < 1 ? 1 : aspect));
    blit(d.write);
    swap(d);
  };

  /** Adds the three emitters' splats (`points`, `colours`) to `d` in one pass. */
  const splat3 = (d: DoubleTarget, colours: Float32Array, radius: number) => {
    const aspect = width / height;
    const u = bind(P.splat3, d.tx, d.ty);
    gl.uniform1i(u.uTarget ?? null, bindTex(0, d.read.tex));
    gl.uniform1f(u.uAspect ?? null, aspect);
    gl.uniform2fv(u['uPoints[0]'] ?? null, points);
    gl.uniform3fv(u['uColors[0]'] ?? null, colours);
    gl.uniform1f(u.uRadius ?? null, (radius / 100) * (aspect < 1 ? 1 : aspect));
    blit(d.write);
    swap(d);
  };

  /**
   * (Re)creates the buffers for the canvas size and seeds the dye. False if the GPU cannot (an
   * incomplete framebuffer, a lost context), with every buffer released: nothing half-built stays.
   */
  const init = (): boolean => {
    releaseTargets();
    try {
      const aspect = width / height;
      const s = size(SIM_RES, aspect, SIM_MAX_AREA);
      const d = size(DYE_RES, aspect, DYE_MAX_AREA);
      // Stop at the first failure: the rest would only be made to be deleted.
      const v = double(s.w, s.h);
      const dd = v && double(d.w, d.h);
      const p = dd && double(s.w, s.h);
      const dv = p && target(s.w, s.h);
      const c = dv && target(s.w, s.h);
      if (!v || !dd || !p || !dv || !c) {
        releaseTargets();
        return false;
      }
      vel = v;
      dye = dd;
      press = p;
      div = dv;
      curl = c;
      for (const [x, y, r, g, b] of SEEDS) {
        scratch[0] = r;
        scratch[1] = g;
        scratch[2] = b;
        splat(dye, x, y, scratch, 0.06);
      }
      return true;
    } catch {
      releaseTargets();
      return false;
    }
  };

  const releasePrograms = () => {
    for (const p of programs) gl.deleteProgram(p);
    programs.length = 0;
  };

  const setPalette = (p: AuroraPalette) => {
    fillPalette(pal, p);
    gl.useProgram(P.show.program);
    applyPalette(gl, P.show, pal);
  };

  // Build everything; on any failure (a compile error, a lost context, an incomplete framebuffer)
  // delete whatever was made and report that fluid cannot run.
  try {
    for (const name of Object.keys(SHADERS) as ProgramName[]) {
      P[name] = createProgram(gl, SHADERS[name]);
      programs.push(P[name].program);
    }
    if (!init()) throw new Error('fluid buffers');
    setPalette(palette);
  } catch {
    releaseTargets();
    releasePrograms();
    return null;
  }

  let ready = true;

  const step = (dt: number) => {
    if (!ready) return;
    t += dt;
    energy *= Math.exp(-dt / ENERGY_TAU);
    if (energy < 0.002) energy = 0;
    const k = dt * 60;
    for (let i = 0; i < EMITTERS.length; i++) {
      const e = EMITTERS[i]!;
      points[2 * i] = 0.5 + e.ax * Math.sin(t * e.fx * TAU + e.px);
      points[2 * i + 1] = 0.5 + e.ay * Math.sin(t * e.fy * TAU + e.py);
      const vx = e.ax * e.fx * TAU * Math.cos(t * e.fx * TAU + e.px);
      const vy = e.ay * e.fy * TAU * Math.cos(t * e.fy * TAU + e.py);
      velColours[3 * i] = vx * vel.w * AMBIENT_FORCE * k;
      velColours[3 * i + 1] = vy * vel.h * AMBIENT_FORCE * k;
      velColours[3 * i + 2] = 0;
      dyeColours[3 * i] = e.ch === 0 ? 0.012 * k : 0;
      dyeColours[3 * i + 1] = e.ch === 1 ? 0.012 * k : 0;
      dyeColours[3 * i + 2] = e.ch === 2 ? 0.012 * k : 0;
    }
    splat3(vel, velColours, 0.9);
    splat3(dye, dyeColours, 1.8);
    let u = bind(P.curl, vel.tx, vel.ty);
    gl.uniform1i(u.uVelocity ?? null, bindTex(0, vel.read.tex));
    blit(curl);

    u = bind(P.vort, vel.tx, vel.ty);
    gl.uniform1i(u.uVelocity ?? null, bindTex(0, vel.read.tex));
    gl.uniform1i(u.uCurl ?? null, bindTex(1, curl.tex));
    gl.uniform1f(u.uCurlK ?? null, CURL_REST + (CURL_STIRRED - CURL_REST) * energy);
    gl.uniform1f(u.uDt ?? null, dt);
    blit(vel.write);
    swap(vel);

    u = bind(P.div, vel.tx, vel.ty);
    gl.uniform1i(u.uVelocity ?? null, bindTex(0, vel.read.tex));
    blit(div);

    u = bind(P.clear, vel.tx, vel.ty);
    gl.uniform1i(u.uTex ?? null, bindTex(0, press.read.tex));
    gl.uniform1f(u.uValue ?? null, PRESSURE_DECAY);
    blit(press.write);
    swap(press);

    u = bind(P.press, vel.tx, vel.ty);
    gl.uniform1i(u.uDivergence ?? null, bindTex(0, div.tex));
    for (let i = 0; i < PRESSURE_ITERATIONS; i++) {
      gl.uniform1i(u.uPressure ?? null, bindTex(1, press.read.tex));
      blit(press.write);
      swap(press);
    }

    u = bind(P.grad, vel.tx, vel.ty);
    gl.uniform1i(u.uPressure ?? null, bindTex(0, press.read.tex));
    gl.uniform1i(u.uVelocity ?? null, bindTex(1, vel.read.tex));
    blit(vel.write);
    swap(vel);

    u = bind(P.advect, vel.tx, vel.ty);
    gl.uniform2f(u.uSimTexel ?? null, vel.tx, vel.ty);
    gl.uniform1f(u.uDt ?? null, dt);
    gl.uniform1i(u.uVelocity ?? null, bindTex(0, vel.read.tex));
    gl.uniform1i(u.uSource ?? null, 0);
    gl.uniform1f(u.uDiss ?? null, DISSIPATION_REST + (DISSIPATION_STIRRED - DISSIPATION_REST) * energy);
    blit(vel.write);
    swap(vel);

    u = bind(P.advect, dye.tx, dye.ty);
    gl.uniform2f(u.uSimTexel ?? null, vel.tx, vel.ty);
    gl.uniform1f(u.uDt ?? null, dt);
    gl.uniform1i(u.uVelocity ?? null, bindTex(0, vel.read.tex));
    gl.uniform1i(u.uSource ?? null, bindTex(1, dye.read.tex));
    gl.uniform1f(u.uDiss ?? null, DYE_DISSIPATION);
    blit(dye.write);
    swap(dye);
  };

  return {
    style: 'fluid',
    step,
    draw() {
      if (!ready) return;
      const u = bind(P.show, 0, 0);
      gl.uniform1i(u.uDye ?? null, bindTex(0, dye.read.tex));
      blit(null);
    },
    setPalette,
    resize(nw, nh) {
      width = nw;
      height = nh;
      // False when the buffers cannot be rebuilt (an incomplete framebuffer at this size, or a lost
      // context): the renderer then falls back to flow.
      ready = init();
      return ready;
    },
    stir(x, y, dx, dy, gain, channel) {
      if (!ready) return;
      const g = Math.min(Math.max(gain, 0), 1);
      if (!(g > 0)) return;
      // The push is the movement itself (a fast mouse pushes harder), bounded so one jump cannot slam the fluid.
      const clamp = (v: number) => Math.min(Math.max(v, -0.08), 0.08);
      scratch[0] = clamp(dx) * STIR_FORCE * g;
      scratch[1] = clamp(dy) * STIR_FORCE * g;
      scratch[2] = 0;
      splat(vel, x, y, scratch, STIR_RADIUS);
      scratch2.fill(0);
      scratch2[Math.min(Math.max(channel | 0, 0), 2)] = STIR_DYE * g;
      splat(dye, x, y, scratch2, STIR_RADIUS * 1.4);
      energy = Math.min(ENERGY_MAX, energy + Math.min(0.2, Math.hypot(dx, dy) * 6) * ENERGY_PER_STIR * g);
    },
    ripple(x, y, gain, channel) {
      if (!ready) return;
      const g = Math.min(Math.max(gain, 0), 1);
      if (!(g > 0)) return;
      // Six soft pushes on a small ring, each pointing outward: a ring spreading from the pane.
      scratch2.fill(0);
      scratch2[Math.min(Math.max(channel | 0, 0), 2)] = RIPPLE_DYE * g;
      const aspect = width / height;
      for (let i = 0; i < 6; i++) {
        const a = (i / 6) * TAU;
        const cx = Math.cos(a);
        const cy = Math.sin(a);
        const px = x + (0.03 * cx) / aspect;
        const py = y + 0.03 * cy;
        scratch[0] = cx * RIPPLE_FORCE * g;
        scratch[1] = cy * RIPPLE_FORCE * g;
        scratch[2] = 0;
        splat(vel, px, py, scratch, RIPPLE_RADIUS);
      }
      splat(dye, x, y, scratch2, RIPPLE_RADIUS * 1.4);
      energy = Math.min(ENERGY_MAX, energy + 0.2 * g);
    },
    release() {
      ready = false;
      releaseTargets();
      releasePrograms();
    },
  };
}
