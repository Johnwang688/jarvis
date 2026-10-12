// Vim keybindings for a File pane's editor (ED-8, decisions E-11): `monaco-vim`
// 0.4.4, pinned exactly, loaded only when the setting is on (its own lazy
// chunk), and attached to one editor at a time with a status line of its own
// (the mode, and the `:` command line).
//
// **`:w` is that editor's own guarded save.** monaco-vim's `:write` calls the
// adapter's `save()` (its global `commands.save` hook is never set here), so
// each editor's `:w` runs the save of the buffer it shows — ED-2's, against the
// etag the buffer was read or saved as, a 409 the conflict bar — and nothing is
// registered window-wide (never `addCommand`: ED-1's review, #43's H1). Under
// an approval card it does nothing.
//
// **Push-to-talk keeps Ctrl+D in the editor, vim or not** (the owner's
// decision, 2026-10-12): the window takes Ctrl+D in the capture phase before
// Monaco — so before vim — sees it, and vim's half-page-down is not there. The
// HUD's other window keys (Ctrl+B, Ctrl+P, the zoom keys …) are the HUD's too.
//
// The library adds no network, storage or `innerHTML`: its status line is
// built from text nodes and elements (audited at 0.4.4). It imports Monaco by
// its pre-0.56 deep paths, which `vite.config.ts` aliases to the same modules
// the HUD's Monaco is built from.

import { editorsBlocked } from "../lib/monaco";

export interface VimHandle {
  dispose(): void;
}

let neutralised = false;

/**
 * What vim must not do in this window, set once on its shared tables (review
 * of PR #59):
 *  - **`:colorscheme <x>` does nothing.** monaco-vim sets it with Monaco's
 *    `setTheme`, which is window-wide: every editor would leave the Graphite
 *    scheme (ED-3), and an unknown name falls back to the light `vs` theme,
 *    red strings and all.
 *  - **`u`, Ctrl+R, `:undo` and `:redo` do nothing on a read-only editor**
 *    (a read-only kind, or any editor under an approval card): monaco-vim
 *    calls the model's `undo()`/`redo()` directly, past the editor's
 *    read-only option.
 */
function neutralise(VimMode: any) {
  if (neutralised) return;
  neutralised = true;
  const Vim = VimMode.Vim;
  Vim.defineEx("colorscheme", "colo", () => {});
  const guarded = (op: "undo" | "redo") => (cm: any) => {
    if (editorsBlocked() || cm?.getOption?.("readOnly")) return;
    cm?.editor?.getModel?.()?.[op]?.();
  };
  VimMode.commands.undo = guarded("undo");
  VimMode.commands.redo = guarded("redo");
  Vim.defineEx("undo", "u", guarded("undo"));
  Vim.defineEx("redo", "red", guarded("redo"));
}

/**
 * Attach vim to `ed`, its status line in `node`; `save` is what `:w` runs.
 * Rejects if the library cannot load (the editor then stays as it was).
 */
export async function attachVim(ed: any, node: HTMLElement, save: () => void): Promise<VimHandle> {
  const { initVimMode, VimMode } = await import("monaco-vim");
  neutralise(VimMode);
  const adapter: any = initVimMode(ed, node);
  // Its `theme` option is Monaco's window-wide `setTheme`: never from here.
  const setOption = adapter.setOption.bind(adapter);
  adapter.setOption = (key: string, value: unknown) => {
    if (key !== "theme") setOption(key, value);
  };
  // `:w` / `:write` (any argument ignored): this editor's buffer, never another's.
  adapter.save = () => {
    if (editorsBlocked() || !ed.getModel?.()) return;
    save();
  };
  return {
    dispose() {
      // Its status line hands focus to the editor as it closes: wherever focus
      // was (the settings dialog that turned vim off) keeps it.
      const had = document.activeElement;
      try {
        adapter.dispose();
      } catch {
        /* an editor already gone */
      }
      if (had instanceof HTMLElement && had.isConnected && document.activeElement !== had) had.focus();
    },
  };
}
