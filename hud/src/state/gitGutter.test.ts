import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GitGutter, MAX_LINES, type GutterModel } from "./gitGutter";
import { GitStatusStore } from "./gitStatus";
import { splitLines, type GutterMark } from "../lib/linediff";
import type { GitBase, GitStatus } from "../types";

/** A model the way Monaco's behaves for this: lines, a version, change events. */
class FakeModel implements GutterModel {
  private fns = new Set<() => void>();
  reads = 0;
  constructor(private text: string) {}
  set(text: string) {
    this.text = text;
    for (const f of Array.from(this.fns)) f();
  }
  onDidChangeContent(fn: () => void) {
    this.fns.add(fn);
    return { dispose: () => void this.fns.delete(fn) };
  }
  getValue() {
    return this.text;
  }
  getLinesContent() {
    this.reads += 1;
    return splitLines(this.text);
  }
  getLineCount() {
    return splitLines(this.text).length;
  }
  isDisposed() {
    return false;
  }
  listeners() {
    return this.fns.size;
  }
}

const HEAD = "int main() {\n  return 0;\n}\n";

function world(opts: { base?: (path: string) => Promise<GitBase>; head?: string } = {}) {
  let head = opts.head ?? "c1";
  const asked: string[] = [];
  const store = new GitStatusStore({
    fetch: async (): Promise<GitStatus> => ({ repo: true, head, branch: "main", files: {}, dirs: {}, truncated: false }),
    now: () => Date.now(),
    visible: () => true,
  }, 1_000);
  const gutter = new GitGutter({
    base: (projectId, path) => {
      asked.push(`${projectId}:${path}`);
      return opts.base ? opts.base(path) : Promise.resolve({ path, state: "ok", head, text: HEAD });
    },
    status: store,
  });
  return { gutter, store, asked, setHead: (h: string) => (head = h) };
}

const settle = async () => {
  for (let i = 0; i < 5; i++) await Promise.resolve();
};

