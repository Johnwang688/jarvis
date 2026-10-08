// The Discord light (PR A, plan §1): one colour and one sentence from
// `GET /discord`. Green when the poster is fine, amber with the reason when
// it is working around something (a broken channel, a failed sync, a recent
// failure), red when it is down. "Off" is grey: no Discord is configured,
// which is a fact about the setup, not a fault.

import type { DiscordStatus, Project, ProjectChannel, Thread } from "../types";

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
  // B1: the server-wide permission check. Administrator is amber too: it
  // works, but it is the setting decisions D5 rule out.
  const perms = status.guild?.configured ? status.permissions : null;
  if (perms?.missing?.length) {
    return { level: "warn", text: `Discord · the bot is missing ${perms.missing.join(", ")}` };
  }
  if (perms?.administrator) {
    return { level: "warn", text: "Discord · the bot has Administrator; remove it (only the 8 permissions are needed)" };
  }
  if (status.linker?.state === "degraded") {
    return { level: "warn", text: `Discord · ${status.linker.reason || "channel housekeeping is behind"}` };
  }
  // PR C: the chat mirror. Amber either way — a chat's lines wait and are
  // caught up from its log, so nothing here is lost, only late.
  const mirror = status.mirror;
  if (mirror && mirror.state !== "ok") {
    return { level: "warn", text: `Discord · chats: ${mirror.reason || mirror.state}` };
  }
  return { level: "ok", text: "Discord ok" };
}

/** PR C: where an open chat lives on Discord, for its header —
 * "On Discord: #school › Essay plan" with a link, or the DM. Null before the
 * chat's first message is mirrored (or for a retired DM chat). */
export function chatPlace(thread: Thread | null | undefined): { text: string; url: string | null } | null {
  const place = thread?.discord;
  if (!thread?.surface || !place) return null;
  if (place.kind === "dm") return { text: "On Discord: your DM with Jarvis", url: null };
  const channel = place.channel ? `#${place.channel} › ` : "";
  // Only Discord's own web address is ever a link here.
  const url = place.url && /^https:\/\/discord\.com\/channels\/\d+\/\d+$/.test(place.url) ? place.url : null;
  return { text: `On Discord: ${channel}${place.name}`, url };
}

/** Is the server set up (`jarvis auth discord-guild`)? */
export function guildConfigured(status: DiscordStatus | null): boolean {
  return !!status?.guild?.configured;
}

/** The projects the one-time backfill would give a channel: live, not the
 * Inbox (the daemon links it to #ungrouped), and not linked yet. The backend
 * also skips a project whose folder is missing, and says so. */
export function backfillTargets(projects: Project[]): Project[] {
  return projects.filter((p) => !p.inbox && !p.archived && !p.discord_channel_id);
}

/** One project's channel, as a pill: a colour and a few words. */
export function channelPill(view: ProjectChannel | null): DiscordLight {
  if (!view) return { level: "off", text: "checking…" };
  const name = view.name ? `#${view.name}` : view.channel_id ? `channel ${view.channel_id}` : "";
  switch (view.state) {
    case "linked_ok":
      return { level: "ok", text: `${name}${view.category ? ` · ${view.category}` : ""}` };
    case "unlinked":
      return { level: "off", text: "no channel" };
    case "unconfigured":
      return { level: "off", text: "Discord server not set up" };
    case "folder_missing":
      return { level: "warn", text: "the project folder is missing" };
    case "missing_permissions":
      return { level: "warn", text: `${name} · bot is missing ${view.missing.join(", ")}` };
    case "not_found":
      return { level: "down", text: `channel ${view.channel_id} no longer exists` };
    case "no_access":
      return { level: "down", text: `the bot cannot see channel ${view.channel_id}` };
    case "wrong_guild":
      return { level: "down", text: `${name} is in another server` };
    case "unreachable":
      return { level: "warn", text: "Discord could not be reached" };
  }
  return { level: "off", text: String(view.state) };
}

export function originText(origin: ProjectChannel["origin"]): string {
  if (origin === "created") return "created by Jarvis";
  if (origin === "linked") return "linked by you";
  return "";
}
