import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ApprovalPanel } from "../src/components/approvals/ApprovalPanel";
import type { ApprovalRequest, ApprovalRow } from "../src/components/approvals/approvalApi";

const TYPED = "  first line\n" + "keep this complete. ".repeat(90);
const site = { label: "Site", value: "https://example.invalid" };
const field: ApprovalRow = { label: "Field", value: "Notes", source: "page" };
const button: ApprovalRow = { label: "Button", value: "Send", source: "page" };
const kind = { label: "Kind", value: "button" };
const then = { label: "Then", value: "the page receives this text; the form is not submitted" };
let host: HTMLElement;
let root: Root;

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.spyOn(document, "hidden", "get").mockReturnValue(false);
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

async function mount(tool: string, title: string, rows: ApprovalRow[]) {
  const request: ApprovalRequest = {
    id: "example", digest: "a".repeat(64), tool, card: { title, rows },
    asked_at: 1_700_000_000, expires_in: 120, level: "L2", why: "hard:tainted",
  };
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ approvals: [request] }))));
  await act(async () => root.render(<ApprovalPanel count={1} stopping={false} />));
  await act(async () => { await vi.advanceTimersByTimeAsync(600); });
}

it("shows all exact text and the field's provenance before approving", async () => {
  await mount("browser_fill", "Fill a browser field", [site, field, { label: "Text", value: TYPED }, then]);
  expect(host.querySelector<HTMLButtonElement>(".approval-approve")?.disabled).toBe(false);
  const summary = host.querySelector(".approval-summary");
  expect(summary?.textContent).toContain("Field (text from the page)");
  expect(Array.from(summary?.querySelectorAll("dd") ?? []).some((row) => row.textContent === TYPED)).toBe(true);
});

it.each(["Press a browser control", "Submit a browser form", "Follow a link on the browser page"])(
  "offers approval for %s", async (title) => {
    const rows = title.startsWith("Follow") ? [{ label: "Link number", value: "1" }] : [site, button, kind];
    await mount("browser_press", title, rows);
    expect(host.querySelector<HTMLButtonElement>(".approval-approve")?.disabled).toBe(false);
  },
);

it("withholds approval if the text is missing", async () => {
  await mount("browser_fill", "Fill a browser field", [site, field, then]);
  expect(host.querySelector(".approval-approve")).toBeNull();
});

it("withholds approval if the control kind is missing", async () => {
  await mount("browser_press", "Press a browser control", [site, button]);
  expect(host.querySelector(".approval-approve")).toBeNull();
});
