import { describe, expect, it } from "vitest";
import { DuckLease, RENEW_MS } from "./duck";
import { FakeClock } from "./testkit";

function setup(opts: { enabled?: boolean; fail?: boolean; hold?: boolean } = {}) {
  const clock = new FakeClock();
  const sent: { on: boolean; keepalive?: boolean }[] = [];
  const pending: (() => void)[] = [];
  const lease = new DuckLease({
    clock,
    send: (on, keepalive) => {
      sent.push({ on, keepalive });
      if (opts.fail) return Promise.reject(new Error("daemon down"));
      if (opts.hold) return new Promise<void>((resolve) => pending.push(resolve));
      return Promise.resolve({});
    },
  }, opts.enabled ?? true);
  const ons = () => sent.filter((s) => s.on).length;
  return { clock, sent, pending, lease, ons };
}

describe("DuckLease", () => {
  it("asks at once when speech turns active, renews while it stays so, and ends it once", async () => {
    const { clock, sent, lease } = setup();
    lease.setSpeaking(true);
    await clock.flush();
    expect(sent).toEqual([{ on: true, keepalive: undefined }]);
    await clock.advance(RENEW_MS * 3);
    expect(sent.filter((s) => s.on).length).toBe(4);
    lease.setSpeaking(false);
    await clock.flush();
    expect(sent.at(-1)).toEqual({ on: false, keepalive: undefined });
    const count = sent.length;
    await clock.advance(RENEW_MS * 5);
    expect(sent.length).toBe(count);              // nothing after the off: no timer left running
    expect(clock.pendingTimers()).toBe(0);
  });

  it("renews faster than the daemon's 3 s lease can run out", () => {
    expect(RENEW_MS).toBeLessThanOrEqual(1000);
  });

  it("sends nothing while speech is idle, and no off for a lease never asked for", async () => {
    const { clock, sent, lease } = setup();
    lease.setSpeaking(false);
    lease.setSpeaking(false);
    await clock.advance(RENEW_MS * 4);
    expect(sent).toEqual([]);
  });

  it("repeated active notifications do not multiply requests", async () => {
    const { clock, sent, lease } = setup();
    for (let i = 0; i < 5; i++) lease.setSpeaking(true);
    await clock.flush();
    expect(sent.length).toBe(1);
    expect(clock.pendingTimers()).toBe(1);
  });

  it("the Settings switch off asks for nothing, and turning it off mid-speech ends the lease", async () => {
    const off = setup({ enabled: false });
    off.lease.setSpeaking(true);
    await off.clock.advance(RENEW_MS * 3);
    expect(off.sent).toEqual([]);
    const { clock, sent, lease } = setup();
    lease.setSpeaking(true);
    await clock.flush();
    lease.setEnabled(false);
    await clock.advance(RENEW_MS * 3);
    expect(sent.map((s) => s.on)).toEqual([true, false]);
    lease.setEnabled(true);                          // still speaking: resumes at once
    await clock.flush();
    expect(sent.at(-1)!.on).toBe(true);
    expect(lease.isEnabled()).toBe(true);
  });

  it("a failing daemon is non-fatal: renewals keep trying and nothing throws", async () => {
    const { clock, sent, lease } = setup({ fail: true });
    lease.setSpeaking(true);
    await clock.advance(RENEW_MS * 3);
    expect(sent.filter((s) => s.on).length).toBe(4);
    lease.setSpeaking(false);
    await clock.flush();
    expect(sent.at(-1)!.on).toBe(false);
  });

  it("never has two requests in flight, and an off cannot overtake an earlier on", async () => {
    const { clock, sent, pending, lease } = setup({ hold: true });
    lease.setSpeaking(true);
    await clock.advance(RENEW_MS * 2);                // renewals wait behind the slow first request
    expect(sent.length).toBe(1);
    lease.setSpeaking(false);
    await clock.flush();
    expect(sent.length).toBe(1);                      // the off waits its turn
    pending.shift()!();
    await clock.flush();
    expect(sent.map((s) => s.on)).toEqual([true, false]);
    pending.shift()!();
    await clock.flush();
    expect(sent.length).toBe(2);
  });

  it("a quick on-then-off collapses to the newest wish", async () => {
    const { clock, sent, pending, lease } = setup({ hold: true });
    lease.setSpeaking(true);
    lease.setSpeaking(false);
    lease.setSpeaking(true);
    lease.setSpeaking(false);
    pending.shift()!();
    await clock.flush();
    expect(sent.map((s) => s.on)).toEqual([true, false]);
  });

  it("release (the window is going away) ends the lease with a keepalive request and stops the timer", async () => {
    const { clock, sent, lease } = setup();
    lease.setSpeaking(true);
    await clock.flush();
    lease.release();
    await clock.flush();
    expect(sent.at(-1)).toEqual({ on: false, keepalive: true });
    expect(clock.pendingTimers()).toBe(0);
    lease.dispose();                                   // idempotent: no second off
    await clock.flush();
    expect(sent.filter((s) => !s.on).length).toBe(1);
  });
});
