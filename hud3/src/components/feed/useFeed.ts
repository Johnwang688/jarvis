import { useEffect, useRef, useState } from "react";
import { feedError, readFeed, writeFeed, type Brief, type FeedSnapshot, type FeedWrite } from "./feedApi";

const POLL_MS = 30_000;
const FOCUS_INTERVAL_MS = 5_000;
const ERROR_BACKOFF_MS = 15_000;

function applyWrite(snapshot: FeedSnapshot, brief: Brief): FeedSnapshot {
  if (brief.state === "open") {
    return { ...snapshot, briefs: snapshot.briefs.map((item) => item.id === brief.id ? brief : item) };
  }
  const recent = snapshot.recent.map((item) => item.id === brief.id ? brief : item);
  return {
    briefs: snapshot.briefs.filter((item) => item.id !== brief.id),
    recent: recent.some((item) => item.id === brief.id) ? recent : [brief, ...recent].slice(0, 20),
  };
}

function applyRefresh(current: FeedSnapshot | undefined, refreshed: FeedSnapshot): FeedSnapshot {
  if (!current) return refreshed;
  const known = new Map([...current.briefs, ...current.recent].map((brief) => [brief.id, brief]));
  function keepFeedback(brief: Brief): Brief {
    // Feedback tags are append-only, including when a read overlaps a write.
    const previous = known.get(brief.id);
    const added = previous?.feedback.filter((tag) => !brief.feedback.includes(tag));
    return added?.length ? { ...brief, feedback: [...brief.feedback, ...added] } : brief;
  }

  let next = { briefs: refreshed.briefs.map(keepFeedback), recent: refreshed.recent.map(keepFeedback) };
  for (const brief of next.briefs) {
    // A read may have captured the brief before a concurrent answer committed.
    const previous = known.get(brief.id);
    if (previous && previous.state !== "open") {
      next = applyWrite(next, { ...previous, feedback: brief.feedback });
    }
  }
  return next;
}

export function useFeed() {
  const [snapshot, setSnapshot] = useState<FeedSnapshot>();
  const [error, setError] = useState("");
  const [pending, setPending] = useState<ReadonlySet<string>>(new Set());
  const [writeErrors, setWriteErrors] = useState<ReadonlyMap<string, string>>(new Map());
  const writes = useRef(new Set<string>());
  const latest = useRef<FeedSnapshot>();
  const queued = useRef<FeedSnapshot>();

  function publish(next: FeedSnapshot) {
    latest.current = next;
    setSnapshot(next);
  }

  useEffect(() => {
    let active: AbortController | undefined;
    let timer: ReturnType<typeof setInterval> | undefined;
    let retryAt = 0;
    let lastFocusPoll = -Infinity;

    function onFailure(message: string) {
      retryAt = Date.now() + ERROR_BACKOFF_MS;
      setError(message);
    }

    async function poll(onFocus = false) {
      const now = Date.now();
      if (document.hidden || active || now < retryAt || (onFocus && now - lastFocusPoll < FOCUS_INTERVAL_MS)) return;
      if (onFocus) lastFocusPoll = now;
      const controller = new AbortController();
      active = controller;
      const deadline = setTimeout(() => {
        active = undefined;
        controller.abort();
        onFailure("Feed unavailable. Retrying…");
      }, 10_000);
      const cancel = () => clearTimeout(deadline);
      controller.signal.addEventListener("abort", cancel, { once: true });

      try {
        const result = await readFeed(controller.signal);
        if (active === controller) {
          if (writes.current.size) queued.current = result;
          else publish(applyRefresh(latest.current, result));
          setError("");
        }
      } catch (failure) {
        if (active === controller) onFailure(feedError(failure));
      } finally {
        cancel();
        if (active === controller) active = undefined;
      }
    }

    function pause() {
      clearInterval(timer);
      timer = undefined;
      const request = active;
      active = undefined;
      request?.abort();
    }

    function onVisibilityChange() {
      if (document.hidden) {
        pause();
      } else {
        timer ??= setInterval(poll, POLL_MS);
        void poll();
      }
    }

    const onFocus = () => void poll(true);
    document.addEventListener("visibilitychange", onVisibilityChange);
    window.addEventListener("focus", onFocus);
    onVisibilityChange();
    return () => {
      pause();
      document.removeEventListener("visibilitychange", onVisibilityChange);
      window.removeEventListener("focus", onFocus);
    };
  }, []);

  async function write(change: FeedWrite) {
    const { id } = change.body;
    const current = latest.current;
    const brief = current?.briefs.find((item) => item.id === id) ?? current?.recent.find((item) => item.id === id);
    if (!brief || writes.current.has(id)) return false;
    const refreshed = queued.current;
    if (change.route === "answer" && (brief.state !== "open" || (refreshed && !refreshed.briefs.some((item) => item.id === id)))) {
      return false;
    }
    if (change.route === "feedback" && brief.feedback.includes(change.body.tag)) return false;
    writes.current.add(id);
    setPending(new Set(writes.current));
    setWriteErrors((current) => {
      const next = new Map(current);
      next.delete(id);
      return next;
    });

    try {
      const updated = await writeFeed(change);
      if (latest.current) publish(applyWrite(latest.current, updated));
      return true;
    } catch (failure) {
      setWriteErrors((current) => new Map(current).set(id, feedError(failure, true)));
      return false;
    } finally {
      writes.current.delete(id);
      if (!writes.current.size && queued.current) {
        publish(applyRefresh(latest.current, queued.current));
        queued.current = undefined;
      }
      setPending(new Set(writes.current));
    }
  }

  return { snapshot, error, pending, writeErrors, write };
}
