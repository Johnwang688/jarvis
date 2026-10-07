// The orb, ported from jarvis.html: canvas rings (RING_SETS, an avatar may
// swap the outer one), the state palette, and push-to-talk. The avatar's face
// is an <img> above the canvas, fixed size and pointer-events:none, so it can
// neither cover the authorization card nor eat a click meant for it.

import { useEffect, useRef } from "react";
import type { OrbState } from "../state/store";

interface Ring {
  r: number;
  ticks?: number;
  arcs?: [number, number][];
  poly?: number;
  copies?: number;
  len?: number;
  w: number;
  speed: number;
  alpha: number;
}

const INNER_RINGS: Ring[] = [
  { r: 0.905, ticks: 72, len: 0.06, w: 2, speed: -0.1, alpha: 0.6 },
  { r: 0.8, arcs: [[0, 1.1], [1.4, 2.6], [3.1, 4.4], [4.8, 5.9]], w: 2.5, speed: 0.16, alpha: 0.55 },
  { r: 0.69, ticks: 36, len: 0.045, w: 1.5, speed: 0.22, alpha: 0.5 },
  { r: 0.6, arcs: [[0, 2.4], [2.9, 5.6]], w: 1, speed: -0.3, alpha: 0.45 },
];

export const RING_SETS: Record<string, Ring[]> = {
  default: [{ r: 0.985, ticks: 144, len: 0.022, w: 1, speed: 0.04, alpha: 0.4 }, ...INNER_RINGS],
  // Two equilateral triangles offset by half a step so they interlock,
  // turning as one figure.
  triangles: [{ r: 1.008, poly: 3, copies: 2, w: 1.6, speed: 0.04, alpha: 0.45 }, ...INNER_RINGS],
};

// The avatar's accent recolours the cyan family only. Amber (tool, pending
// authorization) and red (error) are *meanings*, not decoration — an avatar
// that could repaint them could make a running command look idle.
const ACCENTABLE = new Set(["idle", "listening", "transcribing", "thinking", "composing", "speaking"]);

function palette(state: OrbState, accent?: string | null) {
  const p = (() => {
    switch (state) {
      // Graphite: the cool states are the glacier accent (111,195,223) and its
      // lighter step (154,214,234). Idle is a little dimmer and slower, so the
      // orb is present without glowing at you all session.
      case "listening": return { hue: "154,214,234", boost: 1.35, spin: 1.6 };
      case "transcribing": return { hue: "154,214,234", boost: 1.2, spin: 2.2 };
      case "thinking": return { hue: "111,195,223", boost: 1.15, spin: 3.4 };
      case "composing": return { hue: "125,205,200", boost: 1.3, spin: 1.8 };
      case "tool": return { hue: "251,191,36", boost: 1.2, spin: 3.4 };
      case "approval": return { hue: "251,191,36", boost: 1.5, spin: 0.35 };
      case "speaking": return { hue: "154,214,234", boost: 1.5, spin: 1.2 };
      case "error": return { hue: "248,113,113", boost: 1.3, spin: 0.6 };
      default: return { hue: "111,195,223", boost: 0.85, spin: 0.8 };
    }
  })();
  if (accent && ACCENTABLE.has(state)) p.hue = accent;
  return p;
}

const SIZE = 132;

