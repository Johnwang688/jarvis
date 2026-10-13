// One identity per live HUD page, shared by terminal ownership, SSE and claims.
// It is correlation, not authorization: owner routes/tickets remain the boundary.
// Never recover it from storage; a duplicated/reloaded page must get its own.
export function pageNonce(): string {
  try {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, (x) => x.toString(16).padStart(2, "0")).join("");
  } catch {
    // No guessed or malformed identity: claims stay off and attach stays legacy.
    return "";
  }
}

export const WINDOW_ID = pageNonce();

export function isWindowId(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{32}$/.test(value);
}
