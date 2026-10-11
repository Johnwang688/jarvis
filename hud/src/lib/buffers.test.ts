import { describe, expect, it } from "vitest";
import {
  NO_TABS, TABS_MAX, chatExcerpt, closeTab, eolOf, jarvisMayRead, minimalEdit, openTab, parseKind, parseTabs,
  pinTab, pollAction, sameTabs, touchedPath, writerOf, type Tabs,
} from "./buffers";

const tabs = (paths: string[], active: string | null = paths[0] ?? null, preview: string | null = null): Tabs =>
  ({ paths, active, preview });

describe("tabs: a single click previews, a double click or an edit pins (§2.3)", () => {
  it("a single click opens a preview tab, and the next single click replaces it", () => {
    let t = openTab(NO_TABS, "a.py");
    expect(t).toEqual(tabs(["a.py"], "a.py", "a.py"));
    t = openTab(t, "b.py");
    expect(t).toEqual(tabs(["b.py"], "b.py", "b.py"));
  });

  it("a preview holding an edit is pinned, never replaced", () => {
    const t = openTab(tabs(["a.py"], "a.py", "a.py"), "b.py", { keepPreview: (p) => p === "a.py" });
    expect(t).toEqual(tabs(["a.py", "b.py"], "b.py", "b.py"));
  });

  it("a double click pins: a new pinned tab, or the preview made permanent", () => {
    let t = openTab(NO_TABS, "a.py", { pin: true });
    expect(t).toEqual(tabs(["a.py"], "a.py", null));
    t = openTab(t, "b.py");
    t = openTab(t, "b.py", { pin: true });
    expect(t).toEqual(tabs(["a.py", "b.py"], "b.py", null));
    // A third file is a new preview beside the pinned ones.
    expect(openTab(t, "c.py")).toEqual(tabs(["a.py", "b.py", "c.py"], "c.py", "c.py"));
  });

  it("an open tab is only activated, and a new one goes right of the active one", () => {
    const t = tabs(["a", "b", "c"], "a");
    expect(openTab(t, "c")).toEqual(tabs(["a", "b", "c"], "c"));
    expect(openTab(t, "x", { pin: true }).paths).toEqual(["a", "x", "b", "c"]);
  });

  it("pinTab pins only the preview", () => {
    const t = tabs(["a", "b"], "b", "b");
    expect(pinTab(t, "b").preview).toBeNull();
    expect(pinTab(t, "a")).toBe(t);
  });

  it("closing moves to the right neighbour, else the left; the preview is forgotten", () => {
    expect(closeTab(tabs(["a", "b", "c"], "b"), "b")).toEqual(tabs(["a", "c"], "c"));
    expect(closeTab(tabs(["a", "b", "c"], "c"), "c")).toEqual(tabs(["a", "b"], "b"));
    expect(closeTab(tabs(["a", "b"], "a", "b"), "b")).toEqual(tabs(["a"], "a"));
    expect(closeTab(tabs(["a"], "a"), "a")).toEqual(tabs([], null));
    expect(closeTab(tabs(["a"]), "zz")).toEqual(tabs(["a"]));
  });

  it("never more than TABS_MAX tabs, and the one just opened always stays", () => {
    let t = NO_TABS;
    for (let i = 0; i < TABS_MAX + 5; i++) t = openTab(t, `f${i}`, { pin: true });
    expect(t.paths.length).toBe(TABS_MAX);
    expect(t.active).toBe(`f${TABS_MAX + 4}`);
    expect(t.paths).toContain(`f${TABS_MAX + 4}`);
  });

  it("parses field by field: a bad active or preview falls back alone; no paths is none", () => {
    expect(parseTabs({ paths: ["a", "b"], active: "b", preview: "b" })).toEqual(tabs(["a", "b"], "b", "b"));
    expect(parseTabs({ paths: ["a", "b"], active: "zz", preview: 7 })).toEqual(tabs(["a", "b"], "a", null));
    expect(parseTabs({ paths: ["a", "a", "", 3, "x".repeat(2000), "b"] })).toEqual(tabs(["a", "b"], "a", null));
    expect(parseTabs({ paths: [] })).toBeNull();
    expect(parseTabs({ paths: "a" })).toBeNull();
    expect(parseTabs(null)).toBeNull();
    expect(parseTabs([])).toBeNull();
    expect(sameTabs(tabs(["a"]), tabs(["a"]))).toBe(true);
    expect(sameTabs(tabs(["a"]), tabs(["a"], "a", "a"))).toBe(false);
  });
});

