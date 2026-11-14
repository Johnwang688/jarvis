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
// Custom presets (owner 2026-11-13, lib/presets.ts) join the row after RGB: "Save as preset" names the current
// accent, three washes, background and editor & terminal colour. A custom preset applies like a built-in one and
// also sets the editor & terminal colour; only these have a pencil (rename in place) and a x (delete, with Undo
// for 10 s). The controls are always in the DOM and focusable; CSS only fades them in on hover and focus.
// The list is read with the quick per-colour guards only; the whole-palette proof runs on the one preset
// clicked (checkCustomPreset in applyCustom), which a refusal stops with a note. Nothing here (render,
// pressed state, the storage/event refresh) ever runs the full check over the list.
//
// "Editor & terminal" is its own colour (`codeAccent`), judged exactly like the accent by the same settle and
// commit path. Presets, Aurora and RGB never change it and it stays enabled under RGB; Reset clears it.

import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import {
  APPEARANCE_EVENT, COLOUR_PRESETS, DEFAULT_COLOURS, MOTIONS, checkAccent, checkBase, checkPalette, checkPreset, checkWash, loadAppearance,
  matchingPreset, resolveColours, saveAppearance,
  type Appearance, type ColourPreset, type Motion,
} from "../lib/appearance";
import { deriveAccent } from "../lib/appearance";
import {
  CUSTOM_PRESETS_KEY, PRESETS_EVENT, addCustomPreset, checkCustomPreset, cleanPresetName, deleteCustomPreset, loadCustomPresets,
  renameCustomPreset, restoreCustomPreset, type CustomPreset,
} from "../lib/presets";

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
/** How long Undo stays after deleting a custom preset. */
const UNDO_MS = 10_000;

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

