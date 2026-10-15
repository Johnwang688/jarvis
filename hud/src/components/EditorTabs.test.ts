// A File pane's tabs wear the explorer's file-type icon (the owner's ask,
// after PR #50): the same component, the same lib/fileIcons.ts rule, one
// source of truth. The icon is decorative (`aria-hidden`), the name keeps
// its ellipsis, and the dirty dot / preview italic / close button are
// where they were.

import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it } from "vitest";

import css from "../theme.css?raw";
import { EditorTabs, type TabInfo } from "./EditorTabs";
import { FileIcon } from "./FileIcon";
import { iconFor } from "../lib/fileIcons";
import type { Tabs } from "../lib/buffers";

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;

async function render(node: ReturnType<typeof createElement>) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => {
    root.render(node);
  });
  return { host, done: () => act(async () => root.unmount()).then(() => host.remove()) };
}

const noop = () => {};
const strip = (tabs: Tabs, info: (p: string) => TabInfo = () => ({ dirty: false })) =>
  createElement(EditorTabs, { tabs, info, onActivate: noop, onPin: noop, onClose: noop });

describe("editor tab icons", () => {
  const tabs: Tabs = {
    paths: ["src/main.cpp", "package.json", "README.md", "notes", ".env", "a/util.py", "b/util.py"],
    active: "package.json",
    preview: "README.md",
  };

  it("every tab starts with the explorer's icon for its file name, from the one rule", async () => {
    const info = (p: string): TabInfo => ({ dirty: false, mark: p === ".env" ? "env" : null });
    const { host, done } = await render(strip(tabs, info));
    const rows = [...host.querySelectorAll<HTMLElement>('[role="tab"]')];
    expect(rows).toHaveLength(tabs.paths.length);
    for (const [k, path] of tabs.paths.entries()) {
      const tab = rows[k];
      const icon = tab.querySelector<HTMLElement>(".ficon")!;
      expect(icon, path).not.toBeNull();
      // first thing in the tab, before the name
      expect(tab.firstElementChild, path).toBe(icon);
      const name = path.slice(path.lastIndexOf("/") + 1);
      expect(icon.dataset.icon, path).toBe(iconFor(name, path === ".env" ? "env" : null));
      // byte-identical to what the explorer's row draws
      const tree = await render(createElement(FileIcon, { name, policy: path === ".env" ? "env" : null }));
      expect(icon.outerHTML, path).toBe(tree.host.innerHTML);
      await tree.done();
    }
    expect(host.querySelector('[data-icon="markdown"]')).not.toBeNull();
    expect(host.querySelector('[data-icon="cpp"]')).not.toBeNull();
    expect(host.querySelector('[data-icon="env"]')).not.toBeNull();
    await done();
  });

  it("the icon is decorative: aria-hidden, no focus, no text, no title", async () => {
    const { host, done } = await render(strip(tabs));
    expect(host.querySelectorAll(".ficon")).toHaveLength(tabs.paths.length);
    for (const icon of host.querySelectorAll<HTMLElement>(".ficon")) {
      expect(icon.getAttribute("aria-hidden")).toBe("true");
      expect(icon.textContent).toBe("");
      expect(icon.hasAttribute("tabindex")).toBe(false);
      expect(icon.querySelector("svg")!.getAttribute("focusable")).toBe("false");
    }
    await done();
  });

  it("an icon follows the file's name, not the label: two util.py tabs, one folder each, both python", async () => {
    const { host, done } = await render(strip(tabs));
    const a = host.querySelector('[data-testid="editor-tab-a/util.py"]')!;
    expect(a.querySelector(".edtab-name")!.textContent).toBe("a/util.py");
    expect((a.querySelector(".ficon") as HTMLElement).dataset.icon).toBe("python");
    await done();
  });

  it("names, dirty dot, preview, close button and labels are as they were", async () => {
    const info = (p: string): TabInfo => ({ dirty: p === "package.json", saving: p === "src/main.cpp", mark: p === ".env" ? "env" : null });
    const { host, done } = await render(strip(tabs, info));
    const tab = (p: string) => host.querySelector<HTMLElement>(`[data-testid="editor-tab-${p}"]`)!;
    expect(tab("package.json").getAttribute("aria-selected")).toBe("true");
    expect(tab("package.json").querySelector(".edtab-dot.on")!.getAttribute("aria-label")).toBe("unsaved changes");
    expect(tab("src/main.cpp").querySelector(".edtab-dot.saving")!.getAttribute("aria-label")).toBe("saving");
    expect(tab("README.md").dataset.preview).toBe("true");
    expect(tab("README.md").querySelector(".edtab-name")!.textContent).toBe("README.md");
    expect(tab("README.md").querySelector(".edtab-x")).not.toBeNull();
    // order: icon, name, dot, close
    const kinds = [...tab("README.md").children].map((c) => c.className.split(" ")[0]);
    expect(kinds).toEqual(["ficon", "edtab-name", "edtab-dot", "edtab-x"]);
    // the env tab's words reach assistive technology, once, in text
    const env = tab(".env");
    expect(env.querySelector(".edtab-mark")!.textContent).toBe("Jarvis can't read this file");
    expect(env.querySelectorAll(".edtab-mark")).toHaveLength(1);
    expect(tab("README.md").querySelector(".edtab-mark")).toBeNull();
    await done();
  });

  it("the stylesheet keeps the icon at 16px and lets only the name shrink and ellipsize", () => {
    expect(css).toMatch(/\.edtab \.ficon \{[^}]*flex: 0 0 16px/);
    expect(css).toMatch(/\.edtab-name \{[^}]*overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0/);
    expect(css).toMatch(/\.edtab \{[^}]*max-width: 220px/);
  });
});
