// Spoken playback commands (voice rework V9, decisions V-6, Q8 and Q10).
//
// "Jarvis pause", "Jarvis resume", "Jarvis skip" and "Jarvis stop" -- and only
// those. The words are matched against the *whole* transcript of a claimed
// utterance, the way the plan's section 3.6 puts it: "Jarvis, stop the build" is
// a message to an agent, not a command; "pause the build" is not either. Fillers
// ("uh", "hey", "please") may surround a command, and nothing else may.
//
// **What a command can do is exactly four calls on Jarvis's own voice**
// (`CommandEffects`): pause, resume, skip, stop. "Jarvis stop" stops speech and
// clears the queue; it never interrupts an agent's turn (Q8). A command never
// reaches an approval: this file imports nothing, holds no handle on `decide`,
// `/approve` or any route, and `approvalIsolation.test.ts` pins that. "Jarvis
// approve", "Jarvis yes", "Jarvis allow" are not commands at all.
//
// **Under an authorization card commands are OFF for a transcript** (Q10): with a
// card up, the microphone is suppressed and nothing is uploaded (Capture), so no
// transcript can arrive for one -- and one already in flight when the card
// appeared is not obeyed either. Q10 allows playback commands under a card by
// *local* recognition (nothing uploaded, no approval effect); that recognizer is
// V11. `CommandRouter.setLocalUnderCard` is its seam, and V9 never turns it on.

export type SpeechCommand = "pause" | "resume" | "skip" | "stop";

/** The only things a spoken command may do. */
export interface CommandEffects {
  pause(): void;
  resume(): void;
  skip(): void;
  stop(): void;
}

const COMMANDS: ReadonlySet<string> = new Set<SpeechCommand>(["pause", "resume", "skip", "stop"]);

/** Words that may stand around a command without making it something else. */
const FILLERS: ReadonlySet<string> = new Set(["uh", "um", "er", "erm", "hmm", "ah", "hey", "ok", "okay", "so", "well", "please", "now"]);

function words(text: string): string[] {
  return text
    .normalize("NFKC")
    .toLowerCase()
    // Punctuation and the marks a transcriber adds are not words; apostrophes are kept inside a word.
    .replace(/[^\p{L}\p{N}']+/gu, " ")
    .split(" ")
    .filter(Boolean);
}

/**
 * The command a transcript is, or null. The wake word must be spoken ("jarvis"):
 * a bare "pause" is a message, and so is anything with another word in it.
 */
export function parseSpeechCommand(text: string): SpeechCommand | null {
  if (typeof text !== "string" || text.length > 200) return null;
  const w = words(text);
  const at = w.indexOf("jarvis");
  if (at < 0) return null;
  const before = w.slice(0, at);
  const after = w.slice(at + 1);
  if (!before.every((x) => FILLERS.has(x))) return null;
  // After his name: fillers, exactly one command word, fillers.
  const content = after.filter((x) => !FILLERS.has(x));
  if (content.length !== 1 || !COMMANDS.has(content[0])) return null;
  // Fillers are only allowed to *surround* it: "pause so" is fine, but a command word that was itself
  // listed as a filler cannot be (none is), so no further check is needed.
  return content[0] as SpeechCommand;
}

/** Why a command-shaped transcript was not obeyed. */
export type CommandRefusal = "card" | "stale";

export interface CommandContext {
  /** An authorization card is waiting. */
  card: boolean;
  /** The words arrived late or from the owner's Retry: whatever he wanted then, it is not now. */
  stale: boolean;
}

export type CommandResult =
  | { kind: "none" }                                // not a command: an ordinary message
  | { kind: "ran"; command: SpeechCommand }
  | { kind: "refused"; command: SpeechCommand; why: CommandRefusal };

/** Where recognized commands become calls on his voice. */
export class CommandRouter {
  private localUnderCard = false;

  constructor(private readonly effects: CommandEffects) {}

  /**
   * V11's seam (Q10): whether a command from the *local* recognizer is obeyed under a card. V9 has
   * no local recognizer and never sets this; a transcript is refused under a card whatever it is.
   */
  setLocalUnderCard(on: boolean): void {
    this.localUnderCard = on;
  }

  /** A transcript: run it if it is, as a whole, a command. */
  fromTranscript(text: string, ctx: CommandContext): CommandResult {
    const command = parseSpeechCommand(text);
    if (!command) return { kind: "none" };
    if (ctx.card) return { kind: "refused", command, why: "card" };
    if (ctx.stale) return { kind: "refused", command, why: "stale" };
    this.run(command);
    return { kind: "ran", command };
  }

  /** A command recognized on this machine, nothing uploaded (V11). Under a card only when V11 has said it may. */
  fromLocal(command: SpeechCommand, ctx: { card: boolean }): CommandResult {
    if (!COMMANDS.has(command)) return { kind: "none" };
    if (ctx.card && !this.localUnderCard) return { kind: "refused", command, why: "card" };
    this.run(command);
    return { kind: "ran", command };
  }

  private run(command: SpeechCommand): void {
    switch (command) {
      case "pause": this.effects.pause(); break;
      case "resume": this.effects.resume(); break;
      case "skip": this.effects.skip(); break;
      case "stop": this.effects.stop(); break;
    }
  }
}
