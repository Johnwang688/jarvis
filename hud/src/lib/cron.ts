// When a schedule fires, as a form the owner fills in rather than a cron
// string they compose. The mapping is one-way on purpose: the *form* is the
// truth while the dialog is open, and `toWhen` is the only place that knows
// the 5-field spelling — a second copy of that idea is how two copies drift
// (the lesson `rules.py` keeps re-learning, applied to a much smaller thing).
//
// What the schedule will actually do is never guessed here: the dialog reads
// it back from `POST /schedules/preview`, because the backend owns the
// timezone and the calendar. A plain-English reading computed in the window
// would look exactly as authoritative as the measured one and be wrong at
// every DST boundary.

export type Preset = "daily" | "weekdays" | "weekly" | "interval" | "advanced";

export const PRESETS: { id: Preset; label: string }[] = [
  { id: "daily", label: "Daily" },
  { id: "weekdays", label: "Weekdays" },
  { id: "weekly", label: "Weekly" },
  { id: "interval", label: "Every N" },
  { id: "advanced", label: "Advanced" },
];

/** Cron's own numbering: Sunday is 0. */
export const WEEKDAYS = [
  "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
];

export interface WhenForm {
  preset: Preset;
  /** "HH:MM", 24-hour. */
  time: string;
  weekday: number;
  /** The N of "every N", kept as typed so a half-typed number is not a 0. */
  every: string;
  unit: "minutes" | "hours";
  /** The Advanced tab's raw field. */
  cron: string;
}

export const DEFAULT_WHEN: WhenForm = {
  preset: "daily", time: "08:00", weekday: 1, every: "30", unit: "minutes", cron: "",
};

export interface WhenSpec {
  cron?: string;
  every_s?: number;
  /** Set instead of the other two when the form cannot be turned into one. */
  error?: string;
}

export function parseTime(t: string): { h: number; m: number } | null {
  const m = /^(\d{1,2}):(\d{2})$/.exec((t || "").trim());
  if (!m) return null;
  const h = Number(m[1]);
  const min = Number(m[2]);
  if (h < 0 || h > 23 || min < 0 || min > 59) return null;
  return { h, m: min };
}

export function isCron(s: string): boolean {
  return (s || "").trim().split(/\s+/).filter(Boolean).length === 5;
}

/** The form as the contract's `{cron}` / `{every_s}` — or an error, never a guess. */
export function toWhen(w: WhenForm): WhenSpec {
  if (w.preset === "advanced") {
    const cron = (w.cron || "").trim();
    if (!isCron(cron)) {
      return { error: "A cron expression has 5 fields: minute hour day month weekday." };
    }
    return { cron };
  }
  if (w.preset === "interval") {
    const n = Number((w.every || "").trim());
    if (!Number.isFinite(n) || !Number.isInteger(n) || n < 1) {
      return { error: "Say how many — a whole number of minutes or hours." };
    }
    return { every_s: n * (w.unit === "hours" ? 3600 : 60) };
  }
  const t = parseTime(w.time);
  if (!t) return { error: "Give a time as HH:MM, 24-hour." };
  const dow =
    w.preset === "daily" ? "*" : w.preset === "weekdays" ? "1-5" : String(w.weekday);
  return { cron: `${t.m} ${t.h} * * ${dow}` };
}

/** The inverse, for editing an existing schedule: anything unrecognised opens Advanced. */
export function fromSchedule(s: { cron?: string | null; every_s?: number | null }): WhenForm {
  if (s.every_s) {
    const hours = s.every_s % 3600 === 0;
    return {
      ...DEFAULT_WHEN,
      preset: "interval",
      every: String(hours ? s.every_s / 3600 : Math.round(s.every_s / 60)),
      unit: hours ? "hours" : "minutes",
    };
  }
  const cron = (s.cron || "").trim();
  if (!isCron(cron)) return { ...DEFAULT_WHEN };
  const [min, hour, dom, mon, dow] = cron.split(/\s+/);
  const numeric = /^\d{1,2}$/;
  if (!numeric.test(min) || !numeric.test(hour) || dom !== "*" || mon !== "*") {
    return { ...DEFAULT_WHEN, preset: "advanced", cron };
  }
  const time = `${String(Number(hour)).padStart(2, "0")}:${String(Number(min)).padStart(2, "0")}`;
  if (dow === "*") return { ...DEFAULT_WHEN, preset: "daily", time, cron };
  if (dow === "1-5") return { ...DEFAULT_WHEN, preset: "weekdays", time, cron };
  if (/^[0-6]$/.test(dow)) {
    return { ...DEFAULT_WHEN, preset: "weekly", time, weekday: Number(dow), cron };
  }
  return { ...DEFAULT_WHEN, preset: "advanced", cron };
}
