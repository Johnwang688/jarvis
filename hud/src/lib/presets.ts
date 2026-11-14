// The owner's own colour presets (2026-11-13): a name plus the accent, the three washes, the background and the
// editor & terminal colour (`codeAccent`). Motion, drift and RGB stay separate settings. Only these can be
// renamed or deleted; the built-in presets in lib/appearance.ts stay fixed.
//
// Stored in `localStorage['jarvis.hud.presets']` as a JSON array, through lib/storage.ts (a blocked or throwing
// storage is "nothing stored"). Nothing stored is trusted: every read validates each entry (shape, id, name,
// and the quick per-colour guards: each wash and the background as `checkWash`/`checkBase` would pass it
// unclamped, worked out without the clamp (`darkUnclamped`), the accent and the code accent through
// `checkAccent` against the preset's own washes), and every write stores the
// cleaned list, so an entry this HUD does not understand is dropped rather than carried. The whole-palette
// proof (`checkPalette`, inside `checkPreset`) is too slow to run over a stored list (Sol round 2: seconds for
// 24 near-band palettes), so it runs on one preset at a time: the one being added or restored, and the one
// being applied (the Settings panel calls `checkCustomPreset` at the click and refuses with a note). No read,
// rename, delete or panel refresh ever proves the stored list. Only `#rrggbb` ever leaves this module as a colour,
// lower-cased. A name is plain text for React to render: control, format and bidi characters are removed.
//
// A rename or delete of an id another window already removed is a refusal, not a crash. Every successful write
// fires PRESETS_EVENT on `window` (the `storage` event covers other windows).

import {
  BASE_MAX_LUMINANCE, COLOUR_PRESETS, WASH_CHROMA_FLOOR, WASH_MAX_LUMINANCE, checkAccent, checkBase, checkPreset, checkWash,
  impersonatesMeaning, normHex, type Colours, type Refusal,
} from "./appearance";
import { luminance } from "./oklch";
import { readKey, writeKey, type StorageArg } from "./storage";

export const CUSTOM_PRESETS_KEY = "jarvis.hud.presets";
/** Fired on `window` after a successful write, so an open Settings panel re-reads at once. */
export const PRESETS_EVENT = "hud-presets-change";
export const MAX_CUSTOM_PRESETS = 24;
/** Longest name, in code points. */
export const MAX_PRESET_NAME = 32;

export interface CustomPreset extends Colours {
  /** `c-` plus lower-case base 36. */
  id: string;
  name: string;
}

export type PresetResult =
  | { ok: true; presets: CustomPreset[]; preset?: CustomPreset; index?: number }
  | { ok: false; reason: string };

type Store = StorageArg<Pick<Storage, "getItem" | "setItem">>;

const ID_RE = /^c-[a-z0-9]{1,24}$/;
const refuse = (reason: string): { ok: false; reason: string } => ({ ok: false, reason });

/** A name as it is stored and shown: trimmed, runs of whitespace one space, no control/format/bidi characters. */
export function cleanPresetName(raw: unknown): string {
  if (typeof raw !== "string") return "";
  const spaced = raw.replace(/\s+/gu, " ").replace(/[\p{Cc}\p{Cf}]/gu, "");
  const cut = [...spaced.replace(/\s+/gu, " ").trim()].slice(0, MAX_PRESET_NAME).join("");
  return cut.trim();
}

/** The form names are compared in (a taken name is taken in any case). */
const fold = (name: string) => name.normalize("NFKC").toLowerCase();
const BUILTIN_NAMES = COLOUR_PRESETS.map((p) => fold(p.name));

/** Lower-case `#rrggbb` colours from anything, or null when any of the six is not a colour. */
function normaliseColours(v: unknown): Colours | null {
  if (typeof v !== "object" || v === null) return null;
  const o = v as Record<string, unknown>;
  if (!Array.isArray(o.washes) || o.washes.length !== 3) return null;
  const accent = normHex(o.accent), base = normHex(o.base), codeAccent = normHex(o.codeAccent);
  const washes = o.washes.map(normHex);
  if (!accent || !base || !codeAccent || washes.some((w) => w === null)) return null;
  return { accent, washes: washes as [string, string, string], base, codeAccent };
}

