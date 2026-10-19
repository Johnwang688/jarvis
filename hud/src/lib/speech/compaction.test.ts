// WP-C of the compact-button plan: HUD speech skips every record of a
// compaction turn (the daemon marks each `compaction: true` at the top level of
// the bus record, beside `kind` and `data`), as it skips `relayed` and
// `self_started` ones. The owner's own turn in the same thread is still read.

import { beforeEach, describe, expect, it } from "vitest";
import { SpeechController } from "./controller";
import { FakeClock, FakeGate, FakePlayer, FakeSay } from "./testkit";

let clock: FakeClock;
let player: FakePlayer;
let say: FakeSay;
let c: SpeechController;

const T = "thread-1";
const LINE = "Conversation compacted, and every one of the earlier messages is still here in the log for you.";

beforeEach(() => {
  clock = new FakeClock();
  player = new FakePlayer(clock);
  say = new FakeSay(clock);
  c = new SpeechController({ say: say.fn, player, clock, gate: new FakeGate() });
  c.setSpeakers(new Set([T]));
});

const ev = (kind: string, data: Record<string, unknown> = {}, extra: Record<string, unknown> = {}) =>
  c.event({ kind, thread_id: T, data, ...extra } as never);

describe("a compaction is never read aloud", () => {
  it("its records say nothing", async () => {
    ev("compact_started", {}, { compaction: true });
    ev("turn_started", {}, { compaction: true });
    ev("text", { text: LINE }, { compaction: true });
    ev("turn_finished", { stop: "end", compacted: true }, { compaction: true });
    ev("compacted", { ok: true, text: LINE }, { compaction: true });
    await clock.advance(60_000);
    expect(player.started).toEqual([]);
    expect(say.calls).toEqual([]);
  });

  it("the owner's own turn in the same thread is still read", async () => {
    ev("text", { text: LINE }, { compaction: true });
    ev("text_delta", { text: LINE });
    ev("text", { text: LINE });
    ev("turn_finished", { stop: "end" });
    await clock.advance(60_000);
    expect(player.started.join(" ")).toBe(LINE);
  });
});
