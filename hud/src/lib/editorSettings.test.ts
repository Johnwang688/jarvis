import { describe, expect, it } from "vitest";
import {
  DEFAULTS, displayOptions, editorOptions, loadSettings, parseSettings, safeFontFamily, saveSettings,
  serialize, SETTINGS_KEY, withChange,
} from "./editorSettings";

class MemStorage {
  data = new Map<string, string>();
  getItem(k: string) {
    return this.data.has(k) ? this.data.get(k)! : null;
  }
  setItem(k: string, v: string) {
    this.data.set(k, v);
  }
}

describe("the settings' defaults (ED-8)", () => {
  it("are what the editor drew before, minimap off, Markdown wrapped, vim off", () => {
    expect(DEFAULTS).toEqual({
      fontFamily: "", fontSize: 12, tabSize: 4, insertSpaces: true, wordWrap: "off", wordWrapMarkdown: "on",
      minimap: false, stickyScroll: true, whitespace: "selection", bracketPairs: true, cursorStyle: "line", vim: false,
    });
  });

  it("there is no auto-save or format-on-save setting (decided off)", () => {
    expect(Object.keys(DEFAULTS).filter((k) => /save|format/i.test(k))).toEqual([]);
  });
});

describe("garbage falls back field by field", () => {
  it("nothing, a non-object or JSON that does not parse is all defaults", () => {
    for (const raw of [null, undefined, "", "not json{", "[1,2]", "42", '"str"', 7, [], true]) {
      expect(parseSettings(raw)).toEqual(DEFAULTS);
    }
  });

  it("each bad field takes its own default; the good ones stand", () => {
    const got = parseSettings(JSON.stringify({
      fontSize: "huge", tabSize: 400, insertSpaces: "no", wordWrap: ["on"], wordWrapMarkdown: "off",
      minimap: "yes", stickyScroll: 0, whitespace: "everywhere", bracketPairs: false, cursorStyle: "block",
      fontFamily: "x;}body{color:red}", vim: 1,
    }));
    expect(got).toEqual({
      ...DEFAULTS, wordWrapMarkdown: "off", bracketPairs: false, cursorStyle: "block",
    });
  });

  it("numbers must be whole and in range", () => {
    for (const fontSize of [7, 33, 12.5, NaN, Infinity, -12, "12"]) {
      expect(parseSettings({ fontSize }).fontSize).toBe(12);
    }
    expect(parseSettings({ fontSize: 8 }).fontSize).toBe(8);
    expect(parseSettings({ fontSize: 32 }).fontSize).toBe(32);
    for (const tabSize of [0, 17, 2.5, "2"]) expect(parseSettings({ tabSize }).tabSize).toBe(4);
    expect(parseSettings({ tabSize: 1 }).tabSize).toBe(1);
  });

  it("an inherited or unknown key changes nothing", () => {
    const proto = Object.create({ fontSize: 20 });
    expect(parseSettings(proto)).toEqual(DEFAULTS);
    expect(parseSettings({ autoSave: true, __proto__x: 1 } as any)).toEqual(DEFAULTS);
  });

  it("a whole good record round-trips through what is stored", () => {
    const s = { ...DEFAULTS, fontFamily: '"Cascadia Mono", monospace', fontSize: 15, vim: true, whitespace: "all" as const };
    expect(parseSettings(serialize(s))).toEqual(s);
  });

  it("only the known fields are stored", () => {
    const stored = JSON.parse(serialize({ ...DEFAULTS, extra: "x" } as any));
    expect(Object.keys(stored).sort()).toEqual(Object.keys(DEFAULTS).sort());
  });
});

describe("a change from the dialog", () => {
  it("a bad field keeps its current value, never the default", () => {
    const now = { ...DEFAULTS, fontSize: 18, fontFamily: "Consolas" };
    expect(withChange(now, { fontSize: 99, fontFamily: "a;b", cursorStyle: "block" })).toEqual({
      ...now, cursorStyle: "block",
    });
  });

  it("a key that is no setting is ignored", () => {
    expect(withChange(DEFAULTS, { autoSave: true, formatOnSave: true })).toEqual(DEFAULTS);
  });
});

