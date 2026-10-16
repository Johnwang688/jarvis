import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { RemoteView, type RemoteViewProps } from "./RemoteView";
import { driving } from "../state/driving";
const mocks = vi.hoisted(() => ({ write: vi.fn(), draw: vi.fn(), decode: vi.fn() }));
vi.mock("../api", () => ({ browserApi: { write: mocks.write }, api: {} }));
vi.mock("./BrowserFiles", () => ({ BrowserFiles: () => null }));
(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
class Socket {
  static OPEN = 1; static instances: Socket[] = []; readyState = 0; binaryType = "";
  onopen: (() => void) | null = null; onclose: (() => void) | null = null; onerror: (() => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  sent: any[] = []; closed = false;
  constructor(_url: string) { Socket.instances.push(this); }
  send(text: string) { this.sent.push(JSON.parse(text)); }
  close() { this.closed = true; this.readyState = 3; }
  open() { this.readyState = 1; this.onopen?.(); }
  message(value: unknown) { this.onmessage?.({ data: value instanceof ArrayBuffer ? value : JSON.stringify(value) }); }
}
function binary(id: number) { const b = new ArrayBuffer(10); const h = new DataView(b); h.setUint32(0, id); h.setUint16(4, 320); h.setUint16(6, 200); return b; }
let host: HTMLDivElement; let root: Root; let props: RemoteViewProps;
const render = async (change: Partial<RemoteViewProps> = {}) => { props = { ...props, ...change }; await act(async () => root.render(createElement(RemoteView, props))); };
const socket = () => Socket.instances.at(-1)!;
const proxy = () => host.querySelector<HTMLTextAreaElement>('[data-testid="remote-input"]')!;
const surface = () => host.querySelector<HTMLElement>('[data-testid="remote-surface"]')!;
const overlay = () => host.querySelector<HTMLElement>('[data-testid="remote-picker"]');
const message = async (value: unknown) => { await act(async () => socket().message(value)); };
const key = async (k: string, extra: KeyboardEventInit = {}) => {
  await act(async () => { proxy().dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, key: k, code: k, ...extra })); });
};
const mouse = async (type: string, x = 50, y = 50, extra: MouseEventInit = {}) => {
  await act(async () => { surface().dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, button: 0, buttons: type === "mousedown" ? 1 : 0, clientX: x, clientY: y, ...extra })); });
};
const click = async (x = 50, y = 50) => { await mouse("mousedown", x, y); await mouse("mouseup", x, y); };
const SELECT = {
  t: "picker", id: "0123abcd", kind: "select", rect: { x: 10, y: 20, width: 120, height: 24 },
  items: [{ i: 0, label: "Alpha <img src=x onerror=alert(1)>", disabled: false, g: -1 },
          { i: 1, label: "Bravo", disabled: true, g: 0 }, { i: 2, label: "Charlie", disabled: false, g: 0 }],
  groups: ["Group one"], selected: 0, total: 3, shown: 3,
};
const DATE = { t: "picker", id: "4567cdef", kind: "date", rect: { x: 10, y: 20, width: 120, height: 24 },
  value: "2026-10-16", min: "", max: "", step: "", required: false };

beforeEach(async () => {
  driving.retain([]); Socket.instances = []; vi.stubGlobal("WebSocket", Socket);
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  vi.stubGlobal("createImageBitmap", mocks.decode);
  mocks.write.mockReset().mockResolvedValue({ ticket: "one-use" });
  mocks.draw.mockReset(); mocks.decode.mockReset().mockResolvedValue({ close: vi.fn() });
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ clearRect: vi.fn(), drawImage: mocks.draw } as any);
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(320);
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(200);
  host = document.createElement("div"); document.body.appendChild(host); root = createRoot(host);
  props = { tabId: "0123abcd", live: true, blocked: false, zoom: 100, projectId: null, onLocation: vi.fn(), onPopup: vi.fn(), onAction: vi.fn(), onOwnerFocus: vi.fn() };
  await render(); await act(async () => socket().open());
  const box = () => ({ left: 0, top: 0, width: 320, height: 200, right: 320, bottom: 200, x: 0, y: 0, toJSON() {} }) as DOMRect;
  surface().getBoundingClientRect = box;
  host.querySelector("canvas")!.getBoundingClientRect = box;
  await message(binary(1));
  await act(async () => { await Promise.resolve(); });
  socket().sent = [];
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); driving.retain([]); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("shows nothing the owner did not ask for, and never moves the keyboard", async () => {
  const chat = document.createElement("textarea"); document.body.appendChild(chat); chat.focus();
  await message(SELECT);
  expect(overlay()).toBeNull();
  expect(socket().sent).toContainEqual({ t: "pick_cancel", id: SELECT.id });
  expect(document.activeElement).toBe(chat);
  chat.remove();
});

