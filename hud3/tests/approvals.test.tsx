import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../src/App";
import { ChatPanel } from "../src/components/chat/ChatPanel";
import type { ApprovalRequest } from "../src/components/approvals/approvalApi";
import backendCards from "./fixtures/approval-cards.json";

const chatPath = "/v3/owner/chat";
const listPath = "/v3/owner/approvals";
const answerPath = `${listPath}/answer`;
const snapshot = {
  state: "running", session_id: "sample", seq: 0, history: [], draft: "", tools: [], approvals: 1,
};
const vault: ApprovalRequest = {
  id: "sample-vault", digest: "a".repeat(64), tool: "vault_add", asked_at: 1_700_000_000, expires_in: 120,
  level: "L2", why: "hard:tainted",
  card: {
    title: "Add a vault entry",
    rows: [
      { label: "Entry", value: "facts/example" },
      { label: "Revision", value: "missing" },
      { label: "Body", value: "Sample entry body.\nSecond line." },
    ],
  },
};
const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status });
const fetchMock = vi.fn<(url: string, options: RequestInit) => Promise<Response>>();

let host: HTMLElement;
let root: Root;
let hidden: boolean;
let count: number;
let list: ApprovalRequest[];
let readList: () => Promise<Response>;
let readChat: () => Response;
let readEvents: () => Response;
let writeAnswer: (options: RequestInit) => Promise<Response>;
let stopChat: () => Promise<Response>;

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("fetch", fetchMock);
  hidden = false;
  count = 1;
  list = [vault];
  readList = async () => json({ approvals: list });
  readChat = () => json({ ...snapshot, approvals: count });
  readEvents = () => json({ state: "running", seq: 0, gap: false, events: [], approvals: count });
  stopChat = async () => json({ state: "idle" });
  writeAnswer = async (options) => {
    const { id, decision } = JSON.parse(options.body as string);
    return json({ id, state: decision === "approve" ? "approved" : "denied" });
  };
  vi.spyOn(document, "hidden", "get").mockImplementation(() => hidden);
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (url, options) => {
    switch (url) {
      case chatPath: return readChat();
      case `${chatPath}/events`: return readEvents();
      case listPath: return readList();
      case answerPath: return writeAnswer(options);
      case `${chatPath}/send`: return json({ state: "running", steered: true }, 202);
      case `${chatPath}/stop`: return stopChat();
      case "/v3/owner/feed": return json({ briefs: [], recent: [] });
      case "/v3/owner/work": return json({ projects: [], threads: [] });
      case "/v3/status": return json({});
      default: throw new Error("Unexpected request");
    }
  });
  host = document.body.appendChild(document.createElement("div"));
  root = createRoot(host);
});

