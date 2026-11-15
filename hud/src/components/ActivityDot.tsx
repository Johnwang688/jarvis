import { ACTIVITY_LABEL, type ActivityStatus } from "../lib/activity";

/** One row's status. Idle is the plain `·` it always was; the rest carry a
 * label for the tooltip and for a screen reader. */
export function Dot({ status, testid }: { status: ActivityStatus; testid: string }) {
  if (status === "idle") {
    return <span className="act idle" data-testid={testid} data-status="idle">·</span>;
  }
  const label = ACTIVITY_LABEL[status];
  return (
    <span
      className={`act ${status}`}
      data-testid={testid}
      data-status={status}
      title={label}
      role="img"
      aria-label={label}
    >
      {status === "failed" ? (
        <svg viewBox="0 0 12 12" aria-hidden="true">
          <path d="M6 0.8 L11.6 11 H0.4 Z" />
          <rect x="5.25" y="4" width="1.5" height="3.8" rx="0.5" />
          <rect x="5.25" y="8.6" width="1.5" height="1.5" rx="0.6" />
        </svg>
      ) : null}
    </span>
  );
}