it("answers the owner's own press: shown under the control, keyboard left in the proxy", async () => {
  await click();
  expect(socket().sent.filter(m => m.t === "mouse").map(m => m.e.type)).toEqual(["mousePressed", "mouseReleased"]);
  expect(document.activeElement).toBe(proxy());
  await message(SELECT);
  const box = overlay()!;
  expect(box).not.toBeNull();
  expect(document.activeElement).toBe(proxy());
  expect(box.style.left).toBe("10px");
  expect(box.style.top).toBe("44px");
  expect(box.querySelector("img")).toBeNull();
  expect(box.textContent).toContain("Alpha <img src=x onerror=alert(1)>");
  expect([...box.querySelectorAll('[role="option"]')].map(o => o.getAttribute("aria-disabled"))).toEqual(["false", "true", "false"]);
  expect(box.textContent).toContain("Group one");
  // A second picker for the same press is not shown.
  await message({ ...SELECT, id: "89abcdef" });
  expect(overlay()!.querySelector('[data-index="0"]')!.id).toContain("0123abcd");
});

it("routes the owner's keys to the picker, skipping disabled options, and never to the page", async () => {
  await click(); await message(SELECT); socket().sent = [];
  await key("ArrowDown");
  expect(overlay()!.querySelector('[aria-selected="true"]')!.getAttribute("data-index")).toBe("2");
  await key("ArrowUp"); await key("End"); await key("Home"); await key("c");
  expect(overlay()!.querySelector('[aria-selected="true"]')!.getAttribute("data-index")).toBe("2");
  await key("Enter");
  expect(socket().sent).toEqual([{ t: "pick", id: SELECT.id, index: 2 }]);
  expect(overlay()).toBeNull();
  expect(document.activeElement).toBe(proxy());
});

it("Escape cancels; a click on an enabled option picks it, a disabled one does nothing", async () => {
  await click(); await message(SELECT); socket().sent = [];
  await key("Escape");
  expect(socket().sent).toEqual([{ t: "pick_cancel", id: SELECT.id }]);
  expect(overlay()).toBeNull();
  await click(); await message({ ...SELECT, id: "11112222" }); socket().sent = [];
  await act(async () => overlay()!.querySelector<HTMLElement>('[data-index="1"]')!.click());
  expect(socket().sent).toEqual([]);
  await act(async () => overlay()!.querySelector<HTMLElement>('[data-index="2"]')!.click());
  expect(socket().sent).toEqual([{ t: "pick", id: "11112222", index: 2 }]);
});

it("a press or a wheel on the page closes it and is consumed whole", async () => {
  await click(); await message(SELECT); socket().sent = [];
  await click(200, 150);
  expect(overlay()).toBeNull();
  expect(socket().sent).toEqual([{ t: "pick_cancel", id: SELECT.id }]);
  await act(async () => surface().dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true, button: 2, clientX: 200, clientY: 150 })));
  expect(host.querySelector('[data-testid="remote-context-menu"]')).toBeNull();
  await click(); await message({ ...SELECT, id: "33334444" }); socket().sent = [];
  await act(async () => surface().dispatchEvent(new WheelEvent("wheel", { bubbles: true, cancelable: true, deltaY: 100, clientX: 60, clientY: 60 })));
  expect(overlay()).toBeNull();
  expect(socket().sent).toEqual([{ t: "pick_cancel", id: "33334444" }]);
});