/**
 * `checkWash`/`checkBase` passing a colour unclamped (`ok`, not `clamped`), without paying for the clamp.
 * checkDark refuses a colour in the amber/red band, then clamps it: `clampLuminance` returns the colour itself
 * exactly when its luminance is at most the cap (otherwise a darker colour, at most the cap, so never the
 * same one), and an unchanged colour meets the same band test again. So "passes unclamped" is exactly: not in
 * the band, and luminance at most the cap. The clamp's 30-step bisection only ever produced a value this
 * module refuses anyway (Sol round 3: seconds over a quota-sized list of over-bright colours).
 */
function darkUnclamped(hex: string, cap: number): boolean {
  return !impersonatesMeaning(hex, WASH_CHROMA_FLOOR) && luminance(hex) <= cap;
}

/** One read's verdicts per colour (and per accent against its washes), so repeated colours cost nothing. */
type QuickMemo = Map<string, boolean>;

function remembered(memo: QuickMemo | undefined, key: string, judge: () => boolean): boolean {
  if (!memo) return judge();
  const hit = memo.get(key);
  if (hit !== undefined) return hit;
  const v = judge();
  memo.set(key, v);
  return v;
}

const QUICK_REFUSED: Refusal = { ok: false, reason: "A preset colour is not allowed." };

/**
 * The quick per-colour guards (no whole-palette proof): every wash and the background pass unclamped, and the
 * accent and the code accent are readable and clear of amber and red against the preset's own washes. A
 * handful of colour conversions per colour, so it may run over every stored entry on each read.
 *
 * With `memo` (a read over the stored list), a refusal is a fixed one and verdicts are shared across entries
 * for that read only. Without it (the one preset being added, restored or applied), the refusal is the
 * guard's own reason, worked out only once the colour is known to fail.
 */
function quickCheck(c: Colours, memo?: QuickMemo): Refusal | null {
  for (const w of c.washes) {
    if (!remembered(memo, `w${w}`, () => darkUnclamped(w, WASH_MAX_LUMINANCE))) return memo ? QUICK_REFUSED : darkReason(checkWash(w), "colour");
  }
  if (!remembered(memo, `b${c.base}`, () => darkUnclamped(c.base, BASE_MAX_LUMINANCE))) return memo ? QUICK_REFUSED : darkReason(checkBase(c.base), "background");
  const washes = c.washes.join("");
  for (const accent of [c.accent, c.codeAccent]) {
    if (memo) {
      if (!remembered(memo, `a${accent}${washes}`, () => checkAccent(accent, c.washes).ok)) return QUICK_REFUSED;
    } else {
      const a = checkAccent(accent, c.washes);
      if (!a.ok) return a;
    }
  }
  return null;
}

/** The refusal for a wash or background that does not pass unclamped (the guard's reason, or its darkening note). */
function darkReason(r: ReturnType<typeof checkWash>, what: string): Refusal {
  if (!r.ok) return r;
  return { ok: false, reason: r.note ?? `A preset ${what} would need darkening.` };
}

// The full check repeats for the same colours (Undo, clicking one preset again): its verdict is remembered.
// Only full verdicts live here; the quick guards never write to it, so it can never hold an unproved "safe".
const verdicts = new Map<string, Refusal | null>();

/**
 * null when the preset's colours may be applied as they stand, else the first refusal: the quick guards, then
 * the whole-palette proof (checkPreset). The proof can take a while, so this runs on ONE preset at a time
 * (the one being added, restored or applied), never over the stored list.
 */
