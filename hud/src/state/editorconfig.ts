// `.editorconfig` for the editor's buffers (ED-8): finds the files that apply
// to a buffer and puts what they say on its model, once, as the buffer is made
// (`Buffers.make` asks before it records the text it was read as, so the
// buffer reads clean). The rules are `lib/editorconfig.ts`.
//
// **Read only through the editor's own routes** (ED-2, owner-only, reads
// included): one `files/stat` for every folder from the file's own up to the
// project's root — never above it — then a read of each `.editorconfig` that
// exists (once per etag; never one the stat says is over 64 KB). No route of
// its own. What a stat or a read refuses (a withheld folder, an error, a slow
// disk past `TIMEOUT_MS`) is simply no configuration: opening a file never
// waits on, or fails for, this.
//
// **The window is never held** (review of PR #59, the lead's structural
// backstop): an `.editorconfig` is agent-writable, and judging one is work on
// the main thread of the window that gates approvals. So:
//  - **a real clock**: a resolution — parse, compile, match — spends through
//    one `Deadline` of `RESOLVE_MS` (5 ms), checked every few hundred steps
//    inside the parser, the compiler and the matcher; out of time, the file
//    has no configuration;
//  - **poison**: a config file (by folder and etag) that spent the time, or
//    is over a cap, configures nothing from then on, for any path, until its
//    etag changes — every later resolution touching it is O(1). A time-out is
//    a strike, and two strikes poison (one GC pause must not cost a real
//    config its effect); a cap poisons at once;
//  - **no batching**: resolutions run one per macrotask through a queue that
//    yields between items (`MessageChannel`, else `setTimeout`), so restored
//    tabs never pile into one long task. A path's result is kept per (path,
//    each config's folder and etag), so a tab opened again asks nothing.
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
  candidateDirs, configPath, deadline, FILE_MAX, OutOfTime, overrideOf, parseEditorConfig, resolveProps,
  type ConfigFile, type Deadline, type Override, type Resolved,
} from "../lib/editorconfig";

export interface EditorConfigDeps {
  stat(projectId: string, paths: string[]): Promise<{ files: Record<string, StatRow> }>;
  read(projectId: string, path: string): Promise<FileRead>;
}

/** How long a folder's stat answer is kept: a tab restore asks once per folder, an edit to the file is seen soon. */
export const CACHE_MS = 3000;
/** Opening a file never waits on this longer (a slow `/mnt/c`): past it, no configuration. */
export const TIMEOUT_MS = 2500;
/** Time-outs that poison a config file (a cap poisons at once). */
export const STRIKES = 2;

interface Entry {
  at: number;
  /** The stat's answer for this folder's `.editorconfig`: its etag when it is there and readable as text. */
  etag: Promise<string | null>;
}

/** One config file's content, by (project, folder, etag). */
interface ConfigRecord {
  /** Its text, read once; null when it could not be (or is not a configuration). */
  text: string | null;
  /** Parsed, once, inside a resolution's time. */
  file: ConfigFile | null;
  /** Time-outs blamed on it. */
  strikes: number;
  /** Why it configures nothing (time, a cap): a resolution touching it stops at once. */
  poisoned: string | null;
}

/** Folders' stat answers, kept `CACHE_MS`. */
const folders = new Map<string, Entry>();
/** Config files by (project, folder, etag). */
const records = new Map<string, Promise<ConfigRecord>>();
/** Resolutions by (project, path, every config's folder and etag). */
const resolved = new Map<string, Resolved>();
/** Paths already said in the console to have no configuration: said once each. */
const warned = new Set<string>();
const RECORDS_MAX = 256;
const RESOLVED_MAX = 2048;
const WARNED_MAX = 64;
const keyOf = (projectId: string, dir: string) => `${projectId}\u0000${dir}`;

/** Keep `value`, letting the oldest entry go when full (never a wholesale clear). */
function remember<V>(map: Map<string, V>, key: string, value: V, max: number) {
  if (map.size >= max && !map.has(key)) map.delete(map.keys().next().value as string);
  map.set(key, value);
}

// ---- the queue --------------------------------------------------------------

type Post = (run: () => void) => void;

/** One item per macrotask: a `MessageChannel` in the window, `setTimeout` elsewhere (tests). */
function defaultPost(): Post {
  // Node (the tests) keeps a process alive while a port listens: timers there.
  if ((globalThis as { process?: unknown }).process === undefined && typeof MessageChannel !== "undefined") {
    const channel = new MessageChannel();
    const waiting: (() => void)[] = [];
    channel.port1.onmessage = () => waiting.shift()?.();
    return (run) => {
      waiting.push(run);
      channel.port2.postMessage(0);
    };
  }
  return (run) => setTimeout(run, 0);
}

let post: Post = defaultPost();
const queue: (() => void)[] = [];
let posted = false;
const stats = { jobs: 0, longestMs: 0 };

function pump() {
  if (posted || !queue.length) return;
  posted = true;
  post(() => {
    posted = false;
    const job = queue.shift();
    if (job) {
      const t = performance.now();
      try {
        job();
      } finally {
        stats.jobs += 1;
        stats.longestMs = Math.max(stats.longestMs, performance.now() - t);
      }
    }
    pump();
  });
}

/** Run `job` in a macrotask of its own, after every job queued before it. */
function enqueue<T>(job: () => T): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    queue.push(() => {
      try {
        resolve(job());
      } catch (e) {
        reject(e);
      }
    });
    pump();
  });
}

