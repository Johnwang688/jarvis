// C5 / O-8 of the chat-to-chat plan (§5.2 "Who leaves relayed turns alone",
// §7): HUD speech skips every record of a turn another chat's message started,
// and of the reply turn that reports its answer, as it skips a turn the CLI
// started by itself (SV-6, `self_started`). The daemon marks each such record
// `relayed: {message_id, from}` (a reply turn's adds `reply: true`) at the top
// level of the bus record, beside `kind` and `data` (daemon `_record`).
//
// The implementer: `SpeechController.event` returns before speaking for any
// record whose `relayed` is an object, exactly where it checks `self_started`.
// The owner's own turn in the same thread is still read.

import { beforeEach, describe, expect, it } from "vitest";
import { SpeechController } from "./controller";
import { FakeClock, FakeGate, FakePlayer, FakeSay } from "./testkit";

let clock: FakeClock;
let player: FakePlayer;
let say: FakeSay;
let c: SpeechController;

const T = "thread-1";
const REPLY = "Right, here is what I found. The build is green and every one of the forty tests passes now.";
const RELAYED = { message_id: "m0123456789ab", from: "aaaa1111" };
const REPLY_MARK = { message_id: "m0123456789ab", from: "bbbb2222", reply: true };

beforeEach(() => {
  clock = new FakeClock();
  player = new FakePlayer(clock);
  say = new FakeSay(clock);
  c = new SpeechController({ say: say.fn, player, clock, gate: new FakeGate() });
  c.setSpeakers(new Set([T]));
});

const turn = (mark: Record<string, unknown>, text = REPLY) => {
  const ev = (kind: string, data: Record<string, unknown> = {}) => c.event({ kind, thread_id: T, data, relayed: mark } as never);
  ev("relay_in", { message_id: RELAYED.message_id, from: RELAYED.from, title: "Desk chat", provider: "fast", text: "check the build" });
  ev("turn_started");
  for (let i = 0; i < text.length; i += 9) ev("text_delta", { text: text.slice(i, i + 9) });
  ev("text", { text });
  ev("turn_finished", { stop: "end" });
};

describe("a relayed turn is never read aloud (O-8)", () => {
  it("its words, deltas and end say nothing", async () => {
    turn(RELAYED);
    await clock.advance(60_000);
    expect(player.started).toEqual([]);
    expect(say.calls).toEqual([]);
  });

  it("nor does the reply turn that reports another chat's answer", async () => {
    turn(REPLY_MARK, "The frontend chat says the build is green. Nothing else is needed from you today.");
    await clock.advance(60_000);
    expect(player.started).toEqual([]);
    expect(say.calls).toEqual([]);
  });

  it("the owner's own reply in the same thread is still read, whole", async () => {
    turn(RELAYED, "A relayed answer that must stay silent, however long it runs on.");
    for (let i = 0; i < REPLY.length; i += 9) c.event({ kind: "text_delta", thread_id: T, data: { text: REPLY.slice(i, i + 9) } });
    c.event({ kind: "text", thread_id: T, data: { text: REPLY } });
    c.event({ kind: "turn_finished", thread_id: T, data: { stop: "end" } });
    await clock.advance(60_000);
    expect(player.started.join(" ")).toBe(REPLY);
  });
});
