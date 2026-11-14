// Settings -> General: Appearance. AG-2: the aurora's motion and stirs. AG-3: the accent, the three aurora
// colours and the base, each a colour input whose pick goes through lib/appearance.ts (checkAccent,
// checkWash, checkBase) before it is saved: a refused pick shows its reason and changes nothing, a wash
// that is too bright is darkened and says so. A per-window convenience kept in this window
// (localStorage). The aurora is decoration only: it never moves anything or takes focus.
//
// While a picker is dragged its picks are judged once the input settles (SETTLE_MS): the blend proof
// (checkPalette) takes a few ms on an ordinary palette but can take a few hundred near the band. The
// picker's `change` (released, or a value typed or filled in) judges the last pick at once. Nothing is
// applied before it is judged.
//
// A preset sets all five colours in one click, through checkPreset (the same guards). The first preset is the
// default and clears the stored colours, as Reset does; the pickers stay free to fine-tune any preset.
//
// RGB (the last button, owner 2026-11-13) is a mode, not a preset: it turns the aurora into a slow cycle round
// the whole colour wheel with a white accent (lib/appearance.ts keeps the guards that still apply). It keeps
// the stored colours, so choosing a static preset, Aurora or Reset turns it off and the colours are back. The
// five pickers are disabled while it is on; the speed slider is left slow, right fast.
//
// "Editor & terminal" is its own colour (`codeAccent`), judged exactly like the accent by the same settle and
// commit path. Presets, Aurora and RGB never change it and it stays enabled under RGB; Reset clears it.

import { useEffect, useRef, useState } from "react";
import {
  APPEARANCE_EVENT, COLOUR_PRESETS, DEFAULT_COLOURS, MOTIONS, checkAccent, checkBase, checkPalette, checkPreset, checkWash, loadAppearance,
  matchingPreset, resolveColours, saveAppearance,
  type Appearance, type ColourPreset, type Motion,
} from "../lib/appearance";
import { deriveAccent } from "../lib/appearance";

const LABELS: Record<Motion, string> = { fluid: "Fluid", flow: "Flow", still: "Still" };

type Slot = "accent" | "wash-1" | "wash-2" | "wash-3" | "base" | "code-accent";
const SLOTS: { slot: Slot; label: string }[] = [
  { slot: "accent", label: "Accent" },
  { slot: "wash-1", label: "Aurora 1" },
  { slot: "wash-2", label: "Aurora 2" },
  { slot: "wash-3", label: "Aurora 3" },
  { slot: "base", label: "Background" },
];
const CODE_SLOT: { slot: Slot; label: string } = { slot: "code-accent", label: "Editor & terminal" };
type Note = { kind: "refused" | "clamped"; text: string };
/** How long the picker's input must be still before a pick is judged (its `change` judges at once). */
export const SETTLE_MS = 80;

/** A colour input whose `input` events are passed on as they come and whose native `change` commits. */
function ColourInput(props: { slot: Slot; value: string; disabled?: boolean; onInput: (slot: Slot, value: string) => void; onCommit: (slot: Slot, value: string) => void }) {
  const ref = useRef<HTMLInputElement>(null);
  const commit = useRef(props.onCommit);
  commit.current = props.onCommit;
  const { slot } = props;
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const onChange = () => commit.current(slot, el.value);
    el.addEventListener("change", onChange);
    return () => el.removeEventListener("change", onChange);
  }, [slot]);
  return (
    <input ref={ref} type="color" data-testid={`appearance-${slot}`} value={props.value} disabled={props.disabled} onChange={(e) => props.onInput(slot, e.target.value)} />
  );
}

