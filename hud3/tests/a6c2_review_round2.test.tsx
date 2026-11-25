import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ApprovalPanel } from "../src/components/approvals/ApprovalPanel";
import type { ApprovalRequest } from "../src/components/approvals/approvalApi";

const listPath = "/v3/owner/approvals";
const answerPath = `${listPath}/answer`;
const create: ApprovalRequest = {
  id: "sample-approval", digest: "a".repeat(64), tool: "calendar_create",
  level: "L2", why: "hard:tainted", asked_at: 1_700_000_000, expires_in: 120,
  card: {
    title: "Create a calendar event",
    rows: [
      { label: "Title", value: "Sample planning" },
      { label: "Start", value: "2030-01-01T06:00:00-06:00" },
      { label: "End", value: "2030-01-01T07:00:00-06:00" },
      { label: "Description", value: "" },
      { label: "Reminders", value: "Calendar defaults" },
      { label: "Delivery", value: "No attendees, no emails sent" },
    ],
  },
};
const fetchMock = vi.fn<(url: string, options: RequestInit) => Promise<Response>>();
let request: ApprovalRequest;
let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.spyOn(document, "hidden", "get").mockReturnValue(false);
  request = create;
  fetchMock.mockReset().mockImplementation(async (url) => {
    if (url === listPath) return Response.json({ approvals: [request] });
    if (url === answerPath) return Response.json({ id: request.id, state: "approved" });
    throw new Error(`Unexpected fetch: ${url}`);
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

async function mountArmed() {
  await act(async () => root.render(<ApprovalPanel count={1} stopping={false} />));
  await act(async () => { await vi.advanceTimersByTimeAsync(600); });
}

const button = (text: string) => [...host.querySelectorAll("button")].find((item) => item.textContent === text);
const answers = () => fetchMock.mock.calls.filter(([url]) => url === answerPath);

async function pressEnter(target: HTMLButtonElement, modifiers: KeyboardEventInit = {}) {
  target.focus();
  expect(document.activeElement).toBe(target);
  const init = { key: "Enter", code: "Enter", bubbles: true, cancelable: true, ...modifiers };
  await act(async () => {
    target.dispatchEvent(new KeyboardEvent("keydown", init));
    target.dispatchEvent(new KeyboardEvent("keyup", init));
  });
}

it.each([
  ["Ctrl+Enter", { ctrlKey: true }],
  ["Alt+Enter", { altKey: true }],
] as const)("HIGH: %s does not activate focused Approve", async (_shortcut, modifiers) => {
  await mountArmed();
  const approve = button("Approve")!;
  expect(approve.disabled).toBe(false);
  // Chromium does not activate a native button with either shortcut.
  await pressEnter(approve, modifiers);
  expect(answers()).toHaveLength(0);
  await pressEnter(approve);
  expect(answers()).toHaveLength(1);
});

it.each<[string, ApprovalRequest]>([
  ["MED: a calendar title made entirely of blank Braille cells", {
    ...create,
    card: { ...create.card, rows: [{ label: "Title", value: "\u2800".repeat(4) }, ...create.card.rows.slice(1)] },
  }],
  ["MED: a calendar event ID replaced by the backend redactor", {
    ...create, tool: "calendar_delete",
    card: {
      title: "Delete a calendar event",
      rows: [
        { label: "Event", value: "[withheld: API key]" },
        { label: "Revision", value: '"sample-revision"' },
        { label: "Current title", value: "Sample planning" },
      ],
    },
  }],
  ["LOW: a duplicate target label disguised by a zero-width space", {
    ...create,
    card: { ...create.card, rows: [...create.card.rows, { label: "Title\u200b", value: "Different event" }] },
  }],
])("%s leaves only Deny", async (_case, input) => {
  request = input;
  await mountArmed();
  expect(button("Deny")?.disabled).toBe(false);
  const approve = button("Approve");
  if (approve && !approve.disabled) await pressEnter(approve);
  expect(answers()).toHaveLength(0);
  expect(button("Approve")).toBeUndefined();
  expect(host.textContent).toContain("This request cannot be shown safely");
});
