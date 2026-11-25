import { useEffect, useState } from "react";
import { readWork, type WorkResult } from "./workApi";

const POLL_MS = 10_000;
const TIMEOUT_MS = 5_000;

export function useWork(): WorkResult | undefined {
  const [work, setWork] = useState<WorkResult>();
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    let active: AbortController | undefined;
    let recheck = false;

    async function poll() {
      const request = new AbortController();
      active = request;
      // Abort the request and settle even if fetch or body decoding never resolves.
      const timeout = new Promise<WorkResult>((resolve) => {
        timer = setTimeout(() => {
          resolve("unreachable");
          request.abort();
        }, TIMEOUT_MS);
      });
      const result = await Promise.race([readWork(request.signal).catch(() => "unreachable" as const), timeout]);
      clearTimeout(timer);
      active = undefined;
      if (stopped) return;
      setWork(result);
      if (recheck) {
        recheck = false;
        void poll();
      } else {
        timer = setTimeout(poll, POLL_MS);
      }
    }

    function onVisibilityChange() {
      if (document.hidden) return;
      if (active) {
        recheck = true;
      } else {
        clearTimeout(timer);
        void poll();
      }
    }

    document.addEventListener("visibilitychange", onVisibilityChange);
    void poll();
    return () => {
      stopped = true;
      document.removeEventListener("visibilitychange", onVisibilityChange);
      clearTimeout(timer);
      active?.abort();
    };
  }, []);

  return work;
}
