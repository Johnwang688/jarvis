/** Window preferences only; no daemon configuration is written here. */
import { readKey, writeKey } from "./storage";
export const SETTINGS_SECTIONS = ["General", "Model", "Voice", "Avatar", "Usage", "Routing", "Browser", "Archive"] as const;
export type SettingsSection = typeof SETTINGS_SECTIONS[number];
export const SETTINGS_SECTION_KEY = "jarvis.settings.section";
export function settingsSection(value: unknown): SettingsSection {
  return SETTINGS_SECTIONS.includes(value as SettingsSection) ? value as SettingsSection : "General";
}
export function loadSettingsSection(): SettingsSection {
  return settingsSection(readKey(SETTINGS_SECTION_KEY));
}
export function saveSettingsSection(section: SettingsSection): void {
  // A storage refusal must not prevent this window using Settings.
  writeKey(SETTINGS_SECTION_KEY, section);
}
export function isSettingsKey(e: Pick<KeyboardEvent, "key" | "ctrlKey" | "altKey" | "metaKey" | "shiftKey">): boolean {
  return e.ctrlKey && !e.altKey && !e.metaKey && !e.shiftKey && e.key === ",";
}
