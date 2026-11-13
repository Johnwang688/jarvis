import { useLayoutEffect, useRef, useState } from "react";
import { chatActivity } from "../aurora/activity";
import { parseColour, readPalette, type Rgb } from "../aurora/palette";
import { BOOT_RINGS, FONT_WAIT_MS, HARD_CAP_MS, PLAYED_KEY, SHIELD_ABSOLUTE_MS, SHIELD_DEADLINE_MS, SHIELD_MS, SKIP_FADE_MS, TIMELINE, TINTS, TOTAL_S,
  auroraAt, buildSparks, coreAt, dockFromRect, geoFor, handoffAlpha, maskPoints, orbAt, ringRot, ringSolid,
  shouldPlay, sparkAt, sparkLavender, type BootGeo, type DockTarget, type Spark } from "../lib/boot";
import { readKey, writeKey } from "../lib/storage";

declare global { interface Window { __hudBootForce?: boolean } }

function seenInWindow(): boolean {
  return readKey(PLAYED_KEY, () => sessionStorage) === "1";
}

/** Decorative only: no focus, audio, requests, or dependency of HUD startup. */
export default function BootOverlay({ cardUp }: { cardUp: boolean }) {
  const [playing, setPlaying] = useState(() => shouldPlay({
    webdriver: navigator.webdriver, forced: window.__hudBootForce === true,
    reducedMotion: matchMedia("(prefers-reduced-motion: reduce)").matches,
    seen: seenInWindow(), cardUp,
  }));
  const overlay = useRef<HTMLDivElement>(null);
  const glCv = useRef<HTMLCanvasElement>(null);
  const fxCv = useRef<HTMLCanvasElement>(null);

  // Considering a window consumes its boot, even if a card or reduced motion
  // suppresses it. A later reload must not become a new window opening.
  useLayoutEffect(() => {
    writeKey(PLAYED_KEY, "1", () => sessionStorage); // this mount still only decides once
  }, []);

  useLayoutEffect(() => {
    if (cardUp) { setPlaying(false); return; }
    if (!playing || !overlay.current || !glCv.current || !fxCv.current) return;
    const host = overlay.current;
    // Decorative: whatever fails here ends the animation, never the window that
    // gates approvals. An uncaught throw in a layout effect unmounts the whole
    // HUD, card included (review of PR #82, F1: a canvas with no 2D context).
    let drawing: Drawing;
    try {
      drawing = startDrawing(host, glCv.current, fxCv.current, () => setPlaying(false));
    } catch {
      setPlaying(false);
      return;
    }
    // One expiry ends the shield: the skip fade's end, or SHIELD_MS after the latest release, so the
    // second half of a double-click is eaten too (F3). It is anchored to the RELEASE: an eaten click never
    // moves it (review of #354, LOW), or a click storm could hold the shield up.
    let skipTimer = 0;
    let expiring = false; // an expiry is pending
    let skipped = false;
    let held = false;
    const finish = () => { drawing.release(); setPlaying(false); };
    const expireIn = (ms: number) => { window.clearTimeout(skipTimer); expiring = true; skipTimer = window.setTimeout(finish, ms); };
    const released = () => { held = false; expireIn(SHIELD_MS); };
    const skip = () => {
      if (skipped) return;
      skipped = true;
      drawing.stop(); // the frame freezes while it fades; the GL context goes at finish
      host.style.transition = "opacity 0.2s linear";
      host.style.opacity = "0";
      expireIn(SKIP_FADE_MS);
    };
    // Capture sees keys even when a pane handles them; never consume the key.
    window.addEventListener("keydown", skip, true);
    const motion = matchMedia("(prefers-reduced-motion: reduce)");
    const reduce = () => { if (motion.matches) finish(); };
    motion.addEventListener("change", reduce);
    // Three bounds. The cap bounds the DRAWING and the GL context whatever the pointer is doing; the shield
    // (invisible by then) stays only while something still owes it time. A pointer still held at the
    // held-pointer deadline loses it then; a release before that keeps its own SHIELD_MS even past the
    // deadline (review of #354, MED). The absolute backstop removes the overlay whatever happens.
    const hardCap = window.setTimeout(() => {
      if (!held && !expiring) { finish(); return; } // nothing owes the shield any time
      drawing.release();
      host.style.transition = "none"; host.style.opacity = "0";
    }, HARD_CAP_MS);
    const deadline = window.setTimeout(() => { if (held || !expiring) finish(); }, SHIELD_DEADLINE_MS);
    const backstop = window.setTimeout(finish, SHIELD_ABSOLUTE_MS);
    // DOM listeners keep the shield through pointerup/click, including long holds.
    const eat = (e: Event) => { e.preventDefault(); e.stopPropagation(); };
    const down = (e: PointerEvent) => {
      eat(e);
      try { host.setPointerCapture(e.pointerId); } catch { /* the pointer is already gone */ }
      skip();
      // A held press keeps the shield until it is let go (F4), up to the held-pointer deadline; the cap
      // still ends the drawing.
      window.clearTimeout(skipTimer); expiring = false;
      held = true;
    };
    // A release, a cancelled touch or lost capture all let the shield go (F2).
    const up = (e: Event) => { eat(e); released(); };
    // A click is eaten. Only a lone click, with no press held and no expiry pending, starts one of its own.
    const click = (e: Event) => { eat(e); if (!held && !expiring) expireIn(SHIELD_MS); };
    const RELEASES = ["pointerup", "pointercancel", "lostpointercapture"] as const;
    const CLICKS = ["click", "dblclick", "auxclick", "contextmenu"] as const;
    // The window losing focus mid-press may never deliver the release.
    const blurred = () => { if (held) released(); };
    const hidden = () => { if (document.visibilityState === "hidden") blurred(); };
    window.addEventListener("blur", blurred);
    document.addEventListener("visibilitychange", hidden);
    host.addEventListener("pointerdown", down);
    for (const kind of RELEASES) host.addEventListener(kind, up);
    for (const kind of CLICKS) host.addEventListener(kind, click);
    return () => {
      drawing.release();
      window.clearTimeout(skipTimer); window.clearTimeout(hardCap); window.clearTimeout(deadline); window.clearTimeout(backstop);
      window.removeEventListener("keydown", skip, true);
      motion.removeEventListener("change", reduce);
      window.removeEventListener("blur", blurred);
      document.removeEventListener("visibilitychange", hidden);
      host.removeEventListener("pointerdown", down);
      for (const kind of RELEASES) host.removeEventListener(kind, up);
      for (const kind of CLICKS) host.removeEventListener(kind, click);
    };
  }, [playing, cardUp]);

  if (!playing || cardUp) return null;
  return <div ref={overlay} id="hud-boot" data-testid="boot-overlay" aria-hidden="true">
    <canvas ref={glCv} data-layer="gl" /><canvas ref={fxCv} data-layer="fx" />
  </div>;
}