it("is never drawn under a card or in a pane that is not live, and a card closes it", async () => {
  await click(); await message(SELECT);
  await render({ blocked: true });
  expect(overlay()).toBeNull();
  expect(socket().sent).toContainEqual({ t: "pick_cancel", id: SELECT.id });
  await render({ blocked: false });
  expect(overlay()).toBeNull();
  // A picker arriving under a card is refused, whatever the gesture.
  await click();
  await render({ blocked: true });
  await message({ ...SELECT, id: "55556666" });
  expect(overlay()).toBeNull();
  await render({ blocked: false });
  await message({ ...SELECT, id: "55556666" });
  expect(overlay()).toBeNull();
  // A press made before a card does not count after it.
  await click();
  await render({ blocked: true });
  await render({ blocked: false });
  await message({ ...SELECT, id: "66667777" });
  expect(overlay()).toBeNull();
  await click(); await message({ ...SELECT, id: "77778888" });
  expect(overlay()).not.toBeNull();
  await render({ live: false });
  expect(overlay()).toBeNull();
});

it("a dialog, an auth prompt, an owner reset or a daemon close ends it", async () => {
  await click(); await message(SELECT);
  await message({ t: "dialog", id: "1234abcd", kind: "alert", message: "hi", default: "" });
  expect(overlay()).toBeNull();
  await message({ t: "dialog_closed", id: "1234abcd" });
  await click(); await message({ ...SELECT, id: "aaaabbbb" });
  await message({ t: "owner_reset" });
  expect(overlay()).toBeNull();
  await click(); await message({ ...SELECT, id: "ccccdddd" });
  await message({ t: "picker_closed", id: "ccccdddd", reason: "changed" });
  expect(overlay()).toBeNull();
  expect(host.querySelector('[data-testid="remote-error"]')).toBeNull();
});

it("date: the HUD's own input is not focused for the owner; Set, Clear and a refused apply", async () => {
  await click(); await message(DATE); socket().sent = [];
  const field = overlay()!.querySelector<HTMLInputElement>('[data-testid="remote-picker-input"]')!;
  expect(field.type).toBe("date");
  expect(field.value).toBe("2026-10-16");
  expect(document.activeElement).toBe(proxy());
  await act(async () => { const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    set.call(field, "2026-11-01"); field.dispatchEvent(new Event("input", { bubbles: true })); });
  await act(async () => overlay()!.querySelector<HTMLButtonElement>('[data-testid="remote-picker-set"]')!.click());
  expect(socket().sent).toEqual([{ t: "pick", id: DATE.id, value: "2026-11-01" }]);
  await message({ t: "picker_closed", id: DATE.id, reason: "refused" });
  expect(host.querySelector('[data-testid="remote-error"]')!.textContent).toContain("nothing was set");
  await click(); await message({ ...DATE, id: "eeeeffff" }); socket().sent = [];
  await act(async () => overlay()!.querySelector<HTMLButtonElement>('[data-testid="remote-picker-clear"]')!.click());
  expect(socket().sent).toEqual([{ t: "pick", id: "eeeeffff", value: "" }]);
  await click(); await message({ ...DATE, id: "12121212", required: true });
  expect(overlay()!.querySelector('[data-testid="remote-picker-clear"]')).toBeNull();
  socket().sent = [];
  await key("Enter");
  expect(socket().sent).toEqual([{ t: "pick", id: "12121212", value: "2026-10-16" }]);
});

it("colour: Set sends the HUD input's value", async () => {
  await click(); await message({ t: "picker", id: "abababab", kind: "color", rect: DATE.rect, value: "#336699" }); socket().sent = [];
  expect(overlay()!.querySelector<HTMLInputElement>('[data-testid="remote-picker-input"]')!.type).toBe("color");
  expect(overlay()!.querySelector('[data-testid="remote-picker-clear"]')).toBeNull();
  await act(async () => overlay()!.querySelector<HTMLButtonElement>('[data-testid="remote-picker-set"]')!.click());
  expect(socket().sent).toEqual([{ t: "pick", id: "abababab", value: "#336699" }]);
});

