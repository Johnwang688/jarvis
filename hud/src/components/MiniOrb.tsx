// The thinking line's mini orb (WP-H, decisions S-4), and the line itself.
//
// An 18px inline SVG, not a canvas (plan H-2): eight chat panes may each show
// one, and eight 60 fps canvases — with eight copies of the push-to-talk
// button's ids — are what the big orb (components/Orb.tsx) would cost. Here a
// 24-tick outer ring and a two-arc inner ring turn in opposite directions and
// the core flexes, all CSS `@keyframes` on `transform` (theme.css, `.miniorb`),
// which the compositor runs off the main thread. Its colour is the big orb's
// palette (lib/orbpalette), so the avatar's accent recolours "Thinking…" and
// never amber (a tool, an approval) or red (an error).
//
// It is decoration: `aria-hidden`, no pointer events, no buttons or links, in
// the pane's flow under the transcript. The word is the `role="status"`; the
// time beside it is not, so a screen reader does not hear every second.
// Under `prefers-reduced-motion: reduce` nothing turns or flexes — colour and
// word still carry the state, and the time still counts.

import { memo, useSyncExternalStore, type CSSProperties } from "react";
import type { OrbState } from "../state/store";
import { palette } from "../lib/orbpalette";
import { elapsed, type ThinkingLine } from "../lib/thinking";

// ---- one clock for every line on screen -------------------------------------
//
// The elapsed times share one 1 s interval, which runs only while some drawn
// line shows a time (each subscribes while it is drawn), and only those
// spans re-render on a tick — never the window, nor a chat's transcript.

let clockNow = Date.now();
const clockSubs = new Set<() => void>();
let clockTimer: ReturnType<typeof setInterval> | null = null;

function clockSubscribe(cb: () => void): () => void {
  if (!clockSubs.size) {
    clockNow = Date.now();
    clockTimer = setInterval(() => {
      clockNow = Date.now();
      for (const f of [...clockSubs]) f();
    }, 1000);
  }
  clockSubs.add(cb);
  return () => {
    clockSubs.delete(cb);
    if (!clockSubs.size && clockTimer !== null) {
      clearInterval(clockTimer);
      clockTimer = null;
    }
  };
}
const noSubscribe = () => () => {};
const clockSnapshot = () => clockNow;

/** For the suites: how many drawn lines tick, and whether the interval runs. */
export const thinkingClock = {
  subscribers: () => clockSubs.size,
  running: () => clockTimer !== null,
};

function Elapsed({ since, live }: { since: number; live: boolean }) {
  const now = useSyncExternalStore(live ? clockSubscribe : noSubscribe, clockSnapshot);
  return (
    <span className="tk-time" data-testid="thinking-time">
      {elapsed(Math.max(now, since) - since)}
    </span>
  );
}

// ---- the orb ----------------------------------------------------------------

const C = 12;
const pt = (r: number, a: number) => `${(C + Math.cos(a) * r).toFixed(2)} ${(C + Math.sin(a) * r).toFixed(2)}`;
/** The outer ring: 24 ticks. */
const TICKS = Array.from({ length: 24 }, (_, i) => {
  const a = (i / 24) * Math.PI * 2;
  return `M${pt(9.4, a)}L${pt(11.4, a)}`;
}).join("");
/** The inner ring: two arcs, as the big orb's innermost (0–2.4 and 2.9–5.6 rad). */
const ARCS = ([[0, 2.4], [2.9, 5.6]] as const)
  .map(([a1, a2]) => `M${pt(7.2, a1)}A7.2 7.2 0 ${a2 - a1 > Math.PI ? 1 : 0} 1 ${pt(7.2, a2)}`)
  .join("");

export const MiniOrb = memo(function MiniOrb(props: { orb: OrbState; accent?: string | null; moving: boolean }) {
  const p = palette(props.orb, props.accent);
  // The big orb's spin, sped up for 18px: thinking and a tool turn about once
  // every two seconds, an approval and an error slowly.
  const spin = Math.max(0.2, p.spin);
  const style = {
    "--mo-hue": p.hue,
    "--mo-outer": `${(6 / spin).toFixed(2)}s`,
    "--mo-inner": `${(4 / spin).toFixed(2)}s`,
  } as CSSProperties;
  return (
    <svg
      className={"miniorb" + (props.moving ? "" : " still")}
      data-testid="miniorb"
      data-orb={props.orb}
      viewBox="0 0 24 24"
      width="18"
      height="18"
      aria-hidden="true"
      focusable="false"
      style={style}
    >
      <path className="mo-outer" d={TICKS} />
      <path className="mo-inner" d={ARCS} />
      <circle className="mo-core" cx={C} cy={C} r="3.6" />
    </svg>
  );
});

// ---- the line ---------------------------------------------------------------

const TONE: Partial<Record<OrbState, string>> = { tool: "amber", approval: "amber", error: "red" };

/**
 * The line at a chat's foot: mini orb · word · elapsed time. `drawn`: the pane
 * is on screen, so its time ticks (a hidden pane's does not keep the clock
 * running).
 */
export const ThinkingRow = memo(function ThinkingRow(props: {
  line: ThinkingLine;
  accent?: string | null;
  drawn: boolean;
}) {
  const { line } = props;
  return (
    <div
      className={`thinking ${TONE[line.orb] ?? "accent"}`}
      data-testid="thinking"
      data-word={line.word}
      data-orb={line.orb}
    >
      <MiniOrb orb={line.orb} accent={props.accent} moving={line.moving} />
      <span className="tk-word" role="status" data-testid="thinking-word">{line.text}</span>
      {line.since !== null ? <Elapsed since={line.since} live={props.drawn && line.moving} /> : null}
    </div>
  );
});
