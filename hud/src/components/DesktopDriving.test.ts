import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { api } from "../api";
import type { ApprovalRequest, Thread } from "../types";
import { ApprovalVeil } from "./Approvals";
import { DesktopDriving } from "./DesktopDriving";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const chat = { id: "chat/1", role: "chat", provider: "claude", task_id: null } as Thread;
let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
});

const render = (props: Partial<Parameters<typeof DesktopDriving>[0]> = {}) => act(async () => {
  root.render(createElement(DesktopDriving, {
    state: { enabled: true, thread_id: null, badge_showing: false }, thread: chat, busy: false, canCreate: true,
    onToggle: () => {}, onStop: () => {}, onCreate: () => {}, ...props,
  }));
});

it("shows the toggle only for eligible native chats with master enabled", async () => {
  await render({ state: { enabled: false, thread_id: chat.id, badge_showing: false } });
  expect(host.textContent).toBe("");
  for (const thread of [null, { ...chat, provider: "fast" }, { ...chat, task_id: "task" } ] as const) {
    await render({ thread });
    expect(host.querySelector("input")).toBeNull();
    expect(host.textContent).toBe("New desktop driver chat");
  }
  for (const provider of ["codex", "claude"] as const) {
    await render({ thread: { ...chat, provider } });
    expect(host.querySelector("label")?.textContent?.trim()).toBe("Drive desktop");
  }
});

it("uses the daemon state for the checkbox and sends toggle, Stop and create separately", async () => {
  const calls: [string, RequestInit | undefined][] = [];
  vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
    calls.push([url, init]);
    return new Response(JSON.stringify({ enabled: true, thread_id: chat.id, badge_showing: true }), { status: 200 });
  });
  const props = {
    onToggle: (id: string, on: boolean) => void api.setDesktopDriving(id, on),
    onStop: (id: string) => void api.stopDesktopDriving(id),
    onCreate: () => void api.newDesktopDriverChat("project"),
  };
  await render(props);
  const toggle = host.querySelector("input")!;
  expect(toggle.checked).toBe(false);
  expect(host.querySelector("[role=status]")).toBeNull();
  await act(async () => toggle.click());
  expect(toggle.checked).toBe(false);
  await render({ ...props, state: { enabled: true, thread_id: chat.id, badge_showing: true } });
  expect(toggle.checked).toBe(true);
  expect(host.querySelector("[role=status]")?.textContent).toBe("Driving the desktop");
  await act(async () => toggle.click());
  await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="Stop desktop driving"]')!.click());
  await act(async () => Array.from(host.querySelectorAll("button")).find(b => b.textContent === "New desktop driver chat")!.click());
  expect(calls.map(([url, init]) => [url, init?.method, JSON.parse(String(init?.body))])).toEqual([
    ["/threads/chat%2F1/desktop-driving", "PATCH", { on: true }],
    ["/threads/chat%2F1/desktop-driving", "PATCH", { on: false }],
    ["/threads/chat%2F1/desktop-driving/stop", "POST", {}],
    ["/threads", "POST", { project_id: "project", role: "chat", provider: "codex",
      brief: { model: "gpt-6.2-sol", effort: "medium", speed: "ultrafast" } }],
  ]);
});

it("shows the unavailable badge and Esc message only while driving without the badge", async () => {
  const stop = vi.fn();
  const warning = "Stop badge and Esc unavailable: the voice helper is not connected";
  await render({ onStop: stop });
  expect(host.textContent).not.toContain(warning);
  await render({ onStop: stop, state: { enabled: true, thread_id: chat.id, badge_showing: false } });
  expect(host.querySelector("[role=status]")?.textContent).toContain(warning);
  await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="Stop desktop driving"]')!.click());
  expect(stop).toHaveBeenCalledExactlyOnceWith(chat.id);
  await render({ state: { enabled: true, thread_id: chat.id, badge_showing: true } });
  expect(host.querySelector("[role=status]")?.textContent).toBe("Driving the desktop");
});

it("keeps Stop reachable on the driver's desktop approval card", async () => {
  const stop = vi.fn();
  const decide = vi.fn();
  const request = { req_id: "fixture", thread_id: chat.id, layer: "desktop-gate", tool: "hud_window_click",
    reason: "Approve", timeout_s: 120, allowlistable: false } as ApprovalRequest;
  const card = (req: ApprovalRequest) => act(async () => root.render(createElement(ApprovalVeil, {
    requests: [req], onDecide: decide, desktopDriver: chat.id, onStopDesktopDriving: stop,
  })));
  await card(request);
  const button = () => Array.from(host.querySelectorAll("button")).find(b => b.textContent === "Stop desktop driving");
  await act(async () => button()!.click());
  expect(stop).toHaveBeenCalledExactlyOnceWith(chat.id);
  expect(decide).not.toHaveBeenCalled();
  await card({ ...request, thread_id: "another" });
  expect(button()).toBeUndefined();
});
