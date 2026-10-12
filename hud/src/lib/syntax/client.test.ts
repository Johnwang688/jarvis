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
  const monaco: any = {
    Emitter,
    languages: {
      registerDocumentRangeSemanticTokensProvider: (_id: string, p: any) => {
        provider = p;
        return { dispose() {} };
      },
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
  return { provider: provider as any, created, worker, states, redraws: () => redrawn };
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
