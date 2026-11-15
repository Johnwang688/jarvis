import { describe, expect, it } from "vitest";
import { serialRead } from "./reader";

/** A fetch whose answers the test releases by hand, in any order. */
function manual() {
  const waiting: { n: number; done: (v: string) => void }[] = [];
  let started = 0;
  return {
    fetch: () => new Promise<string>((res) => { waiting.push({ n: ++started, done: res }); }),
    release: (n: number, v: string) => waiting.find((w) => w.n === n)!.done(v),
    get started() { return started; },
  };
}
const tick = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };

describe("serialRead (PX-2)", () => {
  it("never has two reads in flight: triggers meanwhile are one owed re-read, and each answer is applied in order", async () => {
    const m = manual();
    const applied: string[] = [];
    const r = serialRead(m.fetch, (v) => applied.push(v));
    void r.trigger();
    void r.trigger();
    void r.trigger();
    void r.trigger();
    expect(m.started).toBe(1);
    expect(r.busy).toBe(true);
    m.release(1, "old");
    await tick();
    // An ordinary trigger never drops an answer: the first paint is the first answer, and one more read ran.
    expect(applied).toEqual(["old"]);
    expect(m.started).toBe(2);
    m.release(2, "new");
    await tick();
    expect(applied).toEqual(["old", "new"]);
    expect(m.started).toBe(2);
    expect(r.busy).toBe(false);
  });

  it("a trigger after a LOCAL WRITE drops the answer in flight (it may predate the write), and re-reads once", async () => {
    const m = manual();
    const applied: string[] = [];
    const r = serialRead(m.fetch, (v) => applied.push(v));
    void r.trigger();
    void r.trigger({ afterWrite: true });
    void r.trigger();
    m.release(1, "predates the write");
    await tick();
    expect(applied).toEqual([]);
    expect(m.started).toBe(2);
    m.release(2, "after the write");
    await tick();
    expect(applied).toEqual(["after the write"]);
  });

  it("steady triggers cannot starve it: 20 rounds each trigger once mid-read, and every answer applies", async () => {
    const m = manual();
    const applied: string[] = [];
    const r = serialRead(m.fetch, (v) => applied.push(v));
    void r.trigger();
    for (let round = 1; round <= 20; round++) {
      void r.trigger();                       // a trigger arrives while read `round` is in flight
      m.release(round, "r" + round);
      await tick();
    }
    expect(applied.length).toBeGreaterThanOrEqual(20);
    expect(applied[0]).toBe("r1");            // the launch's first paint did not wait for a second read
  });

  it("launch: two triggers at once (the first read, then the stream's reopen) paint after ONE round trip", async () => {
    const m = manual();
    const applied: string[] = [];
    const r = serialRead(m.fetch, (v) => applied.push(v));
    void r.trigger();
    void r.trigger();
    m.release(1, "first paint");
    await tick();
    expect(applied).toEqual(["first paint"]);
  });

  it("reversed completion cannot apply an older read over a newer one", async () => {
    // Serial reads cannot overlap: the second never starts until the first ends, so answers apply in order.
    const m = manual();
    const applied: string[] = [];
    const r = serialRead(m.fetch, (v) => applied.push(v));
    const done = r.trigger();
    void r.trigger();
    m.release(1, "first");
    await tick();
    m.release(2, "second");
    await done;
    expect(applied).toEqual(["first", "second"]);
  });

  it("an event mid-read gives one re-read, and an answer with nothing owed is applied", async () => {
    const m = manual();
    const applied: string[] = [];
    const r = serialRead(m.fetch, (v) => applied.push(v));
    void r.trigger();
    m.release(1, "a");
    await tick();
    expect(applied).toEqual(["a"]);
    void r.trigger();
    expect(m.started).toBe(2);
    m.release(2, "b");
    await tick();
    expect(applied).toEqual(["a", "b"]);
  });

  it("invalidate drops the answer in flight and any owed read", async () => {
    const m = manual();
    const applied: string[] = [];
    const r = serialRead(m.fetch, (v) => applied.push(v));
    void r.trigger();
    void r.trigger();
    r.invalidate();
    m.release(1, "gone");
    await tick();
    expect(applied).toEqual([]);
    expect(m.started).toBe(1);
    expect(r.busy).toBe(false);
  });

  it("a fetch that throws applies nothing and leaves the reader usable", async () => {
    let calls = 0;
    const applied: string[] = [];
    const r = serialRead(async () => { calls += 1; if (calls === 1) throw new Error("boom"); return "ok"; }, (v) => applied.push(v));
    await r.trigger();
    expect(applied).toEqual([]);
    await r.trigger();
    expect(applied).toEqual(["ok"]);
  });
});
