/** Private ticketed auth metadata; credentials never enter this protocol state. */
export interface BrowserAuth { id: string; origin: string; realm: string; scheme: "basic" | "digest"; expiresIn: number }
export type BrowserAuthFrame = { kind: "prompt"; auth: BrowserAuth } | { kind: "closed"; id: string; reason: string };
const id = (v: unknown): v is string => typeof v === "string" && /^[0-9a-f]{32}$/.test(v);
const controls = /[\p{Cc}\p{Cf}\p{Cs}]/u;
export function parseBrowserAuth(raw: unknown): BrowserAuthFrame | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const m = raw as Record<string, unknown>;
  if (!id(m.id)) return null;
  if (m.t === "auth_closed") return ["answered", "cancelled", "expired", "reload", "changed", "refused"].includes(String(m.reason))
    ? { kind: "closed", id: m.id, reason: String(m.reason) } : null;
  if (m.t !== "auth" || (m.scheme !== "basic" && m.scheme !== "digest")
    || typeof m.origin !== "string" || m.origin.length > 2048 || controls.test(m.origin)
    || typeof m.realm !== "string" || [...m.realm].length > 200 || controls.test(m.realm)
    || typeof m.expires_in !== "number" || !Number.isFinite(m.expires_in) || m.expires_in <= 0 || m.expires_in > 60) return null;
  try { const u = new URL(m.origin); if (!["http:", "https:"].includes(u.protocol) || u.origin !== m.origin || u.username || u.password) return null; }
  catch { return null; }
  return { kind: "prompt", auth: { id: m.id, origin: m.origin, realm: m.realm, scheme: m.scheme, expiresIn: m.expires_in } };
}
export function validAuthFields(username: string, secret: string): boolean {
  return !controls.test(username) && !controls.test(secret)
    && new TextEncoder().encode(username).length <= 1024 && new TextEncoder().encode(secret).length <= 4096;
}
