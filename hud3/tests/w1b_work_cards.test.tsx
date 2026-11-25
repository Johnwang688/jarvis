import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApprovalPanel } from "../src/components/approvals/ApprovalPanel";
import type { ApprovalRequest } from "../src/components/approvals/approvalApi";
import cards from "./fixtures/work-approval-cards.json";

type Card = ApprovalRequest["card"];
const examples = [
  { tool: "work_start", card: cards.work_start, target: "Project", other: "Thread", text: "Brief",
    required: ["Folder", "Agent", "Runs in"] },
  { tool: "work_send", card: cards.work_send, target: "Thread", other: "Project", text: "Message",
    required: ["Thread id", "In project"] },
];

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

async function mount(tool: string, card: Card) {
  const request: ApprovalRequest = {
    id: "sample-work", digest: "a".repeat(64), tool, card,
    asked_at: 1_700_000_000, expires_in: 120, level: "L2", why: "default:thread.start",
  };
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ approvals: [request] }))));
  await act(async () => root.render(<ApprovalPanel count={1} stopping={false} />));
  await act(async () => { await vi.advanceTimersByTimeAsync(600); });
}

function field(selector: string, label: string) {
  const row = Array.from(host.querySelectorAll(`${selector} .approval-field`))
    .find((element) => element.querySelector("dt")?.textContent === label);
  return row?.querySelector("dd")?.textContent;
}

function replaceTarget(card: Card, target: string, value: string): Card {
  return { ...card, rows: card.rows.map((row) => row.label === target ? { ...row, value } : row) };
}

describe.each(examples)("$tool card", ({ tool, card, target, other, text, required }) => {
  it("offers approval and expands the full text from the backend", async () => {
    await mount(tool, card);
    expect(host.querySelector<HTMLButtonElement>(".approval-approve")?.disabled).toBe(false);
    expect(field(".approval-target", target)).toBe(card.rows.find((row) => row.label === target)?.value);
    const value = card.rows.find((row) => row.label === text)!.value;
    expect(value).toContain("\n");
    expect(field(".approval-summary", text)).toBe(value.replace(/\s+/gu, " ").slice(0, 119) + "…");
    const details = host.querySelector("details")!;
    expect(details.open).toBe(false);
    expect(details.querySelector("summary")?.textContent).toBe("Details");
    await act(async () => { details.querySelector("summary")!.click(); });
    expect(details.open).toBe(true);
    expect(field("details", text)).toBe(value);
  });

  const targetRow = card.rows.find((row) => row.label === target)!;
  const invalid: Record<string, Card> = {
    "missing target": { ...card, rows: card.rows.filter((row) => row.label !== target) },
    "duplicate target": { ...card, rows: [...card.rows, targetRow] },
    "empty target": replaceTarget(card, target, ""),
    "redacted target": replaceTarget(card, target, "sample-[redacted]"),
    "bidi control in target": replaceTarget(card, target, "sample\u202e-target"),
    "different action title": { ...card, title: "Review a coding thread" },
    "second identity row": { ...card, rows: [...card.rows, { label: other, value: "sample-other" }] },
    "conflicting label": { ...card, rows: [...card.rows, { label: text.toLowerCase(), value: "Different text" }] },
  };

  for (const label of required) {
    const row = card.rows.find((row) => row.label === label)!;
    invalid[`missing ${label}`] = { ...card, rows: card.rows.filter((row) => row.label !== label) };
    invalid[`duplicate ${label}`] = { ...card, rows: [...card.rows, row] };
    invalid[`same ${label} with a different spelling`] = {
      ...card, rows: [...card.rows, { ...row, label: label.toLowerCase() }],
    };
    invalid[`empty ${label}`] = replaceTarget(card, label, " ");
    invalid[`redacted ${label}`] = replaceTarget(card, label, "sample [withheld: credential]");
  }

  it.each(Object.entries(invalid))("withholds approval for %s", async (_case, altered) => {
    await mount(tool, altered);
    expect(Array.from(host.querySelectorAll("button")).map((button) => button.textContent)).toEqual(["Deny"]);
    expect(host.querySelector('[role="status"]')?.textContent).toContain("cannot be shown safely");
  });
});