export function checkCustomPreset(p: CustomPreset): Refusal | null {
  const c = normaliseColours(p);
  if (!c) return { ok: false, reason: "A preset needs six colours written as #rrggbb." };
  const key = [c.accent, ...c.washes, c.base, c.codeAccent].join(",");
  const hit = verdicts.get(key);
  if (hit !== undefined) return hit;
  // A doomed colour set is refused by the quick guards before the proof is paid for.
  const verdict = quickCheck(c) ?? checkPreset({ id: "custom", name: "custom", accent: c.accent, washes: c.washes, base: c.base });
  if (verdicts.size >= 512) verdicts.clear();
  verdicts.set(key, verdict);
  return verdict;
}

/** What one read of the stored list has already accepted, and its per-colour verdicts. */
interface ReadState {
  ids: Set<string>;
  names: Set<string>;
  memo: QuickMemo;
}

/**
 * A stored or offered entry as a clean preset, or null: the id well formed, the name and colours normalised,
 * and the quick guards passed. The whole-palette proof is not run here (see checkCustomPreset).
 *
 * With `read` (loadCustomPresets), an entry whose id or name that read already accepted is null too, found
 * before any colour work: such an entry is dropped whatever its colours, so the order changes no result.
 */
function cleanEntry(e: unknown, read?: ReadState): CustomPreset | null {
  if (typeof e !== "object" || e === null || Array.isArray(e)) return null;
  const id = (e as { id?: unknown }).id;
  if (typeof id !== "string" || !ID_RE.test(id) || read?.ids.has(id)) return null;
  const name = cleanPresetName((e as { name?: unknown }).name);
  if (!name || read?.names.has(fold(name))) return null;
  const c = normaliseColours(e);
  if (!c || quickCheck(c, read?.memo)) return null;
  return { id, name, ...c };
}

/**
 * The valid entries in stored order (at most MAX_CUSTOM_PRESETS). Every stored entry is examined, so a valid
 * preset is kept however many malformed ones come before it; each costs only the quick guards, a repeated
 * colour nothing after its first verdict, and a repeated id or name no colour work at all. Entries that
 * pass them but fail the whole-palette proof are kept here and refused when applied. Never throws; a corrupt
 * or unreadable record is an empty list.
 */
export function loadCustomPresets(storage?: Store | null): CustomPreset[] {
  try {
    const raw = readKey(CUSTOM_PRESETS_KEY, storage);
    if (!raw) return [];
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      return [];
    }
    if (!Array.isArray(parsed)) return [];
    const out: CustomPreset[] = [];
    const read: ReadState = { ids: new Set(), names: new Set(BUILTIN_NAMES), memo: new Map() };
    for (const e of parsed) {
      if (out.length >= MAX_CUSTOM_PRESETS) break;
      const p = cleanEntry(e, read);
      if (!p) continue;
      read.ids.add(p.id);
      read.names.add(fold(p.name));
      out.push(p);
    }
    return out;
  } catch {
    return [];
  }
}

/** Stores the list (already clean) and tells the window; a refusal when the write did not take. */
function commit(list: CustomPreset[], storage?: Store | null): string | null {
  const json = JSON.stringify(list);
  writeKey(CUSTOM_PRESETS_KEY, json, storage);
  if (readKey(CUSTOM_PRESETS_KEY, storage) !== json) return "Could not save the preset: this window's storage did not accept it.";
  try {
    window.dispatchEvent(new Event(PRESETS_EVENT));
  } catch {
    /* no window: nothing is listening */
  }
  return null;
}

function newId(taken: ReadonlySet<string>): string {
  for (let i = 0; i < 50; i++) {
    let s: string;
    if (typeof globalThis.crypto?.getRandomValues === "function") {
      s = [...globalThis.crypto.getRandomValues(new Uint32Array(3))].map((n) => n.toString(36)).join("");
    } else {
      s = Math.random().toString(36).slice(2, 14);
    }
    const id = `c-${s || "0"}`;
    if (ID_RE.test(id) && !taken.has(id)) return id;
  }
  // The random source kept colliding (or is absent): the timestamp, with a counter until it is unique.
  const stamp = Date.now().toString(36).slice(-18);
  for (let n = 0; ; n++) {
    const id = `c-${stamp}${n ? n.toString(36) : ""}`;
    if (ID_RE.test(id) && !taken.has(id)) return id;
  }
}

