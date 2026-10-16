// Settings -> General: the binary click sparkle (lib/sparkle.ts). A convenience
// kept in this window (localStorage), on by default, and off whatever this says
// while the system asks for reduced motion.

import { useState } from "react";
import { setSparkleEnabled, sparkleEnabled } from "../lib/sparkle";

export function SparkleSetting() {
  const [on, setOn] = useState(sparkleEnabled);
  return (
    <section className="read-aloud" data-testid="sparkle" aria-label="Click sparkle">
      <h3>Click sparkle</h3>
      <label>
        <input
          type="checkbox"
          data-testid="sparkle-toggle"
          checked={on}
          onChange={(e) => { setSparkleEnabled(e.target.checked); setOn(e.target.checked); }}
        />{" "}
        Burst a few 1s and 0s when a button is clicked
      </label>
      <p>
        Decoration only: it never moves anything or takes focus, and it is off while your system asks for reduced motion.
      </p>
    </section>
  );
}