/** Tests only: how many resolution jobs ran, and the longest. */
export function resolutionStats() {
  return { ...stats, queued: queue.length };
}

/** Tests only: forget everything, and optionally run the queue through `next`. */
export function forgetEditorConfig(next?: Post) {
  folders.clear();
  records.clear();
  resolved.clear();
  warned.clear();
  queue.length = 0;
  posted = false;
  stats.jobs = 0;
  stats.longestMs = 0;
  post = next ?? defaultPost();
}

// ---- resolving --------------------------------------------------------------

/** The properties `.editorconfig` gives `path` in `projectId` (`{}` when none apply). */
export async function editorConfigFor(
  deps: EditorConfigDeps, projectId: string, path: string, now = Date.now(),
): Promise<Record<string, string>> {
  return (await resolveFor(deps, projectId, path, now)).props;
}

/**
 * The resolution for `path`: its properties, or why it has none. The fetching
 * is asynchronous; the judging is one queued job under `RESOLVE_MS`, or
 * nothing at all when the answer is known or a config on the walk is poisoned.
 * A path left with no configuration is said once in the console, by path,
 * never with any content.
 */
export async function resolveFor(
  deps: EditorConfigDeps, projectId: string, path: string, now = Date.now(),
  clock: () => Deadline = () => deadline(),
): Promise<Resolved> {
  const dirs = candidateDirs(path);
  const missing = dirs.filter((d) => {
    const e = folders.get(keyOf(projectId, d));
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
      remember(folders, keyOf(projectId, d), { at: now, etag }, RECORDS_MAX * 4);
    }
  }
  // Every config on the walk, nearest first (which ones apply — `root = true`
  // — is known once they are parsed, inside the job).
  const walk: { dir: string; etag: string; rec: ConfigRecord }[] = [];
  for (const d of dirs) {
    const e = folders.get(keyOf(projectId, d));
    const etag = e ? await e.etag : null;
    if (!etag) continue;
    const rkey = `${keyOf(projectId, d)}\u0000${etag}`;
    let p = records.get(rkey);
    if (!p) {
      p = readRecord(deps, projectId, configPath(d));
      remember(records, rkey, p, RECORDS_MAX);
    }
    walk.push({ dir: d, etag, rec: await p });
  }
  const key = [projectId, path, ...walk.map((w) => `${w.dir}@${w.etag}`)].join("\u0000");
  const known = resolved.get(key);
  if (known) return known;
  // A poisoned config on the walk: nothing is judged at all.
  if (walk.some((w) => w.rec.poisoned)) return settle(key, path, poisoned());
  return settle(key, path, await enqueue(() => judge(walk, path, clock())));
}

const poisoned = (): Resolved => ({ props: {}, over: "poisoned", culprit: null, steps: 0 });

/** One resolution, synchronous, under its deadline: parse what is not parsed, then match. */
function judge(walk: { dir: string; rec: ConfigRecord }[], path: string, d: Deadline): Resolved {
  if (walk.some((w) => w.rec.poisoned)) return poisoned(); // poisoned while this one waited
  const applying: { dir: string; file: ConfigFile; rec: ConfigRecord }[] = [];
  for (const w of walk) { // nearest first, to `root = true`
    if (w.rec.text === null) continue;
    if (!w.rec.file) {
      try {
        w.rec.file = parseEditorConfig(w.rec.text, d);
      } catch (e) {
        if (!(e instanceof OutOfTime)) throw e;
        strike(w.rec, "time");
        return { props: {}, over: "time", culprit: null, steps: d.total };
      }
    }
    applying.push({ dir: w.dir, file: w.rec.file, rec: w.rec });
    if (w.rec.file.root || w.rec.file.tooMany) break;
  }
  applying.reverse();
  const r = resolveProps(applying, path, d);
  if (r.over && r.culprit !== null) {
    const rec = applying[r.culprit].rec;
    if (r.over === "time") strike(rec, "time");
    else rec.poisoned = r.over; // a cap: at once
  }
  return r;
}

function strike(rec: ConfigRecord, why: string) {
  rec.strikes += 1;
  if (rec.strikes >= STRIKES) rec.poisoned = why;
}

/** Keep a resolution (unless it was only a time-out not yet blamed for good) and say an empty one once. */
function settle(key: string, path: string, r: Resolved): Resolved {
  if (r.over !== "time") remember(resolved, key, r, RESOLVED_MAX);
  if (r.over && !warned.has(key)) {
    if (warned.size >= WARNED_MAX) warned.delete(warned.values().next().value as string);
    warned.add(key);
    const why = r.over === "sections" ? "a file there has too many sections"
      : r.over === "cap" ? "a glob there is over the editor's limits"
        : r.over === "time" ? "it took too long to judge"
          : "a file there was too much work to judge before";
    console.warn(`.editorconfig not applied to ${path}: ${why}`);
  }
  return r;
}

async function readRecord(deps: EditorConfigDeps, projectId: string, path: string): Promise<ConfigRecord> {
  const rec: ConfigRecord = { text: null, file: null, strikes: 0, poisoned: null };
  try {
    const r = await deps.read(projectId, path);
    if (r && !r.protected && r.kind === "text" && typeof r.content === "string" && r.content.length <= FILE_MAX) {
      rec.text = r.content;
    }
  } catch {
    /* no configuration */
  }
  return rec;
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