const takenReason = "That name is already used by a preset.";

/** Why `name` cannot be used (by a preset other than `selfId`), or null. */
function nameProblem(name: string, list: readonly CustomPreset[], selfId?: string): string | null {
  if (!name) return "Give the preset a name.";
  const f = fold(name);
  if (BUILTIN_NAMES.includes(f) || list.some((p) => p.id !== selfId && fold(p.name) === f)) return takenReason;
  return null;
}

/** Saves `colours` as a new preset called `name`, last in the list. */
export function addCustomPreset(name: string, colours: Colours, storage?: Store | null): PresetResult {
  const list = loadCustomPresets(storage);
  if (list.length >= MAX_CUSTOM_PRESETS) return refuse(`You can keep up to ${MAX_CUSTOM_PRESETS} presets. Delete one first.`);
  const clean = cleanPresetName(name);
  const problem = nameProblem(clean, list);
  if (problem) return refuse(problem);
  const c = normaliseColours(colours);
  if (!c) return refuse("A preset needs six colours written as #rrggbb.");
  const preset: CustomPreset = { id: newId(new Set(list.map((p) => p.id))), name: clean, ...c };
  const bad = checkCustomPreset(preset);
  if (bad) return refuse(bad.reason);
  const next = [...list, preset];
  const failed = commit(next, storage);
  return failed ? refuse(failed) : { ok: true, presets: next, preset, index: next.length - 1 };
}

/** Renames a custom preset. Its own name in another case is allowed; a taken name, an empty one and an unknown id are not. */
export function renameCustomPreset(id: string, name: string, storage?: Store | null): PresetResult {
  const list = loadCustomPresets(storage);
  const index = list.findIndex((p) => p.id === id);
  if (index < 0) return refuse("That preset no longer exists.");
  const clean = cleanPresetName(name);
  const problem = nameProblem(clean, list, id);
  if (problem) return refuse(problem);
  const preset = { ...list[index]!, name: clean };
  const next = list.map((p, i) => (i === index ? preset : p));
  const failed = commit(next, storage);
  return failed ? refuse(failed) : { ok: true, presets: next, preset, index };
}

/** Deletes a custom preset, returning it and where it was (for Undo). */
export function deleteCustomPreset(id: string, storage?: Store | null): PresetResult {
  const list = loadCustomPresets(storage);
  const index = list.findIndex((p) => p.id === id);
  if (index < 0) return refuse("That preset no longer exists.");
  const preset = list[index]!;
  const next = list.filter((_, i) => i !== index);
  const failed = commit(next, storage);
  return failed ? refuse(failed) : { ok: true, presets: next, preset, index };
}

/** Puts a deleted preset back at `index` (clamped). A duplicate id or name, a full list or an unsafe preset is refused. */
export function restoreCustomPreset(preset: CustomPreset, index: number, storage?: Store | null): PresetResult {
  const list = loadCustomPresets(storage);
  if (list.length >= MAX_CUSTOM_PRESETS) return refuse(`You can keep up to ${MAX_CUSTOM_PRESETS} presets. Delete one first.`);
  const clean = cleanEntry(preset);
  if (!clean) return refuse("That preset cannot be restored.");
  if (list.some((p) => p.id === clean.id)) return refuse("That preset is already back.");
  const problem = nameProblem(clean.name, list);
  if (problem) return refuse(problem);
  // The one preset coming back gets the full check, last (the stored list is never re-proved).
  const bad = checkCustomPreset(clean);
  if (bad) return refuse(bad.reason);
  const at = Number.isFinite(index) ? Math.min(list.length, Math.max(0, Math.trunc(index))) : list.length;
  const next = [...list.slice(0, at), clean, ...list.slice(at)];
  const failed = commit(next, storage);
  return failed ? refuse(failed) : { ok: true, presets: next, preset: clean, index: at };
}