afterEach(() => {
  if (host.isConnected) act(() => root.unmount());
  host.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const mount = (app = false) => act(async () => root.render(app ? <App /> : <ChatPanel />));
const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
const requests = (path: string) => fetchMock.mock.calls.filter(([url]) => url === path).map(([, options]) => options);
const card = () => host.querySelector<HTMLElement>(".approval-card");
const button = (text: string) => [...host.querySelectorAll("button")].find((button) => button.textContent === text)!;
const composer = () => host.querySelector("textarea")!;
const click = (text: string) => act(async () => button(text).click());

function pointer(target: HTMLElement, type: string, pointerId = 1, pointerType = "mouse",
  detail = type === "click" ? 1 : 0) {
  const event = new MouseEvent(type, {
    bubbles: true, cancelable: true, detail,
    buttons: type === "pointerdown" ? 1 : 0,
  });
  Object.assign(event, { pointerId, pointerType, isPrimary: true });
  target.dispatchEvent(event);
}

function clickApprove(target = button("Approve"), pointerType = "mouse", detail = 1) {
  pointer(target, "pointerdown", 1, pointerType);
  pointer(target, "pointerup", 1, pointerType);
  pointer(target, "click", 1, pointerType, detail);
}

function expectAnswer(request: ApprovalRequest, decision: "approve" | "deny") {
  expect(requests(answerPath)).toEqual([expect.objectContaining({
    method: "POST", headers: { "Content-Type": "application/json" },
    cache: "no-store", redirect: "error",
    body: JSON.stringify({ id: request.id, digest: request.digest, decision }),
  })]);
}

async function key(target: HTMLElement, key: string, repeat = false) {
  const event = new KeyboardEvent("keydown", { key, repeat, bubbles: true, cancelable: true });
  await act(async () => { target.dispatchEvent(event); });
  return event.defaultPrevented;
}

async function visibility(value: boolean) {
  hidden = value;
  await act(async () => { document.dispatchEvent(new Event("visibilitychange")); });
}

describe("approval card", () => {
  it.each(Object.entries(backendCards))("allows approval for the backend's %s card", async (tool, card) => {
    // a6c2_card_shapes_check.py verifies these fixtures against build_card.
    const request = { ...vault, tool, card };
    list = [request];
    await mount();
    await advance(600);
    expect(button("Approve").disabled).toBe(false);
    await act(async () => clickApprove());
    expectAnswer(request, "approve");
  });

  it("puts the actions first, then the target and change, policy and collapsed redacted details", async () => {
    await mount(true);
    expect(host.querySelectorAll(".approval-card")).toHaveLength(1);
    expect(card()?.closest("[data-panel]")?.getAttribute("data-panel")).toBe("chat");
    const buttons = [...card()!.querySelectorAll("button")];
    expect(buttons.map(({ textContent, type }) => [textContent, type])).toEqual([["Deny", "button"], ["Approve", "button"]]);
    const sections = [...card()!.children];
    expect(sections.map((element) => element.className)).toEqual([
      "approval-header", "", "approval-target", "approval-summary", "approval-policy", "approval-details",
    ]);
    expect(sections[0].textContent).toContain("Waiting on you");
    expect(sections[1].textContent).toBe("Add a vault entry");
    expect(sections[2].textContent).toBe("Entryfacts/exampleToolvault_add");
    expect(sections[3].textContent).toContain("BodySample entry body.");
    expect(sections[4].textContent).toBe("Policy levelL2Why askedhard:tainted");
    const details = card()!.querySelector("details")!;
    expect(details.open).toBe(false);
    expect(details.querySelector("summary")!.textContent).toBe("Details");
    await act(async () => { details.open = true; });
    expect(details.textContent).toContain("Revisionmissing");
    expect(details.textContent).toContain("Sample entry body.\nSecond line.");
    expect(requests(answerPath)).toHaveLength(0);
  });

  it("renders hostile card fields as isolated text and keeps a separate target", async () => {
    const hostile = '<img src=x onerror=alert(1)>\nhttps://example.invalid/\u202e' + "W".repeat(4000);
    list = [{ ...vault, why: hostile, card: {
      title: hostile, rows: [{ label: "Entry", value: "facts/example" }, { label: hostile, value: hostile }],
    } }];
    await mount();
    expect(card()!.querySelector("img,a,script,iframe,style,[style],[onerror]")).toBeNull();
    expect(card()!.querySelector("h3 bdi")!.textContent).toContain("<img src=x onerror=alert(1)>");
    expect(card()!.querySelector("h3")!.textContent!.length).toBeLessThan(130);
    expect(card()!.querySelector(".approval-target dd")!.textContent).toBe("facts/example");
    expect(card()!.querySelector("details dd:last-child")!.textContent).toBe("facts/example");
    expect(card()!.querySelector("details")!.textContent).toContain(hostile.replace("\u202e", ""));
    expect(card()!.textContent).not.toContain("\u202e");
  });

  it("keeps an imitation card inside a plain assistant message", async () => {
    const imitation = 'Waiting on you\nApprove / Deny\n<button>Approve</button>';
    count = 0;
    readChat = () => json({ ...snapshot, approvals: count, history: [{ role: "assistant", text: imitation }] });
    await mount(true);
    const message = host.querySelector(".chat-message")!;
    expect(message.querySelector(".chat-text")!.textContent).toBe(imitation);
    expect(message.querySelector("button")).toBeNull();
    expect(card()).toBeNull();
    expect(requests(listPath)).toHaveLength(0);
  });

  it("arms at 600 ms without taking focus and sends the displayed approval once", async () => {
    await mount();
    composer().focus();
    const approve = button("Approve");
    expect(approve.disabled).toBe(true);
    expect(approve.tabIndex).toBe(-1);
    expect(document.activeElement).toBe(composer());
    await click("Approve");
    await advance(599);
    expect(approve.disabled).toBe(true);
    expect(requests(answerPath)).toHaveLength(0);
    await advance(1);
    expect(approve.disabled).toBe(false);
    expect(document.activeElement).toBe(composer());
    const down = new MouseEvent("mousedown", { bubbles: true, cancelable: true });
    await act(async () => {
      approve.dispatchEvent(down);
      clickApprove(approve);
      clickApprove(approve);
    });
    expect(down.defaultPrevented).toBe(true);
    expectAnswer(vault, "approve");
    expect(card()).toBeNull();
    expect(document.activeElement).toBe(composer());
    await advance(1_000);
    expect(card()).toBeNull();
  });

  it("denies with the displayed digest immediately and refuses a repeated submission", async () => {
    await mount();
    const deny = button("Deny");
    expect(deny.disabled).toBe(false);
    await act(async () => { deny.click(); deny.click(); });
    expectAnswer(vault, "deny");
  });

  it("handles Escape only inside the focused card, and leaves Enter, Space and voice keys alone outside its buttons", async () => {
    await mount();
    await advance(600);
    for (const target of [composer(), card()!, card()!.querySelector("summary")!]) {
      target.focus();
      for (const pressed of ["Enter", " ", "d"]) await key(target, pressed);
    }
    composer().focus();
    await act(async () => {
      composer().dispatchEvent(new KeyboardEvent("keydown", {
        key: "d", code: "KeyD", ctrlKey: true, bubbles: true,
      }));
    });
    expect(await key(composer(), "Escape")).toBe(false);
    expect(await key(card()!, "Escape")).toBe(false);
    expect(requests(answerPath)).toHaveLength(0);
    const approve = button("Approve");
    approve.focus();
    expect(await key(approve, "Enter", true)).toBe(true);
    expect(await key(approve, " ", true)).toBe(true);
    expect(await key(approve, "Escape", true)).toBe(true);
    expect(requests(answerPath)).toHaveLength(0);
    expect(await key(approve, "Escape")).toBe(true);
    expectAnswer(vault, "deny");
  });

  it("keeps the composer and Stop usable while waiting", async () => {
    count = 0;
    await mount();
    composer().focus();
    count = 1;
    await advance(700);
    expect(card()).not.toBeNull();
    expect(document.activeElement).toBe(composer());
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
      setter.call(composer(), "Approve / Deny");
      composer().dispatchEvent(new InputEvent("input", { bubbles: true }));
    });
    await key(composer(), "Enter");
    expect(requests(`${chatPath}/send`)[0]?.body).toBe(JSON.stringify({ text: "Approve / Deny" }));
    let stopped!: (response: Response) => void;
    stopChat = () => new Promise((done) => { stopped = done; });
    await click("Stop");
    expect(requests(`${chatPath}/stop`)).toHaveLength(1);
    expect(button("Deny").disabled).toBe(true);
    list = [];
    await act(async () => { stopped(json({ state: "idle" })); });
    expect(card()).toBeNull();
    expect(requests(answerPath)).toHaveLength(0);
  });

  it.each(["id", "digest"] as const)("re-arms when the displayed %s changes and rejects the removed button", async (field) => {
    await mount();
    await advance(600);
    const old = button("Approve");
    old.focus();
    const replacement = { ...vault, [field]: "b".repeat(64) };
    list = [replacement];
    await advance(400);
    expect(old.isConnected).toBe(false);
    expect(document.activeElement).not.toBe(button("Approve"));
    expect(button("Approve").disabled).toBe(true);
    expect(button("Approve").tabIndex).toBe(-1);
    await act(async () => { old.click(); button("Approve").click(); });
    expect(requests(answerPath)).toHaveLength(0);
    await advance(599);
    expect(button("Approve").disabled).toBe(true);
    await advance(1);
    await act(async () => { clickApprove(); });
    expectAnswer(replacement, "approve");
  });

  it.each(["Enter", " "])("requires a fresh %j press and release on the focused armed button", async (pressed) => {
    await mount();
    composer().focus();
    await key(composer(), pressed);
    await advance(600);
    const approve = button("Approve");
    approve.focus();
    const release = () => act(async () => {
      approve.dispatchEvent(new KeyboardEvent("keyup", { key: pressed, bubbles: true, cancelable: true }));
    });
    expect(await key(approve, pressed, true)).toBe(true);
    await release();
    expect(requests(answerPath)).toHaveLength(0);
    expect(await key(approve, pressed)).toBe(true);
    expect(requests(answerPath)).toHaveLength(0);
    await release();
    expectAnswer(vault, "approve");
  });

  it.each(["Enter", " "])("cancels a %j press if it becomes modified or repeats", async (pressed) => {
    await mount();
    await advance(600);
    const approve = button("Approve");
    approve.focus();
    const stroke = (type: "keydown" | "keyup", options: KeyboardEventInit = {}) => {
      approve.dispatchEvent(new KeyboardEvent(type, { key: pressed, bubbles: true, cancelable: true, ...options }));
    };
    await act(async () => {
      stroke("keydown", { metaKey: true });
      stroke("keyup");
      stroke("keydown");
      stroke("keyup", { shiftKey: true });
      stroke("keydown");
      stroke("keydown", { repeat: true });
      stroke("keyup");
    });
    expect(requests(answerPath)).toHaveLength(0);
    await act(async () => {
      stroke("keydown");
      stroke("keyup");
    });
    expectAnswer(vault, "approve");
  });

  it.each([
    { label: "Target\u200b", value: "facts/different" },
    { label: "Body\u200b", value: "Different contents" },
  ])("offers only Deny when $label conflicts with a displayed field", async (row) => {
    list = [{ ...vault, card: { ...vault.card, rows: [...vault.card.rows, row] } }];
    await mount();
    await advance(600);
    expect(button("Approve")).toBeUndefined();
    expect(host.textContent).toContain("This request cannot be shown safely");
    await click("Deny");
    expectAnswer(vault, "deny");
  });

  it("requires a matching touch release and ignores clicks without a press", async () => {
    await mount();
    await advance(600);
    const approve = button("Approve");
    await click("Approve");
    await act(async () => {
      pointer(approve, "pointerdown", 1, "touch");
      pointer(approve, "pointerup", 2, "touch");
      pointer(approve, "click", 2, "touch");
    });
    expect(requests(answerPath)).toHaveLength(0);
    await act(async () => { clickApprove(approve, "touch", 2); });
    expectAnswer(vault, "approve");
  });

  it("submits only the first press of a double click even if its answer fails", async () => {
    writeAnswer = async () => { throw new TypeError("offline"); };
    await mount();
    await advance(600);
    await act(async () => { clickApprove(); });
    expect(button("Approve").disabled).toBe(false);
    await act(async () => { clickApprove(button("Approve"), "mouse", 2); });
    expectAnswer(vault, "approve");
  });

  it("cancels a press when the list reorders behind the displayed request", async () => {
    const second = { ...vault, id: "sample-second" };
    const third = { ...vault, id: "sample-third" };
    list = [vault, second, third];
    count = 3;
    await mount();
    await advance(600);
    await act(async () => { pointer(button("Approve"), "pointerdown"); });
    list = [vault, third, second];
    await advance(400);
    expect(button("Approve").disabled).toBe(true);
    await advance(600);
    await act(async () => {
      pointer(button("Approve"), "pointerup");
      pointer(button("Approve"), "click");
    });
    expect(requests(answerPath)).toHaveLength(0);
    await act(async () => { clickApprove(); });
    expectAnswer(vault, "approve");
  });

  it.each([
    { tool: undefined },
    { tool: 12 },
    { tool: "\u200b " },
    { tool: "vault_archive" },
    { card: { ...vault.card, rows: [{ label: "Entry", value: 12 }] } },
    { card: { ...vault.card, rows: [{ label: "Entry", value: "\u0085\u3164\u2800\u200d" }] } },
    { card: { ...vault.card, rows: [{ label: "Entry", value: "facts/[redacted]" }] } },
    { card: { ...vault.card, rows: [{ label: "Entry", value: "facts/[redacted: value from a protected credential file]" }] } },
    { card: { ...vault.card, rows: [{ label: "Title", value: "Sample title" }] } },
    { card: { ...vault.card, rows: [...vault.card.rows, { label: "Event", value: "sample-event" }] } },
  ])("keeps a valid Deny envelope when the displayed request is unsafe (%j)", async (changes) => {
    readList = async () => json({ approvals: [{ ...vault, ...changes }] });
    await mount();
    expect(card()?.textContent).toContain("This request cannot be shown safely");
    expect(card()?.querySelector(".approval-approve")).toBeNull();
    await click("Deny");
    expectAnswer(vault, "deny");
  });

  it.each([404, 409])("removes a stale request on %d and ignores a list that still contains it", async (status) => {
    writeAnswer = async () => json({ error: "server message" }, status);
    await mount();
    await click("Deny");
    expect(card()).toBeNull();
    expect(host.textContent).toContain("This request is no longer waiting");
    await advance(2_000);
    expect(card()).toBeNull();
    expectAnswer(vault, "deny");
  });

  it("keeps the request when an answer fails and permits a fresh click", async () => {
    writeAnswer = async () => { throw new TypeError("offline"); };
    await mount();
    await click("Deny");
    expect(host.textContent).toContain("The answer was not recorded.");
    expect(card()).not.toBeNull();
    expect(button("Deny").disabled).toBe(false);
    const repeated = new MouseEvent("click", { detail: 2, bubbles: true });
    await act(async () => { button("Deny").dispatchEvent(repeated); });
    expectAnswer(vault, "deny");
    writeAnswer = async () => json({ id: vault.id, state: "denied" });
    await click("Deny");
    expect(requests(answerPath)).toHaveLength(2);
    expect(card()).toBeNull();
  });

  it("counts down without extending on a refresh, expires without moving focus, and starts the next card fresh", async () => {
    list = [{ ...vault, expires_in: 1.5 }];
    await mount();
    composer().focus();
    expect(card()!.querySelector('[role="timer"]')!.textContent).toBe("2s");
    await advance(1_000);
    expect(card()!.querySelector('[role="timer"]')!.textContent).toBe("1s");
    await advance(500);
    expect(card()!.textContent).toContain("Approval expired.");
    expect(card()!.querySelector("button")).toBeNull();
    expect(document.activeElement).toBe(composer());
    expect(requests(answerPath)).toHaveLength(0);
    count = 0;
    list = [];
    await advance(700);
    expect(card()!.textContent).toContain("Approval expired.");
    count = 1;
    list = [{ ...vault, id: "next-card", expires_in: 10 }];
    await advance(700);
    expect(button("Approve").disabled).toBe(true);
    expect(card()!.querySelector('[role="timer"]')!.textContent).toBe("10s");
    expect(document.activeElement).toBe(composer());
  });
});

