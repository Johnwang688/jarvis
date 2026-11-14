// The owner's own colour presets (2026-11-13): a name plus the accent, the three washes, the background and the
// editor & terminal colour (`codeAccent`). Motion, drift and RGB stay separate settings. Only these can be
// renamed or deleted; the built-in presets in lib/appearance.ts stay fixed.
//
// Stored in `localStorage['jarvis.hud.presets']` as a JSON array, through lib/storage.ts (a blocked or throwing
// storage is "nothing stored"). Nothing stored is trusted: every read validates each entry (shape, id, name,
// colours through the same guards a built-in passes: `checkPreset` for the five colours and `checkAccent` for
// the code accent against the preset's own washes), and every write stores the cleaned list, so an entry this
// HUD does not understand is dropped rather than carried. Only `#rrggbb` ever leaves this module as a colour,
// lower-cased. A name is plain text for React to render: control, format and bidi characters are removed.
//
// A rename or delete of an id another window already removed is a refusal, not a crash. Every successful write
// fires PRESETS_EVENT on `window` (the `storage` event covers other windows).

import { COLOUR_PRESETS, checkAccent, checkBase, checkPreset, checkWash, normHex, type Colours, type Refusal } from "./appearance";
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

// The guards are pure and the palette proof can take a few ms, so a verdict is remembered for a colour set.
const verdicts = new Map<string, Refusal | null>();

/** null when the preset's colours may be applied as they stand, else the first refusal. */
export function checkCustomPreset(p: CustomPreset): Refusal | null {
  const c = normaliseColours(p);
  if (!c) return { ok: false, reason: "A preset needs six colours written as #rrggbb." };
  const key = [c.accent, ...c.washes, c.base, c.codeAccent].join(",");
  const hit = verdicts.get(key);
  if (hit !== undefined) return hit;
  // The cheap per-colour guards run first, so a doomed colour set is refused before the palette proof
  // (inside checkPreset) is paid for; nothing that fails a guard is kept either way.
  let verdict: Refusal | null = null;
  for (const w of c.washes) {
    const r = checkWash(w);
    if (!r.ok) { verdict = r; break; }
    if (r.clamped) { verdict = { ok: false, reason: r.note ?? "A preset colour would need darkening." }; break; }
  }
  if (!verdict) {
    const b = checkBase(c.base);
    if (!b.ok) verdict = b;
    else if (b.clamped) verdict = { ok: false, reason: b.note ?? "A preset background would need darkening." };
  }
  if (!verdict) {
    const a = checkAccent(c.accent, c.washes);
    if (!a.ok) verdict = a;
  }
  if (!verdict) {
    const code = checkAccent(c.codeAccent, c.washes);
    if (!code.ok) verdict = code;
  }
  if (!verdict) verdict = checkPreset({ id: "custom", name: "custom", accent: c.accent, washes: c.washes, base: c.base });
  if (verdicts.size >= 512) verdicts.clear();
  verdicts.set(key, verdict);
  return verdict;
}

/** A stored or offered entry as a clean preset, or null. Name and colours are normalised; the id must be well formed. */
function cleanEntry(e: unknown, id: unknown = typeof e === "object" && e !== null ? (e as { id?: unknown }).id : undefined): CustomPreset | null {
  if (typeof e !== "object" || e === null || Array.isArray(e)) return null;
  if (typeof id !== "string" || !ID_RE.test(id)) return null;
  const name = cleanPresetName((e as { name?: unknown }).name);
  const c = normaliseColours(e);
  if (!name || !c) return null;
  const p: CustomPreset = { id, name, ...c };
  return checkCustomPreset(p) ? null : p;
}

/** The valid entries in stored order. Never throws; a corrupt or unreadable record is an empty list. */
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
    const ids = new Set<string>();
    const names = new Set<string>(BUILTIN_NAMES);
    // At most this many raw entries are examined, however many are stored, so a hostile list cannot hold the
    // thread: a list this HUD wrote has at most MAX_CUSTOM_PRESETS, and the rest is slack for dropped entries.
    for (const e of parsed.slice(0, MAX_CUSTOM_PRESETS * 2)) {
      if (out.length >= MAX_CUSTOM_PRESETS) break;
      const p = cleanEntry(e);
      if (!p || ids.has(p.id) || names.has(fold(p.name))) continue;
      ids.add(p.id);
      names.add(fold(p.name));
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
  const at = Number.isFinite(index) ? Math.min(list.length, Math.max(0, Math.trunc(index))) : list.length;
  const next = [...list.slice(0, at), clean, ...list.slice(at)];
  const failed = commit(next, storage);
  return failed ? refuse(failed) : { ok: true, presets: next, preset: clean, index: at };
}
