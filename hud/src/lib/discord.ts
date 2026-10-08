// The Discord light (PR A, plan §1): one colour and one sentence from
// `GET /discord`. Green when the poster is fine, amber with the reason when
// it is working around something (a broken channel, a failed sync, a recent
// failure), red when it is down. "Off" is grey: no Discord is configured,
// which is a fact about the setup, not a fault.

import type { DiscordStatus } from "../types";

export type DiscordLevel = "ok" | "warn" | "down" | "off";

export interface DiscordLight {
  level: DiscordLevel;
  text: string;
}

function lastError(status: DiscordStatus): string {
  const e = status.reporter?.last_error;
  if (!e) return "";
  const parts = [e.op];
  if (e.status != null) parts.push(`HTTP ${e.status}`);
  if (e.code != null) parts.push(`code ${e.code}`);
  return parts.join(" · ");
}

export function discordLight(status: DiscordStatus | null): DiscordLight {
  if (!status) return { level: "off", text: "Discord status not loaded" };
  const reporter = status.reporter;
  if (!status.connected && !reporter && status.commands?.state === "off") {
    return { level: "off", text: "Discord off" };
  }
  if (reporter?.state === "down") {
    const why = reporter.reason || lastError(status) || "failing";
    return { level: "down", text: `Discord down · ${why}` };
  }
  if (!status.connected && status.commands?.state === "failed") {
    // The surface never came up (or its sync failed before it connected).
    return { level: "down", text: `Discord down · did not start${status.commands.error ? ` (${status.commands.error})` : ""}` };
  }
  if (!status.connected) {
    return { level: "warn", text: "Discord not connected yet" };
  }
  if (reporter?.state === "degraded") {
    const why = reporter.reason || lastError(status) || "degraded";
    return { level: "warn", text: `Discord · ${why}` };
  }
  if (status.commands?.state === "failed") {
    return { level: "warn", text: `Discord · commands not synced${status.commands.error ? ` (${status.commands.error})` : ""}` };
  }
  return { level: "ok", text: "Discord ok" };
}
