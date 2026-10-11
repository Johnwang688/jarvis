import { describe, expect, it } from "vitest";
import {
  describe as words, effortAt, keyStep, levelName, sliderModel, snapIndex, stopFraction,
} from "./effortSlider";
import { applyEffort, effortOptions, patchBody, type Choice, type ThreadModels } from "./threadmodel";

const TM: ThreadModels = {
  effort_default: "high",
  providers: {
    fast: {
      label: "OpenRouter", default: "deepseek/deepseek-v4-flash-0731", default_effort: "high",
      models: [
        // Hardest first, as a catalog may list it: the slider sorts it.
        { id: "deepseek/deepseek-v4-flash-0731", efforts: ["high", "medium", "low"] },
        { id: "moonshotai/kimi-k3", efforts: ["medium", "low"], effort: "low" },
        { id: "plain/no-reasoning", efforts: [] },
        { id: "new/unlisted", unlisted: true },
        // Bad data: `ultra` is Codex's alone, whatever a ladder says.
        { id: "odd/ultra", efforts: ["low", "high", "ultra"] },
      ],
    },
    claude: {
      label: "Claude", default: "claude-opus-5-5", default_effort: "high",
      models: [
        { id: "claude-opus-5-5", efforts: ["low", "medium", "high", "xhigh", "max"] },
        { id: "claude-haiku-4-5", efforts: [] },
      ],
    },
    codex: {
      label: "Codex", default: "gpt-6.1-sol", default_effort: "high",
      models: [
        { id: "gpt-6.1-sol", efforts: ["low", "medium", "high", "xhigh", "max", "ultra"] },
        { id: "gpt-5.5", efforts: ["low", "medium", "high", "xhigh"] },
      ],
    },
  },
};

const fast = (model: string | null = null, effort: string | null = null): Choice => ({ provider: "fast", model, effort });
const claude = (model: string | null = null, effort: string | null = null): Choice => ({ provider: "claude", model, effort });
const codex = (model: string | null = null, effort: string | null = null): Choice => ({ provider: "codex", model, effort });

