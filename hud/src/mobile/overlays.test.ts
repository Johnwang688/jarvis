import { afterEach, describe, expect, it, vi } from "vitest";
import { openOverlay, refreshOverlays } from "./overlays";
import { Drawer, type DrawerState } from "./drawer";
import { createProjectFolds } from "./folds";
import { signOutSheet } from "./nav";

const cleanup: (() => void)[] = [];
afterEach(() => { cleanup.splice(0).reverse().forEach((close) => close()); document.body.replaceChildren(); });
const key = (key: string, shiftKey = false) => document.dispatchEvent(
  new KeyboardEvent("keydown", { key, shiftKey, bubbles: true, cancelable: true }));

function fixture() {
  const host = document.createElement("div"), background = document.createElement("main"), opener = document.createElement("button");
  background.append(opener); host.append(background); document.body.replaceChildren(host);
  const state: DrawerState = { section: "inbox", connection: { state: "live", tone: "ok", word: "Live" },
    lastRead: "10:00", projects: [], threads: [], needs: new Set(), running: new Set(), count: 0, review: 0,
    unlock: "Locked", unlocked: false, lockNote: "" };
  const drawer = new Drawer(host, opener, createProjectFolds(null), () => state, { section() {}, thread() {}, lock() {} });
  cleanup.push(() => drawer.stop());
  const q = (id: string) => host.querySelector<HTMLElement>(`[data-testid="${id}"]`)!;
  return { host, background, opener, drawer, q };
}

describe("shared phone overlay stack", () => {
  it("Escape closes only the top action sheet, restores drawer focus, then returns to the brand", () => {
    const h = fixture(); h.drawer.open();
    const row = h.q("m-drawer-inbox"), confirmed = vi.fn(), closed = vi.fn();
    const action = signOutSheet(h.host, confirmed, closed, row); cleanup.push(action.close);
    expect(h.q("m-drawer").closest("[inert]")).not.toBeNull();
    h.opener.focus(); expect(document.activeElement).toBe(h.q("m-signout-sheet"));
    key("Tab", true); expect(document.activeElement).toBe(h.q("m-signout-cancel"));
    key("Escape");
    expect(closed).toHaveBeenCalledOnce(); expect(confirmed).not.toHaveBeenCalled();
    expect(h.drawer.isOpen).toBe(true); expect(h.background.hasAttribute("inert")).toBe(true);
    expect(document.activeElement).toBe(row);
    key("Escape"); expect(h.drawer.isOpen).toBe(false);
    expect(h.background.hasAttribute("inert")).toBe(false); expect(document.activeElement).toBe(h.opener);
  });

  it("closing the lower drawer keeps the action sheet modal and passes its focus return to the brand", () => {
    const h = fixture(); h.drawer.open();
    const action = signOutSheet(h.host, vi.fn(), vi.fn(), h.q("m-drawer-inbox")); cleanup.push(action.close);
    const sheet = h.q("m-signout-sheet");
    h.drawer.close();
    expect(document.activeElement).toBe(sheet); expect(h.background.hasAttribute("inert")).toBe(true);
    expect(sheet.closest("[inert]")).toBeNull();
    h.background.removeAttribute("inert"); refreshOverlays(h.host);
    expect(h.background.hasAttribute("inert")).toBe(true);
    action.close(); action.close();
    expect(document.activeElement).toBe(h.opener); expect(h.background.hasAttribute("inert")).toBe(false);
  });

  it("holds three layers through a middle removal, preserves prior inert and releases listeners", () => {
    const h = fixture(); h.background.setAttribute("inert", ""); h.drawer.open();
    const middle = document.createElement("section"), middleFocus = document.createElement("button");
    middle.append(middleFocus); h.host.append(middle);
    const modal = openOverlay(h.host, middle, { focus: () => middleFocus, restore: () => h.q("m-drawer-inbox"), dismiss: vi.fn() });
    cleanup.push(modal.close); middleFocus.focus();
    const action = signOutSheet(h.host, vi.fn(), vi.fn(), middleFocus); cleanup.push(action.close);
    middle.remove(); modal.close();
    expect(h.background.hasAttribute("inert")).toBe(true);
    expect(document.activeElement).toBe(h.q("m-signout-sheet"));
    key("Escape"); expect(document.activeElement).toBe(h.q("m-drawer-inbox"));
    h.drawer.close(); expect(h.background.hasAttribute("inert")).toBe(true);
    h.background.removeAttribute("inert"); h.opener.focus(); expect(document.activeElement).toBe(h.opener);
    expect(refreshOverlays(h.host)).toBe(false);
  });
});
