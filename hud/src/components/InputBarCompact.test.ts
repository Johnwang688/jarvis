// WP-C of docs/plans/2026-10-19-compact-button-plan.md section 4, with the
// owner's answer CB-2 (confirm first, inline, not window.confirm). Tests first:
// on today's code InputBar has no `compact` prop, so every case that looks for
// the chip fails with "no chip", and the absent-chip cases pass vacuously
// (they guard the "shown for tasks / compose" mutation once it exists).
//
// THE INTERFACE THE IMPLEMENTER BUILDS (chosen where the plan is silent)
// ======================================================================
//   InputBar prop   compact?: { state: "ready" | "running" | "off";
//                               title: string; onCompact: () => void } | null
//   The chip        <button type="button" class="chip compactbtn"
//                    data-testid="compact" data-state={state}
//                    aria-label="Compact the conversation" title={title}>
//                   in the lower `.row`, right after `props.modelChip` and before
//                   `pane-status`. Label "Compact", or "Compacting…" when
//                   state is "running". `disabled` unless state is "ready".
//                   `title` is the caller's (lib/compact.ts decides the words,
//                   see lib/compact.test.ts); InputBar shows it as given.
//   The confirm     A click on the ready chip does NOT call onCompact. It shows
//                   `[data-testid="compact-confirm"]`, whose text includes
//                   "Compact this conversation?", containing
//                   `compact-confirm-yes` (label "Compact") and
//                   `compact-confirm-no` (label "Cancel"), as plain buttons.
//                   Yes calls onCompact exactly once and closes the confirm.
//                   Cancel closes it. Escape closes it from anywhere: the box,
//                   the confirm's own buttons, or the page (the box stops its
//                   keys at the textarea, so a window-level listener alone
//                   will not do: use a document capture listener, or handle
//                   Escape in the box too). The confirm is also gone, with
//                   nothing called, the moment `state` is not "ready" (a turn
//                   starts, a card arrives), and does not come back when the
//                   state returns to "ready".
//   Focus           Rendering, and any change of `state`, never moves focus
//                   (the chip never takes the composer's focus; CLAUDE.md,
//                   no frame/element takes the keyboard without a gesture).
//
// Mutations (each case names its own): no confirm step; yes posts twice;
// enabled under a card (state "off" but clickable); enabled while running;
// chip drawn when `compact` is absent (tasks, compose).

import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { InputBar } from "./InputBar";

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;

type State = "ready" | "running" | "off";
const TITLE = "Run Claude Code's /compact: it summarises the conversation so the model's context shrinks. Every message stays here.";

