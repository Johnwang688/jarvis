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
import { GUEST_LABEL } from "../lib/speaker";
import { noPointerFocus } from "../lib/talkkey";

/** Is there something of Jarvis's speech to control? */
export function speechShown(view: SpeechView): boolean {
  return view.enabled && view.speaker && (view.state !== "idle" || view.failed > 0 || (view.blocked && view.queued > 0));
}

/**
 * Does the strip show? Also decides the sidebar's extra room. While the voice
 * check is not judging the microphone (Guest voice, or the check off or blocked) it
 * always does, whatever Jarvis is doing: the label that keeps it from being
 * forgotten is always visible (V-14, M3).
 */
export function stripShown(view: SpeechView, flagged = false): boolean {
  return flagged || speechShown(view);
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
  /** Guest voice is on (V-14): the strip says so, and its button turns it off. */
  guest?: boolean;
  /** What to say about the voice check when it is not judging (`speakerLabel`); shown whenever it is not empty. */
  voiceLabel?: string;
  /** A voice is enrolled, so Guest voice means something: the strip offers the one-click switch. */
  canGuest?: boolean;
  onGuest?: (on: boolean) => void;
}) {
  const { view } = props;
  const guest = props.guest === true;
  const label = props.voiceLabel ?? (guest ? GUEST_LABEL : "");
  if (!stripShown(view, guest || label !== "")) return null;
  const speech = speechShown(view);
  const guestButton = guest || (props.canGuest && speech && props.onGuest) ? (
    <button
      type="button"
      data-testid="guest-toggle"
      aria-pressed={guest}
      aria-label={guest ? "Guest voice is on: turn it off" : "Turn Guest voice on"}
      title={guest ? "Guest voice is ON: anyone can dictate. Click to verify your voice again." : "Guest voice: let anyone dictate"}
      disabled={(props.blocked && !guest) || !props.onGuest}
      onMouseDown={noPointerFocus}
      onClick={() => props.onGuest?.(!guest)}
    >G</button>
  ) : null;
  if (props.compact && !speech) {
    return <div id="speechstrip" className="compact guest" data-testid="speech-strip" data-state="guest" data-guest={guest} data-voice-label={label}>{guestButton}</div>;
  }
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
    return <div id="speechstrip" className="compact" data-testid="speech-strip" data-state={view.state} data-guest={guest}>{toggle}{guestButton}</div>;
  }
  const note = !speech ? "" : view.blocked ? "CLICK ANYWHERE TO ENABLE SOUND"
    : view.state === "idle" ? `${view.failed} SKIPPED${props.label ? ` · ${props.label}` : ""}`
    : `${paused ? "PAUSED" : "SPEAKING"}${props.label ? ` · ${props.label}` : ""}${view.queued ? ` · ${view.queued} QUEUED` : ""}`
      + (view.failed ? ` · ${view.failed} SKIPPED` : "");
  return (
    <div id="speechstrip" data-testid="speech-strip" data-state={speech ? view.state : "guest"} data-guest={guest} role="group" aria-label="Speech controls">
      {label ? <div className="guestnote" data-testid="guest-label" role="status">{label}</div> : null}
      {note ? <div className="note" data-testid="speech-note" title={note}>{note}</div> : null}
      <div className="btns">
        {speech ? toggle : null}
        {speech ? <button type="button" data-testid="speech-skip" aria-label="Skip this message" title="Skip the rest of this message"
          disabled={props.blocked || view.state === "idle"} onMouseDown={noPointerFocus} onClick={props.onSkip}>⏭</button> : null}
        {speech ? <button type="button" data-testid="speech-stop" aria-label="Stop speaking" title="Stop and clear the queue"
          disabled={props.blocked || (view.state === "idle" && !view.failed)}
          onMouseDown={noPointerFocus} onClick={view.state === "idle" ? props.onClearMarks : props.onStop}>⏹</button> : null}
        {guestButton}
      </div>
    </div>
  );
}
