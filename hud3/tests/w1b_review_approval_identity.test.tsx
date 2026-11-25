import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ApprovalPanel } from "../src/components/approvals/ApprovalPanel";
import type { ApprovalRequest } from "../src/components/approvals/approvalApi";
import cards from "./fixtures/work-approval-cards.json";

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

it.each([
  ["work_send", "Thread id", "missing"],
  ["work_send", "Thread id", "duplicate"],
  ["work_start", "Folder", "missing"],
  ["work_start", "Folder", "redacted"],
  ["work_start", "Agent", "redacted"],
  ["work_start", "Runs in", "missing"],
] as const)("withholds %s approval when %s is %s", async (tool, label, defect) => {
  const original = cards[tool];
  const row = original.rows.find((field) => field.label === label)!;
  const rows = original.rows.filter((field) => field.label !== label);
  if (defect === "duplicate") rows.push(row, row);
  if (defect === "redacted") rows.push({ ...row, value: "example/[redacted]" });
  const request: ApprovalRequest = {
    id: "sample-work", digest: "a".repeat(64), tool,
    card: { ...original, rows }, asked_at: 1_700_000_000,
    expires_in: 120, level: "L2", why: "default:thread.start",
  };
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ approvals: [request] }))));
  await act(async () => root.render(<ApprovalPanel count={1} stopping={false} />));
  await act(async () => { await vi.advanceTimersByTimeAsync(600); });
  expect(host.querySelector(".approval-card")).not.toBeNull();
  expect(host.querySelector<HTMLButtonElement>(".approval-approve")?.disabled ?? true).toBe(true);
});
