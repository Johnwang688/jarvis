// The phone's answering card and the HUD's card draw the same words from one
// fixture (review of PR #313): field by field, the text each shows.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { ApprovalVeil } from "../components/Approvals";
import type { ApprovalRequest } from "../types";
import { cardView, parseDetail } from "./card";
import { TOO_DEEP, TOO_DEEP_WARNING } from "../lib/approvalCard";

vi.mock("../api", () => ({ browserApi: { get: vi.fn() } }));
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const H = '<img src=x onerror="window.__pwned=1"> ‮gnp.exe <b>bold</b>';
const FIXTURE = {
  req_id: "parity_req_1", code: "ABCD", tool: "Bash", layer: "reviewer", thread_id: "aaaaaaaa", task_id: null,
  provider: "claude", origin: 'task 0000beef "' + H + '"', asked_at: new Date().toISOString(), timeout_s: 600,
  allowlistable: true, hud_only: false, headline: "SANDBOX WIDENING: network on · " + H,
  command: "rm -rf build && echo " + "x".repeat(5000) + H, reason: "the reviewer said " + H,
  args: { command: "rm -rf build", description: H, timeout: 5, nested: { list: [1, "two", { k: H }] } },
  relayed_from: { title: "Other chat " + H, provider: "codex", thread_id: "bbbbbbbb", kind: "started" },
} as const;

let host: HTMLDivElement, root: Root;
beforeEach(() => { host = document.createElement("div"); document.body.appendChild(host); root = createRoot(host); });
afterEach(async () => { await act(async () => root.unmount()); host.remove(); });

function fields(node: ParentNode) {
  const text = (sel: string) => Array.from(node.querySelectorAll(sel)).map((n) => n.textContent);
  return {
    headline: text('[data-testid="approval-headline"]'),
    origin: text('[data-testid="approval-origin"]'),
    relayed: text('[data-testid="approval-relayed"]'),
    tool: text('[data-testid="approval-tool"]'),
    command: text('[data-testid="approval-command"]'),
    args: text(".arg:not([data-testid=\"approval-command\"])"),
    reason: text(".reason"),
  };
}

function phone(card: Record<string, unknown>) {
  return cardView({ rowId: "appr:" + String(card.req_id), row: null, refused: null, busy: false, rule: null, notice: "",
    detail: parseDetail({ card, private: false, withheld: false, secure: { ok: true }, can: { allow: true, always: true }, why: "" }) },
  { back() {}, deny() {}, allow() {}, always() {}, confirmAlways() {}, cancelAlways() {} });
}

describe("the HUD card and the phone card show the same text (parity)", () => {
  it("field by field, from one fixture", async () => {
    await act(async () => root.render(createElement(ApprovalVeil, { requests: [FIXTURE as unknown as ApprovalRequest], onDecide: vi.fn() })));
    const desk = fields(host.querySelector('[data-testid="approval-card"]')!);
    const mobile = fields(phone(FIXTURE as unknown as Record<string, unknown>));
    expect(desk.headline.length).toBe(1);
    expect(desk.args.length).toBe(4);
    expect(mobile).toEqual(desk);
  });

  it("an argument nested past the redaction depth: DENY only, on both", async () => {
    let deep: unknown = TOO_DEEP;
    for (let i = 0; i < 3; i++) deep = { [`l${i}`]: deep };
    const card = { ...FIXTURE, req_id: "parity_deep", args: { spec: deep } };
    await act(async () => root.render(createElement(ApprovalVeil, { requests: [card as unknown as ApprovalRequest], onDecide: vi.fn() })));
    expect(host.querySelector('[data-testid="approval-too-deep"]')!.textContent).toBe(TOO_DEEP_WARNING);
    expect(host.querySelector<HTMLButtonElement>('[data-testid="approval-allow"]')!.disabled).toBe(true);
    expect(host.querySelector<HTMLButtonElement>('[data-testid="approval-always"]')!.disabled).toBe(true);
    expect(host.querySelector<HTMLButtonElement>('[data-testid="approval-deny"]')!.disabled).toBe(false);
    const view = phone(card as unknown as Record<string, unknown>);
    expect(view.querySelector('[data-testid="approval-too-deep"]')!.textContent).toBe(TOO_DEEP_WARNING);
    expect(view.querySelector<HTMLButtonElement>('[data-testid="approval-allow"]')!.disabled).toBe(true);
    expect(view.querySelector<HTMLButtonElement>('[data-testid="approval-deny"]')!.disabled).toBe(false);
  });
});
