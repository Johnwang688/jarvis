import { describe, expect, it } from "vitest";
import { backfillTargets, channelPill, discordLight, guildConfigured, originText } from "./discord";
import type { DiscordReporter, DiscordStatus, Project, ProjectChannel } from "../types";

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

describe("B1: the server and the project channels", () => {
  const configured = (over: Partial<DiscordStatus> = {}) => status({
    guild: { configured: true, id: "800000000000000001" },
    linker: { state: "ok", reason: "", pending_renames: 0, awaiting_approval: 0 },
    permissions: { missing: [], excess: [], administrator: false, checked_at: 1 },
    ...over,
  });

  it("stays green when the server is set up and the permissions are right", () => {
    expect(discordLight(configured())).toEqual({ level: "ok", text: "Discord ok" });
    expect(guildConfigured(configured())).toBe(true);
    expect(guildConfigured(status())).toBe(false);
    expect(guildConfigured(null)).toBe(false);
  });

  it("is amber naming a missing permission, and on Administrator", () => {
    const missing = discordLight(configured({ permissions: {
      missing: ["Attach Files", "Embed Links"], excess: [], administrator: false, checked_at: 1 } }));
    expect(missing).toEqual({ level: "warn", text: "Discord · the bot is missing Attach Files, Embed Links" });
    const admin = discordLight(configured({ permissions: {
      missing: [], excess: ["Administrator"], administrator: true, checked_at: 1 } }));
    expect(admin.level).toBe("warn");
    expect(admin.text).toContain("Administrator");
  });

  it("is amber while a channel rename waits out a rate limit", () => {
    const light = discordLight(configured({ linker: {
      state: "degraded", reason: "1 channel rename(s) pending (Discord rate limit)",
      pending_renames: 1, awaiting_approval: 0 } }));
    expect(light).toEqual({ level: "warn", text: "Discord · 1 channel rename(s) pending (Discord rate limit)" });
  });

  const view = (over: Partial<ProjectChannel> = {}): ProjectChannel => ({
    channel_id: "830000000000000001", origin: "created", name: "school", category: "Jarvis",
    state: "linked_ok", missing: [], checked_at: 1, rename_pending: false, ...over,
  });

  it("draws one pill per channel state", () => {
    expect(channelPill(view())).toEqual({ level: "ok", text: "#school · Jarvis" });
    expect(channelPill(view({ state: "unlinked", channel_id: null, name: null })).text).toBe("no channel");
    expect(channelPill(view({ state: "unconfigured" })).level).toBe("off");
    expect(channelPill(view({ state: "missing_permissions", missing: ["Attach Files"] })))
      .toEqual({ level: "warn", text: "#school · bot is missing Attach Files" });
    expect(channelPill(view({ state: "not_found", name: null })).level).toBe("down");
    expect(channelPill(view({ state: "no_access", name: null })).level).toBe("down");
    expect(channelPill(view({ state: "wrong_guild" })).level).toBe("down");
    expect(channelPill(view({ state: "folder_missing" })).text).toContain("folder");
    expect(channelPill(view({ state: "unreachable" })).level).toBe("warn");
    expect(channelPill(null).level).toBe("off");
  });

  it("says who made the link", () => {
    expect(originText("created")).toBe("created by Jarvis");
    expect(originText("linked")).toBe("linked by you");
    expect(originText(null)).toBe("");
  });

  it("backfills live, unlinked projects and never the Inbox", () => {
    const p = (id: string, over: Partial<Project> = {}): Project => ({
      id, name: id, root: `/r/${id}`, created: "", profile: "auto",
      routing: {} as Project["routing"], discord_channel_id: null, extra_dirs: [], always_ask: [],
      inbox: false, ...over,
    });
    const targets = backfillTargets([p("a"), p("b", { discord_channel_id: "1" }),
      p("inbox", { inbox: true }), p("old", { archived: "2026-01-01" })]);
    expect(targets.map((x) => x.id)).toEqual(["a"]);
  });
});
