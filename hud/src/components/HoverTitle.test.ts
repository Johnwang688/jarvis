import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { HoverTitle } from "./HoverTitle";

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;

describe("hovered chat titles", () => {
  let host: HTMLDivElement;
  let root: Root;
  let resize: ResizeObserverCallback;
  let changed: () => void;
  let reduced = false;
  let textWidth = 300;
  let width = 100;
  let cancelled: ReturnType<typeof vi.fn>[];
  let animate: ReturnType<typeof vi.fn>;
  let disconnect: ReturnType<typeof vi.fn>;

  const render = (text = "A long chat title") => act(() => root.render(createElement(HoverTitle, {
    text, untitled: false, testid: "title", onDoubleClick: () => {},
  })));
  const title = () => host.querySelector<HTMLElement>('[data-testid="title"]')!;
  const hover = () => act(() => title().dispatchEvent(new MouseEvent("mouseover", { bubbles: true })));
  const leave = () => act(() => title().dispatchEvent(new MouseEvent("mouseout", { bubbles: true })));

  beforeEach(() => {
    reduced = false;
    width = 100;
    textWidth = 300;
    cancelled = [];
    disconnect = vi.fn();
    vi.stubGlobal("ResizeObserver", class {
      constructor(fn: ResizeObserverCallback) { resize = fn; }
      observe() {}
      disconnect = disconnect;
    });
    vi.stubGlobal("matchMedia", () => ({
      get matches() { return reduced; },
      addEventListener: (_: string, fn: () => void) => { changed = fn; },
      removeEventListener: vi.fn(),
    }));
    vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockImplementation(() => textWidth);
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(() => width);
    animate = vi.fn(() => {
      const cancel = vi.fn();
      cancelled.push(cancel);
      return { cancel };
    });
    // jsdom has no Web Animations API; browser tests cover the real geometry.
    Object.defineProperty(HTMLElement.prototype, "animate", { configurable: true, value: animate });
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    render();
  });

  afterEach(() => {
    act(() => root.unmount());
    host.remove();
    delete (HTMLElement.prototype as any).animate;
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("only animates while hovered and restores the truncated state on leave", () => {
    expect(animate).not.toHaveBeenCalled();
    hover();
    expect(title().dataset.scrolling).toBe("true");
    expect(animate).toHaveBeenCalledOnce();
    leave();
    expect(cancelled[0]).toHaveBeenCalledOnce();
    expect(title().dataset.scrolling).toBeUndefined();
    expect(disconnect).toHaveBeenCalledOnce();
  });

  it("keeps fitting titles still and stops scrolling when a resize makes it fit", () => {
    textWidth = 50;
    hover();
    expect(animate).not.toHaveBeenCalled();
    textWidth = 300;
    act(() => resize([], {} as ResizeObserver));
    expect(title().dataset.scrolling).toBe("true");
    width = 400;
    act(() => resize([], {} as ResizeObserver));
    expect(cancelled[0]).toHaveBeenCalledOnce();
    expect(title().dataset.scrolling).toBeUndefined();
  });

  it("restarts for a live title change and cancels the old animation on unmount", () => {
    hover();
    render("A newly generated chat title");
    expect(cancelled[0]).toHaveBeenCalledOnce();
    expect(animate).toHaveBeenCalledTimes(2);
    expect(title().textContent).toBe("A newly generated chat title");
    expect(title().title).toBe("A newly generated chat title");
    act(() => root.unmount());
    expect(cancelled[1]).toHaveBeenCalledOnce();
  });

  it("honors reduced motion, including preference changes during hover", () => {
    reduced = true;
    hover();
    expect(animate).not.toHaveBeenCalled();
    expect(title().title).toBe("A long chat title");
    reduced = false;
    act(() => changed());
    expect(animate).toHaveBeenCalledOnce();
    reduced = true;
    act(() => changed());
    expect(cancelled[0]).toHaveBeenCalledOnce();
    expect(title().dataset.scrolling).toBeUndefined();
  });
});
