// Settings -> General: the binary click sparkle (lib/sparkle.ts). A convenience
// kept in this window (localStorage), on by default, and off whatever this says
// while the system asks for reduced motion.

import { useState } from "react";
import { setSparkleEnabled, sparkleEnabled } from "../lib/sparkle";

export function SparkleSetting() {
  const [on, setOn] = useState(sparkleEnabled);
  return (
    <div className="settings-row" data-testid="sparkle">
      <label className="settings-check">
        <input
          type="checkbox"
          data-testid="sparkle-toggle"
          checked={on}
          onChange={(e) => { setSparkleEnabled(e.target.checked); setOn(e.target.checked); }}
        />
        <span>Show a sparkle on button clicks</span>
      </label>
      <p>
        Turns off when your system requests reduced motion.
      </p>
    </div>
  );
}
