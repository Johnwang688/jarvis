// The editor settings dialog (ED-8, editor plan §2.8): opened from a File
// pane's ⚙ or the palette's "Editor Settings…". Every change is stored and
// applied to every live editor as it is made — there is nothing to confirm,
// so a dialog closed by a card (App closes it, as it does quick open and the
// palette) loses nothing. It sits beside the card's veil, so a card makes it
// inert in the meantime, and **its Escape never runs under a card**: the
// card's Escape denies.
//
// Built from text, like every picker: nothing here is markup.

import { useEffect, useRef, useState } from "react";
import {
  CURSOR_STYLES, FONT_FAMILY_MAX, FONT_SIZE_MAX, FONT_SIZE_MIN, safeFontFamily, TAB_SIZE_MAX, TAB_SIZE_MIN,
  WHITESPACE, type EditorSettings,
} from "../lib/editorSettings";
import { resetEditorSettings, setEditorSettings, useEditorSettings } from "../state/editorSettings";
import { editorsBlocked } from "../lib/monaco";

const range = (lo: number, hi: number) => Array.from({ length: hi - lo + 1 }, (_, i) => lo + i);

function Row(props: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="esrow">
      <span className="eslabel">
        {props.label}
        {props.hint ? <span className="muted small"> · {props.hint}</span> : null}
      </span>
      <span className="escontrol">{props.children}</span>
    </label>
  );
}

function Toggle(props: { testid: string; on: boolean; set: (v: boolean) => void }) {
  return (
    <input type="checkbox" data-testid={props.testid} checked={props.on}
           onChange={(e) => props.set(e.target.checked)} />
  );
}

export function EditorSettingsDialog(props: { onClose: () => void }) {
  const s = useEditorSettings();
  const set = (change: Partial<EditorSettings>) => setEditorSettings(change);
  // The font field is typed: held here until it is a safe list, then applied.
  const [font, setFont] = useState(s.fontFamily);
  const fontOk = safeFontFamily(font) !== null;
  const box = useRef<HTMLDivElement>(null);
  const first = useRef<HTMLInputElement>(null);
  const onClose = useRef(props.onClose);
  onClose.current = props.onClose;

  useEffect(() => first.current?.focus(), []);
  // A reset (or another window) moves the stored font: the field follows —
  // but never over what is being typed here ("Fira " is "Fira" stored).
  useEffect(() => {
    if (safeFontFamily(font) !== s.fontFamily) setFont(s.fontFamily);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [s.fontFamily]);

  // Escape closes it wherever focus is (it is modal) — and never under a
  // card, whose Escape is a deny (the card has focus then anyway, and App
  // closes this; this is the belt).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented || editorsBlocked() || document.getElementById("authveil")) return;
      e.preventDefault();
      e.stopPropagation();
      onClose.current();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className="pickerveil" data-testid="editor-settings-dialog" onMouseDown={(e) => {
      if (e.target === e.currentTarget) props.onClose();
    }}>
      <div className="picker essettings" role="dialog" aria-label="Editor settings" ref={box}>
        <h3>Editor settings</h3>
        <div className="rows">
          <Row label="Font family" hint="names only, e.g. &quot;Cascadia Mono&quot;, monospace">
            <input
              ref={first}
              data-testid="es-font-family"
              value={font}
              maxLength={FONT_FAMILY_MAX}
              spellCheck={false}
              autoComplete="off"
              placeholder="Monaco's default"
              aria-invalid={!fontOk}
              onChange={(e) => {
                setFont(e.target.value);
                if (safeFontFamily(e.target.value) !== null) set({ fontFamily: e.target.value });
              }}
            />
          </Row>
          {!fontOk ? (
            <div className="small err pad" data-testid="es-font-error">
              Not used: a font list is names, quoted or bare, separated by commas (letters, digits, spaces, . _ -).
            </div>
          ) : null}
          <Row label="Font size">
            <select data-testid="es-font-size" value={s.fontSize}
                    onChange={(e) => set({ fontSize: Number(e.target.value) })}>
              {range(FONT_SIZE_MIN, FONT_SIZE_MAX).map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </Row>
          <Row label="Indent with" hint="a file's own indentation and .editorconfig win">
            <select data-testid="es-indent" value={s.insertSpaces ? "spaces" : "tabs"}
                    onChange={(e) => set({ insertSpaces: e.target.value === "spaces" })}>
              <option value="spaces">Spaces</option>
              <option value="tabs">Tabs</option>
            </select>
          </Row>
          <Row label="Tab size">
            <select data-testid="es-tab-size" value={s.tabSize}
                    onChange={(e) => set({ tabSize: Number(e.target.value) })}>
              {range(TAB_SIZE_MIN, TAB_SIZE_MAX).map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </Row>
          <Row label="Word wrap">
            <select data-testid="es-word-wrap" value={s.wordWrap}
                    onChange={(e) => set({ wordWrap: e.target.value as EditorSettings["wordWrap"] })}>
              <option value="off">Off</option>
              <option value="on">On</option>
            </select>
          </Row>
          <Row label="Word wrap in Markdown">
            <select data-testid="es-word-wrap-md" value={s.wordWrapMarkdown}
                    onChange={(e) => set({ wordWrapMarkdown: e.target.value as EditorSettings["wordWrapMarkdown"] })}>
              <option value="on">On</option>
              <option value="off">Off</option>
            </select>
          </Row>
          <Row label="Minimap">
            <Toggle testid="es-minimap" on={s.minimap} set={(v) => set({ minimap: v })} />
          </Row>
          <Row label="Sticky scroll">
            <Toggle testid="es-sticky" on={s.stickyScroll} set={(v) => set({ stickyScroll: v })} />
          </Row>
          <Row label="Show whitespace">
            <select data-testid="es-whitespace" value={s.whitespace}
                    onChange={(e) => set({ whitespace: e.target.value as EditorSettings["whitespace"] })}>
              {WHITESPACE.map((w) => <option key={w} value={w}>{w}</option>)}
            </select>
          </Row>
          <Row label="Bracket-pair colours">
            <Toggle testid="es-brackets" on={s.bracketPairs} set={(v) => set({ bracketPairs: v })} />
          </Row>
          <Row label="Cursor style">
            <select data-testid="es-cursor" value={s.cursorStyle}
                    onChange={(e) => set({ cursorStyle: e.target.value as EditorSettings["cursorStyle"] })}>
              {CURSOR_STYLES.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </Row>
          <Row label="Vim keys" hint="Ctrl+D stays push-to-talk; :w saves">
            <Toggle testid="es-vim" on={s.vim} set={(v) => set({ vim: v })} />
          </Row>
          <div className="small muted pad" data-testid="es-note">
            Auto-save and format on save are off. A project's .editorconfig sets indentation and new-line
            endings for the files it matches, and never rewrites a file on save.
          </div>
        </div>
        <div className="foot">
          <button type="button" data-testid="es-reset" onClick={() => {
            resetEditorSettings();
            setFont("");
          }}>
            Reset to defaults
          </button>
          <button type="button" data-testid="es-close" onClick={props.onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}
