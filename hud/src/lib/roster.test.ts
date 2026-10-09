import { describe, expect, it } from "vitest";
import {
  chosenHere, configDefaultLine, CONFIG_ENV, effectiveDefault, refusal, rosterIds, rowBadges,
  type RosterView,
} from "./roster";

const ENV = "openai/gpt-5.6-luna";
const KIMI = "moonshotai/kimi-k3";

const following: RosterView = {
  models: [{ id: ENV, name: "Luna" }, { id: KIMI, name: "Kimi" }],
  selected: "",
  default: ENV,
  current: ENV,
  default_source: "config",
};
const chosen: RosterView = { ...following, selected: KIMI, current: KIMI, default_source: "hud" };
const unpinned: RosterView = { ...chosen, models: [{ id: KIMI, name: "Kimi" }] };

describe("the default the picker shows", () => {
  it("is the config default while nothing is chosen", () => {
    expect(effectiveDefault(following)).toBe(ENV);
    expect(chosenHere(following)).toBe(false);
  });
  it("is the HUD's choice once one is made", () => {
    expect(effectiveDefault(chosen)).toBe(KIMI);
    expect(chosenHere(chosen)).toBe(true);
  });
  it("falls back to selected, then default, on an older describe()", () => {
    expect(effectiveDefault({ models: [], selected: KIMI, default: ENV })).toBe(KIMI);
    expect(effectiveDefault({ models: [], selected: "", default: ENV })).toBe(ENV);
    expect(effectiveDefault(null)).toBe("");
  });
});

describe("row badges", () => {
  it("mark exactly one default, and the config model as config", () => {
    expect(rowBadges(following, ENV)).toEqual(["default", "config"]);
    expect(rowBadges(following, KIMI)).toEqual([]);
    expect(rowBadges(chosen, KIMI)).toEqual(["default"]);
    expect(rowBadges(chosen, ENV)).toEqual(["config"]);
  });
});

describe("the config default line", () => {
  it("names the env var and says when it is in use", () => {
    const line = configDefaultLine(following);
    expect(line).toContain(`config default (${CONFIG_ENV}): ${ENV}`);
    expect(line).toContain("in use");
  });
  it("says it is only a fallback once the HUD chose", () => {
    expect(configDefaultLine(chosen)).toContain("used only when nothing is chosen here");
    expect(configDefaultLine(chosen)).not.toContain("unpinned");
  });
  it("says when the env model has been unpinned", () => {
    expect(configDefaultLine(unpinned)).toContain("unpinned");
  });
});

describe("roster ids and refusals", () => {
  it("lists the roster's ids", () => {
    expect(rosterIds(chosen)).toEqual([ENV, KIMI]);
    expect(rosterIds(null)).toEqual([]);
  });
  it("keeps the backend's words, and is never blank", () => {
    expect(refusal(`unpin ${ENV}`, new Error("choose another default first")))
      .toBe(`Could not unpin ${ENV}: choose another default first`);
    expect(refusal("unpin x", {})).toBe("Could not unpin x: the request failed");
    expect(refusal("unpin x", null)).toBe("Could not unpin x: the request failed");
  });
});
