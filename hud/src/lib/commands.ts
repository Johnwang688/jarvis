// The command palette's registry (ED-7, editor plan §2.8): one static list of
// the HUD's commands, plus — when an editor had focus — Monaco's own actions.
// Pure, so it is tested without a window. App runs a command by its id.
//
// **No command downloads to the robot, talks to a VEX device or runs a
// program on one** (decisions V-2/V-3: a download is only ever the VEX view's
// own button, a deliberate click). `forbidden` is the backstop: anything whose
// id or label reads like one is dropped, whatever offered it.

import { fuzzyFilter } from "./fuzzy";

export type CommandId =
  | "file.quickOpen" | "search.findInFiles" | "file.newFile" | "file.newFolder"
  | "file.save" | "file.saveAll" | "file.reload" | "file.closeTab" | "file.nextTab" | "file.prevTab"
  | "file.copyPath" | "chat.addSelection" | "chat.newThread"
  | "view.files" | "view.toggleSidebar" | "view.toggleRight" | "view.togglePanel"
  | "view.zoomIn" | "view.zoomOut" | "view.zoomReset"
  | "view.editorSettings"
  // VX-3: opens the dialog; it makes nothing until the owner approves its card.
  | "project.newVex";

export interface CommandContext {
  /** A File pane is the target (focused, else drawn): file commands apply. */
  filePane: boolean;
  /** That pane has a file open. */
  activeFile: boolean;
  /** An editor had focus when the palette opened: its actions are offered. */
  editor: boolean;
}

export interface Command {
  id: CommandId;
  label: string;
  keys?: string;
  when?: (c: CommandContext) => boolean;
}

const file = (c: CommandContext) => c.filePane;
const open = (c: CommandContext) => c.filePane && c.activeFile;

export const COMMANDS: readonly Command[] = Object.freeze([
  { id: "file.quickOpen", label: "Go to File…", keys: "Ctrl+P" },
  { id: "search.findInFiles", label: "Find in Files", keys: "Ctrl+Shift+F" },
  { id: "file.newFile", label: "New File…", when: file },
  { id: "file.newFolder", label: "New Folder…", when: file },
  { id: "file.save", label: "Save", keys: "Ctrl+S", when: open },
  { id: "file.saveAll", label: "Save All" },
  { id: "file.reload", label: "Reload File from Disk", when: open },
  { id: "file.closeTab", label: "Close Editor", when: open },
  { id: "file.nextTab", label: "Next Editor Tab", keys: "Ctrl+PageDown", when: open },
  { id: "file.prevTab", label: "Previous Editor Tab", keys: "Ctrl+PageUp", when: open },
  { id: "file.copyPath", label: "Copy Relative Path of Active File", when: open },
  { id: "chat.addSelection", label: "Add Selection to Chat", when: open },
  { id: "chat.newThread", label: "New Thread" },
  { id: "view.files", label: "Show Editor" },
  { id: "view.toggleSidebar", label: "Toggle Sidebar", keys: "Ctrl+B" },
  { id: "view.toggleRight", label: "Toggle Right Pane", keys: "Ctrl+Alt+B" },
  { id: "view.togglePanel", label: "Toggle Terminal Panel", keys: "Ctrl+`" },
  { id: "view.zoomIn", label: "Zoom In", keys: "Ctrl+=" },
  { id: "view.zoomOut", label: "Zoom Out", keys: "Ctrl+-" },
  { id: "view.zoomReset", label: "Reset Zoom", keys: "Ctrl+0" },
  // ED-8: font, tabs, wrap, minimap, whitespace, cursor, vim.
  { id: "view.editorSettings", label: "Editor Settings…" },
  { id: "project.newVex", label: "New VEX Project from a Template…" },
] as Command[]);

/** Never in the palette: a download, a VEX device, the brain, `vexcom`. */
export function forbidden(entry: { id: string; label: string }): boolean {
  const text = `${entry.id} ${entry.label}`;
  return /download|vexcom|\bdevice\b|\bbrain\b|\brobot\b|run program|upload/i.test(text);
}

/** One of Monaco's editor actions, as `getSupportedActions()` gives it. */
export interface EditorAction {
  id: string;
  label: string;
}

export interface Entry {
  /** `hud:<command>` or `monaco:<action id>`. */
  key: string;
  label: string;
  keys?: string;
  source: "hud" | "editor";
}

// Monaco's own palette would open over ours; and the save action is ED-1's
// per-editor Ctrl+S, which "Save" already is.
const EDITOR_SKIP = new Set(["editor.action.quickCommand", "jarvis.saveFile"]);

/** What the palette offers in this context, HUD commands first. */
export function paletteEntries(ctx: CommandContext, actions: readonly EditorAction[] = []): Entry[] {
  const out: Entry[] = [];
  const labels = new Set<string>();
  for (const c of COMMANDS) {
    if (c.when && !c.when(ctx)) continue;
    if (forbidden(c)) continue;
    out.push({ key: `hud:${c.id}`, label: c.label, keys: c.keys, source: "hud" });
    labels.add(c.label.toLowerCase());
  }
  if (ctx.editor) {
    for (const a of actions) {
      if (!a || typeof a.id !== "string" || typeof a.label !== "string" || !a.label) continue;
      if (EDITOR_SKIP.has(a.id) || forbidden(a)) continue;
      const label = a.label.length > 120 ? a.label.slice(0, 120) : a.label;
      if (labels.has(label.toLowerCase())) continue;
      labels.add(label.toLowerCase());
      out.push({ key: `monaco:${a.id}`, label, source: "editor" });
    }
  }
  return out;
}

/** The entries for `query`, best first (an empty query keeps their order). */
export function filterEntries(query: string, entries: readonly Entry[], limit = 200) {
  return fuzzyFilter(query, entries, (e) => e.label, limit);
}

export function commandOf(key: string): CommandId | null {
  if (!key.startsWith("hud:")) return null;
  const id = key.slice(4);
  return COMMANDS.some((c) => c.id === id) ? (id as CommandId) : null;
}
