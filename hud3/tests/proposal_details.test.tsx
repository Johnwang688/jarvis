import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FeedPanel } from "../src/components/feed/FeedPanel";
import { isBrief, type Brief } from "../src/components/feed/feedApi";

const title = "Review the fixture proposal?";
const brief: Brief = {
  id: "fixture-brief", proposal_id: "fixture-proposal:fixture-hash", kind: "decision", rank: 4,
  ask: title, due: null, why_now: "The fixture is ready.", recommend: null, options: [],
  default: { action: "none", at: null }, context: [], feedback: [], state: "open",
};
const markup = '<img src=x onerror="alert(1)">https://example.invalid/fixture</a>';
const basisType = "<i>Fixture source</i>";
const basisId = "https://example.invalid/<b>source</b>";
const proposal = {
  kind: "thread.start", level: "L2", why: "Fixture policy", tainted: true,
  basis: [{ type: basisType + "\u202e", id: basisId + "\u202e" }, { type: "thread", id: "fixture-thread" }], state: "briefed",
  action: { tool: "work_start", card: {
    title: "Start a fixture coding thread",
    rows: [{ label: "<b>Fixture label</b>", value: markup + "\u202e" }],
  } },
};
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const fetchMock = vi.fn<(path: string, options: RequestInit) => Promise<Response>>();
let root: Root;
let box: HTMLElement;

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
  box = document.body.appendChild(document.createElement("div"));
  root = createRoot(box);
});

