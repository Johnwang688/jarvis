import { useCallback, useEffect, useLayoutEffect, useRef } from "react";
import { api } from "../api";

// Only a foreground HUD chooses a destination. Web Locks order posts across
// windows, including one already in flight when another HUD takes focus.
export function useVoiceChatTarget(ready: boolean, threadId: string | null) {
  const revision = useRef(0);
  const current = useRef({ ready, threadId });
  const retry = useRef<ReturnType<typeof setTimeout>>();
  const queue = useRef(Promise.resolve());
  const publish = useCallback(() => {
    if (!current.current.ready || document.hidden || !document.hasFocus()) return;
    clearTimeout(retry.current);
    const stamp = ++revision.current;
    const target = current.current.threadId;
    const send = async () => {
      if (stamp !== revision.current) return;
      // Complete the foreground selection even when the owner immediately
      // switches to Chrome. A later foreground selection takes the next lock.
      await api.setVoiceChatTarget(target);
    };
    // Localhost Chromium has Web Locks. Keep selection usable in a browser
    // without that API, serializing at least the current tab's requests.
    const pending = navigator.locks ? navigator.locks.request("jarvis.voice.chat-target", send) : queue.current.then(send);
    queue.current = pending.then(() => undefined, () => {
      if (stamp === revision.current && !document.hidden && document.hasFocus()) {
        retry.current = setTimeout(publish, 1500);
      }
    });
  }, []);
  useLayoutEffect(() => {
    current.current = { ready, threadId };
    publish();
  }, [ready, threadId, publish]);
  useEffect(() => {
    window.addEventListener("focus", publish);
    document.addEventListener("visibilitychange", publish);
    return () => {
      revision.current++;
      clearTimeout(retry.current);
      window.removeEventListener("focus", publish);
      document.removeEventListener("visibilitychange", publish);
    };
  }, [publish]);
  return publish;
}
