// Settings -> General: Appearance (AG-2), the aurora's motion. The colours are AG-3's. A per-window
// convenience kept in this window (localStorage via lib/appearance.ts), Fluid with both stirs on by default.
// The aurora is decoration only: it never moves anything or takes focus.

import { useState } from "react";
import { MOTIONS, loadAppearance, saveAppearance, type Appearance, type Motion } from "../lib/appearance";

const LABELS: Record<Motion, string> = { fluid: "Fluid", flow: "Flow", still: "Still" };

export function AuroraSetting() {
  const [a, setA] = useState<Appearance>(() => loadAppearance());
  const change = (patch: Partial<Appearance>) => setA(saveAppearance(patch));
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
    </section>
  );
}