export function AuroraSetting() {
  const [a, setA] = useState<Appearance>(() => loadAppearance());
  const [notes, setNotes] = useState<Partial<Record<Slot, Note>>>({});
  useEffect(() => {
    const refresh = () => {
      const next = loadAppearance();
      setA((cur) => JSON.stringify(cur) === JSON.stringify(next) ? cur : next);
    };
    window.addEventListener(APPEARANCE_EVENT, refresh);
    window.addEventListener("storage", refresh);
    return () => {
      window.removeEventListener(APPEARANCE_EVENT, refresh);
      window.removeEventListener("storage", refresh);
    };
  }, []);
  const change = (patch: Partial<Appearance>) => setA(saveAppearance(patch));
  // The colours the pickers hold: what is stored, whether or not RGB is on (RGB keeps them for later).
  const colours = resolveColours({ ...a, rgb: false });
  const family = deriveAccent(colours.accent).props;
  const codeColour = colours.codeAccent;

  const note = (slot: Slot, n: Note | null) => setNotes((cur) => {
    const next = { ...cur };
    if (n) next[slot] = n; else delete next[slot];
    return next;
  });
  const pick = (slot: Slot, value: string) => {
    if (slot === "code-accent") {
      const r = checkAccent(value, resolveColours(a).washes);
      if (!r.ok) return note(slot, { kind: "refused", text: r.reason });
      note(slot, null);
      return change({ codeAccent: r.value });
    }
    if (slot === "accent") {
      const r = checkAccent(value, colours.washes);
      if (!r.ok) return note(slot, { kind: "refused", text: r.reason });
      note(slot, null);
      return change({ accent: r.value });
    }
    if (slot === "base") {
      const r = checkBase(value);
      if (!r.ok) return note(slot, { kind: "refused", text: r.reason });
      const mixed = checkPalette(colours.washes, r.value);
      if (mixed) return note(slot, { kind: "refused", text: mixed.reason });
      note(slot, r.clamped ? { kind: "clamped", text: r.note ?? "" } : null);
      return change({ base: r.value });
    }
    const i = Number(slot.slice(5)) - 1;
    const r = checkWash(value);
    if (!r.ok) return note(slot, { kind: "refused", text: r.reason });
    const washes = [...colours.washes];
    washes[i] = r.value;
    const mixed = checkPalette(washes, colours.base);
    if (mixed) return note(slot, { kind: "refused", text: mixed.reason });
    // A brighter aurora must not make the accent unreadable on it.
    const still = checkAccent(colours.accent, washes);
    if (!still.ok) return note(slot, { kind: "refused", text: `That would make the accent hard to read: ${still.reason}` });
    const stillCode = checkAccent(codeColour, washes);
    if (!stillCode.ok) return note(slot, { kind: "refused", text: `That would make the editor and terminal colour hard to read: ${stillCode.reason}` });
    note(slot, r.clamped ? { kind: "clamped", text: r.note ?? "" } : null);
    return change({ washes });
  };
  // The latest pick waits here until the input settles or the picker commits; it is judged by the latest
  // render's `pick` (so against the colours shown then), never applied unjudged.
  const pickRef = useRef(pick);
  pickRef.current = pick;
  const pending = useRef<{ slot: Slot; value: string } | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const flush = () => {
    window.clearTimeout(timer.current);
    timer.current = undefined;
    const p = pending.current;
    pending.current = null;
    if (p) pickRef.current(p.slot, p.value);
  };
  const onInput = (slot: Slot, value: string) => {
    if (pending.current && pending.current.slot !== slot) flush();
    pending.current = { slot, value };
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(flush, SETTLE_MS);
  };
  // A controlled input is reset to the shown value after each input event, so by `change` its own value is
  // stale: the pending pick is the owner's. With nothing pending, a value that differs is judged now.
  const onCommit = (slot: Slot, value: string) => {
    const mine = pending.current?.slot === slot;
    flush();
    if (!mine && value.toLowerCase() !== shown(slot)) pickRef.current(slot, value);
  };
  useEffect(() => () => window.clearTimeout(timer.current), []);
  const shown = (slot: Slot) =>
    slot === "accent" ? colours.accent : slot === "base" ? colours.base : slot === "code-accent" ? codeColour
      : colours.washes[Number(slot.slice(5)) - 1];
  // A click on a preset, RGB or Reset drops any pick still waiting to be judged, so it cannot land after
  // it (verifier LOW).
  const dropPending = () => {
    window.clearTimeout(timer.current);
    timer.current = undefined;
    pending.current = null;
  };
  const reset = () => {
    dropPending();
    setNotes({});
    change({ accent: undefined, washes: undefined, base: undefined, codeAccent: undefined, rgb: false });
  };
  const usePreset = (p: ColourPreset) => {
    dropPending();
    if (p.id === COLOUR_PRESETS[0].id) {
      setNotes({});
      return change({ accent: undefined, washes: undefined, base: undefined, rgb: false });
    }
    const refused = checkPreset(p);
    if (refused) return setNotes({ accent: { kind: "refused", text: refused.reason } });
    setNotes({});
    change({ accent: p.accent, washes: [...p.washes], base: p.base, rgb: false });
  };
  const toggleRgb = () => {
    dropPending();
    setNotes({});
    change({ rgb: !a.rgb });
  };
  const current = a.rgb ? undefined : matchingPreset(colours);
  const custom = colours.accent !== DEFAULT_COLOURS.accent || colours.base !== DEFAULT_COLOURS.base
    || colours.washes.some((w, i) => w !== DEFAULT_COLOURS.washes[i]) || codeColour !== DEFAULT_COLOURS.codeAccent;

  return (
    <section className="read-aloud" data-testid="appearance" aria-label="Appearance">
      <h3>Appearance</h3>
      <label>
        Motion{" "}
        <select
          data-testid="appearance-motion"
          value={a.motion}
          onChange={(e) => change({ motion: e.target.value as Motion })}
        >
          {MOTIONS.map((m) => <option key={m} value={m}>{LABELS[m]}</option>)}
        </select>
      </label>
      <p>
        Fluid drifts slowly and moves a little more when stirred. Flow is the lighter animation. Still draws one frame.
        The system's reduced-motion setting always shows one frame.
      </p>
      <label>
        Natural drift{" "}
        <input
          type="range"
          style={{ width: 220, maxWidth: "60%", verticalAlign: "middle" }}
          min={0}
          max={200}
          step={10}
          data-testid="appearance-drift"
          aria-label="Natural drift"
          value={Math.round(a.drift * 100)}
          disabled={a.motion === "still"}
          onChange={(e) => change({ drift: Number(e.target.value) / 100 })}
        />{" "}
        <output data-testid="appearance-drift-value">{Math.round(a.drift * 100)}%</output>
      </label>
      <p data-testid="appearance-drift-note">
        {a.motion === "still"
          ? "Still draws one frame, so there is no drift to adjust."
          : "How lively the aurora is on its own. 0% holds still (Fluid still reacts to stirs and ripples, then settles)."}
      </p>
      <label>
        <input
          type="checkbox"
          data-testid="appearance-stir"
          checked={a.stir}
          disabled={a.motion !== "fluid"}
          onChange={(e) => change({ stir: e.target.checked })}
        />{" "}
        Stir with the pointer
      </label>
      <br />
      <label>
        <input
          type="checkbox"
          data-testid="appearance-activity"
          checked={a.activity}
          disabled={a.motion !== "fluid"}
          onChange={(e) => change({ activity: e.target.checked })}
        />{" "}
        Ripple while chats work
      </label>
      <p>Stirring is gentle and never takes focus or reaches a Browser page. Only Fluid stirs.</p>
      <h3>Colours</h3>
      <div className="appearance-presets" role="group" aria-label="Colour presets" data-testid="appearance-presets">
        {COLOUR_PRESETS.map((p) => (
          <button
            key={p.id}
            type="button"
            className="appearance-preset"
            data-testid={`appearance-preset-${p.id}`}
            aria-pressed={current?.id === p.id}
            title={`${p.name}: accent ${p.accent}`}
            onClick={() => usePreset(p)}
          >
            <span className="appearance-preset-swatch" aria-hidden="true" style={{ background: p.base }}>
              {p.washes.map((w, i) => <span key={i} style={{ background: w }} />)}
              <span className="appearance-preset-accent" style={{ background: p.accent }} />
            </span>
            {p.name}
          </button>
        ))}
        <button
          type="button"
          className="appearance-preset"
          data-testid="appearance-preset-rgb"
          aria-pressed={a.rgb}
          title="RGB: the aurora cycles through every colour; the accent stays white"
          onClick={toggleRgb}
        >
          <span className="appearance-preset-swatch rgb" aria-hidden="true" />
          RGB
        </button>
      </div>
      <p>A preset sets every colour below at once. Adjust any of them afterwards to make it your own.</p>
      {a.rgb && (
        <p data-testid="appearance-rgb-note">
          RGB is on: the aurora cycles through every colour and the accent stays white. Choosing a preset brings your colours back.
        </p>
      )}
      <label>
        RGB speed{" "}
        <input
          type="range"
          style={{ width: 220, maxWidth: "60%", verticalAlign: "middle" }}
          min={8}
          max={120}
          step={1}
          data-testid="appearance-rgb-speed"
          aria-label="RGB speed"
          value={128 - Math.round(a.rgbSpeed)}
          disabled={!a.rgb || a.motion === "still"}
          onChange={(e) => change({ rgbSpeed: 128 - Number(e.target.value) })}
        />{" "}
        <output data-testid="appearance-rgb-speed-value">{Math.round(a.rgbSpeed)} s per cycle</output>
      </label>
      <div className="appearance-colours" data-testid="appearance-colours">
        {SLOTS.map(({ slot, label }) => (
          <div key={slot} className="appearance-colour">
            <label>
              {label}{" "}
              <ColourInput slot={slot} value={shown(slot)} disabled={a.rgb} onInput={onInput} onCommit={onCommit} />
            </label>{" "}
            <code data-testid={`appearance-${slot}-hex`}>{shown(slot)}</code>
            {notes[slot] && (
              <p className={"appearance-note " + notes[slot]!.kind} role="status" data-testid={`appearance-${slot}-note`}>
                {notes[slot]!.kind === "refused" ? "Not applied: " : ""}{notes[slot]!.text}
              </p>
            )}
          </div>
        ))}
      </div>
      <div className="appearance-preview" data-testid="appearance-preview" aria-hidden="true">
        <span style={{ background: colours.accent }} />
        <span style={{ background: family["--accent-hi"] }} />
        <span style={{ background: colours.base }} />
        {colours.washes.map((w, i) => <span key={i} style={{ background: w }} />)}
      </div>
      <p>
        The accent marks focus, links, the selection bar and the orb; its lighter and quieter relatives follow it.
        Amber and red keep their meanings (a tool running or an approval waiting, an error): a colour close to
        them is not applied. A bright aurora colour is darkened to keep text readable.
      </p>
      <div className="appearance-colour" data-testid="appearance-code-colour">
        <label>
          {CODE_SLOT.label}{" "}
          <ColourInput slot={CODE_SLOT.slot} value={codeColour} onInput={onInput} onCommit={onCommit} />
        </label>{" "}
        <code data-testid="appearance-code-accent-hex">{codeColour}</code>
        {notes["code-accent"] && (
          <p className={"appearance-note " + notes["code-accent"].kind} role="status" data-testid="appearance-code-accent-note">
            {notes["code-accent"].kind === "refused" ? "Not applied: " : ""}{notes["code-accent"].text}
          </p>
        )}
        <p className="appearance-note clamped">
          Colours the editor's function names, cursor and selections and the terminal's cyan. It is separate from the accent and stays put when a preset or RGB changes the rest.
        </p>
      </div>
      <button
        type="button"
        data-testid="appearance-reset"
        disabled={!custom && !a.rgb && a.accent === undefined && !a.washes && a.base === undefined && a.codeAccent === undefined}
        onClick={reset}
      >
        Reset colours to default
      </button>
    </section>
  );
}
