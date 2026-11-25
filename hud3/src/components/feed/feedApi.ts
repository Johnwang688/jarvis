export type BriefKind = "decision" | "news" | "digest";
export type FeedbackTag = "too_long" | "missing_context";

const states = ["open", "approved", "edited", "other", "dismissed", "expired", "done"] as const;

export interface Brief {
  id: string;
  kind: BriefKind;
  rank: number;
  due: string | null;
  ask: string;
  why_now: string;
  recommend: { label: string; because: string } | null;
  options: { label: string }[];
  default: { action: string; at: string | null } | null;
  context: { label: string; ref: string }[];
  state: typeof states[number];
  feedback: FeedbackTag[];
}

export interface FeedSnapshot {
  briefs: Brief[];
  recent: Brief[];
}

export type FeedWrite =
  | { route: "answer"; body:
      | { id: string; action: "approve" | "not_now" }
      | { id: string; action: "edit" | "other"; text: string }
    }
  | { route: "feedback"; body: { id: string; tag: FeedbackTag } };

const feedPath = "/v3/owner/feed";

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function textOrNull(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isBrief(value: unknown): value is Brief {
  if (!record(value)) return false;
  const { id, kind, rank, due, ask, why_now, recommend, options, default: fallback, context, state, feedback } = value;
  return typeof id === "string"
    && (kind === "decision" || kind === "news" || kind === "digest")
    && typeof rank === "number" && Number.isInteger(rank) && rank >= 1 && rank <= 8
    && textOrNull(due) && typeof ask === "string" && typeof why_now === "string"
    && (recommend === null || (record(recommend) && typeof recommend.label === "string" && typeof recommend.because === "string"))
    && Array.isArray(options) && options.every((item) => record(item) && typeof item.label === "string")
    && (fallback === null ? kind !== "decision" : record(fallback) && typeof fallback.action === "string" && textOrNull(fallback.at))
    && Array.isArray(context) && context.every((item) => record(item) && typeof item.label === "string" && typeof item.ref === "string")
    && states.some((allowed) => allowed === state)
    && Array.isArray(feedback) && feedback.every((tag) => tag === "too_long" || tag === "missing_context");
}

class FeedHttpError extends Error {
  constructor(readonly status: number) {
    super("Feed request failed");
  }
}

export function feedError(error: unknown, writing = false) {
  if (error instanceof FeedHttpError) {
    if (error.status === 403) return "Feed access denied.";
    if (error.status === 503) return "Feed is busy. Try again shortly.";
    if (writing && (error.status === 400 || error.status === 404)) return "The feed could not accept this change.";
  }
  return writing ? "Could not confirm the change. Try again shortly." : "Feed unavailable. Retrying…";
}

async function request(path: string, options: RequestInit): Promise<unknown> {
  const response = await fetch(path, { cache: "no-store", redirect: "error", ...options });
  if (!response.ok) throw new FeedHttpError(response.status);
  return response.json();
}

export async function readFeed(signal: AbortSignal): Promise<FeedSnapshot> {
  const value = await request(feedPath, { signal });
  if (!record(value) || !Array.isArray(value.briefs) || !Array.isArray(value.recent)
    || !value.briefs.every((brief) => isBrief(brief) && brief.state === "open")
    || !value.recent.every((brief) => isBrief(brief) && brief.state !== "open")) {
    throw new Error("Invalid feed snapshot");
  }
  return { briefs: value.briefs, recent: value.recent };
}

export async function writeFeed({ route, body }: FeedWrite): Promise<Brief> {
  const value = await request(`${feedPath}/${route}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(10_000),
  });
  if (!record(value) || !isBrief(value.brief) || value.brief.id !== body.id
    || (route === "answer" && value.brief.state === "open")) {
    throw new Error("Invalid feed write response");
  }
  return value.brief;
}
