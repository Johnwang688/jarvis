// Settings -> General: Appearance. AG-2: the aurora's motion and stirs. AG-3: the accent, the three aurora
// colours and the base, each a colour input whose pick goes through lib/appearance.ts (checkAccent,
// checkWash, checkBase) before it is saved: a refused pick shows its reason and changes nothing, a wash
// that is too bright is darkened and says so. A per-window convenience kept in this window
// (localStorage). The aurora is decoration only: it never moves anything or takes focus.

import { useState } from "react";
import {
  DEFAULT_COLOURS, MOTIONS, checkAccent, checkBase, checkWash, loadAppearance, resolveColours, saveAppearance,
  type Appearance, type Motion,
} from "../lib/appearance";
import { deriveAccent } from "../lib/appearance";

const LABELS: Record<Motion, string> = { fluid: "Fluid", flow: "Flow", still: "Still" };

type Slot = "accent" | "wash-1" | "wash-2" | "wash-3" | "base";
const SLOTS: { slot: Slot; label: string }[] = [
  { slot: "accent", label: "Accent" },
  { slot: "wash-1", label: "Aurora 1" },
  { slot: "wash-2", label: "Aurora 2" },
  { slot: "wash-3", label: "Aurora 3" },
  { slot: "base", label: "Background" },
];
type Note = { kind: "refused" | "clamped"; text: string };

export function AuroraSetting() {
  const [a, setA] = useState<Appearance>(() => loadAppearance());
  const [notes, setNotes] = useState<Partial<Record<Slot, Note>>>({});
  const change = (patch: Partial<Appearance>) => setA(saveAppearance(patch));
  const colours = resolveColours(a);
  const family = deriveAccent(colours.accent).props;

  const note = (slot: Slot, n: Note | null) => setNotes((cur) => {
    const next = { ...cur };
    if (n) next[slot] = n; else delete next[slot];
    return next;
  });
  const pick = (slot: Slot, value: string) => {
    if (slot === "accent") {
      const r = checkAccent(value, colours.washes);
      if (!r.ok) return note(slot, { kind: "refused", text: r.reason });
      note(slot, null);
      return change({ accent: r.value });
    }
    if (slot === "base") {
      const r = checkBase(value);
      if (!r.ok) return note(slot, { kind: "refused", text: r.reason });
      note(slot, r.clamped ? { kind: "clamped", text: r.note ?? "" } : null);
      return change({ base: r.value });
    }
    const i = Number(slot.slice(5)) - 1;
    const r = checkWash(value);
    if (!r.ok) return note(slot, { kind: "refused", text: r.reason });
    const washes = [...colours.washes];
    washes[i] = r.value;
    // A brighter aurora must not make the accent unreadable on it.
    const still = checkAccent(colours.accent, washes);
    if (!still.ok) return note(slot, { kind: "refused", text: `That would make the accent hard to read: ${still.reason}` });
    note(slot, r.clamped ? { kind: "clamped", text: r.note ?? "" } : null);
    return change({ washes });
  };
  const shown = (slot: Slot) => slot === "accent" ? colours.accent : slot === "base" ? colours.base : colours.washes[Number(slot.slice(5)) - 1];
  const reset = () => { setNotes({}); change({ accent: undefined, washes: undefined, base: undefined }); };
  const custom = colours.accent !== DEFAULT_COLOURS.accent || colours.base !== DEFAULT_COLOURS.base
    || colours.washes.some((w, i) => w !== DEFAULT_COLOURS.washes[i]);

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
      <div className="appearance-colours" data-testid="appearance-colours">
        {SLOTS.map(({ slot, label }) => (
          <div key={slot} className="appearance-colour">
            <label>
              {label}{" "}
              <input type="color" data-testid={`appearance-${slot}`} value={shown(slot)} onChange={(e) => pick(slot, e.target.value)} />
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
      <button type="button" data-testid="appearance-reset" disabled={!custom && a.accent === undefined && !a.washes && a.base === undefined} onClick={reset}>
        Reset colours to default
      </button>
    </section>
  );
}
