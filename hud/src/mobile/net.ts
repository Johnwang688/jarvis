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
  if (OFFLINE_STATUSES.has(res.status)) return { ok: false, why: "offline", status: res.status };
  let body: unknown = null;
  try { body = await res.json(); } catch { return { ok: false, why: "error", status: res.status }; }
  if (!res.ok) return { ok: false, why: "error", status: res.status, body };
  return { ok: true, body };
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
