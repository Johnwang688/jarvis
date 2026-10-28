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
import { palette } from "../lib/orbpalette";
import { DEFAULT_RINGS, INNER_RINGS, drawEmblem, drawRing, type Ring } from "../lib/orbdraw";
import { noPointerFocus } from "../lib/talkkey";
import type { ReactNode } from "react";

export const RING_SETS: Record<string, Ring[]> = {
  default: DEFAULT_RINGS,
  // Two equilateral triangles offset by half a step so they interlock,
  // turning as one figure.
  triangles: [{ r: 1.008, poly: 3, copies: 2, w: 1.6, speed: 0.04, alpha: 0.45 }, ...INNER_RINGS],
};

// The state palette — and the rule that the avatar's accent recolours the
// cyan family only, never amber (a tool, an authorization) or red (an error)
// — is lib/orbpalette.ts, shared with each chat's mini orb (WP-H).

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
  /**
   * Jarvis's own output level (0..1), read each frame while he speaks: the orb
   * breathes with his voice rather than with the room. Not state, so speech
   * does not re-render the window.
   */
  outputLevel?: () => number;
  /** The speech strip (components/SpeechStrip), above the mic strip. */
  speech?: ReactNode;
  /** Folded into the rail, where the dock is a scaled 36px orb: the strip is one button beside it, outside the scaling. */
  speechRail?: ReactNode;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const live = useRef({ state: props.state, level: props.level, rings: props.rings, accent: props.accent, out: props.outputLevel });
  live.current = { state: props.state, level: props.level, rings: props.rings, accent: props.accent, out: props.outputLevel };

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
      const { state, rings: ringName, accent, out } = live.current;
      const level = state === "speaking" && out ? out() : live.current.level;
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
        drawRing(ctx, ring, c, c, R, rot);
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
        drawEmblem(ctx, c, c, R, -t * 0.15 * P.spin, 0.5 + energy * 0.5, P.hue, 18 * (0.6 + energy));
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
      {props.compact ? null : props.speech}
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
        // Push-to-talk's key since 2026-10-10 (lib/talkkey.ts); in a
        // terminal Ctrl+D is the shell's, and the orb is the way to talk.
        title="Hold to talk, or hold Ctrl+D"
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
    {props.compact ? props.speechRail : null}
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
        // A click must not leave it focused: a habitual Space would cycle the
        // mic on again (OFF → REVIEW → AUTO; lib/talkkey.ts, noPointerFocus).
        onMouseDown={noPointerFocus}
        title={`Dictation: ${props.mode.toUpperCase()} — click for ${next.toUpperCase()}`}
        onClick={() => props.onModeChange(next)}
      >
        {props.mode === "review" ? "rev" : props.mode}
      </button>
    ) : null}
    </>
  );
}