describe("approval polling", () => {
  it("uses the chat count, refreshes once a second, then reads a transition to zero once", async () => {
    count = 0;
    await mount();
    await advance(1_400);
    expect(requests(listPath)).toHaveLength(0);
    count = 1;
    await advance(700);
    expect(requests(listPath)).toHaveLength(1);
    await advance(999);
    expect(requests(listPath)).toHaveLength(1);
    await advance(1);
    expect(requests(listPath)).toHaveLength(2);
    count = 0;
    list = [];
    await advance(400);
    expect(requests(listPath)).toHaveLength(3);
    expect(card()).toBeNull();
    await advance(5_000);
    expect(requests(listPath)).toHaveLength(3);
  });

  it("looks up a positive count from events without waiting for another snapshot", async () => {
    count = 0;
    await mount();
    readEvents = () => json({ state: "idle", seq: 0, gap: false, events: [], approvals: 1 });
    await advance(700);
    expect(requests(chatPath)).toHaveLength(1);
    expect(requests(listPath)).toHaveLength(1);
    expect(card()).not.toBeNull();
  });

  it("disables answers until a malformed refresh is replaced with a valid list", async () => {
    await mount();
    await advance(600);
    readList = async () => json({ approvals: [{ ...vault, digest: null }] });
    await advance(400);
    expect(host.textContent).toContain("Could not refresh approvals.");
    expect(button("Approve").disabled).toBe(true);
    await click("Approve");
    expect(requests(answerPath)).toHaveLength(0);
    readList = async () => json({ approvals: [vault] });
    await advance(1_000);
    expect(requests(listPath)).toHaveLength(2);
    expect(button("Approve").disabled).toBe(true);
    await advance(1_000);
    expect(button("Approve").disabled).toBe(false);
  });

  it("keeps one read in flight, aborts on hide and unmount, and refreshes on return", async () => {
    let resolve!: (response: Response) => void;
    readList = () => new Promise((done) => { resolve = done; });
    await mount();
    await advance(4_000);
    expect(requests(listPath)).toHaveLength(1);
    await visibility(true);
    expect(requests(listPath)[0].signal?.aborted).toBe(true);
    await act(async () => { resolve(json({ approvals: [vault] })); });
    expect(card()).toBeNull();
    await advance(4_000);
    expect(requests(listPath)).toHaveLength(1);
    await visibility(false);
    expect(requests(listPath)).toHaveLength(2);
    await act(async () => { root.unmount(); });
    host.remove();
    expect(requests(listPath)[1].signal?.aborted).toBe(true);
    await act(async () => { resolve(json({ approvals: [vault] })); });
    await advance(4_000);
    expect(requests(listPath)).toHaveLength(2);
  });

  it("ignores a list read overtaken by an answer and leaves a newer card in place", async () => {
    let finishRead!: (response: Response) => void;
    let finishWrite!: (response: Response) => void;
    await mount();
    await advance(600);
    readList = () => new Promise((done) => { finishRead = done; });
    await advance(400);
    writeAnswer = () => new Promise((done) => { finishWrite = done; });
    await act(async () => { clickApprove(); });
    expect(button("Deny").disabled).toBe(true);
    await act(async () => {
      finishRead(json({ approvals: [{ ...vault, id: "next", card: {
        ...vault.card, rows: [{ label: "Entry", value: "facts/next" }, ...vault.card.rows.slice(1)],
      } }] }));
    });
    expect(card()!.querySelector(".approval-target dd")?.textContent).toBe("facts/next");
    await act(async () => { finishWrite(json({ id: vault.id, state: "approved" })); });
    expect(card()!.querySelector(".approval-target dd")?.textContent).toBe("facts/next");
    expect(button("Approve").disabled).toBe(true);
    expectAnswer(vault, "approve");
  });
});
