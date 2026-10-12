import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  applyEditorConfig, editorConfigFor, forgetEditorConfig, overrideFor, reapplyEditorConfig, resolutionStats,
  resolveFor, STRIKES, TIMEOUT_MS, type EditorConfigDeps,
} from "./editorconfig";
import { CHECK_STEPS, deadline, globCost, RESOLVE_MS, type Deadline } from "../lib/editorconfig";
import { Buffers, type BufferDeps, type TextModel } from "./buffers";
import type { FileRead } from "../types";

// Load-proof timing (the flaky-test fix): the lead runs several suites at once,
// and a check that reads the wall clock fails when the CPU is taken away —
// not when the guard is wrong. Two tools, each keeping what the check proves:
//  - `held`: how long a job held the thread, as the least of its wall time and
//    the process's CPU time. Each is at least the thread's own CPU time (the
//    work the job really did), and a descheduled CPU inflates only the first,
//    so a bound on it holds under load and still fails when the work is long.
//    (The thread's own CPU clock is no use: Linux reads it a scheduler tick
//    stale, 4 ms here; the process's is exact.) A GC pause inflates both, so
//    a real-clock batch is run up to three times and the best run is judged
//    (`leastLongest`): a pause hits one run; a guard that fails, every run.
//  - `workDeadline`: a deadline whose clock is the work done, so `RESOLVE_MS`
//    is a fixed number of steps whatever the machine is doing. The guard's
//    logic — where it stops, whom it blames, what poison saves — is judged on
//    it; the real clock's own checks use the default deadline with `held`.
const proc = (globalThis as { process?: { cpuUsage?: () => { user: number; system: number } } }).process;
const cpuUsage = proc?.cpuUsage ? () => proc.cpuUsage!() : null;
const cpuMs = () => {
  const u = cpuUsage!();
  return (u.user + u.system) / 1000;
};
/** Run `fn`, and say how long it held the thread (ms; see above). */
function held(fn: () => void): number {
  const w = performance.now();
  const c = cpuUsage ? cpuMs() : 0;
  fn();
  const wall = performance.now() - w;
  return cpuUsage ? Math.min(wall, cpuMs() - c) : wall;
}
/** Nanoseconds one step stands for on the work clock: about this machine's rate (3–4 ns). */
const WORK_NS = 4;
/** `RESOLVE_MS` on the work clock, in steps. */
const BUDGET = (RESOLVE_MS * 1e6) / WORK_NS;
/** What a job may spend past the budget: the clock is read every `CHECK_STEPS`, and one step can be a large one. */
const OVERSHOOT = CHECK_STEPS + 16_384;
/** A deadline of `RESOLVE_MS` read off the work done (see above). */
function workDeadline(): Deadline {
  let d: Deadline | undefined;
  d = deadline(RESOLVE_MS, () => ((d?.total ?? 0) * WORK_NS) / 1e6);
  return d;
}
/** The resolution queue, run one task per item through a recorder: how long each task held the thread. */
function timedQueue(): number[] {
  const tasks: number[] = [];
  forgetEditorConfig((run) => setTimeout(() => tasks.push(held(run)), 0));
  return tasks;
}
/**
 * Run `batch` on a timed queue from a clean slate, up to three times, and keep
 * the run whose longest task was shortest (stopping at one under `bound`): a
 * GC pause or a preemption lands on one task of one run, while a guard that
 * does not stop costs every run.
 */
async function leastLongest<T>(bound: number, batch: () => Promise<T>) {
  let best: { longest: number; tasks: number[]; jobs: number; result: T } | null = null;
  for (let i = 0; i < 3 && !(best && best.longest < bound); i++) {
    const tasks = timedQueue(); // forgets everything first: no parse, strike or poison carried over
    const result = await batch();
    const longest = Math.max(...tasks);
    if (!best || longest < best.longest) best = { longest, tasks, jobs: resolutionStats().jobs, result };
  }
  return best!;
}

