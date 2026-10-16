/**
 * B-PICKERS (BR-7/M9): the HUD half of the owner's fallback for native
 * select, date, time and colour popups, which Chrome's screencast never draws.
 * Design: docs/plans/2026-10-16-browser-pickers.md.
 *
 * The daemon offers `{t:"picker"}` only after the owner's own click opened
 * that element's native popup (and has closed it). Everything here is pure:
 * the strict message shapes, the value grammars (the daemon's twin), the
 * listbox's keyboard model, the overlay's placement and the gesture window.
 * Page text arrives untrusted: it is cleaned and capped again here and only
 * ever rendered as React text.
 */
import { cleanTitle } from "./remote";

export const PICKER_KINDS = ["select", "date", "time", "datetime-local", "month", "week", "color"] as const;
export type PickerKind = typeof PICKER_KINDS[number];
export type DateKind = Exclude<PickerKind, "select" | "color">;
export const PICKER_LABEL_MAX = 80;
export const PICKER_OPTIONS_MAX = 500;
export const PICKER_GESTURE_MS = 5000;
export const PICKER_TYPEAHEAD_MS = 1000;
export const PICKER_PAGE = 10;
const INDEX_MAX = 0xffff;
const ID = /^[0-9a-f]{8}$/;

export interface PickerRect { x: number; y: number; width: number; height: number }
export interface PickerItem { i: number; label: string; disabled: boolean; g: number }
export interface SelectPicker {
  id: string; kind: "select"; rect: PickerRect; items: PickerItem[]; groups: string[];
  selected: number; total: number; shown: number;
}
export interface DatePicker {
  id: string; kind: DateKind; rect: PickerRect; value: string; min: string; max: string; step: string; required: boolean;
}
export interface ColorPicker { id: string; kind: "color"; rect: PickerRect; value: string }
export type Picker = SelectPicker | DatePicker | ColorPicker;
export type PickerReason = "applied" | "cancelled" | "changed" | "refused" | "held" | "closed" | "replaced";
const REASONS: readonly PickerReason[] = ["applied", "cancelled", "changed", "refused", "held", "closed", "replaced"];

const GRAMMAR: Record<PickerKind, RegExp | null> = {
  select: null,
  date: /^\d{4,6}-\d{2}-\d{2}$/,
  time: /^\d{2}:\d{2}(:\d{2}(\.\d{1,3})?)?$/,
  "datetime-local": /^\d{4,6}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,3})?)?$/,
  month: /^\d{4,6}-\d{2}$/,
  week: /^\d{4,6}-W\d{2}$/,
  color: /^#[0-9a-f]{6}$/,
};

/** The daemon's `pickers.valid_value`: empty clears a date-like control. */
export function validPickerValue(kind: PickerKind, value: unknown): value is string {
  const grammar = GRAMMAR[kind];
  if (!grammar || typeof value !== "string" || value.length > 64) return false;
  if (value === "") return kind !== "color";
  return grammar.test(value);
}

function object(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}
function exact(v: Record<string, unknown>, keys: string[]): boolean {
  const own = Object.keys(v);
  return own.length === keys.length && keys.every(k => Object.prototype.hasOwnProperty.call(v, k));
}
function finite(n: unknown, lo: number, hi: number): n is number {
  return typeof n === "number" && Number.isFinite(n) && n >= lo && n <= hi;
}
function index(n: unknown): n is number {
  return typeof n === "number" && Number.isInteger(n) && n >= 0 && n <= INDEX_MAX;
}
function rect(value: unknown): PickerRect | null {
  const r = object(value);
  if (!r || !exact(r, ["x", "y", "width", "height"])) return null;
  if (!finite(r.x, -100000, 100000) || !finite(r.y, -100000, 100000)
      || !finite(r.width, 0.01, 100000) || !finite(r.height, 0.01, 100000)) return null;
  return { x: r.x, y: r.y, width: r.width, height: r.height };
}
export function pickerLabel(raw: unknown): string {
  return cleanTitle(raw, PICKER_LABEL_MAX);
}

/** A `{t:"picker"}` message, exactly; anything else is null. */
export function parsePicker(value: unknown): Picker | null {
  const v = object(value);
  if (!v || v.t !== "picker" || typeof v.id !== "string" || !ID.test(v.id)
      || typeof v.kind !== "string" || !(PICKER_KINDS as readonly string[]).includes(v.kind)) return null;
  const r = rect(v.rect);
  if (!r) return null;
  const kind = v.kind as PickerKind;
  if (kind === "select") {
    if (!exact(v, ["t", "id", "kind", "rect", "items", "groups", "selected", "total", "shown"])) return null;
    if (!Array.isArray(v.items) || v.items.length > PICKER_OPTIONS_MAX || !Array.isArray(v.groups)
        || v.groups.length > PICKER_OPTIONS_MAX || !index(v.total) || !index(v.shown)
        || v.shown !== v.items.length || v.shown > v.total
        || !(v.selected === -1 || index(v.selected))) return null;
    const groups = v.groups.map(g => typeof g === "string" ? pickerLabel(g) : null);
    if (groups.some(g => g === null)) return null;
    const items: PickerItem[] = [], seen = new Set<number>();
    for (const raw of v.items) {
      const item = object(raw);
      if (!item || !exact(item, ["i", "label", "disabled", "g"]) || !index(item.i) || item.i >= v.total
          || seen.has(item.i) || typeof item.label !== "string" || typeof item.disabled !== "boolean"
          || typeof item.g !== "number" || !Number.isInteger(item.g) || item.g < -1 || item.g >= groups.length) return null;
      seen.add(item.i);
      items.push({ i: item.i, label: pickerLabel(item.label), disabled: item.disabled, g: item.g });
    }
    if (v.selected !== -1 && !seen.has(v.selected as number)) return null;
    return { id: v.id, kind, rect: r, items, groups: groups as string[], selected: v.selected as number,
      total: v.total, shown: v.shown };
  }
  if (kind === "color") {
    if (!exact(v, ["t", "id", "kind", "rect", "value"]) || !validPickerValue("color", v.value)) return null;
    return { id: v.id, kind, rect: r, value: v.value };
  }
  if (!exact(v, ["t", "id", "kind", "rect", "value", "min", "max", "step", "required"])) return null;
  if (!validPickerValue(kind, v.value) || !(v.min === "" || validPickerValue(kind, v.min))
      || !(v.max === "" || validPickerValue(kind, v.max)) || typeof v.step !== "string"
      || !(v.step === "" || v.step === "any" || /^\d{1,24}(\.\d{1,6})?$/.test(v.step))
      || typeof v.required !== "boolean") return null;
  return { id: v.id, kind, rect: r, value: v.value, min: v.min as string, max: v.max as string,
    step: v.step, required: v.required };
}

