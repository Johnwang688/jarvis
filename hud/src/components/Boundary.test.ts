// A render error inside the workspace boundary shows a Reload prompt in its
// place and goes no further: a sibling outside it (in the app, the approval
// card) stays mounted. The boundary logs the error's name, never its message.

import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { CrashProbe, WorkspaceBoundary, errorName } from "./Boundary";

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

  function draw() {
    act(() =>
      root.render(
        createElement(
          "div",
          null,
          createElement(
            WorkspaceBoundary,
            null,
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
    expect(Array.from(fallback!.querySelectorAll("*")).map((el) => el.tagName)).toEqual(["P", "P", "BUTTON"]);
    expect(host.querySelector("#card")?.textContent).toBe("the card");
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
