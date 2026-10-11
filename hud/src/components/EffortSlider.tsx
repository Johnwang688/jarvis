// The effort slider in the model popover (2026-10-10), in place of the effort
// pills: "Effort <level>" with a (?) at the right, Faster … Smarter, a track
// with a dot per stop, a tick and "Default" under the default stop, and a
// thumb. The rules are lib/effortSlider.ts.
//
// One change, one request: a drag previews locally and commits once on
// release, a click commits once, and arrows/Home/End commit once after the
// keys settle (or at once on blur, or when the popover closes). A commit that
// lands where the thread already is sends nothing. Only the latest commit's
// answer may change what is drawn (a sequence number here, and one per thread
// in ThreadModelControls), so an older answer arriving late never moves the
// thumb back.
//
// The HUD is CSS-zoomed on #root: a pointer's screen offset goes through
// `toCss` before it is measured against the rail's own (unzoomed) width.
// Keys stop here (Tab goes on to the popover's round), so Space on the
// slider is never push-to-talk.

import { useEffect, useRef, useState } from "react";
import { toCss } from "../lib/layout";
import {
  describe, effortAt, HELP, keyStep, SETTLE_MS, sliderModel, snapIndex, stopFraction,
} from "../lib/effortSlider";
import type { Choice, ThreadModels } from "../lib/threadmodel";

/** The HUD's zoom, in percent (lib/layout.ts). */
function zoomLevel(): number {
  const z = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--ui-zoom"));
  return Math.round((z || 1) * 100);
}

const pct = (f: number) => `${(f * 100).toFixed(3)}%`;

