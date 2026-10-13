// A render error inside the workspace boundary shows a Reload prompt in its
// place and goes no further: a sibling outside it (in the app, the approval
// card) stays mounted. The boundary logs the error's name, never its message.

import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { CrashProbe, DialogBoundary, WorkspaceBoundary, errorName } from "./Boundary";
import { buffers, PlainModel } from "../state/buffers";

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;

describe("errorName", () => {
  it("names a built-in error", () => {
    expect(errorName(new TypeError("x"))).toBe("TypeError");
    expect(errorName(new Error("x"))).toBe("Error");
  });

  it("never passes on anything but a plain identifier", () => {
    const e = new Error("x");
    e.name = "rm -rf ~ <b>owner's words</b>";
    expect(errorName(e)).toBe("Error");
    expect(errorName("a thrown string")).toBe("Error");
    expect(errorName(null)).toBe("Error");
    expect(errorName({ name: "TypeError" })).toBe("Error");
    const long = new Error("x");
    long.name = "A".repeat(41);
    expect(errorName(long)).toBe("Error");
  });
});

describe("WorkspaceBoundary", () => {
  let host: HTMLDivElement;
  let root: Root;
  let warn: ReturnType<typeof vi.spyOn>;
  let error: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    // React prints a caught error itself; kept out of the test output.
    error = vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    act(() => root.unmount());
    host.remove();
    delete (window as any).__hudCrashProbe;
    warn.mockRestore();
    error.mockRestore();
  });

  let crashes = 0;

  function draw() {
    act(() =>
      root.render(
        createElement(
          "div",
          null,
          createElement(
            WorkspaceBoundary,
            { onCrash: () => crashes++ },
            createElement("div", { id: "work" }, "work", createElement(CrashProbe, { where: "workspace" })),
          ),
          createElement("div", { id: "card" }, "the card"),
        ),
      ),
    );
  }

  it("draws its children while nothing throws, and the probe is inert unless set", () => {
    draw();
    expect(host.querySelector("#work")).not.toBeNull();
    expect(host.querySelector('[data-testid="hud-crashed"]')).toBeNull();
    (window as any).__hudCrashProbe = "somewhere else";
    draw();
    expect(host.querySelector("#work")).not.toBeNull();
  });

  it("keeps a throw inside: Reload in its place, the sibling still mounted", () => {
    draw();
    (window as any).__hudCrashProbe = "workspace";
    draw();
    expect(host.querySelector("#work")).toBeNull();
    const fallback = host.querySelector('[data-testid="hud-crashed"]');
    expect(fallback).not.toBeNull();
    expect(fallback!.querySelector('[data-testid="hud-crashed-reload"]')?.textContent).toBe("Reload");
    // Plain text: the only elements are the two sentences and the button.
    expect(Array.from(fallback!.querySelectorAll("*")).map((el) => el.tagName)).toEqual(["P", "P", "P", "BUTTON"]);
    expect(host.querySelector("#card")?.textContent).toBe("the card");
  });

  it("tells App once, so it can stop what would carry on unseen", () => {
    crashes = 0;
    draw();
    (window as any).__hudCrashProbe = "workspace";
    draw();
    draw();
    expect(crashes).toBe(1);
  });

  it("warns that a reload loses what was not sent (review of PR #32)", () => {
    draw();
    (window as any).__hudCrashProbe = "workspace";
    draw();
    expect(host.querySelector('[data-testid="hud-crashed"]')?.textContent).toContain(
      "Reloading loses anything not yet sent: words typed in a box and files staged for it.",
    );
  });

  it("keeps Space and Enter on Reload to itself: they press it and never reach the document", () => {
    draw();
    (window as any).__hudCrashProbe = "workspace";
    draw();
    const seen: string[] = [];
    const listen = (e: KeyboardEvent) => seen.push(e.type + ":" + e.code);
    document.addEventListener("keydown", listen);
    document.addEventListener("keyup", listen);
    const reload = host.querySelector('[data-testid="hud-crashed-reload"]')!;
    for (const type of ["keydown", "keyup"]) {
      for (const code of ["Space", "Enter"]) {
        reload.dispatchEvent(new KeyboardEvent(type, { code, key: code === "Space" ? " " : "Enter", bubbles: true }));
      }
    }
    // Escape is not Reload's: it must still reach a card's handler.
    reload.dispatchEvent(new KeyboardEvent("keydown", { code: "Escape", key: "Escape", bubbles: true }));
    document.removeEventListener("keydown", listen);
    document.removeEventListener("keyup", listen);
    expect(seen).toEqual(["keydown:Escape"]);
  });

  it("logs the error's name and nothing of its message", () => {
    draw();
    (window as any).__hudCrashProbe = "workspace";
    draw();
    const logged = warn.mock.calls.map((c: unknown[]) => c.map(String).join(" ")).join("\n");
    expect(logged).toContain("Error");
    expect(logged).not.toContain("crash probe");
  });
});