describe("the git gutter", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("follows an unsaved edit, debounced, and is clean again once it is undone", async () => {
    const { gutter } = world();
    const model = new FakeModel(HEAD);
    const seen: GutterMark[][] = [];
    const stop = gutter.watch({ abs: "/p/main.cpp", projectId: "p1", path: "main.cpp" }, model, (m) => seen.push(m));
    await settle();
    expect(seen).toEqual([]); // equal to HEAD: nothing to draw, nothing said
    model.set(HEAD.replace("  return 0;\n", "  return 0;\n  // tuned\n"));
    model.set(HEAD.replace("  return 0;\n", "  return 0;\n  // tuned at the lab\n"));
    expect(seen).toEqual([]); // not per keystroke
    const reads = model.reads;
    vi.advanceTimersByTime(200);
    expect(model.reads).toBe(reads + 1); // one read of the lines per pause
    expect(seen.at(-1)).toEqual([{ kind: "added", start: 3, end: 3 }]);
    model.set(HEAD.replace("return 0", "return 1"));
    vi.advanceTimersByTime(200);
    expect(seen.at(-1)).toEqual([{ kind: "modified", start: 2, end: 2 }]);
    model.set(HEAD); // undo
    vi.advanceTimersByTime(200);
    expect(seen.at(-1)).toEqual([]);
    stop();
    expect(model.listeners()).toBe(0);
  });

  it("one per buffer: a second pane hears the marks at once, and it goes when both stop", async () => {
    const { gutter, asked } = world();
    const model = new FakeModel(HEAD + "// more\n");
    const a: GutterMark[][] = [];
    const b: GutterMark[][] = [];
    const t = { abs: "/p/main.cpp", projectId: "p1", path: "main.cpp" };
    const stopA = gutter.watch(t, model, (m) => a.push(m));
    await settle();
    expect(a.at(-1)).toEqual([{ kind: "added", start: 4, end: 4 }]);
    const stopB = gutter.watch(t, model, (m) => b.push(m));
    expect(b).toEqual([[{ kind: "added", start: 4, end: 4 }]]);
    expect(asked).toEqual(["p1:main.cpp"]);
    expect(gutter.snapshot()).toHaveLength(1);
    stopA();
    expect(gutter.snapshot()).toHaveLength(1);
    stopB();
    expect(gutter.snapshot()).toHaveLength(0);
    expect(model.listeners()).toBe(0);
  });

  it("HEAD's text is asked for again only when the status poll says HEAD moved", async () => {
    const w = world();
    const model = new FakeModel(HEAD);
    const stop = w.gutter.watch({ abs: "/p/main.cpp", projectId: "p1", path: "main.cpp" }, model, () => {});
    await settle();
    vi.advanceTimersByTime(1_000);
    await settle();
    vi.advanceTimersByTime(1_000);
    await settle();
    expect(w.asked).toHaveLength(1);
    w.setHead("c2");
    vi.advanceTimersByTime(1_000);
    await settle();
    expect(w.asked).toHaveLength(2);
    stop();
  });

  it("no gutter when the daemon refuses, has no HEAD text, or the file is too long", async () => {
    for (const base of [
      () => Promise.reject(Object.assign(new Error("an env file"), { status: 403 })),
      (path: string) => Promise.resolve({ path, state: "new", head: "c1", text: null }),
      (path: string) => Promise.resolve({ path, state: "binary", head: "c1", text: null }),
    ]) {
      const { gutter } = world({ base });
      const model = new FakeModel("anything\n");
      const seen: GutterMark[][] = [];
      gutter.watch({ abs: "/p/x", projectId: "p1", path: "x" }, model, (m) => seen.push(m));
      await settle();
      model.set("changed\n");
      vi.advanceTimersByTime(600);
      expect(seen).toEqual([]);
      expect(model.reads).toBe(0); // with no HEAD text the buffer is never read
    }
    const long = "x\n".repeat(MAX_LINES + 1);
    const { gutter } = world({ base: (path) => Promise.resolve({ path, state: "ok", head: "c1", text: long }) });
    const seen: GutterMark[][] = [];
    gutter.watch({ abs: "/p/y", projectId: "p1", path: "y" }, new FakeModel(long + "more\n"), (m) => seen.push(m));
    await settle();
    expect(seen).toEqual([]);
  });

  it("a late answer for a gutter nobody watches any more is dropped", async () => {
    let answer: (b: GitBase) => void = () => {};
    const { gutter } = world({ base: () => new Promise((r) => (answer = r)) });
    const seen: GutterMark[][] = [];
    const stop = gutter.watch({ abs: "/p/x", projectId: "p1", path: "x" }, new FakeModel("new\n"), (m) => seen.push(m));
    stop();
    answer({ path: "x", state: "ok", head: "c1", text: "old\n" });
    await settle();
    expect(seen).toEqual([]);
  });
});

describe("the status poll", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("polls while watched, bumps only on a change, and backs off a refusal", async () => {
    let answer: GitStatus | Error = { repo: true, head: "c1", branch: "main", files: { a: "modified" }, dirs: {}, truncated: false };
    let calls = 0;
    const store = new GitStatusStore({
      fetch: async () => {
        calls += 1;
        if (answer instanceof Error) throw answer;
        return answer;
      },
      now: () => Date.now(),
      visible: () => true,
    }, 1_000);
    let heard = 0;
    const stop = store.subscribe("p1", () => (heard += 1));
    await settle();
    expect(store.get("p1")?.files).toEqual({ a: "modified" });
    expect(calls).toBe(1);
    expect(heard).toBe(1);
    vi.advanceTimersByTime(1_000);
    await settle();
    expect(calls).toBe(2);
    expect(heard).toBe(1); // the same answer: nobody redraws
    answer = { ...answer, files: { a: "modified", b: "untracked" } };
    vi.advanceTimersByTime(1_000);
    await settle();
    expect(heard).toBe(2);
    answer = Object.assign(new Error("this repository's own git config sets 'core.sshcommand'"), { status: 403 });
    vi.advanceTimersByTime(1_000);
    await settle();
    expect(store.error("p1")).toContain("core.sshcommand");
    expect(store.get("p1")).toBeNull();
    expect(heard).toBe(3);
    const before = calls;
    vi.advanceTimersByTime(10_000);
    await settle();
    expect(calls).toBe(before); // refused: not asked again for a while
    stop();
    const after = calls;
    vi.advanceTimersByTime(60_000);
    await settle();
    expect(calls).toBe(after); // nobody watching: no poll
  });
});