describe("stops", () => {
  it("are the model's own ladder, Faster → Smarter", () => {
    expect(sliderModel(TM, claude())?.stops).toEqual(["low", "medium", "high", "xhigh", "max"]);
    expect(sliderModel(TM, fast())?.stops).toEqual(["low", "medium", "high"]);
    expect(sliderModel(TM, fast("moonshotai/kimi-k3"))?.stops).toEqual(["low", "medium"]);
  });

  it("put ultra on top for Codex, and never elsewhere", () => {
    expect(sliderModel(TM, codex())?.stops).toEqual(["low", "medium", "high", "xhigh", "max", "ultra"]);
    expect(sliderModel(TM, codex("gpt-5.5"))?.stops).not.toContain("ultra");
    expect(sliderModel(TM, fast("odd/ultra"))?.stops).toEqual(["low", "high"]);
    // An unknown ladder offers the provider's own words, as the pills did.
    expect(sliderModel(TM, fast("new/unlisted"))?.stops)
      .toEqual(["none", "minimal", "low", "medium", "high", "xhigh", "max"]);
    expect(sliderModel(TM, claude("claude-unknown"))?.stops).not.toContain("ultra");
    expect(sliderModel(TM, codex("gone-model"))?.stops)
      .toEqual(["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]);
  });

  it("are none for a model with no reasoning control", () => {
    expect(sliderModel(TM, claude("claude-haiku-4-5"))).toBeNull();
    expect(sliderModel(TM, fast("plain/no-reasoning"))).toBeNull();
  });

  it("are exactly the levels the pills offered, no more", () => {
    const cases: Choice[] = [
      fast(), fast("moonshotai/kimi-k3"), fast("new/unlisted"), claude(), claude("claude-opus-5-5", "max"),
      codex(), codex("gpt-5.5"), codex("gone-model"), fast(null, "max"), claude(null, "ultra"),
    ];
    for (const c of cases) {
      const m = sliderModel(TM, c)!;
      const pills = effortOptions(TM, c).map((o) => o.value).filter((v) => v !== "" && v !== m.kept);
      expect([...m.stops].sort()).toEqual([...pills].sort());
    }
  });
});

describe("the default stop", () => {
  it("is what `default · X` named, and the thumb sits there on a default thread", () => {
    const m = sliderModel(TM, claude())!;
    expect(m.stops[m.defaultIndex]).toBe("high");
    expect(m.index).toBe(m.defaultIndex);
    expect(m.followsDefault).toBe(true);
    // A model's own default, clamped within its ladder (A4).
    const k = sliderModel(TM, fast("moonshotai/kimi-k3"))!;
    expect(k.stops[k.defaultIndex]).toBe("low");
  });

  it("clears the effort to null; any other stop sets it", () => {
    const m = sliderModel(TM, claude())!;
    expect(effortAt(m, m.defaultIndex)).toBeNull();
    expect(effortAt(m, m.stops.indexOf("xhigh"))).toBe("xhigh");
    expect(effortAt(m, m.stops.indexOf("low"))).toBe("low");
  });

  it("sends exactly what the pills sent: a default thread stays unpinned (A4, A6)", () => {
    const thread = fast();
    const m = sliderModel(TM, thread)!;
    const low = applyEffort(TM, thread, effortAt(m, m.stops.indexOf("low")));
    expect(patchBody(thread, low)).toEqual({ effort: "low" });
    expect(low.model).toBeNull();
    const back = applyEffort(TM, low, effortAt(sliderModel(TM, low)!, m.defaultIndex));
    expect(patchBody(low, back)).toEqual({ effort: null });
    expect(back).toEqual(thread);
  });

  it("is a stop of its own when it names no level (default · none)", () => {
    const tm: ThreadModels = { ...TM, providers: { ...TM.providers,
      claude: { ...TM.providers.claude!, default_effort: null } } };
    const m = sliderModel(tm, claude())!;
    expect(m.stops).toEqual(["", "low", "medium", "high", "xhigh", "max"]);
    expect(m.defaultIndex).toBe(0);
    expect(effortAt(m, 0)).toBeNull();
    expect(words(m).name).toBe("Default");
  });
});

describe("a stored effort", () => {
  it("puts the thumb on its stop", () => {
    const m = sliderModel(TM, claude("claude-opus-5-5", "xhigh"))!;
    expect(m.stops[m.index]).toBe("xhigh");
    expect(m.followsDefault).toBe(false);
    expect(m.kept).toBeNull();
  });

  it("that this model lacks is kept, and the thumb sits where it runs", () => {
    const m = sliderModel(TM, fast(null, "max"))!; // the default stops at high
    expect(m.kept).toBe("max");
    expect(m.stops[m.index]).toBe("high");
    expect(words(m)).toEqual({ name: "Max", note: "runs as High", aria: "Max, runs as High" });
    // A stored ultra off Codex runs the default.
    const u = sliderModel(TM, claude(null, "ultra"))!;
    expect(u.stops).not.toContain("ultra");
    expect(u.kept).toBe("ultra");
    expect(u.index).toBe(u.defaultIndex);
  });
});

describe("position", () => {
  it("snaps to the nearest stop, clamped at the ends", () => {
    expect(snapIndex(0, 5)).toBe(0);
    expect(snapIndex(0.24, 5)).toBe(1);
    expect(snapIndex(0.26, 5)).toBe(1);
    expect(snapIndex(0.4, 5)).toBe(2);
    expect(snapIndex(0.99, 5)).toBe(4);
    expect(snapIndex(-3, 5)).toBe(0);
    expect(snapIndex(7, 5)).toBe(4);
    expect(snapIndex(Number.NaN, 5)).toBe(0);
    expect(snapIndex(0.8, 1)).toBe(0);
  });

  it("round-trips: every stop's own position snaps back to it", () => {
    for (const n of [1, 2, 3, 5, 6, 8]) {
      for (let i = 0; i < n; i++) expect(snapIndex(stopFraction(i, n), n)).toBe(i);
    }
    expect(stopFraction(0, 5)).toBe(0);
    expect(stopFraction(4, 5)).toBe(1);
    expect(stopFraction(0, 1)).toBe(0.5);
  });

  it("moves by key: arrows step, Home and End jump, nothing else", () => {
    expect(keyStep("ArrowRight", 2, 5)).toBe(3);
    expect(keyStep("ArrowUp", 4, 5)).toBe(4);
    expect(keyStep("ArrowLeft", 0, 5)).toBe(0);
    expect(keyStep("ArrowDown", 3, 5)).toBe(2);
    expect(keyStep("Home", 3, 5)).toBe(0);
    expect(keyStep("End", 1, 5)).toBe(4);
    expect(keyStep(" ", 1, 5)).toBeNull();
    expect(keyStep("Enter", 1, 5)).toBeNull();
    expect(keyStep("a", 1, 5)).toBeNull();
  });
});

describe("labels", () => {
  it("name each level as Claude's app does", () => {
    expect(["low", "medium", "high", "xhigh", "max", "ultra"].map(levelName))
      .toEqual(["Low", "Medium", "High", "Extra", "Max", "Ultra"]);
    expect(levelName("minimal")).toBe("Minimal");
    expect(levelName("turbo")).toBe("Turbo");
  });

  it("say default only when the thread follows it", () => {
    const m = sliderModel(TM, claude())!;
    expect(words(m)).toEqual({ name: "High", note: "default", aria: "High, default" });
    const set = sliderModel(TM, claude(null, "xhigh"))!;
    expect(words(set)).toEqual({ name: "Extra", note: "", aria: "Extra" });
    // A stored "high" that equals the default is a choice, not the default.
    const pinned = sliderModel(TM, claude(null, "high"))!;
    expect(words(pinned)).toEqual({ name: "High", note: "", aria: "High" });
  });

  it("describe a previewed stop as what committing it would store", () => {
    const m = sliderModel(TM, claude(null, "xhigh"))!;
    expect(words(m, m.defaultIndex)).toEqual({ name: "High", note: "default", aria: "High, default" });
    expect(words(m, m.stops.indexOf("max"))).toEqual({ name: "Max", note: "", aria: "Max" });
  });
});