afterEach(() => {
  act(() => root.unmount());
  box.remove();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const callsTo = (suffix: string) => fetchMock.mock.calls.filter(([path]) => path.endsWith(suffix));
const disclosure = () => box.querySelector<HTMLDetailsElement>(".feed-proposal")!;
const button = (name: string) => [...box.querySelectorAll<HTMLButtonElement>("article button")]
  .find((item) => item.textContent === name && !item.closest("[hidden]"))!;
const click = (element: HTMLElement) => act(async () => element.click());
const toggleDetails = async () => {
  await click(disclosure().querySelector("summary")!);
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
};

async function mount(
  details: () => Promise<Response>,
  row = brief,
  component: ReactNode = <FeedPanel />,
) {
  fetchMock.mockImplementation(async (path) => {
    if (path.endsWith("/feed")) return json({ briefs: [row], recent: [] });
    if (path.endsWith("/proposal")) return details();
    throw new Error("Unexpected fixture request");
  });
  await act(async () => root.render(component));
}

describe("feed proposal details", () => {
  it("reads old briefs and accepts a string or null proposal reference", () => {
    const { proposal_id, ...oldBrief } = brief;
    expect(isBrief(oldBrief)).toBe(true);
    expect(isBrief(brief)).toBe(true);
    expect(isBrief({ ...brief, proposal_id: null })).toBe(true);
    expect(isBrief({ ...brief, proposal_id: { id: proposal_id } })).toBe(false);
  });

  it.each(["work_start", "work_send", "vault_add", "calendar_create"])(
    "gates Approve and Edit through the details load for %s", async (tool) => {
      let respond!: (response: Response) => void;
      await mount(() => new Promise<Response>((resolve) => { respond = resolve; }));
      expect(callsTo("/proposal")).toHaveLength(0);
      await click(button(title));
      expect(callsTo("/proposal")).toHaveLength(0);
      expect(button("Read the details first").disabled).toBe(true);
      expect(button("Edit").disabled).toBe(true);
      expect(button("Other…").disabled).toBe(false);
      expect(button("Not now").disabled).toBe(false);

      const article = box.querySelector("article")!;
      article.focus();
      for (const key of ["a", "e"]) {
        await act(async () => {
          article.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }));
        });
      }
      expect(callsTo("/feed/answer")).toHaveLength(0);
      expect(article.querySelector("textarea")).toBeNull();

      await toggleDetails();
      expect(callsTo("/proposal")).toEqual([["/v3/owner/proposal", expect.objectContaining({
        method: "POST", body: JSON.stringify({ id: brief.id }),
      })]]);
      expect(button("Read the details first").disabled).toBe(true);
      expect(button("Edit").disabled).toBe(true);
      await act(async () => respond(json({ proposal: { ...proposal, action: { ...proposal.action, tool } } })));
      expect(button("Approve").disabled).toBe(false);
      expect(button("Edit").disabled).toBe(false);
      await toggleDetails();
      expect(disclosure().open).toBe(false);
      expect(button("Read the details first").disabled).toBe(true);
      expect(button("Edit").disabled).toBe(true);
      await toggleDetails();
      expect(disclosure().open).toBe(true);
      expect(button("Approve").disabled).toBe(false);
      expect(button("Edit").disabled).toBe(false);
      expect(callsTo("/proposal")).toHaveLength(1);
    },
  );

  it("caches details that finish loading while collapsed and gates actions until reopened", async () => {
    let respond!: (response: Response) => void;
    await mount(() => new Promise<Response>((resolve) => { respond = resolve; }));
    await click(button(title));
    await toggleDetails();
    expect(callsTo("/proposal")).toHaveLength(1);
    await toggleDetails();
    await act(async () => respond(json({ proposal })));
    expect(disclosure().open).toBe(false);
    expect(button("Read the details first").disabled).toBe(true);
    expect(button("Edit").disabled).toBe(true);

    const row = box.querySelector("article")!;
    row.focus();
    for (const key of ["a", "e"]) {
      await act(async () => row.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true })));
    }
    expect(callsTo("/feed/answer")).toHaveLength(0);
    expect(row.querySelector("textarea")).toBeNull();

    await toggleDetails();
    expect(button("Approve").disabled).toBe(false);
    expect(button("Edit").disabled).toBe(false);
    expect(callsTo("/proposal")).toHaveLength(1);
    await click(button(title));
    await click(button(title));
    expect(disclosure().open).toBe(false);
    expect(button("Read the details first").disabled).toBe(true);
    expect(button("Edit").disabled).toBe(true);
  });

  it("shows taint first and renders the full card, policy, and server strings as text", async () => {
    await mount(async () => json({ proposal }));
    await click(button(title));
    await toggleDetails();
    expect(disclosure().querySelector("summary + p")?.textContent).toBe("Based on untrusted text");
    const basis = disclosure().querySelector("p + ul")!;
    expect([...basis.querySelectorAll("li")].map((row) => [...row.children].map((cell) => cell.textContent)))
      .toEqual([[basisType, basisId], ["thread", "fixture-thread"]]);
    expect(basis.querySelector("i, b, a")).toBeNull();
    expect([...disclosure().querySelectorAll("dt")].map((row) => row.textContent))
      .toEqual(["Policy level", "Why asked", "<b>Fixture label</b>"]);
    expect([...disclosure().querySelectorAll("dd")].map((row) => row.textContent))
      .toEqual(["L2", "Fixture policy", markup]);
    expect(disclosure().querySelector("img, a, dt b")).toBeNull();
  });

  it.each([
    ["conflict", async () => json({ error: markup }, 409), "This proposal changed. It cannot be approved."],
    ["network error", async () => { throw new Error(markup); }, "Could not load proposal details."],
    ["malformed response", async () => json({ proposal: { ...proposal, action: {} } }), "Could not load proposal details."],
    ["missing action", async () => json({ proposal: { ...proposal, action: null } }), "Could not load proposal details."],
    ["empty card", async () => json({ proposal: {
      ...proposal, action: { ...proposal.action, card: { ...proposal.action.card, rows: [] } },
    } }), "Could not load proposal details."],
  ] as const)("keeps both actions disabled after a %s", async (_name, reply, message) => {
    await mount(reply);
    await click(button(title));
    await toggleDetails();
    expect(disclosure().querySelector("[role=status]")?.textContent).toBe(message);
    expect(button("Read the details first").disabled).toBe(true);
    expect(button("Edit").disabled).toBe(true);
  });

  it("retries a failed load when Details is reopened", async () => {
    const details = vi.fn<() => Promise<Response>>()
      .mockResolvedValueOnce(json({ error: markup }, 503))
      .mockResolvedValueOnce(json({ proposal }));
    await mount(details);
    await click(button(title));
    await toggleDetails();
    expect(button("Read the details first").disabled).toBe(true);
    await toggleDetails();
    await toggleDetails();
    expect(button("Approve").disabled).toBe(false);
    expect(button("Edit").disabled).toBe(false);
    expect(callsTo("/proposal")).toHaveLength(2);
  });

  it("shows ask-only details on a news brief with only Got it", async () => {
    await mount(async () => json({ proposal: { ...proposal, kind: "ask", action: null } }),
      { ...brief, kind: "news", default: null });
    await click(button(title));
    await toggleDetails();
    expect(disclosure().textContent).toContain(proposal.why);
    expect(button("Approve")).toBeUndefined();
    expect(button("Edit")).toBeUndefined();
    expect(button("Got it").disabled).toBe(false);
  });

  it("reads proposal details on the phone and retains its unlock rule", async () => {
    vi.resetModules();
    const { setSurface, onPhoneNotice } = await import("../src/api");
    const { UnlockContext } = await import("../src/components/UnlockContext");
    const { FeedPanel: PhoneFeed } = await import("../src/components/feed/FeedPanel");
    setSurface("phone");
    const notice = vi.fn();
    const unsubscribe = onPhoneNotice(notice);
    const unlock = vi.fn();
    try {
      await mount(async () => json({ proposal }), brief,
        <UnlockContext.Provider value={{ locked: true, busy: false, unlock }}>
          <PhoneFeed />
        </UnlockContext.Provider>);
      await click(button(title));
      expect(button("Unlock to approve").disabled).toBe(true);
      await toggleDetails();
      expect(callsTo("/proposal")).toEqual([["/m/api/proposal", expect.objectContaining({
        method: "POST", body: JSON.stringify({ id: brief.id }),
      })]]);
      expect(notice).not.toHaveBeenCalled();
      await click(button("Unlock to approve"));
      expect(unlock).toHaveBeenCalledOnce();
      expect(callsTo("/feed/answer")).toHaveLength(0);
    } finally {
      unsubscribe();
    }
  });
});
