// The frame guard's DOM half (R-1; amendment 2 §E.8) in jsdom: what it does
// when a registered frame gains focus. Chrome's own event order is measured
// in tests/face/hud_v2_browser_check.py; here a frame's focus is jsdom's
// `focusin` on the iframe element, which runs the same decision.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LOAD_GRACE_MS, RESTORE_MS, TAB_MS } from "../lib/frameguard";
import {
  allowRestore, cancelLoad, check, frameGuardProbe, installFrameGuard, ownLoad, registerFrame,
  resetFrameGuardForTests, setClock,
} from "./frameguard";

let t = 1000;
let uninstall: () => void = () => {};
let warn: ReturnType<typeof vi.spyOn>;

interface Made {
  frame: HTMLIFrameElement;
  shield: HTMLElement;
  host: HTMLElement;
  stolen: number;
  focusNotice: boolean | null;
  off: () => void;
}

function frameIn(pane: number, opts: { drawn?: boolean; register?: boolean } = {}): Made {
  const section = document.querySelector(`[data-testid="pane-${pane}"]`)!;
  const host = document.createElement("div");
  const frame = document.createElement("iframe");
  frame.setAttribute("tabindex", "0"); // jsdom focuses an iframe only with one
  frame.setAttribute("data-browser-frame", `${pane}:0`);
  frame.src = "about:blank";
  (frame as any).getClientRects = () => [{ width: 10, height: 10 }];
  const shield = document.createElement("div");
  const made: Made = { frame, shield, host, stolen: 0, focusNotice: null, off: () => {} };
  made.off = registerFrame({
    frame, shield, host, key: `${pane}:0`, pane, slot: () => pane,
    drawn: () => opts.drawn ?? true, onStolen: (n) => { made.stolen += 1; made.focusNotice = n; },
  });
  host.append(frame, shield);
  section.append(host);
  return made;
}

function key(k: string, extra: KeyboardEventInit = {}) {
  window.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true, ...extra }));
}

function pointer(type: string, target: Element) {
  target.dispatchEvent(new MouseEvent(type, { bubbles: true }));
}

beforeEach(() => {
  t = 1000;
  setClock(() => t);
  document.body.innerHTML =
    '<section data-testid="pane-1" data-selected="true"><textarea data-testid="input"></textarea>' +
    '<button data-testid="other">other</button></section>' +
    '<section data-testid="pane-2"><input data-testid="browser-url"></section>' +
    '<section data-testid="pane-3"></section>';
  uninstall = installFrameGuard();
  warn = vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  uninstall();
  resetFrameGuardForTests();
  warn.mockRestore();
  document.body.innerHTML = "";
});

function box(): HTMLTextAreaElement {
  return document.querySelector('[data-testid="input"]') as HTMLTextAreaElement;
}

describe("a frame that takes focus with no gesture", () => {
  it("is removed, focus goes back to the box with its selection, the pane is told, one line is written", () => {
    const b = box();
    b.value = "the owner keeps typing";
    b.focus();
    b.setSelectionRange(4, 9);
    const f = frameIn(2);
    f.frame.focus();
    expect(f.frame.isConnected).toBe(false);
    expect(f.stolen).toBe(1);
    expect(document.activeElement).toBe(b);
    expect([b.selectionStart, b.selectionEnd]).toEqual([4, 9]);
    expect(warn).toHaveBeenCalledTimes(1);
    expect(String(warn.mock.calls[0][0])).toContain("pane 2");
    expect(String(warn.mock.calls[0][0])).not.toContain("about:blank");
    expect(frameGuardProbe().frames).toEqual([]);
  });

  it("with nothing that lost focus to it, focuses nothing that types, and asks the notice to take focus (H1)", () => {
    const f = frameIn(2);
    f.frame.focus();
    expect(f.frame.isConnected).toBe(false);
    expect(document.activeElement).toBe(document.body);
    expect(f.focusNotice).toBe(true);
  });

  it("gives focus back only to what lost it to this theft, never to an element that had it earlier (H1)", () => {
    box().focus();
    const other = document.querySelector('[data-testid="other"]') as HTMLElement;
    other.focus(); // the box had it, then the button
    const f = frameIn(2);
    f.frame.focus();
    expect(document.activeElement).toBe(other);
    expect(f.focusNotice).toBe(false);
  });
});

