// `.editorconfig` for the editor's buffers (ED-8): finds the files that apply
// to a buffer and puts what they say on its model, once, as the buffer is made
// (`Buffers.make` asks before it records the text it was read as, so the
// buffer reads clean). The rules are `lib/editorconfig.ts`.
//
// **Read only through the editor's own routes** (ED-2, owner-only, reads
// included): one `files/stat` for every folder from the file's own up to the
// project's root — never above it — then a read of each `.editorconfig` that
// exists, nearest first, stopping at `root = true`. No route of its own. What a
// stat or a read refuses (a withheld folder, an error, a slow disk past
// `TIMEOUT_MS`) is simply no configuration: opening a file never waits on, or
// fails for, this.
//
// **Indentation and new-line endings only, never a rewrite**: the model's
// indent options change (no text does), and the line ending only of a model
// with no line break yet — which changes no byte of what a save writes.
//
// Monaco recomputes every model's indentation when the editors' tab size,
// spaces or bracket colours change (its model service follows the editor
// settings), which would wash an `.editorconfig` override out; the override is
// kept per model and put back after every such change (`reapplyEditorConfig`).

import type { FileRead } from "../types";
import type { StatRow } from "../lib/buffers";
import {
  candidateDirs, configPath, FILE_MAX, overrideOf, parseEditorConfig, resolveProps,
  type ConfigFile, type Override, type Resolved,
} from "../lib/editorconfig";

export interface EditorConfigDeps {
  stat(projectId: string, paths: string[]): Promise<{ files: Record<string, StatRow> }>;
  read(projectId: string, path: string): Promise<FileRead>;
}

/** How long a folder's answer is kept: a tab restore asks once per folder, an edit to the file is seen soon. */
export const CACHE_MS = 3000;
/** Opening a file never waits on this longer (a slow `/mnt/c`): past it, no configuration. */
export const TIMEOUT_MS = 2500;

interface Entry {
  at: number;
  /** The stat's answer for this folder's `.editorconfig`: its etag when it is there and readable as text. */
  etag: Promise<string | null>;
}

/** Folders' stat answers, kept `CACHE_MS`. */
const cache = new Map<string, Entry>();
/** Parsed files by (project, folder, etag): a file is read once per content. */
const parsed = new Map<string, Promise<ConfigFile | null>>();
/**
 * Resolutions by (project, path, every config's folder and etag): restored
 * tabs, and the same file opened again, never redo the matching (review of
 * PR #59).
 */
const resolved = new Map<string, Resolved>();
/** Resolutions already said to be over their budget: said once each. */
const warned = new Set<string>();
const PARSED_MAX = 256;
const RESOLVED_MAX = 2048;
const keyOf = (projectId: string, dir: string) => `${projectId}\u0000${dir}`;

function remember<V>(map: Map<string, V>, key: string, value: V, max: number) {
  if (map.size >= max) map.clear();
  map.set(key, value);
}

/** Tests only: forget every folder's answer, every parse and every resolution. */
export function forgetEditorConfig() {
  cache.clear();
  parsed.clear();
  resolved.clear();
  warned.clear();
}

/** The properties `.editorconfig` gives `path` in `projectId` (`{}` when none apply). */
export async function editorConfigFor(
  deps: EditorConfigDeps, projectId: string, path: string, now = Date.now(),
): Promise<Record<string, string>> {
  return (await resolveFor(deps, projectId, path, now)).props;
}

/**
 * The resolution for `path`: its properties, or why it has none (a file of too
 * many sections, the work budget spent — `lib/editorconfig.ts`). An over-budget
 * resolution is said once in the console, by name, never with any content.
 */
