import { useLayoutEffect, useRef, useState } from "react";
import { drawEmblem, drawRing } from "../lib/orbdraw";
import { BOOT_DEFAULTS, FADE_OUT_S, HARD_CAP_MS, PLAYED_KEY, SHIELD_MS, SKIP_FADE_MS,
  RINGS, SPIN, bitAt, buildDust, clamp01, easeInOutCubic, hexToRgb, layoutFor,
  particleAt, plateGeometry, penAt, dotsAt, ringRot, shouldPlay } from "../lib/boot";
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
  const orb = useRef<HTMLCanvasElement>(null);
  const dust = useRef<HTMLCanvasElement>(null);
  const plate = useRef<SVGSVGElement>(null);

  // Considering a window consumes its boot, even if a card or reduced motion
  // suppresses it. A later reload must not become a new window opening.
  useLayoutEffect(() => {
    writeKey(PLAYED_KEY, "1", () => sessionStorage); // this mount still only decides once
  }, []);

  useLayoutEffect(() => {
    if (cardUp) { setPlaying(false); return; }
    if (!playing || !overlay.current || !orb.current || !dust.current || !plate.current) return;
    const host = overlay.current;
    // Decorative: whatever fails here ends the animation, never the window that
    // gates approvals. An uncaught throw in a layout effect unmounts the whole
    // HUD, card included (review of PR #82, F1: a canvas with no 2D context).
    let stopDrawing: () => void;
    try {
      stopDrawing = startDrawing(host, orb.current, dust.current, plate.current, () => setPlaying(false));
    } catch {
      setPlaying(false);
      return;
    }
    let skipTimer = 0;
    let skipped = false;
    const finish = () => { stopDrawing(); setPlaying(false); };
    // The shield stays up SHIELD_MS past the last release or click, so the
    // second half of a double-click is eaten too (F3).
    const linger = () => { window.clearTimeout(skipTimer); skipTimer = window.setTimeout(finish, SHIELD_MS); };
    const skip = () => {
      if (skipped) return;
      skipped = true;
      stopDrawing();
      host.style.transition = "opacity 0.2s linear";
      host.style.opacity = "0";
      skipTimer = window.setTimeout(finish, SKIP_FADE_MS);
    };
    // Capture sees keys even when a pane handles them; never consume the key.
    window.addEventListener("keydown", skip, true);
    const motion = matchMedia("(prefers-reduced-motion: reduce)");
    const reduce = () => { if (motion.matches) finish(); };
    motion.addEventListener("change", reduce);
    let hardCap = window.setTimeout(finish, HARD_CAP_MS);
    // DOM listeners keep the shield through pointerup/click, including long holds.
    const eat = (e: Event) => { e.preventDefault(); e.stopPropagation(); };
    const down = (e: PointerEvent) => {
      eat(e);
      try { host.setPointerCapture(e.pointerId); } catch { /* the pointer is already gone */ }
      skip();
      window.clearTimeout(skipTimer);
      // A held press keeps the shield until it is let go, however long (F4).
      window.clearTimeout(hardCap);
      hardCap = 0;
    };
    // A release, a cancelled touch or lost capture all let the shield go (F2).
    const up = (e: Event) => { eat(e); linger(); };
    const click = (e: Event) => { eat(e); linger(); };
    const RELEASES = ["pointerup", "pointercancel", "lostpointercapture"] as const;
    const CLICKS = ["click", "dblclick", "auxclick", "contextmenu"] as const;
    // The window losing focus mid-press may never deliver the release.
    const blurred = () => linger();
    window.addEventListener("blur", blurred);
    host.addEventListener("pointerdown", down);
    for (const kind of RELEASES) host.addEventListener(kind, up);
    for (const kind of CLICKS) host.addEventListener(kind, click);
    return () => {
      stopDrawing();
      window.clearTimeout(skipTimer); window.clearTimeout(hardCap);
      window.removeEventListener("keydown", skip, true);
      motion.removeEventListener("change", reduce);
      window.removeEventListener("blur", blurred);
      host.removeEventListener("pointerdown", down);
      for (const kind of RELEASES) host.removeEventListener(kind, up);
      for (const kind of CLICKS) host.removeEventListener(kind, click);
    };
  }, [playing, cardUp]);

  if (!playing || cardUp) return null;
  return <div ref={overlay} id="hud-boot" data-testid="boot-overlay" aria-hidden="true">
    <canvas ref={orb} /><canvas ref={dust} /><svg ref={plate} />
  </div>;
}

