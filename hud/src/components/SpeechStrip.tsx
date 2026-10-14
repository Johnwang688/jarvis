// Jarvis's voice, controllable (V6, open question 9): a strip above the orb with
// pause/resume, skip and stop, and a line saying whose chat is being read and
// how many chunks wait. Shown only while there is something to control (speech
// playing, paused, queued, or a chunk that failed), so an idle window is exactly
// as it was. Folded into the rail with the orb, it is one pause button.
//
// Output only: nothing here reaches an approval (decision V-6). Under a card the
// buttons are disabled like the mode buttons are, so a press behind the card
// cannot land.

import type { SpeechView } from "../lib/speech/controller";
import { noPointerFocus } from "../lib/talkkey";

/** Does the strip show for this view? Also decides the sidebar's extra room. */
export function stripShown(view: SpeechView): boolean {
  return view.enabled && view.speaker && (view.state !== "idle" || view.failed > 0 || (view.blocked && view.queued > 0));
}

export function SpeechStrip(props: {
  view: SpeechView;
  /** The chat being read. */
  label: string;
  /** Folded into the rail: one button. */
  compact?: boolean;
  /** An authorization card is up. */
  blocked?: boolean;
  onToggle: () => void;
  onSkip: () => void;
  onStop: () => void;
  onClearMarks: () => void;
}) {
  const { view } = props;
  if (!stripShown(view)) return null;
  const paused = view.state === "paused";
  const toggle = (
    <button
      type="button"
      data-testid="speech-toggle"
      aria-label={paused ? "Resume speech" : "Pause speech"}
      title={paused ? "Resume" : "Pause"}
      disabled={props.blocked || view.state === "idle"}
      onMouseDown={noPointerFocus}
      onClick={props.onToggle}
    >
      {paused ? "▶" : "⏸"}
    </button>
  );
  if (props.compact) {
    return <div id="speechstrip" className="compact" data-testid="speech-strip" data-state={view.state}>{toggle}</div>;
  }
  const note = view.blocked ? "CLICK ANYWHERE TO ENABLE SOUND"
    : view.state === "idle" ? `${view.failed} SKIPPED${props.label ? ` · ${props.label}` : ""}`
    : `${paused ? "PAUSED" : "SPEAKING"}${props.label ? ` · ${props.label}` : ""}${view.queued ? ` · ${view.queued} QUEUED` : ""}`
      + (view.failed ? ` · ${view.failed} SKIPPED` : "");
  return (
    <div id="speechstrip" data-testid="speech-strip" data-state={view.state} role="group" aria-label="Speech controls">
      <div className="note" data-testid="speech-note" title={note}>{note}</div>
      <div className="btns">
        {toggle}
        <button type="button" data-testid="speech-skip" aria-label="Skip this message" title="Skip the rest of this message"
          disabled={props.blocked || view.state === "idle"} onMouseDown={noPointerFocus} onClick={props.onSkip}>⏭</button>
        <button type="button" data-testid="speech-stop" aria-label="Stop speaking" title="Stop and clear the queue"
          disabled={props.blocked || (view.state === "idle" && !view.failed)}
          onMouseDown={noPointerFocus} onClick={view.state === "idle" ? props.onClearMarks : props.onStop}>⏹</button>
      </div>
    </div>
  );
}