describe("the owner's gestures", () => {
  it("the pointer over the frame (seen on its shield) lets it keep focus", () => {
    (document.querySelector('[data-testid="other"]') as HTMLElement).focus(); // no text control has the keyboard
    const f = frameIn(2);
    pointer("pointermove", f.shield);
    expect(f.shield.style.pointerEvents).toBe("none");
    f.frame.focus();
    expect(f.frame.isConnected).toBe(true);
    expect(document.activeElement).toBe(f.frame);
    expect(frameGuardProbe().holding).toBe("2:0");
  });

  it("a pointerover on the shield alone is not the pointer over it (Chrome sends one when layout moves)", () => {
    box().focus();
    const f = frameIn(2);
    pointer("pointerover", f.shield);
    f.frame.focus();
    expect(f.frame.isConnected).toBe(false);
  });

  it("the pointer moving anywhere else brings the shield back", () => {
    const f = frameIn(2);
    pointer("pointermove", f.shield);
    pointer("pointermove", document.querySelector('[data-testid="other"]')!);
    expect(f.shield.style.pointerEvents).toBe("auto");
    box().focus();
    f.frame.focus();
    expect(f.frame.isConnected).toBe(false);
  });

  it("while a text control has the keyboard, a move or a wheel over a page never counts (M2)", () => {
    box().focus();
    const f = frameIn(2);
    pointer("pointermove", f.shield);
    pointer("wheel", f.shield);
    expect(f.shield.style.pointerEvents).toBe("auto");
    f.frame.focus();
    expect(f.frame.isConnected).toBe(false);
    expect(document.activeElement).toBe(box());
  });

  it("while a text control has the keyboard, a press puts the keyboard in the page, from the HUD (M2)", () => {
    box().focus();
    const f = frameIn(2);
    let focused = 0;
    Object.defineProperty(f.frame, "contentWindow", { value: { focus: () => { focused += 1; } } });
    const down = new MouseEvent("pointerdown", { bubbles: true, cancelable: true });
    f.shield.dispatchEvent(down);
    expect(down.defaultPrevented).toBe(true);
    expect(focused).toBe(1);
    expect(f.shield.style.pointerEvents).toBe("none");
    f.frame.focus();
    expect(f.frame.isConnected).toBe(true);
  });

  it("focus arriving on a text control brings a resting pointer's shield back (M2)", () => {
    (document.querySelector('[data-testid="other"]') as HTMLElement).focus();
    const f = frameIn(2);
    pointer("pointermove", f.shield);
    expect(f.shield.style.pointerEvents).toBe("none");
    box().focus();
    expect(f.shield.style.pointerEvents).toBe("auto");
  });

  it("a key typed in the HUD stops a resting pointer counting", () => {
    const other = document.querySelector('[data-testid="other"]') as HTMLElement;
    other.focus();
    const f = frameIn(2);
    pointer("pointermove", f.shield);
    expect(f.shield.style.pointerEvents).toBe("none");
    key("a");
    expect(f.shield.style.pointerEvents).toBe("auto");
    f.frame.focus();
    expect(f.frame.isConnected).toBe(false);
    expect(document.activeElement).toBe(other);
  });

  it("the window losing focus to another program stops a pointer last seen over a page counting", () => {
    box().focus();
    const f = frameIn(2);
    pointer("pointermove", f.shield);
    window.dispatchEvent(new FocusEvent("blur"));
    expect(f.shield.style.pointerEvents).toBe("auto");
    f.frame.focus();
    expect(f.frame.isConnected).toBe(false);
  });

  it("Tab or Shift+Tab within TAB_MS, and not after", () => {
    box().focus();
    const f = frameIn(2);
    key("Shift");
    key("Tab", { shiftKey: true });
    t += TAB_MS;
    f.frame.focus();
    expect(f.frame.isConnected).toBe(true);

    box().focus();
    const g = frameIn(3);
    key("Tab");
    t += TAB_MS + 1;
    g.frame.focus();
    expect(g.frame.isConnected).toBe(false);
  });

  it("a Tab something cancelled (Monaco indenting) is no gesture (M1)", () => {
    box().focus();
    const f = frameIn(2);
    const stop = (e: Event) => e.preventDefault();
    window.addEventListener("keydown", stop);
    key("Tab", { cancelable: true });
    window.removeEventListener("keydown", stop);
    t += 120;
    f.frame.focus();
    expect(f.frame.isConnected).toBe(false);
  });

  it("a Tab counts only for the frame it landed in (M1)", async () => {
    box().focus();
    const f = frameIn(2);
    key("Tab"); // it lands on the chat box's neighbour, not a frame
    (document.querySelector('[data-testid="other"]') as HTMLElement).focus();
    await new Promise((r) => setTimeout(r, 5));
    t += 100;
    f.frame.focus();
    expect(f.frame.isConnected).toBe(false);

    box().focus();
    const a = frameIn(2);
    const b = frameIn(3);
    key("Tab");
    a.frame.focus(); // the Tab's own move into A
    expect(a.frame.isConnected).toBe(true);
    await new Promise((r) => setTimeout(r, 5));
    t += 50;
    b.frame.focus(); // B grabs within TAB_MS
    expect(b.frame.isConnected).toBe(false);
  });

  it("where a Tab landed is the first place focus went, so a grab right after it is no Tab (M1)", () => {
    box().focus();
    const b = frameIn(3);
    key("Tab");
    (document.querySelector('[data-testid="other"]') as HTMLElement).focus(); // the Tab's own move
    b.frame.focus(); // B grabs in the same breath
    expect(b.frame.isConnected).toBe(false);
  });

  it("the owner's own load, until LOAD_GRACE_MS after the frame's load", () => {
    box().focus();
    ownLoad("2:0");
    const f = frameIn(2);
    f.frame.dispatchEvent(new Event("load"));
    t += LOAD_GRACE_MS;
    f.frame.focus();
    expect(f.frame.isConnected).toBe(true);

    box().focus();
    ownLoad("3:0");
    const g = frameIn(3);
    g.frame.dispatchEvent(new Event("load"));
    t += LOAD_GRACE_MS + 1;
    g.frame.focus();
    expect(g.frame.isConnected).toBe(false);
  });

  it("the owner's own load stops counting once they move on to another pane, or type", () => {
    const url = document.querySelector('[data-testid="browser-url"]') as HTMLElement;
    url.focus();
    ownLoad("2:0");
    const f = frameIn(2);
    box().focus(); // outside pane 2
    f.frame.focus();
    expect(f.frame.isConnected).toBe(false);

    url.focus();
    ownLoad("2:0");
    const g = frameIn(2);
    key("x");
    g.frame.focus();
    expect(g.frame.isConnected).toBe(false);
  });

  it("a load that is not the owner's carries none over", () => {
    box().focus();
    ownLoad("2:0");
    cancelLoad("2:0");
    const f = frameIn(2);
    f.frame.focus();
    expect(f.frame.isConnected).toBe(false);
  });

  it("an own load is claimed by the first frame made after it only", () => {
    box().focus();
    ownLoad("2:0");
    const f = frameIn(2);
    f.off();
    f.frame.remove();
    const g = frameIn(2);
    g.frame.focus();
    expect(g.frame.isConnected).toBe(false);
  });
});

