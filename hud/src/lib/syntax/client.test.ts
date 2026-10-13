// The syntax client's own guards (ED-5; review of PR #54), on a stand-in
// Monaco and a stand-in worker: a worker that stops answering is treated as
// dead (Monarch everywhere, said in each editor's corner, nothing of
// tree-sitter's left drawn), and a model past the size budget is never sent to
// the worker at all.

import { applySyntax, REPLY_TIMEOUT_MS, type SyntaxStatus } from "./client";
import { BUDGETS } from "./budgets";

class Emitter<T> {
  private listeners = new Set<(v: T) => void>();
  event = (l: (v: T) => void) => {
    this.listeners.add(l);
    return { dispose: () => this.listeners.delete(l) };
  };
  fire(v?: T) {
    this.listeners.forEach((l) => l(v as T));
  }
}

/** A worker that takes messages and never answers. */
class SilentWorker {
  posted: any[] = [];
  terminated = false;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  postMessage(m: unknown) {
    this.posted.push(m);
  }
  terminate() {
    this.terminated = true;
  }
}

function fakeModel(path: string, language: string, text: string) {
  const subs = () => ({ dispose() {} });
  return {
    uri: { toString: () => `file://${path}`, path },
    getLanguageId: () => language,
    getValueLength: () => text.length,
    getValue: () => text,
    getVersionId: () => 1,
    isDisposed: () => false,
    onWillDispose: subs,
    onDidChangeLanguage: subs,
    onDidChangeContent: subs,
  };
}

function setup() {
  let provider: any = null;
  const created = new Emitter<any>();
  const worker = new SilentWorker();
  const foldProviders = new Map<string, any>();
  const monaco: any = {
    Emitter,
    languages: {
      registerDocumentRangeSemanticTokensProvider: (_id: string, p: any) => {
        provider = p;
        return { dispose() {} };
      },
      registerFoldingRangeProvider: (id: string, p: any) => {
        foldProviders.set(id, p);
        return { dispose() {} };
      },
      FoldingRangeKind: { Comment: { value: "comment" }, Imports: { value: "imports" }, Region: { value: "region" } },
    },
    editor: {
      onDidCreateEditor: created.event,
      onDidCreateDiffEditor: () => ({ dispose() {} }),
      OverlayWidgetPositionPreference: { BOTTOM_RIGHT_CORNER: 1 },
    },
  };
  applySyntax(monaco, () => worker as unknown as Worker);
  let redrawn = 0;
  provider.onDidChange(() => redrawn++);
  const states = (): SyntaxStatus[] => (window as any).__hudSyntax.states();
  return { provider: provider as any, created, worker, states, redraws: () => redrawn, foldProviders, monaco };
}

/** An editor showing `model`, its status corner captured. */
function fakeEditor(model: unknown) {
  let node: HTMLElement | null = null;
  return {
    corner: () => node,
    ed: {
      updateOptions() {},
      getModel: () => model,
      addOverlayWidget: (w: { getDomNode(): HTMLElement }) => {
        node = w.getDomNode();
      },
      onDidChangeModel: () => ({ dispose() {} }),
      onDidDispose: () => ({ dispose() {} }),
    },
  };
}

const range = { startLineNumber: 1, endLineNumber: 10 };

