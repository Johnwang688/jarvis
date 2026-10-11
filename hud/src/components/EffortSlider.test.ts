// The effort slider on its own, outside the popover that also stops keys:
// its own guards have to hold wherever it is mounted (the multi-chat panes
// will mount it again). The browser suite drives it inside the popover.

import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { EffortSlider } from "./EffortSlider";
import { SETTLE_MS } from "../lib/effortSlider";
import type { Choice, ThreadModels } from "../lib/threadmodel";

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;

const TM: ThreadModels = {
  effort_default: "high",
  providers: {
    claude: {
      label: "Claude", default: "claude-opus-5-5", default_effort: "high",
      models: [
        { id: "claude-opus-5-5", efforts: ["low", "medium", "high", "xhigh", "max"] },
        { id: "claude-small", efforts: ["low", "medium", "high"] },
      ],
    },
  },
};
const onDefault: Choice = { provider: "claude", model: null, effort: null };

interface Deferred { resolve: () => void; promise: Promise<void> }
function deferred(): Deferred {
  let resolve = () => {};
  const promise = new Promise<void>((r) => { resolve = r; });
  return { resolve, promise };
}

describe("EffortSlider", () => {
  let host: HTMLDivElement;
  let root: Root;
  let sent: (string | null)[];
  let on: string[];
  let answers: Deferred[];
  // What a React handler around the slider hears — the popover's own
  // onKeyDown, where its Tab round lives. A listener on the root container
  // would hear keys React already stopped, so it cannot tell.
  let around: string[];

  const render = (choice: Choice, disabled = false, conversation = "t1|false") =>
    act(() => {
      root.render(createElement("div", { onKeyDown: (e: { key: string }) => around.push(e.key) },
        createElement(EffortSlider, {
          models: TM,
          choice,
          disabled,
          conversation,
          onCommit: (effort: string | null, from: string) => {
            sent.push(effort);
            on.push(from);
            const d = deferred();
            answers.push(d);
            return d.promise;
          },
        })));
    });
  const slider = () => host.querySelector('[data-testid="effort-slider"]') as HTMLElement;
  const key = (k: string, type = "keydown") =>
    act(() => {
      slider().dispatchEvent(new KeyboardEvent(type, { key: k, code: k === " " ? "Space" : k, bubbles: true }));
    });
  const settle = () => act(() => { vi.advanceTimersByTime(SETTLE_MS + 10); });

  beforeEach(() => {
    vi.useFakeTimers();
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    sent = [];
    on = [];
    answers = [];
    around = [];
  });

  afterEach(() => {
    act(() => root.unmount());
    host.remove();
    vi.useRealTimers();
  });

  it("is a slider that says where it is", () => {
    render(onDefault);
    expect(slider().getAttribute("role")).toBe("slider");
    expect(slider().getAttribute("aria-valuemin")).toBe("0");
    expect(slider().getAttribute("aria-valuemax")).toBe("4");
    expect(slider().getAttribute("aria-valuenow")).toBe("2");
    expect(slider().getAttribute("aria-valuetext")).toBe("High, default");
  });

  it("keeps Space and its keys from push-to-talk and every HUD hotkey", () => {
    render(onDefault);
    const heard: string[] = [];
    const listen = (e: KeyboardEvent) => heard.push(`${e.type}:${e.key}`);
    document.addEventListener("keydown", listen);
    document.addEventListener("keyup", listen);
    try {
      for (const k of [" ", "ArrowRight", "Home", "Enter"]) {
        key(k);
        key(k, "keyup");
      }
    } finally {
      document.removeEventListener("keydown", listen);
      document.removeEventListener("keyup", listen);
    }
    expect(heard).toEqual([]);
  });

  it("lets Tab go on to the popover's round (ModelChip), and never moves on it", () => {
    render(onDefault);
    let prevented: boolean | null = null;
    const listen = (e: KeyboardEvent) => { prevented = e.defaultPrevented; };
    host.addEventListener("keydown", listen);
    try {
      key("Tab");
      key("ArrowRight");
      key(" ");
    } finally {
      host.removeEventListener("keydown", listen);
    }
    // The popover's handler hears Tab, and only Tab: the slider's own keys stop there.
    expect(around).toEqual(["Tab"]);
    expect(prevented).toBe(false);
    settle();
    expect(sent).toEqual(["xhigh"]);
  });

  it("disabled (an authorization card is up): out of the Tab order, and no key or pointer moves it", () => {
    render(onDefault, true);
    expect(slider().getAttribute("tabindex")).toBe("-1");
    expect(slider().getAttribute("aria-disabled")).toBe("true");
    for (const k of ["ArrowRight", "End", "Home", "ArrowLeft"]) key(k);
    // jsdom may lack PointerEvent; React reads the type, button and clientX.
    const Ptr = (typeof PointerEvent === "undefined" ? MouseEvent : PointerEvent) as typeof MouseEvent;
    act(() => {
      slider().dispatchEvent(new Ptr("pointerdown", { button: 0, clientX: 0, bubbles: true }));
      slider().dispatchEvent(new Ptr("pointerup", { button: 0, clientX: 0, bubbles: true }));
    });
    settle();
    expect(slider().getAttribute("aria-valuenow")).toBe("2");
    expect(sent).toEqual([]);
    // Its keys are still kept from push-to-talk while it is disabled.
    const heard: string[] = [];
    const listen = (e: KeyboardEvent) => heard.push(e.key);
    document.addEventListener("keydown", listen);
    try {
      key(" ");
      key("ArrowRight");
    } finally {
      document.removeEventListener("keydown", listen);
    }
    expect(heard).toEqual([]);
  });

  it("commits on the conversation the move was made on", () => {
    render(onDefault);
    key("ArrowRight");
    settle();
    expect(sent).toEqual(["xhigh"]);
    expect(on).toEqual(["t1|false"]);
  });

  it("drops a key move still settling when the pane moves on to another conversation", () => {
    render(onDefault, false, "t1|false");
    key("ArrowLeft");
    // Another thread opened in the pane, same provider and model: the timer
    // must not carry t1's move onto it.
    render(onDefault, false, "t2|false");
    expect(slider().getAttribute("aria-valuenow")).toBe("2");
    settle();
    expect(sent).toEqual([]);
    // Nor the popover closing afterwards.
    act(() => root.unmount());
    expect(sent).toEqual([]);
    root = createRoot(host); // for afterEach
  });

  it("drops it when the thread is archived and the pane re-aimed at a new compose row", () => {
    render({ ...onDefault, effort: "high" }, false, "t1|false");
    key("ArrowLeft");
    render({ provider: "claude", model: null, effort: null }, false, "|true");
    act(() => root.unmount());
    expect(sent).toEqual([]);
    root = createRoot(host);
  });

  it("drops a move whose stop the model no longer offers (its model changed under it)", () => {
    render({ provider: "claude", model: "claude-opus-5-5", effort: "low" });
    key("End"); // max, settling
    render({ provider: "claude", model: "claude-small", effort: "low" });
    settle();
    expect(sent).toEqual([]);
  });

  it("commits a key move once, after the keys settle", () => {
    render(onDefault);
    key("ArrowRight");
    key("ArrowRight");
    expect(slider().getAttribute("aria-valuenow")).toBe("4");
    expect(sent).toEqual([]);
    settle();
    expect(sent).toEqual(["max"]);
  });

  it("clears to null on the default stop, and sends nothing for no change", () => {
    render({ ...onDefault, effort: "xhigh" });
    key("ArrowLeft");
    settle();
    expect(sent).toEqual([null]);
    key("ArrowLeft");
    key("ArrowRight");
    settle();
    expect(sent).toEqual([null]);
  });

  it("commits a key move still settling when it unmounts (the popover closed)", () => {
    render(onDefault);
    key("ArrowLeft");
    act(() => root.unmount());
    expect(sent).toEqual(["medium"]);
    root = createRoot(host); // for afterEach
  });

  it("draws only the latest commit: an older answer settling first moves nothing", async () => {
    render(onDefault);
    key("ArrowRight");
    settle(); // xhigh, answer held
    key("ArrowRight");
    settle(); // max, answer held
    expect(sent).toEqual(["xhigh", "max"]);
    // The older answer lands first; the record has not moved yet.
    await act(async () => {
      answers[0].resolve();
      await answers[0].promise;
    });
    expect(slider().getAttribute("aria-valuenow")).toBe("4");
    // The latest lands, and the record says max: drawn from the record now.
    render({ ...onDefault, effort: "max" });
    await act(async () => {
      answers[1].resolve();
      await answers[1].promise;
    });
    expect(slider().getAttribute("aria-valuenow")).toBe("4");
    expect(slider().getAttribute("aria-valuetext")).toBe("Max");
  });
});