describe("DialogBoundary", () => {
  let host: HTMLDivElement;
  let root: Root;
  let warn: ReturnType<typeof vi.spyOn>;
  let error: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    error = vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    act(() => root.unmount());
    host.remove();
    delete (window as any).__hudCrashProbe;
    warn.mockRestore();
    error.mockRestore();
  });

  function draw(resetKey: string, onCrash: () => void) {
    act(() =>
      root.render(
        createElement(
          "div",
          null,
          createElement(
            DialogBoundary,
            { resetKey, onCrash },
            createElement("div", { id: "dialog" }, "a dialog", createElement(CrashProbe, { where: "dialog" })),
          ),
          createElement("div", { id: "card" }, "the card"),
        ),
      ),
    );
  }

  it("closes a dialog that throws and draws again once the open set moves on", () => {
    let closed = 0;
    draw("model", () => closed++);
    expect(host.querySelector("#dialog")).not.toBeNull();
    (window as any).__hudCrashProbe = "dialog";
    draw("model", () => closed++);
    expect(host.querySelector("#dialog")).toBeNull();
    expect(host.querySelector("#card")?.textContent).toBe("the card");
    expect(closed).toBe(1);
    // Still failed while nothing changed; drawn again for the next picker.
    delete (window as any).__hudCrashProbe;
    draw("model", () => closed++);
    expect(host.querySelector("#dialog")).toBeNull();
    draw("voice", () => closed++);
    expect(host.querySelector("#dialog")).not.toBeNull();
  });
});


describe("crash copy recovery", () => {
  let host: HTMLDivElement;
  let root: Root;
  let clipboard: PropertyDescriptor | undefined;
  let copy: ReturnType<typeof vi.fn>;
  let warn: ReturnType<typeof vi.spyOn>;
  let error: ReturnType<typeof vi.spyOn>;
  beforeEach(async () => {
    buffers.reset();
    buffers.configure({
      read: async (_project, path) => ({ path, abs: "/recovery/file.txt", content: "original", kind: "text", protected: false, editable: true, etag: "a".repeat(32) }),
      write: async () => { throw { status: 409, body: { etag: "b".repeat(32) } }; },
      stat: async () => ({ files: {} }), makeModel: async text => new PlainModel(text), now: () => 0,
    });
    await buffers.open("p", "file.txt", "2|file.txt");
    (buffers.model("/recovery/file.txt") as PlainModel).setValue("unsaved\n  exact text\n");
    host = document.createElement("div"); document.body.appendChild(host); root = createRoot(host);
    warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    error = vi.spyOn(console, "error").mockImplementation(() => {});
    clipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
    copy = vi.fn(async () => {});
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: copy } });
    (window as any).__hudCrashProbe = "workspace";
  });
  afterEach(() => {
    act(() => root.unmount()); host.remove(); buffers.reset();
    delete (window as any).__hudCrashProbe;
    if (clipboard) Object.defineProperty(navigator, "clipboard", clipboard); else delete (navigator as any).clipboard;
    warn.mockRestore(); error.mockRestore();
  });
  const button = (name: string) => host.querySelector(`[data-testid="hud-crashed-${name}"]`) as HTMLButtonElement;
  function draw(blocked = false) {
    act(() => root.render(createElement(WorkspaceBoundary, { blocked }, createElement(CrashProbe, { where: "workspace" }))));
  }
  it("copies exact retained text after save409 without clearing dirty state", async () => {
    const getText = vi.spyOn(buffers, "text"); draw();
    expect(getText).not.toHaveBeenCalled(); expect(copy).not.toHaveBeenCalled();
    await act(async () => button("save").click());
    expect(host.textContent).toContain("could not be saved");
    await act(async () => button("copy").click());
    expect(copy).toHaveBeenCalledOnce(); expect(copy).toHaveBeenCalledWith("unsaved\n  exact text\n");
    expect(host.textContent).toContain("Copied file.txt. The edit is still unsaved.");
    expect(buffers.isDirty("/recovery/file.txt")).toBe(true); getText.mockRestore();
  });
  it("never claims copied while pending or after the clipboard rejects", async () => {
    let reject!: (error: Error) => void;
    copy.mockImplementation(() => new Promise<void>((_resolve, no) => { reject = no; })); draw();
    await act(async () => button("copy").click());
    expect(button("copy").disabled).toBe(true); expect(button("save").disabled).toBe(true);
    expect(host.textContent).not.toContain("Copied file.txt");
    await act(async () => reject(new Error("denied")));
    expect(host.textContent).toContain("Copy failed."); expect(host.textContent).not.toContain("Copied file.txt");
    expect(button("copy").disabled).toBe(false); expect(buffers.dirtyCount()).toBe(1);
  });
  it("an approval blocks copying and saving", async () => {
    draw(true); expect(button("copy").disabled).toBe(true); expect(button("save").disabled).toBe(true);
    await act(async () => button("copy").click()); expect(copy).not.toHaveBeenCalled();
    draw(false); await act(async () => button("copy").click()); expect(copy).toHaveBeenCalledOnce();
  });
});
