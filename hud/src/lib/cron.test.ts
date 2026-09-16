import { describe, expect, it } from "vitest";
import { DEFAULT_WHEN, fromSchedule, isCron, parseTime, toWhen, type WhenForm } from "./cron";

const form = (over: Partial<WhenForm>): WhenForm => ({ ...DEFAULT_WHEN, ...over });

describe("schedule presets → the contract's when", () => {
  it("daily at a time is every day", () => {
    expect(toWhen(form({ preset: "daily", time: "08:00" })).cron).toBe("0 8 * * *");
    expect(toWhen(form({ preset: "daily", time: "17:45" })).cron).toBe("45 17 * * *");
  });

  it("weekdays is 1-5, not a list of five entries", () => {
    expect(toWhen(form({ preset: "weekdays", time: "09:30" })).cron).toBe("30 9 * * 1-5");
  });

  it("weekly names the day in cron's own numbering, Sunday = 0", () => {
    expect(toWhen(form({ preset: "weekly", weekday: 0, time: "10:00" })).cron).toBe("0 10 * * 0");
    expect(toWhen(form({ preset: "weekly", weekday: 6, time: "10:00" })).cron).toBe("0 10 * * 6");
  });

  it("an interval is every_s, never a cron", () => {
    const mins = toWhen(form({ preset: "interval", every: "15", unit: "minutes" }));
    expect(mins).toEqual({ every_s: 900 });
    const hours = toWhen(form({ preset: "interval", every: "2", unit: "hours" }));
    expect(hours).toEqual({ every_s: 7200 });
  });

  it("advanced passes a real cron through and refuses anything else", () => {
    expect(toWhen(form({ preset: "advanced", cron: "*/5 * * * 1" })).cron).toBe("*/5 * * * 1");
    // Four fields is the classic mistake, and a guess at the fifth is the one
    // thing worse than an error: the owner would learn what they created from
    // it firing at the wrong hour.
    expect(toWhen(form({ preset: "advanced", cron: "0 9 * *" })).error).toMatch(/5 fields/);
    expect(toWhen(form({ preset: "advanced", cron: "" })).cron).toBeUndefined();
  });

  it("refuses a bad time rather than inventing midnight", () => {
    for (const bad of ["", "9", "25:00", "08:70", "eight"]) {
      const out = toWhen(form({ preset: "daily", time: bad }));
      expect(out.cron, bad).toBeUndefined();
      expect(out.error, bad).toBeTruthy();
    }
  });

  it("refuses a bad interval rather than scheduling every zero seconds", () => {
    for (const bad of ["", "0", "-3", "2.5", "soon"]) {
      const out = toWhen(form({ preset: "interval", every: bad }));
      expect(out.every_s, bad).toBeUndefined();
      expect(out.error, bad).toBeTruthy();
    }
  });

  it("parses and validates a time", () => {
    expect(parseTime("7:05")).toEqual({ h: 7, m: 5 });
    expect(parseTime("23:59")).toEqual({ h: 23, m: 59 });
    expect(parseTime("24:00")).toBeNull();
  });

  it("knows a cron has five fields", () => {
    expect(isCron("0 9 * * 1")).toBe(true);
    expect(isCron("  0   9 * * 1 ")).toBe(true);
    expect(isCron("0 9 * *")).toBe(false);
    expect(isCron("0 9 * * 1 2")).toBe(false);
  });
});

describe("editing an existing schedule opens on the preset that made it", () => {
  it("round-trips every preset", () => {
    for (const w of [
      form({ preset: "daily", time: "08:00" }),
      form({ preset: "weekdays", time: "09:30" }),
      form({ preset: "weekly", weekday: 3, time: "22:05" }),
    ]) {
      const spec = toWhen(w);
      const back = fromSchedule({ cron: spec.cron, every_s: null });
      expect(back.preset, spec.cron).toBe(w.preset);
      expect(back.time, spec.cron).toBe(w.time);
      if (w.preset === "weekly") expect(back.weekday).toBe(w.weekday);
    }
  });

  it("round-trips an interval, in the unit it was made in", () => {
    expect(fromSchedule({ every_s: 7200 })).toMatchObject({ preset: "interval", every: "2", unit: "hours" });
    expect(fromSchedule({ every_s: 900 })).toMatchObject({ preset: "interval", every: "15", unit: "minutes" });
  });

  it("opens Advanced for a cron no preset could have written", () => {
    const back = fromSchedule({ cron: "*/5 9-17 * * 1" });
    expect(back.preset).toBe("advanced");
    // The raw expression survives: an edit dialog that blanked it would lose
    // the schedule on the first save.
    expect(back.cron).toBe("*/5 9-17 * * 1");
  });

  it("degrades to the default rather than throwing on nothing at all", () => {
    expect(fromSchedule({}).preset).toBe("daily");
    expect(fromSchedule({ cron: null, every_s: null }).preset).toBe("daily");
  });
});
