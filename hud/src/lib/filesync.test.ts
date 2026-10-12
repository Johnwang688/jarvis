import { describe, expect, it } from "vitest";
import { afterSave, fileSaved, onFileSaved } from "./filesync";

describe("a save in one File pane, heard by the others (WP-G2)", () => {
  const saved = { projectId: "p1", path: "a.txt", mtime: 20 };
  const mine = (over = {}) => ({ projectId: "p1", path: "a.txt", mtime: 10, dirty: false, ...over });

  it("reloads a clean pane on that file", () => {
    expect(afterSave(saved, mine(), false)).toBe("reload");
  });

  it("never reloads under an unsaved edit: it says the file changed instead", () => {
    expect(afterSave(saved, mine({ dirty: true }), false)).toBe("note");
  });

  it("ignores its own save, another file or project, the version it has, and a pane with nothing open", () => {
    expect(afterSave(saved, mine(), true)).toBe("ignore");
    expect(afterSave(saved, mine({ path: "b.txt" }), false)).toBe("ignore");
    expect(afterSave(saved, mine({ projectId: "p2" }), false)).toBe("ignore");
    expect(afterSave(saved, mine({ mtime: 20 }), false)).toBe("ignore");
    expect(afterSave(saved, mine({ path: null }), false)).toBe("ignore");
    expect(afterSave(saved, mine({ projectId: null }), false)).toBe("ignore");
  });

  it("tells every listener, with who saved, until it stops listening", () => {
    const heard: unknown[] = [];
    const me = {};
    const stop = onFileSaved((s, from) => heard.push([s.path, from === me]));
    fileSaved(saved, me);
    fileSaved(saved, {});
    stop();
    fileSaved(saved, me);
    expect(heard).toEqual([["a.txt", true], ["a.txt", false]]);
  });
});
