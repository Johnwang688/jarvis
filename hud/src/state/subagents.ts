// The window's native sub-agents (SV-3): outside React, like state/toolGroups,
// so one progress tick redraws one row (`useSyncExternalStore` per key).
//
// What it holds, and for whom:
//
//  - per thread a chat pane, a sub-agent pane or an approval card is
//    showing (`watch`): its children, read from the owner's list route when
//    first watched and again on every reconnect, and moved by the bus's
//    `subagent` records in between. A record says ids, a status, times and
//    counts only, so the list is read again behind it for the words — soon
//    for a new child or an end, at most every `LAZY_MS` for progress. Bus
//    records heard while a list read is on its way are laid over it when it
//    lands (`foldList`), so the read never takes a row back.
//  - per child a pane shows (`watchPane`): its transcript, read again behind
//    every record about it. Nothing else of a child's words is kept.
//
// Bounded: `CHILD_CAP` children per thread (lib/subagents), `THREAD_CAP`
// threads (the least recently used unwatched one goes), `REPLAY_CAP` records
// waiting on a read. Nothing here is stored: a reload reads it all again.

import { useEffect, useSyncExternalStore } from "react";
import {
  applyBus, cleanBus, cleanList, cleanTranscript, foldList,
  type SubBus, type Subagent, type SubagentRef, type SubTranscript,
} from "../lib/subagents";

export const THREAD_CAP = 32;
export const REPLAY_CAP = 256;
/** A new child or an end: its name and reason are read this soon after. */
export const SOON_MS = 250;
/** Progress (tokens, the last tool): read at most this often per thread. */
export const LAZY_MS = 1500;

/** Why a read failed, as the pane says it. */
export type Failure = "" | "gone" | "old" | "refused" | "error";
const failure = (e: unknown): Failure => {
  const status = e && typeof e === "object" && typeof (e as { status?: unknown }).status === "number"
    ? (e as { status: number }).status : 0;
  return status === 404 ? "gone" : status === 400 ? "old" : status === 403 ? "refused" : "error";
};

export interface Fetchers {
  list: (threadId: string) => Promise<unknown>;
  transcript: (threadId: string, sid: string) => Promise<unknown>;
}

interface Thread {
  rows: Subagent[];
  anchors: Subagent[];
  anchorKey: string;
  loaded: boolean;
  inflight: boolean;
  again: boolean;
  replay: SubBus[];
  failed: Failure;
  watchers: number;
  timer: ReturnType<typeof setTimeout> | null;
  due: number;
  used: number;
}

interface Pane {
  data: SubTranscript | null;
  failed: Failure;
  inflight: boolean;
  again: boolean;
  watchers: number;
  timer: ReturnType<typeof setTimeout> | null;
  due: number;
}

export interface PaneView { data: SubTranscript | null; failed: Failure; loading: boolean }
const NO_PANE: PaneView = { data: null, failed: "", loading: true };
const NO_ROWS: Subagent[] = [];