export function Orb(props: {
  state: OrbState;
  level: number;
  rings?: string;
  accent?: string | null;
  avatarUrl?: string | null;
  status?: string;
  onPress: () => void;
  onRelease: () => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const live = useRef({ state: props.state, level: props.level, rings: props.rings, accent: props.accent });
  live.current = { state: props.state, level: props.level, rings: props.rings, accent: props.accent };

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = canvas.height = SIZE * dpr;
    let raf = 0;
    const t0 = performance.now();
    let smooth = 0;

    const draw = (now: number) => {
      const { state, level, rings: ringName, accent } = live.current;
      // An unknown ring set falls back to the default rather than drawing
      // nothing: the orb *is* the push-to-talk button, so an avatar that could
      // blank it would take the surface's main control with it.
      const RINGS = RING_SETS[ringName || "default"] || RING_SETS.default;
      const t = (now - t0) / 1000;
      const P = palette(state, accent);
      smooth += (level - smooth) * 0.25;
      const breath = 0.5 + 0.5 * Math.sin(t * 1.4);
      const energy = Math.min(1, smooth * 2.2 + (state === "idle" ? breath * 0.18 : 0.28));
      const c = SIZE / 2;
      const R = SIZE / 2 - 3;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, SIZE, SIZE);

      for (const ring of RINGS) {
        const rot = (t * ring.speed * P.spin * Math.PI * 2) / 8;
        const alpha = ring.alpha * P.boost * (0.75 + energy * 0.45);
        ctx.strokeStyle = `rgba(${P.hue},${Math.min(1, alpha)})`;
        ctx.lineWidth = ring.w;
        if (ring.poly) {
          const copies = ring.copies || 1;
          for (let k = 0; k < copies; k++) {
            const spin = rot + (k / copies) * ((Math.PI * 2) / ring.poly);
            ctx.beginPath();
            for (let i = 0; i < ring.poly; i++) {
              const a = spin + Math.PI / 2 + (i / ring.poly) * Math.PI * 2;
              const x = c + Math.cos(a) * R * ring.r;
              const y = c - Math.sin(a) * R * ring.r;
              i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
            }
            ctx.closePath();
            ctx.stroke();
          }
        } else if (ring.ticks) {
          const inner = R * ring.r * (1 - (ring.len || 0));
          const outer = R * ring.r;
          ctx.beginPath();
          for (let i = 0; i < ring.ticks; i++) {
            const a = rot + (i / ring.ticks) * Math.PI * 2;
            ctx.moveTo(c + Math.cos(a) * inner, c + Math.sin(a) * inner);
            ctx.lineTo(c + Math.cos(a) * outer, c + Math.sin(a) * outer);
          }
          ctx.stroke();
        } else if (ring.arcs) {
          for (const [a1, a2] of ring.arcs) {
            ctx.beginPath();
            ctx.arc(c, c, R * ring.r, rot + a1, rot + a2);
            ctx.stroke();
          }
        }
      }

      const coreR = R * (0.3 + energy * 0.1);
      const glow = ctx.createRadialGradient(c, c, 0, c, c, coreR * 1.9);
      glow.addColorStop(0, `rgba(240,238,232,${0.85 * P.boost * (0.55 + energy * 0.5)})`);
      glow.addColorStop(0.25, `rgba(${P.hue},${0.5 * (0.5 + energy * 0.6)})`);
      glow.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = glow;
      ctx.beginPath();
      ctx.arc(c, c, coreR * 1.9, 0, Math.PI * 2);
      ctx.fill();

      if (!props.avatarUrl) {
        const triR = R * 0.21;
        const rot3 = -t * 0.15 * P.spin;
        ctx.strokeStyle = `rgba(240,238,232,${0.5 + energy * 0.5})`;
        ctx.lineWidth = 2;
        ctx.shadowColor = `rgb(${P.hue})`;
        ctx.shadowBlur = 18 * (0.6 + energy);
        ctx.beginPath();
        for (let i = 0; i <= 3; i++) {
          const a = rot3 + Math.PI / 2 + (i / 3) * Math.PI * 2;
          const x = c + Math.cos(a) * triR;
          const y = c - Math.sin(a) * triR;
          i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
        }
        ctx.stroke();
        ctx.shadowBlur = 0;
      }
      raf = requestAnimationFrame(draw);
    };
    raf = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(raf);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.avatarUrl]);

  return (
    <div id="orbdock">
      <canvas
        ref={canvasRef}
        id="orb"
        data-testid="orb"
        data-state={props.state}
        onPointerDown={(e) => {
          e.preventDefault();
          props.onPress();
        }}
        onPointerUp={props.onRelease}
        onPointerLeave={props.onRelease}
      />
      {props.avatarUrl ? (
        <img
          className="avatar"
          id="avatar"
          alt=""
          src={props.avatarUrl}
          onError={(e) => {
            // A face that will not load falls back to the emblem rather than
            // leaving a broken image where the PTT button is.
            (e.currentTarget as HTMLImageElement).style.display = "none";
          }}
        />
      ) : null}
      <div className="st" id="orbstatus">{props.status || ""}</div>
    </div>
  );
}
