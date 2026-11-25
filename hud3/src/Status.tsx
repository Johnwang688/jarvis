import { useEffect, useState } from "react";

const POLL_MS = 3_000;
const TIMEOUT_MS = 3_500;

export interface JobStatus {
  name: string;
  /** Seconds since the epoch, or null when the job has not run since the daemon started. */
  lastRun: number | null;
  outcome: string | null;
  failures: number;
}

export interface DaemonStatus {
  pid: number;
  uptime_s: number;
  jobs: JobStatus[];
}

const JOB_NAME = /^[a-z0-9][a-z0-9-]{0,39}$/;
const JOB_LIMIT = 40;

// Job rows are extra: a missing or odd table leaves the list empty and never
// changes whether the daemon is shown as running.
function readJobs(value: unknown): JobStatus[] {
  if (!value || typeof value !== "object" || Array.isArray(value)) return [];
  const jobs: JobStatus[] = [];
  for (const [name, row] of Object.entries(value as Record<string, unknown>)) {
    if (jobs.length >= JOB_LIMIT) break;
    if (!JOB_NAME.test(name) || !row || typeof row !== "object") continue;
    const { last_run, last_outcome, failures } = row as Record<string, unknown>;
    jobs.push({
      name,
      lastRun: typeof last_run === "number" && Number.isFinite(last_run) ? last_run : null,
      outcome: typeof last_outcome === "string" ? last_outcome.slice(0, 24) : null,
      failures: typeof failures === "number" && Number.isInteger(failures) && failures > 0 ? failures : 0,
    });
  }
  return jobs;
}

async function readStatus(signal: AbortSignal): Promise<DaemonStatus | null> {
  const response = await fetch("/v3/status", { signal, cache: "no-store", redirect: "error" });
  if (signal.aborted || !response.ok) return null;
  const data: unknown = await response.json();
  if (!data || typeof data !== "object") return null;
  const { service, version, pid, uptime_s, jobs } = data as Record<string, unknown>;
  if (
    service !== "jarvis-agentd" || version !== "v3" ||
    typeof pid !== "number" || !Number.isInteger(pid) ||
    typeof uptime_s !== "number" || !Number.isFinite(uptime_s)
  ) return null;
  return { pid, uptime_s, jobs: readJobs(jobs) };
}

/** One poll of the daemon, shared by everything in the window that shows it. */
export function useDaemonStatus(): DaemonStatus | null | undefined {
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

  return status;
}

function span(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  if (s < 86_400) return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
  return `${Math.floor(s / 86_400)} d ${Math.floor((s % 86_400) / 3600)} h`;
}

export function StatusView({ status }: { status: DaemonStatus | null | undefined }) {
  return (
    <>
      <p role="status" className={status ? "agent-running" : status === null ? "agent-down" : undefined}>
        {status === undefined ? "Checking…" : status ? "Running" : "Not reachable"}
      </p>
      {status && (
        <dl className="agent-status">
          <div><dt>PID</dt><dd>{status.pid}</dd></div>
          <div><dt>Uptime</dt><dd>{span(status.uptime_s)}</dd></div>
        </dl>
      )}
    </>
  );
}

export function Status() {
  return <StatusView status={useDaemonStatus()} />;
}

/** The daemon's background jobs: what ran last, how it ended. Text only. */
export function Jobs({ status }: { status: DaemonStatus | null | undefined }) {
  if (status === undefined) return <p>Checking…</p>;
  if (!status) return <p>Scheduled jobs are not reachable.</p>;
  if (!status.jobs.length) return <p>No scheduled jobs reported.</p>;
  const now = Date.now() / 1000;
  return (
    <ul className="agent-jobs">
      {status.jobs.map((job) => {
        // The daemon reports three things: ran and ended well, never tried,
        // or failing. Anything that is not clearly one of the first two is shown as failing.
        const state = job.outcome === "ok" && job.failures === 0 ? "ok"
          : job.outcome === null && job.lastRun === null && job.failures === 0 ? "idle" : "failed";
        return (
          <li key={job.name} data-state={state}>
            <span className="agent-job-name">{job.name}</span>
            <span className="agent-job-when">{job.lastRun === null ? "" : `${span(now - job.lastRun)} ago`}</span>
            <span className="agent-job-state">
              {state === "idle" ? "Not run yet" : state === "ok" ? "OK"
                : job.outcome && job.outcome !== "ok" ? job.outcome : "Failed"}
              {job.failures > 0 && ` · ${job.failures} failed`}
            </span>
          </li>
        );
      })}
    </ul>
  );
}
