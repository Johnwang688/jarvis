// The phrases the local recognizer listens for, as BPE pieces of the model's own
// tokenizer (voice rework V11, plan §3.6, Q16).
//
// **Wake: "Jarvis" only** (the owner's answer to Q16). The wake list is a single
// phrase; an avatar's other wake phrases do not reach the recognizer (see
// `docs/notes/voice-face.md`, V11, for why and what replaced them).
//
// **Commands: whole phrases, "Jarvis pause|resume|skip|stop"** (V-6, Q10), never a
// bare "pause": V0 measured the bare words at 38 to 69% recall and the whole
// phrases do far better because "Jarvis" anchors them. They are a second keyword
// set, run only while an approval card is up and only if the owner left that on
// (`wakeSettings.ts`): the rest of the time commands come from the transcript (V9).
//
// The pieces below are what sentencepiece's `bpe.model` produced for each phrase
// (upper-cased), and `keywords.test.ts` pins their ids. Nothing here tokenizes at
// run time, so the browser carries no tokenizer.

import type { KeywordSpec } from "./contextGraph";

export const WAKE_PHRASE = "JARVIS";
/** The wake word in text (V9 checks his own sentences for it, so a hit while he says it is not the owner's). */
export const WAKE_NAME = /\bjarvis\b/i;

export const WAKE_PIECES: Readonly<Record<string, readonly string[]>> = {
  JARVIS: ["▁JA", "R", "VI", "S"],
};

export const COMMAND_PIECES: Readonly<Record<string, readonly string[]>> = {
  "JARVIS PAUSE": ["▁JA", "R", "VI", "S", "▁PA", "U", "SE"],
  "JARVIS RESUME": ["▁JA", "R", "VI", "S", "▁RE", "S", "U", "ME"],
  "JARVIS SKIP": ["▁JA", "R", "VI", "S", "▁S", "K", "I", "P"],
  "JARVIS STOP": ["▁JA", "R", "VI", "S", "▁ST", "O", "P"],
};

export type PlaybackCommand = "pause" | "resume" | "skip" | "stop";
export const COMMAND_OF: Readonly<Record<string, PlaybackCommand>> = {
  "JARVIS PAUSE": "pause", "JARVIS RESUME": "resume", "JARVIS SKIP": "skip", "JARVIS STOP": "stop",
};

/** tokens.txt: one "<piece> <id>" per line. */
export function parseTokens(text: string): Map<string, number> {
  const out = new Map<string, number>();
  for (const line of text.split("\n")) {
    const at = line.lastIndexOf(" ");
    if (at <= 0) continue;
    const id = Number(line.slice(at + 1));
    if (Number.isInteger(id)) out.set(line.slice(0, at), id);
  }
  return out;
}

/** Sensitivity: the mean token probability a phrase must reach (V0's grid, boost 1.0). */
export type Sensitivity = "high" | "normal" | "low";
export const SENSITIVITY: Readonly<Record<Sensitivity, { threshold: number; label: string; note: string }>> = {
  high: { threshold: 0.15, label: "High", note: "catches the most; V0 measured 92% of synthetic Jarvis, no false hit in 11.5 min of dictation" },
  normal: { threshold: 0.25, label: "Normal", note: "V0's default: 90% of synthetic Jarvis, no false hit in 11.5 min of dictation" },
  low: { threshold: 0.4, label: "Low", note: "fewest false hits; 79% of synthetic Jarvis" },
};
export const BOOST = 1.0;

export function specsFor(pieces: Readonly<Record<string, readonly string[]>>, tokens: Map<string, number>, threshold: number): KeywordSpec[] {
  return Object.entries(pieces).map(([phrase, ps]) => {
    const ids = ps.map((p) => {
      const id = tokens.get(p);
      if (id === undefined) throw new Error("unknown piece");
      return id;
    });
    return { ids, phrase, boost: BOOST, threshold };
  });
}