export function parsePickerClosed(value: unknown): { id: string; reason: PickerReason } | null {
  const v = object(value);
  if (!v || !exact(v, ["t", "id", "reason"]) || v.t !== "picker_closed" || typeof v.id !== "string"
      || !ID.test(v.id) || !(REASONS as readonly unknown[]).includes(v.reason)) return null;
  return { id: v.id, reason: v.reason as PickerReason };
}

// -- the listbox's keyboard model (positions in `items`, never option indices) -----------------

/** The first position the listbox highlights: the selected option, else the first enabled one. */
export function initialHighlight(p: SelectPicker): number {
  const at = p.items.findIndex(item => item.i === p.selected && !item.disabled);
  return at >= 0 ? at : p.items.findIndex(item => !item.disabled);
}
/** `delta` enabled options away from `from`, clamped (never wrapping), skipping disabled ones. */
export function moveHighlight(items: readonly PickerItem[], from: number, delta: number): number {
  const enabled = items.map((item, at) => item.disabled ? -1 : at).filter(at => at >= 0);
  if (!enabled.length) return -1;
  let here = enabled.indexOf(from);
  if (here < 0) here = delta > 0 ? -1 : enabled.length;
  return enabled[Math.max(0, Math.min(enabled.length - 1, here + delta))];
}
export function edgeHighlight(items: readonly PickerItem[], last: boolean): number {
  return moveHighlight(items, -1, last ? items.length + 1 : 1);
}
/** Type-ahead: the next enabled option (after `from`, wrapping) whose label starts with `prefix`. */
export function typeahead(items: readonly PickerItem[], from: number, prefix: string): number {
  const want = prefix.toLocaleLowerCase();
  if (!want || !items.length) return -1;
  const start = prefix.length > 1 ? Math.max(0, from) : from + 1;
  for (let k = 0; k < items.length; k++) {
    const at = (start + k + items.length) % items.length;
    const item = items[at];
    if (!item.disabled && item.label.toLocaleLowerCase().startsWith(want)) return at;
  }
  return -1;
}

export type PickerKey = "up" | "down" | "home" | "end" | "pageup" | "pagedown" | "enter" | "escape" | "char" | "other";
export function pickerKey(e: { key: string; ctrlKey?: boolean; metaKey?: boolean; altKey?: boolean }): PickerKey {
  if (e.key === "Escape") return "escape";
  if (e.key === "Enter") return "enter";
  if (e.ctrlKey || e.metaKey) return "other";
  switch (e.key) {
    case "ArrowUp": return "up";
    case "ArrowDown": return "down";
    case "Home": return "home";
    case "End": return "end";
    case "PageUp": return "pageup";
    case "PageDown": return "pagedown";
  }
  return !e.altKey && [...e.key].length === 1 && !/[\p{C}]/u.test(e.key) ? "char" : "other";
}

// -- placement ------------------------------------------------------------------------------------

/**
 * Where the overlay goes inside the visible Remote surface (its CSS px):
 * below the control, else above it when there is more room there, clamped
 * on both axes, its height capped by the room it has.
 */
export function pickerPlacement(anchor: PickerRect, size: { width: number; height: number },
  bounds: { width: number; height: number }) {
  const bw = Math.max(0, Number.isFinite(bounds.width) ? bounds.width : 0);
  const bh = Math.max(0, Number.isFinite(bounds.height) ? bounds.height : 0);
  const width = Math.min(Math.max(size.width, Math.min(anchor.width, bw)), bw);
  const below = Math.max(0, bh - (anchor.y + anchor.height));
  const above = Math.max(0, anchor.y);
  const up = size.height > below && above > below;
  const room = Math.max(48, up ? above : below);
  const height = Math.min(size.height, room, bh);
  const y = up ? Math.max(0, anchor.y - height) : Math.min(Math.max(0, anchor.y + anchor.height), Math.max(0, bh - height));
  const x = Math.max(0, Math.min(anchor.x, bw - width));
  return { x, y, width, maxHeight: Math.min(room, bh), above: up };
}

// -- the owner's gesture ------------------------------------------------------------------------

/**
 * A picker is shown only as the answer to this window's own left press, in
 * the same stream generation, within PICKER_GESTURE_MS, once. The daemon is
 * the real check (only the owner's socket reaches its `_deliver`); this keeps
 * a picker the window did not ask for off the screen.
 */
export class PickerGesture {
  private at = -Infinity;
  private gen = -1;
  press(gen: number, now: number) { this.gen = gen; this.at = now; }
  clear() { this.at = -Infinity; this.gen = -1; }
  accept(gen: number, now: number): boolean {
    const ok = gen === this.gen && now >= this.at && now - this.at <= PICKER_GESTURE_MS;
    this.clear();
    return ok;
  }
}
