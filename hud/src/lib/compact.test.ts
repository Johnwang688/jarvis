// WP-C of docs/plans/2026-10-19-compact-button-plan.md: which Compact chip a
// thread gets. Tests first; today `./compact` does not exist, so this whole
// file fails to import.
//
// THE INTERFACE THE IMPLEMENTER BUILDS (chosen where the plan is silent)
// ======================================================================
//   hud/src/lib/compact.ts
//     export type CompactState = "ready" | "running" | "off";
//     export interface CompactView { state: CompactState; title: string }
//     export function compactFor(
//       thread: Thread | null | undefined,
//       now: { turnRunning: boolean; card: boolean },
//     ): CompactView | null
//
//   null (no chip at all)  a compose pane (no thread), any role but "chat",
//                          any thread with a task_id.
//   "running"              thread.compacting (live flag from `thread_json`,
//                          kept by the `compact_started` / `compacted` events).
//                          It wins over everything below: a compaction is a
//                          turn, so turnRunning is true too.
//   "off"                  archived, or a card is up, or a turn is running.
//                          Title precedence (chosen): archived, then card,
//                          then turn:
//                            "This chat is archived."
//                            "Answer the card first."
//                            "A turn is running; compact when it ends."
//   "ready"                everything else. Title per provider (plan section 4):
//                            claude: "Run Claude Code's /compact: it summarises the
//                                     conversation so the model's context shrinks.
//                                     Every message stays here."
//                            codex:  starts "Run Codex's own compaction"
//                            fast:   starts "Summarise older messages (your first
//                                     request is kept)"
//   `running` has some non-empty title of its own (not pinned).
//
// App.renderChat passes `compactFor(thread, {turnRunning: runningHere(chat) ||
// chat.busy, card: workBlocked})` plus `onCompact` to InputBar (see
// components/InputBarCompact.test.ts).
//
// Mutations this file catches: "shown for tasks" (the null cases), "enabled
// under card" (card -> off), "enabled while running" (turn -> off).

import { compactFor } from "./compact";
import type { Thread } from "../types";

const IDLE = { turnRunning: false, card: false };

function thread(over: Partial<Thread> = {}): Thread {
  return {
    id: "t1", project_id: "p1", role: "chat", provider: "claude", provider_session_id: null,
    task_id: null, title: "desk chat", created: "", updated: "", turns: 2, cost_usd: 0, tokens: 0,
    model: null, effort: null, ...over,
  } as Thread;
}

const CLAUDE_TITLE = "Run Claude Code's /compact: it summarises the conversation so the model's "
  + "context shrinks. Every message stays here.";

describe("compactFor: which threads get a chip", () => {
  it("gives none to a compose pane (no thread yet)", () => {
    expect(compactFor(null, IDLE)).toBeNull();
    expect(compactFor(undefined, IDLE)).toBeNull();
  });

  it("gives none to a task's thread, whatever its role or state", () => {
    expect(compactFor(thread({ task_id: "k1" }), IDLE)).toBeNull();
    expect(compactFor(thread({ task_id: "k1", compacting: true }), IDLE)).toBeNull();
  });

  it.each(["orchestrator", "implementer", "reviewer", "researcher"] as const)(
    "gives none to a %s thread: only a chat compacts", (role) => {
      expect(compactFor(thread({ role }), IDLE)).toBeNull();
    });
});

describe("compactFor: state", () => {
  it("is ready for an idle chat", () => {
    for (const provider of ["claude", "codex", "fast"] as const) {
      expect(compactFor(thread({ provider }), IDLE)?.state).toBe("ready");
    }
  });

  it("is running while the thread compacts, even though that is a turn too", () => {
    expect(compactFor(thread({ compacting: true }), IDLE)?.state).toBe("running");
    expect(compactFor(thread({ compacting: true, running: true }), { turnRunning: true, card: false })?.state)
      .toBe("running");
    expect(compactFor(thread({ compacting: true }), IDLE)?.title).toBeTruthy();
  });

  it("is off while a turn runs, and says to wait", () => {
    const view = compactFor(thread(), { turnRunning: true, card: false });
    expect(view).toEqual({ state: "off", title: "A turn is running; compact when it ends." });
  });

  it("is off under a card, and says to answer it", () => {
    const view = compactFor(thread(), { turnRunning: false, card: true });
    expect(view).toEqual({ state: "off", title: "Answer the card first." });
  });

  it("is off in an archived chat; that reason comes first", () => {
    const archived = thread({ archived: "2026-10-19T00:00:00+00:00" });
    expect(compactFor(archived, IDLE)).toEqual({ state: "off", title: "This chat is archived." });
    expect(compactFor(archived, { turnRunning: true, card: true })?.title).toBe("This chat is archived.");
  });

  it("names the card before the running turn when both hold", () => {
    expect(compactFor(thread(), { turnRunning: true, card: true })).toEqual({ state: "off", title: "Answer the card first." });
  });
});

describe("compactFor: titles by provider (plan section 4)", () => {
  it("Claude runs Claude Code's /compact", () => {
    expect(compactFor(thread({ provider: "claude" }), IDLE)?.title).toBe(CLAUDE_TITLE);
  });

  it("Codex runs its own compaction", () => {
    expect(compactFor(thread({ provider: "codex" }), IDLE)?.title).toMatch(/^Run Codex's own compaction/);
  });

  it("Jarvis summarises older messages and keeps the first request", () => {
    expect(compactFor(thread({ provider: "fast" }), IDLE)?.title)
      .toMatch(/^Summarise older messages \(your first request is kept\)/);
  });

  it("the three differ", () => {
    const titles = (["claude", "codex", "fast"] as const).map((provider) => compactFor(thread({ provider }), IDLE)?.title);
    expect(new Set(titles).size).toBe(3);
  });
});
