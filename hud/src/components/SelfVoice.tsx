// Settings -> Voice: how Jarvis's own voice is kept out of the microphone (V9).
// The default is reference gating: the window compares the microphone with what it
// is playing, so the owner can talk over him. The switch is the escape hatch back
// to V6's blanket hold (the mic is deaf for the whole of a reply), for a room where
// the gating misjudges. When the gate cannot run, the hold applies by itself, and
// the line says why.

import { useEffect, useState } from "react";
import type { SelfVoiceGate } from "../lib/selfvoice";

export function SelfVoiceSetting(props: { gate: SelfVoiceGate; onChange: (on: boolean) => void }) {
  const { gate } = props;
  const [st, setSt] = useState(() => gate.status());
  useEffect(() => {
    const timer = setInterval(() => setSt(gate.status()), 250);
    return () => clearInterval(timer);
  }, [gate]);
  const line = !st.supported ? "reference unavailable: the microphone is held while Jarvis speaks"
    : !st.enabled ? "off: the microphone is held while Jarvis speaks"
    : st.broken ? "stopped after an error: the microphone is held while Jarvis speaks"
    : st.lock === "echo" ? `on · his voice reaches the microphone ${Math.round(st.lagMs)} ms late`
    : st.lock === "dry" ? "on · his voice does not reach the microphone (headphones, or good echo cancellation)"
    : "on · learning the room: the microphone is held for the first second or two of a reply";
  return (
    <section className="self-voice" data-testid="self-voice" aria-label="Jarvis's own voice">
      <h3>Jarvis&apos;s own voice</h3>
      <label>
        <input
          type="checkbox"
          data-testid="self-voice-toggle"
          checked={st.enabled}
          onChange={(e) => { props.onChange(e.target.checked); setSt(gate.status()); }}
        />{" "}
        Tell his voice from mine, so I can talk over him
      </label>
      <p data-testid="self-voice-status" data-lock={st.lock} data-runnable={st.runnable ? "true" : "false"}>
        Self-voice gate: {line}
      </p>
      <p>
        Off, the microphone ignores everything while he speaks. Either way only the wake word &quot;Jarvis&quot; or the
        talk key pauses him, and no spoken word ever answers an authorization card.
      </p>
    </section>
  );
}
