// The window's editor settings (ED-8): the one live copy of `lib/editorSettings`'s
// record, stored in localStorage `jarvis.hud.editor` (behind try/catch, so a
// private window or blocked storage just keeps the defaults), and **applied to
// every live editor at once**: each editor listens here and takes a change in
// the same call that made it — never a render later.
//
// Another window of the HUD changing them is heard through the `storage`
// event, so two windows never draw two sets.

import { useSyncExternalStore } from "react";
import {
  DEFAULTS, loadSettings, parseSettings, sameSettings, saveSettings, SETTINGS_KEY, withChange,
  type EditorSettings,
} from "../lib/editorSettings";

let current: EditorSettings = loadSettings();
let version = 0;
const listeners = new Set<(s: EditorSettings) => void>();

function publish(next: EditorSettings) {
  if (sameSettings(next, current)) return;
  current = next;
  version += 1;
  for (const f of Array.from(listeners)) {
    try {
      f(current);
    } catch {
      /* one editor that cannot take a setting never stops the others */
    }
  }
}

/** The settings as they are now. */
export const editorSettings = (): EditorSettings => current;

/**
 * Change some settings (the dialog): a field the change gets wrong keeps its
 * current value. Stored, then told to every editor. Returns what is in force.
 */
export function setEditorSettings(change: Record<string, unknown>): EditorSettings {
  const next = withChange(current, change);
  saveSettings(next);
  publish(next);
  return current;
}

/** Every setting back to its default. */
export function resetEditorSettings(): EditorSettings {
  const next = { ...DEFAULTS };
  saveSettings(next);
  publish(next);
  return current;
}

/** Told every change, synchronously; returns the unsubscribe. */
export function onEditorSettings(f: (s: EditorSettings) => void): () => void {
  listeners.add(f);
  return () => listeners.delete(f);
}

const subscribe = (f: () => void) => onEditorSettings(() => f());
const getVersion = () => version;

/** The settings, for a component that draws from them. */
export function useEditorSettings(): EditorSettings {
  useSyncExternalStore(subscribe, getVersion);
  return current;
}

// Another window stored new settings: take them (each field checked again).
if (typeof window !== "undefined") {
  try {
    window.addEventListener("storage", (e) => {
      if (e.key !== SETTINGS_KEY) return;
      publish(parseSettings(e.newValue));
    });
  } catch {
    /* no window events: one window only */
  }
}

// ---- the editors, for the test hook ------------------------------------------

/** Every File pane editor that takes the settings, and whether vim drives it. */
const editors = new Map<any, { vim: boolean }>();

export function trackEditor(ed: any, info: { vim: boolean }) {
  editors.set(ed, info);
}

export function untrackEditor(ed: any) {
  editors.delete(ed);
}

/**
 * `window.__hud.editorSettings()` (tests only): what is stored and in force,
 * and what each File pane editor and its model really carry — never any text.
 */
export function editorSettingsProbe() {
  let stored: string | null = null;
  try {
    stored = localStorage.getItem(SETTINGS_KEY);
  } catch {
    stored = null;
  }
  const list = [];
  for (const [ed, info] of editors) {
    let raw: any = {};
    try {
      raw = ed.getRawOptions?.() ?? {};
    } catch {
      raw = {};
    }
    const model = ed.getModel?.();
    const mo = model?.getOptions?.();
    list.push({
      uri: model?.uri?.toString?.() ?? null,
      fontFamily: raw.fontFamily ?? null,
      fontSize: raw.fontSize ?? null,
      wordWrap: raw.wordWrap ?? null,
      minimap: raw.minimap?.enabled ?? null,
      stickyScroll: raw.stickyScroll?.enabled ?? null,
      renderWhitespace: raw.renderWhitespace ?? null,
      bracketPairs: raw.bracketPairColorization?.enabled ?? null,
      cursorStyle: raw.cursorStyle ?? null,
      vim: info.vim,
      model: mo ? {
        insertSpaces: mo.insertSpaces, tabSize: mo.tabSize, indentSize: mo.indentSize,
        eol: model.getEOL?.() === "\r\n" ? "CRLF" : "LF",
      } : null,
    });
  }
  return { settings: { ...current }, stored, editors: list };
}
