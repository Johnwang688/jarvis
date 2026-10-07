import { describe, expect, it } from "vitest";
import {
  applyEffort, applyModel, applyProvider, chipEditable, defaultEffort, effective, effortOptions,
  modelLabel, modelOptions, offRoster, patchBody, SEARCH, shortId, threadBody, threadTooltip,
  visionNote, type Choice, type ThreadModels,
} from "./threadmodel";
import type { Thread } from "../types";

const TM: ThreadModels = {
  effort_default: "high",
  providers: {
    fast: {
      label: "OpenRouter", default: "deepseek/deepseek-v4-flash-0731", default_effort: "high",
      models: [
        { id: "deepseek/deepseek-v4-flash-0731", efforts: ["high", "medium", "low"], vision: false,
          prompt_usd: 0.1, completion_usd: 0.3 },
        { id: "moonshotai/kimi-k3", efforts: ["medium", "low"], vision: true, effort: "low",
          prompt_usd: 0.6, completion_usd: 2.5 },
        { id: "plain/no-reasoning", efforts: [], vision: true, prompt_usd: 0, completion_usd: 0 },
        // A roster model the catalog has never described: no ladder known.
        { id: "new/unlisted", name: "new/unlisted", unlisted: true, effort: "" },
      ],
    },
    claude: {
      label: "Claude", default: "claude-opus-5-5", default_effort: "high",
      models: [
        { id: "claude-opus-5-5", efforts: ["low", "medium", "high", "xhigh", "max"], vision: true },
        { id: "claude-haiku-4-5", efforts: [], vision: true },
      ],
    },
    codex: {
      label: "Codex", default: "gpt-6-astra", default_effort: "xhigh",
      models: [{ id: "gpt-6-astra", efforts: ["low", "medium", "high", "xhigh"], vision: true }],
    },
  },
};

const fast = (model: string | null = null, effort: string | null = null): Choice => ({ provider: "fast", model, effort });

const thread = (over: Partial<Thread> = {}): Thread => ({
  id: "t1", project_id: "p1", role: "chat", provider: "fast", provider_session_id: null, task_id: null,
  title: "", created: "", updated: "", turns: 0, cost_usd: 0, tokens: 0, model: null, effort: null,
  ...over,
});

describe("labels", () => {
  it("shortens an OpenRouter id to its model name", () => {
    expect(shortId("deepseek/deepseek-v4-flash-0731")).toBe("deepseek-v4-flash-0731");
    expect(shortId("claude-opus-5-5")).toBe("claude-opus-5-5");
  });

  it("names the default, a pin, and a pin that left the roster", () => {
    expect(modelLabel(TM, fast())).toBe("default · deepseek-v4-flash-0731");
    expect(modelLabel(TM, fast("moonshotai/kimi-k3"))).toBe("kimi-k3");
    expect(modelLabel(TM, fast("gone/model"))).toBe("model (not on roster)");
    expect(offRoster(TM, fast("gone/model"))).toBe(true);
    expect(offRoster(TM, { provider: "claude", model: "claude-opus-5-5", effort: null })).toBe(false);
  });
});

describe("the model list", () => {
  it("is default, the roster with prices, and the catalogue search on OpenRouter", () => {
    const opts = modelOptions(TM, fast());
    expect(opts[0]).toEqual({ value: "", label: "default · deepseek-v4-flash-0731" });
    expect(opts.map((o) => o.value)).toEqual(["", "deepseek/deepseek-v4-flash-0731", "moonshotai/kimi-k3",
                                              "plain/no-reasoning", "new/unlisted", SEARCH]);
    expect(opts[1].label).toBe("deepseek-v4-flash-0731 · $0.1/$0.3");
    expect(opts[3].label).toBe("no-reasoning · free");
  });

  it("keeps a pinned model that left the roster, marked (A3)", () => {
    const opts = modelOptions(TM, fast("gone/model"));
    expect(opts.find((o) => o.value === "gone/model")?.label).toBe("model (not on roster)");
  });

  it("lists Claude's and Codex's own models, without a catalogue search", () => {
    const claude = modelOptions(TM, { provider: "claude", model: null, effort: null }).map((o) => o.value);
    expect(claude).toEqual(["", "claude-opus-5-5", "claude-haiku-4-5"]);
    expect(modelOptions(TM, { provider: "codex", model: null, effort: null }).map((o) => o.value))
      .toEqual(["", "gpt-6-astra"]);
  });
});