/** A project's files (relative paths) and a recorder over the two routes. */
function world(files: Record<string, string>) {
  const stats: string[][] = [];
  const reads: string[] = [];
  const deps: EditorConfigDeps = {
    stat: async (_id, paths) => {
      stats.push(paths);
      const out: Record<string, any> = {};
      for (const p of paths) out[p] = p in files ? { etag: "0".repeat(32), kind: "text" } : { gone: true };
      return { files: out };
    },
    read: async (_id, path): Promise<FileRead> => {
      reads.push(path);
      if (!(path in files)) throw new Error("404");
      return { path, content: files[path], kind: "text", protected: false };
    },
  };
  return { deps, stats, reads };
}

/** The bits of a Monaco model applyEditorConfig touches. */
class Model {
  opts: Record<string, unknown> = { insertSpaces: true, tabSize: 4, indentSize: 4 };
  eol = "\n";
  version = 1;
  dead = false;
  constructor(public text: string) {}
  updateOptions(o: Record<string, unknown>) {
    this.opts = { ...this.opts, ...o };
  }
  getLineCount() {
    return this.text.split(/\r\n|\n/).length;
  }
  getEOL() {
    return this.eol;
  }
  setEOL(e: number) {
    this.eol = e === 1 ? "\r\n" : "\n";
    this.version += 1; // Monaco's setEOL moves the version, as an edit would
  }
  onWillDispose() {
    return { dispose() {} };
  }
  isDisposed() {
    return this.dead;
  }
}

/** `heavyGlob`'s answer by tag length. */
const heaviestFor = new Map<number, number>();
/** The heaviest glob the per-glob caps allow, ending in `tag`: 64 alternatives, the most tokens each. */
function heavyGlob(tag: string): string {
  const make = (k: number) => `${"{a,b}".repeat(6)}${"*x".repeat(k)}y${tag}`;
  // The search below costs up to 40 cap checks a glob, and the deep worlds
  // build thousands: under load that alone ran past vitest's 5 s. The answer
  // depends only on the tag's length (every tag here is digits), so it is
  // searched once per length and checked again for each tag.
  const known = heaviestFor.get(tag.length);
  if (known !== undefined && globCost(make(known)) && !globCost(make(known + 1))) return make(known);
  for (let k = 40; k > 0; k--) {
    const g = make(k);
    if (globCost(g)) {
      heaviestFor.set(tag.length, k);
      return g;
    }
  }
  throw new Error("no glob fits the caps");
}

beforeEach(() => forgetEditorConfig());
afterEach(() => vi.useRealTimers());

