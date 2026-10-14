// Guest voice (V-14) turns speaker verification off and NOTHING else: not the VAD, not noise handling,
// not self-voice gating (Jarvis's own voice still never opens an utterance), not an approval rule.
// The daemon alone reads the switch (for `/stt`); this window only shows it. What is pinned here is the
// structural half: no gate, recorder, command or approval module can read the switch, and every line of
// App.tsx that touches it is on a closed list. The behavioural half (Jarvis speaking and a waiting card
// still stop dictation with Guest voice on) is the Playwright check tests/face/hud_v2_speaker_check.py;
// an earlier unit test here that fed a store into a Capture that never read it proved nothing, and was
// removed (review L5).
import { describe, expect, it } from "vitest";
import { placementFor } from "./stt";

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

describe("Guest voice leaves every other gate alone (structure)", () => {
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
    const uses = src("../App.tsx").split("\n").filter(l => /guest|speaker\.|speakerView|voiceLabel/i.test(l) && !/^\s*\/\//.test(l));
    const allowed = [
      /new SpeakerStore\(/, /useSyncExternalStore\(speaker\.subscribe/, /const voiceLabel = speakerLabel\(speakerView\)/,
      /verified: \(kind\) => \{ o\.verified\(kind\); speaker\.observe\(kind\); \}/, /void speaker\.load\(\)/, /speaker\.apply\(data\)/,
      /speaker: \(\) => speaker\.snapshot\(\),/, /^\s*voiceLabel,$/, /stripShown\(speechView, voiceLabel !== ""\)/,
      /guest=\{speakerView\.guest\} voiceLabel=\{voiceLabel\} canGuest=\{speakerView\.enrolled\} onGuest=/,
    ];
    const stray = uses.filter(l => !allowed.some(a => a.test(l)));
    expect(stray).toEqual([]);
    expect(uses.length).toBe(11);
  });

  it("verification is asked of every chunk, never decided here", () => {
    const app = src("../App.tsx");
    expect(app).toContain("api.stt(blob, { ...o, verify: true, verified:");
    const api = src("../api.ts");
    expect(api).toContain('ids.set("verify", "1")');
  });
});
