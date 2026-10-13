/** Window preferences only; no daemon configuration is written here. */
export const SETTINGS_SECTIONS = ["General", "Model", "Voice", "Avatar", "Usage", "Routing", "Archive"] as const;
export type SettingsSection = typeof SETTINGS_SECTIONS[number];
export const SETTINGS_SECTION_KEY = "jarvis.settings.section";
export function settingsSection(value: unknown): SettingsSection {
  return SETTINGS_SECTIONS.includes(value as SettingsSection) ? value as SettingsSection : "General";
}
export function loadSettingsSection(): SettingsSection {
  try { return settingsSection(window.localStorage.getItem(SETTINGS_SECTION_KEY)); }
  catch { return "General"; }
}
export function saveSettingsSection(section: SettingsSection): void {
  try { window.localStorage.setItem(SETTINGS_SECTION_KEY, section); }
  catch { /* A storage refusal must not prevent this window using Settings. */ }
}
export function isSettingsKey(e: Pick<KeyboardEvent, "key" | "ctrlKey" | "altKey" | "metaKey" | "shiftKey">): boolean {
  return e.ctrlKey && !e.altKey && !e.metaKey && !e.shiftKey && e.key === ",";
}
