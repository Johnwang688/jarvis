// @vitest-environment jsdom
// keepKeyboard / keyboardPane (PR #358 review): a chat's batch of panes must
// not drop the owner's keyboard, must never focus a Browser frame or act
// under a card, and must leave a focus move the owner made alone, including
// a later fall to <body> after it (Sol re-verify).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { keepKeyboard, keyboardPane } from "./Layout";

let frames: FrameRequestCallback[];
const frame = () => { const f = frames; frames = []; f.forEach((cb) => cb(0)); };

beforeEach(() => {
  frames = [];
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => { frames.push(cb); return frames.length; });
  document.body.innerHTML =
    '<section data-testid="pane-2" tabindex="-1"><div data-testid="pane-2-view"><button data-testid="tab-chat">Chat</button></div>'
    + '<textarea data-testid="input"></textarea></section><button id="elsewhere">Sidebar</button>';
  Object.defineProperty(document.querySelector("section"), "offsetParent", { get: () => document.body });
});
afterEach(() => { vi.unstubAllGlobals(); document.body.innerHTML = ""; });

const button = () => document.querySelector<HTMLButtonElement>("section button")!;
const elsewhere = () => document.querySelector<HTMLButtonElement>("#elsewhere")!;
const input = () => document.querySelector<HTMLTextAreaElement>("textarea")!;

describe("keepKeyboard", () => {
  it("finds the pane section through a pane-prefixed control wrapper", () => {
    button().focus();
    expect(keyboardPane()).toBe(2);
  });
  it("puts the keyboard back in the pane when its focused control unmounts", () => {
    button().focus(); keepKeyboard(2, () => false); button().remove(); frame();
    expect(document.activeElement).toBe(input());
  });
  it("leaves a focused composer and its text alone", () => {
    input().value = "still typing"; input().focus(); keepKeyboard(2, () => false);
    for (let n = 0; n < 6; n++) frame();
    expect(document.activeElement).toBe(input());
    expect(input().value).toBe("still typing");
  });
  it("never focuses a Browser frame", () => {
    input().remove(); document.querySelector("section")!.append(document.createElement("iframe"));
    button().focus(); keepKeyboard(2, () => false); button().remove(); frame();
    expect(document.activeElement).toBe(document.querySelector("section"));
  });
  it("does nothing under a card, nor after it closes", () => {
    let blocked = false; button().focus(); keepKeyboard(2, () => blocked); button().remove();
    blocked = true; frame(); blocked = false; frame();
    expect(document.activeElement).toBe(document.body);
  });
  it("leaves a focus move to another control alone", () => {
    button().focus(); keepKeyboard(2, () => false); button().remove(); elsewhere().focus();
    for (let n = 0; n < 6; n++) frame();
    expect(document.activeElement).toBe(elsewhere());
  });
  it("stops watching once focus was seen outside the pane, so a later blur is the owner's", () => {
    button().focus(); keepKeyboard(2, () => false);
    elsewhere().focus(); frame();
    elsewhere().blur(); frame(); frame();
    expect(document.activeElement).toBe(document.body);
  });
});
