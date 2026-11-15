// The phone app's one read helper: a GET of the read API, classified so the
// app can tell "offline" (nothing reached Jarvis, or the proxy in front of
// it could not: `tailscale serve` answers 502 while the daemon is down) from
// "signed out" (401: the session ended, was revoked or expired) from an
// answer. Every read is `no-store`: nothing old is ever served as new.

export type Got =
  | { ok: true; body: unknown }
  | { ok: false; why: "offline" | "signed-out" | "error"; status: number; body?: unknown };

export const OFFLINE_STATUSES: ReadonlySet<number> = new Set([502, 503, 504]);

export async function getJson(path: string): Promise<Got> {
  let res: Response;
  try {
    res = await fetch(path, { credentials: "same-origin", cache: "no-store" });
  } catch {
    return { ok: false, why: "offline", status: 0 };
  }
  if (res.status === 401) return { ok: false, why: "signed-out", status: 401 };
  let body: unknown = null;
  let parsed = true;
  try { body = await res.json(); } catch { parsed = false; }
  // A 502/503/504 is the proxy's (Jarvis unreachable) unless the daemon itself answered it with its words.
  if (reachOf(res.status, body) === "unreachable") return { ok: false, why: "offline", status: res.status };
  if (!parsed) return { ok: false, why: "error", status: res.status };
  if (!res.ok) return { ok: false, why: "error", status: res.status, body };
  return { ok: true, body };
}

/**
 * What one answer says about the connection (orb spec §4, owner decisions §11). EVERY request the phone
 * app makes reports its outcome through this one table (`PhoneApp.report`), so no call site decides it:
 *
 *   - 2xx: Jarvis was reached;
 *   - 401: the session ended (a write's 401 asks `me` first: WS-5c rule 9);
 *   - 502/503/504 without the daemon's `{"error": ...}`: the proxy in front of Jarvis answered for it
 *     (`tailscale serve` with nothing behind it): Jarvis unreachable, red, its status recorded;
 *   - no answer at all (status 0): offline, amber;
 *   - anything else (a 404, a 409, the daemon's own 503 "the In progress list is not running"): Jarvis
 *     answered and refused. Not a connection problem, so the orb does not change.
 */
export type Reach = "reached" | "signed-out" | "unreachable" | "offline" | "refused";

export function reachOf(status: number, body?: unknown): Reach {
  if (status === 0) return "offline";
  if (status === 401) return "signed-out";
  if (OFFLINE_STATUSES.has(status) && !errorWord(body).trim()) return "unreachable";
  if (status >= 200 && status < 300) return "reached";
  return "refused";
}

/** A read's outcome, as `getJson` (or a fake standing in for it) classified it. */
export function reachOfGot(got: Got): Reach {
  if (got.ok) return "reached";
  if (got.why === "signed-out") return "signed-out";
  if (got.why === "offline") return OFFLINE_STATUSES.has(got.status) ? "unreachable" : "offline";
  return reachOf(got.status, got.body) === "unreachable" ? "unreachable" : "refused";
}

/** A write's outcome (`postJson`, an unlock step, Lock now, a push route, the clip upload). */
export function reachOfPosted(posted: { status: number; body: unknown; offline?: boolean } | null): Reach {
  return posted === null || posted.offline === true ? "offline" : reachOf(posted.status, posted.body);
}

/** The error word a refused read carries (`{"error": ...}`), for a 404 or a 409's sentence. */
export function errorWord(body: unknown): string {
  return body && typeof body === "object" && typeof (body as { error?: unknown }).error === "string"
    ? (body as { error: string }).error : "";
}

/** One write (P-4's answers): JSON in, the status and the JSON body out.
 * "offline" when nothing reached Jarvis. Never retried: an answer is sent once. */
export type Posted = { status: number; body: Record<string, unknown> } | { status: 0; body: Record<string, unknown>; offline: true };

export async function postJson(path: string, value: unknown): Promise<Posted> {
  let res: Response;
  try {
    res = await fetch(path, {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(value),
    });
  } catch {
    return { status: 0, body: {}, offline: true };
  }
  let body: Record<string, unknown> = {};
  try {
    const parsed = await res.json();
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) body = parsed as Record<string, unknown>;
  } catch { /* an empty or non-JSON answer: its status says enough */ }
  return { status: res.status, body };
}