describe("finding a file's .editorconfig (ED-8)", () => {
  it("one stat of every folder up to the project's root, never above", async () => {
    const w = world({});
    expect(await editorConfigFor(w.deps, "p1", "a/b/c.py")).toEqual({});
    expect(w.stats).toEqual([["a/b/.editorconfig", "a/.editorconfig", ".editorconfig"]]);
    expect(w.reads).toEqual([]); // nothing there, nothing read
    expect(w.stats.flat().some((p) => p.includes(".."))).toBe(false);
  });

  it("stops at root = true, and nearer wins", async () => {
    const w = world({
      ".editorconfig": "[*]\nindent_size = 8\n",
      "a/.editorconfig": "root = true\n[*]\nindent_style = space\nindent_size = 4\n",
      "a/b/.editorconfig": "[*.py]\nindent_size = 2\n",
    });
    expect(await editorConfigFor(w.deps, "p1", "a/b/c.py")).toEqual({
      indent_style: "space", indent_size: "2", tab_width: "2",
    });
    // Each config on the walk is read (once per etag) before the queued
    // judgement parses them; the one above `root = true` is never applied.
    expect(w.reads.sort()).toEqual([".editorconfig", "a/.editorconfig", "a/b/.editorconfig"]);
  });

  it("a folder's answer is kept a moment: a tab restore asks once per folder", async () => {
    const w = world({ ".editorconfig": "[*]\nindent_style = tab\n" });
    await Promise.all([
      editorConfigFor(w.deps, "p1", "x.c", 1000), editorConfigFor(w.deps, "p1", "y.c", 1000),
      editorConfigFor(w.deps, "p1", "z.c", 1500),
    ]);
    expect(w.stats).toHaveLength(1);
    expect(w.reads).toEqual([".editorconfig"]);
    await editorConfigFor(w.deps, "p1", "x.c", 1000 + 3001);
    expect(w.stats).toHaveLength(2); // stale: asked again
  });

  it("a refused stat, a failed read, a withheld or non-text answer: no configuration, no throw", async () => {
    const failing: EditorConfigDeps = {
      stat: async () => { throw new Error("403"); },
      read: async () => { throw new Error("never"); },
    };
    expect(await editorConfigFor(failing, "p1", "a.c")).toEqual({});
    forgetEditorConfig();
    const odd: EditorConfigDeps = {
      stat: async (_id, paths) => ({ files: Object.fromEntries(paths.map((p) => [p, { kind: "withheld" }])) }),
      read: async () => { throw new Error("never"); },
    };
    expect(await editorConfigFor(odd, "p1", "a.c")).toEqual({});
    forgetEditorConfig();
    const binary: EditorConfigDeps = {
      stat: async (_id, paths) => ({ files: Object.fromEntries(paths.map((p) => [p, { etag: "0".repeat(32), kind: "text" }])) }),
      read: async (_id, path) => ({ path, kind: "binary", protected: false }),
    };
    expect(await editorConfigFor(binary, "p1", "a.c")).toEqual({});
  });
});

describe("applying it to a model", () => {
  const cfg = "root = true\n[*.c]\nindent_style = tab\nindent_size = 4\nend_of_line = crlf\ntrim_trailing_whitespace = true\ninsert_final_newline = true\n";

  it("sets the indentation; never the text", async () => {
    const w = world({ ".editorconfig": cfg });
    const m = new Model("int x;\n    y;\n");
    const o = await applyEditorConfig(w.deps, m as any, "p1", "main.c");
    expect(o).toEqual({ insertSpaces: false, tabSize: 4, indentSize: 4, eol: "CRLF" });
    expect(m.opts).toMatchObject({ insertSpaces: false, tabSize: 4, indentSize: 4 });
    expect(m.text).toBe("int x;\n    y;\n");
    expect(m.eol).toBe("\n"); // a file with line endings keeps them
    expect(overrideFor(m)).toEqual(o);
  });

  it("gives a file with no line break the ending its new lines take", async () => {
    const w = world({ ".editorconfig": cfg });
    const m = new Model("");
    await applyEditorConfig(w.deps, m as any, "p1", "new.c");
    expect(m.eol).toBe("\r\n");
  });

  it("an unmatched file, or the fallback's model (no options), is left alone", async () => {
    const w = world({ ".editorconfig": cfg });
    const m = new Model("x");
    expect(await applyEditorConfig(w.deps, m as any, "p1", "notes.md")).toBeNull();
    expect(m.opts).toEqual({ insertSpaces: true, tabSize: 4, indentSize: 4 });
    expect(await applyEditorConfig(w.deps, { getValue: () => "" } as any, "p1", "a.c")).toBeNull();
  });

  it("is put back after Monaco recomputes a model's indentation (a settings change)", async () => {
    const w = world({ ".editorconfig": cfg });
    const m = new Model("x;\n");
    await applyEditorConfig(w.deps, m as any, "p1", "main.c");
    m.opts = { insertSpaces: true, tabSize: 8, indentSize: 8 }; // what the model service would do
    reapplyEditorConfig();
    expect(m.opts).toMatchObject({ insertSpaces: false, tabSize: 4, indentSize: 4 });
  });

  it("never waits past its timeout: a slow disk opens the file without it", async () => {
    vi.useFakeTimers();
    const slow: EditorConfigDeps = { stat: () => new Promise(() => {}), read: () => new Promise(() => {}) };
    const m = new Model("x;\n");
    const p = applyEditorConfig(slow, m as any, "p1", "slow.c");
    await vi.advanceTimersByTimeAsync(TIMEOUT_MS + 10);
    expect(await p).toBeNull();
  });
});