describe("effort (A4)", () => {
  it("defaults to high, or the model's own roster setting, within its ladder", () => {
    expect(defaultEffort(TM, "fast", "deepseek/deepseek-v4-flash-0731")).toBe("high");
    expect(defaultEffort(TM, "fast", "moonshotai/kimi-k3")).toBe("low");
    expect(defaultEffort(TM, "fast", "plain/no-reasoning")).toBeNull();
    expect(defaultEffort(TM, "claude", "claude-opus-5-5")).toBe("high");
    expect(defaultEffort(TM, "fast", "unknown/model")).toBe("high");
  });

  it("offers only the model's own levels, and nothing for a model with none", () => {
    expect(effortOptions(TM, fast("moonshotai/kimi-k3")).map((o) => o.value)).toEqual(["", "medium", "low"]);
    expect(effortOptions(TM, fast("moonshotai/kimi-k3"))[0].label).toBe("default · low");
    expect(effortOptions(TM, fast("plain/no-reasoning"))).toEqual([]);
    expect(effortOptions(TM, { provider: "claude", model: "claude-haiku-4-5", effort: null })).toEqual([]);
    expect(effortOptions(TM, { provider: "codex", model: null, effort: null })[0].label).toBe("default · xhigh");
  });

  it("treats an unknown ladder as unknown, not as no reasoning control", () => {
    // The backend sends such a model `high` and accepts any level
    // (thread_model.efforts_of -> None); the chip must agree, for an
    // unlisted roster model and for an off-roster pin alike.
    for (const model of ["new/unlisted", "gone/model"]) {
      expect(defaultEffort(TM, "fast", model)).toBe("high");
      const opts = effortOptions(TM, fast(model));
      expect(opts[0].label).toBe("default · high");
      expect(opts.map((o) => o.value)).toEqual(["", "max", "xhigh", "high", "medium", "low", "minimal", "none"]);
      expect(effective(TM, fast(model)).effort).toBe("high");
    }
  });

  it("is reset to the new model's default when the model changes", () => {
    expect(applyModel(fast("moonshotai/kimi-k3", "medium"), "deepseek/deepseek-v4-flash-0731"))
      .toEqual(fast("deepseek/deepseek-v4-flash-0731", null));
  });

  it("chosen on a default thread pins the default model it is an effort of", () => {
    expect(applyEffort(TM, fast(), "low")).toEqual(fast("deepseek/deepseek-v4-flash-0731", "low"));
    expect(applyEffort(TM, fast("moonshotai/kimi-k3"), null)).toEqual(fast("moonshotai/kimi-k3", null));
  });

  it("works out what the next message runs on", () => {
    expect(effective(TM, fast())).toEqual({ model: "deepseek/deepseek-v4-flash-0731", effort: "high" });
    expect(effective(TM, fast("moonshotai/kimi-k3"))).toEqual({ model: "moonshotai/kimi-k3", effort: "low" });
    expect(effective(TM, { provider: "claude", model: null, effort: null }))
      .toEqual({ model: "claude-opus-5-5", effort: "high" });
  });
});

describe("what goes to the server", () => {
  it("a default compose sends no model", () => {
    expect(threadBody(fast())).toEqual({ provider: "fast" });
    expect(threadBody({ provider: "claude", model: null, effort: null })).toEqual({ provider: "claude" });
    expect(threadBody(fast("moonshotai/kimi-k3", "medium")))
      .toEqual({ provider: "fast", brief: { model: "moonshotai/kimi-k3", effort: "medium" } });
  });

  it("a change after the first message is a PATCH of what changed", () => {
    expect(patchBody(fast(), fast("moonshotai/kimi-k3"))).toEqual({ model: "moonshotai/kimi-k3" });
    expect(patchBody(fast("moonshotai/kimi-k3"), fast())).toEqual({ model: null });
    expect(patchBody(fast("moonshotai/kimi-k3"), fast("moonshotai/kimi-k3", "low"))).toEqual({ effort: "low" });
    expect(patchBody(fast(), fast("deepseek/deepseek-v4-flash-0731", "low")))
      .toEqual({ model: "deepseek/deepseek-v4-flash-0731", effort: "low" });
    expect(patchBody(fast("a/b"), fast("a/b"))).toBeNull();
  });

  it("a new provider starts on its own defaults", () => {
    expect(applyProvider(fast("moonshotai/kimi-k3", "low"), "claude"))
      .toEqual({ provider: "claude", model: null, effort: null });
  });
});

describe("who may change what", () => {
  it("the provider only while composing; model and effort on a chat thread; nothing on a task's", () => {
    expect(chipEditable(null, true)).toEqual({ provider: true, model: true });
    expect(chipEditable(thread(), false)).toEqual({ provider: false, model: true });
    expect(chipEditable(thread({ task_id: "k1" }), false)).toEqual({ provider: false, model: false });
    expect(chipEditable(thread({ role: "implementer", task_id: "k1" }), false)).toEqual({ provider: false, model: false });
    expect(chipEditable(null, false)).toEqual({ provider: false, model: false });
  });
});

describe("text-only models (A5)", () => {
  it("say so, and vision models say nothing", () => {
    expect(visionNote(TM, fast())).toContain("text-only");
    expect(visionNote(TM, fast("moonshotai/kimi-k3"))).toBeNull();
    expect(visionNote(TM, fast("unknown/model"))).toBeNull();
  });
});

describe("the sidebar tooltip", () => {
  it("names the provider and the model, pinned or following", () => {
    expect(threadTooltip(thread({ provider: "claude", model: "claude-opus-5-5", effort: "high" })))
      .toBe("runs on Claude · claude-opus-5-5 · high (pinned)");
    expect(threadTooltip(thread(), "/home/x")).toBe("runs on OpenRouter · follows the default\nworks in /home/x");
  });
});
