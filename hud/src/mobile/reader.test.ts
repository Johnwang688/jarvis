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
  it("never has two reads in flight: a trigger meanwhile is one owed re-read", async () => {
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
    // The first answer is stale (a re-read is owed) and is not applied; exactly one more read ran.
    expect(m.started).toBe(2);
    expect(applied).toEqual([]);
    m.release(2, "new");
    await tick();
    expect(applied).toEqual(["new"]);
    expect(m.started).toBe(2);
    expect(r.busy).toBe(false);
  });

  it("reversed completion cannot apply an older read over a newer one", async () => {
    // Two readers' worth of ordering: the second read is released first, then the first. Serial reads
    // cannot overlap, so the second never starts until the first ends, and the answers apply in order.
    const m = manual();
    const applied: string[] = [];
    const r = serialRead(m.fetch, (v) => applied.push(v));
    const done = r.trigger();
    void r.trigger();
    m.release(1, "first");     // the only one that can be released: the second has not started
    await tick();
    m.release(2, "second");
    await done;
    expect(applied).toEqual(["second"]);
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
