// Which Bypass chip a thread gets (2026-10-21): the owner's switch that turns
// Claude Code's auto-mode classifier off for one chat. Only an opened Claude
// chat has one: OpenRouter and Codex chats have no classifier to switch off,
// and a task's workers keep theirs. The daemon refuses the rest (an ask-profile
// chat, an archived one) with a sentence for the pane's status line.

import type { Thread } from "../types";

export interface BypassView { on: boolean; title: string }

const OFF_TITLE = "Turn off Claude Code's auto-mode classifier for this chat's commands and file edits, "
  + "in turns you send. Jarvis's own rules and cards still apply. Click again to go back to auto; "
  + "a restart does too.";
const ON_TITLE = "Bypass is on: the auto-mode classifier is not judging this chat's calls. "
  + "Click to turn it back on.";

export function bypassFor(thread: Thread | null | undefined): BypassView | null {
  if (!thread || thread.role !== "chat" || thread.task_id || thread.provider !== "claude") return null;
  if (thread.archived) return null;
  const on = thread.bypass === true;
  return { on, title: on ? ON_TITLE : OFF_TITLE };
}