describe("InputBar Compact chip", () => {
  let host: HTMLDivElement;
  let root: Root;
  let calls: number;
  let sends: number;

  beforeEach(() => {
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    calls = 0;
    sends = 0;
  });

  afterEach(() => {
    act(() => root.unmount());
    host.remove();
  });

  const draw = (compact: { state: State; title?: string } | null | undefined, extra: Record<string, unknown> = {}) => {
    act(() => {
      root.render(createElement(InputBar, {
        pendingTranscript: "",
        onSend: () => { sends++; },
        onTranscriptTaken: () => {},
        modelChip: createElement("span", { "data-testid": "model-chip" }, "claude"),
        status: "idle",
        ...(compact === undefined ? {} : {
          compact: compact === null ? null : { state: compact.state, title: compact.title ?? TITLE, onCompact: () => { calls++; } },
        }),
        ...extra,
      }));
    });
  };
  const q = (id: string) => host.querySelector(`[data-testid="${id}"]`) as HTMLElement | null;
  const chip = () => q("compact") as HTMLButtonElement | null;
  const need = () => {
    const c = chip();
    expect(c, "the compact chip is drawn").not.toBeNull();
    return c!;
  };
  const confirm = () => q("compact-confirm");
  const click = (el: Element | null) => {
    if (!el) throw new Error("nothing to click: the compact chip or its confirm is not in the DOM");
    act(() => { (el as HTMLElement).click(); });
  };
  const key = (target: EventTarget, k: string) =>
    act(() => { target.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true })); });

  // -- absent ---------------------------------------------------------------

  it("draws no chip when the caller passes none (compose panes, tasks)", () => {
    draw(undefined);
    expect(chip()).toBeNull();
    draw(null);
    expect(chip()).toBeNull();
    expect(confirm()).toBeNull();
  });

  // -- the chip -------------------------------------------------------------

  it("ready: a plain Graphite chip button, labelled, titled and enabled", () => {
    draw({ state: "ready" });
    const c = need();
    expect(c).not.toBeNull();
    expect(c.tagName).toBe("BUTTON");
    expect(c.getAttribute("type")).toBe("button");
    expect(c.classList.contains("chip")).toBe(true);
    expect(c.classList.contains("compactbtn")).toBe(true);
    expect(c.getAttribute("data-state")).toBe("ready");
    expect(c.getAttribute("aria-label")).toBe("Compact the conversation");
    expect(c.textContent?.trim()).toBe("Compact");
    expect(c.getAttribute("title")).toBe(TITLE);
    expect(c.disabled).toBe(false);
  });

  it("sits in the lower row, after the model chip and before the status line", () => {
    draw({ state: "ready" });
    const row = need().closest(".row");
    expect(row).not.toBeNull();
    const before = q("model-chip")!.compareDocumentPosition(need());
    const after = need().compareDocumentPosition(q("pane-status")!);
    expect(before & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(after & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(row!.contains(q("model-chip"))).toBe(true);
  });

  it("running: says Compacting…, is disabled, and a click does nothing", () => {
    draw({ state: "running", title: "Compacting…" });
    const c = need();
    expect(c.getAttribute("data-state")).toBe("running");
    expect(c.getAttribute("aria-label")).toBe("Compact the conversation");
    expect(c.textContent?.trim()).toBe("Compacting…");
    expect(c.disabled).toBe(true);
    click(c);
    act(() => { c.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
    expect(confirm()).toBeNull();
    expect(calls).toBe(0);
  });

  it("off (a turn or a card): disabled, keeps the caller's reason as its title, and a click does nothing", () => {
    draw({ state: "off", title: "Answer the card first." });
    const c = need();
    expect(c.getAttribute("data-state")).toBe("off");
    expect(c.textContent?.trim()).toBe("Compact");
    expect(c.getAttribute("title")).toBe("Answer the card first.");
    expect(c.disabled).toBe(true);
    click(c);
    act(() => { c.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
    expect(confirm()).toBeNull();
    expect(calls).toBe(0);
  });

  // -- the confirm (CB-2) ---------------------------------------------------

  it("the first click only asks: it shows the confirm and calls nothing", () => {
    draw({ state: "ready" });
    expect(confirm()).toBeNull();
    click(chip());
    const ask = confirm();
    expect(ask).not.toBeNull();
    expect(ask!.textContent).toContain("Compact this conversation?");
    expect(q("compact-confirm-yes")!.tagName).toBe("BUTTON");
    expect(q("compact-confirm-yes")!.textContent?.trim()).toBe("Compact");
    expect(q("compact-confirm-no")!.tagName).toBe("BUTTON");
    expect(q("compact-confirm-no")!.textContent?.trim()).toBe("Cancel");
    expect(calls).toBe(0);
    expect(sends).toBe(0);
  });

  it("is not a blocking window.confirm", () => {
    const asked = vi.spyOn(window, "confirm").mockReturnValue(true);
    try {
      draw({ state: "ready" });
      click(chip());
      expect(asked).not.toHaveBeenCalled();
      expect(confirm()).not.toBeNull();
      expect(calls).toBe(0);
    } finally {
      asked.mockRestore();
    }
  });

  it("the confirm click calls onCompact once, closes the confirm, and sends no message", () => {
    draw({ state: "ready" });
    click(chip());
    const yes = q("compact-confirm-yes")!;
    click(yes);
    expect(calls).toBe(1);
    expect(confirm()).toBeNull();
    // A second click on the same (now removed) button is nothing.
    click(yes);
    expect(calls).toBe(1);
    expect(sends).toBe(0);
  });

  it("a second click on the chip after a confirm asks again; it does not post", () => {
    draw({ state: "ready" });
    click(chip());
    click(q("compact-confirm-yes"));
    expect(calls).toBe(1);
    click(chip());
    expect(confirm()).not.toBeNull();
    expect(calls).toBe(1);
  });

  it("Cancel dismisses the confirm and sends nothing; asking again still works", () => {
    draw({ state: "ready" });
    click(chip());
    click(q("compact-confirm-no"));
    expect(confirm()).toBeNull();
    expect(calls).toBe(0);
    click(chip());
    expect(confirm()).not.toBeNull();
    click(q("compact-confirm-yes"));
    expect(calls).toBe(1);
  });

  it("Escape in the composer dismisses the confirm and sends nothing", () => {
    draw({ state: "ready" });
    click(chip());
    expect(confirm()).not.toBeNull();
    key(q("input")!, "Escape");
    expect(confirm()).toBeNull();
    expect(calls).toBe(0);
  });

  it("Escape on the confirm's own buttons dismisses it and sends nothing", () => {
    draw({ state: "ready" });
    click(chip());
    key(q("compact-confirm-yes")!, "Escape");
    expect(confirm()).toBeNull();
    expect(calls).toBe(0);
  });

  it("Escape with focus elsewhere on the page dismisses it and sends nothing", () => {
    draw({ state: "ready" });
    click(chip());
    key(document.body, "Escape");
    expect(confirm()).toBeNull();
    expect(calls).toBe(0);
  });

  it("other keys do not dismiss or confirm it", () => {
    draw({ state: "ready" });
    click(chip());
    key(q("input")!, "a");
    key(q("input")!, "Enter");
    expect(confirm()).not.toBeNull();
    expect(calls).toBe(0);
  });

  it("a card (or a turn) arriving while it is open dismisses it, nothing is sent, and it stays gone", () => {
    draw({ state: "ready" });
    click(chip());
    expect(confirm()).not.toBeNull();
    draw({ state: "off", title: "Answer the card first." });
    expect(confirm()).toBeNull();
    expect(need().disabled).toBe(true);
    expect(calls).toBe(0);
    // The card is answered: the chip is ready again, but nobody asked.
    draw({ state: "ready" });
    expect(confirm()).toBeNull();
    expect(need().getAttribute("data-state")).toBe("ready");
    expect(calls).toBe(0);
  });

  it("the compaction starting under an open confirm dismisses it too", () => {
    draw({ state: "ready" });
    click(chip());
    draw({ state: "running", title: "Compacting…" });
    expect(confirm()).toBeNull();
    expect(calls).toBe(0);
  });

  // -- focus ----------------------------------------------------------------

  it("never takes focus from the composer, whatever the chip's state does", () => {
    draw({ state: "ready" });
    expect(chip(), "the chip is drawn").not.toBeNull();
    const box = q("input") as HTMLTextAreaElement;
    act(() => box.focus());
    expect(document.activeElement).toBe(box);
    for (const state of ["running", "ready", "off", "ready"] as State[]) {
      draw({ state });
      expect(document.activeElement).toBe(box);
    }
    draw(null);
    draw({ state: "ready" });
    expect(document.activeElement).toBe(box);
  });

  it("a fresh render leaves focus where it was (nowhere), not on the chip", () => {
    (document.activeElement as HTMLElement | null)?.blur?.();
    draw({ state: "ready" });
    expect(chip(), "the chip is drawn").not.toBeNull();
    expect(document.activeElement).not.toBe(chip());
    expect(host.contains(document.activeElement) && document.activeElement !== document.body).toBe(false);
  });
});
