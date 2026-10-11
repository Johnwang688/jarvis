import { describe, expect, it } from "vitest";
import { CHORD_MS, chordSecond, chordStep, isSaveKey, resetChord, type ChordState, type KeyLike } from "./editorkeys";
import { inMonaco } from "./layout";

const key = (k: string, over: Partial<KeyLike> = {}): KeyLike => ({
  key: k, ctrlKey: true, metaKey: false, altKey: false, shiftKey: false, repeat: false, isComposing: false, ...over,
});

/** Feeds keys in order (each `[event, inEditor, now]`); returns each step's `second`. */
function run(keys: [KeyLike, boolean, number][], start: ChordState = null): boolean[] {
  let state = start;
  return keys.map(([e, inEditor, now]) => {
    const step = chordStep(state, e, inEditor, now);
    state = step.state;
    return step.second;
  });
}

describe("Monaco's Ctrl+K chords (decisions E-10)", () => {
  it("Ctrl+K then Ctrl+B in an editor: the B is the chord's, so the HUD leaves it to Monaco", () => {
    expect(run([[key("k"), true, 0], [key("b"), true, 100]])).toEqual([false, true]);
  });

  it("a lone Ctrl+B in an editor is not a chord's (the HUD folds the sidebar)", () => {
    expect(run([[key("b"), true, 0]])).toEqual([false]);
    expect(run([[key("x", { ctrlKey: false }), true, 0], [key("b"), true, 50]])).toEqual([false, false]);
  });

  it("the chord holds across the modifier keydowns between its two keys", () => {
    const control = key("Control");
    expect(run([[key("k"), true, 0], [control, true, 30], [key("Shift", { shiftKey: true }), true, 40],
      [key("b"), true, 60]])).toEqual([false, false, false, true]);
  });

  it("the chord is spent by its second key: the Ctrl+B after it folds again", () => {
    expect(run([[key("k"), true, 0], [key("b"), true, 50], [key("b"), true, 900]])).toEqual([false, true, false]);
  });

  it("any second key ends the chord, including the HUD's zoom keys (Ctrl+K Ctrl+0 is fold all)", () => {
    expect(run([[key("k"), true, 0], [key("0"), true, 50]])).toEqual([false, true]);
    expect(run([[key("k"), true, 0], [key("Escape", { ctrlKey: false }), true, 50], [key("b"), true, 90]]))
      .toEqual([false, true, false]);
  });

  it("auto-repeat changes nothing: a held Ctrl+K still starts one chord, a repeated key never ends it", () => {
    expect(run([[key("k"), true, 0], [key("k", { repeat: true }), true, 30], [key("k", { repeat: true }), true, 60],
      [key("b"), true, 90]])).toEqual([false, false, false, true]);
    expect(run([[key("k"), true, 0], [key("b", { repeat: true }), true, 30], [key("b"), true, 60]]))
      .toEqual([false, false, true]);
  });

  it("composition changes nothing: an IME key is neither a chord's start nor its end", () => {
    expect(run([[key("k", { isComposing: true }), true, 0], [key("b"), true, 30]])).toEqual([false, false]);
    expect(run([[key("k"), true, 0], [key("Process", { isComposing: true, ctrlKey: false }), true, 30],
      [key("b"), true, 60]])).toEqual([false, false, true]);
  });

  it("only Ctrl+K typed in an editor starts one: not outside, not with Shift or Alt (AltGr)", () => {
    expect(run([[key("k"), false, 0], [key("b"), true, 50]])).toEqual([false, false]);
    expect(run([[key("k", { shiftKey: true }), true, 0], [key("b"), true, 50]])).toEqual([false, false]);
    expect(run([[key("k", { altKey: true }), true, 0], [key("b"), true, 50]])).toEqual([false, false]);
    expect(run([[key("K", { metaKey: true, ctrlKey: false }), true, 0], [key("b"), true, 50]])).toEqual([false, true]);
  });

  it("a key outside the editor is the HUD's and leaves the chord waiting, as Monaco's does", () => {
    // Monaco's keybinding service hears only keys typed in an editor: Ctrl+K
    // in the editor, a letter in the chat box, then Ctrl+B in the editor is
    // still its chord (review of PR #43).
    expect(run([[key("k"), true, 0], [key("x", { ctrlKey: false }), false, 50], [key("b"), true, 90]]))
      .toEqual([false, false, true]);
    expect(run([[key("k"), true, 0], [key("b"), false, 50], [key("b"), true, 90]])).toEqual([false, false, true]);
  });

  it("a chord that has timed out is not ended by a key outside the editor either", () => {
    expect(run([[key("k"), true, 0], [key("x", { ctrlKey: false }), false, CHORD_MS + 1], [key("b"), true, CHORD_MS + 2]]))
      .toEqual([false, false, false]);
  });

  it("Monaco gives up waiting after five seconds, and so does the tracker", () => {
    expect(run([[key("k"), true, 0], [key("b"), true, CHORD_MS]])).toEqual([false, true]);
    expect(run([[key("k"), true, 0], [key("b"), true, CHORD_MS + 1]])).toEqual([false, false]);
  });
});

