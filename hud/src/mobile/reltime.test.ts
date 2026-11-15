// UX-2: the phone's relative times. Pure words from two timestamps; the DOM label and its refresh.
import { afterEach, describe, expect, it, vi } from "vitest";
import { absoluteWords, agoWords, atMs, denyWords, inWords, JUST_NOW_MS, refreshTimes, timeLabel } from "./reltime";

const NOW = Date.parse("2026-11-15T12:00:00Z");
const S = 1000, M = 60 * S, H = 60 * M, D = 24 * H;
const ago = (ms: number) => agoWords(NOW - ms, NOW);
const inn = (ms: number) => inWords(NOW + ms, NOW);

describe("agoWords: how long ago", () => {
  it("seconds are just now", () => {
    expect(ago(0)).toBe("just now");
    expect(ago(5 * S)).toBe("just now");
    expect(ago(JUST_NOW_MS - 1)).toBe("just now");
  });

  it("minutes, rounded down, from one", () => {
    expect(ago(JUST_NOW_MS)).toBe("1 min ago");
    expect(ago(M)).toBe("1 min ago");
    expect(ago(3 * M + 59 * S)).toBe("3 min ago");
    expect(ago(59 * M + 59 * S)).toBe("59 min ago");
  });

  it("hours", () => {
    expect(ago(H)).toBe("1 h ago");
    expect(ago(5 * H + 30 * M)).toBe("5 h ago");
    expect(ago(D - 1)).toBe("23 h ago");
  });

  it("days", () => {
    expect(ago(D)).toBe("1 d ago");
    expect(ago(3 * D + 5 * H)).toBe("3 d ago");
    expect(ago(40 * D)).toBe("40 d ago");
  });

  it("a missing or unreadable value says nothing", () => {
    for (const bad of [null, undefined, "", "not a date", NaN]) expect(agoWords(bad as never, NOW)).toBe("");
  });

  it("clock skew: a server time at or a little after now is just now, never negative", () => {
    expect(agoWords(NOW + 1, NOW)).toBe("just now");
    expect(agoWords(NOW + 30 * S, NOW)).toBe("just now");
    expect(agoWords(NOW + 10 * M, NOW)).toBe("just now");
    expect(agoWords(NOW + D, NOW)).not.toMatch(/-|NaN/);
  });

  it("takes an ISO string as well as epoch ms", () => {
    expect(agoWords("2026-11-15T11:57:00Z", NOW)).toBe("3 min ago");
    expect(atMs("2026-11-15T11:57:00Z")).toBe(NOW - 3 * M);
  });
});

describe("inWords: the countdown", () => {
  it("minutes, rounded up so it never says 0 while time is left", () => {
    expect(inn(9 * M)).toBe("in 9 min");
    expect(inn(8 * M + 1)).toBe("in 9 min");
    expect(inn(59 * M)).toBe("in 59 min");
  });

  it("under a minute", () => {
    expect(inn(M)).toBe("in under a minute");
    expect(inn(1)).toBe("in under a minute");
  });

  it("hours and days", () => {
    expect(inn(H)).toBe("in 1 h");
    expect(inn(2 * H + 1)).toBe("in 3 h");
    expect(inn(2 * D)).toBe("in 2 d");
  });

  it("the past is now, and a missing value says nothing", () => {
    expect(inn(0)).toBe("now");
    expect(inn(-1)).toBe("now");
    expect(inn(-3 * D)).toBe("now");
    for (const bad of [null, undefined, "", "x"]) expect(inWords(bad as never, NOW)).toBe("");
  });
});

describe("denyWords: the card's deadline line", () => {
  it("is a countdown, not a clock", () => {
    expect(denyWords(NOW + 4 * M, NOW)).toBe("Denies automatically in 4 min");
    expect(denyWords(NOW + 20 * S, NOW)).toBe("Denies automatically in under a minute");
    expect(denyWords(NOW - 1, NOW)).toBe("Denies automatically now");
    expect(denyWords(null, NOW)).toBe("Denies automatically when it times out");
  });
});

describe("absoluteWords: the exact time stays available", () => {
  it("is the clock for today, and the date first for another day", () => {
    expect(absoluteWords(NOW, NOW)).toMatch(/^\d\d:\d\d:\d\d$/);
    expect(absoluteWords(NOW - 3 * D, NOW)).toMatch(/^\S.* \d\d:\d\d:\d\d$/);
    expect(absoluteWords(null, NOW)).toBe("");
  });
});

describe("timeLabel and refreshTimes: a label updates in place", () => {
  afterEach(() => { vi.useRealTimers(); document.body.replaceChildren(); });

  it("a label has the words as its text and the clock time as its title", () => {
    const n = timeLabel("ago", NOW - 3 * M, "asked ", NOW)!;
    expect(n.textContent).toBe("asked 3 min ago");
    expect(n.title).toMatch(/^\d\d:\d\d:\d\d$/);
    expect(n.dataset.testid).toBe("m-rel");
  });

  it("no label for a time it cannot read", () => {
    expect(timeLabel("ago", null)).toBeNull();
    expect(timeLabel("in", "garbage")).toBeNull();
  });

  it("refreshTimes rewrites only the labels, and the words move with the clock", () => {
    const host = document.createElement("div");
    const keep = document.createElement("button");
    keep.textContent = "Allow";
    const a = timeLabel("ago", NOW - M, "asked ", NOW)!;
    const b = timeLabel("in", NOW + 9 * M, "expires ", NOW)!;
    host.append(keep, a, b);
    expect(refreshTimes(host, NOW + 5 * M)).toBe(2);
    expect(a.textContent).toBe("asked 6 min ago");
    expect(b.textContent).toBe("expires in 4 min");
    expect(host.querySelector("button")).toBe(keep);
    expect(keep.textContent).toBe("Allow");
    refreshTimes(host, NOW + 20 * M);
    expect(b.textContent).toBe("expires now");
  });

  it("a tap on a row's label swaps the exact time in and out", () => {
    const n = timeLabel("ago", NOW - 3 * M, "asked ", NOW, true)!;
    document.body.append(n);
    n.click();
    expect(n.textContent).toMatch(/^asked \d\d:\d\d:\d\d$/);
    n.click();
    expect(n.textContent).toMatch(/^asked .* ago$|^asked just now$/);
  });
});