describe("the buffer registry's prepare hook", () => {
  class FakeModel implements TextModel {
    version = 1;
    eol = "\n";
    fns = new Set<() => void>();
    constructor(public text: string) {}
    getValue() { return this.text; }
    getAlternativeVersionId() { return this.version; }
    onDidChangeContent(fn: () => void) { this.fns.add(fn); return { dispose: () => void this.fns.delete(fn) }; }
    isDisposed() { return false; }
    dispose() {}
    getEOL() { return this.eol; }
  }

  const deps = (prepare?: BufferDeps["prepare"]): BufferDeps => ({
    read: async (_id, path) => ({ path, abs: `/w/${path}`, content: "", etag: "0".repeat(32), kind: "text", protected: false, editable: true }),
    write: async () => ({ etag: "1".repeat(32) }),
    stat: async () => ({ files: {} }),
    makeModel: async (text) => new FakeModel(text),
    now: () => 0,
    prepare,
  });

  it("runs before the registry records the text, so an EOL set there leaves the buffer clean", async () => {
    const b = new Buffers();
    let seen: { projectId: string; path: string } | null = null;
    b.configure(deps(async (model, projectId, path) => {
      seen = { projectId, path };
      (model as FakeModel).eol = "\r\n";
      (model as FakeModel).version += 1; // as Monaco's setEOL does
    }));
    const v = await b.open("p1", "new.c", "2|new.c");
    expect(seen).toEqual({ projectId: "p1", path: "new.c" });
    expect(v.dirty).toBe(false);
    expect(v.modelEol).toBe("CRLF");
  });

  it("one that throws never stops a file opening", async () => {
    const b = new Buffers();
    b.configure(deps(async () => { throw new Error("boom"); }));
    const v = await b.open("p1", "a.c", "2|a.c");
    expect(v.dirty).toBe(false);
    expect(v.kind).toBe("text");
  });
});

// The review of PR #59: what is cached, and what is never fetched.
describe("caching by content, and the size cap before a read", () => {
  /** Files with an etag and a size each, as the real stat answers. */
  function world2(files: Record<string, { text: string; etag: string; size?: number }>) {
    const reads: string[] = [];
    const deps: EditorConfigDeps = {
      stat: async (_id, paths) => ({
        files: Object.fromEntries(paths.map((p) => [p, p in files
          ? { etag: files[p].etag, kind: "text", size: files[p].size ?? files[p].text.length } as any
          : { gone: true }])),
      }),
      read: async (_id, path): Promise<FileRead> => {
        reads.push(path);
        return { path, content: files[path].text, kind: "text", protected: false };
      },
    };
    return { deps, reads, files };
  }

  it("restored tabs on one file resolve once per configuration content", async () => {
    const w = world2({ ".editorconfig": { text: "[*.c]\nindent_style = tab\n", etag: "a".repeat(32) } });
    const first = await resolveFor(w.deps, "p1", "src/main.c", 1000);
    const again = await resolveFor(w.deps, "p1", "src/main.c", 5000); // past the folder cache: stat again
    expect(again).toBe(first); // the same resolution, not matched again
    expect(w.reads).toEqual([".editorconfig"]); // and the same content not read again
    w.files[".editorconfig"] = { text: "[*.c]\nindent_style = space\n", etag: "b".repeat(32) };
    const changed = await resolveFor(w.deps, "p1", "src/main.c", 9000);
    expect(changed).not.toBe(first);
    expect(changed.props.indent_style).toBe("space");
    expect(w.reads).toEqual([".editorconfig", ".editorconfig"]);
  });

  it("a file the stat says is over 64 KB is never downloaded", async () => {
    const w = world2({ ".editorconfig": { text: "[*]\nindent_size = 2\n", etag: "c".repeat(32), size: 64 * 1024 + 1 } });
    expect(await editorConfigFor(w.deps, "p1", "a.c")).toEqual({});
    expect(w.reads).toEqual([]);
  });

  it("a path left with no configuration is said once, by path, never with content", async () => {
    const heavy = Array.from({ length: 123 }, (_, n) => `[${heavyGlob(String(n))}]\nindent_size = 2`);
    const w = world2({ ".editorconfig": { text: `root = true\n${heavy.join("\n")}`, etag: "d".repeat(32) } });
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      const path = `src/${"robot_drive_subsystem_controller_".repeat(8)}.py`;
      const r = await resolveFor(w.deps, "p1", path, 1000);
      expect(r).toMatchObject({ over: "time", props: {} });
      await resolveFor(w.deps, "p1", path, 1000);
      await resolveFor(w.deps, "p1", path, 1000);
      expect(warn).toHaveBeenCalledTimes(1);
      expect(String(warn.mock.calls[0][0])).toContain(path);
      expect(String(warn.mock.calls[0][0])).not.toContain("{a,b}");
    } finally {
      warn.mockRestore();
    }
  });
});

