// A File pane's tab strip (ED-2, editor plan §2.3). A click in the tree opens
// a normal (pinned) tab (2026-10-14, the owner's request). A *preview* tab
// (italic; the next preview replaces it) can still come from a saved pane
// spec; a double click on it — or an edit — pins it. Middle-click or × closes
// a tab; closing the last tab on an unsaved edit asks first (FileTab).

import type { Tabs } from "../lib/buffers";

export interface TabInfo {
  dirty: boolean;
  /** An env file (Jarvis can't read it), withheld, or read-only: the mark beside the name. */
  mark?: "env" | "withheld" | "readonly" | null;
  loading?: boolean;
  conflict?: boolean;
  /** A save is on its way (or a second one waits for it): a hollow dot. */
  saving?: boolean;
}

const base = (path: string) => path.slice(path.lastIndexOf("/") + 1) || path;

export function EditorTabs(props: {
  tabs: Tabs;
  info: (path: string) => TabInfo;
  onActivate: (path: string) => void;
  onPin: (path: string) => void;
  onClose: (path: string) => void;
}) {
  const { tabs } = props;
  if (!tabs.paths.length) return null;
  // Two tabs with one name say which folder each is in.
  const names = new Map<string, number>();
  for (const p of tabs.paths) names.set(base(p), (names.get(base(p)) || 0) + 1);
  return (
    <div className="edtabs" data-testid="editor-tabs" role="tablist">
      {tabs.paths.map((path) => {
        const i = props.info(path);
        const active = tabs.active === path;
        const preview = tabs.preview === path;
        const label = (names.get(base(path)) || 0) > 1 ? path : base(path);
        return (
          <div
            key={path}
            role="tab"
            aria-selected={active}
            tabIndex={active ? 0 : -1}
            className={"edtab" + (active ? " on" : "") + (preview ? " preview" : "") + (i.conflict ? " conflict" : "")}
            data-testid={`editor-tab-${path}`}
            data-dirty={String(i.dirty)}
            data-saving={String(!!i.saving)}
            data-preview={String(preview)}
            data-active={String(active)}
            title={path + (i.mark === "env" ? " · Jarvis can't read this file" : "") + (i.saving ? " · saving…" : i.dirty ? " · unsaved changes" : "")}
            onClick={() => props.onActivate(path)}
            onDoubleClick={() => props.onPin(path)}
            onAuxClick={(e) => {
              if (e.button !== 1) return;
              e.preventDefault();
              props.onClose(path);
            }}
            onMouseDown={(e) => {
              // A middle press must not start the browser's autoscroll.
              if (e.button === 1) e.preventDefault();
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                e.stopPropagation();
                props.onActivate(path);
              }
            }}
          >
            {i.mark === "env" ? <span className="edtab-mark" aria-label="Jarvis can't read this file">🔒</span> : null}
            <span className="edtab-name">{label}</span>
            {/* Dirty: the dot stands where × is, and hovering or focusing the
                tab brings × back (VS Code's; theme.css swaps them). Saving
                (ED-2): the same slot holds a hollow ring until the write
                answers, dirty or not. */}
            <span
              className={"edtab-dot" + (i.dirty ? " on" : "") + (i.saving ? " saving" : "")}
              role={i.dirty || i.saving ? "img" : undefined}
              aria-label={i.saving ? "saving" : i.dirty ? "unsaved changes" : undefined}
              title={i.saving ? "saving…" : i.dirty ? "Unsaved changes" : undefined}
            />
            <button
              type="button"
              className="edtab-x quiet"
              data-testid={`editor-tab-close-${path}`}
              tabIndex={-1}
              title="Close (middle-click)"
              onClick={(e) => {
                e.stopPropagation();
                props.onClose(path);
              }}
            >
              ×
            </button>
          </div>
        );
      })}
    </div>
  );
}