describe("the change poll: what a stat row means for a buffer (§2.4's table)", () => {
  const clean = { etag: "e1", dirty: false, saving: false, conflict: null };
  const dirty = { ...clean, dirty: true };

  it("nothing moved: nothing happens", () => {
    expect(pollAction(clean, { etag: "e1" })).toBe("none");
    expect(pollAction(dirty, { etag: "e1" })).toBe("none");
  });

  it("moved under a clean buffer: reload in place; under an edit: the conflict bar", () => {
    expect(pollAction(clean, { etag: "e2" })).toBe("reload");
    expect(pollAction(dirty, { etag: "e2" })).toBe("conflict");
  });

  it("a conflict already shown for that etag is not raised again; a newer write is", () => {
    const shown = { ...dirty, conflict: { state: "changed" as const, etag: "e2" } };
    expect(pollAction(shown, { etag: "e2" })).toBe("none");
    expect(pollAction(shown, { etag: "e3" })).toBe("conflict");
  });

  it("the disk came back to the buffer's base: the bar clears", () => {
    expect(pollAction({ ...dirty, conflict: { state: "changed", etag: "e2" } }, { etag: "e1" })).toBe("clear");
    expect(pollAction({ ...clean, conflict: { state: "gone" } }, { etag: "e1" })).toBe("clear");
  });

  it("an edit undone while a conflict waits: a clean buffer simply reloads", () => {
    expect(pollAction({ ...clean, conflict: { state: "changed", etag: "e2" } }, { etag: "e2" })).toBe("reload");
  });

  it("gone is said once", () => {
    expect(pollAction(dirty, { gone: true })).toBe("gone");
    expect(pollAction(clean, { gone: true })).toBe("gone");
    expect(pollAction({ ...dirty, conflict: { state: "gone" } }, { gone: true })).toBe("none");
  });

  it("a save in flight, a row the poll could not read, a withheld or oversized file: left alone", () => {
    expect(pollAction({ ...dirty, saving: true }, { etag: "e9" })).toBe("none");
    expect(pollAction(dirty, undefined)).toBe("none");
    expect(pollAction(dirty, { error: 403 })).toBe("none");
    expect(pollAction(dirty, { kind: "withheld" })).toBe("none");
    expect(pollAction(dirty, { etag: null })).toBe("none");
  });
});

describe("minimalEdit: a reload in place keeps what did not change", () => {
  const apply = (prev: string, e: ReturnType<typeof minimalEdit>) =>
    e ? prev.slice(0, e.start) + e.text + prev.slice(e.end) : prev;

  it("round-trips, and touches only the middle", () => {
    for (const [a, b] of [["hello world", "hello there world"], ["abc", "abc"], ["", "x"], ["x", ""],
      ["a\nb\nc\n", "a\nB\nc\n"], ["same start", "same end"], ["aaaa", "aaaaa"], ["😀x", "😁x"],
      ["l1\r\nl2\r\n", "l1\r\nl2\r\nl3\r\n"], ["a\r\nb", "a\nb"]]) {
      const e = minimalEdit(a, b);
      expect(apply(a, e)).toBe(b);
    }
    expect(minimalEdit("same", "same")).toBeNull();
    expect(minimalEdit("abcXdef", "abcYdef")).toEqual({ start: 3, end: 4, text: "Y" });
  });

  it("never splits a surrogate pair or a CRLF", () => {
    const e = minimalEdit("😀", "😁")!;
    expect(e.start).toBe(0);
    const f = minimalEdit("a\r\nb", "a\r\nc\r\nb")!;
    expect("a\r\nb".slice(0, f.start).endsWith("\r")).toBe(false);
  });
});

describe("the rest", () => {
  it("eolOf", () => {
    expect(["a\nb", "a\r\nb", "a\rb", "a\r\nb\n", "ab", ""].map(eolOf)).toEqual(["LF", "CRLF", "CR", "mixed", null, null]);
  });

  it("kinds: unknown is text; Jarvis reads neither an env file nor a withheld one", () => {
    expect(["text", "env", "gate", "safety", "binary", "withheld", "x", 3].map(parseKind))
      .toEqual(["text", "env", "gate", "safety", "binary", "withheld", "text", "text"]);
    expect(jarvisMayRead("env")).toBe(false);
    expect(jarvisMayRead("withheld")).toBe(false);
    for (const k of ["text", "gate", "safety", "binary"] as const) expect(jarvisMayRead(k)).toBe(true);
  });

  it("touchedPath reads the runner's file keys", () => {
    expect(touchedPath({ file_path: "/a/b.py" })).toBe("/a/b.py");
    expect(touchedPath({ path: " src/x.ts " })).toBe("src/x.ts");
    expect(touchedPath({ command: "ls" })).toBeNull();
    expect(touchedPath(null)).toBeNull();
  });

  it("writerOf: an absolute path exactly, a relative one at a path boundary, newest first, recent only", () => {
    const now = 1_000_000;
    const t = [
      { path: "/w/app/main.py", writer: "Claude · chat", at: now - 10 },
      { path: "app/main.py", writer: "Codex · task 41", at: now - 5 },
      { path: "ain.py", writer: "nope", at: now - 4 },
    ];
    expect(writerOf(t, "/w/app/main.py", now)).toBe("Codex · task 41");
    expect(writerOf(t.slice(0, 1), "/w/app/main.py", now)).toBe("Claude · chat");
    expect(writerOf(t, "/w/app/other.py", now)).toBeNull();
    expect(writerOf(t, "/w/app/main.py", now + 10 * 60_000)).toBeNull();
  });

  it("chatExcerpt fences past any backticks inside", () => {
    expect(chatExcerpt("a.py", "x = 1\n", 3, 3)).toBe("a.py:L3\n```\nx = 1\n```\n");
    expect(chatExcerpt("a.md", "````\nhi", 1, 2)).toBe("a.md:L1-2\n`````\n````\nhi\n`````\n");
  });
});
