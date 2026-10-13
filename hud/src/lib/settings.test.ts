import { loadSettingsSection, saveSettingsSection, settingsSection, isSettingsKey, SETTINGS_SECTIONS, SETTINGS_SECTION_KEY } from "./settings";
describe("Settings window preference", () => {
  afterEach(() => { vi.restoreAllMocks(); localStorage.clear(); });
  it("accepts only the sections in their displayed order", () => {
    expect(SETTINGS_SECTIONS).toEqual(["General", "Model", "Voice", "Avatar", "Usage", "Routing", "Browser", "Archive"]);
    for (const name of SETTINGS_SECTIONS) expect(settingsSection(name)).toBe(name);
    for (const bad of [null, "", "general", "Terminal", {}, "__proto__"]) expect(settingsSection(bad)).toBe("General");
  });
  it("loads and remembers a valid section, falls back on corrupt or inaccessible storage", () => {
    saveSettingsSection("Routing");
    expect(loadSettingsSection()).toBe("Routing");
    localStorage.setItem(SETTINGS_SECTION_KEY, "bad");
    expect(loadSettingsSection()).toBe("General");
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    expect(loadSettingsSection()).toBe("General");
    expect(() => saveSettingsSection("Model")).not.toThrow();
  });
  it("takes exactly Ctrl+, without stealing other modified commas", () => {
    const key = { key: ",", ctrlKey: true, altKey: false, metaKey: false, shiftKey: false };
    expect(isSettingsKey(key)).toBe(true);
    for (const change of [{ key: "<" }, { ctrlKey: false }, { altKey: true }, { metaKey: true }, { shiftKey: true }])
      expect(isSettingsKey({ ...key, ...change })).toBe(false);
  });
});
