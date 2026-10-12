// Tree-sitter colour, the main thread's half (ED-5, editor plan §2.6). In the
// lazy Monaco chunk: `monacoBundle.ts` exports `applySyntax`, and
// `loadMonaco()` calls it once.
//
// What it does:
//  - registers a `DocumentRangeSemanticTokensProvider` for each Monaco
//    language a grammar covers (lib/syntax/grammars.ts). Monaco asks for the
//    visible lines (plus a screen above and below) on open, scroll and edit;
//    the provider asks the syntax worker and hands back its tokens. Monarch
//    stays underneath: until the worker answers, and wherever it does not,
//    Monarch's colour is what shows;
//  - sends each covered model's text to the worker the first time Monaco asks
//    for its colour, then every content change, as `{offset, length, text}`
//    with the model's version;
//  - keeps the budgets the main thread can see: a model past
//    `BUDGETS.maxChars` never reaches the worker (Monarch, "syntax: basic
//    (large file)"); the worker's own (a slow first parse, reparses cancelled
//    twice running) arrive as a state;
//  - draws that state in each editor's bottom-right corner — only when a
//    covered language is on Monarch, so the owner knows why it is quieter —
//    as text (`data-testid="syntax-status"`);
//  - turns `semanticHighlighting.enabled` on for every editor it sees (the
//    standalone theme leaves it off), without touching where editors are made.
//
// The worker is made by Monaco's `getWorker` with the label `SYNTAX_WORKER`
// (lib/monaco.ts): the one place the HUD makes a worker.

import type * as MonacoNS from "monaco-editor/editor";
import { BUDGETS, type BasicReason, type DocState } from "./budgets";
import { COVERED_LANGUAGES, grammarFor, type GrammarName } from "./grammars";
import { legend } from "./legend";

type Monaco = typeof MonacoNS;
type Model = MonacoNS.editor.ITextModel;

export interface SyntaxStatus {
  uri: string;
  language: string;
  grammar: GrammarName;
  state: DocState;
  reason?: BasicReason | "no worker";
  /** Token answers with data, applied or not (tests wait on this). */
  served: number;
}

interface Track extends SyntaxStatus {
  id: string;
  model: Model;
  opened: boolean;
  subs: { dispose(): void }[];
}

type Reply = {
  req: number;
  id: string;
  version: number;
  data: Uint32Array | null;
  state: DocState;
  reason?: BasicReason;
  truncatedAt?: number;
  stale?: boolean;
};

const REPLY_TIMEOUT_MS = 10_000;

