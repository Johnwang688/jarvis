// Guest voice (V-14) turns speaker verification off and NOTHING else: not the VAD, not noise handling,
// not self-voice gating (Jarvis's own voice still never opens an utterance), not an approval rule.
// The daemon alone reads the switch (for `/stt?verify=1`); this window only shows it. These checks pin that
// from both ends: the gates behave the same with the switch on, and no gate can read it.
import { describe, expect, it } from "vitest";
import { Capture } from "./capture";
import { HANGOVER_MS } from "./vad";
import { placementFor } from "./stt";
import { SpeakerStore } from "./speaker";
import type { DropReason } from "./voiceDiagnostics";

const speech = 0.06, quiet = 0.0009;
/** Every source file under src/, tests excluded, by path. */
const SOURCES = import.meta.glob(["../**/*.ts", "../**/*.tsx", "!../**/*.test.ts", "!../**/*.test.tsx"], {
  query: "?raw", import: "default", eager: true,
}) as Record<string, string>;
const src = (rel: string) => {
  // "../App.tsx" is the window; "capture.ts" is lib/capture.ts; "speech/policy.ts" is lib/speech/policy.ts.
  const tail = rel.startsWith("../") ? rel.slice(3) : `lib/${rel}`;
  const hit = Object.entries(SOURCES).find(([k]) => k === `../${tail}` || k.endsWith(`/${tail}`) || k === `./${tail.replace(/^lib\//, "")}`);
  if (!hit) throw new Error(`no source ${rel}`);
  return hit[1];
};

function run(guest: boolean, reason: "speaking" | "card") {
  // A window whose Guest switch is on (or off) while Jarvis is speaking, or a card is up.
  const store = new SpeakerStore({ status: async () => null, guest: async () => null });
  if (guest) store.apply({ guest: true, enrolled: true, mode: "guest" });
  expect(store.snapshot().guest).toBe(guest);
  const drops: DropReason[] = [];
  let chunks = 0;
  const cap = new Capture({
    suppressed: () => true, suppressionReason: () => reason, muted: () => false,
    onLevel: () => {}, onListening: () => {}, onIdle: () => {}, onDrop: r => drops.push(r),
    onChunk: () => { chunks++; }, onUtteranceEnd: () => {},
  }, () => 10_000);
  cap.feedMs(2000, quiet);
  cap.openFollowUp();
  cap.feedMs(2500, speech);
  cap.feedMs(HANGOVER_MS + 300, quiet);
  return { drops, chunks };
}

describe("Guest voice leaves every other gate alone", () => {
  it("Jarvis's own voice (self-voice gating) and a waiting card still stop an utterance, guest or not", () => {
    for (const reason of ["speaking", "card"] as const) {
      const off = run(false, reason), on = run(true, reason);
      expect(on).toEqual(off);
      expect(on.chunks).toBe(0);
      expect(on.drops).toEqual([`suppressed:${reason}`]);
    }
  });

  it("an utterance's placement does not take the switch: a card still puts words in the box, never sent", () => {
    const landing = { mode: "auto" as const, chat: "c", busy: false, steers: false, card: true };
    expect(placementFor({ mode: "auto", chat: "c" }, landing, { late: false, retried: false })).toBe("review");
    expect(placementFor.length).toBe(3);                    // (captured, landing, item): no fourth, no guest
  });

  it("no gate, recorder, command or approval module can read the switch", () => {
    for (const file of ["capture.ts", "vad.ts", "recorder.ts", "micInput.ts", "micWorklet.ts", "resample.ts", "approval.ts",
      "commands.ts", "speech/controller.ts", "speech/floor.ts", "speech/policy.ts", "stt.ts", "dictation.ts", "talkkey.ts", "browserapproval.ts"]) {
      const text = src(file);
      const body = file === "dictation.ts"
        // dictation.ts holds orbLine, which only DRAWS the label; everything else in it must not know.
        ? text.replace('import { GUEST_LABEL } from "./speaker";', "").split("export function orbLine")[0] + text.split("/** The statuses a turn writes")[1]
        : text;
      expect(body, file).not.toMatch(/guest|SpeakerStore|\/speaker"|speakerView/i);
    }
  });

  it("the window uses the switch for the label and the daemon's call, and nowhere else", () => {
    const uses = src("../App.tsx").split("\n").filter(l => /guest/i.test(l) && !/^\s*\/\//.test(l));
    const allowed = [
      /new SpeakerStore\(/, /speaker\.apply\(data\)/, /guest: speakerView\.guest,$/, /stripShown\(speechView, speakerView\.guest\)/,
      /guest=\{speakerView\.guest\} canGuest=\{speakerView\.enrolled\} onGuest=/,
      /nobody is enrolled or Guest voice is on, and a refusal uploads nothing/,
    ];
    const stray = uses.filter(l => !allowed.some(a => a.test(l)));
    expect(stray).toEqual([]);
    expect(uses.length).toBe(6);
  });

  it("verification is asked of every chunk, never decided here", () => {
    const app = src("../App.tsx");
    expect(app).toContain("api.stt(blob, { ...o, verify: true })");
    const api = src("../api.ts");
    expect(api).toContain('ids.set("verify", "1")');
  });
});