describe("never a gesture", () => {
  it("under a card any frame gaining focus goes, and focus goes to the card", () => {
    box().focus();
    const f = frameIn(2);
    pointer("pointermove", f.shield);
    const veil = document.createElement("div");
    veil.id = "authveil";
    veil.innerHTML = '<div data-testid="approval-card" tabindex="-1"></div>';
    document.body.append(veil);
    f.frame.focus();
    expect(f.frame.isConnected).toBe(false);
    expect((document.activeElement as HTMLElement).getAttribute("data-testid")).toBe("approval-card");
  });

  it("a hidden or background frame goes, pointer over it or not", () => {
    box().focus();
    const f = frameIn(2, { drawn: false });
    pointer("pointermove", f.shield);
    f.frame.focus();
    expect(f.frame.isConnected).toBe(false);
  });

  it("a frame with no box on screen (zero client rects) counts as hidden", () => {
    box().focus();
    const f = frameIn(2);
    (f.frame as any).getClientRects = () => [];
    pointer("pointermove", f.shield);
    f.frame.focus();
    expect(f.frame.isConnected).toBe(false);
  });
});

describe("the HUD giving focus back after a card", () => {
  it("is allowed once, for that frame, within RESTORE_MS", () => {
    box().focus();
    const f = frameIn(2);
    allowRestore(f.frame);
    f.frame.focus();
    expect(f.frame.isConnected).toBe(true);

    box().focus();
    f.frame.focus(); // the grant was spent
    expect(f.frame.isConnected).toBe(false);

    box().focus();
    const g = frameIn(3);
    allowRestore(g.frame);
    t += RESTORE_MS + 1;
    g.frame.focus();
    expect(g.frame.isConnected).toBe(false);
  });
});

describe("frame to frame", () => {
  it("a frame grabbing focus from one the owner put it in goes, and focus goes to nothing that types (H1)", () => {
    box().value = "a draft";
    box().focus();
    const a = frameIn(2);
    const b = frameIn(3);
    const down = new MouseEvent("pointerdown", { bubbles: true, cancelable: true });
    a.shield.dispatchEvent(down); // the owner clicks into page A from the chat box
    a.frame.focus();
    expect(document.activeElement).toBe(a.frame);
    b.frame.focus();
    expect(b.frame.isConnected).toBe(false);
    expect(a.frame.isConnected).toBe(true);
    expect(document.activeElement).not.toBe(box());
    expect(b.focusNotice).toBe(true);
  });

  it("the check alone (the belt) catches a frame that took focus with no event", () => {
    box().focus();
    const f = frameIn(2);
    uninstall(); // no listeners: only what check() sees
    uninstall = () => {};
    f.frame.focus();
    expect(f.frame.isConnected).toBe(true);
    check();
    expect(f.frame.isConnected).toBe(false);
  });

  it("a held frame that stops being drawn loses the keyboard to nothing that types, and is not removed", () => {
    (document.querySelector('[data-testid="other"]') as HTMLElement).focus();
    let drawn = true;
    const section = document.querySelector('[data-testid="pane-2"]')!;
    const host = document.createElement("div");
    const frame = document.createElement("iframe");
    frame.setAttribute("tabindex", "0");
    (frame as any).getClientRects = () => [{}];
    const shield = document.createElement("div");
    registerFrame({ frame, shield, host, key: "2:0", pane: 2, slot: () => 2, drawn: () => drawn, onStolen: () => {} });
    host.append(frame, shield);
    section.append(host);
    pointer("pointermove", shield);
    frame.focus();
    expect(document.activeElement).toBe(frame);
    drawn = false;
    check();
    expect(frame.isConnected).toBe(true);
    expect(document.activeElement).toBe(document.body);
  });
});
