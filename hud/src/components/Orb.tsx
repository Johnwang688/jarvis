// The orb, ported from jarvis.html: canvas rings (RING_SETS, an avatar may
// swap the outer one), the state palette, and push-to-talk. The avatar's face
// is an <img> above the canvas, fixed size and pointer-events:none, so it can
// neither cover the authorization card nor eat a click meant for it.
//
// The mic lives here too (2026-10-10): the dictation mode (AUTO / REVIEW /
// OFF) and the level meter sit just above the orb, so everything about
// listening is one place, and the selected chat's status is the line under
// it. Where the strip cannot ride — a folded sidebar shrinks the dock to a
// 36px mini orb, and a short window needs the sidebar's rows more than a
// 44px strip — it folds into one button (`#modecycle`) that cycles the mode
// (OFF → REVIEW → AUTO, lib/dictation `nextMode`) and goes red at OFF: above
// the mini orb, or beside the full one.
//
// Under an authorization card none of it is a lever: everything outside the
// card is inert (Approvals.tsx), and the mode buttons are disabled as well,
// so a button that had focus when the card came up cannot be pressed by an
// Enter or a Space behind it (review of PR #29).

import { useEffect, useRef } from "react";
import type { OrbState } from "../state/store";
import { DICTATION_MODES, nextMode, type DictationMode } from "../lib/dictation";

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
  /** The dictation mode, and its change. */
  mode: DictationMode;
  onModeChange: (m: DictationMode) => void;
  /** Folded into the left rail's foot while the sidebar is hidden. */
  compact?: boolean;
  /** A short window: the strip folds into the one cycling button, beside the
   * orb, so the sidebar keeps its rows. */
  tight?: boolean;
  /** An authorization card is up: the mode buttons are disabled. */
  blocked?: boolean;
  /** The HUD zoom in percent: the canvas is drawn at that many more pixels. */
  zoom?: number;
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
    // Drawn at the zoom as well as the screen's density, or 160% is a blur.
    const dpr = Math.min(window.devicePixelRatio || 1, 2) * Math.max(0.5, (props.zoom || 100) / 100);
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
  }, [props.avatarUrl, props.zoom]);

  const next = nextMode(props.mode);
  const folded = !!props.compact || !!props.tight;
  return (
    <>
    <div
      id="orbdock"
      className={props.compact ? "mini" : props.tight ? "tight" : undefined}
      data-mode={props.mode}
    >
      <div id="micstrip">
        <div id="dictation" data-testid="dictation" title="What happens to what you say: AUTO sends it, REVIEW puts it in the box, OFF mutes the mic">
          {DICTATION_MODES.map((m) => (
            <button
              type="button"
              key={m}
              data-testid={`dictation-${m}`}
              className={(props.mode === m ? "on " : "") + (m === "off" ? "off" : "")}
              aria-pressed={props.mode === m}
              disabled={props.blocked}
              onClick={() => props.onModeChange(m)}
            >
              {m}
            </button>
          ))}
        </div>
        <div id="level" data-testid="level" data-level={props.level.toFixed(3)}>
          <i style={{ width: `${Math.min(100, props.level * 100)}%` }} />
        </div>
      </div>
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
      <div className="st" id="orbstatus" data-testid="orb-status" title={props.status || undefined}>
        {props.status || ""}
      </div>
    </div>
    {folded ? (
      // The dock is scaled down to a 36px mini orb, and so would a strip inside
      // it be; a short window has no room above the orb for it. Either way the
      // mode is one small button that cycles: above the mini orb, or beside the
      // full one.
      <button
        type="button"
        id="modecycle"
        className={props.compact ? undefined : "beside"}
        data-testid="dictation-cycle"
        data-mode={props.mode}
        disabled={props.blocked}
        title={`Dictation: ${props.mode.toUpperCase()} — click for ${next.toUpperCase()}`}
        onClick={() => props.onModeChange(next)}
      >
        {props.mode === "review" ? "rev" : props.mode}
      </button>
    ) : null}
    </>
  );
}