export async function resolveFor(
  deps: EditorConfigDeps, projectId: string, path: string, now = Date.now(),
): Promise<Resolved> {
  const dirs = candidateDirs(path);
  const missing = dirs.filter((d) => {
    const e = cache.get(keyOf(projectId, d));
    return !e || now - e.at > CACHE_MS;
  });
  if (missing.length) {
    const rows = deps.stat(projectId, missing.map(configPath)).then(
      (r) => (r && typeof r === "object" && r.files && typeof r.files === "object" ? r.files : {}),
      () => ({} as Record<string, StatRow>),
    );
    for (const d of missing) {
      const etag = rows.then((files) => {
        const row = files[configPath(d)];
        if (!row || row.gone || row.error || row.kind !== "text" || typeof row.etag !== "string") return null;
        // A file over the cap is never downloaded (the real stat says its size).
        const size = (row as { size?: unknown }).size;
        if (typeof size === "number" && size > FILE_MAX) return null;
        return row.etag;
      });
      cache.set(keyOf(projectId, d), { at: now, etag });
    }
  }
  const found: { dir: string; file: ConfigFile; etag: string }[] = [];
  for (const d of dirs) { // nearest first
    const e = cache.get(keyOf(projectId, d));
    const etag = e ? await e.etag : null;
    if (!etag) continue;
    const pkey = `${keyOf(projectId, d)}\u0000${etag}`;
    let p = parsed.get(pkey);
    if (!p) {
      p = readConfig(deps, projectId, configPath(d));
      remember(parsed, pkey, p, PARSED_MAX);
    }
    const file = await p;
    if (!file) continue;
    found.push({ dir: d, file, etag });
    if (file.root || file.tooMany) break;
  }
  found.reverse();
  const rkey = [projectId, path, ...found.map((f) => `${f.dir}@${f.etag}`)].join("\u0000");
  const known = resolved.get(rkey);
  if (known) return known;
  const r = resolveProps(found, path);
  remember(resolved, rkey, r, RESOLVED_MAX);
  if (r.over && !warned.has(rkey)) {
    warned.add(rkey);
    console.warn(`.editorconfig not applied to ${path}: ${r.over === "sections"
      ? "a file there has too many sections" : "its sections are too much work to match"}`);
  }
  return r;
}

async function readConfig(deps: EditorConfigDeps, projectId: string, path: string): Promise<ConfigFile | null> {
  try {
    const r = await deps.read(projectId, path);
    if (!r || r.protected || r.kind !== "text" || typeof r.content !== "string" || r.content.length > FILE_MAX) return null;
    return parseEditorConfig(r.content);
  } catch {
    return null;
  }
}

// ---- models -----------------------------------------------------------------

/** The part of a Monaco model this touches (the fallback's shim has none of it). */
interface ConfigurableModel {
  updateOptions?(o: Record<string, unknown>): void;
  getLineCount?(): number;
  getEOL?(): string;
  setEOL?(eol: number): void;
  onWillDispose?(fn: () => void): { dispose(): void };
  isDisposed?(): boolean;
}

const overrides = new Map<ConfigurableModel, Override>();

function indentOf(o: Override): Record<string, unknown> | null {
  const opts: Record<string, unknown> = {};
  if (o.insertSpaces !== undefined) opts.insertSpaces = o.insertSpaces;
  if (o.tabSize !== undefined) opts.tabSize = o.tabSize;
  if (o.indentSize !== undefined) opts.indentSize = o.indentSize;
  return Object.keys(opts).length ? opts : null;
}

/**
 * Put `.editorconfig`'s word on a buffer's freshly made model (Buffers' prepare
 * hook). Never throws and never waits past `TIMEOUT_MS`. The line ending is set
 * only on a model with no line break (one line), before the registry records
 * the version it was read as.
 */
export async function applyEditorConfig(
  deps: EditorConfigDeps, model: ConfigurableModel, projectId: string, path: string,
): Promise<Override | null> {
  if (!model || typeof model.updateOptions !== "function") return null;
  let props: Record<string, string> = {};
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    props = await Promise.race([
      editorConfigFor(deps, projectId, path),
      new Promise<Record<string, string>>((resolve) => {
        timer = setTimeout(() => resolve({}), TIMEOUT_MS);
      }),
    ]);
  } catch {
    props = {};
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
  const o = overrideOf(props);
  if (!o || model.isDisposed?.()) return null;
  const indent = indentOf(o);
  if (indent) {
    try {
      model.updateOptions(indent);
    } catch {
      /* a model that refuses keeps its own indentation */
    }
    if (!overrides.has(model)) model.onWillDispose?.(() => overrides.delete(model));
    overrides.set(model, o);
  }
  if (o.eol && typeof model.setEOL === "function" && model.getLineCount?.() === 1) {
    const want = o.eol === "CRLF" ? "\r\n" : "\n";
    if (model.getEOL?.() !== want) model.setEOL(o.eol === "CRLF" ? 1 : 0);
  }
  return o;
}

/** Put every live model's `.editorconfig` indentation back (after an editor setting moved Monaco's). */
export function reapplyEditorConfig() {
  for (const [model, o] of overrides) {
    if (model.isDisposed?.()) {
      overrides.delete(model);
      continue;
    }
    const indent = indentOf(o);
    if (!indent) continue;
    try {
      model.updateOptions?.(indent);
    } catch {
      /* ignore */
    }
  }
}

/** Tests only: the override a model holds. */
export function overrideFor(model: unknown): Override | null {
  return overrides.get(model as ConfigurableModel) ?? null;
}