it("a picker arriving behind a dialog or a file chooser is refused, and never shown once they close", async () => {
  await message({ t: "dialog", id: "1234abcd", kind: "alert", message: "hi", default: "" });
  await click();
  await message({ ...SELECT, id: "0a0a0a0a" });
  expect(overlay()).toBeNull();
  expect(socket().sent).toContainEqual({ t: "pick_cancel", id: "0a0a0a0a" });
  await message({ t: "dialog_closed", id: "1234abcd" });
  expect(overlay()).toBeNull();
  // The same within one task: the dialog and the picker before React renders.
  await click();
  await act(async () => {
    socket().message({ t: "dialog", id: "5678abcd", kind: "alert", message: "hi", default: "" });
    socket().message({ ...SELECT, id: "0b0b0b0b" });
  });
  await message({ t: "dialog_closed", id: "5678abcd" });
  expect(overlay()).toBeNull();
  await click();
  await act(async () => {
    socket().message({ t: "filechooser", id: "9999aaaa", multiple: false });
    socket().message({ ...SELECT, id: "0c0c0c0c" });
  });
  expect(overlay()).toBeNull();
  await message({ t: "filechooser_closed", id: "9999aaaa", files: 0 });
  expect(overlay()).toBeNull();
  expect(socket().sent.filter(m => m.t === "pick_cancel").map(m => m.id)).toEqual(expect.arrayContaining(["0b0b0b0b", "0c0c0c0c"]));
  // Once clear, the owner's next press works again.
  await click(); await message({ ...SELECT, id: "0d0d0d0d" });
  expect(overlay()).not.toBeNull();
});

it("an IME composition committed while the picker is open never reaches the page or the picker", async () => {
  await click(); await message(SELECT); socket().sent = [];
  const before = overlay()!.querySelector('[aria-selected="true"]')!.getAttribute("data-index");
  await act(async () => {
    proxy().dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true }));
    proxy().dispatchEvent(new CompositionEvent("compositionupdate", { bubbles: true, data: "c" }));
  });
  // IME keydowns, each its own task: keyCode 229 with a letter key, composing or not.
  await act(async () => { proxy().dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, key: "c", code: "KeyC", keyCode: 229, isComposing: true } as KeyboardEventInit)); });
  await act(async () => { proxy().dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, key: "c", code: "KeyC", keyCode: 229 } as KeyboardEventInit)); });
  await act(async () => {
    proxy().dispatchEvent(new CompositionEvent("compositionend", { bubbles: true, data: "漢字" }));
    proxy().dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: "字" }));
  });
  await act(async () => { await new Promise(r => setTimeout(r, 80)); });
  expect(before).toBe("0");
  expect(overlay()!.querySelector('[aria-selected="true"]')!.getAttribute("data-index")).toBe("0");
  await key("ArrowDown");
  expect(socket().sent.filter(m => m.t === "text" || m.t === "key")).toEqual([]);
  expect(overlay()!.querySelector('[aria-selected="true"]')!.getAttribute("data-index")).toBe("2");
});

it("a composition the picker interrupted never commits after the picker closes", async () => {
  await act(async () => { proxy().focus(); proxy().dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true })); });
  await click(); await message(SELECT);
  await message({ t: "picker_closed", id: SELECT.id, reason: "changed" });
  expect(overlay()).toBeNull();
  socket().sent = [];
  await act(async () => { proxy().dispatchEvent(new CompositionEvent("compositionend", { bubbles: true, data: "ni" })); });
  await act(async () => { await new Promise(r => setTimeout(r, 80)); });
  expect(socket().sent.filter(m => m.t === "text")).toEqual([]);
});

it("a composition begun before the picker opened is cancelled by it", async () => {
  await act(async () => { proxy().focus(); proxy().dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true })); });
  await click(); await message(SELECT); socket().sent = [];
  await act(async () => { proxy().dispatchEvent(new CompositionEvent("compositionend", { bubbles: true, data: "ni" })); });
  await act(async () => { await new Promise(r => setTimeout(r, 80)); });
  expect(socket().sent.filter(m => m.t === "text")).toEqual([]);
});