export function createSubagentStore(fetchers: Fetchers) {
  const threads = new Map<string, Thread>();
  const panes = new Map<string, Pane>();
  const paneViews = new Map<string, PaneView>();
  const listeners = new Map<string, Set<() => void>>();
  const blocks = new Map<string, true>();
  let tick = 0;
  let opener: ((ref: SubagentRef) => void) | null = null;

  const notify = (key: string) => { for (const fn of [...(listeners.get(key) ?? [])]) fn(); };
  const subscribe = (key: string, fn: () => void) => {
    if (!listeners.has(key)) listeners.set(key, new Set());
    listeners.get(key)!.add(fn);
    return () => { const set = listeners.get(key); set?.delete(fn); if (!set?.size) listeners.delete(key); };
  };
  const paneKey = (threadId: string, sid: string) => `${threadId}|${sid}`;

  function held(threadId: string): Thread {
    let t = threads.get(threadId);
    if (!t) {
      t = { rows: NO_ROWS, anchors: NO_ROWS, anchorKey: "", loaded: false, inflight: false, again: false,
        replay: [], failed: "", watchers: 0, timer: null, due: 0, used: 0 };
      threads.set(threadId, t);
    }
    t.used = ++tick;
    return t;
  }

  /** The least recently used thread nobody watches goes, past `THREAD_CAP`. */
  function trim() {
    while (threads.size > THREAD_CAP) {
      let oldest: string | null = null, at = Infinity;
      for (const [id, t] of threads) {
        const shown = t.watchers > 0 || [...panes.keys()].some((k) => k.startsWith(id + "|"));
        if (!shown && t.used < at) { oldest = id; at = t.used; }
      }
      if (oldest === null) return;
      const t = threads.get(oldest)!;
      if (t.timer) clearTimeout(t.timer);
      threads.delete(oldest);
    }
  }

  /** New rows for a thread: tell exactly the rows that changed, the thread, and its anchors if they moved. */
  function setRows(threadId: string, t: Thread, rows: Subagent[]) {
    if (rows === t.rows) return;
    const before = new Map(t.rows.map((r) => [r.sid, r]));
    t.rows = rows;
    const changed = rows.filter((r) => before.get(r.sid) !== r).map((r) => r.sid);
    for (const sid of before.keys()) if (!rows.some((r) => r.sid === sid)) changed.push(sid);
    const anchorKey = rows.map((r) => `${r.sid}\u0000${r.parent_sid ?? ""}\u0000${r.started_at}`).join("\u0001");
    if (anchorKey !== t.anchorKey) {
      t.anchorKey = anchorKey;
      t.anchors = rows;
      notify("a:" + threadId);
    }
    for (const sid of changed) notify(`r:${threadId}|${sid}`);
    notify("t:" + threadId);
  }

  async function load(threadId: string) {
    const t = threads.get(threadId);
    if (!t) return;
    if (t.inflight) { t.again = true; return; }
    t.inflight = true;
    t.again = false;
    t.replay = [];
    try {
      const list = cleanList(await fetchers.list(threadId));
      const now = threads.get(threadId);
      if (now !== t) return;
      t.failed = "";
      t.loaded = true;
      const replay = t.replay;
      t.replay = [];
      setRows(threadId, t, foldList(list, replay));
    } catch (e) {
      if (threads.get(threadId) !== t) return;
      t.failed = failure(e);
      t.loaded = true;
      t.replay = [];
      notify("t:" + threadId);
    } finally {
      t.inflight = false;
      if (t.again && threads.get(threadId) === t) schedule(threadId, SOON_MS);
    }
  }

  /** Read the list again in `ms` (or sooner, if a read is already due sooner). */
  function schedule(threadId: string, ms: number) {
    const t = threads.get(threadId);
    if (!t) return;
    const due = Date.now() + ms;
    if (t.timer && t.due <= due) return;
    if (t.timer) clearTimeout(t.timer);
    t.due = due;
    t.timer = setTimeout(() => { t.timer = null; void load(threadId); }, ms);
  }

  function publishPane(key: string, p: Pane | undefined) {
    paneViews.set(key, p ? { data: p.data, failed: p.failed, loading: !p.data && !p.failed } : NO_PANE);
    notify("x:" + key);
  }

  async function loadPane(threadId: string, sid: string) {
    const key = paneKey(threadId, sid);
    const p = panes.get(key);
    if (!p) return;
    if (p.inflight) { p.again = true; return; }
    p.inflight = true;
    p.again = false;
    try {
      const data = cleanTranscript(await fetchers.transcript(threadId, sid), sid);
      if (panes.get(key) !== p) return;
      p.data = data;
      p.failed = data ? "" : "error";
    } catch (e) {
      if (panes.get(key) !== p) return;
      p.failed = failure(e);
    } finally {
      p.inflight = false;
      if (panes.get(key) === p) {
        publishPane(key, p);
        if (p.again) schedulePane(threadId, sid, SOON_MS);
      }
    }
  }

  /** Read a shown child's transcript again in `ms` (or sooner, if a read is already due sooner). */
  function schedulePane(threadId: string, sid: string, ms = SOON_MS) {
    const p = panes.get(paneKey(threadId, sid));
    if (!p) return;
    const due = Date.now() + ms;
    if (p.timer && p.due <= due) return;
    if (p.timer) clearTimeout(p.timer);
    p.due = due;
    p.timer = setTimeout(() => { p.timer = null; void loadPane(threadId, sid); }, ms);
  }

  return {
    subscribe,
    /** Hold `threadId`'s children while something shows them; read them the first time. */
    watch(threadId: string): () => void {
      const t = held(threadId);
      t.watchers += 1;
      trim();
      if (!t.loaded && !t.inflight) void load(threadId);
      let done = false;
      return () => {
        if (done) return;
        done = true;
        t.watchers = Math.max(0, t.watchers - 1);
        trim();
      };
    },
    /** A bus `subagent` record (`data`) for `threadId`. Ignored for a thread nothing shows. */
    heard(threadId: string, data: unknown) {
      const t = threads.get(threadId);
      const rec = cleanBus(data);
      if (!t || !rec) return;
      if (t.watchers <= 0 && ![...panes.keys()].some((k) => k.startsWith(threadId + "|"))) {
        // Nothing shows it: no work for it now, and the next `watch` reads
        // it whole again (review of #212, LOW-2).
        t.loaded = false;
        return;
      }
      const known = t.rows.some((r) => r.sid === rec.sid);
      if (t.inflight) {
        t.replay.push(rec);
        if (t.replay.length > REPLAY_CAP) {
          // Too much to lay over the read: read again once it lands.
          t.replay.splice(0, t.replay.length - REPLAY_CAP);
          t.again = true;
        }
      }
      setRows(threadId, t, applyBus(t.rows, rec));
      const soon = !known || rec.op !== "progress";
      schedule(threadId, soon ? SOON_MS : LAZY_MS);
      // A shown child's pane: soon for a start or an end, at most every
      // LAZY_MS for progress (review of #212, LOW-3).
      if (panes.has(paneKey(threadId, rec.sid))) schedulePane(threadId, rec.sid, soon ? SOON_MS : LAZY_MS);
    },
    /** The event stream came back: whatever was said in the gap is read again. */
    reconnect() {
      for (const [id, t] of threads) {
        if (t.watchers > 0) void load(id);
        else t.loaded = false;
      }
      for (const key of panes.keys()) {
        const at = key.indexOf("|");
        void loadPane(key.slice(0, at), key.slice(at + 1));
      }
    },
    rows: (threadId: string | null | undefined): Subagent[] => (threadId && threads.get(threadId)?.rows) || NO_ROWS,
    anchors: (threadId: string | null | undefined): Subagent[] => (threadId && threads.get(threadId)?.anchors) || NO_ROWS,
    row: (threadId: string | null | undefined, sid: string): Subagent | undefined =>
      threadId ? threads.get(threadId)?.rows.find((r) => r.sid === sid) : undefined,
    failed: (threadId: string): Failure => threads.get(threadId)?.failed ?? "",
    /** Hold a child's transcript while a pane shows it, read again behind every record about it. */
    watchPane(threadId: string, sid: string): () => void {
      const key = paneKey(threadId, sid);
      let p = panes.get(key);
      if (!p) {
        p = { data: null, failed: "", inflight: false, again: false, watchers: 0, timer: null, due: 0 };
        panes.set(key, p);
        publishPane(key, p);
        void loadPane(threadId, sid);
      }
      p.watchers += 1;
      const mine = p;
      let done = false;
      return () => {
        if (done) return;
        done = true;
        mine.watchers -= 1;
        if (mine.watchers <= 0 && panes.get(key) === mine) {
          if (mine.timer) clearTimeout(mine.timer);
          panes.delete(key);
          paneViews.delete(key);
          trim();
        }
      };
    },
    pane: (threadId: string, sid: string): PaneView => paneViews.get(paneKey(threadId, sid)) ?? NO_PANE,
    /** A run of more than six rows: whether the owner opened it. Window-local, never stored. */
    blockOpen: (key: string) => blocks.has(key),
    toggleBlock(key: string) {
      if (blocks.has(key)) blocks.delete(key);
      else {
        blocks.set(key, true);
        if (blocks.size > 200) blocks.delete(blocks.keys().next().value!);
      }
      notify("b:" + key);
    },
    /** The window's way to open a child's pane (App sets it; rows, cards and panes call it). */
    setOpener(fn: ((ref: SubagentRef) => void) | null) { opener = fn; },
    open(ref: SubagentRef) { opener?.(ref); },
    /** Read-only, for checks: what is held, never a child's words. */
    snapshot: () => ({
      threads: Object.fromEntries([...threads].map(([id, t]) => [id, {
        watchers: t.watchers, loaded: t.loaded, failed: t.failed,
        rows: t.rows.map((r) => ({ sid: r.sid, status: r.status, entries: r.entries, parent_sid: r.parent_sid ?? null })),
      }])),
      panes: [...panes.keys()],
    }),
  };
}

