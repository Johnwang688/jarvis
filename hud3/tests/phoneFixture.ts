import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, vi } from "vitest";
import { setSurface } from "../src/api";
import type { Brief } from "../src/components/feed/feedApi";
import { PhoneApp } from "../src/phone/PhoneApp";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
setSurface("phone");

export const challenge = "AQID";
export const approval = {
  id: "approval-fixture", digest: "fake-digest", level: "ask", why: "Fixture policy",
  tool: "calendar_create", asked_at: 123, expires_in: 120,
  card: { title: "Create a calendar event", rows: [{ label: "Title", value: "Fixture event" }] },
};
export const brief: Brief = {
  id: "brief-fixture", kind: "decision", rank: 1, due: null, ask: "Approve the fixture?",
  why_now: "Fixture context", recommend: null, options: [], default: { action: "none", at: null },
  context: [], feedback: [], state: "open",
};
export const chat = {
  state: "running", seq: 1, session_id: "fixture-session", approvals: 1, draft: "", history: [], tools: [],
};

export const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const assertion = {
  id: "AQID", rawId: new Uint8Array([1, 2, 3]).buffer, type: "public-key",
  getClientExtensionResults: () => ({}),
  response: {
    clientDataJSON: new Uint8Array([4]).buffer,
    authenticatorData: new Uint8Array([5]).buffer,
    signature: new Uint8Array([6]).buffer,
  },
};
const attestation = { ...assertion, response: {
  clientDataJSON: new Uint8Array([4]).buffer, attestationObject: new Uint8Array([5]).buffer,
} };

type Reply = (path: string, init: RequestInit) => Response | undefined;
export function phoneFixture() {
  let box: HTMLDivElement;
  let root: Root;
  let options: Reply | undefined;
  let unlockSeconds: number;
  const get = vi.fn<CredentialsContainer["get"]>();
  const create = vi.fn<CredentialsContainer["create"]>();
  const fetch = vi.fn(async (path: string, init: RequestInit) => {
    const override = options?.(path, init);
    if (override) return override;
    switch (path) {
      case "/m/api/me": return json({ signed_in: true, unlock_s: unlockSeconds });
      case "/m/api/status": return json({ service: "jarvis-agentd", version: "v3", pid: 10, uptime_s: 2, jobs: {} });
      case "/m/api/feed": return json({ briefs: [brief], recent: [] });
      case "/m/api/feed/answer": {
        const { action } = JSON.parse(String(init.body));
        return json({ brief: { ...brief, state: { approve: "approved", edit: "edited", not_now: "dismissed", other: "other" }[String(action)] } });
      }
      case "/m/api/chat": return json(chat);
      case "/m/api/chat/events":
        return json({ ...chat, gap: false, events: [] });
      case "/m/api/approvals": return json({ approvals: [approval], session_id: chat.session_id });
      case "/m/api/approvals/answer":
        return json({ id: approval.id, state: JSON.parse(String(init.body)).decision === "deny" ? "denied" : "approved" });
      case "/m/api/work": return json({ projects: [], threads: [] });
      case "/m/api/unlock/options": return json({ publicKey: { challenge } });
      case "/m/api/unlock":
        unlockSeconds = 3600;
        return json({ unlocked: true, unlock_s: unlockSeconds });
      case "/m/api/logout":
        return json({ signed_in: false });
      default: throw new Error(`Unexpected request ${path}`);
    }
  });

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date", "performance"] });
    options = undefined;
    unlockSeconds = 0;
    fetch.mockClear();
    get.mockReset().mockResolvedValue(assertion);
    create.mockReset().mockResolvedValue(attestation);
    vi.stubGlobal("fetch", fetch);
    Object.defineProperty(navigator, "credentials", { configurable: true, value: { get, create } });
    history.replaceState(null, "", "/m/");
    box = document.createElement("div");
    document.body.append(box);
    root = createRoot(box);
  });
  afterEach(() => {
    act(() => root.unmount());
    box.remove();
    Reflect.deleteProperty(navigator, "credentials");
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  function button(label: string, within = box) {
    const result = [...within.querySelectorAll("button")]
      .find((item) => item.textContent === label && !item.closest("[hidden]"));
    expect(result, label).toBeDefined();
    return result!;
  }

  return {
    get box() { return box; },
    fetch, get, create, button,
    reply: (reply: Reply) => { options = reply; },
    unlocked: (seconds: number) => { unlockSeconds = seconds; },
    requests: (path: string) => fetch.mock.calls.filter(([url]) => url === path).map(([, init]) => init),
    mount: async (pairingCode?: string) => {
      await act(async () => root.render(createElement(PhoneApp, { pairingCode })));
    },
    click: (label: string, within?: HTMLDivElement) => act(async () => button(label, within).click()),
    selectTab: (name: string) => act(async () => box.querySelector<HTMLButtonElement>(`#tab-${name.toLowerCase()}`)!.click()),
    advance: (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); }),
  };
}

export function fill(el: HTMLTextAreaElement, text: string) {
  return act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(el, text);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