/** The small palette strip a preset button shows (the same markup for a built-in and a custom preset). */
function PresetSwatch({ p }: { p: { base: string; washes: readonly string[]; accent: string } }) {
  return (
    <span className="appearance-preset-swatch" aria-hidden="true" style={{ background: p.base }}>
      {p.washes.map((w, i) => <span key={i} style={{ background: w }} />)}
      <span className="appearance-preset-accent" style={{ background: p.accent }} />
    </span>
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

  // ---- custom presets: the list (re-read on this window's event and on another window's storage event) ----
  const [customs, setCustoms] = useState<CustomPreset[]>(() => loadCustomPresets());
  const [naming, setNaming] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<{ id: string; value: string } | null>(null);
  const renamingRef = useRef<string | null>(null);
  const [customNote, setCustomNote] = useState("");
  const [undo, setUndo] = useState<{ preset: CustomPreset; index: number } | null>(null);
  const undoTimer = useRef<number | undefined>(undefined);
  const refocus = useRef<string | null>(null);
  const sectionRef = useRef<HTMLElement>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  const renameRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const refreshList = (e?: Event) => {
      if (e instanceof StorageEvent && e.key !== null && e.key !== CUSTOM_PRESETS_KEY) return;
      const next = loadCustomPresets();
      setCustoms((cur) => JSON.stringify(cur) === JSON.stringify(next) ? cur : next);
    };
    window.addEventListener(PRESETS_EVENT, refreshList);
    window.addEventListener("storage", refreshList);
    return () => {
      window.removeEventListener(PRESETS_EVENT, refreshList);
      window.removeEventListener("storage", refreshList);
    };
  }, []);
  useEffect(() => () => window.clearTimeout(undoTimer.current), []);
  // A rename whose preset another window removed is over.
  useEffect(() => {
    if (renaming && !customs.some((p) => p.id === renaming.id)) { renamingRef.current = null; setRenaming(null); }
  }, [customs, renaming]);
  useEffect(() => { if (naming !== null) { nameRef.current?.focus(); nameRef.current?.select(); } }, [naming === null]);
  useEffect(() => { if (renaming) { renameRef.current?.focus(); renameRef.current?.select(); } }, [renaming?.id]);
  // After a form closes by key or button, focus goes back to the control that opened it.
  useEffect(() => {
    if (!refocus.current) return;
    sectionRef.current?.querySelector<HTMLElement>(`[data-testid="${refocus.current}"]`)?.focus();
    refocus.current = null;
  });
  // The colours the pickers hold: what is stored, whether or not RGB is on (RGB keeps them for later).
  const colours = resolveColours({ ...a, rgb: false });
  const family = deriveAccent(colours.accent).props;
  // The code accent is shown exactly as it is applied: judged against the washes actually displayed (RGB's own
  // under RGB), not the stored ones the other pickers keep showing.
  const applied = resolveColours(a);
  const codeColour = applied.codeAccent;

  const note = (slot: Slot, n: Note | null) => setNotes((cur) => {
    const next = { ...cur };
    if (n) next[slot] = n; else delete next[slot];
    return next;
  });
  const pick = (slot: Slot, value: string) => {
    if (slot === "code-accent") {
      const r = checkAccent(value, applied.washes);
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
    clearUndo();
    setNotes({});
    change({ accent: undefined, washes: undefined, base: undefined, codeAccent: undefined, rgb: false });
  };
  const usePreset = (p: ColourPreset) => {
    dropPending();
    clearUndo();
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
    clearUndo();
    setNotes({});
    change({ rgb: !a.rgb });
  };
  const current = a.rgb ? undefined : matchingPreset(colours);

  // ---- custom presets: apply, save, rename, delete, undo ----
  const clearUndo = () => {
    window.clearTimeout(undoTimer.current);
    undoTimer.current = undefined;
    setUndo(null);
  };
  const applyCustom = (p: CustomPreset) => {
    dropPending();
    clearUndo();
    const refused = checkCustomPreset(p);
    if (refused) return setCustomNote(refused.reason);
    setNotes({});
    setCustomNote("");
    change({ accent: p.accent, washes: [...p.washes], base: p.base, codeAccent: p.codeAccent, rgb: false });
  };
  const customPressed = (p: CustomPreset) =>
    !a.rgb && p.accent === colours.accent && p.base === colours.base && p.washes.every((w, i) => w === colours.washes[i])
    && p.codeAccent === codeColour;
  const openSave = () => {
    flush(); // a pick still settling is part of the colours being saved
    clearUndo();
    setCustomNote("");
    let n = customs.length + 1;
    const used = new Set(customs.map((p) => p.name.toLowerCase()));
    while (used.has(`custom ${n}`)) n++;
    setNaming(`Custom ${n}`);
  };
  const closeSave = () => { refocus.current = "appearance-save-preset"; setNaming(null); setCustomNote(""); };
  const commitSave = () => {
    if (naming === null) return;
    if (a.rgb) return setCustomNote("Turn RGB off to save your colours as a preset.");
    const r = addCustomPreset(naming, { ...colours, codeAccent: codeColour });
    if (!r.ok) return setCustomNote(r.reason);
    clearUndo();
    setCustoms(r.presets);
    closeSave();
  };
  const startRename = (p: CustomPreset) => {
    clearUndo();
    setCustomNote("");
    renamingRef.current = p.id;
    setRenaming({ id: p.id, value: p.name });
  };
  const endRename = (refocusPencil: boolean) => {
    if (refocusPencil && renamingRef.current) refocus.current = `appearance-custom-rename-${renamingRef.current}`;
    renamingRef.current = null;
    setRenaming(null);
  };
  const commitRename = (id: string, raw: string, viaKey: boolean) => {
    if (renamingRef.current !== id) return;
    const before = customs.find((p) => p.id === id);
    if (before && cleanPresetName(raw) === before.name) return endRename(viaKey);
    const r = renameCustomPreset(id, raw);
    if (!r.ok) {
      setCustomNote(r.reason);
      setCustoms(loadCustomPresets());
      return;
    }
    clearUndo();
    setCustomNote("");
    setCustoms(r.presets);
    endRename(viaKey);
  };
  const onRenameKey = (e: KeyboardEvent<HTMLInputElement>, id: string) => {
    if (e.key === "Escape") { e.preventDefault(); setCustomNote(""); endRename(true); }
    else if (e.key === "Enter" && !e.nativeEvent.isComposing) { e.preventDefault(); commitRename(id, e.currentTarget.value, true); }
  };
  const removeCustom = (p: CustomPreset) => {
    clearUndo();
    const r = deleteCustomPreset(p.id);
    if (!r.ok) {
      setCustomNote(r.reason);
      return setCustoms(loadCustomPresets());
    }
    setCustomNote("");
    setCustoms(r.presets);
    setUndo({ preset: r.preset!, index: r.index! });
    undoTimer.current = window.setTimeout(() => { undoTimer.current = undefined; setUndo(null); }, UNDO_MS);
  };
  const undoDelete = () => {
    if (!undo) return;
    const r = restoreCustomPreset(undo.preset, undo.index);
    clearUndo();
    if (!r.ok) return setCustomNote(r.reason);
    setCustomNote("");
    setCustoms(r.presets);
  };
  const custom = colours.accent !== DEFAULT_COLOURS.accent || colours.base !== DEFAULT_COLOURS.base
    || colours.washes.some((w, i) => w !== DEFAULT_COLOURS.washes[i]) || codeColour !== DEFAULT_COLOURS.codeAccent;

  return (
    <section ref={sectionRef} className="read-aloud" data-testid="appearance" aria-label="Appearance">
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
        {customs.map((p) => (
          <span key={p.id} className="appearance-custom">
            {renaming?.id === p.id ? (
              <span className="appearance-preset appearance-custom-editing">
                <PresetSwatch p={p} />
                <input
                  ref={renameRef}
                  type="text"
                  className="appearance-custom-name"
                  data-testid={`appearance-custom-name-${p.id}`}
                  aria-label={`New name for ${p.name}`}
                  value={renaming.value}
                  onChange={(e) => setRenaming({ id: p.id, value: e.target.value })}
                  onKeyDown={(e) => onRenameKey(e, p.id)}
                  onBlur={(e) => commitRename(p.id, e.currentTarget.value, false)}
                />
              </span>
            ) : (
              <button
                type="button"
                className="appearance-preset"
                data-testid={`appearance-custom-${p.id}`}
                aria-pressed={customPressed(p)}
                title={`${p.name}: accent ${p.accent}, editor and terminal ${p.codeAccent}`}
                onClick={() => applyCustom(p)}
              >
                <PresetSwatch p={p} />
                {p.name}
              </button>
            )}
            <button
              type="button"
              className="appearance-custom-act"
              data-testid={`appearance-custom-rename-${p.id}`}
              aria-label={`Rename ${p.name}`}
              title="Rename"
              onClick={() => startRename(p)}
            >
              <span aria-hidden="true">{"\u270E"}</span>
            </button>
            <button
              type="button"
              className="appearance-custom-act"
              data-testid={`appearance-custom-delete-${p.id}`}
              aria-label={`Delete ${p.name}`}
              title="Delete"
              onClick={() => removeCustom(p)}
            >
              <span aria-hidden="true">{"\u00D7"}</span>
            </button>
          </span>
        ))}
      </div>
      <div className="appearance-custom-bar" data-testid="appearance-custom-bar">
        {naming === null ? (
          <button
            type="button"
            data-testid="appearance-save-preset"
            disabled={a.rgb}
            title={a.rgb ? "Turn RGB off to save your colours as a preset" : "Save the current colours, including the editor and terminal colour, as a preset"}
            onClick={openSave}
          >
            Save as preset
          </button>
        ) : (
          <span className="appearance-custom-form">
            <input
              ref={nameRef}
              type="text"
              data-testid="appearance-preset-name"
              aria-label="Preset name"
              value={naming}
              onChange={(e) => setNaming(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape") { e.preventDefault(); closeSave(); }
                else if (e.key === "Enter" && !e.nativeEvent.isComposing) { e.preventDefault(); commitSave(); }
              }}
            />{" "}
            <button type="button" data-testid="appearance-preset-name-save" disabled={a.rgb} onClick={commitSave}>Save</button>{" "}
            <button type="button" data-testid="appearance-preset-name-cancel" onClick={closeSave}>Cancel</button>
          </span>
        )}
        {undo && (
          <span className="appearance-custom-undo" role="status">
            {" "}Deleted {undo.preset.name}.{" "}
            <button type="button" data-testid="appearance-custom-undo" onClick={undoDelete}>Undo</button>
          </span>
        )}
        {customNote && (
          <p className="appearance-note refused" role="status" data-testid="appearance-custom-note">{customNote}</p>
        )}
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
        <p className="appearance-help">
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