describe("a worker that never answers is treated as dead (review of PR #54, LOW 2)", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("after the reply timeout: Monarch for every model, the corner says why, and nothing stale stays drawn", async () => {
    const s = setup();
    const model = fakeModel("/p/a.cpp", "cpp", "int main() {}\n");
    const other = fakeModel("/p/b.py", "python", "x = 1\n");
    const view = fakeEditor(model);
    s.created.fire(view.ed);
    await Promise.resolve(); // the editor's queued setup
    const pending = s.provider.provideDocumentRangeSemanticTokens(model, range);
    void s.provider.provideDocumentRangeSemanticTokens(other, range);
    expect(s.worker.posted.filter((m) => m.t === "tokens")).toHaveLength(2);
    expect(s.states().map((x) => x.state)).toEqual(["loading", "loading"]);
    expect(view.corner()?.textContent).toBe("");

    const before = s.redraws();
    await vi.advanceTimersByTimeAsync(REPLY_TIMEOUT_MS + 1);
    // An empty answer, not null: null keeps whatever tree-sitter drew before.
    expect(await pending).toEqual({ data: new Uint32Array(0) });
    expect(s.worker.terminated).toBe(true);
    expect(s.states().map((x) => [x.state, x.reason])).toEqual([["basic", "no worker"], ["basic", "no worker"]]);
    expect(s.redraws()).toBeGreaterThan(before); // Monaco asked to draw again
    expect(view.corner()?.textContent).toBe("syntax: basic (no worker)");
    // Asked again, it answers empty at once and sends the dead worker nothing.
    const sent = s.worker.posted.length;
    expect(await s.provider.provideDocumentRangeSemanticTokens(model, range)).toEqual({ data: new Uint32Array(0) });
    expect(s.worker.posted).toHaveLength(sent);
  });

  it("an answer in time keeps the worker", async () => {
    const s = setup();
    const model = fakeModel("/p/a.cpp", "cpp", "int main() {}\n");
    const pending = s.provider.provideDocumentRangeSemanticTokens(model, range);
    await vi.advanceTimersByTimeAsync(REPLY_TIMEOUT_MS - 1000);
    const req = s.worker.posted.find((m) => m.t === "tokens");
    const id = s.worker.posted.find((m) => m.t === "open").id;
    s.worker.onmessage?.({ data: { t: "tokens", req: req.req, id, version: 1, data: new Uint32Array([0, 0, 3, 1, 0]), state: "ready" } } as MessageEvent);
    expect(await pending).toEqual({ data: new Uint32Array([0, 0, 3, 1, 0]) });
    await vi.advanceTimersByTimeAsync(REPLY_TIMEOUT_MS * 2);
    expect(s.worker.terminated).toBe(false);
    expect(s.states()[0]).toMatchObject({ state: "ready", served: 1 });
  });
});

describe("a model past the size budget is never sent to the worker (review of PR #54, LOW 1)", () => {
  it("no open, no text posted, opens 0, and it says why", async () => {
    const s = setup();
    const big = fakeModel("/p/big.py", "python", "x".repeat(BUDGETS.maxChars + 1));
    const small = fakeModel("/p/small.py", "python", "x = 1\n");
    const answer = s.provider.provideDocumentRangeSemanticTokens(big, range);
    void s.provider.provideDocumentRangeSemanticTokens(small, range);
    // At once: it never waits on a worker that was never asked.
    const soon = new Promise((r) => setTimeout(() => r("still waiting"), 50));
    expect(await Promise.race([answer, soon])).toEqual({ data: new Uint32Array(0) });
    const opens = s.worker.posted.filter((m) => m.t === "open");
    expect(opens).toHaveLength(1);
    expect(opens[0].text).toBe("x = 1\n");
    const byPath = Object.fromEntries(s.states().map((x) => [x.uri.split("/").pop(), x]));
    expect(byPath["big.py"]).toMatchObject({ state: "basic", reason: "large file", opens: 0, served: 0 });
    expect(byPath["small.py"]).toMatchObject({ opens: 1 });
  });
});

// ---- folds (ED-6) -----------------------------------------------------------

const noCancel = { isCancellationRequested: false };

/** The worker's answer to the last folds request it was sent. */
function answerFolds(s: ReturnType<typeof setup>, reply: Record<string, unknown>, nth = -1) {
  const asks = s.worker.posted.filter((m) => m.t === "folds");
  const ask = asks.at(nth);
  s.worker.onmessage?.({ data: { t: "folds", req: ask.req, id: ask.id, state: "ready", ...reply } } as MessageEvent);
  return ask;
}

