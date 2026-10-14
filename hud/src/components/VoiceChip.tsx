// A chat pane's voice chip (V7, decision V-9 and open question 9): which voice
// this chat reads in, a pulse while it is the one speaking, and -- opened --
// the voice to read in, "skip" (the rest of what it is saying) and "mute".
//
// It sits in the pane's header strip (`paneextras`), and opens inline rather
// than as a floating menu, so the strip's wrapping, the HUD's zoom and the
// pane's clipping can never put it out of reach. Output only: nothing here
// reaches an approval (decision V-6); under a card it is disabled like the
// strip's buttons are, so a press behind the card cannot land.

import { useState } from "react";
import { voiceLabel, type ThreadVoice } from "../lib/speech/voices";
import { noPointerFocus } from "../lib/talkkey";
import type { PaneNo } from "../lib/workspace";

export function VoiceChip(props: {
  pane: PaneNo;
  info: ThreadVoice;
  /** The voices the daemon hands out, for the picker. */
  pool: readonly string[];
  /** This chat is the one being read right now. */
  speaking: boolean;
  /** This chat has speech queued or playing (skip has something to act on). */
  active: boolean;
  /** An authorization card is up. */
  blocked?: boolean;
  onChoose: (voice: string) => void;
  onSkip: () => void;
  onMute: (muted: boolean) => void;
}) {
  const { pane, info } = props;
  const [open, setOpen] = useState(false);
  const label = voiceLabel(info.voice);
  const voices = info.voice && !props.pool.includes(info.voice) ? [info.voice, ...props.pool] : props.pool;
  const id = `pane-${pane}-voice`;
  return (
    <span className="voicechip" data-testid={id} data-speaking={props.speaking ? "true" : undefined}
          data-muted={info.muted ? "true" : undefined} data-voice={info.voice ?? ""}>
      <button
        type="button"
        className="voicebtn"
        data-testid={`${id}-button`}
        aria-expanded={open}
        aria-label={`Voice of this chat: ${label}${info.muted ? ", muted" : ""}`}
        title={info.muted ? "This chat is muted. Click for its voice" : "This chat's voice. Click to change it, skip or mute"}
        disabled={props.blocked}
        onMouseDown={noPointerFocus}
        onClick={() => setOpen((o) => !o)}
      >
        <svg width="11" height="11" viewBox="0 0 12 12" aria-hidden="true" focusable="false">
          <path d="M1 4.2h2.3L6 2v8L3.3 7.8H1z" fill="currentColor" />
          {info.muted
            ? <path d="M8 4l3 4M11 4L8 8" fill="none" stroke="currentColor" strokeWidth="1.1" strokeLinecap="round" />
            : <path className="wave" d="M8.2 4.2a2.6 2.6 0 0 1 0 3.6M9.6 2.9a4.4 4.4 0 0 1 0 6.2" fill="none" stroke="currentColor" strokeWidth="1.1" strokeLinecap="round" />}
        </svg>
        <span className="vname">{label}</span>
      </button>
      {open ? (
        <>
          <select
            data-testid={`${id}-select`}
            aria-label="Voice for this chat"
            value={info.voice ?? ""}
            disabled={props.blocked}
            onChange={(e) => props.onChoose(e.target.value)}
            onKeyDown={(e) => e.stopPropagation()}
            onKeyUp={(e) => e.stopPropagation()}
          >
            <option value="">Automatic</option>
            {voices.map((v) => <option key={v} value={v}>{voiceLabel(v)}</option>)}
          </select>
          <button type="button" data-testid={`${id}-skip`} disabled={props.blocked || !props.active}
                  title="Skip the rest of what this chat is saying" onMouseDown={noPointerFocus} onClick={props.onSkip}>
            Skip
          </button>
          <button type="button" data-testid={`${id}-mute`} aria-pressed={info.muted} disabled={props.blocked}
                  title={info.muted ? "Read this chat aloud again" : "Stop reading this chat aloud"}
                  onMouseDown={noPointerFocus} onClick={() => props.onMute(!info.muted)}>
            {info.muted ? "Unmute" : "Mute"}
          </button>
        </>
      ) : null}
    </span>
  );
}