export function applySyntax(monaco: Monaco, makeWorker: () => Worker) {
  let worker: Worker | null = null;
  let dead = false;
  let seq = 0;
  let ids = 0;
  const tracks = new Map<string, Track>();
  const byId = new Map<string, Track>();
  const waiting = new Map<number, (r: Reply | null) => void>();
  const probes = new Map<number, (r: { ok: boolean; error?: string }) => void>();
  const asks = new Map<number, (names: string[]) => void>();
  const changed = new monaco.Emitter<void>();
  const redraws = new Set<() => void>();

  const redraw = () => redraws.forEach((f) => f());

  function fail() {
    dead = true;
    worker?.terminate();
    worker = null;
    for (const t of tracks.values()) {
      if (t.state !== "basic") Object.assign(t, { state: "basic", reason: "no worker" });
    }
    waiting.forEach((resolve) => resolve(null));
    waiting.clear();
    probes.forEach((resolve) => resolve({ ok: false, error: "no syntax worker" }));
    probes.clear();
    asks.forEach((resolve) => resolve([]));
    asks.clear();
    changed.fire();
    redraw();
  }

  function post(m: unknown): boolean {
    if (dead) return false;
    if (!worker) {
      try {
        worker = makeWorker();
      } catch {
        fail();
        return false;
      }
      worker.onmessage = (ev: MessageEvent) => onMessage(ev.data);
      worker.onerror = () => fail();
    }
    worker.postMessage(m);
    return true;
  }

  function onMessage(m: any) {
    if (!m || typeof m !== "object") return;
    if (m.t === "tokens") {
      const resolve = waiting.get(m.req);
      waiting.delete(m.req);
      resolve?.(m as Reply);
      const t = byId.get(m.id);
      if (t && m.state === "basic" && t.state !== "basic") setState(t, "basic", m.reason);
    } else if (m.t === "state") {
      const t = byId.get(m.id);
      if (t) setState(t, m.state, m.reason);
    } else if (m.t === "changed") {
      if (byId.has(m.id)) changed.fire();
    } else if (m.t === "probe") {
      const resolve = probes.get(m.req);
      probes.delete(m.req);
      resolve?.({ ok: !!m.ok, ...(m.error ? { error: String(m.error) } : {}) });
    } else if (m.t === "loaded") {
      const resolve = asks.get(m.req);
      asks.delete(m.req);
      resolve?.(Array.isArray(m.names) ? m.names.map(String) : []);
    }
  }

  function setState(t: Track, state: DocState, reason?: Track["reason"]) {
    if (t.state === state && t.reason === reason) return;
    t.state = state;
    t.reason = state === "basic" ? reason : undefined;
    // On Monarch now: ask Monaco to draw again, so tokens already given are
    // cleared (an empty answer) rather than left to go stale.
    changed.fire();
    redraw();
  }

  function untrack(t: Track, tellWorker = true) {
    if (tellWorker && t.opened) post({ t: "close", id: t.id });
    t.subs.forEach((s) => s.dispose());
    tracks.delete(t.uri);
    byId.delete(t.id);
    redraw();
  }

  /** The model's track, opening it in the worker the first time. Null for a language tree-sitter does not colour. */
  function track(model: Model): Track | null {
    const uri = model.uri.toString();
    const have = tracks.get(uri);
    if (have && have.model === model) return have;
    if (have) untrack(have);
    const language = model.getLanguageId();
    const grammar = grammarFor(language, model.uri.path);
    if (!grammar) return null;
    const t: Track = {
      id: `m${++ids}`, uri, language, grammar, model, state: "loading", served: 0, opened: false, subs: [],
    };
    tracks.set(uri, t);
    byId.set(t.id, t);
    t.subs.push(
      model.onWillDispose(() => untrack(t)),
      model.onDidChangeLanguage(() => {
        untrack(t);
        changed.fire();
      }),
      model.onDidChangeContent((e) => {
        if (t.state === "basic") return;
        if (model.getValueLength() > BUDGETS.maxChars) {
          if (t.opened) post({ t: "close", id: t.id });
          t.opened = false;
          setState(t, "basic", "large file");
          return;
        }
        if (e.isFlush) open(t);
        else {
          post({
            t: "change", id: t.id, version: e.versionId,
            changes: e.changes.map((c) => ({ offset: c.rangeOffset, length: c.rangeLength, text: c.text })),
          });
        }
      }),
    );
    if (model.getValueLength() > BUDGETS.maxChars) setState(t, "basic", "large file");
    else open(t);
    redraw();
    return t;
  }

  function open(t: Track) {
    t.opened = post({
      t: "open", id: t.id, grammar: t.grammar, text: t.model.getValue(), version: t.model.getVersionId(),
      styledAs: t.language,
    });
    if (!t.opened && !dead) setState(t, "basic", "no worker");
  }

  const provider: MonacoNS.languages.DocumentRangeSemanticTokensProvider = {
    onDidChange: changed.event,
    getLegend: legend,
    async provideDocumentRangeSemanticTokens(model, range) {
      const t = track(model);
      if (!t) return null;
      // On Monarch: an empty answer clears whatever tree-sitter drew before.
      if (t.state === "basic" || !t.opened) return { data: new Uint32Array(0) };
      const req = ++seq;
      const version = model.getVersionId();
      const reply = new Promise<Reply | null>((resolve) => {
        waiting.set(req, resolve);
        setTimeout(() => {
          if (waiting.delete(req)) resolve(null);
        }, REPLY_TIMEOUT_MS);
      });
      if (!post({ t: "tokens", id: t.id, req, version, first: range.startLineNumber - 1, last: range.endLineNumber - 1 })) {
        waiting.delete(req);
        return { data: new Uint32Array(0) };
      }
      const r = await reply;
      if (!r || model.isDisposed() || model.getVersionId() !== r.version) return null;
      if (r.state === "basic") return { data: new Uint32Array(0) };
      if (!r.data) return null;
      t.served++;
      if (t.state === "loading") setState(t, "ready");
      return { data: r.data };
    },
  };
  for (const id of COVERED_LANGUAGES) monaco.languages.registerDocumentRangeSemanticTokensProvider(id, provider);

  // ---- every editor: semantic colour on, and the status corner -----------

  const enable = (ed: { updateOptions(o: object): void }) => {
    try {
      ed.updateOptions({ "semanticHighlighting.enabled": true });
    } catch {
      /* an editor that went away before its first task */
    }
  };
  monaco.editor.onDidCreateDiffEditor((diff) => queueMicrotask(() => enable(diff)));
  monaco.editor.onDidCreateEditor((ed) => {
    queueMicrotask(() => {
      enable(ed);
      statusCorner(monaco, ed, (model) => (model ? tracks.get(model.uri.toString()) ?? null : null), redraws);
    });
  });

  // ---- test hooks (read-only, and the loader's gate) ----------------------

  (window as any).__hudSyntax = {
    /** Every model tree-sitter has been asked about: its grammar and state. */
    states: (): SyntaxStatus[] =>
      [...tracks.values()].map(({ uri, language, grammar, state, reason, served }) => ({ uri, language, grammar, state, reason, served })),
    /**
     * The worker's loader asked to fetch `url`: a shipped grammar's URL loads,
     * any other is refused before a request is made.
     */
    probe: (url: string): Promise<{ ok: boolean; error?: string }> =>
      new Promise((resolve) => {
        const req = ++seq;
        probes.set(req, resolve);
        setTimeout(() => {
          if (probes.delete(req)) resolve({ ok: false, error: "no answer from the syntax worker" });
        }, REPLY_TIMEOUT_MS);
        if (!post({ t: "probe", req, url })) {
          probes.delete(req);
          resolve({ ok: false, error: "no syntax worker" });
        }
      }),
    /** The grammars the worker has compiled so far, by name; none if it never started. */
    loaded: (): Promise<string[]> =>
      new Promise((resolve) => {
        if (!worker) return resolve([]);
        const req = ++seq;
        asks.set(req, resolve);
        setTimeout(() => {
          if (asks.delete(req)) resolve([]);
        }, REPLY_TIMEOUT_MS);
        if (!post({ t: "loaded", req })) {
          asks.delete(req);
          resolve([]);
        }
      }),
  };
}

