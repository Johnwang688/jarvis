// The Phone settings' daemon routes (phone design PH-6, P-2): owner-only HUD
// routes under /phone (jarvis/v2/remote_auth.py `hud_route`). Every string in
// a passkey's sessions came from the phone (its user agent) or from
// Tailscale's headers (advisory): untrusted, rendered only as text.

export interface PhoneSession {
  id: string;
  ua: string;
  created: number;
  last_seen: number;
  advisory: { login: string; name: string };
}

export interface PhonePasskey {
  key: string;
  created: number;
  last_used: number | null;
  backed_up: boolean;
  sessions: PhoneSession[];
}

export interface PhoneRemote {
  port: number | null;
  bound: boolean;
  host_configured: boolean;
}

export interface PhoneView {
  state: "ok" | "unreadable";
  host_configured: boolean;
  max_passkeys: number;
  pairing: { open: boolean; expires_in: number };
  passkeys: PhonePasskey[];
  remote?: PhoneRemote;
}

export interface PairingWindow {
  url: string;
  expires_in: number;
}

export class PhoneError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function call<T>(path: string, method: "GET" | "POST" = "GET"): Promise<T> {
  const init: RequestInit = method === "POST"
    ? { method, headers: { "Content-Type": "application/json" }, body: "{}" }
    : { method };
  const res = await fetch(path, init);
  let body: any = null;
  try { body = await res.json(); } catch { body = null; }
  if (!res.ok) {
    const message = body && typeof body.error === "string" ? body.error : `HTTP ${res.status}`;
    throw new PhoneError(res.status, message);
  }
  return body as T;
}

export const phoneApi = {
  view: () => call<PhoneView>("/phone"),
  remote: async (): Promise<PhoneRemote | null> => {
    const status = await call<{ remote?: PhoneRemote }>("/status");
    return status && status.remote && typeof status.remote.bound === "boolean" ? status.remote : null;
  },
  pair: () => call<PairingWindow>("/phone/pair", "POST"),
  cancel: () => call<{ open: boolean }>("/phone/pair/cancel", "POST"),
  revoke: (key: string) => call<{ revoked: boolean }>(`/phone/passkeys/${encodeURIComponent(key)}/revoke`, "POST"),
  end: (id: string) => call<{ ended: boolean }>(`/phone/sessions/${encodeURIComponent(id)}/end`, "POST"),
};

/** "4:59" for a countdown in seconds. */
export function countdown(seconds: number): string {
  const s = Math.max(0, Math.ceil(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** "just now", "5 min ago", "3 h ago", "2 d ago": an epoch-seconds time against `now` (ms). */
export function ago(epochSeconds: number, now = Date.now()): string {
  const s = Math.max(0, Math.round(now / 1000 - epochSeconds));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

/** A calendar date for an epoch-seconds time. */
export function day(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}
