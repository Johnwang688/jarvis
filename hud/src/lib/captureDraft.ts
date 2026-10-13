// A capture pins a conversation before asynchronous screenshot/export work.
import type { State } from "../state/store";
import { draftKey } from "./chats";
import type { PaneNo } from "./workspace";
export interface CaptureTarget {
  key: string;
  compose?: { id: string; pane: PaneNo; projectId: string | null };
}
export class CaptureDrafts {
  private pending = new Set<CaptureTarget>();
  pin(state: State, pane: PaneNo | null): CaptureTarget | null {
    if (pane === null) return null;
    const chat = state.chats[pane], key = draftKey(chat, pane);
    if (!key) return null;
    const compose = !chat.threadId && !chat.compose?.openedId ? chat.compose : null;
    if (compose && !compose.id) return null;
    const target: CaptureTarget = { key, ...(compose ? {
      compose: { id: compose.id!, pane, projectId: compose.projectId },
    } : {}) };
    this.pending.add(target);
    return target;
  }
  opened(composeId: string | undefined, threadId: string) {
    if (!composeId) return;
    for (const target of this.pending) if (target.compose?.id === composeId) {
      target.key = threadId;
      delete target.compose;
    }
  }
  release(target: CaptureTarget) { this.pending.delete(target); }
}