describe("folds from the tree: a FoldingRangeProvider whose null means indentation (ED-6)", () => {
  it("is registered for exactly the languages tree-sitter colours, so an env file (dotenv) never has one", async () => {
    const { COVERED_LANGUAGES } = await import("./grammars");
    const s = setup();
    expect([...s.foldProviders.keys()].sort()).toEqual([...COVERED_LANGUAGES].sort());
    expect(s.foldProviders.has("dotenv")).toBe(false);
    // And should Monaco ever ask about one anyway: null (indentation), nothing sent.
    const env = fakeModel("/p/.env", "dotenv", "KEY=value\n");
    expect(await s.foldProviders.get("cpp").provideFoldingRanges(env, {}, noCancel)).toBeNull();
    expect(s.worker.posted).toEqual([]);
  });

  it("asks the worker for the model's version and hands Monaco its ranges, with Monaco's kinds; answers matched by request number", async () => {
    const s = setup();
    const a = fakeModel("/p/a.cpp", "cpp", "int main() {\n  return 0;\n}\n");
    const b = fakeModel("/p/b.py", "python", "def f():\n    pass\n");
    const pa = s.foldProviders.get("cpp").provideFoldingRanges(a, {}, noCancel);
    const pb = s.foldProviders.get("python").provideFoldingRanges(b, {}, noCancel);
    const asks = s.worker.posted.filter((m) => m.t === "folds");
    expect(asks.map((m) => [m.version, typeof m.req])).toEqual([[1, "number"], [1, "number"]]);
    expect(s.worker.posted.filter((m) => m.t === "open")).toHaveLength(2);
    // Answered out of order: each goes to its own request.
    answerFolds(s, { version: 1, ranges: [{ start: 1, end: 2 }] }, 1);
    answerFolds(s, { version: 1, ranges: [{ start: 1, end: 2, kind: "imports" }, { start: 4, end: 6, kind: "comment" }] }, 0);
    expect(await pa).toEqual([
      { start: 1, end: 2, kind: s.monaco.languages.FoldingRangeKind.Imports },
      { start: 4, end: 6, kind: s.monaco.languages.FoldingRangeKind.Comment },
    ]);
    expect(await pb).toEqual([{ start: 1, end: 2 }]);
    expect(s.states().map((x) => x.folds)).toEqual([1, 1]);
  });

  it("drops a stale answer (the model moved on) and a null one (over budget): null, so Monaco folds by indentation", async () => {
    const s = setup();
    let version = 1;
    const m = { ...fakeModel("/p/a.cpp", "cpp", "int main() {}\n"), getVersionId: () => version };
    const p1 = s.foldProviders.get("cpp").provideFoldingRanges(m, {}, noCancel);
    version = 2;
    answerFolds(s, { version: 1, ranges: [{ start: 1, end: 2 }] });
    expect(await p1).toBeNull();
    const p2 = s.foldProviders.get("cpp").provideFoldingRanges(m, {}, noCancel);
    answerFolds(s, { version: 2, ranges: null });
    expect(await p2).toBeNull();
    expect(s.states()[0]).toMatchObject({ folds: 0, state: "loading" });
  });

  it("a model sent back to Monarch folds by indentation: the answer's state, then every later ask at once", async () => {
    const s = setup();
    const m = fakeModel("/p/a.cpp", "cpp", "int main() {}\n");
    const p = s.foldProviders.get("cpp").provideFoldingRanges(m, {}, noCancel);
    answerFolds(s, { version: 1, ranges: null, state: "basic", reason: "slow parse" });
    expect(await p).toBeNull();
    expect(s.states()[0]).toMatchObject({ state: "basic", reason: "slow parse" });
    const sent = s.worker.posted.length;
    expect(await s.foldProviders.get("cpp").provideFoldingRanges(m, {}, noCancel)).toBeNull();
    expect(s.worker.posted).toHaveLength(sent);
  });

  it("a model past the size budget folds by indentation, at once, its text never sent", async () => {
    const s = setup();
    const big = fakeModel("/p/big.py", "python", "x".repeat(BUDGETS.maxChars + 1));
    expect(await s.foldProviders.get("python").provideFoldingRanges(big, {}, noCancel)).toBeNull();
    expect(s.worker.posted).toEqual([]);
    expect(s.states()[0]).toMatchObject({ state: "basic", reason: "large file", opens: 0, folds: 0 });
  });

  describe("a worker that never answers a folds request", () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => vi.useRealTimers());

    it("is dead after the reply timeout: null (indentation), Monarch everywhere, and nothing more is sent", async () => {
      const s = setup();
      const m = fakeModel("/p/a.cpp", "cpp", "int main() {}\n");
      const p = s.foldProviders.get("cpp").provideFoldingRanges(m, {}, noCancel);
      const before = s.redraws();
      await vi.advanceTimersByTimeAsync(REPLY_TIMEOUT_MS + 1);
      expect(await p).toBeNull();
      expect(s.worker.terminated).toBe(true);
      expect(s.states()[0]).toMatchObject({ state: "basic", reason: "no worker" });
      expect(s.redraws()).toBeGreaterThan(before); // Monaco asked to fold (and colour) again
      const sent = s.worker.posted.length;
      expect(await s.foldProviders.get("cpp").provideFoldingRanges(m, {}, noCancel)).toBeNull();
      expect(s.worker.posted).toHaveLength(sent);
    });

    it("a tokens request timing out ends a folds request waiting with it", async () => {
      const s = setup();
      const m = fakeModel("/p/a.cpp", "cpp", "int main() {}\n");
      const tokens = s.provider.provideDocumentRangeSemanticTokens(m, range);
      await vi.advanceTimersByTimeAsync(REPLY_TIMEOUT_MS / 2);
      const folds = s.foldProviders.get("cpp").provideFoldingRanges(m, {}, noCancel);
      await vi.advanceTimersByTimeAsync(REPLY_TIMEOUT_MS / 2 + 1);
      expect(await tokens).toEqual({ data: new Uint32Array(0) });
      expect(await folds).toBeNull();
    });
  });
});