export type SubagentStore = ReturnType<typeof createSubagentStore>;

async function getJson(path: string): Promise<unknown> {
  const res = await fetch(path);
  const text = await res.text();
  let body: unknown = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = null; }
  if (!res.ok) {
    const message = body && typeof body === "object" && typeof (body as { error?: unknown }).error === "string"
      ? (body as { error: string }).error : `HTTP ${res.status}`;
    throw Object.assign(new Error(message), { status: res.status });
  }
  return body;
}

/** The window's store, on the owner's two read-only routes. */
export const subagents = createSubagentStore({
  list: (threadId) => getJson(`/threads/${encodeURIComponent(threadId)}/subagents`),
  transcript: (threadId, sid) =>
    getJson(`/threads/${encodeURIComponent(threadId)}/subagents/${encodeURIComponent(sid)}/transcript`),
});

// ---- hooks ------------------------------------------------------------------

/** Hold a thread's children while the caller is mounted. */
export function useWatch(threadId: string | null | undefined, store: SubagentStore = subagents) {
  useEffect(() => (threadId ? store.watch(threadId) : undefined), [threadId, store]);
}

/** The rows a transcript places: the same array until a child comes, goes or moves. */
export function useAnchors(threadId: string | null | undefined, store: SubagentStore = subagents): Subagent[] {
  return useSyncExternalStore(
    (fn) => (threadId ? store.subscribe("a:" + threadId, fn) : () => {}),
    () => store.anchors(threadId),
  );
}

/** Every row of a thread (any change). */
export function useRows(threadId: string | null | undefined, store: SubagentStore = subagents): Subagent[] {
  return useSyncExternalStore(
    (fn) => (threadId ? store.subscribe("t:" + threadId, fn) : () => {}),
    () => store.rows(threadId),
  );
}

/** One row: redrawn when it, and only it, changes. */
export function useRow(threadId: string | null | undefined, sid: string, store: SubagentStore = subagents) {
  return useSyncExternalStore(
    (fn) => (threadId ? store.subscribe(`r:${threadId}|${sid}`, fn) : () => {}),
    () => store.row(threadId, sid),
  );
}

/** A child's transcript, held while the caller is mounted. */
export function usePane(threadId: string, sid: string, store: SubagentStore = subagents): PaneView {
  useEffect(() => store.watchPane(threadId, sid), [threadId, sid, store]);
  return useSyncExternalStore(
    (fn) => store.subscribe(`x:${threadId}|${sid}`, fn),
    () => store.pane(threadId, sid),
  );
}

export function useBlockOpen(key: string, store: SubagentStore = subagents): boolean {
  return useSyncExternalStore((fn) => store.subscribe("b:" + key, fn), () => store.blockOpen(key));
}