// ---- the aurora shader (ported from the concept: the HUD's palette, plus a ripple) ----

const VERT = "attribute vec2 aPos; void main(){ gl_Position = vec4(aPos, 0.0, 1.0); }";
const FRAG = `
precision highp float;
uniform vec2 uRes; uniform float uT, uAur;
uniform vec3 uBase, uW1, uW2, uW3;
uniform vec4 uRip;
float hash(vec2 p){ return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
float noise(vec2 p){ vec2 i = floor(p), f = fract(p); vec2 u = f * f * (3.0 - 2.0 * f);
  return mix(mix(hash(i), hash(i + vec2(1, 0)), u.x), mix(hash(i + vec2(0, 1)), hash(i + vec2(1, 1)), u.x), u.y); }
float fbm(vec2 p){ float v = 0.0, a = 0.5; for (int i = 0; i < 5; i++) { v += a * noise(p); p = p * 2.03 + vec2(1.7, 9.2); a *= 0.5; } return v; }
vec3 aurora(vec2 px){
  vec2 uv = px / uRes.y; float t = uT * 0.07;
  vec2 q = vec2(fbm(uv * 1.2 + vec2(0.0, t)), fbm(uv * 1.2 + vec2(5.2, -t)));
  float n1 = fbm(uv + 1.7 * q + vec2(t * 0.8, 0.0));
  float n2 = fbm(uv * 1.3 - 1.4 * q + vec2(0.0, t * 0.6) + 3.1);
  float n3 = fbm(uv * 0.9 + 2.0 * q.yx + 7.7);
  vec3 c = uBase;
  c = mix(c, uW1, smoothstep(0.38, 0.82, n1) * 0.9);
  c = mix(c, uW2, smoothstep(0.42, 0.88, n2) * 0.8);
  c = mix(c, uW3, smoothstep(0.46, 0.92, n3) * 0.8);
  return mix(uBase, c, uAur);
}
void main(){
  vec2 p = vec2(gl_FragCoord.x, uRes.y - gl_FragCoord.y);
  float px = max(1.0, uRes.y / 900.0);
  vec2 off = vec2(0.0); float crest = 0.0;
  if (uRip.z > 0.0) {
    vec2 d = p - uRip.xy; float dist = length(d);
    float front = uRip.z * uRes.y * 0.75, w = uRes.y * 0.05, x = dist - front;
    float env = exp(-x * x / (w * w * 2.5)) * exp(-uRip.z * 0.9) * uRip.w;
    float wave = sin(x / w * 3.14159);
    off = d / (dist + 1.0) * wave * env * 18.0 * px;
    crest = max(wave, 0.0) * env;
  }
  vec3 col = aurora(p + off) + vec3(0.6, 0.8, 0.9) * crest * 0.06;
  vec2 q = p / uRes - 0.5; col *= 1.0 - dot(q, q) * 0.5;
  col += (hash(p + uT) - 0.5) / 255.0;
  gl_FragColor = vec4(col, 1.0);
}`;

