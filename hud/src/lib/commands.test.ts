import { describe, expect, it } from "vitest";
import { COMMANDS, commandOf, filterEntries, forbidden, paletteEntries, type CommandContext } from "./commands";
import { isEditorShortcut, shortcutFor } from "./layout";
import { terminalTakesKey } from "./terminal";

const ALL: CommandContext = { filePane: true, activeFile: true, editor: true };
const NONE: CommandContext = { filePane: false, activeFile: false, editor: false };

describe("the registry", () => {
  it("has unique ids and labels, and every id runs", () => {
    expect(new Set(COMMANDS.map((c) => c.id)).size).toBe(COMMANDS.length);
    expect(new Set(COMMANDS.map((c) => c.label)).size).toBe(COMMANDS.length);
    for (const c of COMMANDS) expect(commandOf(`hud:${c.id}`)).toBe(c.id);
    expect(commandOf("hud:nope")).toBeNull();
    expect(commandOf("monaco:editor.action.commentLine")).toBeNull();
  });

  it("has no download or VEX device entry, and drops one an editor offers", () => {
    for (const c of COMMANDS) expect(forbidden(c)).toBe(false);
    const offered = [
      { id: "vex.download", label: "VEX: Download to Brain" },
      { id: "x.run", label: "Download program" },
      { id: "vexcom.run", label: "Run vexcom" },
      { id: "dev.info", label: "VEX: Device Info" },
      { id: "editor.action.commentLine", label: "Toggle Line Comment" },
    ];
    const keys = paletteEntries(ALL, offered).map((e) => e.key);
    expect(keys).toContain("monaco:editor.action.commentLine");
    expect(keys.filter((k) => k !== "hud:project.newVex" && /download|vex|dev\.info/i.test(k))).toEqual([]);
    expect(paletteEntries(ALL, offered).some((e) => /download|device|brain|vexcom/i.test(e.label))).toBe(false);
    // VX-3: the HUD's one VEX entry opens the New VEX project dialog, which
    // makes a folder only on the owner's yes and never touches a device.
    expect(keys.filter((k) => /vex/i.test(k))).toEqual(["hud:project.newVex"]);
  });

  it("offers New VEX Project everywhere (VX-3)", () => {
    expect(paletteEntries(NONE).some((e) => e.key === "hud:project.newVex")).toBe(true);
    expect(commandOf("hud:project.newVex")).toBe("project.newVex");
  });

  it("offers file commands only with a File pane, Monaco's only with an editor", () => {
    const none = paletteEntries(NONE, [{ id: "editor.action.commentLine", label: "Toggle Line Comment" }]);
    expect(none.some((e) => e.key === "hud:file.save")).toBe(false);
    expect(none.some((e) => e.source === "editor")).toBe(false);
    expect(none.some((e) => e.key === "hud:file.quickOpen")).toBe(true);
    const all = paletteEntries(ALL, [{ id: "editor.action.quickCommand", label: "Command Palette" },
      { id: "jarvis.saveFile", label: "Save" }]);
    expect(all.some((e) => e.key === "monaco:editor.action.quickCommand")).toBe(false);
    expect(all.filter((e) => e.label === "Save")).toHaveLength(1);
  });

  it("filters by label, fuzzily", () => {
    const got = filterEntries("togside", paletteEntries(ALL));
    expect(got[0].item.key).toBe("hud:view.toggleSidebar");
    expect(filterEntries("", paletteEntries(NONE)).length).toBe(paletteEntries(NONE).length);
  });
});

const k = (key: string, over: Partial<{ ctrlKey: boolean; shiftKey: boolean; altKey: boolean; metaKey: boolean; code: string }> = {}) =>
  ({ key, ctrlKey: false, shiftKey: false, altKey: false, metaKey: false, ...over });

describe("the keys (editor plan §2.10)", () => {
  it("Ctrl+P quick open, Ctrl+Shift+P and F1 the palette, Ctrl+Shift+F find in files", () => {
    expect(shortcutFor(k("p", { ctrlKey: true }))).toBe("quickOpen");
    expect(shortcutFor(k("P", { ctrlKey: true, shiftKey: true }))).toBe("palette");
    expect(shortcutFor(k("F1"))).toBe("palette");
    expect(shortcutFor(k("F", { ctrlKey: true, shiftKey: true }))).toBe("findInFiles");
    expect(isEditorShortcut("quickOpen") && isEditorShortcut("palette") && isEditorShortcut("findInFiles")).toBe(true);
    expect(isEditorShortcut("zoomIn")).toBe(false);
  });

  it("leaves AltGr, a modified F1, plain letters and Ctrl+F alone", () => {
    expect(shortcutFor(k("p", { ctrlKey: true, altKey: true }))).toBeNull();
    expect(shortcutFor(k("F1", { shiftKey: true }))).toBeNull();
    expect(shortcutFor(k("F1", { ctrlKey: true }))).toBeNull();
    expect(shortcutFor(k("p"))).toBeNull();
    expect(shortcutFor(k(" "))).toBeNull();
    expect(shortcutFor(k("f", { ctrlKey: true }))).toBeNull();
    expect(shortcutFor(k("d", { ctrlKey: true }))).toBeNull();
  });

  it("in a terminal Ctrl+P is the shell's; the palette and find in files are not", () => {
    expect(terminalTakesKey("quickOpen", "p")).toBe(true);
    expect(terminalTakesKey("palette", "P")).toBe(false);
    expect(terminalTakesKey("findInFiles", "F")).toBe(false);
  });
});
