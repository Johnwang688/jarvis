// Relative times for the phone (UX-2, the owner's 2026-11-15 decision): "asked 3 min ago",
// "expires in 9 min", never "asked 00:46:13". Phone only: the desk HUD keeps its clock
// (lib/inbox.ts clockOf), which this file reads but never changes.
//
// The words come from code and the daemon's timestamps alone. A time the phone cannot read says
// nothing (an empty string), never "NaN min ago". The absolute time stays one look away: every
// label carries it in its title, and a tap on a row's label swaps it in place.
//
// Updating: a label is a span with its own timestamp (data-rt-at, data-rt-kind); `refreshTimes`
// rewrites those spans' text only. The App calls it from one coarse timer (a minute), so a list
// is never redrawn for the sake of a clock.
import { clockOf } from "../lib/inbox";

const SEC = 1000;
const MIN = 60 * SEC;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;
/** Under this, a time in the past (or a little in the future: clock skew) reads "just now". */
export const JUST_NOW_MS = 45 * SEC;

/** A timestamp (ISO string or epoch ms) as epoch ms, or null if it says nothing the phone can read. */
export function atMs(at: string | number | null | undefined): number | null {
  const ms = typeof at === "number" ? at : typeof at === "string" && at ? Date.parse(at) : NaN;
  return Number.isFinite(ms) ? ms : null;
}

/** How long ago: "just now", "3 min ago", "2 h ago", "4 d ago"; "" for a missing or unreadable time.
 * A time at or after `now` (the daemon's clock a little ahead of the phone's) is "just now", never
 * a negative number. */
export function agoWords(at: string | number | null | undefined, now: number = Date.now()): string {
  const ms = atMs(at);
  if (ms === null) return "";
  const d = now - ms;
  if (d < JUST_NOW_MS) return "just now";
  if (d < HOUR) return `${Math.max(1, Math.floor(d / MIN))} min ago`;
  if (d < DAY) return `${Math.floor(d / HOUR)} h ago`;
  return `${Math.floor(d / DAY)} d ago`;
}

/** How long to go: "in 9 min", "in 2 h", "in 3 d", "in under a minute"; "now" once it has passed;
 * "" for a missing or unreadable time. Rounded up, so a countdown never says 0 while time is left. */
export function inWords(at: string | number | null | undefined, now: number = Date.now()): string {
  const ms = atMs(at);
  if (ms === null) return "";
  const left = ms - now;
  if (left <= 0) return "now";
  if (left <= MIN) return "in under a minute";
  if (left < HOUR) return `in ${Math.ceil(left / MIN)} min`;
  if (left < DAY) return `in ${Math.ceil(left / HOUR)} h`;
  return `in ${Math.ceil(left / DAY)} d`;
}

/** The absolute time for a title or a tap: "15:04:11", with the date first when it is not today. */
export function absoluteWords(at: string | number | null | undefined, now: number = Date.now()): string {
  const ms = atMs(at);
  if (ms === null) return "";
  const d = new Date(ms);
  const clock = clockOf(ms);
  if (d.toDateString() === new Date(now).toDateString()) return clock;
  return `${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${clock}`;
}

export type RelKind = "ago" | "in";

function words(kind: RelKind, at: number, now: number): string {
  return kind === "in" ? inWords(at, now) : agoWords(at, now);
}

function paint(node: HTMLElement, now: number): void {
  const at = node.dataset.rtAt ? atMs(Number(node.dataset.rtAt)) : null;
  const kind: RelKind = node.dataset.rtKind === "in" ? "in" : "ago";
  if (at === null) return;
  const prefix = node.dataset.rtPrefix ?? "";
  const abs = absoluteWords(at, now);
  node.title = abs;
  node.textContent = node.dataset.rtAbs === "1" ? `${prefix}${abs}` : `${prefix}${words(kind, at, now)}`;
}

/**
 * One label: a span whose text is `prefix + relative words`, its absolute time in its title.
 * Null when the time is missing or unreadable (the caller then shows no label at all).
 * `toggle` lets a tap swap the absolute time in and out (rows only: never inside a button).
 */
export function timeLabel(
  kind: RelKind, at: string | number | null | undefined, prefix = "", now: number = Date.now(), toggle = false,
): HTMLElement | null {
  const ms = atMs(at);
  if (ms === null) return null;
  const node = document.createElement("span");
  node.className = "rt";
  node.dataset.testid = "m-rel";
  node.dataset.rtAt = String(ms);
  node.dataset.rtKind = kind;
  if (prefix) node.dataset.rtPrefix = prefix;
  if (toggle) {
    node.classList.add("rt-toggle");
    node.addEventListener("click", () => {
      node.dataset.rtAbs = node.dataset.rtAbs === "1" ? "0" : "1";
      paint(node, Date.now());
    });
  }
  paint(node, now);
  return node;
}

/** Rewrites the text of every time label under `root` (and nothing else). Returns how many it touched. */
export function refreshTimes(root: ParentNode, now: number = Date.now()): number {
  const nodes = root.querySelectorAll<HTMLElement>("[data-rt-at]");
  nodes.forEach((n) => paint(n, now));
  return nodes.length;
}

/** The deadline card's words: "Denies automatically in 9 min" (code's words, no model text). */
export function denyWords(at: number | null, now: number): string {
  if (at === null) return "Denies automatically when it times out";
  return `Denies automatically ${inWords(at, now)}`;
}