// The lead's structural backstop (review of PR #59, round 3): a real clock,
// poison, and one resolution per macrotask.
describe("the window is never held", () => {
  /** Each heavy text, built once for the file (the deep worlds use the same 32). */
  const heavyTexts = new Map<string, string>();
  /** A config of 123 sections as heavy as the caps allow (the review's shape): every glob just inside them. */
  const heavyText = (salt: string) => {
    let text = heavyTexts.get(salt);
    if (text === undefined) {
      text = Array.from({ length: 123 }, (_, n) => heavyGlob(`${salt}${n}`))
        .map((g) => `[${g}]\nindent_size = 2`).join("\n");
      heavyTexts.set(salt, text);
    }
    return text;
  };
  /** 32 folders deep, a heavy config in each, and `count` files at the bottom. */
  function deepWorld(count: number) {
    const dirs = Array.from({ length: 31 }, (_, i) => `d${i + 1}`);
    const files: Record<string, string> = {};
    for (let i = 0; i <= dirs.length; i++) {
      const dir = dirs.slice(0, i).join("/");
      files[dir ? `${dir}/.editorconfig` : ".editorconfig"] = heavyText(String(i));
    }
    const paths = Array.from({ length: count }, (_, i) => `${dirs.join("/")}/robot_drive_subsystem_${i}.cpp`);
    const w = world(files);
    return { ...w, paths };
  }
  const quiet = () => vi.spyOn(console, "warn").mockImplementation(() => {});

  // The guard's logic, on the work clock: deterministic whatever the load.
  it("32 configs deep and 40 tabs at once: every resolution its own task, none long", async () => {
    const w = deepWorld(40);
    const warn = quiet();
    try {
      const tasks = timedQueue();
      const spent: Deadline[] = [];
      const clock = () => {
        const d = workDeadline();
        spent.push(d);
        return d;
      };
      const results = await Promise.all(w.paths.map((p) => resolveFor(w.deps, "p1", p, 1000, clock)));
      expect(results.every((r) => r.over !== null && Object.keys(r.props).length === 0)).toBe(true);
      // Every resolution its own task: one macrotask per job, never a batch.
      expect(resolutionStats().jobs).toBe(40);
      expect(tasks).toHaveLength(40);
      // None long: each job stopped at its deadline, plus at most one look's worth.
      for (const d of spent) expect(d.total).toBeLessThanOrEqual(BUDGET + OVERSHOOT);
      // Poison: two time-outs blame the config that spent the time, and the
      // other 38 tabs are judged in O(1) — the whole batch is a few deadlines,
      // not 40.
      expect(results.filter((r) => r.over === "time").length).toBeLessThanOrEqual(2);
      const work = spent.reduce((sum, d) => sum + d.total, 0);
      expect(work).toBeLessThanOrEqual(2 * (BUDGET + OVERSHOOT));
    } finally {
      warn.mockRestore();
    }
  }, 30_000);

  // The real clock (the default deadline): no task held the thread past the
  // bound, measured by `held`, so a descheduled CPU cannot fail it.
  it("and on the real clock no task holds the thread", async () => {
    const w = deepWorld(40);
    const warn = quiet();
    try {
      const run = await leastLongest(16, () => Promise.all(w.paths.map((p) => resolveFor(w.deps, "p1", p, 1000))));
      expect(run.result.every((r) => r.over !== null && Object.keys(r.props).length === 0)).toBe(true);
      expect(run.tasks).toHaveLength(run.jobs);
      expect(run.longest).toBeLessThan(16);
    } finally {
      warn.mockRestore();
    }
  }, 30_000);

  it("a poisoned config: a later resolution touching it, for any path, does no work", async () => {
    const w = deepWorld(3);
    const warn = quiet();
    try {
      // A fake clock that is always past its deadline: each judgement times out at its first look.
      let t = 0;
      const late = () => deadline(5, () => (t += 100));
      await resolveFor(w.deps, "p1", w.paths[0], 1000, late);
      await resolveFor(w.deps, "p1", w.paths[1], 1000, late); // the second strike poisons
      const before = resolutionStats().jobs;
      const r = await resolveFor(w.deps, "p1", w.paths[2], 1000, late);
      expect(r).toMatchObject({ over: "poisoned", props: {}, steps: 0 });
      expect(resolutionStats().jobs).toBe(before); // never even queued
    } finally {
      warn.mockRestore();
    }
  }, 30_000);

  it("a poisoned config comes back when its content changes", async () => {
    const files: Record<string, string> = { ".editorconfig": `root = true\n[${"{a,b}".repeat(7)}]\nindent_size = 4\n` };
    let etag = "1".repeat(32);
    const reads: string[] = [];
    const deps: EditorConfigDeps = {
      stat: async (_id, paths) => ({ files: Object.fromEntries(paths.map((p) => [p, p in files ? { etag, kind: "text" } : { gone: true }])) }),
      read: async (_id, path) => {
        reads.push(path);
        return { path, content: files[path], kind: "text", protected: false };
      },
    };
    const warn = quiet();
    try {
      // On the work clock: finding the cap reads the clock, and a real one
      // under load could time out first (a strike, not the cap's poison).
      expect((await resolveFor(deps, "p1", "a.c", 1000, workDeadline)).over).toBe("cap"); // a cap poisons at once
      expect((await resolveFor(deps, "p1", "b.c", 1000, workDeadline)).over).toBe("poisoned");
      files[".editorconfig"] = "root = true\n[*.c]\nindent_size = 4\n";
      etag = "2".repeat(32);
      expect((await resolveFor(deps, "p1", "b.c", 9000, workDeadline)).props).toMatchObject({ indent_size: "4" });
    } finally {
      warn.mockRestore();
    }
  });

  it("the number-range shape: each open a task under the deadline", async () => {
    const nums = ["root = true"];
    for (let n = 0; n < 123; n++) nums.push(`[${"{0..999999999}".repeat(30)}${n}x]`, "indent_size = 2");
    const w = world({ ".editorconfig": nums.join("\n") });
    const warn = quiet();
    try {
      // The real clock: the shape's cost is what is under test (its digits
      // were once under-counted), so no work clock here — `held` instead.
      const names = Array.from({ length: 40 }, (_, i) => `${"9".repeat(202)}${i}.c`);
      const run = await leastLongest(16, () => Promise.all(names.map((p) => resolveFor(w.deps, "p1", p, 1000))));
      expect(run.tasks).toHaveLength(run.jobs);
      expect(run.longest).toBeLessThan(16);
    } finally {
      warn.mockRestore();
    }
  });

  it("an ordinary config is not slowed: it resolves in its own task, fast", async () => {
    const w = world({ ".editorconfig": "root = true\n[*]\nindent_style = space\n[*.c]\nindent_size = 2\n" });
    const paths = Array.from({ length: 40 }, (_, i) => `src/f${i}.c`);
    // On the real clock: a resolution this small does under `CHECK_STEPS` of
    // work, so it never looks at the clock and cannot time out.
    const run = await leastLongest(5, () => Promise.all(paths.map((p) => resolveFor(w.deps, "p1", p, 1000))));
    expect(run.result.every((r) => r.props.indent_size === "2")).toBe(true);
    expect(run.result.every((r) => r.steps < CHECK_STEPS)).toBe(true);
    expect(run.tasks).toHaveLength(40);
    expect(run.longest).toBeLessThan(5);
  });
});

