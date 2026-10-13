// Three quick clicks on ⊞ used to
// unmount the whole window. The menu's position was read from the click's
// `currentTarget` inside a state updater; React runs an updater during the
// click only when nothing is pending, so the second and third clicks' ran at
// render time, when `currentTarget` was already null, and
// `getBoundingClientRect` threw inside render (review of PR #29). Inside one
// `act` nothing is flushed between the clicks, which is the pending case.

import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { resetChord } from "../lib/editorkeys";
import { TitleBar, useLayout } from "./Layout";

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;

function Host() {
  const view = useLayout(false);
  return createElement(TitleBar, { view, blocked: false, onSettings: () => {} });
}

const WIDTH = window.innerWidth;

function setWidth(w: number) {
  Object.defineProperty(window, "innerWidth", { configurable: true, value: w });
}

describe("TitleBar menus under quick clicks", () => {
  let host: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    localStorage.clear();
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
  });

  afterEach(() => {
    act(() => root.unmount());
    host.remove();
    setWidth(WIDTH);
  });

  const cases: [string, string, string, number][] = [
    ["the layout menu (⊞)", "layout-customize", "layout-menu", 1280],
    ["the layout menu at narrow width", "layout-customize", "layout-menu", 420],
  ];
  for (const [what, opener, menu, width] of cases) {
    it(`${what}: three clicks in one task toggle it open and keep the window`, () => {
      setWidth(width);
      act(() => root.render(createElement(Host)));
      const button = host.querySelector<HTMLButtonElement>(`[data-testid="${opener}"]`);
      expect(button).not.toBeNull();
      act(() => {
        button!.click();
        button!.click();
        button!.click();
      });
      expect(host.querySelector('[data-testid="titlebar"]')).not.toBeNull();
      // Open, closed, open: three toggles end open, and the button says so.
      expect(host.querySelector(`[data-testid="${menu}"]`)).not.toBeNull();
      expect(button!.getAttribute("aria-expanded")).toBe("true");
      act(() => button!.click());
      expect(host.querySelector(`[data-testid="${menu}"]`)).toBeNull();
    });
  }
});


describe("Settings shortcut capture", () => {
  it("takes Ctrl+, once, respects composition, cards and Monaco chords", () => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    const toggle = vi.fn();
    function Keys(props: { blocked: boolean }) {
      useLayout(props.blocked, undefined, { open: false, toggle });
      return createElement("div", { className: "monaco-editor" }, createElement("textarea"));
    }
    const event = (key: string, extra: KeyboardEventInit = {}) =>
      new KeyboardEvent("keydown", { key, ctrlKey: true, bubbles: true, cancelable: true, ...extra });
    try {
      act(() => root.render(createElement(Keys, { blocked: false })));
      const input = host.querySelector("textarea")!;
      const heard = vi.fn();
      input.addEventListener("keydown", heard);
      const comma = event(",");
      act(() => input.dispatchEvent(comma));
      expect(comma.defaultPrevented).toBe(true);
      expect(toggle).toHaveBeenCalledTimes(1);
      expect(heard).not.toHaveBeenCalled();
      act(() => input.dispatchEvent(event(",", { repeat: true })));
      act(() => input.dispatchEvent(event(",", { isComposing: true })));
      expect(toggle).toHaveBeenCalledTimes(1);
      heard.mockClear();
      resetChord();
      act(() => input.dispatchEvent(event("k")));
      const second = event(",");
      act(() => input.dispatchEvent(second));
      expect(second.defaultPrevented).toBe(true);
      expect(heard).toHaveBeenCalledTimes(2);
      expect(toggle).toHaveBeenCalledTimes(1);
      act(() => root.render(createElement(Keys, { blocked: true })));
      act(() => input.dispatchEvent(event(",")));
      expect(toggle).toHaveBeenCalledTimes(1);
    } finally {
      act(() => root.unmount());
      host.remove();
      resetChord();
    }
  });
});