describe("the font family is a list of names, nothing else", () => {
  it("takes bare and quoted names, normalised", () => {
    expect(safeFontFamily("")).toBe("");
    expect(safeFontFamily("   ")).toBe("");
    expect(safeFontFamily("Consolas")).toBe("Consolas");
    expect(safeFontFamily('"Cascadia Mono",Consolas ,  monospace')).toBe('"Cascadia Mono", Consolas, monospace');
    expect(safeFontFamily("'Fira Code', monospace")).toBe('"Fira Code", monospace');
    expect(safeFontFamily("Fira   Code")).toBe("Fira Code");
    expect(safeFontFamily("-apple-system, ui-monospace")).toBe("-apple-system, ui-monospace");
    expect(safeFontFamily("JetBrains Mono NL 2.304")).toBe("JetBrains Mono NL 2.304");
  });

  it("refuses anything that could end a CSS value, start a function or hide", () => {
    for (const bad of [
      "a;b", "x}body{color:red", "a{b", "url(x)", "a(b)", "a\\b", "a/b", "a:b", "a<b>", "@import", "a!important",
      '"unbalanced', "'mixed\"", '"a"b"', '""', "a,,b", "a,", ",a", "1Font", "--", "a\nb", "a\tb",
      "a​b", "аpple", "a\u0000", "x".repeat(201), "a,b,c,d,e,f,g,h,i",
      '"a\'b"',
    ]) {
      expect(safeFontFamily(bad)).toBeNull();
    }
    for (const notString of [null, undefined, 12, {}, ["a"]]) expect(safeFontFamily(notString)).toBeNull();
  });
});

describe("storage", () => {
  it("loads defaults when storage is missing, throws or holds garbage", () => {
    expect(loadSettings(null)).toEqual(DEFAULTS);
    const throwing = { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); } };
    expect(loadSettings(throwing)).toEqual(DEFAULTS);
    expect(saveSettings(DEFAULTS, throwing)).toBe(false);
    const mem = new MemStorage();
    mem.setItem(SETTINGS_KEY, "{bad");
    expect(loadSettings(mem)).toEqual(DEFAULTS);
  });

  it("saves under jarvis.hud.editor and loads it back", () => {
    const mem = new MemStorage();
    const s = { ...DEFAULTS, minimap: true, cursorStyle: "underline" as const };
    expect(saveSettings(s, mem)).toBe(true);
    expect(mem.data.has("jarvis.hud.editor")).toBe(true);
    expect(loadSettings(mem)).toEqual(s);
  });
});

describe("the options an editor takes", () => {
  it("wrap follows Markdown; the minimap, tabs and font go through", () => {
    const s = { ...DEFAULTS, minimap: true, tabSize: 2, insertSpaces: false, fontFamily: "Consolas", wordWrap: "off" as const };
    const code = editorOptions(s, { markdown: false });
    expect(code).toMatchObject({
      fontFamily: "Consolas", fontSize: 12, tabSize: 2, insertSpaces: false, wordWrap: "off",
      minimap: { enabled: true }, stickyScroll: { enabled: true }, renderWhitespace: "selection",
      bracketPairColorization: { enabled: true }, cursorStyle: "line",
    });
    expect(editorOptions(s, { markdown: true }).wordWrap).toBe("on");
  });

  it("an empty font is Monaco's own (the key present, undefined, so a cleared font goes back)", () => {
    const o = displayOptions(DEFAULTS);
    expect("fontFamily" in o).toBe(true);
    expect(o.fontFamily).toBeUndefined();
  });

  it("a diff or Compare view never takes the tabs, wrap or minimap", () => {
    const o = displayOptions({ ...DEFAULTS, minimap: true });
    expect(Object.keys(o).sort()).toEqual(
      ["bracketPairColorization", "cursorStyle", "fontFamily", "fontSize", "renderWhitespace", "stickyScroll"]);
  });
});
