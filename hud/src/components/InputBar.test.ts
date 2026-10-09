// The input box takes each hand-back exactly once (Bugbot on PR #22): its
// effect used to depend on the whole props object, so any parent render while
// a restore was still pending prepended the same words again.

import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { InputBar } from "./InputBar";
import type { GiveBack } from "../lib/giveback";

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;

function render(root: Root, restore: GiveBack[], level: number, taken: number[]) {
  act(() => {
    root.render(
      createElement(InputBar, {
        mode: "review",
        level,
        hint: "",
        pendingTranscript: "",
        onModeChange: () => {},
        onSend: () => {},
        onTranscriptTaken: () => {},
        restore,
        // A parent that has not cleared the hand-back yet: it stays pending
        // across the renders below, which is the case that used to double.
        onRestoreTaken: (nonce: number) => taken.push(nonce),
      }),
    );
  });
}

describe("InputBar hand-backs", () => {
  let host: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
  });

  afterEach(() => {
    act(() => root.unmount());
    host.remove();
  });

  const box = () => host.querySelector('[data-testid="input"]') as HTMLTextAreaElement;

  it("puts handed-back words in the box once, however often the parent renders", () => {
    const taken: number[] = [];
    const restore = [{ text: "use the other file", files: [], nonce: 41 }];
    render(root, restore, 0, taken);
    expect(box().value).toBe("use the other file");
    // The parent re-renders (the mic level moves) with the hand-back still set.
    render(root, restore, 0.2, taken);
    render(root, [...restore], 0.4, taken);
    expect(box().value).toBe("use the other file");
    expect(taken).toEqual([41]);
  });

  it("takes a newer hand-back, and only the new words", () => {
    const taken: number[] = [];
    const first = { text: "first", files: [], nonce: 51 };
    render(root, [first], 0, taken);
    render(root, [first, { text: "second", files: [], nonce: 52 }], 0.1, taken);
    expect(box().value).toBe("second\nfirst");
    expect(taken).toEqual([51, 52]);
  });
});
