import { useLayoutEffect, useRef, useState } from "react";
import { answerApproval, readApprovals, type ListedApproval, type Decision } from "./approvalApi";

const POLL_MS = 1_000;
const MAX_BACKOFF_MS = 30_000;
const TIMEOUT_MS = 10_000;

export interface ShownApproval {
  request: ListedApproval;
  key: string;
  deadline: number;
}

const identity = ({ id, digest }: ListedApproval) => JSON.stringify([id, digest]);

export function useApprovals(count: number, sessionId?: string | null) {
  const [shown, setShown] = useState<ShownApproval>();
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [readError, setReadError] = useState(false);
  const [notice, setNotice] = useState<"gone" | "failed">();
  const observe = useRef<(count: number) => void>();
  const submit = useRef<(shown: ShownApproval, decision: Decision) => void>();

  useLayoutEffect(() => {
    let current: ShownApproval | undefined;
    let pendingCount = 0;
    let refresh = false;
    let verified = false;
    let stopped = false;
    let backoff = 0;
    let retryAt = 0;
    let timer: ReturnType<typeof setTimeout>;
    let read: AbortController | undefined;
    let write: AbortController | undefined;
    const answered = new Set<string>();

    function publish(next: ShownApproval | undefined) {
      current = next;
      setShown(next);
    }

    function setVerified(next: boolean) {
      verified = next;
      setReady(next);
    }

    async function poll() {
      clearTimeout(timer);
      if (stopped || document.hidden || read || (!pendingCount && !refresh)) return;
      const wait = retryAt - performance.now();
      if (wait > 0) {
        timer = setTimeout(poll, Math.ceil(wait));
        return;
      }
      refresh = false;
      const controller = new AbortController();
      read = controller;
      const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(TIMEOUT_MS)]);
      const started = performance.now();
      try {
        const list = await readApprovals(signal, sessionId);
        if (controller.signal.aborted || refresh) return;
        if (signal.aborted) throw new Error("Approval list timed out");
        // Keep a completed card out even if this read began before its answer.
        const order = list.map(identity);
        for (const key of answered) {
          if (!order.includes(key)) answered.delete(key);
        }
        const request = list.find((request) => !answered.has(identity(request)));
        if (request) {
          const key = JSON.stringify([order, request.id, request.digest, request.tool, request.card, request.level, request.why]);
          let deadline = started + request.expires_in * 1000;
          if (current && identity(current.request) === identity(request)) deadline = Math.min(deadline, current.deadline);
          publish({ request, key, deadline });
        } else if (current && performance.now() < current.deadline) {
          publish(undefined);
        }
        backoff = 0;
        retryAt = 0;
        setReadError(false);
        setVerified(true);
      } catch {
        if (!controller.signal.aborted) {
          backoff = Math.min((backoff || POLL_MS) * 2, MAX_BACKOFF_MS);
          retryAt = performance.now() + backoff;
          setVerified(false);
          setReadError(pendingCount > 0);
          if (!pendingCount && current && performance.now() < current.deadline) publish(undefined);
        }
      } finally {
        read = undefined;
        if (!stopped && !document.hidden && (pendingCount || refresh)) {
          timer = setTimeout(poll, Math.max(refresh ? 0 : POLL_MS, Math.ceil(retryAt - performance.now())));
        }
      }
    }

    observe.current = (next) => {
      if (next === pendingCount) return;
      pendingCount = next;
      refresh = true;
      setVerified(false);
      void poll();
    };

    submit.current = async (displayed, decision) => {
      if (!verified || document.hidden || write || current?.key !== displayed.key
        || performance.now() >= current.deadline) return;
      const controller = new AbortController();
      write = controller;
      setBusy(true);
      setNotice(undefined);
      try {
        const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(TIMEOUT_MS)]);
        const result = await answerApproval(displayed.request, decision, signal);
        if (stopped) return;
        if (signal.aborted) throw new Error("Approval answer timed out");
        const key = identity(displayed.request);
        answered.add(key);
        if (current && identity(current.request) === key) publish(undefined);
        if (result === "gone") setNotice("gone");
      } catch {
        if (!stopped) setNotice("failed");
      } finally {
        write = undefined;
        if (!stopped) setBusy(false);
      }
    };

    function onVisibilityChange() {
      if (document.hidden) {
        clearTimeout(timer);
        // A read interrupted by hiding still needs to run on return, even on a count of zero.
        refresh ||= !!read;
        setVerified(false);
        read?.abort();
      } else {
        void poll();
      }
    }

    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      stopped = true;
      observe.current = undefined;
      submit.current = undefined;
      document.removeEventListener("visibilitychange", onVisibilityChange);
      clearTimeout(timer);
      read?.abort();
      write?.abort();
    };
  }, [sessionId]);

  useLayoutEffect(() => observe.current?.(count), [count]);

  return { shown, ready, busy, readError, notice,
    answer: (displayed: ShownApproval, decision: Decision) => submit.current?.(displayed, decision) };
}