export function EffortSlider(props: {
  models: ThreadModels | null;
  choice: Choice;
  disabled?: boolean;
  /** Store this effort (null: the default). May return the request's promise. */
  onCommit: (effort: string | null) => void | Promise<unknown>;
}) {
  const m = sliderModel(props.models, props.choice);
  // A stop being dragged to or keyed to, not yet committed.
  const [preview, setPreview] = useState<string | null>(null);
  // The latest commit, drawn until its own answer settles.
  const [sent, setSent] = useState<{ word: string; effort: string | null; seq: number } | null>(null);
  const sentRef = useRef(sent);
  sentRef.current = sent;
  const seq = useRef(0);
  const pending = useRef<string | null>(null);
  const timer = useRef<number | null>(null);
  const drag = useRef<string | null>(null);
  const rail = useRef<HTMLDivElement>(null);
  const live = useRef({ m, choice: props.choice, onCommit: props.onCommit });
  live.current = { m, choice: props.choice, onCommit: props.onCommit };

  const clearTimer = () => {
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = null;
  };

  const commit = (word: string | null) => {
    clearTimer();
    pending.current = null;
    drag.current = null;
    setPreview(null);
    const { m: now, choice, onCommit } = live.current;
    if (word === null || !now) return;
    const i = now.stops.indexOf(word);
    if (i < 0) return;
    const effort = effortAt(now, i);
    // Against what is drawn as committed: a change still on its way counts.
    const current = sentRef.current !== null ? sentRef.current.effort : choice.effort;
    if (effort === current) return;
    const n = ++seq.current;
    const next = { word, effort, seq: n };
    sentRef.current = next;
    setSent(next);
    Promise.resolve(onCommit(effort))
      .catch(() => undefined)
      .finally(() => {
        if (seq.current !== n) return; // a later commit decides what is drawn
        sentRef.current = null;
        setSent(null);
      });
  };
  const commitRef = useRef(commit);
  commitRef.current = commit;

  // The popover closing (Escape, a click outside, a card arriving) unmounts
  // the slider: a key move still settling is committed then, not dropped.
  useEffect(
    () => () => {
      if (pending.current !== null) commitRef.current(pending.current);
      clearTimer();
    },
    [],
  );

  if (!m) return null;
  const n = m.stops.length;
  const shown = preview ?? sent?.word ?? null;
  const at = shown !== null && m.stops.includes(shown) ? m.stops.indexOf(shown) : null;
  const index = at ?? m.index;
  const words = describe(m, at);

  const stopAt = (clientX: number): string => {
    const el = rail.current;
    if (!el || !el.offsetWidth) return m.stops[index];
    const r = el.getBoundingClientRect();
    return m.stops[snapIndex(toCss(clientX - r.left, zoomLevel()) / el.offsetWidth, n)];
  };

  const stopKeys = (e: React.KeyboardEvent) => e.stopPropagation();

  return (
    <div className="eslider" data-testid="effort-section">
      <div className="es-head">
        <span className="es-title">Effort</span>
        <span className="es-level" data-testid="effort-level">{words.name}</span>
        {words.note ? <span className="es-note" data-testid="effort-note">· {words.note}</span> : null}
        <span className="es-help" role="img" aria-label={HELP} title={HELP} data-testid="effort-help">?</span>
      </div>
      <div className="es-ends" aria-hidden="true">
        <span>Faster</span>
        <span>Smarter</span>
      </div>
      <div
        className="es-track"
        role="slider"
        tabIndex={props.disabled ? -1 : 0}
        aria-label="Effort"
        aria-disabled={props.disabled || undefined}
        aria-valuemin={0}
        aria-valuemax={n - 1}
        aria-valuenow={index}
        aria-valuetext={words.aria}
        data-testid="effort-slider"
        data-stops={m.stops.join(",")}
        data-default={m.defaultIndex}
        onKeyUp={stopKeys}
        onKeyDown={(e) => {
          // Tab is the popover's: it goes round its stops and never leaves it
          // (ModelChip's Popover, which stops it there in turn).
          if (e.key === "Tab") return;
          e.stopPropagation();
          // Space and Enter do nothing here — and never reach push-to-talk.
          if (e.key === " " || e.code === "Space" || e.key === "Enter") {
            e.preventDefault();
            return;
          }
          if (props.disabled) return;
          const next = keyStep(e.key, index, n);
          if (next === null) return;
          e.preventDefault();
          const word = m.stops[next];
          pending.current = word;
          setPreview(word);
          clearTimer();
          timer.current = window.setTimeout(() => commitRef.current(pending.current), SETTLE_MS);
        }}
        onBlur={() => {
          if (pending.current !== null) commit(pending.current);
        }}
        onPointerDown={(e) => {
          if (props.disabled || e.button !== 0) return;
          e.preventDefault(); // no text selection; focus is taken by hand
          (e.currentTarget as HTMLElement).focus();
          // The pointer supersedes a key move still settling.
          clearTimer();
          pending.current = null;
          e.currentTarget.setPointerCapture?.(e.pointerId);
          const word = stopAt(e.clientX);
          drag.current = word;
          setPreview(word);
        }}
        onPointerMove={(e) => {
          if (drag.current === null) return;
          const word = stopAt(e.clientX);
          if (word !== drag.current) {
            drag.current = word;
            setPreview(word);
          }
        }}
        onPointerUp={(e) => {
          if (drag.current === null) return;
          commit(stopAt(e.clientX));
        }}
        onPointerCancel={() => {
          drag.current = null;
          setPreview(null);
        }}
        onLostPointerCapture={() => {
          if (drag.current === null) return;
          drag.current = null;
          setPreview(null);
        }}
      >
        <div className="es-rail" ref={rail}>
          {m.stops.map((w, i) =>
            i === m.defaultIndex ? (
              <span key={w || "default"} className="es-tick" data-testid="effort-stop" data-value={w}
                    data-default="1" style={{ left: pct(stopFraction(i, n)) }} />
            ) : (
              <span key={w} className="es-dot" data-testid="effort-stop" data-value={w}
                    style={{ left: pct(stopFraction(i, n)) }} />
            ),
          )}
          <span className="es-thumb" data-testid="effort-thumb" style={{ left: pct(stopFraction(index, n)) }} />
        </div>
      </div>
      <div className="es-under" aria-hidden="true">
        <span
          className={"es-dlabel" + (n > 1 && m.defaultIndex === 0 ? " first" : n > 1 && m.defaultIndex === n - 1 ? " last" : "")}
          data-testid="effort-default-label"
          style={{ left: pct(stopFraction(m.defaultIndex, n)) }}
        >
          Default
        </span>
      </div>
    </div>
  );
}