/** The animation's handle: `stop` freezes the frames; `release` also frees the GL context and every listener. */
interface Drawing { stop: () => void; release: () => void }

const mix = (a: Rgb, b: Rgb, k: number): Rgb => [a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k, a[2] + (b[2] - a[2]) * k];
const css = (c: Rgb) => c.map((v) => Math.round(Math.max(0, Math.min(1, v)) * 255)).join(",");
const TAU = Math.PI * 2;

/** The name's face: 800 where it is bundled (the nearest bundled weight otherwise). */
const NAME_FONT = '"Plus Jakarta Sans", "Segoe UI", system-ui, sans-serif';

function startDrawing(host: HTMLDivElement, glCv: HTMLCanvasElement, fxCv: HTMLCanvasElement, done: () => void): Drawing {
  const gl0 = glCv.getContext("webgl", { antialias: false, alpha: false });
  if (!gl0) throw new Error("no WebGL");
  const gl: WebGLRenderingContext = gl0;
  let fx!: CanvasRenderingContext2D;

  // Disposal comes first: from here on, anything that throws (a missing 2D context, a shader that will not
  // compile, the first frame) releases the GL context and every listener and timer before it propagates.
  let raf = 0, fontTimer = 0, stopped = false, released = false, stirred = false, dockMeasured = false;
  let layoutNow: () => void = () => {};
  const stop = () => {
    stopped = true;
    cancelAnimationFrame(raf);
    window.clearTimeout(fontTimer);
    window.removeEventListener("resize", resize);
  };
  const lost = (e: Event) => { e.preventDefault(); stop(); done(); };
  const release = () => {
    stop();
    if (released) return;
    released = true;
    glCv.removeEventListener("webglcontextlost", lost);
    try { gl.getExtension("WEBGL_lose_context")?.loseContext(); } catch { /* already gone */ }
    glCv.width = glCv.height = 1; fxCv.width = fxCv.height = 1;
  };
  // A frame or a resize that throws ends the animation rather than leaving a
  // frozen overlay over the HUD.
  const fail = () => { release(); done(); };
  function resize() { try { layoutNow(); } catch { fail(); } }

  try {
    const fx0 = fxCv.getContext("2d");
    if (!fx0) throw new Error("no 2D canvas");
    fx = fx0;

    // ---- the live tokens: --base, --wash-1..3 and --accent (a custom Appearance palette shows in the boot too) ----
    const root = document.documentElement;
    const pal = readPalette(root);
    const rootStyle = getComputedStyle(root);
    const accent = parseColour(rootStyle.getPropertyValue("--accent")) ?? [0x6f / 255, 0xc3 / 255, 0xdf / 255];
    const accentHi = parseColour(rootStyle.getPropertyValue("--accent-hi")) ?? mix(accent, [1, 1, 1], 0.4);
    const WHITE: Rgb = [1, 1, 1];
    const A = css(accent);
    const GLASS = css(mix(accentHi, WHITE, 0.5));
    const lavender = sparkLavender(pal.colours[2], accent);
    const VIOLET = css(mix(lavender, accent, 0.25));
    const tints = [accent, accentHi, lavender, mix(WHITE, accentHi, 0.12)].slice(0, TINTS);

    // ---- WebGL ----
    const compile = (type: number, src: string) => {
      const s = gl.createShader(type)!;
      gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s) || "shader");
      return s;
    };
    const prog = gl.createProgram()!;
    gl.attachShader(prog, compile(gl.VERTEX_SHADER, VERT));
    gl.attachShader(prog, compile(gl.FRAGMENT_SHADER, FRAG));
    gl.bindAttribLocation(prog, 0, "aPos");
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error("link");
    gl.useProgram(prog);
    gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    const U = (n: string) => gl.getUniformLocation(prog, n);
    const uRes = U("uRes"), uT = U("uT"), uAur = U("uAur"), uRip = U("uRip");
    gl.uniform3fv(U("uBase"), pal.base);
    gl.uniform3fv(U("uW1"), pal.colours[0]); gl.uniform3fv(U("uW2"), pal.colours[1]); gl.uniform3fv(U("uW3"), pal.colours[2]);

    // ---- sparks: a tinted glass dot, pre-drawn once per tint (a glow with a white core) ----
    const SPR = 64, UNIT = 12; // sprite px, and px per unit of spark radius
    const sprites = tints.map((c) => {
      const cv = document.createElement("canvas");
      cv.width = cv.height = SPR;
      const g = cv.getContext("2d");
      if (!g) throw new Error("no 2D canvas");
      g.fillStyle = `rgba(${css(c)},0.32)`; g.beginPath(); g.arc(SPR / 2, SPR / 2, 2.6 * UNIT, 0, TAU); g.fill();
      g.fillStyle = "rgba(255,255,255,0.9)"; g.beginPath(); g.arc(SPR / 2, SPR / 2, 0.7 * UNIT, 0, TAU); g.fill();
      return cv;
    });

    let W = 0, H = 0, sd = 1, gs = 1;
    let geo: BootGeo = geoFor(1, 1);
    let dock: DockTarget = dockFromRect(null, 1, 1);
    let sparks: Spark[] | null = null;
    let fontReady = false;
    let started = 0;

    const measureDock = () => {
      const rect = document.getElementById("orb")?.getBoundingClientRect() ?? null;
      dock = dockFromRect(rect, W, H);
    };
    /** The name's grid points from an offscreen canvas, in the bundled face. */
    const sampleName = (): [number, number][] => {
      const off = document.createElement("canvas");
      off.width = W; off.height = H;
      const o = off.getContext("2d", { willReadFrequently: true });
      if (!o) throw new Error("no 2D canvas");
      o.fillStyle = "#fff"; o.textAlign = "center"; o.textBaseline = "middle";
      o.font = `800 ${geo.wsize}px ${NAME_FONT}`;
      o.fillText("Jarvis", geo.cx, geo.wy);
      return maskPoints(o.getImageData(0, 0, W, H).data, W, H, geo.wsize);
    };
    const layout = () => {
      W = innerWidth; H = innerHeight;
      // The 2D canvas is sharp up to 2x; the aurora is smooth, so it renders at most 1x and 0.75x on a big window.
      sd = Math.min(devicePixelRatio || 1, 2);
      gs = Math.min(devicePixelRatio || 1, 1) * (W * H > 1_000_000 ? 0.75 : 1);
      glCv.width = Math.max(1, Math.round(W * gs)); glCv.height = Math.max(1, Math.round(H * gs));
      fxCv.width = Math.round(W * sd); fxCv.height = Math.round(H * sd);
      geo = geoFor(W, H);
      measureDock();
      if (fontReady) sparks = buildSparks(W, H, geo, sampleName());
    };

    function background(t: number) {
      gl.viewport(0, 0, glCv.width, glCv.height);
      gl.uniform2f(uRes, glCv.width, glCv.height);
      gl.uniform1f(uT, t + 4);
      gl.uniform1f(uAur, auroraAt(t));
      const rip = t > TIMELINE.ignite;
      gl.uniform4f(uRip, rip ? geo.cx * gs : 0, rip ? geo.cy * gs : 0, rip ? t - TIMELINE.ignite : 0, rip ? 1 : 0);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    }

    /** The glass rings and the core, translated to the orb's centre. */
    function drawOrb(t: number, o: { cx: number; cy: number; R: number }) {
      const R = o.R;
      fx.save(); fx.translate(o.cx, o.cy);
      const ring = (r: number, a0: number, a1: number, w: number, c: string) => {
        fx.beginPath(); fx.arc(0, 0, r, a0, a1); fx.lineWidth = w; fx.strokeStyle = c; fx.stroke();
      };
      let s = ringSolid(0, t);
      if (s > 0) {
        fx.save(); fx.rotate(ringRot(0, t)); fx.globalAlpha = s;
        for (let i = 0; i < 120; i++) {
          const a = (i / 120) * TAU, l = i % 10 === 0 ? 0.07 : 0.035;
          fx.beginPath();
          fx.moveTo(Math.cos(a) * R * (0.97 - l), Math.sin(a) * R * (0.97 - l));
          fx.lineTo(Math.cos(a) * R * 0.97, Math.sin(a) * R * 0.97);
          fx.strokeStyle = `rgba(${GLASS},${i % 10 === 0 ? 0.75 : 0.28})`; fx.lineWidth = i % 10 === 0 ? 1.4 : 0.8; fx.stroke();
        }
        fx.restore();
      }
      s = ringSolid(1, t);
      if (s > 0) {
        fx.save(); fx.rotate(ringRot(1, t)); fx.globalAlpha = s;
        BOOT_RINGS[1]!.arcs!.forEach(([a0, a1], i) => {
          ring(R * 0.82, a0, a1, R * 0.05, `rgba(${A},${i % 3 === 0 ? 0.55 : 0.18})`);
          ring(R * 0.80, a0, a1, 1, `rgba(255,255,255,${i % 3 === 0 ? 0.5 : 0.18})`);
        });
        fx.restore();
      }
      s = ringSolid(2, t);
      if (s > 0) {
        fx.save(); fx.rotate(ringRot(2, t)); fx.globalAlpha = s;
        BOOT_RINGS[2]!.arcs!.forEach(([a0, a1], i) => {
          ring(R * 0.64, a0, a1, R * 0.035, `rgba(${A},${[0.4, 0.7, 0.25, 0.55][i]})`);
          ring(R * 0.62, a0, a1, 1, "rgba(255,255,255,0.35)");
        });
        fx.restore();
      }
      s = ringSolid(3, t);
      if (s > 0) {
        fx.save(); fx.rotate(ringRot(3, t)); fx.globalAlpha = s;
        const dot = R / geo.R * 0.6 + 0.4;
        for (let i = 0; i < 72; i++) {
          const a = (i / 72) * TAU;
          fx.beginPath(); fx.arc(Math.cos(a) * R * 0.52, Math.sin(a) * R * 0.52, (i % 9 === 0 ? 2.2 : 1.2) * dot, 0, TAU);
          fx.fillStyle = i % 9 === 0 ? `rgba(${A},0.95)` : `rgba(${GLASS},0.35)`; fx.fill();
        }
        fx.restore();
      }
      s = ringSolid(4, t);
      if (s > 0) {
        fx.globalAlpha = s; fx.setLineDash([R * 0.04, R * 0.02]);
        fx.save(); fx.rotate(ringRot(4, t)); ring(R * 0.4, 0, TAU, 1, `rgba(${GLASS},0.45)`); fx.restore();
        fx.setLineDash([]); ring(R * 0.33, 0, TAU, 1.4, `rgba(${A},0.65)`);
      }
      fx.globalAlpha = 1;
      // The core ignites.
      const { glow, pulse } = coreAt(t);
      const cr = R * (0.18 + 0.1 * glow + 0.1 * pulse);
      const g = fx.createRadialGradient(0, 0, 0, 0, 0, cr * 1.9);
      g.addColorStop(0, `rgba(255,255,255,${0.95 * glow})`);
      g.addColorStop(0.25, `rgba(${A},${0.85 * glow})`);
      g.addColorStop(0.6, `rgba(${VIOLET},${0.22 * glow})`);
      g.addColorStop(1, "rgba(0,0,0,0)");
      fx.fillStyle = g; fx.beginPath(); fx.arc(0, 0, cr * 1.9, 0, TAU); fx.fill();
      fx.restore();
    }

    function drawSparks(t: number, o: { cx: number; cy: number; R: number }) {
      if (!sparks) return;
      fx.globalCompositeOperation = "lighter";
      const D = Math.max(W, H);
      for (const p of sparks) {
        const [x, y, a] = sparkAt(p, t, o, D);
        if (a < 0.01) continue;
        const half = p.r * 2.6;
        fx.globalAlpha = a;
        fx.drawImage(sprites[p.tint]!, x - half, y - half, half * 2, half * 2);
      }
      fx.globalAlpha = 1;
      fx.globalCompositeOperation = "source-over";
    }

    function frame(t: number) {
      background(t);
      fx.setTransform(sd, 0, 0, sd, 0, 0);
      fx.clearRect(0, 0, W, H);
      if (t >= TIMELINE.dockStart - 0.3 && !dockMeasured) { dockMeasured = true; measureDock(); } // the HUD has settled by now
      const o = orbAt(geo, dock, t);
      drawOrb(t, o);
      drawSparks(t, o);
      host.style.opacity = String(handoffAlpha(t));
      host.dataset.orb = `${o.cx.toFixed(1)},${o.cy.toFixed(1)},${o.R.toFixed(1)}`;
      host.dataset.docked = o.dock >= 1 ? "1" : "0";
      // The real aurora is stirred once, from the ignition's point, as the HUD takes over.
      if (!stirred && t >= TIMELINE.end) { stirred = true; chatActivity.rippleAt(geo.cx, geo.cy); }
    }

    const tick = (now: number) => {
      if (stopped) return;
      const t = fontReady ? (now - started) / 1000 : 0;
      try {
        frame(t);
      } catch {
        fail(); return;
      }
      if (t < TOTAL_S) raf = requestAnimationFrame(tick);
      else done();
    };

    glCv.addEventListener("webglcontextlost", lost);
    layoutNow = layout;
    layout();
    window.addEventListener("resize", resize);
    frame(0);
    raf = requestAnimationFrame(tick);

    // The name is sampled from the bundled face, so the clock starts when it is ready (or after a short wait).
    const fonts = document.fonts;
    const wait = new Promise<void>((resolve) => {
      fontTimer = window.setTimeout(resolve, FONT_WAIT_MS);
      const ok = () => { window.clearTimeout(fontTimer); resolve(); };
      if (fonts?.load) fonts.load(`800 64px "Plus Jakarta Sans"`, "Jarvis").then(ok, ok); else ok();
    });
    void wait.then(() => {
      if (stopped) return;
      try {
        fontReady = true;
        sparks = buildSparks(W, H, geo, sampleName());
        started = performance.now();
      } catch {
        fail();
      }
    });
  } catch (e) {
    release();
    throw e;
  }
  return { stop, release };
}