/** The bottom-right corner of one editor: "syntax: basic (<why>)" while its model is on Monarch. */
function statusCorner(
  monaco: Monaco,
  ed: MonacoNS.editor.ICodeEditor,
  stateOf: (model: Model | null) => SyntaxStatus | null,
  redraws: Set<() => void>,
) {
  const node = document.createElement("div");
  node.setAttribute("data-testid", "syntax-status");
  node.setAttribute("role", "status");
  Object.assign(node.style, {
    pointerEvents: "none",
    fontSize: "11px",
    lineHeight: "16px",
    padding: "0 6px",
    margin: "0 4px 4px 0",
    color: "var(--text-muted)",
    background: "var(--bg-2)",
    border: "1px solid var(--border)",
    borderRadius: "3px",
    whiteSpace: "nowrap",
  });
  const widget: MonacoNS.editor.IOverlayWidget = {
    getId: () => "jarvis.syntaxStatus",
    getDomNode: () => node,
    getPosition: () => ({ preference: monaco.editor.OverlayWidgetPositionPreference.BOTTOM_RIGHT_CORNER }),
  };
  const draw = () => {
    const s = stateOf(ed.getModel());
    const text = s && s.state === "basic" ? `syntax: basic${s.reason ? ` (${s.reason})` : ""}` : "";
    node.textContent = text;
    node.style.display = text ? "" : "none";
    node.setAttribute("data-state", s?.state ?? "off");
  };
  try {
    ed.addOverlayWidget(widget);
  } catch {
    return;
  }
  draw();
  redraws.add(draw);
  const sub = ed.onDidChangeModel(draw);
  ed.onDidDispose(() => {
    redraws.delete(draw);
    sub.dispose();
  });
}
