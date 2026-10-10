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
      models: [{ id: "claude-opus-5-5", efforts: ["low", "medium", "high", "xhigh", "max"] }],
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
  let answers: Deferred[];

  const render = (choice: Choice) =>
    act(() => {
      root.render(createElement(EffortSlider, {
        models: TM,
        choice,
        onCommit: (effort: string | null) => {
          sent.push(effort);
          const d = deferred();
          answers.push(d);
          return d.promise;
        },
      }));
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
    answers = [];
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