describe("Ctrl+S", () => {
  it("is Ctrl+S or Cmd+S, either case, and nothing with Alt (AltGr) or Shift", () => {
    expect(isSaveKey(key("s"))).toBe(true);
    expect(isSaveKey(key("S"))).toBe(true);
    expect(isSaveKey(key("s", { ctrlKey: false, metaKey: true }))).toBe(true);
    expect(isSaveKey(key("s", { altKey: true }))).toBe(false);
    expect(isSaveKey(key("s", { shiftKey: true }))).toBe(false);
    expect(isSaveKey(key("s", { ctrlKey: false }))).toBe(false);
    expect(isSaveKey(key("d"))).toBe(false);
  });
});

describe("keys going into Monaco", () => {
  it("are told by the nearest .monaco-editor, however deep the target sits (EditContext or textarea)", () => {
    const root = document.createElement("div");
    root.innerHTML = `
      <div class="editorhost"><div class="monaco-editor"><div class="overflow-guard">
        <div class="native-edit-context" id="ec" tabindex="0"></div>
        <textarea id="ta"></textarea>
        <div class="shadow-root-host"><span id="deep"></span></div>
      </div></div></div>
      <div class="filetree"><div id="tree" class="tree-row"></div></div>
      <textarea id="box"></textarea>`;
    document.body.appendChild(root);
    for (const id of ["ec", "ta", "deep"]) expect(inMonaco(root.querySelector(`#${id}`))).toBe(true);
    for (const id of ["tree", "box"]) expect(inMonaco(root.querySelector(`#${id}`))).toBe(false);
    expect(inMonaco(null)).toBe(false);
    root.remove();
  });
});

describe("one chord tracker for the window (PR #40 on ED-1)", () => {
  const ev = (key: string, t: number, over: Partial<KeyLike> = {}) =>
    ({ key, ctrlKey: true, altKey: false, shiftKey: false, repeat: false, isComposing: false, timeStamp: t, ...over });

  it("steps a keydown once, whichever listener asks first, and both get the same answer", () => {
    resetChord();
    const k = ev("k", 1000);
    expect(chordSecond(k, true)).toBe(false);
    expect(chordSecond(k, true)).toBe(false);          // the second listener: the same event, not a second step
    const d = ev("d", 1500);
    expect(chordSecond(d, true)).toBe(true);           // Ctrl+K then Ctrl+D in an editor: Monaco's
    expect(chordSecond(d, true)).toBe(true);
    expect(chordSecond(ev("d", 1600), true)).toBe(false);
  });

  it("a blur ends a pending chord", () => {
    resetChord();
    expect(chordSecond(ev("k", 1000), true)).toBe(false);
    resetChord();
    expect(chordSecond(ev("d", 1100), true)).toBe(false);
  });
});
