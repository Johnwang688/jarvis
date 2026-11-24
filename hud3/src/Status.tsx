import { useEffect, useState } from "react";

const POLL_MS = 3_000;
const TIMEOUT_MS = 3_500;

interface DaemonStatus {
  pid: number;
  uptime_s: number;
}

async function readStatus(signal: AbortSignal): Promise<DaemonStatus | null> {
  const response = await fetch("/v3/status", { signal, cache: "no-store", redirect: "error" });
  if (signal.aborted || !response.ok) return null;
  const data: unknown = await response.json();
  if (!data || typeof data !== "object") return null;
  const { service, version, pid, uptime_s } = data as Record<string, unknown>;
  if (
    service !== "jarvis-agentd" || version !== "v3" ||
    typeof pid !== "number" || !Number.isInteger(pid) ||
    typeof uptime_s !== "number" || !Number.isFinite(uptime_s)
  ) return null;
  return { pid, uptime_s };
}

export function Status() {
  const [status, setStatus] = useState<DaemonStatus | null | undefined>(undefined);
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    let active: AbortController | undefined;
    let recheck = false;

    async function poll() {
      const request = new AbortController();
      active = request;
      // The deadline settles this poll even if an answer arrives after abort.
      const timeout = new Promise<null>((resolve) => {
        timer = setTimeout(() => {
          request.abort();
          resolve(null);
        }, TIMEOUT_MS);
      });
      const result = await Promise.race([readStatus(request.signal).catch(() => null), timeout]);
      clearTimeout(timer);
      active = undefined;
      if (stopped) return;
      setStatus(result);
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

  return (
    <>
      <p role="status" className={status ? "agent-running" : undefined}>
        {status === undefined ? "Checking…" : status ? "Running" : "Not reachable"}
      </p>
      {status && (
        <dl className="agent-status">
          <div><dt>PID</dt><dd>{status.pid}</dd></div>
          <div><dt>Uptime</dt><dd>{Math.floor(status.uptime_s)} s</dd></div>
        </dl>
      )}
    </>
  );
}
