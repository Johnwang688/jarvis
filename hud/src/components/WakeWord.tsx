// Settings -> Voice: the local wake word (V11). "Jarvis" is heard by a small model running in
// this window; no audio is sent anywhere for it, and Chrome's cloud speech recognizer is not
// used. The line says whether it is listening, loading, off (mic muted) or unavailable and why;
// there is no cloud stand-by, so "unavailable" means saying "Jarvis" does nothing until it loads
// (the talk key and the orb still work), and a button tries again.

import { useEffect, useState } from "react";
import { SENSITIVITY, type Sensitivity } from "../lib/wake/keywords";
import type { WakeStage } from "../lib/wake/stage";

export function WakeWordSetting(props: {
  stage: WakeStage;
  muted: boolean;
  sensitivity: Sensitivity;
  onSensitivity: (s: Sensitivity) => void;
  cardCommands: boolean;
  onCardCommands: (on: boolean) => void;
}) {
  const { stage, muted } = props;
  const [st, setSt] = useState(() => stage.status());
  useEffect(() => {
    const timer = setInterval(() => setSt(stage.status()), 500);
    return () => clearInterval(timer);
  }, [stage]);
  const cpu = st.cpuPct > 0 ? ` · about ${st.cpuPct.toFixed(1)}% of one core` : "";
  const engine = st.state === "failed"
    ? `unavailable — ${st.note}. Saying "Jarvis" does nothing until this loads; the talk key and the orb still work`
    : muted ? "off while the microphone is muted"
    : st.state === "ready" ? `on · listening for "Jarvis" in this window, nothing sent anywhere${cpu}`
    : st.state === "loading" ? "loading the model (the talk key works meanwhile)"
    : "not started";
  return (
    <section className="wake-word" data-testid="wake-word" aria-label="Wake word">
      <h3>Wake word</h3>
      <p data-testid="wake-status" data-state={muted && st.state !== "failed" ? "off" : st.state} data-code={st.code ?? ""}>
        Local wake word: {engine}
        {st.state === "failed" ? <> <button type="button" data-testid="wake-retry" onClick={() => { stage.retry(); setSt(stage.status()); }}>Try the wake word again</button></> : null}
      </p>
      <label>
        Sensitivity{" "}
        <select data-testid="wake-sensitivity" value={props.sensitivity}
          onChange={(e) => { props.onSensitivity(e.target.value as Sensitivity); setSt(stage.status()); }}>
          {(Object.keys(SENSITIVITY) as Sensitivity[]).map((k) => <option key={k} value={k}>{SENSITIVITY[k].label}</option>)}
        </select>
      </label>
      <p data-testid="wake-sensitivity-note">{SENSITIVITY[props.sensitivity].note} (synthetic speech; not yet measured on your voice or room).</p>
      <label>
        <input type="checkbox" data-testid="wake-card-commands" checked={props.cardCommands}
          onChange={(e) => props.onCardCommands(e.target.checked)} />{" "}
        While an authorization card is up, hear &quot;Jarvis pause / resume / skip / stop&quot; for his voice
      </label>
      <p>
        Those four only pause, resume, skip or clear his speech, recognized here and nothing uploaded. No spoken word ever
        answers an authorization card, and nothing is sent to be transcribed while one is up.
      </p>
      <p>
        &quot;Jarvis&quot; wakes every avatar by voice. An avatar&apos;s other wake phrases are not listened for any more: they rode on
        Chrome&apos;s cloud recognizer, which streamed the room to Google, and that is retired.
      </p>
    </section>
  );
}
