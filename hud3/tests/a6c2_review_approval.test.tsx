import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApprovalRequest } from "../src/components/approvals/approvalApi";
import { ChatPanel } from "../src/components/chat/ChatPanel";

const chatPath = "/v3/owner/chat";
const listPath = "/v3/owner/approvals";
const answerPath = `${listPath}/answer`;
const vault: ApprovalRequest = {
  id: "sample-first", digest: "a".repeat(64), tool: "vault_add",
  level: "L2", why: "hard:tainted", asked_at: 1_700_000_000, expires_in: 120,
  card: {
    title: "Add a vault entry",
    rows: [
      { label: "Entry", value: "facts/sample-first" },
      { label: "Revision", value: "missing" },
      { label: "Body", value: "Sample body" },
    ],
  },
};
const second: ApprovalRequest = {
  ...vault, id: "sample-second", digest: "b".repeat(64),
  card: {
    ...vault.card,
    rows: [{ label: "Entry", value: "facts/sample-second" }, ...vault.card.rows.slice(1)],
  },
};
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
const fetchMock = vi.fn<(path: string, options: RequestInit) => Promise<Response>>();

let host: HTMLDivElement;
let root: Root;
let list: ApprovalRequest[];
let listUnavailable: boolean;

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.spyOn(document, "hidden", "get").mockReturnValue(false);
  list = [vault];
  listUnavailable = false;
  fetchMock.mockReset().mockImplementation(async (path, options) => {
    switch (path) {
      case chatPath:
        return json({
          state: "running", session_id: "sample", seq: 0, history: [], draft: "", tools: [],
          approvals: list.length,
        });
      case `${chatPath}/events`:
        return json({ state: "running", seq: 0, gap: false, events: [], approvals: list.length });
      case listPath:
        return listUnavailable ? json({ error: "Temporarily unavailable" }, 503) : json({ approvals: list });
      case answerPath: {
        const { id, decision } = JSON.parse(options.body as string);
        return json({ id, state: decision === "approve" ? "approved" : "denied" });
      }
      default:
        throw new Error(`Unexpected fetch: ${path}`);
    }
  });
  vi.stubGlobal("fetch", fetchMock);
  host = document.body.appendChild(document.createElement("div"));
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const mount = () => act(async () => root.render(<ChatPanel />));
const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
const requests = (path: string) => fetchMock.mock.calls.filter(([url]) => url === path);
const approve = () => [...host.querySelectorAll("button")].find((button) => button.textContent === "Approve");
const target = () => host.querySelector(".approval-target dd")?.textContent;

describe("A6c-2 review", () => {
  it.each(["first appearance", "a new digest", "list reordering"])(
    "HIGH: ignores a pointer press started during arming after %s",
    async (change) => {
      if (change === "list reordering") list = [vault, second];
      await mount();
      if (change !== "first appearance") {
        await advance(600);
        expect(approve()?.disabled).toBe(false);
        list = change === "a new digest" ? [{ ...vault, digest: "c".repeat(64) }] : [second, vault];
        await advance(400);
      }
      expect(target()).toBe(change === "list reordering" ? "facts/sample-second" : "facts/sample-first");
      const button = approve()!;
      expect(button.disabled).toBe(true);
      expect(button.tabIndex).toBe(-1);

      // Chromium delivers pointerdown on a disabled button and suppresses mousedown.
      const Pointer = window.PointerEvent ?? MouseEvent;
      const pointer = { bubbles: true, cancelable: true, pointerId: 1, pointerType: "mouse", isPrimary: true };
      await act(async () => {
        button.dispatchEvent(new Pointer("pointerdown", { ...pointer, buttons: 1 }));
      });
      await advance(600);
      const armedButton = approve()!;
      expect(armedButton.disabled).toBe(false);
      // If the button arms during the hold, Chromium delivers an ordinary click on release.
      await act(async () => {
        armedButton.dispatchEvent(new Pointer("pointerup", pointer));
        armedButton.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, detail: 1 }));
        button.dispatchEvent(new Pointer("click", { ...pointer, detail: 1 }));
      });
      expect(requests(answerPath)).toHaveLength(0);
    },
  );

  it.each([
    { label: "missing", rows: [{ label: "Body", value: "Sample body" }] },
    { label: "blank after rendering", rows: [{ label: "Entry", value: " \n\u202e\t" }] },
    { label: "conflicting", rows: [{ label: "Entry", value: "facts/one" }, { label: "Entry", value: "facts/two" }] },
  ])("MED: refuses approval when the target is $label", async ({ rows }) => {
    list = [{ ...vault, card: { ...vault.card, rows } }];
    await mount();
    await advance(600);
    await act(async () => { approve()?.click(); });
    expect(requests(answerPath)).toHaveLength(0);
    expect(host.querySelector("textarea")?.disabled).toBe(false);
  });

  it("MED: distinguishes valid event IDs with the same long prefix in the target line", async () => {
    const prefix = "testevent".repeat(14);
    const event = (id: string, digest: string, eventId: string): ApprovalRequest => ({
      ...vault, id, digest, tool: "calendar_delete",
      card: {
        title: "Delete a calendar event",
        rows: [
          { label: "Event", value: eventId },
          { label: "Revision", value: '"sample-revision"' },
          { label: "Current title", value: "Sample event" },
        ],
      },
    });
    const first = event("sample-first", "a".repeat(64), prefix + "1");
    const next = event("sample-second", "b".repeat(64), prefix + "2");
    list = [first, next];
    await mount();
    const firstTarget = target();
    expect(firstTarget).toBeTruthy();
    list = [next];
    await advance(1_000);
    expect(target()).toBeTruthy();
    expect(target()).not.toBe(firstTarget);
  });

  it("LOW: backs off after repeated list errors while there is no card to refresh", async () => {
    listUnavailable = true;
    await mount();
    expect(requests(listPath)).toHaveLength(1);
    expect(host.textContent).toContain("Could not refresh approvals");
    await advance(12_000);
    expect(requests(listPath).length).toBeGreaterThan(1);
    expect(requests(listPath).length).toBeLessThanOrEqual(7);
  });
});
