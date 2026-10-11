/// <reference lib="webworker" />
// The syntax worker (ED-5, editor plan §2.6): tree-sitter, off the main
// thread. It holds each open model's text, tree and parser, reparses on edits
// (coalesced, within budget), and answers token requests for a range of
// lines. All of the work is `SyntaxEngine`'s (engine.ts); this file is the
// messages and the timers.
//
// It is created only through Monaco's `getWorker`, by its own label
// (`SYNTAX_WORKER`, lib/syntax/client.ts) — the one place the HUD makes a
// worker, which still refuses every language-service label (decisions E-1).
//
// Messages in (from client.ts):
//   {t: "open", id, grammar, text, version, styledAs}
//   {t: "change", id, version, changes: [{offset, length, text}]}
//   {t: "tokens", id, req, version, first, last}
//   {t: "close", id}
//   {t: "probe", req, url}      — the loader's gate, asked about one URL (tests)
// Messages out:
//   {t: "state", id, version, state, reason?}
//   {t: "tokens", req, id, version, data, state, reason?, truncatedAt?, stale?}
//   {t: "changed", id}           — colour may have changed with no edit: ask again
//   {t: "probe", req, ok, error?}

import { Language, Parser, Query } from "web-tree-sitter";
import { fetchShipped, QUERIES, RUNTIME_URL, shippedUrl, WASM_URLS } from "./assets";
import { SyntaxEngine, type GrammarAssets } from "./engine";
import type { GrammarName } from "./grammars";
import type { Change } from "./positions";

type In =
  | { t: "open"; id: string; grammar: GrammarName; text: string; version: number; styledAs: string }
  | { t: "change"; id: string; version: number; changes: Change[] }
  | { t: "tokens"; id: string; req: number; version: number; first: number; last: number }
  | { t: "close"; id: string }
  | { t: "probe"; req: number; url: string };

const post = (m: unknown, transfer: Transferable[] = []) => (self as unknown as DedicatedWorkerGlobalScope).postMessage(m, transfer);

let engine: Promise<SyntaxEngine> | null = null;

function start(): Promise<SyntaxEngine> {
  engine ??= (async () => {
    // The runtime's own .wasm through the same gate as the grammars; and no
    // other file may be located (a side module's dependencies included).
    const wasmBinary = await fetchShipped(RUNTIME_URL);
    await Parser.init({
      wasmBinary,
      locateFile: (path: string) => {
        throw new Error(`the syntax worker loads nothing by name (${path})`);
      },
    } as never);
    return new SyntaxEngine({
      load: async (g: GrammarName): Promise<GrammarAssets> => {
        const language = await Language.load(await fetchShipped(WASM_URLS[g]));
        const q = QUERIES[g];
        return {
          language,
          highlights: new Query(language, q.highlights),
          injections: /\(/.test(q.injections.replace(/;[^\n]*/g, "")) ? new Query(language, q.injections) : null,
        };
      },
      changed: (id) => post({ t: "changed", id }),
    });
  })();
  return engine;
}

// Per document: the coalescing timer, the retry timer, and requests waiting
// for its first parse.
const opening = new Map<string, Promise<unknown>>();
const coalesce = new Map<string, ReturnType<typeof setTimeout>>();
const retry = new Map<string, ReturnType<typeof setTimeout>>();

function clearTimers(id: string) {
  clearTimeout(coalesce.get(id));
  clearTimeout(retry.get(id));
  coalesce.delete(id);
  retry.delete(id);
}

/** Cancelled: the last tree is kept; try again once the edits pause. */
function scheduleRetry(e: SyntaxEngine, id: string) {
  clearTimeout(retry.get(id));
  retry.set(id, setTimeout(() => {
    retry.delete(id);
    if (reparse(e, id) === "ok") post({ t: "changed", id });
  }, e.budgets.retryIdleMs));
}

function reparse(e: SyntaxEngine, id: string) {
  const r = e.reparse(id);
  if (r === "stale") scheduleRetry(e, id);
  else if (r === "basic") {
    const s = e.state(id);
    post({ t: "state", id, version: s?.version ?? -1, state: "basic", reason: s?.reason });
  }
  return r;
}

async function handle(m: In) {
  if (m.t === "probe") {
    try {
      shippedUrl(m.url, self.location.href);
      post({ t: "probe", req: m.req, ok: true });
    } catch (err) {
      post({ t: "probe", req: m.req, ok: false, error: err instanceof Error ? err.message : String(err) });
    }
    return;
  }
  const e = await start();
  switch (m.t) {
    case "open": {
      clearTimers(m.id);
      const p = e.open(m.id, m.grammar, m.text, m.version, m.styledAs).then((s) => {
        const now = e.state(m.id);
        if (now) post({ t: "state", id: m.id, version: now.version, state: s.state, reason: s.reason });
      });
      opening.set(m.id, p);
      await p;
      if (opening.get(m.id) === p) opening.delete(m.id);
      return;
    }
    case "change": {
      const before = e.state(m.id)?.state;
      const s = e.change(m.id, m.version, m.changes);
      if (s && s.state === "basic" && before !== "basic") post({ t: "state", id: m.id, version: m.version, state: "basic", reason: s.reason });
      clearTimeout(coalesce.get(m.id));
      clearTimeout(retry.get(m.id));
      coalesce.set(m.id, setTimeout(() => {
        coalesce.delete(m.id);
        reparse(e, m.id);
      }, e.budgets.coalesceMs));
      return;
    }
    case "tokens": {
      await opening.get(m.id);
      clearTimeout(coalesce.get(m.id));
      coalesce.delete(m.id);
      const r = e.tokens(m.id, m.version, m.first, m.last);
      if (r.stale) scheduleRetry(e, m.id);
      if (r.state === "basic" && r.reason === "slow edits") post({ t: "state", id: m.id, version: r.version, state: "basic", reason: r.reason });
      post({ t: "tokens", req: m.req, id: m.id, ...r }, r.data ? [r.data.buffer] : []);
      return;
    }
    case "close":
      clearTimers(m.id);
      opening.delete(m.id);
      e.close(m.id);
      return;
  }
}

// One message at a time, in order: an edit is never applied while a token
// request that predates it is half answered.
let queue: Promise<void> = Promise.resolve();
self.addEventListener("message", (ev: MessageEvent<In>) => {
  const m = ev.data;
  queue = queue.then(() => handle(m)).catch((err) => {
    if (m && m.t === "tokens") post({ t: "tokens", req: m.req, id: m.id, version: -1, data: null, state: "basic", reason: "grammar failed" });
    console.error("syntax worker:", err instanceof Error ? err.message : err);
  });
});
