import { describe, expect, it } from "vitest";
import {
  applyEffort, applyModel, applyProvider, canSetDefault, chipEditable, chipState, defaultBody, defaultChosenHere,
  defaultEffort, defaultEffortOptions, defaultRows, defaultSourceLabel, effective, effortOptions, resetTitle,
  modelLabel, modelOptions, offRoster, patchBody, providerRefusal, SEARCH, shortId, threadBody, threadTooltip,
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

  it("chosen on a default thread keeps it following the default model (A4 amendment)", () => {
    expect(applyEffort(TM, fast(), "low")).toEqual(fast(null, "low"));
    expect(applyEffort(TM, fast(null, "low"), null)).toEqual(fast(null, null));
    expect(applyEffort(TM, fast("moonshotai/kimi-k3"), null)).toEqual(fast("moonshotai/kimi-k3", null));
    // So the change is a PATCH of the effort alone, and the chip still says default.
    expect(patchBody(fast(), applyEffort(TM, fast(), "low"))).toEqual({ effort: "low" });
    expect(modelLabel(TM, fast(null, "low"))).toBe("default · deepseek-v4-flash-0731");
    expect(modelOptions(TM, fast(null, "low"))[0]).toEqual({ value: "", label: "default · deepseek-v4-flash-0731" });
    expect(threadTooltip(thread({ effort: "low" }))).toBe("runs on OpenRouter · follows the default · effort low");
  });

  it("on a default thread is clamped to whatever the default is now", () => {
    expect(effective(TM, fast(null, "low"))).toEqual({ model: "deepseek/deepseek-v4-flash-0731", effort: "low" });
    // The default moves to a model whose ladder stops at medium: max runs as medium.
    const moved = (model: string): ThreadModels => ({
      ...TM, providers: { ...TM.providers, fast: { ...TM.providers.fast!, default: model } },
    });
    expect(effective(moved("moonshotai/kimi-k3"), fast(null, "max")))
      .toEqual({ model: "moonshotai/kimi-k3", effort: "medium" });
    const opts = effortOptions(moved("moonshotai/kimi-k3"), fast(null, "max"));
    expect(opts.map((o) => o.value)).toEqual(["", "medium", "low", "max"]);
    expect(opts[opts.length - 1].label).toBe("max (runs as medium)");
    // A default with no reasoning control: no effort, and no effort chip.
    expect(effective(moved("plain/no-reasoning"), fast(null, "low")))
      .toEqual({ model: "plain/no-reasoning", effort: null });
    expect(effortOptions(moved("plain/no-reasoning"), fast(null, "low"))).toEqual([]);
    // An unknown ladder sends it as asked; the label of "default" is the default model's own.
    expect(effective(moved("gone/model"), fast(null, "max"))).toEqual({ model: "gone/model", effort: "max" });
    expect(effortOptions(TM, fast(null, "low"))[0].label).toBe("default · high");
  });

  it("an explicit model still pins, on its own default effort", () => {
    expect(applyModel(fast(null, "low"), "moonshotai/kimi-k3")).toEqual(fast("moonshotai/kimi-k3", null));
    expect(patchBody(fast(null, "low"), fast("moonshotai/kimi-k3"))).toEqual({ model: "moonshotai/kimi-k3" });
    // Back to the default clears the stored effort with the pin.
    expect(applyModel(fast("moonshotai/kimi-k3", "low"), null)).toEqual(fast(null, null));
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

describe("providers a project cannot run", () => {
  // The table as `GET /thread-models` sends it (thread_model.PROFILES).
  const limits: ThreadModels = {
    providers: {
      fast: { ...TM.providers.fast!, profiles: ["auto", "ask"], always_ask: true },
      claude: { ...TM.providers.claude!, profiles: ["auto", "ask"], always_ask: true },
      codex: { ...TM.providers.codex!, profiles: ["auto"], always_ask: false },
    },
  };
  const project = (profile: "auto" | "ask" | "strict", always_ask: string[] = []) => ({ profile, always_ask });

  it("greys out Codex in an ask project or one with always-ask commands", () => {
    expect(providerRefusal(limits, "codex", project("ask"))).toContain("auto profile");
    expect(providerRefusal(limits, "codex", project("auto", ["make deploy"]))).toContain("make deploy");
    expect(providerRefusal(limits, "claude", project("ask", ["make deploy"]))).toBeNull();
    expect(providerRefusal(limits, "codex", project("auto"))).toBeNull();
  });

  it("greys out Claude and OpenRouter in a strict project", () => {
    expect(providerRefusal(limits, "claude", project("strict"))).toContain("strict");
    expect(providerRefusal(limits, "fast", project("strict"))).toContain("strict");
  });

  it("refuses nothing it has no table or project for", () => {
    expect(providerRefusal(TM, "codex", project("ask"))).toBeNull();
    expect(providerRefusal(limits, "codex", null)).toBeNull();
    expect(providerRefusal(null, "codex", project("ask"))).toBeNull();
  });
});

describe("chipState (Bugbot 2026-10-08)", () => {
  const opened = { projectId: "p1", openedId: "t9", provider: "fast" as const, model: "openai/gpt-5.6-luna", effort: "low" };

  it("keeps the chips after a failed first send, on the compose row's choice", () => {
    // The thread is on the server, its first message failed, and its record
    // is not loaded yet: the chips used to vanish here.
    const s = chipState({ threadId: null, compose: opened, threads: [] });
    expect(s.choice).toEqual({ provider: "fast", model: "openai/gpt-5.6-luna", effort: "low" });
    expect(s.targetId).toBe("t9");
    expect(s.composing).toBe(false);
    expect(s.editable).toEqual({ provider: false, model: true });
  });

  it("reads the opened thread's record once it is loaded", () => {
    const t = thread({ id: "t9", model: "x/y", effort: "high" });
    const s = chipState({ threadId: null, compose: opened, threads: [t] });
    expect(s.choice).toEqual({ provider: "fast", model: "x/y", effort: "high" });
    expect(s.thread?.id).toBe("t9");
  });

  it("composing and an open thread behave as before", () => {
    const c = chipState({ threadId: null, compose: { projectId: "p1" }, threads: [] });
    expect(c.composing).toBe(true);
    expect(c.editable).toEqual({ provider: true, model: true });
    expect(c.targetId).toBeNull();
    const t = thread({ id: "t1" });
    const open = chipState({ threadId: "t1", compose: null, threads: [t] });
    expect(open.targetId).toBe("t1");
    expect(chipState({ threadId: null, compose: null, threads: [t] }).choice).toBeNull();
  });
});

describe("a provider's default, set from the chip (2026-10-08)", () => {
  // The daemon's answer after "Set as default" on Sonnet at low.
  const SET: ThreadModels = {
    ...TM,
    providers: {
      ...TM.providers,
      claude: {
        ...TM.providers.claude!, default: "claude-sonnet-5-5", default_effort: "low",
        default_source: "hud", settable: true,
        hud_default: { model: "claude-sonnet-5-5", effort: "low" },
        builtin: { model: "claude-opus-5-5", effort: "high" },
        models: [
          { id: "claude-opus-5-5", name: "Claude Opus 5.5", efforts: ["low", "medium", "high", "xhigh", "max"],
            default_effort: "high" },
          { id: "claude-sonnet-5-5", name: "Claude Sonnet 5.5", efforts: ["low", "medium", "high", "xhigh", "max"],
            default_effort: "high" },
          { id: "claude-haiku-4-5", name: "<img src=x onerror=alert(1)>", efforts: [], default_effort: null },
        ],
      },
      codex: { ...TM.providers.codex!, default_source: "routing", settable: true, hud_default: null,
               builtin: { model: "gpt-6-astra", effort: "xhigh" } },
    },
  };
  const claude = (model: string | null = null, effort: string | null = null): Choice => ({ provider: "claude", model, effort });

  it("is offered for Claude and Codex only, and only by a backend that says so", () => {
    expect(canSetDefault(SET, "claude")).toBe(true);
    expect(canSetDefault(SET, "codex")).toBe(true);
    expect(canSetDefault(SET, "fast")).toBe(false);
    expect(canSetDefault(TM, "claude")).toBe(false);   // an older backend: no route
    expect(canSetDefault(null, "claude")).toBe(false);
  });

  it("badges the default and relabels default threads, never pinned ones", () => {
    expect(defaultRows(SET, "claude").filter((r) => r.isDefault).map((r) => r.id)).toEqual(["claude-sonnet-5-5"]);
    expect(modelLabel(SET, claude())).toBe("default · claude-sonnet-5-5");
    expect(modelOptions(SET, claude())[0].label).toBe("default · claude-sonnet-5-5");
    expect(effective(SET, claude())).toEqual({ model: "claude-sonnet-5-5", effort: "low" });
    expect(modelLabel(SET, claude("claude-opus-5-5"))).toBe("claude-opus-5-5");
    expect(effective(SET, claude("claude-opus-5-5"))).toEqual({ model: "claude-opus-5-5", effort: "high" });
    // An effort-only default thread runs its effort on the new default.
    expect(effective(SET, claude(null, "max"))).toEqual({ model: "claude-sonnet-5-5", effort: "max" });
  });

  it("keeps a model name as text, never markup", () => {
    const row = defaultRows(SET, "claude").find((r) => r.id === "claude-haiku-4-5");
    expect(row?.name).toBe("<img src=x onerror=alert(1)>");
  });

  it("says where the default comes from, and what Reset returns to", () => {
    expect(defaultSourceLabel(SET, "claude")).toBe("set here");
    expect(defaultSourceLabel(SET, "codex")).toBe("from routing");
    expect(defaultSourceLabel(TM, "claude")).toBe("built-in");
    expect(defaultChosenHere(SET, "claude")).toBe(true);
    expect(defaultChosenHere(SET, "codex")).toBe(false);
    expect(resetTitle(SET, "claude")).toBe("Back to the built-in default: claude-opus-5-5 · high");
    expect(resetTitle(SET, "codex")).toBe("Back to routing's Codex default: gpt-6-astra · xhigh");
  });

  it("offers the default model's own ladder, on the stored effort", () => {
    const eff = defaultEffortOptions(SET, "claude");
    expect(eff.value).toBe("low");
    expect(eff.options.map((o) => o.value)).toEqual(["", "low", "medium", "high", "xhigh", "max"]);
    expect(eff.options[0].label).toBe("model default · high");
    expect(defaultEffortOptions(SET, "codex").value).toBe("");
    const haiku: ThreadModels = { ...SET, providers: { ...SET.providers,
      claude: { ...SET.providers.claude!, default: "claude-haiku-4-5",
                hud_default: { model: "claude-haiku-4-5", effort: null } } } };
    expect(defaultEffortOptions(haiku, "claude").options).toEqual([]);
  });

  it("sends exactly the keys the route takes", () => {
    expect(defaultBody("claude", "claude-sonnet-5-5", "")).toEqual({ provider: "claude", model: "claude-sonnet-5-5" });
    expect(defaultBody("claude", "claude-sonnet-5-5", "low"))
      .toEqual({ provider: "claude", model: "claude-sonnet-5-5", effort: "low" });
    expect(defaultBody("codex", "", "low")).toEqual({ provider: "codex", model: "" });
  });
});