function startDrawing(host: HTMLDivElement, orbCv: HTMLCanvasElement,
  dustCv: HTMLCanvasElement, svg: SVGSVGElement, done: () => void): () => void {
  const ctx2d = (cv: HTMLCanvasElement) => {
    const ctx = cv.getContext("2d");
    if (!ctx) throw new Error("no 2D canvas");
    return ctx;
  };
  const octx = ctx2d(orbCv);
  const dctx = ctx2d(dustCv);
  const CFG = BOOT_DEFAULTS;
  const theme = getComputedStyle(host);
  const HUE = {
    accent: hexToRgb(theme.getPropertyValue("--accent"))!,
    hi: hexToRgb(theme.getPropertyValue("--accent-hi"))!,
    text: hexToRgb(theme.getPropertyValue("--text"))!,
  };
  const hues = [HUE.accent, HUE.hi, HUE.text];
  let W = innerWidth, H = innerHeight, dpr = devicePixelRatio || 1;
  let L = layoutFor(W, H);
  let parts: ReturnType<typeof buildDust>["parts"];
  let timeline: ReturnType<typeof buildDust>["timeline"];
  let orbStart = 0, orbEnd = 0;
  function drawDust(t: number) {
    dctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    // Fade what is there toward transparent: that is the trail.
    dctx.globalCompositeOperation = "destination-out";
    dctx.fillStyle = `rgba(0,0,0,${1 - CFG.trail})`;
    dctx.fillRect(0, 0, W, H);
    dctx.globalCompositeOperation = "source-over";
    dctx.textAlign = "center"; dctx.textBaseline = "middle";
    let font = "";
    for (const p of parts) {
      const at = particleAt(L, p, t);
      if (!at || at[2] <= 0.01) continue;
      const f = `${p.size}px ui-monospace, "Cascadia Mono", Consolas, monospace`;
      if (f !== font) { dctx.font = f; font = f; }
      dctx.fillStyle = `rgba(${hues[p.hue]},${at[2].toFixed(3)})`;
      dctx.fillText(bitAt(p, t), at[0], at[1]);
    }
  }

  // ---- the orb (ported from hud/src/components/Orb.tsx) ----
  function drawOrb(t: number) {
    octx.setTransform(dpr, 0, 0, dpr, 0, 0);
    octx.clearRect(0, 0, W, H);
    const k = easeInOutCubic(clamp01((t - orbStart) / Math.max(0.01, orbEnd - orbStart)));
    if (k <= 0) return;
    const c = { x: L.cx, y: L.cy };
    const R = L.orbR * (0.92 + 0.08 * k);
    const hue = HUE.accent, boost = 1.1;
    const energy = 0.35 + 0.15 * Math.sin(t * 1.4);
    for (const ring of RINGS) {
      const rot = ringRot(ring, t);
      octx.strokeStyle = `rgba(${hue},${Math.min(1, ring.alpha * boost * (0.75 + energy * 0.45)) * k})`;
      octx.lineWidth = ring.w;
      drawRing(octx, ring, c.x, c.y, R, rot);
    }
    const coreR = R * (0.3 + energy * 0.1);
    const glow = octx.createRadialGradient(c.x, c.y, 0, c.x, c.y, coreR * 1.9);
    glow.addColorStop(0, `rgba(240,238,232,${0.85 * boost * (0.55 + energy * 0.5) * k})`);
    glow.addColorStop(0.25, `rgba(${hue},${0.5 * (0.5 + energy * 0.6) * k})`);
    glow.addColorStop(1, "rgba(0,0,0,0)");
    octx.fillStyle = glow;
    octx.beginPath(); octx.arc(c.x, c.y, coreR * 1.9, 0, Math.PI * 2); octx.fill();
    drawEmblem(octx, c.x, c.y, R, -t * 0.15 * SPIN, (0.5 + energy * 0.5) * k, hue, 18 * (0.6 + energy));
  }


  const NS = "http://www.w3.org/2000/svg";
  const el = <K extends keyof SVGElementTagNameMap>(tag: K, attrs: Record<string, string | number>) => {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, String(v));
    svg.appendChild(e);
    return e;
  };
  const strokeOf = (d: string, width: number) => {
    const e = el("path", { d, class: "boot-stroke", "stroke-width": width, filter: "url(#boot-glow)" });
    const len = e.getTotalLength();
    e.style.strokeDasharray = `${len} ${len + 1}`;
    return { e, len };
  };
  type Stroke = ReturnType<typeof strokeOf>;
  const pen = () => el("circle", { r: 3.2, fill: "#f4f6f6", filter: "url(#boot-pen)", opacity: 0 });
  function buildPlate() {
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.innerHTML = `<defs>
      <filter id="boot-glow" filterUnits="userSpaceOnUse" x="0" y="0" width="${W}" height="${H}">
        <feGaussianBlur stdDeviation="2.2" result="b"/>
        <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
      <filter id="boot-pen" filterUnits="userSpaceOnUse" x="0" y="0" width="${W}" height="${H}">
        <feGaussianBlur stdDeviation="3.5" result="b"/>
        <feFlood flood-color="rgb(${HUE.hi})"/><feComposite in2="b" operator="in" result="g"/>
        <feMerge><feMergeNode in="g"/><feMergeNode in="g"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
    </defs>`;
    const g = plateGeometry(L), r = g.rect;
    const fill = el("rect", { x: r.x, y: r.y, width: r.w, height: r.h,
      fill: `rgba(${HUE.accent},0.05)`, opacity: 0 });
    const rule = el("rect", { x: r.x + g.inset, y: r.y + g.inset,
      width: r.w - 2 * g.inset, height: r.h - 2 * g.inset,
      fill: "none", stroke: `rgba(${HUE.accent},0.22)`, "stroke-width": 1, opacity: 0 });
    const A = strokeOf(g.boxA, g.boxWidth), B = strokeOf(g.boxB, g.boxWidth);
    const letters = g.letters.map(ds => {
      const strokes = ds.map(d => strokeOf(d, g.letterWidth));
      return { strokes, total: strokes.reduce((a, st) => a + st.len, 0), pen: null as SVGCircleElement | null };
    });
    const dots = g.dots.map(d => ({ center: d, e: el("rect", {
      x: d.x - d.size / 2, y: d.y - d.size / 2, width: d.size, height: d.size,
      fill: `rgb(${HUE.hi})`, filter: "url(#boot-glow)", opacity: 0,
    }) }));
    const penA = pen(), penB = pen();
    for (const lt of letters) lt.pen = pen();
    return { A, B, penA, penB, letters, dots, fill, rule };
  }
  let drawingPlate: ReturnType<typeof buildPlate>;
  function drawRun(st: Stroke, len: number) {
    const seg = Math.max(0, Math.min(st.len, len));
    st.e.style.strokeDashoffset = String(st.len - seg);
    st.e.style.opacity = seg > 0 ? "1" : "0";
  }
  function placePen(c: SVGCircleElement, st: Stroke, len: number, vis: number) {
    const pt = st.e.getPointAtLength(Math.max(0, Math.min(st.len, len)));
    c.setAttribute("cx", String(pt.x)); c.setAttribute("cy", String(pt.y)); c.setAttribute("opacity", String(vis));
  }
  function drawPlate(t: number) {
    const { e, vis } = penAt(timeline, CFG, t);
    const p = drawingPlate;
    for (const [st, c] of [[p.A, p.penA], [p.B, p.penB]] as const) {
      drawRun(st, e * st.len); placePen(c, st, e * st.len, vis);
    }
    for (const lt of p.letters) {
      let left = e * lt.total, tip: Stroke | null = null, tipLen = 0;
      for (const st of lt.strokes) {
        drawRun(st, left);
        if (left >= 0 && (left <= st.len || !tip)) { tip = st; tipLen = left; }
        left -= st.len;
      }
      placePen(lt.pen!, tip!, tipLen, vis);
    }
    const { q, pop } = dotsAt(timeline, t);
    for (const d of p.dots) {
      d.e.setAttribute("opacity", String(q));
      d.e.setAttribute("transform", `translate(${d.center.x} ${d.center.y}) scale(${pop}) translate(${-d.center.x} ${-d.center.y})`);
    }
    p.fill.setAttribute("opacity", String(q)); p.rule.setAttribute("opacity", String(q));
  }
  const layout = () => {
    W = innerWidth; H = innerHeight; dpr = devicePixelRatio || 1;
    for (const cv of [orbCv, dustCv]) { cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); }
    L = layoutFor(W, H);
    ({ parts, timeline } = buildDust(L));
    orbStart = timeline.orbStart; orbEnd = timeline.orbEnd;
    drawingPlate = buildPlate();
  };
  layout();
  // A frame or a resize that throws ends the animation rather than leaving a
  // frozen overlay over the HUD.
  const resize = () => { try { layout(); } catch { stop(); done(); } };
  window.addEventListener("resize", resize);
  let raf = 0, stopped = false;
  const started = performance.now();
  const stop = () => { stopped = true; cancelAnimationFrame(raf); window.removeEventListener("resize", resize); };
  const tick = (now: number) => {
    if (stopped) return;
    const t = (now - started) / 1000;
    try {
      drawOrb(t); drawDust(t); drawPlate(t);
    } catch {
      stop(); done(); return;
    }
    host.style.opacity = String(1 - clamp01((t - timeline.end) / FADE_OUT_S));
    if (t < timeline.end + FADE_OUT_S) raf = requestAnimationFrame(tick);
    else done();
  };
  drawOrb(0); drawDust(0); drawPlate(0);
  raf = requestAnimationFrame(tick);
  return stop;
}