// Strikes count in a row (review of PR #72, LOW 3). A config judged in time
// starts its count again. Before, strikes never reset, so two time-outs any
// distance apart — a busy machine, a GC pause: a probe of 3,000 resolutions
// of the real-world config on a loaded core timed out 2.4% of them — poisoned
// the owner's real `.editorconfig` until its etag changed.
describe("strikes count in a row", () => {
  /** An ordinary project's config: ~20,000 steps on a deep path, so it looks at the clock. */
  const REAL = [
    "root = true", "[*]", "indent_style = space", "indent_size = 4", "end_of_line = lf",
    "[*.{c,h,cpp,hpp,cc}]", "indent_size = 2", "[{Makefile,makefile,*.mk}]", "indent_style = tab",
    "[*.{json,yml,yaml}]", "indent_size = 2", "[*.md]", "trim_trailing_whitespace = false",
    "[src/**/vendor/**]", "indent_size = unset", "[lib/**.js]", "indent_size = 2",
  ].join("\n");
  /** A new path each time: a success is kept per path, and would answer without judging. */
  const deep = (n: number) => `src/${"deep/".repeat(40)}robot${n}.cpp`;
  /** A clock always past its deadline: the judgement times out at its first look (a busy machine). */
  const late = () => {
    let t = 0;
    return deadline(RESOLVE_MS, () => (t += 100));
  };
  const quiet = () => vi.spyOn(console, "warn").mockImplementation(() => {});

  it("two strikes poison", () => {
    expect(STRIKES).toBe(2);
  });

  it("time-out, success, time-out: not poisoned — the config still applies", async () => {
    const w = world({ ".editorconfig": REAL });
    const warn = quiet();
    try {
      expect((await resolveFor(w.deps, "p1", deep(1), 1000, late)).over).toBe("time");
      expect((await resolveFor(w.deps, "p1", deep(2), 1000, workDeadline)).over).toBeNull();
      expect((await resolveFor(w.deps, "p1", deep(3), 1000, late)).over).toBe("time");
      const next = await resolveFor(w.deps, "p1", deep(4), 1000, workDeadline);
      expect(next.over).toBeNull();
      expect(next.props).toMatchObject({ indent_style: "space", indent_size: "2" });
    } finally {
      warn.mockRestore();
    }
  });

  it("time-out, time-out: poisoned, for any path", async () => {
    const w = world({ ".editorconfig": REAL });
    const warn = quiet();
    try {
      expect((await resolveFor(w.deps, "p1", deep(1), 1000, late)).over).toBe("time");
      expect((await resolveFor(w.deps, "p1", deep(2), 1000, late)).over).toBe("time");
      expect(await resolveFor(w.deps, "p1", "src/main.c", 1000, workDeadline))
        .toMatchObject({ over: "poisoned", props: {}, steps: 0 });
    } finally {
      warn.mockRestore();
    }
  });

  it("a cap poisons at once: no second strike needed", async () => {
    const w = world({ ".editorconfig": `${REAL}\n[lib/${"{a,b}".repeat(7)}]\nindent_size = 3\n` });
    const warn = quiet();
    try {
      expect((await resolveFor(w.deps, "p1", "src/main.c", 1000, workDeadline)).over).toBe("cap");
      expect((await resolveFor(w.deps, "p1", "src/other.c", 1000, workDeadline)).over).toBe("poisoned");
    } finally {
      warn.mockRestore();
    }
  });
});
