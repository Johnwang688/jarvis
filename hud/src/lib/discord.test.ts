import { describe, expect, it } from "vitest";
import { discordLight } from "./discord";
import type { DiscordReporter, DiscordStatus } from "../types";

const reporter = (over: Partial<DiscordReporter> = {}): DiscordReporter => ({
  state: "ok", reason: "", counters: {}, dropped: 0, last_error: null, ...over,
});

const status = (over: Partial<DiscordStatus> = {}): DiscordStatus => ({
  connected: true,
  commands: { state: "ok", count: 11, synced_at: 1, error: null },
  reporter: reporter(),
  ...over,
});

describe("the Discord light", () => {
  it("is green when connected, synced and posting", () => {
    expect(discordLight(status())).toEqual({ level: "ok", text: "Discord ok" });
  });

  it("is grey when Discord is not set up at all", () => {
    expect(discordLight(status({ connected: false, reporter: null,
      commands: { state: "off", count: 0, synced_at: null, error: null } })).level).toBe("off");
    expect(discordLight(null).level).toBe("off");
  });

  it("is amber with the reason when a channel is broken", () => {
    const light = discordLight(status({ reporter: reporter({
      state: "degraded", reason: "channel 12 is missing a bot permission (50013); attention updates go to your DM" }) }));
    expect(light.level).toBe("warn");
    expect(light.text).toContain("50013");
    expect(light.text).toContain("DM");
  });

  it("falls back to the last error when no reason is given", () => {
    const light = discordLight(status({ reporter: reporter({
      state: "degraded", last_error: { op: "edit_card", status: 503, code: null, at: 1 } }) }));
    expect(light.text).toBe("Discord · edit_card · HTTP 503");
  });

  it("is red when the poster is down, whatever else is true", () => {
    const light = discordLight(status({ reporter: reporter({
      state: "down", reason: "", last_error: { op: "create_thread", status: 429, code: null, at: 1 } }) }));
    expect(light).toEqual({ level: "down", text: "Discord down · create_thread · HTTP 429" });
  });

  it("is amber when connecting, or when the command sync failed", () => {
    expect(discordLight(status({ connected: false })).level).toBe("warn");
    const failed = discordLight(status({ commands: { state: "failed", count: 0, synced_at: null, error: "DiscordHTTPError" } }));
    expect(failed.level).toBe("warn");
    expect(failed.text).toContain("commands not synced");
  });

  it("is red, not pending, when the Discord surface failed to start", () => {
    const failed = status({ connected: false, reporter: null,
      commands: { state: "failed", count: 0, synced_at: null, error: "RuntimeError" } });
    expect(discordLight(failed)).toEqual({ level: "down", text: "Discord down · did not start (RuntimeError)" });
    const routed = status({ connected: false,
      commands: { state: "failed", count: 0, synced_at: null, error: "RuntimeError" },
      reporter: reporter({ state: "down", reason: "the Discord surface did not start (RuntimeError)" }) });
    expect(discordLight(routed).level).toBe("down");
  });
});
